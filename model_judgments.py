"""Explain model activity independently of successful predictions."""
from collections import Counter
from datetime import datetime, timezone
import logging


REASONS = {
    "judgment_request_rejected": "判断请求被服务端校验拦截，未调用模型",
    "deepseek_not_configured": "未配置 DeepSeek key，未调用模型，转人工审核",
    "deepseek_timeout": "DeepSeek 请求超时，转人工审核",
    "deepseek_auth_failed": "DeepSeek key 认证失败，转人工审核",
    "deepseek_insufficient_balance": "DeepSeek 余额不足，转人工审核",
    "deepseek_invalid_request": "DeepSeek 模型名称或请求参数无效，转人工审核",
    "deepseek_rate_limited": "DeepSeek 请求限流，转人工审核",
    "deepseek_request_failed": "DeepSeek 请求失败，转人工审核",
    "deepseek_incomplete_output": "DeepSeek 输出未完成，转人工审核",
    "deepseek_invalid_judgment": "模型结果未通过候选或证据校验，转人工审核",
    "deepseek_invalid_ocr_judgment": "模型结果未通过 OCR 原文校验，转人工审核",
    "deepseek_uncertain": "模型无法确定合适候选，转人工审核",
    "judgment_service_unavailable": "判断服务不可用，无法确认是否调用模型，转人工审核",
    "ocr_judge_unavailable": "OCR 判断服务不可用，无法确认是否调用模型，转人工审核",
    "no_candidates": "没有真实候选，无法请求模型判断",
    "too_many_candidates": "候选超过接口容量，未调用模型",
    "unsupported_control": "当前控件不支持模型判断",
    "operational_field": "价格、库存或店铺操作按确定规则处理，未调用模型",
    "explicit_text": "Excel 已唯一匹配真实候选，无需模型判断",
    "human_override": "已复用本商品人工审核结果，无需模型判断",
    "human_no_fill": "审核已明确不填写，无需模型判断",
    "cross_product_human": "已复用人工确认结果，无需模型判断",
    "verified_history": "已复用保存回读确认的结果，无需模型判断",
    "mature_rule": "成熟规则已满足自动填写条件，无需模型判断",
    "review_learning": "人工学习库已满足复用条件，无需模型判断",
    "ocr_rule_match": "OCR 规则已通过行列和数值校验，无需模型判断",
    "ocr_reused": "OCR 已复用本次确认含义，无需重复调用模型",
    "ocr_structure_invalid": "尺码原图与 Excel 的尺寸资料未通过校验；文字含义判断无法修复，请核对原始资料",
}


def reason_text(code):
    return REASONS.get(code, "判断结果不可用，转人工审核")


def api_called(response):
    value = response.get("api_called")
    if type(value) is bool:
        return value
    if response.get("reason_code") == "deepseek_not_configured":
        return False
    if response.get("source") == "deepseek" and response.get("status") == "suggested":
        return True
    return None


def record_event(owner, event, *, sink=None, logger=None):
    """One entry per request; retries using cached answers do not add calls."""
    previous = next((row for row in owner.model_events if row["key"] == event["key"]), None)
    if previous is not None and all(previous.get(k) == v for k, v in event.items()):
        return
    entry = {**event, "recorded_at": datetime.now(timezone.utc).isoformat()}
    if previous is None:
        owner.model_events.append(entry)
    else:
        previous.clear()
        previous.update(entry)
    labels = "、".join(event.get("selected_labels", [])) or "无确定候选"
    confidence = event.get("confidence")
    detail = f"；置信度={confidence:.1%}" if isinstance(confidence, (int, float)) else ""
    (logger or logging.getLogger("kuaimai_erp")).info(
        "大模型判断：%s | %s；状态=%s；原值=%s；建议=%s%s；原因=%s",
        event.get("platform_id", ""), event.get("field_label", ""),
        {"requesting": "正在请求判断服务", "skipped": "未调用", "adopted": "自动采用",
         "review_required": "转人工审核", "rejected": "校验拒绝", "unavailable": "不可用"}.get(event["status"], event["status"]),
        event.get("excel_value", "") or "无", labels, detail, event.get("reason", ""),
    )
    if sink is not None:
        sink()


def summarize(decisions, events, *, run_status="running", stop_reason=""):
    requests = [event for event in events if event["status"] != "skipped"]
    calls = sum(event.get("api_called") is True for event in requests)
    unknown = sum(event.get("api_called") is None for event in requests)
    skipped = Counter(event.get("reason_code", "") for event in events if event["status"] == "skipped")
    explanation = ""
    if not requests:
        if run_status in {"failed", "interrupted"}:
            explanation = "未调用大模型：运行提前中断，尚未触发需要模型判断的字段。中断原因：" + (stop_reason or "请查看运行错误")
        elif run_status == "completed":
            explanation = "未调用大模型：已处理字段由 Excel、规则或已有审核结果确定，没有触发模型兜底。"
        else:
            explanation = "尚未调用大模型：当前没有触发需要模型判断的字段。"
    elif not calls and not unknown:
        explanation = "未调用大模型：" + "；".join(dict.fromkeys(event.get("reason", "") for event in requests))
    elif not calls and unknown:
        explanation = "判断服务未返回完整调用状态，是否到达 DeepSeek 接口暂无法确认。"
    return {
        "request_count": len(requests), "api_call_count": calls, "unknown_call_count": unknown,
        "decision_count": len(decisions),
        "adopted_count": sum(row["status"] == "adopted" for row in decisions),
        "review_count": sum(event["status"] == "review_required" for event in requests),
        "unavailable_count": sum(event["status"] == "unavailable" for event in requests),
        "rejected_count": sum(event["status"] == "rejected" for event in requests),
        "skipped_count": sum(skipped.values()),
        "skip_reasons": [{"reason": reason_text(code), "count": count} for code, count in skipped.items()],
        "not_called_reason": explanation, "run_status": run_status, "stop_reason": stop_reason,
    }
