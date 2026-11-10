from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort: whole text, then fenced block, then the outermost {...} span."""
    if not text:
        return None
    t = text.strip()
    for candidate in (t, *(m.group(1) for m in _FENCE.finditer(t))):
        try:
            v = json.loads(candidate)
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            v = json.loads(t[i : j + 1])
            if isinstance(v, dict):
                return v
        except ValueError:
            return None
    return None


def classify_error(text: str) -> str:
    s = (text or "").lower()
    if any(k in s for k in ("authentication_error", "401", "oauth", "not logged in", "login required",
                            "re-authenticate", "unauthorized")):
        return "auth"
    if any(k in s for k in ("rate limit", "rate_limit", "429", "usage limit", "quota", "too many requests")):
        return "rate_limit"
    if any(k in s for k in ("model not found", "unknown model", "invalid model", "model_not_found",
                            "does not exist", "not supported", "unsupported model")):
        return "model_unavailable"
    return "other"


def shorten(s: str, n: int = 400) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"
