"""The MCP gateway: naming, allow-lists, timeouts, untrusted wrapping, health. Fake servers are in-memory FastMCP."""
import asyncio
import contextlib
import json
import random
import re
import threading
import time
from dataclasses import asdict

import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool

from backend.core.trace import Trace
from backend.tutor.mcp.gateway import CallContext, Gateway, wire_name
from backend.tutor.mcp.registry import Registry, ServerSpec
from backend.tutor.untrusted import TAG

pytestmark = pytest.mark.anyio
WIRE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MARKER = " [truncated 9999999 chars]"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _spec(name, ns=None, **kw):
    return ServerSpec(name=name, namespace=ns or name, transport="inproc", target=name, **kw)


@contextlib.contextmanager
def _gateway(specs, servers, agents=None, env=None):
    gw = Gateway(Registry(servers=tuple(specs), agents=agents or {"all": ("*",)}), engine=None,
                 inproc=servers, env=env or {})
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()
        assert not [t for t in threading.enumerate() if t.name == "mcp-gateway"]


def _web(calls=None):
    web = FastMCP("web")

    @web.tool
    def search(query: str) -> str:
        """Search the web."""
        if calls is not None:
            calls.append(query)
        return f"results for {query}"

    return web


def _browser():
    b = FastMCP("browser")

    @b.tool
    def browser_navigate(url: str) -> str:
        """Open a URL."""
        return f"Page URL: {url}"

    @b.tool
    def browser_close() -> str:
        return "closed"

    return b


def _canonicals(n=200):
    rnd = random.Random(7)
    namespaces = ["web", "repo.deepwiki", "docs.context7", "ünï"]
    alphabet = "abcxyz_-.0é漢"
    out = {(ns, "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 20)))) for ns in namespaces
           for _ in range(n)}
    pairs = sorted(out)[: n - 4]
    long = "t" * 85
    pairs += [("web", long + "aaaaa"), ("web", long + "bbbbb"), ("repo.deepwiki", "q" * 60 + "1"),
              ("repo.deepwiki", "q" * 60 + "2")]
    return pairs


def _const(value):
    def fn() -> str:
        return value
    return fn


async def test_wire_names_valid_unique_roundtrip():
    pairs = _canonicals()
    assert len(pairs) == 200
    wires = {wire_name(f"{ns}.{t}", namespace=ns) for ns, t in pairs}
    assert len(wires) == 200 and all(WIRE.match(w) for w in wires)
    servers = {}
    for ns, tool in pairs:
        servers.setdefault(ns, FastMCP(ns)).add_tool(Tool.from_function(_const(f"{ns}.{tool}"), name=tool))
    specs = [_spec(f"s{i}", ns, untrusted=False) for i, ns in enumerate(servers)]
    with _gateway(specs, {s.target: servers[s.namespace] for s in specs}) as gw:
        defs = await gw.tool_defs("all")
        names = [d["name"] for d in defs]
        assert len(names) == 200 == len(set(names)) and names == sorted(names)
        assert all(WIRE.match(n) for n in names)
        assert set(names) == wires
        ctx = CallContext(agent="all")
        namespace_of = {f"{ns}.{t}": ns for ns, t in pairs}
        for d in defs:
            out = await gw.call(d["name"], {}, ctx)
            canonical = out["data"]["result"]
            assert wire_name(canonical, namespace=namespace_of.pop(canonical)) == d["name"]
            assert await gw.call(canonical, {}, ctx) == out
        assert namespace_of == {}


def test_wire_name_rules():
    assert wire_name("web.search") == "web__search"
    assert wire_name("repo.deepwiki.ask_question") == "repo_deepwiki__ask_question"
    long = wire_name("web." + "x" * 90)
    assert len(long) == 64 and long.startswith("web__xxx") and re.fullmatch(r".{55}_[0-9a-f]{8}", long)


async def test_namespaces_that_meet_after_dot_to_underscore_stay_distinct():
    specs = [_spec("one", "a.b", untrusted=False), _spec("two", "a_b", untrusted=False)]
    servers = {}
    for spec in specs:
        servers[spec.name] = FastMCP(spec.name)
        servers[spec.name].add_tool(Tool.from_function(_const(spec.namespace), name="c"))
    with _gateway(specs, servers) as gw:
        names = [d["name"] for d in await gw.tool_defs("all")]
        assert len(set(names)) == 2 and "a_b__c" in names and all(WIRE.match(n) for n in names)
        ctx = CallContext(agent="all")
        assert sorted([(await gw.call(n, {}, ctx))["data"]["result"] for n in names]) == ["a.b", "a_b"]


async def test_namespacing_and_strip_prefix():
    with _gateway([_spec("browser", strip_prefix="browser_")], {"browser": _browser()}) as gw:
        defs = await gw.tool_defs("all")
        assert [d["name"] for d in defs] == ["browser__close", "browser__navigate"]
        nav = defs[1]
        assert nav["description"] == "[browser] Open a URL."
        assert nav["input_schema"]["properties"]["url"]["type"] == "string"
        ctx = CallContext(agent="all")
        for name in ("browser__navigate", "browser.navigate"):
            out = await gw.call(name, {"url": "https://a.example/"}, ctx)
            assert out["data"]["result"] == f"<{TAG}>Page URL: https://a.example/</{TAG}>"


async def test_allow_list_filters_defs_and_refuses_calls():
    calls = []
    agents = {"crawler": ("browser.navigate", "browser.close"), "teacher": ("web.search",)}
    with _gateway([_spec("web"), _spec("browser", strip_prefix="browser_")],
                  {"web": _web(calls), "browser": _browser()}, agents=agents) as gw:
        assert [d["name"] for d in await gw.tool_defs("crawler")] == ["browser__close", "browser__navigate"]
        assert [d["name"] for d in await gw.tool_defs("teacher")] == ["web__search"]
        assert await gw.tool_defs("nobody") == []
        for name in ("web.search", "web__search"):
            out = await gw.call(name, {"query": "x"}, CallContext(agent="crawler"))
            assert out == {"error": "tool web.search is not allowed for crawler"}
        assert calls == []


async def test_disallowed_name_refused_before_connecting():
    s, state = _failing_server("never", fail_times=0)
    with _gateway([_spec("f")], {"f": s}, agents={"crawler": ("browser.*",)}) as gw:
        for name in ("f__ping", "f.ping"):
            out = await gw.call(name, {}, CallContext(agent="crawler"))
            assert out == {"error": "tool f.ping is not allowed for crawler"}
        assert state["connects"] == 0
        assert await gw.call("nope__x", {}, CallContext(agent="crawler")) == {"error": "unknown tool nope__x"}


async def test_disabled_and_missing_env_servers_are_absent():
    specs = [_spec("web"), _spec("off", enabled_default=False), _spec("needs", requires_env=("NEED_ME",))]
    servers = {"web": _web(), "off": _web(), "needs": _web()}
    with _gateway(specs, servers) as gw:
        assert [d["name"] for d in await gw.tool_defs("all")] == ["web__search"]
        states = {h.name: (h.state, h.enabled) for h in await gw.health(refresh=True)}
        assert states == {"web": ("ok", True), "off": ("disabled", False), "needs": ("missing_env", True)}
        out = await gw.call("off.search", {"query": "x"}, CallContext(agent="all"))
        assert "disabled" in out["error"]
        out = await gw.call("needs.search", {"query": "x"}, CallContext(agent="all"))
        assert "NEED_ME" in out["error"]


async def test_hanging_server_times_out_and_others_work():
    slow = FastMCP("slow")

    @slow.tool
    async def wait() -> str:
        await asyncio.sleep(30)
        return "late"

    specs = [_spec("slow", timeout_s=0.5), _spec("web"),
             ServerSpec(name="ghost", namespace="ghost", transport="stdio", command="no-such-command-xyz-123")]
    gw = Gateway(Registry(servers=tuple(specs), agents={"all": ("*",)}), engine=None,
                 inproc={"slow": slow, "web": _web()}, env={})
    t0 = time.monotonic()
    gw.start()
    try:
        assert time.monotonic() - t0 < 0.5
        ctx = CallContext(agent="all")
        t0 = time.monotonic()
        out = await gw.call("slow.wait", {}, ctx)
        assert time.monotonic() - t0 < 2
        assert out == {"error": "slow.wait timed out after 0.5s"}
        health = {h.name: h for h in await gw.health()}
        assert health["slow"].state == "down"
        assert health["ghost"].state == "down" and "no-such-command-xyz-123" in health["ghost"].error
        out = await gw.call("web.search", {"query": "q"}, ctx)
        assert out == {"data": {"result": f"<{TAG}>results for q</{TAG}>"}}
        out = await gw.call("ghost.anything", {}, ctx)
        assert out["error"].startswith("ghost.anything unavailable: ")
    finally:
        gw.stop()
    assert not [t for t in threading.enumerate() if t.name == "mcp-gateway"]


async def test_untrusted_strings_are_wrapped_and_capped():
    def server():
        s = FastMCP("t")

        @s.tool
        def big() -> str:
            return "a" * 50_000

        @s.tool
        def nested() -> dict:
            return {"items": [{"title": "<b>x</b>", "n": 3}], "note": "y"}

        return s

    specs = [_spec("ext"), _spec("own", untrusted=False)]
    with _gateway(specs, {"ext": server(), "own": server()}) as gw:
        ctx = CallContext(agent="all")
        out = await gw.call("ext.big", {}, ctx)
        text = out["data"]["result"]
        assert text.startswith(f"<{TAG}>aaa") and f"</{TAG}> [truncated " in text
        assert len(json.dumps(out, ensure_ascii=False)) <= 20_000 + len(MARKER)
        kept, cut = re.fullmatch(rf"<{TAG}>(a*)</{TAG}> \[truncated (\d+) chars\]", text).groups()
        assert len(kept) + int(cut) == 50_000
        out = await gw.call("ext.nested", {}, ctx)
        assert out == {"data": {"items": [{"title": f"<{TAG}>&lt;b>x&lt;/b></{TAG}>", "n": 3}],
                                "note": f"<{TAG}>y</{TAG}>"}}
        out = await gw.call("own.big", {}, ctx)
        text = out["data"]["result"]
        assert TAG not in text and text.startswith("aaa") and text.endswith(" chars]")
        assert len(json.dumps(out, ensure_ascii=False)) <= 20_000 + len(MARKER)


async def test_raw_results_only_for_crawler():
    s = FastMCP("browser")

    @s.tool
    def page(size: int) -> str:
        return "<html>" + "a" * size

    with _gateway([_spec("browser")], {"browser": s}) as gw:
        out = await gw.call("browser.page", {"size": 50_000}, CallContext(agent="all", raw=True))
        assert out == {"error": "raw results are only for the crawler agent"}
        out = await gw.call("browser.page", {"size": 50_000}, CallContext(agent="crawler", raw=True))
        assert out == {"error": "tool browser.page is not allowed for crawler"}
    with _gateway([_spec("browser")], {"browser": s}, agents={"crawler": ("browser.page",)}) as gw:
        ctx = CallContext(agent="crawler", raw=True)
        out = await gw.call("browser.page", {"size": 50_000}, ctx)
        assert out == {"data": {"result": "<html>" + "a" * 50_000}}
        out = await gw.call("browser.page", {"size": 6 * 1024 * 1024}, ctx)
        text = out["data"]["result"]
        assert TAG not in text and text.endswith(" chars]")
        assert len(json.dumps(out)) <= 5 * 1024 * 1024 + len(MARKER)


async def test_passages_shape_passes_through():
    kb = FastMCP("kb")
    passages = [{"text": "<b>raw</b> text", "source": "https://a.example/", "score": 0.5}]

    @kb.tool
    def search(query: str) -> dict:
        return {"passages": passages}

    with _gateway([_spec("kb")], {"kb": kb}) as gw:
        assert await gw.call("kb__search", {"query": "x"}, CallContext(agent="all")) == {"passages": passages}


async def test_tool_error_becomes_error_result():
    s = FastMCP("e")

    @s.tool
    def fail() -> str:
        raise ValueError("bad")

    with _gateway([_spec("e")], {"e": s}) as gw:
        out = await gw.call("e.fail", {}, CallContext(agent="all"))
        assert set(out) == {"error"} and "bad" in out["error"]
        assert [h.state for h in await gw.health()] == ["ok"]


def _failing_server(message, fail_times):
    state = {"connects": 0}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        state["connects"] += 1
        if state["connects"] <= fail_times:
            raise RuntimeError(message)
        yield {}

    s = FastMCP("f", lifespan=lifespan)

    @s.tool
    def ping() -> str:
        return "pong"

    return s, state


async def test_health_never_shows_secrets():
    msg = "refused Authorization: Bearer ghp_x123456 (token ghp_x123456, key s3cr3t-value-9)"
    s, _ = _failing_server(msg, fail_times=99)
    spec = _spec("gh", headers={"Authorization": "Bearer ghp_x123456"}, requires_env=("MY_KEY",))
    with _gateway([spec], {"gh": s}, env={"MY_KEY": "s3cr3t-value-9"}) as gw:
        out = await gw.call("gh.ping", {}, CallContext(agent="all"))
        h = await gw.check("gh")
        dumped = json.dumps(asdict(h)) + json.dumps([asdict(x) for x in await gw.health(refresh=True)])
        assert h.state == "down" and h.error and len(h.error) <= 300
        for secret in ("ghp_x123456", "s3cr3t-value-9"):
            assert secret not in dumped and secret not in out["error"]
        assert json.loads(json.dumps(asdict(h)))["name"] == "gh"


async def test_reconnects_after_server_failure():
    s, state = _failing_server("connection reset", fail_times=1)
    with _gateway([_spec("f", untrusted=False)], {"f": s}) as gw:
        ctx = CallContext(agent="all")
        out = await gw.call("f.ping", {}, ctx)
        assert out["error"].startswith("f.ping unavailable: ") and "connection reset" in out["error"]
        assert (await gw.check("f")).state == "ok"
        assert await gw.call("f.ping", {}, ctx) == {"data": {"result": "pong"}}
        assert state["connects"] == 2


async def test_reset_closes_connection_and_clears_tool_cache():
    s, state = _failing_server("never", fail_times=0)
    with _gateway([_spec("f", untrusted=False)], {"f": s}) as gw:
        assert [d["name"] for d in await gw.tool_defs("all")] == ["f__ping"]
        s.add_tool(Tool.from_function(_const("new"), name="added"))
        assert [d["name"] for d in await gw.tool_defs("all")] == ["f__ping"]  # cached per connection
        await gw.reset("f")
        assert [d["name"] for d in await gw.tool_defs("all")] == ["f__added", "f__ping"]
        assert state["connects"] == 2


async def test_call_is_traced():
    trace = Trace("t")
    with _gateway([_spec("web")], {"web": _web()}) as gw:
        await gw.call("web__search", {"query": "mail me at someone@example.com"}, CallContext("all", trace=trace))
    (span,) = trace.spans
    assert span.name == "mcp:web.search" and span.kind == "tool"
    assert span.attrs["tool"] == "web.search" and span.attrs["ok"] is True
    assert "[email]" in span.attrs["args"] and len(span.attrs["args"]) <= 200
    assert isinstance(span.attrs["latency_ms"], int)


async def test_call_sync_from_worker_thread():
    with _gateway([_spec("web")], {"web": _web()}) as gw:
        ctx = CallContext(agent="all")
        expected = await gw.call("web__search", {"query": "t"}, ctx)
        got = {}
        worker = threading.Thread(target=lambda: got.update(r=gw.call_sync("web.search", {"query": "t"}, ctx)))
        worker.start()
        await asyncio.to_thread(worker.join, 10)
        assert got["r"] == expected and "data" in expected
