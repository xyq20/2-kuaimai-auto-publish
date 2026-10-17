from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

from local_review.app import create_app
from local_review.config import Settings
from local_review.database import migrate, transaction
from local_review.launcher import MonthlyProductCatalog, ProductCatalog, atomic_json
from local_review.security import create_user
from local_review.service import ApiError


class MonthlyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.month = self.root/'materials/26.10'
        self.month.mkdir(parents=True)
        self.legacy = self.root/'legacy'
        self.legacy.mkdir()
        self.runs = self.root/'runs'
        self.catalog = MonthlyProductCatalog(self.legacy, self.runs, materials_root=self.month.parent)
        clock = patch('local_review.launcher.current_month_directory', return_value=self.month)
        clock.start()
        self.addCleanup(clock.stop)
        resolver_clock = patch('product_materials.current_month_directory', return_value=self.month)
        resolver_clock.start()
        self.addCleanup(resolver_clock.stop)

    def artwork(self, name):
        folder = self.month/name
        (folder/'1：1主图').mkdir(parents=True)
        (folder/'1：1主图/1.png').write_bytes(b'image')
        return folder

    def workbook(self, folder, nickname, code='CODE-1', filename='产品信息.xlsx'):
        folder.mkdir(parents=True, exist_ok=True)
        book = Workbook()
        for row in (('属性','内容'), ('商品标题',f'【{nickname}】产品标题'), ('货号',code), ('颜色','咖啡色')):
            book.active.append(row)
        path = folder/filename
        book.save(path)
        book.close()
        return path

    def test_only_current_month_products_with_excel_are_listed(self):
        ready = self.artwork('10.13【太阳照常升起】')
        excel = self.workbook(ready, '太阳照常升起')
        self.artwork('10.14【旅行家】')
        self.workbook(self.root/'materials/26.9月份/9.1【旧款】', '旧款')
        listing = self.catalog.listing()
        self.assertEqual([p['title'] for p in listing], [ready.name])
        self.assertTrue(listing[0]['runnable'])
        self.assertEqual(self.catalog.path(listing[0]['id']), excel)
        self.assertEqual(self.catalog.detail(listing[0]['id'])['style_code'], 'CODE-1')
        self.assertEqual(self.catalog.image(listing[0]['id']), ready/'1：1主图/1.png')

    def test_refresh_discovers_added_excel_and_removes_deleted_excel(self):
        folder = self.artwork('10.14【旅行家】')
        self.assertEqual(self.catalog.listing(), [])
        excel = self.workbook(folder, '旅行家', filename='产品信息 .xlsx')
        item = self.catalog.listing()[0]
        self.assertEqual(self.catalog.path(item['id']), excel)
        canonical = self.workbook(folder, '旅行家')
        self.assertEqual(self.catalog.path(item['id']), canonical)
        canonical.unlink()
        excel.unlink()
        self.assertEqual(self.catalog.listing(), [])
        with self.assertRaises(ApiError) as error:
            self.catalog.path(item['id'])
        self.assertEqual(error.exception.code, 'product_excel_missing')

    def test_legacy_excel_can_be_linked_and_local_excel_takes_precedence(self):
        folder = self.artwork('10.9【轰炸机】')
        old = self.workbook(self.legacy/'轰炸机+NGBL-2096', '轰炸机', 'NGBL-2096')
        item = self.catalog.listing()[0]
        self.assertEqual(self.catalog.path(item['id']), old)
        local = self.workbook(folder, '轰炸机', 'NGBL-2096')
        self.assertEqual(self.catalog.path(item['id']), local)

    def test_ambiguous_external_excel_is_hidden(self):
        folder = self.artwork('10.9【轰炸机】')
        self.workbook(self.legacy/'轰炸机+NGBL-2096', '轰炸机', 'NGBL-2096')
        self.workbook(self.legacy/'另一个简称+NGBL-2096', '轰炸机', 'NGBL-2096')
        self.assertEqual(self.catalog.listing(), [])
        self.workbook(folder, '轰炸机', 'NGBL-2096')
        self.assertEqual(len(self.catalog.listing()), 1)

    def test_local_workbook_works_without_old_library_mount_and_rejects_escaping_files(self):
        folder = self.artwork('10.13【太阳照常升起】')
        excel = self.workbook(folder, '太阳照常升起')
        self.legacy.rmdir()
        self.assertEqual(len(self.catalog.listing()), 1)
        excel.unlink()
        outside = self.workbook(self.root/'outside', '太阳照常升起')
        excel.symlink_to(outside)
        self.assertEqual(self.catalog.listing(), [])

    def test_optional_old_library_failure_does_not_hide_local_excel(self):
        folder = self.artwork('10.13【太阳照常升起】')
        self.workbook(folder, '太阳照常升起')
        with patch.object(ProductCatalog, 'paths', side_effect=ApiError(503, 'products_unavailable')):
            self.assertEqual(len(self.catalog.listing()), 1)

    def test_saved_history_survives_excel_move_without_merging_other_products(self):
        folder = self.artwork('10.9【轰炸机】')
        old = self.workbook(self.legacy/'轰炸机+NGBL-2096', '轰炸机', 'NGBL-2096')
        run = self.runs/'success'
        (run/'tmall').mkdir(parents=True)
        (run/'base').mkdir()
        atomic_json(run/'input-summary.json', {'excel_path':str(old), 'product_dir':str(old.parent)})
        atomic_json(run/'tmall/save-result.json', {'result':1})
        atomic_json(run/'base/base-save-result.json', {'result':1})
        self.workbook(folder, '轰炸机', 'NGBL-2096')
        other = self.artwork('10.13【太阳照常升起】')
        self.workbook(other, '太阳照常升起', 'NGBL-2143')
        items = {p['title']:p for p in self.catalog.listing()}
        self.assertTrue(items[folder.name]['processed'])
        self.assertTrue(self.catalog.detail(items[folder.name]['id'])['base_saved'])
        self.assertIn('天猫', items[folder.name]['processed_platforms'])
        self.assertFalse(items[other.name]['processed'])

    def test_month_rollover_removes_previous_products(self):
        old = self.artwork('10.13【太阳照常升起】')
        self.workbook(old, '太阳照常升起')
        november = self.root/'materials/26.11月份'
        new = november/'11.1【新款】'
        self.workbook(new, '新款')
        with patch('local_review.launcher.current_month_directory', return_value=november), \
             patch('product_materials.current_month_directory', return_value=november):
            self.assertEqual([p['title'] for p in self.catalog.listing()], [new.name])

    def test_workbench_api_discovers_local_excel_and_launches_that_workbook(self):
        folder = self.artwork('10.13【太阳照常升起】')
        excel = self.workbook(folder, '太阳照常升起', 'NGBL-2143')
        self.artwork('10.14【旅行家】')
        settings = Settings(self.root/'data', 'device', products_root=self.legacy,
                            materials_root=self.month.parent)
        migrate(settings)
        with transaction(settings) as connection:
            create_user(connection, 'admin', '1234', role='admin')
        with TestClient(create_app(settings)) as client:
            origin = {'Origin':'http://testserver'}
            self.assertEqual(client.post('/api/login', headers=origin,
                                        json={'username':'admin','password':'1234'}).status_code, 200)
            products = client.get('/api/launcher/products').json()['products']
            self.assertEqual([p['title'] for p in products], [folder.name])
            product_id = products[0]['id']
            self.assertEqual(client.get(f'/api/launcher/products/{product_id}/image').content, b'image')
            child = Mock(pid=987654)
            child.poll.return_value = None
            with patch('local_review.launcher.external_runner_active', return_value=False), \
                 patch('local_review.launcher.subprocess.Popen', return_value=child):
                response = client.post('/api/launcher/jobs', headers=origin, json={
                    'request_key':'a'*32, 'product_id':product_id, 'mode':'preview',
                    'platforms':['tmall'], 'learning':False})
            self.assertEqual(response.status_code, 201, response.text)
            launch = client.app.state.launcher.root/response.json()['id']/'launch.json'
            import json
            argv = json.loads(launch.read_text())['argv']
            self.assertEqual(argv[argv.index('--excel-url') + 1], str(excel))
            with patch('local_review.launcher.subprocess.run') as opened:
                reply = client.post(f'/api/launcher/products/{product_id}/open-folder', headers=origin, json={})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(opened.call_args.args[0], ['/usr/bin/open', str(folder)])


if __name__ == '__main__':
    unittest.main()
