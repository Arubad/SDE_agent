"""The session lifecycle: create a tutor session, end it (queueing memory curation), list and look up sessions.

`enqueue` has `JobContext.enqueue`'s shape: `enqueue(type, payload, key_parts, *, roadmap_id)`.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from backend.tutor.db import session_scope
from backend.tutor.models import Roadmap, TutorSession
from backend.tutor.repo import get_roadmap_by_slug

NOT_READY = "Roadmap '{slug}' is '{status}'; the tutor opens once ingestion is ready."
READY_STATUSES = ("ready", "ready_with_gaps")


class NotReady(Exception):
    """The roadmap is not ready for a tutor session."""


def _has_skills(spec: dict | None) -> bool:
    return any(st.get("skills") for st in (spec or {}).get("stages", []))


def create_session(engine: Engine, slug: str, mode: str, *, enqueue: Callable[..., Any]) -> TutorSession:
    """A new active session. Ends the roadmap's active session(s) first."""
    with session_scope(engine) as s:
        rm = get_roadmap_by_slug(s, slug)
        if rm is None:
            raise LookupError(f"No roadmap '{slug}'")
        if rm.status not in READY_STATUSES or not _has_skills(rm.spec):
            raise NotReady(NOT_READY.format(slug=slug, status=rm.status))
        roadmap_id = rm.id
        active = list(s.scalars(select(TutorSession.id).where(
            TutorSession.roadmap_id == roadmap_id, TutorSession.status == "active")))
    for session_id in active:
        end_session(engine, session_id, enqueue)
    with session_scope(engine) as s:
        row = TutorSession(roadmap_id=roadmap_id, mode=mode, status="active")
        s.add(row)
        s.flush()
        return row


def end_session(engine: Engine, session_id: uuid.UUID | str, enqueue: Callable[..., Any]) -> TutorSession:
    """Idempotent: an ended session is returned as it is, and nothing is enqueued."""
    sid = session_id if isinstance(session_id, uuid.UUID) else _uuid(session_id)
    with session_scope(engine) as s:
        row = s.get(TutorSession, sid, with_for_update=True)
        if row is None:
            raise LookupError(str(session_id))
        if row.status == "ended":
            return row
        row.status, row.ended_at = "ended", datetime.now(timezone.utc)
    # After the commit, so the curation job sees an ended session. The job key makes a retry harmless.
    enqueue("curate_session", {"session_id": str(sid)}, (str(sid),), roadmap_id=row.roadmap_id)
    return row


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise LookupError(value) from None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def list_sessions(engine: Engine, slug: str) -> list[dict]:
    """The roadmap's sessions, newest first. `title` is None until memory curation names the session."""
    with session_scope(engine) as s:
        rm = get_roadmap_by_slug(s, slug)
        if rm is None:
            raise LookupError(f"No roadmap '{slug}'")
        rows = s.scalars(select(TutorSession).where(TutorSession.roadmap_id == rm.id)
                         .order_by(TutorSession.started_at.desc(), TutorSession.id.desc()))
        return [{"id": str(r.id), "title": r.title, "started_at": _iso(r.started_at),
                 "ended_at": _iso(r.ended_at), "summary": r.summary, "status": r.status} for r in rows]


def ended_sessions(engine: Engine, roadmap: str, limit: int) -> list[TutorSession]:
    """The roadmap's ended sessions (by slug), newest `ended_at` first."""
    with session_scope(engine) as s:
        return list(s.scalars(
            select(TutorSession).join(Roadmap, Roadmap.id == TutorSession.roadmap_id)
            .where(Roadmap.slug == roadmap, TutorSession.status == "ended")
            .order_by(TutorSession.ended_at.desc(), TutorSession.id.desc()).limit(limit)))


def last_ended(engine: Engine, roadmap_id: uuid.UUID) -> TutorSession | None:
    with session_scope(engine) as s:
        return s.scalars(select(TutorSession).where(
            TutorSession.roadmap_id == roadmap_id, TutorSession.status == "ended")
            .order_by(TutorSession.ended_at.desc(), TutorSession.id.desc()).limit(1)).first()
