import uuid
from datetime import date

import pytest
from sqlalchemy import select

from backend.core import fake_llm
from backend.tutor.db import session_scope
from backend.tutor.learner.store import set_mastery
from backend.tutor.models import Resource, Roadmap, StudyPlan
from backend.tutor.tests.tutor_helpers import (
    SPEC,
    _skill,
    clear_memo,  # noqa: F401 (fixture)
    interrupts,
    make_runtime,
    new_session,
    nodes,
    run,
)
from backend.tutor.tutor import planner
from backend.tutor.tutor.planner import (
    active_plan,
    active_version,
    build_plan,
    get_study_settings,
    is_big_change,
    put_study_settings,
    save_plan,
    today_card,
)
from backend.tutor.tutor.runtime import BadResume

pytestmark = pytest.mark.anyio

MON = date(2026, 11, 23)
SETTINGS = {"weekly_hours": None, "session_minutes": 45, "study_days": [0, 1, 2, 3, 4, 5, 6], "deadline": None}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _spec(skills, weekly_hours=None):
    return {"title": "T", "goals": [], "weekly_hours": weekly_hours,
            "stages": [{"id": "s1", "title": "S", "skills": skills}]}


def _six():
    return _spec([_skill(c) for c in "abcdef"])


def _plan(*ids):
    return {"sessions": [{"index": i, "date": "2026-11-23", "skills": [s], "minutes": 45}
                         for i, s in enumerate(ids)], "skipped_mastered": [], "fits_deadline": None}


def _prog(sid, mastery=0.0):
    return {"skill_id": sid, "title": sid, "mastery": mastery, "weak_spots": [], "locked": False,
            "next_review_at": None}


def _firsts(plan):
    return [s["skills"][0] for s in plan["sessions"]]


def _slug(engine, sid):
    with session_scope(engine) as s:
        from backend.tutor.models import TutorSession
        return s.get(Roadmap, s.get(TutorSession, uuid.UUID(sid)).roadmap_id).slug


# --- build_plan -------------------------------------------------------------------------------------------

def test_prerequisite_before_dependant_even_if_listed_later():
    spec = _spec([_skill("b", ["a"]), _skill("a")])
    assert _firsts(build_plan(spec, [], SETTINGS, MON)) == ["a", "b"]


def test_unknown_prerequisite_is_ignored():
    assert _firsts(build_plan(_spec([_skill("a", ["zzz"])]), [], SETTINGS, MON)) == ["a"]


def test_prerequisite_cycle_is_broken_by_spec_order():
    spec = _spec([_skill("a", ["b"]), _skill("b", ["a"]), _skill("c")])
    assert _firsts(build_plan(spec, [], SETTINGS, MON)) == ["c", "a", "b"]  # c is free; then the cycle by spec order


def test_mastered_skills_skipped():
    plan = build_plan(_spec([_skill(c) for c in "abc"]), [_prog("a", 0.9), _prog("b", 0.84)], SETTINGS, MON)
    assert plan["skipped_mastered"] == ["a"] and _firsts(plan) == ["b", "c"]


def test_sessions_per_skill_from_objectives():
    sk = {"id": "a", "title": "A", "objectives": ["1", "2", "3"], "prerequisites": []}
    assert _firsts(build_plan(_spec([sk, _skill("b")]), [], SETTINGS, MON)) == ["a", "a", "b"]
    assert _firsts(build_plan(_spec([{**sk, "objectives": []}]), [], SETTINGS, MON)) == ["a"]


def test_weekly_hours_caps_sessions_per_week():  # 1.5 h/week, 45-minute sessions: 2 per ISO week
    plan = build_plan(_six(), [], {**SETTINGS, "weekly_hours": 1.5, "study_days": [0, 2, 4]}, MON)
    assert [s["date"] for s in plan["sessions"][:5]] == ["2026-11-23", "2026-11-25", "2026-11-30", "2026-12-02",
                                                          "2026-12-07"]


def test_week_of_today_counts_only_days_from_today():
    wed = date(2026, 11, 25)
    plan = build_plan(_six(), [], {**SETTINGS, "weekly_hours": 1.5, "study_days": [0, 2, 4]}, wed)
    assert [s["date"] for s in plan["sessions"][:3]] == ["2026-11-25", "2026-11-27", "2026-11-30"]


def _dates_of(plan):
    return [s["date"] for s in plan["sessions"]]


def test_weekly_hours_fall_back_to_the_spec():  # spec 0.75 h -> 1 session per week, whatever the study days
    plan = build_plan(_spec([_skill(c) for c in "abcdef"], weekly_hours=0.75), [], SETTINGS, MON)
    assert _dates_of(plan) == ["2026-11-23", "2026-11-30", "2026-12-07", "2026-12-14", "2026-12-21", "2026-12-28"]


def test_weekly_hours_default_to_five_without_setting_or_spec():  # 5 h / 45 min -> 6 per week
    plan = build_plan(_spec([_skill(c) for c in "abcdefg"]), [], SETTINGS, MON)
    assert _dates_of(plan) == [f"2026-11-{d}" for d in (23, 24, 25, 26, 27, 28)] + ["2026-11-30"]


def test_setting_beats_the_spec():
    plan = build_plan(_spec([_skill(c) for c in "abc"], weekly_hours=0.75), [],
                      {**SETTINGS, "weekly_hours": 1.5}, MON)  # 2 per week
    assert _dates_of(plan) == ["2026-11-23", "2026-11-24", "2026-11-30"]


def test_weekend_only_study_days():
    plan = build_plan(_six(), [], {**SETTINGS, "study_days": [5, 6]}, MON)
    assert _dates_of(plan)[:5] == ["2026-11-28", "2026-11-29", "2026-12-05", "2026-12-06", "2026-12-12"]


def test_cap_of_one_with_several_study_days_is_one_per_iso_week():
    plan = build_plan(_six(), [], {**SETTINGS, "weekly_hours": 0.75, "study_days": [1, 3, 5]}, MON)
    assert _dates_of(plan)[:3] == ["2026-11-24", "2026-12-01", "2026-12-08"]


def test_weekly_cap_across_the_year_boundary_uses_iso_weeks():  # 2026-12-31 and 2027-01-01 share ISO week 53
    plan = build_plan(_six(), [], {**SETTINGS, "weekly_hours": 1.5}, date(2026, 12, 31))
    assert _dates_of(plan)[:4] == ["2026-12-31", "2027-01-01", "2027-01-04", "2027-01-05"]


def test_today_counts_only_when_a_study_day():
    plan = build_plan(_six(), [], {**SETTINGS, "study_days": [1]}, MON)
    assert plan["sessions"][0]["date"] == "2026-11-24"


def test_deadline_fit_reported():
    spec = _spec([_skill(c) for c in "abc"])
    assert build_plan(spec, [], SETTINGS, MON)["fits_deadline"] is None
    assert build_plan(spec, [], {**SETTINGS, "deadline": "2026-11-25"}, MON)["fits_deadline"] is True
    assert build_plan(spec, [], {**SETTINGS, "deadline": "2026-11-24"}, MON)["fits_deadline"] is False


def test_big_change_threshold():
    assert is_big_change(_plan("a", "b", "c", "d", "e"), _plan("a", "b", "c", "f", "g")) is True
    assert is_big_change(_plan("a", "b", "c", "d", "e"), _plan("a", "b", "c", "e", "f")) is False
    assert is_big_change(None, _plan("a")) is False


# --- settings ---------------------------------------------------------------------------------------------

def test_settings_defaults_are_not_written(engine):
    slug = _slug(engine, new_session(engine))
    got = get_study_settings(engine, slug)
    assert got == {"weekly_hours": None, "effective_weekly_hours": 3, "session_minutes": 45,
                   "study_days": [0, 1, 2, 3, 4], "deadline": None}
    with session_scope(engine) as s:
        assert s.scalars(select(planner.StudySettings)).all() == []


def test_settings_put_creates_and_merges(engine):
    slug = _slug(engine, new_session(engine))
    out = put_study_settings(engine, slug, study_days=[4, 2, 2, 0], deadline="2027-01-31")
    assert out["study_days"] == [0, 2, 4] and out["deadline"] == "2027-01-31" and out["session_minutes"] == 45
    out = put_study_settings(engine, slug, weekly_hours=7.5)
    assert out["weekly_hours"] == 7.5 and out["effective_weekly_hours"] == 7.5 and out["study_days"] == [0, 2, 4]
    assert put_study_settings(engine, slug, weekly_hours=None, deadline=None)["effective_weekly_hours"] == 3


@pytest.mark.parametrize("fields,name", [
    ({"study_days": []}, "study_days"), ({"study_days": [7]}, "study_days"), ({"study_days": [True]}, "study_days"),
    ({"weekly_hours": 0}, "weekly_hours"), ({"weekly_hours": 80.5}, "weekly_hours"),
    ({"weekly_hours": "5"}, "weekly_hours"), ({"session_minutes": 14}, "session_minutes"),
    ({"session_minutes": 241}, "session_minutes"), ({"session_minutes": 45.5}, "session_minutes"),
    ({"deadline": "soon"}, "deadline"), ({"nope": 1}, "nope")])
def test_settings_validation(engine, fields, name):
    slug = _slug(engine, new_session(engine))
    with pytest.raises(ValueError, match=name):
        put_study_settings(engine, slug, **fields)
    assert get_study_settings(engine, slug)["study_days"] == [0, 1, 2, 3, 4]


def test_settings_unknown_roadmap(engine):
    with pytest.raises(ValueError, match="No roadmap"):
        get_study_settings(engine, "nope")


# --- stored plans -----------------------------------------------------------------------------------------

def _statuses(engine, slug):
    with session_scope(engine) as s:
        rows = s.scalars(select(StudyPlan).join(Roadmap, Roadmap.id == StudyPlan.roadmap_id)
                         .where(Roadmap.slug == slug).order_by(StudyPlan.version)).all()
        return [(r.version, r.status) for r in rows]


def test_save_active_supersedes_active_and_proposed(engine):
    slug = _slug(engine, new_session(engine))
    assert save_plan(engine, slug, _plan("a"), "active") == 1
    assert save_plan(engine, slug, _plan("b"), "proposed") == 2
    assert save_plan(engine, slug, _plan("c"), "proposed") == 3  # supersedes the earlier proposal only
    assert _statuses(engine, slug) == [(1, "active"), (2, "superseded"), (3, "proposed")]
    assert save_plan(engine, slug, _plan("d"), "active") == 4
    assert _statuses(engine, slug) == [(1, "superseded"), (2, "superseded"), (3, "superseded"), (4, "active")]
    assert active_plan(engine, slug)["sessions"][0]["skills"] == ["d"] and active_version(engine, slug) == 4
    with pytest.raises(ValueError):
        save_plan(engine, slug, _plan("a"), "superseded")


def test_save_retries_a_version_clash_once(engine, monkeypatch):
    slug = _slug(engine, new_session(engine))
    save_plan(engine, slug, _plan("a"), "active")
    real, clashed = planner.func.max, []

    def max_then_clash(col):  # another writer takes version 2 right after this save read the maximum
        if not clashed:
            clashed.append(1)
            with session_scope(engine) as s:
                rm = s.scalars(select(Roadmap).where(Roadmap.slug == slug)).one()
                s.add(StudyPlan(roadmap_id=rm.id, version=2, plan=_plan("x"), status="proposed"))
        return real(col)

    monkeypatch.setattr(planner.func, "max", max_then_clash)
    assert save_plan(engine, slug, _plan("b"), "active") == 3
    assert [v for v, st in _statuses(engine, slug) if st == "active"] == [3]


def test_today_card_has_skills_and_done_resources(engine):
    sid = new_session(engine)
    slug = _slug(engine, sid)
    with session_scope(engine) as s:
        rm = s.scalars(select(Roadmap).where(Roadmap.slug == slug)).one()
        for i, status in enumerate(["done", "done", "done", "done", "failed"]):
            s.add(Resource(roadmap_id=rm.id, key=f"r{i}", stage_id="s1", skill_ids=["cache-basics"],
                           title=f"R{i}", kind="doc", url=f"https://x/{i}", status=status))
        s.add(Resource(roadmap_id=rm.id, key="other", stage_id="s1", skill_ids=["eviction"], title="O",
                       kind="doc", url="https://x/o", status="done"))
    set_mastery(engine, slug, "cache-basics", 0.4, ["ttl"])
    card = today_card(engine, slug, build_plan(SPEC, get_progress_for(engine, slug), SETTINGS, MON))
    assert card["session"]["skills"] == ["cache-basics"]
    (sk,) = card["skills"]
    assert sk["id"] == "cache-basics" and sk["title"] == "CACHE-BASICS" and sk["mastery"] == 0.4
    assert sk["weak_spots"] == ["ttl"] and sk["objectives"] == ["understand cache-basics"]
    assert [r["title"] for r in sk["resources"]] == ["R0", "R1", "R2"]
    assert sk["resources"][0] == {"title": "R0", "url": "https://x/0", "kind": "doc"}


def get_progress_for(engine, slug):
    from backend.tutor.learner.store import get_progress
    return get_progress(engine, slug)


def test_today_card_of_an_empty_plan(engine):
    slug = _slug(engine, new_session(engine))
    assert today_card(engine, slug, _plan()) == {"session": None, "skills": []}


# --- the graph --------------------------------------------------------------------------------------------

def _rt(engine):
    return make_runtime(engine)


def _set_spec(engine, slug, spec):
    with session_scope(engine) as s:
        s.scalars(select(Roadmap).where(Roadmap.slug == slug)).one().spec = spec


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(planner, "today_date", lambda: MON)


def _plan_events(events):
    return [e for e in events if e["type"] == "plan"]


async def test_first_plan_is_saved_active_and_emitted(engine, clear_memo):  # noqa: F811
    rt, sid = _rt(engine), new_session(engine)
    slug = _slug(engine, sid)
    events = await run(rt, sid, {"action": "plan"})
    assert nodes(events) == ["coordinator", "planner"] and not interrupts(events)
    (ev,) = _plan_events(events)
    assert ev["version"] == 1 and ev["today"]["session"]["skills"] == ["cache-basics"]
    assert active_plan(engine, slug)["sessions"][0]["date"] == "2026-11-23"
    assert (await rt.state(sid))["today"] == ev["today"] and (await rt.state(sid)).get("proposal") is None
    assert fake_llm.requests == []


async def test_same_sequence_on_a_new_day_does_not_save_but_refreshes_dates(engine, clear_memo, monkeypatch):  # noqa: F811
    rt, sid = _rt(engine), new_session(engine)
    slug = _slug(engine, sid)
    await run(rt, sid, {"action": "plan"})
    monkeypatch.setattr(planner, "today_date", lambda: date(2026, 11, 24))
    (ev,) = _plan_events(await run(rt, sid, {"action": "plan"}))
    assert ev["version"] == 1 and ev["today"]["session"]["date"] == "2026-11-24"
    assert _statuses(engine, slug) == [(1, "active")]


async def test_small_change_applies_silently(engine, clear_memo):  # noqa: F811
    rt, sid = _rt(engine), new_session(engine, spec=_six())
    slug = _slug(engine, sid)
    await run(rt, sid, {"action": "plan"})
    set_mastery(engine, slug, "a", 0.9, None)  # a leaves: only one skill (f) enters the next five
    events = await run(rt, sid, {"action": "plan"})
    assert not interrupts(events) and _plan_events(events)[0]["version"] == 2
    assert _firsts(active_plan(engine, slug)) == list("bcdef")
    assert _statuses(engine, slug) == [(1, "superseded"), (2, "active")]


async def _big(engine):
    rt, sid = _rt(engine), new_session(engine, spec=_six())
    slug = _slug(engine, sid)
    await run(rt, sid, {"action": "plan"})
    set_mastery(engine, slug, "a", 0.9, None)
    set_mastery(engine, slug, "b", 0.9, None)
    return rt, sid, slug


async def test_big_replan_waits_for_approval_and_shows_payload(engine, clear_memo):  # noqa: F811
    rt, sid, slug = await _big(engine)
    events = await run(rt, sid, {"action": "plan"})
    assert nodes(events) == ["coordinator", "planner", "planner_approve"] and not _plan_events(events)
    (card,) = interrupts(events)
    assert card == {"kind": "approval", "action": "replace_plan",
                    "summary": "Your plan changed: 2 skills differ in the next 5 sessions. Use the new plan?",
                    "payload": {"old": [[c] for c in "abcde"], "new": [[c] for c in "cdef"], "version": 2}}
    assert _statuses(engine, slug) == [(1, "active"), (2, "proposed")]
    events = await run(rt, sid, {"resume": {"approved": False}})
    (ev,) = _plan_events(events)
    assert ev["version"] == 1 and ev["today"]["session"]["skills"] == ["a"]
    assert _firsts(active_plan(engine, slug)) == list("abcdef")
    assert _statuses(engine, slug) == [(1, "active"), (2, "superseded")]
    state = await rt.state(sid)
    assert state.get("proposal") is None and state["today"] == ev["today"] and await rt.pending(sid) is None
    assert fake_llm.requests == []


async def test_approving_makes_the_proposal_active(engine, clear_memo):  # noqa: F811
    rt, sid, slug = await _big(engine)
    await run(rt, sid, {"action": "plan"})
    events = await run(rt, sid, {"resume": {"approved": True}})
    (ev,) = _plan_events(events)
    assert ev["version"] == 2 and ev["today"]["session"]["skills"] == ["c"]
    assert _firsts(active_plan(engine, slug)) == list("cdef")
    assert _statuses(engine, slug) == [(1, "superseded"), (2, "active")]


@pytest.mark.parametrize("bad", [{}, {"approved": "yes"}, {"approved": 1}, {"approved": True, "x": 1},
                                 {"choice": "skip"}, True])
async def test_approval_resume_shape_is_strict(engine, clear_memo, bad):  # noqa: F811
    rt, sid, _ = await _big(engine)
    await run(rt, sid, {"action": "plan"})
    with pytest.raises(BadResume):
        await run(rt, sid, {"resume": bad})
    assert await rt.pending(sid) is not None


async def test_abandoned_card_leaves_no_stale_proposal(engine, clear_memo):  # noqa: F811
    rt, sid, slug = await _big(engine)
    await run(rt, sid, {"action": "plan"})  # card shown, then abandoned by the next action
    set_mastery(engine, slug, "a", 0.0, None)
    set_mastery(engine, slug, "b", 0.0, None)  # back to the active plan: nothing changed
    events = await run(rt, sid, {"action": "plan"})
    assert not interrupts(events) and _plan_events(events)[0]["version"] == 1
    assert (await rt.state(sid)).get("proposal") is None


async def test_planner_entered_from_another_node(engine, clear_memo):  # noqa: F811
    rt = make_runtime(engine, {"assessor": lambda b, d: _grader_group(b)})
    sid = new_session(engine)
    events = await run(rt, sid, {"action": "quiz"})
    assert nodes(events) == ["coordinator", "grader", "planner"] and len(_plan_events(events)) == 1


def _grader_group(builder):
    from backend.tutor.tutor.graph import Group, node

    async def grader(state):
        return None

    builder.add_node("grader", node("grader", grader))

    def wire(builder, entries):
        builder.add_edge("grader", planner.ENTRY)

    return Group("grader", None, wire)
