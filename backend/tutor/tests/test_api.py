import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text

from backend.agent import access
from backend.core import config
from backend.tutor import api
from backend.tutor.api import create_app
from backend.tutor.db import make_engine, session_scope
from backend.tutor.settings import Settings, SettingsError

DOWN_URL = "postgresql+psycopg://x:y@127.0.0.1:9/z"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    access._failures.clear()
    yield
    access._failures.clear()


def _settings(tmp_path, **kw):
    return Settings(web_dist=tmp_path / "no-dist", **kw)


@pytest.fixture
def client(engine, tmp_path):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
    with TestClient(create_app(_settings(tmp_path), engine=engine)) as c:
        yield c


@pytest.fixture
def down_engine():
    eng = make_engine(DOWN_URL)
    yield eng
    eng.dispose()


def _body(**kw):
    return {"slug": "intro", "title": "Intro", "source_text": "Learn things."} | kw


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "llm_provider": "fake", "public_mode": False}
    assert r.headers["X-Trace-Id"]


def test_create_and_list(client):
    r = client.post("/api/roadmaps", json=_body())
    assert r.status_code == 201
    created = r.json()
    assert set(created) == {"id", "slug", "title", "status", "created_at"}
    assert created["slug"] == "intro" and created["title"] == "Intro" and created["status"] == "draft"
    r2 = client.post("/api/roadmaps", json=_body(slug="second", title="Second"))
    assert r2.status_code == 201
    listed = client.get("/api/roadmaps")
    assert listed.status_code == 200
    assert [x["slug"] for x in listed.json()] == ["second", "intro"]
    assert listed.json()[1] == created


def test_duplicate_returns_409(client):
    assert client.post("/api/roadmaps", json=_body(slug="dup")).status_code == 201
    r = client.post("/api/roadmaps", json=_body(slug="dup", title="Again"))
    assert r.status_code == 409
    assert r.json() == {"error": "A roadmap with slug 'dup' already exists"}
    assert len(client.get("/api/roadmaps").json()) == 1


@pytest.mark.parametrize(
    "override",
    [{"slug": "Bad Slug"}, {"title": ""}, {"source_text": "x" * 200_001}],
    ids=["bad-slug", "empty-title", "too-long"],
)
def test_create_roadmap_validation(client, override):
    r = client.post("/api/roadmaps", json=_body(**override))
    assert r.status_code == 422
    assert isinstance(r.json()["error"], str) and r.json()["error"]
    assert client.get("/api/roadmaps").json() == []


def test_create_roadmap_missing_field(client):
    r = client.post("/api/roadmaps", json={"slug": "intro", "title": "Intro"})
    assert r.status_code == 422


def test_emoji_source_accepted(client):
    r = client.post("/api/roadmaps", json=_body(title="Emoji 😀", source_text="😀" * 1000))
    assert r.status_code == 201
    assert r.json()["title"] == "Emoji 😀"


def test_public_mode_guard(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "pw123456")
    app = create_app(_settings(tmp_path, public_mode=True), engine=engine)
    with TestClient(app) as c:
        health = c.get("/api/health")
        assert health.status_code == 200 and health.json()["public_mode"] is True
        r = c.get("/api/roadmaps")
        assert r.status_code == 401 and r.json() == {"error": "Passcode required"}
        assert c.get("/api/roadmaps", headers={"X-Access-Code": "wrong"}).status_code == 401
        assert c.get("/api/roadmaps", headers={"X-Access-Code": "pw123456"}).status_code == 200
        for _ in range(4):  # 5 wrong in total
            assert c.get("/api/roadmaps", headers={"X-Access-Code": "nope"}).status_code == 401
        locked = c.get("/api/roadmaps", headers={"X-Access-Code": "pw123456"})
        assert locked.status_code == 429
        assert locked.json()["error"].startswith("Too many wrong passcodes")


def test_db_down_returns_503(down_engine, tmp_path):
    with TestClient(create_app(_settings(tmp_path), engine=down_engine)) as c:
        h = c.get("/api/health")
        assert h.status_code == 503
        assert h.json()["status"] == "degraded" and h.json()["db"] == "down"
        for r in (c.get("/api/roadmaps"), c.post("/api/roadmaps", json=_body())):
            assert r.status_code == 503
            assert r.json() == {"error": "Database unavailable"}


def test_spa_fallback(down_engine, tmp_path):
    (tmp_path / "index.html").write_text("<h1>app</h1>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    with TestClient(create_app(Settings(web_dist=tmp_path), engine=down_engine)) as c:
        page = c.get("/roadmaps/abc")
        assert page.status_code == 200 and page.text == "<h1>app</h1>"
        assert "text/html" in page.headers["content-type"]
        assert c.get("/").text == "<h1>app</h1>"
        asset = c.get("/assets/app.js")
        assert asset.status_code == 200 and asset.text == "console.log(1)"
        assert c.get("/assets/../../etc/passwd").text == "<h1>app</h1>"
        nope = c.get("/api/nope")
        assert nope.status_code == 404
        assert nope.headers["content-type"].startswith("application/json")


def test_unknown_api_path_is_json_404_without_spa(down_engine, tmp_path):
    with TestClient(create_app(_settings(tmp_path), engine=down_engine)) as c:
        r = c.get("/api/nope")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")


def test_startup_refuses_public_mode_without_passcode(down_engine, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "")
    app = create_app(_settings(tmp_path, public_mode=True), engine=down_engine)
    with pytest.raises(SettingsError):
        with TestClient(app):
            pass


def test_trace_exported_with_route_name(engine, tmp_path, monkeypatch):
    from backend.tutor.telemetry import setup_tracing

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(api, "setup_tracing", lambda endpoint: setup_tracing(None, exporter=exporter))
    with TestClient(create_app(_settings(tmp_path), engine=engine)) as c:
        r = c.get("/api/roadmaps")
    assert r.status_code == 200
    roots = [s for s in exporter.get_finished_spans() if s.name == "GET /api/roadmaps"]
    assert len(roots) == 1
    assert roots[0].attributes["trace.id"] == r.headers["X-Trace-Id"]


def test_telemetry_failure_does_not_break_request(engine, tmp_path, monkeypatch):
    def boom(trace, provider):
        raise RuntimeError("exporter exploded")

    monkeypatch.setattr(api, "export_trace", boom)
    with TestClient(create_app(_settings(tmp_path), engine=engine)) as c:
        assert c.get("/api/health").status_code == 200


@pytest.fixture
def guarded(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "pw123456")
    with TestClient(create_app(_settings(tmp_path, public_mode=True), engine=engine)) as c:
        yield c


def test_spoofed_forwarded_for_does_not_bypass_lockout(guarded):
    codes = [
        guarded.get("/api/roadmaps", headers={"X-Access-Code": "nope", "X-Forwarded-For": f"10.0.0.{i}"}).status_code
        for i in range(6)
    ]
    assert codes == [401] * 5 + [429]
    assert len(access._failures) == 1


def test_forwarded_for_cannot_lock_out_a_victim(guarded):
    for _ in range(6):
        guarded.get("/api/roadmaps", headers={"X-Access-Code": "nope", "X-Forwarded-For": "203.0.113.7"})
    victim = {"X-Access-Code": "pw123456", "CF-Connecting-IP": "203.0.113.7"}
    assert guarded.get("/api/roadmaps", headers=victim).status_code == 200
    assert not access._failures.get("203.0.113.7")  # no failures recorded against the victim


def test_cf_connecting_ip_counters_are_independent(guarded):
    a = {"X-Access-Code": "nope", "CF-Connecting-IP": "198.51.100.1"}
    for _ in range(5):
        assert guarded.get("/api/roadmaps", headers=a).status_code == 401
    assert guarded.get("/api/roadmaps", headers=a).status_code == 429
    b = {"X-Access-Code": "pw123456", "CF-Connecting-IP": "198.51.100.2"}
    assert guarded.get("/api/roadmaps", headers=b).status_code == 200
    assert guarded.get("/api/roadmaps", headers={**b, "X-Access-Code": "nope"}).status_code == 401
