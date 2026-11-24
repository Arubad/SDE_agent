import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.core import fake_llm
from backend.core.fake_llm import FakeReply
from backend.tutor.db import session_scope
from backend.tutor.models import TutorSession
from backend.tutor.repo import create_roadmap
from backend.tutor.tests.tutor_helpers import (
    SPEC,
    Enqueued,
    add_ended_session,
    clear_memo,  # noqa: F401 (fixture)
    interrupts,
    make_runtime,
    new_session,
    nodes,
    replies,
    run,
)
from backend.tutor.tutor import runtime as runtime_mod
from backend.tutor.tutor.graph import single
from backend.tutor.tutor.runtime import BadResume, Busy, SessionEnded
from backend.tutor.tutor.session_start import NO_SESSION_TEXT, NO_SUMMARY_TEXT
from backend.tutor.tutor.sessions import NotReady, create_session, end_session, last_ended, list_sessions

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _roadmap(engine, *, status="ready", spec=SPEC) -> str:
    slug = f"rm-{uuid.uuid4().hex[:8]}"
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=slug, title="T", source_text="x")
        rm.status, rm.spec = status, spec
    return slug


def _row(engine, sid) -> TutorSession:
    with session_scope(engine) as s:
        return s.get(TutorSession, uuid.UUID(str(sid)))


# --- lifecycle ---------------------------------------------------------------------------------------------

def test_create_refuses_unready(engine):
    with pytest.raises(NotReady, match="is 'parsed'; the tutor opens once ingestion is ready"):
        create_session(engine, _roadmap(engine, status="parsed"), "tutor", enqueue=Enqueued())


def test_create_refuses_a_ready_roadmap_without_skills(engine):
    empty = {**SPEC, "stages": [{**SPEC["stages"][0], "skills": []}]}
    with pytest.raises(NotReady, match="is 'ready'"):
        create_session(engine, _roadmap(engine, spec=empty), "tutor", enqueue=Enqueued())


def test_create_unknown_roadmap(engine):
    with pytest.raises(LookupError, match="No roadmap 'nope'"):
        create_session(engine, "nope", "tutor", enqueue=Enqueued())


def test_ready_with_gaps_is_fine(engine):
    assert create_session(engine, _roadmap(engine, status="ready_with_gaps"), "tutor",
                          enqueue=Enqueued()).status == "active"


def test_new_session_ends_previous(engine):
    enq, slug = Enqueued(), _roadmap(engine)
    a = create_session(engine, slug, "tutor", enqueue=enq)
    b = create_session(engine, slug, "qa", enqueue=enq)
    assert _row(engine, a.id).status == "ended" and _row(engine, a.id).ended_at is not None
    assert _row(engine, b.id).status == "active" and b.mode == "qa"
    assert [(t, p, k) for t, p, k, _ in enq] == [("curate_session", {"session_id": str(a.id)}, (str(a.id),))]
    assert enq[0][3] == a.roadmap_id


def test_end_session_is_idempotent(engine):
    enq = Enqueued()
    a = create_session(engine, _roadmap(engine), "tutor", enqueue=enq)
    first = end_session(engine, a.id, enq)
    again = end_session(engine, str(a.id), enq)
    assert again.ended_at == first.ended_at and len(enq) == 1
    with pytest.raises(LookupError):
        end_session(engine, uuid.uuid4(), enq)
    with pytest.raises(LookupError):
        end_session(engine, "not-a-uuid", enq)


def test_list_sessions_newest_first_and_last_ended(engine):
    enq, slug = Enqueued(), _roadmap(engine)
    a = create_session(engine, slug, "tutor", enqueue=enq)
    b = create_session(engine, slug, "tutor", enqueue=enq)
    listed = list_sessions(engine, slug)
    assert [r["id"] for r in listed] == [str(b.id), str(a.id)]
    assert listed[0] == {"id": str(b.id), "title": None, "started_at": listed[0]["started_at"], "ended_at": None,
                         "summary": None, "status": "active"}
    assert datetime.fromisoformat(listed[1]["ended_at"]) and listed[1]["status"] == "ended"
    assert last_ended(engine, a.roadmap_id).id == a.id
    with pytest.raises(LookupError):
        list_sessions(engine, "nope")


# --- the graph ---------------------------------------------------------------------------------------------

@pytest.fixture
def rt(engine, clear_memo):  # noqa: F811
    return make_runtime(engine)


async def test_first_session_goes_to_planner(engine, rt):
    sid = new_session(engine)
    events = await run(rt, sid, {"action": "start"})
    assert nodes(events) == ["coordinator", "session_start_prepare", "planner"] and not interrupts(events)


async def test_reload_resending_start_ends_quietly(engine, rt):
    sid = new_session(engine)
    add_ended_session(engine, sid, summary="s")
    await run(rt, sid, {"action": "start"})
    events = await run(rt, sid, {"action": "start"})
    assert [e["type"] for e in events] == ["node", "node", "done"] and await rt.pending(sid) is None


@pytest.fixture
def ended(engine):
    """A session with an ended 'last' and two earlier ended sessions; returns (sid, last_id, [earlier ids])."""
    sid = new_session(engine)
    now = datetime.now(timezone.utc)
    e1 = add_ended_session(engine, sid, summary="old one", title="Old", ended_at=now - timedelta(days=5))
    e2 = add_ended_session(engine, sid, summary=None, ended_at=now - timedelta(days=3))
    last = add_ended_session(engine, sid, summary="Day 1: pods; struggled with probes", title="Pods",
                             ended_at=now - timedelta(days=1))
    return sid, last, [e2, e1]


async def test_card_then_recap_without_model(rt, ended):
    sid, last, earlier = ended
    events = await run(rt, sid, {"action": "start"})
    [card] = interrupts(events)
    day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    assert card == {"kind": "session_start", "trigger": "start", "options": ["recap_last", "recap_earlier", "warm_up",
                                                                              "skip"],
                    "last": {"id": last, "title": "Pods", "date": day, "summary": "Day 1: pods; struggled with probes"},
                    "earlier": [{"id": earlier[0], "title": None, "date": card["earlier"][0]["date"]},
                                {"id": earlier[1], "title": "Old", "date": card["earlier"][1]["date"]}]}
    assert (await rt.state(sid))["pending"] == card
    events = await run(rt, sid, {"resume": {"choice": "recap_last"}})
    assert replies(events) == ["Day 1: pods; struggled with probes"] and fake_llm.requests == []
    assert nodes(events) == ["session_start_ask"] and await rt.pending(sid) is None
    assert (await rt.graph.aget_state({"configurable": {"thread_id": sid}})).values["proposal"] is None
    assert (await rt.state(sid))["messages"][-1] == {"role": "assistant", "content": "Day 1: pods; struggled with probes"}


async def test_recap_earlier_and_missing_summary(rt, ended):
    sid, _, (no_summary, old) = ended
    await run(rt, sid, {"action": "start"})
    events = await run(rt, sid, {"resume": {"choice": "recap_earlier", "session_id": old}})
    assert replies(events) == ["old one"]
    await run(rt, sid, {"action": "recap"})
    events = await run(rt, sid, {"resume": {"choice": "recap_earlier", "session_id": no_summary}})
    assert replies(events) == [NO_SUMMARY_TEXT]


async def test_last_without_summary_says_not_ready(engine, rt):
    sid = new_session(engine)
    add_ended_session(engine, sid, summary=None)
    await run(rt, sid, {"action": "start"})
    assert replies(await run(rt, sid, {"resume": {"choice": "recap_last"}})) == [NO_SUMMARY_TEXT]


async def test_warm_up_goes_to_assessor_with_weak_spot_focus(engine, rt):
    sid = new_session(engine)
    add_ended_session(engine, sid, summary="s")
    await run(rt, sid, {"action": "start"})
    events = await run(rt, sid, {"resume": {"choice": "warm_up"}})
    assert nodes(events) == ["session_start_ask", "assessor"]
    cfg = {"configurable": {"thread_id": sid}}
    assert (await rt.graph.aget_state(cfg)).values["quiz_focus"] == "weak_spots"


async def test_skip_from_start_goes_to_planner(engine, rt):
    sid = new_session(engine)
    add_ended_session(engine, sid, summary="s")
    await run(rt, sid, {"action": "start"})
    assert nodes(await run(rt, sid, {"resume": {"choice": "skip"}})) == ["session_start_ask", "planner"]


async def test_no_earlier_sessions_omits_recap_earlier(engine, rt):
    sid = new_session(engine)
    add_ended_session(engine, sid, summary="s")
    [card] = interrupts(await run(rt, sid, {"action": "start"}))
    assert card["earlier"] == [] and card["options"] == ["recap_last", "warm_up", "skip"]


async def test_earlier_is_capped_at_five(engine, rt):
    sid = new_session(engine)
    for _ in range(8):
        add_ended_session(engine, sid, summary="s")
    [card] = interrupts(await run(rt, sid, {"action": "start"}))
    assert len(card["earlier"]) == 5


@pytest.mark.parametrize("turn", ["button", "text"])
async def test_recap_shows_the_card_and_skip_ends(engine, rt, turn):
    sid = new_session(engine)
    add_ended_session(engine, sid, summary="s")
    await run(rt, sid, {"action": "start"})
    await run(rt, sid, {"resume": {"choice": "recap_last"}})
    if turn == "button":
        events = await run(rt, sid, {"action": "recap"})
    else:
        with fake_llm.script([FakeReply(json={"route": "recap"})]):
            events = await run(rt, sid, {"text": "what did we do last time?"})
    [card] = interrupts(events)
    assert card["trigger"] == "recap" and nodes(events)[:2] == ["coordinator", "session_start_prepare"]
    events = await run(rt, sid, {"resume": {"choice": "skip"}})
    assert nodes(events) == ["session_start_ask"] and replies(events) == []
    assert await rt.pending(sid) is None


async def test_recap_without_any_ended_session(engine, rt):
    sid = new_session(engine)
    await run(rt, sid, {"action": "start"})  # first session: planner
    events = await run(rt, sid, {"action": "recap"})
    assert replies(events) == [NO_SESSION_TEXT] and not interrupts(events)


async def test_recap_does_not_start_the_session(engine, rt):
    sid = new_session(engine)
    add_ended_session(engine, sid, summary="s")
    await run(rt, sid, {"action": "recap"})
    cfg = {"configurable": {"thread_id": sid}}
    assert not (await rt.graph.aget_state(cfg)).values.get("started")
    await run(rt, sid, {"resume": {"choice": "skip"}})
    assert nodes(await run(rt, sid, {"action": "start"}))[1:] == ["session_start_prepare", "session_start_ask"]


@pytest.mark.parametrize("resume", [
    {"choice": "flashcards"}, {}, {"choice": "recap_earlier"}, {"choice": "recap_earlier", "session_id": "nope"},
    {"choice": "recap_earlier", "session_id": None}, "recap_last", {"choice": ["skip"]}])
async def test_bad_resume_leaves_state_unchanged(engine, rt, ended, resume):
    sid, last, _ = ended
    await run(rt, sid, {"action": "start"})
    before = await rt.pending(sid)
    with pytest.raises(BadResume):
        await run(rt, sid, {"resume": resume})
    assert await rt.pending(sid) == before


async def test_earlier_id_must_belong_to_the_card(engine, rt, ended):
    sid, last, _ = ended
    await run(rt, sid, {"action": "start"})
    with pytest.raises(BadResume):  # `last` is on the card, but not as an earlier session
        await run(rt, sid, {"resume": {"choice": "recap_earlier", "session_id": last}})
    other = new_session(engine)
    foreign = add_ended_session(engine, other, summary="elsewhere")
    with pytest.raises(BadResume):
        await run(rt, sid, {"resume": {"choice": "recap_earlier", "session_id": foreign}})


async def test_a_one_argument_validator_still_works(engine, clear_memo, monkeypatch):  # noqa: F811
    from backend.tutor.tutor.runtime import RESUME_SHAPES, _accepts

    assert _accepts(lambda v: v == 1, 1, {"x": 1}) and not _accepts(lambda v: v == 1, 2, {})
    assert _accepts(lambda v, p: v == p, 3, 3) and not _accepts(lambda v, p: v == p, 3, 4)
    assert RESUME_SHAPES["session_start"]


async def test_qa_mode_start_never_reaches_session_start(engine, rt):
    sid = new_session(engine, mode="qa")
    add_ended_session(engine, sid, summary="s")
    assert nodes(await run(rt, sid, {"action": "start"})) == ["coordinator"]


# --- coordination with running turns and the one-active-session rule -----------------------------------------

def _slow_teacher(started, release):
    def add(builder, deps):
        async def teacher(state):
            started.set()
            await release.wait()

        return single(builder, "teacher", teacher)

    return add


async def _running_turn(engine, enq):
    """A runtime with a turn blocked in the teacher; returns (rt, sid, release event, the turn's task)."""
    started, release = asyncio.Event(), asyncio.Event()
    rt = make_runtime(engine, {"teacher": _slow_teacher(started, release)}, enqueue=enq)
    sid = str((create_session(engine, _roadmap(engine), "tutor", enqueue=enq)).id)
    task = asyncio.create_task(run(rt, sid, {"action": "continue"}))
    await started.wait()
    return rt, sid, release, task


async def test_ending_waits_for_the_running_turn(engine, clear_memo):  # noqa: F811
    enq = Enqueued()
    rt, sid, release, turn = await _running_turn(engine, enq)
    ending = asyncio.create_task(rt.end_session(sid))
    await asyncio.sleep(0.2)
    assert not ending.done() and enq == [] and _row(engine, sid).status == "active"
    with pytest.raises(Busy):  # a new turn does not squeeze in
        await run(rt, sid, {"action": "continue"})
    release.set()
    events, ended = await turn, await ending
    assert events[-1]["type"] == "done" and ended.status == "ended"
    assert [(t, k) for t, _, k, _ in enq] == [("curate_session", (sid,))]
    with pytest.raises(SessionEnded):
        await run(rt, sid, {"action": "continue"})


async def test_ending_gives_up_with_busy(engine, clear_memo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runtime_mod, "END_WAIT_S", 0.05)
    enq = Enqueued()
    rt, sid, release, turn = await _running_turn(engine, enq)
    with pytest.raises(Busy):
        await rt.end_session(sid)
    assert enq == [] and _row(engine, sid).status == "active"
    release.set()
    await turn
    assert (await rt.end_session(sid)).status == "ended" and len(enq) == 1


async def test_runtime_create_waits_for_the_old_turn(engine, clear_memo):  # noqa: F811
    enq = Enqueued()
    rt, sid, release, turn = await _running_turn(engine, enq)
    slug = list_sessions_slug(engine, sid)
    creating = asyncio.create_task(rt.create_session(slug, "tutor"))
    await asyncio.sleep(0.2)
    assert not creating.done() and _row(engine, sid).status == "active"
    release.set()
    await turn
    new = await creating
    assert new.status == "active" and _row(engine, sid).status == "ended" and len(enq) == 1


def list_sessions_slug(engine, sid):
    with engine.connect() as c:
        return c.execute(text("SELECT r.slug FROM roadmaps r JOIN tutor_sessions s ON s.roadmap_id = r.id "
                              "WHERE s.id = :i"), {"i": sid}).scalar()


async def test_turn_on_a_session_ended_meanwhile_is_refused(engine, clear_memo):  # noqa: F811
    enq = Enqueued()
    rt = make_runtime(engine, enqueue=enq)
    sid = str(create_session(engine, _roadmap(engine), "tutor", enqueue=enq).id)
    await rt.end_session(sid)
    with pytest.raises(SessionEnded):
        await run(rt, sid, {"action": "continue"})


def test_concurrent_creates_leave_one_active_session(engine):
    enq, slug = Enqueued(), _roadmap(engine)
    create_session(engine, slug, "tutor", enqueue=enq)
    with ThreadPoolExecutor(6) as pool:
        made = list(pool.map(lambda _: create_session(engine, slug, "tutor", enqueue=enq), range(6)))
    statuses = [r["status"] for r in list_sessions(engine, slug)]
    assert statuses.count("active") == 1 and len(statuses) == 7 and len({m.id for m in made}) == 6
    assert len(enq) == 6  # every session but the last was ended exactly once


def test_unique_index_refuses_a_second_active_row(engine):
    slug = _roadmap(engine)
    first = create_session(engine, slug, "tutor", enqueue=Enqueued())
    with pytest.raises(IntegrityError), session_scope(engine) as s:
        s.add(TutorSession(roadmap_id=first.roadmap_id, mode="tutor", status="active"))
