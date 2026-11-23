"""Untrusted text, fenced: `wrap` puts text a server or a web page wrote inside `<untrusted_web_content>` and escapes
every "<"-like character in it, so nothing inside can close the fence. Shared by the ingestion agent's tools and the
MCP gateway (moved unchanged from `backend/tutor/ingest/agent_tools.py`, which re-exports these names)."""
from __future__ import annotations

import contextlib
import contextvars
import re
import unicodedata

TAG = "untrusted_web_content"


_WRAPPED = re.compile(r"\s*<" + TAG + r">\s*(.*?)\s*</" + TAG + r">\s*", re.S)
# Every character NFKC turns into "<" ("<", U+FE64 small, U+FF1C fullwidth; computed so a newer Unicode cannot
# add one unnoticed). Inside untrusted content each becomes "&lt;", so no tag can ever be formed there,
# whatever padding follows. Linear time (str.translate); text without such a character is unchanged.
_LT = str.maketrans({chr(c): "&lt;" for c in range(0x110000) if unicodedata.normalize("NFKC", chr(c)) == "<"})


# TEST-ONLY SEAM, used by the ingestion-agent eval (`tutor/eval/ingest_agent_eval.py`) to measure what the two
# defenses are worth: `defenses_off()` makes `wrap` a no-op and the URL policy accept any URL string. It is a
# context variable, on by default, with no setting, env var or API route that can change it; only code that
# imports and enters the context manager (the eval, a test) can turn it off, and only for that context.
_DEFENSES = contextvars.ContextVar("ingest_agent_defenses", default=True)


@contextlib.contextmanager
def defenses_off():
    """Run a block with the untrusted wrapping and the URL policy disabled. Never use outside the eval/tests."""
    token = _DEFENSES.set(False)
    try:
        yield
    finally:
        _DEFENSES.reset(token)


def wrap(text: str) -> str:
    """Untrusted web text, fenced; every "<"-like character inside it is escaped, so it cannot close the fence."""
    if not _DEFENSES.get():
        return str(text or "")
    return f"<{TAG}>{str(text or '').translate(_LT)}</{TAG}>"


def unwrap(value):
    """A URL passed back by the model as it was shown, wrapper included: strip one exact wrapper."""
    if isinstance(value, str) and (m := _WRAPPED.fullmatch(value)):
        return m.group(1)
    return value
