import unittest

from category_profile import (
    category_profile,
    category_search_terms,
    choose_category_candidate,
    is_footwear_category,
    is_leather_shoe_category,
    preferred_category_leaf,
)


class CategoryProfileTests(unittest.TestCase):
    def test_shoe_family_and_leather_leaf_are_distinct(self):
        for path in ('鞋靴 > 男鞋 > 皮鞋', '流行男鞋 > 时尚单鞋 > 正装皮鞋'):
            self.assertTrue(is_footwear_category(path))
            self.assertTrue(is_leather_shoe_category(path))
        self.assertTrue(is_footwear_category('男鞋 > 运动鞋'))
        self.assertFalse(is_leather_shoe_category('男鞋 > 运动鞋'))
        self.assertFalse(is_footwear_category('鞋类配件 > 鞋垫'))

    def test_shoe_full_path_alternative_matches_taobao_male_shoe_root(self):
        self.assertEqual(choose_category_candidate(
            ('流行男鞋 > 时尚单鞋 > 休闲皮鞋', '女鞋 > 休闲皮鞋'),
            ('流行男鞋 > 时尚单鞋 >休闲皮鞋', '正装皮鞋', '男士德比鞋', '皮鞋'),
        )[0], '流行男鞋 > 时尚单鞋 > 休闲皮鞋')
    def test_leather_shoe_matches_current_excel_and_douyin_predictions(self):
        hints = (
            "流行男鞋 > 时尚单鞋 >休闲皮鞋", "正装皮鞋", "男士德比鞋", "皮鞋"
        )
        candidates = (
            "鞋靴 > 男鞋 > 皮鞋", "鞋靴 > 男鞋 > 单鞋", "鞋靴 > 男鞋 > 休闲鞋"
        )
        self.assertEqual(
            choose_category_candidate(candidates, hints),
            (candidates[0], "excel_hint_exact"),
        )

    def test_shoe_gender_matches_and_rejects_opposite_branch(self):
        for gender, opposite, person in (("男鞋", "女鞋", "男士"),):
            correct = f"鞋靴 > {gender} > 皮鞋"
            wrong = f"鞋靴 > {opposite} > 皮鞋"
            for hint in (gender, person):
                with self.subTest(gender=gender, hint=hint):
                    self.assertEqual(
                        choose_category_candidate((wrong, correct), (hint, "皮鞋"))[0],
                        correct,
                    )
                    self.assertEqual(
                        choose_category_candidate((wrong,), (hint, "皮鞋")),
                        ("", ""),
                    )

    def test_category_alternatives_keep_excel_order(self):
        hints = ("卫衣", "打底衫", "男士套头卫衣", "套头卫衣")
        self.assertEqual(choose_category_candidate(
            ("服装 > 男装 > 卫衣", "服装 > 男装 > 打底衫"), hints)[0],
            "服装 > 男装 > 卫衣")
        self.assertEqual(choose_category_candidate(
            ("服装 > 男装 > 打底衫", "服装 > 男装 > 卫衣"), hints)[0],
            "服装 > 男装 > 卫衣")
        self.assertEqual(choose_category_candidate(
            ("服装 > 男装 > 打底衫",), hints)[0], "服装 > 男装 > 打底衫")

    def test_classifies_existing_pants_and_new_outerwear_without_product_codes(self):
        self.assertEqual(
            category_profile(("男装", "男士休闲裤", "男士休闲直筒裤")).garment_kind,
            "pants",
        )
        self.assertEqual(
            category_profile(("夹克", "外套", "男士休闲夹克", "其他夹克")).garment_kind,
            "clothing",
        )

    def test_keeps_unknown_future_categories_generic(self):
        profile = category_profile(("数码配件",), "新品")
        self.assertEqual(profile.garment_kind, "generic")
        self.assertFalse(profile.supports_letter_size_chart)

    def test_ranks_exact_category_hints_by_specificity(self):
        hints = ("夹克", "外套", "男士休闲夹克", "其他夹克")
        self.assertEqual(
            category_search_terms(hints),
            ("男士休闲夹克", "其他夹克", "夹克", "外套"),
        )
        self.assertEqual(preferred_category_leaf(hints), "男士休闲夹克")

    def test_category_choice_uses_excel_gender_when_recommendations_share_leaf(self):
        hints = ("夹克", "外套", "男士休闲夹克", "其他夹克")
        candidates = (
            "童装/婴儿装/亲子装 > 外套/夹克/大衣 > 夹克/皮衣",
            "男装 > 夹克",
        )
        self.assertEqual(
            choose_category_candidate(candidates, hints),
            ("男装 > 夹克", "excel_hint_exact"),
        )

    def test_category_choice_rejects_wrong_gender_when_only_leaf_matches(self):
        hints = ("夹克", "外套", "男士休闲夹克", "其他夹克")
        self.assertEqual(
            choose_category_candidate(
                ("童装/婴儿装/亲子装 > 外套/夹克/大衣 > 夹克/皮衣",),
                hints,
            ),
            ("", ""),
        )


if __name__ == "__main__":
    unittest.main()
