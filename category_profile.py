"""Shared, conservative product-category decisions.

Excel category cells are supplier hints.  They are not assumed to be the
literal taxonomy path of every marketplace.  Platform adapters may search the
hints, but must still prove one exact API/DOM candidate before clicking it.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from itertools import product
from typing import Iterable, Optional, Tuple
import unicodedata


PANTS_MARKERS = ("裤",)
FOOTWEAR_MARKERS = ("鞋", "靴")
BELT_MARKERS = ("腰带", "皮带", "腰链", "腰封")
# These known belt category labels contain literal slashes. Keep them intact
# across platforms instead of turning their words into separate categories.
_COMPOSITE_CATEGORY_LABELS = {
    "腰带/皮带/腰链",
    "腰带/腰链/男士皮带",
    "腰带/皮带/腰链/背带",
}
# 拼多多在同一组合类目名称末尾增加“背带”，仍保留整段名称；
# 兼容仅用于这两个已确认的完整名称，不回退到单词类目。
_BELT_COMPOSITE_EQUIVALENTS = {"腰带/皮带/腰链", "腰带/皮带/腰链/背带"}
_CATEGORY_SEARCH_STATUS_TEXTS = {"暂无结果", "暂无数据", "无数据", "无匹配结果", "加载中", "搜索中"}
CLOTHING_MARKERS = (
    "衣",
    "服",
    "夹克",
    "外套",
    "衫",
    "裙",
    "背心",
    "马甲",
    "卫衣",
    "西装",
    "风衣",
    "棉服",
    "羽绒",
)


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def category_input_levels(value: object) -> Tuple[Tuple[str, ...], ...]:
    """Parse an Excel category expression without changing its meaning.

    ``>`` separates path levels.  ``/`` separates alternatives within one
    level, except known composite category labels such as ``腰带/皮带/腰链``.
    The distinction is important: treating a slash alternative as a
    child level can select an unrelated category whose leaf merely happens to
    share one of the supplier's hints.
    """

    if isinstance(value, (tuple, list)):
        raw_levels = tuple(_text(item) for item in value if _text(item))
    else:
        raw_levels = tuple(
            part.strip()
            for part in re.split(r"[>＞]", _text(value))
            if part.strip()
        )
    levels = []
    for raw_level in raw_levels:
        options = tuple(
            option.strip()
            for option in re.split(r"[/／]", raw_level)
            if option.strip()
        )
        composite = "/".join(options)
        if composite in _COMPOSITE_CATEGORY_LABELS:
            options = (composite,)
        if options:
            levels.append(tuple(dict.fromkeys(options)))
    return tuple(levels)


def category_input_hints(value: object) -> Tuple[str, ...]:
    """Return category alternatives in authored order, without path syntax."""

    return tuple(
        option
        for level in category_input_levels(value)
        for option in level
    )


def category_input_variants(value: object) -> Tuple[Tuple[str, ...], ...]:
    """Expand a category expression into concrete ``>`` paths."""

    levels = category_input_levels(value)
    if not levels:
        return ()
    return tuple(tuple(parts) for parts in product(*levels))


@dataclass(frozen=True)
class CategoryProfile:
    hints: Tuple[str, ...]
    garment_kind: str

    @property
    def supports_letter_size_chart(self) -> bool:
        return self.garment_kind in {"pants", "clothing", "vest"}

    @property
    def uses_pants_recommendation_table(self) -> bool:
        return self.garment_kind == "pants"


def category_profile(hints: Iterable[str], title: str = "") -> CategoryProfile:
    cleaned = tuple(
        dict.fromkeys(
            option
            for value in hints
            for option in category_input_hints(value)
            if option
        )
    )
    text = " ".join((*cleaned, str(title or "")))
    has_pants = any(marker in text for marker in PANTS_MARKERS)
    has_footwear = any(marker in text for marker in FOOTWEAR_MARKERS)
    belt_text = " ".join(cleaned) if cleaned else str(title or "")
    has_belt = any(marker in belt_text for marker in BELT_MARKERS)
    has_clothing = any(marker in text for marker in CLOTHING_MARKERS)
    if has_belt and not has_footwear:
        kind = "accessory"
    elif has_pants and not has_footwear:
        kind = "pants"
    elif has_footwear and not has_pants:
        kind = "footwear"
    elif has_clothing and not has_footwear:
        kind = "vest" if any(marker in text for marker in ("马甲", "背心")) else "clothing"
    else:
        kind = "generic"
    return CategoryProfile(cleaned, kind)


def category_search_terms(hints: Iterable[str]) -> Tuple[str, ...]:
    """Search complete leaf names in Excel order, keeping literal names."""
    return tuple(dict.fromkeys(query for query, _path in category_search_plans(hints)))


def preferred_category_leaf(hints: Iterable[str]) -> str:
    terms = category_search_terms(hints)
    return terms[0] if terms else ""


def category_search_plans(hints: Iterable[str]) -> Tuple[Tuple[str, str], ...]:
    """Pair each complete leaf query with its authored target path."""
    if isinstance(hints, str):
        hints = (hints,)
    return tuple(dict.fromkeys(
        (variant[-1], " > ".join(variant))
        for hint in hints
        for variant in category_input_variants(hint)
        if variant
    ))


def preferred_category_hints(hints: Iterable[str]) -> Tuple[str, ...]:
    """Accept both authored belt taxonomies before trying another search."""
    plans = category_search_plans(hints)
    if not plans:
        return ()
    if plans[0][0] in _COMPOSITE_CATEGORY_LABELS:
        return tuple(dict.fromkeys(
            path for query, path in plans
            if query in _COMPOSITE_CATEGORY_LABELS
        ))
    return (plans[0][1],)


def category_path_parts(value: object) -> Tuple[str, ...]:
    """Normalize a marketplace path into its first option at each level.

    Marketplace responses occasionally render alternatives with ``/`` inside
    one level.  Callers that need every option should use
    :func:`category_path_levels`; this compatibility view keeps one stable
    segment per ``>`` level.
    """

    return tuple(level[0] for level in category_path_levels(value) if level)


def is_category_search_result(value: object) -> bool:
    """Exclude empty/search-status rows, including status text within a path."""
    parts = tuple(re.sub(r"\s+", "", part) for part in re.split(r"[>＞]", _text(value)))
    return bool(parts) and all(
        part and part.rstrip(".…") not in _CATEGORY_SEARCH_STATUS_TEXTS
        for part in parts
    )


def category_path_levels(value: object) -> Tuple[Tuple[str, ...], ...]:
    """Return normalized marketplace path levels and their slash options."""

    return tuple(
        tuple(re.sub(r"[\s*:：]+", "", option).casefold() for option in level)
        for level in category_input_levels(value)
    )


def is_leather_shoe_category(value: object) -> bool:
    levels = category_path_levels(value)
    leaf_options = levels[-1] if levels else ()
    return bool(is_footwear_category(value) and any("皮鞋" in leaf for leaf in leaf_options))


def is_footwear_category(value: object) -> bool:
    levels = category_path_levels(value)
    leaf_options = levels[-1] if levels else ()
    return bool(
        leaf_options
        and any(leaf.endswith(("鞋", "靴", "鞋子", "靴子", "鞋靴")) for leaf in leaf_options)
    )


def is_belt_category(value: object) -> bool:
    """Identify belt intent from the leaf, never from a broad parent name."""
    levels = category_input_levels(value)
    return bool(levels and any(
        marker in option for option in levels[-1] for marker in BELT_MARKERS
    ))


def category_gender_roots(hints: Iterable[str]) -> Tuple[str, ...]:
    text = " ".join(str(value) for value in hints)
    if any(marker in text for marker in ("男士", "男装", "男性", "男鞋")):
        return ("男装", "男士", "男款", "男鞋", "流行男鞋")
    if any(marker in text for marker in ("女士", "女装", "女性")):
        return ("女装", "女士", "女款")
    return ()


def choose_category_candidate(
    result_texts: Iterable[str], hints: Iterable[str]
) -> Tuple[str, str]:
    """Pick one exact category result using supplier hints.

    Platform taxonomies can omit supplier-only middle levels. Matching uses
    exact path segments, with gender as a tie-breaker. An empty result means
    the caller should keep searching or raise a platform-specific error.
    """
    paths = tuple(dict.fromkeys(str(text).strip() for text in result_texts if str(text).strip()))
    hint_values = tuple(str(value).strip() for value in hints if str(value).strip())
    if not paths or not hint_values:
        return "", ""
    path_hints = tuple(
        hint for hint in hint_values if any(separator in hint for separator in (">", "＞"))
    )
    authored_paths = tuple(category_path_levels(hint) for hint in path_hints)
    expected_paths = tuple(
        tuple(
            re.sub(r"[\s*:：]+", "", part).casefold()
            for part in variant
        )
        for hint in path_hints
        for variant in category_input_variants(hint)
    )
    gender_roots = tuple(root.casefold() for root in category_gender_roots(hint_values))

    def flattened(levels: Tuple[Tuple[str, ...], ...]) -> Tuple[str, ...]:
        return tuple(option for level in levels for option in level)

    def leaf_matches(options: Tuple[str, ...], wanted: str) -> bool:
        if wanted in _COMPOSITE_CATEGORY_LABELS:
            # A platform may group fewer of the authored belt types into one
            # leaf. Require at least two explicit members; a single subtype
            # still needs the qualified-member or semantic decision below.
            members = {member for option in options for member in option.split("/")}
            if len(members) >= 2 and members.issubset(wanted.split("/")):
                return True
        return wanted in options or (
            wanted in _BELT_COMPOSITE_EQUIVALENTS
            and bool(_BELT_COMPOSITE_EQUIVALENTS.intersection(options))
        )

    def has_gender_root(levels: Tuple[Tuple[str, ...], ...]) -> bool:
        if not gender_roots:
            return True
        # Supplier hints such as “男士休闲夹克” carry a gender signal even
        # when the marketplace path uses the shorter “男装” segment.  A
        # candidate from the opposite branch, or a generic branch with the
        # same leaf, must not win merely because it shares “夹克”.
        return any(root in flattened(levels) for root in gender_roots)

    def contains_ordered(levels: Tuple[Tuple[str, ...], ...], expected_path: Tuple[str, ...]) -> bool:
        cursor = 0
        for wanted in expected_path:
            while cursor < len(levels) and wanted not in levels[cursor]:
                cursor += 1
            if cursor == len(levels):
                return False
            cursor += 1
        return True

    candidate_levels = {
        text: category_path_levels(text)
        for text in paths
    }

    # If the live candidate set contains descendants, the shorter path is a
    # known branch even when an Excel alternative literally ends there.
    if any(is_belt_category(hint) for hint in hint_values):
        known_branches = {
            levels[:depth]
            for levels in candidate_levels.values()
            for depth in range(len(levels))
        }
        paths = tuple(text for text in paths if candidate_levels[text] not in known_branches)
    else:
        paths = tuple(text for text in paths if not any(
            len(candidate_levels[other]) > len(candidate_levels[text])
            and candidate_levels[other][:len(candidate_levels[text])] == candidate_levels[text]
            for other in paths
        ))

    for expected in expected_paths:
        complete = [
            text for text in paths
            # Explicit full paths establish their own branch. A male marker
            # in a later alternative must not reject an exact unisex path.
            if (has_gender_root(candidate_levels[text])
                or candidate_levels[text] in authored_paths)
            and candidate_levels[text]
            and expected[-1] in candidate_levels[text][-1]
            and contains_ordered(candidate_levels[text], expected)
        ]
        if len(complete) == 1:
            return complete[0], "full_ordered_path"
        if len(complete) > 1:
            return "", "ambiguous_full_path"

    # Slash alternatives are leaves at the same level.  Never treat a root
    # hint such as “服装” as a valid leaf merely because an unrelated result
    # also ends in “服装”; use the final level of each authored path.
    leaf_terms = tuple(
        re.sub(r"[\s*:：]+", "", option).casefold()
        for hint in hint_values
        for level in category_input_levels(hint)[-1:]
        for option in level
    )
    for wanted_leaf in leaf_terms:
        normalized_leaf = re.sub(r"[\s*:：]+", "", wanted_leaf).casefold()
        matching = [
            text
            for text in paths
            if (has_gender_root(candidate_levels[text])
                # A unisex composite belt leaf remains valid when a later
                # supplier alternative carries a male marker.
                or (normalized_leaf in _COMPOSITE_CATEGORY_LABELS
                    and not category_gender_roots(flattened(candidate_levels[text]))))
            and candidate_levels[text]
            and leaf_matches(candidate_levels[text][-1], normalized_leaf)
        ]
        if matching:
            if len(matching) > 1 and any(
                normalized_leaf not in candidate_levels[text][-1] for text in matching
            ):
                return "", "ambiguous_belt_leaf"
            shortest = min(len(candidate_levels[text]) for text in matching)
            winners = [
                text for text in matching if len(candidate_levels[text]) == shortest
            ]
            if len(winners) == 1:
                strategy = (
                    "excel_hint_exact" if normalized_leaf in candidate_levels[winners[0]][-1]
                    else "belt_leaf_compatible"
                )
                return winners[0], strategy
            return "", "ambiguous_hint"

    # Literal composite labels remain one search query. A platform can expose
    # an explicitly qualified member as its own leaf, e.g. the male member of
    # a mixed belt label. This is authored category intent, not an inferred
    # product gender or a platform-path alias. Unqualified members of a broad
    # group still need product/semantic evidence to choose between subtypes.
    qualified_members = tuple(dict.fromkeys(
        member
        for leaf in leaf_terms if leaf in _COMPOSITE_CATEGORY_LABELS
        for member in leaf.split("/")
        if category_gender_roots((member,))
    ))
    member_matches = []
    for text in paths:
        levels = candidate_levels[text]
        if len(levels) < 2:
            continue
        for member in qualified_members:
            if levels[-1] != (member,):
                continue
            branch_gender = category_gender_roots(flattened(levels[:-1]))
            member_gender = category_gender_roots((member,))
            if branch_gender and branch_gender != member_gender:
                continue
            member_matches.append(text)
            break
    if len(member_matches) == 1:
        return member_matches[0], "composite_member_exact"
    if member_matches:
        return "", "ambiguous_composite_member"

    # An ancestor overlap is useful evidence for the model, never an exact
    # leaf match. Returning no choice routes adapters to semantic/review flow.
    return "", ""


def choose_category_object(
    candidates: Iterable[object], hints: Iterable[str]
) -> Tuple[Optional[object], str]:
    """Choose one object with a ``path`` sequence using the shared matcher."""
    values = tuple(candidates)
    rendered = tuple(
        " > ".join(str(part) for part in getattr(candidate, "path", ()) if str(part).strip())
        for candidate in values
    )
    chosen, strategy = choose_category_candidate(rendered, hints)
    if not chosen or strategy.startswith("ambiguous"):
        return None, strategy
    matches = [
        candidate
        for candidate, path in zip(values, rendered)
        if path == chosen
    ]
    return (matches[0], strategy) if len(matches) == 1 else (None, "ambiguous_object")


__all__ = [
    "CategoryProfile",
    "category_profile",
    "category_input_levels",
    "category_input_hints",
    "category_input_variants",
    "category_search_terms",
    "category_search_plans",
    "preferred_category_leaf",
    "preferred_category_hints",
    "category_path_parts",
    "is_category_search_result",
    "is_belt_category",
    "category_path_levels",
    "category_gender_roots",
    "choose_category_candidate",
    "choose_category_object",
]
