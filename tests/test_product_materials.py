from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

import kuaimai_erp as erp
from local_review.config import Settings
from local_review.launcher import ProductCatalog, build_command
from product_materials import MaterialPathError, current_month_directory, resolve_product_materials


class MaterialPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def month(self, name):
        path = self.root / name
        path.mkdir()
        return path

    def test_current_month_ignores_other_years_days_and_newer_folders(self):
        current = self.month('26.10')
        for name in ('25.10月份', '26.9月份', '26.11月份', '10.14【旅行家】'):
            self.month(name)
        self.assertEqual(current_month_directory(self.root, on_date=date(2026, 10, 14)), current)

    def test_supported_month_names_and_year_rollover(self):
        for name in ('26.1月份', '26.01', '2026.1', '2026年1月'):
            with self.subTest(name=name):
                current = self.month(name)
                self.assertEqual(current_month_directory(self.root, on_date=date(2026, 1, 1)), current)
                current.rmdir()
        previous = self.month('26.12月份')
        next_month = self.month('27.1月份')
        self.assertEqual(current_month_directory(previous, on_date=date(2027, 1, 1)), next_month)

    def test_month_uses_shanghai_date_at_utc_boundary(self):
        october = self.month('26.10')
        with patch('product_materials.datetime') as clock:
            clock.now.side_effect = lambda tz: datetime(2026, 9, 30, 16, 1, tzinfo=timezone.utc).astimezone(tz)
            self.assertEqual(current_month_directory(self.root), october)

    def test_missing_and_ambiguous_months_stop_instead_of_choosing_history(self):
        self.month('26.9月份')
        with self.assertRaisesRegex(MaterialPathError, '找不到 2026 年 10 月'):
            current_month_directory(self.root, on_date=date(2026, 10, 14))
        self.month('26.10')
        self.month('26.10月份')
        with self.assertRaisesRegex(MaterialPathError, '不唯一'):
            current_month_directory(self.root, on_date=date(2026, 10, 14))

    def resolve(self, month, **kwargs):
        with patch('product_materials.current_month_directory', return_value=month):
            return resolve_product_materials(self.root, self.root/'轰炸机+NGBL-2096'/'产品信息.xlsx',
                                             style_code='NGBL-2096', **kwargs)

    def test_bracket_name_matches_without_excel_in_material_directory(self):
        month = self.month('26.10')
        expected = month/'10.9【轰炸机】'
        expected.mkdir()
        (month/'10.9【轰炸机二代】').mkdir()
        self.assertEqual(self.resolve(month, title='【轰炸机】羊皮夹克'), expected)

    def test_same_product_colors_are_matched_exactly(self):
        month = self.month('26.10')
        for color in ('卡其色', '黑灰色'):
            (month/f'10.7【曼哈顿计划】{color}').mkdir()
        expected = month/'10.7【曼哈顿计划】黑灰色'
        self.assertEqual(self.resolve(month, title='【曼哈顿计划】外套', colors=('黑灰色',)), expected)
        for colors in ((), ('卡其色', '黑灰色'), ('蓝色',)):
            with self.subTest(colors=colors), self.assertRaises(MaterialPathError):
                self.resolve(month, title='【曼哈顿计划】外套', colors=colors)

    def test_one_wrong_color_directory_is_not_selected(self):
        month = self.month('26.10')
        (month/'10.9【轰炸机】黑色').mkdir()
        with self.assertRaisesRegex(MaterialPathError, '找不到商品'):
            self.resolve(month, title='【轰炸机】皮衣', colors=('咖啡色',))

    def test_unique_style_code_takes_priority_over_nickname(self):
        month = self.month('26.10')
        expected = month/'10.9【新简称】+NGBL-2096'
        expected.mkdir()
        (month/'10.9【轰炸机】').mkdir()
        (month/'其他+NGBL-20960').mkdir()
        self.assertEqual(self.resolve(month), expected)

    def test_style_code_and_color_can_both_disambiguate(self):
        month = self.month('26.10')
        expected = month/'10.9【新简称】咖啡色+NGBL-2096'
        expected.mkdir()
        (month/'10.9【新简称】黑色+NGBL-2096').mkdir()
        self.assertEqual(self.resolve(month, colors=('咖啡色',)), expected)
        with self.assertRaises(MaterialPathError):
            self.resolve(month, colors=('蓝色',))

    def test_duplicate_product_and_escaping_symlink_are_rejected(self):
        month = self.month('26.10')
        (month/'10.9【轰炸机】').mkdir()
        duplicate = month/'10.10【轰炸机】'
        duplicate.mkdir()
        with self.assertRaisesRegex(MaterialPathError, '多个素材目录'):
            self.resolve(month)
        duplicate.rmdir()
        (month/'10.9【轰炸机】').rmdir()
        (month/'10.9【轰炸机】').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(MaterialPathError, '超出当月目录'):
            self.resolve(month)

    def workbook_and_artwork(self):
        month = self.month('26.10')
        folder = month/'10.9【轰炸机】'
        folder.mkdir()
        excel = self.root/'轰炸机+NGBL-2096'/'产品信息.xlsx'
        excel.parent.mkdir()
        workbook = Workbook()
        for row in (('属性', '内容'), ('商品标题', '【轰炸机】羊皮夹克'), ('货号', 'NGBL-2096'),
                    ('价格', 899), ('颜色', '咖啡色'), ('尺码', 'S/M/L')):
            workbook.active.append(row)
        workbook.save(excel)
        workbook.close()
        for name in ('1：1主图', '3：4主图', '详情页图', 'SKU图'):
            directory = folder/name
            if name == '详情页图':
                directory /= 'images'
            directory.mkdir(parents=True)
            (directory/'1.png').write_bytes(b'image')
        for name in ('2_10.jpg', '2_2.jpg'):
            (folder/'详情页图/images'/name).write_bytes(b'image')
        return excel, folder

    def test_excel_stays_at_original_location_and_every_image_comes_from_artwork(self):
        excel, folder = self.workbook_and_artwork()
        with patch('product_materials.current_month_directory', return_value=folder.parent):
            product = erp.read_product_data(excel, include_douyin=False, identity_only=True, materials_root=self.root)
            seed = erp.read_new_product_seed(excel, materials_root=self.root)
            catalog = ProductCatalog(excel.parent.parent, materials_root=self.root)
            product_id = next(iter(catalog.paths()))
            thumbnail = catalog.image(product_id)
        self.assertEqual(product.excel_path, excel)
        self.assertEqual(product.product_dir, folder)
        self.assertTrue(all(p.is_relative_to(folder) for p in
                            product.main_images + product.main_images_34 + product.detail_images + product.sku_images))
        self.assertEqual(seed.main_images, [folder/'1：1主图/1.png'])
        self.assertEqual(thumbnail, folder/'1：1主图/1.png')
        self.assertEqual([p.name for p in product.detail_images], ['1.png', '2_2.jpg', '2_10.jpg'])

    def test_configured_source_reaches_cli_and_workbench_jobs(self):
        with patch.dict('os.environ', {'KUAIMAI_MATERIALS_ROOT': str(self.root)}):
            self.assertEqual(erp.build_parser().parse_args([]).materials_root, self.root)
            settings = Settings.from_env()
            self.assertEqual(settings.materials_root, self.root)
        command = build_command(self.root/'产品信息.xlsx',
                                {'mode':'preview', 'platforms':['tmall'], 'learning':False},
                                'run', self.root/'output', settings)
        self.assertEqual(command[command.index('--materials-root') + 1], str(self.root))


if __name__ == '__main__':
    unittest.main()
