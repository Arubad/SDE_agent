"""The own MCP servers over HTTP (mounted in the API, behind McpGuard) and stdio, and the gateway the API starts."""
import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from backend.agent import access
from backend.core import config
from backend.tutor.api import create_app
from backend.tutor.db import make_engine
from backend.tutor.mcp.gateway import Gateway
from backend.tutor.mcp.http import McpGuard
from backend.tutor.settings import REPO_ROOT, Settings

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
DOWN_URL = "postgresql+psycopg://x:y@127.0.0.1:9/z"
KNOWLEDGE_TOOLS = {"list_roadmaps", "search", "get_section", "list_resources"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    monkeypatch.setenv("FAKE_LLM_REPLIES_DIR", str(FIXTURES / "llm_replies"))
    access._failures.clear()
    yield
    access._failures.clear()


def _settings(tmp_path, **kw):
    kw.setdefault("ingest_agent", False)
    kw.setdefault("web_dist", tmp_path / "no-dist")
    return Settings(net_fixtures_dir=FIXTURES / "net", ingest_workers=kw.pop("ingest_workers", 0),
                    embedder="hash", qdrant_url=":memory:", **kw)


@pytest.fixture
def down_engine():
    eng = make_engine(DOWN_URL)
    yield eng
    eng.dispose()


def _init(c, name="knowledge", **headers):
    return c.post(f"/mcp/{name}/", json=INIT, headers={**MCP_HEADERS, **headers})


# ---- McpGuard ----------------------------------------------------------------------------------------------------
def test_mcp_rejects_foreign_host_and_origin(down_engine, tmp_path):
    with TestClient(create_app(_settings(tmp_path), engine=down_engine)) as c:
        assert _init(c, Host="evil.example").status_code == 403
        assert _init(c, Host="evil.example:8000").status_code == 403
        assert _init(c, Host="127.0.0.1.evil.example").status_code == 403
        assert _init(c, Host="localhost@evil.example").status_code == 403
        assert _init(c, Host="127.0.0.1:8000", Origin="https://evil.example").status_code == 403
        assert _init(c, Host="127.0.0.1:8000", Origin="null").status_code == 403
        assert _init(c, Host="127.0.0.1:8000", Origin="file://localhost").status_code == 403
        for host in ("127.0.0.1:8000", "localhost", "LOCALHOST:1234", "[::1]:8000", "[::1]"):
            r = _init(c, Host=host)
            assert r.status_code == 200, (host, r.text)
            assert r.headers.get("mcp-session-id")
        ok = _init(c, Host="localhost:8000", Origin="http://localhost:5173")
        assert ok.status_code == 200 and "serverInfo" in ok.text
        for name in ("learner", "ingestion", "web"):
            assert _init(c, name, Host="127.0.0.1:8000").status_code == 200, name
        # the tunnel guard still covers /mcp outside public mode
        assert _init(c, Host="127.0.0.1:8000", **{"CF-Ray": "abc"}).status_code == 403


def test_public_mode_requires_bearer(down_engine, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "pw123456")
    with TestClient(create_app(_settings(tmp_path, public_mode=True), engine=down_engine)) as c:
        tunnel = "tutor.example.com"
        r = _init(c, Host=tunnel)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert _init(c, Host=tunnel, Authorization="Bearer wrong").status_code == 401
        assert _init(c, Host=tunnel, Authorization="pw123456").status_code == 401  # not a Bearer header
        ok = _init(c, Host=tunnel, Authorization="Bearer pw123456")
        assert ok.status_code == 200 and "serverInfo" in ok.text
        assert _init(c, Host=tunnel, Authorization="bearer pw123456").status_code == 200
        # the Origin check is kept in public mode
        assert _init(c, Host=tunnel, Authorization="Bearer pw123456",
                     Origin="https://evil.example").status_code == 403
        for _ in range(4):  # 5 wrong in total: the same lockout as the API passcode
            assert _init(c, Host=tunnel, Authorization="Bearer nope").status_code == 401
        locked = _init(c, Host=tunnel, Authorization="Bearer pw123456")
        assert locked.status_code == 429 and locked.json()["error"].startswith("Too many wrong passcodes")


async def test_guard_passes_lifespan_and_503_before_startup():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    guard = McpGuard(inner, public_mode=False, lockout_key=lambda request: "k")
    await guard({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]
    # a mount whose server is not built yet (startup has not finished) answers 503
    app = create_app(Settings(web_dist=Path("/nonexistent")), engine=None)
    sent = []

    async def send(message):
        sent.append(message)

    route = next(r for r in app.routes if getattr(r, "path", None) == "/mcp/knowledge")
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"host", b"127.0.0.1:8000")],
             "query_string": b"", "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}
    await route.app(scope, None, send)
    assert sent[0]["status"] == 503


def test_spa_does_not_shadow_mcp(down_engine, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>app</h1>")
    with TestClient(create_app(_settings(tmp_path, web_dist=dist), engine=down_engine)) as c:
        r = _init(c, Host="127.0.0.1:8000")
        assert r.status_code == 200 and "serverInfo" in r.text
        assert c.get("/mcp/nothing", headers={"Host": "127.0.0.1"}).status_code == 404
        assert c.get("/some/page").text == "<h1>app</h1>"


# ---- served over HTTP --------------------------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _serve(app):
    port = _free_port()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not uv.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        uv.should_exit = True
        await asyncio.to_thread(thread.join, 20)
        assert not thread.is_alive()


async def test_knowledge_over_http_end_to_end(engine, tmp_path):
    app = create_app(_settings(tmp_path), engine=engine)
    async with _serve(app) as base:
        async with Client(StreamableHttpTransport(f"{base}/mcp/knowledge/")) as c:
            assert {t.name for t in await c.list_tools()} == KNOWLEDGE_TOOLS
            assert isinstance((await c.call_tool("list_roadmaps", {})).structured_content, dict)
        # the gateway started with the API reaches the very same servers in process
        gateway = app.state.gateway
        assert isinstance(gateway, Gateway)
        wire = {d["name"] for d in await gateway.tool_defs("teacher")}
        assert {"kb__search", "kb__get_section", "web__search", "web__fetch"} <= wire
        assert set(gateway.inproc) == {"knowledge", "learner", "ingestion", "web"}
    assert app.state.gateway._loop is None  # stopped on shutdown


async def test_app_starts_with_unreachable_external_server(engine, tmp_path):
    reg = tmp_path / "servers.json"
    reg.write_text(json.dumps({
        "servers": [
            {"name": "kb", "namespace": "kb", "transport": "inproc", "target": "knowledge"},
            {"name": "ext", "namespace": "ext", "transport": "http", "url": "http://127.0.0.1:1/mcp"},
        ],
        "agents": {"teacher": ["kb.*", "ext.*"]},
    }))
    started = time.monotonic()
    with TestClient(create_app(_settings(tmp_path, mcp_registry=reg), engine=engine)) as c:
        r = c.get("/api/health")
        assert r.status_code == 200, r.text
        assert time.monotonic() - started < 2.0
        assert [s.name for s in c.app.state.gateway.registry.servers] == ["kb", "ext"]


def test_http_and_gateway_never_share_a_server_instance(down_engine, tmp_path):
    # R22: a FastMCP instance belongs to one event loop: uvicorn's for HTTP, the gateway's for inproc
    app = create_app(_settings(tmp_path), engine=down_engine)
    with TestClient(app):
        served, inproc = app.state.mcp_servers, app.state.gateway.inproc
        assert set(served) == set(inproc) == {"knowledge", "learner", "ingestion", "web"}
        for name in served:
            assert served[name] is not inproc[name], name


def test_workers_get_the_gateway(engine, tmp_path):
    app = create_app(_settings(tmp_path, ingest_workers=1), engine=engine, handlers={"noop": lambda ctx, job: None})
    with TestClient(app):
        pool = app.state.worker_pool
        assert pool.ctx.gateway is app.state.gateway
        assert pool.ctx.index is not None


# ---- stdio -------------------------------------------------------------------------------------------------------
def _stdio_env(db_url) -> dict[str, str]:
    return {**os.environ, "DATABASE_URL": db_url, "LLM_PROVIDER": "fake", "EMBEDDER": "hash",
            "PLATFORM_QDRANT_URL": ":memory:", "NET_FIXTURES_DIR": str(FIXTURES / "net"),
            "PYTHONUNBUFFERED": "1"}


def _raw_stdio_session(cmd: list[str], env: dict[str, str]) -> tuple[list[str], dict]:
    """Speak JSON-RPC by hand and return every stdout line plus the tools/list result."""
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    lines: queue.Queue = queue.Queue()
    out: list[str] = []

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def until(msg_id):
        while True:
            line = lines.get(timeout=60)
            assert line is not None, "server exited early"
            out.append(line)
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg

    try:
        send(INIT)
        until(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = until(2)
        proc.stdin.close()
        while (line := lines.get(timeout=60)) is not None:
            out.append(line)
        proc.wait(30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return out, listed


async def test_stdio_entrypoint_lists_tools(db_url):
    env = _stdio_env(db_url)
    args = ["-m", "backend.tutor", "mcp", "web", "--no-workers"]
    transport = StdioTransport(sys.executable, args, env=env, cwd=str(REPO_ROOT))
    async with Client(transport) as c:
        assert {t.name for t in await c.list_tools()} == {"search", "fetch"}
    out, listed = await asyncio.to_thread(_raw_stdio_session, [sys.executable, *args], env)
    assert {t["name"] for t in listed["result"]["tools"]} == {"search", "fetch"}
    for line in out:  # stdout carries protocol only
        assert json.loads(line)["jsonrpc"] == "2.0", line
