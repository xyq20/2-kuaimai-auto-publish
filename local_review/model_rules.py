"""Model-authored proposals, bounded by deterministic human-evidence validation."""
import hashlib
import json
import re
from .database import canonical_json, connect, utc_now

VERSION = 'human-grounded-rules-v1'


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def scope(data):
    return {k: data[k] for k in ('platform_id', 'category_leaf_id', 'field_label', 'control_type', 'excel_value')}


def evidence(data, context):
    intent = [v.strip() for v in re.split(r'[/／]', data['excel_value']) if v.strip()]
    return [e for e in context['human_examples'] if e.get('same_category')
            and e.get('excel_candidates') == intent and e.get('control_type') == data['control_type']]


def validate(data, context, proposal):
    rows = evidence(data, context)
    reason = ''
    labels = proposal.get('selected_labels') if isinstance(proposal, dict) else None
    if not isinstance(labels, list) or not labels or not all(isinstance(v, str) for v in labels) or len(set(labels)) != len(labels):
        reason = 'invalid_labels'
    elif proposal.get('_candidate_labels') is not None and proposal['_candidate_labels'] != sorted(o['label'] for o in data['options']):
        reason = 'candidate_set_changed'
    elif proposal.get('relation') != 'equivalent' or proposal.get('exceptions') != []:
        reason = 'inference_or_exceptions'
    elif data['field_label'] not in {'风格', '细分风格', '基础风格', '版型', '版型分类', '服装版型'} or data['control_type'] == 'category' or re.search(r'年龄|适用|材质|成分|品牌|价格|库存|运费|物流|上市|尺码|尺寸|长度|宽度|性别', data['field_label']) or re.search(r'\d', data['excel_value']):
        reason = 'fact_or_category_requires_judgment'
    elif not data['excel_value'].strip() or data['evidence'].get('selection_only') or data['evidence'].get('validation_error'):
        reason = 'explicit_review_or_missing_intent'
    elif (context.get('history_summary', {}).get('history_scan_truncated')
          or context.get('history_summary', {}).get('human_records_matched', 0) > len(context['human_examples'])):
        reason = 'history_truncated'
    elif len({e.get('style_code') for e in rows if e.get('style_code')}) < 3:
        reason = 'needs_three_independent_products'
    elif any(e.get('no_fill') or set(e['selected_labels']) != set(labels) for e in rows):
        reason = 'human_conflict'
    elif any(sum(o['label'] == label for o in data['options']) != 1 for label in labels):
        reason = 'candidate_missing_or_ambiguous'
    elif data['control_type'] == 'select' and len(labels) != 1:
        reason = 'cardinality_mismatch'
    return {'version': VERSION, 'passed': not reason, 'reason': reason or 'unanimous_human_mapping',
            'review_ids': [e['review_id'] for e in rows],
            'independent_products': len({e.get('style_code') for e in rows if e.get('style_code')})}


def reuse(settings, data, context):
    key = digest(scope(data))
    with connect(settings) as c:
        rows = c.execute("SELECT * FROM model_learning_rules WHERE scope_key=? AND status='active' ORDER BY created_at DESC", (key,)).fetchall()
        for row in rows:
            proposal = json.loads(row['proposal_json'])
            check = validate(data, context, proposal)
            if not check['passed']:
                c.execute("UPDATE model_learning_rules SET status='suspended',validation_json=?,updated_at=? WHERE id=?",
                          (canonical_json(check), utc_now(), row['id']))
                continue
            return row['id'], proposal, check
    return None


def save_proposal(settings, data, context, proposal):
    if (not isinstance(proposal, dict) or not isinstance(proposal.get('selected_labels'), list)
            or not proposal['selected_labels'] or not all(isinstance(v, str) for v in proposal['selected_labels'])
            or len(canonical_json(proposal)) > 12000):
        return None
    proposal = {**proposal, "_candidate_labels": sorted(o["label"] for o in data["options"])}
    check = validate(data, context, proposal)
    key, version = digest(scope(data)), digest(evidence(data, context))
    ident = digest([VERSION, key, version, proposal])
    now = utc_now()
    with connect(settings) as c:
        c.execute('INSERT OR IGNORE INTO model_learning_rules VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (ident, key, canonical_json(scope(data)), canonical_json(evidence(data, context)), version, 'active' if check['passed'] else 'pending_review',
                   canonical_json(proposal), canonical_json(check), now, now))
    return {'id': ident, **check}


def approved_guidance(settings, data, context):
    """Approved complex rules inform the model; they never execute code or bypass gates."""
    result = []
    with connect(settings) as c:
        for row in c.execute("SELECT * FROM model_learning_rules WHERE scope_key=? AND status='approved_guidance' ORDER BY created_at DESC", (digest(scope(data)),)).fetchall():
            if (row['evidence_version'] != digest(evidence(data, context))
                    or json.loads(row['proposal_json']).get('_candidate_labels') != sorted(o['label'] for o in data['options'])):
                c.execute("UPDATE model_learning_rules SET status='suspended',updated_at=? WHERE id=?", (utc_now(), row['id']))
                continue
            result.append({'rule_id': row['id'], 'proposal': json.loads(row['proposal_json']),
                           'scope': json.loads(row['scope_json']), 'source': 'human_approved_guidance'})
    return result[:5]
