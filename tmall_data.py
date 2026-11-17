"""Inputs and local assets for the Tmall listing form."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


IMAGE_SUFFIXES = frozenset(
    (".jpg", ".jpeg", ".jfif", ".png", ".gif", ".webp", ".bmp")
)


class TmallDataError(ValueError):
    pass


class FrozenFields(Mapping[str, str]):
    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("天猫 Excel 字段不可修改")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("天猫 Excel 字段不可修改")

    def __getitem__(self, key: str) -> str:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __deepcopy__(self, _memo: Dict[int, Any]) -> "FrozenFields":
        return self


@dataclass(frozen=True)
class TmallFields:
    fields: Mapping[str, str]


@dataclass(frozen=True)
class TmallAssets:
    vertical_image: Optional[Path]
    transparent_image: Path
    parameter_image: Optional[Path]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_tmall_fields(fields: Mapping[str, Any]) -> TmallFields:
    normalized = {
        str(key).strip(): _cell_text(value)
        for key, value in fields.items()
        if str(key).strip() and _cell_text(value)
    }
    return TmallFields(fields=FrozenFields(normalized))


def _exactly_one_image(directory: Path, label: str) -> Path:
    if not directory.is_dir():
        raise TmallDataError("找不到{0}文件夹：{1}".format(label, directory))
    images = tuple(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES
    )
    if len(images) != 1:
        raise TmallDataError(
            "{0}文件夹必须恰好 1 张支持的图片，当前为 {1} 张：{2}".format(
                label,
                len(images),
                directory,
            )
        )
    return images[0]


def _first_asset_directory(
    product_dir: Path,
    names: Tuple[str, ...],
    label: str,
) -> Path:
    for name in names:
        directory = product_dir / name
        if directory.is_dir():
            return directory
    raise TmallDataError(
        "找不到{0}文件夹，已尝试：{1}".format(label, "、".join(names))
    )


def read_tmall_assets(product_dir: Path, *, garment_kind: str = "clothing") -> TmallAssets:
    product_dir = Path(product_dir)
    vertical_dir = None if garment_kind in {"footwear", "accessory"} else _first_asset_directory(
        product_dir,
        ("2:3", "2：3", "2:3图", "2：3图"),
        "2:3",
    )
    transparent_dir = product_dir / "透明素材图"
    if not transparent_dir.is_dir():
        transparent_dir = _first_asset_directory(
            product_dir,
            ("1:1", "1：1", "1:1图", "1：1图"),
            "1:1透明素材",
        )
    parameter_dir = product_dir / "尺码信息表"
    if garment_kind == "accessory" and (product_dir / "产品参数图").is_dir():
        parameter_dir = product_dir / "产品参数图"
    parameter_image = (
        _exactly_one_image(parameter_dir, parameter_dir.name)
        if parameter_dir.is_dir()
        else None
    )
    if garment_kind != "accessory" and parameter_image is None:
        raise TmallDataError(
            "找不到产品参数图或尺码信息表文件夹：{0}".format(product_dir)
        )
    return TmallAssets(
        vertical_image=_exactly_one_image(vertical_dir, "2:3") if vertical_dir else None,
        transparent_image=_exactly_one_image(transparent_dir, "1:1透明素材"),
        parameter_image=parameter_image,
    )


def prepare_tmall_transparent_image(source: Path, target: Path) -> Path:
    """Prepare an 800px square PNG for upload without modifying the source."""
    import cv2
    import numpy as np

    image = cv2.imread(str(source), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise TmallDataError(f"无法读取天猫透明素材图：{source}")
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA)
    elif image.shape[2] == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    height, width = image.shape[:2]
    scale = 800 / max(width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    resized = cv2.resize(
        image, size,
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
    )
    canvas = np.zeros((800, 800, 4), dtype=resized.dtype)
    left, top = (800 - size[0]) // 2, (800 - size[1]) // 2
    canvas[top:top + size[1], left:left + size[0]] = resized
    if not cv2.imwrite(str(target), canvas):
        raise TmallDataError(f"无法生成天猫 800×800 透明素材图：{target}")
    return target
