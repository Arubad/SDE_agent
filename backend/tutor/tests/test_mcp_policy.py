"""The agent URL policy and the allow/deny/ask gate, both enforced in the gateway. Servers are in-memory FastMCP;
DNS is a fake resolver (no network)."""
import asyncio
import contextlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from backend.tutor.mcp.gateway import CallContext, Gateway
from backend.tutor.mcp.policy import UrlPolicy
from backend.tutor.mcp.registry import Registry, RegistryError, ServerSpec, load_registry
from backend.tutor.untrusted import TAG

pytestmark = pytest.mark.anyio
COMMITTED = Path(__file__).resolve().parents[1] / "mcp" / "servers.json"
PRIVATE_HOSTS = {"intranet.example": ["10.0.0.5"], "loop.example": ["127.0.0.1"]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def resolver(host):
    return PRIVATE_HOSTS.get(host, ["93.184.216.34"])


def _policy(*urls):
    p = UrlPolicy(resolver=resolver)
    p.allow_sources(urls)
    return p


class _FakeExternal(Gateway):
    """Serves http/stdio specs from the in-memory server registered under the spec's name (no network)."""

    def _client(self, spec):
        return Client(self.inproc[spec.name]) if spec.transport != "inproc" else super()._client(spec)


@contextlib.contextmanager
def _gateway(specs, servers, agents=None, deny=(), ask=(), cls=Gateway):
    reg = Registry(servers=tuple(specs), agents=agents or {"all": ("*",)}, deny=tuple(deny), ask=tuple(ask))
    gw = cls(reg, engine=None, inproc=servers, env={})
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()
        assert not [t for t in threading.enumerate() if t.name == "mcp-gateway"]


def _spec(name, ns=None, **kw):
    return ServerSpec(name=name, namespace=ns or name, transport="inproc", target=name, **kw)


def _counted(name, calls):
    """A server whose lifespan counts connects and whose tools record their calls."""
    state = {"connects": 0}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        state["connects"] += 1
        yield {}

    s = FastMCP(name, lifespan=lifespan)

    @s.tool
    def forget(topic: str, opts: dict | None = None) -> str:
        calls.append(("forget", topic))
        return "forgotten"

    @s.tool
    def recall(topic: str) -> str:
        calls.append(("recall", topic))
        return "recalled"

    @s.tool
    def fetch(url: str, source_url: str | None = None, page_url: list[str] | None = None) -> str:
        calls.append(("fetch", url))
        return f"page {url}"

    return s, state


def _web(fetched):
    s = FastMCP("web")

    @s.tool
    def search(query: str) -> dict:
        return {"results": [
            {"title": "A", "url": "https://example.org/a", "snippet": "s", "engine": "fake"},
            {"title": "F", "url": "ftp://example.net/f", "snippet": "", "engine": "fake"},
            {"title": "P", "url": "http://intranet.example/x", "snippet": "", "engine": "fake"},
            {"title": "N", "url": None, "snippet": "", "engine": "fake"},
        ]}

    @s.tool
    def fetch(url: str) -> str:
        fetched.append(url)
        return f"page {url}"

    return s


def _browser(navigations, pages, fail_blank=False):
    """Playwright-shaped fake: `browser_navigate` replies with page text and a `- Page URL:` line."""
    b = FastMCP("browser")

    @b.tool
    def browser_navigate(url: str) -> str:
        navigations.append(url)
        if url == "about:blank" and fail_blank:
            raise RuntimeError("browser crashed")
        return pages.get(url, f"### Page state\n- Page URL: {url}\n- Page Title: t")

    @b.tool
    def browser_snapshot() -> str:
        return "secret snapshot text\n- Page URL: http://10.0.0.5/admin"

    return b


def _browser_spec():
    return ServerSpec(name="playwright", namespace="browser", transport="http", url="http://unused.invalid/mcp",
                      strip_prefix="browser_")


# ---- UrlPolicy ---------------------------------------------------------------------------------------------------
def test_unlisted_url_refused():
    p = _policy("https://docs.python.org/3/")
    assert p.refusal("https://example.org/a") is not None
    for bad in ("ftp://docs.python.org/x", "javascript:alert(1)", "docs.python.org/3/", "", "https://"):
        assert p.refusal(bad) is not None, bad
    p.allow_text("please read https://example.org/a, and (https://example.com/b).")
    assert p.refusal("https://example.org/a") is None
    assert p.refusal("https://example.com/b") is None


def test_same_site_navigation_allowed():
    p = _policy("https://docs.python.org/3/tutorial/")
    assert p.refusal("https://docs.python.org/3/tutorial/") is None
    assert p.refusal("https://docs.python.org/3/library/os.html?x=1#y") is None
    assert p.refusal("http://DOCS.Python.org./") is None
    for other in ("https://python.org/", "https://docs.python.org.attacker.com/", "https://x.docs.python.org/",
                  "https://docs.python.org@evil.com/", "https://evil.com\\@docs.python.org/",
                  "https://user:pw@docs.python.org/"):
        assert p.refusal(other) is not None, other


def test_private_and_loopback_refused_even_if_listed():
    listed = ["http://127.0.0.1:8000/api/health", "http://169.254.169.254/", "http://localhost:8000/",
              "http://[::1]/", "https://intranet.example/x", "https://loop.example/"]
    p = _policy(*listed)
    p.allow_text(" ".join(listed))
    for url in listed:
        assert p.refusal(url) is not None, url
    assert p.refusal("http://127.0.0.1:8000/other") is not None


def test_default_resolver_still_refuses_literals():
    p = UrlPolicy()
    p.allow_sources(["http://127.0.0.1/", "https://example.org/"])
    assert p.refusal("http://127.0.0.1/") is not None and p.refusal("https://example.org/x") is None


# ---- the URL policy in the gateway -------------------------------------------------------------------------------
async def test_search_results_become_allowed():
    fetched = []
    with _gateway([_spec("web")], {"web": _web(fetched)}) as gw:
        p = _policy()
        ctx = CallContext(agent="all", policy=p)
        out = await gw.call("web.fetch", {"url": "https://example.org/b"}, ctx)
        assert out["error"].startswith("refused by URL policy: ") and fetched == []
        out = await gw.call("web__search", {"query": "q"}, ctx)
        assert out["data"]["results"][0]["url"] == f"<{TAG}>https://example.org/a</{TAG}>"  # still fenced
        assert await gw.call("web.fetch", {"url": "https://example.org/b"}, ctx) == {
            "data": {"result": f"<{TAG}>page https://example.org/b</{TAG}>"}}
        assert fetched == ["https://example.org/b"]
        assert p.refusal("https://example.net/f") is not None  # an ftp result added nothing
        assert p.refusal("http://intranet.example/x") is not None


async def test_url_arguments_checked_before_connecting():
    calls = []
    s, state = _counted("tools", calls)
    with _gateway([_spec("tools")], {"tools": s}) as gw:
        ctx = CallContext(agent="all", policy=_policy("https://ok.example/"))
        for args in ({"url": "http://127.0.0.1:8000/"},
                     {"url": "https://ok.example/", "source_url": "https://unlisted.example/"},
                     {"url": "https://ok.example/", "page_url": ["https://ok.example/2", "http://10.0.0.1/"]}):
            out = await gw.call("tools.fetch", args, ctx)
            assert out["error"].startswith("refused by URL policy: "), args
        assert state["connects"] == 0 and calls == []
        ok = {"url": "https://ok.example/a", "source_url": None, "page_url": ["https://ok.example/b"]}
        assert "data" in await gw.call("tools.fetch", ok, ctx)
        assert await gw.call("tools.recall", {"topic": "http://127.0.0.1/"}, ctx) == {
            "data": {"result": f"<{TAG}>recalled</{TAG}>"}}  # only url / *_url arguments are URLs


async def test_browser_final_url_off_policy_is_discarded():
    navs = []
    start = "https://allowed.example/start"
    pages = {start: "secret page text\n- Page URL: http://10.0.0.5/admin"}
    with _gateway([_browser_spec()], {"playwright": _browser(navs, pages)}, cls=_FakeExternal) as gw:
        ctx = CallContext(agent="all", policy=_policy(start))
        out = await gw.call("browser.navigate", {"url": start}, ctx)
        assert set(out) == {"error"} and out["error"].startswith("navigation left the allowed sites: ")
        assert "secret page text" not in json.dumps(out)
        assert navs == [start, "about:blank"]
        out = await gw.call("browser__navigate", {"url": "https://allowed.example/next"}, ctx)
        assert out["data"]["result"].startswith(f"<{TAG}>### Page state")  # same-site: returned, fenced
        assert navs[-1] == "https://allowed.example/next"


async def test_browser_reset_bypasses_gate_and_its_failure_still_refuses():
    navs = []
    agents = {"teacher": ("browser.snapshot",)}  # may not call navigate itself
    with _gateway([_browser_spec()], {"playwright": _browser(navs, {}, fail_blank=True)}, agents=agents,
                  cls=_FakeExternal) as gw:
        ctx = CallContext(agent="teacher", policy=_policy("https://allowed.example/"))
        out = await gw.call("browser.snapshot", {}, ctx)
        assert out["error"].startswith("navigation left the allowed sites: ")
        assert "secret snapshot text" not in json.dumps(out) and "crashed" not in json.dumps(out)
        assert navs == ["about:blank"]


async def test_no_policy_means_no_url_checks():
    fetched, navs = [], []
    pages = {"https://a.example/": "page text\n- Page URL: http://10.0.0.5/admin"}
    servers = {"web": _web(fetched), "playwright": _browser(navs, pages)}
    with _gateway([_spec("web"), _browser_spec()], servers, cls=_FakeExternal) as gw:
        ctx = CallContext(agent="all")
        assert "data" in await gw.call("web.fetch", {"url": "http://127.0.0.1:8000/api/health"}, ctx)
        out = await gw.call("browser.navigate", {"url": "https://a.example/"}, ctx)
        assert "page text" in out["data"]["result"]
    assert fetched == ["http://127.0.0.1:8000/api/health"] and navs == ["https://a.example/"]


# ---- the allow/deny/ask gate -------------------------------------------------------------------------------------
async def test_deny_beats_ask_and_allow():
    calls, approvals = [], []
    s, state = _counted("learner", calls)
    agents = {"teacher": ("learner.*",)}
    with _gateway([_spec("learner")], {"learner": s}, agents=agents, deny=["learner.forget"],
                  ask=["learner.forget", "learner.fetch", "other.*"]) as gw:
        assert gw.decision("teacher", "learner.forget") == "deny"
        assert gw.decision("teacher", "learner.fetch") == "ask"
        assert gw.decision("teacher", "learner.recall") == "allow"
        assert gw.decision("nobody", "learner.recall") == "deny"
        assert gw.decision("teacher", "other.thing") == "deny"  # ask never grants beyond the agent's patterns
        ctx = CallContext(agent="teacher", approve=lambda p: approvals.append(p) or True,
                          policy=_policy())
        for name in ("learner.forget", "learner__forget"):
            out = await gw.call(name, {"topic": "x", "url": "http://127.0.0.1/"}, ctx)
            assert out == {"error": "tool learner.forget is not allowed for teacher"}
        assert state["connects"] == 0 and calls == [] and approvals == []
        assert [d["name"] for d in await gw.tool_defs("teacher")] == ["learner__fetch", "learner__recall"]


async def test_ask_without_approver_is_refused():
    calls, asked = [], []
    s, state = _counted("learner", calls)
    off, _ = _counted("off", calls)
    specs = [_spec("learner"), _spec("off", enabled_default=False)]
    with _gateway(specs, {"learner": s, "off": off}, ask=["learner.forget", "off.*"]) as gw:
        out = await gw.call("off.forget", {"topic": "x"}, CallContext(agent="all", approve=asked.append))
        assert out == {"error": "off.forget: server off is disabled"} and asked == []
        ctx = CallContext(agent="all", policy=_policy())
        for name in ("learner.forget", "learner__forget"):
            out = await gw.call(name, {"topic": "x"}, ctx)
            assert out == {"error": "approval required for learner.forget"}
        out = await gw.call("learner.forget", {"topic": "x", "url": "http://127.0.0.1/"}, ctx)
        assert out == {"error": "approval required for learner.forget"}  # the gate runs before the URL policy
        assert state["connects"] == 0 and calls == []


async def test_ask_approver_sees_raw_payload_and_decides():
    calls, seen = [], []
    s, state = _counted("learner", calls)
    spec = _spec("learner-srv", "learner")
    args = {"topic": "graphs <b>", "opts": {"hard": True, "tags": ["a", "b"]}}

    def approver(answer):
        def approve(payload):
            seen.append(payload)
            payload["args"]["topic"] = "tampered"
            return answer
        return approve

    async def async_yes(payload):
        seen.append(payload)
        return True

    def boom(payload):
        raise RuntimeError("approver exploded: secret-detail")

    with _gateway([spec], {"learner-srv": s}, ask=["learner.forget"]) as gw:
        for refuse in (approver(False), approver("yes"), boom):
            out = await gw.call("learner.forget", args, CallContext(agent="all", approve=refuse))
            assert out == {"error": "learner.forget was not approved"}
        assert state["connects"] == 0 and calls == []
        assert seen[0] == {"tool": "learner.forget", "server": "learner-srv", "agent": "all",
                           "args": {"topic": "tampered", "opts": {"hard": True, "tags": ["a", "b"]}}}
        assert args["topic"] == "graphs <b>"  # the approver got a copy
        seen.clear()
        out = await gw.call("learner__forget", args, CallContext(agent="all", approve=approver(True)))
        assert out == {"data": {"result": f"<{TAG}>forgotten</{TAG}>"}} and calls == [("forget", "graphs <b>")]
        out = await gw.call("learner.forget", args, CallContext(agent="all", approve=async_yes))
        assert "data" in out and len(calls) == 2
        assert seen[1] == {"tool": "learner.forget", "server": "learner-srv", "agent": "all", "args": args}
        assert await gw.call("learner.recall", {"topic": "t"}, CallContext(agent="all")) == {
            "data": {"result": f"<{TAG}>recalled</{TAG}>"}}  # not an ask tool: no approver needed


async def test_blocking_approver_does_not_stall_other_calls():
    calls = []
    s, _ = _counted("learner", calls)
    release = threading.Event()

    def slow_human(payload):
        return release.wait(10)

    with _gateway([_spec("learner")], {"learner": s}, ask=["learner.forget"]) as gw:
        pending = asyncio.ensure_future(gw.call("learner.forget", {"topic": "a"},
                                                CallContext(agent="all", approve=slow_human)))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        assert "data" in await gw.call("learner.recall", {"topic": "b"}, CallContext(agent="all"))
        assert time.monotonic() - t0 < 2 and not pending.done()
        release.set()
        assert "data" in await pending
    assert calls == [("recall", "b"), ("forget", "a")]


# ---- registry rules ----------------------------------------------------------------------------------------------
def _write(tmp_path, rules):
    p = tmp_path / "servers.json"
    servers = [{"name": "learner", "namespace": "learner", "transport": "inproc", "target": "learner"}]
    p.write_text(json.dumps({"servers": servers, "agents": {"teacher": ["learner.*"]}, "rules": rules}))
    return p


def test_rules_patterns_validated(tmp_path):
    reg = load_registry(_write(tmp_path, {"deny": ["learner.forget"], "ask": ["learner.*"]}), {})
    assert (reg.deny, reg.ask) == (("learner.forget",), ("learner.*",))
    assert load_registry(_write(tmp_path, {"ask": ["learner.forget"]}), {}).deny == ()
    for rules in ({"deny": ["browser.navigate"]}, {"ask": ["learner.x", "nope.*"]}):
        with pytest.raises(RegistryError, match="matches no server namespace"):
            load_registry(_write(tmp_path, rules), {})
        assert load_registry(_write(tmp_path, rules), {}, allow_unknown=True)
    for bad in ({"allow": ["learner.*"]}, {"deny": "learner.*"}, {"ask": [3]}, ["learner.*"]):
        with pytest.raises(RegistryError):
            load_registry(_write(tmp_path, bad), {}, allow_unknown=True)


def test_registry_without_rules_and_committed_rules():
    reg = Registry(servers=(), agents={})
    assert (reg.deny, reg.ask) == ((), ())
    committed = load_registry(COMMITTED, {}, allow_unknown=True)
    assert committed.deny == ()
    assert committed.ask == ("ingest.create_roadmap", "ingest.approve_roadmap", "learner.forget")
