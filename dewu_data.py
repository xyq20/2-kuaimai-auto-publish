"""Dewu inputs: its own product folders, with the shared Excel field reader."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Mapping

from openpyxl import load_workbook

from douyin_data import field_lookup


DEFAULT_DEWU_ROOT = Path('/Volumes/美工文件/得物上款')
DEWU_START_URL = 'https://stark.dewu.com/vueProduct/newProductApply/start?noLayout=1'
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png'}


class DewuError(RuntimeError):
    """An actionable input or form error; never a reason to submit anyway."""


def natural_key(path: Path):
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r'(\d+)', path.as_posix()))


def image_files(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(sorted((p for p in directory.rglob('*')
                         if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
                         and not any(part.startswith('.') for part in p.relative_to(directory).parts)),
                        key=natural_key))


def discover_products(root: Path) -> tuple[Path, ...]:
    """Only product-level workbooks count. Root-level templates never do."""
    if not root.is_dir():
        raise DewuError(f'得物上款目录不存在：{root}')
    return tuple(sorted((p for p in root.glob('*/产品信息.xlsx') if p.is_file()), key=natural_key))


def select_workbook(root: Path, product: str = '', excel: str = '') -> Path:
    if excel:
        path = Path(excel).expanduser().resolve()
        if not path.is_file():
            raise DewuError(f'产品信息表不存在：{path}')
        return path
    candidates = discover_products(root)
    if product:
        candidates = tuple(p for p in candidates if p.parent.name == product)
    if len(candidates) != 1:
        names = '、'.join(p.parent.name for p in candidates) or '无匹配商品'
        raise DewuError(f'请用 --dewu-product 指定唯一商品目录名（{names}）')
    return candidates[0]


def category_path(value: str) -> tuple[str, ...]:
    parts = tuple(p.strip() for p in re.split(r'\s*(?:>+|＞+|»|→)\s*', value) if p.strip())
    if not parts:
        raise DewuError('Excel 缺少类目')
    return parts


def split_values(value: str) -> tuple[str, ...]:
    parts = tuple(p.strip() for p in re.split(r'[/／,，、;；\n]+', value) if p.strip())
    if not parts:
        raise DewuError('Excel 规格不能为空')
    normalized = tuple(re.sub(r'\s+', '', p).casefold() for p in parts)
    if len(set(normalized)) != len(parts):
        raise DewuError(f'Excel 规格重复：{value}')
    return parts


def amount(value: str, label: str, *, integer=False, positive=False) -> str:
    raw = value.strip().removesuffix('元').replace(',', '').strip()
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise DewuError(f'{label}不是有效数字：{value!r}') from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise DewuError(f'{label}必须是有效的{"正" if positive else "非负"}数')
    if integer and number != number.to_integral_value():
        raise DewuError(f'{label}必须是整数')
    return format(number.normalize(), 'f')


def chinese_measurement(label: str) -> str:
    """Preserve the Chinese measurement name; remove English and cm units."""
    name = ''.join(re.findall(r'[\u4e00-\u9fff]+', label))
    if not name:
        raise DewuError(f'尺码参数没有中文名称：{label}')
    # Platform canonical names differ from the image's Chinese/English heading.
    return {'脚围': '裤脚围', '脚口': '裤脚围', '脚口围': '裤脚围',
            '袖口': '袖口围'}.get(name, name)


def split_detail_images(images: tuple[Path, ...]) -> dict[str, tuple[Path, ...]]:
    if len(images) > 43:
        raise DewuError(f'详情图 {len(images)} 张超过三个区域总上限 43 张，请先调整素材')
    return {'商品展示': images[:20], '穿搭效果': images[20:35], '细节呈现': images[35:]}


@dataclass(frozen=True)
class DewuProduct:
    excel_path: Path
    fields: Mapping[str, str]
    brand: str
    category: tuple[str, ...]
    audience: str
    selling_point: str
    style_code: str
    release_price: str
    link: str
    colors: tuple[str, ...]
    sizes: tuple[str, ...]
    white_images: tuple[Path, ...]
    other_images: tuple[Path, ...]
    detail_images: tuple[Path, ...]
    size_image: Path
    bid_type: str
    sku_code: str
    stock: str
    bid_price: str
    listed: str

    @property
    def initial_images(self):
        return self.white_images + self.other_images

    def summary(self):
        return {
            'platform': 'dewu', 'excel_path': str(self.excel_path),
            'product_dir': str(self.excel_path.parent), 'title': self.selling_point,
            'style_code': self.style_code, 'brand': self.brand, 'category': list(self.category),
            'title_category': self.category[-1], 'audience': self.audience,
            'release_price': self.release_price, 'colors': list(self.colors), 'sizes': list(self.sizes),
            'initial_images': [str(p) for p in self.initial_images],
            'detail_groups': {k: [str(p) for p in v] for k, v in split_detail_images(self.detail_images).items()},
            'size_image': str(self.size_image), 'size_fit': '正码',
            'size_template': '通用裤装模板' if any('裤' in p for p in self.category) else '按页面类目模板',
            'try_on_template': '钊叔', 'submit_review': False,
        }


def read_dewu_product(excel_path: Path) -> DewuProduct:
    # Import lazily to keep the standalone adapter independent of the ERP runner.
    from kuaimai_erp import read_excel_fields, normalize_cell
    workbook = load_workbook(excel_path, read_only=True, data_only=True)
    try:
        fields = {k: normalize_cell(v) for k, v in
                  read_excel_fields(workbook.worksheets[0].iter_rows(values_only=True)).items()}
    finally:
        workbook.close()

    def value(*aliases, required=True):
        match = field_lookup(fields, *aliases)
        result = normalize_cell(match[1]) if match else ''
        if required and not result:
            raise DewuError(f'Excel 缺少必填字段：{aliases[0]}')
        return result

    folder = excel_path.parent
    white = image_files(folder / '白底图')
    other = image_files(folder / '11')
    details = image_files(folder / '详情页') or image_files(folder / '详情页图')
    charts = tuple(p for name in ('尺码表', '尺码信息表') for p in image_files(folder / name))
    if not white or not other or not details:
        raise DewuError(f'商品目录须包含白底图、11、详情页图片：{folder}')
    if len(white) + len(other) > 8:
        raise DewuError('得物创建新品的商品图片上限 8 张，白底图与 11 合计超限')
    if len(charts) != 1:
        raise DewuError(f'尺码表/尺码信息表目录须有且仅有一张尺码图片，当前 {len(charts)} 张')
    split_detail_images(details)
    # The start page and carousel have different size limits; validate both now.
    import cv2
    import numpy as np
    for image in white + other + details + charts:
        decoded = cv2.imdecode(np.fromfile(str(image), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if decoded is None:
            raise DewuError(f'图片无法解码：{image}')
        height, width = decoded.shape[:2]
        if image in white + other:
            if width != height or width < 750 or width > 4096 or image.stat().st_size > 5 * 1024**2:
                raise DewuError(f'商品图片须为 750–4096 像素正方形且不超过 5MB：{image}')
        elif height > 30000 or image.stat().st_size > 20 * 1024**2:
            raise DewuError(f'图片超过高度 30000 或大小 20MB：{image}')
    listed = value('上下架', '商品状态')
    if listed not in {'上架', '下架'}:
        raise DewuError('Excel 上下架只能是“上架”或“下架”')
    return DewuProduct(
        excel_path.resolve(), fields, value('品牌', '商品品牌'), category_path(value('类目', '商品类目', '商品分类')),
        value('适用人群'), value('卖点提炼'), value('货号', '款式编码'),
        amount(value('发售价格'), '发售价格', integer=True, positive=True), value('外网链接', '商品链接'),
        split_values(value('颜色')), split_values(value('尺码', '尺码（尺码表）（尺码推荐）（试穿报告）')),
        white, other, details, charts[0], value('出价类型'), value('商品编码'),
        amount(value('库存'), '库存', integer=True, positive=True),
        amount(value('出价'), '出价', positive=True), listed,
    )
