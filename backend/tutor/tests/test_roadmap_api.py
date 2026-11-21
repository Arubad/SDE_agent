import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.agent import access
from backend.core import config
from backend.core.llm import LLMUnavailable
from backend.tutor import api
from backend.tutor.api import create_app
from backend.tutor.db import session_scope
from backend.tutor.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
K8S = (FIXTURES / "roadmaps" / "kubernetes.txt").read_text()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    monkeypatch.setenv("FAKE_LLM_REPLIES_DIR", str(FIXTURES / "llm_replies"))
    monkeypatch.setattr(api, "EVENTS_INTERVAL_S", 0.05)
    access._failures.clear()
    yield
    access._failures.clear()


def _settings(tmp_path, **kw):
    return Settings(web_dist=tmp_path / "no-dist", net_fixtures_dir=FIXTURES / "net", ingest_workers=0, **kw)


@pytest.fixture
def client(engine, tmp_path):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
    with TestClient(create_app(_settings(tmp_path), engine=engine)) as c:
        yield c


def _create(client, slug="k8s"):
    r = client.post("/api/roadmaps", json={"slug": slug, "title": "K8s", "source_text": K8S})
    assert r.status_code == 201
    return slug


def _parse(client, slug="k8s"):
    r = client.post(f"/api/roadmaps/{slug}/parse")
    assert r.status_code == 200, r.text
    return r.json()


def _choices(parsed):
    out = {}
    for res in parsed["resolution"]:
        out[res["resource_id"]] = res["candidates"][0]["url"] if res["candidates"] else None
    return out


def test_parse_then_approve_flow(client):
    slug = _create(client)
    parsed = _parse(client, slug)
    assert len(parsed["spec"]["stages"]) == 4
    choices = _choices(parsed)
    r = client.post(f"/api/roadmaps/{slug}/approve", json={"spec": parsed["spec"], "choices": choices})
    assert r.status_code == 202 and r.json() == {"status": "ingesting"}, r.text
    got = client.get(f"/api/roadmaps/{slug}").json()
    assert got["status"] == "ingesting" and got["spec"]["title"]
    by_title = {x["title"]: x for x in got["resources"]}
    assert len(got["resources"]) == 8
    killer = next(x for t, x in by_title.items() if "Killercoda" in t)
    assert killer["status"] == "linked_only"
    crawlable = [x for x in got["resources"] if x["status"] == "pending"]
    assert crawlable
    assert got["jobs"]["ingest_resource"] == {"queued": len(crawlable)}
    assert got["slug"] == slug and got["resources"][0]["stats"] == {}
    # idempotent-safe: a second approve is a 409, and nothing is duplicated
    assert client.post(f"/api/roadmaps/{slug}/approve", json={"spec": parsed["spec"], "choices": choices}).status_code == 409


def test_approve_skips_resources_without_url(client):
    slug = _create(client)
    parsed = _parse(client, slug)
    choices = {rid: None for rid in _choices(parsed)}
    assert client.post(f"/api/roadmaps/{slug}/approve", json={"spec": parsed["spec"], "choices": choices}).status_code == 202
    got = client.get(f"/api/roadmaps/{slug}").json()
    assert {x["status"] for x in got["resources"]} == {"skipped"}
    assert {x["reason"] for x in got["resources"]} == {"no_url"}
    assert "ingest_resource" not in got["jobs"]


def test_put_spec_and_state_rules(client):
    slug = _create(client)
    assert client.put(f"/api/roadmaps/{slug}/spec", json={"spec": {}}).status_code == 409  # still draft
    parsed = _parse(client, slug)
    spec = parsed["spec"]
    spec["title"] = "Edited"
    r = client.put(f"/api/roadmaps/{slug}/spec", json={"spec": spec})
    assert r.status_code == 200 and r.json()["title"] == "Edited"
    assert client.get(f"/api/roadmaps/{slug}").json()["spec"]["title"] == "Edited"
    assert client.put(f"/api/roadmaps/{slug}/spec", json={"spec": {"title": 1}}).status_code == 422
    assert _parse(client, slug)["spec"]["title"] != "Edited"  # re-parse while parsed overwrites
    assert client.post("/api/roadmaps/nope/parse").status_code == 404


def test_approve_requires_parsed(client):
    slug = _create(client)
    assert client.post(f"/api/roadmaps/{slug}/approve", json={"spec": {}, "choices": {}}).status_code == 409


def test_parse_after_approve_conflicts(client):
    slug = _create(client)
    parsed = _parse(client, slug)
    client.post(f"/api/roadmaps/{slug}/approve", json={"spec": parsed["spec"], "choices": _choices(parsed)})
    assert client.post(f"/api/roadmaps/{slug}/parse").status_code == 409
    assert client.put(f"/api/roadmaps/{slug}/spec", json={"spec": parsed["spec"]}).status_code == 409


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://10.0.0.5/", "file:///etc/passwd", "http://localhost:8080/", "http://[::1]/",
    "http://169.254.169.254/latest", "http://100.64.0.1/", "http://192.168.1.1/", "http://0.0.0.0/", "ftp://example.test/x", "not a url",
])
def test_approve_rejects_unsafe_urls(client, url):
    slug = _create(client)
    parsed = _parse(client, slug)
    rid = parsed["resolution"][0]["resource_id"]
    r = client.post(f"/api/roadmaps/{slug}/approve", json={"spec": parsed["spec"], "choices": {rid: url}})
    assert r.status_code == 422 and "error" in r.json()
    assert client.get(f"/api/roadmaps/{slug}").json()["status"] == "parsed"


def test_approve_rejects_unknown_resource_id(client):
    slug = _create(client)
    parsed = _parse(client, slug)
    r = client.post(f"/api/roadmaps/{slug}/approve", json={"spec": parsed["spec"], "choices": {"nope": None}})
    assert r.status_code == 422


def test_approve_allows_public_urls(client):
    slug = _create(client)
    parsed = _parse(client, slug)
    rid = parsed["resolution"][0]["resource_id"]
    r = client.post(f"/api/roadmaps/{slug}/approve",
                    json={"spec": parsed["spec"], "choices": {rid: "https://www.youtube.com/watch?v=abc"}})
    assert r.status_code == 202


def test_llm_unavailable_is_503(client, monkeypatch):
    def boom(_text):
        raise LLMUnavailable("no key")

    monkeypatch.setattr("backend.tutor.ingest.service.parse_roadmap", boom)
    slug = _create(client)
    r = client.post(f"/api/roadmaps/{slug}/parse")
    assert r.status_code == 503 and r.json()["error"].startswith("Model unavailable")
    assert client.get(f"/api/roadmaps/{slug}").json()["status"] == "draft"


def _set_status(engine, slug, status):
    with session_scope(engine) as s:
        s.execute(text("UPDATE roadmaps SET status=:st WHERE slug=:s"), {"st": status, "s": slug})


def _events(lines):
    event = None
    for line in lines:
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            yield event, json.loads(line.split(":", 1)[1])


def test_events_stream_progress_then_done(client, engine):
    slug = _create(client)
    parsed = _parse(client, slug)
    client.post(f"/api/roadmaps/{slug}/approve", json={"spec": parsed["spec"], "choices": _choices(parsed)})
    seen = []
    with client.stream("GET", f"/api/roadmaps/{slug}/events") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for event, data in _events(r.iter_lines()):
            seen.append((event, data))
            assert len(seen) < 100, "stream never reached a terminal status"
            if event == "progress" and data["status"] == "ingesting" and len(seen) == 1:
                assert "queued" in data["jobs"]["ingest_resource"] and data["resources"]
                _set_status(engine, slug, "ready")
            if event == "done":
                break
    assert seen[0][0] == "progress" and seen[-1][0] == "done"
    assert seen[-1][1]["status"] == "ready"


def test_events_unknown_roadmap_404(client):
    assert client.get("/api/roadmaps/nope/events").status_code == 404


def test_events_require_passcode(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "pw123456")
    monkeypatch.setattr(access, "check_passcode", lambda code, key: code == "pw")
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
    from backend.tutor.repo import create_roadmap

    with session_scope(engine) as s:
        create_roadmap(s, slug="k8s", title="K8s", source_text=K8S)
    _set_status(engine, "k8s", "ready")
    with TestClient(create_app(_settings(tmp_path, public_mode=True), engine=engine)) as c:
        assert c.get("/api/roadmaps/k8s/events").status_code == 401
        with c.stream("GET", "/api/roadmaps/k8s/events", headers={"X-Access-Code": "pw"}) as r:
            assert r.status_code == 200
            events = [e for e, _ in _events(r.iter_lines())]
        assert events[-1] == "done"


def test_report_gating(client, engine):
    slug = _create(client)
    assert client.get(f"/api/roadmaps/{slug}/report").status_code == 409
    _set_status(engine, slug, "ready")
    r = client.get(f"/api/roadmaps/{slug}/report")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready" and body["resources"] == [] and body["skipped"] == []
    assert body["summary"] == "0 videos" and body["videos"]["total"] == 0


def test_workers_start_only_with_handlers(engine, tmp_path):
    import threading

    def workers():
        return [t for t in threading.enumerate() if t.name.startswith("job-worker")]

    base = len(workers())
    s = Settings(web_dist=tmp_path / "x", net_fixtures_dir=FIXTURES / "net", ingest_workers=1)
    with TestClient(create_app(s, engine=engine)):
        assert len(workers()) == base
    with TestClient(create_app(s, engine=engine, handlers={"noop": lambda ctx, job: None})):
        assert len(workers()) == base + 1
    assert len(workers()) == base


URL_SOURCE = "Learn Rust.\n\n## Basics\n\nRead https://doc.rust-lang.org/book/ first.\n"


def _url_spec(*_a):
    from backend.tutor.roadmap.parse import normalize_spec

    def res(i, url):
        return {"id": i, "title": f"Res {i}", "kind": "doc", "url": url, "url_hint": None, "query": i, "note": "",
                "focus": [], "skill_ids": []}

    raw = {"title": "Rust", "goals": [], "weekly_hours": None, "stages": [
        {"id": "s", "title": "Basics", "timeframe": None, "summary": "", "skills": [],
         "resources": [res("with-url", "https://doc.rust-lang.org/book/"), res("no-url", None)],
         "practice_goals": []}]}
    return normalize_spec(raw, URL_SOURCE)


def _approve_url_case(client, monkeypatch, choices):
    monkeypatch.setattr("backend.tutor.ingest.service.parse_roadmap", lambda text: _url_spec())
    r = client.post("/api/roadmaps", json={"slug": "rust", "title": "Rust", "source_text": URL_SOURCE})
    assert r.status_code == 201
    parsed = _parse(client, "rust")
    ids = [x["id"] for st in parsed["spec"]["stages"] for x in st["resources"]]
    assert ids == ["res-with-url", "res-no-url"]
    r = client.post("/api/roadmaps/rust/approve", json={"spec": parsed["spec"], "choices": choices})
    assert r.status_code == 202, r.text
    return {x["key"]: x for x in client.get("/api/roadmaps/rust").json()["resources"]}


def test_absent_choice_falls_back_to_source_url(client, monkeypatch):
    got = _approve_url_case(client, monkeypatch, {})
    assert got["res-with-url"]["status"] == "pending" and got["res-with-url"]["url"] == "https://doc.rust-lang.org/book/"
    assert got["res-no-url"]["status"] == "skipped" and got["res-no-url"]["reason"] == "no_url"
    jobs = client.get("/api/roadmaps/rust").json()["jobs"]
    assert jobs["ingest_resource"] == {"queued": 1}


def test_explicit_null_skips_even_with_source_url(client, monkeypatch):
    got = _approve_url_case(client, monkeypatch, {"res-with-url": None})
    assert got["res-with-url"]["status"] == "skipped" and got["res-with-url"]["reason"] == "no_url"
    assert got["res-with-url"]["url"] is None
    assert "ingest_resource" not in client.get("/api/roadmaps/rust").json()["jobs"]


def test_choice_url_is_stored_stripped(client, monkeypatch):
    got = _approve_url_case(client, monkeypatch, {"res-no-url": "  https://example.test/x  "})
    assert got["res-no-url"]["url"] == "https://example.test/x" and got["res-no-url"]["status"] == "pending"
