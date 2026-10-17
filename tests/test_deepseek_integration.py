import dataclasses
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from local_review.app import create_app
from local_review.config import Settings
from local_review.database import migrate, transaction
from local_review.semantic import judgment_context
from local_review.cli import main
from local_review.security import create_user
from tests.test_ocr_semantics import unfamiliar_tokens


class DeepSeekIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(Path(self.tmp.name), 'device-secret', deepseek_api_key='private-key')
        migrate(self.settings)
        self.client = TestClient(create_app(self.settings))
        self.headers = {'Authorization': 'Device device-secret'}
        self.event('product', 'product.upsert', {'product_version':'p', 'title':'费尔岛羊毛上衣',
                   'style_code':'2024', 'category_json':{'hints':['毛衣']}})
        self.event('fields', 'text_facts.created', {'product_version':'p', 'source':'product_info',
                   'payload_json':{'product_info':{'面料材质':'羊毛','风格':'复古'}}})
        self.event('snapshot', 'snapshot.created', {'snapshot_version':'s','platform_id':'xhs',
                   'category_leaf_id':'c','field_id':'category_path','field_label':'商品类目',
                   'schema_version':'schema','custom_allowed':False,'control_type':'category',
                   'options':[{'value_id':'knit','label':'男装 > 针织衫','position':0}]})

    def event(self, key, kind, payload):
        response = self.client.post('/api/device/events', headers=self.headers,
                                    json={'idempotency_key':key,'event_type':kind,'payload':payload})
        self.assertEqual(response.status_code, 201, response.text)

    def test_attribute_endpoint_receives_excel_product_and_real_candidates(self):
        payload = {'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'category_path',
                   'field_label':'商品类目','control_type':'category','snapshot_version':'s',
                   'excel_value':'男装 > 毛衣/针织衫','evidence':{},
                   'options':[{'value_id':'knit','label':'男装 > 针织衫'}]}
        answer = {'value_ids':['knit'],'confidence':.99,'reason':'Excel 毛衣对应针织衫叶子', 'evidence_kinds':['excel','product']}
        with patch('local_review.semantic.request_json', return_value=answer) as call:
            unauthorized = self.client.post('/api/device/semantic-judge', json=payload)
            self.assertEqual(unauthorized.status_code, 401)
            call.assert_not_called()
            response = self.client.post('/api/device/semantic-judge', headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['value_ids'], ['knit'])
        context = call.call_args.args[2]['product_and_learning']
        self.assertEqual(context['title'], '费尔岛羊毛上衣')
        self.assertEqual(context['product_info']['面料材质'], '羊毛')
        self.assertNotIn('private-key', response.text)
        with transaction(self.settings) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM review_actions").fetchone()[0], 0)

    def test_multi_exact_mapping_retries_invalid_provenance_with_full_groups(self):
        from local_review.semantic import judge_attribute
        payload = {'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'audience',
            'field_label':'适用对象','control_type':'multi_select','snapshot_version':'audience-s',
            'excel_value':'通用/青年,中年/青中年',
            'evidence':{'full_excel_value':'通用/青年,中年/青中年',
                        'requested_groups':[['通用','青年'],['中年','青中年']],
                        'validation_error':'dropdown is hidden','bounded_write_retry':True},
            'options':[{'value_id':'all','label':'通用'},{'value_id':'young','label':'青年'},
                       {'value_id':'middle','label':'中年'}]}
        wrong = {'value_ids':['all','middle'],'confidence':.72,'reason':'误记历史回读为人工',
                 'evidence_kinds':['excel','human_learning']}
        # The legacy requested_groups cannot override the first complete slash alternative.
        good = {**wrong,'value_ids':['all'],'reason':'首个完整可用方案：通用','evidence_kinds':['excel'],
                'assessment':{'relation':'equivalent','source_text':payload['excel_value'],
                              'assumptions':[],'conflicts':[],'competing_value_ids':[]}}
        with patch('local_review.semantic.request_json', side_effect=[wrong,good]) as call:
            result = judge_attribute(self.settings,payload)
        self.assertEqual(result['status'],'suggested')
        self.assertEqual(result['value_ids'],['all'])
        self.assertEqual(call.call_count,2)
        sent=call.call_args.args[2]
        self.assertEqual([o['value_id'] for o in sent['request']['options']],['all'])
        self.assertNotIn('validation_error',sent['request']['evidence'])
        self.assertEqual(sent['execution_state']['validation_error'],'dropdown is hidden')
        self.assertIn('human_learning_without_records',sent['validation_feedback'])

    def test_unknown_model_candidate_still_rejected_after_one_retry(self):
        from local_review.semantic import judge_attribute
        payload={'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'category_path',
                 'field_label':'商品类目','control_type':'category','snapshot_version':'s',
                 'excel_value':'毛衣','evidence':{},'options':[{'value_id':'knit','label':'针织衫'}]}
        wrong={'value_ids':['invented'],'confidence':.99,'reason':'候选不存在','evidence_kinds':['excel']}
        with patch('local_review.semantic.request_json',return_value=wrong) as call:
            result=judge_attribute(self.settings,payload)
        self.assertEqual(result['reason_code'],'deepseek_invalid_judgment')
        self.assertEqual(call.call_count,2)
        self.assertIn('candidate_not_in_live_options',result['reason'])

    def test_multiselect_learning_excludes_single_and_reuses_confirmed_combination(self):
        from local_review.semantic import judge_attribute
        with transaction(self.settings) as connection:
            create_user(connection,'admin','1234',role='admin')
        headers={'Origin':'http://testserver'}
        self.client.post('/api/login',headers=headers,json={'username':'admin','password':'1234'})
        intent='通用/青年,中年/青中年'
        options=[{'value_id':'all','label':'通用','position':0},
                 {'value_id':'young','label':'青年','position':1},
                 {'value_id':'middle','label':'中年','position':2}]
        for kind, selected in [('select','all'),('multi_select','young,middle')]:
            self.event(kind+'-snapshot','snapshot.created',{'snapshot_version':kind,
                'platform_id':'xhs','category_leaf_id':'c','field_id':'audience','field_label':'适用对象',
                'schema_version':kind,'custom_allowed':False,'control_type':kind,'options':options})
            self.event(kind+'-review','review.created',{'id':kind,'run_id':'run','device_id':'device',
                'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'audience',
                'field_label':'适用对象','canonical_field':None,'snapshot_version':kind,
                'reason_code':'uncertain','suggested_value_id':None,
                'evidence_json':{'reuse_context':{'control_type':kind,'excel_candidates':intent.split('/')}}})
            claimed=self.client.post('/api/reviews/'+kind+'/claim',headers=headers,json={'version':1}).json()
            result=self.client.post('/api/reviews/'+kind+'/confirm',headers=headers,
                json={'version':claimed['version'],'final_value_id':selected})
            self.assertEqual(result.status_code,200,result.text)
        context=judgment_context(self.settings,'p','xhs','适用对象','multi_select',intent,'c')
        self.assertEqual([e['review_id'] for e in context['human_examples']],['multi_select'])
        self.assertEqual(set(context['human_examples'][0]['selected_labels']),{'青年','中年'})
        payload={'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'audience',
            'field_label':'适用对象','control_type':'multi_select','snapshot_version':'multi_select',
            'excel_value':intent,'evidence':{'requested_groups':[['通用','青年'],['中年','青中年']]},
            'options':[{k:o[k] for k in ('value_id','label')} for o in options]}
        with patch('local_review.semantic.request_json') as call:
            result=judge_attribute(self.settings,payload)
        call.assert_not_called()
        self.assertEqual(set(result['value_ids']),{'young','middle'})
        self.assertFalse(result['api_called'])
        self.assertEqual(result['learning_review_ids'],['multi_select'])

    def test_verified_model_experience_is_context_and_never_a_human_vote(self):
        from local_review.service import _condition_signature
        self.event('excel-evidence', 'text_facts.created', {'product_version':'p', 'source':'excel',
            'payload_json': {'text_tokens':['羊毛'], 'values':{'fabric':{'value_id':'wool'}}}})
        with transaction(self.settings) as connection:
            signature = _condition_signature(connection, 'p')
        self.event('model-experience', 'text_facts.created', {'product_version':'p', 'source':'model_verified',
            'payload_json': {'model_verified': {'platform_id':'xhs', 'field_label':'商品类目',
                'control_type':'category', 'excel_value':'毛衣', 'selected_labels':['男装 > 针织衫'],
                'readback_verified':True, 'model':'deepseek-flash'}}})
        context = judgment_context(self.settings, 'p', 'xhs', '商品类目', 'category')
        self.assertEqual(context['verified_model_examples'][0]['selected_labels'], ['男装 > 针织衫'])
        self.assertEqual(context['human_examples'], [])
        self.assertEqual(judgment_context(self.settings, 'p', 'jd', '商品类目', 'category')['verified_model_examples'], [])
        with transaction(self.settings) as connection:
            self.assertEqual(_condition_signature(connection, 'p'), signature)

    def test_conflicting_or_no_fill_human_records_do_not_auto_reuse_combination(self):
        from local_review.semantic import judge_attribute
        payload={'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'audience',
            'field_label':'适用对象','control_type':'multi_select','snapshot_version':'s',
            'excel_value':'青年,中年','evidence':{},
            'options':[{'value_id':'young','label':'青年'},{'value_id':'middle','label':'中年'}]}
        base={'review_id':'r','same_product':True,'same_category':True,'control_type':'multi_select',
              'excel_candidates':['青年,中年'],'selected_labels':['青年','中年'],'no_fill':False}
        for other in ({**base,'review_id':'r2','selected_labels':['青年']},
                      {**base,'review_id':'r2','selected_labels':[],'no_fill':True}):
            with self.subTest(other=other), \
                 patch('local_review.semantic.judgment_context',return_value={'human_examples':[base,other]}), \
                 patch('local_review.semantic.request_json',return_value={'value_ids':[],'reason':'人工记录冲突，无法确定'}) as call:
                result=judge_attribute(self.settings,payload)
                self.assertEqual(result['reason_code'],'deepseek_uncertain')
                self.assertEqual(call.call_count,1)

    def test_other_product_multi_learning_is_not_excluded_by_excel_first_alternative(self):
        from local_review.semantic import judge_attribute
        intent='通用/青年,中年/青中年'
        options=[{'value_id':'all','label':'通用'},{'value_id':'young','label':'青年'},
                 {'value_id':'middle','label':'中年'}]
        payload={'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'audience',
            'field_label':'适用对象','control_type':'multi_select','snapshot_version':'s',
            'excel_value':intent,'options':options,
            'evidence':{'requested_groups':[['通用','青年'],['中年','青中年']]}}
        context={'human_examples':[{'review_id':'other','same_product':False,'same_category':True,
            'control_type':'multi_select','excel_candidates':intent.split('/'),
            'selected_labels':['青年','中年'],'no_fill':False}]}
        answer={'value_ids':['young','middle'],'confidence':.9,'reason':'参考同类商品多选人工组合',
                'evidence_kinds':['excel','human_learning']}
        with patch('local_review.semantic.judgment_context',return_value=context), \
             patch('local_review.semantic.request_json',return_value=answer) as call:
            result=judge_attribute(self.settings,payload)
        self.assertEqual(call.call_args.args[2]['request']['options'],options)
        self.assertEqual(result['value_ids'],['young','middle'])

    def test_ocr_endpoint_only_accepts_real_non_numeric_row_ids(self):
        payload = {'product_version':'p','garment_kind':'clothing',
                   'tokens':[dataclasses.asdict(t) for t in unfamiliar_tokens()],
                   'label_token_ids':[0], 'reason':'unknown label'}
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
            {'suggestions':[{'token_id':0,'meaning':'length','confidence':.99}]})}}]}
        with patch('local_review.deepseek.httpx.post', return_value=response) as call:
            result = self.client.post('/api/device/ocr-judge', headers=self.headers, json=payload)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['status'], 'suggested')
            self.assertIn('费尔岛羊毛上衣', call.call_args.kwargs['json']['messages'][1]['content'])
            call.reset_mock()
            result = self.client.post('/api/device/ocr-judge', headers=self.headers,
                                      json={**payload,'label_token_ids':[6]})
            self.assertEqual(result.status_code, 400)
            call.assert_not_called()

    def test_stale_or_modified_snapshot_does_not_call_model(self):
        payload = {'product_version':'p','platform_id':'xhs','category_leaf_id':'c','field_id':'category_path',
                   'field_label':'商品类目','control_type':'category','snapshot_version':'s',
                   'excel_value':'毛衣','evidence':{}, 'options':[{'value_id':'changed','label':'男装 > 工装裤'}]}
        with patch('local_review.semantic.request_json') as call:
            response = self.client.post('/api/device/semantic-judge', headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 422)
            call.assert_not_called()

    def test_configure_key_is_private_hidden_and_persists_for_service(self):
        with patch.dict(os.environ, {'KUAIMAI_REVIEW_DATA_DIR':self.tmp.name}, clear=True), patch('local_review.cli.getpass.getpass', return_value='private-key'), patch('builtins.print') as output:
            self.assertEqual(main(['configure-deepseek']), 0)
            settings = Settings.from_env()
        config = Path(self.tmp.name) / 'deepseek.json'
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(settings.deepseek_api_key, 'private-key')
        self.assertNotIn('private-key', str(output.call_args_list))

    def test_service_reloads_key_file_and_allows_disabling_without_restart(self):
        config=Path(self.tmp.name)/'deepseek.json'
        config.write_text(json.dumps({'api_key':'new-private-key','model':'deepseek-v4-pro'}))
        with patch.dict(os.environ, {}, clear=True):
            settings=self.settings.with_deepseek_configuration()
            self.assertEqual((settings.deepseek_api_key,settings.deepseek_model),('new-private-key','deepseek-v4-pro'))
            config.write_text(json.dumps({'api_key':'','model':'deepseek-flash'}))
            self.assertEqual(settings.with_deepseek_configuration().deepseek_api_key,'')

    def test_ocr_review_cannot_confirm_invented_or_multiple_meanings(self):
        self.event('ocr-snapshot', 'snapshot.created', {'snapshot_version':'ocr-s','platform_id':'base',
                   'category_leaf_id':'ocr:clothing','field_id':'ocr:length','field_label':'尺码 OCR 含义 · 衣身丈',
                   'schema_version':'schema','custom_allowed':False,'control_type':'ocr_semantic',
                   'options':[{'value_id':'length','label':'衣长','position':0},{'value_id':'chest','label':'胸围','position':1}]})
        self.event('ocr-review','review.created', {'id':'ocr-review','run_id':'run','device_id':'device',
                   'product_version':'p','platform_id':'base','category_leaf_id':'ocr:clothing','field_id':'ocr:length',
                   'field_label':'尺码 OCR 含义 · 衣身丈','canonical_field':None,'snapshot_version':'ocr-s',
                   'reason_code':'uncertain','suggested_value_id':None,'evidence_json':{}})
        with transaction(self.settings) as connection:
            create_user(connection,'admin','1234',role='admin')
        headers={'Origin':'http://testserver'}
        self.assertEqual(self.client.post('/api/login',headers=headers,json={'username':'admin','password':'1234'}).status_code,200)
        task=self.client.get('/api/reviews').json()['tasks'][0]
        self.assertFalse(task['custom_allowed'])
        claimed=self.client.post('/api/reviews/ocr-review/claim',headers=headers,json={'version':1}).json()
        for value in ('invented','length,chest'):
            response=self.client.post('/api/reviews/ocr-review/confirm',headers=headers,
                                      json={'version':claimed['version'],'final_value_id':value})
            self.assertEqual(response.status_code,422,response.text)


if __name__ == '__main__':
    unittest.main()
