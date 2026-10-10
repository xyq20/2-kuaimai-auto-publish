import unittest
from local_review.rule_ui import PAGE

class RuleUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_rule_approval_is_explicit_and_renders_untrusted_text(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='chrome', headless=True)
            try:
                tab = await browser.new_page()
                errors=[]
                tab.on('pageerror', lambda error: errors.append(str(error)))
                await tab.route('https://rules.test/**', lambda route: route.fulfill(body=PAGE,content_type='text/html'))
                await tab.add_init_script('''window.actions=[];window.fetch=async(path,opts={})=>{
                    if(opts.method==='POST'){window.actions.push(path);return new Response('{}');}
                    return new Response(JSON.stringify({rules:[{id:'r',status:'pending_review',updated_at:'v',
                    scope:{platform_id:'douyin',field_label:'适用年龄'},proposal:{reason:'<img src=x onerror=alert(1)>'},
                    validation:{passed:false},evidence:[]}]}));};''')
                await tab.goto('https://rules.test/rules')
                await tab.get_by_role('button',name='同意，作为判断参考').wait_for()
                self.assertEqual(await tab.evaluate('window.actions'), [])
                self.assertEqual(await tab.locator('article img').count(), 0)
                await tab.get_by_role('button',name='同意，作为判断参考').click()
                self.assertEqual(await tab.evaluate('window.actions'), ['/api/model-rules/r/approve'])
                self.assertEqual(errors, [])
            finally:
                await browser.close()
