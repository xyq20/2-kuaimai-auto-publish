"""Observe merchant-console requests; all product writes remain DOM actions.

Paths and payload names were checked against the merchant console's published
goodsDetail-PMq49yyn / Publish-DihhzvXz bundles (2026-11-12).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from bilibili_data import BilibiliError
from platform_schema import sanitize_message


PREFIX = '/mall-merchant-items'
CATEGORIES = PREFIX + '/merchant/category/getAllPlatCategoryByMerchantType'
ATTRIBUTES = PREFIX + '/material/categoryAttr'
BRANDS = PREFIX + '/merchant/searchMerchantBrandIp'
FREIGHT = PREFIX + '/merchant/freight_template/all'
INFO_CALL = PREFIX + '/items/infoCall'
INFO_BACK = PREFIX + '/items/infoBack'
SAVE_CALLS = {PREFIX + '/items/addCall', PREFIX + '/items/updateCall'}
SAVE_BACKS = {PREFIX + '/items/addBack', PREFIX + '/items/updateBack'}
OBSERVED_PATHS = {CATEGORIES, ATTRIBUTES, BRANDS, FREIGHT, INFO_CALL, INFO_BACK, *SAVE_CALLS, *SAVE_BACKS}


def category_candidates(records):
    """Reconstruct full leaf paths from the platform's flat cateId/parentId list."""
    if not isinstance(records, list):
        raise BilibiliError('B 站类目接口返回结构不正确')
    nodes = {str(r['cateId']): r for r in records if isinstance(r, dict) and r.get('cateId') is not None}
    if len(nodes) != len(records):
        raise BilibiliError('B 站类目 ID 缺失或重复')
    parents = {str(r.get('parentId')) for r in records}
    candidates = []
    for leaf_id, node in nodes.items():
        path, seen, current = [], set(), node
        while current:
            key = str(current['cateId'])
            if key in seen or not current.get('name'):
                raise BilibiliError('B 站类目路径循环或名称缺失')
            seen.add(key)
            path.insert(0, str(current['name']))
            parent = str(current.get('parentId', 0))
            if parent == '0':
                break
            current = nodes.get(parent)
            if current is None:
                raise BilibiliError('B 站类目缺少父级，无法校验完整路径')
        # `valid` marks selectable leaf categories. Directory nodes also return
        # 0, including ancestors of categories this merchant may publish in.
        if leaf_id not in parents and node.get('valid', 1) != 0:
            candidates.append({'id': leaf_id, 'path': path, 'label': ' > '.join(path)})
    return tuple(candidates)


def same_number(actual, expected):
    try:
        left, right = Decimal(str(actual)), Decimal(str(expected))
        return left.is_finite() and right.is_finite() and left == right
    except (InvalidOperation, TypeError, ValueError):
        return False


def verify_sku_payload(rows, product):
    expected = {(c, s) for c in product.colors for s in (product.sizes or ('',))} or {('', '')}
    result = {}
    for row in rows:
        values = {v['specKeyName']: v['specValueName'] for v in row.get('specList', ())}
        if set(values) - {'颜色', '尺码'}:
            raise BilibiliError('B 站 SKU 存在未配置的规格维度')
        key = (values.get('颜色', ''), values.get('尺码', ''))
        if key in result or key not in expected:
            raise BilibiliError('B 站 SKU 组合重复或与 Excel 不一致')
        if (not same_number(row.get('price'), product.price)
                or not same_number(row.get('stock'), product.stock)
                or row.get('shopCode') != product.merchant_code(*key)):
            raise BilibiliError(f'B 站 SKU {key} 售价、可售库存或商家编码回读不一致')
        result[key] = row
    if set(result) != expected:
        raise BilibiliError('B 站 SKU 组合不全')
    return len(result)


class BilibiliCapture:
    def __init__(self, page):
        self.page = page
        self.categories = ()
        self.attributes = {}
        self.freight = []
        self.brands = []
        self.info = None
        self.receipts = []
        self.observations = []
        self.tasks = set()
        self.errors = []
        self.sequence = 0
        self.category_sequence = {}
        self.latest_sequence = {}
        self._listener = self._on_response
        self._request_listener = self._on_request
        self._requests = {}
        page.on('request', self._request_listener)
        page.on('response', self._listener)

    def _on_request(self, request):
        parsed = urlsplit(request.url)
        if parsed.hostname != 'shop.bilibili.com' or parsed.path not in OBSERVED_PATHS:
            return
        self.sequence += 1
        self._requests[request] = self.sequence

    def _on_response(self, response):
        parsed = urlsplit(response.url)
        if parsed.hostname != 'shop.bilibili.com' or parsed.path not in OBSERVED_PATHS:
            return
        task = asyncio.create_task(self.consume(response, parsed.path))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def consume(self, response, path):
        request = response.request
        sequence = self._requests.pop(request, 0)
        observation = {'path': path, 'method': request.method, 'http_status': response.status, 'sequence': sequence}
        try:
            params = request.post_data_json or {}
            params = params if isinstance(params, dict) else {}
            observation['parameter_names'] = sorted(k for k in params if k.lower() not in {'csrf', 'token', 'access_key'})
            payload = await response.json()
            code = payload.get('code')
            observation['code'] = code
            if response.status != 200 or code != 0:
                observation['status'] = 'pending' if code == 1 else 'error'
                observation['message'] = sanitize_message(payload.get('message', ''))
                return
            observation['status'] = 'ok'
            data = payload.get('data')
            if path in {CATEGORIES, FREIGHT, BRANDS, INFO_BACK}:
                if sequence < self.latest_sequence.get(path, -1):
                    observation['status'] = 'superseded'
                    return
                self.latest_sequence[path] = sequence
            if path == CATEGORIES:
                self.categories = category_candidates(data)
            elif path == ATTRIBUTES:
                category_id = str(params.get('cateId', ''))
                if not category_id or not isinstance(data, dict):
                    raise BilibiliError('B 站属性接口缺少类目 ID 或字段结构')
                if sequence >= self.category_sequence.get(category_id, -1):
                    self.attributes[category_id] = data
                    self.category_sequence[category_id] = sequence
            elif path == FREIGHT:
                self.freight = data
            elif path == BRANDS:
                self.brands = (data or {}).get('brandList', [])
            elif path == INFO_BACK:
                self.info = data
            elif path in SAVE_BACKS:
                self.receipts.append({'sequence': sequence, 'data': data, 'path': path})
        except Exception as exc:
            observation.update(status='error', message=sanitize_message(str(exc)))
            self.errors.append(f'{path}: {observation["message"]}')
        finally:
            self.observations.append(observation)

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*tuple(self.tasks))

    async def close(self):
        self.page.remove_listener('request', self._request_listener)
        self.page.remove_listener('response', self._listener)
        await self.drain()

    def schema(self):
        # No cookies, headers, request values, shop names, or freight names in reports.
        return {
            'platform_id': 'bilibili', 'endpoints': list(self.observations),
            'category_count': len(self.categories),
            'categories': list(self.categories),
            'attributes': {key: [{'id': str(a.get('attrId', '')), 'label': a.get('attrName'),
                                  'required': bool(a.get('isMust')), 'input_type': a.get('attrInputType'),
                                  'options': [{'id': str(v.get('attrValueId', '')), 'label': v.get('attrValueName')}
                                              for v in a.get('attrValues', ())]}
                                 for a in value.get('attrs', ())]
                           for key, value in self.attributes.items()},
            'freight_count': len(self.freight), 'issues': list(self.errors),
        }
