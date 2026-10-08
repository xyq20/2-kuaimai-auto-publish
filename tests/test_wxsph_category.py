import unittest
from unittest.mock import AsyncMock
from playwright.async_api import async_playwright
from wxsph_form_listing import WxsphFormListing, WxsphFormListingError


class WxsphCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(channel='chrome', headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content('''
          <div id="panel">
            <div class="platform-category-input"><span class="current">服饰内衣 &gt; 男装 &gt; 裤装 &gt; 工装裤</span>
              <button onclick="document.querySelector('[role=dialog]').style.display='block'">修改类目</button>
            </div>
            <div class="recommendation">推荐 <span class="path">服饰内衣 &gt; 男装 &gt; 裤装 &gt; 牛仔裤</span>
              <button onclick="document.querySelector('.current').textContent=document.querySelector('.path').textContent">点击使用</button>
            </div>
          </div>
          <div role="dialog" aria-label="修改类目" style="display:none">
            <input placeholder="请输入类目关键词，支持模糊查询" oninput="window.terms.push(this.value);document.querySelector('.el-autocomplete-suggestion').style.display='block'">
            <div id="selected">已选：</div>
            <button onclick="document.querySelector('.current').textContent=window.chosen;this.parentElement.style.display='none'">确定</button>
          </div>
          <div class="el-autocomplete-suggestion" style="display:none"><ul>
            <li onclick="window.chosen=this.textContent;document.querySelector('#selected').textContent='已选：'+window.chosen">服饰内衣 &gt; 男装 &gt; 裤装 &gt; 牛仔裤</li>
            <li>服饰内衣 &gt; 女装 &gt; 裤子 &gt; 牛仔裤</li>
          </ul></div>
          <script>window.terms=[];</script>
        ''')
        self.listing = WxsphFormListing(self.page, self.page.locator('body'), None,
                                       category_hints=('牛仔裤', '男士直筒牛仔裤'))
        self.listing.panel = self.page.locator('#panel')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def test_valid_recommendation_wins_before_search(self):
        self.assertEqual(await self.listing.apply_recommended_category(), '服饰内衣 > 男装 > 裤装 > 牛仔裤')
        self.assertEqual(await self.page.evaluate('window.terms'), [])
        self.assertEqual(self.listing.category_selection['source'], 'validated_recommendation')

    async def test_empty_current_category_uses_recommendation(self):
        await self.page.locator('.current').evaluate("n=>n.textContent=''")
        self.assertEqual(await self.listing.apply_recommended_category(),
                         '服饰内衣 > 男装 > 裤装 > 牛仔裤')
        self.assertEqual(self.listing.category_selection['previous'], '')
        self.assertEqual(self.listing.category_selection['source'], 'validated_recommendation')
        self.assertEqual(await self.page.evaluate('window.terms'), [])

    async def test_empty_current_category_without_recommendation_searches_excel(self):
        await self.page.locator('.current').evaluate("n=>n.textContent=''")
        await self.page.locator('.recommendation').evaluate('n=>n.remove()')
        self.assertEqual(await self.listing.apply_recommended_category(),
                         '服饰内衣 > 男装 > 裤装 > 牛仔裤')
        self.assertEqual(await self.page.evaluate('window.terms'), ['牛仔裤'])
        self.assertEqual(self.listing.category_selection['source'], 'excel_search')

    async def test_ambiguous_current_category_reports_wechat_error(self):
        await self.page.locator('.platform-category-input').evaluate(
            "n=>n.insertAdjacentHTML('beforeend', '<span class=category-path>服饰内衣 > 男装 > 毛衣</span>')")
        with self.assertRaisesRegex(WxsphFormListingError, '微信小店'):
            await self.listing._category_text()

    async def test_wrong_recommendation_does_not_preserve_wrong_current(self):
        await self.page.locator('.path').evaluate("n=>n.textContent='服饰内衣 > 男装 > 裤装 > 工装裤'")
        self.assertEqual(await self.listing.apply_recommended_category(), '服饰内衣 > 男装 > 裤装 > 牛仔裤')
        self.assertEqual(await self.page.evaluate('window.terms'), ['牛仔裤'])
        self.assertEqual(self.listing.category_selection['source'], 'excel_search')

    async def test_no_recommendation_searches_excel(self):
        await self.page.locator('.recommendation').evaluate('n=>n.remove()')
        await self.page.evaluate("""() => {
          const popup = document.querySelector('.el-autocomplete-suggestion');
          popup.classList.add('el-popper');
          const wrap = document.createElement('div');
          wrap.className = 'categoryList-wrap';
          for (const li of popup.querySelectorAll('li')) {
            const row = document.createElement('div');
            row.className = 'text item';
            row.innerHTML = li.textContent.replace('牛仔裤', '<span style="color:red">牛仔裤</span>');
            row.onclick = li.onclick;
            wrap.appendChild(row);
          }
          popup.replaceChildren(wrap);
        }""")
        self.assertEqual(await self.listing.apply_recommended_category(), '服饰内衣 > 男装 > 裤装 > 牛仔裤')
        self.assertEqual(self.listing.category_selection['recommendations'], [])

    async def test_valid_current_is_retained_after_wrong_recommendation_rejected(self):
        await self.page.locator('.current').evaluate("n=>n.textContent='服饰内衣 > 男装 > 裤装 > 牛仔裤'")
        await self.page.locator('.path').evaluate("n=>n.textContent='服饰内衣 > 男装 > 裤装 > 工装裤'")
        await self.listing.apply_recommended_category()
        self.assertEqual(self.listing.category_selection['source'], 'validated_current')
        self.assertEqual(await self.page.evaluate('window.terms'), [])

    async def test_category_failure_prevents_attribute_writes(self):
        self.listing.apply_recommended_category = AsyncMock(side_effect=WxsphFormListingError('类目不匹配'))
        self.listing._attribute_items = AsyncMock()
        with self.assertRaises(WxsphFormListingError):
            await self.listing.fill_category_attributes(None)
        self.listing._attribute_items.assert_not_called()

    async def test_persisted_category_checked_before_attributes(self):
        self.listing._validate_category_readback = AsyncMock(side_effect=WxsphFormListingError('类目回读不一致'))
        self.listing._verify_persisted_attributes = AsyncMock()
        self.listing._api_dom_validation = AsyncMock()
        with self.assertRaises(WxsphFormListingError):
            await self.listing.verify_persisted_values(None, expected_category='牛仔裤')
        self.listing._verify_persisted_attributes.assert_not_called()
