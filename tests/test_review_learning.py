import unittest
from review_learning import choose_review_learning


class ReviewLearningTests(unittest.TestCase):
    def request(self, **changes):
        value = dict(product_version='new', field_label='适用人群', excel_value='通用/男女通用',
                     control_type='multi_select', category={'hints':['夹克']},
                     options=[{'value_id':'n1','label':'青年'}, {'value_id':'n2','label':'中年'}])
        value.update(changes)
        return value

    def record(self, product='old', **changes):
        value = dict(product_version=product, field_label='适用人群',
                     excel_candidates=['通用','男女通用'], category={'hints':['夹克']},
                     labels=['青年','中年'], review_id=product, platform_id='jd')
        value.update(changes)
        return value

    def test_same_product_other_platform_remaps_multi_ids(self):
        result = choose_review_learning(self.request(), [self.record('new')])
        self.assertEqual(result['value_ids'], ['n1','n2'])
        self.assertTrue(result['same_product'])

    def test_three_independent_products_enable_unmapped_field(self):
        result = choose_review_learning(self.request(), [self.record(str(i)) for i in range(3)])
        self.assertEqual(result['support_count'], 3)
        self.assertEqual(result['value_labels'], ['青年','中年'])

    def test_repeated_reviews_of_one_product_do_not_raise_confidence(self):
        result = choose_review_learning(self.request(), [self.record() for _ in range(4)])
        self.assertNotIn('value_ids', result)

    def test_latest_disagreement_requires_review(self):
        rows = [self.record('latest', labels=['青年'])] + [self.record(str(i)) for i in range(25)]
        self.assertEqual(choose_review_learning(self.request(), rows)['reason_code'], 'review_learning_conflict')

    def test_age_and_audience_are_separate(self):
        result = choose_review_learning(self.request(field_label='适用年龄'), [self.record('new')])
        self.assertNotIn('value_ids', result)

    def test_excel_change_or_category_change_prevents_cross_product_reuse(self):
        for change in ({'excel_value':'青少年'}, {'category':{'hints':['童装']}}, {'category':{}}):
            self.assertNotIn('value_ids', choose_review_learning(self.request(**change), [self.record(str(i)) for i in range(3)]))

    def test_missing_live_choice_and_single_select_cannot_accept_pair(self):
        for change in ({'control_type':'select'}, {'options':[{'value_id':'n1','label':'青年'}]}):
            self.assertNotIn('value_ids', choose_review_learning(self.request(**change), [self.record('new')]))

    def test_generic_unmapped_field_and_explicit_field_alias(self):
        for old, new in [('流行元素','款式细节'), ('自定义属性','自定义属性')]:
            rows = [self.record(str(i),field_label=old) for i in range(3)]
            self.assertIn('value_ids',choose_review_learning(self.request(field_label=new),rows))


class DouyinReviewControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_select_preserves_actual_multiple_capability(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from douyin_listing import DouyinListing
        for multiple in (False, True):
            with self.subTest(multiple=multiple):
                listing = object.__new__(DouyinListing)
                listing.logger = None
                listing._category_properties_leaf_id = 'category'
                listing._api_property = AsyncMock(return_value={
                    'id': 'field', 'options': [{'id': 'young', 'name': '青年'}]})
                listing.attribute_runtime = SimpleNamespace(
                    resolve=AsyncMock(return_value=SimpleNamespace(label='青年')))
                select = Mock()
                select.locator.return_value.count = AsyncMock(return_value=int(multiple))
                await listing._resolve_learning_select_value(
                    '适用人群', select, ('青年',), observed_values=('青年',))
                request = listing.attribute_runtime.resolve.await_args.args[0]
                self.assertEqual(request.control_type, 'multi_select' if multiple else 'select')
