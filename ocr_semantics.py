"""Run-scoped label corrections; models never supply measurement numbers."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from learning_models import canonical_sha256


MEANINGS = {
    "clothing": {"length": "衣长", "chest": "胸围", "shoulder": "肩宽", "sleeve": "袖长", "cuff": "袖口", "other": "其他测量项"},
    "pants": {"length": "裤长", "waist": "腰围", "hip": "臀围", "foot_opening": "脚围", "other": "其他测量项"},
}
CURRENT_SESSION: ContextVar = ContextVar("ocr_semantic_session", default=None)


class OCRJudgmentRequired(RuntimeError):
    """Escapes old RecognitionError catches so the common review loop owns it."""
    def __init__(self, path: Path, tokens: tuple, garment_kind: str, parser: Callable, reason: str):
        super().__init__(reason)
        self.path = path
        self.tokens = tokens
        self.garment_kind = garment_kind
        self.parser = parser
        self.failure = None
        self.key = canonical_sha256({"path": str(path.resolve()), "tokens": [asdict(t) for t in tokens]})

    @property
    def label_token_ids(self) -> tuple:
        from size_image_recognition import _normalize_size, _number, _center_x, _center_y
        headers = [t for t in self.tokens if _normalize_size(t.text)]
        if not headers:
            return ()
        first_column = min(_center_x(t) for t in headers)
        header_bottom = max(t.y + t.height for t in headers)
        numeric = [t for t in self.tokens if _number(t.text) is not None]
        # A row name must sit below the header and align with multiple data
        # cells. Header titles and footnotes cannot be corrected into rows.
        return tuple(i for i, t in enumerate(self.tokens)
                     if _number(t.text) is None and _normalize_size(t.text) is None
                     and _center_x(t) < first_column and _center_y(t) > header_bottom
                     and sum(_center_x(n) > t.x + t.width
                             and abs(_center_y(n) - _center_y(t)) <= max(t.height, n.height) * .8
                             for n in numeric) >= 2)

    def corrected(self, decisions: Mapping[int, str]) -> tuple:
        allowed = MEANINGS.get(self.garment_kind, {})
        if any(type(i) is not int or i not in self.label_token_ids or meaning not in allowed
               for i, meaning in decisions.items()):
            raise ValueError("OCR correction must reference a real row label")
        return tuple(replace(t, text=allowed[decisions[i]]) if i in decisions else t
                     for i, t in enumerate(self.tokens))

    def validate(self, decisions: Mapping[int, str]) -> Any:
        return self.parser(self.corrected(decisions))


class OCRSemanticSession:
    def __init__(self):
        self.corrections: dict[str, dict[int, str]] = {}
        self.predictions: dict[str, Mapping[str, Any]] = {}
        self.model_decisions: list[dict[str, Any]] = []
        self.model_events: list[dict[str, Any]] = []
        self.report_sink = None

    def record_decision(self, decision):
        self.model_decisions.append(decision)
        if self.report_sink is not None:
            self.report_sink()

    @contextmanager
    def activate(self):
        token = CURRENT_SESSION.set(self)
        try:
            yield self
        finally:
            CURRENT_SESSION.reset(token)


def parse_chart(path: Path, garment_kind: str, parser: Callable):
    from size_image_recognition import RecognitionError, vision_ocr
    tokens = vision_ocr(path)
    session = CURRENT_SESSION.get()
    error = OCRJudgmentRequired(Path(path), tokens, garment_kind, parser, "")
    applied = session.corrections.get(error.key, {}) if session else {}
    try:
        result = error.validate(applied)
        if session is not None:
            from model_judgments import reason_text, record_event
            code = "ocr_reused" if applied else "ocr_rule_match"
            record_event(session, {"key": "skip:" + error.key, "platform_id": "base", "field_id": "ocr:chart",
                         "field_label": "尺码 OCR", "excel_value": str(path.name), "source_image": str(path),
                         "status": "skipped", "api_called": False, "reason_code": code,
                         "reason": reason_text(code)}, sink=session.report_sink)
        return result
    except RecognitionError as failure:
        if session is None or garment_kind not in MEANINGS:
            raise
        error.args = (str(failure),)
        error.failure = failure
        raise error from failure


def validate_suggestions(error: OCRJudgmentRequired, response: Mapping) -> dict[int, tuple[str, float]]:
    import math
    if response.get("status") != "suggested" or response.get("source") != "deepseek":
        return {}
    values = response.get("suggestions")
    if not isinstance(values, list) or len(values) > len(error.label_token_ids):
        return {}
    result = {}
    for value in values:
        if not isinstance(value, dict) or set(value) != {"token_id", "meaning", "confidence"}:
            return {}
        ident, meaning, confidence = value["token_id"], value["meaning"], value["confidence"]
        if (type(ident) is not int or ident not in error.label_token_ids or ident in result
                or not isinstance(meaning, str) or meaning not in MEANINGS[error.garment_kind]
                or type(confidence) not in (int, float) or not math.isfinite(confidence)
                or not 0 <= confidence <= 1):
            return {}
        result[ident] = (meaning, confidence)
    return result
