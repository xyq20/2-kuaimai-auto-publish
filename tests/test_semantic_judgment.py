import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from attribute_runtime import AttributeRuntime, ReviewRequired
from learning_models import CandidateValue
from learning_store import LearningStore
from local_review.config import Settings
from tests.test_attribute_runtime import make_length_request


class SemanticJudgmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_style_learning_can_use_model_operator_preference(self):
        request = dataclasses.replace(self.request, field_id='style', field_label='风格',
            excel_value='美式休闲风', candidates=(CandidateValue('casual', '休闲风'), CandidateValue('american', '美式')))
        self.client.review_learning.return_value = {'status': 'review_required', 'reason_code': 'review_learning_conflict'}
        self.suggestion(.72, value_ids=['casual'], reason='两种风格都合理，本商品优先休闲风',
            assessment={'relation': 'preference', 'source_text': '美式休闲风',
                'acceptable_value_ids': ['casual', 'american'], 'assumptions': [],
                'conflicts': [], 'competing_value_ids': []})
        self.runtime.begin_review_collection('wxsph')
        result = await self.runtime.resolve(request)
        self.runtime.record_verified_readbacks('wxsph', {'风格': ['休闲风']})
        await self.runtime.drain()
        self.assertIsNotNone(result)
        self.assertEqual(result.label, '休闲风')
        self.assertFalse(self.runtime.deferred_reviews)
        self.assertEqual(self.runtime.model_decisions[0]['acceptable_labels'], ['休闲风', '美式'])
        facts = [json.loads(row[0]) for row in self.store.connection.execute(
            "SELECT payload_json FROM sync_outbox WHERE event_type='text_facts.created'")]
        model_fact = next(f for f in facts if f.get('source') == 'model_verified')
        self.assertEqual(model_fact['payload_json']['model_verified']['acceptable_labels'], ['休闲风', '美式'])

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LearningStore(Path(self.tmp.name) / 'learning.sqlite3')
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.client = Mock()
        self.client.post_event.return_value = {'ok': True}
        self.client.review_learning.return_value = {'status': 'review_required'}
        self.client.decide.return_value = {'status': 'review_required', 'reason_code': 'insufficient_evidence'}
        self.request = make_length_request(field_id='cut', field_label='版型', excel_value='平直廓形',
                                           candidates=(CandidateValue('straight', '直筒'), CandidateValue('loose', '宽松')))
        self.runtime = AttributeRuntime(self.store, self.client, 'run', 'product')

    def suggestion(self, confidence=.99, **overrides):
        def judge(payload):
            return {'status': 'suggested', 'source': 'deepseek',
                    'snapshot_version': payload['snapshot_version'],
                    'value_ids': ['straight'], 'confidence': confidence,
                    'reason': 'Excel 平直廓形与候选直筒含义一致',
                    'assessment': {'relation':'equivalent','source_text':payload['excel_value'],
                                   'assumptions':[],'conflicts':[],'competing_value_ids':[]},
                    'evidence_kinds': ['excel'], **overrides}
        self.client.semantic_judge.side_effect = judge

    async def test_unmatched_excel_uses_model_and_records_trace_without_human_sample(self):
        import kuaimai_erp as erp
        from types import SimpleNamespace
        from ocr_semantics import OCRSemanticSession
        root=Path(self.tmp.name)
        self.runtime.model_decision_sink=lambda: erp.write_model_judgment_report(root,SimpleNamespace(attribute_runtime=self.runtime),OCRSemanticSession())
        self.suggestion()
        result = await self.runtime.resolve(self.request)
        self.assertEqual((result.value_id, result.source), ('straight', 'deepseek'))
        await self.runtime.drain()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM review_resolutions').fetchone()[0], 0)
        trace = self.runtime.model_decisions[0]
        self.assertEqual((trace['field_label'], trace['excel_value'], trace['selected_labels']), ('版型', '平直廓形', ['直筒']))
        self.assertEqual(trace['status'], 'adopted')
        self.assertIn('含义一致', trace['reason'])
        self.assertEqual(json.loads((root/'model-judgments.json').read_text())['decisions'][0]['selected_labels'],['直筒'])

    async def test_uncertain_or_unverifiable_prediction_enters_review(self):
        for confidence, overrides in ((.7, {}), (.99, {'value_ids': ['invented']}),
                                      (.99, {'snapshot_version': 'stale'}),
                                      (.99, {'evidence_kinds': []}),
                                      (.99, {'value_ids': ['straight', 'loose']})):
            with self.subTest(confidence=confidence, overrides=overrides):
                self.suggestion(confidence, **overrides)
                self.runtime._model_judgments.clear()
                with self.assertRaises(ReviewRequired):
                    await self.runtime.resolve(self.request)

    async def test_exact_excel_and_operator_confirmation_take_precedence(self):
        self.suggestion()
        await self.runtime.resolve(dataclasses.replace(self.request, excel_value='直筒'))
        self.client.semantic_judge.assert_not_called()
        from learning_models import CandidateSnapshot
        request = self.request
        snapshot = CandidateSnapshot(request.platform_id, request.category_leaf_id, request.field_id,
                                     request.field_label, request.candidates, request.schema_version, False)
        self.store.save_candidate_snapshot(snapshot)
        self.store.save_review_resolution(product_version='product', platform_id='wxsph',
                                         snapshot_version=snapshot.snapshot_version, review_id='review', final_value_id='loose')
        result = await self.runtime.resolve(request)
        self.assertEqual(result.value_id, 'loose')
        self.client.semantic_judge.assert_not_called()
        await self.runtime.drain()

    async def test_failed_confirmed_write_uses_original_intent_for_model_recovery(self):
        from learning_models import CandidateSnapshot
        snapshot = CandidateSnapshot(self.request.platform_id, self.request.category_leaf_id,
            self.request.field_id, self.request.field_label, self.request.candidates,
            self.request.schema_version, False)
        self.store.save_candidate_snapshot(snapshot)
        self.store.save_review_resolution(product_version='product', platform_id='wxsph',
            snapshot_version=snapshot.snapshot_version, review_id='old', final_value_id='straight')
        approved = await self.runtime.resolve(self.request)
        self.assertEqual(approved.label, '直筒')
        self.suggestion()

        recovered = await self.runtime.recover_failed_write(
            platform_id='wxsph', field_label='版型', error='选择后页面仍为空')
        await self.runtime.drain()

        self.assertEqual((recovered.label, recovered.source), ('直筒', 'deepseek'))
        trace = self.runtime.model_decisions[0]
        self.assertEqual(trace['excel_value'], '平直廓形')
        self.assertEqual(trace['input_evidence']['failed_selection'], '直筒')
        self.assertEqual(trace['input_evidence']['validation_error'], '选择后页面仍为空')
        self.assertEqual(trace['selected_labels'], ['直筒'])

    async def test_write_recovery_does_not_silently_replace_human_choice(self):
        from attribute_runtime import ResolvedAttribute
        self.runtime.remember_resolved(self.request,
            ResolvedAttribute('loose', '宽松', 'human_override', 'old'))
        self.runtime.begin_review_collection('wxsph')
        self.suggestion()
        result = await self.runtime.recover_failed_write(
            platform_id='wxsph', field_label='版型', error='候选点击失败')
        await self.runtime.drain()
        self.assertIsNone(result)
        self.assertEqual(len(self.runtime.deferred_reviews), 1)
        self.assertEqual(self.runtime.model_decisions[0]['status'], 'review_required')

    async def test_uncertain_write_recovery_queues_review_with_failure_evidence(self):
        await self.runtime.resolve(dataclasses.replace(self.request, excel_value='直筒'))
        self.runtime.begin_review_collection('wxsph')
        self.suggestion(.7)

        result = await self.runtime.recover_failed_write(
            platform_id='wxsph', field_label='版型', error='回读为空')
        await self.runtime.drain()

        self.assertIsNone(result)
        self.assertEqual(len(self.runtime.deferred_reviews), 1)
        review = self.runtime.deferred_reviews[0]
        self.assertEqual(review.reason_code, 'platform_write_failed')
        self.assertEqual(review.request.evidence['validation_error'], '回读为空')
        self.assertEqual(tuple(v.label for v in review.request.candidates), ('直筒', '宽松'))
        self.assertEqual(self.runtime.model_events[-1]['status'], 'review_required')

    async def test_failed_model_retry_queues_review_without_another_model_request(self):
        await self.runtime.resolve(dataclasses.replace(self.request, excel_value='直筒'))
        self.runtime.begin_review_collection('wxsph')
        self.suggestion()
        await self.runtime.recover_failed_write(
            platform_id='wxsph', field_label='版型', error='候选点击失败')
        # A second API request would now fail: retry failure must reuse the
        # judgment already made and queue review, rather than loop.
        self.client.semantic_judge.side_effect = AssertionError('unexpected second model request')

        result = await self.runtime.recover_failed_write(
            platform_id='wxsph', field_label='版型', error='重试回读仍为空', force_review=True)
        await self.runtime.drain()

        self.assertIsNone(result)
        self.assertEqual(len(self.runtime.deferred_reviews), 1)
        row = self.store.connection.execute(
            "SELECT payload_json FROM sync_outbox WHERE event_type='review.created'").fetchone()
        payload = json.loads(row[0])
        self.assertEqual(payload['suggested_value_id'], 'straight')
        self.assertEqual(payload['evidence_json']['validation_error'], '重试回读仍为空')
        self.assertEqual(self.runtime.model_decisions[0]['status'], 'review_required')
        self.assertEqual(self.runtime.model_decisions[0]['readback_status'], 'write_failed')

    async def test_category_requires_excel_or_confirmed_library_evidence(self):
        self.request = dataclasses.replace(self.request, field_id='__category__', field_label='商品类目',
                                          excel_value='男装 > 毛衣/针织衫', control_type='category')
        self.suggestion(evidence_kinds=['product'])
        with self.assertRaises(ReviewRequired):
            await self.runtime.resolve(self.request)
        self.suggestion(evidence_kinds=['excel', 'human_learning'])
        self.runtime._model_judgments.clear()
        self.assertEqual((await self.runtime.resolve(self.request)).source, 'deepseek')
        await self.runtime.drain()

    async def test_failed_model_request_is_visible_in_log_and_report(self):
        from types import SimpleNamespace
        import kuaimai_erp as erp
        from ocr_semantics import OCRSemanticSession
        self.client.semantic_judge.return_value = {
            'status': 'review_required', 'source': 'deepseek', 'api_called': True,
            'reason_code': 'deepseek_timeout', 'model': 'deepseek-flash',
        }
        with self.assertLogs('kuaimai_erp', level='INFO') as logs:
            with self.assertRaises(ReviewRequired):
                await self.runtime.resolve(self.request)
        erp.write_model_judgment_report(Path(self.tmp.name), SimpleNamespace(attribute_runtime=self.runtime), OCRSemanticSession())
        report = json.loads((Path(self.tmp.name)/'model-judgments.json').read_text())
        self.assertEqual(report['summary']['api_call_count'], 1)
        self.assertEqual(report['summary']['unavailable_count'], 1)
        self.assertEqual(report['events'][0]['field_label'], '版型')
        self.assertIn('超时', '\n'.join(logs.output))
        self.assertEqual(report['decisions'], [])

    async def test_uncertain_result_logs_suggestion_and_review_reason(self):
        self.suggestion(.7, api_called=True, model='deepseek-flash')
        with self.assertLogs('kuaimai_erp', level='INFO') as logs:
            with self.assertRaises(ReviewRequired):
                await self.runtime.resolve(self.request)
        text = '\n'.join(logs.output)
        self.assertIn('直筒', text)
        self.assertIn('70.0%', text)
        self.assertIn('审核', text)

    async def test_excel_match_explains_why_model_is_skipped(self):
        with self.assertLogs('kuaimai_erp', level='INFO') as logs:
            await self.runtime.resolve(dataclasses.replace(self.request, excel_value='直筒'))
            await self.runtime.drain()
        self.assertIn('Excel', '\n'.join(logs.output))
        self.assertEqual(self.runtime.model_events[0]['api_called'], False)
        self.assertEqual(self.runtime.model_events[0]['status'], 'skipped')

    async def test_equivalence_at_ninety_percent_adopts_without_human_review(self):
        self.suggestion(.90)
        result = await self.runtime.resolve(self.request)
        await self.runtime.drain()
        self.assertEqual(result.source, 'deepseek')
        self.assertEqual(self.runtime.model_decisions[0]['adoption_policy']['threshold'], .9)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM sync_outbox WHERE event_type='review.created'").fetchone()[0],0)

    async def test_high_score_with_competing_candidate_goes_to_review(self):
        self.suggestion(.99, assessment={'relation':'equivalent','source_text':self.request.excel_value,
                        'assumptions':[],'conflicts':[],'competing_value_ids':['loose']})
        with self.assertRaises(ReviewRequired):
            await self.runtime.resolve(self.request)
        await self.runtime.drain()
        self.assertIn('其他可能候选', self.runtime.model_decisions[0]['treatment_reason'])

    async def test_verified_model_results_are_stored_separately_from_human_votes(self):
        self.suggestion(api_called=True)
        await self.runtime.resolve(self.request)
        self.runtime.record_verified_readbacks('wxsph', {'版型': ['直筒']})
        await self.runtime.drain()
        facts = [json.loads(row['payload_json']) for row in self.store.connection.execute(
            "SELECT payload_json FROM sync_outbox WHERE event_type='text_facts.created'"
        )]
        model_facts = [fact for fact in facts if fact.get('source') == 'model_verified']
        self.assertEqual(model_facts[0]['payload_json']['model_verified']['selected_labels'], ['直筒'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM review_resolutions').fetchone()[0], 0)


class SemanticEndpointTests(unittest.TestCase):
    def test_style_operator_assessment_keeps_multiple_acceptable_candidates(self):
        from local_review.semantic import judge_attribute
        from semantic_policy import adoption_policy
        from types import SimpleNamespace
        settings = Settings(Path('unused'), 'device', deepseek_api_key='private')
        payload = {'product_version': 'p', 'platform_id': 'tm', 'category_leaf_id': 'c',
            'field_id': 'style', 'field_label': '风格', 'control_type': 'select',
            'snapshot_version': 's', 'excel_value': '美式休闲风',
            'options': [{'value_id': 'casual', 'label': '休闲风'}, {'value_id': 'american', 'label': '美式'}],
            'evidence': {}}
        context = {'title': '宽松圆领针织衫', 'product_info': {}, 'human_examples': []}
        answer = {'value_ids': ['casual'], 'confidence': .72, 'reason': '两个都合理，结合日常穿着优先休闲风',
            'evidence_kinds': ['excel', 'product'],
            'supporting_quotes': [{'source': 'product', 'text': '宽松圆领针织衫'}],
            'assessment': {'relation': 'preference', 'source_text': '美式休闲风',
                'acceptable_value_ids': ['casual', 'american'], 'assumptions': [],
                'conflicts': [], 'competing_value_ids': []}}
        with patch('local_review.semantic.judgment_context', return_value=context), \
             patch('local_review.semantic.request_json', return_value=answer):
            result = judge_attribute(settings, payload)
        request = SimpleNamespace(field_label='风格', control_type='select',
            excel_value='美式休闲风', evidence={},
            candidates=(CandidateValue('casual', '休闲风'), CandidateValue('american', '美式')))
        self.assertTrue(adoption_policy(result, request)['auto_adopt'])
        self.assertEqual(result['assessment']['acceptable_value_ids'], ['casual', 'american'])
        self.assertEqual(result['grounded_support'][0]['text'], '宽松圆领针织衫')

    def test_write_failure_requests_fresh_model_instead_of_reusing_old_rule(self):
        from local_review.semantic import judge_attribute
        settings = Settings(Path('unused'), 'device', deepseek_api_key='private')
        payload = {'product_version': 'p', 'platform_id': 'tm', 'category_leaf_id': 'c',
            'field_id': 'detail', 'field_label': '款式细节', 'control_type': 'select',
            'snapshot_version': 's', 'excel_value': '针织',
            'options': [{'value_id': 'a', 'label': '提花'}],
            'evidence': {'validation_error': '打开下拉框后没有可见选项'}}
        context = {'title': '提花毛衣', 'product_info': {}, 'human_examples': [],
                   'history_summary': {'human_records_matched': 1}}
        cached = ('old-rule', {'selected_labels': ['提花']}, {'review_ids': ['r']})
        answer = {'value_ids': ['a'], 'confidence': .95, 'reason': '商品资料支持提花，需重试点击',
            'evidence_kinds': ['excel', 'product'],
            'assessment': {'relation': 'inferred', 'source_text': '针织',
                'assumptions': [], 'conflicts': [], 'competing_value_ids': []}}
        with patch('local_review.semantic.judgment_context', return_value=context), \
             patch('local_review.model_rules.reuse', return_value=cached), \
             patch('local_review.model_rules.learned_guidance', return_value=[]), \
             patch('local_review.model_rules.save_proposal'), \
             patch('local_review.semantic.request_json', return_value=answer):
            result = judge_attribute(settings, payload)
        self.assertTrue(result['api_called'])
        self.assertEqual(result['reason'], '商品资料支持提花，需重试点击')

    def test_inference_quotes_are_checked_and_same_product_human_conflict_is_attached(self):
        from local_review.semantic import judge_attribute
        settings=Settings(Path('unused'),'device',deepseek_api_key='private')
        payload={'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'style',
                 'field_label':'风格','control_type':'select','snapshot_version':'s','excel_value':'休闲风',
                 'options':[{'value_id':'a','label':'美式休闲'}],'evidence':{}}
        context={'title':'商品提花毛衣','product_info':{},'human_examples':[{'review_id':'r','same_product':True,
                 'excel_candidates':['休闲风'],'selected_labels':['商务休闲']}]}
        answer={'value_ids':['a'],'confidence':.99,'reason':'推断','evidence_kinds':['excel','product'],
                'assessment':{'relation':'inferred','source_text':'休闲风','assumptions':[],'conflicts':[],'competing_value_ids':[]},
                'supporting_quotes':[{'source':'product','text':'不存在的原文'}]}
        with patch('local_review.semantic.judgment_context',return_value=context),patch('local_review.semantic.request_json',return_value=answer):
            result=judge_attribute(settings,payload)
        self.assertEqual(result['grounded_support'],[])
        self.assertTrue(result['assessment']['conflicts'])

    def test_api_validates_candidate_and_keeps_human_records_as_context(self):
        from local_review.semantic import judge_attribute
        settings = Settings(Path('unused'), 'device', deepseek_api_key='private')
        payload = {'product_version': 'p', 'platform_id': 'xhs', 'category_leaf_id': 'c',
                   'field_id': '__category__', 'field_label': '商品类目', 'control_type': 'category',
                   'snapshot_version': 'snapshot', 'excel_value': '男装 > 毛衣/针织衫',
                   'options': [{'value_id': 'knit', 'label': '男装 > 针织衫'}], 'evidence': {}}
        context = {'title': '费尔岛羊毛上衣', 'category': {'hints': ['毛衣']},
                   'human_examples': [{'review_id': 'r', 'source': 'human', 'selected_labels': ['男装 > 针织衫']}]}
        answer = {'value_ids': ['knit'], 'confidence': .99, 'reason': '类目匹配', 'evidence_kinds': ['excel', 'human_learning']}
        with patch('local_review.semantic.judgment_context', return_value=context), patch('local_review.semantic.request_json', return_value=answer) as call:
            result = judge_attribute(settings, payload)
        self.assertEqual(result['value_ids'], ['knit'])
        self.assertEqual(result['learning_review_ids'], ['r'])
        self.assertIn('毛衣', json.dumps(call.call_args.args[2], ensure_ascii=False))
        answer['value_ids'] = ['invented']
        with patch('local_review.semantic.judgment_context', return_value=context), patch('local_review.semantic.request_json', return_value=answer):
            self.assertEqual(judge_attribute(settings, payload)['status'], 'review_required')


if __name__ == '__main__':
    unittest.main()
