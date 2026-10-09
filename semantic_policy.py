"""Versioned evidence gates; model confidence is not an observed accuracy rate."""

POLICY_VERSION = "semantic-evidence-v1"


def adoption_policy(answer, request, *, allow_auto=True):
    assessment = answer.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    relation = assessment.get("relation", "unclassified")
    threshold = .90 if relation == "equivalent" and request.control_type != "category" else .95
    support = answer.get("grounded_support")
    grounded_kinds = {quote.get("source") for quote in support if isinstance(quote, dict)} if isinstance(support, list) else set()
    evidence_kinds = set(answer.get("evidence_kinds", []))
    reason = ""
    if not allow_auto or request.evidence.get("selection_only"):
        reason = "已有审核冲突或页面操作失败，需要人工确认"
    elif relation not in {"equivalent", "inferred"}:
        reason = "模型未提供完整的语义判定依据"
    elif assessment.get("conflicts") != [] or assessment.get("competing_value_ids") != []:
        reason = "存在证据冲突或其他可能候选"
    elif assessment.get("assumptions") != []:
        reason = "判断依赖未经证实的商品假设"
    elif assessment.get("source_text") != request.excel_value.strip() or (relation == "equivalent" and not request.excel_value.strip()):
        reason = "未准确引用 Excel 原文，无法校验映射依据"
    elif relation == "inferred" and len(evidence_kinds) < 2:
        reason = "推断类判断需要至少两类支持依据"
    elif relation == "inferred" and (not grounded_kinds or not (evidence_kinds - {"excel"}).issubset(grounded_kinds)):
        reason = "商品资料或人工经验中的支持原文未通过校验"
    elif answer["confidence"] < threshold:
        reason = "{0}判断未达到 {1:.0%} 门槛".format("同义" if relation == "equivalent" else "推断", threshold)
    return {"version": POLICY_VERSION, "relation": relation, "threshold": threshold,
            "auto_adopt": not reason,
            "reason": reason + "，转人工审核" if reason else "原文、候选唯一性及证据门槛通过，自动采用"}
