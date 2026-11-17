import unittest

from youzan_data import parse_youzan_fields


class YouzanDataTests(unittest.TestCase):
    def test_belt_has_its_own_kind_and_keeps_other_accessories_unknown(self):
        self.assertEqual(parse_youzan_fields({
            '商品分类': '服装 > 服装配饰 > 腰带/皮带/腰链/服饰内衣 > 服饰配件 > 腰带/腰链/男士皮带',
        }).garment_kind, 'belt')
        self.assertEqual(parse_youzan_fields({
            '商品分类': '服饰配件/皮带/帽子/围巾 > 帽子',
        }).garment_kind, 'unknown')
    def test_preserves_fields_and_same_level_category_alternatives(self):
        parsed = parse_youzan_fields(
            {
                "商品分类": "休闲裤/男士休闲直筒裤/工装休闲裤",
                "价格/一口价": 586.0,
                "数量": 100.0,
            }
        )

        self.assertEqual(
            parsed.category_path,
            ("休闲裤/男士休闲直筒裤/工装休闲裤",),
        )
        self.assertEqual(parsed.fields["价格/一口价"], "586")
        self.assertEqual(parsed.fields["数量"], "100")
        self.assertEqual(parsed.garment_kind, "pants")

    def test_classifies_shoes_separately_from_clothing(self):
        parsed = parse_youzan_fields({"商品分类": "流行男鞋 > 时尚单鞋 > 休闲皮鞋/正装皮鞋"})
        self.assertEqual(parsed.garment_kind, "footwear")

    def test_classifies_coat_and_preserves_unknown_kind(self):
        coat = parse_youzan_fields({"商品分类": "男装/外套/皮衣"})
        self.assertEqual(coat.garment_kind, "coat")

        unknown = parse_youzan_fields({"商品分类": "男装"})
        self.assertEqual(unknown.garment_kind, "unknown")

    def test_missing_category_is_deferred_to_youzan_preflight(self):
        parsed = parse_youzan_fields({"价格": "586"})

        self.assertEqual(parsed.category_path, ())
        self.assertEqual(parsed.garment_kind, "unknown")

    def test_fields_are_immutable(self):
        parsed = parse_youzan_fields({"商品分类": "休闲裤"})

        with self.assertRaises(TypeError):
            parsed.fields["价格"] = "1"


if __name__ == "__main__":
    unittest.main()
