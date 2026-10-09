import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import kuaimai_erp as erp
from learning_assets import PreparedLearningAsset
from learning_client import PermanentCloudError, RetryableCloudError
from learning_store import LearningStore


class LearningInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_category_change_registers_without_conflicting_with_prior_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / 'learning.sqlite3')
            store.migrate()
            old_payload = {'product_version': 'current', 'style_code': '2025',
                           'title': 'sweater', 'category_json': {'hints': ['男装 > 针织衫', '毛衣']}}
            cloud_events = {'product.upsert:current': old_payload}
            old_id = store.enqueue('product.upsert:current', 'product.upsert', old_payload)
            store.mark_delivered(old_id)
            client = Mock()

            def post(key, event_type, payload):
                if key in cloud_events and cloud_events[key] != payload:
                    raise PermanentCloudError('cloud status 409 (idempotency_payload_conflict)')
                cloud_events[key] = payload
                return {'event_id': key}

            client.post_event.side_effect = post
            client.analyze.return_value = {'status': 'done'}
            product = SimpleNamespace(style_code='2025', title='sweater',
                                      category_hints=('男装 > 针织衫/毛衣',),
                                      main_images=(), main_images_34=(), detail_images=())
            keys = []
            for run in ('first', 'rerun'):
                context = erp.LearningRunContext(store, run, 'current', 'device', 'images', client)
                await erp.initialize_learning_run(context, product, logging.getLogger(__name__))
                self.assertTrue(context.initialized)
                keys.append(client.post_event.call_args.args[0])
            self.assertEqual(keys[0], keys[1], '同数据复跑必须沿用去重编号')
            self.assertEqual(cloud_events[keys[0]]['category_json']['hints'], ['男装 > 针织衫/毛衣'])
            self.assertEqual(len(cloud_events), 2)
            self.assertEqual(store.pending_outbox(), ())
            store.connection.close()

    async def test_learning_requires_review_service_address(self):
        args = SimpleNamespace(learning_enabled=True, learning_api_url="")
        with self.assertRaisesRegex(erp.AutomationError, "未配置审核服务地址"):
            erp.create_learning_context(args, object())

    async def test_unmatched_excel_page_field_blocks_save_unless_review_says_no_fill(self):
        report = {"attributes": {"skipped_values": {"鞋垫材质": "皮革"}}}
        with self.assertRaisesRegex(erp.AutomationError, "鞋垫材质=皮革"):
            erp.require_resolved_page_attributes("douyin", report)
        runtime = Mock()
        runtime.intentionally_omitted.return_value = True
        erp.require_resolved_page_attributes("douyin", report, runtime)
        runtime.intentionally_omitted.assert_called_once_with("douyin", "鞋垫材质")

    async def test_current_product_is_registered_before_assets_despite_backlog(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / 'learning.sqlite3')
            store.migrate()
            for index in range(58):
                store.enqueue(f'old:{index}', 'checkpoint.updated', {'index': index})
            payload = {'product_version': 'current', 'style_code': '1228-1',
                       'title': 'shoe', 'category_json': {'hints': ['男鞋']}}
            product = SimpleNamespace(style_code='1228-1', title='shoe', category_hints=('男鞋',),
                                      main_images=(Path('image.png'),), main_images_34=(), detail_images=())
            key, _ = erp.product_registration_event('current', product)
            event_id = store.enqueue(key, 'product.upsert', payload)
            registered = set()
            client = Mock()

            def post(key, event_type, data):
                if event_type == 'product.upsert':
                    registered.add(data['product_version'])
                return {'event_id': key}

            def upload(version, *args):
                self.assertIn(version, registered, '图片上传前必须确认当前商品已登记')
                return {'asset_id': 'asset'}

            client.post_event.side_effect = post
            client.upload_asset.side_effect = upload
            client.analyze.return_value = {'status': 'done'}
            context = erp.LearningRunContext(store, 'run', 'current', 'device', 'images', client)
            asset = PreparedLearningAsset('original', 'hash', 'image/png', b'image', None)
            with patch.object(erp, 'prepare_learning_assets', return_value=(asset, asset)):
                await erp.initialize_learning_run(context, product, logging.getLogger(__name__))
            self.assertTrue(context.initialized)
            first_key, first_type, first_payload = client.post_event.call_args_list[0].args
            self.assertTrue(first_key.startswith('product.upsert:current:'))
            self.assertEqual((first_type, first_payload), ('product.upsert', payload))
            self.assertIsNotNone(store.connection.execute('SELECT delivered_at FROM sync_outbox WHERE id=?', (event_id,)).fetchone()[0])
            self.assertEqual(len(store.pending_outbox()), 8)
            client.upload_asset.assert_called_once()
            await erp.initialize_learning_run(context, product, logging.getLogger(__name__))
            client.upload_asset.assert_called_once()
            store.connection.close()

    async def test_registration_failure_prevents_upload_and_keeps_event_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / 'learning.sqlite3')
            store.migrate()
            client = Mock()
            client.post_event.side_effect = RetryableCloudError('cloud status 503')
            context = erp.LearningRunContext(store, 'run', 'current', 'device', 'images', client)
            product = SimpleNamespace(style_code='1228-1', title='shoe', category_hints=('男鞋',),
                                      main_images=(), main_images_34=(), detail_images=())
            with self.assertRaises(RetryableCloudError):
                await erp.initialize_learning_run(context, product, logging.getLogger(__name__))
            self.assertFalse(context.initialized)
            client.upload_asset.assert_not_called()
            client.analyze.assert_not_called()
            self.assertEqual(store.pending_outbox()[0].event_type, 'product.upsert')
            store.connection.close()
