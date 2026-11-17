"""Browser regressions for merged SKU cells and shared attribute learning."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from playwright.async_api import async_playwright

from attribute_runtime import AttributeRuntime, ReviewBatchRequired
from bilibili_data import BilibiliError
from bilibili_listing import BilibiliListing
from learning_store import LearningStore
from tests.test_dewu_workbench_ui import HTML, SCRIPT, MOCK


SELECT_HTML = '''
<div class="attr-panel"><span class="label">面料</span>
 <div class="el-select"><input aria-controls="material-options" aria-expanded="false"></div>
</div>
<div id="old-options"><div class="el-select-dropdown__item selected">错误旧候选</div></div>
<div id="material-options" style="display:none">
 <div class="el-select-dropdown__item is-selected">棉</div>
 <div class="el-select-dropdown__item">羊毛</div>
 <div class="el-select-dropdown__item">尼龙</div>
</div>
<script>
 const root=document.querySelector('.el-select'),input=root.querySelector('input'),popup=document.querySelector('#material-options');
 root.onclick=()=>{const open=input.getAttribute('aria-expanded')==='true';popup.style.display=open?'none':'block';input.setAttribute('aria-expanded',String(!open));};
 input.onkeydown=e=>{if(e.key==='Escape'){popup.style.display='none';input.setAttribute('aria-expanded','false')}};
 popup.querySelectorAll('.el-select-dropdown__item').forEach(o=>o.onclick=()=>o.classList.toggle('is-selected'));
</script>'''


class BilibiliBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='chrome', headless=True)
        self.page = await self.browser.new_page(viewport={'width': 1440, 'height': 960})
        self.store = LearningStore(self.root / 'learning.sqlite3')
        self.store.migrate()
        self.client = Mock()
        self.runtime = AttributeRuntime(self.store, self.client, 'run', 'product')
        self.listing = BilibiliListing(self.page, SimpleNamespace(style_code='TEST'), self.root,
                                       timeout=1, attribute_runtime=self.runtime)

    async def asyncTearDown(self):
        await self.runtime.drain()
        await self.listing.capture.close()
        await self.browser.close()
        await self.pw.stop()
        self.store.close()
        self.temp.cleanup()

    async def test_multiselect_clears_stale_values_uses_owned_popup_and_shared_runtime(self):
        await self.page.set_content(SELECT_HTML.replace('class="el-select-dropdown__item">羊毛', 'class="el-select-dropdown__item is-selected">羊毛'))
        attr = {'attrId': 30, 'attrName': '面料', 'attrInputType': 2, 'isMust': False,
                'attrValues': [{'attrValueId': i, 'attrValueName': name} for i, name in enumerate(('棉', '羊毛', '尼龙'))]}
        self.listing.category_id = '24008'
        await self.listing.fill_attribute(attr, {'面料': '羊毛，尼龙'})
        self.assertEqual(self.listing.report['steps']['attributes']['面料'], ['羊毛', '尼龙'])
        self.assertEqual(await self.page.locator('#old-options .selected').inner_text(), '错误旧候选')
        self.client.semantic_judge.assert_not_called()
        self.assertIn(('bilibili', '面料'), self.runtime._resolved_attributes)
        self.assertEqual(self.runtime.record_verified_readbacks('bilibili', {'面料': ['尼龙', '羊毛']}), 1)

    async def test_api_dom_candidate_mismatch_blocks_learning_and_write(self):
        await self.page.set_content(SELECT_HTML)
        with self.assertRaisesRegex(BilibiliError, '接口候选'):
            await self.listing.fill_attribute({'attrId': 30, 'attrName': '面料', 'attrInputType': 2,
                'attrValues': [{'attrValueId': 1, 'attrValueName': '不存在'}]}, {'面料': '不存在'})
        self.client.semantic_judge.assert_not_called()
        self.assertEqual(await self.page.locator('#material-options .is-selected').inner_text(), '棉')

    async def test_unmatched_attribute_enters_shared_review_batch_without_writing_guess(self):
        await self.page.set_content(SELECT_HTML)
        self.client.review_learning.return_value = {'status': 'review_required', 'reason_code': 'no_learning'}
        self.client.semantic_judge.return_value = {'source': 'deepseek', 'status': 'unavailable',
                                                   'api_called': False, 'reason': 'fixture unavailable'}
        attr = {'attrId': 30, 'attrName': '面料', 'attrInputType': 2,
                'attrValues': [{'attrValueId': i, 'attrValueName': name} for i, name in enumerate(('棉', '羊毛', '尼龙'))]}
        self.listing.category_id = '24008'
        self.runtime.begin_review_collection('bilibili')
        await self.listing.fill_attribute(attr, {'面料': '暖和'})
        with self.assertRaises(ReviewBatchRequired) as error:
            self.runtime.raise_deferred_reviews()
        self.assertEqual(error.exception.reviews[0].request.platform_id, 'bilibili')
        self.assertEqual(await self.page.locator('#material-options .is-selected').inner_text(), '棉')

    async def test_real_rowspan_maps_sku_inputs_to_each_size(self):
        self.listing.product = SimpleNamespace(colors=('深灰色',), sizes=('S', 'M', 'L'), price='590', stock='50',
                                              merchant_code=lambda color, size: 'NGBL-2128' + color + size)
        await self.page.set_content('''<div class="price-stock-form-item"><div class="el-table">
          <div class="el-table__header-wrapper"><table><tr>
          <th>颜色</th><th>尺码</th><th>*售价(元)</th><th>总库存</th><th>*可售库存</th><th>预留库存</th><th>商家编码</th>
          </tr></table></div><div class="el-table__body-wrapper"><table>
          <tr class="el-table__row"><td rowspan="3">深灰色</td><td>S</td><td><input></td><td>0</td><td><input></td><td>0</td><td><input></td></tr>
          <tr class="el-table__row"><td>M</td><td><input></td><td>0</td><td><input></td><td>0</td><td><input></td></tr>
          <tr class="el-table__row"><td>L</td><td><input></td><td>0</td><td><input></td><td>0</td><td><input></td></tr>
          </table></div></div></div>''')
        await self.listing.fill_skus()
        values = await self.page.locator('input').evaluate_all('es=>es.map(e=>e.value)')
        self.assertEqual(values, [v for size in ('S', 'M', 'L') for v in ('590', '50', 'NGBL-2128深灰色' + size)])

    async def test_detail_preview_does_not_duplicate_upload_receipts(self):
        await self.page.set_content('''<div id="details"><img class="el-image__inner" src="https://assets.test/a.jpg@960w.webp">
          <div class="image-list"><div class="image-result"><img src="https://assets.test/a.jpg@600w.webp"></div></div></div>''')
        self.assertEqual(await self.listing.image_urls(self.page.locator('#details')), ['https://assets.test/a.jpg'])

    async def test_portrait_upload_uses_selected_ratio_and_blocks_saving_after_ratio_switch(self):
        paths = (self.root / 'portrait-1.png', self.root / 'portrait-2.png')
        for path in paths:
            path.write_bytes(b'image-fixture')
        self.listing.product = SimpleNamespace(image_ratio='3:4', images=paths)
        self.listing.category_id = '23993'
        self.listing.capture.attributes['23993'] = {'imgScaleOptions': ['1:1']}
        await self.page.set_content('''<div id="image-list"><div class="main-image-field">
          <label class="el-radio"><input type="radio" name="ratio" value="1:1" checked><span class="el-radio__label">1:1</span></label>
          <label class="el-radio"><input type="radio" name="ratio" value="3:4"><span class="el-radio__label">3:4</span></label>
          <div class="img-upload-wrap"><div id="images"></div><input type="file"></div>
          </div></div><button>保存草稿</button><script>
          const images={'1:1':['https://assets.test/square.png'],'3:4':[]};
          const ratio=()=>document.querySelector('input[type=radio]:checked').value;
          const render=()=>document.querySelector('#images').innerHTML=images[ratio()].map(src=>`<img class="el-image__inner" src="${src}">`).join('');
          document.querySelectorAll('input[type=radio]').forEach(e=>e.onchange=render);
          document.querySelector('input[type=file]').onchange=e=>{images[ratio()].push('https://assets.test/'+e.target.files[0].name);render();};
          let saves=0;document.querySelector('button').onclick=()=>saves++;
          render();
          </script>''')
        await self.listing.fill_main_images()
        self.assertEqual(await self.page.locator('input[type=radio]:checked').input_value(), '3:4')
        self.assertEqual(await self.listing.image_urls(self.page.locator('.img-upload-wrap')),
                         ['https://assets.test/portrait-1.png', 'https://assets.test/portrait-2.png'])
        self.assertEqual(self.listing.report['steps']['main_images'], {'ratio': '3:4', 'count': 2, 'verified': True})
        self.assertEqual(self.listing.report['steps']['main_image_ratio_options'], {'page': ['1:1', '3:4'], 'api': ['1:1']})
        await self.page.locator('input[value="1:1"]').check()
        with self.assertRaisesRegex(BilibiliError, '主图比例'):
            await self.listing.save()
        self.assertEqual(await self.page.evaluate('saves'), 0)
        await self.page.locator('input[value="3:4"]').check()
        await self.page.locator('#images img').last.evaluate('e=>e.remove()')
        with self.assertRaisesRegex(BilibiliError, '主图数量或顺序'):
            await self.listing.save()
        self.assertEqual(await self.page.evaluate('saves'), 0)

    async def boot_workbench(self, **config):
        await self.page.set_content(HTML)
        mock = MOCK.replace("'dewu'", "'bilibili'").replace('DEWU-1', 'BILI-1')
        mock = mock.replace('https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/123456',
                            'https://shop.bilibili.com/#/g/goods/publish?draftId=123456')
        await self.page.evaluate(mock, config)
        await self.page.add_script_tag(content=SCRIPT)
        await self.page.wait_for_function("document.querySelector('#account').textContent.includes('admin')")

    async def bili(self):
        await self.page.get_by_role('button', name='bilibili', exact=True).click()
        await self.page.wait_for_function("document.querySelector('#product-code').textContent==='BILI-1' && !document.querySelector('#preview-run').disabled")

    async def test_workbench_selects_own_catalog_learning_and_default_draft(self):
        await self.boot_workbench()
        await self.bili()
        self.assertEqual(await self.page.locator('#plan-list li').count(), 1)
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '保存草稿')
        self.assertTrue(await self.page.locator('#learning-toggle').is_checked())
        self.assertTrue(await self.page.locator('[data-mode=preview]').is_enabled())
        self.assertTrue(await self.page.locator('[data-mode=publish]').is_enabled())
        await self.page.locator('#preview-run').click()
        await self.page.get_by_role('button', name='确认开始', exact=True).click()
        await self.page.wait_for_function('createdJobs.length===1')
        job = await self.page.evaluate('createdJobs[0]')
        self.assertEqual((job['operation'], job['platforms'], job['product_id'], job['mode']),
                         ('bilibili', ['bilibili'], 'd'*32, 'save'))
        self.assertTrue(job['learning'])
        self.assertFalse(job['confirm_publish'])
        await self.page.locator('#dewu-draft-link').wait_for(state='visible')
        self.assertIn('draftId=123456', await self.page.locator('#dewu-draft-link').get_attribute('href'))

    async def test_publish_requires_workbench_confirmation_and_switch_resets_mode(self):
        await self.boot_workbench()
        await self.bili()
        await self.page.locator('[data-mode=publish]').click()
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '发布商品')
        await self.page.locator('#preview-run').click()
        self.assertEqual(await self.page.evaluate('createdJobs.length'), 0)
        await self.page.get_by_role('button', name='确认发布商品', exact=True).click()
        await self.page.wait_for_function('createdJobs.length===1')
        self.assertTrue((await self.page.evaluate('createdJobs[0]'))['confirm_publish'])

    async def test_saved_bilibili_selection_restores_own_source_and_select_all_excludes_it(self):
        await self.boot_workbench(saved={'operation': 'bilibili', 'selected': ['bilibili'], 'productId': 'd'*32, 'mode': 'publish'})
        await self.page.wait_for_function("document.querySelector('#product-code').textContent==='BILI-1'")
        self.assertEqual(await self.page.locator('#plan-mode').inner_text(), '保存草稿')
        await self.page.get_by_role('button', name='全选', exact=True).click()
        await self.page.wait_for_function("document.querySelector('#product-code').textContent==='PLAIN-1'")
        self.assertEqual(await self.page.locator('[data-platform=bilibili]').get_attribute('aria-pressed'), 'false')
        self.assertEqual(await self.page.locator('[data-platform][aria-pressed=true]').count(), 8)


if __name__ == '__main__':
    unittest.main()
