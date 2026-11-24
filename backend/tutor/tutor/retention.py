"""Retention: the raw-message threads of old sessions, and the startup/periodic maintenance task (D3, D6)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from backend.tutor.db import session_scope
from backend.tutor.learner import store
from backend.tutor.models import TutorSession
from backend.tutor.tutor import checkpoint

log = logging.getLogger(__name__)

RETENTION_EVERY_S = 21600
DEFAULT_RETENTION_DAYS = 90


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
    return len(rows)


async def run_forever(engine: Engine, saver: Any, enqueue: Callable[..., Any],
                      every_s: float = RETENTION_EVERY_S) -> None:
    """Re-enqueue, then sweep, now and every `every_s`. Failures are logged; cancel the task to stop it."""
    while True:
        for name, step in (("re-enqueue", lambda: reenqueue_uncurated(engine, enqueue)),
                           ("sweep", lambda: sweep(engine, saver))):
            try:
                await step()
            except Exception as exc:
                log.warning("retention %s failed: %s", name, type(exc).__name__)
        await asyncio.sleep(every_s)
