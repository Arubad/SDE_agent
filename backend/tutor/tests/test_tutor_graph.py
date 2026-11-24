import json
from decimal import Decimal
from typing import get_args

import pytest
from langgraph.types import interrupt
from sqlalchemy import text

from backend.core import config, fake_llm
from backend.core.fake_llm import FakeReply
from backend.core.llm import LLMUnavailable
from backend.tutor.mcp.registry import load_registry
from backend.tutor.settings import Settings
from backend.tutor.tests.tutor_helpers import (
    clear_memo,  # noqa: F401 (fixture)
    interrupts,
    make_deps,
    make_runtime,
    new_session,
    nodes,
    replies,
    route_requests,
    run,
)
from backend.tutor.tutor import budget, coordinator, runtime as runtime_mod
from backend.tutor.tutor.budget import OUTAGE_MESSAGE, BudgetExceeded
from backend.tutor.tutor.checkpoint import delete_thread, open_saver
from backend.tutor.tutor.coordinator import COORDINATOR_SCHEMA
from backend.tutor.tutor.graph import EXERCISE_TEXT, PLACEHOLDER_TEXT, emit, build_tutor_graph, single
from backend.tutor.tutor.runtime import RESUME_SHAPES, BadResume, Busy, SessionEnded
from backend.tutor.tutor.state import Action, Route, reply_event

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def instant_retries(monkeypatch):
    monkeypatch.setattr(budget, "RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture
def rt(engine, clear_memo):  # noqa: F811
    return make_runtime(engine)


@pytest.fixture
def sid(engine):
    return new_session(engine)


def _asking_teacher(kind="pick"):
    """A teacher group that asks before answering: interrupt first, nothing before it (D8)."""

    def add(builder, deps):
        async def teacher(state):
            answer = interrupt({"kind": kind, "summary": "pick one"})
            emit(reply_event(f"you said {answer['choice']}"))
            return {"messages": [*(state.get("messages") or []), {"role": "assistant", "content": "answered"}]}

        return single(builder, "teacher", teacher)

    return add


def _raising_teacher(exc):
    def add(builder, deps):
        async def teacher(state):
            raise exc

        return single(builder, "teacher", teacher)

    return add


def _cost(engine, sid):
    with engine.connect() as c:
        return c.execute(text("SELECT cost_usd FROM tutor_sessions WHERE id = :i"), {"i": sid}).scalar()


def test_no_flashcard_anywhere():
    assert "flashcard" not in json.dumps(COORDINATOR_SCHEMA).lower()
    assert all("flashcard" not in r for r in get_args(Route)) and all("flashcard" not in a for a in get_args(Action))
    assert all("flashcard" not in n for n in build_tutor_graph(make_deps()).get_graph().nodes)


def test_schema_lists_every_route():
    assert COORDINATOR_SCHEMA["title"] == "TutorRoute"
    assert COORDINATOR_SCHEMA["properties"]["route"]["enum"] == list(get_args(Route))


def test_assessor_may_only_search_the_roadmap():
    assert load_registry(Settings().mcp_registry, {}).agents["assessor"] == ("kb.search",)


@pytest.mark.parametrize("action,node", [("start", "session_start"), ("continue", "teacher"), ("quiz", "assessor"),
                                         ("plan", "planner"), ("research", "researcher"),
                                         ("recap", "session_start")])
async def test_buttons_route_without_model(rt, sid, action, node):
    events = await run(rt, sid, {"action": action})
    assert nodes(events) == ["coordinator", node]
    assert route_requests(fake_llm.requests) == []
    assert replies(events) == [PLACEHOLDER_TEXT]
    assert events[-1]["type"] == "done"


async def test_free_text_is_classified(rt, sid):
    with fake_llm.script([FakeReply(json={"route": "quiz_me"})]):
        events = await run(rt, sid, {"text": "test me"})
    assert nodes(events) == ["coordinator", "assessor"]
    [req] = route_requests(fake_llm.requests)
    assert req["output_config"]["effort"] == "low"
    assert (await rt.state(sid))["messages"] == [{"role": "user", "content": "test me"}]


async def test_classifier_sees_the_last_four_messages(rt, sid):
    for i in range(5):
        with fake_llm.script([FakeReply(json={"route": "question"})]):
            await run(rt, sid, {"text": f"message {i}"})
    fake_llm.requests.clear()
    with fake_llm.script([FakeReply(json={"route": "question"})]):
        await run(rt, sid, {"text": "message 5"})
    [req] = route_requests(fake_llm.requests)
    prompt = json.loads(req["messages"][0]["content"])
    assert prompt["learner_message"] == "message 5"
    assert [m["content"] for m in prompt["recent_messages"]] == [f"message {i}" for i in range(1, 5)]


@pytest.mark.parametrize("route,node", [("continue_lesson", "teacher"), ("question", "teacher"),
                                        ("new_topic", "researcher"), ("recap", "session_start")])
async def test_text_routes(rt, sid, route, node):
    with fake_llm.script([FakeReply(json={"route": route})]):
        events = await run(rt, sid, {"text": f"please {route}"})
    assert nodes(events) == ["coordinator", node]


async def test_exercise_routes_to_stub_then_teacher(rt, sid):
    with fake_llm.script([FakeReply(json={"route": "exercise"})]):
        events = await run(rt, sid, {"text": "give me a coding exercise"})
    assert nodes(events) == ["coordinator", "exercise_stub", "teacher"]
    assert replies(events) == [EXERCISE_TEXT, PLACEHOLDER_TEXT]
    assert EXERCISE_TEXT == "Code exercises arrive in a later version; here is the concept instead."
    stub = next(e for e in events if e["type"] == "reply")
    assert stub == {"type": "reply", "text": EXERCISE_TEXT, "sources": {"roadmap": [], "web": []}, "notice": None}


@pytest.mark.parametrize("route,node", [("quiz_me", "teacher"), ("exercise", "teacher"), ("recap", "teacher"),
                                        ("continue_lesson", "teacher"), ("new_topic", "researcher")])
async def test_qa_mode_sends_routes_to_teacher(engine, rt, route, node):
    qa = new_session(engine, mode="qa")
    with fake_llm.script([FakeReply(json={"route": route})]):
        events = await run(rt, qa, {"text": f"qa {route}"})
    assert nodes(events) == ["coordinator", node]


async def test_graph_input_carries_the_session(engine, clear_memo):  # noqa: F811
    seen = {}

    def add(builder, deps):
        async def teacher(state):
            seen.update({k: state.get(k) for k in ("roadmap", "session_id", "mode", "input", "route")})

        return single(builder, "teacher", teacher)

    rt = make_runtime(engine, {"teacher": add})
    sid = new_session(engine, mode="qa")
    await run(rt, sid, {"action": "continue"})
    with engine.connect() as c:
        slug = c.execute(text("SELECT r.slug FROM roadmaps r JOIN tutor_sessions s ON s.roadmap_id = r.id "
                              "WHERE s.id = :i"), {"i": sid}).scalar()
    assert seen == {"roadmap": slug, "session_id": sid, "mode": "qa", "input": {"action": "continue"},
                    "route": "continue"}


async def test_unknown_and_ended_sessions(engine, rt):
    with pytest.raises(LookupError):
        await run(rt, "00000000-0000-0000-0000-000000000000", {"action": "continue"})
    with pytest.raises(LookupError):
        await run(rt, "not-a-uuid", {"action": "continue"})
    with pytest.raises(SessionEnded):
        await run(rt, new_session(engine, status="ended"), {"action": "continue"})


@pytest.mark.parametrize("turn", [{}, {"text": 3}, {"action": "flashcards"}, {"action": "quiz", "topic": "x"},
                                  {"text": "a", "action": "quiz"}, {"resume": 1, "text": "a"}])
async def test_malformed_turns_are_refused(rt, sid, turn):
    with pytest.raises(ValueError):
        await run(rt, sid, turn)


async def test_research_topic_is_redacted(engine, clear_memo):  # noqa: F811
    seen = {}

    def add(builder, deps):
        async def researcher(state):
            seen.update(state["input"])

        return single(builder, "researcher", researcher)

    rt = make_runtime(engine, {"researcher": add})
    await run(rt, new_session(engine), {"action": "research", "topic": "Rust, mail me at a@b.co"})
    assert seen["action"] == "research" and "a@b.co" not in seen["topic"] and seen["topic"].startswith("Rust")


async def test_concurrent_turn_is_busy(rt, sid):
    first = rt.run_turn(sid, {"action": "continue"})
    assert (await anext(first)) == {"type": "node", "name": "coordinator"}
    with pytest.raises(Busy):
        await anext(rt.run_turn(sid, {"action": "continue"}))
    rest = [e async for e in first]
    assert rest[-1]["type"] == "done"
    assert (await run(rt, sid, {"action": "continue"}))[-1]["type"] == "done"  # the lock was released


async def test_other_sessions_are_not_busy(engine, rt, sid):
    first = rt.run_turn(sid, {"action": "continue"})
    await anext(first)
    assert (await run(rt, new_session(engine), {"action": "continue"}))[-1]["type"] == "done"
    await first.aclose()


async def test_resume_without_pending_is_bad(rt, sid):
    with pytest.raises(BadResume):
        await run(rt, sid, {"resume": {"approved": True}})


async def test_interrupt_event_and_resume(engine, clear_memo, monkeypatch):  # noqa: F811
    monkeypatch.setitem(RESUME_SHAPES, "pick", lambda v: isinstance(v, dict) and isinstance(v.get("choice"), str))
    rt = make_runtime(engine, {"teacher": _asking_teacher()})
    sid = new_session(engine)
    events = await run(rt, sid, {"action": "continue"})
    assert [e["type"] for e in events] == ["node", "node", "interrupt", "done"]
    assert interrupts(events) == [{"kind": "pick", "summary": "pick one"}]
    assert await rt.pending(sid) == {"kind": "pick", "summary": "pick one"}
    assert (await rt.state(sid))["pending"] == {"kind": "pick", "summary": "pick one"}

    with pytest.raises(BadResume):  # the wrong shape: refused before the graph runs
        await run(rt, sid, {"resume": {"choice": 3}})
    assert await rt.pending(sid) == {"kind": "pick", "summary": "pick one"}

    events = await run(rt, sid, {"resume": {"choice": "skip"}})
    assert nodes(events) == ["teacher"] and replies(events) == ["you said skip"]
    assert await rt.pending(sid) is None
    assert (await rt.state(sid))["messages"] == [{"role": "assistant", "content": "answered"}]


async def test_resume_of_a_kind_without_validator_is_bad(engine, clear_memo):  # noqa: F811
    rt = make_runtime(engine, {"teacher": _asking_teacher(kind="unregistered")})
    sid = new_session(engine)
    await run(rt, sid, {"action": "continue"})
    with pytest.raises(BadResume):
        await run(rt, sid, {"resume": {"choice": "x"}})
    assert await rt.pending(sid) == {"kind": "unregistered", "summary": "pick one"}


async def _asked(engine):
    """A session whose teacher is waiting for an answer, after one user message."""
    rt = make_runtime(engine, {"teacher": _asking_teacher()})
    sid = new_session(engine)
    with fake_llm.script([FakeReply(json={"route": "question"})]):
        await run(rt, sid, {"text": "first message"})
    assert await rt.pending(sid) is not None
    return rt, sid


async def test_action_abandons_a_pending_interrupt(engine, clear_memo):  # noqa: F811
    """R5: an action turn while an interrupt is pending starts a fresh run; earlier state is kept."""
    rt, sid = await _asked(engine)
    events = await run(rt, sid, {"action": "plan"})
    assert nodes(events) == ["coordinator", "planner"] and interrupts(events) == []
    assert await rt.pending(sid) is None
    assert (await rt.state(sid))["messages"] == [{"role": "user", "content": "first message"}]
    with pytest.raises(BadResume):
        await run(rt, sid, {"resume": {"choice": "x"}})


async def test_text_abandons_a_pending_interrupt(engine, clear_memo):  # noqa: F811
    rt, sid = await _asked(engine)
    with fake_llm.script([FakeReply(json={"route": "recap"})]):
        events = await run(rt, sid, {"text": "recap please"})
    assert nodes(events) == ["coordinator", "session_start"] and interrupts(events) == []
    assert await rt.pending(sid) is None
    assert [m["content"] for m in (await rt.state(sid))["messages"]] == ["first message", "recap please"]


async def test_outage_is_visible_and_next_turn_works(engine, rt, sid, monkeypatch, instant_retries):
    calls = []

    def down(*args, **kwargs):
        calls.append(1)
        raise LLMUnavailable("down")

    monkeypatch.setattr(coordinator, "haiku_json_memo", down)
    events = await run(rt, sid, {"text": "hi"})
    assert len(calls) == 3
    assert events[-2] == {"type": "error", "message": OUTAGE_MESSAGE}
    assert events[-1]["type"] == "done"
    assert any(s["name"] == "node:coordinator" and s["status"] == "error" for s in events[-1]["trace"]["spans"])
    events = await run(rt, sid, {"action": "continue"})
    assert nodes(events) == ["coordinator", "teacher"] and events[-1]["type"] == "done"


async def test_outage_while_an_interrupt_is_pending_still_abandons_it(engine, clear_memo, monkeypatch,  # noqa: F811
                                                                      instant_retries):
    rt = make_runtime(engine, {"teacher": _asking_teacher()})
    sid = new_session(engine)
    await run(rt, sid, {"action": "continue"})

    def down(*args, **kwargs):
        raise LLMUnavailable("down")

    monkeypatch.setattr(coordinator, "haiku_json_memo", down)
    assert (await run(rt, sid, {"text": "hello"}))[-2]["type"] == "error"
    assert await rt.pending(sid) is None


async def test_budget_breach_is_an_error_then_done(engine, clear_memo):  # noqa: F811
    exc = BudgetExceeded("token", "70000", "60000")
    rt = make_runtime(engine, {"teacher": _raising_teacher(exc)})
    sid = new_session(engine)
    events = await run(rt, sid, {"action": "continue"})
    assert events[-2] == {"type": "error", "message": exc.message}
    assert events[-1]["type"] == "done"
    assert nodes(await run(rt, sid, {"action": "plan"})) == ["coordinator", "planner"]  # the next turn works


async def test_other_errors_propagate(engine, clear_memo):  # noqa: F811
    rt = make_runtime(engine, {"teacher": _raising_teacher(RuntimeError("bug"))})
    with pytest.raises(RuntimeError, match="bug"):
        await run(rt, new_session(engine), {"action": "continue"})


async def test_secret_in_user_text_never_reaches_state(rt, sid):
    with fake_llm.script([FakeReply(json={"route": "question"})]):
        events = await run(rt, sid, {"text": "my key is sk-ant-abcdefghijklmnop"})
    assert "sk-ant-abcdefghijklmnop" not in json.dumps(await rt.state(sid))
    assert "sk-ant-abcdefghijklmnop" not in json.dumps(fake_llm.requests, default=str)
    assert "sk-ant-abcdefghijklmnop" not in json.dumps(events)


async def test_done_carries_the_trace_with_node_spans(rt, sid):
    events = await run(rt, sid, {"action": "quiz"})
    done = events[-1]
    assert done["type"] == "done" and done["trace"]["name"] == "tutor_turn"
    spans = [(s["name"], s["kind"]) for s in done["trace"]["spans"]]
    assert spans == [("node:coordinator", "node"), ("node:assessor", "node")]


async def test_turn_cost_is_added_to_the_session(engine, rt, sid, monkeypatch):
    monkeypatch.setattr(config, "PRICE_TABLE_JSON", json.dumps({"fake": {"output": 1000}}))  # 5 tokens -> $0.005
    with fake_llm.script([FakeReply(json={"route": "question"})]):
        events = await run(rt, sid, {"text": "what costs money?"})
    assert events[-1]["trace"]["cost_usd"] == 0.005
    assert _cost(engine, sid) == Decimal("0.005")
    await run(rt, sid, {"action": "continue"})  # no model call: nothing added
    assert _cost(engine, sid) == Decimal("0.005")


async def test_trace_is_exported_when_a_provider_is_set(engine, clear_memo, monkeypatch):  # noqa: F811
    exported = []
    monkeypatch.setattr(runtime_mod, "export_trace", lambda trace, provider: exported.append((trace.name, provider)))
    provider = object()
    rt = make_runtime(engine, tracer_provider=provider)
    await run(rt, new_session(engine), {"action": "continue"})
    assert exported == [("tutor_turn", provider)]


async def test_postgres_checkpointer_keeps_the_session(engine, db_url, clear_memo, monkeypatch):  # noqa: F811
    monkeypatch.setitem(RESUME_SHAPES, "pick", lambda v: isinstance(v, dict) and isinstance(v.get("choice"), str))
    sid = new_session(engine)
    async with open_saver(db_url) as saver:
        rt = make_runtime(engine, {"teacher": _asking_teacher()}, saver=saver)
        with fake_llm.script([FakeReply(json={"route": "question"})]):
            await run(rt, sid, {"text": "what is write-back?"})
    async with open_saver(db_url) as saver:  # a restart: a new saver and a new graph
        rt = make_runtime(engine, {"teacher": _asking_teacher()}, saver=saver)
        assert await rt.pending(sid) == {"kind": "pick", "summary": "pick one"}
        events = await run(rt, sid, {"resume": {"choice": "go"}})
        assert replies(events) == ["you said go"]
        assert [m["role"] for m in (await rt.state(sid))["messages"]] == ["user", "assistant"]
        await delete_thread(saver, sid)
