"""Worker loop and pool over the durable queue. Handlers are registered by later tasks."""
from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from backend.core.trace import start_trace
from backend.tutor.jobs import queue
from backend.tutor.jobs.queue import PermanentError, RetryableError  # noqa: F401  (re-exported for handlers)
from backend.tutor.models import Job

if TYPE_CHECKING:
    from backend.tutor.mcp.gateway import Gateway

log = logging.getLogger(__name__)

Handler = Callable[["JobContext", Job], "dict | None"]
SettledHook = Callable[[uuid.UUID], None]
POLL_S = 1.0
REQUEUE_EVERY_S = 60.0
# A roadmap still `ingesting`/`finalizing` with no queued or running job, whose last job changed longer ago than
# this, was left behind by a crash (or a failed settle check); the reaper settles it. Long enough that a settle
# hook that is still running (it only reads the KB and finalizes, in seconds) is not run a second time alongside.
STUCK_AFTER_S = 60.0


def process_tag() -> str:
    """`<hostname>-<pid>-<random>`: unique to this process (and call), so the lease fence (`locked_by`) can tell
    apart workers of the API and of a CLI `ingest` running at the same time, on one host or several."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


@dataclass
class JobContext:
    engine: Engine
    net: Any = None
    settings: Any = None
    index: Any = None
    gateway: Gateway | None = None  # the MCP gateway the API started (None: no gateway runs)
    embedder: Any = None  # the index's embedder, for handlers that write learner facts (curate_session)

    def enqueue(self, type: str, payload: dict, key_parts: tuple | list = (), parent_id: int | None = None,
                *, roadmap_id: uuid.UUID, max_attempts: int = 5) -> Job:
        """Idempotently add a job (own transaction, committed). Same key_parts -> same job."""
        key = queue.job_key(type, roadmap_id, *[str(p) for p in key_parts])
        with Session(self.engine, expire_on_commit=False) as s:
            job = queue.enqueue(s, type=type, roadmap_id=roadmap_id, payload=payload, key=key,
                                parent_id=parent_id, max_attempts=max_attempts)
            s.commit()
            return job


def _maybe_settle(engine: Engine, roadmap_id: uuid.UUID, on_settled: SettledHook | None) -> None:
    """If the roadmap has no queued/running jobs, CAS ingesting -> finalizing; only the winner fires the hook."""
    try:
        with Session(engine) as s:
            if not queue.is_roadmap_settled(s, roadmap_id):
                return
            won = s.execute(
                text("UPDATE roadmaps SET status='finalizing' WHERE id=:id AND status='ingesting'"),
                {"id": roadmap_id},
            ).rowcount
            s.commit()
        if won and on_settled is not None:
            on_settled(roadmap_id)
    except Exception:
        log.exception("settled hook failed for roadmap %s", roadmap_id)


maybe_settle = _maybe_settle  # public name for callers outside the worker loop (CLI, approve)


def sweep_stuck(engine: Engine, on_settled: SettledHook | None, older_than_s: float = STUCK_AFTER_S) -> list:
    """Settle roadmaps a crash left `ingesting` (died between the last `complete` and the settle check, or the
    check failed) or `finalizing` (died inside the hook): no queued/running job, and nothing touched their jobs
    for `older_than_s` (a roadmap with no jobs at all counts from its creation). `ingesting` goes through the
    usual CAS, so only one caller fires the hook; `finalizing` fires the hook again, which is idempotent
    (`finalize_roadmap` recomputes everything in one transaction; starting a recovery round is a CAS plus a
    unique job key). Without a hook nothing is done (the CAS alone would strand the roadmap in `finalizing`).
    Never raises; returns the roadmap ids it settled."""
    if on_settled is None:
        return []
    try:
        cutoff = queue.clock() - timedelta(seconds=older_than_s)
        with Session(engine) as s:
            rows = s.execute(text(
                "SELECT r.id, r.status FROM roadmaps r WHERE r.status IN ('ingesting', 'finalizing') "
                "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.roadmap_id = r.id "
                "AND j.status IN ('queued', 'running')) "
                "AND COALESCE((SELECT max(j.updated_at) FROM jobs j WHERE j.roadmap_id = r.id), r.created_at) "
                "< :cutoff ORDER BY r.id"), {"cutoff": cutoff}).all()
    except Exception:
        log.exception("could not look for stuck roadmaps")
        return []
    for rid, status in rows:
        log.warning("roadmap %s was left %s with no work; settling it", rid, status)
        if status == "ingesting":
            _maybe_settle(engine, rid, on_settled)
            continue
        try:
            with Session(engine) as s:  # re-check: another sweep or worker may have moved it meanwhile
                still = s.execute(text("SELECT status FROM roadmaps WHERE id = :id"), {"id": rid}).scalar()
                idle = queue.is_roadmap_settled(s, rid)
            if still == "finalizing" and idle:
                on_settled(rid)
        except Exception:
            log.exception("settled hook failed for stuck roadmap %s", rid)
    return [rid for rid, _ in rows]


def reap_and_settle(engine: Engine, on_settled: SettledHook | None = None, lease_s: int = 600) -> int:
    """Requeue/dead-letter expired leases, then run the settle check for roadmaps that lost a job to dead-lettering.
    Returns the requeued count."""
    requeued, dead_roadmaps = queue.reap_expired(engine, lease_s)
    for rid in dead_roadmaps:
        _maybe_settle(engine, rid, on_settled)
    return requeued


def run_one(
    ctx: JobContext,
    handlers: dict[str, Handler],
    worker_id: str,
    *,
    on_settled: SettledHook | None = None,
    tracer_provider=None,
    roadmap_id: uuid.UUID | None = None,
) -> bool:
    if not handlers:
        return False
    job = queue.claim(ctx.engine, worker_id, types=set(handlers), roadmap_id=roadmap_id)
    if job is None:
        return False
    trace = start_trace(f"job:{job.type}")
    status = "done"
    try:
        try:
            with trace.span(f"job:{job.type}", kind="step", job_id=job.id, attempt=job.attempts):
                handler = handlers[job.type]
                result = handler(ctx, job)
            if not queue.complete(ctx.engine, job.id, result, worker_id=worker_id):
                status = None  # lease lost: another worker owns the job now
        except PermanentError as e:
            status = queue.fail(ctx.engine, job.id, f"{type(e).__name__}: {e}", retryable=False, worker_id=worker_id)
        except Exception as e:  # RetryableError and anything else
            status = queue.fail(ctx.engine, job.id, f"{type(e).__name__}: {e}", retryable=True, worker_id=worker_id)
        if status in ("done", "dead"):
            _maybe_settle(ctx.engine, job.roadmap_id, on_settled)
    finally:
        if tracer_provider is not None:
            from backend.tutor.telemetry import export_trace

            export_trace(trace, tracer_provider)  # never raises
    return True


def run_until_idle(
    ctx: JobContext,
    handlers: dict[str, Handler],
    *,
    roadmap_id: uuid.UUID | None = None,
    max_jobs: int = 10_000,
    on_settled: SettledHook | None = None,
    worker_id: str | None = None,
) -> int:
    """Run claimable jobs until none are due (jobs backing off in the future are not waited for)."""
    worker_id = worker_id or f"inline-{process_tag()}"
    n = 0
    while n < max_jobs and run_one(ctx, handlers, worker_id, on_settled=on_settled, roadmap_id=roadmap_id):
        n += 1
    return n


class WorkerPool:
    def __init__(self, ctx: JobContext, handlers: dict[str, Handler], n: int,
                 on_settled: SettledHook | None = None, tracer_provider=None):
        self.ctx, self.handlers, self.n = ctx, handlers, n
        self.on_settled, self.tracer_provider = on_settled, tracer_provider
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        tag = process_tag()
        self.worker_ids = [f"{tag}-{i}" for i in range(n)]  # the lease owner (`locked_by`) of each thread

    def start(self) -> None:
        self._stop.clear()
        for i, worker_id in enumerate(self.worker_ids):
            t = threading.Thread(target=self._loop, args=(worker_id, i == 0), name=f"job-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = [t for t in self._threads if t.is_alive()]

    def _loop(self, worker_id: str, reaper: bool) -> None:
        import time

        last_requeue = None  # the first tick runs at once: a restart picks up what a crash left behind
        while not self._stop.is_set():
            try:
                if reaper and (last_requeue is None or time.monotonic() - last_requeue >= REQUEUE_EVERY_S):
                    last_requeue = time.monotonic()
                    reap_and_settle(self.ctx.engine, self.on_settled)
                    sweep_stuck(self.ctx.engine, self.on_settled)
                if run_one(self.ctx, self.handlers, worker_id, on_settled=self.on_settled,
                           tracer_provider=self.tracer_provider):
                    continue
            except Exception:
                log.exception("worker %s loop error", worker_id)
            self._stop.wait(POLL_S)
