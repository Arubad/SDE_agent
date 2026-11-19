"""Prompt caching markers on every Claude call, the PROMPT_CACHE=false legacy request, and cache usage in traces."""
import asyncio

import pytest

from backend.agent.loop import run_tool_loop
from backend.agent.orchestrator import Agent, system_prompt
from backend.core import config, fake_llm, llm, trace as trace_mod
from backend.core.fake_llm import FakeReply
from backend.core.trace import Trace

SCHEMA = {"type": "object", "required": ["k"], "properties": {"k": {"type": "integer"}}}
TOOLS = [{"name": "b", "description": "B", "input_schema": {"type": "object"}},
         {"name": "a", "description": "A", "input_schema": {"type": "object"}}]


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(config, "PROMPT_CACHE", True)
    fake_llm.reset()
    yield
    fake_llm.reset()


def _one_loop_step(tools):
    async def call_tool(name, args):
        return {"passages": []}

    async def emit(_):
        pass

    return asyncio.run(run_tool_loop([{"role": "user", "content": "q"}], system="loop system", tool_defs=tools,
                                     call_tool=call_tool, check_call=lambda n, a, seen: None, trace=Trace(),
                                     emit=emit, max_steps=1))


def test_cache_markers_on_every_call():
    llm.haiku_json(system="json system", prompt="p", schema=SCHEMA)
    _one_loop_step(TOOLS)
    assert len(fake_llm.requests) == 2
    for request in fake_llm.requests:
        assert isinstance(request["system"], list)
        assert request["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert request["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in fake_llm.requests[1]["tools"]] == ["a", "b"]
    assert [t["name"] for t in TOOLS] == ["b", "a"]  # the caller's list is not reordered


def test_prompt_cache_off_matches_legacy_request(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE", False)
    llm.haiku_json(system="json system", prompt="p", schema=SCHEMA, effort="low", max_tokens=123)
    assert fake_llm.requests == [{
        "model": config.HAIKU_MODEL,
        "max_tokens": 123,
        "system": "json system",
        "messages": [{"role": "user", "content": "p"}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    }]


def test_prompt_cache_off_leaves_loop_request_unchanged(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE", False)
    _one_loop_step(TOOLS)
    request = fake_llm.requests[0]
    assert request["system"] == "loop system" and "cache_control" not in request
    assert [t["name"] for t in request["tools"]] == ["b", "a"]


@pytest.mark.parametrize("cache_on", [True, False])
def test_generate_claude_request(monkeypatch, cache_on):
    monkeypatch.setattr(config, "PROMPT_CACHE", cache_on)

    async def emit(_):
        pass

    asyncio.run(Agent("premium", Trace(), emit).generate_claude("q", [], []))
    request = fake_llm.requests[0]
    if cache_on:
        assert request["system"] == [{"type": "text", "text": system_prompt("pipeline"),
                                      "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        assert request["cache_control"] == {"type": "ephemeral"}
    else:
        assert request["system"] == system_prompt("pipeline") and "cache_control" not in request
        assert set(request) == {"model", "max_tokens", "system", "messages", "output_config"}


def test_requests_are_snapshots_not_live_kwargs():
    replies = [FakeReply(tool_uses=[("a", {})]), FakeReply(text="done")]
    with fake_llm.script(replies):
        async def call_tool(name, args):
            return {"passages": []}

        async def emit(_):
            pass

        asyncio.run(run_tool_loop([{"role": "user", "content": "q"}], system="s", tool_defs=TOOLS,
                                  call_tool=call_tool, check_call=lambda n, a, seen: None, trace=Trace(),
                                  emit=emit, max_steps=4))
    assert len(fake_llm.requests[0]["messages"]) == 1
    assert len(fake_llm.requests[1]["messages"]) == 3


def test_usage_and_cache_tokens_reach_trace():
    trace = Trace()
    token = trace_mod._current.set(trace)
    try:
        usage = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 900,
                 "cache_creation_input_tokens": 0}
        with fake_llm.script([FakeReply(json={"k": 1}, usage=usage)]):
            llm.haiku_json(system="s", prompt="p", schema=SCHEMA)
    finally:
        trace_mod._current.reset(token)
    summary = trace.summary()
    assert summary["input_tokens"] == 100 and summary["output_tokens"] == 50
    assert summary["cache_read_tokens"] == 900 and summary["cache_write_tokens"] == 0
    assert summary["cache_hit_rate"] == 0.9
    assert summary["cost_usd"] == 0.0  # the fake model is not in the price table


def test_cache_write_split_is_recorded():
    trace = Trace()
    token = trace_mod._current.set(trace)
    try:
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": None,
                 "cache_creation_input_tokens": 700,
                 "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 600}}
        with fake_llm.script([FakeReply(json={"k": 1}, usage=usage)]):
            llm.haiku_json(system="s", prompt="p", schema=SCHEMA)
    finally:
        trace_mod._current.reset(token)
    attrs = trace.spans[0].attrs
    assert attrs["cache_read_tokens"] == 0 and attrs["cache_write_tokens"] == 700
    assert attrs["cache_write_5m_tokens"] == 100 and attrs["cache_write_1h_tokens"] == 600


def test_hit_rate_is_zero_without_tokens():
    assert Trace().summary()["cache_hit_rate"] == 0.0
