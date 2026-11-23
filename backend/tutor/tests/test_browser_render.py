"""The crawler renders `needs_browser` pages through the gateway's browser (Playwright MCP). The browser is an
in-memory FastMCP server with Playwright MCP's tool names and output formats; the net is a fixture net."""
import contextlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastmcp import FastMCP
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.tutor.db import session_scope
from backend.tutor.ingest import video
from backend.tutor.ingest.fetch import public_resolver
from backend.tutor.jobs import queue
from backend.tutor.mcp.browser import EVALUATE_FUNCTION, RenderResult, render_page
from backend.tutor.mcp.gateway import Gateway
from backend.tutor.mcp.registry import Registry, ServerSpec, load_registry
from backend.tutor.models import Job, Section
from backend.tutor.tests.test_ingestion_flow import (SPA, _ctx, _docs, _drain, _page, _prose, _report, _resources,
                                                     _roadmap, _site)

COMMITTED = Path(__file__).resolve().parents[1] / "mcp" / "servers.json"
ROOT = "https://spa.test/docs/"


@pytest.fixture(autouse=True)
def _clean(engine, monkeypatch):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
    monkeypatch.setattr(video, "CAPTIONS_THROTTLE", video.Throttle(0, sleep=lambda s: None))
    monkeypatch.setattr(video, "CAPTIONS_BREAKER", video.CircuitBreaker())
    yield


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(queue, "clock", lambda: now[0])
    return now


def _playwright(pages: dict, calls: list) -> FastMCP:
    """Playwright MCP v0.0.86's formats: navigate reports `- Page URL:`; evaluate returns `### Result` and the
    JSON of the value. pages: url -> (final url, value the page's outerHTML evaluates to)."""
    s = FastMCP("playwright")
    state = {"value": None}

    @s.tool
    def browser_navigate(url: str):
        calls.append(("navigate", url))
        final, state["value"] = pages.get(url, (url, None))
        return (f"### Ran Playwright code\n```js\nawait page.goto('{url}');\n```\n\n### Page state\n"
                f"- Page URL: {final}\n- Page Title: Rendered\n- Page Snapshot: [snapshot](page-1.yml)\n")

    @s.tool
    def browser_evaluate(function: str):
        calls.append(("evaluate", function))
        return (f"### Result\n{json.dumps(state['value'])}\n\n### Ran Playwright code\n```js\n"
                f"await page.evaluate('{function}');\n```")

    @s.tool
    def browser_close():
        calls.append(("close",))
        state["value"] = None
        return "### Result\nNo open tabs. Navigate to a URL to create one."

    return s


@contextlib.contextmanager
def _gateway(server: FastMCP, *, enabled: bool = True):
    spec = ServerSpec(name="playwright", namespace="browser", transport="inproc", target="playwright",
                      strip_prefix="browser_", timeout_s=10, enabled_default=enabled)
    agents = load_registry(COMMITTED, {}).agents  # the committed crawler allow-list
    gw = Gateway(Registry(servers=(spec,), agents=agents), engine=None, inproc={"playwright": server}, env={})
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()
        assert not [t for t in threading.enumerate() if t.name == "mcp-gateway"]


RENDERED = _page("Rendered docs", _prose("Client routing"),
                 "".join(f'<a href="/docs/p{i}">Page {i}</a> ' for i in range(1, 6))
                 + '<a href="https://other.test/x">Elsewhere</a>')


def _site_with_spa(tmp_path):
    return _site(tmp_path / "net", {ROOT: SPA, **{f"https://spa.test/docs/p{i}": _page(f"P{i}", _prose(f"Topic {i}"))
                                                  for i in range(1, 6)}})


def _root_doc(engine, rid):
    return next(d for d in _docs(engine, rid) if d.url == ROOT)


def test_needs_browser_page_is_rendered_and_chunked(engine, tmp_path, clock):
    calls = []
    with _gateway(_playwright({ROOT: (ROOT, RENDERED)}, calls)) as gw:
        ctx = _ctx(engine, _site_with_spa(tmp_path), crawl_max_depth=0)
        ctx.gateway = gw
        rid = _roadmap(engine, "spa", [("docs", "doc", ROOT)])
        _drain(ctx, rid, clock)
    assert calls == [("navigate", ROOT), ("evaluate", EVALUATE_FUNCTION), ("close",)]
    doc = _root_doc(engine, rid)
    assert (doc.status, doc.reason, doc.meta["rendered"], doc.meta["final_url"]) == ("ok", None, "browser", ROOT)
    assert "Client routing detail number 0" in doc.markdown and doc.title == "Rendered docs"
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Section).where(Section.document_id == doc.id)) > 0
    res = _resources(engine, rid)["docs"]
    assert (res.status, res.stats["rendered_by_browser"]) == ("done", 1)
    rep = _report(engine, rid)
    assert rep["rendered_by_browser"] == 1
    assert rep["resources"][0]["rendered_by_browser"] == 1
    assert "1 page rendered by the browser" in rep["summary"]
    assert rep["skipped"] == []


def test_render_leaving_site_is_skipped(engine, tmp_path, clock):
    calls = []
    with _gateway(_playwright({ROOT: ("https://login.other.test/", RENDERED)}, calls)) as gw:
        ctx = _ctx(engine, _site_with_spa(tmp_path))
        ctx.gateway = gw
        rid = _roadmap(engine, "away", [("docs", "doc", ROOT)])
        _drain(ctx, rid, clock)
    # the gateway refused the page and reset the tab; nothing was read from it
    assert calls == [("navigate", ROOT), ("navigate", "about:blank"), ("close",)]
    doc = _root_doc(engine, rid)
    assert (doc.status, doc.reason, doc.meta["detail"]) == ("skipped", "needs_browser", "left the site")
    assert doc.markdown == "" and "rendered" not in doc.meta
    rep = _report(engine, rid)
    assert rep["rendered_by_browser"] == 0 and "rendered_by_browser" not in rep["resources"][0]
    assert "rendered by the browser" not in rep["summary"]
    assert [(x["url"], x["reason"]) for x in rep["skipped"]] == [(ROOT, "needs_browser")]


def test_browser_down_keeps_phase2_behaviour(engine, tmp_path, clock):
    calls = []
    with _gateway(_playwright({ROOT: (ROOT, RENDERED)}, calls), enabled=False) as gw:
        ctx = _ctx(engine, _site_with_spa(tmp_path))
        ctx.gateway = gw
        rid = _roadmap(engine, "down", [("docs", "doc", ROOT)])
        _drain(ctx, rid, clock)
    assert calls == []
    doc = _root_doc(engine, rid)
    assert (doc.status, doc.reason, doc.markdown) == ("skipped", "needs_browser", "")
    assert doc.meta["detail"].startswith("browser unavailable: ") and "disabled" in doc.meta["detail"]
    res = _resources(engine, rid)["docs"]
    assert (res.status, res.reason) == ("skipped", "needs_browser")
    with Session(engine) as s:  # nothing discovered, nothing indexed: exactly Phase 2
        assert {j.type for j in s.scalars(select(Job).where(Job.roadmap_id == rid))} == {
            "ingest_resource", "fetch_page"}


def test_render_off_or_no_gateway_keeps_phase2_behaviour(engine, tmp_path, clock):
    calls = []
    site = _site_with_spa(tmp_path)
    with _gateway(_playwright({ROOT: (ROOT, RENDERED)}, calls)) as gw:
        ctx = _ctx(engine, site, browser_render=False)
        ctx.gateway = gw
        rid = _roadmap(engine, "off", [("docs", "doc", ROOT)])
        _drain(ctx, rid, clock)
    assert calls == []
    doc = _root_doc(engine, rid)
    assert (doc.status, doc.reason, doc.meta["detail"]) == ("skipped", "needs_browser", "browser rendering is off")
    rid = _roadmap(engine, "nogw", [("docs", "doc", ROOT)])
    _drain(_ctx(engine, site), rid, clock)
    doc = _root_doc(engine, rid)
    assert (doc.status, doc.reason, doc.meta["detail"]) == ("skipped", "needs_browser", "browser unavailable: no gateway")


def test_rendered_links_are_discovered_within_caps(engine, tmp_path, clock):
    calls = []
    with _gateway(_playwright({ROOT: (ROOT, RENDERED)}, calls)) as gw:
        ctx = _ctx(engine, _site_with_spa(tmp_path), crawl_max_pages=3)
        ctx.gateway = gw
        rid = _roadmap(engine, "caps", [("docs", "doc", ROOT)])
        _drain(ctx, rid, clock)
    with Session(engine) as s:
        pages = sorted(j.payload["url"] for j in s.scalars(
            select(Job).where(Job.roadmap_id == rid, Job.type == "fetch_page")))
    # the resource's own page takes one of the 3 slots; two rendered links take the rest; other hosts never
    assert pages == [ROOT, "https://spa.test/docs/p1", "https://spa.test/docs/p2"]
    assert [c[0] for c in calls] == ["navigate", "evaluate", "close"]  # the linked pages are plain HTTP pages
    res = _resources(engine, rid)["docs"]
    assert (res.status, res.stats["pages"], res.stats["rendered_by_browser"]) == ("done", 3, 1)


def test_redirected_spa_is_rendered_at_its_final_url(engine, tmp_path, clock):
    """apex -> www: the browser opens where the fetch ended (fr.url), and the resource's own page takes its policy
    and scope host from there, so the render is not "left the site"."""
    apex, www = "https://apex.test/docs/", "https://www.apex.test/docs/"
    rendered = _page("Rendered docs", _prose("Client routing"), '<a href="/docs/p1">P1</a>')
    site = _site(tmp_path / "net", {
        apex: {"status": 301, "body": "", "headers": {"location": www}},
        www: SPA,
        "https://www.apex.test/docs/p1": _page("P1", _prose("Topic 1"))})
    calls = []
    with _gateway(_playwright({apex: (www, rendered), www: (www, rendered)}, calls)) as gw:
        ctx = _ctx(engine, site)
        ctx.gateway = gw
        rid = _roadmap(engine, "apex", [("docs", "doc", apex)])
        _drain(ctx, rid, clock)
    assert calls[0] == ("navigate", www) and [c[0] for c in calls] == ["navigate", "evaluate", "close"]
    doc = next(d for d in _docs(engine, rid) if d.url == apex)
    assert (doc.status, doc.meta["rendered"], doc.meta["final_url"]) == ("ok", "browser", www)
    res = _resources(engine, rid)["docs"]
    assert res.meta["final_url"] == www  # recorded: the rendered links are crawled on www
    assert sorted(d.url for d in _docs(engine, rid)) == [apex, "https://www.apex.test/docs/p1"]


def _render(pages, calls, url=ROOT, scope_host="spa.test"):
    with _gateway(_playwright(pages, calls)) as gw:
        return render_page(gw, url, scope_host=scope_host, resolver=public_resolver)


def test_render_page_result_and_reasons():
    calls = []
    assert _render({ROOT: (ROOT, RENDERED)}, calls) == RenderResult(final_url=ROOT, html=RENDERED)
    # same site for the policy (the URL's own host), but not the resource's crawl scope
    assert _render({ROOT: (ROOT, RENDERED)}, calls, scope_host="www.spa.test") == "left the site"
    assert _render({ROOT: (ROOT, {"not": "html"})}, calls) == "no html"
    assert _render({ROOT: (ROOT, "x" * (5 * 1024 * 1024 + 1))}, calls) == "too_large"
    calls.clear()
    assert _render({}, calls, url="http://127.0.0.1:8000/").startswith("browser unavailable: ")
    assert calls == [("close",)]  # refused before the browser was touched; close is still sent


def test_cli_ingest_runs_a_gateway(engine, db_url, tmp_path, monkeypatch, capsys):
    # R7: the CLI starts (and stops) a gateway too; here its browser is a refused loopback port (no network)
    from backend.tutor.__main__ import main

    registry = tmp_path / "servers.json"
    registry.write_text(json.dumps({"servers": [
        {"name": "playwright", "namespace": "browser", "transport": "http", "url": "http://127.0.0.1:9/mcp",
         "strip_prefix": "browser_", "timeout_s": 5}],
        "agents": {"crawler": ["browser.navigate", "browser.evaluate", "browser.close"]}}))
    for k, v in {"DATABASE_URL": db_url, "NET_FIXTURES_DIR": str(_site_with_spa(tmp_path)), "EMBEDDER": "hash",
                 "PLATFORM_QDRANT_URL": ":memory:", "TRANSCRIPT_ROUTES": "captions", "INGEST_AGENT": "false",
                 "MCP_REGISTRY": str(registry)}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BROWSER_RENDER", raising=False)
    rid = _roadmap(engine, "cli-spa", [("docs", "doc", ROOT)])
    main(["ingest", "cli-spa", "--until-idle"])
    capsys.readouterr()
    doc = _root_doc(engine, rid)
    assert (doc.status, doc.reason) == ("skipped", "needs_browser")
    assert doc.meta["detail"].startswith("browser unavailable: ") and "no gateway" not in doc.meta["detail"]
    assert not [t for t in threading.enumerate() if t.name == "mcp-gateway"]
