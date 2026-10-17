import unittest
from review_learning import choose_review_learning


class ReviewLearningTests(unittest.TestCase):
    def test_category_learning_is_platform_scoped_and_requires_mature_support(self):
        path = '男装 > 牛仔裤 > 牛仔裤'
        request = self.request(platform_id='pdd', field_label='商品类目',
                               excel_value='牛仔裤', control_type='category',
                               options=[{'value_id': 'path-new', 'label': path}])
        records = [self.record(str(i), platform_id='pdd', field_label='商品类目',
                               excel_candidates=['牛仔裤'], control_type='category',
                               labels=[path]) for i in range(3)]
        self.assertNotIn('value_ids', choose_review_learning(request, records[:2]))
        self.assertEqual(choose_review_learning(request, records)['value_ids'], ['path-new'])
        for row in records:
            row['platform_id'] = 'jd'
        self.assertNotIn('value_ids', choose_review_learning(request, records))

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

    def test_category_or_storage_formats_reuse_same_human_combination(self):
        hints = ['卫衣', '打底衫', '男士套头卫衣', '套头卫衣']
        request = self.request(category={'hints': ['／'.join(hints)]})
        records = [self.record(str(i), category={'hints': hints},
                               control_type='multi_select') for i in range(3)]
        result = choose_review_learning(request, records)
        self.assertEqual(result['support_count'], 3)
        self.assertEqual(result['value_labels'], ['青年', '中年'])
        for different in (['裤子', *hints[1:]], list(reversed(hints))):
            self.assertNotIn('value_ids', choose_review_learning(
                self.request(category={'hints': ['/'.join(different)]}), records))

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

    def test_related_heel_fields_reuse_review_across_platforms_on_same_product(self):
        request = self.request(
            product_version='shoe-2', platform_id='xhs', field_label='鞋跟高',
            excel_value='中跟/中跟（3cm-5cm）', control_type='select',
            category={'hints': ['流行男鞋', '休闲皮鞋']},
            options=[{'value_id': 'middle-xhs', 'label': '中跟(3-5cm)'},
                     {'value_id': 'low-xhs', 'label': '低跟(1-3cm)'}],
        )
        old = self.record(
            'shoe-2', platform_id='wxsph', field_label='鞋跟高度',
            excel_candidates=['中跟', '中跟（3cm-5cm）'],
            category=request['category'], control_type='select',
            labels=['中跟(3-5.5cm)'],
        )

        result = choose_review_learning(request, [old])

        self.assertEqual(result['value_ids'], ['middle-xhs'])
        self.assertEqual(result['support_count'], 1)
        self.assertTrue(result['same_product'])

    def test_related_fields_count_distinct_products_after_value_remapping(self):
        request = self.request(
            platform_id='wxsph', field_label='鞋跟高度',
            excel_value='中跟/中跟（3cm-5cm）', control_type='select',
            category={'hints': ['流行男鞋', '休闲皮鞋']},
            options=[{'value_id': 'middle-wx', 'label': '中跟(3-5.5cm)'},
                     {'value_id': 'low-wx', 'label': '低跟(小于3cm)'}],
        )
        records = [
            self.record(
                product, platform_id=platform, field_label=label,
                excel_candidates=['中跟', '中跟（3cm-5cm）'],
                category=request['category'], control_type='select',
                labels=[choice],
            )
            for product, platform, label, choice in (
                ('shoe-1', 'xhs', '鞋跟高', '中跟(3-5cm)'),
                ('shoe-1', 'wxsph', '鞋跟高度', '中跟(3-5.5cm)'),
                ('shoe-2', 'xhs', '鞋跟高', '中跟(3-5cm)'),
                ('shoe-3', 'wxsph', '鞋跟高度', '中跟(3-5.5cm)'),
            )
        ]

        result = choose_review_learning(request, records)

        self.assertEqual(result['support_count'], 3)
        self.assertEqual(result['value_ids'], ['middle-wx'])
        self.assertFalse(result['same_product'])

    def test_similar_name_needs_same_control_excel_and_unique_compatible_value(self):
        request = self.request(
            platform_id='xhs', field_label='鞋跟高',
            excel_value='中跟/中跟（3cm-5cm）', control_type='select',
            category={'hints': ['皮鞋']},
            options=[{'value_id': 'middle', 'label': '中跟(3-5cm)'}],
        )
        old = self.record(
            'new', platform_id='wxsph', field_label='鞋跟高度',
            excel_candidates=['中跟', '中跟（3cm-5cm）'],
            category=request['category'], control_type='select',
            labels=['中跟(3-5.5cm)'],
        )
        for change in (
            {'control_type': 'text'},
            {'excel_candidates': ['低跟']},
            {'labels': ['中跟(3-8cm)']},
            {'field_label': '鞋跟高度（cm）'},
        ):
            with self.subTest(change=change):
                self.assertNotIn('value_ids', choose_review_learning(request, [{**old, **change}]))

        ambiguous = {**request, 'options': [
            {'value_id': 'short-range', 'label': '中跟(3-5cm)'},
            {'value_id': 'long-range', 'label': '中跟(3-5.5cm)'},
        ]}
        self.assertNotIn('value_ids', choose_review_learning(
            ambiguous, [{**old, 'labels': ['中跟(3-5.25cm)']}]
        ))

        different_attribute = self.record(
            'new', platform_id='wxsph', field_label='鞋面内里材质',
            excel_candidates=['马皮'], category=request['category'],
            control_type='select', labels=['马皮（革）'],
        )
        material_request = self.request(
            product_version='new', platform_id='xhs', field_label='鞋面材质',
            excel_value='马皮', control_type='select', category=request['category'],
            options=[{'value_id': 'leather', 'label': '马皮（革）'}],
        )
        self.assertNotIn('value_ids', choose_review_learning(material_request, [different_attribute]))


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
