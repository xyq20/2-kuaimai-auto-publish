"""Resolve artwork by the current Shanghai month and an exact product identity."""
from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
import re
import unicodedata
from typing import Sequence
from zoneinfo import ZoneInfo


DEFAULT_MATERIALS_ROOT = Path("/Volumes/美工文件/主图、详情页")
_MONTH = re.compile(r"^(\d{4}|\d{2})[.年\-/](\d{1,2})(?:月份|月)?$")
_BRACKETS = re.compile(r"[【\[]([^】\]]+)[】\]]")


class MaterialPathError(RuntimeError):
    pass


def default_materials_root() -> Path:
    return Path(os.environ.get("KUAIMAI_MATERIALS_ROOT", str(DEFAULT_MATERIALS_ROOT))).expanduser()


def _text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def _month(value: str):
    match = _MONTH.fullmatch(_text(value))
    if not match:
        return None
    year, month = map(int, match.groups())
    if year < 100:
        year += 2000
    return (year, month) if 1 <= month <= 12 else None


def current_month_directory(root: Path, *, on_date: date | None = None) -> Path:
    today = on_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    root = Path(root).expanduser()
    # A previously supplied 26.10 path must also roll over next month.
    if _month(root.name):
        root = root.parent
    expected = (today.year, today.month)
    try:
        matches = sorted(p for p in root.iterdir() if p.is_dir() and _month(p.name) == expected)
    except OSError as exc:
        raise MaterialPathError(f"无法读取素材根目录：{root}；{exc}") from exc
    if len(matches) != 1:
        if matches:
            raise MaterialPathError("当月素材目录不唯一：" + "、".join(str(p) for p in matches))
        raise MaterialPathError(f"找不到 {today.year} 年 {today.month} 月素材目录：{root}")
    return matches[0]


def _name_and_color(name: str) -> tuple[str, str]:
    normalized = _text(name)
    match = _BRACKETS.search(normalized)
    if match:
        return match[1], normalized[match.end():].strip("+_-—")
    return re.sub(r"^\d{1,2}[.]\d{1,2}", "", normalized), ""


def resolve_product_materials(
    root: Path,
    excel_path: Path,
    *,
    title: str = "",
    style_code: str = "",
    colors: Sequence[str] = (),
    nickname: str = "",
) -> Path:
    month_dir = current_month_directory(root)
    excel_path = Path(excel_path).expanduser()
    # 当工作台选中的 Excel 就放在当月商品素材目录中时，该目录本身
    # 是明确的商品选择；目录末尾也可能是“低帮”等款式描述而非颜色。
    # 跨目录的旧资料仍使用下面的货号、名称和颜色匹配规则。
    if excel_path.is_file() and excel_path.parent.parent.resolve() == month_dir.resolve():
        selected = excel_path.parent.resolve()
        if selected.parent != month_dir.resolve():
            raise MaterialPathError(f"商品素材目录超出当月目录：{excel_path.parent}")
        if not excel_path.resolve().is_relative_to(selected):
            raise MaterialPathError(f"商品 Excel 超出商品素材目录：{excel_path}")
        return selected
    folder_name = excel_path.parent.name
    if style_code:
        folder_name = re.sub(
            r"[+_\s-]*" + re.escape(style_code) + r"$", "", folder_name, flags=re.IGNORECASE
        )
    title_match = _BRACKETS.search(_text(title))
    names = {_name_and_color(folder_name)[0]}
    if title_match:
        names.add(title_match[1])
    if nickname:
        names.add(_name_and_color(nickname)[0])
    names.discard("")
    expected_colors = {_text(c) for c in colors if c}
    try:
        folders = sorted(p for p in month_dir.iterdir() if p.is_dir())
    except OSError as exc:
        raise MaterialPathError(f"无法读取当月素材目录：{month_dir}；{exc}") from exc
    code_pattern = (re.compile(r"(?<![a-z0-9])" + re.escape(_text(style_code)) + r"(?![a-z0-9])")
                    if style_code else None)
    code_matches = [p for p in folders if code_pattern and code_pattern.search(_text(p.name))]
    matches = code_matches or [p for p in folders if _name_and_color(p.name)[0] in names]
    def color_suffix(path):
        name = code_pattern.sub("", _text(path.name)) if code_pattern else _text(path.name)
        return _name_and_color(name)[1]
    color_matches = [p for p in matches if color_suffix(p) and color_suffix(p) in expected_colors]
    matches = color_matches or [p for p in matches if not color_suffix(p)]
    if len(matches) != 1:
        identity = nickname or (title_match[1] if title_match else folder_name) or style_code
        if matches:
            raise MaterialPathError(f"商品 {identity} 匹配到多个素材目录：" + "、".join(p.name for p in matches))
        raise MaterialPathError(f"当月素材目录中找不到商品 {identity}（货号 {style_code}）：{month_dir}")
    selected = matches[0].resolve()
    if not selected.is_relative_to(month_dir.resolve()):
        raise MaterialPathError(f"商品素材目录超出当月目录：{matches[0]}")
    return selected
