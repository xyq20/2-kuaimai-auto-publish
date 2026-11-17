"""Small, explicit business policies shared by platform form adapters.

These policies describe field scope and actions, never infer product facts
from a previous style or a coarse garment category.
"""
import re
import unicodedata
from datetime import date
from typing import Mapping, Optional, Sequence, Tuple


CLEARED_CATEGORY_FIELDS = {"yz": frozenset({"尺码"})}


def skip_color_attribute(label: str) -> bool:
    """Color attributes are user-excluded; JD sale-spec colors are separate."""
    normalized = re.sub(r"[\s*＊：:]", "", str(label)).split("#", 1)[0]
    return normalized in {"颜色", "颜色分类", "商品颜色", "色彩", "色系"}


def without_color_attributes(items: Mapping) -> dict:
    return {key: item for key, item in items.items() if not skip_color_attribute(item[0])}


REQUIRED_SERVICES = {
    "yz": ("参加会员折扣", "支持买家申请换货", "7天无理由退货"),
}

# Freight update scope is distinct from publish targets and authorized shops.
# Never extend this list merely because another shop is visible on the page.
FREIGHT_TEMPLATE_SHOPS = {
    "wxsph": ("NEIGBORL钊叔制鞋服",),
    "xhs": ("Z钊叔制的店", "啊亮製NEIGBORL的店", "钊叔制NEIGBORL的店"),
}


LISTING_DATE_FIELDS = ("上市时间", "上市年份季节", "上市时节")


def positional_listing_date_value(
    page_label: str, source_key: str, source_value: object, *, today: Optional[date] = None
) -> Optional[str]:
    """Try the paired listing-date value, then the Excel values left to right.

    The returned slash-separated values are ordered OR candidates, not values
    to paste together into a text input.  Only the exact three-field Excel row
    uses this policy; other fields keep their existing slash semantics.
    """
    def clean(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value))
        return re.sub(r"[\s*:：]+", "", text)

    names = tuple(
        clean(part) for part in re.split(r"[/／]", str(source_key)) if clean(part)
    )
    if len(names) != 3 or set(names) != set(LISTING_DATE_FIELDS):
        return None
    values = tuple(part.strip() for part in re.split(r"[/／]", str(source_value)))
    if len(values) != 3 or any(not part for part in values):
        raise ValueError("Excel 上市时间三字段必须按位置填写三个非空值")
    label = clean(page_label)
    label = {
        "年份": "上市时间",
        "上市年份": "上市时间",
        "上市季节": "上市年份季节",
    }.get(label, label)
    if label not in names:
        return None
    current = today or date.today()

    def materialize(value: str) -> str:
        if value != "动态选择当天":
            return value
        if label in {"上市年份季节", "上市时节"}:
            season = (
                "春季" if current.month in (3, 4, 5)
                else "夏季" if current.month in (6, 7, 8)
                else "秋季" if current.month in (9, 10, 11)
                else "冬季"
            )
            return f"{current.year}年{season}"
        return current.strftime("%Y.%m.%d")

    ordered = (values[names.index(label)], *values)
    return "/".join(dict.fromkeys(materialize(value) for value in ordered))


def listing_date_text_value(page_label: str, value: object) -> str:
    """A free-text date field has no option list; enter only its paired value."""
    normalized = re.sub(r"[\s*:：]+", "", unicodedata.normalize("NFKC", str(page_label)))
    if normalized in {*LISTING_DATE_FIELDS, "年份", "上市年份", "上市季节"}:
        return str(value).split("/", 1)[0].strip()
    return str(value).strip()


def clear_category_field(platform: str, label: str) -> bool:
    return label.strip().rstrip("：:") in CLEARED_CATEGORY_FIELDS.get(platform, ())


def freight_alternatives(fields: Mapping[str, str]) -> Tuple[str, ...]:
    """Resolve aliases without splitting punctuation inside template names."""
    values = set()
    for key, value in fields.items():
        aliases = {part.strip() for part in re.split(r"[/／]", str(key))}
        if aliases.intersection({"运费设置", "运费模板", "运费模版"}) and str(value).strip():
            values.add(str(value).strip())
    if len(values) > 1:
        raise ValueError("运费设置匹配到相互冲突的 Excel 字段")
    if not values:
        return ()
    return tuple(dict.fromkeys(part.strip() for part in re.split(r"[/／]", values.pop()) if part.strip()))


def is_single_material_expression(value: str) -> bool:
    # Slash alternatives are OR, whereas explicit composition separators may
    # represent multiple materials. Never remove rows for an unparsed mixture.
    if not str(value).strip() or re.search(r"[、,，;；+＋]", value):
        return False
    component = r"[^\d%％/／]+?(?:\s*[（(]?\s*\d+(?:\.\d+)?\s*[%％]\s*[）)]?)?"
    return all(re.fullmatch(component, part.strip()) is not None
               for part in re.split(r"[/／]", value))


MATERIAL_COMPONENT_SEPARATORS = r"[,，、;；]"
_MATERIAL_PERCENTAGE = r"(\d+(?:\.\d+)?)\s*[%％]"


def is_material_field_label(label: object) -> bool:
    """Return whether the field is in the shared fabric/material scope."""
    text = unicodedata.normalize("NFKC", "" if label is None else str(label))
    normalized = re.sub(r"[\s*:：]+", "", text.replace("重要", "")).casefold()
    return bool(
        normalized
        and "含量" not in normalized
        and ("材质" in normalized or "面料" in normalized)
    )


def material_name_groups(value: object) -> Tuple[Tuple[str, ...], ...]:
    """Parse material names: commas mean multiple values, slashes mean OR."""
    groups = []
    for raw_group in re.split(MATERIAL_COMPONENT_SEPARATORS, str(value or "")):
        names = []
        normalized_names = set()
        for raw_candidate in re.split(r"[/／]", raw_group):
            candidate = raw_candidate.strip()
            if not candidate or re.fullmatch(_MATERIAL_PERCENTAGE, candidate):
                continue
            name = re.sub(
                r"\s*[（(]?\s*\d+(?:\.\d+)?\s*[%％]\s*[）)]?\s*$",
                "",
                candidate,
            ).strip()
            normalized = _normalize_option_text(name)
            if name and normalized not in normalized_names:
                normalized_names.add(normalized)
                names.append(name)
        if names:
            groups.append(tuple(names))
    return tuple(groups)


def primary_material_names(value: object) -> Tuple[str, ...]:
    """Names of the one material a single-select field should receive.

    Operator rule: a single-select fabric field takes the component with the
    highest percentage; equal (or missing) percentages keep the first one in
    Excel order.  Slash alternatives of that component stay ordered ORs.  The
    caller still sends the field to review when none of them is a candidate,
    rather than falling through to a lower-percentage component.
    """
    best_names: Tuple[str, ...] = ()
    best_percentage = None
    for raw_group in re.split(MATERIAL_COMPONENT_SEPARATORS, str(value or "")):
        parsed = material_name_groups(raw_group)
        if not parsed:
            continue
        match = re.search(_MATERIAL_PERCENTAGE, raw_group)
        percentage = float(match.group(1)) if match else None
        if not best_names or (
            percentage is not None
            and (best_percentage is None or percentage > best_percentage)
        ):
            best_names, best_percentage = parsed[0], percentage
    return best_names


def material_review_key(value: object) -> Tuple[str, ...]:
    """Identify a fabric review by its set of materials, not its Excel text.

    ``棉60%，涤纶40%``, ``棉55%，涤纶45%`` and ``涤纶40%，棉60%`` are the same
    review question, so one human answer covers all of them.  A different set
    of materials (for example cotton plus spandex) is a new question.
    """
    return tuple(sorted({
        _normalize_option_text(names[0])
        for names in material_name_groups(value)
    }))


def _normalize_option_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return re.sub(r"[\s，,;；、・·\-_（）()]", "", text).casefold()


def _normalized_parts(value: object) -> frozenset:
    """Split raw '氨纶(聚氨酯弹性纤维)' into {'氨纶', '聚氨酯弹性纤维'}.

    Bracket splitting must happen on the raw text BEFORE punctuation is
    stripped, otherwise the annotation structure is lost. Splitting first
    also makes 京东语序“俗名(化学名)” and 天猫语序“化学名(俗名)” resolve
    to the same part set.
    """
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    match = re.fullmatch(r"([^（）()]*)[（(]([^（）()]*)[）)]", text)
    parts = (
        (match.group(1), match.group(2)) if match else (text,)
    )
    normalized = {
        _normalize_option_text(part) for part in parts
    }
    return frozenset(part for part in normalized if part)


# Known material/fabric spellings outrank broad substring matches. Keep them
# here so JD's numeric-ID writer and other platforms' DOM writers agree.
# Do not strip arbitrary fabric suffixes: 棉麻/珠地棉/牛仔布 add distinct facts.
MATERIAL_OPTION_EQUIVALENTS = (frozenset({"棉", "棉布"}),)


STYLE_OPTION_FIELDS = frozenset({"款式", "裤型", "版型", "服饰版型", "服装版型", "裤脚款式", "裤脚口款式"})
STYLE_OPTION_SUFFIXES = ("裤型", "版型", "裤", "型")


def season_option_aliases(field_label: object, value: object) -> Tuple[str, ...]:
    if _normalize_option_text(field_label) not in {"适用季节", "季节"}:
        return ()
    text = str(value).strip()
    return (text[:-1],) if text in {"春季", "夏季", "秋季", "冬季"} else ()


def _style_stem(value: object) -> str:
    text = _normalize_option_text(value)
    for suffix in STYLE_OPTION_SUFFIXES:
        if text.endswith(suffix) and len(text) - len(suffix) >= 2:
            return text[:-len(suffix)]
    return text


def style_option_aliases(field_label: object, value: object) -> Tuple[str, ...]:
    """Compact garment style wording, e.g. 直筒裤 -> 直筒."""
    field = _normalize_option_text(field_label)
    text = str(value).strip()
    if field not in STYLE_OPTION_FIELDS or not text:
        return ()
    stem = _style_stem(text)
    return (stem,) if stem != _normalize_option_text(text) else ()


def match_style_option_candidates(
    field_label: object, desired: object, candidate_labels: Sequence[str]
) -> Tuple[int, ...]:
    """Match a style's removable garment suffix only when the tier is unique.

    The caller tries exact labels first.  Prefer the bare stem over another
    suffixed form; a tie is left for the ordinary review path.
    """
    if _normalize_option_text(field_label) not in STYLE_OPTION_FIELDS:
        return ()
    wanted = _normalize_option_text(desired)
    if not wanted:
        return ()
    stem = _style_stem(wanted)
    normalized = tuple(_normalize_option_text(label) for label in candidate_labels)
    bare = tuple(i for i, label in enumerate(normalized)
                 if label == stem and label != wanted)
    if bare:
        return bare
    return tuple(i for i, label in enumerate(normalized)
                 if label != wanted and _style_stem(label) == stem
                 and (stem != wanted or _style_stem(label) != label))


def heel_height_option_matches(
    field_label: str, desired: str, candidate_labels: Sequence[str]
) -> Tuple[int, ...]:
    """Match explicit shoe/boot height labels; never infer from a bare number."""
    field = _normalize_option_text(field_label)
    if field not in {"鞋跟高度", "鞋跟高", "靴筒高度", "筒高"}:
        return ()
    def height_text(value):
        return re.sub(r"(?<=\d)cm(?=\d)", "", _normalize_option_text(value))
    wanted = height_text(desired)
    classes = ({"短筒", "中筒", "高筒"} if field in {"靴筒高度", "筒高"}
               else {"平跟", "低跟", "中跟", "高跟", "超高跟"})
    if wanted in classes:
        return tuple(i for i, label in enumerate(candidate_labels)
                     if re.split(r"[（(]", str(label), maxsplit=1)[0].strip() == wanted)
    if not re.match(r"^(?:平跟|低跟|中跟|高跟|超高跟)", wanted):
        return ()
    return tuple(i for i, label in enumerate(candidate_labels)
                 if height_text(label) == wanted)


def match_option_candidates(
    desired_labels: Sequence[str],
    candidate_labels: Sequence[str],
    *, allow_component: bool = True,
) -> Tuple[int, ...]:
    """Tiered Excel-to-platform option matching shared by form adapters.

    Tier 0 (exact): the candidate equals a desired label. Excel 棉 must win
    over 京东's lookalike 木棉 even though both "contain" the name.
    Tier 1 (annotated): the candidate is the desired name plus or minus a
    parenthetical annotation, compared as whole name parts — Excel 氨纶 vs
    京东候选 氨纶(聚氨酯弹性纤维), or the reversed 天猫 alias
    聚氨酯弹性纤维(氨纶). Part equality keeps 木棉 (a different fiber that
    merely contains 棉) and 弹性纤维 (a substring of the alias, not a part)
    out of this tier.
    Tier 2 (known spelling): 棉 and 棉布 refer to the same material in platform
    dictionaries; unrelated cotton fabrics must not make that match ambiguous.
    Tier 3 (component): plain substring in either direction, e.g. Excel 羊绒
    inside 京东候选 山羊绒. Compound names legitimately contain shorter
    option names, so tier-3 only counts when no stronger candidate exists.
    Returns indices into ``candidate_labels``; the caller decides whether a
    non-unique result is an error or a review deferral.
    """
    desired = tuple(
        text
        for text in (_normalize_option_text(label) for label in desired_labels)
        if text
    )
    normalized = tuple(_normalize_option_text(label) for label in candidate_labels)
    desired_parts = tuple(_normalized_parts(label) for label in desired_labels)
    candidate_parts = tuple(
        _normalized_parts(label) for label in candidate_labels
    )

    def annotated_equivalent(candidate: frozenset, wanted: frozenset) -> bool:
        if not candidate or not wanted:
            return False
        if candidate == wanted and len(candidate) > 1:
            # Same parts, flipped annotation order (天猫 vs 京东 style).
            return True
        return len(candidate) != len(wanted) and (
            candidate < wanted or wanted < candidate
        )

    exact = tuple(
        index
        for index, text in enumerate(normalized)
        if text and any(text == want for want in desired)
    )
    if exact:
        return exact
    annotated = tuple(
        index
        for index, parts in enumerate(candidate_parts)
        if any(annotated_equivalent(parts, wanted) for wanted in desired_parts)
    )
    if annotated:
        return annotated
    known_spellings = tuple(
        index
        for index, text in enumerate(normalized)
        if text and any(
            text in group and any(want in group for want in desired)
            for group in MATERIAL_OPTION_EQUIVALENTS
        )
    )
    if known_spellings:
        return known_spellings
    if not allow_component:
        return ()
    return tuple(
        index
        for index, (raw_label, text) in enumerate(zip(candidate_labels, normalized))
        if text
        and not (
            "混纺" in _normalize_option_text(raw_label)
            or re.search(r"[与和+＋&]", str(raw_label))
        )
        and any(text in want or want in text for want in desired)
    )
