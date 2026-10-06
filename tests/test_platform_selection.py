import json
import logging
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import kuaimai_erp as erp
from douyin_listing import DouyinListingError
from learning_client import RetryableCloudError


LOGGER = logging.getLogger(__name__)


def batch_args(platforms, mode):
    # Convert parser exits into assertion failures inside Python 3.9 async tests.
    try:
        return erp.build_parser().parse_args(["--platform", platforms, mode])
    except SystemExit as exc:
        raise AssertionError(f"valid platform selection rejected: {platforms}") from exc


class PlatformSelectionTests(unittest.TestCase):
    def test_all_mode_resume_and_skip_preserve_existing_behavior(self):
        args = erp.build_parser().parse_args([
            "--platform", "all", "--save-only", "--all-platform-start-at", "xhs",
            "--all-platform-skip", "youzan",
        ])
        erp.validate_execution_mode(args)
        self.assertEqual(erp.platform_execution_stages(args, SimpleNamespace(douyin_fields=object())), ("xhs", "jd"))

    def test_selecting_every_commerce_platform_does_not_implicitly_add_base(self):
        platforms = "jd,douyin,taobao,tmall,pdd,wxsph,xhs,youzan"
        args = erp.build_parser().parse_args(["--platform", platforms, "--save-only"])
        erp.validate_execution_mode(args)
        self.assertEqual(erp.platform_execution_stages(args, SimpleNamespace(douyin_fields=object())), tuple(platforms.split(",")))

    def test_cli_normalizes_aliases_separators_and_duplicates(self):
        args = erp.build_parser().parse_args([
            "--platform", "fxg，jd xhs、douyin", "--save-only",
        ])
        selected = erp.validate_execution_mode(args)
        self.assertEqual(args.platform, "douyin,jd,xhs")
        self.assertEqual(tuple(item.cli_name for item in selected), ("douyin", "jd", "xhs"))

    def test_invalid_platform_list_is_rejected(self):
        for value in ("", ",", "douyin,unknown", "all,jd"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                erp.build_parser().parse_args(["--platform", value, "--no-save"])

    def test_combination_keeps_single_platform_only_modes_restricted(self):
        for flag in (
            "--inspect-only", "--create-product", "--jd-publish-preview",
            "--wash-label-upload-test", "--all-platform-one-shop-test",
        ):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                args = erp.build_parser().parse_args([
                    "--platform", "douyin,jd,xhs", "--no-save", flag,
                ])
                erp.validate_execution_mode(args)

    def test_custom_preview_cannot_include_forced_base_save(self):
        args = erp.build_parser().parse_args(["--platform", "base,jd", "--no-save"])
        with self.assertRaisesRegex(SystemExit, "基础资料"):
            erp.validate_execution_mode(args)

    def test_main_loads_douyin_inputs_and_dispatches_only_selected_platforms(self):
        for platforms, include_douyin in (("douyin,jd,xhs", True), ("jd,xhs", False)):
            with self.subTest(platforms=platforms), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                product = SimpleNamespace(
                    style_code="TEST", title="商品", base_price="100",
                    main_images=(), main_images_34=(), detail_images=(), sku_images=(),
                )
                stack.enter_context(patch("sys.argv", [
                    "kuaimai_erp.py", "--platform", platforms, "--save-only",
                ]))
                stack.enter_context(patch.object(erp, "SCRIPT_DIR", Path(directory)))
                stack.enter_context(patch.object(erp, "setup_logging", side_effect=lambda path, **kw: (path.mkdir(parents=True), LOGGER)[1]))
                stack.enter_context(patch.object(erp, "resolve_excel_path", return_value=Path("input.xlsx")))
                read = stack.enter_context(patch.object(erp, "read_product_data", return_value=product))
                stack.enter_context(patch.object(erp, "product_summary", return_value={}))
                stack.enter_context(patch.object(erp, "create_learning_context", return_value=None))
                browser = stack.enter_context(patch.object(erp, "run_browser_automation", new_callable=AsyncMock))
                stack.enter_context(patch.object(erp, "close_shared_browser_session", new_callable=AsyncMock))
                single = stack.enter_context(patch.object(erp, "run_single_platform_with_learning", new_callable=AsyncMock))
                self.assertEqual(erp.main(), 0)
                self.assertEqual(read.call_args.kwargs["include_douyin"], include_douyin)
                self.assertEqual([call.args[0].platform for call in browser.await_args_list], platforms.split(","))
                self.assertTrue(all(call.args[0].save_only for call in browser.await_args_list))
                single.assert_not_awaited()


class PlatformBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_douyin_readback_retries_initial_stale_price(self):
        verify = AsyncMock(side_effect=[
            DouyinListingError("保存后第 1 个 SKU 的价格不一致：页面为 0"),
            {"sku_verified_rows": 10},
        ])
        with patch.object(erp.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            report = await erp.retry_douyin_saved_readback(verify)

        self.assertEqual(report["sku_verified_rows"], 10)
        self.assertEqual(verify.await_count, 2)
        sleep.assert_awaited_once_with(0.25)

    async def test_saved_readback_reload_replaces_shared_editor(self):
        page = SimpleNamespace(reload=AsyncMock())
        old_drawer = object()
        new_drawer = object()
        shared_session = {"drawer": old_drawer}
        product = SimpleNamespace(style_code="MY019-1")
        with patch.object(
            erp, "open_product_editor", new_callable=AsyncMock, return_value=new_drawer
        ) as open_editor:
            drawer, reopened = await erp.product_editor_for_saved_readback(
                page,
                old_drawer,
                shared_session,
                product,
                timeout_seconds=30,
                logger=LOGGER,
                platform_label="抖音资料持久化结果",
                force_reload=True,
            )

        self.assertTrue(reopened)
        self.assertIs(drawer, new_drawer)
        self.assertIs(shared_session["drawer"], new_drawer)
        page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded", timeout=30000
        )
        open_editor.assert_awaited_once()

    async def test_review_wait_survives_temporary_service_restart(self):
        class StopAfterRetry(Exception):
            pass

        client = Mock()
        client.poll_resume.side_effect = [
            RetryableCloudError("cloud request failed"), StopAfterRetry(),
        ]
        context = SimpleNamespace(client=client, device_id="device")
        review = SimpleNamespace(review_id="review-1")
        with patch.object(erp, "save_learning_checkpoint", return_value=object()), patch.object(
            erp, "drain_learning_events", new_callable=AsyncMock
        ), patch.object(erp.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(StopAfterRetry):
                await erp.wait_for_platform_review_batch(
                    context, SimpleNamespace(), ("pdd",), 0, (review,), LOGGER,
                )

        self.assertEqual(client.poll_resume.call_count, 2)
        sleep.assert_awaited_once_with(5)

    async def _run(self, platforms, mode, *, product=None, failure_at=None):
        args = batch_args(platforms, mode)
        erp.validate_execution_mode(args)
        calls = []
        sessions = []

        async def browser(stage, product, directory, logger, **kwargs):
            calls.append(stage)
            sessions.append(kwargs["shared_session"])
            if stage.platform == failure_at:
                raise erp.AutomationError("fixture failure")

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(erp, "run_browser_automation", side_effect=browser), patch.object(
                erp, "close_shared_browser_session", new_callable=AsyncMock
            ) as close:
                try:
                    await erp.run_all_implemented_platforms(
                        args, product or SimpleNamespace(douyin_fields=object()), Path(directory), LOGGER,
                    )
                except erp.AutomationError:
                    if failure_at is None:
                        raise
                close.assert_awaited_once()
            result = json.loads((Path(directory) / "all-platform-result.json").read_text())
        self.assertTrue(all(session is sessions[0] for session in sessions))
        return calls, result

    async def test_custom_selection_runs_only_chosen_platforms_in_order_in_each_mode(self):
        for mode in ("--no-save", "--save-only", "--save"):
            with self.subTest(mode=mode):
                calls, result = await self._run("douyin,jd,xhs", mode)
                self.assertEqual([item.platform for item in calls], ["douyin", "jd", "xhs"])
                self.assertEqual([row["platform"] for row in result], ["douyin", "jd", "xhs"])
                self.assertTrue(all(row["status"] == "success" for row in result))
                self.assertTrue(all(item.save == (mode != "--no-save") for item in calls))
                self.assertTrue(all(item.save_only == (mode == "--save-only") for item in calls))

    async def test_taobao_authorization_applies_only_to_its_selected_stage(self):
        for mode in ("--no-save", "--save-only", "--save"):
            with self.subTest(mode=mode):
                calls, _ = await self._run("jd,taobao,xhs", mode)
                self.assertEqual([item.platform for item in calls], ["jd", "taobao", "xhs"])
                self.assertEqual([item.allow_taobao_save_once for item in calls], [False, mode == "--save-only", False])
                self.assertEqual([item.allow_taobao_publish_once for item in calls], [False, mode == "--save", False])

    async def test_failure_stops_before_later_platforms_and_records_results(self):
        calls, result = await self._run("douyin,jd,xhs", "--save-only", failure_at="jd")
        self.assertEqual([item.platform for item in calls], ["douyin", "jd"])
        self.assertEqual([row["status"] for row in result], ["success", "failed"])

    async def test_custom_selection_does_not_silently_drop_douyin_without_inputs(self):
        calls, _ = await self._run("douyin,jd", "--no-save", product=SimpleNamespace(douyin_fields=None))
        self.assertEqual([item.platform for item in calls], ["douyin", "jd"])

    async def test_all_mode_retains_base_save_and_default_platform_order(self):
        calls, _ = await self._run("all", "--save-only")
        self.assertEqual([item.platform for item in calls], [
            "base", "douyin", "taobao", "tmall", "pdd", "wxsph", "xhs", "youzan", "jd",
        ])
        calls, _ = await self._run("all", "--no-save")
        self.assertNotIn("base", [item.platform for item in calls])

    async def test_review_resume_and_checkpoints_keep_custom_platform_order(self):
        args = batch_args("douyin,jd,xhs", "--save-only")
        context = SimpleNamespace(
            client=object(), store=Mock(), run_id="run", product_version="product",
            device_id="device", image_version="images", attribute_runtime=None,
        )
        review = erp.ReviewRequired("review", SimpleNamespace(field_label="字段"), "unmatched", "snapshot")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            erp, "run_browser_automation", new_callable=AsyncMock, side_effect=[None, review, None, None]
        ) as browser, patch.object(erp, "initialize_learning_run", new_callable=AsyncMock), patch.object(
            erp, "wait_for_platform_review_batch", new_callable=AsyncMock
        ) as wait, patch.object(erp, "close_shared_browser_session", new_callable=AsyncMock):
            await erp.run_all_implemented_platforms(
                args, SimpleNamespace(douyin_fields=object()), Path(directory), LOGGER, context,
            )
        self.assertEqual([call.args[0].platform for call in browser.await_args_list], ["douyin", "jd", "jd", "xhs"])
        self.assertEqual(wait.await_args.args[2:4], (("douyin", "jd", "xhs"), 1))
        checkpoints = [call.args[0] for call in context.store.save_checkpoint.call_args_list]
        self.assertTrue(all(item.platform_order == ("douyin", "jd", "xhs") for item in checkpoints))
        self.assertEqual((checkpoints[-1].status, checkpoints[-1].current_index), ("completed", 3))
