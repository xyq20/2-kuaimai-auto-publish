"""Use human learning first, then bounded DeepSeek label interpretation."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging
import hashlib
import mimetypes

from attribute_runtime import AttributeRequest, ReviewBatchRequired, ReviewRequired
from learning_client import LearningClientError
from learning_models import CandidateValue, canonical_sha256
from ocr_semantics import MEANINGS, OCRJudgmentRequired, OCRSemanticSession, validate_suggestions
from size_image_recognition import RecognitionError, SizeSetMismatch, _matches_alias_composition, _MEASUREMENT_ALIASES, _CLOTHING_MEASUREMENT_ALIASES, _CLOTHING_EXTRA_ROW_ALIASES


def unresolved_labels(error):
    aliases = (_MEASUREMENT_ALIASES if error.garment_kind == "pants" else
               {**_CLOTHING_MEASUREMENT_ALIASES, **_CLOTHING_EXTRA_ROW_ALIASES})
    return tuple(i for i in error.label_token_ids
                 if not any(_matches_alias_composition(error.tokens[i].text, a) for a in aliases.values()))


async def resolve_ocr_failure(runtime, client, session: OCRSemanticSession,
                              error: OCRJudgmentRequired, logger: logging.Logger, *, platform_id="base"):
    meanings = MEANINGS[error.garment_kind]
    unknown = unresolved_labels(error)
    if isinstance(error.failure, SizeSetMismatch) or not unknown:
        from model_judgments import record_event
        if isinstance(error.failure, SizeSetMismatch):
            detail = (f"Excel 尺码：{'、'.join(error.failure.expected)}；"
                      f"尺码原图识别到：{'、'.join(error.failure.actual) or '无'}。"
                      "请核对 Excel 尺码或补齐原图；测量项含义审核无法补出缺失尺码与尺寸。")
        else:
            detail = "当前没有待确认的测量项名称；请核对原图的行列或尺寸数据，含义审核无法修复此问题。"
        reason = f"{detail}原校验：{error}"
        record_event(session, {"key": "structure:" + error.key, "platform_id": platform_id,
                     "field_id": "ocr:chart", "field_label": "尺码资料校验", "excel_value": error.path.name,
                     "source_image": str(error.path), "status": "skipped", "api_called": False,
                     "reason_code": "ocr_structure_invalid", "reason": reason}, sink=session.report_sink, logger=logger)
        raise RecognitionError(reason)
    label_ids = unknown
    source_body = await asyncio.to_thread(error.path.read_bytes)
    source_sha256 = hashlib.sha256(source_body).hexdigest()
    requests = {}
    decisions = dict(session.corrections.get(error.key, {}))
    pending = []
    for ident in label_ids:
        text = error.tokens[ident].text
        request = AttributeRequest(
            platform_id=platform_id, category_leaf_id="ocr:" + error.garment_kind,
            field_id="ocr:" + canonical_sha256((error.garment_kind, text))[:24],
            field_label="尺码 OCR 含义 · " + text,
            candidates=tuple(CandidateValue(k, v) for k, v in meanings.items()),
            excel_value=text,
            evidence={"ocr_text": text, "ocr_row_index": ident,
                      "source_sha256": source_sha256, "source_filename": error.path.name,
                      "ocr_bbox": {k: getattr(error.tokens[ident], k) for k in ("x", "y", "width", "height")},
                      "summary": f"确认尺码原图中“{text}”对应哪个测量项（单选）。只确认文字含义，尺寸数字保持原图数据。若这是标题或备注，选择“不是测量项”。原校验：{error}"},
            custom_allowed=False, schema_version="ocr-v1:" + error.key,
            control_type="ocr_semantic",
        )
        requests[ident] = request
        if ident in decisions:
            continue
        approved = runtime.reusable_choice(request)
        if approved is not None:
            if approved.is_no_fill:
                decisions[ident] = "other"
            elif approved.value_id in meanings:
                decisions[ident] = approved.value_id
            else:
                raise RecognitionError("OCR 已审核值不属于当前测量项")
            continue
        # This uses the normal service gate (same product, or >=3 independent
        # human examples and >=95% agreement), without proposing model samples.
        from learning_models import CandidateSnapshot
        snapshot = CandidateSnapshot(request.platform_id, request.category_leaf_id, request.field_id,
                                     request.field_label, request.candidates, request.schema_version, False)
        runtime._queue_candidate_snapshot(snapshot, None, request.control_type)
        await runtime.drain()
        try:
            learned = await asyncio.to_thread(client.review_learning, {
                "product_version": runtime.product_version, "platform_id": platform_id,
                "category_leaf_id": request.category_leaf_id, "field_id": request.field_id,
                "snapshot_version": snapshot.snapshot_version, "excel_value": text,
                "control_type": request.control_type,
            })
        except LearningClientError:
            learned = {}
        import math
        ids = learned.get("value_ids")
        support, rate = learned.get("support_count"), learned.get("calibrated_acceptance_rate")
        if (learned.get("status") == "auto_fill_ready" and learned.get("source") == "review_learning"
                and learned.get("snapshot_version") == snapshot.snapshot_version
                and type(support) is int and support >= (1 if learned.get("same_product") is True else 3)
                and type(rate) in (float, int) and math.isfinite(rate) and .95 <= rate <= 1
                and isinstance(ids, list) and len(ids) == 1 and ids[0] in meanings
                and learned.get("value_labels") == [meanings[ids[0]]]):
            decisions[ident] = ids[0]
        else:
            pending.append(ident)
    resolved_structure = False
    if decisions:
        try:
            error.validate(decisions)
            resolved_structure = True
        except RecognitionError:
            pass
    if not pending or resolved_structure:
        try:
            error.validate(decisions)
        except RecognitionError:
            raise RecognitionError(f"OCR 已审核含义后，表格行列或数值仍未通过校验，请修正原图：{error}") from None
        session.corrections[error.key] = decisions
        from model_judgments import record_event, reason_text
        record_event(session, {"key": "skip:learned:" + error.key, "platform_id": platform_id,
                     "field_id": "ocr:chart", "field_label": "尺码 OCR", "excel_value": error.path.name,
                     "status": "skipped", "api_called": False, "reason_code": "review_learning",
                     "reason": reason_text("review_learning")}, sink=session.report_sink, logger=logger)
        logger.info("尺码 OCR 已复用人工审核/学习，结构与数值校验通过")
        return
    prediction_key = canonical_sha256({"chart": error.key, "reason": str(error), "decisions": decisions})
    from model_judgments import api_called, reason_text, record_event
    event = {"key": prediction_key, "platform_id": platform_id, "field_id": "ocr:chart",
             "field_label": "尺码 OCR", "excel_value": "、".join(error.tokens[i].text for i in pending),
             "source_image": str(error.path)}
    if prediction_key not in session.predictions:
        record_event(session, {**event, "status": "requesting", "api_called": None,
                     "reason": "OCR 规则和人工学习尚未确定含义，请求模型判断原文"}, sink=session.report_sink, logger=logger)
        judge = getattr(client, "ocr_judge", None)
        try:
            result = await asyncio.to_thread(judge, {
                "product_version": runtime.product_version, "garment_kind": error.garment_kind,
                "platform_id": platform_id,
                "tokens": [asdict(t) for t in error.tokens], "label_token_ids": list(label_ids),
                "reason": str(error),
            }) if callable(judge) else {"reason_code": "ocr_judge_unavailable"}
        except LearningClientError:
            result = {"reason_code": "ocr_judge_unavailable"}
        session.predictions[prediction_key] = result if isinstance(result, dict) else {}
    response = session.predictions[prediction_key]
    suggestions = validate_suggestions(error, response)
    proposal = {**decisions, **{i: suggestions[i][0] for i in pending if i in suggestions}}
    proposed_ids = [i for i in pending if i in suggestions]
    valid = bool(proposed_ids)
    if valid:
        try:
            error.validate(proposal)
        except RecognitionError:
            valid = False
    if valid and all(suggestions[i][1] >= .98 for i in proposed_ids):
        record_event(session, {**event, "status": "adopted", "api_called": api_called(response),
                     "model": response.get("model", ""), "selected_labels": [meanings[proposal[i]] for i in proposed_ids],
                     "confidence": min(suggestions[i][1] for i in proposed_ids),
                     "reason": "OCR 原文语义映射通过原表行列和数值校验，自动采用"}, sink=session.report_sink, logger=logger)
        session.corrections[error.key] = proposal
        for ident in proposed_ids:
            session.record_decision({
                "request_key": prediction_key,
                "platform_id": platform_id, "field_id": requests[ident].field_id,
                "field_label": requests[ident].field_label, "excel_value": error.tokens[ident].text,
                "selected_labels": [meanings[proposal[ident]]], "selected_value_ids": [proposal[ident]],
                "confidence": suggestions[ident][1], "reason": "OCR 原文语义映射；原表行列与正数校验通过",
                "evidence_kinds": ["ocr"], "status": "adopted", "readback_status": "source_validated",
                "source_image": str(error.path), "ocr_row_index": ident,
                "snapshot_version": requests[ident].schema_version, "source": "deepseek",
                "model": response.get("model", ""), "original_ocr": [asdict(t) for t in error.tokens],
            })
        logger.info("DeepSeek 尺码 OCR 含义判断通过原表行列/数值校验：%s", "、".join(
            f"{error.tokens[i].text} → {meanings[proposal[i]]}" for i in proposed_ids))
        return
    code = str(response.get("reason_code") or ("deepseek_invalid_ocr_judgment" if not suggestions else "deepseek_uncertain"))
    record_event(session, {**event, "status": "review_required" if suggestions else "unavailable",
                 "api_called": api_called(response), "model": response.get("model", ""), "reason_code": code,
                 "reason": "模型置信度或原表结构校验未通过，转人工审核" if suggestions else reason_text(code),
                 "selected_labels": [meanings[suggestions[i][0]] for i in proposed_ids]}, sink=session.report_sink, logger=logger)
    reviews = []
    # Bind reviews to this chart, never an arbitrary product thumbnail.
    content_type = mimetypes.guess_type(error.path.name)[0] or "application/octet-stream"
    try:
        await asyncio.to_thread(client.upload_asset, runtime.product_version, source_sha256,
                                "original", content_type, source_body)
    except LearningClientError:
        logger.warning("尺码审核原图上传失败；审核页会提示原图不可用")
    for ident in pending:
        request = requests[ident]
        from learning_models import CandidateSnapshot
        snapshot = CandidateSnapshot(request.platform_id, request.category_leaf_id, request.field_id,
                                     request.field_label, request.candidates, request.schema_version, False)
        if ident in suggestions:
            session.record_decision({
                "request_key": prediction_key,
                "platform_id": platform_id, "field_id": request.field_id, "field_label": request.field_label,
                "excel_value": error.tokens[ident].text, "selected_labels": [meanings[suggestions[ident][0]]],
                "selected_value_ids": [suggestions[ident][0]], "confidence": suggestions[ident][1],
                "reason": "模型判断未满足置信度或原表结构校验，等待人工确认", "status": "review_required",
                "readback_status": "pending", "source_image": str(error.path), "ocr_row_index": ident,
                "snapshot_version": request.schema_version, "source": "deepseek",
                "model": response.get("model", ""), "original_ocr": [asdict(t) for t in error.tokens],
            })
        try:
            # A model prediction is only a suggestion. The operator's resulting
            # review action is the only new training evidence.
            await runtime._raise_review(request, snapshot, None,
                                        str(response.get("reason_code") or "ocr_semantic_uncertain"),
                                        suggestions[ident][0] if valid and ident in suggestions else None)
        except ReviewRequired as review:
            reviews.append(review)
    if reviews:
        raise ReviewBatchRequired(tuple(reviews))
    runtime.raise_deferred_reviews()
