import re
import unittest
from pathlib import Path

from local_review.ui import SCRIPT, page as review_page


class ReviewUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ocr_card_explains_question_source_and_allows_only_one_meaning(self):
        from playwright.async_api import async_playwright
        from local_review.ui import STYLES
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                tab = await browser.new_page(viewport={'width':1400, 'height':700})
                html = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '', review_page(False, embedded=True))
                await tab.set_content(html)
                await tab.add_style_tag(content=STYLES)
                await tab.add_script_tag(content=SCRIPT)
                await tab.evaluate('''() => {
                    document.getElementById('login').hidden=true;
                    current={id:'ocr',field_id:'ocr:length',title:'工装裤',platform_id:'douyin',field_label:'尺码 OCR 含义 · 衣身丈',
                      suggested_value_id:null,options:[{value_id:'length',label:'裤长'},{value_id:'waist',label:'腰围'}],
                      evidence:{ocr_text:'衣身丈',source_filename:'size.jpg',summary:'识别原文有歧义',ocr_bbox:{x:.1,y:.4,width:.2,height:.03}},
                      asset_ids:[],custom_allowed:false};
                    render(current);
                }''')
                self.assertIn('衣身丈', await tab.locator('#field-name').inner_text())
                self.assertIn('对应哪个测量项', await tab.locator('#review-question').inner_text())
                self.assertTrue(await tab.locator('#review-context').is_visible())
                self.assertIn('尺码原图', await tab.locator('#image-note').inner_text())
                self.assertIn('单选', await tab.locator('#option-search-label').inner_text())
                self.assertEqual(await tab.locator('#no-fill').inner_text(), '不是测量项')
                await tab.get_by_role('button', name='裤长', exact=True).click()
                await tab.get_by_role('button', name='腰围', exact=True).click()
                self.assertEqual(await tab.evaluate('selectedParts'), ['waist'])
                await tab.route('http://review.test/api/assets/chart', lambda route: route.fulfill(
                    content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="800"><rect width="1000" height="800" fill="white"/></svg>'))
                await tab.evaluate("const base=document.createElement('base');base.href='http://review.test/';document.head.append(base);current.asset_ids=['chart'];render(current)")
                await tab.wait_for_function("document.getElementById('product-image').naturalWidth===1000")
                for width, height in [(1400,700), (900,600), (390,650)]:
                    await tab.set_viewport_size({'width':width, 'height':height})
                    await tab.evaluate('highlightOCR(current)')
                    self.assertTrue(await tab.locator('#ocr-highlight').is_visible())
                    measured = await tab.locator('#product-image').evaluate('el=>({width:el.clientWidth,height:el.clientHeight})')
                    mark = await tab.locator('#ocr-highlight').bounding_box()
                    scale = min(measured['width']/1000, measured['height']/800)
                    self.assertAlmostEqual(mark['width'], 200*scale, delta=1)
                    self.assertAlmostEqual(mark['height'], 24*scale, delta=1)
                    for selector in ['#skip', '#no-fill', '#confirm']:
                        box = await tab.locator(selector).bounding_box()
                        self.assertGreaterEqual(box['x'], 0)
                        self.assertLessEqual(box['x'] + box['width'], width)
                        self.assertLessEqual(box['y'] + box['height'], height)
                # Switching back to a normal attribute restores its existing multiple selection.
                await tab.evaluate("current={...current,field_id:'style',field_label:'风格',evidence:{}};render(current)")
                self.assertFalse(await tab.locator('#review-context').is_visible())
                self.assertEqual(await tab.locator('#no-fill').inner_text(), '不填写')
                await tab.get_by_role('button', name='裤长', exact=True).click()
                await tab.get_by_role('button', name='腰围', exact=True).click()
                self.assertEqual(await tab.evaluate('selectedParts'), ['length', 'waist'])
            finally:
                await browser.close()

    async def test_confirmation_reports_run_ended_since_page_loaded(self):
        from playwright.async_api import async_playwright
        from local_review.ui import STYLES
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                tab = await browser.new_page()
                html = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '', review_page(True, embedded=True))
                await tab.set_content(html)
                await tab.add_style_tag(content=STYLES)
                await tab.evaluate('''() => {
                  let confirmed = false;
                  window.fetch = async (path, options={}) => {
                    const reply = data => new Response(JSON.stringify(data), {status:200, headers:{'content-type':'application/json'}});
                    if (path === '/api/reviews') return reply({tasks:confirmed ? [] : [{
                      id:'review', version:1, run_status:'waiting_review', title:'商品', platform_id:'pdd', field_label:'风格',
                      suggested_value_id:null, options:[{value_id:'american',label:'美式'}], evidence:{}, asset_ids:[], custom_allowed:false}]});
                    if (path.endsWith('/claim')) return reply({version:2,status:'claimed'});
                    if (path.endsWith('/confirm')) {
                      confirmed=true;
                      return reply({status:'resume_ready',run_status:'failed',resume_expected:false});
                    }
                    throw Error('unexpected request');
                  };
                }''')
                await tab.add_script_tag(content=SCRIPT)
                await tab.get_by_role('button',name='美式',exact=True).click()
                await tab.get_by_role('button',name='确认并继续',exact=True).click()
                await tab.locator('#empty').wait_for(state='visible')
                self.assertTrue(await tab.locator('#status').is_visible())
                self.assertIn('审核已保存', await tab.locator('#status').inner_text())
                self.assertIn('重新运行', await tab.locator('#status').inner_text())
            finally:
                await browser.close()

    async def test_model_board_displays_results_failures_and_zero_reason(self):
        from playwright.async_api import async_playwright
        static = Path(__file__).resolve().parents[1]/'local_review'/'static'
        html = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '',
                      (static/'index.html').read_text())
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                tab = await browser.new_page(viewport={'width':1280, 'height':900})
                await tab.set_content(html)
                await tab.add_style_tag(content=(static/'styles.css').read_text())
                await tab.evaluate('''() => {
                  window.fetch = async path => new Response(JSON.stringify(
                    path === '/api/launcher/config' ? {user:{username:'admin',role:'admin'},learning_available:true} :
                    path === '/api/launcher/products' ? {products:[]} :
                    path === '/api/launcher/jobs' ? {jobs:[]} : {tasks:[]}
                  ), {status:200,headers:{'content-type':'application/json'}});
                }''')
                await tab.add_script_tag(content=(static/'app.js').read_text())
                await tab.evaluate('''() => {
                  renderJob({id:'a'.repeat(32), style_code:'2024', mode:'save', status:'failed',stages:[],
                    model_decisions:[], model_events:[], model_summary:{api_call_count:0, decision_count:0,
                      adopted_count:0,review_count:0,unavailable_count:0,rejected_count:0,
                      not_called_reason:'未调用大模型：小红书类目路径校验中断'}});
                  document.getElementById('run-dialog').showModal();
                }''')
                board = tab.locator('#model-judgment-details')
                self.assertTrue(await board.is_visible())
                self.assertIn('未调用大模型：小红书类目路径校验中断', await board.inner_text())
                self.assertTrue(await tab.locator('#run-error').is_visible())
                await tab.evaluate('''() => renderJob({id:'a'.repeat(32),style_code:'2024',mode:'save',status:'failed',stages:[],
                  model_summary:{api_call_count:2,adopted_count:1,review_count:0,unavailable_count:1,rejected_count:0},
                  model_events:[{key:'bad',platform_id:'xhs',field_label:'商品类目',status:'unavailable',reason:'DeepSeek 请求超时'}],
                  model_decisions:[{key:'good',platform_id:'wxsph',field_label:'版型',excel_value:'平直廓形',selected_labels:['直筒'],
                    confidence:.99,status:'adopted',model:'deepseek-flash',reason:'含义一致',readback_status:'verified'}]})''')
                text = await board.inner_text()
                self.assertIn('直筒', text)
                self.assertIn('自动采用', text)
                self.assertIn('请求超时', text)
                self.assertEqual(await board.locator('.model-judgment-item').count(), 1)
                self.assertTrue(await tab.locator('#run-error').is_visible())
                self.assertIn('回读通过', text)
                self.assertEqual(await board.locator('.model-metric').count(), 4)
                await tab.evaluate('''() => {
                  window.pendingJob={id:'a'.repeat(32),style_code:'2024',mode:'save',status:'failed',stages:[],pending_reviews:1,
                    review_tasks:[{id:'r1',platform_id:'xhs',field_id:'style',snapshot_version:'s1',status:'pending'}],
                    model_decisions:[{key:'pending',platform_id:'xhs',field_id:'style',field_label:'细分风格',snapshot_version:'s1',
                      excel_value:'美式休闲风',selected_labels:['美式休闲'],confidence:.95,status:'review_required',reason:'语义一致',model:'deepseek-flash'}]};
                  renderJob(window.pendingJob);
                }''')
                self.assertTrue(await tab.locator('#run-review').is_visible())
                self.assertTrue(await board.is_visible())
                self.assertEqual(await board.locator('.model-judgment-item').count(), 0)
                self.assertNotIn('请求超时', await board.inner_text())
                self.assertNotIn('直筒', await board.inner_text())
                await tab.locator('#run-review').click()
                self.assertEqual(await tab.locator('#review-frame').get_attribute('src'),
                                 '/review?embedded=1&run_id='+'a'*32)
                await tab.set_viewport_size({'width':390,'height':844})
                self.assertTrue(await tab.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
            finally:
                await browser.close()

    async def test_full_workbench_review_uses_width_and_keeps_actions_visible(self):
        from playwright.async_api import async_playwright
        from local_review.ui import STYLES
        static = Path(__file__).resolve().parents[1] / 'local_review' / 'static'
        html = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '', (static / 'index.html').read_text())
        inner = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '', review_page(False, embedded=True))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                tab = await browser.new_page()
                await tab.set_content(html)
                await tab.add_style_tag(content=(static / 'styles.css').read_text())
                await tab.evaluate("""inner => {
                    document.body.classList.add('review-active');
                    document.querySelectorAll('main').forEach(e=>e.hidden=e.id!=='review-view');
                    document.getElementById('review-frame').srcdoc=inner;
                }""", inner)
                frame = tab.frames[-1]
                await frame.wait_for_selector('#confirm', state='attached')
                await frame.add_style_tag(content=STYLES)
                await frame.add_script_tag(content=SCRIPT)
                await frame.evaluate("""() => {
                    document.getElementById('login').hidden=true;
                    message('');
                    render({title:'费尔岛羊毛针织衫',platform_id:'pdd',field_label:'风格',
                      options:Array.from({length:78},(_,i)=>({value_id:String(i),label:'候选'+i})),
                      evidence:{},asset_ids:[],custom_allowed:false});
                }""")
                for width,height in [(1920,920),(1440,800),(1024,700),(390,750)]:
                    await tab.set_viewport_size({'width':width,'height':height})
                    await tab.wait_for_timeout(80)
                    panel = await tab.locator('#review-frame').bounding_box()
                    workspace = await tab.locator('.workspace').bounding_box()
                    self.assertGreater(panel['width'], workspace['width'] * .8)
                    self.assertLessEqual(panel['y'] + panel['height'], height)
                    for selector in ['#skip','#no-fill','#confirm']:
                        box = await frame.locator(selector).bounding_box()
                        self.assertGreaterEqual(box['x'], panel['x'])
                        self.assertLessEqual(box['x'] + box['width'], panel['x'] + panel['width'])
                        self.assertLessEqual(box['y'] + box['height'], panel['y'] + panel['height'])
                        self.assertLessEqual(box['y'] + box['height'], height)
                    if width >= 1024:
                        columns = await frame.locator('#review .layout').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns')
                        self.assertEqual(len(columns.split()), 2)
            finally:
                await browser.close()

    async def test_embedded_submit_error_stays_in_view(self):
        from playwright.async_api import async_playwright
        from local_review.ui import STYLES
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                tab = await browser.new_page(viewport={'width':1400, 'height':700})
                html = re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>', '', review_page(False, embedded=True))
                await tab.set_content(html)
                await tab.add_style_tag(content=STYLES)
                await tab.add_script_tag(content=SCRIPT)
                await tab.evaluate("""() => {
                    document.getElementById('login').hidden=true;
                    document.getElementById('review').hidden=false;
                    message('候选校验失败，请刷新审核后重新选择。');
                }""")
                self.assertTrue(await tab.locator('#status').is_visible())
                box = await tab.locator('#status').bounding_box()
                self.assertGreaterEqual(box['y'], 0)
                self.assertLessEqual(box['y'] + box['height'], 700)
                for width, height in [(1400,700),(900,600),(390,650)]:
                    await tab.set_viewport_size({'width':width,'height':height})
                    for selector in ['#skip','#no-fill','#confirm']:
                        button = await tab.locator(selector).bounding_box()
                        self.assertGreaterEqual(button['x'], 0)
                        self.assertLessEqual(button['x'] + button['width'], width)
                        self.assertLessEqual(button['y'] + button['height'], height)

            finally:
                await browser.close()

    async def test_focused_failed_review_loads_and_explains_rerun(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(channel='chrome',headless=True)
            try:
                tab=await browser.new_page()
                html=re.sub(r'<script[^>]+src="[^"]+"[^>]*></script>','',review_page(True))
                await tab.route('https://review.test/**',lambda route:route.fulfill(body=html,content_type='text/html'))
                await tab.goto('https://review.test/review?run_id=failed-run&review_id=target')
                await tab.evaluate('''() => {
                  window.requests=[];
                  const task={id:'target',version:1,run_status:'failed',title:'商品',platform_id:'xhs',field_label:'细分风格',
                    options:[{value_id:'young',label:'18-24岁'},{value_id:'adult',label:'25-34岁'}],
                    suggested_value_id:'young,adult',evidence:{},asset_ids:[],custom_allowed:false};
                  window.fetch=async path=>{
                    window.requests.push(path);
                    return new Response(JSON.stringify(path.includes('/claim')?{version:2,status:'claimed'}:
                      {tasks:[{...task,id:'other'},task]}),{status:200,headers:{'content-type':'application/json'}});
                  };
                }''')
                await tab.add_script_tag(content=SCRIPT)
                await tab.locator('#review').wait_for(state='visible')
                self.assertIn('/api/reviews?run_id=failed-run',await tab.evaluate('window.requests'))
                self.assertIn('/api/reviews/target/claim',await tab.evaluate('window.requests'))
                self.assertIn('审核后需重新运行',await tab.locator('#progress').inner_text())
                self.assertEqual(await tab.locator('#confirm').inner_text(),'确认并保存审核')
                self.assertEqual(await tab.locator('#suggestion').inner_text(), '18-24岁、25-34岁')
                self.assertEqual(await tab.locator('[aria-pressed=true]').count(), 2)
                self.assertFalse(await tab.evaluate('selectionDiffers()'))
                await tab.get_by_role('button', name='25-34岁', exact=True).click()
                self.assertTrue(await tab.evaluate('selectionDiffers()'))
                self.assertTrue(await tab.locator('#reason-label').is_visible())
            finally: await browser.close()

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
