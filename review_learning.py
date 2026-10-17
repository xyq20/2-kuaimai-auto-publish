"""Confidence-gated reuse of human reviews, including fields without AI mappings."""
import re
import unicodedata
from collections import Counter
from decimal import Decimal

from canonical_fields import FieldMappingError, map_platform_field

MULTI_CONTROLS = {'multi_select', 'multi-select', 'multiselect'}
FIELD_ALIASES = {
    '适用年龄段': '适用年龄', '年龄段': '适用年龄',
    '款式细节': '流行元素',
}


def normalized(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def field_key(value):
    label = normalized(value).strip('*:：')
    return FIELD_ALIASES.get(label, label)


def _category_intent(category):
    if not isinstance(category, dict) or not isinstance(category.get('hints'), list):
        return category
    # Older product registrations stored each OR alternative as a list item;
    # current registrations keep the original slash-separated Excel cell.
    # Preserve their order and all other category metadata when comparing.
    return {**category, 'hints': [normalized(part)
        for hint in category['hints'] for part in re.split(r'[/／]', str(hint))
        if normalized(part)]}


def _related_fields(request, row):
    current = field_key(request['field_label'])
    previous = field_key(row['field_label'])
    if current == previous:
        return True
    if request.get('platform_id') and row.get('platform_id'):
        try:
            if map_platform_field(request['platform_id'], request['field_label']) == map_platform_field(
                row['platform_id'], row['field_label']
            ):
                return True
        except FieldMappingError:
            pass
    # A one-character suffix is a candidate relationship, not proof by
    # itself. Excel intent, control type and the approved option are checked
    # separately before this record can contribute a vote.
    shorter, longer = sorted((current, previous), key=len)
    return len(shorter) >= 3 and len(longer) == len(shorter) + 1 and longer.startswith(shorter)


def _compatible_controls(request, row):
    previous = str(row.get('control_type') or '').casefold()
    current = str(request['control_type']).casefold()
    if not previous:
        # Old records can still reuse exact field names, but lack enough
        # evidence to infer a relationship between different names.
        return field_key(row['field_label']) == field_key(request['field_label'])
    return (previous in MULTI_CONTROLS and current in MULTI_CONTROLS) or previous == current


def _choice_equivalent(previous, current, excel):
    old, new = normalized(previous), normalized(current)
    if old == new:
        return True
    old_match = re.fullmatch(r'([^()]+)(?:\(([^()]*)\))?', old)
    new_match = re.fullmatch(r'([^()]+)(?:\(([^()]*)\))?', new)
    if not old_match or not new_match:
        return False
    base = old_match.group(1)
    if len(base) < 2 or base != new_match.group(1) or base not in excel:
        return False
    old_detail, new_detail = old_match.group(2), new_match.group(2)
    if not old_detail or not new_detail:
        return True
    # Platform ranges may differ slightly, e.g. 中跟(3-5cm) versus
    # 中跟(3-5.5cm). Keep the same unit and a tight numeric tolerance.
    if not (('cm' in old_detail or '厘米' in old_detail)
            and ('cm' in new_detail or '厘米' in new_detail)):
        return False
    old_numbers = tuple(Decimal(value) for value in re.findall(r'\d+(?:\.\d+)?', old_detail))
    new_numbers = tuple(Decimal(value) for value in re.findall(r'\d+(?:\.\d+)?', new_detail))
    return (bool(old_numbers) and len(old_numbers) == len(new_numbers)
            and all(abs(a - b) <= Decimal('0.5') for a, b in zip(old_numbers, new_numbers)))


def _remap_choice(labels, options, excel):
    selected = []
    for label in labels:
        exact = [option for option in options if normalized(option['label']) == normalized(label)]
        matches = exact or [
            option for option in options
            if _choice_equivalent(label, option['label'], excel)
        ]
        if len(matches) != 1 or matches[0] in selected:
            return None
        selected.append(matches[0])
    return tuple(selected)


def choose_review_learning(request, records):
    """Records must be genuine human approvals, newest first, never predictions.

    A product contributes one vote. Conflicting platforms for that product
    remain a conflict instead of becoming extra support. Historical agreement
    is an empirical gate, not a claim of statistical model accuracy.
    """
    waiting = {'reason_code': 'review_learning_insufficient', 'support_count': 0,
               'calibrated_acceptance_rate': 0.0}
    excel = tuple(normalized(v) for v in re.split(r'[/／]', request['excel_value']) if normalized(v))
    eligible = []
    seen = set()
    for row in records:
        # Marketplace category trees are platform-specific even when their
        # displayed paths happen to coincide. Never borrow another platform's
        # category approval as an automatic selection.
        if request['control_type'] == 'category' and row.get('platform_id') != request.get('platform_id'):
            continue
        if not _related_fields(request, row) or not _compatible_controls(request, row):
            continue
        if tuple(normalized(v) for v in row.get('excel_candidates', [])) != excel:
            continue
        same = row['product_version'] == request['product_version']
        if not same and (not excel or not request['category']
                         or _category_intent(row['category']) != _category_intent(request['category'])):
            continue
        mapped = _remap_choice(row['labels'], request['options'], excel)
        if not mapped:
            continue
        key = (row['product_version'], row['platform_id'])
        if key in seen:
            continue
        seen.add(key)
        eligible.append((row, mapped))
    same_product = [entry for entry in eligible if entry[0]['product_version'] == request['product_version']]
    pool = same_product or eligible
    if not pool:
        return waiting
    votes = {}
    for row, mapped in pool:
        choice = tuple(sorted(value['value_id'] for value in mapped))
        if not choice or len(set(choice)) != len(choice):
            return {**waiting, 'reason_code': 'review_learning_conflict'}
        previous = votes.get(row['product_version'])
        if previous is not None and previous != choice:
            return {**waiting, 'reason_code': 'review_learning_conflict'}
        votes[row['product_version']] = choice
    counts = Counter(votes.values())
    chosen, support = counts.most_common(1)[0]
    rate = support / len(votes)
    stats = {'support_count': support, 'calibrated_acceptance_rate': rate}
    if len(counts) > 1 and (rate < .95 or any(v != chosen for v in list(votes.values())[:3])):
        return {**stats, 'reason_code': 'review_learning_conflict'}
    if not same_product and support < 3:
        return {**waiting, **stats}
    if len(chosen) > 1 and request['control_type'].casefold() not in MULTI_CONTROLS:
        return {**stats, 'reason_code': 'review_learning_control_mismatch'}
    selected = [value for value in request['options'] if value['value_id'] in chosen]
    if len(selected) != len(chosen):
        return {**stats, 'reason_code': 'review_learning_candidate_mismatch'}
    return {**stats, 'reason_code': 'validated', 'same_product': bool(same_product),
            'value_ids': [v['value_id'] for v in selected],
            'value_labels': [v['label'] for v in selected]}
