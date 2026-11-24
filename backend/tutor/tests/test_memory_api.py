"""The Memory & privacy API through the app: consent and purge, facts, progress reset, session delete, export."""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from backend.tutor.api import create_app
from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.learner import store
from backend.tutor.models import LearnerFact, LearnerSettings, SkillProgress, StudyPlan, TutorSession
from backend.tutor.repo import create_roadmap
from backend.tutor.settings import Settings
from backend.tutor.tests.clients import local_client
from backend.tutor.tests.tutor_helpers import SPEC
from backend.tutor.tutor.checkpoint import open_saver
from backend.tutor.tutor.state import TutorState

EMBEDDER = HashEmbedder()


@pytest.fixture(autouse=True)
def _clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
        s.execute(text("DELETE FROM learner_facts"))
        s.execute(text("DELETE FROM learner_settings"))
    yield


@pytest.fixture
def client(engine, tmp_path):
    app = create_app(Settings(web_dist=tmp_path / "no-dist", embedder="hash"), engine=engine)
    with local_client(app) as c:
        yield c


@pytest.fixture
def no_saver_client(engine, tmp_path):
    app = create_app(Settings(web_dist=tmp_path / "no-dist", embedder="hash"), engine=engine)
    with local_client(app) as c:
        c.app.state.saver = None
        yield c


def _consent(engine, personal=None, raw=None, days=90):
    with session_scope(engine) as s:
        s.merge(LearnerSettings(id=1, consent_personal=personal, consent_raw_messages=raw, retention_days=days))


def _roadmap(engine, slug="rm", spec=SPEC) -> uuid.UUID:
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=slug, title="Caching", source_text="x")
        rm.spec, rm.status = spec, "ready"
        s.flush()
        return rm.id


def _session(engine, rm_id, status="ended", **kw) -> str:
    with session_scope(engine) as s:
        row = TutorSession(roadmap_id=rm_id, mode="tutor", status=status,
                           ended_at=datetime.now(timezone.utc) if status == "ended" else None, **kw)
        s.add(row)
        s.flush()
        return str(row.id)


def _seed(db_url, sid, messages):
    async def go():
        async with open_saver(db_url) as saver:
            g = StateGraph(TutorState)
            g.add_node("n", lambda s: {"messages": list(messages)})
            g.add_edge(START, "n")
            g.add_edge("n", END)
            await g.compile(checkpointer=saver).ainvoke({}, {"configurable": {"thread_id": sid}})

    asyncio.run(go())


def _has_thread(db_url, sid) -> bool:
    async def go():
        async with open_saver(db_url) as saver:
            return await saver.aget_tuple({"configurable": {"thread_id": sid}}) is not None

    return asyncio.run(go())


def _fact(engine, text_, kind="fact", slug=None) -> str:
    return store.remember(engine, EMBEDDER, text_, kind, slug)["id"]


# --- settings ---------------------------------------------------------------------------------------------

def test_settings_default_and_put_creates(client, engine):
    assert client.get("/api/learner/settings").json() == {
        "consent_personal": None, "consent_raw_messages": None, "retention_days": 90}
    r = client.put("/api/learner/settings", json={"consent_personal": True, "retention_days": 30})
    assert r.status_code == 200 and r.json() == {
        "consent_personal": True, "consent_raw_messages": None, "retention_days": 30}
    assert client.get("/api/learner/settings").json() == r.json()


@pytest.mark.parametrize("body", [{"nope": 1}, {"retention_days": 0}, {"retention_days": 3651},
                                  {"retention_days": True}, {"retention_days": "9"}, {"retention_days": 1.5},
                                  {"consent_personal": "yes"}, {"confirm_purge": 1}])
def test_settings_rejects_bad_bodies(client, body):
    assert client.put("/api/learner/settings", json=body).status_code == 422


def test_settings_rejects_a_non_object(client):
    assert client.put("/api/learner/settings", json=[1]).status_code == 422


def test_revoking_personal_consent_needs_confirmation_then_purges(client, engine):
    _consent(engine, True, None)
    _fact(engine, "likes diagrams")
    _fact(engine, "wants an SRE job", "goal")
    r = client.put("/api/learner/settings", json={"consent_personal": False})
    assert r.status_code == 409 and "deletes 2 remembered facts" in r.json()["error"]
    assert client.get("/api/learner/settings").json()["consent_personal"] is True  # nothing changed
    r = client.put("/api/learner/settings", json={"consent_personal": False, "confirm_purge": True})
    assert r.status_code == 200 and r.json()["consent_personal"] is False
    assert client.get("/api/learner/facts").json() == []
    with session_scope(engine) as s:
        assert s.query(LearnerFact).count() == 0


def test_revoke_from_null_needs_no_confirmation(client, engine):
    r = client.put("/api/learner/settings", json={"consent_personal": False, "confirm_purge": True})
    assert r.status_code == 200 and r.json()["consent_personal"] is False
    _consent(engine, None, None)
    assert client.put("/api/learner/settings", json={"consent_personal": False}).status_code == 200


def test_confirm_purge_with_nothing_to_purge_is_fine(client, engine):
    _consent(engine, True)
    assert client.put("/api/learner/settings", json={"consent_personal": False, "confirm_purge": True}
                      ).status_code == 200


def test_revoking_raw_messages_deletes_kept_threads(client, engine, db_url):
    _consent(engine, True, True)
    rm = _roadmap(engine)
    kept, other = _session(engine, rm, raw_kept=True), _session(engine, rm, raw_kept=False)
    active = _session(engine, rm, status="active", raw_kept=True)
    for sid in (kept, other, active):
        _seed(db_url, sid, [{"role": "user", "content": "hi"}])
    r = client.put("/api/learner/settings", json={"consent_raw_messages": False})
    assert r.status_code == 200 and r.json()["consent_raw_messages"] is False
    assert not _has_thread(db_url, kept) and _has_thread(db_url, other) and _has_thread(db_url, active)
    with session_scope(engine) as s:
        assert s.get(TutorSession, uuid.UUID(kept)).raw_kept is False
        assert s.get(TutorSession, uuid.UUID(active)).raw_kept is True  # the curator decides at its end


def test_raw_revoke_without_saver_is_503_and_changes_nothing(no_saver_client, engine):
    _consent(engine, True, True)
    r = no_saver_client.put("/api/learner/settings", json={"consent_raw_messages": False, "retention_days": 5})
    assert r.status_code == 503 and r.json() == {"error": "Database unavailable"}
    assert no_saver_client.get("/api/learner/settings").json() == {
        "consent_personal": True, "consent_raw_messages": True, "retention_days": 90}


# --- facts ------------------------------------------------------------------------------------------------

def test_facts_listing_by_roadmap(client, engine):
    _roadmap(engine, "rm")
    _roadmap(engine, "other")
    _consent(engine, True)
    _fact(engine, "general fact")
    _fact(engine, "rm fact", slug="rm")
    _fact(engine, "other fact", slug="other")
    allf = client.get("/api/learner/facts").json()
    assert [(f["text"], f["roadmap"]) for f in allf] == [
        ("general fact", None), ("rm fact", "rm"), ("other fact", "other")]
    assert set(allf[0]) == {"id", "kind", "text", "roadmap", "created_at", "updated_at"}
    scoped = client.get("/api/learner/facts?roadmap=rm").json()
    assert [f["text"] for f in scoped] == ["general fact", "rm fact"]
    assert client.get("/api/learner/facts?roadmap=nope").status_code == 404
    _consent(engine, False)
    assert client.get("/api/learner/facts").json() == []
    assert client.get("/api/learner/facts?roadmap=rm").json() == []


def test_edit_fact_redacts_and_keeps_history(client, engine):
    _consent(engine, True)
    fid = _fact(engine, "likes videos", "preference")
    r = client.put(f"/api/learner/facts/{fid}", json={"text": "mail me at a@b.io", "kind": "goal"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == fid and body["kind"] == "goal" and body["text"] == "mail me at [email]"
    assert body["roadmap"] is None
    with session_scope(engine) as s:
        fact = s.get(LearnerFact, uuid.UUID(fid))
        assert [h["text"] for h in fact.provenance["history"]] == ["likes videos"]


def test_edit_fact_errors(client, engine):
    _consent(engine, True)
    fid = _fact(engine, "likes videos")
    ok = {"text": "t", "kind": "fact"}
    assert client.put(f"/api/learner/facts/{uuid.uuid4()}", json=ok).status_code == 404
    assert client.put("/api/learner/facts/not-a-uuid", json=ok).status_code == 404
    assert client.put(f"/api/learner/facts/{fid}", json={"text": "x" * 1001, "kind": "fact"}).status_code == 422
    assert client.put(f"/api/learner/facts/{fid}", json={"text": "t", "kind": "mood"}).status_code == 422
    assert client.put(f"/api/learner/facts/{fid}", json={"text": "", "kind": "fact"}).status_code == 422
    assert client.put(f"/api/learner/facts/{fid}", json={"text": "t"}).status_code == 422
    assert client.put(f"/api/learner/facts/{fid}", json={**ok, "x": 1}).status_code == 422
    _consent(engine, False)
    r = client.put(f"/api/learner/facts/{fid}", json=ok)
    assert r.status_code == 409 and r.json() == {"error": store.CONSENT_OFF}


def test_delete_fact_works_without_consent(client, engine):
    _consent(engine, True)
    fid = _fact(engine, "gone soon")
    _consent(engine, False)
    assert client.delete(f"/api/learner/facts/{fid}").status_code == 204
    assert client.delete(f"/api/learner/facts/{fid}").status_code == 404
    assert client.delete("/api/learner/facts/bad").status_code == 404


# --- progress and sessions --------------------------------------------------------------------------------

def test_reset_skill_progress(client, engine):
    rm = _roadmap(engine)
    with session_scope(engine) as s:
        s.add(SkillProgress(roadmap_id=rm, skill_id="cache-basics", mastery=0.8))
    assert client.delete("/api/roadmaps/rm/progress/cache-basics").status_code == 204
    with session_scope(engine) as s:
        assert s.query(SkillProgress).count() == 0
    assert client.delete("/api/roadmaps/rm/progress/cache-basics").status_code == 204  # known skill, no row
    assert client.delete("/api/roadmaps/rm/progress/nope").status_code == 404
    assert client.delete("/api/roadmaps/none/progress/cache-basics").status_code == 404


def test_delete_active_session_refused(client, engine, db_url):
    sid = _session(engine, _roadmap(engine), status="active")
    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 409 and r.json() == {"error": "This session is still active; end it first."}
    with session_scope(engine) as s:
        assert s.get(TutorSession, uuid.UUID(sid)) is not None


def test_delete_session_removes_row_and_thread(client, engine, db_url):
    sid = _session(engine, _roadmap(engine), raw_kept=True)
    _seed(db_url, sid, [{"role": "user", "content": "hi"}])
    assert client.delete(f"/api/sessions/{sid}").status_code == 204
    assert not _has_thread(db_url, sid)
    with session_scope(engine) as s:
        assert s.get(TutorSession, uuid.UUID(sid)) is None
    assert client.delete(f"/api/sessions/{sid}").status_code == 404
    assert client.delete("/api/sessions/zzz").status_code == 404


def test_delete_session_without_saver_is_503(no_saver_client, engine):
    sid = _session(engine, _roadmap(engine))
    r = no_saver_client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 503 and r.json() == {"error": "Database unavailable"}
    with session_scope(engine) as s:
        assert s.get(TutorSession, uuid.UUID(sid)) is not None


# --- export -----------------------------------------------------------------------------------------------

@pytest.fixture
def populated(engine, db_url):
    _consent(engine, True, True)
    rm = _roadmap(engine)
    _roadmap(engine, "bare", spec=None)
    _fact(engine, "likes diagrams", slug="rm")
    with session_scope(engine) as s:
        s.add(SkillProgress(roadmap_id=rm, skill_id="cache-basics", mastery=0.5))
        s.add(StudyPlan(roadmap_id=rm, version=1, plan={"sessions": []}, status="active"))
    kept = _session(engine, rm, raw_kept=True, summary="Day 1", cost_usd=0.0123)
    plain = _session(engine, rm, raw_kept=False)
    _seed(db_url, kept, [{"role": "user", "content": "my key is sk-ant-abcdefgh12345678"}])
    return kept, plain


def test_export_contains_everything_and_no_secrets(client, engine, populated):
    kept, plain = populated
    body = client.get("/api/learner/export")
    assert body.status_code == 200 and body.headers["content-type"].startswith("application/json")
    assert body.headers["content-disposition"] == 'attachment; filename="roadmap-tutor-export.json"'
    data = body.json()
    assert {"exported_at", "settings", "facts", "progress", "sessions", "study_settings", "plans"} <= set(data)
    assert datetime.fromisoformat(data["exported_at"]).tzinfo is not None
    assert data["settings"] == client.get("/api/learner/settings").json()
    assert [f["text"] for f in data["facts"]] == ["likes diagrams"] and "embedding" not in data["facts"][0]
    assert set(data["progress"]) == {"rm"}
    assert {p["skill_id"]: p["mastery"] for p in data["progress"]["rm"]}["cache-basics"] == 0.5
    by_id = {s["id"]: s for s in data["sessions"]}
    assert by_id[kept]["messages"] == [{"role": "user", "content": "my key is [secret]"}]
    assert by_id[kept]["cost_usd"] == 0.0123 and by_id[kept]["roadmap"] == "rm" and by_id[kept]["mode"] == "tutor"
    assert "messages" not in by_id[plain] and "messages_unavailable" not in by_id[plain]
    assert set(data["study_settings"]) == {"rm", "bare"}
    assert data["plans"]["rm"][0]["version"] == 1 and data["plans"]["bare"] == []
    assert "sk-ant-" not in body.text


def test_export_lists_facts_even_without_consent(client, engine, populated):
    _consent(engine, False, True)
    assert len(client.get("/api/learner/export").json()["facts"]) == 1


def test_export_without_saver_marks_messages_unavailable(no_saver_client, engine, populated):
    kept, _ = populated
    data = no_saver_client.get("/api/learner/export").json()
    row = next(s for s in data["sessions"] if s["id"] == kept)
    assert row["messages_unavailable"] is True and "messages" not in row
