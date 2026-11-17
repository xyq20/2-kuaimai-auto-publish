import importlib.util
from pathlib import Path
import tempfile
import unittest

import tmall_data


class TmallDataModuleTests(unittest.TestCase):
    def test_tmall_data_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("tmall_data"))


class TmallFieldsTests(unittest.TestCase):
    def test_parse_keeps_every_nonempty_field_in_order_and_freezes_the_result(self):
        parser = getattr(tmall_data, "parse_tmall_fields", None)
        fields_type = getattr(tmall_data, "TmallFields", None)
        self.assertTrue(callable(parser))
        self.assertIsNotNone(fields_type)

        result = parser(
            {
                " 品牌 ": " NEIGBORL ",
                "吊牌价/价格/基本售价": 586.0,
                "数量": 0,
                "空值": None,
                "空文本": "   ",
                "": "忽略",
            }
        )

        self.assertIsInstance(result, fields_type)
        self.assertTrue(result.__dataclass_params__.frozen)
        self.assertEqual(
            tuple(result.fields.items()),
            (
                ("品牌", "NEIGBORL"),
                ("吊牌价/价格/基本售价", "586"),
                ("数量", "0"),
            ),
        )
        with self.assertRaises((AttributeError, TypeError)):
            result.fields["品牌"] = "changed"
        with self.assertRaises(AttributeError):
            del result.fields._items
        with self.assertRaises((AttributeError, TypeError)):
            result.fields = {}


class TmallAssetsTests(unittest.TestCase):
    def test_footwear_does_not_require_vertical_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._image(root / "透明素材图")
            self._image(root / "尺码信息表")
            result = tmall_data.read_tmall_assets(root, garment_kind="footwear")
            self.assertIsNone(result.vertical_image)
            with self.assertRaises(tmall_data.TmallDataError):
                tmall_data.read_tmall_assets(root)

    def test_accessory_uses_optional_product_parameter_image_without_size_chart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transparent = self._image(root / "透明素材图")
            parameter = self._image(root / "产品参数图")

            result = tmall_data.read_tmall_assets(root, garment_kind="accessory")

        self.assertIsNone(result.vertical_image)
        self.assertEqual(result.transparent_image, transparent)
        self.assertEqual(result.parameter_image, parameter)

    def test_accessory_without_parameter_or_size_chart_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._image(root / "透明素材图")

            result = tmall_data.read_tmall_assets(root, garment_kind="accessory")

        self.assertIsNone(result.parameter_image)

    def test_transparent_upload_is_800_square_and_preserves_source_and_alpha(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for width, height in ((1200, 1200), (1200, 600), (400, 400), (800, 800)):
                with self.subTest(size=(width, height)):
                    source = root / "source.png"
                    pixels = np.full((height, width, 4), (10, 40, 90, 128), dtype=np.uint8)
                    cv2.imwrite(str(source), pixels)
                    before = source.read_bytes()

                    target = tmall_data.prepare_tmall_transparent_image(source, root / "upload.png")

                    actual = cv2.imread(str(target), cv2.IMREAD_UNCHANGED)
                    self.assertEqual(actual.shape, (800, 800, 4))
                    self.assertEqual(tuple(actual[400, 400]), (10, 40, 90, 128))
                    self.assertEqual(source.read_bytes(), before)
                    if width != height:
                        self.assertEqual(actual[0, 400, 3], 0)

    def test_transparent_upload_rejects_unreadable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.png"
            source.write_bytes(b"invalid")
            with self.assertRaisesRegex(tmall_data.TmallDataError, "无法读取"):
                tmall_data.prepare_tmall_transparent_image(source, Path(directory) / "upload.png")

    @staticmethod
    def _image(directory: Path, name: str = "1.jpg") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(b"image")
        return path

    def test_reads_one_image_from_each_ascii_named_asset_group(self):
        reader = getattr(tmall_data, "read_tmall_assets", None)
        assets_type = getattr(tmall_data, "TmallAssets", None)
        self.assertTrue(callable(reader))
        self.assertIsNotNone(assets_type)

        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            vertical = self._image(product_dir / "2:3", "vertical.JPEG")
            transparent = self._image(product_dir / "1:1", "transparent.png")
            parameter = self._image(product_dir / "尺码信息表", "parameter.webp")
            (product_dir / "2:3" / "说明.txt").write_text("ignored", encoding="utf-8")

            result = reader(product_dir)

        self.assertIsInstance(result, assets_type)
        self.assertTrue(result.__dataclass_params__.frozen)
        self.assertEqual(result.vertical_image, vertical)
        self.assertEqual(result.transparent_image, transparent)
        self.assertEqual(result.parameter_image, parameter)

    def test_supports_real_fullwidth_directories_and_prefers_transparent_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            vertical = self._image(product_dir / "2：3图", "1.jfif")
            transparent = self._image(product_dir / "透明素材图", "1.png")
            for index in range(5):
                self._image(product_dir / "1：1主图", "{0}.jpg".format(index + 1))
            parameter = self._image(product_dir / "尺码信息表", "1.jpg")

            try:
                result = tmall_data.read_tmall_assets(product_dir)
            except Exception as error:
                self.fail("真实全角目录应可读取：{0}".format(error))

        self.assertEqual(result.vertical_image, vertical)
        self.assertEqual(result.transparent_image, transparent)
        self.assertEqual(result.parameter_image, parameter)

    def test_falls_back_to_fullwidth_single_image_group_without_guessing_main_image(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            self._image(product_dir / "2:3")
            transparent = self._image(product_dir / "1：1", "transparent.png")
            for index in range(5):
                self._image(product_dir / "1：1主图", "{0}.jpg".format(index + 1))
            self._image(product_dir / "尺码信息表")

            try:
                result = tmall_data.read_tmall_assets(product_dir)
            except Exception as error:
                self.fail("全角 1：1 单图目录应可作为透明素材回退：{0}".format(error))

        self.assertEqual(result.transparent_image, transparent)

    def test_missing_parameter_group_raises_tmall_data_error(self):
        with tempfile.TemporaryDirectory() as directory:
            product_dir = Path(directory)
            self._image(product_dir / "2:3")
            self._image(product_dir / "透明素材图")

            try:
                tmall_data.read_tmall_assets(product_dir)
            except getattr(tmall_data, "TmallDataError") as error:
                self.assertIn("尺码信息表", str(error))
            except Exception as error:
                self.fail("缺少素材目录不应泄漏底层异常：{0}".format(error))
            else:
                self.fail("缺少尺码信息表目录时必须失败")

    def test_rejects_more_than_one_image_in_every_asset_group(self):
        groups = ("2:3", "透明素材图", "尺码信息表")
        for duplicated_group in groups:
            with self.subTest(
                group=duplicated_group
            ), tempfile.TemporaryDirectory() as directory:
                product_dir = Path(directory)
                for group in groups:
                    self._image(product_dir / group)
                self._image(product_dir / duplicated_group, "2.png")

                with self.assertRaisesRegex(
                    tmall_data.TmallDataError,
                    r"必须恰好 1 张.*当前为 2 张",
                ):
                    tmall_data.read_tmall_assets(product_dir)

    def test_rejects_empty_existing_directory_for_every_asset_group(self):
        groups = ("2:3", "透明素材图", "尺码信息表")
        for empty_group in groups:
            with self.subTest(
                group=empty_group
            ), tempfile.TemporaryDirectory() as directory:
                product_dir = Path(directory)
                for group in groups:
                    group_dir = product_dir / group
                    if group == empty_group:
                        group_dir.mkdir(parents=True)
                    else:
                        self._image(group_dir)

                try:
                    tmall_data.read_tmall_assets(product_dir)
                except tmall_data.TmallDataError as error:
                    self.assertRegex(str(error), r"必须恰好 1 张.*当前为 0 张")
                except Exception as error:
                    self.fail("空素材目录不应泄漏底层异常：{0}".format(error))
                else:
                    self.fail("空素材目录必须失败")


if __name__ == "__main__":
    unittest.main()
