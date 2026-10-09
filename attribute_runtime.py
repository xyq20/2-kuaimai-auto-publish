from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple
import uuid

from attribute_decision import DecisionInput, DecisionStatus, validate_decision
from canonical_fields import FieldMappingError, map_platform_field
from field_policies import match_style_option_candidates
from historical_readbacks import (
    VerifiedAttributeHistory,
    normalize_history_field_label,
)
from learning_client import LearningClientError, flush_outbox_async
from learning_models import CandidateSnapshot, CandidateValue, canonical_sha256
from learning_store import LearningStore
from platform_registry import canonical_platform_name
from platform_candidate_source import first_per_label


UNSUPPORTED_CONTROL_TYPES = frozenset({"shop", "logistics", "freight"})

MULTI_CHOICE_SEPARATORS = r"[,，、;；]"
EXCEL_ALTERNATIVE_SEPARATORS = r"[,，、;；/／]"
MULTI_SELECT_CONTROL_TYPES = frozenset({"multi_select", "multi-select", "multiselect"})

# Stored in review_resolutions.final_value_id because that column is NOT NULL.
# It is scoped by product/platform/snapshot when read back.
NO_FILL_VALUE_ID = "__NO_FILL__"
NO_FILL_LABEL = "不填写"


def _normalize_choice_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def split_multi_choice(value: object) -> Tuple[str, ...]:
    """Split a confirmed/review value into its chosen parts.

    审核确认值兼容多选：云端可在 final_value_id 里用逗号分隔多个
    valueId 或 label（例如“101,102”或“男,女”）。逗号与 Excel 的
    “分组全选”语法一致，消费端 selection_value_groups 会按同样规则
    拆组后全选。
    """
    return tuple(
        part.strip()
        for part in re.split(MULTI_CHOICE_SEPARATORS, str(value or ""))
        if part.strip()
    )


def excel_alternative_set(excel_value: object) -> frozenset:
    """All normalized alternatives (comma groups + slash ORs) in Excel text."""
    return frozenset(
        _normalize_choice_text(part)
        for part in re.split(
            EXCEL_ALTERNATIVE_SEPARATORS, str(excel_value or "")
        )
        if part.strip()
    )


def _matched_excel_variants(
    excel_value: object,
    candidates: Tuple[CandidateValue, ...],
    *,
    control_type: str,
    field_label: str = "",
) -> Tuple[Tuple[CandidateValue, ...], ...]:
    """Resolve ordered Excel alternatives against live platform candidates.

    For a proven multi-select control, slash separates alternative plans and
    commas separate the values required by one plan.  Thus
    ``秋季，冬季/春秋冬/春秋`` means ``[秋季 + 冬季]`` first, then either
    one-value fallback. Single-select and text controls also try slash-separated
    alternatives in Excel order, while keeping commas inside each label.
    """

    text = str(excel_value or "").strip()
    if not text:
        return ()
    if str(control_type).casefold() in MULTI_SELECT_CONTROL_TYPES:
        raw_variants = tuple(
            tuple(
                part.strip()
                for part in re.split(MULTI_CHOICE_SEPARATORS, alternative)
                if part.strip()
            )
            for alternative in re.split(r"[/／]", text)
            if alternative.strip()
        )
    else:
        raw_variants = tuple(
            (part.strip(),)
            for part in re.split(r"[/／]", text)
            if part.strip()
        )

    matched_variants = []
    for variant in raw_variants:
        matched = []
        for part in variant:
            found = tuple(
                candidate
                for candidate in candidates
                if part in {candidate.value_id, candidate.label}
            )
            if not found:
                style_matches = match_style_option_candidates(
                    field_label, part, tuple(candidate.label for candidate in candidates)
                )
                found = tuple(candidates[index] for index in style_matches)
            if len(found) != 1 or found[0] in matched:
                matched = []
                break
            matched.append(found[0])
        if matched:
            matched_variants.append(tuple(matched))
    return tuple(matched_variants)


@dataclass(frozen=True)
class AttributeRequest:
    platform_id: str
    category_leaf_id: str
    field_id: str
    field_label: str
    candidates: Tuple[CandidateValue, ...]
    excel_value: str
    evidence: Mapping[str, Any]
    custom_allowed: bool
    schema_version: str
    control_type: str = "select"


@dataclass(frozen=True)
class ResolvedAttribute:
    value_id: str
    label: str
    source: str
    snapshot_version: str

    @property
    def is_no_fill(self) -> bool:
        return self.value_id == NO_FILL_VALUE_ID


class ReviewRequired(RuntimeError):
    def __init__(
        self,
        review_id: str,
        request: AttributeRequest,
        reason_code: str,
        snapshot_version: str,
    ) -> None:
        super().__init__(f"属性需要人工审核：{request.field_label}")
        self.review_id = review_id
        self.request = request
        self.reason_code = reason_code
        self.snapshot_version = snapshot_version


class ReviewBatchRequired(RuntimeError):
    """Raised after one platform has collected every unresolved attribute."""

    def __init__(self, reviews: Tuple[ReviewRequired, ...]) -> None:
        if not reviews:
            raise ValueError("review batch cannot be empty")
        labels = "、".join(review.request.field_label for review in reviews)
        super().__init__(f"平台属性需要人工审核：{labels}")
        self.reviews = reviews


class AttributeRuntime:
    def __init__(
        self,
        store: LearningStore,
        client: Any,
        run_id: str,
        product_version: str,
        *,
        device_id: str = "",
        verified_history: Optional[VerifiedAttributeHistory] = None,
    ) -> None:
        self.store = store
        self.client = client
        self.run_id = run_id
        self.product_version = product_version
        self.device_id = device_id or store.get_or_create_device_id()
        self.verified_history = dict(verified_history or {})
        # Python 3.9 binds Lock() to the current loop at construction.  The
        # runtime is created before asyncio.run(), so construct it on first use.
        self._flush_lock: Optional[asyncio.Lock] = None
        self._background_flush: Optional[asyncio.Task[int]] = None
        self._collecting_platform_id = ""
        self._deferred_reviews: dict[str, ReviewRequired] = {}
        self._resolved_attributes: dict[
            tuple[str, str], tuple[AttributeRequest, ResolvedAttribute]
        ] = {}
        self.model_decisions: list[dict[str, Any]] = []
        self._model_judgments: dict[str, Mapping[str, Any]] = {}
        self.model_decision_sink = None

    def begin_review_collection(self, platform_id: str) -> None:
        """Start a fresh platform pass that defers reviews until its boundary."""
        self._collecting_platform_id = canonical_platform_name(platform_id)
        self._deferred_reviews.clear()

    @property
    def deferred_reviews(self) -> Tuple[ReviewRequired, ...]:
        return tuple(self._deferred_reviews.values())

    @property
    def has_deferred_reviews(self) -> bool:
        return bool(self._deferred_reviews)

    def raise_deferred_reviews(self) -> None:
        if self._deferred_reviews:
            raise ReviewBatchRequired(self.deferred_reviews)

    def intentionally_omitted(self, platform_id: str, field_label: str) -> bool:
        """Only an explicit review decision may exempt a visible Excel field."""
        key = (
            canonical_platform_name(platform_id),
            normalize_history_field_label(field_label.split("#", 1)[0]),
        )
        remembered = self._resolved_attributes.get(key)
        return bool(remembered and remembered[1].is_no_fill)

    def review_pending_or_omitted(self, platform_id: str, field_label: str) -> bool:
        """Tell adapters to leave a field alone after review or no-fill."""
        if self.intentionally_omitted(platform_id, field_label):
            return True
        platform = canonical_platform_name(platform_id)
        label = normalize_history_field_label(field_label.split("#", 1)[0])
        return any(
            canonical_platform_name(review.request.platform_id) == platform
            and normalize_history_field_label(review.request.field_label) == label
            for review in self._deferred_reviews.values()
        )

    async def resolve_unmatched_page_options(
        self,
        *,
        platform_id: str,
        category_leaf_id: str,
        field_label: str,
        excel_value: str,
        options: Tuple[Mapping[str, Any], ...],
        multi: bool = False,
    ) -> Optional[ResolvedAttribute]:
        """Submit an unmatched visible dropdown to the normal review pipeline.

        Some category schemas omit a field or its option IDs. The live dropdown
        still supplies reviewable labels; stable label IDs keep that snapshot
        scoped to this platform, category and field across retries.
        """
        candidates = []
        seen_labels = set()
        for option in options:
            label = str(option.get("name") or option.get("label") or "").strip()
            if not label or option.get("disabled") or label in seen_labels:
                continue
            seen_labels.add(label)
            value_id = str(option.get("value") or option.get("id") or "").strip()
            if not value_id:
                value_id = "dom:" + canonical_sha256(label)[:20]
            candidates.append(CandidateValue(value_id, label))
        if not candidates:
            raise ValueError(f"属性“{field_label}”未取得可审核的页面候选")
        category_id = str(category_leaf_id).strip() or (
            "dom:" + canonical_sha256((platform_id, self.product_version))[:20]
        )
        field_id = "dom:" + normalize_history_field_label(field_label)
        schema_version = canonical_sha256({
            "platform_id": canonical_platform_name(platform_id),
            "category_leaf_id": category_id,
            "field_id": field_id,
            "options": [
                {"value_id": value.value_id, "label": value.label}
                for value in candidates
            ],
        })
        return await self.resolve(AttributeRequest(
            platform_id=platform_id,
            category_leaf_id=category_id,
            field_id=field_id,
            field_label=field_label,
            candidates=tuple(candidates),
            excel_value=excel_value,
            evidence={"excel": True, "live_dom": True},
            custom_allowed=False,
            schema_version=schema_version,
            control_type="multi_select" if multi else "select",
        ))

    async def _flush(self) -> int:
        # All event delivery uses one ordered worker.  Snapshot/review/checkpoint
        # dependencies therefore keep their durable SQLite order even while the
        # browser continues filling the next field.
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
        async with self._flush_lock:
            return await flush_outbox_async(
                self.store,
                self.client,
                datetime.now(timezone.utc),
            )

    def _schedule_flush(self) -> None:
        """Start best-effort delivery without blocking a verified local choice."""
        if self._background_flush is not None and self._background_flush.done():
            # Never replace a completed worker before observing its result.
            # Permanent API failures must stop the run instead of becoming an
            # unhandled background-task warning while browser filling continues.
            self._background_flush.result()
            self._background_flush = None
        if self._background_flush is None:
            self._background_flush = asyncio.create_task(self._flush())

    async def drain(self) -> None:
        """Finish ordered delivery at a platform/checkpoint boundary."""
        background = self._background_flush
        if background is not None:
            await background
        # Events can be added while the previous worker is awaiting the network.
        # One final locked pass includes those events and the newest checkpoint.
        while await self._flush():
            pass

    async def _raise_review(
        self,
        request: AttributeRequest,
        snapshot: CandidateSnapshot,
        canonical_field: Optional[str],
        reason_code: str,
        suggested_value_id: Optional[str] = None,
    ) -> None:
        existing = self._deferred_reviews.get(snapshot.snapshot_version)
        if existing is not None:
            return
        if suggested_value_id is None and request.control_type != "ocr_semantic":
            suggestion = await self._semantic_choice(request, snapshot, allow_auto=False)
            if suggestion is not None:
                suggested_value_id = suggestion.value_id
        review_id = uuid.uuid4().hex
        evidence = {
            **dict(request.evidence),
            "kinds": sorted(str(key) for key in request.evidence),
            "snapshot_version": snapshot.snapshot_version,
            "reuse_context": {
                "excel_candidates": [v.strip() for v in re.split(r'[/／]', request.excel_value) if v.strip()],
                "control_type": request.control_type,
            },
        }
        payload = {
            "id": review_id,
            "run_id": self.run_id,
            "device_id": self.device_id,
            "product_version": self.product_version,
            "platform_id": request.platform_id,
            "category_leaf_id": request.category_leaf_id,
            "field_id": request.field_id,
            "field_label": request.field_label,
            "canonical_field": canonical_field,
            "snapshot_version": snapshot.snapshot_version,
            "suggested_value_id": suggested_value_id,
            "reason_code": reason_code,
            "evidence_json": evidence,
        }
        self.store.enqueue(
            f"review.created:{self.run_id}:{snapshot.snapshot_version}",
            "review.created",
            payload,
        )
        review = ReviewRequired(
            review_id,
            request,
            reason_code,
            snapshot.snapshot_version,
        )
        if self._collecting_platform_id:
            request_platform = canonical_platform_name(request.platform_id)
            if request_platform != self._collecting_platform_id:
                raise ValueError(
                    "attribute review platform changed during collection"
                )
            self._deferred_reviews[snapshot.snapshot_version] = review
            self._schedule_flush()
            return
        await self._flush()
        raise review

    async def _semantic_choice(
        self, request: AttributeRequest, snapshot: CandidateSnapshot, *, allow_auto: bool = True,
    ) -> Optional[ResolvedAttribute]:
        """Interpret an unmatched intent; candidate membership is mandatory."""
        judge = getattr(self.client, "semantic_judge", None)
        if (not callable(judge) or not request.candidates or len(request.candidates) > 1000
                or request.control_type not in {"select", "category", *MULTI_SELECT_CONTROL_TYPES}
                or re.search(r"价格|售价|库存|运费|物流|店铺", request.field_label)):
            return None
        payload = {
            "product_version": self.product_version, "platform_id": request.platform_id,
            "category_leaf_id": request.category_leaf_id, "field_id": request.field_id,
            "field_label": request.field_label, "control_type": request.control_type,
            "snapshot_version": snapshot.snapshot_version, "excel_value": request.excel_value,
            "options": [{"value_id": c.value_id, "label": c.label} for c in request.candidates],
            "evidence": dict(request.evidence),
        }
        key = canonical_sha256(payload)
        if key not in self._model_judgments:
            try:
                await self._flush()
                answer = await asyncio.to_thread(judge, payload)
            except LearningClientError as failure:
                logging.getLogger("kuaimai_erp").warning("DeepSeek 辅助判断不可用，转人工审核：%s", failure)
                answer = {}
            self._model_judgments[key] = answer if isinstance(answer, Mapping) else {}
        answer = self._model_judgments[key]
        ids, confidence = answer.get("value_ids"), answer.get("confidence")
        kinds = answer.get("evidence_kinds")
        valid = (
            answer.get("source") == "deepseek" and answer.get("status") == "suggested"
            and answer.get("snapshot_version") == snapshot.snapshot_version
            and isinstance(ids, list) and ids and all(isinstance(v, str) for v in ids)
            and len(ids) == len(set(ids))
            and (len(ids) == 1 or request.control_type in MULTI_SELECT_CONTROL_TYPES)
            and all(sum(c.value_id == ident for c in request.candidates) == 1 for ident in ids)
            and type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1
            and isinstance(kinds, list) and kinds and all(k in {"excel", "human_learning", "product"} for k in kinds)
            and ("excel" not in kinds or bool(request.excel_value.strip()))
            and (request.control_type != "category" or bool({"excel", "human_learning"}.intersection(kinds)))
            and isinstance(answer.get("reason"), str) and bool(answer["reason"].strip())
        )
        if not valid:
            if answer.get("source") == "deepseek" and answer.get("status") == "suggested" and not any(row["key"] == key for row in self.model_decisions):
                self.model_decisions.append({
                    "key": key, "platform_id": canonical_platform_name(request.platform_id),
                    "field_id": request.field_id, "field_label": request.field_label,
                    "excel_value": request.excel_value, "selected_labels": [],
                    "selected_value_ids": ids if isinstance(ids, list) and all(isinstance(i, str) for i in ids) else [],
                    "confidence": confidence if type(confidence) in (int, float) and math.isfinite(confidence) else 0,
                    "reason": "模型响应未通过候选、证据或快照校验；" + str(answer.get("reason") or "")[:1000],
                    "snapshot_version": snapshot.snapshot_version, "status": "rejected", "readback_status": "pending",
                    "candidate_options": payload["options"], "source": "deepseek", "model": answer.get("model", ""),
                })
                if self.model_decision_sink is not None:
                    self.model_decision_sink()
            return None
        labels = [next(c.label for c in request.candidates if c.value_id == ident) for ident in ids]
        ready = allow_auto and confidence >= .98 and not request.evidence.get("selection_only")
        trace = {
            "key": key, "platform_id": canonical_platform_name(request.platform_id), "field_id": request.field_id,
            "field_label": request.field_label, "category_leaf_id": request.category_leaf_id,
            "snapshot_version": snapshot.snapshot_version, "excel_value": request.excel_value,
            "selected_value_ids": ids, "selected_labels": labels, "confidence": confidence,
            "reason": answer["reason"], "evidence_kinds": kinds,
            "learning_review_ids": answer.get("learning_review_ids", []),
            "product_evidence": answer.get("product_evidence", {}),
            "candidate_options": payload["options"], "input_evidence": payload["evidence"],
            "status": "adopted" if ready else "review_required", "readback_status": "pending",
            "source": "deepseek",
            "model": answer.get("model", ""),
        }
        if not any(row["key"] == key for row in self.model_decisions):
            self.model_decisions.append(trace)
            if self.model_decision_sink is not None:
                self.model_decision_sink()
        result = ResolvedAttribute(
            ",".join(ids), ",".join(labels), "deepseek", snapshot.snapshot_version,
        )
        if ready:
            logging.getLogger("kuaimai_erp").info("DeepSeek 自动判断：%s | %s：Excel=%s → %s；置信度=%.1f%%；依据=%s",
                request.platform_id, request.field_label, request.excel_value, "、".join(labels), confidence * 100, answer["reason"])
            return self._remember_resolved(request, result)
        return result if not allow_auto else None

    def confirmed_choice(self, request: AttributeRequest) -> Optional[ResolvedAttribute]:
        """Read a product/field/snapshot scoped approval without attempting a write."""
        request = replace(request, candidates=first_per_label(request.candidates))
        snapshot = CandidateSnapshot(
            request.platform_id, request.category_leaf_id, request.field_id,
            request.field_label, request.candidates, request.schema_version,
            request.custom_allowed,
        )
        value = self.store.load_review_resolution(
            product_version=self.product_version,
            platform_id=canonical_platform_name(request.platform_id),
            snapshot_version=snapshot.snapshot_version,
        )
        if value is None:
            return None
        if value == NO_FILL_VALUE_ID:
            return ResolvedAttribute(
                NO_FILL_VALUE_ID, "", "human_no_fill", snapshot.snapshot_version
            )
        matches = [candidate for candidate in request.candidates
                   if value in (candidate.value_id, candidate.label)]
        if len(matches) == 1:
            return ResolvedAttribute(matches[0].value_id, matches[0].label,
                                     "human_override", snapshot.snapshot_version)
        if not matches and request.custom_allowed:
            return ResolvedAttribute(value, value, "human_override", snapshot.snapshot_version)
        return None

    def reusable_choice(self, request: AttributeRequest) -> Optional[ResolvedAttribute]:
        """Read-only preflight before an adapter attempts Excel input.

        An approval is exact for the same product/snapshot, and the same
        product may reuse its approval across category snapshots.  A different
        product's review is only training evidence: it must pass the learning
        service's confidence gate before this runtime can use it.
        """
        approved = self.confirmed_choice(request)
        if approved is not None:
            return approved
        same_product_review = self._cross_product_choice(
            request, same_product_only=True
        )
        if same_product_review is not None:
            return same_product_review
        matched_excel = _matched_excel_variants(
            request.excel_value,
            request.candidates,
            control_type=request.control_type,
            field_label=request.field_label,
        )
        exact = matched_excel[0] if matched_excel else ()
        labels = self.verified_history.get((canonical_platform_name(request.platform_id),
                                           normalize_history_field_label(request.field_label)), ())
        if labels:
            matches = [c for c in request.candidates if c.label in labels]
            # 多选字段的历史是多个 label（如 适用季节=秋季,冬季）：每个都唯一
            # 命中当前候选时组合复用，与 resolve 的多值历史语义保持一致。
            if (
                len(matches) == len(labels)
                and len(set(matches)) == len(matches)
                and (not exact or all(match in exact for match in matches))
            ):
                return ResolvedAttribute(
                    ",".join(match.value_id for match in matches),
                    ",".join(match.label for match in matches),
                    'verified_history',
                    '',
                )
        if exact:
            return None
        return None

    def _cross_product_choice(
        self, request: AttributeRequest, *, same_product_only: bool = False
    ) -> Optional[ResolvedAttribute]:
        request = replace(request, candidates=first_per_label(request.candidates))
        # A selection_only request means the confirmed value was already tried
        # against the live control and could not be written. Reusing it would
        # repeat the same failed click instead of opening a fresh review.
        if request.evidence.get("selection_only"):
            return None
        snapshot = CandidateSnapshot(
            request.platform_id, request.category_leaf_id, request.field_id,
            request.field_label, request.candidates, request.schema_version,
            request.custom_allowed,
        )
        try:
            canonical_field = map_platform_field(
                request.platform_id, request.field_label
            )
        except FieldMappingError:
            canonical_field = None
        label = self.store.reusable_review_label(
            snapshot,
            request.excel_value,
            request.control_type,
            canonical_field,
            self.product_version if same_product_only else None,
        )
        if label == NO_FILL_LABEL or label == NO_FILL_VALUE_ID:
            return ResolvedAttribute(
                NO_FILL_VALUE_ID, "", "human_no_fill", snapshot.snapshot_version
            )
        parts = split_multi_choice(label)
        matches = []
        for part in parts:
            part_matches = [v for v in request.candidates if v.label == part]
            if len(part_matches) != 1 or part_matches[0] in matches:
                return None
            matches.append(part_matches[0])
        if not matches:
            return None
        return ResolvedAttribute(
            ",".join(match.value_id for match in matches),
            ",".join(match.label for match in matches),
                                 'cross_product_human', snapshot.snapshot_version)

    def _remember_resolved(
        self, request: AttributeRequest, resolved: ResolvedAttribute
    ) -> ResolvedAttribute:
        self._resolved_attributes[
            (
                canonical_platform_name(request.platform_id),
                normalize_history_field_label(request.field_label),
            )
        ] = (request, resolved)
        return resolved

    def remember_resolved(
        self, request: AttributeRequest, resolved: ResolvedAttribute
    ) -> ResolvedAttribute:
        """Remember an approval consumed by an adapter before normal resolve()."""
        return self._remember_resolved(request, resolved)

    def _queue_candidate_snapshot(
        self,
        snapshot: CandidateSnapshot,
        canonical_field: Optional[str],
        control_type: str,
    ) -> None:
        self.store.save_candidate_snapshot(snapshot)
        self.store.enqueue(
            f"snapshot.created:{snapshot.snapshot_version}",
            "snapshot.created",
            {
                "snapshot_version": snapshot.snapshot_version,
                "platform_id": snapshot.platform_id,
                "category_leaf_id": snapshot.category_leaf_id,
                "field_id": snapshot.field_id,
                "field_label": snapshot.field_label,
                "schema_version": snapshot.schema_version,
                "custom_allowed": snapshot.custom_allowed,
                "canonical_field": canonical_field,
                "control_type": control_type,
                "options": [
                    {
                        "value_id": candidate.value_id,
                        "label": candidate.label,
                        "position": position,
                    }
                    for position, candidate in enumerate(snapshot.values)
                ],
            },
        )

    def record_verified_readbacks(
        self, platform_id: str, attributes: Mapping[str, Any]
    ) -> int:
        """Queue field-level learning only after save/reopen readback succeeds."""
        platform = canonical_platform_name(platform_id)
        recorded = 0
        for raw_label, raw_values in attributes.items():
            values_source = (
                raw_values
                if isinstance(raw_values, (list, tuple))
                else (raw_values,)
            )
            values = tuple(
                str(value).strip()
                for value in values_source
                if value is not None and str(value).strip()
            )
            remembered = self._resolved_attributes.get(
                (platform, normalize_history_field_label(raw_label))
            )
            if remembered is None:
                continue
            request, resolved = remembered
            for trace in self.model_decisions:
                if (canonical_platform_name(trace["platform_id"]) == platform and trace["field_id"] == request.field_id
                        and trace["snapshot_version"] == resolved.snapshot_version):
                    expected = trace["selected_labels"]
                    actual = tuple(part for value in values for part in split_multi_choice(value))
                    trace["readback_status"] = "verified" if Counter(actual) == Counter(expected) else "mismatch"
                    trace["readback_values"] = list(values)
            if resolved.source == "deepseek":
                # Keep model adoption in the run audit; it cannot create human
                # training votes merely because a browser write succeeded.
                continue
            if resolved.is_no_fill:
                # An intentional omission is not a verified platform value.
                continue
            resolved_ids = split_multi_choice(resolved.value_id)
            is_multi_choice = len(resolved_ids) > 1 and all(
                sum(
                    1
                    for candidate in request.candidates
                    if candidate.value_id == value_id
                )
                == 1
                for value_id in resolved_ids
            )
            if is_multi_choice:
                actual_labels = tuple(
                    part
                    for value in values
                    for part in split_multi_choice(value)
                )
                resolved_labels = split_multi_choice(resolved.label)
                if Counter(
                    normalize_history_field_label(value)
                    for value in actual_labels
                ) != Counter(
                    normalize_history_field_label(value)
                    for value in resolved_labels
                ):
                    continue
                actual_label = resolved.label
            else:
                if len(values) != 1 or normalize_history_field_label(
                    values[0]
                ) != normalize_history_field_label(resolved.label):
                    continue
                actual_label = values[0]
            payload = {
                "run_id": self.run_id,
                "product_version": self.product_version,
                "platform_id": platform,
                "category_leaf_id": request.category_leaf_id,
                "field_id": request.field_id,
                "snapshot_version": resolved.snapshot_version,
                "actual_value_id": resolved.value_id,
                "actual_label": actual_label,
                "verified": True,
                "payload_json": {"source": "save_readback"},
            }
            self.store.enqueue(
                "readback.recorded:"
                + canonical_sha256(payload),
                "readback.recorded",
                payload,
            )
            recorded += 1
        if self.model_decisions and self.model_decision_sink is not None:
            self.model_decision_sink()
        return recorded

    async def resolve(
        self, request: AttributeRequest
    ) -> Optional[ResolvedAttribute]:
        request = replace(request, candidates=first_per_label(request.candidates))
        if request.control_type.casefold() in UNSUPPORTED_CONTROL_TYPES:
            raise ValueError("operational selector cannot use attribute learning")
        requested_candidates = tuple(request.candidates)
        matched_excel_variants = _matched_excel_variants(
            request.excel_value,
            requested_candidates,
            control_type=request.control_type,
            field_label=request.field_label,
        )
        exact_excel = matched_excel_variants[0] if matched_excel_variants else ()
        historical_labels = self.verified_history.get(
            (
                canonical_platform_name(request.platform_id),
                normalize_history_field_label(request.field_label),
            ),
            (),
        )
        historical_candidates = tuple(
            candidate
            for candidate in requested_candidates
            if any(candidate.label == label for label in historical_labels)
        )
        # Multi-select fields persist several verified labels per field; only
        # reuse the group when every historical label resolves to exactly one
        # live candidate and the whole group stays within the Excel intent.
        excel_alternatives = excel_alternative_set(request.excel_value)
        historical_group_valid = bool(historical_labels) and bool(historical_candidates) and (
            len({candidate.label for candidate in historical_candidates})
            == len(historical_candidates)
        ) and (
            any(
                {candidate.label for candidate in historical_candidates}
                == {candidate.label for candidate in variant}
                for variant in matched_excel_variants
            )
            if matched_excel_variants
            else (
                not excel_alternatives
                or all(
                    _normalize_choice_text(candidate.label) in excel_alternatives
                    for candidate in historical_candidates
                )
            )
        )
        historical_candidate = (
            historical_candidates[0]
            if len(historical_candidates) == 1
            and (not exact_excel or historical_candidates[0] in exact_excel)
            else None
        )
        # Very large remote dictionaries (brands are the common example) can
        # contain tens of thousands of values.  When Excel already identifies
        # one unique live API candidate, transmit only that verified choice to
        # the review service.  It keeps the snapshot bounded without weakening
        # the click-time DOM/readback validation performed by the adapter.
        snapshot_candidates = (
            exact_excel if exact_excel and not request.evidence.get("force_review") else requested_candidates
        )
        snapshot = CandidateSnapshot(
            request.platform_id,
            request.category_leaf_id,
            request.field_id,
            request.field_label,
            snapshot_candidates,
            request.schema_version,
            request.custom_allowed,
        )
        # Human approval of the full live dictionary takes precedence over
        # an Excel match which would otherwise narrow the snapshot to one value.
        full_snapshot = CandidateSnapshot(
            request.platform_id, request.category_leaf_id, request.field_id,
            request.field_label, requested_candidates, request.schema_version,
            request.custom_allowed,
        )
        full_confirmed = self.store.load_review_resolution(
            product_version=self.product_version,
            platform_id=canonical_platform_name(request.platform_id),
            snapshot_version=full_snapshot.snapshot_version,
        )
        if full_confirmed is not None:
            snapshot = full_snapshot
        self.store.save_candidate_snapshot(snapshot)
        confirmed_value = self.store.load_review_resolution(
            product_version=self.product_version,
            platform_id=canonical_platform_name(request.platform_id),
            snapshot_version=snapshot.snapshot_version,
        )
        if confirmed_value is not None:
            if confirmed_value == NO_FILL_VALUE_ID:
                return self._remember_resolved(
                    request,
                    ResolvedAttribute(
                        NO_FILL_VALUE_ID, "", "human_no_fill", snapshot.snapshot_version
                    ),
                )
            # 审核确认兼容多选：确认值可用逗号分隔多个 valueId/label
            # （如“101,102”或“男,女”）。全部唯一命中候选后按 Excel 同款
            # 逗号语法组合返回，消费端 selection_value_groups 会拆组全选。
            parts = split_multi_choice(confirmed_value)
            if not parts:
                raise ValueError("confirmed review value is empty")
            matched = []
            for part in parts:
                found = tuple(
                    candidate
                    for candidate in snapshot.values
                    if part in {candidate.value_id, candidate.label}
                )
                if len(found) > 1:
                    raise ValueError("confirmed review value is ambiguous")
                if not found:
                    matched = None
                    break
                matched.append(found[0])
            if matched is not None:
                unique = tuple(dict.fromkeys(matched))
                return self._remember_resolved(
                    request,
                    ResolvedAttribute(
                        ",".join(candidate.value_id for candidate in unique),
                        ",".join(candidate.label for candidate in unique),
                        "human_override",
                        snapshot.snapshot_version,
                    ),
                )
            if (
                snapshot.custom_allowed
                or (
                    request.field_id != "__category__"
                    and request.control_type.casefold() != "category"
                )
            ):
                return self._remember_resolved(
                    request,
                    ResolvedAttribute(
                        confirmed_value,
                        confirmed_value,
                        "human_override",
                        snapshot.snapshot_version,
                    ),
                )
            raise ValueError("confirmed review value no longer matches snapshot")
        try:
            canonical_field = map_platform_field(
                request.platform_id, request.field_label
            )
        except FieldMappingError:
            canonical_field = None
        same_product_review = self._cross_product_choice(
            request, same_product_only=True
        )
        if same_product_review is not None:
            self._queue_candidate_snapshot(
                full_snapshot, canonical_field, request.control_type
            )
            self._schedule_flush()
            return self._remember_resolved(request, same_product_review)
        self._queue_candidate_snapshot(
            snapshot, canonical_field, request.control_type
        )
        if request.evidence.get("force_review"):
            await self._raise_review(request, snapshot, canonical_field,
                                     "platform_write_failed")
            return None
        if historical_candidate is not None or historical_group_valid:
            if historical_candidate is None:
                # 多选字段的保存回读是一组值（如 适用季节=秋季,冬季）：
                # 组内每个 label 都唯一命中当前候选且都在 Excel 意图内，
                # 按逗号语法组合复用，消费端 selection_value_groups 拆组全选。
                historical_candidate = CandidateValue(
                    ",".join(
                        candidate.value_id
                        for candidate in historical_candidates
                    ),
                    ",".join(
                        candidate.label for candidate in historical_candidates
                    ),
                )
            values = {}
            if canonical_field is not None:
                values[canonical_field] = {
                    "value_id": historical_candidate.value_id
                }
            self.store.enqueue(
                "text_facts.created:"
                + canonical_sha256(
                    {
                        "product_version": self.product_version,
                        "snapshot": snapshot.snapshot_version,
                        "history": historical_candidate.value_id,
                        "source": "verified_readback",
                    }
                ),
                "text_facts.created",
                {
                    "product_version": self.product_version,
                    "source": "verified_readback",
                    "payload_json": {
                        "values": values,
                        "platform_values": {
                            "|".join(
                                (
                                    request.platform_id,
                                    request.category_leaf_id,
                                    request.field_id,
                                )
                            ): {
                                "field_label": request.field_label,
                                "value_id": historical_candidate.value_id,
                                "value_label": historical_candidate.label,
                            }
                        },
                        "text_tokens": split_multi_choice(
                            historical_candidate.label
                        ),
                    },
                },
            )
            self._schedule_flush()
            return self._remember_resolved(
                request,
                ResolvedAttribute(
                    historical_candidate.value_id,
                    historical_candidate.label,
                    "verified_history",
                    snapshot.snapshot_version,
                ),
            )
        if exact_excel:
            exact_candidate = CandidateValue(
                ",".join(candidate.value_id for candidate in exact_excel),
                ",".join(candidate.label for candidate in exact_excel),
            )
            values = {}
            if canonical_field is not None:
                values[canonical_field] = {
                    "value_id": exact_candidate.value_id
                }
            text_payload = {
                "product_version": self.product_version,
                "source": "excel",
                "payload_json": {
                    "values": values,
                    "platform_values": {
                        "|".join(
                            (
                                request.platform_id,
                                request.category_leaf_id,
                                request.field_id,
                            )
                        ): {
                            "field_label": request.field_label,
                            "value_id": exact_candidate.value_id,
                            "value_label": exact_candidate.label,
                        }
                    },
                    "text_tokens": [request.excel_value.strip()],
                },
            }
            self.store.enqueue(
                "text_facts.created:"
                + canonical_sha256(
                    {
                        "product_version": self.product_version,
                        "snapshot": snapshot.snapshot_version,
                        "value": exact_candidate.value_id,
                        "source": "excel",
                    }
                ),
                "text_facts.created",
                text_payload,
            )
            # Excel remains authoritative while the rule library is being
            # accumulated. The candidate has already been cross-checked against
            # both API JSON and the live DOM before this point, so asking the
            # operator to confirm the same exact value adds no safety.
            self._schedule_flush()
            return self._remember_resolved(
                request,
                ResolvedAttribute(
                    exact_candidate.value_id,
                    exact_candidate.label,
                    "explicit_text",
                    snapshot.snapshot_version,
                ),
            )
        learning_reason = None
        learn = getattr(self.client, "review_learning", None)
        if callable(learn):
            await self._flush()
            learned = await asyncio.to_thread(learn, {
                "product_version": self.product_version, "platform_id": request.platform_id,
                "category_leaf_id": request.category_leaf_id, "field_id": request.field_id,
                "snapshot_version": snapshot.snapshot_version,
                "excel_value": request.excel_value, "control_type": request.control_type,
            })
            learning_reason = learned.get("reason_code")
            if learned.get("status") == "auto_fill_ready":
                ids = learned.get("value_ids", [])
                labels = learned.get("value_labels", [])
                support = learned.get("support_count", 0)
                rate = learned.get("calibrated_acceptance_rate", 0)
                valid = (
                    learned.get("snapshot_version") == snapshot.snapshot_version
                    and learned.get("source") == "review_learning"
                    and type(support) is int and support >= (1 if learned.get("same_product") is True else 3)
                    and type(rate) in (int, float) and math.isfinite(rate) and .95 <= rate <= 1
                    and isinstance(ids, list) and isinstance(labels, list) and len(ids) == len(labels) > 0
                    and all(isinstance(v, str) for v in ids + labels)
                    and len(set(ids)) == len(ids)
                    and (len(ids) == 1 or request.control_type.casefold() in MULTI_SELECT_CONTROL_TYPES)
                    and all(sum(c.value_id == ident and c.label == label for c in requested_candidates) == 1
                            for ident, label in zip(ids, labels))
                )
                if valid:
                    logging.getLogger("kuaimai_erp").info(
                        "属性“%s”：审核学习命中 %s；独立商品样本 %s，历史一致率 %.1f%%，同商品=%s",
                        request.field_label, "、".join(labels), support, rate * 100,
                        learned.get("same_product") is True,
                    )
                    return self._remember_resolved(request, ResolvedAttribute(
                        ",".join(ids), ",".join(labels), "review_learning", snapshot.snapshot_version))
                learning_reason = "review_learning_invalid_response"
            logging.getLogger("kuaimai_erp").info(
                "属性“%s”：审核学习暂不自动填写，原因=%s，独立商品样本=%s，历史一致率=%s",
                request.field_label, learning_reason, learned.get("support_count", 0),
                learned.get("calibrated_acceptance_rate", 0),
            )
            if learning_reason in {"review_learning_conflict", "review_learning_invalid_response"}:
                await self._raise_review(request, snapshot, canonical_field, learning_reason)
                return None
        if canonical_field is None:
            if request.control_type != "ocr_semantic":
                semantic = await self._semantic_choice(request, snapshot)
                if semantic is not None:
                    return semantic
            # A platform pass owns an ordered local outbox.  While collecting a
            # batch, enqueue every unmapped field first and drain once at the
            # platform boundary; otherwise a size table with many empty cells
            # pays one network round trip per cell before the browser can move
            # on to the remaining fields.
            if not self._collecting_platform_id:
                await self._flush()
            await self._raise_review(
                request, snapshot, None, learning_reason or "field_mapping_required"
            )
            return None
        await self._flush()
        response = await asyncio.to_thread(
            self.client.decide,
            {
                "product_version": self.product_version,
                "platform_id": request.platform_id,
                "category_leaf_id": request.category_leaf_id,
                "field_id": request.field_id,
                "canonical_field": canonical_field,
                "snapshot_version": snapshot.snapshot_version,
            },
        )
        if response.get("status") != "auto_fill_ready":
            if response.get("reason_code") not in {"evidence_conflict", "review_learning_conflict"}:
                semantic = await self._semantic_choice(request, snapshot)
                if semantic is not None:
                    return semantic
            await self._raise_review(
                request,
                snapshot,
                canonical_field,
                str(response.get("reason_code") or "insufficient_evidence"),
                response.get("value_id")
                if isinstance(response.get("value_id"), str)
                else None,
            )
            return None
        proposed = response.get("value_id")
        decision = validate_decision(
            DecisionInput(
                canonical_field=canonical_field,
                proposed_value_id=str(proposed or ""),
                evidence_kinds=tuple(
                    str(value) for value in response.get("evidence_kinds", ())
                ),
                source=str(response.get("source") or ""),
                mature_rule=response.get("mature_rule") is True,
                support_count=response.get("support_count", 0),
                calibrated_acceptance_rate=response.get(
                    "calibrated_acceptance_rate", 0.0
                ),
                expected_snapshot_version=str(
                    response.get("snapshot_version") or ""
                ),
            ),
            snapshot,
        )
        if decision.status is not DecisionStatus.AUTO_FILL_READY:
            await self._raise_review(
                request,
                snapshot,
                canonical_field,
                decision.reason_code,
                str(proposed) if isinstance(proposed, str) else None,
            )
            return None
        return self._remember_resolved(
            request,
            ResolvedAttribute(
                decision.value_id or "",
                decision.value_label or "",
                str(response.get("source") or ""),
                decision.snapshot_version,
            ),
        )


__all__ = [
    "AttributeRequest",
    "AttributeRuntime",
    "NO_FILL_LABEL",
    "NO_FILL_VALUE_ID",
    "ResolvedAttribute",
    "ReviewBatchRequired",
    "ReviewRequired",
]
