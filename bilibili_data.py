"""Inputs for publishing directly in the bilibili merchant console."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import re
import unicodedata

from douyin_data import field_lookup
from money_values import normalize_money_value, MoneyValueError


BILIBILI_LIST_URL = 'https://shop.bilibili.com/#/g/goods/list'
DEFAULT_BILIBILI_ROOT = Path('/Volumes/美工文件/B站上款')


class BilibiliError(RuntimeError):
    """An input, platform validation, or persistence error."""


def excel_value(fields, *aliases, default=''):
    found = field_lookup(fields, *aliases)
    return str(found[1]).strip() if found and found[1] is not None else default


def specification_values(value):
    return tuple(part.strip() for part in re.split(r'[/／,，、;；\n]+', value) if part.strip())


def color_name(value):
    name = str(value).strip()
    # Complete Chinese hue names, while preserving labels such as 条纹 and 格纹.
    if '色' not in name and re.fullmatch(r'[\u4e00-\u9fff]*(?:[黑白灰红橙黄绿青蓝紫棕褐粉杏米驼银金]|卡其|咖啡)', name):
        return name + '色'
    return name


def match_color_images(paths, colors):
    def key(value):
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', color_name(value))).casefold()

    names = tuple(key(value) for value in colors)
    if not names or any(not name for name in names) or len(set(names)) != len(names):
        raise BilibiliError('B 站颜色规格为空或重复，无法匹配 SKU 图')
    matched = []
    for value, name in zip(colors, names):
        matches = [path for path in paths if key(path.stem) == name]
        if len(matches) != 1:
            raise BilibiliError(f'B 站颜色“{value}”匹配到 {len(matches)} 张 SKU 图，请核对图片名称')
        matched.append(matches[0])
    return tuple(matched)


def integer(value, label, *, minimum=0):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value() or number < minimum:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise BilibiliError(f'{label}必须是大于等于 {minimum} 的整数') from None
    return str(int(number))


@dataclass(frozen=True)
class BilibiliProduct:
    source: object
    fields: Mapping[str, str]
    brand: str
    colors: tuple[str, ...]
    sizes: tuple[str, ...]
    price: str
    stock: str
    sku_code: str
    image_ratio: str
    delivery_days: str
    freight_template: str
    listing_state: str

    def __getattr__(self, name):
        return getattr(self.source, name)

    @property
    def images(self):
        return tuple(self.main_images if self.image_ratio == '1:1' else self.main_images_34)[:5]

    @property
    def descriptive_fields(self):
        return dict(self.fields)

    def merchant_code(self, color, size):
        code = self.sku_code
        if color and not code.endswith(color):
            code += color
        return code + size

    def summary(self):
        return {
            'platform': 'bilibili', 'excel_path': str(self.excel_path), 'title': self.title,
            'style_code': self.style_code, 'brand': self.brand,
            'category_hints': list(self.category_hints), 'colors': list(self.colors),
            'sizes': list(self.sizes), 'price': self.price, 'stock': self.stock,
            'sku_code': self.sku_code, 'image_ratio': self.image_ratio,
            'delivery_days': self.delivery_days, 'freight_template': self.freight_template,
            'listing_state': self.listing_state, 'main_images': [str(p) for p in self.images],
            'detail_images': [str(p) for p in self.detail_images],
        }


def parse_bilibili_product(source, fields) -> BilibiliProduct:
    normalized = {str(k).strip(): str(v).strip() for k, v in fields.items()
                  if str(k).strip() and v is not None and str(v).strip()}
    value = lambda *aliases, default='': excel_value(normalized, *aliases, default=default)
    title = source.title
    # The merchant console counts Unicode code points, with a limit of 60.
    if len(title) < 6 or len(title) > 60:
        raise BilibiliError('B 站商品标题须为 6–60 个字符')
    if not source.category_hints:
        raise BilibiliError('Excel 缺少商品分类，无法确定 B 站类目')
    brand = value('品牌', '商品品牌')
    if not brand:
        raise BilibiliError('Excel 缺少品牌')
    if value('发布类型', default='普通商品') != '普通商品':
        raise BilibiliError('B 站当前自动上款仅支持普通商品')
    if value('定时开售', default='否') != '否':
        raise BilibiliError('B 站当前自动上款须关闭定时开售')
    if value('退换说明', default='支持7天无理由') not in {'支持7天无理由', '支持7天无理由退货'}:
        raise BilibiliError('请核对 B 站退换说明，当前支持7天无理由')
    colors = specification_values(value('颜色'))
    if not colors:
        raise BilibiliError('B 站 Excel 缺少“颜色”行或内容为空，请在表格中填写颜色')
    sizes = specification_values(value('尺码')) or tuple(getattr(source, 'derived_size_names', ()))
    for label, values in (('颜色', colors), ('尺码', sizes)):
        if len(set(values)) != len(values):
            raise BilibiliError(f'Excel {label}规格重复')
    raw_price = value('售价', '销售价', '商品价格', '价格', '基本售价', '吊牌价')
    try:
        price = normalize_money_value(raw_price)
        if Decimal(price) <= 0:
            raise ValueError
    except (MoneyValueError, ValueError, InvalidOperation):
        raise BilibiliError('Excel B 站售价必须是有效正数') from None
    stock = integer(value('可售库存', '库存', '数量'), 'Excel 可售库存')
    default_ratio = '3:4' if getattr(source, 'main_images_34', ()) else '1:1'
    ratio = value('bilibili主图比例', 'B站主图比例', '主图比例', default=default_ratio).replace('：', ':')
    if ratio not in {'1:1', '3:4'}:
        raise BilibiliError('B 站主图比例只能是 1:1 或 3:4')
    raw_delivery = value('bilibili发货天数', 'B站发货天数', '发货天数', '发货时效', default='15')
    delivery = integer(re.sub(r'天$', '', raw_delivery), '发货天数', minimum=1)
    listing = value('bilibili上架设置', 'B站上架设置', '上架设置', '上下架', default='立即上架')
    listing = {'上架': '立即上架'}.get(listing, listing)
    if listing not in {'立即上架', '下架'}:
        raise BilibiliError('B 站目前支持立即上架或下架；定时操作请在平台手工设置')
    freight = value('bilibili运费模板', 'B站运费模板', '运费模板', '运费模版')
    if not freight:
        raise BilibiliError('Excel 缺少 B 站运费模板（请填写当前店铺的完整模板名称）')
    result = BilibiliProduct(source, MappingProxyType(normalized), brand, colors, sizes, price, stock,
                            value('商品编码', '商家编码', default=source.style_code), ratio, delivery,
                            freight, listing)
    if not result.images:
        raise BilibiliError(f'B 站所选 {ratio} 主图素材不能为空')
    if not source.detail_images:
        raise BilibiliError('B 站详情图不能为空')
    if len(source.detail_images) > 50:
        raise BilibiliError('B 站详情图最多 50 张，请先调整素材')
    for path in (*result.images, *source.detail_images, *source.sku_images):
        if not Path(path).is_file() or Path(path).stat().st_size > 20 * 1024**2:
            raise BilibiliError(f'图片不存在或超过 20MB：{path}')
    return result


def read_bilibili_product(excel_path, *, materials_root=None):
    # B 站工作簿使用“售价/可售库存/商品类目”，独立读取，复用图片发现和学习输入结构。
    from openpyxl import load_workbook
    from kuaimai_erp import ProductData, read_excel_fields, normalize_cell, find_image_dir, list_images, parse_category_hints
    from category_profile import category_profile
    excel_path = Path(excel_path).expanduser().resolve()
    workbook = load_workbook(excel_path, read_only=True, data_only=True)
    try:
        fields = {k: normalize_cell(v) for k, v in read_excel_fields(workbook.worksheets[0].iter_rows(values_only=True)).items()}
    finally:
        workbook.close()
    value = lambda *aliases: excel_value(fields, *aliases)
    title, code = value('商品标题', '商品名称', '宝贝标题'), value('货号', '款式编码', '商家外部编码')
    if not title or not code:
        raise BilibiliError('B 站 Excel 缺少商品标题或货号')
    folder = excel_path.parent
    def images(names, label, *, optional=False):
        if optional and not any((folder / name).is_dir() for name in names):
            return []
        return list_images(find_image_dir(folder, names, label), label)
    main = images(('1：1主图', '1:1主图', '主图/1：1', '主图/1:1', '主图'), '1:1 主图', optional=True)
    main34 = images(('3：4主图', '3:4主图', '主图/3：4', '主图/3:4'), '3:4 主图', optional=True)
    details = images(('详情页图/images', '详情页图', '商品详情图/images', '商品详情图', '详情页/images', '详情页'), '详情图')
    skus = images(('SKU图', 'sku图', 'Sku图'), 'SKU 图')
    colors = specification_values(value('颜色'))
    hints = parse_category_hints(value('商品类目', '商品分类', '类目'))
    source = ProductData(excel_path, folder, title, code, value('售价', '价格', '基本售价', '吊牌价'),
                         main, main34, details, skus, category_hints=hints, colors=colors,
                         garment_kind=category_profile(hints, title).garment_kind, descriptive_fields=fields)
    return parse_bilibili_product(source, fields)
