"""Local DOM regressions for the independent Dewu draft adapter.

The fixtures use the Element/Ant control structures observed on Dewu, and do
not contact Dewu or create any real draft.
"""
from __future__ import annotations

import base64
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from dewu_data import DEWU_START_URL, DewuError
from dewu_listing import (DewuListing, choose_resume, image_fingerprint, product_identity,
                          needs_sku_code_update, sku_codes_match, run_dewu_browser, REVIEW_ENDPOINT,
                          white_backgrounds_complete, WHITE_BACKGROUND_RECIPE)


DRAFT_URL = 'https://stark.dewu.com/vueProduct/newProductApply/spuEdit/operation/123456?noLayout=1'


class DewuRecoveryTests(unittest.TestCase):
    def test_saved_receipt_requires_every_sized_code_before_skipping(self):
        product = SimpleNamespace(colors=('卡其色',), sizes=('S', '2XL', '3XL'), sku_code='7076卡其色')
        rows = [{'颜色': '卡其色', '尺码': size, '商品编码': '7076卡其色' + size} for size in product.sizes]
        self.assertTrue(sku_codes_match(product, rows))
        for invalid in (None, [], rows[:-1], rows + rows[:1],
                        [{**row, '商品编码': '7076卡其色'} for row in rows]):
            with self.subTest(rows=invalid):
                self.assertFalse(sku_codes_match(product, invalid))

    def test_legacy_saved_draft_and_interrupted_code_update_use_code_only_path(self):
        previous = {'fingerprint': 'same', 'draft_url': DRAFT_URL}
        self.assertTrue(needs_sku_code_update(previous, '', 'same', True))
        previous.update(status='failed', update_scope='sku_codes')
        self.assertTrue(needs_sku_code_update(previous, '', 'same', False))
        self.assertFalse(needs_sku_code_update(previous, DRAFT_URL, 'same', False))
        self.assertFalse(needs_sku_code_update(previous, '', 'changed', False))
        self.assertFalse(needs_sku_code_update({}, '', 'same', False))

    def test_pending_creation_without_url_cannot_create_again(self):
        with self.assertRaisesRegex(DewuError, '已点击创建'):
            choose_resume({'fingerprint': 'same', 'creation_attempted': True}, '', 'same')

    def test_saved_draft_with_unchanged_inputs_is_skipped(self):
        previous = {'fingerprint': 'same', 'draft_url': DRAFT_URL, 'status': 'draft_saved'}
        self.assertEqual(choose_resume(previous, '', 'same'), (DRAFT_URL, True))

    def test_changed_attributes_resume_known_draft_without_skipping_updates(self):
        previous = {'fingerprint': 'old', 'draft_url': DRAFT_URL, 'status': 'draft_saved'}
        self.assertEqual(choose_resume(previous, '', 'new'), (DRAFT_URL, False))
        previous['status'] = 'failed'
        self.assertEqual(choose_resume(previous, '', 'new'), (DRAFT_URL, False))

    def test_identity_changes_still_require_explicit_draft_selection(self):
        identity = {'excel_path': '/products/工装裤/产品信息.xlsx', 'style_code': 'NGBL-7076',
                    'brand': 'NEIGBORL', 'category': ['服装', '长裤', '工装裤']}
        previous = {'fingerprint': 'old', 'draft_url': DRAFT_URL, 'status': 'draft_saved',
                    'product_identity': identity}
        self.assertEqual(choose_resume(previous, '', 'new', identity=identity), (DRAFT_URL, False))
        for key, value in {'excel_path':'/products/其他/产品信息.xlsx', 'style_code':'OTHER',
                           'brand':'其他品牌', 'category':['服装', '衬衫']}.items():
            with self.subTest(key=key), self.assertRaisesRegex(DewuError, '原草稿记录不一致'):
                choose_resume(previous, '', 'new', identity={**identity, key:value})
            self.assertEqual(choose_resume(previous, DRAFT_URL, 'new', identity={**identity, key:value}),
                             (DRAFT_URL, False))

    def test_legacy_report_uses_retained_identity_without_blocking_attribute_updates(self):
        identity = {'excel_path': '/products/工装裤/产品信息.xlsx', 'style_code': 'NGBL-7076',
                    'brand': 'NEIGBORL', 'category': ['服装', '长裤', '工装裤']}
        previous = {'fingerprint': 'old', 'draft_url': DRAFT_URL, 'excel_path': identity['excel_path'],
                    'steps': {'basic': {'style_code': 'NGBL-7076'}}}
        self.assertEqual(choose_resume(previous, '', 'new', identity=identity), (DRAFT_URL, False))
        with self.assertRaisesRegex(DewuError, '原草稿记录不一致'):
            choose_resume(previous, '', 'new', identity={**identity, 'style_code':'OTHER'})

    def test_changed_excel_cannot_repeat_uncertain_creation_without_recorded_url(self):
        with self.assertRaisesRegex(DewuError, '已点击创建'):
            choose_resume({'fingerprint':'old', 'creation_attempted':True}, '', 'new')

    def test_explicit_official_draft_allows_resuming_changed_inputs(self):
        previous = {'fingerprint': 'old', 'draft_url': DRAFT_URL, 'status': 'draft_saved'}
        self.assertEqual(choose_resume(previous, DRAFT_URL, 'new'), (DRAFT_URL, False))

    def test_explicit_non_draft_or_nonofficial_urls_are_rejected(self):
        for url in ('https://example.com/vueProduct/newProductApply/spuEdit/operation/123456',
                    'https://stark.dewu.com.evil.example/vueProduct/newProductApply/spuEdit/operation/123456',
                    'https://stark.dewu.com/vueProduct/newProductApply/start?noLayout=1'):
            with self.subTest(url=url), self.assertRaises(DewuError):
                choose_resume({}, url, 'same')

    def test_first_run_and_failed_draft_are_distinguished(self):
        self.assertEqual(choose_resume({}, '', 'same'), ('', False))
        previous = {'fingerprint': 'same', 'draft_url': DRAFT_URL, 'status': 'failed'}
        self.assertEqual(choose_resume(previous, '', 'same'), (DRAFT_URL, False))


MULTISELECT_HTML = """
<div id="field"><div class="el-select">
  <div class="el-select__tags"></div>
  <div class="el-input"><input class="el-input__inner" readonly></div>
  <input class="el-select__input">
</div></div>
<div class="el-select-dropdown" style="display:none"></div>
<script>
window.selected = ['旧值一', '旧值二'];
const choices = ['旧值一', '旧值二', '棉', '羊毛'];
const dropdown = document.querySelector('.el-select-dropdown');
function render() {
  const tags = document.querySelector('.el-select__tags');
  tags.innerHTML = '';
  if (selected.length) {
    const tag = document.createElement('span'); tag.className = 'el-tag';
    tag.title = selected[0]; tag.append(selected[0]);
    const close = document.createElement('button'); close.className = 'el-tag__close';
    close.textContent = '×'; close.onclick = () => { selected.shift(); render(); };
    tag.append(close); tags.append(tag);
    if (selected.length > 1) {
      const more = document.createElement('span'); more.className = 'el-tag';
      more.textContent = '+ ' + (selected.length - 1); tags.append(more);
    }
  }
  dropdown.innerHTML = '';
  for (const value of choices) {
    const item = document.createElement('div');
    item.className = 'el-select-dropdown__item' + (selected.includes(value) ? ' selected' : '');
    item.textContent = value;
    item.onclick = () => {
      if (!selected.includes(value)) selected.push(value);
      if (window.injectExtra && value === '棉') selected.push('旧值一');
      render();
    };
    dropdown.append(item);
  }
}
document.querySelector('.el-input__inner').onclick = () => dropdown.style.display = 'block';
document.querySelector('.el-select__input').onkeydown = e => {
  if (e.key === 'Escape') dropdown.style.display = 'none';
};
render();
</script>
"""


COLOR_HTML = """
<div class="el-form-item"><label class="el-form-item__label">颜色</label>
  <div class="color-list"></div><button id="addColor">新增</button>
</div>
<script>
function addColor(value = '') {
  const list = document.querySelector('.color-list');
  const row = document.createElement('div'); row.className = 'singleDragItem';
  const input = document.createElement('input');
  input.placeholder = '请选择或输入标准色'; input.value = value;
  input.oninput = () => {
    if (input.value && row === list.lastElementChild) addColor();
  };
  const remove = document.createElement('button'); remove.textContent = '删除';
  const wrapper = document.createElement('div'); wrapper.setAttribute('placeholder', input.placeholder);
  wrapper.append(input);
  remove.onclick = () => row.remove(); row.append(wrapper, remove); list.append(row);
}
document.getElementById('addColor').onclick = () => addColor();
addColor();
</script>
"""


SKU_HTML = """
<div class="batch-form">
  <input id="merchantSkuNo" placeholder="商品编码">
  <input id="bidPrice" placeholder="出价">
  <input id="stock" placeholder="库存">
  <div class="ant-select" data-kind="biddingCode"><div class="ant-select-selector">
    <span class="ant-select-selection-item">请选择</span><input id="biddingCode" aria-controls="sku-options" readonly>
  </div></div>
  <div class="ant-select" data-kind="status"><div class="ant-select-selector">
    <span class="ant-select-selection-item">请选择</span><input id="status" aria-controls="sku-options" readonly>
  </div></div>
  <button id="batch">批量设置</button>
</div>
<div id="sku-options" class="ant-select-dropdown" style="display:none"></div>
<div class="el-table">
  <div class="el-table__header-wrapper"><table><thead><tr>
    <th class="el-table_1_column_1"><div class="cell">颜色</div></th>
    <th class="el-table_1_column_2"><div class="cell">尺码</div></th>
    <th class="el-table_1_column_3"><div class="cell">SKUID</div></th>
    <th class="el-table_1_column_4"><div class="cell"><span>商品编码</span> <div role="tooltip" style="display:none">商品编码填写指南</div></div></th>
    <th class="el-table_1_column_5"><div class="cell">辅助编码</div></th>
    <th class="el-table_1_column_6"><div class="cell">出价类型</div></th>
    <th class="el-table_1_column_7"><div class="cell">出价</div></th>
    <th class="el-table_1_column_8"><div class="cell">预计收入</div></th>
    <th class="el-table_1_column_9"><div class="cell">库存</div></th>
    <th class="el-table_1_column_10"><div class="cell">包装长</div></th>
    <th class="el-table_1_column_11"><div class="cell">包装宽</div></th>
    <th class="el-table_1_column_12"><div class="cell">包装高</div></th>
    <th class="el-table_1_column_13"><div class="cell">重量</div></th>
  </tr></thead></table></div>
  <div class="el-table__body-wrapper"><table><tbody id="skuRows"></tbody></table></div>
  <div class="el-table__fixed-right"><div class="el-table__fixed-body-wrapper">
    <table><tbody id="fixedRows"></tbody></table>
  </div></div>
</div>
<div class="el-pagination" style="display:none">
  <button class="btn-prev">上一页</button><ul class="el-pager"></ul><button class="btn-next">下一页</button>
</div>
<script>
window.failureMode = '';
window.skuSizes = ['S','M'];
window.skuColors = ['卡其色'];
window.pageSize = 100;
window.currentPage = 1;
window.skuEdits = {};
window.batchClicks = 0;
let appliedState = false;
const dropdown = document.querySelector('.ant-select-dropdown');
for (const select of document.querySelectorAll('.batch-form .ant-select')) {
  select.querySelector('.ant-select-selector').onclick = () => {
    dropdown.innerHTML = ''; dropdown.style.display = 'block';
    const options = select.dataset.kind === 'biddingCode' ? ['直发预售','现货'] : ['上架','下架'];
    for (const value of options) {
      const item = document.createElement('div'); item.className = 'ant-select-item-option';
      const label = document.createElement('div'); label.className = 'ant-select-item-option-content';
      label.textContent = value; item.append(label);
      item.onclick = () => {
        select.querySelector('input').value = value;
        select.querySelector('.ant-select-selection-item').textContent = value;
        dropdown.style.display = 'none';
      };
      dropdown.append(item);
    }
  };
}
function renderSku(applied) {
  appliedState = applied;
  document.getElementById('skuRows').innerHTML = '';
  document.getElementById('fixedRows').innerHTML = '';
  const variants = skuColors.flatMap(color => skuSizes.map(size => ({color,size})));
  variants.slice((currentPage-1)*pageSize,currentPage*pageSize).forEach(({color,size}, index) => {
    const key = color + '|' + size;
    const code = skuEdits[key] ?? (applied ? document.getElementById('merchantSkuNo').value : '');
    let price = applied ? document.getElementById('bidPrice').value : '0';
    let stock = applied ? document.getElementById('stock').value : '0';
    if (failureMode === 'swap') [price, stock] = [stock, price];
    const type = applied && failureMode !== 'type' ? document.getElementById('biddingCode').value : '现货';
    const listed = applied && failureMode !== 'status' && document.getElementById('status').value === '上架';
    const shownSize = failureMode === 'size' && index === 1 ? 'S' : size;
    const tr = document.createElement('tr'); tr.className = 'el-table__row';
    const values = [
      color, shownSize, 'sku-' + size,
      `<div class="el-input"><input class="el-input__inner" value="${code}"></div>`,
      `<div class="el-input"><input class="el-input__inner" value=""></div>`,
      `<div class="el-select"><div class="el-input"><input class="el-input__inner" readonly value="${type}"></div></div>`,
      `<div class="el-input"><input class="el-input__inner" value="${price}"></div>`,
      '800',
      `<div class="el-input"><input class="el-input__inner" value="${stock}"></div>`,
      '', '', '', ''
    ];
    values.forEach((value, column) => {
      const td = document.createElement('td'); td.className = `el-table_1_column_${column+1}`;
      td.innerHTML = `<div class="cell">${value}</div>`; tr.append(td);
    });
    const codeInput = tr.querySelector('.el-table_1_column_4 input');
    codeInput.oninput = () => { skuEdits[key] = codeInput.value; };
    codeInput.onblur = () => {
      if (failureMode === 'code') codeInput.value = 'rejected';
    };
    document.getElementById('skuRows').append(tr);
    const fixed = document.createElement('tr'); fixed.className = 'el-table__row';
    fixed.innerHTML = `<td class="el-table_1_column_14"><div class="cell"><div class="el-switch ${listed ? 'is-checked' : ''}" role="switch" aria-checked="${listed}"><input class="el-switch__input" type="checkbox" ${listed ? 'checked' : ''}></div></div></td>`;
    document.getElementById('fixedRows').append(fixed);
  });
  const pages = Math.ceil(variants.length/pageSize);
  const pagination = document.querySelector('.el-pagination');
  pagination.style.display = pages > 1 ? '' : 'none';
  pagination.querySelector('.btn-prev').disabled = currentPage === 1;
  pagination.querySelector('.btn-next').disabled = currentPage === pages;
  pagination.querySelector('.el-pager').innerHTML = '';
  for (let page=1; page<=pages; page++) {
    const item = document.createElement('li');
    item.className = 'number' + (page === currentPage ? ' active' : '');
    item.textContent = page; item.onclick = () => navigate(page);
    pagination.querySelector('.el-pager').append(item);
  }
}
function navigate(page) { currentPage = page; renderSku(appliedState); }
document.querySelector('.btn-next').onclick = () => navigate(currentPage+1);
document.querySelector('.btn-prev').onclick = () => navigate(currentPage-1);
document.getElementById('batch').onclick = () => { batchClicks++; skuEdits={}; renderSku(true); };
renderSku(false);
</script>
"""


CHART_HTML = """
<div id="chart" class="ant-modal-content">
  <div id="parameters"></div>
  <table><thead><tr id="headers"><th>尺码</th></tr></thead>
    <tbody id="chartRows"></tbody></table>
</div>
<script>
const names = ['腰围/WAISTLINE(cm)', '裤长/LENGTH(cm)', '脚围/FOOT OPENING(cm)'];
const values = [['S','78','104','38.5'],['M','82','105','40']];
window.renameHistory = [];
function renderChart() {
  document.getElementById('parameters').innerHTML = '';
  document.getElementById('headers').innerHTML = '<th>尺码</th>';
  names.forEach((name, index) => {
    const parent = document.createElement('div');
    const label = document.createElement('label'); label.className = 'ant-checkbox-wrapper';
    label.innerHTML = `<input type="checkbox" checked><span>${name}</span>`;
    const edit = document.createElement('button'); edit.setAttribute('aria-label','edit');
    edit.textContent = '编辑'; edit.onclick = () => editParameter(index);
    parent.append(label, edit); document.getElementById('parameters').append(parent);
    const th = document.createElement('th'); th.textContent = name;
    document.getElementById('headers').append(th);
  });
  const actions = document.createElement('th'); actions.textContent = '操作';
  document.getElementById('headers').append(actions);
  document.getElementById('chartRows').innerHTML = '';
  for (const row of values) {
    const tr = document.createElement('tr');
    for (const value of row) {
      const td = document.createElement('td'); const input = document.createElement('input');
      input.className = 'ant-input'; input.value = value; td.append(input); tr.append(td);
    }
    tr.append(document.createElement('td'));
    document.getElementById('chartRows').append(tr);
  }
}
function editParameter(index) {
  const editor = document.createElement('div'); editor.className = 'ant-modal-content';
  editor.innerHTML = `<div>编辑尺码参数</div><input id="name"><input id="unit" value="cm"><button>确 定</button>`;
  document.body.append(editor); editor.querySelector('#name').value = names[index].split('/')[0];
  editor.querySelector('button').onclick = () => {
    const name = editor.querySelector('#name').value;
    const standard = document.createElement('div'); standard.className = 'ant-modal-content';
    standard.innerHTML = `<div>当前您编辑自定义参数【${name}】，系统已自动匹配平台标准参数【${name}】，是否确认添加？</div><button>确 定</button>`;
    standard.querySelector('button').onclick = () => {
      names[index] = name + '(cm)'; renameHistory.push(name);
      setTimeout(() => { standard.remove(); editor.remove(); renderChart(); }, 200);
    };
    document.body.append(standard);
  };
}
renderChart();
</script>
"""


RUN_HTML = """
<main>
  <p>已选类目：服装 >> 长裤 >> 工装裤</p>
  <div class="el-form-item"><label class="el-form-item__label">品牌</label>
    <input value="NEIGBORL" disabled>
  </div>
  <div class="el-form-item"><label class="el-form-item__label">货号</label>
    <input value="NGBL-7076">
  </div>
  <input placeholder="卖点提炼" value="测试商品">
  <div id="errors"></div>
  <button onclick="saveClicks++; const toast=document.createElement('div'); toast.className='el-message--success'; toast.textContent='保存成功'; document.body.append(toast)">保存草稿</button>
  <button onclick="reviewClicks++">提交审核</button>
</main>
<script>window.saveClicks=0; window.reviewClicks=0;</script>
"""


CAROUSEL_HTML = """
<div class="el-form-item"><label class="el-form-item__label">商品轮播图</label>
  <div class="du-upload" id="carousel">
    <ul class="el-upload-list"><li class="is-success"><img class="el-upload-list__item-thumbnail" src="__FIRST_URL__"></li></ul>
    <div class="el-upload"><span>正面图</span><input id="front" type="file" accept=".jpg,.png"></div>
    <div class="el-upload"><span>背面图</span><input id="back" type="file" accept=".jpg,.png"></div>
    <div class="el-upload"><span>模特图</span><input id="model" type="file" accept=".jpg,.png"></div>
    <div class="el-upload"><span>上传图片</span><input id="generic" type="file" accept=".jpg,.png"></div>
  </div>
</div>
<script>
window.uploadEvents = [];
const wrapper = document.querySelector('#carousel');
wrapper.__vue__ = {fileList: [{url: '__FIRST_URL__'}]};
for (const input of document.querySelectorAll('input[type=file]')) {
  input.onchange = () => {
    const file = input.files[0];
    uploadEvents.push({slot: input.id, filename: file.name});
    const reader = new FileReader();
    reader.onload = () => {
      const item = document.createElement('li'); item.className = 'is-uploading';
      const image = document.createElement('img'); image.className = 'el-upload-list__item-thumbnail';
      image.src = reader.result; item.append(image);
      document.querySelector('.el-upload-list').append(item);
      setTimeout(() => {
        wrapper.__vue__.fileList.push({url: 'https://cdn.poizon.com/test/' + file.name});
        item.className = 'is-success';
      }, 35);
    };
    reader.readAsDataURL(file);
  };
}
</script>
"""


def one_pixel_png(red, green, blue):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes([0, red, green, blue, 255])))
            + chunk(b'IEND', b''))


class DewuDomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel='chrome', headless=True)
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(1500)
        self.product = SimpleNamespace(colors=('卡其色',), sizes=('S', 'M'),
                                       sku_code='7076卡其色', bid_price='865', stock='50',
                                       bid_type='直发预售', listed='上架', white_images=(),
                                       brand='NEIGBORL', category=('服装', '长裤', '工装裤'),
                                       style_code='NGBL-7076', excel_path=Path(self.directory.name)/'product.xlsx')
        self.listing = DewuListing(self.page, self.product, Path(self.directory.name), timeout=1)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.directory.cleanup()

    async def prepare_run_boundary(self):
        """Use real DOM save buttons while stubbing independently tested steps."""
        await self.page.set_content(RUN_HTML)
        self.product.brand = 'NEIGBORL'
        self.product.category = ('服装', '长裤', '工装裤')
        for name in ('basic', 'colors', 'verify_carousel', 'process_white_backgrounds', 'size_chart',
                     'recommendations', 'sku_batch', 'detail_images'):
            setattr(self.listing, name, AsyncMock())

    async def test_manual_login_dashboard_redirect_saves_auth_before_creation(self):
        await self.prepare_run_boundary()
        events = []
        start_visits = 0

        async def route_page(route):
            nonlocal start_visits
            url = route.request.url
            if url == DEWU_START_URL:
                start_visits += 1
                events.append('start')
                if start_visits == 1:
                    await route.fulfill(content_type='text/html; charset=utf-8', body='''
                        <script>location.replace('https://stark.dewu.com/main')</script>''')
                else:
                    await route.fulfill(content_type='text/html; charset=utf-8', body=RUN_HTML + '''
                        <div class="el-form-item">
                          <label class="el-form-item__label">商品品牌</label><input>
                        </div>''')
            elif url == 'https://stark.dewu.com/main':
                events.append('dashboard')
                await route.fulfill(content_type='text/html; charset=utf-8', body='<main>已登录工作台</main>')
            else:
                await route.abort()

        async def authenticated():
            self.assertEqual(self.page.url, DEWU_START_URL)
            self.listing.create.assert_not_awaited()
            events.append('authenticated')

        async def create():
            events.append('create')

        await self.page.route('**/*', route_page)
        self.listing.login_timeout = 3
        self.listing.on_authenticated = authenticated
        self.listing.create = AsyncMock(side_effect=create)
        await self.listing.run(save=False)

        self.assertEqual(events, ['start', 'dashboard', 'start', 'authenticated', 'create'])
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 0])
        self.assertEqual(self.listing.report['status'], 'filled')

    async def test_save_only_clicks_draft_and_never_review(self):
        await self.prepare_run_boundary()
        with patch.object(self.page, 'goto', new=AsyncMock()) as navigate:
            await self.listing.run(save=True, resume_url=DRAFT_URL)
        navigate.assert_awaited_once_with(DRAFT_URL, wait_until='domcontentloaded')
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [1, 0])
        self.assertEqual(self.listing.report['status'], 'draft_saved')
        self.assertFalse(self.listing.report['submit_review'])

    async def test_platform_prefilled_style_code_is_updated_from_excel_on_resume(self):
        await self.prepare_run_boundary()
        await self.listing.item('货号').locator('input').fill('1076946914524')
        self.listing.create = AsyncMock()
        async def basic():
            await self.listing.fill_field('货号', self.product.style_code)
        self.listing.basic.side_effect = basic
        with patch.object(self.page, 'goto', new=AsyncMock()):
            await self.listing.run(save=True, resume_url=DRAFT_URL)
        self.listing.create.assert_not_awaited()
        self.listing.basic.assert_awaited_once()
        self.assertEqual(await self.listing.item('货号').locator('input').input_value(), self.product.style_code)
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [1, 0])

    async def test_partial_draft_with_empty_style_code_can_resume(self):
        await self.prepare_run_boundary()
        await self.listing.item('货号').locator('input').fill('')
        with patch.object(self.page, 'goto', new=AsyncMock()):
            await self.listing.run(save=False, resume_url=DRAFT_URL)
        self.listing.basic.assert_awaited_once()
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 0])

    async def test_draft_business_rejection_is_reported_without_waiting_for_timeout(self):
        await self.prepare_run_boundary()
        endpoint = '/api/v1/h5/biz/nps/saveOrUpdateDraft'
        html = RUN_HTML.replace('onclick="saveClicks++;',
                                'onclick="saveClicks++;fetch(\'' + endpoint + '\',{method:\'POST\'});')
        async def route_page(route):
            if route.request.url.endswith(endpoint):
                await route.fulfill(content_type='application/json', body=json.dumps(
                    {'code': 20833072, 'msg': '请勿重复保存，前往申请新品列表查看最新申请状态。'}))
            else:
                await route.fulfill(content_type='text/html; charset=utf-8', body=html.replace(
                    "toast.className='el-message--success'", "toast.className='pending'"))
        await self.page.route('https://stark.dewu.com/**', route_page)
        with self.assertRaisesRegex(DewuError, '请勿重复保存'):
            await self.listing.run(resume_url=DRAFT_URL)
        self.assertEqual(self.listing.report['save_receipt']['code'], 20833072)
        self.assertEqual(self.listing.report['status'], 'failed')

    async def test_draft_result_page_requires_business_success_receipt(self):
        await self.prepare_run_boundary()
        endpoint = '/api/v1/h5/biz/nps/saveOrUpdateDraft'
        html = RUN_HTML + '''<script>document.querySelector('button').onclick=async()=>{
            saveClicks++;await fetch('__ENDPOINT__',{method:'POST'});
            location.href='https://stark.dewu.com/main/newProductApply/spuEdit/result?preType=2';
        };</script>'''.replace('__ENDPOINT__', endpoint)
        async def route_page(route):
            if route.request.url.endswith(endpoint):
                await route.fulfill(content_type='application/json', body=json.dumps({'code': 200}))
            else:
                await route.fulfill(content_type='text/html; charset=utf-8', body=html)
        await self.page.route('https://stark.dewu.com/**', route_page)
        await self.listing.run(resume_url=DRAFT_URL)
        self.assertEqual(self.listing.report['status'], 'draft_saved')
        self.assertEqual(self.listing.report['save_receipt']['code'], 200)

    async def prepare_review_run(self, response, *, precheck=False, http_status=200):
        await self.prepare_run_boundary()
        self.listing.upload_timeout = 1
        action = "reviewClicks++;fetch('" + REVIEW_ENDPOINT + "',{method:'POST'})"
        if precheck:
            script = '''<script>
                document.querySelector('button:last-child').onclick = () => {
                    reviewClicks++;
                    const modal=document.createElement('div');modal.className='ant-modal-content';
                    const button=document.createElement('button');button.textContent='继续提交';
                    button.onclick=()=>{modal.remove();fetch('__ENDPOINT__',{method:'POST'});};
                    modal.append(button);document.body.append(modal);
                };
            </script>'''.replace('__ENDPOINT__', REVIEW_ENDPOINT)
        else:
            script = ''
        html = RUN_HTML.replace('onclick="reviewClicks++"', 'onclick="' + action + '"') + script
        async def route_page(route):
            if route.request.url.endswith(REVIEW_ENDPOINT):
                await route.fulfill(status=http_status, content_type='application/json', body=json.dumps(response))
            else:
                await route.fulfill(content_type='text/html; charset=utf-8', body=html)
        await self.page.route('https://stark.dewu.com/**', route_page)

    async def test_review_mode_requires_successful_review_endpoint_receipt(self):
        await self.prepare_review_run({'code': 200, 'data': {'newApplyId': 98765}})
        await self.listing.run(resume_url=DRAFT_URL, submit_review=True)
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 1])
        self.assertEqual(self.listing.report['status'], 'submitted_for_review')
        self.assertTrue(self.listing.report['submit_review'])
        self.assertTrue(self.listing.report['review_submission_attempted'])
        self.assertEqual(self.listing.report['review_receipt']['application_id'], '98765')

    async def test_review_mode_handles_pre_audit_confirmation(self):
        await self.prepare_review_run({'code': 200}, precheck=True)
        await self.listing.run(resume_url=DRAFT_URL, submit_review=True)
        self.assertEqual(self.listing.report['status'], 'submitted_for_review')
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 1])

    async def test_review_business_rejection_is_not_success(self):
        await self.prepare_review_run({'code': 400, 'msg': '货号已存在'})
        with self.assertRaisesRegex(DewuError, '货号已存在'):
            await self.listing.run(resume_url=DRAFT_URL, submit_review=True)
        self.assertEqual(self.listing.report['status'], 'failed')
        self.assertFalse(self.listing.report['submit_review'])
        self.assertTrue(self.listing.report['review_submission_rejected'])

    async def test_review_http_failure_keeps_submission_uncertain(self):
        await self.prepare_review_run({'code': 500}, http_status=500)
        with self.assertRaisesRegex(DewuError, '提交得物审核失败'):
            await self.listing.run(resume_url=DRAFT_URL, submit_review=True)
        self.assertTrue(self.listing.report['review_submission_attempted'])
        self.assertFalse(self.listing.report['review_submission_rejected'])
        self.assertFalse(self.listing.report['submit_review'])

    async def test_invalid_review_response_is_not_treated_as_rejected_or_successful(self):
        await self.prepare_review_run([])
        with self.assertRaisesRegex(DewuError, '提交得物审核失败'):
            await self.listing.run(resume_url=DRAFT_URL, submit_review=True)
        self.assertTrue(self.listing.report['review_submission_attempted'])
        self.assertFalse(self.listing.report['review_submission_rejected'])
        self.assertFalse(self.listing.report['submit_review'])

    async def test_review_never_accepts_save_receipt_or_generic_success_page(self):
        await self.prepare_run_boundary()
        self.listing.upload_timeout = 1
        async def route_page(route):
            await route.fulfill(content_type='text/html; charset=utf-8', body=RUN_HTML.replace(
                'onclick="reviewClicks++"', 'onclick="reviewClicks++;document.body.append(\'提交成功\')"'))
        await self.page.route('https://stark.dewu.com/**', route_page)
        with self.assertRaisesRegex(DewuError, '未取得得物审核提交成功回执'):
            await self.listing.run(resume_url=DRAFT_URL, submit_review=True)
        self.assertFalse(self.listing.report['submit_review'])

    async def test_save_reports_image_validation_instead_of_generic_timeout(self):
        await self.page.set_content('''<button onclick="document.querySelector('.item-card').style.display='block'">保存草稿</button>
          <div class="item-card" style="display:none"><div class="title">穿搭效果</div>
            <div>图片中有违禁词，请重新上传!</div></div>''')
        with self.assertRaisesRegex(DewuError, '穿搭效果：图片中有违禁词'):
            await self.listing.save_draft()
        self.assertNotIn('save_receipt', self.listing.report)
        self.assertEqual(self.listing.report['validation_errors'], ['穿搭效果：图片中有违禁词，请重新上传!'])

    async def test_image_validation_blocks_save_and_review(self):
        await self.prepare_run_boundary()
        await self.page.locator('#errors').evaluate('''e=>e.innerHTML='<div class="item-card"><div class="title">穿搭效果</div><div>图片中有违禁词，请重新上传!</div></div>' ''')
        with patch.object(self.page, 'goto', new=AsyncMock()), self.assertRaisesRegex(DewuError, '穿搭效果：图片中有违禁词'):
            await self.listing.run(resume_url=DRAFT_URL)
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 0])

    async def test_duplicate_style_blocks_review_without_clicking_save_or_submit(self):
        await self.prepare_run_boundary()
        await self.page.locator('#errors').evaluate(
            "e=>e.innerHTML='<div class=el-form-item__error>商品货号已存在,请检查后重试</div>'")
        self.listing.submit_for_review = AsyncMock()
        with patch.object(self.page, 'goto', new=AsyncMock()), self.assertRaisesRegex(DewuError, '货号已存在'):
            await self.listing.run(resume_url=DRAFT_URL, submit_review=True)
        self.listing.submit_for_review.assert_not_awaited()
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 0])

    async def test_saved_draft_is_not_skipped_when_review_is_requested(self):
        directory = Path(self.directory.name)
        state = directory / 'state.json'
        previous = {'fingerprint': 'same', 'draft_url': DRAFT_URL, 'status': 'draft_saved', 'steps': {'sku': [
            {'颜色': '卡其色', '尺码': s, '商品编码': '7076卡其色' + s} for s in self.product.sizes]}}
        state.write_text(json.dumps(previous))
        self.product.excel_path = directory / 'product.xlsx'
        args = SimpleNamespace(dewu_resume_url='', dewu_submit_review=True, cdp_url='http://fixture',
                               auth_state=str(directory / 'auth.json'), headless=True, timeout=1,
                               upload_timeout=1, login_timeout=1, save=True)
        context = SimpleNamespace(new_page=AsyncMock(return_value=self.page))
        engine = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(
            return_value=SimpleNamespace(contexts=[context]))))
        factory = Mock()
        factory.return_value.__aenter__ = AsyncMock(return_value=engine)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch('playwright.async_api.async_playwright', factory), \
                patch('dewu_listing.input_fingerprint', return_value='same'), \
                patch('dewu_listing.restore_dewu_auth_state', new=AsyncMock()), \
                patch.object(DewuListing, 'run', new=AsyncMock()) as run:
            await run_dewu_browser(args, self.product, directory, Mock(), state)
        self.assertTrue(run.await_args.kwargs['submit_review'])
        self.assertFalse(run.await_args.kwargs['sku_codes_only'])
        self.assertTrue(run.await_args.kwargs['white_images_only'])
        self.assertTrue(run.await_args.kwargs['maintenance'])
        self.assertEqual(run.await_args.kwargs['resume_url'], DRAFT_URL)

    async def test_submitted_or_uncertain_submission_never_repeats_request(self):
        directory = Path(self.directory.name)
        state = directory / 'state.json'
        args = SimpleNamespace(dewu_resume_url='', dewu_submit_review=True)
        previous = {'fingerprint': 'same', 'draft_url': DRAFT_URL, 'status': 'submitted_for_review'}
        state.write_text(json.dumps(previous))
        with patch('playwright.async_api.async_playwright') as browser, \
                patch('dewu_listing.input_fingerprint', return_value='same'):
            await run_dewu_browser(args, self.product, directory, Mock(), state)
            self.assertEqual(json.loads((directory / 'dewu-result.json').read_text())['status'], 'already_submitted')
            previous['fingerprint'] = 'before-excel-edit'
            state.write_text(json.dumps(previous))
            with self.assertRaisesRegex(DewuError, '已提交得物审核'):
                await run_dewu_browser(args, self.product, directory, Mock(), state)
            previous['fingerprint'] = 'same'
            previous.update(status='failed', review_submission_attempted=True)
            state.write_text(json.dumps(previous))
            with self.assertRaisesRegex(DewuError, '结果未确认'):
                await run_dewu_browser(args, self.product, directory, Mock(), state)
            browser.assert_not_called()

    async def test_changed_excel_opens_original_draft_and_keeps_upload_checkpoints(self):
        directory = Path(self.directory.name)
        state = directory / 'state.json'
        uploads = {'商品图片': [{'path':'white.png','url':'https://fixture.test/white.png','sha256':'hash'}]}
        previous = {'fingerprint':'old', 'draft_url':DRAFT_URL, 'status':'draft_saved',
                    'excel_path':str(self.product.excel_path), 'uploads':uploads,
                    'product_identity':product_identity(self.product)}
        state.write_text(json.dumps(previous))
        args = SimpleNamespace(dewu_resume_url='', dewu_submit_review=False, cdp_url='http://fixture',
                               auth_state=str(directory/'auth.json'), headless=True, timeout=1,
                               upload_timeout=1, login_timeout=1, save=True)
        context = SimpleNamespace(new_page=AsyncMock(return_value=self.page))
        engine = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(
            return_value=SimpleNamespace(contexts=[context]))))
        factory = Mock()
        factory.return_value.__aenter__ = AsyncMock(return_value=engine)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        captured = {}
        async def run(listing, **kwargs):
            captured.update(kwargs)
            self.assertEqual(listing.report['uploads'], uploads)
            listing.report['status'] = 'draft_saved'
            listing.persist()
        with patch('playwright.async_api.async_playwright', factory), \
                patch('dewu_listing.input_fingerprint', return_value='new'), \
                patch('dewu_listing.restore_dewu_auth_state', new=AsyncMock()), \
                patch.object(DewuListing, 'run', new=run):
            await run_dewu_browser(args, self.product, directory, Mock(), state)
        self.assertEqual(captured['resume_url'], DRAFT_URL)
        self.assertFalse(captured['maintenance'])
        self.assertFalse(captured['sku_codes_only'])
        self.assertFalse(captured['white_images_only'])
        report = json.loads(state.read_text())
        self.assertEqual(report['fingerprint'], 'new')
        self.assertEqual(report['product_identity'], product_identity(self.product))

    async def test_changed_product_identity_still_stops_before_opening_browser(self):
        directory = Path(self.directory.name)
        state = directory / 'state.json'
        previous = {'fingerprint':'old', 'draft_url':DRAFT_URL, 'status':'failed',
                    'product_identity':{**product_identity(self.product), 'style_code':'ANOTHER-PRODUCT'}}
        state.write_text(json.dumps(previous))
        with patch('playwright.async_api.async_playwright') as browser, \
                patch('dewu_listing.input_fingerprint', return_value='changed'):
            with self.assertRaisesRegex(DewuError, '原草稿记录不一致'):
                await run_dewu_browser(SimpleNamespace(dewu_resume_url=''), self.product, directory, Mock(), state)
            browser.assert_not_called()

    async def test_step_failure_never_clicks_save_or_review(self):
        await self.prepare_run_boundary()
        self.listing.basic.side_effect = DewuError('输入未通过验证')
        with patch.object(self.page, 'goto', new=AsyncMock()):
            with self.assertRaisesRegex(DewuError, '输入未通过验证'):
                await self.listing.run(save=True, resume_url=DRAFT_URL)
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 0])
        self.assertEqual(self.listing.report['status'], 'failed')

    async def test_form_validation_failure_never_clicks_save_or_review(self):
        await self.prepare_run_boundary()
        await self.page.locator('#errors').evaluate("e=>e.innerHTML='<div class=el-form-item__error>请输入货号</div>'")
        with patch.object(self.page, 'goto', new=AsyncMock()):
            with self.assertRaisesRegex(DewuError, '表单校验未通过'):
                await self.listing.run(save=True, resume_url=DRAFT_URL)
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 0])
        self.assertEqual(self.listing.report['status'], 'failed')

    async def test_duplicate_style_warning_is_retained_when_saving_draft(self):
        await self.prepare_run_boundary()
        warning = '商品货号已存在,请检查后重试,重复的货号: NGBL-7076'
        await self.page.locator('#errors').evaluate(
            '(e,text)=>{const d=document.createElement("div");d.className="el-form-item__error";d.textContent=text;e.append(d)}', warning)
        with patch.object(self.page, 'goto', new=AsyncMock()):
            await self.listing.run(save=True, resume_url=DRAFT_URL)
        self.assertEqual(self.listing.report['validation_errors'], [warning])
        self.assertEqual(self.listing.report['status'], 'draft_saved')
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [1, 0])

    async def test_spinbutton_accepts_decimal_format_without_loosening_text_fields(self):
        await self.page.set_content('''<input id="ratio" role="spinbutton"
          onblur="this.value=Number(this.value).toFixed(2)">
          <input id="code" onblur="this.value=Number(this.value).toFixed(2)">''')
        await self.listing.set_input(self.page.locator('#ratio'), '100', '成分比例')
        self.assertEqual(await self.page.locator('#ratio').input_value(), '100.00')
        with self.assertRaisesRegex(DewuError, '回读不一致'):
            await self.listing.set_input(self.page.locator('#code'), '100', '编码')

    async def test_save_accepts_result_redirect_only_with_successful_draft_response(self):
        async def route_page(route):
            if route.request.url.endswith('/saveOrUpdateDraft'):
                await route.fulfill(status=200, content_type='application/json', body='{"code":200}')
            elif '/result?' in route.request.url:
                await route.fulfill(content_type='text/html; charset=utf-8', body='<h1>提交成功</h1>')
            else:
                await route.fulfill(content_type='text/html; charset=utf-8', body='''
                    <button onclick="fetch('/api/v1/h5/biz/nps/saveOrUpdateDraft',{method:'POST'}).then(r=>r.json()).then(()=>setTimeout(()=>location.href='/main/newProductApply/spuEdit/result?preType=2',50))">保存草稿</button>''')
        await self.page.route('**/*', route_page)
        await self.page.goto(DRAFT_URL)
        await self.listing.save_draft()
        self.assertEqual(self.listing.report['save_receipt']['http_status'], 200)
        self.assertTrue(self.listing.report['save_receipt']['endpoint'].endswith('/saveOrUpdateDraft'))

    async def test_result_screen_without_draft_response_is_not_save_evidence(self):
        async def route_page(route):
            await route.fulfill(content_type='text/html; charset=utf-8', body='''
                <button onclick="location.href='/main/newProductApply/spuEdit/result?preType=2'">保存草稿</button>''')
        await self.page.route('**/*', route_page)
        await self.page.goto(DRAFT_URL)
        with self.assertRaisesRegex(DewuError, '未收到成功'):
            await self.listing.save_draft()

    async def test_preview_does_not_click_save_or_review(self):
        await self.prepare_run_boundary()
        with patch.object(self.page, 'goto', new=AsyncMock()):
            await self.listing.run(save=False, resume_url=DRAFT_URL)
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 0])
        self.assertEqual(self.listing.report['status'], 'filled')

    async def prepare_recorded_image(self):
        image = Path(self.directory.name) / '1.png'
        image.write_bytes(b'original image bytes')
        url = 'https://cdn.poizon.com/test/1.png'
        await self.page.set_content(f'<div id="upload"><ul class="el-upload-list"><li class="is-success"><img src="{url}"></li></ul></div>')
        self.listing.report['uploads'] = {'商品展示': [
            {'path': str(image), 'url': url, 'sha256': image_fingerprint(image)}]}
        return image

    async def test_resume_keeps_verified_uploaded_image_without_appending(self):
        image = await self.prepare_recorded_image()
        await self.listing.upload(self.page.locator('#upload'), (image,), '商品展示')
        self.assertEqual(await self.page.locator('#upload li').count(), 1)

    async def test_image_urls_use_uploaded_identity_when_preview_is_blob(self):
        await self.page.set_content('''<div id="images">
          <div class="du-upload" id="first"><ul class="el-upload-list">
            <li><img src="blob:https://stark.dewu.com/preview"></li>
          </ul></div>
          <div class="du-upload" id="second"><ul class="el-upload-list">
            <li><img src="https://cdn.poizon.com/fallback.png?size=100"></li>
          </ul></div></div>''')
        await self.page.locator('#first').evaluate('''e => {
            e.__vue__ = {fileList: [{url: 'https://cdn.poizon.com/original.png?size=100'}]};
        }''')
        self.assertEqual(await self.listing.image_urls(self.page.locator('#images')),
                         ['https://cdn.poizon.com/original.png', 'https://cdn.poizon.com/fallback.png'])

    async def test_upload_waits_for_remote_identity_after_preview_completes(self):
        paths, records = await self.prepare_carousel_uploads()
        await self.page.locator('#carousel .el-upload-list').evaluate('e=>e.innerHTML=""')
        await self.page.locator('#carousel').evaluate('''e => {
            e.__vue__.fileList = [];
            document.querySelector('#generic').onchange = event => {
                const file = event.target.files[0];
                const item = document.createElement('li'); item.className = 'is-success';
                const image = document.createElement('img'); image.src = URL.createObjectURL(file);
                item.append(image); e.querySelector('ul').append(item);
                setTimeout(() => e.__vue__.fileList.push({url: 'https://cdn.poizon.com/test/' + file.name}), 200);
            };
        }''')
        await self.listing.upload(self.page.locator('#carousel'), paths[:1], '商品展示')
        self.assertEqual(self.listing.report['uploads']['商品展示'], records[:1])

    async def test_resume_rejects_changed_bytes_at_the_same_image_path(self):
        image = await self.prepare_recorded_image()
        image.write_bytes(b'changed image bytes')
        with self.assertRaisesRegex(DewuError, '未匹配'):
            await self.listing.upload(self.page.locator('#upload'), (image,), '商品展示')
        self.assertEqual(await self.page.locator('#upload li').count(), 1)

    async def prepare_carousel_uploads(self):
        paths, records = [], []
        for number, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255)), 1):
            path = Path(self.directory.name) / f'{number}.png'
            content = one_pixel_png(*color)
            path.write_bytes(content)
            paths.append(path)
            records.append({'path': str(path), 'sha256': image_fingerprint(path),
                            'url': 'https://cdn.poizon.com/test/' + path.name})
        await self.page.set_content(CAROUSEL_HTML.replace('__FIRST_URL__', records[0]['url']))
        self.product.initial_images = tuple(paths)
        self.product.white_images = tuple(paths[:2])
        self.listing.upload_timeout = 3
        return paths, records

    async def test_carousel_appends_after_verified_prefix_in_original_file_order(self):
        paths, records = await self.prepare_carousel_uploads()
        self.listing.report['uploads'] = {'商品图片': records}
        await self.listing.verify_carousel()
        self.assertEqual(await self.listing.image_urls(self.page.locator('#carousel')),
                         [record['url'] for record in records])
        self.assertEqual(await self.page.evaluate('uploadEvents'),
                         [{'slot': 'generic', 'filename': path.name} for path in paths[1:]])
        self.assertEqual(self.listing.report['uploads']['商品轮播图-1'], records)
        self.assertEqual(self.listing.report['steps']['carousel']['count'], 3)

    async def prepare_white_editor(self):
        paths, records = await self.prepare_carousel_uploads()
        self.listing.report['uploads'] = {'商品图片': records}
        await self.listing.verify_carousel()
        async def image(route):
            await route.fulfill(content_type='image/png', body=one_pixel_png(255, 255, 255))
        await self.page.route('https://cdn.poizon.com/test/generated-*.png', image)
        await self.page.evaluate('''() => {
            window.generationCounts = [0, 0]; window.appliedIndices = []; window.failureMode = '';
            const drawer = document.createElement('div'); drawer.className = 'ant-drawer-content';
            drawer.style.display = 'none';
            drawer.innerHTML = '<h2>图片编辑</h2><button id="generate">抠图（1:1白底图）</button>'
              + '<div class="cropper-canvas"><img></div><button id="original">应用原图</button>'
              + '<button id="apply" disabled>应用生成图</button>';
            document.body.append(drawer);
            let active = -1;
            document.querySelectorAll('#carousel li').forEach((li, index) => {
                const wrapper = document.createElement('div'); wrapper.className = 'w88_img_wrap';
                wrapper.style.cssText = 'width:88px;height:88px;background:#eee';
                wrapper.append(li.querySelector('img'));
                const edit = document.createElement('span'); edit.innerHTML = '<i class="el-icon-edit">编辑</i>';
                edit.style.display = 'none'; wrapper.append(edit); li.append(wrapper);
                wrapper.onmouseover = () => edit.style.display = 'inline';
                edit.onclick = () => {
                    active = index; drawer.style.display = 'block';
                    drawer.querySelector('img').removeAttribute('src');
                    drawer.querySelector('#apply').disabled = true;
                };
            });
            drawer.querySelector('#generate').onclick = () => {
                generationCounts[active]++;
                if (failureMode === 'generation-' + active) return;
                setTimeout(() => {
                    drawer.querySelector('img').src = 'https://cdn.poizon.com/test/generated-' + active + '.png';
                    drawer.querySelector('#apply').disabled = false;
                }, 60);
            };
            drawer.querySelector('#original').onclick = () => { throw new Error('Must use generated image'); };
            drawer.querySelector('#apply').onclick = () => {
                appliedIndices.push(active);
                if (failureMode !== 'apply') {
                    const url = drawer.querySelector('img').src;
                    document.querySelector('#carousel').__vue__.fileList[active].url = url;
                    document.querySelectorAll('#carousel li img')[active].src = url;
                }
                if (failureMode === 'reorder') {
                    document.querySelector('#carousel').__vue__.fileList[2].url = 'https://cdn.poizon.com/wrong.png';
                }
                drawer.style.display = 'none';
            };
        }''')
        return paths, records

    async def test_white_images_generate_then_apply_in_order_and_repeat_without_processing(self):
        paths, originals = await self.prepare_white_editor()
        await self.listing.process_white_backgrounds()
        self.assertEqual(await self.page.evaluate('[generationCounts, appliedIndices]'), [[1, 1], [0, 1]])
        self.assertTrue(white_backgrounds_complete(self.product, self.listing.report))
        self.assertEqual(self.listing.report['uploads']['商品图片'], originals)
        self.assertEqual((await self.listing.image_urls(self.page.locator('#carousel')))[2], originals[2]['url'])
        self.assertEqual([r['source_url'] for r in self.listing.report['steps']['white_backgrounds']],
                         [r['url'] for r in originals[:2]])
        await self.listing.verify_carousel()
        await self.listing.process_white_backgrounds()
        self.assertEqual(await self.page.evaluate('generationCounts'), [1, 1])
        paths[0].write_bytes(one_pixel_png(1, 1, 1))
        self.assertFalse(white_backgrounds_complete(self.product, self.listing.report))

    async def test_saved_complete_white_and_sku_receipts_skip_browser_on_repeat(self):
        await self.prepare_white_editor()
        await self.listing.process_white_backgrounds()
        self.listing.report.update(fingerprint='same', draft_url=DRAFT_URL, status='draft_saved')
        self.listing.report['steps']['sku'] = [
            {'颜色': '卡其色', '尺码': s, '商品编码': '7076卡其色' + s} for s in self.product.sizes]
        directory = Path(self.directory.name)
        state = directory / 'state.json'
        state.write_text(json.dumps(self.listing.report))
        args = SimpleNamespace(dewu_resume_url='', dewu_submit_review=False)
        with patch('playwright.async_api.async_playwright') as browser, \
                patch('dewu_listing.input_fingerprint', return_value='same'):
            await run_dewu_browser(args, self.product, directory, Mock(), state)
        browser.assert_not_called()
        self.assertEqual(json.loads((directory / 'dewu-result.json').read_text())['status'], 'already_saved')

    async def test_only_first_two_white_images_are_processed(self):
        paths, originals = await self.prepare_white_editor()
        self.product.white_images = tuple(paths)
        await self.listing.process_white_backgrounds()
        self.assertEqual(await self.page.evaluate('appliedIndices'), [0, 1])
        self.assertEqual((await self.listing.image_urls(self.page.locator('#carousel')))[2], originals[2]['url'])

    async def test_second_white_failure_resumes_without_reprocessing_first(self):
        await self.prepare_white_editor()
        await self.page.evaluate("failureMode='generation-1'")
        self.listing.upload_timeout = .35
        with self.assertRaisesRegex(DewuError, '生成未完成'):
            await self.listing.process_white_backgrounds()
        checkpoint = json.loads((Path(self.directory.name) / 'dewu-result.json').read_text())
        self.assertEqual(len(checkpoint['steps']['white_backgrounds']), 1)
        self.assertEqual(checkpoint['steps']['white_backgrounds'][0]['status'], 'applied')
        self.assertFalse(white_backgrounds_complete(self.product, checkpoint))
        await self.page.evaluate("failureMode='';document.querySelector('.ant-drawer-content').style.display='none'")
        self.listing.upload_timeout = 3
        await self.listing.verify_carousel()
        await self.listing.process_white_backgrounds()
        self.assertEqual(await self.page.evaluate('generationCounts'), [1, 2])
        self.assertTrue(white_backgrounds_complete(self.product, self.listing.report))

    async def test_unsaved_white_checkpoint_recovers_exact_original_urls(self):
        _, originals = await self.prepare_white_editor()
        await self.listing.process_white_backgrounds()
        # Reopened server draft still contains the originals from its last save.
        await self.page.locator('#carousel').evaluate('(e,urls)=>e.__vue__.fileList=urls.map(url=>({url}))',
                                                       [r['url'] for r in originals])
        await self.listing.verify_carousel()
        self.assertEqual(self.listing.report['steps']['white_backgrounds'], [])
        self.assertEqual(self.listing.report['uploads']['商品轮播图-1'], originals)
        await self.listing.process_white_backgrounds()
        self.assertEqual(await self.page.evaluate('generationCounts'), [2, 2])

    async def test_generated_checkpoint_recovers_already_applied_result(self):
        await self.prepare_white_editor()
        await self.listing.process_white_backgrounds()
        receipt = self.listing.report['steps']['white_backgrounds'][0]
        receipt['status'] = 'generated'
        self.listing.report['uploads']['商品轮播图-1'][0]['url'] = receipt['source_url']
        await self.listing.verify_carousel()
        await self.listing.process_white_backgrounds()
        self.assertEqual(await self.page.evaluate('generationCounts'), [1, 1])
        self.assertTrue(white_backgrounds_complete(self.product, self.listing.report))

    async def test_generated_image_not_replaced_is_not_complete(self):
        await self.prepare_white_editor()
        await self.page.evaluate("failureMode='apply'")
        with self.assertRaisesRegex(DewuError, '未应用成功'):
            await self.listing.process_white_backgrounds()
        self.assertEqual(self.listing.report['steps']['white_backgrounds'][0]['status'], 'generated')
        self.assertFalse(white_backgrounds_complete(self.product, self.listing.report))

    async def test_white_editor_rejects_changes_to_other_images(self):
        await self.prepare_white_editor()
        await self.page.evaluate("failureMode='reorder'")
        with self.assertRaisesRegex(DewuError, '其他轮播图或顺序'):
            await self.listing.process_white_backgrounds()

    async def test_white_resume_rejects_unrecognized_manual_replacement(self):
        await self.prepare_white_editor()
        await self.listing.process_white_backgrounds()
        await self.page.locator('#carousel').evaluate("e=>e.__vue__.fileList[0].url='https://cdn.poizon.com/unknown.png'")
        with self.assertRaisesRegex(DewuError, '均不匹配'):
            await self.listing.verify_carousel()

    async def test_multiple_upload_slots_use_only_the_generic_slot(self):
        paths, records = await self.prepare_carousel_uploads()
        await self.page.locator('#carousel .el-upload-list').evaluate('e=>e.innerHTML=""')
        await self.page.locator('#carousel').evaluate('e=>e.__vue__.fileList=[]')
        await self.listing.upload(self.page.locator('#carousel'), paths, '商品展示')
        self.assertEqual(await self.page.evaluate('uploadEvents'),
                         [{'slot': 'generic', 'filename': path.name} for path in paths])
        self.assertEqual(await self.page.locator('#front, #back, #model').evaluate_all('es=>es.map(e=>e.files.length)'), [0, 0, 0])
        self.assertEqual(self.listing.report['uploads']['商品展示'], records)

    async def test_multiselect_removes_all_collapsed_ai_values(self):
        await self.page.set_content(MULTISELECT_HTML)
        await self.listing.select(self.page.locator('#field'), '棉/羊毛', '面料')
        self.assertEqual(await self.page.evaluate('selected'), ['棉', '羊毛'])

    async def test_multiselect_rejects_unrequested_value_even_when_desired_tag_is_present(self):
        await self.page.set_content(MULTISELECT_HTML)
        await self.page.evaluate('window.injectExtra = true')
        with self.assertRaises(DewuError):
            await self.listing.select(self.page.locator('#field'), '棉', '面料')

    async def test_color_typing_removes_new_empty_row(self):
        await self.page.set_content(COLOR_HTML)
        await self.listing.colors()
        self.assertEqual(await self.page.locator('input[placeholder="请选择或输入标准色"]').evaluate_all('es=>es.map(e=>e.value)'), ['卡其色'])

    async def test_color_never_deletes_populated_extra_row(self):
        await self.page.set_content(COLOR_HTML)
        await self.page.evaluate("addColor('黑色')")
        with self.assertRaises(DewuError):
            await self.listing.colors()
        self.assertEqual(await self.page.locator('input[placeholder="请选择或输入标准色"]').nth(1).input_value(), '黑色')

    async def test_sku_batch_reads_each_column_and_ignores_fixed_table_duplicates(self):
        await self.page.set_content(SKU_HTML)
        await self.listing.sku_batch()
        self.assertEqual(len(self.listing.report['steps']['sku']), 2)
        self.assertEqual([row['商品编码'] for row in self.listing.report['steps']['sku']],
                         ['7076卡其色S', '7076卡其色M'])

    async def test_sku_codes_follow_row_size_across_pages_and_repeated_runs(self):
        await self.page.set_content(SKU_HTML)
        self.product.sizes = ('S', 'M', 'L', 'XL', '2XL', '3XL')
        await self.page.evaluate("skuSizes=['3XL','S','2XL','XL','M','L'];pageSize=2;renderSku(false)")
        await self.listing.sku_batch()
        first = self.listing.report['steps']['sku']
        self.assertEqual({row['尺码']: row['商品编码'] for row in first},
                         {size: '7076卡其色' + size for size in self.product.sizes})
        self.assertEqual(await self.page.evaluate('currentPage'), 3)
        await self.listing.update_sku_codes()
        self.assertEqual(self.listing.report['steps']['sku'], first)
        self.assertEqual(await self.page.evaluate('batchClicks'), 1)

    async def test_code_only_update_preserves_prices_stocks_and_auxiliary_codes(self):
        await self.page.set_content(SKU_HTML)
        await self.listing.sku_batch()
        await self.page.locator('#skuRows .el-table_1_column_7 input').first.fill('999.00')
        await self.page.locator('#skuRows .el-table_1_column_9 input').last.fill('8')
        await self.page.locator('#skuRows .el-table_1_column_5 input').first.fill('辅助编码')
        before = await self.listing.sku_snapshot(await self.listing.sku_table())
        await self.listing.update_sku_codes()
        self.assertEqual(self.listing.report['steps']['sku'], before)
        self.assertEqual(await self.page.evaluate('batchClicks'), 1)

    async def test_code_only_update_matches_both_color_and_size(self):
        await self.page.set_content(SKU_HTML)
        self.product.colors = ('卡其色', '黑色')
        await self.page.evaluate("skuColors=['黑色','卡其色'];renderSku(false)")
        await self.listing.update_sku_codes()
        self.assertTrue(sku_codes_match(self.product, self.listing.report['steps']['sku']))

    async def test_sku_code_readback_rejects_platform_reverting_input(self):
        await self.page.set_content(SKU_HTML)
        await self.page.evaluate("failureMode='code'")
        with self.assertRaisesRegex(DewuError, '商品编码填写后回读不一致'):
            await self.listing.sku_batch()

    async def test_code_update_allows_estimated_income_to_refresh(self):
        await self.page.set_content(SKU_HTML)
        await self.page.evaluate('''() => document.addEventListener('input',e=>{
            if(e.target.closest('.el-table_1_column_4')){
                document.querySelectorAll('#skuRows .el-table_1_column_8 .cell').forEach(c=>c.textContent='546.39');
            }
        })''')
        await self.listing.update_sku_codes()
        self.assertTrue(sku_codes_match(self.product, self.listing.report['steps']['sku']))
        self.assertEqual({r['预计收入'] for r in self.listing.report['steps']['sku']}, {'546.39'})

    async def test_code_update_still_rejects_price_changes_during_income_refresh(self):
        await self.page.set_content(SKU_HTML)
        await self.page.evaluate('''() => document.addEventListener('input',e=>{
            if(e.target.closest('.el-table_1_column_4')){
                document.querySelectorAll('#skuRows .el-table_1_column_8 .cell').forEach(c=>c.textContent='546.39');
                document.querySelector('#skuRows .el-table_1_column_7 input').value='999';
            }
        })''')
        with self.assertRaisesRegex(DewuError, '其他字段发生变化'):
            await self.listing.update_sku_codes()
        self.assertNotIn('sku', self.listing.report['steps'])

    async def test_sku_code_update_rejects_missing_size(self):
        await self.page.set_content(SKU_HTML)
        await self.page.evaluate("skuSizes=['S'];renderSku(false)")
        with self.assertRaisesRegex(DewuError, '组合不全'):
            await self.listing.update_sku_codes()
        self.assertNotIn('sku', self.listing.report['steps'])

    async def test_code_only_run_skips_other_steps_and_only_saves_existing_draft(self):
        await self.prepare_run_boundary()
        self.listing.update_sku_codes = AsyncMock()
        with patch.object(self.page, 'goto', new=AsyncMock()):
            await self.listing.run(save=True, resume_url=DRAFT_URL, sku_codes_only=True)
        self.listing.update_sku_codes.assert_awaited_once_with()
        for name in ('basic', 'colors', 'verify_carousel', 'process_white_backgrounds', 'size_chart',
                     'recommendations', 'sku_batch', 'detail_images'):
            getattr(self.listing, name).assert_not_awaited()
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [1, 0])

    async def test_white_only_maintenance_preserves_other_steps_and_saves_draft(self):
        await self.prepare_run_boundary()
        with patch.object(self.page, 'goto', new=AsyncMock()):
            await self.listing.run(resume_url=DRAFT_URL, white_images_only=True, maintenance=True)
        self.listing.verify_carousel.assert_awaited_once_with()
        self.listing.process_white_backgrounds.assert_awaited_once_with()
        for name in ('basic', 'colors', 'size_chart', 'recommendations', 'sku_batch', 'detail_images'):
            getattr(self.listing, name).assert_not_awaited()
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [1, 0])

    async def test_white_failure_blocks_saving_and_review(self):
        await self.prepare_run_boundary()
        self.listing.process_white_backgrounds.side_effect = DewuError('白底图生成失败')
        with patch.object(self.page, 'goto', new=AsyncMock()):
            with self.assertRaisesRegex(DewuError, '白底图生成失败'):
                await self.listing.run(resume_url=DRAFT_URL)
        self.assertEqual(await self.page.evaluate('[saveClicks, reviewClicks]'), [0, 0])

    async def test_code_only_run_without_existing_draft_cannot_create(self):
        self.listing.create = AsyncMock()
        with patch.object(self.page, 'goto', new=AsyncMock()) as navigate:
            with self.assertRaisesRegex(DewuError, '必须指定已有草稿'):
                await self.listing.run(sku_codes_only=True)
        navigate.assert_not_awaited()
        self.listing.create.assert_not_awaited()

    async def test_sku_batch_rejects_swapped_price_and_stock(self):
        await self.page.set_content(SKU_HTML)
        await self.page.evaluate("failureMode = 'swap'")
        with self.assertRaises(DewuError):
            await self.listing.sku_batch()

    async def test_sku_batch_rejects_unchanged_bid_type(self):
        await self.page.set_content(SKU_HTML)
        await self.page.evaluate("failureMode = 'type'")
        with self.assertRaises(DewuError):
            await self.listing.sku_batch()

    async def test_sku_batch_rejects_unchanged_listing_switch(self):
        await self.page.set_content(SKU_HTML)
        await self.page.evaluate("failureMode = 'status'")
        with self.assertRaises(DewuError):
            await self.listing.sku_batch()

    async def test_sku_batch_rejects_duplicate_size(self):
        await self.page.set_content(SKU_HTML)
        await self.page.evaluate("failureMode = 'size'")
        with self.assertRaises(DewuError):
            await self.listing.sku_batch()

    async def test_chinese_parameter_confirmation_preserves_numeric_columns(self):
        await self.page.set_content(CHART_HTML)
        rows = self.page.locator('#chart tbody tr')
        before = await rows.evaluate_all("rs => rs.map(r => Array.from(r.querySelectorAll('input')).map(i=>i.value))")
        await self.listing.normalize_chart_columns(self.page.locator('#chart'))
        after = await rows.evaluate_all("rs => rs.map(r => Array.from(r.querySelectorAll('input')).map(i=>i.value))")
        self.assertEqual(after, before)
        self.assertEqual(await self.page.evaluate('renameHistory'), ['腰围', '裤长', '裤脚围'])
        self.assertEqual(await self.page.locator('#chart thead th').all_text_contents(),
                         ['尺码', '腰围(cm)', '裤长(cm)', '裤脚围(cm)', '操作'])
        result = await self.listing.validate_chart(self.page.locator('#chart'))
        self.assertEqual(result, {'sizes': ['S', 'M'], 'measurements': {
            '腰围': ['78', '82'], '裤长': ['104', '105'], '裤脚围': ['38.5', '40']}})

    async def test_chart_rejects_measurements_moved_to_another_column(self):
        await self.page.set_content(CHART_HTML)
        await self.listing.normalize_chart_columns(self.page.locator('#chart'))
        expected = await self.listing.validate_chart(self.page.locator('#chart'))
        await self.page.locator('#chart tbody tr').evaluate_all('''rs=>rs.forEach(r=>{
            const inputs = r.querySelectorAll('input');
            [inputs[1].value, inputs[2].value] = [inputs[2].value, inputs[1].value];
        })''')
        with self.assertRaises(DewuError):
            await self.listing.validate_chart(self.page.locator('#chart'), expected=expected)

    async def test_cuff_uses_platform_canonical_name_and_preserves_measurements(self):
        await self.page.set_content(CHART_HTML.replace('脚围/FOOT OPENING(cm)', '袖口/CUFF(cm)'))
        expected = await self.listing.chart_snapshot(self.page.locator('#chart'))
        await self.listing.normalize_chart_columns(self.page.locator('#chart'))
        actual = await self.listing.validate_chart(self.page.locator('#chart'), expected=expected)
        self.assertEqual(await self.page.evaluate('renameHistory'), ['腰围', '裤长', '袖口围'])
        self.assertEqual(actual['measurements']['袖口围'], ['38.5', '40'])

    async def test_chart_rejects_nonfinite_measurements(self):
        await self.page.set_content(CHART_HTML)
        await self.listing.normalize_chart_columns(self.page.locator('#chart'))
        await self.page.locator('#chart tbody tr').first.locator('input').nth(1).fill('NaN')
        with self.assertRaises(DewuError):
            await self.listing.validate_chart(self.page.locator('#chart'))

    async def test_chart_rejects_missing_measurement(self):
        await self.page.set_content(CHART_HTML)
        await self.listing.normalize_chart_columns(self.page.locator('#chart'))
        await self.page.locator('#chart tbody tr').first.locator('input').nth(1).fill('')
        with self.assertRaises(DewuError):
            await self.listing.validate_chart(self.page.locator('#chart'))


if __name__ == '__main__':
    unittest.main()
