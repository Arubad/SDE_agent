import logging

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from backend.core.trace import Trace
from backend.tutor.telemetry import export_trace, setup_tracing


def _provider():
    exporter = InMemorySpanExporter()
    return setup_tracing(None, exporter=exporter), exporter


def test_export_builds_parent_child_spans():
    provider, exporter = _provider()
    trace = Trace("chat")
    with trace.span("route"):
        pass
    with trace.span("agent_turn", kind="llm", input_tokens=5, output_tokens=7):
        pass
    export_trace(trace, provider)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"chat", "route", "agent_turn"}
    root = spans["chat"]
    for child in ("route", "agent_turn"):
        assert spans[child].parent.span_id == root.context.span_id
        assert spans[child].context.trace_id == root.context.trace_id
    attrs = spans["agent_turn"].attributes
    assert attrs["openinference.span.kind"] == "LLM"
    assert attrs["llm.token_count.prompt"] == 5
    assert attrs["llm.token_count.completion"] == 7
    assert spans["route"].attributes["openinference.span.kind"] == "CHAIN"
    assert spans["route"].start_time >= root.start_time
    expected = int((trace.wall_t0 + (trace.spans[0].start - trace.t0)) * 1e9)
    assert abs(spans["route"].start_time - expected) < 1_000_000


def test_exported_attributes_are_redacted():
    provider, exporter = _provider()
    trace = Trace("chat")
    with trace.span("search", query="mail me at a@b.com", meta={"k": [1, 2]}):
        pass
    export_trace(trace, provider)
    span = [s for s in exporter.get_finished_spans() if s.name == "search"][0]
    assert span.attributes["query"] == "mail me at [email]"
    assert isinstance(span.attributes["meta"], str)


def test_error_span_status():
    provider, exporter = _provider()
    trace = Trace("chat")
    try:
        with trace.span("tool", kind="tool"):
            raise ValueError("boom")
    except ValueError:
        pass
    export_trace(trace, provider)
    span = [s for s in exporter.get_finished_spans() if s.name == "tool"][0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["openinference.span.kind"] == "TOOL"


class _Boom(InMemorySpanExporter):
    def export(self, spans):
        raise RuntimeError("down")


class _Failing(InMemorySpanExporter):
    def export(self, spans):
        return SpanExportResult.FAILURE


def _one_span_trace():
    trace = Trace("chat")
    with trace.span("route"):
        pass
    return trace


@pytest.mark.parametrize("exporter_cls", [_Boom, _Failing])
def test_export_failure_is_swallowed_and_logged_once(exporter_cls, caplog):
    provider = setup_tracing(None, exporter=exporter_cls())
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            export_trace(_one_span_trace(), provider)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None
    assert "export failed" in warnings[0].getMessage()


def test_unreachable_endpoint_does_not_raise(capfd):
    provider = setup_tracing("http://127.0.0.1:9/v1/traces")
    try:
        export_trace(_one_span_trace(), provider)
        provider.force_flush(timeout_millis=2000)
        export_trace(_one_span_trace(), object())  # a broken provider must not raise either
    finally:
        provider.shutdown()
    assert "Traceback" not in capfd.readouterr().err


def test_no_endpoint_is_noop():
    assert setup_tracing(None) is None
    trace = Trace("chat")
    export_trace(trace, None)
