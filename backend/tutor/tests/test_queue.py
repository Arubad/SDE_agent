import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.tutor.jobs import queue
from backend.tutor.jobs.queue import PermanentError, RetryableError, job_key
from backend.tutor.jobs.worker import JobContext, WorkerPool, run_one, run_until_idle
from backend.tutor.models import Job, Roadmap


@pytest.fixture
def rid(engine):
    r = uuid.uuid4()
    with Session(engine) as s:
        s.add(Roadmap(id=r, slug=f"q-{r.hex[:10]}", title="t", source_text="x", status="ingesting"))
        s.commit()
    yield r
    with Session(engine) as s:  # cascades the jobs; keeps unscoped queue calls in other tests isolated
        s.execute(text("delete from roadmaps where id=:r"), {"r": r})
        s.commit()


@pytest.fixture
def ctx(engine):
    return JobContext(engine=engine)


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"now": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(queue, "clock", lambda: state["now"])
    return state


def _add(engine, rid, n=1, prefix="j", **kw):
    ids = []
    with Session(engine) as s:
        for i in range(n):
            j = queue.enqueue(s, type=kw.get("type", "t"), roadmap_id=rid, payload={"i": i},
                              key=f"{prefix}:{rid}:{i}", max_attempts=kw.get("max_attempts", 5))
            ids.append(j.id)
        s.commit()
    return ids


def _get(engine, jid):
    with Session(engine) as s:
        return s.get(Job, jid)


def test_enqueue_is_idempotent(engine, rid):
    with Session(engine) as s:
        a = queue.enqueue(s, type="t", roadmap_id=rid, payload={"a": 1}, key=f"k:{rid}")
        b = queue.enqueue(s, type="t", roadmap_id=rid, payload={"a": 2}, key=f"k:{rid}")
        s.commit()
        assert a.id == b.id
        assert s.execute(text("select count(*) from jobs where roadmap_id=:r"), {"r": rid}).scalar() == 1


def test_job_key_normalizes_urls():
    r = uuid.uuid4()
    assert job_key("fetch", r, "HTTP://A.dev/x/#f?") == job_key("fetch", r, "http://a.dev/x?utm_source=z")
    assert job_key("fetch", r, "http://a.dev/x") != job_key("fetch", r, "http://a.dev/y")
    assert job_key("fetch", r, "http://a.dev/x?q=1&utm_medium=m") == job_key("fetch", r, "http://a.dev/x?q=1")


def test_concurrent_claims_never_duplicate(engine, rid):
    _add(engine, rid, 50)
    got, lock = [], threading.Lock()

    def work(w):
        while True:
            j = queue.claim(engine, f"w{w}", roadmap_id=rid)
            if j is None:
                return
            with lock:
                got.append(j.id)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 50 and len(set(got)) == 50


def test_failure_backs_off_then_dead_letters(engine, rid, fake_clock):
    (jid,) = _add(engine, rid, max_attempts=3)
    t0 = fake_clock["now"]
    j = queue.claim(engine, "w", roadmap_id=rid)
    assert j.id == jid and j.attempts == 1
    assert queue.fail(engine, jid, "boom") == "queued"
    row = _get(engine, jid)
    assert row.status == "queued"
    assert 8 <= (row.not_before - t0).total_seconds() <= 12
    assert queue.claim(engine, "w", roadmap_id=rid) is None  # still backing off
    fake_clock["now"] = row.not_before + timedelta(seconds=1)
    assert queue.claim(engine, "w", roadmap_id=rid).attempts == 2
    queue.fail(engine, jid, "boom")
    fake_clock["now"] += timedelta(seconds=1000)
    assert queue.claim(engine, "w", roadmap_id=rid).attempts == 3
    assert queue.fail(engine, jid, "boom") == "dead"
    assert _get(engine, jid).status == "dead"


def test_backoff_is_capped():
    assert all(queue.backoff_seconds(20) <= 300 * 1.2 + 1e-9 for _ in range(50))


def test_error_is_redacted(engine, rid):
    (jid,) = _add(engine, rid)
    queue.claim(engine, "w", roadmap_id=rid)
    queue.fail(engine, jid, "contact bob@example.com failed")
    err = _get(engine, jid).error
    assert "bob@example.com" not in err and "[email]" in err


def test_permanent_error_goes_straight_to_dead(engine, rid, ctx):
    _add(engine, rid)

    def h(c, j):
        raise PermanentError("nope")

    assert run_until_idle(ctx, {"t": h}, roadmap_id=rid) == 1
    with Session(engine) as s:
        j = s.execute(select(Job).where(Job.roadmap_id == rid)).scalar_one()
    assert j.status == "dead" and j.attempts == 1 and "nope" in j.error


def test_unexpected_exception_is_retryable(engine, rid, ctx, fake_clock):
    (jid,) = _add(engine, rid)

    def h(c, j):
        raise ValueError("odd")

    run_one(ctx, {"t": h}, "w", roadmap_id=rid)
    assert _get(engine, jid).status == "queued"
    assert _get(engine, jid).updated_at == fake_clock["now"]


def test_expired_lease_is_requeued(engine, rid, fake_clock):
    (jid,) = _add(engine, rid)
    queue.claim(engine, "dead-worker", roadmap_id=rid)
    assert queue.requeue_expired(engine, lease_s=600) == 0
    fake_clock["now"] += timedelta(seconds=601)
    assert queue.requeue_expired(engine, lease_s=600) >= 1
    row = _get(engine, jid)
    assert row.status == "queued" and row.locked_by is None
    assert queue.claim(engine, "w2", roadmap_id=rid).id == jid


def test_run_until_idle_runs_children(engine, rid, ctx):
    _add(engine, rid, type="parent")

    def parent(c, j):
        for i in range(3):
            c.enqueue("child", {"i": i}, (j.id, i), j.id, roadmap_id=j.roadmap_id)
        return {"spawned": 3}

    handlers = {"parent": parent, "child": lambda c, j: {"ok": True}}
    assert run_until_idle(ctx, handlers, roadmap_id=rid) == 4
    with Session(engine) as s:
        rows = s.execute(select(Job).where(Job.roadmap_id == rid)).scalars().all()
    assert len(rows) == 4 and all(r.status == "done" for r in rows)
    parent_id = next(r.id for r in rows if r.type == "parent")
    assert {r.parent_id for r in rows if r.type == "child"} == {parent_id}
    with Session(engine) as s:
        counts = queue.roadmap_job_counts(s, rid)
    assert counts == {"parent": {"done": 1}, "child": {"done": 3}}


def _status(engine, rid):
    with Session(engine) as s:
        return s.get(Roadmap, rid).status


@pytest.mark.parametrize("attempt", range(3))
def test_settled_hook_fires_once(engine, rid, ctx, attempt):
    _add(engine, rid, 20)
    calls = []
    barrier = threading.Barrier(2)

    def h(c, j):
        return None

    def work(w):
        barrier.wait()
        while run_one(ctx, {"t": h}, f"w{w}", on_settled=calls.append, roadmap_id=rid):
            pass

    ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls == [rid]
    assert _status(engine, rid) == "finalizing"


def test_dead_last_job_settles_and_hook_errors_are_swallowed(engine, rid, ctx):
    _add(engine, rid)

    def h(c, j):
        raise PermanentError("x")

    def hook(r):
        raise RuntimeError("hook blew up")

    assert run_until_idle(ctx, {"t": h}, roadmap_id=rid, on_settled=hook) == 1
    assert _status(engine, rid) == "finalizing"


def test_no_settle_while_work_remains(engine, rid, ctx):
    _add(engine, rid, 2)
    calls = []
    run_one(ctx, {"t": lambda c, j: None}, "w", on_settled=calls.append, roadmap_id=rid)
    assert calls == [] and _status(engine, rid) == "ingesting"


def test_worker_pool_processes_and_stops(engine, rid, ctx):
    _add(engine, rid, 6)
    done = threading.Event()
    pool = WorkerPool(ctx, {"t": lambda c, j: None}, 2, on_settled=lambda r: done.set())
    pool.start()
    try:
        assert done.wait(10)
    finally:
        pool.stop(5)
    assert not pool._threads
    assert not [t for t in threading.enumerate() if t.name.startswith("job-worker")]


def test_job_span_is_exported_when_provider_configured(engine, rid, ctx):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from backend.tutor.telemetry import setup_tracing

    exporter = InMemorySpanExporter()
    provider = setup_tracing(None, exporter=exporter)
    _add(engine, rid)
    run_one(ctx, {"t": lambda c, j: None}, "w", tracer_provider=provider, roadmap_id=rid)
    assert "job:t" in {s.name for s in exporter.get_finished_spans()}
