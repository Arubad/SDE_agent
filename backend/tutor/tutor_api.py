"""The tutor HTTP API: sessions, SSE turns, the skill map, study settings and the plan.

- Every route answers 503 `Database unavailable` when the tutor runtime did not start (no checkpointer: the
  database was down at startup). Dependencies come from `request.app.state` (`tutor_runtime`, `engine`).
- Sessions are created and ended through the runtime only (R7), which waits for a running turn.
- A turn body is validated before anything is acquired. The turn's first event is awaited before the response
  starts, so `Busy`, `SessionEnded`, `BadResume` and an unknown session become status codes; then every event goes
  out as `event: <type>\\ndata: <json>\\n\\n`, ending with `done`. The response closes the turn generator however it
  ends (a client disconnect included), so the session's turn lock is released and the trace is exported.
- JSON errors are `{"error": msg}`.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import get_args

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from backend.tutor.db import session_scope
from backend.tutor.learner.store import get_progress
from backend.tutor.models import Resource
from backend.tutor.repo import get_roadmap_by_slug
from backend.tutor.tutor import planner, sessions
from backend.tutor.tutor.runtime import BadResume, Busy, SessionEnded, TutorRuntime
from backend.tutor.tutor.sessions import NotReady
from backend.tutor.tutor.state import Action, Mode, TurnEvent

MAX_TEXT_CHARS = 4000
TURN_KEYS = ("text", "action", "resume")  # a turn has exactly one of these
ACTIONS = get_args(Action)
MODES = get_args(Mode)
UNAVAILABLE = "Database unavailable"
BUSY = "Another turn is running for this session."
ENDED = "This session has ended."
NOT_OBJECT = "The body must be a JSON object."
NO_SPEC = "Roadmap '{slug}' has no approved spec yet."
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class TutorUnavailable(Exception):
    """The tutor runtime is not running."""


def _require_runtime(request: Request) -> None:
    if getattr(request.app.state, "tutor_runtime", None) is None:
        raise TutorUnavailable


async def unavailable_handler(request: Request, exc: TutorUnavailable) -> JSONResponse:
    return _error(503, UNAVAILABLE)


router = APIRouter(prefix="/api", dependencies=[Depends(_require_runtime)])


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _runtime(request: Request) -> TutorRuntime:
    return request.app.state.tutor_runtime


def _known(engine, slug: str) -> bool:
    with session_scope(engine) as s:
        return get_roadmap_by_slug(s, slug) is not None


def _no_roadmap(slug: str) -> JSONResponse:
    return _error(404, f"No roadmap '{slug}'")


async def _json_object(request: Request) -> dict | None:
    """The body as a JSON object ({} when empty), or None when it is not one."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _bounded_text(body: dict, key: str) -> str | None:
    value = body[key]
    if not isinstance(value, str) or not value.strip():
        return f"'{key}' must be a non-empty string."
    if len(value) > MAX_TEXT_CHARS:
        return f"'{key}' is at most {MAX_TEXT_CHARS} characters."
    return None


def turn_error(body: dict) -> str | None:
    """Why `body` is not a turn, or None when it is one."""
    unknown = sorted(set(body) - {*TURN_KEYS, "topic"})
    if unknown:
        return f"Unknown field(s): {', '.join(unknown)}."
    if sum(k in body for k in TURN_KEYS) != 1:
        return "A turn has exactly one of 'text', 'action' or 'resume'."
    if "topic" in body and body.get("action") != "research":
        return "'topic' goes only with \"action\": \"research\"."
    if "action" in body and body["action"] not in ACTIONS:
        return f"'action' must be one of: {', '.join(ACTIONS)}."
    for key in ("text", "topic"):
        if key in body and (reason := _bounded_text(body, key)):
            return reason
    return None


# --- sessions ---------------------------------------------------------------------------------------------

@router.post("/roadmaps/{slug}/sessions", status_code=201)
async def create_session(slug: str, request: Request):
    body = await _json_object(request)
    if body is None:
        return _error(422, NOT_OBJECT)
    if set(body) - {"mode"}:
        return _error(422, f"Unknown field(s): {', '.join(sorted(set(body) - {'mode'}))}.")
    mode = body.get("mode", "tutor")
    if mode not in MODES:
        return _error(422, f"'mode' must be one of: {', '.join(MODES)}.")
    try:
        row = await _runtime(request).create_session(slug, mode)
    except NotReady as exc:
        return _error(409, str(exc))
    except Busy:
        return _error(409, "A turn is still running in this roadmap's active session; try again when it ends.")
    except LookupError:
        return _no_roadmap(slug)
    return {"id": str(row.id), "mode": row.mode, "status": row.status, "roadmap": slug}


@router.get("/roadmaps/{slug}/sessions")
def list_sessions(slug: str, request: Request):
    try:
        return sessions.list_sessions(request.app.state.engine, slug)
    except LookupError:
        return _no_roadmap(slug)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    """The session and its stored state: messages, summary, today's card and the pending interrupt (a reload sees
    the same card again). An ended session whose thread was deleted has empty state."""
    try:
        row = await asyncio.to_thread(sessions.get_session, request.app.state.engine, session_id)
    except LookupError:
        return _error(404, f"No session '{session_id}'")
    return {**row, **await _runtime(request).state(row["id"])}


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str, request: Request):
    try:
        row = await _runtime(request).end_session(session_id)
    except Busy:
        return _error(409, BUSY)
    except LookupError:
        return _error(404, f"No session '{session_id}'")
    return {"id": str(row.id), "status": row.status}


# --- turns ------------------------------------------------------------------------------------------------

def _frame(event: TurnEvent) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


async def _frames(events: AsyncIterator[TurnEvent], first: TurnEvent) -> AsyncIterator[str]:
    async with aclosing(events):
        event = first
        while True:
            yield _frame(event)
            if event["type"] == "done":
                return
            try:
                event = await anext(events)
            except StopAsyncIteration:
                return


class _TurnStream(StreamingResponse):
    """Closes its body when the response ends, however it ends: Starlette leaves a body suspended at a `yield` when
    the client disconnects. The close is shielded from that cancellation, so the turn can finish its bookkeeping."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


@router.post("/sessions/{session_id}/turn")
async def turn(session_id: str, request: Request):
    body = await _json_object(request)
    reason = NOT_OBJECT if body is None else turn_error(body)
    if reason:
        return _error(422, reason)
    events = _runtime(request).run_turn(session_id, body)  # `resume` passes through unchanged
    try:
        first = await anext(events)  # these are raised here, before any graph work
    except Busy:
        return _error(409, BUSY)
    except SessionEnded:
        return _error(409, ENDED)
    except BadResume as exc:
        return _error(422, str(exc))
    except LookupError:
        return _error(404, f"No session '{session_id}'")
    return _TurnStream(_frames(events, first), media_type="text/event-stream", headers=SSE_HEADERS)


# --- skill map, study settings, plan ----------------------------------------------------------------------

def _skill_state(progress: dict) -> str:
    if progress["locked"]:
        return "locked"
    if progress["mastery"] >= planner.MASTERED:
        return "mastered"
    return "learning" if progress["mastery"] > 0 else "not_started"


@router.get("/roadmaps/{slug}/skills")
def skill_map(slug: str, request: Request):
    """Stages and skills in spec order, each skill with its progress, state and resources (dismissed proposals
    left out)."""
    engine = request.app.state.engine
    with session_scope(engine) as s:
        rm = get_roadmap_by_slug(s, slug)
        if rm is None:
            return _no_roadmap(slug)
        stages = (rm.spec or {}).get("stages") or []
        placed = [(st["id"], sk) for st in stages for sk in st.get("skills", [])]
        if not placed:
            return _error(409, NO_SPEC.format(slug=slug))
        rows = s.scalars(select(Resource).where(Resource.roadmap_id == rm.id, Resource.status != "dismissed")
                         .order_by(Resource.key)).all()
        resources = [(set(r.skill_ids or []), {"title": r.title, "url": r.url, "kind": r.kind, "status": r.status})
                     for r in rows]
    progress = {p["skill_id"]: p for p in get_progress(engine, slug)}
    out = []
    for stage_id, sk in placed:
        p = progress[sk["id"]]
        out.append({"id": sk["id"], "title": sk["title"], "stage_id": stage_id,
                    "objectives": sk.get("objectives", []), "prerequisites": sk.get("prerequisites", []),
                    "mastery": p["mastery"], "weak_spots": p["weak_spots"], "locked": p["locked"],
                    "next_review_at": p["next_review_at"], "state": _skill_state(p),
                    "resources": [view for ids, view in resources if sk["id"] in ids]})
    return {"stages": [{"id": st["id"], "title": st["title"]} for st in stages], "skills": out}


@router.get("/roadmaps/{slug}/study-settings")
def get_study_settings(slug: str, request: Request):
    engine = request.app.state.engine
    if not _known(engine, slug):
        return _no_roadmap(slug)
    return planner.get_study_settings(engine, slug)


@router.put("/roadmaps/{slug}/study-settings")
async def put_study_settings(slug: str, request: Request):
    body = await _json_object(request)
    engine = request.app.state.engine
    if not await asyncio.to_thread(_known, engine, slug):
        return _no_roadmap(slug)
    if body is None:
        return _error(422, NOT_OBJECT)
    try:
        return await asyncio.to_thread(lambda: planner.put_study_settings(engine, slug, **body))
    except ValueError as exc:
        return _error(422, str(exc))


@router.get("/roadmaps/{slug}/plan")
def get_plan(slug: str, request: Request):
    """The active plan and its next session's card, as the planner last saved them; nothing is computed or saved."""
    engine = request.app.state.engine
    if not _known(engine, slug):
        return _no_roadmap(slug)
    plan = planner.active_plan(engine, slug)
    return {"plan": plan, "today": planner.today_card(engine, slug, plan) if plan else None}
