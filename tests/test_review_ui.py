import re
import unittest
from pathlib import Path

from local_review.ui import SCRIPT, page as review_page


class ReviewUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_claim_retries_same_selection_after_reclaim(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                tab = await browser.new_page()
                html = re.sub(r"<script[^>]+src=\"[^\"]+\"></script>", "", review_page(True))
                await tab.set_content(html)
                await tab.evaluate("""() => {
                  window.confirmCalls = [];
                  let claims = 0, confirmed = false;
                  const task = {
                    id: 'review-1', version: 1, title: '测试鞋',
                    platform_id: 'pdd', field_label: '鞋垫材质',
                    suggested_value_id: null, custom_allowed: false,
                    options: [{value_id: 'pad', label: '皮垫'}],
                    evidence: {}, asset_ids: []
                  };
                  window.fetch = async (path, options={}) => {
                    const body = options.body ? JSON.parse(options.body) : {};
                    const reply = (data, status=200) => new Response(
                      JSON.stringify(data), {status, headers:{'content-type':'application/json'}});
                    if (path === '/api/reviews')
                      return reply({tasks: confirmed ? [] : [{...task, version: claims ? claims+1 : 1}]});
                    if (path.endsWith('/claim')) {
                      claims++;
                      return reply({version: claims+1, status:'claimed'});
                    }
                    if (path.endsWith('/confirm')) {
                      window.confirmCalls.push(body);
                      if (window.confirmCalls.length === 1)
                        return reply({error:'task_locked_or_updated'}, 409);
                      confirmed = true;
                      return reply({status:'resume_ready'});
                    }
                    throw Error('unexpected request: '+path);
                  };
                }""")
                await tab.add_script_tag(content=SCRIPT)
                await tab.get_by_role("button", name="皮垫").click()
                await tab.get_by_role("button", name="确认并继续").click()
                await tab.wait_for_function("window.confirmCalls.length === 2")
                await tab.locator("#empty").wait_for(state="visible")
                self.assertEqual(
                    await tab.evaluate("window.confirmCalls"),
                    [
                        {"version": 2, "final_value_id": "pad"},
                        {"version": 3, "final_value_id": "pad"},
                    ],
                )
            finally:
                await browser.close()

    async def test_workbench_history_search_and_open_product_folder(self):
        from playwright.async_api import async_playwright

        static = Path(__file__).resolve().parents[1] / 'local_review' / 'static'
        html = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '',
                      (static / 'index.html').read_text(encoding='utf-8'))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                tab = await browser.new_page()
                await tab.set_content(html)
                await tab.evaluate('''() => {
                  window.folderCalls = 0;
                  const id = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
                  const jobs = [
                    {id:'1'.repeat(32), style_code:'MY019-1', title:'铁骑皮鞋', platforms:['xhs'],
                     mode:'save', status:'completed', created_at:'2026-09-30T15:00:00+08:00', created_by:'admin', stages:[]},
                    {id:'2'.repeat(32), style_code:'NGBL-2', title:'测试上衣', platforms:['jd'],
                     mode:'save', status:'completed', created_at:'2026-09-30T14:00:00+08:00', created_by:'admin', stages:[]}
                  ];
                  window.fetch = async (path, options={}) => {
                    const reply = data => new Response(JSON.stringify(data), {status:200, headers:{'content-type':'application/json'}});
                    if (path === '/api/launcher/config') return reply({user:{username:'admin',role:'admin'},learning_available:true});
                    if (path === '/api/launcher/products') return reply({products:[{id,title:'铁骑+MY019-1',processed:false}]});
                    if (path === '/api/launcher/products/'+id) return reply({id,title:'铁骑皮鞋',style_code:'MY019-1',category:'男鞋',folder:'铁骑+MY019-1'});
                    if (path === '/api/launcher/products/'+id+'/open-folder') { window.folderCalls++; return reply({opened:true}); }
                    if (path === '/api/launcher/jobs') return reply({jobs});
                    if (path.startsWith('/api/launcher/jobs?search=')) {
                      const query = decodeURIComponent(path.split('=')[1]).toLowerCase();
                      const matches = jobs.filter(job => (job.title+' '+job.style_code).toLowerCase().includes(query));
                      return reply({jobs:matches,total_matches:matches.length});
                    }
                    if (path === '/api/reviews') return reply({tasks:[]});
                    throw Error('unexpected request: '+path);
                  };
                }''')
                await tab.add_script_tag(content=(static / 'app.js').read_text(encoding='utf-8'))
                await tab.get_by_role('button', name='运行记录').click()
                await tab.locator('#history-list .history-row').first.wait_for()
                await tab.get_by_role('searchbox', name='搜索运行记录').fill('MY019')
                await tab.wait_for_function("document.querySelectorAll('#history-list .history-row').length === 1")
                self.assertIn('铁骑皮鞋', await tab.locator('#history-list').inner_text())
                await tab.get_by_role('searchbox', name='搜索运行记录').fill('')
                await tab.wait_for_function("document.querySelectorAll('#history-list .history-row').length === 2")
                await tab.get_by_role('button', name='商品运行').click()
                await tab.get_by_role('button', name='打开当前商品文件夹').click()
                await tab.wait_for_function('window.folderCalls === 1')
            finally:
                await browser.close()

    async def test_learning_service_reenables_after_legacy_disabled_state(self):
        from playwright.async_api import async_playwright

        static = Path(__file__).resolve().parents[1] / 'local_review' / 'static'
        html = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '',
                      (static / 'index.html').read_text(encoding='utf-8'))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                tab = await browser.new_page()
                await tab.set_content(html)
                await tab.evaluate("""() => {
                  Object.defineProperty(window, 'localStorage', {value: {
                    getItem: () => JSON.stringify({learning:false}),
                    setItem: () => {},
                  }});
                  window.fetch = async (path) => {
                    const reply = data => new Response(JSON.stringify(data), {
                      status: 200, headers: {'content-type': 'application/json'}
                    });
                    if (path === '/api/launcher/config')
                      return reply({user:{username:'admin', role:'admin'}, learning_available:true});
                    if (path === '/api/launcher/products') return reply({products:[]});
                    if (path === '/api/launcher/jobs') return reply({jobs:[]});
                    if (path === '/api/reviews') return reply({tasks:[]});
                    throw Error('unexpected request: '+path);
                  };
                }""")
                await tab.add_script_tag(content=(static / 'app.js').read_text(encoding='utf-8'))
                await tab.wait_for_function("document.querySelector('#learning-toggle')?.checked === true")
                self.assertTrue(await tab.locator('#learning-toggle').is_checked())
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
