"""The planner: a deterministic study plan (D2), study settings, and approval of big replans. No model call.

Group `planner`: `planner` (computes the plan; reads and writes the plan tables) -> `planner_approve` only when the
change is big, else the turn ends. `planner_approve` interrupts first (D8) with an approval card and resumes with
`{"approved": bool}` (`RESUME_SHAPES["approval"]`). Enter the group through `ENTRY` from any node (Task 10 does after
grading). The planner owns `state.today` and overwrites `state.proposal` unconditionally.

Plan shape: `{"sessions": [{"index", "date", "skills": [id], "minutes"}], "skipped_mastered": [id],
"fits_deadline": bool | None}`; one skill per session, in prerequisite-consistent spec order.
"""
from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from backend.tutor.db import session_scope
from backend.tutor.learner.store import get_progress, skills
from backend.tutor.models import Resource, Roadmap, StudyPlan, StudySettings
from backend.tutor.repo import get_roadmap_by_slug
from backend.tutor.tutor.graph import Group, TutorDeps, emit, node
from backend.tutor.tutor.state import TutorState

ENTRY = "planner"
MASTERED = 0.85  # a skill at or above this mastery is left out of the plan
HORIZON = 5  # sessions compared when deciding whether a replan is big
BIG_CHANGE = 2  # a replan is big when this many of the next HORIZON sessions' skills were replaced
DEFAULT_WEEKLY_HOURS = 5
MAX_RESOURCES = 3
_FIELDS = ("weekly_hours", "session_minutes", "study_days", "deadline")


def today_date() -> date:
    return date.today()


# --- study settings ---------------------------------------------------------------------------------------

def _roadmap(s, slug: str) -> Roadmap:
    rm = get_roadmap_by_slug(s, slug)
    if rm is None:
        raise ValueError(f"No roadmap '{slug}'")
    return rm


def _view(rm: Roadmap, row: StudySettings | None) -> dict:
    stored = row.weekly_hours if row else None
    return {"weekly_hours": stored,
            "effective_weekly_hours": stored or (rm.spec or {}).get("weekly_hours") or DEFAULT_WEEKLY_HOURS,
            "session_minutes": row.session_minutes if row else 45,
            "study_days": sorted(row.study_days) if row else [0, 1, 2, 3, 4],
            "deadline": row.deadline.isoformat() if row and row.deadline else None}


def get_study_settings(engine: Engine, slug: str) -> dict:
    """The roadmap's settings, or the defaults when none are stored (nothing is written). `weekly_hours` is the
    stored value (None: not set); `effective_weekly_hours` is what the planner uses."""
    with session_scope(engine) as s:
        rm = _roadmap(s, slug)
        return _view(rm, s.get(StudySettings, rm.id))


def _number(field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    return value


def _clean_field(field: str, value: Any) -> Any:
    if field == "weekly_hours":
        if value is None:
            return None
        if not 0 < _number(field, value) <= 80:
            raise ValueError("weekly_hours must be more than 0 and at most 80")
        return float(value)
    if field == "session_minutes":
        if isinstance(value, bool) or not isinstance(value, int) or not 15 <= value <= 240:
            raise ValueError("session_minutes must be a whole number from 15 to 240")
        return value
    if field == "study_days":
        if (not isinstance(value, (list, tuple)) or not value
                or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in value)):
            raise ValueError("study_days must be a non-empty list of weekday numbers, 0 (Monday) to 6 (Sunday)")
        return sorted(set(value))
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("deadline must be an ISO date (YYYY-MM-DD) or null") from None


def put_study_settings(engine: Engine, slug: str, **fields: Any) -> dict:
    """Set the given fields (others keep their value), creating the row with the defaults if needed."""
    unknown = sorted(set(fields) - set(_FIELDS))
    if unknown:
        raise ValueError(f"Unknown settings field(s): {', '.join(unknown)}")
    clean = {f: _clean_field(f, v) for f, v in fields.items()}
    with session_scope(engine) as s:
        rm = _roadmap(s, slug)
        row = s.get(StudySettings, rm.id)
        if row is None:
            row = StudySettings(roadmap_id=rm.id, session_minutes=45, study_days=[0, 1, 2, 3, 4])
            s.add(row)
        for f, v in clean.items():
            setattr(row, f, v)
        s.flush()
        return _view(rm, row)


# --- building a plan --------------------------------------------------------------------------------------

def _ordered(all_skills: list[dict]) -> list[dict]:
    """Spec order made consistent with prerequisites: repeatedly take the first unplaced skill whose known
    prerequisites are all placed (unknown ids are ignored). When none qualifies (a prerequisite cycle), take the
    first unplaced skill in spec order, which breaks the cycle there."""
    known = {sk["id"] for sk in all_skills}
    placed: set[str] = set()
    remaining, out = list(all_skills), []
    while remaining:
        pick = next((sk for sk in remaining
                     if all(p in placed for p in sk.get("prerequisites", []) if p in known and p != sk["id"])),
                    remaining[0])
        remaining.remove(pick)
        placed.add(pick["id"])
        out.append(pick)
    return out


def _dates(count: int, study_days: set[int], per_week: int, start: date) -> Iterator[date]:
    """`count` study dates from `start` (inclusive): study days only, at most `per_week` in each ISO week."""
    used: dict[tuple[int, int], int] = {}
    day = start
    while count > 0:
        week = day.isocalendar()[:2]
        if day.weekday() in study_days and used.get(week, 0) < per_week:
            used[week] = used.get(week, 0) + 1
            count -= 1
            yield day
        day += timedelta(days=1)


def _as_date(value: Any) -> date | None:
    return value if value is None or isinstance(value, date) else date.fromisoformat(value)


def build_plan(spec: dict, progress: list[dict], settings: dict, today: date) -> dict:
    minutes = settings.get("session_minutes") or 45
    weekly_hours = settings.get("weekly_hours") or spec.get("weekly_hours") or DEFAULT_WEEKLY_HOURS
    study_days = set(settings.get("study_days") or [0, 1, 2, 3, 4])
    mastery = {p["skill_id"]: p["mastery"] for p in progress}
    skipped, todo = [], []
    for sk in _ordered(skills(spec)):
        (skipped if mastery.get(sk["id"], 0.0) >= MASTERED else todo).append(sk)
    queue = [sk["id"] for sk in todo for _ in range(max(1, math.ceil(len(sk.get("objectives", [])) / 2)))]
    per_week = max(1, math.floor(weekly_hours * 60 / minutes))
    days = list(_dates(len(queue), study_days, per_week, today))
    sessions = [{"index": i, "date": d.isoformat(), "skills": [sid], "minutes": minutes}
                for i, (sid, d) in enumerate(zip(queue, days))]
    deadline = _as_date(settings.get("deadline"))
    fits = None if deadline is None else (not days or days[-1] <= deadline)
    return {"sessions": sessions, "skipped_mastered": [sk["id"] for sk in skipped], "fits_deadline": fits}


def _next_skills(plan: dict) -> list[list[str]]:
    return [s["skills"] for s in plan["sessions"][:HORIZON]]


def replaced_count(old: dict, new: dict) -> int:
    """How many skills of the next HORIZON sessions were swapped: the larger of the skills only in `new` and the
    skills only in `old`."""
    a = {sid for s in _next_skills(old) for sid in s}
    b = {sid for s in _next_skills(new) for sid in s}
    return max(len(b - a), len(a - b))


def is_big_change(old: dict | None, new: dict) -> bool:
    return old is not None and replaced_count(old, new) >= BIG_CHANGE


def _sequence(plan: dict) -> list[list[str]]:
    return [s["skills"] for s in plan["sessions"]]


# --- stored plans -----------------------------------------------------------------------------------------

def _row(s, rm: Roadmap, status: str) -> StudyPlan | None:
    return s.scalars(select(StudyPlan).where(StudyPlan.roadmap_id == rm.id, StudyPlan.status == status)
                     .order_by(StudyPlan.version.desc())).first()


def active_plan(engine: Engine, slug: str) -> dict | None:
    with session_scope(engine) as s:
        row = _row(s, _roadmap(s, slug), "active")
        return row.plan if row else None


def active_version(engine: Engine, slug: str) -> int | None:
    with session_scope(engine) as s:
        row = _row(s, _roadmap(s, slug), "active")
        return row.version if row else None


def proposed_plan(engine: Engine, slug: str) -> tuple[int, dict] | None:
    with session_scope(engine) as s:
        row = _row(s, _roadmap(s, slug), "proposed")
        return (row.version, row.plan) if row else None


def save_plan(engine: Engine, slug: str, plan: dict, status: str) -> int:
    """Store `plan` as the next version and return it. `active` supersedes the previous active plan and any
    proposed one; `proposed` supersedes an earlier proposed one. A concurrent save that took the same version is
    retried once."""
    if status not in ("active", "proposed"):
        raise ValueError("status must be 'active' or 'proposed'")
    replaced = ("active", "proposed") if status == "active" else ("proposed",)
    for attempt in (0, 1):
        try:
            with session_scope(engine) as s:
                rm = _roadmap(s, slug)
                version = (s.scalar(select(func.max(StudyPlan.version)).where(StudyPlan.roadmap_id == rm.id))
                           or 0) + 1
                s.execute(update(StudyPlan).where(StudyPlan.roadmap_id == rm.id, StudyPlan.status.in_(replaced))
                          .values(status="superseded"))
                s.add(StudyPlan(roadmap_id=rm.id, version=version, plan=plan, status=status))
            return version
        except IntegrityError:
            if attempt:
                raise
    raise AssertionError("unreachable")


def resolve_proposed(engine: Engine, slug: str, version: int, approved: bool) -> None:
    """The learner's answer: the proposed plan `version` becomes active (the old one is superseded) or is itself
    superseded. A version that is no longer proposed is left alone."""
    with session_scope(engine) as s:
        rm = _roadmap(s, slug)
        row = s.scalars(select(StudyPlan).where(StudyPlan.roadmap_id == rm.id, StudyPlan.version == version,
                                                StudyPlan.status == "proposed")).first()
        if row is None:
            return
        if approved:
            s.execute(update(StudyPlan).where(StudyPlan.roadmap_id == rm.id, StudyPlan.status == "active")
                      .values(status="superseded"))
        row.status = "active" if approved else "superseded"


def today_card(engine: Engine, slug: str, plan: dict) -> dict:
    """The next session of `plan` with what the learner needs for it. With nothing left to plan, `session` is
    None and `skills` is empty."""
    if not plan["sessions"]:
        return {"session": None, "skills": []}
    session = plan["sessions"][0]
    progress = {p["skill_id"]: p for p in get_progress(engine, slug)}
    with session_scope(engine) as s:
        rm = _roadmap(s, slug)
        by_id = {sk["id"]: sk for sk in skills(rm.spec)}
        cards = []
        for sid in session["skills"]:
            rows = s.scalars(select(Resource).where(Resource.roadmap_id == rm.id, Resource.status == "done",
                                                    Resource.skill_ids.contains([sid]))
                             .order_by(Resource.key).limit(MAX_RESOURCES)).all()
            cards.append({"id": sid, "title": by_id[sid]["title"], "objectives": by_id[sid].get("objectives", []),
                          "mastery": progress[sid]["mastery"], "weak_spots": progress[sid]["weak_spots"],
                          "resources": [{"title": r.title, "url": r.url, "kind": r.kind} for r in rows]})
    return {"session": session, "skills": cards}


# --- graph ------------------------------------------------------------------------------------------------

def _roadmap_spec(engine: Engine, slug: str) -> dict:
    with session_scope(engine) as s:
        return _roadmap(s, slug).spec


def add_planner(builder: StateGraph, deps: TutorDeps) -> Group:
    engine = deps.engine

    def finish(slug: str, plan: dict, version: int) -> dict:
        card = today_card(engine, slug, plan)
        emit({"type": "plan", "today": card, "version": version})
        return {"today": card, "proposal": None}

    async def planner(state: TutorState) -> dict:
        slug = state["roadmap"]
        new = build_plan(_roadmap_spec(engine, slug), get_progress(engine, slug),
                         get_study_settings(engine, slug), today_date())
        old, version = active_plan(engine, slug), active_version(engine, slug)
        if old is not None and is_big_change(old, new):
            proposed = save_plan(engine, slug, new, "proposed")
            n = replaced_count(old, new)
            return {"proposal": {
                "kind": "approval", "action": "replace_plan",
                "summary": f"Your plan changed: {n} skills differ in the next {HORIZON} sessions. Use the new plan?",
                "payload": {"old": _next_skills(old), "new": _next_skills(new), "version": proposed}}}
        if old is None or _sequence(old) != _sequence(new):
            version = save_plan(engine, slug, new, "active")
        return finish(slug, new, version)

    async def approve(state: TutorState) -> dict:
        card = state["proposal"]
        answer = interrupt(card)  # first statement (D8)
        slug = state["roadmap"]
        resolve_proposed(engine, slug, card["payload"]["version"], answer["approved"])
        return finish(slug, active_plan(engine, slug), active_version(engine, slug))

    builder.add_node("planner", node("planner", planner))
    builder.add_node("planner_approve", node("planner_approve", approve))

    def wire(builder: StateGraph, entries: dict[str, str]) -> None:
        builder.add_conditional_edges("planner", lambda s: "planner_approve" if s.get("proposal") else END,
                                      {END: END, "planner_approve": "planner_approve"})
        builder.add_edge("planner_approve", END)

    return Group(ENTRY, None, wire)
