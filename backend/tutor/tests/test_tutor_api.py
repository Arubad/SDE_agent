"""The tutor HTTP API: sessions, SSE turns, the skill map, study settings and the plan, through the app."""
import asyncio
import contextlib
import json
import threading
import uuid
from contextlib import asynccontextmanager

import anyio
import pytest
from sqlalchemy import text
from starlette.requests import ClientDisconnect

from backend.agent import access
from backend.core import config, fake_llm
from backend.core.fake_llm import FakeReply
from backend.tutor import api
from backend.tutor.api import create_app
from backend.tutor.db import session_scope
from backend.tutor.learner import store
from backend.tutor.models import Document, Resource, Section
from backend.tutor.repo import create_roadmap
from backend.tutor.settings import Settings
from backend.tutor.tests.clients import local_client
from backend.tutor.tests.tutor_helpers import SPEC, _skill, clear_memo  # noqa: F401 (fixture)
from backend.tutor.tutor import runtime as runtime_mod
from backend.tutor.tutor.graph import single
from backend.tutor.tutor.session_start import NO_SESSION_TEXT

NOT_READY_TAIL = "the tutor opens once ingestion is ready."
BUSY = "Another turn is running for this session."


@pytest.fixture(autouse=True)
def _clean(engine):
    access._failures.clear()
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
    yield
    access._failures.clear()


def _app(engine, tmp_path, **kw):
    settings = Settings(web_dist=tmp_path / "no-dist", **kw.pop("settings", {}))
    return create_app(settings, engine=engine, **kw)


@pytest.fixture
def client(engine, tmp_path):
    with local_client(_app(engine, tmp_path)) as c:
        yield c


def _roadmap(engine, slug="rm", *, status="ready", spec=SPEC) -> uuid.UUID:
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=slug, title="Caching", source_text="x")
        rm.spec, rm.status = spec, status
        s.flush()
        return rm.id


def _mcq(n):
    return {"type": "mcq", "prompt": f"Q{n}?", "options": [f"a{n}", f"b{n}", f"c{n}"], "answer": f"b{n}",
            "explanation": f"because {n}", "concept": f"concept{n}"}


def _add_quizzes(engine, rm_id, skill="cache-basics"):
    """Four stored questions for `skill`, so a quiz needs no model or gateway call."""
    with session_scope(engine) as s:
        doc = Document(roadmap_id=rm_id, url="https://docs.example/a", kind="page", title="Doc")
        s.add(doc)
        s.flush()
        for i, quiz in enumerate([[_mcq(1), _mcq(2)], [_mcq(3), _mcq(4)]]):
            s.add(Section(id=f"{doc.id}:{i}", roadmap_id=rm_id, document_id=doc.id, skill_ids=[skill],
                          text="t", ord=i, quiz=quiz))


def _sse(r) -> list[tuple[str, dict]]:
    assert r.status_code == 200, r.text
    out = []
    for block in r.text.split("\n\n"):
        if block.strip():
            fields = dict(line.split(": ", 1) for line in block.split("\n"))
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def _start(c, slug="rm", mode="tutor") -> str:
    r = c.post(f"/api/roadmaps/{slug}/sessions", json={"mode": mode})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _turn(c, sid, body):
    return c.post(f"/api/sessions/{sid}/turn", json=body)


# --- availability and guards ------------------------------------------------------------------------------

def test_tutor_routes_503_without_checkpointer(engine, tmp_path, monkeypatch):
    @asynccontextmanager
    async def down(url):
        raise OSError("checkpointer down")
        yield

    monkeypatch.setattr(api, "open_saver", down)
    _roadmap(engine)
    with local_client(_app(engine, tmp_path)) as c:
        assert c.get("/api/roadmaps").status_code == 200  # the rest of the API still works
        for r in (c.post("/api/roadmaps/rm/sessions", json={}), c.get("/api/roadmaps/rm/sessions"),
                  c.get(f"/api/sessions/{uuid.uuid4()}"), _turn(c, str(uuid.uuid4()), {"action": "start"}),
                  c.post(f"/api/sessions/{uuid.uuid4()}/end"), c.get("/api/roadmaps/rm/skills"),
                  c.get("/api/roadmaps/rm/study-settings"), c.put("/api/roadmaps/rm/study-settings", json={}),
                  c.get("/api/roadmaps/rm/plan")):
            assert r.status_code == 503 and r.json() == {"error": "Database unavailable"}


def test_tutor_route_needs_the_passcode_in_public_mode(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "pw123456")
    _roadmap(engine)
    with local_client(_app(engine, tmp_path, settings={"public_mode": True})) as c:
        r = c.get("/api/roadmaps/rm/skills")
        assert r.status_code == 401 and r.json() == {"error": "Passcode required"}
        assert c.post("/api/roadmaps/rm/sessions", json={}).status_code == 401
        assert c.get("/api/roadmaps/rm/skills", headers={"X-Access-Code": "pw123456"}).status_code == 200


# --- sessions ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["draft", "parsed", "ingesting", "finalizing", "failed"])
def test_session_on_unready_roadmap_409(client, engine, status):
    _roadmap(engine, status=status)
    r = client.post("/api/roadmaps/rm/sessions", json={"mode": "tutor"})
    assert r.status_code == 409
    assert r.json()["error"] == f"Roadmap 'rm' is '{status}'; {NOT_READY_TAIL}"


def test_session_needs_a_spec_with_skills_and_a_known_roadmap(client, engine):
    _roadmap(engine, spec={**SPEC, "stages": [{**SPEC["stages"][0], "skills": []}]})
    r = client.post("/api/roadmaps/rm/sessions", json={})
    assert r.status_code == 409 and r.json()["error"].endswith(NOT_READY_TAIL)
    r = client.post("/api/roadmaps/nope/sessions", json={})
    assert r.status_code == 404 and r.json() == {"error": "No roadmap 'nope'"}
    assert client.get("/api/roadmaps/nope/sessions").status_code == 404


def test_create_session_defaults_to_tutor_and_ends_the_previous_one(client, engine):
    _roadmap(engine, status="ready_with_gaps")
    r = client.post("/api/roadmaps/rm/sessions", json={})
    assert r.status_code == 201
    first = r.json()
    assert first["mode"] == "tutor" and first["status"] == "active" and first["roadmap"] == "rm"
    assert set(first) == {"id", "mode", "status", "roadmap"}
    second = _start(client, mode="qa")
    listed = client.get("/api/roadmaps/rm/sessions").json()
    assert [(s["id"], s["status"]) for s in listed] == [(second, "active"), (first["id"], "ended")]
    assert client.get(f"/api/sessions/{second}").json()["mode"] == "qa"


@pytest.mark.parametrize("body", [{"mode": "flash"}, {"mode": "tutor", "extra": 1}, ["tutor"]])
def test_create_session_validates_its_body(client, engine, body):
    _roadmap(engine)
    r = client.post("/api/roadmaps/rm/sessions", json=body)
    assert r.status_code == 422 and isinstance(r.json()["error"], str)
    assert client.get("/api/roadmaps/rm/sessions").json() == []


def test_get_session_and_unknown_ids(client, engine):
    _roadmap(engine)
    sid = _start(client)
    body = client.get(f"/api/sessions/{sid}").json()
    assert body["id"] == sid and body["roadmap"] == "rm" and body["mode"] == "tutor" and body["status"] == "active"
    assert body["started_at"] and body["ended_at"] is None
    assert (body["messages"], body["summary"], body["today"], body["pending"]) == ([], "", None, None)
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        assert client.get(f"/api/sessions/{bad}").status_code == 404
        assert _turn(client, bad, {"action": "start"}).status_code == 404
        assert client.post(f"/api/sessions/{bad}/end").status_code == 404


# --- turns ------------------------------------------------------------------------------------------------

def test_turn_streams_events_and_done(client, engine):
    _roadmap(engine)
    sid = _start(client)
    r = _turn(client, sid, {"action": "start"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    events = _sse(r)
    assert all(name == data["type"] for name, data in events)
    assert [d["name"] for n, d in events if n == "node"] == ["coordinator", "session_start_prepare", "planner"]
    assert "plan" in [n for n, _ in events]
    assert events[-1][0] == "done" and "trace_id" in events[-1][1]["trace"]
    assert r.text.endswith("\n\n")
    assert client.get(f"/api/sessions/{sid}").json()["today"]["session"] is not None


def test_pending_interrupt_survives_reload(client, engine):
    _add_quizzes(engine, _roadmap(engine))
    sid = _start(client)
    events = _sse(_turn(client, sid, {"action": "quiz"}))
    [card] = [d["value"] for n, d in events if n == "interrupt"]
    for _ in range(2):  # a reload, then a second tab
        pending = client.get(f"/api/sessions/{sid}").json()["pending"]
        assert pending == card and pending["kind"] == "quiz"


def test_bad_resume_422_and_state_unchanged(client, engine):
    _add_quizzes(engine, _roadmap(engine))
    sid = _start(client)
    r = _turn(client, sid, {"resume": {"answers": {}}})
    assert r.status_code == 422 and r.json()["error"] == "nothing is waiting for an answer"
    _sse(_turn(client, sid, {"action": "quiz"}))
    before = client.get(f"/api/sessions/{sid}").json()
    ids = [q["id"] for q in before["pending"]["questions"]]
    for bad in ({"answers": {"zz": "a"}}, {"choice": "skip"}, {"approved": True, "card": "x"}, "b1"):
        r = _turn(client, sid, {"resume": bad})
        assert r.status_code == 422 and r.json()["error"] == "that answer does not fit the pending 'quiz' question"
        assert client.get(f"/api/sessions/{sid}").json() == before
    events = _sse(_turn(client, sid, {"resume": {"answers": {i: "b" for i in ids}}}))
    assert "quiz_result" in [n for n, _ in events] and events[-1][0] == "done"
    assert client.get(f"/api/sessions/{sid}").json()["pending"] is None


@pytest.mark.parametrize("body, reason", [
    ({}, "exactly one"),
    ({"text": "hi", "action": "start"}, "exactly one"),
    ({"action": "start", "resume": {}}, "exactly one"),
    ({"topic": "kafka"}, "exactly one"),
    ({"action": "start", "topic": "kafka"}, "topic"),
    ({"text": "hi", "topic": "kafka"}, "topic"),
    ({"action": "fly"}, "action"),
    ({"action": "flashcards"}, "action"),
    ({"text": 5}, "text"),
    ({"text": "   "}, "text"),
    ({"text": "x" * 4001}, "4000"),
    ({"action": "research", "topic": "x" * 4001}, "4000"),
    ({"action": "research", "topic": 3}, "topic"),
    ({"text": "hi", "mode": "qa"}, "Unknown"),
    ([{"text": "hi"}], "JSON object"),
])
def test_body_must_have_exactly_one_key(client, engine, body, reason):
    _roadmap(engine)
    sid = _start(client)
    for target in (sid, str(uuid.uuid4())):  # validated before the session is looked up
        r = _turn(client, target, body)
        assert r.status_code == 422 and reason in r.json()["error"], r.text


def test_text_turn_at_the_limit(client, engine, clear_memo):  # noqa: F811
    _roadmap(engine)
    sid = _start(client)
    with fake_llm.script([FakeReply(json={"route": "recap"})]):
        events = _sse(_turn(client, sid, {"text": "x" * 4000}))
    assert [d["text"] for n, d in events if n == "reply"] == [NO_SESSION_TEXT] and events[-1][0] == "done"


def test_invalid_json_body_422(client, engine):
    _roadmap(engine)
    sid = _start(client)
    r = client.post(f"/api/sessions/{sid}/turn", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "The body must be a JSON object."


def test_ended_session_rejects_turns(client, engine):
    _roadmap(engine)
    sid = _start(client)
    _sse(_turn(client, sid, {"action": "start"}))
    r = client.post(f"/api/sessions/{sid}/end")
    assert r.status_code == 200 and r.json() == {"id": sid, "status": "ended"}
    assert client.post(f"/api/sessions/{sid}/end").json() == {"id": sid, "status": "ended"}  # idempotent
    r = _turn(client, sid, {"action": "continue"})
    assert r.status_code == 409 and r.json() == {"error": "This session has ended."}
    body = client.get(f"/api/sessions/{sid}").json()
    assert body["status"] == "ended" and body["ended_at"] is not None
    with engine.connect() as conn:
        jobs = conn.execute(text("SELECT type, payload FROM jobs WHERE type = 'curate_session'")).all()
    assert [(t, p["session_id"]) for t, p in jobs] == [("curate_session", sid)]


class _Gate:
    """A planner group that waits: `entered` is set when its node runs; it returns once `release` is set (or
    after `hold_s`). Its node keeps the name `planner`: other groups route to that node."""

    def __init__(self, hold_s=10.0):
        self.entered, self.release, self.hold_s = threading.Event(), threading.Event(), hold_s

    def add(self, builder, deps):
        async def planner(state):
            self.entered.set()
            await asyncio.to_thread(self.release.wait, self.hold_s)

        return single(builder, "planner", planner)


def test_concurrent_turn_409(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_mod, "END_WAIT_S", 0.2)
    gate = _Gate()
    _roadmap(engine)
    with local_client(_app(engine, tmp_path, tutor_groups={"planner": gate.add})) as c:
        sid = _start(c)
        results = []
        worker = threading.Thread(target=lambda: results.append(_turn(c, sid, {"action": "plan"})))
        worker.start()
        try:
            assert gate.entered.wait(10)
            before = c.get(f"/api/sessions/{sid}").json()
            r = _turn(c, sid, {"action": "plan"})
            assert r.status_code == 409 and r.json() == {"error": BUSY}
            r = c.post(f"/api/sessions/{sid}/end")  # ending waits for the turn (END_WAIT_S), then gives up
            assert r.status_code == 409 and r.json() == {"error": BUSY}
            assert c.post("/api/roadmaps/rm/sessions", json={}).status_code == 409
            assert c.get(f"/api/sessions/{sid}").json() == before
        finally:
            gate.release.set()
            worker.join(10)
        events = _sse(results[0])
        assert [d["name"] for n, d in events if n == "node"] == ["coordinator", "planner"]
        assert events[-1][0] == "done"
        assert c.get(f"/api/sessions/{sid}").json()["status"] == "active"
        assert _sse(_turn(c, sid, {"action": "plan"}))[-1][0] == "done"  # the next turn works


def _scope(path: str, spec_version: str) -> dict:
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}, "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 80)}


@pytest.mark.parametrize("spec_version", [
    "2.0",  # Starlette cancels the response when `http.disconnect` arrives: mid-turn, inside the turn generator
    "2.4",  # the next `send` fails (OSError, as uvicorn's does); the guard middleware then cancels the app
])
def test_aborted_stream_releases_the_session_and_exports_the_trace(engine, tmp_path, monkeypatch, spec_version):
    exported = []
    real_export = runtime_mod.export_trace
    monkeypatch.setattr(runtime_mod, "export_trace", lambda t, p: (exported.append(t.name), real_export(t, p)))
    gate = _Gate(hold_s=3.0)
    _roadmap(engine)
    app = _app(engine, tmp_path, tutor_groups={"planner": gate.add})
    with local_client(app) as c:
        sid = _start(c)

        async def abort() -> list[dict]:
            """A turn whose client disconnects once the first event arrived and the slow node runs."""
            sent, first_body, asked = [], anyio.Event(), False

            async def receive():
                nonlocal asked
                if not asked:
                    asked = True
                    return {"type": "http.request", "body": json.dumps({"action": "plan"}).encode(),
                            "more_body": False}
                await first_body.wait()
                await anyio.to_thread.run_sync(gate.entered.wait, 10)
                return {"type": "http.disconnect"}

            async def send(message):
                if spec_version == "2.4" and first_body.is_set():
                    await anyio.to_thread.run_sync(gate.entered.wait, 10)
                    raise OSError("client went away")
                sent.append(message)
                if message["type"] == "http.response.body" and message.get("body"):
                    first_body.set()

            with contextlib.suppress(ClientDisconnect, OSError):  # the server's concern
                await app(_scope(f"/api/sessions/{sid}/turn", spec_version), receive, send)
            return sent

        sent = c.portal.call(abort)
        assert sent[0]["status"] == 200
        bodies = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode()
        assert bodies.startswith("event: node\n") and "event: done" not in bodies  # cut off mid-turn
        assert gate.entered.is_set() and "tutor_turn" in exported
        gate.release.set()
        r = _turn(c, sid, {"action": "plan"})  # not Busy: the aborted turn let go of the session
        assert _sse(r)[-1][0] == "done"


# --- skill map, study settings, plan ----------------------------------------------------------------------

SPEC4 = {**SPEC, "stages": [SPEC["stages"][0], {**SPEC["stages"][1], "skills": [
    *SPEC["stages"][1]["skills"], _skill("ttl")]}]}


def _resource(engine, rm_id, key, skill_ids, status, kind="doc"):
    with session_scope(engine) as s:
        s.add(Resource(roadmap_id=rm_id, key=key, stage_id="s1", skill_ids=skill_ids, title=key.upper(),
                       kind=kind, url=f"https://docs.example/{key}", status=status))


def test_skill_map_states(client, engine):
    rm_id = _roadmap(engine, spec=SPEC4)
    store.set_mastery(engine, "rm", "cache-basics", 0.9, ["ttl"])
    store.set_mastery(engine, "rm", "eviction", 0.3, None)
    _resource(engine, rm_id, "r2", ["cache-basics", "eviction"], "proposed")
    _resource(engine, rm_id, "r1", ["cache-basics"], "done", kind="video")
    _resource(engine, rm_id, "r3", ["cache-basics"], "dismissed")
    body = client.get("/api/roadmaps/rm/skills").json()
    assert body["stages"] == [{"id": "s1", "title": "Basics"}, {"id": "s2", "title": "Distributed"}]
    by_id = {sk["id"]: sk for sk in body["skills"]}
    assert [sk["id"] for sk in body["skills"]] == ["cache-basics", "eviction", "write-back", "ttl"]
    assert {k: v["state"] for k, v in by_id.items()} == {
        "cache-basics": "mastered", "eviction": "learning", "write-back": "locked", "ttl": "not_started"}
    basics = by_id["cache-basics"]
    assert basics["stage_id"] == "s1" and basics["title"] == "CACHE-BASICS"
    assert basics["objectives"] == ["understand cache-basics"] and basics["prerequisites"] == []
    assert basics["mastery"] == 0.9 and basics["weak_spots"] == ["ttl"] and basics["locked"] is False
    assert basics["next_review_at"] is None
    assert basics["resources"] == [
        {"title": "R1", "url": "https://docs.example/r1", "kind": "video", "status": "done"},
        {"title": "R2", "url": "https://docs.example/r2", "kind": "doc", "status": "proposed"}]
    assert [r["title"] for r in by_id["eviction"]["resources"]] == ["R2"]
    assert by_id["write-back"]["stage_id"] == "s2" and by_id["write-back"]["locked"] is True
    assert by_id["ttl"]["resources"] == []


def test_skill_map_unknown_roadmap_or_no_spec(client, engine):
    assert client.get("/api/roadmaps/nope/skills").status_code == 404
    _roadmap(engine, status="draft", spec=None)
    r = client.get("/api/roadmaps/rm/skills")
    assert r.status_code == 409 and r.json() == {"error": "Roadmap 'rm' has no approved spec yet."}


def test_study_settings_roundtrip_and_validation(client, engine):
    _roadmap(engine)
    assert client.get("/api/roadmaps/rm/study-settings").json() == {
        "weekly_hours": None, "effective_weekly_hours": 3, "session_minutes": 45, "study_days": [0, 1, 2, 3, 4],
        "deadline": None}
    r = client.put("/api/roadmaps/rm/study-settings", json={"weekly_hours": 6, "study_days": [5, 6],
                                                            "deadline": "2027-01-31"})
    assert r.status_code == 200
    saved = {"weekly_hours": 6.0, "effective_weekly_hours": 6.0, "session_minutes": 45, "study_days": [5, 6],
             "deadline": "2027-01-31"}
    assert r.json() == saved and client.get("/api/roadmaps/rm/study-settings").json() == saved
    for bad, message in (({"session_minutes": 5}, "session_minutes must be a whole number from 15 to 240"),
                         ({"deadline": "soon"}, "deadline must be an ISO date (YYYY-MM-DD) or null"),
                         ({"pace": 2}, "Unknown settings field(s): pace"),
                         ([1], "The body must be a JSON object.")):
        r = client.put("/api/roadmaps/rm/study-settings", json=bad)
        assert r.status_code == 422 and r.json() == {"error": message}
    assert client.get("/api/roadmaps/rm/study-settings").json() == saved
    assert client.get("/api/roadmaps/nope/study-settings").status_code == 404
    assert client.put("/api/roadmaps/nope/study-settings", json={}).status_code == 404


def test_plan_reads_only(client, engine):
    _roadmap(engine)
    assert client.get("/api/roadmaps/rm/plan").json() == {"plan": None, "today": None}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM study_plans")).scalar() == 0  # nothing was saved
    sid = _start(client)
    _sse(_turn(client, sid, {"action": "plan"}))
    body = client.get("/api/roadmaps/rm/plan").json()
    assert body["plan"]["sessions"][0]["skills"] == ["cache-basics"]
    assert body["today"]["session"] == body["plan"]["sessions"][0]
    assert body["today"]["skills"][0]["id"] == "cache-basics"
    assert client.get("/api/roadmaps/nope/plan").status_code == 404
