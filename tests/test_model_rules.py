import tempfile
import unittest
from pathlib import Path
from local_review.config import Settings
from local_review.database import migrate, connect
from local_review.model_rules import validate, save_proposal, reuse


class ModelRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(Path(self.tmp.name), 'device')
        migrate(self.settings)
        self.data = dict(platform_id='douyin', category_leaf_id='knit', field_label='风格',
                         control_type='select', excel_value='美式休闲风', evidence={},
                         options=[{'value_id': 'new-id', 'label': '美式休闲'}])
        self.context = {'human_examples': [dict(review_id=str(i), style_code=str(i), same_category=True,
                        excel_candidates=['美式休闲风'], control_type='select', no_fill=False,
                        selected_labels=['美式休闲']) for i in range(3)],
                        'history_summary': {'human_records_matched': 3}}
        self.proposal = dict(relation='equivalent', selected_labels=['美式休闲'], exceptions=[])

    def test_proposal_reuse_and_correction_suspends(self):
        saved = save_proposal(self.settings, self.data, self.context, self.proposal)
        self.assertTrue(saved['passed'])
        self.assertEqual(reuse(self.settings, self.data, self.context)[0], saved['id'])
        self.context['human_examples'][0]['no_fill'] = True
        self.assertIsNone(reuse(self.settings, self.data, self.context))
        with connect(self.settings) as c:
            self.assertEqual(c.execute('SELECT status FROM model_learning_rules').fetchone()[0], 'suspended')
        self.context['human_examples'][0]['no_fill'] = False
        self.assertIsNone(reuse(self.settings, self.data, self.context))

    def test_verified_rule_skips_model_api_and_keeps_current_candidate_id(self):
        from dataclasses import replace
        from unittest.mock import patch
        from local_review.semantic import judge_attribute
        save_proposal(self.settings, self.data, self.context, self.proposal)
        request = {**self.data, "product_version":"p", "field_id":"style", "snapshot_version":"snapshot"}
        with patch('local_review.semantic.judgment_context', return_value=self.context), patch('local_review.semantic.request_json') as call:
            result = judge_attribute(replace(self.settings, deepseek_api_key='test'), request)
        call.assert_not_called()
        self.assertFalse(result['api_called'])
        self.assertEqual(result['value_ids'], ['new-id'])

    def test_boundary_gates(self):
        for change in ({'field_label':'适用年龄'}, {'control_type':'category'},
                       {'excel_value':'26-40'}, {'evidence':{'selection_only':True}},
                       {'evidence':{'validation_error':'save failed'}},
                       {'options':[{'value_id':'a','label':'美式休闲'},{'value_id':'b','label':'美式休闲'}]}):
            with self.subTest(change=change):
                self.assertFalse(validate({**self.data, **change}, self.context, self.proposal)['passed'])
        self.context['human_examples'][1]['style_code'] = '0'
        self.assertFalse(validate(self.data, self.context, self.proposal)['passed'])

    def test_scope_is_exact_and_history_truncation_blocks(self):
        save_proposal(self.settings, self.data, self.context, self.proposal)
        for key in ('platform_id', 'category_leaf_id', 'field_label', 'control_type', 'excel_value'):
            self.assertIsNone(reuse(self.settings, {**self.data,key:'different'}, self.context))
        self.context['history_summary']['human_records_matched'] = 99
        self.assertIsNone(reuse(self.settings, self.data, self.context))

    def test_complex_summary_is_available_without_review_but_never_auto_executes(self):
        from local_review.model_rules import learned_guidance
        data = {**self.data, 'field_label': '适用年龄'}
        result = save_proposal(self.settings, data, self.context, self.proposal)
        self.assertFalse(result['passed'])
        self.assertIsNone(reuse(self.settings, data, self.context))
        summary = learned_guidance(self.settings, data, self.context)
        self.assertEqual(summary[0]['source'], 'model_summary')
        self.assertFalse(summary[0]['human_confirmed'])
        self.context['human_examples'][0]['no_fill'] = True
        self.assertEqual(learned_guidance(self.settings, data, self.context), [])
