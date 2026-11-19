"""Export finished request traces to OpenTelemetry (OTLP). Platform-only; never raises."""
from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status, StatusCode

from backend.core.redact import redact_obj
from backend.core.trace import Trace

log = logging.getLogger(__name__)
_warned = False
_EXPORT_TIMEOUT_S = 2


def _warn_once(msg: str, exc: BaseException | None = None) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.warning("%s%s", msg, f": {type(exc).__name__}: {exc}" if exc else "")


def _reset_warning_state() -> None:
    global _warned
    _warned = False


class _GuardedExporter(SpanExporter):
    """Delegates to a real exporter; never raises and logs one concise warning for the first failure."""

    def __init__(self, inner: SpanExporter):
        self._inner = inner
        self._warned = False

    def export(self, spans) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
            if result == SpanExportResult.SUCCESS:
                return result
            err = None
        except Exception as e:
            err = e
        if not self._warned:
            self._warned = True
            log.warning(
                "OpenTelemetry export failed (further failures are not logged)%s",
                f": {type(err).__name__}: {err}" if err else "",
            )
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception:
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception:
            return False


def setup_tracing(endpoint: str | None, *, service_name: str = "roadmap-tutor", exporter=None):
    if exporter is None and not endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(_GuardedExporter(exporter)))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # The OTLP exporter logs every failed batch itself (with tracebacks); the guard logs once instead.
        logging.getLogger("opentelemetry.exporter.otlp").setLevel(logging.CRITICAL)
        provider.add_span_processor(
            BatchSpanProcessor(_GuardedExporter(OTLPSpanExporter(endpoint=endpoint, timeout=_EXPORT_TIMEOUT_S)))
        )
    return provider


def _primitive(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str)):
        return value
    return json.dumps(value, default=str)


def _kind(kind: str) -> str:
    return {"llm": "LLM", "tool": "TOOL"}.get(kind, "CHAIN")


def export_trace(trace: Trace, provider) -> None:
    if provider is None:
        return
    try:
        tracer = provider.get_tracer("roadmap-tutor")

        def ns(t: float) -> int:
            return int((trace.wall_t0 + (t - trace.t0)) * 1e9)

        end_of_trace = max([s.end or s.start for s in trace.spans] + [trace.t0])
        root = tracer.start_span(trace.name, start_time=ns(trace.t0), attributes={"trace.id": trace.id})
        ctx = otel_trace.set_span_in_context(root)
        for s in trace.spans:
            attrs = {k: _primitive(v) for k, v in redact_obj(s.attrs).items()}
            attrs["openinference.span.kind"] = _kind(s.kind)
            if "input_tokens" in s.attrs:
                attrs["llm.token_count.prompt"] = attrs["input_tokens"]
            if "output_tokens" in s.attrs:
                attrs["llm.token_count.completion"] = attrs["output_tokens"]
            child = tracer.start_span(s.name, context=ctx, start_time=ns(s.start), attributes=attrs)
            if s.status == "error":
                child.set_status(Status(StatusCode.ERROR, str(attrs.get("error", ""))[:300]))
            child.end(end_time=ns(s.end or s.start))
        root.end(end_time=ns(end_of_trace))
    except Exception as e:  # telemetry must never break a request
        _warn_once("OpenTelemetry export failed", e)
