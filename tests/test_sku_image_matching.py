import unittest
from pathlib import Path

from kuaimai_erp import AutomationError, match_sku_images_by_name


class SkuImageMatchingTests(unittest.TestCase):
    def test_filenames_follow_page_colors_instead_of_directory_order(self):
        paths = [Path('灰色.png'), Path('棕色.jpg')]
        self.assertEqual(match_sku_images_by_name(paths, ('棕色', '灰色')),
                         (paths[1], paths[0]))

    def test_equal_count_does_not_hide_missing_color(self):
        with self.assertRaisesRegex(AutomationError, '灰色.*0 张'):
            match_sku_images_by_name([Path('棕色.png'), Path('黑色.png')], ('棕色', '灰色'))

    def test_duplicate_color_files_are_ambiguous(self):
        with self.assertRaisesRegex(AutomationError, '棕色.*2 张'):
            match_sku_images_by_name([Path('棕色.png'), Path('棕色.jpg')], ('棕色',))

    def test_numbered_legacy_images_require_matching_count(self):
        self.assertEqual(match_sku_images_by_name([Path('3.png')], ('棕色',)), (Path('3.png'),))
        with self.assertRaises(AutomationError):
            match_sku_images_by_name([Path('3.png')], ('棕色', '灰色'))
        with self.assertRaisesRegex(AutomationError, '不能按图片编号猜测'):
            match_sku_images_by_name([Path('1.png'), Path('2.png')], ('棕色', '灰色'))

    def test_unrelated_file_cannot_shift_named_mapping(self):
        self.assertEqual(
            match_sku_images_by_name([Path('灰色.png'), Path('备用.png'), Path('棕色.png')], ('棕色', '灰色')),
            (Path('棕色.png'), Path('灰色.png')),
        )
