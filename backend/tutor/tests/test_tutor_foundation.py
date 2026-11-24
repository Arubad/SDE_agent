import asyncio
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from backend.core import config, fake_llm
from backend.core.fake_llm import FakeReply
from backend.core.llm import LLMUnavailable
from backend.core.trace import Span, Trace, start_trace
from backend.tutor.tutor.budget import OUTAGE_MESSAGE, Budget, BudgetExceeded, with_llm_retry
from backend.tutor.tutor.checkpoint import checkpoint_url, delete_thread, open_saver
from backend.tutor.tutor.memo import haiku_json_memo

SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"],
          "additionalProperties": False}


def _llm_span(**attrs) -> Span:
    return Span(name="llm", kind="llm", start=0.0, end=0.0, attrs=attrs)


def _trace(**attrs) -> Trace:
    t = Trace("t")
    t.spans.append(_llm_span(**attrs))
    return t


def test_budget_token_message():
    with pytest.raises(BudgetExceeded) as e:
        Budget(6, 60_000, 0.02).check(_trace(input_tokens=70_000))
    assert e.value.message == ("This turn reached its token budget (70000 of 60000). Your session is saved; "
                               "ask again to continue.")
    assert str(e.value) == e.value.message


def test_budget_counts_all_token_kinds_and_is_strict():
    t = _trace(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=40)
    Budget(6, 100, 1.0).check(t)  # exactly at the limit is fine
    with pytest.raises(BudgetExceeded) as e:
        Budget(6, 99, 1.0).check(t)
    assert "(100 of 99)" in e.value.message


def test_budget_cost_message():
    t = _trace(model=config.HAIKU_MODEL, input_tokens=0, output_tokens=0, cache_read_tokens=0)
    assert t.summary()["cost_usd"] == 0
    t = _trace(model=config.HAIKU_MODEL, output_tokens=42_600)  # 42600 * $0.50 / 1M = $0.0213
    with pytest.raises(BudgetExceeded) as e:
        Budget(6, 10**9, 0.02).check(t)
    assert e.value.message == ("This turn reached its cost budget ($0.0213 of $0.0200). Your session is saved; "
                               "ask again to continue.")


def test_budget_ignores_steps_and_empty_trace():
    Budget(1, 10, 0.01).check(Trace("t"))


def test_outage_message():
    assert OUTAGE_MESSAGE == "The model is unavailable right now. Your session is saved; try again in a minute."


def test_llm_retry_then_raises():
    calls = []

    async def boom():
        calls.append(1)
        raise LLMUnavailable("x")

    with pytest.raises(LLMUnavailable):
        asyncio.run(with_llm_retry(boom, delays=(0, 0)))
    assert len(calls) == 3


def test_llm_retry_recovers_and_other_errors_propagate(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise LLMUnavailable("x")
        return "ok"

    assert asyncio.run(with_llm_retry(flaky)) == "ok"
    assert slept == [1.0, 2.0]

    async def bad():
        calls.append(1)
        raise ValueError("no")

    calls.clear()
    with pytest.raises(ValueError):
        asyncio.run(with_llm_retry(bad))
    assert len(calls) == 1


def test_memo_second_call_skips_model(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM llm_memo"))
    trace = start_trace("memo")
    kw = dict(system="s", prompt="p", schema=SCHEMA, effort="low", span_name="x")
    with fake_llm.script([FakeReply(json={"intent": "question"})]):
        a = haiku_json_memo(engine, **kw)
    b = haiku_json_memo(engine, **kw)
    assert a == b == {"intent": "question"} and len(fake_llm.requests) == 1
    assert [s.name for s in trace.spans] == ["x", "memo_hit"]
    assert trace.spans[1].kind == "memo" and trace.spans[1].attrs["span_name"] == "x"
    haiku_json_memo(engine, **{**kw, "prompt": "other"})  # a different input is a different key
    assert len(fake_llm.requests) == 2
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM llm_memo")).scalar() == 2


def test_checkpoint_url_strips_driver_only():
    assert checkpoint_url("postgresql+psycopg://u:p%40@h:5432/db?sslmode=require") == \
        "postgresql://u:p%40@h:5432/db?sslmode=require"
    assert checkpoint_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


class _S(TypedDict):
    n: int


def _tiny_graph() -> StateGraph:
    g = StateGraph(_S)
    g.add_node("inc", lambda s: {"n": s["n"] + 1})
    g.add_edge(START, "inc")
    g.add_edge("inc", END)
    return g


def test_saver_roundtrip_and_delete(db_url):
    async def run():
        async with open_saver(db_url) as saver:
            graph = _tiny_graph().compile(checkpointer=saver)
            cfg = {"configurable": {"thread_id": "t1"}}
            await graph.ainvoke({"n": 1}, cfg)
            assert (await graph.aget_state(cfg)).values["n"] == 2
            await delete_thread(saver, "t1")
            assert (await graph.aget_state(cfg)).values == {}

    asyncio.run(run())
