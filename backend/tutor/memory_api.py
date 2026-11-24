"""The Memory & privacy API: consent settings, facts, progress reset, session delete and the full export.

- Consent: turning personal memory off (true -> false) purges every fact and needs `confirm_purge`; turning raw
  message keeping off deletes the threads of ended sessions that kept theirs. Active sessions are left alone: the
  curator decides when they end, from the consent in force then.
- The checkpoint is the raw-message store (D3), so anything that deletes or reads messages needs the saver
  (`app.state.saver`); without it those routes answer 503 before changing anything.
- Sync database work runs in a thread. JSON errors are `{"error": msg}`.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from backend.core.redact import redact_obj
from backend.tutor.db import session_scope
from backend.tutor.learner import store
from backend.tutor.models import LearnerFact, LearnerSettings, Roadmap, SkillProgress, StudyPlan, TutorSession
from backend.tutor.repo import get_roadmap_by_slug
from backend.tutor.tutor import checkpoint, planner
from backend.tutor.tutor.sessions import parse_id

UNAVAILABLE = "Database unavailable"
NOT_OBJECT = "The body must be a JSON object."
PURGE_NEEDED = "Turning personal memory off deletes {n} remembered facts. Send confirm_purge to continue."
STILL_ACTIVE = "This session is still active; end it first."
SETTING_KEYS = ("consent_personal", "consent_raw_messages", "retention_days")
KINDS = ("fact", "preference", "goal")
DEFAULT_RETENTION_DAYS = 90
MAX_RETENTION_DAYS = 3650
EXPORT_NAME = "roadmap-tutor-export.json"

router = APIRouter(prefix="/api")


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _unavailable() -> JSONResponse:
    return _error(503, UNAVAILABLE)


async def _json_object(request: Request) -> dict | None:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


# --- settings ---------------------------------------------------------------------------------------------

def _settings_view(row: LearnerSettings | None) -> dict:
    return {"consent_personal": row.consent_personal if row else None,
            "consent_raw_messages": row.consent_raw_messages if row else None,
            "retention_days": row.retention_days if row else DEFAULT_RETENTION_DAYS}


def _settings_error(body: dict) -> str | None:
    unknown = sorted(set(body) - {*SETTING_KEYS, "confirm_purge"})
    if unknown:
        return f"Unknown field(s): {', '.join(unknown)}."
    for key in ("consent_personal", "consent_raw_messages", "confirm_purge"):
        if key in body and not isinstance(body[key], bool):
            return f"'{key}' must be true or false."
    if "retention_days" in body:
        days = body["retention_days"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_RETENTION_DAYS:
            return f"'retention_days' must be a whole number from 1 to {MAX_RETENTION_DAYS}."
    return None


def _fact_count(engine: Engine) -> int:
    with session_scope(engine) as s:
        return s.scalar(select(func.count()).select_from(LearnerFact))


def _save_settings(engine: Engine, changes: dict) -> dict:
    with session_scope(engine) as s:
        row = s.get(LearnerSettings, 1)
        if row is None:
            row = LearnerSettings(id=1, retention_days=DEFAULT_RETENTION_DAYS)
            s.add(row)
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_at = datetime.now(timezone.utc)
        s.flush()
        return _settings_view(row)


def _kept_sessions(engine: Engine) -> list[uuid.UUID]:
    with session_scope(engine) as s:
        return list(s.scalars(select(TutorSession.id).where(
            TutorSession.status == "ended", TutorSession.raw_kept.is_(True))))


def _unkeep(engine: Engine, sid: uuid.UUID) -> None:
    with session_scope(engine) as s:
        s.get(TutorSession, sid).raw_kept = False


@router.get("/learner/settings")
def get_settings(request: Request):
    return _settings_view(store.get_settings(request.app.state.engine))


@router.put("/learner/settings")
async def put_settings(request: Request):
    body = await _json_object(request)
    if body is None:
        return _error(422, NOT_OBJECT)
    if reason := _settings_error(body):
        return _error(422, reason)
    engine, saver = request.app.state.engine, request.app.state.saver
    current = await asyncio.to_thread(store.get_settings, engine)
    changes = {k: body[k] for k in SETTING_KEYS if k in body}
    purge = current is not None and current.consent_personal is True and changes.get("consent_personal") is False
    drop_raw = current is not None and current.consent_raw_messages is True \
        and changes.get("consent_raw_messages") is False
    if purge and not body.get("confirm_purge"):
        return _error(409, PURGE_NEEDED.format(n=await asyncio.to_thread(_fact_count, engine)))
    if drop_raw and saver is None:
        return _unavailable()  # before anything changes
    view = await asyncio.to_thread(_save_settings, engine, changes)
    if purge:
        await asyncio.to_thread(store.purge_facts, engine)
    if drop_raw:
        for sid in await asyncio.to_thread(_kept_sessions, engine):
            await checkpoint.delete_thread(saver, str(sid))
            await asyncio.to_thread(_unkeep, engine, sid)
    return view


# --- facts ------------------------------------------------------------------------------------------------

def _fact_view(fact: LearnerFact, slug: str | None) -> dict:
    return {"id": str(fact.id), "kind": fact.kind, "text": fact.text, "roadmap": slug,
            "created_at": _iso(fact.created_at), "updated_at": _iso(fact.updated_at)}


def _facts(engine: Engine, roadmap: str) -> list[dict] | None:
    """Facts with their roadmap slug, oldest first: all of them, or one roadmap's plus the general ones. None for
    an unknown roadmap. Empty while consent is off."""
    consent = store.consented(engine)
    with session_scope(engine) as s:
        stmt = select(LearnerFact, Roadmap.slug).join(Roadmap, Roadmap.id == LearnerFact.roadmap_id, isouter=True)
        if roadmap != "all":
            rm = get_roadmap_by_slug(s, roadmap)
            if rm is None:
                return None
            stmt = stmt.where(LearnerFact.roadmap_id.is_(None) | (LearnerFact.roadmap_id == rm.id))
        if not consent:
            return []
        rows = s.execute(stmt.order_by(LearnerFact.created_at, LearnerFact.id)).all()
        return [_fact_view(f, slug) for f, slug in rows]


@router.get("/learner/facts")
async def list_facts(request: Request, roadmap: str = "all"):
    out = await asyncio.to_thread(_facts, request.app.state.engine, roadmap)
    return _error(404, f"No roadmap '{roadmap}'") if out is None else out


def _fact_edit_error(body: dict) -> str | None:
    unknown = sorted(set(body) - {"text", "kind"})
    if unknown:
        return f"Unknown field(s): {', '.join(unknown)}."
    if not isinstance(body.get("text"), str) or not body["text"].strip():
        return "'text' must be a non-empty string."
    if body.get("kind") not in KINDS:
        return f"'kind' must be one of: {', '.join(KINDS)}."
    return None


def _one_fact(engine: Engine, fact_id: str) -> dict:
    with session_scope(engine) as s:
        fact = s.get(LearnerFact, uuid.UUID(fact_id))
        slug = s.scalar(select(Roadmap.slug).where(Roadmap.id == fact.roadmap_id)) if fact.roadmap_id else None
        return _fact_view(fact, slug)


@router.put("/learner/facts/{fact_id}")
async def edit_fact(fact_id: str, request: Request):
    body = await _json_object(request)
    if body is None:
        return _error(422, NOT_OBJECT)
    if reason := _fact_edit_error(body):
        return _error(422, reason)
    app = request.app
    try:
        await asyncio.to_thread(store.update_fact, app.state.engine, app.state.embedder, fact_id,
                                body["text"], body["kind"])
    except ValueError as exc:
        message = str(exc)
        if message == store.CONSENT_OFF:
            return _error(409, message)
        return _error(404 if message.startswith("No fact") else 422, message)
    return await asyncio.to_thread(_one_fact, app.state.engine, fact_id)


@router.delete("/learner/facts/{fact_id}")
async def delete_fact(fact_id: str, request: Request):
    out = await asyncio.to_thread(store.forget, request.app.state.engine, fact_id)
    return Response(status_code=204) if out["deleted"] else _error(404, f"No fact '{fact_id}'")


# --- progress and sessions --------------------------------------------------------------------------------

def _reset_skill(engine: Engine, slug: str, skill_id: str) -> str | None:
    """An error message, or None once the skill's progress row is gone (or never existed)."""
    with session_scope(engine) as s:
        rm = get_roadmap_by_slug(s, slug)
        if rm is None:
            return f"No roadmap '{slug}'"
        known = {sk["id"] for st in (rm.spec or {}).get("stages") or [] for sk in st.get("skills", [])}
        if skill_id not in known:
            return f"No skill '{skill_id}' in roadmap '{slug}'"
        s.execute(delete(SkillProgress).where(SkillProgress.roadmap_id == rm.id, SkillProgress.skill_id == skill_id))
        return None


@router.delete("/roadmaps/{slug}/progress/{skill_id}")
async def reset_progress(slug: str, skill_id: str, request: Request):
    problem = await asyncio.to_thread(_reset_skill, request.app.state.engine, slug, skill_id)
    return _error(404, problem) if problem else Response(status_code=204)


def _session_status(engine: Engine, sid: uuid.UUID) -> str | None:
    with session_scope(engine) as s:
        return s.scalar(select(TutorSession.status).where(TutorSession.id == sid))


def _drop_session(engine: Engine, sid: uuid.UUID) -> None:
    with session_scope(engine) as s:
        s.execute(delete(TutorSession).where(TutorSession.id == sid))


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    engine, saver = request.app.state.engine, request.app.state.saver
    try:
        sid = parse_id(session_id)
    except LookupError:
        return _error(404, f"No session '{session_id}'")
    status = await asyncio.to_thread(_session_status, engine, sid)
    if status is None:
        return _error(404, f"No session '{session_id}'")
    if status == "active":
        return _error(409, STILL_ACTIVE)
    if saver is None:
        return _unavailable()
    await checkpoint.delete_thread(saver, str(sid))
    await asyncio.to_thread(_drop_session, engine, sid)  # a queued curation job finds no session and skips
    return Response(status_code=204)


# --- export -----------------------------------------------------------------------------------------------

def _export_rows(engine: Engine) -> dict:
    """Everything except the raw messages, which need the saver."""
    with session_scope(engine) as s:
        roadmaps = [(r.id, r.slug, bool((r.spec or {}).get("stages"))) for r in s.scalars(
            select(Roadmap).order_by(Roadmap.slug))]
        sessions = [{"id": str(t.id), "roadmap": slug, "mode": t.mode, "status": t.status, "title": t.title,
                     "started_at": _iso(t.started_at), "ended_at": _iso(t.ended_at), "summary": t.summary,
                     "cost_usd": float(t.cost_usd), "raw_kept": t.raw_kept}
                    for t, slug in s.execute(select(TutorSession, Roadmap.slug)
                                             .join(Roadmap, Roadmap.id == TutorSession.roadmap_id)
                                             .order_by(TutorSession.started_at, TutorSession.id))]
        plans = {slug: [{"version": p.version, "status": p.status, "created_at": _iso(p.created_at), "plan": p.plan}
                        for p in s.scalars(select(StudyPlan).where(StudyPlan.roadmap_id == rid)
                                           .order_by(StudyPlan.version))]
                 for rid, slug, _ in roadmaps}
        facts = [{**_fact_view(f, slug), "provenance": f.provenance}
                 for f, slug in s.execute(
                     select(LearnerFact, Roadmap.slug).join(Roadmap, Roadmap.id == LearnerFact.roadmap_id,
                                                            isouter=True)
                     .order_by(LearnerFact.created_at, LearnerFact.id))]  # no embeddings
    return {"settings": _settings_view(store.get_settings(engine)), "facts": facts,
            "progress": {slug: store.get_progress(engine, slug) for _, slug, has_spec in roadmaps if has_spec},
            "sessions": sessions,
            "study_settings": {slug: planner.get_study_settings(engine, slug) for _, slug, _ in roadmaps},
            "plans": plans}


async def _messages(saver, sid: str) -> list:
    tup = await saver.aget_tuple({"configurable": {"thread_id": sid}})
    values = (tup.checkpoint or {}).get("channel_values") or {} if tup is not None else {}
    return list(values.get("messages") or [])


@router.get("/learner/export")
async def export(request: Request):
    saver = request.app.state.saver
    doc = await asyncio.to_thread(_export_rows, request.app.state.engine)
    for row in doc["sessions"]:
        if row["raw_kept"]:
            if saver is None:
                row["messages_unavailable"] = True
            else:
                row["messages"] = await _messages(saver, row["id"])
    doc = {"exported_at": datetime.now(timezone.utc).isoformat(), **doc}
    return Response(json.dumps(redact_obj(doc), default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{EXPORT_NAME}"'})
