"""The own `web` MCP server: search and a guarded fetch, called through an in-memory fastmcp Client."""
import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from backend.tutor.ingest.fetch import Fetcher, public_resolver
from backend.tutor.ingest.net import Net, SearchResult, SearchUnavailable
from backend.tutor.mcp.servers.web import build_web_server

pytestmark = pytest.mark.anyio
ARTICLE = "<html><head><title>Raft</title></head><body><article><h1>Raft</h1><p>" + \
    "Consensus explained in plain words. " * 20 + "</p></article></body></html>"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeSearch:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results or [], error, 0

    def search(self, query, *, category="general"):
        self.calls += 1
        if self.error:
            raise self.error
        return self.results


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_net(handler, search=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(client, domain_interval_s=0.0, resolver=public_resolver, sleep=lambda s: None)
    return Net(fetcher=fetcher, search=search, youtube=None)


def site(seen=None, robots="", redirect=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=robots, headers={"content-type": "text/plain"})
        if redirect and req.url.path == "/go":
            return httpx.Response(302, headers={"location": redirect})
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
    return handler


async def call(server, tool, args):
    async with Client(server) as c:
        return (await c.call_tool(tool, args)).data


async def test_search_returns_results():
    rs = [SearchResult(f"T{i}", f"https://a.test/{i}", f"s{i}", "duckduckgo") for i in range(5)]
    server = build_web_server(make_net(site(), FakeSearch(rs)))
    data = await call(server, "search", {"query": "raft", "max_results": 3})
    assert data == {"results": [{"title": f"T{i}", "url": f"https://a.test/{i}", "snippet": f"s{i}",
                                 "engine": "duckduckgo"} for i in range(3)]}


async def test_search_unavailable_is_tool_error():
    server = build_web_server(make_net(site(), FakeSearch(error=SearchUnavailable("down"))))
    with pytest.raises(ToolError, match="search unavailable: down"):
        await call(server, "search", {"query": "x"})
    server = build_web_server(make_net(site(), None))
    with pytest.raises(ToolError, match="SearXNG is not configured"):
        await call(server, "search", {"query": "x"})


async def test_fetch_returns_markdown():
    server = build_web_server(make_net(site()))
    data = await call(server, "fetch", {"url": "https://a.test/page", "max_chars": 1000})
    assert data["url"] == data["final_url"] == "https://a.test/page"
    assert data["title"] == "Raft" and "Consensus explained" in data["markdown"]
    assert len(data["markdown"]) <= 1000


async def test_web_fetch_refuses_private_redirect():
    server = build_web_server(make_net(site(redirect="http://127.0.0.1/")))
    with pytest.raises(ToolError, match="private_address") as e:
        await call(server, "fetch", {"url": "https://a.test/go"})
    assert "Consensus" not in str(e.value)


async def test_fetch_respects_robots():
    server = build_web_server(make_net(site(robots="User-agent: *\nDisallow: /")))
    with pytest.raises(ToolError, match="robots"):
        await call(server, "fetch", {"url": "https://a.test/page"})


async def test_fetch_rejects_non_html():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF-1.4 x", headers={"content-type": "application/pdf"})
    server = build_web_server(make_net(handler))
    with pytest.raises(ToolError, match="unsupported_type"):
        await call(server, "fetch", {"url": "https://a.test/a.pdf"})


async def test_results_cached_within_ttl():
    seen: list[str] = []
    clock = Clock()
    search = FakeSearch([SearchResult("T", "https://a.test/1", "s", "e")])
    server = build_web_server(make_net(site(seen), search), ttl_s=900, clock=clock)
    page = [u for u in seen if not u.endswith("robots.txt")]
    await call(server, "fetch", {"url": "https://a.test/page"})
    await call(server, "fetch", {"url": "https://a.test/page"})
    await call(server, "search", {"query": "raft"})
    await call(server, "search", {"query": "raft"})
    assert [u for u in seen if u.endswith("/page")] == ["https://a.test/page"] and search.calls == 1
    clock.t += 901
    await call(server, "fetch", {"url": "https://a.test/page"})
    await call(server, "search", {"query": "raft"})
    assert [u for u in seen if u.endswith("/page")] == ["https://a.test/page"] * 2 and search.calls == 2
    assert page == []


async def test_errors_are_not_cached():
    search = FakeSearch(error=SearchUnavailable("down"))
    server = build_web_server(make_net(site(), search))
    for _ in range(2):
        with pytest.raises(ToolError):
            await call(server, "search", {"query": "x"})
    assert search.calls == 2
