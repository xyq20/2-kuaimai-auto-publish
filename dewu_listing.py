"""Independent Dewu workflow; draft saves are default, review requires opt-in."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
from types import SimpleNamespace
from decimal import Decimal, InvalidOperation
import sys
import time
from urllib.parse import parse_qs, urlsplit

from dewu_auth import restore_dewu_auth_state, save_dewu_auth_state
from dewu_data import (
    DEWU_START_URL, DewuError, DewuProduct, chinese_measurement, read_dewu_product,
    select_workbook, split_detail_images, split_values,
)
from douyin_data import field_lookup
from attribute_runtime import (ReviewRequired, ReviewBatchRequired, split_multi_choice,
                               _matched_excel_variants)
from learning_models import CandidateValue, canonical_sha256

REVIEW_ENDPOINT = '/api/v1/h5/biz/nps/v2/newApply'
SUBMITTED_STATUSES = {'submitted_for_review', 'already_submitted'}
WHITE_BACKGROUND_RECIPE = 'dewu-matting-1x1-white-v1'

# ElTooltip can overwrite the select root's __vue__ when its tags collapse.
# Resolve the ElSelect component through its own DOM/component tree instead.
SELECT_VUE_OWNER_JS = '''function selectVueOwner(root) {
    const owners=new Set(),seen=new Set();
    const inspect=vm=>{
        for(let n=0;vm&&n<20&&!seen.has(vm);n++,vm=vm.$parent){
            seen.add(vm);
            if(vm.$el===root&&vm.$options?.name==='ElSelect')owners.add(vm);
        }
    };
    for(const e of [root,...root.querySelectorAll('.el-input,.el-tag,.el-select__tags')])inspect(e.__vue__);
    for(const vm of root.__vue__?.$children||[])inspect(vm);
    if(owners.size===1)return [...owners][0];
    // Legacy standalone controls may expose an unnamed component directly.
    return !owners.size&&!root.__vue__?.$options?.name?root.__vue__:null;
}'''


def exact(text: str):
    return re.compile(r'^\s*' + re.escape(text) + r'\s*$')


def same_number(actual, expected):
    try:
        left, right = Decimal(actual), Decimal(expected)
        return left.is_finite() and right.is_finite() and left == right
    except (InvalidOperation, TypeError, ValueError):
        return False


def valid_draft_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == 'https' and parsed.hostname == 'stark.dewu.com'
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
            and bool(re.fullmatch(r'/vueProduct/newProductApply/spuEdit/operation/\d+', parsed.path)))


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def input_fingerprint(product):
    digest = hashlib.sha256()
    files = (product.excel_path, *product.initial_images, *product.detail_images, product.size_image)
    for path in files:
        digest.update(str(path.resolve()).encode())
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()


def image_fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def product_identity(product):
    return {'excel_path': str(product.excel_path.resolve()), 'style_code': product.style_code,
            'brand': product.brand, 'category': list(product.category)}


def sku_codes_match(product, rows):
    """A saved receipt is current only when every expected SKU has its size suffix."""
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return False
    pairs = [(row.get('颜色'), row.get('尺码')) for row in rows]
    expected = {(color, size) for color in product.colors for size in product.sizes}
    return (len(pairs) == len(expected) and set(pairs) == expected
            and all(row.get('商品编码') == product.sku_code + row['尺码'] for row in rows))


def needs_sku_code_update(previous, explicit_url, fingerprint, already_saved):
    return (not explicit_url and previous.get('fingerprint') == fingerprint
            and bool(previous.get('draft_url'))
            and (already_saved or previous.get('update_scope') in {'sku_codes', 'draft_maintenance'}))


def white_backgrounds_complete(product, report):
    """Only a verified derived image receipt can satisfy the white-image step."""
    paths = product.white_images[:2]
    groups = report.get('steps', {}).get('carousel', {}).get('groups', [])
    if not groups:
        return False
    receipts = report.get('steps', {}).get('white_backgrounds', [])
    for group_index, urls in enumerate(groups, 1):
        label = f'商品轮播图-{group_index}'
        uploads = report.get('uploads', {}).get(label, [])
        for index, path in enumerate(paths):
            matching = [r for r in receipts if r.get('group') == label and r.get('index') == index]
            if len(matching) != 1:
                return False
            receipt = matching[0]
            if not (receipt.get('recipe') == WHITE_BACKGROUND_RECIPE and receipt.get('status') == 'applied'
                    and receipt.get('path') == str(path) and receipt.get('sha256') == image_fingerprint(path)
                    and len(urls) > index and len(uploads) > index
                    and receipt.get('result_url') == urls[index] == uploads[index].get('url')
                    and uploads[index].get('sha256') == receipt['sha256']):
                return False
    return True


def choose_resume(previous, explicit_url, fingerprint, *, identity=None):
    if explicit_url:
        if not valid_draft_url(explicit_url):
            raise DewuError('恢复地址必须是 stark.dewu.com 的新品草稿编辑地址')
        return explicit_url, False
    if previous.get('draft_url'):
        if not valid_draft_url(previous['draft_url']):
            raise DewuError('已记录的草稿地址无效，请核对原草稿后用 --dewu-resume-url 指定正确地址')
        # Content changes decide whether to update, not which draft to open.
        # Legacy reports have no identity snapshot; check what they retained,
        # then verify the draft's brand and category on the page. Its editable
        # style code may still be the platform's prefill after an unsaved run.
        recorded = previous.get('product_identity') or {
            'excel_path': previous.get('excel_path'),
            'style_code': previous.get('steps', {}).get('basic', {}).get('style_code')}
        if identity and any(value and value != identity.get(key) for key, value in recorded.items()):
            raise DewuError('商品货号、品牌、类目或资料目录与原草稿记录不一致，请核对后用 --dewu-resume-url 指定更新地址')
        return previous['draft_url'], (previous.get('status') == 'draft_saved'
                                       and previous.get('fingerprint') == fingerprint)
    if previous.get('creation_attempted'):
        raise DewuError('上次已点击创建但未取得草稿地址；请从得物草稿列表核对后使用 --dewu-resume-url，避免重复创建')
    return '', False


class DewuListing:
    def __init__(self, page, product: DewuProduct, directory: Path, logger=None, *, timeout=60, upload_timeout=600,
                 attribute_runtime=None):
        self.page, self.product, self.directory = page, product, directory
        self.logger = logger or logging.getLogger(__name__)
        self.timeout, self.upload_timeout = timeout, upload_timeout
        self.login_timeout = timeout
        self.report = {'platform': 'dewu', 'submit_review': False, 'steps': {}, 'status': 'running'}
        self.on_checkpoint = None
        self.on_authenticated = None
        self.attribute_runtime = attribute_runtime
        self.on_review = None

    async def wait(self, predicate, message, *, timeout=None):
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            result = await predicate()
            if result:
                return result
            await asyncio.sleep(.25)
        raise DewuError(message)

    async def unique(self, locator, label):
        tour = self.page.locator('.v-tour .v-step__button-stop:visible')
        if await tour.count():
            await tour.click()
        await locator.first.wait_for(state='visible', timeout=self.timeout * 1000)
        if await locator.count() != 1:
            raise DewuError(f'得物控件不唯一：{label}')
        return locator

    def item(self, label, scope=None):
        scope = scope or self.page
        return scope.locator('.el-form-item').filter(
            has=self.page.locator('label.el-form-item__label').filter(
                has_text=re.compile(r'^\s*\*?\s*' + re.escape(label) + r'\s*$')))

    async def set_input(self, locator, value, label):
        locator = await self.unique(locator, label)
        await locator.fill(value)
        await locator.press('Tab')
        actual = await locator.input_value()
        matches = actual == value
        if not matches and await locator.get_attribute('role') == 'spinbutton':
            matches = same_number(actual, value)
        if not matches:
            raise DewuError(f'{label}填写后回读不一致')
        return value

    async def select_dropdown(self, select, kind):
        """Bind a portaled popup to its control, never to global visibility."""
        self._dropdown_sequence = getattr(self, '_dropdown_sequence', 0) + 1
        token = f'dewu-{id(self)}-{self._dropdown_sequence}'
        async def owned():
            found = await select.evaluate('''(root, {kind,token}) => {''' + SELECT_VUE_OWNER_JS + '''
                const selector=kind==='el'?'.el-select-dropdown':'.ant-select-dropdown';
                const candidates=new Set();let hinted=false;
                const add=node=>{const p=node?.closest?.(selector);if(p?.isConnected)candidates.add(p);};
                for(const e of [root,...root.querySelectorAll('[aria-owns],[aria-controls]')]){
                    for(const attr of ['aria-owns','aria-controls']){
                        for(const id of (e.getAttribute(attr)||'').split(/\\s+/).filter(Boolean)){
                            hinted=true;add(document.getElementById(id));
                        }
                    }
                }
                const vm=selectVueOwner(root);
                if(vm?.popperElm||vm?.$refs?.popper){
                    hinted=true;add(vm.popperElm);add(vm.$refs?.popper?.$el);
                }
                for(const p of root.querySelectorAll(selector))add(p);
                // Compatibility for standalone controls without ownership metadata.
                // Multiple controls/popups require an explicit association.
                if(!hinted&&!candidates.size&&document.querySelectorAll(kind==='el'?'.el-select':'.ant-select').length===1){
                    const all=document.querySelectorAll(selector);if(all.length===1)add(all[0]);
                }
                if(candidates.size!==1)return false;
                [...candidates][0].setAttribute('data-dewu-dropdown',token);return true;
            }''', {'kind':kind,'token':token})
            if not found:
                return False
            popup = self.page.locator(f'[data-dewu-dropdown="{token}"]')
            return popup if await popup.is_visible() else False
        return await self.wait(owned, '未找到当前属性控件所属的下拉框')

    async def open_select_dropdown(self, select, kind, *, multi=False):
        expanded = await select.evaluate('''root=>{''' + SELECT_VUE_OWNER_JS + '''
            const vm=selectVueOwner(root);
            if(typeof vm?.visible==='boolean')return vm.visible;
            const e=root.matches('[aria-expanded]')?root:root.querySelector('[aria-expanded]');
            return e?e.getAttribute('aria-expanded')==='true':false;
        }''')
        if not expanded:
            trigger = select.locator('.ant-select-selector' if kind == 'ant' else '.el-input__inner')
            if kind == 'el' and multi:
                arrow = select.locator('.el-input__suffix')
                if await arrow.count() == 1 and await arrow.is_visible():
                    trigger = arrow
            await trigger.click()
        return await self.select_dropdown(select, kind)

    async def close_select_dropdown(self, select, kind, popup):
        target = select.locator('input' if kind == 'ant' else 'input.el-select__input')
        if not await target.count():
            target = select.locator('.el-input__inner')
        await target.first.press('Escape')
        await popup.wait_for(state='hidden', timeout=self.timeout * 1000)

    async def select(self, scope, value, label, *, selected_values=None):
        """Element and Ant selects require real candidate selection, then readback."""
        radio = scope.locator('label.el-radio, label.ant-radio-wrapper').filter(has_text=exact(value))
        if await radio.count() == 1:
            checked = radio.locator('input[type=radio]')
            if await checked.is_checked():
                return value
            target = radio.locator('.el-radio__inner,.ant-radio-inner')
            await (target if await target.count() else radio).click()
            await self.wait(lambda: radio.locator('input[type=radio]').is_checked(), f'{label}单选未选中')
            return value
        ant = scope.locator('.ant-select')
        if await ant.count() == 1:
            popup = await self.open_select_dropdown(ant, 'ant')
            search = ant.locator('input:not([readonly])')
            if await search.count():
                await search.fill(value)
            option = popup.locator('.ant-select-item-option').filter(
                has=self.page.locator('.ant-select-item-option-content').filter(has_text=exact(value)))
            # Template options also display a secondary "自定义" badge.
            if not await option.count():
                option = popup.locator('.ant-select-item-option').filter(
                    has=self.page.get_by_text(value, exact=True))
            await (await self.unique(option, label + ':' + value)).click()
            selected = ant.locator('.ant-select-selection-item')
            await self.wait(lambda: selected.count(), f'{label}未生成选中值')
            if value not in await selected.inner_text():
                raise DewuError(f'{label}选择后回读不一致')
            await self.close_select_dropdown(ant, 'ant', popup)
            return value
        el = scope.locator('.el-select')
        if await el.count() != 1:
            raise DewuError(f'{label}没有唯一可识别的下拉或单选控件')
        multiple = await el.locator('.el-select__tags').count() > 0
        if multiple:
            # Remove inferred AI values first. Do not accumulate conflicting values.
            closers = el.locator('.el-tag__close')
            for _ in range(100):
                if not await closers.count():
                    break
                await closers.first.click()
            if await closers.count():
                raise DewuError(f'{label}旧多选值未清空')
        values = selected_values or (split_values(value) if multiple else (value,))
        for part in values:
            popup = await self.open_select_dropdown(el, 'el', multi=multiple)
            search = el.locator('input.el-select__input')
            if await search.count():
                await search.fill(part)
            option = popup.locator('.el-select-dropdown__item').filter(has_text=exact(part))
            if not await option.count():
                option = popup.locator('.el-select-dropdown__item').filter(
                    has=self.page.get_by_text(part, exact=True))
            await (await self.unique(option, label + ':' + part)).click()
            await self.close_select_dropdown(el, 'el', popup)
        if multiple:
            # Tags can collapse to "+ n"; use their title/text, not the search input.
            # Always check the full selected set, including collapsed AI values.
            popup = await self.open_select_dropdown(el, 'el', multi=True)
            search = el.locator('input.el-select__input')
            if await search.count():
                await search.fill('')
            chosen = await popup.locator('.el-select-dropdown__item.selected').all_text_contents()
            await self.close_select_dropdown(el, 'el', popup)
            if set(part.strip() for part in chosen) != set(values):
                raise DewuError(f'{label}多选回读不一致：{chosen}')
        elif await el.locator('.el-input__inner').input_value() != value:
            raise DewuError(f'{label}下拉回读不一致')
        return value

    async def select_attribute(self, scope, value, label):
        """Resolve Excel intent using live options and the shared review runtime."""
        self.report.setdefault('attribute_choices', {}).pop(label, None)
        radios = scope.locator('label.el-radio, label.ant-radio-wrapper')
        ant, el = scope.locator('.ant-select'), scope.locator('.el-select')
        if await radios.count():
            options = await radios.evaluate_all('''es => es.map(e => ({name: e.innerText.trim(),
                disabled: !!e.querySelector('input:disabled')}))''')
            multi = False
        elif await ant.count() == 1:
            multi = 'ant-select-multiple' in (await ant.get_attribute('class') or '').split()
            popup = await self.open_select_dropdown(ant, 'ant', multi=multi)
            search = ant.locator('input:not([readonly])')
            if await search.count():
                await search.fill('')
            choices = popup.locator('.ant-select-item-option')
            await self.wait(lambda: choices.count(), f'{label}未取得页面候选')
            options = await choices.evaluate_all('''es => es.map(e => ({
                name: e.getAttribute('title') || e.querySelector('.ant-select-item-option-content')?.innerText.trim(),
                disabled: e.classList.contains('ant-select-item-option-disabled')}))''')
            await self.close_select_dropdown(ant, 'ant', popup)
        elif await el.count() == 1:
            multi = await el.locator('.el-select__tags').count() > 0
            popup = await self.open_attribute_select(el, multi)
            search = el.locator('input.el-select__input')
            if await search.count():
                await search.fill('')
            choices = popup.locator('.el-select-dropdown__item')
            await self.wait(lambda: choices.count(), f'{label}未取得页面候选')
            options = await choices.evaluate_all('''es => es.map(e => ({name: e.innerText.trim(),
                disabled: e.classList.contains('is-disabled')}))''')
            await self.close_select_dropdown(el, 'el', popup)
        else:
            raise DewuError(f'{label}没有唯一可识别的属性控件')
        options = tuple(o for o in options if o.get('name') and not o.get('disabled'))
        if not options:
            raise DewuError(f'{label}没有可选择的页面候选')
        runtime = self.attribute_runtime
        async def resolve(*, error='', force=False):
            if runtime is not None:
                return await runtime.resolve_unmatched_page_options(
                    platform_id='dewu', category_leaf_id='path:' + canonical_sha256(self.product.category)[:20],
                    field_label=label, excel_value=value, options=options, multi=multi,
                    write_error=error, force_review=force)
            candidates = tuple(CandidateValue(o['name'], o['name']) for o in options)
            matches = _matched_excel_variants(value, candidates, field_label=label,
                                              control_type='multi_select' if multi else 'select')
            if not matches:
                raise DewuError(f'{label}原值“{value}”未匹配页面候选，请开启属性学习与审核后继续')
            return SimpleNamespace(label=','.join(c.label for c in matches[0]), is_no_fill=False, source='explicit_text')
        resolved = await resolve()
        for attempt in range(2):
            if resolved is None:
                return None
            if resolved.is_no_fill:
                await self.clear_attribute(scope, label)
                return None
            values = split_multi_choice(resolved.label) if multi else (resolved.label,)
            try:
                names = [o['name'] for o in options]
                if not values or any(names.count(v) != 1 for v in values):
                    raise DewuError(f'{label}判断结果不在当前页面候选中')
                if await ant.count() == 1 and multi:
                    await self.clear_attribute(scope, label)
                    popup = await self.open_select_dropdown(ant, 'ant', multi=True)
                    for part in values:
                        choice = popup.locator('.ant-select-item-option').filter(
                            has=self.page.locator('.ant-select-item-option-content').filter(has_text=exact(part)))
                        await (await self.unique(choice, label + ':' + part)).click()
                    await self.close_select_dropdown(ant, 'ant', popup)
                    actual = await ant.locator('.ant-select-selection-item').evaluate_all(
                        'es=>es.map(e=>e.getAttribute("title")||e.innerText.trim())')
                    if set(actual) != set(values):
                        raise DewuError(f'{label}多选回读不一致：{actual}')
                else:
                    await self.select(scope, resolved.label, label, selected_values=values)
                self.report.setdefault('attribute_choices', {})[label] = {
                    'excel_value': value, 'selected': list(values), 'source': resolved.source,
                    'options': [o['name'] for o in options], 'top_level': await self.item(label).count() == 1}
                return resolved.label
            except Exception as exc:
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                if runtime is None or not isinstance(exc, (DewuError, PlaywrightTimeoutError)):
                    raise
                resolved = await resolve(error=str(exc), force=attempt == 1)
        if resolved is not None:
            raise DewuError(f'{label}重试后仍未通过页面回读')
        return None

    async def open_attribute_select(self, el, multi):
        # Selected tags cover the text input on Dewu's multi-select. The
        # arrow remains clickable even when tags collapse into "+ n".
        return await self.open_select_dropdown(el, 'el', multi=multi)

    async def clear_attribute(self, scope, label):
        el, ant = scope.locator('.el-select'), scope.locator('.ant-select')
        if await el.count() == 1:
            tags = el.locator('.el-select__tags .el-tag__close')
            for _ in range(100):
                if not await tags.count():
                    break
                await tags.first.click()
            if await el.locator('.el-select__tags').count():
                if await tags.count():
                    raise DewuError(f'{label}旧多选值未清空')
                return
            if not await el.locator('.el-input__inner').input_value():
                return
            await el.hover()
            clear = el.locator('.el-select__caret.el-icon-circle-close:visible')
        elif await ant.count() == 1:
            if not await ant.locator('.ant-select-selection-item').count():
                return
            await ant.hover()
            clear = ant.locator('.ant-select-clear:visible')
        else:
            if not await scope.locator('input:checked').count():
                return
            raise DewuError(f'{label}已确认不填写，但当前已选值无法清空')
        await (await self.unique(clear, label + '清空')).click()
        if await el.count() == 1:
            remaining = await el.locator('.el-input__inner').input_value()
        else:
            remaining = await ant.locator('.ant-select-selection-item').count()
        if remaining:
            raise DewuError(f'{label}清空后回读不一致')

    async def verify_saved_attributes(self):
        """Only saved, reopened values become verified learning evidence."""
        choices = self.report.get('attribute_choices', {})
        # Component rows such as material composition need their own row key;
        # only unambiguous top-level fields can be reopened by form label.
        fields = {label: choice for label, choice in choices.items() if choice.get('top_level')}
        if not fields:
            return
        await self.page.goto(self.report['draft_url'], wait_until='domcontentloaded')
        await self.page.get_by_placeholder('卖点提炼', exact=True).filter(visible=True).wait_for(
            state='visible', timeout=self.timeout * 1000)
        readback = {}
        for label, choice in fields.items():
            scope = await self.unique(self.item(label), label)
            radios = scope.locator('label.el-radio, label.ant-radio-wrapper')
            el, ant = scope.locator('.el-select'), scope.locator('.ant-select')
            if await radios.count():
                actual = await radios.evaluate_all('''es=>es.filter(e=>e.querySelector('input:checked'))
                    .map(e=>e.innerText.trim())''')
            elif await el.count() == 1:
                if await el.locator('.el-select__tags').count():
                    popup = await self.open_attribute_select(el, True)
                    search = el.locator('input.el-select__input')
                    if await search.count():
                        await search.fill('')
                    selected = popup.locator('.el-select-dropdown__item.selected')
                    await self.wait(lambda: selected.count(), f'{label}保存后未取得已选值')
                    actual = [s.strip() for s in await selected.all_text_contents()]
                    await self.close_select_dropdown(el, 'el', popup)
                else:
                    actual = [await el.locator('.el-input__inner').input_value()]
            elif await ant.count() == 1:
                actual = await ant.locator('.ant-select-selection-item').evaluate_all(
                    'es=>es.map(e=>e.getAttribute("title")||e.innerText.trim())')
            else:
                raise DewuError(f'{label}保存后未找到原属性控件')
            if sorted(actual) != sorted(choice['selected']):
                raise DewuError(f'{label}保存后回读不一致：{actual}')
            readback[label] = actual
        self.report['steps']['attribute_readback'] = readback
        self.attribute_runtime.record_verified_readbacks('dewu', readback)

    async def fill_field(self, label, value, *, learn=False):
        item = await self.unique(self.item(label), label)
        if await item.locator('.el-select,.ant-select,label.el-radio,label.ant-radio-wrapper').count():
            return await (self.select_attribute(item, value, label) if learn else self.select(item, value, label))
        return await self.set_input(item.locator('input:not([type=hidden]),textarea'), value, label)

    async def upload(self, scope, images, label):
        """Select one file at a time to preserve order even on concurrent uploaders."""
        images = tuple(images)
        existing = await self.image_urls(scope)
        recorded = self.report.setdefault('uploads', {}).get(label, [])
        if existing:
            valid = (len(existing) <= len(recorded) <= len(images)
                     and existing == [entry['url'] for entry in recorded[:len(existing)]]
                     and [str(p) for p in images[:len(existing)]] == [entry['path'] for entry in recorded[:len(existing)]]
                     and [image_fingerprint(p) for p in images[:len(existing)]] == [entry.get('sha256') for entry in recorded[:len(existing)]])
            if not valid:
                raise DewuError(f'{label}已有未匹配的图片，请核对后清空该区域再重试')
        self.report['uploads'][label] = recorded[:len(existing)]
        for image in images[len(existing):]:
            inputs = scope.locator('input[type=file][accept*=".jpg"],input[type=file][accept*="image"]')
            if await inputs.count() > 1:
                # A carousel has dedicated front/back/model slots and one
                # general upload slot. Append through the general slot only.
                generic = scope.locator('.el-upload').filter(
                    has=self.page.get_by_text('上传图片', exact=True))
                inputs = generic.locator('input[type=file]')
            if await inputs.count() != 1:
                raise DewuError(f'{label}没有唯一的本地上传控件')
            before = await scope.locator('.el-upload-list > li').count()
            self.logger.info('得物 %s：上传 %s（%s/%s）', label, image.name, before + 1, len(images))
            await inputs.set_input_files(str(image))
            async def uploaded():
                errors = scope.locator('.ant-upload-list-item-error,.el-upload-list__item.is-fail')
                if await errors.count():
                    raise DewuError(f'{label}上传失败：{image.name}')
                cards = scope.locator('.el-upload-list > li')
                if await cards.count() != before + 1:
                    return False
                complete = await cards.last.evaluate('''e => {
                    const img = e.querySelector('img');
                    const busy = Array.from(e.querySelectorAll('.is-uploading,.el-progress,.el-loading-mask'))
                        .some(n => n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden');
                    return e.classList.contains('is-success') && img && img.complete && img.naturalWidth > 0
                      && !busy;
                }''')
                if not complete:
                    return False
                # The preview can remain a blob after the upload completes.
                # Persist the remote identity so a reopened draft can resume.
                urls = await self.image_urls(scope)
                return len(urls) == before + 1 and all(urls)
            await self.wait(uploaded, f'{label}上传后未收到完成确认：{image.name}', timeout=self.upload_timeout)
            urls = await self.image_urls(scope)
            self.report['uploads'][label].append({'path': str(image), 'url': urls[-1], 'sha256': image_fingerprint(image)})
            self.persist()

    async def image_urls(self, scope):
        return await scope.locator('.el-upload-list > li').evaluate_all('''es => es.map(e => {
            const remote = value => typeof value === 'string' && /^https?:[/][/]/.test(value)
                ? value.split('?')[0] : '';
            const wrapper = e.closest('.du-upload');
            const index = wrapper ? Array.from(wrapper.querySelectorAll('.el-upload-list > li')).indexOf(e) : -1;
            const file = wrapper?.__vue__?.fileList?.[index];
            const uploaded = remote(file?.url);
            if (uploaded) return uploaded;
            const image = e.querySelector('img');
            return remote(image?.src);
        })''')

    async def confirm(self, scope):
        button = scope.get_by_role('button', name=re.compile(r'^\s*确\s*定\s*$'))
        await (await self.unique(button, '确定')).click()

    async def create(self):
        p = self.product
        await self.fill_field('商品品牌', p.brand)
        category = await self.unique(self.item('商品类目'), '商品类目')
        search = category.locator('.el-cascader input')
        await self.wait(lambda: search.is_enabled(), '选择品牌后类目未加载完成')
        # The control enables before its asynchronous category tree arrives.
        # Selecting a recent path before that response is discarded by Vue.
        await search.click()
        await self.wait(lambda: self.page.locator(
            '.el-cascader__dropdown:visible .el-cascader-node').count(), '品牌类目树未加载完成')
        await search.press('Escape')
        expected = '>>'.join(p.category)
        recent = category.get_by_text('/'.join(p.category), exact=True)
        if await recent.count() == 1:
            await recent.click()
        else:
            await search.click()
            for depth, name in enumerate(p.category):
                menu = self.page.locator('.el-cascader__dropdown:visible .el-cascader-menu').nth(depth)
                node = menu.locator('.el-cascader-node').filter(
                    has=self.page.locator('.el-cascader-node__label').filter(has_text=exact(name)))
                await (await self.unique(node, '类目 ' + name)).click()
        async def category_selected():
            return re.sub(r'\s+', '', await search.input_value()) == expected
        await self.wait(category_selected, '类目回读不一致：' + expected)
        await self.fill_field('适用人群', p.audience)
        await self.upload(await self.unique(self.item('商品图片'), '商品图片'), p.initial_images, '商品图片')
        await self.set_input(self.item('商品链接').locator('input[type=text]'), p.link, '商品链接')
        radio = self.page.locator('label.el-radio').filter(has_text=exact('生成草稿'))
        await radio.click()
        if not await radio.locator('input').is_checked():
            raise DewuError('未选中“生成草稿”，停止创建')
        self.report['steps']['start'] = {'category': list(p.category), 'images': [str(i) for i in p.initial_images], 'mode': '生成草稿'}
        self.report['creation_attempted'] = True
        self.persist()
        await self.page.get_by_role('button', name='创建新品发布申请', exact=True).click()
        # Never retry this action. A timeout may already have created a draft.
        await self.wait(lambda: self._draft_open(), '创建后未进入草稿页，请检查平台草稿列表，勿直接重复创建', timeout=self.upload_timeout)
        self.report['draft_url'] = self.page.url
        self.persist()

    async def _draft_open(self):
        return valid_draft_url(self.page.url)

    def persist(self):
        write_json(self.directory / 'dewu-result.json', self.report)
        if self.on_checkpoint:
            self.on_checkpoint(self.report)

    async def form_validation_errors(self):
        errors = await self.page.locator(
            '.el-form-item__error:visible,.ant-form-item-explain-error:visible').all_text_contents()
        cards = self.page.locator('.item-card')
        for index in range(await cards.count()):
            card = cards.nth(index)
            notices = card.get_by_text(re.compile(r'^图片中有违禁词[，,]\s*请重新上传[!！]?$')).filter(visible=True)
            if await notices.count():
                title = card.locator('.title')
                label = (await title.inner_text()).strip() if await title.count() == 1 else '图片'
                errors.extend(label + '：' + text.strip() for text in await notices.all_text_contents())
        return list(dict.fromkeys(error.strip() for error in errors if error.strip()))

    async def submit_for_review(self):
        receipt = {}
        pending = []
        def is_submission(request):
            parsed = urlsplit(request.url)
            return (parsed.scheme == 'https' and parsed.hostname == 'stark.dewu.com'
                    and parsed.path == REVIEW_ENDPOINT and request.method == 'POST')
        def requested(request):
            if is_submission(request):
                self.report['review_submission_attempted'] = True
                self.persist()
        async def read_response(response):
            receipt.update(endpoint=REVIEW_ENDPOINT, http_status=response.status)
            try:
                body = await response.json()
                receipt.update(code=body.get('code'), message=str(body.get('msg') or '')[:500])
                data = body.get('data')
                if isinstance(data, dict) and data.get('newApplyId'):
                    receipt['application_id'] = str(data['newApplyId'])
            except Exception:
                receipt['invalid_response'] = True
            receipt['complete'] = True
        def responded(response):
            if is_submission(response.request):
                pending.append(asyncio.create_task(read_response(response)))
        self.page.on('request', requested)
        self.page.on('response', responded)
        try:
            await self.page.get_by_role('button', name='提交审核', exact=True).click()
            continued = False
            async def completed():
                nonlocal continued
                if receipt.get('complete'):
                    self.report['review_receipt'] = {k: v for k, v in receipt.items() if k != 'complete'}
                    if not (200 <= receipt['http_status'] < 300 and receipt.get('code') == 200):
                        self.report['review_submission_rejected'] = (
                            200 <= receipt['http_status'] < 300 and receipt.get('code') is not None
                            and receipt['code'] != 200)
                        raise DewuError('提交得物审核失败：' + (receipt.get('message') or
                            f"HTTP {receipt['http_status']}，业务码 {receipt.get('code')}"))
                    return True
                errors = await self.form_validation_errors()
                if errors:
                    raise DewuError('得物审核校验未通过：' + '；'.join(errors))
                # Dewu may show a pre-audit confirmation before the final request.
                dialogs = self.page.locator('.el-dialog:visible,.ant-modal-content:visible,[role=dialog]:visible')
                continuation = dialogs.get_by_role('button', name=exact('继续提交'))
                if not continued and await continuation.count() == 1:
                    await continuation.click()
                    continued = True
                return False
            await self.wait(completed, '未取得得物审核提交成功回执；请检查页面提示或审核记录，勿重复提交', timeout=self.upload_timeout)
            self.report.update(submit_review=True, status='submitted_for_review')
        finally:
            self.page.remove_listener('request', requested)
            self.page.remove_listener('response', responded)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def save_draft(self):
        receipt = {}
        pending = []
        async def read_response(response):
            receipt.update(endpoint=urlsplit(response.url).path, http_status=response.status)
            try:
                body = await response.json()
                receipt.update(code=body.get('code'), message=str(body.get('msg') or '')[:500])
            except Exception:
                receipt['invalid_response'] = True
            receipt['complete'] = True
        def saved_response(response):
            url = urlsplit(response.url)
            if (url.hostname == 'stark.dewu.com'
                    and url.path == '/api/v1/h5/biz/nps/saveOrUpdateDraft'
                    and response.request.method == 'POST'):
                pending.append(asyncio.create_task(read_response(response)))
        self.page.on('response', saved_response)
        try:
            await self.page.get_by_role('button', name='保存草稿', exact=True).click()
            success = self.page.locator('.el-message--success:visible,.ant-message-success:visible').filter(
                has_text=re.compile('保存成功|草稿.*成功|成功.*草稿'))
            async def completed():
                if receipt.get('complete'):
                    self.report['save_receipt'] = {k: v for k, v in receipt.items() if k != 'complete'}
                    if not (200 <= receipt['http_status'] < 300 and receipt.get('code') == 200):
                        raise DewuError('保存得物草稿失败：' + (receipt.get('message') or str(self.report['save_receipt'])))
                elif pending:
                    return False
                else:
                    errors = [error for error in await self.form_validation_errors()
                              if not error.startswith('商品货号已存在')]
                    if errors:
                        self.report['validation_errors'] = errors
                        raise DewuError('保存得物草稿校验未通过：' + '；'.join(errors))
                if await success.count():
                    return True
                url = urlsplit(self.page.url)
                # Dewu uses the same result screen wording for several actions.
                # Only accept it after the observed draft-save endpoint succeeded.
                return (receipt.get('complete') and receipt.get('code') == 200
                        and 200 <= receipt.get('http_status', 0) < 300
                        and url.hostname == 'stark.dewu.com'
                        and url.path == '/main/newProductApply/spuEdit/result'
                        and parse_qs(url.query).get('preType') == ['2'])
            await self.wait(completed, '保存草稿后未收到成功响应或确认页面')
            if receipt:
                self.report['save_receipt'] = {**{k: v for k, v in receipt.items() if k != 'complete'},
                                               'result_url': self.page.url}
        finally:
            self.page.remove_listener('response', saved_response)
            if pending:
                await asyncio.gather(*pending)

    async def basic(self):
        p = self.product
        await self.set_input(self.page.get_by_placeholder('卖点提炼', exact=True).filter(visible=True), p.selling_point, '卖点提炼')
        await self.set_input(self.page.get_by_placeholder('类目', exact=True).filter(visible=True), p.category[-1], '结构化标题类目')
        await self.fill_field('货号', p.style_code)
        await self.select(await self.unique(self.item('发售价格'), '发售价格'), '有发售价格', '发售价格方式')
        await self.set_input(self.page.get_by_placeholder('请输入发售价格', exact=True), p.release_price, '发售价格')
        await self.fill_field('外网链接', p.link)
        attributes = {}
        # Match the same dynamic Excel field names as other platforms, using only
        # actual form labels. The AI sidebar is intentionally outside these items.
        labels = await self.page.locator('.el-form-item > label.el-form-item__label:visible').all_text_contents()
        skipped = {'商品标题','结构化标题','品牌','适用人群','货号','发售价格','颜色','尺码','外网链接','成分含量'}
        aliases = {'发售价证明渠道来源': ('发售价格证明渠道来源',), '发售价资料证明链接': ('发售价格资料证明链接',)}
        for raw in dict.fromkeys(labels):
            label = raw.strip().lstrip('*').strip()
            if label in skipped:
                continue
            match = field_lookup(p.fields, label, *aliases.get(label, ()))
            if match and str(match[1]).strip():
                attributes[label] = await self.fill_field(label, str(match[1]).strip(), learn=True)
        match = field_lookup(p.fields, '成分含量', '材质成分')
        if match:
            await self.materials(str(match[1]))
            attributes['成分含量'] = str(match[1])
        self.report['steps']['basic'] = {'attributes': attributes, 'release_price': p.release_price,
                                         'style_code': p.style_code, 'title_category': p.category[-1]}

    async def materials(self, text):
        scope = await self.unique(self.item('成分含量'), '成分含量')
        parts = [p.strip() for p in re.split(r'[,，、;；/\n]+', text) if p.strip()]
        parsed = []
        for part in parts:
            match = re.fullmatch(r'([^\d%]+?)\s*(\d+(?:\.\d+)?)\s*%', part)
            if not match:
                raise DewuError(f'成分含量须包含明确百分比：{part}')
            parsed.append((match[1].strip(), match[2]))
        if sum(float(p[1]) for p in parsed) != 100:
            raise DewuError('成分含量百分比之和须为 100%')
        for index, (name, percentage) in enumerate(parsed):
            rows = scope.locator('[class*="_materialRow_"]')
            if await rows.count() <= index:
                await scope.get_by_role('button', name='新增', exact=True).click()
            row = rows.nth(index)
            await self.select_attribute(row, name, '材质')
            await self.set_input(row.locator('input[role=spinbutton]'), percentage, '成分比例')
        rows = scope.locator('[class*="_materialRow_"]')
        while await rows.count() > len(parsed):
            delete = rows.last.get_by_role('button', name='删除', exact=True)
            await (await self.unique(delete, '多余成分删除')).click()

    async def colors(self):
        scope = await self.unique(self.item('颜色'), '颜色规格')
        inputs = scope.locator('input[placeholder="请选择或输入标准色"]')
        for index, color in enumerate(self.product.colors):
            if await inputs.count() <= index:
                await scope.get_by_role('button', name='新增', exact=True).last.click()
            await self.set_input(inputs.nth(index), color, '颜色')
        # Typing a color inserts another row. Remove only rows after the expected
        # colors, never a populated desired color or the entire specification.
        for _ in range(20):
            if await inputs.count() <= len(self.product.colors):
                break
            extra = inputs.nth(len(self.product.colors))
            if await extra.input_value():
                raise DewuError('出现非空的额外颜色，请核对后继续')
            row = scope.locator('.singleDragItem').nth(len(self.product.colors))
            await row.get_by_role('button', name='删除', exact=True).click()
        actual = await inputs.evaluate_all('es=>es.map(e=>e.value)')
        if actual != list(self.product.colors):
            raise DewuError(f'颜色数量/顺序回读不一致：{actual}')
        self.report['steps']['colors'] = actual

    async def size_chart(self):
        scope = await self.unique(self.item('尺码'), '尺码')
        add = scope.get_by_role('button', name=re.compile('添加尺码表'))
        if await add.count():
            await add.click()
        else:
            await scope.locator('.size-box').get_by_role('button', name='编辑', exact=True).click()
        drawer = self.page.locator('.ant-drawer-content:visible').filter(has_text='添加尺码表')
        await self.unique(drawer, '添加尺码表')
        if any('裤' in name for name in self.product.category):
            template = drawer.locator('[class*="_templateSelect_"]')
            await self.select_container(template, '通用裤装模板', '尺码模板')
        # Template and fit are independent Ant selects; use the fit's placeholder
        # or selected text to avoid a numeric rc_select id.
        fit = drawer.locator('.ant-select:not([class*="_templateSelect_"])')
        await self.select_container(fit, '正码', '偏码说明')
        await drawer.get_by_role('button', name=re.compile('图片解析')).click()
        modal = self.page.locator('.ant-modal-content:visible').filter(has_text='上传图片生成尺码表')
        modal = await self.unique(modal, '上传图片生成尺码表')
        # The parser's upload control differs from the product material picker.
        inputs = modal.locator('input[type=file]')
        if await inputs.count() == 1:
            await inputs.set_input_files(str(self.product.size_image))
        else:
            async with self.page.expect_file_chooser() as chooser:
                await modal.get_by_role('button', name='本地上传', exact=True).click()
            await (await chooser.value).set_files(str(self.product.size_image))
        generate = modal.get_by_role('button', name='生成尺码表', exact=True)
        await generate.wait_for(state='visible')
        await generate.click()
        rows = modal.locator('tbody tr').filter(has=self.page.locator('input.ant-input'))
        await self.wait(lambda: rows.count(), '得物图片解析未生成尺码数据', timeout=self.upload_timeout)
        parsed = await self.chart_snapshot(modal)
        await self.normalize_chart_columns(modal)
        await self.validate_chart(modal, expected=parsed)
        await self.confirm(modal)
        await modal.wait_for(state='hidden')
        # Platform adds mandatory Chinese columns when merging OCR results.
        await self.validate_chart(drawer, expected=parsed)
        await self.confirm(drawer)
        await drawer.wait_for(state='hidden')
        all_sizes = scope.locator('.el-checkbox.select-all')
        if not await all_sizes.locator('input').is_checked():
            await all_sizes.click()
        if not await all_sizes.locator('input').is_checked():
            raise DewuError('尺码全选未生效')
        actual = await scope.locator('.el-checkbox-group input:checked').evaluate_all(
            'es=>es.map(e=>e.closest("label").innerText.trim())')
        if actual != list(self.product.sizes):
            raise DewuError(f'尺码全选回读与 Excel 不一致：{actual}')
        self.report['steps']['size_chart'] = {**parsed, 'fit': '正码', 'image': str(self.product.size_image)}

    async def select_container(self, control, value, label):
        # select() expects a containing element, not the select itself.
        await self.unique(control, label)
        await control.locator('.ant-select-selector').click()
        search = control.locator('input:not([readonly])')
        if await search.count():
            await search.fill(value)
        option = self.page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter(
            has=self.page.get_by_text(value, exact=True))
        await (await self.unique(option, label + ':' + value)).click()
        if value not in await control.locator('.ant-select-selection-item').inner_text():
            raise DewuError(f'{label}回读失败')

    async def normalize_chart_columns(self, modal):
        original = await self.chart_snapshot(modal)
        labels = modal.locator('label.ant-checkbox-wrapper').filter(
            has_text=re.compile(r'[\u4e00-\u9fff].*[A-Za-z].*[（(]cm[）)]', re.I))
        # Editing each OCR parameter preserves its numeric column; unchecking it
        # would discard the measurements instead of renaming them.
        initial = await labels.all_text_contents()
        for label in initial:
            old = label.strip()
            if '区间值' in old:
                continue
            name = chinese_measurement(old)
            checkbox = modal.locator('label.ant-checkbox-wrapper').filter(has_text=exact(old))
            if await checkbox.count() != 1:
                raise DewuError(f'尺码参数匹配不唯一：{old}')
            parameter = checkbox.locator('xpath=..')
            edit = parameter.locator('[aria-label=edit]')
            if await edit.count() != 1:
                raise DewuError(f'尺码参数“{old}”缺少编辑入口')
            await edit.click()
            editor = self.page.locator('.ant-modal-content:visible').filter(has=self.page.locator('input#name'))
            await self.unique(editor, '编辑尺码参数')
            await self.set_input(editor.locator('input#name'), name, '尺码参数中文名')
            await self.confirm(editor)
            async def rename_confirmed():
                standard = self.page.locator('.ant-modal-content:visible').filter(
                    has_text=f'系统已自动匹配平台标准参数【{name}】')
                if await standard.count() == 1:
                    await self.confirm(standard)
                    await standard.wait_for(state='hidden')
                return not await editor.is_visible()
            await self.wait(rename_confirmed, f'尺码参数“{name}”重命名未完成')
            await editor.wait_for(state='hidden')
            await modal.locator('label.ant-checkbox-wrapper').filter(has_text=exact(name + '(cm)')).wait_for(state='visible')
        await self.validate_chart(modal, expected=original)

    async def chart_snapshot(self, scope):
        tables = await scope.locator('table').evaluate_all('''tables => tables.map(table => ({
            labels: Array.from(table.querySelectorAll('thead th')).map(e=>e.innerText.trim()),
            rows: Array.from(table.querySelectorAll('tbody tr')).map(r=>
                Array.from(r.querySelectorAll('input.ant-input')).map(e=>e.value.trim())).filter(r=>r.length)
        })).filter(t=>t.rows.length)''')
        if len(tables) != 1:
            raise DewuError('尺码表必须有且仅有一个非空数据表')
        table = tables[0]
        data, labels = table['rows'], table['labels']
        if not data or tuple(row[0] for row in data) != self.product.sizes:
            raise DewuError(f'图片解析尺码与 Excel 不一致：{[r[0] for r in data]}')
        if len(labels) < 3 or any(len(row) != len(labels) - 1 for row in data):
            raise DewuError('尺码表测量列与表头不一致')
        names = [chinese_measurement(label.split('区间值')[0]) for label in labels[1:-1]]
        if len(names) != len(set(names)):
            raise DewuError('尺码表有重复测量列，请核对中文参数')
        for row in data:
            for value in row[1:]:
                try:
                    number = Decimal(value)
                except InvalidOperation as exc:
                    raise DewuError(f'尺码表存在空或非数字测量值：{value!r}') from exc
                if not number.is_finite() or number <= 0:
                    raise DewuError(f'尺码表测量值必须是有效正数：{value!r}')
        return {'sizes': [row[0] for row in data],
                'measurements': {name: [row[index + 1] for row in data] for index, name in enumerate(names)}}

    async def validate_chart(self, scope, expected=None):
        data = await self.chart_snapshot(scope)
        labels = await scope.locator('thead th').all_text_contents()
        if any(re.search(r'[A-Za-z]', re.sub(r'cm', '', name, flags=re.I)) for name in labels[1:-1]):
            raise DewuError(f'尺码参数仍包含英文：{labels}')
        if expected is not None and data != expected:
            raise DewuError('尺码参数重命名或合并后数值发生变化，停止确认')
        return data

    async def recommendations(self):
        scope = await self.unique(self.item('尺码'), '尺码')
        button = scope.get_by_role('button', name=re.compile('添加尺码推荐'))
        if await button.count():
            await button.click()
        else:
            box = scope.locator('.size-box').filter(has_text='尺码推荐')
            await (await self.unique(box.get_by_role('button', name='编辑', exact=True), '尺码推荐编辑')).click()
        drawer = self.page.locator('.size-recommend-drawer:visible')
        await self.unique(drawer, '尺码推荐')
        await self.confirm(drawer)
        await drawer.wait_for(state='hidden')
        add = scope.get_by_role('button', name=re.compile('添加试穿报告'))
        if await add.count():
            await add.click()
        else:
            box = scope.locator('.size-box').filter(has_text='试穿报告')
            await (await self.unique(box.get_by_role('button', name='编辑', exact=True), '试穿报告编辑')).click()
        drawer = self.page.locator('.sell-report-drawer:visible')
        await self.unique(drawer, '试穿报告')
        await self.select(drawer, '钊叔试穿模版', '试穿模版')
        await self.confirm(drawer)
        await drawer.wait_for(state='hidden')
        await scope.get_by_role('button', name='钊叔试穿模版', exact=True).wait_for(state='visible')
        self.report['steps']['recommendations'] = {'size_recommendation': True, 'try_on_template': '钊叔试穿模版'}

    async def sku_batch(self):
        p = self.product
        # Batch form IDs come from the platform's schema; avoid positional inputs.
        await self.set_input(self.page.locator('input#merchantSkuNo'), p.sku_code, '批量商品编码')
        await self.set_input(self.page.locator('input#bidPrice'), p.bid_price, '批量出价')
        await self.set_input(self.page.locator('input#stock'), p.stock, '批量库存')
        bid = self.page.locator('.ant-select').filter(has=self.page.locator('input#biddingCode'))
        state = self.page.locator('.ant-select').filter(has=self.page.locator('input#status'))
        await self.select_container(bid, p.bid_type, '出价类型')
        await self.select_container(state, p.listed, '上下架')
        await self.page.get_by_role('button', name='批量设置', exact=True).click()
        await self.update_sku_codes(check_batch=True)

    async def sku_table(self):
        table = self.page.locator('.el-table').filter(has=self.page.locator(
            '.el-table__header-wrapper th').filter(has_text=re.compile(r'^\s*商品编码(?:\s|$)')))
        return await self.unique(table, 'SKU 表格')

    async def sku_snapshot(self, table):
        return await table.evaluate('''table => {
            const headers = Array.from(table.querySelectorAll('.el-table__header-wrapper th'));
            const label = e => e.innerText.trim();
            const fixed = Array.from(table.querySelectorAll('.el-table__fixed-right .el-table__row'));
            return Array.from(table.querySelectorAll('.el-table__body-wrapper tr.el-table__row')).map((row,ri) => {
                const cells = Array.from(row.children).filter(e=>e.tagName==='TD');
                const result = {};
                headers.forEach((h,i)=>{
                    const column = Array.from(h.classList).find(c=>/^el-table_.*_column_/.test(c));
                    const cell = (column && cells.find(c=>c.classList.contains(column))) || cells[i];
                    if (!cell) return;
                    const input = cell.querySelector('input:not([type=checkbox])');
                    result[label(h)] = input ? input.value : cell.innerText.trim();
                });
                const toggle = row.querySelector('[role=switch]') || fixed[ri]?.querySelector('[role=switch]');
                result.listed = toggle?.getAttribute('aria-checked') === 'true';
                return result;
            });
        }''')

    async def sku_column(self, table, label):
        headers = await table.locator('.el-table__header-wrapper th').evaluate_all(
            'es=>es.map(e=>({label:e.innerText.trim(),classes:Array.from(e.classList)}))')
        matches = [(index, h) for index, h in enumerate(headers) if h['label'] == label]
        if len(matches) != 1:
            raise DewuError('SKU 列不唯一：' + label)
        index, header = matches[0]
        column = next((c for c in header['classes'] if re.fullmatch(r'el-table_\w+_column_\w+', c)), '')
        return 'td.' + column if column else f'td:nth-child({index + 1})'

    async def sku_pages(self):
        table = await self.sku_table()
        pagination = self.page.locator('.el-pagination:visible')
        if await pagination.count():
            await self.unique(pagination, 'SKU 分页')
            first = pagination.locator('.el-pager li.number').filter(has_text=exact('1'))
            if await first.count() == 1:
                old_page = await pagination.locator('.el-pager .active').inner_text()
                old_rows = await self.sku_snapshot(table)
                await first.click()
                async def first_page_ready():
                    if not await pagination.locator('.el-pager .active').filter(has_text=exact('1')).count():
                        return False
                    rows = await self.sku_snapshot(table)
                    return rows and (old_page.strip() == '1' or
                        [(r.get('颜色'), r.get('尺码')) for r in rows] !=
                        [(r.get('颜色'), r.get('尺码')) for r in old_rows])
                await self.wait(first_page_ready, 'SKU 未切回第一页')
        expected = {(color, size) for color in self.product.colors for size in self.product.sizes}
        seen = set()
        for _ in range(len(expected) + 1):
            current = await self.wait(lambda: self.sku_snapshot(table), 'SKU 表格未加载')
            pairs = [(r.get('颜色'), r.get('尺码')) for r in current]
            if (len(set(pairs)) != len(pairs) or set(pairs) & seen
                    or not set(pairs) <= expected):
                raise DewuError(f'SKU 颜色/尺码组合重复或与 Excel 不一致：{pairs}')
            seen.update(pairs)
            yield table, current
            next_page = pagination.locator('button.btn-next:not([disabled])')
            if not await next_page.count():
                break
            if seen == expected:
                raise DewuError('SKU 存在多余分页，停止保存')
            await (await self.unique(next_page, 'SKU 下一页')).click()
            async def page_changed():
                rows = await self.sku_snapshot(table)
                return rows and [(r.get('颜色'), r.get('尺码')) for r in rows] != pairs
            await self.wait(page_changed, 'SKU 翻页未切换，停止保存')
        if seen != expected:
            raise DewuError(f'SKU 颜色/尺码组合不全：缺少 {sorted(expected - seen)}')

    async def update_sku_codes(self, *, check_batch=False):
        p = self.product
        data = []
        async for table, current in self.sku_pages():
            if check_batch:
                async def batch_applied():
                    rows = await self.sku_snapshot(table)
                    return rows if rows and all(
                        row.get('商品编码') == p.sku_code and row.get('出价类型') == p.bid_type
                        and same_number(row.get('出价'), p.bid_price) and same_number(row.get('库存'), p.stock)
                        and row['listed'] == (p.listed == '上架') for row in rows) else False
                current = await self.wait(batch_applied, 'SKU 批量设置后编码/出价类型/出价/库存/上下架回读不一致')
            color_column = await self.sku_column(table, '颜色')
            size_column = await self.sku_column(table, '尺码')
            code_column = await self.sku_column(table, '商品编码')
            for row in current:
                color, size = row['颜色'], row['尺码']
                sku = table.locator('.el-table__body-wrapper tr.el-table__row').filter(
                    has=self.page.locator(color_column).filter(has_text=exact(color))).filter(
                    has=self.page.locator(size_column).filter(has_text=exact(size)))
                code = sku.locator(code_column).locator('input:not([type=hidden])')
                await self.set_input(code, p.sku_code + size, f'{color}/{size} 商品编码')
            updated = await self.sku_snapshot(table)
            # Dewu calculates estimated income asynchronously after batch pricing.
            # It can refresh while editing codes; all configured fields must stay intact.
            derived = {'商品编码', '预计收入'}
            before = {(r['颜色'], r['尺码']): {k: v for k, v in r.items() if k not in derived} for r in current}
            after = {(r.get('颜色'), r.get('尺码')): {k: v for k, v in r.items() if k not in derived} for r in updated}
            if (len(updated) != len(current) or before != after
                    or any(r.get('商品编码') != p.sku_code + r['尺码'] for r in updated)):
                raise DewuError('SKU 编码回读不一致或其他字段发生变化，停止保存')
            data.extend(updated)
        if not sku_codes_match(p, data):
            raise DewuError('SKU 编码与颜色/尺码组合回读不一致，停止保存')
        self.report['steps']['sku'] = data

    async def detail_images(self):
        counts = {}
        for label, images in split_detail_images(self.product.detail_images).items():
            card = self.page.locator('.item-card').filter(
                has=self.page.locator('.title').filter(has_text=exact(label)))
            await self.unique(card, label)
            # upload() only resumes image sequences with recorded URL identities.
            await self.upload(card, images, label)
            await self.wait(lambda: self.detail_count_matches(card, len(images)), f'{label}上传数量未达到 {len(images)}')
            counts[label] = len(images)
        self.report['steps']['details'] = counts

    async def detail_count(self, card):
        text = await card.locator('.description').inner_text()
        match = re.search(r'已上传\s*(\d+)\s*/', text)
        if not match:
            raise DewuError('详情图区域缺少已上传数量')
        return int(match[1])

    async def detail_count_matches(self, card, expected):
        return await self.detail_count(card) == expected

    async def verify_carousel(self):
        uploaded = self.report.get('uploads', {}).get('商品图片', [])
        expected = [entry['url'] for entry in uploaded]
        if len(expected) != len(self.product.initial_images):
            raise DewuError('缺少创建时商品图片记录，无法核对当前轮播图；请检查草稿和原运行报告')
        if [image_fingerprint(p) for p in self.product.initial_images] != [entry.get('sha256') for entry in uploaded]:
            raise DewuError('商品图片内容已变更，请核对原草稿的轮播图后重新填写')
        scope = await self.unique(self.item('商品轮播图'), '商品轮播图')
        groups = scope.locator('.du-upload')
        if not await groups.count():
            raise DewuError('未找到商品轮播图上传区域')
        result = []
        for index in range(await groups.count()):
            group = groups.nth(index)
            current = await self.image_urls(group)
            label = f'商品轮播图-{index + 1}'
            recorded = self.report['uploads'].get(label)
            if not recorded:
                if current != expected[:len(current)]:
                    raise DewuError('商品轮播图与创建时图片顺序不一致，请核对草稿')
                self.report['uploads'][label] = deepcopy(uploaded[:len(current)])
            # A process checkpoint can precede the final draft save. Reconcile
            # only the exact input/output URLs witnessed by our image editor.
            await self.reconcile_white_backgrounds(group, label)
            # Some drafts carry only the first image from the start page.
            # Append the remaining source images in order, with upload readback.
            await self.upload(group, self.product.initial_images, label)
            result.append(await self.image_urls(group))
        self.report['steps']['carousel'] = {'count': len(expected), 'groups': result}

    async def reconcile_white_backgrounds(self, group, label):
        current = await self.image_urls(group)
        receipts = self.report['steps'].setdefault('white_backgrounds', [])
        recorded = self.report.get('uploads', {}).get(label, [])
        for receipt in receipts[:]:
            if receipt.get('group') != label:
                continue
            index = receipt.get('index')
            paths = self.product.white_images[:2]
            if not (isinstance(index, int) and 0 <= index < len(paths)
                    and index < len(current) and index < len(recorded)
                    and receipt.get('recipe') == WHITE_BACKGROUND_RECIPE
                    and receipt.get('path') == str(paths[index])
                    and receipt.get('sha256') == image_fingerprint(paths[index])):
                raise DewuError('白底图处理记录与原图不一致，请核对草稿')
            if current[index] == receipt.get('result_url'):
                recorded[index] = {**recorded[index], 'url': current[index]}
                receipt['status'] = 'applied'
            elif current[index] == receipt.get('source_url'):
                # The browser closed before the modified draft was saved.
                recorded[index] = {**recorded[index], 'url': current[index]}
                receipts.remove(receipt)
            else:
                raise DewuError('白底图与已记录的原图、生成图均不匹配，请核对草稿')

    async def process_white_backgrounds(self):
        """Use Dewu's native editor on the first two white images, in order."""
        paths = self.product.white_images[:2]
        if tuple(self.product.initial_images[:len(paths)]) != tuple(paths):
            raise DewuError('前两张轮播图必须对应白底图目录中的图片')
        scope = await self.unique(self.item('商品轮播图'), '商品轮播图')
        groups = scope.locator('.du-upload')
        if not await groups.count():
            raise DewuError('未找到商品轮播图上传区域')
        receipts = self.report['steps'].setdefault('white_backgrounds', [])
        for group_index in range(await groups.count()):
            group, label = groups.nth(group_index), f'商品轮播图-{group_index + 1}'
            await self.reconcile_white_backgrounds(group, label)
            for index, path in enumerate(paths):
                before = await self.image_urls(group)
                records = self.report.get('uploads', {}).get(label, [])
                if not (len(before) == len(records) == len(self.product.initial_images)
                        and before == [r['url'] for r in records]
                        and records[index].get('path') == str(path)
                        and records[index].get('sha256') == image_fingerprint(path)):
                    raise DewuError('白底图处理前轮播图身份或顺序校验失败')
                matching = [r for r in receipts if r.get('group') == label and r.get('index') == index]
                if len(matching) > 1:
                    raise DewuError('白底图处理记录重复，请核对草稿')
                if matching and matching[0].get('status') == 'applied':
                    continue
                self.logger.info('得物 %s：第 %s 张抠图（1:1白底图）', label, index + 1)
                card = group.locator('.el-upload-list > li').nth(index)
                await card.locator('.w88_img_wrap').hover()
                await (await self.unique(card.locator('.el-icon-edit').locator('xpath=..'), '图片编辑')).click()
                drawer = await self.unique(self.page.locator('.ant-drawer-content:visible').filter(
                    has_text='图片编辑'), '图片编辑抽屉')
                await drawer.get_by_role('button', name='抠图（1:1白底图）', exact=True).click()
                apply = drawer.get_by_role('button', name='应用生成图', exact=True)
                async def generated():
                    if not await apply.is_enabled():
                        return False
                    images = await drawer.locator('.cropper-canvas img').evaluate_all('''es => es.map(e => ({
                        url: e.src.split('?')[0], loaded: e.complete && e.naturalWidth > 0,
                        width: e.naturalWidth, height: e.naturalHeight}))''')
                    if (len(images) == 1 and images[0]['loaded'] and images[0]['width'] == images[0]['height']
                            and images[0]['url'].startswith('https://') and images[0]['url'] != before[index]):
                        return images[0]
                    return False
                generated_image = await self.wait(generated, f'第 {index + 1} 张白底图生成未完成', timeout=self.upload_timeout)
                receipt = {'group': label, 'index': index, 'path': str(path), 'sha256': image_fingerprint(path),
                           'recipe': WHITE_BACKGROUND_RECIPE, 'source_url': before[index],
                           'result_url': generated_image['url'], 'width': generated_image['width'],
                           'height': generated_image['height'], 'status': 'generated'}
                receipts.append(receipt)
                self.persist()
                await apply.click()
                await drawer.wait_for(state='hidden', timeout=self.timeout * 1000)
                expected = before.copy()
                expected[index] = receipt['result_url']
                async def applied():
                    after = await self.image_urls(group)
                    if len(after) != len(before) or any(after[i] != before[i] for i in range(len(before)) if i != index):
                        raise DewuError('应用白底图后其他轮播图或顺序发生变化')
                    return after == expected
                await self.wait(applied, f'第 {index + 1} 张白底图未应用成功')
                records[index] = {**records[index], 'url': receipt['result_url']}
                receipt['status'] = 'applied'
                self.report['steps']['carousel']['groups'][group_index] = expected
                self.persist()

    async def run(self, *, save=True, resume_url='', on_created=None, sku_codes_only=False,
                  white_images_only=False, maintenance=False, submit_review=False):
        try:
            if submit_review and not save:
                raise DewuError('提交审核不能使用仅填写模式')
            self.report['submit_review_requested'] = submit_review
            if (sku_codes_only or white_images_only or maintenance) and not resume_url:
                raise DewuError('仅更新草稿必须指定已有草稿')
            if resume_url:
                if not valid_draft_url(resume_url):
                    raise DewuError('恢复地址必须是 stark.dewu.com 的新品草稿编辑地址')
                await self.page.goto(resume_url, wait_until='domcontentloaded')
                self.report['draft_url'] = resume_url
            else:
                await self.page.goto(DEWU_START_URL, wait_until='domcontentloaded')
            async def ready():
                parsed = urlsplit(self.page.url)
                if parsed.hostname == 'stark.dewu.com' and (
                        parsed.path in ('/', '/main') or parsed.path.startswith('/main/')):
                    # Manual sign-in can return to the merchant dashboard.
                    # Re-enter the requested form using the established session.
                    await self.page.goto(resume_url or DEWU_START_URL, wait_until='domcontentloaded')
                if resume_url:
                    return await self.page.get_by_placeholder('卖点提炼', exact=True).filter(visible=True).count()
                return await self.item('商品品牌').first.is_visible()
            self.logger.info('等待得物登录/页面加载；如需登录，请在专用 Chrome 中完成')
            await self.wait(ready, '得物登录或页面加载超时', timeout=self.login_timeout)
            guide = self.page.locator('.el-dialog:visible').filter(has_text='单品发布、批量发布都支持AI自动识别')
            if await guide.count():
                await guide.get_by_role('button', name='Close', exact=True).click()
            if self.on_authenticated:
                await self.on_authenticated()
            if not resume_url:
                await self.create()
            if on_created:
                on_created(self.page.url)
            await self.page.get_by_placeholder('卖点提炼', exact=True).filter(visible=True).wait_for(state='visible')
            # Verify brand/category before changing a resumed draft.
            brand = await self.item('品牌').locator('input').input_value()
            category_text = await self.page.locator('main').inner_text()
            if brand != self.product.brand or not re.search(
                    r'已选类目[：:]\s*' + r'\s*>>\s*'.join(map(re.escape, self.product.category)), category_text):
                raise DewuError('草稿品牌或类目与 Excel 不一致，停止填写')
            steps = (
                ('基础属性', self.basic), ('颜色', self.colors), ('商品轮播图', self.verify_carousel),
                ('前两张白底图抠图并应用', self.process_white_backgrounds),
                ('图片解析尺码表', self.size_chart), ('尺码推荐与试穿模板', self.recommendations),
                ('SKU 出价与库存', self.sku_batch), ('详情图片', self.detail_images),
            )
            if maintenance or sku_codes_only or white_images_only:
                steps = ((('SKU 商品编码追加尺码', self.update_sku_codes),) if sku_codes_only else ()) + (
                    (('商品轮播图', self.verify_carousel), ('前两张白底图抠图并应用', self.process_white_backgrounds))
                    if white_images_only else ())
            for label, step in steps:
                self.logger.info('得物：%s', label)
                while True:
                    if self.attribute_runtime is not None:
                        self.attribute_runtime.begin_review_collection('dewu')
                    try:
                        await step()
                        if self.attribute_runtime is not None:
                            self.attribute_runtime.raise_deferred_reviews()
                        break
                    except (ReviewRequired, ReviewBatchRequired) as exc:
                        self.report.update(status='waiting_review', pending_attributes=[
                            review.request.field_label for review in
                            (exc.reviews if isinstance(exc, ReviewBatchRequired) else (exc,))])
                        self.persist()
                        if self.on_review is None:
                            raise
                        await self.on_review(exc)
                        self.report.update(status='running', pending_attributes=[])
                self.persist()
            self.report['validation_errors'] = await self.form_validation_errors()
            blocking_errors = [e for e in self.report['validation_errors']
                               if not (save and not submit_review and e.startswith('商品货号已存在'))]
            if blocking_errors:
                raise DewuError('得物表单校验未通过：' + '；'.join(blocking_errors))
            if self.report['validation_errors']:
                self.logger.warning('平台提示货号已存在；保留 Excel 货号并只保存草稿，不提交审核')
            if submit_review:
                await self.submit_for_review()
            elif save:
                await self.save_draft()
                if self.attribute_runtime is not None:
                    await self.verify_saved_attributes()
                self.report['status'] = 'draft_saved'
            else:
                self.report['status'] = 'filled'
        except (ReviewRequired, ReviewBatchRequired):
            raise
        except Exception as exc:
            self.report.update(status='failed', error=str(exc))
            raise
        finally:
            if valid_draft_url(self.page.url):
                self.report['draft_url'] = self.page.url
            self.persist()
            try:
                await self.page.screenshot(path=str(self.directory / 'dewu-final.png'), full_page=True)
            except Exception:
                self.logger.warning('得物截图未完成；已保留 JSON 报告')


async def run_dewu_browser(args, product, directory, logger, state_path, *, learning_context=None):
    from playwright.async_api import async_playwright
    previous = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
    fingerprint = input_fingerprint(product)
    submit_review = getattr(args, 'dewu_submit_review', False)
    if previous.get('status') in SUBMITTED_STATUSES:
        if previous.get('fingerprint') != fingerprint:
            raise DewuError('本商品已提交得物审核，资料修改后请先核对得物审核状态，程序不会自动修改或重复提交')
        logger.info('本商品已提交得物审核，跳过重复提交')
        write_json(directory / 'dewu-result.json', {**previous, 'status': 'already_submitted'})
        return
    if previous.get('review_submission_attempted') and not previous.get('review_submission_rejected'):
        raise DewuError('上次得物审核提交结果未确认，请先在得物审核记录核对，勿重复提交')
    identity = product_identity(product)
    resume_url, already_saved = choose_resume(previous, args.dewu_resume_url, fingerprint, identity=identity)
    maintenance = needs_sku_code_update(previous, args.dewu_resume_url, fingerprint, already_saved)
    sku_current = sku_codes_match(product, previous.get('steps', {}).get('sku'))
    white_current = white_backgrounds_complete(product, previous)
    sku_codes_only = maintenance and not sku_current
    white_images_only = maintenance and not white_current
    if not submit_review and already_saved and sku_current and white_current:
        logger.info('本商品资料未变，已有保存成功的得物草稿，跳过重复创建：%s', resume_url)
        write_json(directory / 'dewu-result.json', {**previous, 'status': 'already_saved'})
        return
    if resume_url:
        logger.info('恢复本商品已有得物草稿：%s', resume_url)
        if previous.get('draft_url') == resume_url and previous.get('fingerprint') != fingerprint:
            logger.info('商品资料有更新，继续填写原草稿')
    async with async_playwright() as playwright:
        owned = not args.cdp_url
        if args.cdp_url:
            browser = await playwright.chromium.connect_over_cdp(args.cdp_url)
            if not browser.contexts:
                raise DewuError('CDP 浏览器没有可用上下文')
            context = browser.contexts[0]
        else:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(Path(args.user_data_dir).expanduser()), channel='chrome',
                headless=args.headless, no_viewport=True)
        auth_state_path = Path(args.auth_state).expanduser()
        await restore_dewu_auth_state(context, auth_state_path, logger)
        page = await context.new_page()
        if not args.headless:
            await page.bring_to_front()
        page.set_default_timeout(min(args.timeout, 60) * 1000)
        listing = DewuListing(page, product, directory, logger, timeout=min(args.timeout, 60), upload_timeout=args.upload_timeout,
                              attribute_runtime=getattr(learning_context, 'attribute_runtime', None))
        listing.login_timeout = args.login_timeout
        authenticated = False
        async def persist_login():
            nonlocal authenticated
            authenticated = True
            await save_dewu_auth_state(context, auth_state_path, logger)
        listing.on_authenticated = persist_login
        if learning_context is not None:
            async def wait_for_attributes(error):
                from kuaimai_erp import wait_for_platform_review_batch, review_requirements
                reviews = review_requirements(error)
                logger.info('得物有 %s 项属性等待工作台审核：%s；确认后自动继续原草稿', len(reviews),
                            '、'.join(review.request.field_label for review in reviews))
                await wait_for_platform_review_batch(learning_context, args, ('dewu',), 0, reviews, logger)
            listing.on_review = wait_for_attributes
        if resume_url and previous.get('draft_url') == resume_url:
            listing.report = deepcopy(previous)
            listing.report.update(status='running', submit_review=False)
            listing.report.pop('error', None)
            listing.report.pop('save_receipt', None)
            listing.report.pop('review_receipt', None)
            listing.report.pop('review_submission_rejected', None)
            listing.report.pop('review_submission_attempted', None)
            listing.report.pop('update_scope', None)
        if maintenance:
            logger.info('更新已有草稿：商品编码%s；前两张白底图%s',
                        '需修正' if sku_codes_only else '已符合规则', '需抠图' if white_images_only else '已处理')
            listing.report['update_scope'] = 'draft_maintenance'
        listing.report.update(fingerprint=fingerprint, excel_path=str(product.excel_path), product_identity=identity)
        def checkpoint(report):
            write_json(state_path, report)
        listing.on_checkpoint = checkpoint
        try:
            await listing.run(save=args.save, resume_url=resume_url, sku_codes_only=sku_codes_only,
                              white_images_only=white_images_only, maintenance=maintenance, submit_review=submit_review)
        finally:
            if owned and not args.headless and listing.report['status'] in {'filled', 'failed'} and sys.stdin.isatty():
                logger.info('浏览器保持打开供检查；确认完毕后在终端按回车结束。尚未保存的修改请在页面手工保存草稿。')
                loop = asyncio.get_running_loop()
                entered = loop.create_future()
                loop.add_reader(sys.stdin.fileno(), lambda: entered.done() or entered.set_result(sys.stdin.readline()))
                try:
                    await entered
                finally:
                    loop.remove_reader(sys.stdin.fileno())
            if authenticated:
                await save_dewu_auth_state(context, auth_state_path, logger)
            if owned:
                await context.close()


async def run_dewu_with_learning(args, product, directory, logger, state_path):
    """Keep Dewu's browser independent while sharing attribute learning/review."""
    from kuaimai_erp import (create_learning_context, initialize_learning_run, save_learning_checkpoint,
                            drain_learning_events, write_model_judgment_report)
    learning_product = SimpleNamespace(
        style_code=product.style_code, title=product.selling_point, main_images=product.initial_images,
        main_images_34=(), detail_images=product.detail_images,
        descriptive_fields=dict(product.fields), category_hints=(' > '.join(product.category),))
    learning = create_learning_context(args, learning_product)
    if learning is None:
        return await run_dewu_browser(args, product, directory, logger, state_path)
    session = SimpleNamespace(model_decisions=[], model_events=[])
    status, error = 'running', ''
    def report():
        write_model_judgment_report(directory, learning, session, run_status=status, stop_reason=error)
    learning.attribute_runtime.model_decision_sink = report
    try:
        await initialize_learning_run(learning, learning_product, logger)
        save_learning_checkpoint(learning, args, ('dewu',), 0, 'running')
        await run_dewu_browser(args, product, directory, logger, state_path, learning_context=learning)
        save_learning_checkpoint(learning, args, ('dewu',), 1, 'completed')
        status = 'completed'
    except BaseException as exc:
        status, error = ('interrupted' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'failed'), str(exc)
        save_learning_checkpoint(learning, args, ('dewu',), 0, 'failed')
        raise
    finally:
        report()
        try:
            await drain_learning_events(learning)
        finally:
            learning.store.close()


def run_dewu_command(args):
    from kuaimai_erp import DEFAULT_AUTH_STATE, DEFAULT_EXCEL_URL, DEFAULT_PROFILE_DIR, SCRIPT_DIR, automation_run_lock, setup_logging
    directory = args.output_dir or SCRIPT_DIR / 'output/dewu/runs' / datetime.now().strftime('%Y%m%d-%H%M%S')
    logger = setup_logging(directory)
    try:
        if Path(args.user_data_dir).expanduser().resolve() == DEFAULT_PROFILE_DIR.resolve():
            args.user_data_dir = str(SCRIPT_DIR / 'output/dewu/chrome-profile')
        if Path(args.auth_state).expanduser().resolve() == DEFAULT_AUTH_STATE.resolve():
            args.auth_state = str(SCRIPT_DIR / 'output/dewu/auth-state.json')
        # The ERP default workbook must never leak into the Dewu flow.
        excel = '' if args.excel_url == DEFAULT_EXCEL_URL else args.excel_url
        path = select_workbook(args.dewu_root, args.dewu_product, excel)
        product = read_dewu_product(path)
        submit_review = getattr(args, 'dewu_submit_review', False)
        summary = {**product.summary(), 'submit_review': submit_review}
        write_json(directory / 'input-summary.json', summary)
        logger.info('得物素材：%s；图片 %s 张；详情 %s 张；%s', path.parent, len(product.initial_images), len(product.detail_images),
                    '校验通过后提交审核' if submit_review else '只处理草稿')
        if args.dry_run:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
        state = SCRIPT_DIR / '.local-state/dewu' / (key + '.json')
        with automation_run_lock(Path(args.user_data_dir)):
            asyncio.run(run_dewu_with_learning(args, product, directory, logger, state))
        logger.info('得物流程完成；%s。报告：%s', '提交审核模式' if submit_review else '仅保存草稿模式', directory)
        return 0
    except KeyboardInterrupt:
        logger.error('已中止得物流程；请使用报告中的草稿地址继续')
        return 130
    except Exception as exc:
        logger.error('得物流程停止：%s', exc)
        return 2
