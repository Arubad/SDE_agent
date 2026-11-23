"""Result shaping for the MCP gateway: a fastmcp CallToolResult -> the agent loop's `ToolCaller` shape, then the
untrusted fence and the size cap (see the `gateway` module docstring for the rules)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from backend.tutor.untrusted import wrap

if TYPE_CHECKING:
    from backend.tutor.mcp.gateway import CallContext
    from backend.tutor.mcp.registry import ServerSpec

CAP = 20_000
RAW_CAP = 5 * 1024 * 1024


def _text(result) -> str:
    return "\n".join(b.text for b in result.content if getattr(b, "text", None) is not None)


def _shape(result, spec: ServerSpec) -> dict:
    """fastmcp CallToolResult -> the loop's shape, before wrapping and capping. Only an own (inproc) server's
    `{"passages": [...]}` becomes passages; from any other server it is ordinary data."""
    if result.is_error:
        return {"error": _text(result)}
    sc = result.structured_content
    if spec.transport == "inproc" and isinstance(sc, dict) and isinstance(sc.get("passages"), list):
        return {"passages": sc["passages"]}
    if sc is not None:
        return {"data": json.loads(json.dumps(sc, default=str))}
    return {"data": {"text": _text(result)}}


def _leaves(obj, out: list) -> list:
    """(container, key) for every string leaf below `obj` (dict values and list items; keys are left alone)."""
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    for k, v in items:
        if isinstance(v, str):
            out.append((obj, k))
        else:
            _leaves(v, out)
    return out


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def _marker(n: int) -> str:
    return f" [truncated {n} chars]"


def _cut(text: str, excess: int) -> tuple[str, int] | None:
    """(kept text, chars cut) so that text + marker is at least `excess` chars shorter than text, or None when
    cutting the whole text would save less than the marker costs."""
    cut = excess + len(_marker(excess))
    cut = excess + len(_marker(cut))  # the marker's own length depends on the number in it
    if cut > len(text):
        if len(text) <= len(_marker(len(text))):
            return None
        cut = len(text)
    return text[:len(text) - cut], cut


# Passages feed backend/agent/citations.py, which indexes these keys directly.
_PASSAGE_KEYS = ("chunk_id", "url", "text", "title")


def _passages(found: list, cap: int, canonical: str) -> dict:
    """Valid passages only, trailing ones dropped until the JSON fits; the first is kept (its text cut). No
    passages is a result (no hits); only a non-empty list with no valid entry is an error."""
    ok = [p for p in found if isinstance(p, dict) and all(isinstance(p.get(k), str) for k in _PASSAGE_KEYS)]
    if found and not ok:
        return {"error": f"{canonical} returned no valid passages"}
    out = {"passages": ok}
    while len(ok) > 1 and _size(out) > cap:
        ok.pop()
    if (excess := _size(out) - cap) > 0 and (cut := _cut(ok[0]["text"], excess)):
        ok[0] = {**ok[0], "text": cut[0] + _marker(cut[1])}
    return out


def _finish(out: dict, spec: ServerSpec, ctx: CallContext, canonical: str) -> dict:
    """Wrap every string leaf (untrusted server, not raw), then cut the largest leaves, marker included in the
    budget, until the JSON fits; a leaf too small to pay for its marker is never touched."""
    cap = RAW_CAP if ctx.raw else CAP
    if "passages" in out:
        return _passages(out["passages"], cap, canonical)
    fence = spec.untrusted and not ctx.raw
    leaves = _leaves(out, [])
    raw = [c[k] for c, k in leaves]
    for i, (c, k) in enumerate(leaves):
        c[k] = wrap(raw[i]) if fence else raw[i]
    total = _size(out)
    for i in sorted(range(len(leaves)), key=lambda j: -len(raw[j])):
        if total <= cap:
            break
        if (cut := _cut(raw[i], total - cap)) is None:
            continue
        c, k = leaves[i]
        c[k] = (wrap(cut[0]) if fence else cut[0]) + _marker(cut[1])
        total = _size(out)
    return out
