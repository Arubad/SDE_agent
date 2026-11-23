import json

import pytest
from sqlalchemy import text

from backend.agent import access
from backend.core import config
from backend.tutor.api import create_app
from backend.tutor.db import session_scope
from backend.tutor.settings import Settings
from backend.tutor.tests.clients import local_client

TOKEN = "ghp_FAKE_TOKEN_for_tests_0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    access._failures.clear()
    yield
    access._failures.clear()


@pytest.fixture
def registry(tmp_path):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": [
        {"name": "web", "namespace": "web", "transport": "inproc", "target": "web", "enabled": True,
         "note": "Web search."},
        # nothing listens on port 9: the check fails fast and the error must not show the token
        {"name": "github", "namespace": "repo.github", "transport": "http", "url": "http://127.0.0.1:9/mcp",
         "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}, "requires_env": ["GITHUB_TOKEN"],
         "enabled": True, "note": "GitHub."},
        {"name": "ctx", "namespace": "docs.ctx", "transport": "http", "url": "http://127.0.0.1:9/mcp",
         "requires_env": ["CTX_API_KEY_FOR_TEST"], "enabled": True},
    ], "agents": {}}))
    return p


def _app(engine, tmp_path, registry, **kw):
    return create_app(Settings(web_dist=tmp_path / "no-dist", mcp_registry=registry, **kw), engine=engine)


@pytest.fixture(autouse=True)
def _clean_state(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM mcp_servers"))
    yield
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM mcp_servers"))


@pytest.fixture
def client(engine, tmp_path, registry):
    with local_client(_app(engine, tmp_path, registry)) as c:
        yield c


def test_list_servers_hides_secrets(client):
    r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    assert TOKEN not in r.text and "Bearer" not in r.text
    by = {h["name"]: h for h in r.json()}
    assert set(by) == {"web", "github", "ctx"}
    assert by["web"]["state"] == "ok" and by["web"]["tools"] > 0
    assert by["web"]["namespace"] == "web" and by["web"]["transport"] == "inproc"
    assert by["github"]["state"] == "down" and by["github"]["error"]
    assert by["ctx"]["state"] == "missing_env" and "CTX_API_KEY_FOR_TEST" in by["ctx"]["error"]
    # the check route is just as clean
    c = client.post("/api/mcp/servers/github/check")
    assert c.status_code == 200 and TOKEN not in c.text and c.json()["name"] == "github"


TOGGLED = "toggle-test-web"  # not a real server's name: its stored on/off row can never switch off the real `web`


def test_toggle_persists_and_updates_health(engine, tmp_path):
    reg = tmp_path / "toggle-servers.json"
    reg.write_text(json.dumps({"servers": [
        {"name": TOGGLED, "namespace": "web", "transport": "inproc", "target": "web", "enabled": True}],
        "agents": {}}))
    try:
        with local_client(_app(engine, tmp_path, reg)) as first:
            assert first.get("/api/mcp/servers").json()[0]["state"] == "ok"
            r = first.put(f"/api/mcp/servers/{TOGGLED}", json={"enabled": False})
            assert r.status_code == 200
            assert r.json()["state"] == "disabled" and r.json()["enabled"] is False
            by = {h["name"]: h for h in first.get("/api/mcp/servers").json()}
            assert by[TOGGLED]["state"] == "disabled"
        with local_client(_app(engine, tmp_path, reg)) as second:  # an app restart on the same database
            assert {h["name"]: h for h in second.get("/api/mcp/servers").json()}[TOGGLED]["state"] == "disabled"
            r = second.put(f"/api/mcp/servers/{TOGGLED}", json={"enabled": True})
            assert r.json()["state"] == "ok" and r.json()["enabled"] is True
    finally:  # whatever failed above, leave no stored override behind
        with session_scope(engine) as s:
            s.execute(text("DELETE FROM mcp_servers WHERE name = :n"), {"n": TOGGLED})


def test_put_requires_a_boolean(client):
    assert client.put("/api/mcp/servers/web", json={"enabled": "yes"}).status_code == 422
    assert client.put("/api/mcp/servers/web", json={}).status_code == 422


def test_unknown_server_404(client):
    for r in (client.post("/api/mcp/servers/nope/check"), client.put("/api/mcp/servers/nope", json={"enabled": False})):
        assert r.status_code == 404
        assert "nope" in r.json()["error"]


def test_public_mode_guards_mcp_api(engine, tmp_path, registry, monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "pw123456")
    with local_client(_app(engine, tmp_path, registry, public_mode=True)) as c:
        for r in (c.get("/api/mcp/servers"), c.post("/api/mcp/servers/web/check"),
                  c.put("/api/mcp/servers/web", json={"enabled": False})):
            assert r.status_code == 401
        ok = c.get("/api/mcp/servers", headers={"X-Access-Code": "pw123456"})
        assert ok.status_code == 200 and TOKEN not in ok.text
