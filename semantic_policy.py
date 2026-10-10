"""Versioned evidence gates; model confidence is not an observed accuracy rate."""

import re

POLICY_VERSION = "semantic-evidence-v2"
STYLE_JUDGMENT_FIELDS = frozenset({'风格', '细分风格', '基础风格'})


def is_style_judgment_field(field_label):
    label = re.sub(r'[\s*:：]+', '', str(field_label).split('#', 1)[0]).replace('重要', '')
    return label in STYLE_JUDGMENT_FIELDS


def adoption_policy(answer, request, *, allow_auto=True):
    assessment = answer.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    relation = assessment.get("relation", "unclassified")
    preference = (relation == 'preference' and request.control_type == 'select'
                  and is_style_judgment_field(getattr(request, 'field_label', '')))
    threshold = .70 if preference else (.90 if relation == "equivalent" and request.control_type != "category" else .95)
    support = answer.get("grounded_support")
    grounded_kinds = {quote.get("source") for quote in support if isinstance(quote, dict)} if isinstance(support, list) else set()
    evidence_kinds = set(answer.get("evidence_kinds", []))
    competing = assessment.get('competing_value_ids')
    acceptable = assessment.get('acceptable_value_ids')
    if preference and isinstance(competing, list) and isinstance(acceptable, list):
        competing = [ident for ident in competing if ident not in acceptable]
    reason = ""
    if not allow_auto or (request.evidence.get("selection_only") and not request.evidence.get("bounded_write_retry")):
        reason = "已有审核冲突或页面操作失败，需要人工确认"
    elif (request.evidence.get('bounded_write_retry')
          and request.evidence.get('failed_selection_source') in {'human_override', 'cross_product_human', 'review_learning'}
          and set(answer.get('value_ids', [])) != set(request.evidence.get('failed_selection_value_ids', []))):
        reason = "模型建议与已有人工选择不同，需要人工确认"
    elif relation not in {"equivalent", "inferred"} and not preference:
        reason = "模型未提供完整的语义判定依据"
    elif assessment.get("conflicts") != [] or competing != []:
        reason = "存在证据冲突或其他可能候选"
    elif assessment.get("assumptions") != []:
        reason = "判断依赖未经证实的商品假设"
    elif assessment.get("source_text") != request.excel_value.strip() or (relation == "equivalent" and not request.excel_value.strip()):
        reason = "未准确引用 Excel 原文，无法校验映射依据"
    elif preference and not (
        isinstance(assessment.get('acceptable_value_ids'), list)
        and assessment['acceptable_value_ids']
        and all(isinstance(ident, str) for ident in assessment['acceptable_value_ids'])
        and len(set(assessment['acceptable_value_ids'])) == len(assessment['acceptable_value_ids'])
        and set(answer.get('value_ids', [])).issubset(assessment['acceptable_value_ids'])
        and all(sum(c.value_id == ident for c in request.candidates) == 1
                for ident in assessment['acceptable_value_ids'])
    ):
        reason = '运营选择未列出有效且包含所选值的可用候选'
    elif preference and not (('excel' in evidence_kinds and request.excel_value.strip())
                            or grounded_kinds.intersection(evidence_kinds)):
        reason = '运营选择缺少可核对的商品或历史依据'
    elif relation == "inferred" and len(evidence_kinds) < 2:
        reason = "推断类判断需要至少两类支持依据"
    elif relation == "inferred" and (not grounded_kinds or not (evidence_kinds - {"excel"}).issubset(grounded_kinds)):
        reason = "商品资料或人工经验中的支持原文未通过校验"
    elif answer["confidence"] < threshold:
        reason = "{0}判断未达到 {1:.0%} 门槛".format("运营" if preference else "同义" if relation == "equivalent" else "推断", threshold)
    return {"version": POLICY_VERSION, "relation": relation, "threshold": threshold,
            "auto_adopt": not reason,
            "acceptable_value_ids": assessment.get('acceptable_value_ids', []) if preference else [],
            "reason": reason + "，转人工审核" if reason else (
                '运营风格选择有依据，其他合理候选不构成冲突，自动采用' if preference else
                "原文、候选唯一性及证据门槛通过，自动采用")}
