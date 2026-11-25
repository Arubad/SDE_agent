"""The memory curator and retention: the summary, consent-gated facts, weak spots, the raw thread, the sweep."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from backend.core import fake_llm
from backend.core.fake_llm import FakeReply
from backend.core.llm import LLMUnavailable
from backend.core.trace import start_trace
from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.jobs.queue import PermanentError, RetryableError
from backend.tutor.jobs.worker import JobContext
from backend.tutor.learner import store
from backend.tutor.models import LearnerFact, Job, TutorSession
from backend.tutor.settings import Settings
from backend.tutor.tests.tutor_helpers import Enqueued, clear_memo, new_session  # noqa: F401 (fixture)
from backend.tutor.tutor import curator, retention
from backend.tutor.tutor.checkpoint import open_saver
from backend.tutor.tutor.curator import CURATE_SCHEMA, CURATE_SCHEMA_NO_FACTS, curate_session
from backend.tutor.untrusted import wrap
from backend.tutor.tutor.state import TutorState

pytestmark = pytest.mark.anyio
MSGS = [{"role": "user", "content": "I prefer videos over text"}, {"role": "assistant", "content": "Noted."}]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM learner_facts"))
        s.execute(text("UPDATE learner_settings SET consent_personal = NULL, consent_raw_messages = NULL, "
                       "retention_days = 90"))


def _consent(engine, personal=None, raw=None):
    with session_scope(engine) as s:
        s.execute(text("UPDATE learner_settings SET consent_personal = :p, consent_raw_messages = :r WHERE id = 1"),
                  {"p": personal, "r": raw})


def _ctx(engine, db_url):
    return JobContext(engine=engine, settings=Settings(database_url=db_url), embedder=HashEmbedder())


def _job(sid):
    return Job(id=1, type="curate_session", key=f"k{sid}", attempts=1, payload={"session_id": sid})


def _ended(engine, **kw) -> str:
    sid = new_session(engine, status="ended")
    with session_scope(engine) as s:
        row = s.get(TutorSession, uuid.UUID(sid))
        row.ended_at = kw.pop("ended_at", datetime.now(timezone.utc))
        for k, v in kw.items():
            setattr(row, k, v)
    return sid


def _seed(db_url, sid, messages=MSGS, mastery_log=()):
    """Write a checkpoint for the session's thread by running a one-node graph on the real Postgres saver."""
    async def go():
        async with open_saver(db_url) as saver:
            g = StateGraph(TutorState)
            g.add_node("n", lambda s: {"messages": list(messages), "mastery_log": list(mastery_log)})
            g.add_edge(START, "n")
            g.add_edge("n", END)
            await g.compile(checkpointer=saver).ainvoke({}, {"configurable": {"thread_id": sid}})

    asyncio.run(go())


def _has_thread(db_url, sid) -> bool:
    async def go():
        async with open_saver(db_url) as saver:
            return await saver.aget_tuple({"configurable": {"thread_id": sid}}) is not None

    return asyncio.run(go())


def _row(engine, sid):
    with session_scope(engine) as s:
        row = s.get(TutorSession, uuid.UUID(sid))
        s.expunge(row)
        return row


def _slug(engine, sid):
    with session_scope(engine) as s:
        return s.execute(text("SELECT r.slug FROM roadmaps r JOIN tutor_sessions t ON t.roadmap_id = r.id "
                              "WHERE t.id = :i"), {"i": sid}).scalar()


def _facts(engine):
    with session_scope(engine) as s:
        rows = s.query(LearnerFact).order_by(LearnerFact.created_at).all()
        s.expunge_all()
        return rows


def _reply(title="Pods", summary="Day 1: pods", facts=(), resolved=()):
    return FakeReply(json={"title": title, "summary": summary, "facts": list(facts),
                           "resolved_weak_spots": list(resolved)})


def test_schema_is_strict_and_titled():
    assert CURATE_SCHEMA["title"] == "SessionCurate"
    assert CURATE_SCHEMA_NO_FACTS["title"] == "SessionCurateNoFacts"
    assert "facts" not in CURATE_SCHEMA_NO_FACTS["properties"]
    for node in (CURATE_SCHEMA, CURATE_SCHEMA["properties"]["facts"]["items"],
                 CURATE_SCHEMA["properties"]["resolved_weak_spots"]["items"], CURATE_SCHEMA_NO_FACTS):
        assert node["additionalProperties"] is False and node["required"] == list(node["properties"])


def _request_schema(req: dict) -> dict:
    return req["output_config"]["format"]["schema"]


def _memo_rows(engine) -> int:
    with session_scope(engine) as s:
        return s.execute(text("SELECT count(*) FROM llm_memo")).scalar()


@pytest.mark.parametrize("personal", [None, False])
def test_without_consent_asks_for_no_facts_and_memoises_nothing(engine, db_url, clear_memo, personal):
    _consent(engine, personal, False)
    sid = _ended(engine)
    _seed(db_url, sid)
    with fake_llm.script([FakeReply(json={"title": "Pods", "summary": "Day 1", "resolved_weak_spots": []})]):
        curate_session(_ctx(engine, db_url), _job(sid))
    req = fake_llm.requests[-1]
    schema = _request_schema(req)
    assert schema["title"] == "SessionCurateNoFacts" and "facts" not in schema["properties"]
    assert "facts" not in str(req["system"]).lower()
    assert _memo_rows(engine) == 0 and _row(engine, sid).summary == "Day 1"


def test_with_consent_asks_for_facts_and_memoises_nothing(engine, db_url, clear_memo):
    _consent(engine, True, False)
    sid = _ended(engine)
    _seed(db_url, sid)
    with fake_llm.script([_reply(facts=[{"kind": "fact", "text": "likes go", "replaces": None}])]):
        curate_session(_ctx(engine, db_url), _job(sid))
    assert _request_schema(fake_llm.requests[-1])["title"] == "SessionCurate"
    assert _memo_rows(engine) == 0 and len(_facts(engine)) == 1


def test_an_echoed_fenced_concept_clears_the_weak_spot(engine, db_url, clear_memo):
    sid = _ended(engine)
    slug = _slug(engine, sid)
    store.set_mastery(engine, slug, "cache-basics", 0.6, ["a<b ordering", "keys"])
    _seed(db_url, sid, mastery_log=[{"skill_id": "cache-basics", "before": 0.4, "after": 0.6}])
    resolved = [{"skill_id": "cache-basics", "concept": wrap("a<b ordering")}]
    with fake_llm.script([_reply(resolved=resolved)]):
        curate_session(_ctx(engine, db_url), _job(sid))
    assert {p["skill_id"]: p for p in store.get_progress(engine, slug)}["cache-basics"]["weak_spots"] == ["keys"]


def test_without_consent_keeps_summary_drops_raw_and_facts(engine, db_url, clear_memo):
    trace = start_trace("curator-test")  # the cost comes from this trace's llm spans (0.0 for the fake model)
    sid = _ended(engine)
    _seed(db_url, sid)
    reply = _reply(facts=[{"kind": "preference", "text": "prefers videos", "replaces": None}])
    with fake_llm.script([reply]):
        out = curate_session(_ctx(engine, db_url), _job(sid))
    row = _row(engine, sid)
    assert (row.title, row.summary, row.raw_kept) == ("Pods", "Day 1: pods", False) and row.curated_at
    assert _facts(engine) == [] and out["facts"] == 0 and out["cost_usd"] == 0.0
    assert [s.name for s in trace.spans if s.kind == "llm"] and trace.llm_calls == 1
    assert not _has_thread(db_url, sid)


def test_with_consent_merges_replaces_and_keeps_thread(engine, db_url, clear_memo):
    _consent(engine, True, True)
    sid = _ended(engine)
    slug = _slug(engine, sid)
    _seed(db_url, sid)
    old = store.remember(engine, HashEmbedder(), "prefers videos", "preference", slug)["id"]
    reply = _reply(facts=[
        {"kind": "preference", "text": "now prefers reading", "replaces": old},
        {"kind": "goal", "text": "wants a platform job", "replaces": str(uuid.uuid4())},
        {"kind": "fact", "text": "x" * 2000, "replaces": None}])
    with fake_llm.script([reply]):
        out = curate_session(_ctx(engine, db_url), _job(sid))
    by_text = {f.text: f for f in _facts(engine)}
    assert set(by_text) == {"now prefers reading", "wants a platform job"}
    assert str(by_text["now prefers reading"].id) == old and by_text["now prefers reading"].provenance["history"]
    assert by_text["wants a platform job"].provenance == {"source": "curator", "session_id": sid}
    assert (out["facts"], out["updated"], out["skipped"]) == (1, 1, 1)
    assert _row(engine, sid).raw_kept is True and _has_thread(db_url, sid)
    # the existing fact is offered to the model by id, and the transcript is fenced
    prompt = fake_llm.requests[-1]["messages"][0]["content"]
    assert f"{old}: " in prompt and "<untrusted_web_content>" in prompt


def test_facts_are_not_offered_or_written_without_consent(engine, db_url, clear_memo):
    _consent(engine, False, False)
    sid = _ended(engine)
    _seed(db_url, sid)
    with fake_llm.script([_reply(facts=[{"kind": "fact", "text": "secret", "replaces": None}])]):
        curate_session(_ctx(engine, db_url), _job(sid))
    assert "Remembered facts" not in fake_llm.requests[-1]["messages"][0]["content"] and _facts(engine) == []


def test_idempotent_and_thread_decision_repeats(engine, db_url, clear_memo):
    sid = _ended(engine, curated_at=datetime.now(timezone.utc), summary="done")
    _seed(db_url, sid)  # a crash after curating, before the delete
    n = len(fake_llm.requests)
    assert curate_session(_ctx(engine, db_url), _job(sid)) == {"facts": 0, "skipped": "already_curated"}
    assert len(fake_llm.requests) == n and not _has_thread(db_url, sid) and _row(engine, sid).summary == "done"
    assert curate_session(_ctx(engine, db_url), _job(sid))["skipped"] == "already_curated"  # safe to repeat


def test_resolved_weak_spots_only_where_mastery_rose(engine, db_url, clear_memo):
    sid = _ended(engine)
    slug = _slug(engine, sid)
    store.set_mastery(engine, slug, "cache-basics", 0.6, ["ttl", "keys"])
    store.set_mastery(engine, slug, "eviction", 0.4, ["lru", "lfu"])
    _seed(db_url, sid, mastery_log=[{"skill_id": "cache-basics", "before": 0.4, "after": 0.6},
                                    {"skill_id": "eviction", "before": 0.5, "after": 0.4}])
    resolved = [{"skill_id": "cache-basics", "concept": "TTL"}, {"skill_id": "eviction", "concept": "lru"},
                {"skill_id": "nope", "concept": "x"}, {"skill_id": "cache-basics", "concept": "unknown"}]
    with fake_llm.script([_reply(resolved=resolved)]):
        curate_session(_ctx(engine, db_url), _job(sid))
    prog = {p["skill_id"]: p for p in store.get_progress(engine, slug)}
    assert prog["cache-basics"]["weak_spots"] == ["keys"] and prog["cache-basics"]["mastery"] == 0.6
    assert prog["eviction"]["weak_spots"] == ["lru", "lfu"]


def test_title_and_summary_are_redacted_and_capped(engine, db_url, clear_memo):
    sid = _ended(engine)
    _seed(db_url, sid)
    with fake_llm.script([_reply(title="T" * 500, summary="mail me at jane.doe@example.com " + "s" * 5000)]):
        curate_session(_ctx(engine, db_url), _job(sid))
    row = _row(engine, sid)
    assert len(row.title) == 120 and len(row.summary) <= 2000 and "jane.doe@example.com" not in row.summary


def test_transcript_cap_keeps_the_newest(engine, db_url, clear_memo):
    sid = _ended(engine)
    msgs = [{"role": "user", "content": f"m{i} " + "w" * 1000} for i in range(60)]
    _seed(db_url, sid, messages=msgs)
    with fake_llm.script([_reply()]):
        curate_session(_ctx(engine, db_url), _job(sid))
    prompt = fake_llm.requests[-1]["messages"][0]["content"]
    assert "m59 " in prompt and "m0 " not in prompt and len(prompt) < curator.MAX_TRANSCRIPT_CHARS + 2000


def test_empty_session_makes_no_model_call(engine, db_url, clear_memo):
    sid = _ended(engine)  # no checkpoint at all
    n = len(fake_llm.requests)
    out = curate_session(_ctx(engine, db_url), _job(sid))
    row = _row(engine, sid)
    assert row.summary == "No conversation in this session." and row.title is None and row.curated_at
    assert len(fake_llm.requests) == n and out["facts"] == 0 and row.raw_kept is False


def test_errors(engine, db_url, clear_memo, monkeypatch):
    ctx = _ctx(engine, db_url)
    assert curate_session(ctx, _job(str(uuid.uuid4()))) == {"skipped": "no_session"}
    with pytest.raises(PermanentError, match="still active"):
        curate_session(ctx, _job(new_session(engine)))
    sid = _ended(engine)
    _seed(db_url, sid)

    def down(*a, **k):
        raise LLMUnavailable("down")

    monkeypatch.setattr(curator, "haiku_json", down)
    with pytest.raises(RetryableError):
        curate_session(ctx, _job(sid))
    assert _row(engine, sid).curated_at is None and _has_thread(db_url, sid)  # nothing lost; the retry redoes it


def test_embedder_falls_back_to_the_index_and_fails_without_one(engine, db_url, clear_memo):
    _consent(engine, True, True)
    facts = [{"kind": "fact", "text": "likes go", "replaces": None}]
    sid = _ended(engine)
    _seed(db_url, sid)
    bare = JobContext(engine=engine, settings=Settings(database_url=db_url))
    with fake_llm.script([_reply(facts=facts)]), pytest.raises(PermanentError, match="no embedder"):
        curate_session(bare, _job(sid))
    bare.index = type("Idx", (), {"embedder": HashEmbedder()})()
    with fake_llm.script([_reply(facts=facts)]):
        assert curate_session(bare, _job(sid))["facts"] == 1


async def test_retention_sweep(engine, db_url):
    now = datetime.now(timezone.utc)
    old = _ended(engine, raw_kept=True, ended_at=now - timedelta(days=91))
    new = _ended(engine, raw_kept=True, ended_at=now - timedelta(days=5))
    gone = _ended(engine, raw_kept=False, ended_at=now - timedelta(days=200))
    for sid in (old, new, gone):
        await asyncio.to_thread(_seed, db_url, sid)
    async with open_saver(db_url) as saver:
        assert await retention.sweep(engine, saver) == 1
        assert await saver.aget_tuple({"configurable": {"thread_id": old}}) is None
        assert await saver.aget_tuple({"configurable": {"thread_id": new}}) is not None
        assert await saver.aget_tuple({"configurable": {"thread_id": gone}}) is not None  # not ours to touch
    assert not _row(engine, old).raw_kept and _row(engine, new).raw_kept
    with session_scope(engine) as s:
        s.execute(text("UPDATE learner_settings SET retention_days = 1"))
    async with open_saver(db_url) as saver:
        assert await retention.sweep(engine, saver) == 1  # the 5-day-old one now qualifies
    assert not _row(engine, new).raw_kept


async def test_reenqueue_uncurated(engine):
    pending = _ended(engine)
    _ended(engine, curated_at=datetime.now(timezone.utc))
    active = new_session(engine)
    enq = Enqueued()
    n = await retention.reenqueue_uncurated(engine, enq)
    ids = {p["session_id"] for _, p, _, _ in enq}
    assert pending in ids and active not in ids and n == len(enq)
    assert all(t == "curate_session" and key == (p["session_id"],) for t, p, key, _ in enq)


def test_lifespan_requeues_uncurated_sessions_without_workers(engine, db_url, tmp_path):
    import time

    from backend.tutor.api import create_app
    from backend.tutor.tests.clients import local_client

    sid = _ended(engine)
    app = create_app(Settings(web_dist=tmp_path / "no-dist", database_url=db_url, ingest_workers=0), engine=engine)
    with local_client(app):
        for _ in range(50):  # the maintenance task runs in the background
            with session_scope(engine) as s:
                n = s.execute(text("SELECT count(*) FROM jobs WHERE type = 'curate_session' "
                                   "AND payload->>'session_id' = :i"), {"i": sid}).scalar()
            if n:
                break
            time.sleep(0.1)
    assert n == 1


async def test_sweep_revives_a_dead_curate_job_which_then_curates(engine, db_url, clear_memo):
    from backend.tutor.jobs import queue

    sid = _ended(engine)
    await asyncio.to_thread(_seed, db_url, sid)
    ctx = _ctx(engine, db_url)
    rid = _row(engine, sid).roadmap_id
    await retention.reenqueue_uncurated(engine, ctx.enqueue)
    key = queue.job_key("curate_session", rid, sid)
    with session_scope(engine) as s:
        s.execute(text("UPDATE jobs SET status='dead', attempts=5, error='down' WHERE key = :k"), {"k": key})
    await retention.reenqueue_uncurated(engine, ctx.enqueue)
    with session_scope(engine) as s:
        status, attempts, err = s.execute(text("SELECT status, attempts, error FROM jobs WHERE key = :k"),
                                          {"k": key}).one()
    assert (status, attempts, err) == ("queued", 0, None)
    assert queue.revive(engine, key) is False  # only a dead job is revived
    with fake_llm.script([_reply()]):
        assert await asyncio.to_thread(curate_session, ctx, _job(sid)) is not None
    assert _row(engine, sid).summary == "Day 1: pods"


async def test_stale_uncurated_thread_dropped_only_without_raw_consent(engine, db_url, clear_memo):
    old = datetime.now(timezone.utc) - timedelta(seconds=retention.UNCURATED_GRACE_S + 60)
    stale = _ended(engine, ended_at=old)
    fresh = _ended(engine)
    curated = _ended(engine, ended_at=old, curated_at=datetime.now(timezone.utc), raw_kept=True)
    for sid in (stale, fresh, curated):
        await asyncio.to_thread(_seed, db_url, sid)
    async with open_saver(db_url) as saver:
        _consent(engine, True, True)
        assert await retention.drop_stale_threads(engine, saver) == 0
        assert await _has_thread_async(saver, stale)
        _consent(engine, True, False)
        assert await retention.drop_stale_threads(engine, saver) >= 1
        assert not await _has_thread_async(saver, stale) and await _has_thread_async(saver, fresh)
    assert _row(engine, stale).raw_kept is False
    # the late job finds no thread and still completes
    n = len(fake_llm.requests)
    out = await asyncio.to_thread(curate_session, _ctx(engine, db_url), _job(stale))
    assert _row(engine, stale).summary == "No conversation in this session." and len(fake_llm.requests) == n
    assert out["facts"] == 0


async def _has_thread_async(saver, sid) -> bool:
    return await saver.aget_tuple({"configurable": {"thread_id": sid}}) is not None
