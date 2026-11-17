import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import kuaimai_erp as erp
from attribute_runtime import AttributeRuntime
from learning_store import LearningStore
from ocr_semantics import OCRSemanticSession
from tests.test_ocr_semantics import unfamiliar_tokens
import size_image_recognition as recognition


class OCRRunRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_decisions_explain_early_validation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            erp.write_model_judgment_report(root, None, OCRSemanticSession(),
                run_status='failed', stop_reason='小红书类目确认前路径不匹配')
            report = json.loads((root/'model-judgments.json').read_text())
            self.assertEqual(report['summary']['api_call_count'], 0)
            self.assertIn('路径不匹配', report['summary']['not_called_reason'])
            self.assertIn('未调用', (root/'model-judgments.html').read_text())

    async def test_ocr_recovery_does_not_discard_other_pending_fields(self):
        from tests.test_attribute_runtime import make_length_request
        from learning_models import RunCheckpoint
        from ocr_semantics import OCRJudgmentRequired
        with tempfile.TemporaryDirectory() as directory:
            store=LearningStore(Path(directory)/'learning.sqlite3');store.migrate()
            client=Mock();client.post_event.return_value={'ok':True}
            client.review_learning.return_value={'status':'review_required'}
            runtime=AttributeRuntime(store,client,'run','product')
            runtime.begin_review_collection('tmall')
            await runtime.resolve(make_length_request(platform_id='tmall',field_id='unmapped',field_label='测试歧义属性'))
            pending=runtime.deferred_reviews
            store.save_checkpoint(RunCheckpoint('run','product','save_only',('xhs','tmall'),1,'running',None,1,'device','images'))
            context=erp.LearningRunContext(store,'run','product','device','images',client,runtime)
            error=OCRJudgmentRequired(Path('chart.jpg'),unfamiliar_tokens(),'clothing',lambda _:None,'unknown')
            with patch('ocr_review.resolve_ocr_failure',new=AsyncMock()), patch.object(erp,'wait_for_platform_review_batch',new=AsyncMock()) as wait:
                await erp.recover_ocr_with_learning(context,SimpleNamespace(save=True,save_only=True,platform='all'),OCRSemanticSession(),error,logging.getLogger(__name__))
            self.assertEqual(wait.call_count,1)
            self.assertEqual(wait.call_args.args[4],pending)
            store.close()

    async def test_real_review_resume_keeps_later_stage_identity(self):
        from fastapi.testclient import TestClient
        from local_review.app import create_app
        from local_review.config import Settings
        from local_review.database import migrate, transaction
        from local_review.security import create_user
        from learning_models import RunCheckpoint, checkpoint_event_payload
        from ocr_semantics import OCRJudgmentRequired
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            settings=Settings(root/'review','device-secret')
            migrate(settings)
            with transaction(settings) as connection:
                create_user(connection,'admin','1234',role='admin')
            api=TestClient(create_app(settings))
            headers={'Authorization':'Device device-secret'}
            origin={'Origin':'http://testserver'}
            self.assertEqual(api.post('/api/login',headers=origin,json={'username':'admin','password':'1234'}).status_code,200)
            test=self
            class Cloud:
                def post_event(self,key,event_type,payload):
                    reply=api.post('/api/device/events',headers=headers,json={'idempotency_key':key,'event_type':event_type,'payload':payload})
                    test.assertIn(reply.status_code,(200,201),reply.text)
                    return reply.json()
                def review_learning(self,payload):
                    reply=api.post('/api/device/review-learning',headers=headers,json=payload)
                    test.assertEqual(reply.status_code,200,reply.text)
                    return reply.json()
                def ocr_judge(self,payload):
                    return {'status':'review_required','reason_code':'deepseek_not_configured'}
                def upload_asset(self,product_version,sha256,kind,content_type,body):
                    reply=api.put('/api/device/assets/'+sha256,
                        headers={**headers,'X-Product-Version':product_version,
                                 'X-Asset-Kind':kind,'Content-Type':content_type},content=body)
                    test.assertIn(reply.status_code,(200,201),reply.text)
                    return reply.json()
                def poll_resume(self,*args):
                    for task in api.get('/api/reviews').json()['tasks']:
                        claimed=api.post('/api/reviews/'+task['id']+'/claim',headers=origin,json={'version':task['version']}).json()
                        value='length' if '衣身丈' in task['field_label'] else 'other'
                        reply=api.post('/api/reviews/'+task['id']+'/confirm',headers=origin,json={'version':claimed['version'],'final_value_id':value})
                        test.assertEqual(reply.status_code,200,reply.text)
                    return api.get('/api/device/resume?device_id=device&wait_seconds=0',headers=headers).json()
                def acknowledge_resume(self,event_id,**kwargs):
                    reply=api.post('/api/device/resume/'+event_id+'/ack',headers=headers,json={**kwargs,'checkpoint_persisted':True})
                    test.assertEqual(reply.status_code,200,reply.text)
                    return reply.json()
            cloud=Cloud()
            cloud.post_event('product','product.upsert',{'product_version':'product','style_code':'2024','title':'羊毛上衣','category_json':{'hints':['毛衣']}})
            store=LearningStore(root/'learning.sqlite3');store.migrate()
            runtime=AttributeRuntime(store,cloud,'run','product',device_id='device')
            context=erp.LearningRunContext(store,'run','product','device','images',cloud,runtime)
            args=SimpleNamespace(save=True,save_only=True,platform='all')
            checkpoint=store.save_checkpoint(RunCheckpoint('run','product','save_only',('base','xhs','tmall'),2,'running',None,1,'device','images'))
            cloud.post_event('checkpoint','checkpoint.updated',checkpoint_event_payload(checkpoint))
            import base64
            chart = root/'size.png'
            chart.write_bytes(base64.b64decode(
                'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/Z5kAAAAASUVORK5CYII='))
            error=OCRJudgmentRequired(chart,unfamiliar_tokens(),'clothing',
                                     lambda tokens: recognition.parse_size_names(tokens,'clothing'),'衣长未识别')
            session=OCRSemanticSession()
            await erp.recover_ocr_with_learning(context,args,session,error,logging.getLogger(__name__))
            final=store.load_checkpoint('run')
            self.assertEqual((final.platform_order,final.current_index,final.status),(('base','xhs','tmall'),2,'running'))
            self.assertEqual(error.validate(session.corrections[error.key]),('M','L','XL','2XL'))
            store.close()

    async def test_input_failure_uses_base_review_and_continues_to_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LearningStore(root / 'learning.sqlite3')
            store.migrate()
            client = Mock()
            client.post_event.return_value = {'ok': True}
            client.review_learning.return_value = {'status': 'review_required'}
            client.ocr_judge.return_value = {'status': 'suggested', 'source': 'deepseek',
                'suggestions': [{'token_id': 0, 'meaning': 'length', 'confidence': .99}]}
            runtime = AttributeRuntime(store, client, 'run', 'product')
            context = erp.LearningRunContext(store, 'run', 'product', 'device', 'images', client, runtime)
            session = OCRSemanticSession()
            chart = root / 'size.jpg'
            chart.write_bytes(b'chart')
            args = SimpleNamespace(save=True, save_only=True, platform='xhs')
            with session.activate(), patch.object(recognition, 'vision_ocr', return_value=unfamiliar_tokens()):
                from ocr_semantics import OCRJudgmentRequired
                with self.assertRaises(OCRJudgmentRequired) as caught:
                    recognition.recognize_size_names(chart, 'clothing')
                with patch.object(erp, 'read_product_data', side_effect=lambda *a, **kw: recognition.recognize_size_names(chart, 'clothing')):
                    product = await erp.read_product_with_ocr_review(root / 'product.xlsx', args, context, session,
                                                                   caught.exception, logging.getLogger(__name__), include_douyin=False)
                self.assertEqual(product, ('M', 'L', 'XL', '2XL'))
                erp.write_model_judgment_report(root, context, session)
                report = json.loads((root / 'model-judgments.json').read_text())
                self.assertEqual(report['decisions'][0]['selected_labels'], ['衣长'])
                self.assertIn('衣身丈', (root / 'model-judgments.html').read_text())
                store.close()

    async def test_uncertain_ocr_preserves_execution_plan_and_current_platform(self):
        from ocr_semantics import OCRJudgmentRequired
        from attribute_runtime import AttributeRequest, ReviewRequired, ReviewBatchRequired
        from learning_models import CandidateValue
        error = OCRJudgmentRequired(Path('size.jpg'), unfamiliar_tokens(), 'clothing', lambda _: None, 'unknown')
        request = AttributeRequest('tmall', 'ocr:clothing', 'ocr:length', '尺码 OCR',
                                   (CandidateValue('length', '衣长'),), '', {}, False, 's', 'ocr_semantic')
        review = ReviewRequired('r', request, 'uncertain', 's')
        from learning_models import RunCheckpoint
        checkpoint = RunCheckpoint('r','p','save_only',('base','xhs','tmall'),2,'running',None,1,'d','images')
        context = SimpleNamespace(client=Mock(), attribute_runtime=Mock(), store=Mock(), run_id='r', product_version='p', device_id='d')
        context.store.load_checkpoint.return_value = checkpoint
        context.attribute_runtime.drain = AsyncMock()
        context.attribute_runtime.has_deferred_reviews = False
        args = SimpleNamespace(save=True, save_only=True, platform='xhs')
        with patch('ocr_review.resolve_ocr_failure', new=AsyncMock(side_effect=[ReviewBatchRequired((review,)), None])), patch.object(erp, 'wait_for_platform_review_batch', new=AsyncMock()) as wait:
            await erp.recover_ocr_with_learning(context, args, OCRSemanticSession(), error, logging.getLogger(__name__))
        self.assertEqual(wait.call_args.args[2:4], (('base','xhs','tmall'), 2))


if __name__ == '__main__':
    unittest.main()
