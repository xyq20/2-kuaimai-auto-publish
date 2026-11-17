"""Dewu live-option resolution through the shared model and review runtime."""
import json
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from attribute_runtime import AttributeRuntime, NO_FILL_VALUE_ID, ReviewBatchRequired
from dewu_data import DewuError
from dewu_listing import DewuListing, run_dewu_with_learning
from learning_store import LearningStore
from tests.test_dewu_listing import MULTISELECT_HTML, RUN_HTML, DRAFT_URL


SEASON_HTML = MULTISELECT_HTML.replace(
    "const choices = ['旧值一', '旧值二', '棉', '羊毛'];",
    "const choices = ['旧值一', '旧值二', '春季', '夏季', '秋季', '冬季'];")


class JudgmentClient:
    def __init__(self, confidence=.99, invalid=False):
        self.events, self.judgments, self.learning_calls = [], [], []
        self.confidence, self.invalid = confidence, invalid

    def post_event(self, key, event_type, payload):
        self.events.append((key, event_type, payload))
        return {'event_id': key}

    def review_learning(self, payload):
        self.learning_calls.append(payload)
        return {'status': 'review_required', 'reason_code': 'no_learning'}

    def semantic_judge(self, payload):
        self.judgments.append(payload)
        ids = [o['value_id'] for o in payload['options'] if o['label'] in {'春季', '秋季', '冬季', '春', '秋', '冬'}]
        return {'source': 'deepseek', 'status': 'suggested', 'api_called': True,
                'snapshot_version': payload['snapshot_version'], 'value_ids': ['missing'] if self.invalid else ids,
                'confidence': self.confidence, 'evidence_kinds': ['excel'], 'reason': '原文春秋冬对应三个季节',
                'assessment': {'relation': 'equivalent', 'source_text': payload['excel_value'],
                               'conflicts': [], 'assumptions': [], 'competing_value_ids': []}}


class DewuAttributeLearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LearningStore(self.root / 'learning.sqlite3')
        self.store.migrate()
        self.client = JudgmentClient()
        self.runtime = AttributeRuntime(self.store, self.client, 'run', 'product')
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='chrome', headless=True)
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(1500)
        self.product = SimpleNamespace(category=('服装', '长裤', '工装裤'), brand='NEIGBORL', style_code='NGBL-7076')
        self.listing = DewuListing(self.page, self.product, self.root, timeout=1,
                                   attribute_runtime=self.runtime)
        await self.page.set_content(SEASON_HTML)

    async def asyncTearDown(self):
        await self.runtime.drain()
        await self.browser.close()
        await self.pw.stop()
        self.store.close()
        self.temp.cleanup()

    async def test_comma_separated_multiselect_is_exact_and_does_not_call_model(self):
        result = await self.listing.select_attribute(self.page.locator('#field'), '春季，秋季，冬季', '适用季节')
        self.assertEqual(result, '春季,秋季,冬季')
        self.assertEqual(await self.page.evaluate('selected'), ['春季', '秋季', '冬季'])
        self.assertEqual(self.client.judgments, [])
        self.assertEqual(self.client.learning_calls, [])

    async def test_collar_candidates_follow_vue_owner_while_previous_material_popup_is_visible(self):
        await self.page.goto('about:blank')
        await self.page.set_content('''
          <div class="el-select" id="material"><input class="el-input__inner"></div>
          <div class="el-form-item"><label class="el-form-item__label">领型</label>
            <div class="el-select" id="collar"><input class="el-input__inner" readonly></div>
          </div>
          <div class="el-select-dropdown" id="material-popup"><div class="el-select-dropdown__item selected">棉</div><div class="el-select-dropdown__item">麻</div></div>
          <div class="el-select-dropdown" id="collar-popup" style="display:none"><div class="el-select-dropdown__item">立领</div><div class="el-select-dropdown__item">翻领</div></div>
          <script>
            window.materialClicks=0;
            const old=document.getElementById('material-popup'),popup=document.getElementById('collar-popup');
            old.onclick=()=>materialClicks++;
            const root=document.getElementById('collar'),input=root.querySelector('input');
            root.__vue__={visible:false,popperElm:popup};
            input.onclick=()=>{root.__vue__.visible=true;setTimeout(()=>popup.style.display='block',100)};
            input.onkeydown=e=>{if(e.key==='Escape'){root.__vue__.visible=false;setTimeout(()=>popup.style.display='none',80)}};
            popup.querySelectorAll('.el-select-dropdown__item').forEach(o=>o.onclick=()=>{input.value=o.textContent});
          </script>''')
        await self.listing.select_attribute(self.listing.item('领型'), '立领', '领型')
        self.assertEqual(await self.page.locator('#collar input').input_value(), '立领')
        self.assertEqual(self.listing.report['attribute_choices']['领型']['options'], ['立领', '翻领'])
        self.assertEqual(await self.page.evaluate('materialClicks'), 0)
        self.assertEqual(self.client.judgments, [])

    async def test_multiselect_write_and_saved_readback_ignore_another_selected_popup(self):
        await self.page.goto('about:blank')
        html=SEASON_HTML.replace('<div id="field">', '<div class="el-form-item"><label class="el-form-item__label">适用季节</label><div id="field">')+'</div>'
        await self.page.set_content(RUN_HTML+html)
        await self.page.evaluate('''() => {
          const popup=document.querySelector('.el-select-dropdown');popup.id='season-popup';
          document.querySelector('.el-input__inner').setAttribute('aria-controls','season-popup');
          const old=document.createElement('div');old.className='el-select-dropdown';old.id='old-material';
          old.innerHTML='<div class="el-select-dropdown__item selected">棉</div>';document.body.append(old);
        }''')
        await self.listing.select_attribute(self.page.locator('#field'), '春季,秋季,冬季', '适用季节')
        self.assertEqual(await self.page.evaluate('selected'), ['春季','秋季','冬季'])
        self.listing.report['draft_url']=DRAFT_URL
        with patch.object(self.page, 'goto', new=AsyncMock()):
            await self.listing.verify_saved_attributes()
        self.assertEqual(self.listing.report['steps']['attribute_readback']['适用季节'], ['春季','秋季','冬季'])
        self.assertTrue(await self.page.locator('#old-material .selected').is_visible())

    async def test_ambiguous_popup_without_control_owner_never_enters_learning(self):
        await self.page.goto('about:blank')
        await self.page.set_content('''<div class="el-select"><input class="el-input__inner"></div>
          <div id="collar"><div class="el-select"><input class="el-input__inner"></div></div>
          <div class="el-select-dropdown"><div class="el-select-dropdown__item">棉</div></div>''')
        with self.assertRaisesRegex(DewuError, '所属的下拉框'):
            await self.listing.select_attribute(self.page.locator('#collar'), '立领', '领型')
        self.assertEqual(self.client.learning_calls, [])
        self.assertEqual(self.client.judgments, [])

    async def test_multiselect_owner_survives_tooltip_overwriting_vue_reference(self):
        await self.page.goto('about:blank')
        await self.page.set_content(SEASON_HTML)
        await self.page.evaluate('''() => {
          const root=document.querySelector('.el-select'),popup=document.querySelector('.el-select-dropdown');
          const vm={$el:root,$options:{name:'ElSelect'},visible:false,popperElm:popup,$refs:{popper:{$el:popup}}};
          const tooltip={$el:root,$options:{name:'ElTooltip'},$children:[vm],popperElm:document.createElement('div')};
          tooltip.popperElm.className='el-tooltip__popper';document.body.append(tooltip.popperElm);
          vm.$parent=tooltip;root.__vue__=vm;
          root.querySelector('.el-input').__vue__={$options:{name:'ElInput'},$parent:vm};
          root.querySelector('.el-input__inner').onclick=()=>{
            vm.visible=!vm.visible;popup.style.display=vm.visible?'block':'none';root.__vue__=tooltip;
          };
          root.addEventListener('keydown',e=>{if(e.key==='Escape')vm.visible=false});
          const old=document.createElement('div');old.className='el-select-dropdown';
          old.innerHTML='<div class="el-select-dropdown__item selected">棉</div>';document.body.append(old);
        }''')
        for _ in range(2):
            await self.listing.select_attribute(self.page.locator('#field'), '春季,秋季,冬季', '适用季节')
            self.assertEqual(await self.page.evaluate('selected'), ['春季', '秋季', '冬季'])
        self.assertEqual(self.client.judgments, [])
        self.assertEqual(self.listing.report['attribute_choices']['适用季节']['options'],
                         ['旧值一', '旧值二', '春季', '夏季', '秋季', '冬季'])

    async def test_unseparated_seasons_use_learning_then_model_and_apply_all_choices(self):
        result = await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
        self.assertEqual(result, '春季,秋季,冬季')
        self.assertEqual(await self.page.evaluate('selected'), ['春季', '秋季', '冬季'])
        self.assertEqual(len(self.client.learning_calls), 1)
        self.assertEqual(len(self.client.judgments), 1)
        payload = self.client.judgments[0]
        self.assertEqual(payload['platform_id'], 'dewu')
        self.assertEqual(payload['excel_value'], '春秋冬')
        self.assertEqual(payload['control_type'], 'multi_select')
        self.assertIn('夏季', [o['label'] for o in payload['options']])
        await self.runtime.drain()
        self.assertFalse(any(kind == 'review.created' for _, kind, _ in self.client.events))
        self.assertEqual(self.listing.report['attribute_choices']['适用季节']['source'], 'deepseek')

    async def test_real_season_labels_and_tag_overlay_use_arrow_and_select_three_values(self):
        await self.page.goto('about:blank')
        html = SEASON_HTML.replace('春季', '春').replace('夏季', '夏').replace('秋季', '秋').replace('冬季', '冬')
        await self.page.set_content(html)
        await self.page.evaluate('''() => {
            const el=document.querySelector('.el-select');
            const input=el.querySelector('.el-input');input.style.cssText='position:relative;width:260px;height:40px';
            input.querySelector('input').style.cssText='width:260px;height:40px';
            const tags=el.querySelector('.el-select__tags');
            tags.style.cssText='position:absolute;width:220px;height:40px;z-index:3;top:0;left:0';
            input.append(tags);
            const arrow=document.createElement('span');arrow.className='el-input__suffix';arrow.textContent='▼';
            arrow.style.cssText='position:absolute;right:0;top:0;width:25px;height:40px;z-index:4';
            arrow.onclick=()=>document.querySelector('.el-select-dropdown').style.display='block';input.append(arrow);
        }''')
        await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
        self.assertEqual(await self.page.evaluate('selected'), ['春', '秋', '冬'])

    async def test_only_save_and_reopen_readback_records_model_learning(self):
        saved = RUN_HTML + SEASON_HTML.replace("window.selected = ['旧值一', '旧值二'];",
                                              "window.selected = ['春季', '秋季', '冬季'];")
        saved = saved.replace('<div id="field">', '<div class="el-form-item"><label class="el-form-item__label">适用季节</label><div id="field">') + '</div>'
        async def route(route):
            await route.fulfill(content_type='text/html; charset=utf-8', body=saved)
        await self.page.route('https://stark.dewu.com/**', route)
        await self.page.goto(DRAFT_URL)
        await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
        self.listing.report['draft_url'] = DRAFT_URL
        await self.runtime.drain()
        self.assertFalse(any(kind == 'text_facts.created' and data.get('source') == 'model_verified'
                             for _, kind, data in self.client.events))
        await self.listing.verify_saved_attributes()
        await self.runtime.drain()
        verified = [data for _, kind, data in self.client.events if kind == 'text_facts.created'
                    and data.get('source') == 'model_verified']
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]['payload_json']['model_verified']['selected_labels'], ['春季', '秋季', '冬季'])

    async def test_uncertain_model_queues_review_and_human_confirmation_resumes_multi_selection(self):
        self.client.confidence = .5
        self.runtime.begin_review_collection('dewu')
        self.assertIsNone(await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节'))
        with self.assertRaises(ReviewBatchRequired) as caught:
            self.runtime.raise_deferred_reviews()
        review = caught.exception.reviews[0]
        await self.runtime.drain()
        self.assertEqual(len(self.client.judgments), 1)
        events = [p for _, kind, p in self.client.events if kind == 'review.created']
        self.assertEqual(events[0]['platform_id'], 'dewu')
        self.assertTrue(events[0]['evidence_json']['model_fallback']['api_called'])
        ids = [c.value_id for c in review.request.candidates if c.label in {'春季', '秋季', '冬季'}]
        self.store.save_review_resolution(product_version='product', platform_id='dewu',
            snapshot_version=review.snapshot_version, review_id=review.review_id, final_value_id=','.join(ids))
        self.runtime.begin_review_collection('dewu')
        await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
        self.assertEqual(await self.page.evaluate('selected'), ['春季', '秋季', '冬季'])
        self.assertFalse(self.runtime.has_deferred_reviews)
        self.assertEqual(len(self.client.judgments), 1)
        self.assertEqual(self.listing.report['attribute_choices']['适用季节']['source'], 'human_override')

    async def test_real_review_api_confirms_resumes_and_reuses_dewu_multiselect_learning(self):
        from fastapi.testclient import TestClient
        from kuaimai_erp import wait_for_platform_review_batch
        from local_review.app import create_app
        from local_review.config import Settings
        from local_review.database import migrate, transaction
        from local_review.security import create_user

        settings = Settings(self.root / 'review-server', 'fixture-device', deepseek_api_key='fixture-model')
        migrate(settings)
        with transaction(settings) as connection:
            create_user(connection, 'operator', '1234', role='operator')
        owner = self
        with TestClient(create_app(settings)) as api:
            class ApiBridge:
                def post(self, path, payload):
                    response = api.post(path, headers={'Authorization': 'Device fixture-device'}, json=payload)
                    owner.assertIn(response.status_code, (200, 201, 409), response.text)
                    return response.json()

                def post_event(self, key, kind, payload):
                    return self.post('/api/device/events', {'idempotency_key': key, 'event_type': kind, 'payload': payload})

                def review_learning(self, payload):
                    return self.post('/api/device/review-learning', payload)

                def semantic_judge(self, payload):
                    return self.post('/api/device/semantic-judge', payload)

                def poll_resume(self, device_id, wait_seconds=0):
                    response = api.get('/api/device/resume', params={'device_id': device_id, 'wait_seconds': 0},
                                       headers={'Authorization': 'Device fixture-device'})
                    owner.assertEqual(response.status_code, 200, response.text)
                    return response.json()

                def acknowledge_resume(self, event_id, *, checkpoint_id, device_id):
                    return self.post(f'/api/device/resume/{event_id}/ack', {
                        'checkpoint_persisted': True, 'checkpoint_id': checkpoint_id, 'device_id': device_id})

            bridge = ApiBridge()
            bridge.post_event('product', 'product.upsert', {'product_version': 'product',
                'style_code': 'D-1', 'title': '得物工装裤', 'category_json': {'hints': ['服装 > 工装裤']}})
            self.runtime.client = bridge
            def uncertain(_settings, _system, data):
                return {'value_ids': [o['value_id'] for o in data['request']['options']
                                     if o['label'] in {'春季', '秋季', '冬季'}],
                        'confidence': .5, 'reason': '原文可能包含三个季节，需要人工确认', 'evidence_kinds': ['excel'],
                        'assessment': {'relation': 'equivalent', 'source_text': '春秋冬',
                                       'conflicts': [], 'assumptions': [], 'competing_value_ids': []}}
            try:
                with patch('local_review.semantic.request_json', side_effect=uncertain) as model:
                    self.runtime.begin_review_collection('dewu')
                    await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
                    await self.runtime.drain()
                    self.assertTrue(self.runtime.has_deferred_reviews)
                    self.assertTrue(model.called)
                    login = api.post('/api/login', headers={'Origin': 'http://testserver'},
                                     json={'username': 'operator', 'password': '1234'})
                    self.assertEqual(login.status_code, 200, login.text)
                    task, = api.get('/api/reviews').json()['tasks']
                    self.assertEqual(task['platform_id'], 'dewu')
                    self.assertEqual(task['control_type'], 'multi_select')
                    self.assertFalse(task['custom_allowed'])
                    final = ','.join(o['value_id'] for o in task['options'] if o['label'] in {'春季', '秋季', '冬季'})
                    headers = {'Origin': 'http://testserver'}
                    claimed = api.post(f"/api/reviews/{task['id']}/claim", headers=headers,
                                       json={'version': task['version']})
                    self.assertEqual(claimed.status_code, 200, claimed.text)
                    confirmed = api.post(f"/api/reviews/{task['id']}/confirm", headers=headers,
                                         json={'version': claimed.json()['version'], 'final_value_id': final})
                    self.assertEqual(confirmed.status_code, 200, confirmed.text)
                    context = SimpleNamespace(store=self.store, client=bridge, attribute_runtime=self.runtime,
                        run_id='run', product_version='product', device_id=self.runtime.device_id, image_version='images')
                    await wait_for_platform_review_batch(context, SimpleNamespace(save=True, platform='dewu'),
                        ('dewu',), 0, self.runtime.deferred_reviews, Mock())
                    self.runtime.begin_review_collection('dewu')
                    await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
                    self.assertEqual(await self.page.evaluate('selected'), ['春季', '秋季', '冬季'])
                    self.assertEqual(self.listing.report['attribute_choices']['适用季节']['source'], 'human_override')
                    calls = model.call_count
                    # A new local store has no resolution cache: reuse must come from the server's human library.
                    with closing(LearningStore(self.root / 'fresh-learning.sqlite3')) as fresh_store:
                        fresh_store.migrate()
                        fresh = AttributeRuntime(fresh_store, bridge, 'new-run', 'product')
                        self.listing.attribute_runtime = fresh
                        try:
                            await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
                            self.assertEqual(self.listing.report['attribute_choices']['适用季节']['source'], 'review_learning')
                            self.assertEqual(model.call_count, calls)
                        finally:
                            await fresh.drain()
            finally:
                await self.runtime.drain()
                self.runtime.client = self.client
                self.listing.attribute_runtime = self.runtime

    async def test_human_no_fill_clears_existing_collapsed_values(self):
        self.client.confidence = .5
        self.runtime.begin_review_collection('dewu')
        await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
        review = self.runtime.deferred_reviews[0]
        self.store.save_review_resolution(product_version='product', platform_id='dewu',
            snapshot_version=review.snapshot_version, review_id=review.review_id, final_value_id=NO_FILL_VALUE_ID)
        self.runtime.begin_review_collection('dewu')
        self.assertIsNone(await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节'))
        self.assertEqual(await self.page.evaluate('selected'), [])

    async def test_invalid_model_candidate_never_gets_clicked(self):
        self.client.invalid = True
        self.runtime.begin_review_collection('dewu')
        await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
        self.assertEqual(await self.page.evaluate('selected'), ['旧值一', '旧值二'])
        self.assertTrue(self.runtime.has_deferred_reviews)

    async def test_without_learning_missing_candidate_fails_promptly_with_actionable_error(self):
        self.listing.attribute_runtime = None
        with self.assertRaisesRegex(DewuError, '开启属性学习与审核'):
            await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')

    async def test_page_readback_failure_retries_once_then_queues_review(self):
        self.runtime.begin_review_collection('dewu')
        html = SEASON_HTML.replace("value === '棉'", "value === '春季'")
        await self.page.goto('about:blank')
        await self.page.set_content(html)
        await self.page.evaluate('injectExtra=true')
        await self.listing.select_attribute(self.page.locator('#field'), '春秋冬', '适用季节')
        self.assertTrue(self.runtime.has_deferred_reviews)
        self.assertTrue(self.runtime.deferred_reviews[0].request.evidence['write_retry_exhausted'])

    async def test_review_wait_resumes_same_page_and_saves_only_after_confirmation(self):
        await self.page.set_content(RUN_HTML)
        for name in ('colors', 'verify_carousel', 'process_white_backgrounds', 'size_chart',
                     'recommendations', 'sku_batch', 'detail_images'):
            setattr(self.listing, name, AsyncMock())
        # Two unresolved attributes in one pass, followed by a confirmed retry.
        self.client.confidence = .5
        confirmed = False
        async def basic():
            if not confirmed:
                for label in ('适用季节', '风格'):
                    await self.runtime.resolve_unmatched_page_options(platform_id='dewu',category_leaf_id='pants',
                        field_label=label, excel_value='春秋冬', multi=True,
                        options=({'name':'春季'}, {'name':'夏季'}, {'name':'秋季'}, {'name':'冬季'}))
        async def wait(error):
            nonlocal confirmed
            self.assertEqual(await self.page.evaluate('[saveClicks,reviewClicks]'), [0,0])
            self.assertEqual(self.listing.report['status'], 'waiting_review')
            self.assertEqual(len(error.reviews), 2)
            confirmed = True
        self.listing.basic = AsyncMock(side_effect=basic)
        self.listing.on_review = AsyncMock(side_effect=wait)
        with patch.object(self.page, 'goto', new=AsyncMock()) as navigate:
            await self.listing.run(resume_url=DRAFT_URL)
        navigate.assert_awaited_once()
        self.assertEqual(self.listing.basic.await_count, 2)
        self.listing.on_review.assert_awaited_once()
        self.assertEqual(await self.page.evaluate('[saveClicks,reviewClicks]'), [1,0])
        self.assertEqual(self.listing.report['status'], 'draft_saved')


class DewuLearningLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_lifecycle_registers_product_and_checkpoints_dewu_only(self):
        with tempfile.TemporaryDirectory() as temp:
            product = SimpleNamespace(style_code='D-1', selling_point='工装裤', initial_images=(),
                detail_images=(), fields={'适用季节':'春秋冬'}, category=('服装','工装裤'))
            runtime = SimpleNamespace(model_decisions=[], model_events=[])
            context = SimpleNamespace(attribute_runtime=runtime, store=Mock(), run_id='run', product_version='p')
            with patch('kuaimai_erp.create_learning_context', return_value=context) as create, \
                 patch('kuaimai_erp.initialize_learning_run', new=AsyncMock()), \
                 patch('kuaimai_erp.save_learning_checkpoint') as checkpoint, \
                 patch('kuaimai_erp.drain_learning_events', new=AsyncMock()), \
                 patch('kuaimai_erp.write_model_judgment_report') as report, \
                 patch('dewu_listing.run_dewu_browser', new=AsyncMock()) as run:
                await run_dewu_with_learning(SimpleNamespace(), product, Path(temp), Mock(), Path(temp)/'state.json')
            adapter = create.call_args.args[1]
            self.assertEqual(adapter.descriptive_fields, {'适用季节':'春秋冬'})
            self.assertEqual(adapter.category_hints, ('服装 > 工装裤',))
            self.assertIs(run.await_args.kwargs['learning_context'], context)
            self.assertEqual([c.args[2:] for c in checkpoint.call_args_list], [(('dewu',),0,'running'),(('dewu',),1,'completed')])
            self.assertEqual(report.call_args.kwargs['run_status'], 'completed')
            context.store.close.assert_called_once()
