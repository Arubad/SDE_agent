"""Strip personal data and secrets from text before it is logged or traced (stdlib only)."""
from __future__ import annotations

import os
import re
from typing import Any

SECRET_ENV_NAMES: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "QDRANT_API_KEY",
    "TESTFORGE_FOUNDRY_API_KEY",
    "PREMIUM_PASSCODE",
    "HF_TOKEN",
    "GITHUB_TOKEN",
    "YOUTUBE_API_KEY",
)
_MIN_SECRET_LEN = 6

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_KEYS = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_\-]{8,}"
    r"|sk-[A-Za-z0-9_\-]{20,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_\-]{35})"
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]+")
# Digits with optional + and space/dot/dash/paren separators; must not start or end inside a word.
_PHONE = re.compile(r"(?<![\w.+])\+?\d[\d\s.()\-]{5,}\d(?![\w])")


def _phone_sub(m: re.Match) -> str:
    return "[phone]" if sum(c.isdigit() for c in m.group()) >= 10 else m.group()


def _secret_values() -> list[str]:
    vals = {os.environ.get(n, "") for n in SECRET_ENV_NAMES}
    return sorted((v for v in vals if len(v) >= _MIN_SECRET_LEN), key=len, reverse=True)


def redact(text: str) -> str:
    for v in _secret_values():
        text = text.replace(v, "[secret]")
    text = _EMAIL.sub("[email]", text)
    text = _KEYS.sub("[secret]", text)
    text = _BEARER.sub("Bearer [secret]", text)
    return _PHONE.sub(_phone_sub, text)


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v) for v in obj)
    return obj
