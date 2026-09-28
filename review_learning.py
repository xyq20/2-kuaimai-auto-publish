"""Confidence-gated reuse of human reviews, including fields without AI mappings."""
import re
import unicodedata
from collections import Counter

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
        if field_key(row['field_label']) != field_key(request['field_label']):
            continue
        if tuple(normalized(v) for v in row.get('excel_candidates', [])) != excel:
            continue
        same = row['product_version'] == request['product_version']
        if not same and (not excel or not request['category'] or row['category'] != request['category']):
            continue
        key = (row['product_version'], row['platform_id'])
        if key in seen:
            continue
        seen.add(key)
        eligible.append(row)
    same_product = [r for r in eligible if r['product_version'] == request['product_version']]
    pool = same_product or eligible
    if not pool:
        return waiting
    votes = {}
    for row in pool:
        choice = tuple(sorted(normalized(v) for v in row['labels']))
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
    selected = []
    for label in chosen:
        found = [v for v in request['options'] if normalized(v['label']) == label]
        if len(found) != 1:
            return {**stats, 'reason_code': 'review_learning_candidate_mismatch'}
        selected.append(found[0])
    selected.sort(key=lambda v: request['options'].index(v))
    return {**stats, 'reason_code': 'validated', 'same_product': bool(same_product),
            'value_ids': [v['value_id'] for v in selected],
            'value_labels': [v['label'] for v in selected]}
