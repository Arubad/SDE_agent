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


def test_export_failure_is_swallowed():
    trace = Trace("chat")
    with trace.span("route"):
        pass
    export_trace(trace, setup_tracing(None, exporter=_Boom()))

    provider = setup_tracing("http://127.0.0.1:9/v1/traces")
    export_trace(trace, provider)
    provider.force_flush(timeout_millis=2000)
    export_trace(trace, object())  # a broken provider must not raise either


def test_no_endpoint_is_noop():
    assert setup_tracing(None) is None
    trace = Trace("chat")
    export_trace(trace, None)
