"""Postgres-backed job queue: SKIP LOCKED claims, idempotency keys, backoff, dead-lettering, leases.

Clock: every time comparison (`not_before`, lease expiry, `updated_at`) uses the Python-side `clock()`
bound as a query parameter, never DB `now()`, so tests can fake time and all workers agree with one
source. (In production all workers should share NTP-synced clocks; skew only shifts backoff/lease edges.)
"""
from __future__ import annotations

import hashlib
import logging
import random
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import Engine, case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend.core.redact import redact
from backend.tutor.models import Job

BACKOFF_BASE_S = 5
BACKOFF_CAP_S = 300
JITTER = 0.2
ERROR_MAX_CHARS = 2000
log = logging.getLogger(__name__)


class RetryableError(Exception):
    pass


class PermanentError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level injectable clock (tests replace it).
clock: Callable[[], datetime] = _utcnow


def _normalize(part: str) -> str:
    if "://" not in part:
        return part
    u = urlsplit(part.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not k.lower().startswith("utm_")])
    path = u.path.rstrip("/")
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), path, query, ""))


def job_key(type: str, roadmap_id, *parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(_normalize(str(p)) for p in parts).encode()).hexdigest()[:16]
    return f"{type}:{roadmap_id}:{digest}"


def enqueue(
    session: Session,
    *,
    type: str,
    roadmap_id: uuid.UUID,
    payload: dict,
    key: str,
    parent_id: int | None = None,
    max_attempts: int = 5,
) -> Job:
    stmt = (
        insert(Job)
        .values(type=type, roadmap_id=roadmap_id, payload=payload, key=key, parent_id=parent_id,
                max_attempts=max_attempts, not_before=clock())
        .on_conflict_do_nothing(index_elements=["key"])
        .returning(Job.id)
    )
    session.execute(stmt)
    session.flush()
    job = session.execute(select(Job).where(Job.key == key).execution_options(populate_existing=True)).scalar_one()
    return job


def claim(
    engine: Engine,
    worker_id: str,
    *,
    types: set[str] | None = None,
    lease_s: int = 600,  # accepted for API symmetry; leases are enforced by requeue_expired
    roadmap_id: uuid.UUID | None = None,
) -> Job | None:
    now = clock()
    with Session(engine, expire_on_commit=False) as s:
        q = select(Job).where(Job.status == "queued", Job.not_before <= now)
        if types is not None:
            q = q.where(Job.type.in_(types))
        if roadmap_id is not None:
            q = q.where(Job.roadmap_id == roadmap_id)
        job = s.execute(q.order_by(Job.id).limit(1).with_for_update(skip_locked=True)).scalar_one_or_none()
        if job is None:
            s.rollback()
            return None
        job.status = "running"
        job.locked_by = worker_id
        job.locked_at = now
        job.attempts += 1
        job.updated_at = now
        s.commit()
        s.expunge(job)
        return job


def complete(engine: Engine, job_id: int, result: dict | None = None, *, worker_id: str) -> bool:
    """Mark done, fenced on (running, locked_by=worker_id). Returns False (and logs) if the lease was lost."""
    now = clock()
    with Session(engine) as s:
        res = s.execute(
            update(Job).where(Job.id == job_id, Job.status == "running", Job.locked_by == worker_id)
            .values(status="done", result=result, error=None, locked_by=None, locked_at=None, updated_at=now)
        )
        s.commit()
    if not res.rowcount:
        log.warning("complete ignored: job %s is no longer running under %s", job_id, worker_id)
    return bool(res.rowcount)


def backoff_seconds(attempts: int) -> float:
    base = min((2**attempts) * BACKOFF_BASE_S, BACKOFF_CAP_S)
    return base * (1 + random.uniform(-JITTER, JITTER))


def fail(engine: Engine, job_id: int, error: str, *, retryable: bool = True, worker_id: str) -> str | None:
    """Record a failure, fenced on (running, locked_by=worker_id).

    Returns the new status ('queued' for a retry, 'dead' otherwise), or None if the lease was lost."""
    now = clock()
    err = redact(error)[:ERROR_MAX_CHARS]
    with Session(engine) as s:
        job = s.execute(
            select(Job).where(Job.id == job_id, Job.status == "running", Job.locked_by == worker_id).with_for_update()
        ).scalar_one_or_none()
        if job is None:
            log.warning("fail ignored: job %s is no longer running under %s", job_id, worker_id)
            return None
        if retryable and job.attempts < job.max_attempts:
            job.status = "queued"
            job.not_before = now + timedelta(seconds=backoff_seconds(job.attempts))
        else:
            job.status = "dead"
        job.error = err
        job.locked_by = None
        job.locked_at = None
        job.updated_at = now
        status = job.status
        s.commit()
        return status


def reap_expired(engine: Engine, lease_s: int = 600) -> tuple[int, list[uuid.UUID]]:
    """Expired running jobs go back to 'queued', or to 'dead' once attempts >= max_attempts (poison jobs).

    Returns (requeued_count, roadmap_ids that had a job dead-lettered) so the caller can run the settle check."""
    now = clock()
    dead = case((Job.attempts >= Job.max_attempts, "dead"), else_="queued")
    with Session(engine) as s:
        rows = s.execute(
            update(Job)
            .where(Job.status == "running", Job.locked_at < now - timedelta(seconds=lease_s))
            .values(
                status=dead,
                error=case((Job.attempts >= Job.max_attempts, "lease expired"), else_=Job.error),
                not_before=now, locked_by=None, locked_at=None, updated_at=now,
            )
            .returning(Job.status, Job.roadmap_id)
        ).all()
        s.commit()
    requeued = sum(1 for st, _ in rows if st == "queued")
    return requeued, sorted({r for st, r in rows if st == "dead"}, key=str)


def requeue_expired(engine: Engine, lease_s: int = 600) -> int:
    """Returns the number of jobs requeued (dead-lettered poison jobs are not counted; see reap_expired)."""
    return reap_expired(engine, lease_s)[0]


def roadmap_job_counts(session: Session, roadmap_id: uuid.UUID) -> dict[str, dict[str, int]]:
    rows = session.execute(
        select(Job.type, Job.status, func.count()).where(Job.roadmap_id == roadmap_id).group_by(Job.type, Job.status)
    ).all()
    out: dict[str, dict[str, int]] = {}
    for t, st, n in rows:
        out.setdefault(t, {})[st] = n
    return out


def is_roadmap_settled(session: Session, roadmap_id: uuid.UUID) -> bool:
    n = session.execute(
        select(func.count()).select_from(Job).where(Job.roadmap_id == roadmap_id, Job.status.in_(("queued", "running")))
    ).scalar_one()
    return n == 0
