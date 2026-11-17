import copy
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import kuaimai_erp as erp
from jd_data import JdFields
from jd_form_listing import JD_BRAND, JD_CATEGORY_PATH, JdFormListing, JdFormListingError
from pdd_data import PddFields
from pdd_form_listing import PddFormListing, PddFormListingError


LOGGER = logging.getLogger(__name__)


class SavedPlatformReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def _run_shared_save(self, platform, *, corrupt_saved_values=False):
        """Simulate Save resetting the old grid, independently of server data.

        Keep the orchestration and platform readback/validators real; replace
        browser I/O and unrelated category/image editing with controlled data.
        """
        if platform == "pdd":
            listing_type, field_type = PddFormListing, PddFields
            expected = {"拼单价": "599", "单买价": "600", "库存": "100"}
            defaults = ["0", "1", "0"]
            symbol = "PddFormListing"
        else:
            listing_type, field_type = JdFormListing, JdFields
            expected = {"京东价": "599", "库存": "100"}
            defaults = ["0", "0"]
            symbol = "JdFormListing"
        good_snapshot = {"headers": list(expected), "rows": [list(expected.values())] * 5}
        default_snapshot = {"headers": list(expected), "rows": [defaults] * 5}
        old_drawer = SimpleNamespace(is_visible=AsyncMock(return_value=True), snapshot=None)
        fresh_drawer = SimpleNamespace(snapshot=None)
        page = SimpleNamespace(set_default_timeout=Mock(), on=Mock())
        state = {"reloaded": False, "fill_count": 0, "save_count": 0}

        async def reload(**_kwargs):
            state["reloaded"] = True

        async def open_editor(*_args, **_kwargs):
            self.assertTrue(state["reloaded"], "readback must fetch saved server data")
            return fresh_drawer

        async def save(*_args, **_kwargs):
            state["save_count"] += 1
            self.assertEqual(old_drawer.snapshot, good_snapshot)
            fresh_drawer.snapshot = copy.deepcopy(
                default_snapshot if corrupt_saved_values else old_drawer.snapshot
            )
            old_drawer.snapshot = copy.deepcopy(default_snapshot)
            return {"result": 1, "confirmed_by": "toast"}

        def listing_factory(page, drawer, logger, **kwargs):
            listing = listing_type(page, drawer, logger, **kwargs)
            listing.open = AsyncMock(return_value=listing)
            listing._sku_table_snapshot = AsyncMock(side_effect=lambda: drawer.snapshot)

            async def apply(*_args, **_kwargs):
                state["fill_count"] += 1
                drawer.snapshot = copy.deepcopy(good_snapshot)
                return {"attributes": {"attributes": {}}}

            listing.apply_excel_fields = apply
            if platform == "jd":
                listing.scroll_label_into_view = AsyncMock()
                listing.scroll_color_image_groups_into_view = AsyncMock()
                listing._category_text = AsyncMock(return_value=" > ".join(JD_CATEGORY_PATH))
                listing._form_item_exact = AsyncMock()
                listing._read_brand_value = AsyncMock(return_value=(JD_BRAND, (JD_BRAND,)))
                listing._size_spec_binding_snapshot = AsyncMock(return_value=())
                listing._verify_single_material = AsyncMock(return_value={})
                listing._verify_persisted_attributes = AsyncMock(return_value={})
                listing._read_input_item = AsyncMock(return_value="599")
                listing._read_delivery_template = AsyncMock(return_value="48小时")
            return listing

        page.reload = reload
        shared = {
            "playwright": object(), "context": object(), "page": page,
            "drawer": old_drawer, "record": {}, "style_code": "NGBL-2136",
        }
        fields = field_type({**expected, "市场价": "599", "商品分类": "男士休闲裤"})
        product = SimpleNamespace(
            style_code="NGBL-2136", category_hints=(), douyin_fields=None,
            main_images=(), main_images_34=(), sku_images=(),
            **{platform + "_fields": fields},
        )
        args = erp.build_parser().parse_args(["--platform", platform, "--save-only"])
        erp.validate_execution_mode(args)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            erp, symbol, side_effect=listing_factory
        ), patch.object(erp, "open_product_editor", side_effect=open_editor), patch.object(
            erp, "click_save_and_confirm", side_effect=save
        ), patch.object(erp, "dismiss_publish_progress_dialog", new_callable=AsyncMock), patch.object(
            erp, "safe_screenshot", new_callable=AsyncMock
        ):
            output = Path(directory)
            await erp.run_browser_automation(
                args, product, output, LOGGER, shared_session=shared,
            )
            report = json.loads((output / (platform + "-after-save-validation.json")).read_text())
            self.assertTrue((output / "save-result.json").exists())
        self.assertEqual(report["row_count"], 5)
        self.assertIs(shared["drawer"], fresh_drawer)
        self.assertEqual(state, {"reloaded": True, "fill_count": 1, "save_count": 1})

    async def test_pdd_shared_save_reads_server_instead_of_reset_old_grid(self):
        await self._run_shared_save("pdd")

    async def test_jd_shared_save_reads_server_instead_of_reset_old_grid(self):
        await self._run_shared_save("jd")

    async def test_pdd_still_rejects_wrong_server_prices_and_inventory(self):
        with self.assertRaisesRegex(PddFormListingError, "第1行拼单价='0'"):
            await self._run_shared_save("pdd", corrupt_saved_values=True)

    async def test_jd_still_rejects_wrong_server_prices_and_inventory(self):
        with self.assertRaisesRegex(JdFormListingError, "第1行京东价='0'"):
            await self._run_shared_save("jd", corrupt_saved_values=True)
