"""DOM automation for the native bilibili merchant product publisher."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, urlunsplit

from attribute_runtime import AttributeRequest, ReviewBatchRequired, _matched_excel_variants, split_multi_choice
from bilibili_api import BilibiliCapture, same_number, verify_sku_payload
from bilibili_data import BILIBILI_LIST_URL, BilibiliError, excel_value, match_color_images, read_bilibili_product
from category_profile import category_path_levels, category_search_terms
from field_policies import without_color_attributes
from learning_models import CandidateValue, canonical_sha256
from taobao_listing import normalize_label


PUBLISH_URL = 'https://shop.bilibili.com/#/g/goods/publish'


def exact(text):
    return re.compile(r'^\s*' + re.escape(text) + r'\s*$')


def media_identity(url):
    parsed = urlsplit('https:' + url if str(url).startswith('//') else str(url))
    # B 站 appends mtx and thumbnail transforms to previews, then persists the base asset.
    query = '' if (parsed.hostname or '').endswith('.hdslb.com') else parsed.query
    return urlunsplit(('https', parsed.netloc, parsed.path.split('@')[0], query, ''))


def valid_edit_url(url, *, draft_only=False):
    try:
        p = urlsplit(url)
        route, _, query = p.fragment.partition('?')
        values = parse_qs(query, keep_blank_values=True)
        keys = {'draftId'} if draft_only else {'draftId', 'itemsId'}
        return (p.scheme == 'https' and p.hostname == 'shop.bilibili.com'
                and p.port in (None, 443) and not p.username and not p.password
                and p.path == '/' and not p.query and route == '/g/goods/publish'
                and len(values) == 1 and set(values) <= keys
                and len(next(iter(values.values()))) == 1
                and bool(re.fullmatch(r'[1-9]\d*', next(iter(values.values()))[0])))
    except (ValueError, TypeError, AttributeError, StopIteration):
        return False


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def fingerprint(product):
    digest = hashlib.sha256()
    digest.update(json.dumps(product.summary(), ensure_ascii=False, sort_keys=True).encode())
    for path in (product.excel_path, *product.images, *product.detail_images, *product.sku_images):
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def resume_state(previous, product, explicit_url=''):
    if explicit_url and not valid_edit_url(explicit_url, draft_only=True):
        raise BilibiliError('恢复地址必须是 shop.bilibili.com 的商品草稿编辑地址')
    if previous.get('excel_path') and previous['excel_path'] != str(product.excel_path):
        raise BilibiliError('B 站草稿记录与当前 Excel 不一致')
    if previous.get('submission_attempted') and previous.get('status') != 'published':
        raise BilibiliError('上次 B 站发布结果未确认，请先核对商品列表，避免重复发布')
    if previous.get('save_attempted') and not previous.get('draft_url') and not explicit_url:
        raise BilibiliError('上次 B 站草稿保存结果未确认，请从草稿列表核对并用 --bilibili-resume-url 继续')
    url = explicit_url or previous.get('draft_url', '')
    if url and not valid_edit_url(url, draft_only=True):
        raise BilibiliError('已记录的 B 站草稿地址无效')
    return url


class BilibiliListing:
    def __init__(self, page, product, directory, logger=None, *, timeout=60,
                 upload_timeout=600, attribute_runtime=None):
        self.page, self.product, self.directory = page, product, Path(directory)
        self.logger = logger or logging.getLogger(__name__)
        self.timeout, self.upload_timeout = timeout, upload_timeout
        self.attribute_runtime = attribute_runtime
        self.capture = BilibiliCapture(page)
        self.form = page
        self.category_id = ''
        self.report = {'platform': 'bilibili', 'status': 'running', 'steps': {}, 'uploads': {}}
        self.on_checkpoint = None
        self.on_review = None

    def persist(self):
        write_json(self.directory / 'bilibili-result.json', self.report)
        if self.on_checkpoint:
            self.on_checkpoint(deepcopy(self.report))

    async def wait(self, predicate, message, *, timeout=None):
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            result = await predicate()
            if result:
                return result
            await asyncio.sleep(.2)
        raise BilibiliError(message)

    async def unique(self, locator, label):
        await locator.first.wait_for(state='visible', timeout=self.timeout * 1000)
        if await locator.count() != 1:
            raise BilibiliError(f'B 站{label}控件不唯一')
        return locator

    async def bind_form(self, *, timeout=None):
        # The outer merchant app hosts the goods app inside #mainIframe.
        async def ready():
            frames = [f for f in self.page.frames if urlsplit(f.url).hostname == 'shop.bilibili.com'
                      and (f == self.page.main_frame or urlsplit(f.url).path.startswith('/g/'))]
            matches = []
            from playwright.async_api import Error
            for frame in frames:
                try:
                    if await frame.locator('input[placeholder="至少输入6个字"], #name input').count():
                        matches.append(frame)
                except Error as exc:
                    if 'detached' not in str(exc).lower() and 'execution context' not in str(exc).lower():
                        raise
            if len(matches) == 1:
                self.form = matches[0]
                return True
            return False
        await self.wait(ready, 'B 站商品发布页未加载，请确认已登录且有发品权限', timeout=timeout)

    def item(self, label):
        return self.form.locator('.el-form-item').filter(has=self.form.locator(
            '.el-form-item__label').filter(has_text=exact(label)))

    async def set_input(self, locator, value, label, *, numeric=False):
        locator = await self.unique(locator, label)
        await locator.fill(str(value))
        await locator.press('Tab')
        actual = await locator.input_value()
        if not (same_number(actual, value) if numeric else actual == str(value)):
            raise BilibiliError(f'B 站{label}填写后回读不一致')
        return actual

    async def radio(self, scope, value, label):
        radio = scope.locator('label.el-radio').filter(
            has=self.form.locator('.el-radio__label').filter(has_text=exact(value)))
        radio = await self.unique(radio, label + ':' + value)
        if not await radio.locator('input[type=radio]').is_checked():
            await radio.click()
        if not await radio.locator('input[type=radio]').is_checked():
            raise BilibiliError(f'B 站{label}单选未生效')

    async def popup(self, select):
        async def owned():
            ids = await select.evaluate('''root => [...new Set([root,...root.querySelectorAll('[aria-controls],[aria-owns]')]
              .flatMap(e=>[e.getAttribute('aria-controls'),e.getAttribute('aria-owns')])
              .filter(Boolean).flatMap(v=>v.split(/\s+/)))].filter(id=>document.getElementById(id))''')
            matches = []
            for popup_id in ids:
                node = self.form.locator('[id=' + json.dumps(popup_id) + ']')
                if await node.is_visible():
                    matches.append(node)
            return matches[0] if len(matches) == 1 else False
        return await self.wait(owned, '未找到 B 站当前下拉框所属的候选列表')

    async def open_select(self, scope, *, search=None):
        select = await self.unique(scope.locator('.el-select'), '下拉框')
        expanded = select.locator('[aria-expanded=true]')
        if not await expanded.count():
            await select.click()
        if search is not None:
            inputs = select.locator('input:not([readonly])')
            if await inputs.count() == 1:
                await inputs.fill(str(search))
        popup = await self.popup(select)
        return select, popup

    async def select_exact(self, scope, value, label, *, search=False):
        select, popup = await self.open_select(scope, search=value if search else None)
        option = popup.locator('.el-select-dropdown__item:not(.is-disabled)').filter(has_text=exact(str(value)))
        await (await self.unique(option, label + ':' + str(value))).click()
        # Reopen to verify selected state; a typed search string is insufficient.
        await select.locator('input').first.press('Escape')
        await select.click()
        popup = await self.popup(select)
        chosen = popup.locator('.el-select-dropdown__item.is-selected,.el-select-dropdown__item.selected')
        await self.wait(lambda: chosen.count(), f'B 站{label}没有已提交的选中项')
        actual = [x.strip() for x in await chosen.all_text_contents()]
        await select.locator('input').first.press('Escape')
        if actual != [str(value)]:
            raise BilibiliError(f'B 站{label}选择后回读不一致：{actual}')
        return str(value)

    async def select_category(self):
        title = self.form.locator('#name input')
        if not await title.count():
            title = self.form.get_by_placeholder('至少输入6个字', exact=True)
        await self.set_input(title, self.product.title, '商品标题')
        dialog = self.form.get_by_role('dialog', name='选择类目').filter(visible=True)
        if await dialog.count() == 1:
            pass
        elif not await self.form.locator('#brand').count():
            button = self.form.get_by_role('button', name=re.compile('更多类目|手动选择'))
            await (await self.unique(button, '类目选择入口')).click()
            dialog = await self.unique(self.form.get_by_role('dialog', name='选择类目'), '类目弹窗')
        else:
            await self.form.locator('#cateId').get_by_text('修改', exact=True).click()
            dialog = await self.unique(self.form.get_by_role('dialog', name='选择类目'), '类目弹窗')
        await self.wait(lambda: self._captured_categories(), '未捕获 B 站完整类目接口')
        candidates = self.capture.categories
        hints = tuple(self.product.category_hints)
        matching = []
        for hint in hints:
            path_key = lambda text: normalize_label(re.sub(r'\s*(?:>|＞|/|／)\s*', '>', text))
            authored = [c for c in candidates if path_key('/'.join(c['path'])) == path_key(hint)]
            if authored:
                matching = authored
                break
            levels = category_path_levels(hint)
            exact_paths = [c for c in candidates if len(c['path']) == len(levels)
                           and all(normalize_label(part) in {normalize_label(v) for v in level}
                                   for part, level in zip(c['path'], levels))]
            if exact_paths:
                matching = exact_paths
                break
        if not matching:
            for hint in category_search_terms(hints):
                matching = [c for c in candidates if normalize_label(c['path'][-1]) == normalize_label(hint)]
                if matching:
                    break
        if not matching:
            matching = list(candidates)
        values = tuple(CandidateValue(c['id'], c['label']) for c in matching)
        if len(matching) == 1:
            chosen = matching[0]
        elif self.attribute_runtime is not None:
            resolved = await self.attribute_runtime.resolve(AttributeRequest(
                'bilibili', 'category-selection', 'category', '商品类目', values,
                ' / '.join(hints), {'excel': True, 'live_api': True, 'force_review': True},
                False, canonical_sha256([c['id'] for c in matching]), control_type='category'))
            if resolved is None:
                return False
            if resolved.is_no_fill:
                raise BilibiliError('商品类目不能不填写')
            chosen = next(c for c in matching if c['id'] == resolved.value_id)
        else:
            raise BilibiliError('B 站类目不是唯一精确匹配，请开启学习与审核')
        search_scope = dialog.locator('.el-select').locator('xpath=..')
        select, popup = await self.open_select(search_scope, search=chosen['path'][-1])
        option = popup.locator('.el-select-dropdown__item:not(.is-disabled)').filter(has_text=exact(chosen['label']))
        await (await self.unique(option, chosen['label'])).click()
        await dialog.get_by_role('button', name=re.compile(r'^\s*确\s*定\s*$')).click()
        await dialog.wait_for(state='hidden')
        self.category_id = chosen['id']
        self.report['steps']['category'] = chosen
        if not await self.form.locator('#brand').count():
            # Current publisher requires main images on step 1 before opening the full form.
            await self.fill_main_images()
            await self.form.get_by_role('button', name='下一步', exact=True).click()
        await self.form.locator('#brand').wait_for(state='visible')
        await self.wait(lambda: self._captured_attributes(), '未取得当前 B 站类目的属性接口')
        return True

    async def _captured_categories(self):
        await self.capture.drain()
        return bool(self.capture.categories)

    async def _captured_attributes(self):
        await self.capture.drain()
        return self.capture.attributes.get(self.category_id)

    async def fill_attribute(self, attr, fields):
        label = attr['attrName']
        scope = self.form.locator('.attr-panel').filter(has=self.form.locator('span.label').filter(has_text=exact(label)))
        if await scope.count() == 1 and not await scope.is_visible():
            group = scope.locator('xpath=ancestor::*[contains(concat(" ",normalize-space(@class)," ")," el-collapse-item ")][1]')
            await group.locator('.el-collapse-item__header').click()
        scope = await self.unique(scope, label)
        aliases = {'面料': ('面料材质', '面料俗称'), '版型': ('服装版型', '服饰版型', '款式版型'),
                   '厚薄': ('厚度',), '货号': ('款式编码', '商家外部编码')}
        value = excel_value(fields, label, *aliases.get(label, ()))
        if not value and label == '货号':
            value = self.product.style_code
        if not await scope.locator('.el-select').count():
            if not value:
                if attr.get('isMust'):
                    raise BilibiliError(f'B 站必填属性“{label}”缺少 Excel 来源')
                return
            await self.set_input(scope.locator('input,textarea'), value, label)
            self.report['steps'].setdefault('attributes', {})[label] = [value]
            return
        select, popup = await self.open_select(scope)
        options = popup.locator('.el-select-dropdown__item:not(.is-disabled)')
        await self.wait(lambda: options.count(), f'B 站{label}未加载候选')
        names = tuple(x.strip() for x in await options.all_text_contents())
        api_values = tuple(CandidateValue(str(v['attrValueId']), str(v['attrValueName']))
                           for v in attr.get('attrValues', ()) if v.get('attrValueId') is not None and v.get('attrValueName'))
        if (not api_values or len(set(names)) != len(names)
                or set(names) != {v.label for v in api_values}
                or len({v.value_id for v in api_values}) != len(api_values)):
            raise BilibiliError(f'B 站{label}接口候选与当前 DOM 不一致')
        multi = attr.get('attrInputType') == 2
        await select.locator('input').first.press('Escape')
        request = AttributeRequest('bilibili', self.category_id, str(attr['attrId']), label,
                                   api_values, value, {'excel': bool(value), 'live_dom': True, 'live_api': True},
                                   False, canonical_sha256(attr), control_type='multi_select' if multi else 'select')
        if self.attribute_runtime:
            resolved = await self.attribute_runtime.resolve(request)
        else:
            matches = _matched_excel_variants(value, api_values, control_type=request.control_type, field_label=label)
            if not matches:
                if not value and not attr.get('isMust'):
                    return
                raise BilibiliError(f'B 站{label}未匹配，请开启学习与审核')
            resolved = SimpleNamespace(label=','.join(v.label for v in matches[0]), is_no_fill=False)
        if resolved is None:
            return
        if resolved.is_no_fill and attr.get('isMust'):
            raise BilibiliError(f'B 站必填属性“{label}”不能不填写')
        select, popup = await self.open_select(scope)
        selected = popup.locator('.el-select-dropdown__item.is-selected,.el-select-dropdown__item.selected')
        if multi:
            # The selected locator shrinks after every click; index snapshots skip entries.
            while await selected.count():
                count = await selected.count()
                await selected.first.click()
                await self.wait(lambda: self._count_is(selected, count - 1), f'B 站{label}旧选项未清除')
        wanted = () if resolved.is_no_fill else split_multi_choice(resolved.label) if multi else (resolved.label,)
        if resolved.is_no_fill and not multi:
            await select.hover()
            clear = select.locator('.el-select__clear,.el-icon-circle-close')
            await (await self.unique(clear, label + '清空')).click()
        else:
            for name in wanted:
                if names.count(name) != 1:
                    raise BilibiliError(f'B 站{label}判断结果不在当前候选中')
                await popup.locator('.el-select-dropdown__item').filter(has_text=exact(name)).click()
                if not multi:
                    await select.click()
                    popup = await self.popup(select)
        actual = tuple(x.strip() for x in await popup.locator(
            '.el-select-dropdown__item.is-selected,.el-select-dropdown__item.selected').all_text_contents())
        await select.locator('input').first.press('Escape')
        if sorted(actual) != sorted(wanted):
            if self.attribute_runtime:
                await self.attribute_runtime.recover_failed_write(platform_id='bilibili', field_label=label,
                    error='选择后 DOM 回读不一致', force_review=True, fallback_request=request)
                return
            raise BilibiliError(f'B 站{label}选择后回读不一致')
        self.report['steps'].setdefault('attributes', {})[label] = list(actual)

    async def fill_basic(self):
        await self.radio(await self.unique(self.item('发布类型'), '发布类型'), '普通商品', '发布类型')
        await self.set_input(self.form.locator('#name input'), self.product.title, '商品标题')
        await self.select_exact(self.form.locator('#brand'), self.product.brand, '品牌', search=True)
        await self.capture.drain()
        brands = [b for b in self.capture.brands if b.get('brandName') == self.product.brand]
        if len(brands) != 1:
            raise BilibiliError('B 站品牌接口没有唯一匹配项')
        self.report['steps']['brand_id'] = str(brands[0]['brandId'])
        fields = without_color_attributes(self.product.fields)
        attrs = self.capture.attributes[self.category_id].get('attrs', ())
        # Only fields observed in the live schema may be filled.
        for attr in attrs:
            if attr.get('isShow', True) is False:
                continue
            if normalize_label(attr.get('attrName', '')) in {'颜色', '色系', '颜色分类'}:
                continue
            await self.fill_attribute(attr, fields)

    @staticmethod
    async def image_urls(scope):
        # Detail editor also renders a second mobile preview of every image.
        selector = '.image-list .image-result img' if await scope.locator('.image-list').count() else 'img.el-image__inner'
        urls = await scope.locator(selector).evaluate_all(
            '''es=>es.map(e=>e.currentSrc||e.src).filter(u=>/^https?:/.test(u))
            .map(u=>u.split('@')[0])''')
        return [media_identity(url) for url in urls]

    async def upload(self, scope, paths, label):
        paths = tuple(paths)
        existing = await self.image_urls(scope)
        receipts = self.report.setdefault('uploads', {}).get(label, [])
        identities = [{'path': str(p), 'sha256': hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in paths]
        if existing and (len(existing) > len(receipts) or existing != [media_identity(r['url']) for r in receipts[:len(existing)]]
                         or identities[:len(existing)] != [{k: r[k] for k in ('path', 'sha256')} for r in receipts[:len(existing)]]):
            raise BilibiliError(f'B 站{label}已有图片与本次素材或上传顺序不一致')
        self.report['uploads'][label] = receipts[:len(existing)]
        for index in range(len(existing), len(paths)):
            inputs = scope.locator('input[type=file]')
            if not await inputs.count():
                raise BilibiliError(f'B 站{label}没有本地上传控件')
            before = await self.image_urls(scope)
            self.logger.info('B 站%s上传 %s/%s：%s', label, index + 1, len(paths), paths[index].name)
            # Main-image placeholders each expose an input; the first empty slot appends one image.
            await inputs.first.set_input_files(str(paths[index]))
            async def uploaded():
                after = await self.image_urls(scope)
                if len(after) != len(before) + 1 or after[:-1] != before:
                    return False
                if await scope.locator('.el-loading-mask:visible,.image-loading:visible,.is-error:visible').count():
                    return False
                return after[-1]
            url = await self.wait(uploaded, f'B 站{label}上传失败或顺序校验失败', timeout=self.upload_timeout)
            self.report['uploads'][label].append({**identities[index], 'url': url})
            self.persist()

    async def fill_main_images(self):
        schema = self.capture.attributes.get(self.category_id, {})
        main = await self.unique(self.form.locator('#image-list .main-image-field'), '主图区域')
        page_options = await main.locator('input[type=radio]').evaluate_all('es=>es.filter(e=>!e.disabled).map(e=>e.value)')
        api_options = schema.get('imgScaleOptions') or []
        self.report['steps']['main_image_ratio_options'] = {'page': page_options, 'api': api_options}
        if self.product.image_ratio not in page_options:
            raise BilibiliError('当前 B 站页面没有可用的所选主图比例')
        # The category API returns only 1:1 for apparel while the native form
        # offers 3:4 too. Treat that API as auxiliary and verify the saved ratio.
        if api_options and self.product.image_ratio not in api_options:
            self.logger.info('B 站类目接口主图比例候选 %s；实际页面提供 %s，按页面填写 %s 并校验保存回读',
                             api_options, page_options, self.product.image_ratio)
        await self.radio(main, self.product.image_ratio, '主图比例')
        await self.upload(main.locator('.img-upload-wrap'), self.product.images, '主图')
        await self.validate_main_images()

    async def validate_main_images(self):
        main = await self.unique(self.form.locator('#image-list .main-image-field'), '主图区域')
        selected = main.locator('input[type=radio]:checked')
        if await selected.count() != 1 or await selected.input_value() != self.product.image_ratio:
            raise BilibiliError('B 站当前主图比例与本次素材不一致')
        actual = await self.image_urls(main.locator('.img-upload-wrap'))
        receipts = self.report['uploads'].get('主图', ())
        if len(actual) != len(self.product.images) or actual != [media_identity(r['url']) for r in receipts]:
            raise BilibiliError(f'B 站 {self.product.image_ratio} 主图数量或顺序与本次素材不一致')
        self.report['steps']['main_images'] = {'ratio': self.product.image_ratio, 'count': len(actual), 'verified': True}

    async def fill_images(self):
        await self.fill_main_images()
        await self.upload(await self.unique(self.form.locator('#mobile-desc-imgs'), '详情图区域'),
                          self.product.detail_images, '详情图')

    async def fill_specs(self):
        ordered_images = match_color_images(self.product.sku_images, self.product.colors)
        if len(self.product.sku_images) != len(self.product.colors):
            raise BilibiliError('B 站每个颜色须对应一张 SKU 图，请核对表格颜色和素材数量')
        dimensions = [('颜色', self.product.colors), ('尺码', self.product.sizes)]
        dimensions = [(name, values) for name, values in dimensions if values]
        spec_scope = await self.unique(self.item('商品规格'), '商品规格')
        rows = spec_scope.locator('.spec-wrap')
        if await rows.count() > len(dimensions):
            raise BilibiliError('B 站已有规格维度多于 Excel，请核对原草稿')
        for index, (name, values) in enumerate(dimensions):
            if await rows.count() <= index:
                await spec_scope.locator('button').filter(has_text=re.compile(r'添加规格\d/3')).click()
                await self.wait(lambda: self._count_is(rows, index + 1), 'B 站规格维度未添加')
            row = rows.nth(index)
            await self.set_input(row.get_by_placeholder('输入规格名称，如“颜色”', exact=True), name, '规格名称')
            boxes = row.locator('.auto-complete-value input')
            current = [v for v in await boxes.evaluate_all('es=>es.map(e=>e.value)') if v]
            if current and current != list(values[:len(current)]):
                raise BilibiliError('B 站已有规格值与 Excel 顺序不一致')
            for value_index, value in enumerate(values):
                await self.set_input(boxes.nth(value_index), value, f'{name}规格值')
            if name == '颜色':
                toggle = row.locator('.show-img')
                if await toggle.get_attribute('aria-checked') == 'false':
                    await toggle.click()
                for value_index, image in enumerate(ordered_images):
                    await self.upload(row.locator('.boby-item').nth(value_index), (image,), f'SKU图:{values[value_index]}')

    @staticmethod
    async def _count_is(locator, expected):
        return await locator.count() == expected

    async def fill_skus(self):
        table = await self.unique(self.form.locator('.price-stock-form-item .el-table'), 'SKU 表格')
        headers = await table.locator('.el-table__header-wrapper th').all_text_contents()
        normalized = [re.sub(r'\s|\*', '', v).replace('（', '(').replace('）', ')') for v in headers]
        columns = {}
        for name in ('颜色', '尺码', '售价(元)', '可售库存', '商家编码'):
            matches = [i for i, v in enumerate(normalized) if v == name.replace('（', '(').replace('）', ')')]
            if len(matches) == 1:
                columns[name] = matches[0]
        if any(name not in columns for name in ('售价(元)', '可售库存', '商家编码')):
            raise BilibiliError('B 站 SKU 表头不完整或不唯一')
        rows = table.locator('.el-table__body-wrapper tr.el-table__row')
        grid = await rows.evaluate_all('''rows => {
          const carried=[];
          return rows.map((row,rowIndex)=>{
            const values=[];
            carried.forEach((entry,col)=>{if(entry?.left>0){values[col]=entry.cell;entry.left--;}});
            let col=0;
            [...row.children].filter(e=>e.tagName==='TD').forEach((cell,cellIndex)=>{
              while(values[col])col++;
              const entry={row:rowIndex,index:cellIndex,text:cell.innerText.trim()};
              for(let k=0;k<(cell.colSpan||1);k++){
                values[col+k]=entry;
                carried[col+k]={cell:entry,left:(cell.rowSpan||1)-1};
              }
              col+=cell.colSpan||1;
            });
            return values;
          });
        }''')
        expected = {(c, s) for c in self.product.colors for s in (self.product.sizes or ('',))} or {('', '')}
        seen = set()
        for index in range(await rows.count()):
            row = rows.nth(index)
            cells = row.locator('td')
            key = []
            for dimension in ('颜色', '尺码'):
                if dimension not in columns:
                    key.append('')
                    continue
                key.append(grid[index][columns[dimension]]['text'])
            pair = tuple(key)
            if pair not in expected or pair in seen:
                raise BilibiliError(f'B 站 SKU 组合重复或与 Excel 不一致：{pair}')
            seen.add(pair)
            for name, value in (('售价(元)', self.product.price), ('可售库存', self.product.stock),
                                ('商家编码', self.product.merchant_code(*pair))):
                cell = grid[index][columns[name]]
                if cell['row'] != index:
                    raise BilibiliError('B 站价格库存列出现意外合并单元格')
                await self.set_input(cells.nth(cell['index']).locator('input'), value,
                                     f'{pair} {name}', numeric=name != '商家编码')
        if seen != expected:
            raise BilibiliError('B 站 SKU 组合不完整或存在未处理分页')
        self.report['steps']['sku_count'] = len(seen)

    async def fill_services(self):
        days = int(self.product.delivery_days)
        configs = self.capture.attributes[self.category_id].get('deliveryTimeConfigs', ())
        if days > 2 and not any(days in (c.get('deliveryTimeValues') or []) for c in configs if c.get('deliveryTimeType') == 1):
            raise BilibiliError('Excel 发货天数不在当前 B 站类目接口允许范围内')
        delivery = await self.unique(self.item('发货时效'), '发货时效')
        label = '24小时内发货' if days == 1 else '48小时内发货' if days == 2 else '大于48小时发货'
        await self.radio(delivery, label, '发货时效')
        if days > 2:
            await self.select_exact(delivery, str(days), '发货天数')
            # Platform nests two validators for the same radio field; changing the
            # day clears only the inner validator. Re-select through the native radios.
            if await delivery.locator(':scope > .el-form-item__content > .el-form-item__error:visible').count():
                await self.radio(delivery, '24小时内发货', '发货时效')
                await self.radio(delivery, label, '发货时效')
                await self.select_exact(delivery, str(days), '发货天数')
        deduction = excel_value(self.product.fields, '库存扣减', default='下单减库存')
        if deduction not in {'下单减库存', '支付减库存'}:
            raise BilibiliError('Excel 库存扣减须为下单减库存或支付减库存')
        await self.radio(await self.unique(self.item('库存扣减'), '库存扣减'), deduction, '库存扣减')
        self.report['steps']['stock_deduction'] = 1 if deduction == '下单减库存' else 2
        await self.radio(await self.unique(self.item('上架设置'), '上架设置'), self.product.listing_state, '上架设置')
        await self.radio(await self.unique(self.item('定时开售'), '定时开售'), '否', '定时开售')
        await self.wait(lambda: self._freight_loaded(), '未捕获 B 站运费模板接口')
        candidates = [t for t in self.capture.freight if t.get('name') == self.product.freight_template]
        if len(candidates) != 1:
            raise BilibiliError('Excel 运费模板未唯一匹配当前 B 站店铺接口')
        await self.select_exact(await self.unique(self.item('运费模板'), '运费模板'), self.product.freight_template, '运费模板')
        self.report['steps']['freight_id'] = str(candidates[0]['templateId'])
        await self.radio(await self.unique(self.item('退换说明'), '退换说明'), '支持7天无理由', '退换说明')
        await self.fill_flow()

    async def fill_flow(self):
        fields = self.product.fields
        expected = {}
        for label in ('同步至动态', 'UP主带货'):
            value = excel_value(fields, label)
            scope = self.item(label)
            if not value and not await scope.count():
                continue
            if value:
                if value not in {'是', '否'}:
                    raise BilibiliError(f'Excel {label}须为是或否')
                await self.radio(await self.unique(scope, label), value, label)
            checked = scope.locator('label.el-radio').filter(has=self.form.locator('input:checked'))
            expected[label] = (await checked.locator('.el-radio__label').inner_text()).strip()
        if expected.get('同步至动态') == '是':
            text = self.item('同步至动态').locator('textarea')
            description = excel_value(fields, '动态文案', '同步动态文案')
            if description:
                await self.set_input(text, description, '动态文案')
            expected['dynamic_desc'] = await text.input_value()
        if expected.get('UP主带货') == '是':
            scope = await self.unique(self.item('商品佣金'), '商品佣金')
            kind = excel_value(fields, '商品佣金', '商家佣金', default='固定佣金')
            if kind != '固定佣金':
                raise BilibiliError('当前 B 站自动上款仅支持固定佣金')
            await self.radio(scope, kind, '商品佣金')
            ratio = scope.locator('input[type=text]')
            value = excel_value(fields, '佣金比例', '商品佣金比例', '固定佣金比例').removesuffix('%')
            if value:
                await self.set_input(ratio, value, '佣金比例', numeric=True)
            current = await ratio.input_value()
            from decimal import Decimal, InvalidOperation
            try:
                if not Decimal(current).is_finite() or not 0 <= Decimal(current) <= 50:
                    raise InvalidOperation
            except InvalidOperation:
                raise BilibiliError('B 站固定佣金比例须为 0–50；请填写 Excel 佣金比例或配置店铺默认值') from None
            expected.update(commission_ratio=current, commission_source='excel' if value else 'merchant_default')
        self.report['steps']['flow'] = expected

    async def _freight_loaded(self):
        await self.capture.drain()
        return bool(self.capture.freight)

    async def validate_page(self):
        errors = [s.strip() for s in await self.form.locator(
            '.el-form-item__error:visible,.error-text:visible,.validation-error:visible').all_text_contents() if s.strip()]
        if errors:
            raise BilibiliError('B 站页面校验失败：' + '；'.join(errors))

    async def save(self, *, publish=False):
        await self.validate_main_images()
        await self.validate_page()
        sequence = self.capture.sequence
        self.report['submission_attempted' if publish else 'save_attempted'] = True
        self.persist()
        await self.form.get_by_role('button', name='发布商品' if publish else '保存草稿', exact=True).click()
        async def receipt():
            await self.capture.drain()
            from bilibili_api import SAVE_CALLS, SAVE_BACKS
            failures = [o for o in self.capture.observations if o['sequence'] > sequence
                        and o['path'] in SAVE_CALLS | SAVE_BACKS and o['status'] == 'error']
            if failures:
                raise BilibiliError('B 站保存接口未成功：' + str(failures[-1].get('message') or failures[-1].get('code')))
            current = [r['data'] for r in self.capture.receipts if r['sequence'] > sequence and isinstance(r['data'], dict)]
            key = 'itemsId' if publish else 'draftId'
            return next((r for r in current if str(r.get(key, '')).isdigit() and int(r[key]) > 0), False)
        saved = await self.wait(receipt, 'B 站保存/发布结果未确认，请核对平台记录后继续', timeout=self.upload_timeout)
        key = 'itemsId' if publish else 'draftId'
        if publish and saved.get('freightTemplateBindSuccess') is not True:
            raise BilibiliError('B 站发布回执未确认运费模板绑定成功，请先检查平台商品状态')
        url = PUBLISH_URL + '?' + key + '=' + str(saved[key])
        self.report['item_url' if publish else 'draft_url'] = url
        self.persist()
        # Reopen by the server-issued ID and inspect the server's readback.
        self.capture.info = None
        await self.page.goto(url, wait_until='domcontentloaded')
        await self.bind_form()
        async def reopened():
            await self.capture.drain()
            info = self.capture.info
            return info if isinstance(info, dict) and str(info.get(key)) == str(saved[key]) else False
        info = await self.wait(reopened, 'B 站保存后未取得同一商品的接口回读')
        self.verify_saved(info)
        if self.attribute_runtime:
            self.attribute_runtime.record_verified_readbacks('bilibili', self.report['steps'].get('attributes', {}))
        self.report['status'] = 'published' if publish else 'draft_saved'
        self.report['saved_id'] = str(saved[key])
        write_json(self.directory / 'bilibili-after-save-validation.json', {
            'attributes': self.report['steps'].get('attributes', {}), 'verified': True,
            'sku_count': len(info.get('skuList', ()))})
        self.persist()

    def verify_saved(self, info):
        p = self.product
        if info.get('name') != p.title or str(info.get('cateId')) != self.category_id:
            raise BilibiliError('B 站保存后商品标题或类目回读不一致')
        if str(info.get('brandId')) != self.report['steps']['brand_id'] or info.get('deployType') != 1:
            raise BilibiliError('B 站保存后品牌或发布类型回读不一致')
        verify_sku_payload(info.get('skuList', ()), p)
        commitment = info.get('itemsCommitmentTag') or {}
        for key, expected in (('freightTemplateId', self.report['steps']['freight_id']),
                              ('deliveryTimeType', 0 if int(p.delivery_days) <= 2 else 1),
                              ('deliveryTimeValue', int(p.delivery_days) * 24 if int(p.delivery_days) <= 2 else p.delivery_days),
                              ('stockDeductMethod', self.report['steps']['stock_deduction']), ('supportReturn', 1)):
            actual = commitment.get(key) if key in {'deliveryTimeType', 'deliveryTimeValue', 'supportReturn'} else info.get(key)
            if str(actual) != str(expected):
                raise BilibiliError(f'B 站保存后 {key} 回读不一致')
        if info.get('autoOnSaleTime') not in (None, '', 0):
            raise BilibiliError('B 站保存后定时开售未关闭')
        if any(s.get('autoOnSaleTime') not in (None, '', 0) for s in info.get('skuList', ())):
            raise BilibiliError('B 站保存后 SKU 定时开售未关闭')
        if info.get('imgScale') != p.image_ratio:
            raise BilibiliError('B 站保存后主图比例回读不一致')
        expected_operation = 1 if p.listing_state == '立即上架' else 3
        if info.get('draftId'):
            if info.get('operateStatus') != expected_operation:
                raise BilibiliError('B 站草稿上架设置回读不一致')
        elif info.get('status') != (1 if expected_operation == 1 else 2):
            raise BilibiliError('B 站商品上下架状态回读不一致')
        main = [r['url'] for r in self.report['uploads']['主图']]
        details = [r['url'] for r in self.report['uploads']['详情图']]
        normalize = media_identity
        if [normalize(u) for u in info.get('imageList', ())] != [normalize(u) for u in main]:
            raise BilibiliError('B 站保存后主图数量或顺序不一致')
        description_images = re.findall(r'<img[^>]+src=["\']([^"\']+)', info.get('mobileDesc', ''))
        if [normalize(u) for u in description_images] != [normalize(u) for u in details]:
            raise BilibiliError('B 站保存后详情图数量或顺序不一致')
        for sku in info.get('skuList', ()):
            color = next((s.get('specValueName') for s in sku.get('specList', ()) if s.get('specKeyName') == '颜色'), '')
            if color:
                images = self.report['uploads'].get('SKU图:' + color, ())
                if len(images) != 1 or normalize(sku.get('img')) != normalize(images[0]['url']):
                    raise BilibiliError('B 站保存后 SKU 图片回读不一致')
        flow = self.report['steps'].get('flow', {})
        if 'UP主带货' in flow and bool(info.get('upCommerce')) != (flow['UP主带货'] == '是'):
            raise BilibiliError('B 站保存后 UP主带货回读不一致')
        if '同步至动态' in flow:
            dynamic = info.get('dynamicInfo') or {}
            if bool(dynamic.get('syncDynamic')) != (flow['同步至动态'] == '是'):
                raise BilibiliError('B 站保存后同步至动态回读不一致')
            if flow['同步至动态'] == '是' and dynamic.get('dynamicDesc') != flow['dynamic_desc']:
                raise BilibiliError('B 站保存后动态文案回读不一致')
        if flow.get('commission_ratio') is not None:
            ratios = info.get('commissionRatioList') or []
            if len(ratios) != 1 or not same_number(ratios[0].get('ratio'), flow['commission_ratio']):
                raise BilibiliError('B 站保存后固定佣金比例回读不一致')
        readback = {a['attrName']: [v['attrValueName'] for v in a.get('itemsAttrValues', ())]
                    for a in info.get('itemsAttrs', ()) if a.get('attrName')}
        schema = self.capture.attributes.get(self.category_id, {}).get('attrs', ())
        labels = {str(a['attrId']): a['attrName'] for a in schema}
        for attr in info.get('itemsAttrs', ()):
            label = labels.get(str(attr.get('attrId')))
            if label:
                readback[label] = [v['attrValueName'] for v in attr.get('itemsAttrValues', ())]
        for label, expected in self.report['steps'].get('attributes', {}).items():
            if sorted(readback.get(label, [])) != sorted(expected):
                raise BilibiliError(f'B 站保存后属性“{label}”回读不一致')
        self.report['steps']['persisted_readback'] = {'verified': True, 'sku_count': len(info.get('skuList', ())),
                                                     'attributes': readback, 'image_ratio': info['imgScale'],
                                                     'main_image_count': len(main)}

    async def run(self, *, save=True, publish=False, resume_url='', login_timeout=600):
        try:
            await self.page.goto(resume_url or PUBLISH_URL, wait_until='domcontentloaded')
            self.logger.info('等待 B 站商家后台登录；请在独立浏览器中完成扫码')
            await self.bind_form(timeout=login_timeout)
            if resume_url:
                async def restored():
                    await self.capture.drain()
                    return self.capture.info
                info = await self.wait(restored, 'B 站原草稿未加载')
                self.category_id = str(info.get('cateId', ''))
                if info.get('name') != self.product.title:
                    raise BilibiliError('B 站原草稿标题与当前商品不一致')
                await self.wait(lambda: self._captured_attributes(), '原草稿类目属性未加载')
            while True:
                if self.attribute_runtime:
                    self.attribute_runtime.begin_review_collection('bilibili')
                try:
                    if not self.category_id:
                        selected = await self.select_category()
                        if not selected:
                            self.attribute_runtime.raise_deferred_reviews()
                    await self.fill_basic()
                    if self.attribute_runtime:
                        self.attribute_runtime.raise_deferred_reviews()
                except ReviewBatchRequired as error:
                    if not self.on_review:
                        raise
                    self.report['status'] = 'waiting_review'
                    self.persist()
                    await self.on_review(error)
                    self.report['status'] = 'running'
                    # Keep the current unsaved page and the open category dialog.
                    continue
                break
            await self.fill_images()
            await self.fill_specs()
            await self.fill_skus()
            await self.fill_services()
            await self.validate_main_images()
            await self.validate_page()
            self.report['status'] = 'filled'
            self.persist()
            if save:
                await self.save(publish=publish)
        except BaseException as exc:
            self.report.update(status='failed', error=str(exc))
            self.persist()
            try:
                await self.page.screenshot(path=str(self.directory / 'bilibili-error.png'), full_page=True)
            except Exception:
                pass
            raise
        finally:
            await self.capture.close()
            write_json(self.directory / 'bilibili-api-schema.json', self.capture.schema())


async def run_bilibili_browser(args, product, directory, logger, state_path, *, learning_context=None):
    from playwright.async_api import async_playwright
    from kuaimai_erp import discover_existing_profile_cdp_url
    from bilibili_auth import restore_auth, save_auth
    previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
    digest = fingerprint(product)
    if previous.get('status') == 'published':
        if previous.get('fingerprint') != digest:
            raise BilibiliError('本商品已发布至 B 站；资料已变化，请先核对平台商品再操作')
        write_json(directory / 'bilibili-result.json', {**previous, 'status': 'already_published'})
        return
    resume_url = resume_state(previous, product, args.bilibili_resume_url)
    if (not args.bilibili_publish and not args.bilibili_resume_url and args.save
            and previous.get('status') == 'draft_saved' and previous.get('fingerprint') == digest
            and previous.get('steps', {}).get('persisted_readback', {}).get('verified')):
        write_json(directory / 'bilibili-result.json', {**previous, 'status': 'already_saved'})
        return
    async with async_playwright() as playwright:
        profile = Path(args.user_data_dir).expanduser().resolve()
        cdp_url = args.cdp_url or discover_existing_profile_cdp_url(profile)
        owned = not cdp_url
        if cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise BilibiliError('CDP 浏览器没有可用上下文')
            context = browser.contexts[0]
        else:
            context = await playwright.chromium.launch_persistent_context(
                str(profile), channel='chrome',
                headless=args.headless, no_viewport=True)
        auth_path = profile / 'bilibili-auth-state.json'
        await restore_auth(context, auth_path, logger)
        page = await context.new_page()
        page.set_default_timeout(min(args.timeout, 60) * 1000)
        listing = BilibiliListing(page, product, directory, logger, timeout=min(args.timeout, 60),
                                  upload_timeout=args.upload_timeout,
                                  attribute_runtime=getattr(learning_context, 'attribute_runtime', None))
        if previous.get('draft_url') == resume_url and resume_url:
            listing.report = deepcopy(previous)
            listing.report.update(status='running')
            listing.report.pop('error', None)
        listing.report.update(fingerprint=digest, excel_path=str(product.excel_path))
        listing.on_checkpoint = lambda report: write_json(state_path, report)
        if learning_context:
            async def on_review(error):
                from kuaimai_erp import wait_for_platform_review_batch
                await wait_for_platform_review_batch(learning_context, args, ('bilibili',), 0, error.reviews, logger)
            listing.on_review = on_review
        try:
            await listing.run(save=args.save, publish=args.bilibili_publish, resume_url=resume_url,
                              login_timeout=args.login_timeout)
        finally:
            await save_auth(context, auth_path, logger)
            if owned:
                if not args.save and not args.headless:
                    logger.info('仅填写模式：请在浏览器检查表单；关闭 B 站页面后结束任务')
                    if not page.is_closed():
                        await page.wait_for_event('close', timeout=0)
                await context.close()


async def run_bilibili_with_learning(args, product, directory, logger, state_path):
    from kuaimai_erp import (create_learning_context, initialize_learning_run, save_learning_checkpoint,
                            drain_learning_events, write_model_judgment_report)
    learning = create_learning_context(args, product)
    if learning is None:
        return await run_bilibili_browser(args, product, directory, logger, state_path)
    from historical_readbacks import load_verified_attribute_history
    from kuaimai_erp import SCRIPT_DIR
    learning.attribute_runtime.verified_history.update(load_verified_attribute_history(
        SCRIPT_DIR / 'output/bilibili/runs', product.style_code))
    session = SimpleNamespace(model_decisions=[], model_events=[])
    status, error = 'running', ''
    def report():
        write_model_judgment_report(directory, learning, session, run_status=status, stop_reason=error)
    learning.attribute_runtime.model_decision_sink = report
    try:
        await initialize_learning_run(learning, product, logger)
        save_learning_checkpoint(learning, args, ('bilibili',), 0, 'running')
        await run_bilibili_browser(args, product, directory, logger, state_path, learning_context=learning)
        save_learning_checkpoint(learning, args, ('bilibili',), 1, 'completed')
        status = 'completed'
    except BaseException as exc:
        status, error = 'failed', str(exc)
        save_learning_checkpoint(learning, args, ('bilibili',), 0, 'failed')
        raise
    finally:
        report()
        try:
            await drain_learning_events(learning)
        finally:
            learning.store.close()


def run_bilibili_command(args):
    from kuaimai_erp import (DEFAULT_PROFILE_DIR, DEFAULT_EXCEL_URL, SCRIPT_DIR, automation_run_lock, setup_logging,
                            resolve_excel_path)
    directory = args.output_dir or SCRIPT_DIR / 'output/bilibili/runs' / datetime.now().strftime('%Y%m%d-%H%M%S')
    logger = setup_logging(directory)
    try:
        if Path(args.user_data_dir).expanduser().resolve() == DEFAULT_PROFILE_DIR.resolve():
            args.user_data_dir = str(SCRIPT_DIR / 'output/bilibili/chrome-profile')
        if args.excel_url == DEFAULT_EXCEL_URL:
            candidates = sorted(Path(args.bilibili_root).expanduser().glob('*/产品信息.xlsx'))
            if args.bilibili_product:
                candidates = [p for p in candidates if p.parent.name == args.bilibili_product]
            if len(candidates) != 1:
                raise BilibiliError('请用 --bilibili-product 指定唯一商品目录，或用 --excel-url 指定表格')
            excel_path = candidates[0]
        else:
            excel_path = resolve_excel_path(args.excel_url)
        product = read_bilibili_product(excel_path)
        summary = {**product.summary(), 'publish': args.bilibili_publish}
        write_json(directory / 'input-summary.json', summary)
        if args.dry_run:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        key = canonical_sha256(str(product.excel_path.resolve()))[:24]
        state_path = SCRIPT_DIR / '.local-state/bilibili' / (key + '.json')
        with automation_run_lock(Path(args.user_data_dir)):
            asyncio.run(run_bilibili_with_learning(args, product, directory, logger, state_path))
        logger.info('B 站流程完成：%s', directory)
        return 0
    except KeyboardInterrupt:
        logger.error('B 站流程已中止，可从已记录的原草稿继续')
        return 130
    except Exception as exc:
        logger.error('B 站流程停止：%s', exc)
        return 2
