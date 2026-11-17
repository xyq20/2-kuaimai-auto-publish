import unittest

from category_profile import (
    category_input_levels,
    category_input_variants,
    category_profile,
    category_search_terms,
    category_search_plans,
    choose_category_candidate,
    is_footwear_category,
    is_leather_shoe_category,
    preferred_category_leaf,
    preferred_category_hints,
)


class CategoryProfileTests(unittest.TestCase):
    def test_belt_platform_group_can_contain_a_subset_of_authored_members(self):
        hints = ("服装 > 服装配饰 > 腰带/皮带/腰链",)
        for leaf in ("腰带/腰链", "皮带/腰带"):
            with self.subTest(leaf=leaf):
                path = "服饰内衣 > 服饰配件 > " + leaf
                self.assertEqual(choose_category_candidate((path,), hints), (path, "belt_leaf_compatible"))
        for leaf in ("腰带", "腰链/围巾", "男士皮带/腰链"):
            with self.subTest(leaf=leaf):
                self.assertEqual(choose_category_candidate(("服饰配件 > " + leaf,), hints), ("", ""))
        self.assertEqual(choose_category_candidate((
            "服饰内衣 > 服饰配件 > 腰带/腰链",
            "服装 > 其他配件 > 皮带/腰带",
        ), hints), ("", "ambiguous_belt_leaf"))

    def test_belt_compatibility_scope_uses_leaf_instead_of_broad_parent(self):
        from category_profile import is_belt_category
        self.assertTrue(is_belt_category('服装 > 服装配饰 > 腰带/皮带/腰链'))
        for path in ('男装 > 牛仔裤', '男鞋 > 正装皮鞋',
                     '服饰配件/皮带/帽子/围巾 > 帽子'):
            self.assertFalse(is_belt_category(path))

    def test_vest_category_uses_clothing_workflow(self):
        self.assertEqual(category_profile(("服装 > 男装 > 马甲",)).garment_kind, "vest")
        self.assertTrue(category_profile(("男装 > 常规马甲",)).supports_letter_size_chart)
    def test_shoe_search_plans_keep_paths_and_alternatives_in_excel_order(self):
        self.assertEqual(category_search_plans((
            '鞋靴 > 男鞋 > 休闲鞋',
            '流行男鞋 > 休闲鞋 > 时尚休闲鞋/运动休闲鞋',
            '流行男鞋 > 帆布鞋 > 帆布鞋/其它男士休闲鞋',
        )), (
            ('休闲鞋', '鞋靴 > 男鞋 > 休闲鞋'),
            ('时尚休闲鞋', '流行男鞋 > 休闲鞋 > 时尚休闲鞋'),
            ('运动休闲鞋', '流行男鞋 > 休闲鞋 > 运动休闲鞋'),
            ('帆布鞋', '流行男鞋 > 帆布鞋 > 帆布鞋'),
            ('其它男士休闲鞋', '流行男鞋 > 帆布鞋 > 其它男士休闲鞋'),
        ))

    def test_known_parent_does_not_win_over_available_leaf(self):
        self.assertEqual(choose_category_candidate(
            ("流行男鞋 > 靴子", "流行男鞋 > 靴子 > 时装靴"),
            ("鞋靴 > 男鞋 > 靴子", "流行男鞋 > 靴子 > 时装靴"),
        )[0], "流行男鞋 > 靴子 > 时装靴")

    def test_ancestor_overlap_is_not_an_exact_leaf_match(self):
        self.assertEqual(choose_category_candidate(
            ("流行男鞋 > 靴子 > 雪地靴",),
            ("流行男鞋 > 靴子 > 时装靴",),
        ), ("", ""))

    def test_supplier_can_supply_two_full_boot_paths_and_peer_leaves(self):
        from kuaimai_erp import parse_category_hints

        expression = "鞋靴 > 男鞋 > 靴子/流行男鞋 > 靴子 > 时装靴/工装靴"
        hints = parse_category_hints(expression)
        self.assertEqual(hints, ("鞋靴 > 男鞋 > 靴子", "流行男鞋 > 靴子 > 时装靴/工装靴"))
        self.assertEqual(choose_category_candidate(
            ("鞋靴 > 男鞋 > 皮鞋", "鞋靴 > 男鞋 > 靴子"), hints
        ), ("鞋靴 > 男鞋 > 靴子", "full_ordered_path"))
        self.assertEqual(choose_category_candidate(
            ("流行男鞋 > 靴子 > 时装靴",), hints
        )[0], "流行男鞋 > 靴子 > 时装靴")
        self.assertEqual(parse_category_hints("服装 > 男装 > 针织衫/毛衣"),
                         ("服装 > 男装 > 针织衫/毛衣",))

    def test_category_syntax_distinguishes_path_levels_from_peer_options(self):
        expression = "服装 > 男装 > 针织衫/毛衣/羊毛衫"
        self.assertEqual(
            category_input_levels(expression),
            (("服装",), ("男装",), ("针织衫", "毛衣", "羊毛衫")),
        )
        self.assertEqual(
            category_input_variants(expression),
            (
                ("服装", "男装", "针织衫"),
                ("服装", "男装", "毛衣"),
                ("服装", "男装", "羊毛衫"),
            ),
        )

    def test_matching_gender_alone_does_not_accept_an_unrelated_category(self):
        for hints in (
            ("牛仔裤", "男士直筒牛仔裤"),
            ("男装", "牛仔裤", "男士直筒牛仔裤"),
        ):
            with self.subTest(hints=hints):
                self.assertEqual(choose_category_candidate(
                    ("男装 > 休闲裤 > 休闲裤",), hints,
                ), ("", ""))

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

    def test_classifies_belts_as_accessories_before_clothing_parent_markers(self):
        profile = category_profile(
            ("服装 > 服装配饰 > 腰带/皮带/腰链/服饰内衣 > 服饰配件 > 腰带/腰链/男士皮带",),
            "头层牛皮针扣皮带",
        )
        self.assertEqual(profile.garment_kind, "accessory")
        self.assertFalse(profile.supports_letter_size_chart)
        self.assertEqual(
            category_profile(('男装 > 休闲裤',), '附皮带休闲裤').garment_kind,
            'pants',
        )

    def test_exact_unisex_belt_path_is_not_rejected_by_later_male_alternative(self):
        hints = (
            '服装 > 服装配饰 > 腰带/皮带/腰链',
            '服饰内衣 > 服饰配件 > 腰带/腰链/男士皮带',
        )
        self.assertEqual(choose_category_candidate((
            '奢侈品 > 奢品配饰 > 腰带',
            '服装>服装配饰>腰带/皮带/腰链',
            '服装 > 男装 > 休闲裤',
        ), hints), ('服装>服装配饰>腰带/皮带/腰链', 'full_ordered_path'))

    def test_belt_composite_leaf_rejects_single_word_category_results(self):
        hints = ('服装 > 服装配饰 > 腰带/皮带/腰链',)
        self.assertEqual(category_input_levels(hints[0]), (
            ('服装',), ('服装配饰',), ('腰带/皮带/腰链',),
        ))
        self.assertEqual(category_input_variants(hints[0]), (
            ('服装', '服装配饰', '腰带/皮带/腰链'),
        ))
        self.assertEqual(category_search_terms(hints), ('腰带/皮带/腰链',))
        self.assertEqual(preferred_category_leaf(hints), '腰带/皮带/腰链')
        self.assertEqual(
            category_search_plans(hints),
            (
                ('腰带/皮带/腰链', '服装 > 服装配饰 > 腰带/皮带/腰链'),
            ),
        )
        for leaf in ('腰带', '皮带', '腰链'):
            with self.subTest(leaf=leaf):
                self.assertEqual(choose_category_candidate(
                    (f'服装 > 服装配饰 > {leaf}',), hints,
                ), ('', ''))
                self.assertEqual(choose_category_candidate(
                    (f'服装 > 服装配饰 > {leaf}',), ('腰带/皮带/腰链',),
                ), ('', ''))
        self.assertEqual(choose_category_candidate(
            ('服装 > 服装配饰 > 腰带/皮带/腰链',), hints,
        ), ('服装 > 服装配饰 > 腰带/皮带/腰链', 'full_ordered_path'))

    def test_composite_belt_leaf_matches_other_platform_root_with_later_gender_hint(self):
        hints = (
            '服装 > 服装配饰 > 腰带/皮带/腰链',
            '服饰内衣 > 服饰配件 > 腰带/腰链/男士皮带',
        )
        correct = '服饰配件/皮带/帽子/围巾 > 腰带/皮带/腰链'
        self.assertEqual(choose_category_candidate((
            '奢侈品 > 奢品配饰 > 腰带', correct,
        ), hints), (correct, 'excel_hint_exact'))
        self.assertEqual(choose_category_candidate((
            '女装 > 腰带/皮带/腰链',
        ), hints), ('', ''))

    def test_pdd_belt_composite_with_suspenders_keeps_complete_name(self):
        hints = ('服装 > 服装配饰 > 腰带/皮带/腰链',
                 '服饰内衣 > 服饰配件 > 腰带/腰链/男士皮带')
        path = '服饰配件 > 腰带/皮带/腰链/背带 > 腰带/皮带/腰链/背带'
        self.assertEqual(category_input_variants(path), (
            ('服饰配件', '腰带/皮带/腰链/背带', '腰带/皮带/腰链/背带'),
        ))
        self.assertEqual(choose_category_candidate((path,), hints), (path, 'belt_leaf_compatible'))
        self.assertEqual(choose_category_candidate((path.rsplit(' > ', 1)[0], path), hints)[0], path)
        for wrong in ('服饰配件 > 腰带 > 腰带', '服饰配件 > 背带',
                      '女装 > 腰带/皮带/腰链/背带'):
            with self.subTest(wrong=wrong):
                self.assertEqual(choose_category_candidate((wrong,), hints), ('', ''))
        other = '配饰 > 腰带/皮带/腰链/背带'
        self.assertTrue(choose_category_candidate((path, other), hints)[1].startswith('ambiguous'))

    def test_product_excel_keeps_both_platform_belt_categories_complete(self):
        from kuaimai_erp import parse_category_hints

        first = '服装 > 服装配饰 > 腰带/皮带/腰链'
        second = '服饰内衣 > 服饰配件 > 腰带/腰链/男士皮带'
        hints = parse_category_hints(first + '/' + second)
        self.assertEqual(hints, (first, second))
        self.assertEqual(preferred_category_hints(hints), hints)
        self.assertEqual(category_search_terms(hints), (
            '腰带/皮带/腰链', '腰带/腰链/男士皮带',
        ))
        self.assertEqual(category_input_variants(second), (
            ('服饰内衣', '服饰配件', '腰带/腰链/男士皮带'),
        ))
        for query, target in category_search_plans(hints):
            with self.subTest(query=query, target=target):
                self.assertIn(target, (first, second))
        for candidate in (first, second, '服饰配件/皮带/帽子/围巾 > 腰带/皮带/腰链'):
            with self.subTest(candidate=candidate):
                self.assertEqual(choose_category_candidate((candidate,), hints)[0], candidate)
        for leaf in ('腰带', '皮带', '腰链'):
            with self.subTest(leaf=leaf):
                self.assertEqual(choose_category_candidate(
                    (f'服饰内衣 > 服饰配件 > {leaf}',), hints,
                ), ('', ''))

    def test_composite_category_member_can_be_a_platform_leaf_with_different_parents(self):
        hints = ('服装 > 服装配饰 > 腰带/皮带/腰链',
                 '服饰内衣 > 服饰配件 > 腰带/腰链/男士皮带')
        for path in ('箱包皮具 > 箱包皮具配件 > 男士皮带',
                     '服饰内衣 > 服饰配件 > 男士皮带',
                     '时尚配饰 > 男士配饰 > 男士皮带'):
            with self.subTest(path=path):
                self.assertEqual(choose_category_candidate((path,), hints),
                                 (path, 'composite_member_exact'))
        for path in ('男士皮带', '女装 > 男士皮带', '箱包皮具 > 女士皮带'):
            self.assertEqual(choose_category_candidate((path,), hints), ('', ''))
        self.assertEqual(choose_category_candidate(
            ('箱包皮具 > 箱包皮具配件 > 男士皮带',), hints[:1]), ('', ''))
        self.assertEqual(choose_category_candidate((
            '箱包皮具 > 箱包皮具配件 > 男士皮带',
            '服饰配件 > 男士皮带',
        ), hints), ('', 'ambiguous_composite_member'))


    def test_belt_composite_handles_spacing_and_full_width_slashes(self):
        hint = '服装 ＞ 服装配饰 ＞ 腰带 ／ 皮带 ／ 腰链'
        self.assertEqual(category_input_variants(hint), (
            ('服装', '服装配饰', '腰带/皮带/腰链'),
        ))
        candidate = '服装 > 服装配饰 > 腰带 / 皮带 / 腰链'
        self.assertEqual(choose_category_candidate((candidate,), (hint,)), (
            candidate, 'full_ordered_path',
        ))

    def test_composite_belt_does_not_block_authored_alternative_for_other_platform(self):
        self.assertEqual(choose_category_candidate(
            ('服饰内衣 > 服饰配件 > 男士皮带',),
            ('服装 > 服装配饰 > 腰带/皮带/腰链', '服饰内衣 > 服饰配件 > 男士皮带'),
        ), ('服饰内衣 > 服饰配件 > 男士皮带', 'full_ordered_path'))

    def test_keeps_unknown_future_categories_generic(self):
        profile = category_profile(("数码配件",), "新品")
        self.assertEqual(profile.garment_kind, "generic")
        self.assertFalse(profile.supports_letter_size_chart)

    def test_search_terms_keep_authored_order(self):
        hints = ("夹克", "外套", "男士休闲夹克", "其他夹克")
        self.assertEqual(
            category_search_terms(hints),
            hints,
        )
        self.assertEqual(preferred_category_leaf(hints), "夹克")

    def test_search_terms_use_final_path_level_and_deduplicate_in_order(self):
        self.assertEqual(category_search_terms((
            "服装 > 男装 > 针织衫/毛衣/羊毛衫", "毛衣/开衫",
        )), ("针织衫", "毛衣", "羊毛衫", "开衫"))
        self.assertEqual(category_search_terms((
            "休闲裤/男士休闲直筒裤/工装休闲裤",
        )), ("休闲裤", "男士休闲直筒裤", "工装休闲裤"))

    def test_matching_path_alternatives_respects_excel_priority(self):
        self.assertEqual(preferred_category_hints(('男装 > 卫衣', '男装 > 打底衫')), ('男装 > 卫衣',))
        self.assertEqual(choose_category_candidate(
            ("服装 > 男装 > 毛衣", "服装 > 男装 > 针织衫"),
            ("服装 > 男装 > 针织衫/毛衣",),
        ), ("服装 > 男装 > 针织衫", "full_ordered_path"))

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
