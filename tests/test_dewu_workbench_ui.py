"""Workbench UI tests use a fresh local Chrome and mock all network requests."""
import json
from pathlib import Path
import re
import unittest

from playwright.async_api import async_playwright


STATIC = Path(__file__).resolve().parents[1] / 'local_review/static'
HTML = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '', (STATIC / 'index.html').read_text())
SCRIPT = (STATIC / 'app.js').read_text()

MOCK = r'''config => {
  window.calls = [];
  window.createdJobs = [];
  window.releases = {};
  Object.defineProperty(window, 'localStorage', {value:{
    getItem:()=>JSON.stringify(config.saved || {}),
    setItem:(_key,value)=>window.savedState=JSON.parse(value),
  }});
  if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', {value:()=> '11111111-2222-4333-8444-555555555555'});
  const plain = 'a'.repeat(32), dewu = 'd'.repeat(32);
  const standard = {id:plain,title:'普通测试商品',folder:'普通测试商品',style_code:'PLAIN-1',category:'上衣',base_saved:false};
  const independent = {id:dewu,title:'得物双头鹰',folder:'双头鹰',style_code:'DEWU-1',category:'服装>长裤>工装裤',base_saved:false,image:'/api/launcher/products/'+dewu+'/image?source=dewu'};
  window.fetch = async (path,options={}) => {
    calls.push({path,method:options.method || 'GET',body:options.body ? JSON.parse(options.body) : null});
    const reply = data=>new Response(JSON.stringify(data),{status:200,headers:{'content-type':'application/json'}});
    const parsed = new URL(path,'http://fixture.test'), dewuSource = parsed.searchParams.get('source') === 'dewu';
    if (path === '/api/launcher/config') return reply({user:{username:'admin',role:'admin'},learning_available:true});
    if (path === '/api/reviews') return reply({tasks:[]});
    if (parsed.pathname === '/api/launcher/products') {
      if (!dewuSource && config.delayList) await new Promise(resolve=>releases.list=resolve);
      return reply({products:[dewuSource ? independent : standard]});
    }
    if (parsed.pathname.endsWith('/open-folder')) return reply({opened:true});
    if (parsed.pathname.startsWith('/api/launcher/products/')) {
      if (!dewuSource && config.delayDetail) await new Promise(resolve=>releases.detail=resolve);
      return reply(dewuSource ? independent : standard);
    }
    if (parsed.pathname === '/api/launcher/jobs' && options.method === 'POST') {
      const data = JSON.parse(options.body); createdJobs.push(data);
      return reply({id:'9'.repeat(32),...data,style_code:'DEWU-1',title:'得物双头鹰',status:'completed',created_at:'2026-11-09T12:00:00+08:00',created_by:'admin',stages:[{platform:'dewu',status:'success'}],pending_reviews:0,draft_url:'https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/123456'});
    }
    if (parsed.pathname === '/api/launcher/jobs') return reply({jobs:[]});
    throw Error('unexpected '+path);
  };
}'''


class DewuWorkbenchUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='chrome', headless=True)
        self.page = await self.browser.new_page(viewport={'width':1440,'height':960})
        await self.page.set_content(HTML)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def boot(self, **config):
        await self.page.evaluate(MOCK, config)
        await self.page.add_script_tag(content=SCRIPT)
        await self.page.wait_for_function("document.querySelector('#account').textContent.includes('admin')")

    async def dewu(self):
        await self.page.get_by_role('button', name='得物', exact=True).click()
        await self.page.wait_for_function("document.querySelector('#product-code').textContent === 'DEWU-1' && !document.querySelector('#preview-run').disabled")

    async def test_dewu_is_exclusive_uses_own_catalog_and_submits_only_draft(self):
        await self.boot()
        await self.dewu()
        self.assertEqual(await self.page.locator('[data-platform][aria-pressed=true]').get_attribute('data-platform'), 'dewu')
        self.assertEqual(await self.page.locator('#plan-list li').count(), 1)
        self.assertEqual(await self.page.locator('#plan-list .queue-name').inner_text(), '得物')
        self.assertTrue(await self.page.locator('[data-mode=publish]').is_visible())
        self.assertEqual(await self.page.locator('[data-mode=publish] .mode-top > span').first.inner_text(), '提交审核')
        self.assertTrue(await self.page.locator('[data-mode=preview]').is_hidden())
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '保存草稿')
        self.assertTrue(await self.page.locator('#learning-toggle').is_enabled())
        self.assertTrue(await self.page.locator('#learning-toggle').is_checked())
        await self.page.locator('#preview-run').click()
        self.assertIn('不提交审核', await self.page.locator('#run-subtitle').inner_text())
        await self.page.get_by_role('button', name='确认保存草稿').click()
        await self.page.wait_for_function('createdJobs.length === 1')
        sent = await self.page.evaluate('createdJobs[0]')
        self.assertEqual(sent['operation'], 'dewu')
        self.assertEqual(sent['platforms'], ['dewu'])
        self.assertEqual(sent['product_id'], 'd' * 32)
        self.assertEqual(sent['mode'], 'save')
        self.assertTrue(sent['learning'])
        self.assertFalse(sent['confirm_publish'])
        await self.page.locator('#dewu-draft-link').wait_for(state='visible')
        self.assertTrue((await self.page.locator('#dewu-draft-link').get_attribute('href')).startswith('https://stark.dewu.com/'))

    async def test_submit_review_is_explicit_and_confirmed_before_job_start(self):
        await self.boot()
        await self.dewu()
        await self.page.locator('[data-mode=publish]').click()
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '提交审核')
        await self.page.locator('#preview-run').click()
        self.assertIn('真实提交得物审核', await self.page.locator('#run-subtitle').inner_text())
        self.assertEqual(await self.page.evaluate('createdJobs.length'), 0)
        await self.page.get_by_role('button', name='确认提交审核', exact=True).click()
        await self.page.wait_for_function('createdJobs.length === 1')
        sent = await self.page.evaluate('createdJobs[0]')
        self.assertEqual(sent['platforms'], ['dewu'])
        self.assertEqual(sent['mode'], 'publish')
        self.assertTrue(sent['confirm_publish'])
        self.assertTrue(sent['learning'])
        self.assertIn('提交审核', await self.page.locator('#run-summary').inner_text())

    async def test_dewu_learning_preference_can_be_disabled_like_other_platforms(self):
        await self.boot()
        await self.dewu()
        await self.page.locator('#learning-toggle').uncheck()
        await self.page.locator('#preview-run').click()
        await self.page.get_by_role('button', name='确认保存草稿', exact=True).click()
        await self.page.wait_for_function('createdJobs.length === 1')
        self.assertFalse((await self.page.evaluate('createdJobs[0]'))['learning'])

    async def test_dewu_model_board_shows_multi_selection_and_pending_reviews_in_review_queue(self):
        await self.boot()
        await self.page.evaluate('''() => {
          window.dewuJob={id:'9'.repeat(32),operation:'dewu',style_code:'D',mode:'save',status:'running',stages:[],
            model_summary:{api_call_count:1,adopted_count:1},
            model_decisions:[{key:'season',platform_id:'dewu',field_id:'season',field_label:'适用季节',snapshot_version:'s1',
              excel_value:'春秋冬',selected_labels:['春','秋','冬'],confidence:.99,status:'adopted',reason:'对应三个季节'}]};
          renderJob(window.dewuJob);
          document.getElementById('run-dialog').showModal();
        }''')
        board = self.page.locator('#model-judgment-details')
        self.assertTrue(await board.is_visible())
        self.assertEqual(await board.locator('.model-judgment-item').count(), 1)
        self.assertIn('春、秋、冬', await board.inner_text())
        await self.page.evaluate('''() => {
          dewuJob.pending_reviews=1;
          dewuJob.review_tasks=[{id:'review',platform_id:'dewu',field_id:'season',snapshot_version:'s1',status:'pending'}];
          dewuJob.model_decisions[0].status='review_required';
          renderJob(dewuJob);
        }''')
        self.assertEqual(await board.locator('.model-judgment-item').count(), 0)
        self.assertTrue(await self.page.locator('#run-review').is_visible())
        await self.page.locator('#run-review').click()
        self.assertEqual(await self.page.locator('#review-frame').get_attribute('src'),
                         '/review?embedded=1&run_id='+'9'*32)

    async def test_keyboard_can_select_review_and_switching_platform_resets_to_draft(self):
        await self.boot()
        await self.dewu()
        await self.page.locator('[data-mode=save]').focus()
        await self.page.keyboard.press('ArrowRight')
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '提交审核')
        await self.page.get_by_role('button', name='京东', exact=True).click()
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '只保存')
        await self.dewu()
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '保存草稿')

    async def test_switching_back_and_select_all_restore_standard_catalog(self):
        await self.boot()
        await self.dewu()
        await self.page.get_by_role('button', name='京东', exact=True).click()
        await self.page.wait_for_function("document.querySelector('#product-code').textContent === 'PLAIN-1' && !document.querySelector('#preview-run').disabled")
        self.assertEqual(await self.page.locator('[data-platform][aria-pressed=true]').get_attribute('data-platform'), 'jd')
        self.assertEqual(await self.page.locator('#product-source-note').inner_text(), '资料表与图片均来自原商品目录。')
        self.assertTrue(await self.page.locator('[data-mode=publish]').is_visible())
        await self.dewu()
        await self.page.get_by_role('button', name='全选', exact=True).click()
        await self.page.wait_for_function("document.querySelector('#product-code').textContent === 'PLAIN-1'")
        self.assertEqual(await self.page.locator('[data-platform][aria-pressed=true]').count(), 8)
        self.assertEqual(await self.page.locator('[data-platform=dewu]').get_attribute('aria-pressed'), 'false')

    async def test_old_catalog_response_cannot_replace_dewu_selection(self):
        await self.boot(delayList=True)
        await self.page.wait_for_function('!!releases.list')
        await self.dewu()
        await self.page.evaluate('releases.list()')
        await self.page.wait_for_timeout(50)
        self.assertEqual(await self.page.locator('#product-code').inner_text(), 'DEWU-1')
        self.assertEqual(await self.page.locator('#product-list [data-product]').get_attribute('data-product'), 'd'*32)

    async def test_old_detail_response_cannot_replace_dewu_selection(self):
        await self.boot(delayDetail=True)
        await self.page.wait_for_function('!!releases.detail')
        await self.dewu()
        await self.page.evaluate('releases.detail()')
        await self.page.wait_for_timeout(50)
        self.assertEqual(await self.page.locator('#product-code').inner_text(), 'DEWU-1')

    async def test_boot_restores_dewu_source_and_open_folder_uses_same_source(self):
        await self.boot(saved={'operation':'dewu','selected':['dewu'],'productId':'d'*32,'mode':'publish'})
        await self.page.wait_for_function("document.querySelector('#product-code').textContent === 'DEWU-1' && !document.querySelector('#preview-run').disabled")
        calls = await self.page.evaluate('calls')
        self.assertFalse(any(c['path'] == '/api/launcher/products' for c in calls))
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '保存草稿')
        await self.page.get_by_role('button', name='打开当前商品文件夹').click()
        await self.page.wait_for_function("calls.some(c=>c.path.endsWith('/open-folder?source=dewu'))")

    async def test_untrusted_draft_url_never_becomes_clickable(self):
        await self.boot()
        for url in ('javascript:alert(1)','https://evil.test/draft','https://stark.dewu.com.evil.test/vueProduct/newProductApply/spuEdit/operation/123'):
            await self.page.evaluate('''url=>renderJob({id:'9'.repeat(32),operation:'dewu',style_code:'D',mode:'save',status:'completed',stages:[],draft_url:url})''', url)
            self.assertTrue(await self.page.locator('#dewu-draft-link').is_hidden())
            self.assertIsNone(await self.page.locator('#dewu-draft-link').get_attribute('href'))


if __name__ == '__main__':
    unittest.main()
