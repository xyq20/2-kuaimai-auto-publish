import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LauncherArgumentTests(unittest.TestCase):
    def test_bilibili_menu_modes_and_explicit_publish(self):
        for selection, flag in [('1', '--no-save'), ('2', '--save-only'), ('3\nPUBLISH', '--bilibili-publish')]:
            arguments, _ = self._run_launcher(input_text='13\n' + selection + '\n')
            self.assertEqual(arguments, ('kuaimai_erp.py', '--platform', 'bilibili', flag))

    def test_bilibili_menu_uses_own_products_and_ignores_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / 'bili' / '寒战' / '产品信息.xlsx'
            product.parent.mkdir(parents=True)
            product.touch()
            (root / 'bili' / '毛衣模板.xlsx').touch()
            normal = root / 'normal' / '其他商品' / '产品信息.xlsx'
            normal.parent.mkdir(parents=True)
            normal.touch()
            arguments, output = self._run_launcher(input_text='13\n1\n2\n', extra_env={
                'KUAIMAI_SKIP_PRODUCT_SELECTION': '0', 'BILIBILI_PRODUCTS_ROOT': str(root / 'bili'),
                'BILIBILI_LAST_PRODUCT_FILE': str(root / 'last-bili'), 'KUAIMAI_PRODUCTS_ROOT': str(root / 'normal')})
            self.assertEqual(arguments, ('kuaimai_erp.py', '--excel-url', str(product), '--platform', 'bilibili', '--save-only'))
            self.assertNotIn('毛衣模板', output)
            self.assertNotIn('其他商品', output)

    def test_all_except_youzan_menu_is_preserved(self):
        arguments, _ = self._run_launcher(input_text="11\n2\n")
        self.assertEqual(arguments, (
            "kuaimai_erp.py", "--all-platform-skip", "youzan",
            "--platform", "all", "--save-only",
        ))

    def test_platform_menu_accepts_multiple_platforms_directly(self):
        arguments, output = self._run_launcher(input_text="2,9,7\n2\n")
        self.assertEqual(arguments, (
            "kuaimai_erp.py", "--platform", "douyin,jd,xhs", "--save-only",
        ))
        self.assertIn("平台编号可多选", output)
        self.assertIn("12) 得物", output)
        self.assertNotIn("自定义多平台", output)
        self.assertIn("抖音 → 京东 → 小红书", output)

    def test_platform_menu_accepts_separators_and_removes_duplicates(self):
        arguments, _ = self._run_launcher(input_text="9，2、9 7\n1\n")
        self.assertEqual(arguments, (
            "kuaimai_erp.py", "--platform", "jd,douyin,xhs", "--no-save",
        ))

    def test_platform_menu_retries_invalid_or_empty_selection(self):
        arguments, output = self._run_launcher(input_text="14\n\n2,0\n2,1\n2,10\n2,11\n9,12\n2,xhs\n2,7\n1\n")
        self.assertEqual(arguments, (
            "kuaimai_erp.py", "--platform", "douyin,xhs", "--no-save",
        ))
        self.assertIn("请输入 2-9 的平台编号", output)

    def test_multiple_platform_publish_keeps_confirmation(self):
        arguments, _ = self._run_launcher(input_text="2,3,9\n3\nPUBLISH\n")
        self.assertEqual(arguments, (
            "kuaimai_erp.py", "--platform", "douyin,taobao,jd", "--save",
        ))
        arguments, _ = self._run_launcher(input_text="2,3,9\n3\nno\n")
        self.assertEqual(arguments, (
            "kuaimai_erp.py", "--platform", "douyin,taobao,jd", "--no-save",
        ))

    def test_repeated_taobao_selection_keeps_save_authorization(self):
        arguments, _ = self._run_launcher(input_text="3,3\n2\n")
        self.assertEqual(arguments, (
            "kuaimai_erp.py", "--platform", "taobao", "--save-only",
            "--allow-taobao-save-once",
        ))

    def test_multiple_platforms_work_with_review_launcher_environment(self):
        arguments, _ = self._run_launcher(
            input_text="2,9,7\n1\n",
            extra_env={"KUAIMAI_LEARNING_AUTO_ENABLE": "1"},
        )
        self.assertEqual(arguments, (
            "kuaimai_erp.py", "--platform", "douyin,jd,xhs", "--no-save",
            "--learning-enabled",
        ))

    def test_new_product_menu_saves_without_publishing(self):
        arguments, _ = self._run_launcher(input_text="10\n2\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "base", "--create-product", "--save-only"))

    def test_new_product_menu_rejects_publish_mode(self):
        arguments, output = self._run_launcher(input_text="10\n3\n1\n")
        self.assertIn("请选择 1 或 2", output)
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "base", "--create-product", "--no-save"))

    def test_dewu_menu_defaults_to_draft_unless_review_is_confirmed(self):
        arguments, output = self._run_launcher(input_text="12\n3\nno\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "dewu", "--save-only"))
        self.assertIn("未确认提交审核", output)
        self.assertIn("完整填写并保存草稿（不提交审核）", output)
        self.assertNotIn("确认请输入大写 PUBLISH", output)
        arguments, _ = self._run_launcher(input_text="12\n1\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "dewu", "--no-save"))
        arguments, _ = self._run_launcher(input_text="12\n3\nREVIEW\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "dewu", "--dewu-submit-review"))

    def test_dewu_cli_arguments_are_forwarded_without_erp_defaults(self):
        user_args = ("--platform=dewu", "--dewu-product", "双头鹰", "--save-only")
        arguments, output = self._run_launcher(user_args)
        self.assertEqual(arguments, ("kuaimai_erp.py", *user_args))
        self.assertNotIn("请选择需要运行的平台", output)

    def test_dewu_product_menu_uses_its_own_root_and_remembered_product(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            erp_excel = root / "erp" / "其他平台商品" / "产品信息.xlsx"
            dewu_excel = root / "dewu" / "双头鹰" / "产品信息.xlsx"
            erp_excel.parent.mkdir(parents=True)
            dewu_excel.parent.mkdir(parents=True)
            erp_excel.touch()
            dewu_excel.touch()
            (root / "dewu" / "休闲裤模板.xlsx").touch()
            erp_last = root / "erp-last"
            erp_last.write_text(str(erp_excel) + "\n", encoding="utf-8")
            dewu_last = root / "dewu-last"
            env = {
                "KUAIMAI_SKIP_PRODUCT_SELECTION": "0",
                "KUAIMAI_PRODUCTS_ROOT": str(root / "erp"),
                "KUAIMAI_LAST_PRODUCT_FILE": str(erp_last),
                "DEWU_PRODUCTS_ROOT": str(root / "dewu"),
                "DEWU_LAST_PRODUCT_FILE": str(dewu_last),
            }
            arguments, output = self._run_launcher(input_text="12\n1\n2\n", extra_env=env)
            self.assertEqual(arguments, (
                "kuaimai_erp.py", "--excel-url", str(dewu_excel),
                "--platform", "dewu", "--save-only",
            ))
            self.assertNotIn("其他平台商品", output)
            self.assertNotIn("休闲裤模板", output)
            self.assertEqual(erp_last.read_text(encoding="utf-8").strip(), str(erp_excel))
            self.assertEqual(dewu_last.read_text(encoding="utf-8").strip(), str(dewu_excel))
            repeated, output = self._run_launcher(input_text="12\n\n1\n", extra_env=env)
            self.assertIn("[上次选择]", output)
            self.assertEqual(repeated[-3:], ("--platform", "dewu", "--no-save"))

    def test_double_click_menu_allows_jd_no_save_preview(self):
        arguments, output = self._run_launcher(input_text="9\n1\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "jd", "--no-save"))
        self.assertIn("京东（填写、保存、铺货）", output)

    def test_double_click_menu_allows_jd_save_only(self):
        arguments, _output = self._run_launcher(input_text="9\n2\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "jd", "--save-only"))

    def test_double_click_menu_allows_jd_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="9\n3\nPUBLISH\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "jd", "--save"))
        self.assertIn("的指定店铺提交铺货", output)

    def test_create_cli_defaults_to_base_and_preserves_equals_platform(self):
        arguments, _ = self._run_launcher(("--create-product", "--no-save"))
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "base", "--create-product", "--no-save"))
        arguments, _ = self._run_launcher(("--platform=base", "--create-product", "--no-save"))
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform=base", "--create-product", "--no-save"))

    def _run_launcher(self, user_arguments=(), input_text="", extra_env=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run.command"
            shutil.copy2(PROJECT_ROOT / "run.command", launcher)
            launcher.chmod(0o755)
            fake_python = root / ".venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/bin/zsh\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"pip\" ]]; then exit 0; fi\n"
                "print -r -- RUN_ARGS_START\n"
                "for argument in \"$@\"; do print -r -- \"$argument\"; done\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            completed = subprocess.run(
                ["/bin/zsh", str(launcher), *user_arguments],
                input=input_text,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
                env={**__import__("os").environ,
                     "KUAIMAI_SKIP_PRODUCT_SELECTION": "1", **(extra_env or {})},
            )
        output = completed.stdout.splitlines()
        marker = output.index("RUN_ARGS_START")
        return tuple(output[marker + 1 :]), completed.stdout

    def test_double_click_menu_selects_pdd_no_save_preview(self):
        arguments, output = self._run_launcher(input_text="5\n1\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "pdd", "--no-save"),
        )
        self.assertIn("请选择需要运行的平台", output)
        self.assertIn("不会改动基础资料", output)

    def test_double_click_menu_identifies_pdd_as_part_of_all(self):
        _arguments, output = self._run_launcher(input_text="0\n1\n")

        self.assertIn("基础资料 + 抖音 + 淘宝 + 天猫 + 拼多多 + 微信小店 + 小红书", output)

    def test_double_click_menu_allows_wxsph_save_without_publish(self):
        arguments, output = self._run_launcher(input_text="6\n2\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "wxsph", "--save-only"),
        )
        self.assertIn("微信小店（视频号，填写、保存、铺货）", output)

    def test_double_click_menu_allows_wxsph_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="6\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "wxsph", "--save"),
        )
        self.assertIn("的指定店铺提交铺货", output)

    def test_double_click_menu_selects_xhs_form_preview_without_platform_save(self):
        arguments, output = self._run_launcher(input_text="7\n1\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "xhs", "--no-save"),
        )
        self.assertIn("小红书（填写、保存、铺货）", output)

    def test_double_click_menu_allows_xhs_save_without_publish(self):
        arguments, output = self._run_launcher(input_text="7\n2\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "xhs", "--save-only"),
        )
        self.assertNotIn("只允许不保存预览", output)

    def test_double_click_menu_allows_xhs_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="7\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "xhs", "--save"),
        )
        self.assertIn("的指定店铺提交铺货", output)

    def test_double_click_menu_selects_youzan_no_save_preview(self):
        arguments, output = self._run_launcher(input_text="8\n1\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "youzan", "--no-save"),
        )
        self.assertIn("有赞（填写、保存、铺货）", output)

    def test_double_click_menu_allows_youzan_save_only(self):
        arguments, _output = self._run_launcher(input_text="8\n2\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "youzan", "--save-only"),
        )

    def test_double_click_menu_allows_youzan_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="8\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "youzan", "--save"),
        )
        self.assertIn("的指定店铺提交铺货", output)

    def test_double_click_menu_allows_pdd_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="5\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "pdd", "--save"),
        )
        self.assertIn("的指定店铺提交铺货", output)

    def test_double_click_menu_allows_tmall_save_only_without_once_flag(self):
        arguments, output = self._run_launcher(input_text="4\n2\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "tmall", "--save-only"),
        )
        self.assertNotIn("只允许不保存预览", output)

    def test_double_click_menu_runs_tmall_formal_publish_without_once_flag(self):
        arguments, output = self._run_launcher(input_text="4\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "tmall", "--save"),
        )
        self.assertNotIn("allow-tmall-publish-once", arguments)

    def test_double_click_menu_can_select_taobao_save_without_publish(self):
        arguments, _output = self._run_launcher(input_text="3\n2\n")

        self.assertEqual(
            arguments,
            (
                "kuaimai_erp.py",
                "--platform",
                "taobao",
                "--save-only",
                "--allow-taobao-save-once",
            ),
        )

    def test_publish_requires_exact_confirmation_and_cancel_falls_back_to_no_save(self):
        arguments, output = self._run_launcher(input_text="3\n3\nno\n")

        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "taobao", "--no-save"))
        self.assertIn("未确认，已改为平台仅填写/检查", output)
        self.assertNotIn("--allow-taobao-publish-once", arguments)

    def test_command_line_arguments_bypass_menu_and_are_forwarded(self):
        arguments, output = self._run_launcher(
            ("--platform", "xhs", "--inspect-only", "--no-save")
        )

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "xhs", "--inspect-only", "--no-save"),
        )
        self.assertNotIn("请选择需要运行的平台", output)

    def test_command_line_defaults_platform_to_all_when_omitted(self):
        arguments, _output = self._run_launcher(("--no-save",))

        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "all", "--no-save"))

    def test_learning_helper_environment_adds_flag_without_changing_menu_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run.command"
            shutil.copy2(PROJECT_ROOT / "run.command", launcher)
            launcher.chmod(0o755)
            fake_python = root / ".venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/bin/zsh\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"pip\" ]]; then exit 0; fi\n"
                "print -r -- RUN_ARGS_START\n"
                "for argument in \"$@\"; do print -r -- \"$argument\"; done\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            completed = subprocess.run(
                ["/bin/zsh", str(launcher)],
                input="9\n1\n",
                check=True,
                capture_output=True,
                text=True,
                env={**__import__("os").environ, "KUAIMAI_LEARNING_AUTO_ENABLE": "1",
                     "KUAIMAI_SKIP_PRODUCT_SELECTION": "1"},
            )
        output = completed.stdout.splitlines()
        marker = output.index("RUN_ARGS_START")
        self.assertEqual(
            tuple(output[marker + 1 :]),
            ("kuaimai_erp.py", "--platform", "jd", "--no-save", "--learning-enabled"),
        )

    def test_product_menu_scans_and_remembers_last_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products = root / "products"
            first = products / "A款" / "产品信息.xlsx"
            second = products / "B 款" / "产品信息.xlsx"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.touch()
            second.touch()
            last_file = root / "state" / "last-product-path"
            env = {"KUAIMAI_SKIP_PRODUCT_SELECTION": "0",
                   "KUAIMAI_PRODUCTS_ROOT": str(products),
                   "KUAIMAI_LAST_PRODUCT_FILE": str(last_file)}
            arguments, output = self._run_launcher(input_text="9\n2\n1\n", extra_env=env)
            self.assertIn("B 款", output)
            self.assertEqual(arguments,
                ("kuaimai_erp.py", "--excel-url", str(second), "--platform", "jd", "--no-save"))
            arguments, output = self._run_launcher(input_text="9\n\n1\n", extra_env=env)
            self.assertIn("[上次选择]", output)
            self.assertEqual(arguments,
                ("kuaimai_erp.py", "--excel-url", str(second), "--platform", "jd", "--no-save"))


if __name__ == "__main__":
    unittest.main()
