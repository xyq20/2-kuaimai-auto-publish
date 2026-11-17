import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

import kuaimai_erp as erp
from local_review.app import create_app
from local_review.config import PROJECT_DIR, Settings
from local_review.database import migrate, transaction, utc_now
from local_review.launcher import Launcher, ProductCatalog, atomic_json, build_command, external_runner_active
from local_review.security import create_user
from local_review.service import ApiError


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.product = self.root / 'products' / '测试款 + 1001' / '产品信息.xlsx'
        self.product.parent.mkdir(parents=True)
        book = Workbook()
        book.active.append(['属性', '内容'])
        book.active.append(['商品标题', '棉质测试上衣'])
        book.active.append(['货号', '1001'])
        book.active.append(['商品分类', '男装/上衣'])
        book.save(self.product)
        book.close()
        self.settings = Settings(self.root/'data', 'private-device-token', products_root=self.root/'products', review_port=8899)
        migrate(self.settings)
        with transaction(self.settings) as connection:
            create_user(connection, 'admin', '1234', role='admin')
            create_user(connection, 'reviewer', '4321', role='operator')
        self.context = TestClient(create_app(self.settings), base_url='http://testserver')
        self.client = self.context.__enter__()
        self.launcher = self.client.app.state.launcher
        self.product_id = next(iter(self.launcher.catalog.paths()))
        self.payload = dict(request_key='a'*32, product_id=self.product_id, platforms=['jd','xhs'], mode='save', learning=True)
        self.headers = {'Origin':'http://testserver'}

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, role='admin'):
        response = self.client.post('/api/login', headers=self.headers, json={'username':role, 'password':'1234' if role=='admin' else '4321'})
        self.assertEqual(response.status_code,200)

    def fake_start(self, payload=None):
        child = Mock(pid=987654)
        child.poll.return_value = None
        with patch('local_review.launcher.external_runner_active',return_value=False), patch('local_review.launcher.subprocess.Popen',return_value=child):
            return self.launcher.start(payload or self.payload, 'admin')

    def test_catalog_resolves_real_excel_but_excludes_symlinks_outside_root(self):
        detail = self.launcher.catalog.detail(self.product_id)
        self.assertEqual(detail['title'],'棉质测试上衣')
        self.assertEqual(detail['style_code'],'1001')
        outside = self.root/'outside'
        outside.mkdir()
        (outside/'产品信息.xlsx').write_bytes(self.product.read_bytes())
        (self.settings.products_root/'escape').symlink_to(outside, target_is_directory=True)
        self.assertEqual(len(self.launcher.catalog.listing()),1)
        with self.assertRaises(ApiError): self.launcher.catalog.path('../../outside')
        folder = self.product.parent/'1：1主图'
        folder.mkdir()
        (folder/'1.png').symlink_to(self.root/'data/review.sqlite3')
        self.assertIsNone(self.launcher.catalog.image(self.product_id))

    def test_health_reports_product_root_state(self):
        self.login()
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['products_root_exists'])
        self.assertEqual(body['products_root'], str(self.settings.products_root))

    def test_catalog_missing_mount_is_clear_error(self):
        with self.assertRaises(ApiError) as error:
            ProductCatalog(self.root/'not-mounted').listing()
        self.assertEqual(error.exception.code,'products_unavailable')

    def test_catalog_history_matches_old_and_new_smb_mount_paths(self):
        self.assertEqual(
            ProductCatalog._folder_key(
                Path('/Volumes/共享文件/谭/products/测试款 + 1001')
            ),
            ProductCatalog._folder_key(
                Path.home() / 'Desktop/共享文件/谭/products/测试款 + 1001'
            ),
        )

    def test_catalog_groups_only_successful_saves_for_the_same_product_folder(self):
        runs = self.root / 'runs'
        catalog = ProductCatalog(self.settings.products_root, runs)
        other = self.settings.products_root / '同货号不同颜色+1001' / '产品信息.xlsx'
        other.parent.mkdir()
        other.write_bytes(self.product.read_bytes())
        run = runs / '20260926-120000'
        (run / 'jd').mkdir(parents=True)
        atomic_json(run / 'input-summary.json', {'excel_path': str(self.product)})
        atomic_json(run / 'jd/save-result.json', {'result': 0})
        self.assertFalse(any(p['processed'] for p in catalog.listing()))
        atomic_json(run / 'jd/save-result.json', {'result': 1, 'action': '保存'})
        products = {p['id']: p for p in catalog.listing()}
        self.assertTrue(products[self.product_id]['processed'])
        self.assertEqual(products[self.product_id]['processed_platforms'], ['京东'])
        self.assertFalse(products[self.product_id]['base_saved'])
        self.assertFalse(next(p for p in products.values() if p['id'] != self.product_id)['processed'])

    def test_catalog_marks_successful_base_save_separately(self):
        runs = self.root / 'runs'
        catalog = ProductCatalog(self.settings.products_root, runs)
        run = runs / '20260926-130000'
        (run / 'base').mkdir(parents=True)
        atomic_json(run / 'input-summary.json', {'excel_path': str(self.product)})
        atomic_json(run / 'base/base-save-result.json', {'result': 1, 'action': '保存'})

        item = catalog.listing()[0]
        self.assertTrue(item['base_saved'])
        self.assertEqual(item['processed_platforms'], ['商品资料'])

    def test_catalog_detail_refreshes_base_saved_after_cached_excel_read(self):
        runs = self.root / 'runs'
        catalog = ProductCatalog(self.settings.products_root, runs)
        self.assertFalse(catalog.detail(self.product_id)['base_saved'])
        run = runs / '20260926-130500'
        (run / 'base').mkdir(parents=True)
        atomic_json(run / 'input-summary.json', {'excel_path': str(self.product)})
        atomic_json(run / 'base/save-result.json', {'result': 1, 'action': '保存'})
        self.assertTrue(catalog.detail(self.product_id)['base_saved'])

    def test_catalog_refresh_discovers_new_workbook_with_filename_whitespace(self):
        catalog = self.launcher.catalog
        self.assertEqual(len(catalog.listing()), 1)
        added = self.settings.products_root / '新款+2002' / '产品信息 .xlsx'
        added.parent.mkdir()
        added.write_bytes(self.product.read_bytes())
        listing = catalog.listing()
        self.assertEqual(len(listing), 2)
        item = next(item for item in listing if item['title'] == '新款+2002')
        self.assertEqual(catalog.path(item['id']), added.resolve())
        self.assertEqual(catalog.detail(item['id'])['title'], '棉质测试上衣')

    def test_catalog_prefers_canonical_filename_without_duplicate_product(self):
        (self.product.parent / '产品信息 .xlsx').write_bytes(self.product.read_bytes())
        listing = self.launcher.catalog.listing()
        self.assertEqual(len(listing), 1)
        self.assertEqual(self.launcher.catalog.path(listing[0]['id']), self.product.resolve())

    def test_open_product_folder_requires_admin_and_uses_catalog_path(self):
        endpoint = f'/api/launcher/products/{self.product_id}/open-folder'
        self.assertEqual(self.client.post(endpoint, headers=self.headers, json={}).status_code, 401)
        self.login('reviewer')
        self.assertEqual(self.client.post(endpoint, headers=self.headers, json={}).status_code, 403)
        self.client.post('/api/logout', headers=self.headers, json={})
        self.login()
        with patch('local_review.launcher.subprocess.run') as opened:
            response = self.client.post(endpoint, headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['opened'])
        self.assertEqual(opened.call_args.args[0], ['/usr/bin/open', str(self.product.parent.resolve())])

    def test_history_search_finds_product_beyond_recent_hundred(self):
        self.login()
        for index in range(101):
            job_id = f'{index:032x}'
            directory = self.launcher.root / job_id
            directory.mkdir()
            atomic_json(directory / 'job.json', {
                'id': job_id, 'status': 'completed', 'title': '旧款皮鞋' if index == 0 else '新款',
                'style_code': 'MY019-1' if index == 0 else f'NEW-{index}',
                'platforms': ['xhs'], 'mode': 'save', 'created_by': 'admin',
                'created_at': f'2026-09-30T{index // 60:02d}:{index % 60:02d}:00+08:00',
                'output_dir': str(directory),
            })
        recent = self.client.get('/api/launcher/jobs').json()['jobs']
        self.assertEqual(len(recent), 101)
        self.assertTrue(any(job['style_code'] == 'MY019-1' for job in recent))
        searched = self.client.get('/api/launcher/jobs', params={'search': 'MY019-1 小红书'}).json()
        self.assertEqual(searched['total_matches'], 1)
        self.assertEqual(searched['jobs'][0]['style_code'], 'MY019-1')

    def test_job_detail_exposes_zero_judgment_reason_and_call_failures(self):
        self.login()
        job = self.fake_start()
        directory = self.launcher.root / job['id']
        stored = json.loads((directory/'job.json').read_text())
        output = Path(stored['output_dir'])
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output/'model-judgments.json', {
            'decisions': [], 'events': [{'key':'request', 'status':'unavailable', 'field_label':'商品类目',
                                        'api_called':False, 'reason':'未配置 DeepSeek key'}],
            'summary': {'api_call_count':0, 'not_called_reason':'未配置 DeepSeek key'},
        })
        response = self.client.get('/api/launcher/jobs/'+job['id'])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['model_summary']['api_call_count'], 0)
        self.assertIn('未配置', body['model_events'][0]['reason'])
        self.assertEqual(body['model_decisions'], [])

    def test_old_zero_judgment_report_does_not_claim_no_api_calls(self):
        job = self.fake_start()
        stored = json.loads((self.launcher.root/job['id']/'job.json').read_text())
        output = Path(stored['output_dir']); output.mkdir(parents=True, exist_ok=True)
        atomic_json(output/'model-judgments.json', {'decisions': []})
        (self.launcher.root/job['id']/'run.log').write_text('ERROR | 小红书类目路径不匹配\n')
        body = self.launcher._public(stored, logs=True)
        self.assertIsNone(body['model_summary']['api_call_count'])
        self.assertIn('旧版', body['model_summary']['not_called_reason'])
        self.assertIn('路径不匹配', body['model_summary']['stop_reason'])

    def test_api_requires_login_admin_and_same_origin_for_mutations(self):
        self.assertEqual(self.client.get('/api/launcher/products').status_code,401)
        self.login('reviewer')
        self.assertEqual(self.client.get('/api/launcher/products').status_code,200)
        self.assertEqual(self.client.post('/api/launcher/jobs',json=self.payload,headers=self.headers).status_code,403)
        self.assertEqual(self.client.post('/api/launcher/jobs/'+'a'*32+'/stop',json={},headers=self.headers).status_code,403)
        self.login()
        self.assertEqual(self.client.post('/api/launcher/jobs',json=self.payload).status_code,403)
        self.assertEqual(self.client.post('/api/launcher/jobs',json=self.payload,headers={'Origin':'http://evil.example'}).status_code,403)

    def test_dashboard_and_review_share_session_but_keep_review_controls(self):
        self.login()
        dashboard = self.client.get('/')
        self.assertIn('id="nav-review"',dashboard.text)
        self.assertIn('id="preview-run"',dashboard.text)
        self.assertIn("frame-src 'self'",dashboard.headers['content-security-policy'])
        review = self.client.get('/review?embedded=1')
        self.assertIn('id="option-search"',review.text)
        self.assertIn('class="embedded"',review.text)
        self.assertIn("frame-ancestors 'self'",review.headers['content-security-policy'])
        self.assertEqual(self.client.get('/workbench/app.js').status_code,200)
        self.assertEqual(self.client.get('/workbench/config.py').status_code,404)

    def test_commands_preserve_single_multi_order_and_execution_gates(self):
        for names in (['taobao'], ['xhs','jd','douyin']):
            for mode in ('preview','save','publish'):
                config = {**self.payload,'platforms':names,'mode':mode,'confirm_publish':mode=='publish'}
                argv = build_command(self.product,config,'b'*32,self.root/'output',self.settings)
                args = erp.build_parser().parse_args(argv[3:])
                erp.validate_execution_mode(args)
                self.assertEqual(args.platform,','.join(names))
                self.assertEqual(args.save,mode!='preview')
                self.assertEqual(args.save_only,mode=='save')
                self.assertEqual(args.run_id,'b'*32)
                self.assertEqual(args.output_dir,self.root/'output')
                self.assertEqual(args.learning_api_url,'http://127.0.0.1:8899')
                self.assertEqual(args.allow_taobao_save_once,names==['taobao'] and mode=='save')
                self.assertEqual(args.allow_taobao_publish_once,names==['taobao'] and mode=='publish')
                self.assertNotIn(self.settings.device_token,' '.join(argv))

    def test_platform_save_command_can_prepend_base_stage(self):
        config = {**self.payload, 'platforms':['jd','xhs'], 'include_base':True}
        argv = build_command(self.product, config, 'b'*32, self.root/'output', self.settings)
        args = erp.build_parser().parse_args(argv[3:])
        erp.validate_execution_mode(args)
        self.assertEqual(args.platform, 'base,jd,xhs')

    def test_platform_save_command_with_auto_base_keeps_taobao_stage_authorization(self):
        config = {**self.payload, 'platforms':['taobao'], 'include_base':True}
        argv = build_command(self.product, config, 'b'*32, self.root/'output', self.settings)
        args = erp.build_parser().parse_args(argv[3:])
        erp.validate_execution_mode(args)
        self.assertEqual(args.platform, 'base,taobao')
        self.assertFalse(args.allow_taobao_save_once)

    def test_web_start_auto_adds_base_for_product_without_base_save(self):
        result = self.fake_start()
        self.assertEqual(result['platforms'], ['base', 'jd', 'xhs'])
        launch = json.loads((self.launcher.root / result['id'] / 'launch.json').read_text())
        self.assertIn('--platform', launch['argv'])
        self.assertEqual(launch['argv'][launch['argv'].index('--platform') + 1], 'base,jd,xhs')

    def test_web_start_does_not_repeat_base_after_successful_base_save(self):
        run = self.root / 'runs' / '20260926-130100'
        (run / 'base').mkdir(parents=True)
        atomic_json(run / 'input-summary.json', {'excel_path': str(self.product)})
        atomic_json(run / 'base/save-result.json', {'result': 1, 'action': '保存'})
        self.launcher.catalog.runs_root = run.parent

        result = self.fake_start()
        self.assertEqual(result['platforms'], ['jd', 'xhs'])
        launch = json.loads((self.launcher.root / result['id'] / 'launch.json').read_text())
        self.assertEqual(launch['argv'][launch['argv'].index('--platform') + 1], 'jd,xhs')

    def test_invalid_commands_and_unconfirmed_publish_never_spawn(self):
        invalid = [dict(platforms=[]),dict(platforms=['jd','jd']),dict(platforms=['jd;touch /tmp/bad']),
                   dict(platforms='jd'),dict(learning='true'),dict(mode='publish'),dict(operation='base',mode='preview'),dict(operation='create',mode='publish')]
        with patch('local_review.launcher.external_runner_active',return_value=False), patch('local_review.launcher.subprocess.Popen') as spawn:
            for change in invalid:
                with self.subTest(change=change),self.assertRaises(ApiError):
                    self.launcher.start({**self.payload,**change},'admin')
            spawn.assert_not_called()

    def test_base_and_create_only_save_and_do_not_enable_attribute_review(self):
        for operation in ('base','create'):
            args = build_command(self.product,{**self.payload,'operation':operation,'platforms':[]},'b'*32,self.root/'output',self.settings)
            parsed = erp.build_parser().parse_args(args[3:])
            erp.validate_execution_mode(parsed)
            self.assertEqual(parsed.platform,'base')
            self.assertTrue(parsed.save_only)
            self.assertEqual(parsed.create_product,operation=='create')
            self.assertFalse(parsed.learning_enabled)

    def test_duplicate_request_and_concurrent_job_do_not_spawn_again(self):
        first = self.fake_start()
        with patch.object(self.launcher,'_owned',return_value=True),patch('local_review.launcher.subprocess.Popen') as spawn:
            self.assertEqual(self.launcher.start(self.payload,'admin')['id'],first['id'])
            with self.assertRaises(ApiError) as error:
                self.launcher.start({**self.payload,'request_key':'c'*32},'admin')
            self.assertEqual(error.exception.code,'job_already_running')
            spawn.assert_not_called()

    def test_external_cli_prevents_a_second_job(self):
        with patch('local_review.launcher.external_runner_active',return_value=True),self.assertRaises(ApiError) as error:
            self.launcher.start(self.payload,'admin')
        self.assertEqual(error.exception.code,'external_job_running')

    def test_receipt_survives_service_restart_and_redacts_logs(self):
        first = self.fake_start()
        directory = self.launcher.root/first['id']
        atomic_json(directory/'result.json',dict(exit_code=0,cancelled=False,finished_at=utc_now()))
        (directory/'run.log').write_text('完成 '+self.settings.device_token)
        restarted = Launcher(self.settings)
        result = restarted.get(first['id'])
        self.assertEqual(result['status'],'completed')
        self.assertTrue(all(s['status']=='success' for s in result['stages']))
        self.assertNotIn(self.settings.device_token,result['log'])
        self.assertNotIn('pid',result)
        self.assertNotIn('output_dir',result)

    def test_lost_process_is_interrupted_never_success_and_not_signalled(self):
        first = self.fake_start()
        with patch('local_review.launcher.process_command',return_value='unrelated-program'),patch('local_review.launcher.os.killpg') as kill:
            result = Launcher(self.settings).stop(first['id'])
            self.assertEqual(result['status'],'interrupted')
            kill.assert_not_called()

    def test_skipped_stage_stays_skipped_after_checkpoint_progress_and_completion(self):
        first = self.fake_start()
        stored = json.loads((self.launcher.root / first['id'] / 'job.json').read_text())
        output = Path(stored['output_dir'])
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output / 'all-platform-result.json', [{'platform': 'jd', 'status': 'skipped'}])
        with transaction(self.settings) as db:
            now = utc_now()
            db.execute("INSERT INTO products(product_version,style_code,title,created_at,updated_at) VALUES('p','1001','test',?,?)", (now, now))
            db.execute("INSERT INTO run_checkpoints(run_id,product_version,device_id,execution_mode,platform_order_json,current_index,status,updated_at) VALUES(?,'p','device','save_only','[\"base\",\"jd\",\"xhs\"]',2,'running',?)", (first['id'], now))
        for status in ('running', 'completed'):
            with self.subTest(status=status):
                body = self.launcher._public({**stored, 'status': status})
                stages = {row['platform']: row['status'] for row in body['stages']}
                self.assertEqual(stages['jd'], 'skipped')
                self.assertEqual(stages['base'], 'success')
                self.assertEqual(stages['xhs'], 'running' if status == 'running' else 'success')

    def test_stop_signals_only_owned_group_and_waits_for_actual_exit(self):
        first = self.fake_start()
        with patch.object(self.launcher,'_owned',return_value=True), patch('local_review.launcher.os.killpg') as kill:
            self.assertEqual(self.launcher.stop(first['id'])['status'],'stopping')
            kill.assert_called_once_with(987654,signal.SIGINT)
        atomic_json(self.launcher.root/first['id']/'result.json',dict(exit_code=130,cancelled=True,finished_at=utc_now()))
        self.assertEqual(self.launcher.get(first['id'])['status'],'stopped')

    def test_checkpoint_correlates_to_web_job_and_preserves_prior_stages(self):
        first = self.fake_start()
        with transaction(self.settings) as db:
            now=utc_now()
            db.execute("INSERT INTO products(product_version,style_code,title,created_at,updated_at) VALUES('p','1001','test',?,?)",(now,now))
            db.execute("INSERT INTO run_checkpoints(run_id,product_version,device_id,execution_mode,platform_order_json,current_index,status,updated_at) VALUES(?, 'p','device','save_only','[\"base\",\"jd\",\"xhs\"]',1,'running',?)",(first['id'],now))
        with patch.object(self.launcher,'_owned',return_value=True):
            result=self.launcher.get(first['id'])
        self.assertEqual(result['stages'],[{'platform':'base','status':'success'},{'platform':'jd','status':'running'},{'platform':'xhs','status':'pending'}])


class WorkerReceiptTests(unittest.TestCase):
    def test_real_worker_records_cli_exit_and_cooperative_stop(self):
        for should_stop in (False,True):
            with self.subTest(stop=should_stop),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                code = "import time; print('ready',flush=True); time.sleep(30)" if should_stop else "print('checked'); raise SystemExit(7)"
                atomic_json(root/'launch.json',{'argv':[sys.executable,'-u','-c',code]})
                with (root/'run.log').open('w') as log:
                    process=subprocess.Popen([sys.executable,'-u','-m','local_review.launcher_worker',str(root)],cwd=PROJECT_DIR,stdout=log,stderr=log,start_new_session=True)
                    try:
                        if should_stop:
                            deadline=time.monotonic()+10
                            while 'ready' not in (root/'run.log').read_text() and time.monotonic()<deadline: time.sleep(.05)
                            self.assertIn('ready',(root/'run.log').read_text())
                            os.killpg(process.pid,signal.SIGINT)
                        process.wait(timeout=10)
                    finally:
                        if process.poll() is None: os.killpg(process.pid,signal.SIGKILL); process.wait()
                receipt=json.loads((root/'result.json').read_text())
                self.assertEqual(receipt['cancelled'],should_stop)
                self.assertNotEqual(receipt['exit_code'],0)
                if not should_stop:self.assertEqual(receipt['exit_code'],7)

    def test_external_detection_ignores_shell_inspection_and_finds_python_runner(self):
        shell='zsh zsh -lc ps -Ao args= | rg kuaimai_erp.py'
        runner='Python /Applications/Xcode.app/Contents/Developer/usr/bin/python3 /Users/a/项目/kuaimai_erp.py --platform jd'
        with patch('local_review.launcher.subprocess.check_output',return_value=shell): self.assertFalse(external_runner_active())
        with patch('local_review.launcher.subprocess.check_output',return_value=runner): self.assertTrue(external_runner_active())
