import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from openpyxl import Workbook

from dewu_data import (
    DewuError,
    amount,
    category_path,
    chinese_measurement,
    discover_products,
    read_dewu_product,
    select_workbook,
    split_detail_images,
    split_values,
)


class DewuInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / '5.19【双头鹰】'
        self.folder.mkdir()
        self.fields = {
            '卖点提炼': '双头鹰多口袋直筒牛仔裤',
            '类目': '服装 > 长裤 > 工装裤',
            '外网链接': 'https://example.com/item/7076',
            '货号': 'NGBL-7076',
            '适用人群': '男',
            '品牌': 'NEIGBORL',
            '发售价格': 865.0,
            '发售价格证明渠道来源': '品牌官网',
            '成分含量': '棉100%',
            '颜色': '卡其色',
            '尺码（尺码表）（尺码推荐）（试穿报告）': 'S/M/L/XL/2XL/3XL',
            '出价类型': '直发预售',
            '商品编码': '7076卡其色',
            '库存': 50.0,
            '出价': 865.0,
            '上下架': '上架',
        }
        self.white = self.write_image('白底图/1.png', size=(750, 750))
        self.other = self.write_image('11/1.png', size=(750, 750))
        self.detail = self.write_image('详情页/1_01.jpg')
        self.chart = self.write_image('尺码表/1_09.jpg')
        self.excel = self.write_workbook()

    def write_image(self, name, *, size=(50, 80), folder=None):
        path = (folder or self.folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = cv2.imencode(path.suffix, np.full((*size, 3), 255, dtype=np.uint8))[1]
        encoded.tofile(str(path))
        return path

    def write_workbook(self, fields=None, *, reordered=False, no_header=False):
        workbook = Workbook()
        sheet = workbook.active
        fields = self.fields if fields is None else fields
        if reordered:
            sheet.append(('得物商品资料',))
            sheet.append(('说明', '内 容', '序号', '属 性'))
            for index, (key, value) in enumerate(reversed(tuple(fields.items())), start=1):
                sheet.append(('无关列', value, index, key))
        else:
            if not no_header:
                sheet.append(('属性', '内容'))
            for key, value in fields.items():
                sheet.append((key, value))
        path = self.folder / '产品信息.xlsx'
        workbook.save(path)
        workbook.close()
        return path

    def test_reads_named_columns_in_any_order_and_keeps_extra_attributes(self):
        self.write_workbook(reordered=True)

        product = read_dewu_product(self.excel)

        self.assertEqual(product.style_code, 'NGBL-7076')
        self.assertEqual(product.category, ('服装', '长裤', '工装裤'))
        self.assertEqual(product.release_price, '865')
        self.assertEqual(product.stock, '50')
        self.assertEqual(product.bid_price, '865')
        self.assertEqual(product.colors, ('卡其色',))
        self.assertEqual(product.sizes, ('S', 'M', 'L', 'XL', '2XL', '3XL'))
        self.assertEqual(product.fields['发售价格证明渠道来源'], '品牌官网')
        self.assertEqual(product.fields['成分含量'], '棉100%')

    def test_preserves_legacy_key_value_workbooks_and_field_aliases(self):
        aliases = {
            '类目': '商品分类', '品牌': '商品品牌', '货号': '款式编码',
            '外网链接': '商品链接', '上下架': '商品状态',
            '尺码（尺码表）（尺码推荐）（试穿报告）': '尺码',
        }
        self.write_workbook({aliases.get(k, k): v for k, v in self.fields.items()}, no_header=True)

        product = read_dewu_product(self.excel)

        self.assertEqual(product.brand, 'NEIGBORL')
        self.assertEqual(product.style_code, 'NGBL-7076')
        self.assertEqual(product.category[-1], '工装裤')
        self.assertEqual(product.listed, '上架')
        self.assertEqual(product.sizes[-1], '3XL')

    def test_only_product_level_workbooks_are_discovered_and_selection_is_exact(self):
        (self.root / '休闲裤模板.xlsx').write_bytes(b'template')
        sibling = self.root / '5.19【双头鹰】副本'
        sibling.mkdir()
        other_excel = sibling / '产品信息.xlsx'
        other_excel.write_bytes(b'other')
        (self.folder / '归档').mkdir()
        (self.folder / '归档/产品信息.xlsx').write_bytes(b'archive')

        self.assertEqual(set(discover_products(self.root)), {self.excel, other_excel})
        self.assertEqual(select_workbook(self.root, self.folder.name), self.excel)
        with self.assertRaisesRegex(DewuError, '指定唯一商品'):
            select_workbook(self.root)
        with self.assertRaisesRegex(DewuError, '无匹配商品'):
            select_workbook(self.root, '双头鹰')
        self.assertEqual(select_workbook(self.root, excel=str(other_excel)), other_excel.resolve())

    def test_missing_root_and_workbook_are_actionable(self):
        with self.assertRaisesRegex(DewuError, '目录不存在'):
            discover_products(self.root / '不存在')
        with self.assertRaisesRegex(DewuError, '产品信息表不存在'):
            select_workbook(self.root, excel=str(self.root / '不存在.xlsx'))

    def test_images_stay_with_selected_product_and_white_images_come_first(self):
        white_two = self.write_image('白底图/2.png', size=(750, 750))
        other_two = self.write_image('11/2.png', size=(750, 750))
        other_ten = self.write_image('11/10.png', size=(750, 750))
        self.write_image('白底图/1.png', folder=self.root / '另一个商品', size=(750, 750))
        (self.folder / '白底图/Thumbs.db').write_bytes(b'ignored')
        (self.folder / '11/.DS_Store').write_bytes(b'ignored')
        self.write_image('11/.缓存/3.png', size=(750, 750))

        product = read_dewu_product(self.excel)

        self.assertEqual(product.initial_images, (self.white, white_two, self.other, other_two, other_ten))
        self.assertEqual(product.size_image, self.chart)

    def test_detail_images_are_naturally_sorted_then_split_20_15_and_overflow(self):
        self.detail.unlink()
        for index in reversed(range(1, 22)):
            self.write_image(f'详情页/1_{index:02}.jpg')
        for index in reversed(range(1, 17)):
            self.write_image(f'详情页/2_{index:02}.jpg')

        product = read_dewu_product(self.excel)
        groups = split_detail_images(product.detail_images)

        self.assertEqual([len(groups[name]) for name in ('商品展示', '穿搭效果', '细节呈现')], [20, 15, 2])
        self.assertEqual(groups['商品展示'][0].name, '1_01.jpg')
        self.assertEqual(groups['商品展示'][-1].name, '1_20.jpg')
        self.assertEqual(groups['穿搭效果'][0].name, '1_21.jpg')
        self.assertEqual(groups['穿搭效果'][-1].name, '2_14.jpg')
        self.assertEqual([p.name for p in groups['细节呈现']], ['2_15.jpg', '2_16.jpg'])
        self.assertEqual(tuple(p for name in ('商品展示', '穿搭效果', '细节呈现') for p in groups[name]), product.detail_images)

    def test_detail_limit_never_discards_images(self):
        images = tuple(Path(f'{i}.jpg') for i in range(43))
        groups = split_detail_images(images)
        self.assertEqual([len(groups[name]) for name in ('商品展示', '穿搭效果', '细节呈现')], [20, 15, 8])
        with self.assertRaisesRegex(DewuError, '43'):
            split_detail_images(images + (Path('44.jpg'),))

    def test_supports_alternate_detail_and_size_chart_folder_names(self):
        (self.folder / '详情页').rename(self.folder / '详情页图')
        (self.folder / '尺码表').rename(self.folder / '尺码信息表')

        product = read_dewu_product(self.excel)

        self.assertEqual(product.detail_images[0].parent.name, '详情页图')
        self.assertEqual(product.size_image.parent.name, '尺码信息表')

    def test_missing_or_ambiguous_size_chart_stops_before_browser(self):
        self.chart.unlink()
        with self.assertRaisesRegex(DewuError, '尺码.*当前 0 张'):
            read_dewu_product(self.excel)
        self.write_image('尺码表/1.jpg')
        self.write_image('尺码信息表/2.jpg')
        with self.assertRaisesRegex(DewuError, '尺码.*当前 2 张'):
            read_dewu_product(self.excel)

    def test_missing_required_asset_group_is_rejected(self):
        self.other.unlink()
        with self.assertRaisesRegex(DewuError, '须包含白底图、11、详情页'):
            read_dewu_product(self.excel)

    def test_too_many_initial_images_are_rejected(self):
        for index in range(2, 9):
            self.write_image(f'11/{index}.png', size=(750, 750))
        with self.assertRaisesRegex(DewuError, '上限 8 张'):
            read_dewu_product(self.excel)

    def test_unreadable_image_and_invalid_initial_dimensions_are_rejected(self):
        self.other.write_bytes(b'not an image')
        with self.assertRaisesRegex(DewuError, '图片无法解码'):
            read_dewu_product(self.excel)
        self.write_image('11/1.png', size=(750, 800))
        with self.assertRaisesRegex(DewuError, '正方形'):
            read_dewu_product(self.excel)
        self.write_image('11/1.png', size=(749, 749))
        with self.assertRaisesRegex(DewuError, '750'):
            read_dewu_product(self.excel)

    def test_missing_required_field_and_unknown_listing_status_are_rejected(self):
        fields = dict(self.fields)
        del fields['品牌']
        self.write_workbook(fields)
        with self.assertRaisesRegex(DewuError, '必填字段：品牌'):
            read_dewu_product(self.excel)
        self.write_workbook({**self.fields, '上下架': '自动'})
        with self.assertRaisesRegex(DewuError, '上下架只能'):
            read_dewu_product(self.excel)

    def test_summary_records_draft_only_policy_and_leaf_category(self):
        summary = read_dewu_product(self.excel).summary()
        self.assertFalse(summary['submit_review'])
        self.assertEqual(summary['title_category'], '工装裤')
        self.assertEqual(summary['size_fit'], '正码')
        self.assertEqual(summary['size_template'], '通用裤装模板')
        self.assertEqual(summary['size_image'], str(self.chart))
        self.assertEqual(summary['initial_images'], [str(self.white), str(self.other)])


class DewuFieldValidationTests(unittest.TestCase):
    def test_specification_separators_preserve_order_and_reject_empty_or_duplicate_values(self):
        self.assertEqual(split_values('S／M,L，XL、2XL;3XL\n4XL'), ('S', 'M', 'L', 'XL', '2XL', '3XL', '4XL'))
        for text in ('/，\n', 'S/s', '蓝 色/蓝色', 'S / S'):
            with self.subTest(text=text), self.assertRaises(DewuError):
                split_values(text)

    def test_category_paths_accept_platform_and_excel_separators(self):
        for text in ('服装>长裤>工装裤', '服装 >> 长裤 >> 工装裤', '服装＞长裤＞工装裤', '服装→长裤→工装裤'):
            with self.subTest(text=text):
                self.assertEqual(category_path(text), ('服装', '长裤', '工装裤'))
        with self.assertRaisesRegex(DewuError, '缺少类目'):
            category_path(' > ')

    def test_chinese_measurement_names_drop_english_units_and_use_canonical_foot_name(self):
        for text, expected in (
            ('腰围/WAISTLINE(cm)', '腰围'),
            ('裤长/LENGTH(cm)', '裤长'),
            ('大腿围/THIGH(cm)', '大腿围'),
            ('脚围/FOOT OPENING(cm)', '裤脚围'),
            ('臀围(cm)', '臀围'),
        ):
            with self.subTest(text=text):
                self.assertEqual(chinese_measurement(text), expected)
        with self.assertRaisesRegex(DewuError, '没有中文名称'):
            chinese_measurement('WAIST(cm)')

    def test_numeric_fields_reject_nonfinite_negative_and_fractional_inventory(self):
        self.assertEqual(amount(' 1,200.00元 ', '出价', positive=True), '1200')
        self.assertEqual(amount('0', '库存', integer=True), '0')
        for text in ('NaN', 'Infinity', '-Infinity', '-1', '不可用'):
            with self.subTest(text=text), self.assertRaises(DewuError):
                amount(text, '出价', positive=True)
        with self.assertRaisesRegex(DewuError, '整数'):
            amount('1.5', '库存', integer=True)
        with self.assertRaisesRegex(DewuError, '正数'):
            amount('0', '发售价格', positive=True)


if __name__ == '__main__':
    unittest.main()
