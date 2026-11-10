"""Access tiers. Public = free Gemini pipeline; premium (passcode) = Claude Haiku pipeline + MCP endpoint."""
from __future__ import annotations

import hmac
import threading
import time

from backend.core import config

WINDOW_S = 15 * 60
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


class TooManyAttempts(Exception):
    pass


def client_ip(headers, fallback: str = "unknown") -> str:
    # HF Spaces sits behind a proxy; the first X-Forwarded-For hop is the visitor
    fwd = headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or fallback


def premium_enabled() -> bool:
    return bool(config.PREMIUM_PASSCODE)


def check_passcode(code: str | None, ip: str) -> bool:
    """Constant-time passcode check with a per-IP failure limit (brute-force protection)."""
    if not code:
        return False
    if not premium_enabled():
        return False
    now = time.time()
    with _lock:
        recent = [t for t in _failures.get(ip, []) if now - t < WINDOW_S]
        _failures[ip] = recent
        if len(recent) >= config.PASSCODE_MAX_FAILURES:
            raise TooManyAttempts("Too many wrong passcodes. Try again in 15 minutes.")
    ok = hmac.compare_digest(code.strip().encode(), config.PREMIUM_PASSCODE.encode())
    if not ok:
        with _lock:
            _failures.setdefault(ip, []).append(now)
    return ok
