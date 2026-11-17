"""Native B 站 input, passive API validation and recovery invariants."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openpyxl import Workbook, load_workbook
import pytest

from bilibili_data import BilibiliError, color_name, match_color_images, read_bilibili_product, parse_bilibili_product
from bilibili_api import BilibiliCapture, CATEGORIES, ATTRIBUTES, BRANDS, INFO_BACK, category_candidates, verify_sku_payload
from bilibili_listing import BilibiliListing, resume_state, valid_edit_url, PUBLISH_URL
from kuaimai_erp import build_parser, validate_execution_mode, learning_execution_mode
from local_review.config import Settings
from local_review.launcher import BilibiliProductCatalog, Launcher, atomic_json, build_command
from local_review.service import ApiError


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def product(tmp_path):
    folder = tmp_path / 'B站上款' / '【寒战】'
    folder.mkdir(parents=True)
    book = Workbook()
    book.active.append(['属性', '内容'])
    for row in [('商品标题', '寒战复古羊毛重磅针织毛衣'), ('货号', 'NGBL-2128'), ('品牌', 'NEIGBORL'),
                ('商品类目', '服饰内衣/男装/针织衫/毛衣'), ('颜色', '深灰色'), ('主图比例', '1:1'), ('尺码', 'S/M/L'), ('售价', 590),
                ('可售库存', 50), ('商家编码', 'NGBL-2128深灰色'), ('发货时效', '15天'),
                ('运费模板', '指定运费模板'), ('面料', '羊毛，尼龙')]:
        book.active.append(row)
    excel = folder / '产品信息.xlsx'
    book.save(excel)
    book.close()
    for dirname, names in [('1：1主图', ['1.png', '2.png']), ('3：4主图', ['1.png']),
                           ('详情页图', ['1_01.jpg', '1_02.jpg']), ('SKU图', ['深灰色.png'])]:
        directory = folder / dirname
        directory.mkdir()
        for name in names:
            (directory / name).write_bytes(b'test-image')
    return read_bilibili_product(excel)


def test_supplied_template_dialect_and_named_sku_assets(product):
    assert product.colors == ('深灰色',)
    assert product.sizes == ('S', 'M', 'L')
    assert (product.price, product.stock, product.delivery_days) == ('590', '50', '15')
    assert product.category_hints == ('服饰内衣/男装/针织衫/毛衣',)
    assert product.merchant_code('深灰色', 'M') == 'NGBL-2128深灰色M'
    assert product.descriptive_fields['面料'] == '羊毛，尼龙'


def test_main_images_prefer_portraits_unless_excel_explicitly_selects_a_ratio(product):
    fields = {k: v for k, v in product.fields.items() if k != '主图比例'}
    portrait = parse_bilibili_product(product.source, fields)
    assert portrait.image_ratio == '3:4'
    assert portrait.images == tuple(product.main_images_34)
    square = parse_bilibili_product(product.source, {**fields, 'B站主图比例': '1：1'})
    assert square.image_ratio == '1:1'
    assert square.images == tuple(product.main_images)
    fallback = parse_bilibili_product(replace(product.source, main_images_34=[]), fields)
    assert fallback.image_ratio == '1:1'
    with pytest.raises(BilibiliError, match='3:4 主图素材不能为空'):
        parse_bilibili_product(replace(product.source, main_images_34=[]), {**fields, '主图比例': '3:4'})


def test_workbook_without_square_directory_can_use_existing_portrait_assets(product):
    for path in product.main_images:
        path.unlink()
    product.main_images[0].parent.rmdir()
    workbook = load_workbook(product.excel_path)
    for row in workbook.active.iter_rows():
        if row[0].value == '主图比例':
            row[1].value = None
    workbook.save(product.excel_path)
    workbook.close()
    actual = read_bilibili_product(product.excel_path)
    assert actual.image_ratio == '3:4'
    assert actual.images == tuple(product.main_images_34)


def test_excel_color_row_controls_names_and_order_with_abbreviated_image_binding(product):
    product.sku_images[0].unlink()
    directory = product.sku_images[0].parent
    for name in ('黑.png', '卡其.png'):
        (directory / name).write_bytes(b'test-image')
    workbook = load_workbook(product.excel_path)
    for row in workbook.active.iter_rows():
        if row[0].value == '颜色':
            row[1].value = '黑色，卡其色'
    workbook.save(product.excel_path)
    workbook.close()
    actual = read_bilibili_product(product.excel_path)
    assert actual.colors == ('黑色', '卡其色')
    assert actual.fields['颜色'] == '黑色，卡其色'
    # Image discovery orders 卡其 before 黑; the workbook controls specification order.
    assert [p.name for p in match_color_images(actual.sku_images, actual.colors)] == ['黑.png', '卡其.png']
    assert actual.merchant_code('黑色', 'M').endswith('黑色M')


@pytest.mark.parametrize(('value', 'expected'), [('卡其', '卡其色'), ('黑', '黑色'), ('深灰', '深灰色'),
                                               ('黑色', '黑色'), ('条纹', '条纹'), ('Black', 'Black')])
def test_color_names_complete_hues_without_changing_other_specification_labels(value, expected):
    assert color_name(value) == expected


def test_excel_color_names_are_preserved_and_duplicates_are_rejected(product):
    actual = parse_bilibili_product(product.source, {**product.fields, '颜色': '军绿色/黑/卡其色'})
    assert actual.colors == ('军绿色', '黑', '卡其色')
    assert actual.fields['颜色'] == '军绿色/黑/卡其色'
    with pytest.raises(BilibiliError, match='颜色规格重复'):
        parse_bilibili_product(product.source, {**product.fields, '颜色': '黑色/黑色'})


@pytest.mark.parametrize('value', [None, '', '， / '])
def test_missing_excel_color_does_not_fall_back_to_source_or_named_images(product, value):
    fields = dict(product.fields)
    fields.pop('颜色')
    if value is not None:
        fields['颜色'] = value
    assert product.source.colors == ('深灰色',)
    assert product.sku_images[0].name == '深灰色.png'
    with pytest.raises(BilibiliError, match='缺少“颜色”行或内容为空'):
        parse_bilibili_product(product.source, fields)
    workbook = load_workbook(product.excel_path)
    for row in workbook.active.iter_rows():
        if row[0].value == '颜色':
            row[1].value = value
    workbook.save(product.excel_path)
    workbook.close()
    with pytest.raises(BilibiliError, match='缺少“颜色”行或内容为空'):
        read_bilibili_product(product.excel_path)


def test_color_image_binding_rejects_missing_and_ambiguous_normalized_names():
    with pytest.raises(BilibiliError, match='匹配到 0 张'):
        match_color_images((Path('卡其.png'),), ('黑色',))
    with pytest.raises(BilibiliError, match='匹配到 2 张'):
        match_color_images((Path('黑.png'), Path('黑色.png')), ('黑色',))


@pytest.mark.parametrize('updates', [{'可售库存': '-1'}, {'可售库存': '1.5'}, {'售价': 'NaN'},
                                     {'售价': '0'}, {'运费模板': ''}, {'尺码': 'M/M'},
                                     {'发货时效': '0天'}, {'上架设置': '定时上架'}])
def test_invalid_commercial_inputs_stop_before_browser(product, updates):
    with pytest.raises(BilibiliError):
        parse_bilibili_product(product.source, {**product.fields, **updates})


def test_title_limit_and_numbered_assets_cannot_supply_colors(product):
    with pytest.raises(BilibiliError, match='标题'):
        parse_bilibili_product(replace(product.source, title='短'), product.fields)
    product.sku_images[0].rename(product.sku_images[0].with_name('1.png'))
    actual = read_bilibili_product(product.excel_path)
    assert actual.colors == ('深灰色',)
    with pytest.raises(BilibiliError, match='匹配到 0 张'):
        match_color_images(actual.sku_images, actual.colors)


def test_category_reconstruction_checks_leaf_availability_and_preserves_native_slash():
    rows = [{'cateId': 1, 'parentId': 0, 'name': '服饰内衣', 'valid': 0},
            {'cateId': 2, 'parentId': 1, 'name': '男装', 'valid': 0},
            {'cateId': 3, 'parentId': 2, 'name': '针织衫/毛衣', 'valid': 1},
            {'cateId': 4, 'parentId': 0, 'name': '另一目录', 'valid': 1},
            {'cateId': 5, 'parentId': 4, 'name': '未开通叶子', 'valid': 0}]
    assert category_candidates(rows) == ({'id': '3', 'path': ['服饰内衣', '男装', '针织衫/毛衣'],
                                          'label': '服饰内衣 > 男装 > 针织衫/毛衣'},)


def test_jacket_category_remains_selectable_under_zero_valid_directory_nodes():
    # Shape captured from the merchant API for the failing product.
    rows = [{'cateId': 23554, 'parentId': 0, 'name': '服饰内衣', 'valid': 0},
            {'cateId': 23575, 'parentId': 23554, 'name': '男装', 'valid': 0},
            {'cateId': 23993, 'parentId': 23575, 'name': '夹克', 'valid': 1},
            {'cateId': 24008, 'parentId': 23575, 'name': '针织衫/毛衣', 'valid': 1},
            {'cateId': 99999, 'parentId': 23575, 'name': '未开通', 'valid': 0}]
    candidates = category_candidates(rows)
    assert [c['id'] for c in candidates] == ['23993', '24008']
    assert candidates[0]['label'] == '服饰内衣 > 男装 > 夹克'


@pytest.mark.parametrize('rows', [[{'cateId': 1, 'parentId': 1, 'name': '循环'}],
                                 [{'cateId': 1, 'parentId': 2, 'name': '孤立'}],
                                 [{'cateId': 1, 'parentId': 0, 'name': 'a'}] * 2])
def test_invalid_category_graph_is_rejected(rows):
    with pytest.raises(BilibiliError):
        category_candidates(rows)


def sku_payload(product):
    return [{'price': 590.0, 'stock': 50, 'shopCode': product.merchant_code('深灰色', size),
             'specList': [{'specKeyName': '颜色', 'specValueName': '深灰色'},
                          {'specKeyName': '尺码', 'specValueName': size}],
             'img': '//assets.test/gray.png', 'autoOnSaleTime': 0} for size in product.sizes]


def test_sku_validation_rejects_stale_values_missing_rows_and_duplicates(product):
    rows = sku_payload(product)
    assert verify_sku_payload(rows, product) == 3
    for altered in [rows[:2], rows + [rows[0]], [{**rows[0], 'price': 591}, *rows[1:]],
                    [{**rows[0], 'shopCode': 'wrong'}, *rows[1:]], [{**rows[0], 'stock': 49}, *rows[1:]]]:
        with pytest.raises(BilibiliError):
            verify_sku_payload(altered, product)


def test_edit_url_and_uncertain_save_recovery(product):
    draft = PUBLISH_URL + '?draftId=123'
    assert valid_edit_url(draft, draft_only=True)
    for url in [draft + '&itemsId=2', draft.replace('shop.bilibili.com', 'shop.bilibili.com.evil.test'),
                draft.replace('https:', 'http:'), PUBLISH_URL + '?draftId=0', PUBLISH_URL + '?itemsId=123']:
        assert not valid_edit_url(url, draft_only=True)
    with pytest.raises(BilibiliError, match='结果未确认'):
        resume_state({'save_attempted': True}, product)
    assert resume_state({'save_attempted': True}, product, draft) == draft
    with pytest.raises(BilibiliError, match='重复发布'):
        resume_state({'submission_attempted': True}, product, draft)


def test_workbench_catalog_uses_dedicated_root_and_ignores_root_template(product, tmp_path):
    root = product.excel_path.parent.parent
    (root / '毛衣模板.xlsx').write_bytes(b'template')
    catalog = BilibiliProductCatalog(root)
    assert len(catalog.paths()) == 1
    key = next(iter(catalog.paths()))
    assert catalog.listing()[0]['source'] == 'bilibili'
    detail = catalog.detail(key)
    assert detail['category'] == '服饰内衣/男装/针织衫/毛衣'
    assert detail['image'].endswith('?source=bilibili')
    settings = Settings(tmp_path / 'data', 'device', bilibili_products_root=root)
    config = {'operation': 'bilibili', 'platforms': ['bilibili'], 'mode': 'save', 'learning': True}
    command = build_command(product.excel_path, config, 'a' * 32, tmp_path / 'out', settings)
    args = build_parser().parse_args(command[3:])
    validate_execution_mode(args)
    assert args.platform == 'bilibili' and args.save_only and args.learning_enabled
    assert args.bilibili_root == root
    assert '--materials-root' not in command
    assert learning_execution_mode(args) == 'save_only'
    with pytest.raises(ApiError):
        build_command(product.excel_path, {**config, 'mode': 'publish'}, 'run', tmp_path / 'out', settings)
    with pytest.raises(ApiError):
        build_command(tmp_path / 'other.xlsx', config, 'a' * 32, tmp_path / 'out', settings)


def test_workbench_start_routes_bilibili_source_without_adding_base(product, tmp_path):
    from local_review.database import migrate
    settings = Settings(tmp_path / 'data', 'device', products_root=tmp_path / 'ordinary',
                        bilibili_products_root=product.excel_path.parent.parent)
    migrate(settings)
    launcher = Launcher(settings)
    launcher.bilibili_catalog.runs_root = tmp_path / 'runs'
    key = next(iter(launcher.bilibili_catalog.paths()))
    payload = {'request_key': 'a'*32, 'product_id': key, 'operation': 'bilibili',
               'platforms': ['bilibili'], 'mode': 'save', 'learning': True}
    child = Mock(pid=987654)
    with patch('local_review.launcher.external_runner_active', return_value=False), \
            patch('local_review.launcher.subprocess.Popen', return_value=child):
        job = launcher.start(payload, 'admin')
    assert not job['base_auto_added'] and [s['platform'] for s in job['stages']] == ['bilibili']
    command = json.loads((launcher.root / job['id'] / 'launch.json').read_text())['argv']
    assert command[command.index('--excel-url') + 1] == str(product.excel_path)
    assert '--learning-enabled' in command and '--save-only' in command
    run = launcher.bilibili_catalog.runs_root / 'saved'
    run.mkdir(parents=True)
    report = {'status': 'draft_saved', 'excel_path': str(product.excel_path)}
    atomic_json(run / 'bilibili-result.json', report)
    assert not launcher.bilibili_catalog.listing()[0]['processed']
    report['steps'] = {'persisted_readback': {'verified': True}}
    atomic_json(run / 'bilibili-result.json', report)
    assert launcher.bilibili_catalog.listing()[0]['processed']


@pytest.mark.parametrize('options', [['--platform', 'bilibili,jd'], ['--platform', 'jd', '--bilibili-publish'],
                                    ['--platform', 'bilibili', '--bilibili-publish', '--no-save'],
                                    ['--platform', 'bilibili', '--bilibili-publish', '--save-only']])
def test_cli_rejects_mixed_or_conflicting_operations(options):
    with pytest.raises(SystemExit):
        validate_execution_mode(build_parser().parse_args(options))


class Response:
    def __init__(self, path, data, params=None, *, code=0, delay=0):
        self.url = 'https://shop.bilibili.com' + path
        self.request = SimpleNamespace(method='POST', post_data_json=params or {})
        self.status, self.payload, self.delay = 200, {'code': code, 'data': data}, delay

    async def json(self):
        await asyncio.sleep(self.delay)
        return self.payload


@pytest.mark.anyio
async def test_passive_capture_ignores_older_brand_response_and_redacts_values():
    cap = BilibiliCapture(Mock())
    old = Response(BRANDS, {'brandList': [{'brandName': 'old'}]}, {'brandName': 'private'}, delay=.03)
    new = Response(BRANDS, {'brandList': [{'brandName': 'new'}]})
    # SimpleNamespace requests are unhashable; use identity-bearing mocks like Playwright.
    for sequence, response in enumerate((old, new), 1):
        request = Mock(method='POST', post_data_json=response.request.post_data_json)
        response.request = request
        cap._requests[request] = sequence
    await asyncio.gather(cap.consume(old, BRANDS), cap.consume(new, BRANDS))
    assert cap.brands == [{'brandName': 'new'}]
    assert 'private' not in json.dumps(cap.schema())
    assert any(o['status'] == 'superseded' for o in cap.observations)
    await cap.close()


@pytest.mark.anyio
async def test_capture_scopes_attributes_to_requested_category_and_pending_info():
    cap = BilibiliCapture(Mock())
    for category in (1, 2):
        response = Response(ATTRIBUTES, {'attrs': [{'attrId': category, 'attrName': '面料'}]}, {'cateId': category})
        response.request = Mock(method='POST', post_data_json={'cateId': category})
        await cap.consume(response, ATTRIBUTES)
    pending = Response(INFO_BACK, {'name': 'not-ready'}, code=1)
    pending.request = Mock(method='POST', post_data_json={})
    await cap.consume(pending, INFO_BACK)
    assert cap.info is None and not cap.errors
    assert set(cap.attributes) == {'1', '2'}
    assert cap.attributes['2']['attrs'][0]['attrId'] == 2
    await cap.close()


def verified_fixture(product, tmp_path):
    listing = BilibiliListing(Mock(), product, tmp_path)
    listing.category_id = '24008'
    listing.report['steps'].update(brand_id='5542', freight_id='7', stock_deduction=1,
                                   attributes={'面料': ['羊毛', '尼龙']})
    listing.capture.attributes['24008'] = {'attrs': [{'attrId': 30, 'attrName': '面料'}]}
    listing.report['uploads'] = {'主图': [{'url': 'https://assets.test/main.png'}],
                                '详情图': [{'url': 'https://assets.test/detail.jpg'}],
                                'SKU图:深灰色': [{'url': 'https://assets.test/gray.png'}]}
    info = {'draftId': 123, 'itemsId': None, 'operateStatus': 1, 'name': product.title, 'cateId': 24008,
            'brandId': 5542, 'deployType': 1, 'skuList': sku_payload(product), 'imgScale': '1:1',
            'freightTemplateId': 7, 'stockDeductMethod': 1,
            'itemsCommitmentTag': {'deliveryTimeType': 1, 'deliveryTimeValue': 15, 'supportReturn': 1},
            'imageList': ['//assets.test/main.png'], 'mobileDesc': '<p><img src="//assets.test/detail.jpg"/></p>',
            'itemsAttrs': [{'attrId': 30, 'itemsAttrValues': [{'attrValueName': '尼龙'}, {'attrValueName': '羊毛'}]}]}
    return listing, info


def test_persisted_validation_covers_nested_services_images_specs_and_attributes(product, tmp_path):
    listing, info = verified_fixture(product, tmp_path)
    listing.verify_saved(info)
    assert listing.report['steps']['persisted_readback']['verified']
    for key, value in [('operateStatus', 3), ('imgScale', '3:4'), ('imageList', []),
                       ('itemsCommitmentTag', {'deliveryTimeType': 1, 'deliveryTimeValue': 30, 'supportReturn': 1}),
                       ('itemsAttrs', []), ('skuList', [{**info['skuList'][0], 'img': '//wrong.test/img.png'}, *info['skuList'][1:]])]:
        with pytest.raises(BilibiliError):
            listing.verify_saved({**info, key: value})


@pytest.mark.anyio
@pytest.mark.parametrize('mismatch', [False, True])
async def test_save_reopens_same_draft_and_learns_only_verified_readback(product, tmp_path, mismatch):
    listing, info = verified_fixture(product, tmp_path)
    listing.attribute_runtime = Mock()
    listing.validate_main_images = AsyncMock()
    listing.validate_page = AsyncMock()
    listing.bind_form = AsyncMock()
    if mismatch:
        info['skuList'][0]['stock'] = 0
    async def clicked():
        listing.capture.receipts.append({'sequence': 1, 'data': {'draftId': 123}})
    async def reopened(*args, **kwargs):
        listing.capture.info = info
    listing.page.get_by_role.return_value.click = AsyncMock(side_effect=clicked)
    listing.page.goto = AsyncMock(side_effect=reopened)
    if mismatch:
        with pytest.raises(BilibiliError):
            await listing.save()
        listing.attribute_runtime.record_verified_readbacks.assert_not_called()
        assert listing.report['save_attempted'] and listing.report['draft_url'].endswith('draftId=123')
    else:
        await listing.save()
        assert listing.report['status'] == 'draft_saved'
        listing.page.goto.assert_awaited_once_with(PUBLISH_URL + '?draftId=123', wait_until='domcontentloaded')
        listing.attribute_runtime.record_verified_readbacks.assert_called_once_with('bilibili', {'面料': ['羊毛', '尼龙']})


@pytest.mark.anyio
async def test_bilibili_login_snapshot_filters_sites_and_restores_missing_cookies(tmp_path):
    from bilibili_auth import save_auth, restore_auth
    cookie = lambda domain, value='test': {'name': 'session', 'value': value, 'domain': domain, 'path': '/', 'expires': -1}
    context = Mock()
    context.storage_state = AsyncMock(return_value={'cookies': [cookie('.bilibili.com'), cookie('.other.test')],
        'origins': [{'origin': 'https://shop.bilibili.com', 'localStorage': [{'name': 'test', 'value': 'value'}]},
                    {'origin': 'https://other.test', 'localStorage': [{'name': 'other', 'value': 'unrelated'}]}]})
    context.pages = []
    path = tmp_path / 'auth.json'
    await save_auth(context, path, Mock())
    saved = json.loads(path.read_text())
    assert len(saved['cookies']) == 1 and saved['cookies'][0]['domain'] == '.bilibili.com'
    assert 'unrelated' not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    context.cookies = AsyncMock(return_value=[])
    context.add_cookies = AsyncMock()
    context.add_init_script = AsyncMock()
    await restore_auth(context, path, Mock())
    context.add_cookies.assert_awaited_once_with(saved['cookies'])
    context.cookies = AsyncMock(return_value=[cookie('.bilibili.com', 'newer')])
    context.add_cookies.reset_mock()
    await restore_auth(context, path, Mock())
    context.add_cookies.assert_not_called()
