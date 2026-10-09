"""DeepSeek official text API. No credentials or remote error bodies escape."""
from __future__ import annotations

import json
import httpx

from .config import Settings


class JudgmentUnavailable(RuntimeError):
    pass


def request_json(settings: Settings, system: str, evidence: dict) -> dict:
    if not settings.deepseek_api_key:
        raise JudgmentUnavailable("deepseek_not_configured")
    try:
        response = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": "Bearer " + settings.deepseek_api_key},
            json={"model": settings.deepseek_model, "thinking": {"type": "disabled"},
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}],
                  "response_format": {"type": "json_object"}, "max_tokens": 4096,
                  "temperature": 0},
            timeout=30,
        )
        response.raise_for_status()
        envelope = response.json()
        choice = envelope["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise JudgmentUnavailable("deepseek_incomplete_output")
        result = json.loads(choice["message"]["content"])
        if not isinstance(result, dict):
            raise ValueError("object required")
        return result
    except JudgmentUnavailable:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        raise JudgmentUnavailable("deepseek_request_failed") from None
