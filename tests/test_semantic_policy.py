import unittest
from types import SimpleNamespace
from semantic_policy import adoption_policy


class SemanticPolicyTests(unittest.TestCase):
    def decide(self, confidence, relation='equivalent', control='select', **assessment):
        request = SimpleNamespace(control_type=control, excel_value='原文', evidence={})
        answer = {'confidence':confidence, 'evidence_kinds':['excel','product'],
                  'grounded_support':[{'source':'product','text':'商品原文'}],
                  'assessment':{'relation':relation,'source_text':'原文','assumptions':[],
                                'conflicts':[],'competing_value_ids':[],**assessment}}
        return adoption_policy(answer, request)

    def test_thresholds_depend_on_meaning_and_category_scope(self):
        for score, relation, control, expected in [(.90,'equivalent','select',True),
                (.899,'equivalent','select',False),(.95,'inferred','select',True),
                (.949,'inferred','select',False),(.94,'equivalent','category',False),
                (.95,'equivalent','category',True)]:
            with self.subTest(score=score, relation=relation, control=control):
                self.assertEqual(self.decide(score,relation,control)['auto_adopt'],expected)

    def test_high_confidence_never_overrides_conflicts_assumptions_or_ambiguity(self):
        for details in ({'conflicts':['人工已纠正']},{'assumptions':['假定为羊毛']},
                        {'competing_value_ids':['another']},{'source_text':'不是原文'}):
            with self.subTest(details=details):
                self.assertFalse(self.decide(.999, **details)['auto_adopt'])

    def test_unclassified_and_unsupported_inference_require_review(self):
        request = SimpleNamespace(control_type='select',excel_value='原文',evidence={})
        self.assertFalse(adoption_policy({'confidence':.99},request)['auto_adopt'])
        answer={'confidence':.99,'evidence_kinds':['excel','product'],
                'assessment':{'relation':'inferred','source_text':'原文','assumptions':[],
                              'conflicts':[],'competing_value_ids':[]}}
        self.assertFalse(adoption_policy(answer,request)['auto_adopt'])

    def test_missing_excel_requires_both_product_and_human_support_for_inference(self):
        request=SimpleNamespace(control_type='select',excel_value='',evidence={})
        answer={'confidence':.95,'evidence_kinds':['product','human_learning'],
                'grounded_support':[{'source':'product','text':'商品资料'}],
                'assessment':{'relation':'inferred','source_text':'','assumptions':[],
                              'conflicts':[],'competing_value_ids':[]}}
        self.assertFalse(adoption_policy(answer,request)['auto_adopt'])
        answer['grounded_support'].append({'source':'human_learning','text':'人工记录'})
        self.assertTrue(adoption_policy(answer,request)['auto_adopt'])
