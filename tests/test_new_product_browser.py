"""使用本地 DOM 重现已观察到的快麦模板控件，不连接或写入线上商品。"""
import unittest
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
import kuaimai_erp as erp

HTML = '''
<button onclick="document.querySelector('.el-popover').style.display='block'">新增商品</button>
<div class="el-popover" style="display:none"><div class="drop-list-item" onclick="document.querySelector('.el-drawer').style.display='block'"><span>手工新增商品</span></div></div>
<table><tbody><tr><td>手工新增商品</td></tr><tr><td>手工新增商品</td></tr></tbody></table>
<div class="el-drawer" style="display:none">
  <header>手工新增商品</header>
  <div class="el-form-item"><label class="el-form-item__label">款式编码</label><input id="style-code"></div>
  <div class="el-form-item"><label class="el-form-item__label">商品名称</label><input></div>
  <div class="el-form-item"><label class="el-form-item__label">商品主图</label><div class="sc-upload"><img class="file-img"></div></div>
  <div class="block-specification">
    <div><div class="title-bg"><div class="el-input"><input value="颜色"></div><button>填充常用规格</button></div>
      <div class="specification-value" id="colors"><button onclick="addColor()">添加规格值</button></div>
    </div>
    <div><div class="title-bg"><div class="el-input"><input value="尺码"></div><button onclick="showTemplate()">填充常用规格</button></div>
      <div class="specification-value" id="sizes"></div>
    </div>
  </div>
  <div class="block-specification-list"><button onclick="document.querySelector('#generator').style.display='block'">批量生成</button>
    <div class="el-table"><div class="el-table__header-wrapper"><table><thead><tr id="head"></tr></thead></table></div>
      <div class="el-table__body-wrapper"><table><tbody id="rows"></tbody></table></div></div>
  </div>
  <div class="drawer-footer"><button>保 存</button></div>
</div>
<div class="el-dialog" id="template" style="display:none"><span class="el-dialog__title">常用规格</span><span id="apply"></span></div>
<div class="el-dialog" id="generator" style="display:none"><span class="el-dialog__title">批量生成商品编码</span>
  <label><input type="radio" name="rule">款式编码+序号</label>
  <label><input type="radio" name="rule" checked>款式编码+规格值</label>
  <button onclick="generate()">确 定</button>
</div>
<script>
const sizes=['S','M','L','XL','2XL'];
const labels=['颜色','尺码','商品编码','基本售价','销售价','市场价','成本价','库存','重量(kg)'];
document.querySelector('#head').innerHTML=labels.map(x=>`<th><div class="cell" title="${x}">${x}</div></th>`).join('');
function addColor(){ document.querySelector('#colors').insertAdjacentHTML('afterbegin','<div class="specification-value-flex_input"><input></div>'); }
function showTemplate(){
 document.querySelector('#template').style.display='block';
 setTimeout(()=>document.querySelector('#apply').innerHTML='<a onclick="applyTemplate()">应用至资料</a>',80);
}
function applyTemplate(){
 document.querySelector('#sizes').innerHTML=sizes.map(s=>`<div class="specification-value-flex_input"><input value="${s}"></div>`).join('');
 document.querySelector('#template').style.display='none';
 const colors=[...document.querySelectorAll('#colors .specification-value-flex_input input')].map(input=>input.value);
 document.querySelector('#rows').innerHTML=colors.flatMap(color=>sizes.map(s=>`<tr><td>${color}</td><td>${s}</td><td><input type="text"></td>${Array(6).fill('<td><input type="text" value="0"></td>').join('')}</tr>`)).join('');
}
function generate(){
 [...document.querySelectorAll('#rows tr')].forEach(row=>{
   const cells=row.querySelectorAll('td');
   row.querySelector('input').value=document.querySelector('#style-code').value+cells[0].innerText+cells[1].innerText;
 });
 document.querySelector('#generator').style.display='none';
}
</script>
'''


class BrowserTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_sku_images_follow_untouched_page_colors(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('''
                  <div id="sku-form"><div class="block-specification">
                    <div class="title-bg"><input value="颜色"></div>
                    <div class="specification-value"><div class="specification-value-flex">
                      <div class="specification-value-flex_item">
                        <div class="specification-value-flex_input"><input value="灰色"></div>
                        <div class="specification-value-flex_img"><input type="file"></div>
                      </div>
                      <div class="specification-value-flex_item">
                        <div class="specification-value-flex_input"><input value="棕色"></div>
                        <div class="specification-value-flex_img"><input type="file"></div>
                      </div>
                      <div class="specification-value-flex_item"><button>添加规格值</button></div>
                    </div>
                  </div></div>
                ''')
                with tempfile.TemporaryDirectory() as directory:
                    brown = Path(directory) / '棕色.png'
                    gray = Path(directory) / '灰色.png'
                    brown.write_bytes(b'brown image')
                    gray.write_bytes(b'gray image')
                    with patch.object(erp, 'wait_for_image_uploads', AsyncMock()):
                        with self.assertRaisesRegex(erp.AutomationError, '灰色.*0 张'):
                            await erp.replace_sku_images(
                                page, page.locator('#sku-form'),
                                (brown, Path(directory) / '黑色.png'), 3,
                            )
                        self.assertEqual(
                            await page.locator('.specification-value-flex_img input').evaluate_all(
                                'inputs => inputs.map(input => input.files.length)'
                            ),
                            [0, 0],
                        )
                        uploaded = await erp.replace_sku_images(
                            page, page.locator('#sku-form'), (brown, gray), 3
                        )
                self.assertEqual(uploaded, 2)
                self.assertEqual(
                    await page.locator('.specification-value-flex_img input').evaluate_all(
                        'inputs => inputs.map(input => input.files[0]?.name)'
                    ),
                    ['灰色.png', '棕色.png'],
                )
                self.assertEqual(
                    await page.locator('.specification-value-flex_input input').evaluate_all(
                        'inputs => inputs.map(input => input.value)'
                    ),
                    ['灰色', '棕色'],
                )
                await page.locator('.specification-value-flex_item').nth(1).locator(
                    '.specification-value-flex_img'
                ).evaluate('node => node.remove()')
                with self.assertRaisesRegex(erp.AutomationError, '没有唯一对应的 SKU 图位'):
                    await erp.replace_sku_images(
                        page, page.locator('#sku-form'), (brown, gray), 3
                    )
            finally:
                await browser.close()

    async def test_real_locators_apply_default_templates_and_validate_every_sku(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                # 新增按钮展开后，真实页面会出现“手工新增商品”菜单项；此处标题兼作该菜单项。
                drawer = await erp.open_new_product_drawer(page, 3)
                product = SimpleNamespace(
                    style_code='TEST-7',
                    main_images=[Path('/tmp/product.png')],
                    colors=('复古蓝',),
                )
                args = SimpleNamespace(timeout=3, upload_timeout=3)
                with patch.object(erp, 'sync_image_group', AsyncMock()) as uploading:
                    result = await erp.fill_new_product_form(page, drawer, product, args)
                self.assertEqual(result['size_template'], list(erp.NEW_PRODUCT_SIZES))
                self.assertEqual(result['code_rule'], '款式编码+规格值')
                self.assertEqual(uploading.call_args.args[2], product.main_images)
                report = await erp.validate_new_product_form(drawer, product)
                self.assertEqual(report['sku_count'], 5)
                self.assertEqual(report['rows'][-1]['商品编码'], 'TEST-7复古蓝2XL')
                for index, label in enumerate(('颜色分类', '尺码大小', '* 商品编码 批量生成')):
                    await page.locator('#head th').nth(index).locator('.cell').evaluate(
                        '(cell, label) => { cell.removeAttribute("title"); cell.textContent = label; }',
                        label,
                    )
                sku_rows = await erp.read_base_sku_color_code_rows(drawer)
                self.assertEqual(
                    erp.validate_base_sku_color_codes(sku_rows, 'TEST-7', ('复古蓝',)),
                    5,
                )
                await page.locator('#rows tr').first.locator('td').first.evaluate(
                    "cell => cell.innerText = '灰色'"
                )
                with self.assertRaisesRegex(erp.AutomationError, '颜色与商品编码不一致'):
                    erp.validate_base_sku_color_codes(
                        await erp.read_base_sku_color_code_rows(drawer),
                        'TEST-7', ('复古蓝', '灰色'),
                    )
                await page.locator('#rows tr').last.locator('input').first.fill('WRONG')
                with self.assertRaises(erp.AutomationError):
                    await erp.validate_new_product_form(drawer, product)
                # 新增使用“创建成功”弹窗，并非编辑接口或“保存成功”toast。
                await page.set_content('''<div id="drawer"><div class="drawer-footer"><button onclick="document.querySelector('.el-dialog').style.display='block'">保 存</button></div></div><div class="el-dialog" style="display:none">提示 创建成功 查看商品 继续发布</div>''')
                saved = await erp.click_save_and_confirm(
                    page, page.locator('#drawer'), False, 3,
                    logging.getLogger('create-browser-test'), creation=True,
                )
                self.assertEqual(saved['confirmed_by'], 'creation_dialog')
            finally:
                await browser.close()

    async def test_new_product_colors_follow_excel_order(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('''
                  <div id="drawer"><div class="block-specification">
                    <div class="title-bg"><input value="颜色"></div>
                    <div class="specification-value"><div class="specification-value-flex_input"><input value="灰色"></div>
                      <div class="specification-value-flex_input"><input value="棕色"></div></div>
                  </div></div>
                ''')
                report = await erp.sync_base_color_spec_values(
                    page.locator('#drawer'), ('棕色', '灰色')
                )
                self.assertEqual(report['before'], ('灰色', '棕色'))
                self.assertEqual(report['after'], ('棕色', '灰色'))
                self.assertEqual(report['changed'], 2)
            finally:
                await browser.close()

    async def test_size_chart_missing_page_size_errors_without_editing(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('''
                  <div id="drawer"><div class="block-specification">
                    <div><div class="title-bg"><input value="颜色"></div>
                      <div class="specification-value">
                        <div class="specification-value-flex_input"><input value="灰色"></div>
                        <div class="specification-value-flex_input"><input value="棕色"></div>
                      </div></div>
                    <div><div class="title-bg"><input value="尺码"></div>
                      <div class="specification-value">
                        <div class="specification-value-flex_item"><div class="specification-value-flex_input"><input value="S"></div><button title="删除" onclick="this.parentElement.remove()">删除</button></div>
                        <div class="specification-value-flex_item"><div class="specification-value-flex_input"><input value="M"></div><button title="删除" onclick="this.parentElement.remove()">删除</button></div>
                        <div class="specification-value-flex_item"><div class="specification-value-flex_input"><input value="2XL"></div><button title="删除" onclick="this.parentElement.remove()">删除</button></div>
                      </div></div>
                  </div></div>
                ''')
                drawer = page.locator('#drawer')
                page_sizes = await erp.read_base_specification_values(drawer, '尺码')
                with self.assertRaisesRegex(erp.AutomationError, '页面 S / M / 2XL；尺码表 S / M'):
                    erp.validate_base_size_spec_values(page_sizes, ('S', 'M'))
                self.assertEqual(
                    await erp.read_base_specification_values(drawer, '尺码'),
                    ('S', 'M', '2XL'),
                )
                self.assertEqual(
                    await erp.read_base_specification_values(drawer, '颜色'),
                    ('灰色', '棕色'),
                )
            finally:
                await browser.close()

    async def test_size_chart_extra_size_errors_without_editing(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('''
                  <div id="drawer"><div class="block-specification">
                    <div class="title-bg"><input value="尺码"></div>
                    <div class="specification-value">
                      <div class="specification-value-flex_input"><input value="S"></div>
                      <div class="specification-value-flex_input"><input value="2XL"></div>
                    </div>
                  </div></div>
                ''')
                drawer = page.locator('#drawer')
                page_sizes = await erp.read_base_specification_values(drawer, '尺码')
                with self.assertRaisesRegex(erp.AutomationError, '页面 S / 2XL；尺码表 S / 2XL / 3XL'):
                    erp.validate_base_size_spec_values(page_sizes, ('S', '2XL', '3XL'))
                self.assertEqual(
                    await erp.read_base_specification_values(drawer, '尺码'),
                    ('S', '2XL'),
                )
            finally:
                await browser.close()

    def test_size_chart_order_difference_preserves_page_order(self):
        report = erp.validate_base_size_spec_values(
            ('S', 'M', '2XL'), ('2XL', 'S', 'M')
        )
        self.assertEqual(report['after'], ('S', 'M', '2XL'))
        self.assertEqual(report['added'], 0)
        self.assertEqual(report['removed'], 0)


if __name__ == '__main__':
    unittest.main()
