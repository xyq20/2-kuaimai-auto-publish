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
    level.  The distinction is important: treating a slash alternative as a
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
        return self.garment_kind in {"pants", "clothing"}

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
    has_clothing = any(marker in text for marker in CLOTHING_MARKERS)
    if has_pants and not has_footwear:
        kind = "pants"
    elif has_footwear and not has_pants:
        kind = "footwear"
    elif has_clothing and not has_footwear:
        kind = "clothing"
    else:
        kind = "generic"
    return CategoryProfile(cleaned, kind)


def category_search_terms(hints: Iterable[str]) -> Tuple[str, ...]:
    """Return distinct hints from most specific to broadest.

    A longer exact label is normally the marketplace leaf (for example
    ``男士休闲夹克`` before ``夹克``).  Original order is retained for equal
    lengths so the result is deterministic.
    """

    cleaned = tuple(
        dict.fromkeys(
            option
            for value in hints
            for option in category_input_hints(value)
            if option
        )
    )
    return tuple(
        value
        for _index, value in sorted(
            enumerate(cleaned), key=lambda item: (-len(item[1]), item[0])
        )
    )


def preferred_category_leaf(hints: Iterable[str]) -> str:
    terms = category_search_terms(hints)
    return terms[0] if terms else ""


def category_path_parts(value: object) -> Tuple[str, ...]:
    """Normalize a marketplace path into its first option at each level.

    Marketplace responses occasionally render alternatives with ``/`` inside
    one level.  Callers that need every option should use
    :func:`category_path_levels`; this compatibility view keeps one stable
    segment per ``>`` level.
    """

    return tuple(level[0] for level in category_path_levels(value) if level)


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
    expected_paths = tuple(
        tuple(
            re.sub(r"[\s*:：]+", "", part).casefold()
            for part in variant
        )
        for hint in path_hints
        for variant in category_input_variants(hint)
    )
    terms = tuple(
        re.sub(r"[\s*:：]+", "", value).casefold()
        for value in category_search_terms(hint_values)
    )
    gender_roots = tuple(root.casefold() for root in category_gender_roots(hint_values))

    def flattened(levels: Tuple[Tuple[str, ...], ...]) -> Tuple[str, ...]:
        return tuple(option for level in levels for option in level)

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

    complete = []
    for text in paths:
        levels = candidate_levels[text]
        if not has_gender_root(levels):
            continue
        if any(contains_ordered(levels, expected) for expected in expected_paths):
            complete.append(text)
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
            if has_gender_root(candidate_levels[text])
            and candidate_levels[text]
            and normalized_leaf in candidate_levels[text][-1]
        ]
        if matching:
            shortest = min(len(candidate_levels[text]) for text in matching)
            winners = [
                text for text in matching if len(candidate_levels[text]) == shortest
            ]
            if len(winners) == 1:
                return winners[0], "excel_hint_exact"
            return "", "ambiguous_hint"

    scored = []
    # A gender root is a branch constraint, not a category leaf.  When the
    # Excel value also contains a concrete leaf (for example ``牛仔裤``), do
    # not let the shared ``男装`` segment score an unrelated candidate such
    # as ``男装 > 休闲裤``.
    scoring_terms = tuple(term for term in terms if term not in gender_roots) or terms
    for text in paths:
        levels = candidate_levels[text]
        parts = flattened(levels)
        if not has_gender_root(levels):
            continue
        score = 0
        for index, wanted in enumerate(scoring_terms):
            if wanted in parts:
                score += 1000 - index * 10 + len(wanted)
        # Gender narrows a category match; it cannot create one by itself.
        # Otherwise any male category would match an unrelated Excel leaf
        # such as “牛仔裤” vs “休闲裤”.
        if score and gender_roots and any(root in parts for root in gender_roots):
            score += 5000
        if score:
            # When the Excel hint only identifies a lower branch (for example
            # 男装/夹克), a prediction may prepend unrelated levels such as
            # 新制造. Prefer the shortest matching path; it is the candidate
            # that introduces the fewest category levels absent from Excel.
            scored.append((score, len(parts), text))
    if not scored:
        return "", ""
    best_score = max(score for score, _path_length, _text in scored)
    score_winners = tuple(
        (path_length, text)
        for score, path_length, text in scored
        if score == best_score
    )
    shortest_path = min(path_length for path_length, _text in score_winners)
    winners = tuple(
        text for path_length, text in score_winners if path_length == shortest_path
    )
    if len(winners) != 1:
        return "", "ambiguous_hint"
    return winners[0], "excel_hint_exact"


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
    "preferred_category_leaf",
    "category_path_parts",
    "category_path_levels",
    "category_gender_roots",
    "choose_category_candidate",
    "choose_category_object",
]
