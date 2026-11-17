"""Dewu uses its own catalog, with draft saves and explicit review submission."""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

import kuaimai_erp as erp
from local_review.app import create_app
from local_review.config import Settings
from local_review.database import migrate, transaction
from local_review.launcher import DewuProductCatalog, atomic_json, build_command
from local_review.security import create_user
from local_review.service import ApiError


IMAGE = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jx1sAAAAASUVORK5CYII=')
DRAFT_URL = 'https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/123456?noLayout=1'


def workbook(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.active.append(['属性', '内容'])
    for row in rows:
        book.active.append(row)
    book.save(path)
    book.close()


class DewuWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.normal = self.root / 'products' / '测试款' / '产品信息.xlsx'
        self.dewu = self.root / 'dewu' / '测试款' / '产品信息.xlsx'
        workbook(self.normal, [('商品标题', '普通平台商品'), ('货号', 'ERP-1'), ('商品分类', '男装/裤子')])
        workbook(self.dewu, [('卖点提炼', '双头鹰复古多口袋直筒'), ('货号', 'NGBL-7076'), ('类目', '服装>长裤>工装裤')])
        image_dir = self.dewu.parent / '白底图'
        image_dir.mkdir()
        for name in ('10.png', '2.png'):
            (image_dir / name).write_bytes(IMAGE)
        self.settings = Settings(self.root / 'data', 'test-device-token', products_root=self.root / 'products',
                                 dewu_products_root=self.root / 'dewu')
        migrate(self.settings)
        with transaction(self.settings) as connection:
            create_user(connection, 'admin', '1234', role='admin')
            create_user(connection, 'reviewer', '4321', role='operator')
        self.context = TestClient(create_app(self.settings), base_url='http://testserver')
        self.client = self.context.__enter__()
        self.launcher = self.client.app.state.launcher
        self.launcher.dewu_catalog.runs_root = self.root / 'dewu-runs'
        self.dewu_id = next(iter(self.launcher.dewu_catalog.paths()))
        self.normal_id = next(iter(self.launcher.catalog.paths()))
        self.headers = {'Origin': 'http://testserver'}
        self.payload = {'request_key': 'a' * 32, 'product_id': self.dewu_id, 'operation': 'dewu',
                        'platforms': ['dewu'], 'mode': 'save', 'learning': False}

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, username='admin'):
        response = self.client.post('/api/login', headers=self.headers,
                                    json={'username': username, 'password': '1234' if username == 'admin' else '4321'})
        self.assertEqual(response.status_code, 200)

    def start(self, payload=None):
        child = Mock(pid=987654)
        child.poll.return_value = None
        with patch('local_review.launcher.external_runner_active', return_value=False), \
                patch('local_review.launcher.subprocess.Popen', return_value=child):
            return self.launcher.start(payload or self.payload, 'admin')

    def test_source_catalogs_are_separate_and_ignore_root_templates(self):
        workbook(self.root / 'dewu' / '休闲裤模板.xlsx', [('货号', 'TEMPLATE')])
        workbook(self.root / 'dewu' / '宽松空白' / '产品信息 .xlsx', [('货号', 'NON-CANONICAL')])
        self.login()
        default = self.client.get('/api/launcher/products').json()['products']
        dewu = self.client.get('/api/launcher/products?source=dewu').json()['products']
        self.assertEqual([row['id'] for row in default], [self.normal_id])
        self.assertEqual([row['id'] for row in dewu], [self.dewu_id])
        self.assertNotEqual(self.dewu_id, self.normal_id)
        self.assertEqual(dewu[0]['source'], 'dewu')
        self.assertFalse(dewu[0]['base_saved'])

    def test_dewu_detail_reads_own_excel_and_natural_white_image(self):
        self.login()
        detail = self.client.get(f'/api/launcher/products/{self.dewu_id}?source=dewu').json()
        self.assertEqual(detail['title'], '双头鹰复古多口袋直筒')
        self.assertEqual(detail['style_code'], 'NGBL-7076')
        self.assertEqual(detail['category'], '服装>长裤>工装裤')
        self.assertEqual(self.launcher.dewu_catalog.image(self.dewu_id).name, '2.png')
        self.assertEqual(self.client.get(detail['image']).content, IMAGE)
        self.assertEqual(self.client.get(f'/api/launcher/products/{self.dewu_id}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/launcher/products/{self.normal_id}?source=dewu').status_code, 404)

    def test_unknown_source_and_unmounted_dewu_root_have_clear_errors(self):
        self.login()
        response = self.client.get('/api/launcher/products?source=untrusted')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error'], 'invalid_product_source')
        self.launcher.dewu_catalog = DewuProductCatalog(self.root / 'missing')
        response = self.client.get('/api/launcher/products?source=dewu')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'dewu_products_unavailable')

    def test_catalog_rejects_outside_product_and_image_symlinks(self):
        outside = self.root / 'outside' / '产品信息.xlsx'
        workbook(outside, [('货号', 'OUTSIDE')])
        (self.root / 'dewu' / 'escape').symlink_to(outside.parent, target_is_directory=True)
        self.assertEqual(len(self.launcher.dewu_catalog.paths()), 1)
        for path in (self.dewu.parent / '白底图').iterdir():
            path.unlink()
        (self.dewu.parent / '白底图' / '1.png').symlink_to(outside)
        self.assertIsNone(self.launcher.dewu_catalog.image(self.dewu_id))

    def test_processed_products_require_successful_dewu_draft_receipt(self):
        runs = self.launcher.dewu_catalog.runs_root
        run = runs / 'one'
        run.mkdir(parents=True)
        atomic_json(run / 'input-summary.json', {'excel_path': str(self.dewu)})
        atomic_json(run / 'dewu-result.json', {'status': 'failed'})
        self.assertFalse(self.launcher.dewu_catalog.listing()[0]['processed'])
        atomic_json(run / 'dewu-result.json', {'status': 'draft_saved'})
        item = self.launcher.dewu_catalog.listing()[0]
        self.assertTrue(item['processed'])
        self.assertEqual(item['processed_platforms'], ['得物'])
        self.assertFalse(item['base_saved'])
        atomic_json(run / 'dewu-result.json', {'status': 'already_saved', 'excel_path': str(self.dewu)})
        self.assertTrue(self.launcher.dewu_catalog.detail(self.dewu_id)['processed'])
        self.assertFalse(self.launcher.catalog.listing()[0]['processed'])

    def test_build_command_uses_only_dewu_adapter_and_draft_save(self):
        settings = replace(self.settings, materials_root=self.root / 'monthly-materials')
        argv = build_command(self.dewu, self.payload, 'b' * 32, self.root / 'output', settings)
        args = erp.build_parser().parse_args(argv[3:])
        erp.validate_execution_mode(args)
        self.assertEqual(args.platform, 'dewu')
        self.assertEqual(args.excel_url, str(self.dewu))
        self.assertEqual(args.dewu_root, settings.dewu_products_root)
        self.assertTrue(args.save_only)
        self.assertNotIn('--materials-root', argv)
        self.assertNotIn('--learning-enabled', argv)
        self.assertNotIn('--learning-api-url', argv)
        self.assertFalse(args.learning_enabled)

    def test_invalid_dewu_modes_and_mixed_requests_never_spawn(self):
        invalid = ({'mode': 'preview'}, {'mode': 'publish'}, {'mode': 'publish', 'confirm_publish': False},
                   {'platforms': ['dewu', 'jd']}, {'platforms': ['jd']}, {'platforms': []},
                   {'learning': 'yes'}, {'include_base': True}, {'operation': 'platforms'})
        with patch('local_review.launcher.external_runner_active', return_value=False), \
                patch('local_review.launcher.subprocess.Popen') as spawn:
            for update in invalid:
                with self.subTest(update=update), self.assertRaises(ApiError):
                    self.launcher.start({**self.payload, **update}, 'admin')
            spawn.assert_not_called()

    def test_dewu_can_use_shared_attribute_learning_and_review(self):
        result = self.start({**self.payload, 'learning': True})
        command = json.loads((self.launcher.root / result['id'] / 'launch.json').read_text())['argv']
        args = erp.build_parser().parse_args(command[3:])
        erp.validate_execution_mode(args)
        self.assertTrue(result['learning'])
        self.assertTrue(args.learning_enabled)
        self.assertEqual(args.learning_api_url, 'http://127.0.0.1:8787')
        self.assertEqual(args.platform, 'dewu')
        self.assertNotIn('--materials-root', command)

    def test_review_mode_requires_confirmation_and_uses_dedicated_flag(self):
        payload = {**self.payload, 'mode': 'publish', 'confirm_publish': True}
        result = self.start(payload)
        command = json.loads((self.launcher.root / result['id'] / 'launch.json').read_text())['argv']
        self.assertIn('--dewu-submit-review', command)
        self.assertNotIn('--save-only', command)
        args = erp.build_parser().parse_args(command[3:])
        erp.validate_execution_mode(args)
        self.assertTrue(args.dewu_submit_review)
        self.assertEqual(result['mode'], 'publish')
        self.assertFalse(result['base_auto_added'])

    def test_default_product_cannot_be_sent_to_dewu_adapter(self):
        with patch('local_review.launcher.external_runner_active', return_value=False), \
                patch('local_review.launcher.subprocess.Popen') as spawn, self.assertRaises(ApiError):
            self.launcher.start({**self.payload, 'product_id': self.normal_id}, 'admin')
        spawn.assert_not_called()

    def test_started_job_does_not_add_base_or_use_erp_output(self):
        result = self.start()
        self.assertEqual(result['platforms'], ['dewu'])
        self.assertEqual(result['stages'][0]['platform'], 'dewu')
        self.assertFalse(result['base_auto_added'])
        self.assertFalse(result['learning'])
        command = json.loads((self.launcher.root / result['id'] / 'launch.json').read_text())['argv']
        self.assertEqual(command[command.index('--platform') + 1], 'dewu')
        output = Path(command[command.index('--output-dir') + 1])
        self.assertEqual(output.parent.parts[-3:], ('output', 'dewu', 'runs'))

    def test_public_history_exposes_only_official_draft_urls(self):
        started = self.start()
        path = self.launcher.root / started['id'] / 'job.json'
        stored = json.loads(path.read_text())
        output = self.root / 'report'
        output.mkdir()
        stored.update(output_dir=str(output), status='completed')
        atomic_json(path, stored)
        for url, valid in ((DRAFT_URL, True), ('https://example.com/edit', False),
                           ('https://stark.dewu.com:bad/vueProduct/newProductApply/spuEdit/operation/123456', False)):
            atomic_json(output / 'dewu-result.json', {'status': 'draft_saved', 'draft_url': url})
            result = self.launcher.get(started['id'])
            self.assertEqual(result['dewu_status'], 'draft_saved')
            self.assertEqual('draft_url' in result, valid)
        self.assertEqual(self.launcher.search_history('得物')['total_matches'], 1)

    def test_product_routes_require_session_and_job_start_requires_admin_and_origin(self):
        self.assertEqual(self.client.get('/api/launcher/products?source=dewu').status_code, 401)
        self.login('reviewer')
        self.assertEqual(self.client.get('/api/launcher/products?source=dewu').status_code, 200)
        self.assertEqual(self.client.post('/api/launcher/jobs', headers=self.headers, json=self.payload).status_code, 403)
        self.client.post('/api/logout', headers=self.headers, json={})
        self.login()
        self.assertEqual(self.client.post('/api/launcher/jobs', json=self.payload).status_code, 403)

    def test_open_folder_uses_selected_dewu_source(self):
        self.login()
        with patch('local_review.launcher.subprocess.run') as opened:
            response = self.client.post(f'/api/launcher/products/{self.dewu_id}/open-folder?source=dewu',
                                        headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(opened.call_args.args[0], ['/usr/bin/open', str(self.dewu.parent.resolve())])

    def test_env_has_independent_dewu_root(self):
        with patch.dict(os.environ, {'DEWU_PRODUCTS_ROOT': str(self.root / 'custom-dewu')}):
            self.assertEqual(Settings.from_env().dewu_products_root, self.root / 'custom-dewu')


if __name__ == '__main__':
    unittest.main()
