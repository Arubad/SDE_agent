"""Lightweight request tracing: nested spans with timings, token usage and status.

Each chat request gets a Trace (held in a ContextVar so tools and LLM helpers can add spans
without plumbing). The trace is streamed to the UI and logged as one JSON line per request.
"""
from __future__ import annotations

import contextvars
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from backend.core.cost import span_cost
from backend.core.redact import redact_obj

_current: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar("current_trace", default=None)


@dataclass
class Span:
    name: str
    kind: str
    start: float
    attrs: dict = field(default_factory=dict)
    end: float | None = None
    status: str = "ok"

    def set(self, **attrs: Any) -> None:
        self.attrs.update({k: v for k, v in attrs.items() if v is not None})

    def to_dict(self, t0: float) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "start_ms": round((self.start - t0) * 1000),
            "duration_ms": round(((self.end or time.perf_counter()) - self.start) * 1000),
            "status": self.status,
            **self.attrs,
        }


class Trace:
    def __init__(self, name: str = "request"):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.t0 = time.perf_counter()
        self.spans: list[Span] = []
        self.llm_calls = 0

    @contextmanager
    def span(self, name: str, kind: str = "step", **attrs: Any):
        s = Span(name=name, kind=kind, start=time.perf_counter(), attrs=dict(attrs))
        self.spans.append(s)
        try:
            yield s
        except BaseException as e:
            s.status = "error"
            s.set(error=f"{type(e).__name__}: {str(e)[:300]}")
            raise
        finally:
            s.end = time.perf_counter()

    def count_llm_call(self) -> None:
        self.llm_calls += 1

    def summary(self) -> dict:
        spans = [s.to_dict(self.t0) for s in self.spans]
        input_tokens = sum(s.get("input_tokens", 0) for s in spans)
        cache_read = sum(s.get("cache_read_tokens", 0) for s in spans)
        cache_write = sum(s.get("cache_write_tokens", 0) for s in spans)
        prompt_tokens = cache_read + cache_write + input_tokens
        return {
            "trace_id": self.id,
            "name": self.name,
            "total_ms": round((time.perf_counter() - self.t0) * 1000),
            "llm_calls": self.llm_calls,
            "input_tokens": input_tokens,
            "output_tokens": sum(s.get("output_tokens", 0) for s in spans),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_hit_rate": round(cache_read / prompt_tokens, 3) if prompt_tokens else 0.0,
            "cost_usd": round(sum(span_cost(s) for s in spans if s["kind"] == "llm"), 6),
            "spans": spans,
        }

    def log(self) -> None:
        print("[TRACE] " + json.dumps(redact_obj(self.summary()), default=str), flush=True)


def current_trace() -> Trace:
    """The active request trace, or a throwaway one (offline scripts, MCP calls from IDEs)."""
    return _current.get() or Trace("untraced")


def start_trace(name: str) -> Trace:
    trace = Trace(name)
    _current.set(trace)
    return trace
