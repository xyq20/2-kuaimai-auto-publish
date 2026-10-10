import unittest
from types import SimpleNamespace
from semantic_policy import adoption_policy


class SemanticPolicyTests(unittest.TestCase):
    def preference(self, *, field='风格', control='select', confidence=.72, **assessment):
        from learning_models import CandidateValue
        request = SimpleNamespace(field_label=field, control_type=control,
            excel_value='美式休闲风', evidence={},
            candidates=(CandidateValue('casual', '休闲风'), CandidateValue('american', '美式')))
        answer = {'value_ids': ['casual'], 'confidence': confidence, 'evidence_kinds': ['excel'],
            'assessment': {'relation': 'preference', 'source_text': '美式休闲风',
                'acceptable_value_ids': ['casual', 'american'], 'assumptions': [],
                'conflicts': [], 'competing_value_ids': [], **assessment}}
        return adoption_policy(answer, request)

    def test_style_accepts_one_operator_choice_among_multiple_valid_candidates(self):
        for field in ('风格', '细分风格', '基础风格'):
            with self.subTest(field=field):
                decision = self.preference(field=field)
                self.assertTrue(decision['auto_adopt'])
                self.assertEqual(decision['acceptable_value_ids'], ['casual', 'american'])

    def test_style_still_reviews_unknown_candidates_conflicts_and_low_confidence(self):
        for overrides in ({'acceptable_value_ids': ['casual', 'invented']},
                          {'acceptable_value_ids': ['american']},
                          {'acceptable_value_ids': ['casual', 'casual']},
                          {'acceptable_value_ids': []},
                          {'conflicts': ['商品明确不适合休闲风']},
                          {'assumptions': ['假定商品印有美式标志']},
                          {'competing_value_ids': ['unknown-style']}):
            with self.subTest(overrides=overrides):
                self.assertFalse(self.preference(**overrides)['auto_adopt'])
        self.assertFalse(self.preference(confidence=.69)['auto_adopt'])

    def test_style_acceptable_alternative_is_not_an_unresolved_competitor(self):
        decision = self.preference(competing_value_ids=['american'])
        self.assertTrue(decision['auto_adopt'])

    def test_style_can_use_explicit_excel_intent_without_requiring_two_sources(self):
        from learning_models import CandidateValue
        request = SimpleNamespace(field_label='风格', control_type='select',
            excel_value='美式休闲风', evidence={}, candidates=(CandidateValue('casual', '休闲风'),))
        answer = {'value_ids': ['casual'], 'confidence': .8,
            'evidence_kinds': ['excel', 'product'], 'grounded_support': [],
            'assessment': {'relation': 'preference', 'source_text': '美式休闲风',
                'acceptable_value_ids': ['casual'], 'assumptions': [], 'conflicts': [], 'competing_value_ids': []}}
        self.assertTrue(adoption_policy(answer, request)['auto_adopt'])

    def test_operator_preference_cannot_relax_fact_fields_or_category(self):
        for field, control in (('材质', 'select'), ('版型', 'select'), ('适用性别', 'select'),
                               ('商品类目', 'category'), ('风格', 'category')):
            with self.subTest(field=field):
                self.assertFalse(self.preference(field=field, control=control, confidence=.99)['auto_adopt'])

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
