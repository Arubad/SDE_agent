"""Retention: the raw-message threads of old sessions, idle sessions, and the startup/periodic maintenance task
(D3, D6, R19)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from backend.tutor.db import session_scope
from backend.tutor.jobs import queue
from backend.tutor.learner import store
from backend.tutor.models import LearnerFact, TutorSession
from backend.tutor.tutor import checkpoint

log = logging.getLogger(__name__)

RETENTION_EVERY_S = 21600
DEFAULT_RETENTION_DAYS = 90
UNCURATED_GRACE_S = 86400  # curation had a day to run; after that an uncurated session's raw thread is dropped
IDLE_END_S = 21600  # an active session idle this long (since its last turn, else its start) is ended (R19)
IDLE_LOCK_WAIT_S = 1.0  # a running turn holds the session's lock: skip it after this, the next pass retries


def _idle(engine: Engine, cutoff: datetime) -> list:
    with session_scope(engine) as s:
        return list(s.scalars(select(TutorSession.id).where(
            TutorSession.status == "active",
            func.coalesce(TutorSession.last_turn_at, TutorSession.started_at) < cutoff)))


async def end_idle_sessions(engine: Engine, runtime: Any) -> int:
    """End every active session idle for over IDLE_END_S through `runtime.end_session` (its turn lock, curation
    and D3 apply). A session with a running turn is skipped (`Busy`) and retried on the next pass; one used since it
    was found idle is left active. Returns how many were ended."""
    from backend.tutor.tutor.runtime import Busy

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=IDLE_END_S)
    n = 0
    for sid in await asyncio.to_thread(_idle, engine, cutoff):
        try:
            row = await runtime.end_session(str(sid), wait_s=IDLE_LOCK_WAIT_S, idle_before=cutoff)
            n += row.status == "ended"
        except Busy:
            log.info("retention: idle session %s is busy; retried next pass", sid)
        except Exception as exc:
            log.warning("retention: idle session %s not ended: %s", sid, type(exc).__name__)
    return n


def _due(engine: Engine) -> list:
    settings = store.get_settings(engine)
    days = settings.retention_days if settings is not None and settings.retention_days else DEFAULT_RETENTION_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope(engine) as s:
        return list(s.scalars(select(TutorSession.id).where(
            TutorSession.status == "ended", TutorSession.raw_kept.is_(True), TutorSession.ended_at < cutoff)))


def _unkeep(engine: Engine, sid) -> None:
    with session_scope(engine) as s:
        s.get(TutorSession, sid).raw_kept = False


async def sweep(engine: Engine, saver: Any) -> int:
    """Delete the threads of sessions that ended more than `retention_days` ago and still have `raw_kept`, then
    clear the flag. Returns how many were swept; one that fails is logged and left for the next run."""
    swept = 0
    for sid in await asyncio.to_thread(_due, engine):
        try:
            await checkpoint.delete_thread(saver, str(sid))
            await asyncio.to_thread(_unkeep, engine, sid)
            swept += 1
        except Exception as exc:
            log.warning("retention: session %s not swept: %s", sid, type(exc).__name__)
    return swept


def _uncurated(engine: Engine) -> list:
    with session_scope(engine) as s:
        return list(s.execute(select(TutorSession.id, TutorSession.roadmap_id).where(
            TutorSession.status == "ended", TutorSession.curated_at.is_(None))).all())


async def reenqueue_uncurated(engine: Engine, enqueue: Callable[..., Any]) -> int:
    """Queue `curate_session` for every ended session not curated yet. A crash between `end_session`'s commit and
    its enqueue would otherwise lose the job; the job key makes this idempotent."""
    rows = await asyncio.to_thread(_uncurated, engine)
    for sid, roadmap_id in rows:
        await asyncio.to_thread(enqueue, "curate_session", {"session_id": str(sid)}, (str(sid),),
                                roadmap_id=roadmap_id)
        # a job that died (a long model outage) stays dead under its key: give it fresh attempts
        await asyncio.to_thread(queue.revive, engine, queue.job_key("curate_session", roadmap_id, str(sid)))
    return len(rows)


def _stale_uncurated(engine: Engine) -> list:
    settings = store.get_settings(engine)
    if settings is not None and settings.consent_raw_messages is True:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=UNCURATED_GRACE_S)
    with session_scope(engine) as s:
        return list(s.scalars(select(TutorSession.id).where(
            TutorSession.status == "ended", TutorSession.curated_at.is_(None), TutorSession.ended_at < cutoff)))


async def drop_stale_threads(engine: Engine, saver: Any) -> int:
    """Delete the raw thread of sessions that ended over UNCURATED_GRACE_S ago and were never curated, unless raw
    messages are consented to (D3: without consent they must not outlive the session). `raw_kept` stays false;
    a curation job that runs later finds no thread and writes the empty summary."""
    n = 0
    for sid in await asyncio.to_thread(_stale_uncurated, engine):
        try:
            await checkpoint.delete_thread(saver, str(sid))
            n += 1
        except Exception as exc:
            log.warning("retention: thread of %s not dropped: %s", sid, type(exc).__name__)
    return n


def _revoked(engine: Engine) -> tuple[list, bool]:
    """Ended sessions still holding a raw thread while raw consent is not on, and whether personal consent is off
    while facts remain."""
    settings = store.get_settings(engine)
    with session_scope(engine) as s:
        kept = [] if settings is not None and settings.consent_raw_messages is True else list(s.scalars(
            select(TutorSession.id).where(TutorSession.status == "ended", TutorSession.raw_kept.is_(True))))
        facts = settings is not None and settings.consent_personal is False and s.scalar(
            select(LearnerFact.id).limit(1)) is not None
    return kept, bool(facts)


async def sweep_revoked(engine: Engine, saver: Any) -> int:
    """State-based cleanup of what a revoke left behind (a crash between its steps): delete the kept raw threads
    while `consent_raw_messages` is not True, and purge every fact while `consent_personal` is False."""
    kept, facts = await asyncio.to_thread(_revoked, engine)
    n = 0
    for sid in kept:
        try:
            await checkpoint.delete_thread(saver, str(sid))
            await asyncio.to_thread(_unkeep, engine, sid)
            n += 1
        except Exception as exc:
            log.warning("retention: revoked thread %s not deleted: %s", sid, type(exc).__name__)
    if facts:
        n += await asyncio.to_thread(store.purge_facts, engine)
    return n


async def run_forever(engine: Engine, saver: Any, enqueue: Callable[..., Any],
                      every_s: float = RETENTION_EVERY_S, runtime: Any = None) -> None:
    """End idle sessions (only with a `runtime`), re-enqueue, then sweep, now and every `every_s`. Failures are
    logged; cancel the task to stop it."""
    while True:
        steps = [("end-idle", lambda: end_idle_sessions(engine, runtime))] if runtime is not None else []
        for name, step in (*steps,
                           ("re-enqueue", lambda: reenqueue_uncurated(engine, enqueue)),
                           ("drop-stale", lambda: drop_stale_threads(engine, saver)),
                           ("sweep", lambda: sweep(engine, saver)),
                           ("revoked", lambda: sweep_revoked(engine, saver))):
            try:
                await step()
            except Exception as exc:
                log.warning("retention %s failed: %s", name, type(exc).__name__)
        await asyncio.sleep(every_s)
