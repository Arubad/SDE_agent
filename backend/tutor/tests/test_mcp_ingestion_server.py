import asyncio
import threading
from pathlib import Path

import pytest
from fastmcp import Client
from qdrant_client import QdrantClient
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from backend.core import config, fake_llm
from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.index import PlatformIndex
from backend.tutor.ingest.handlers import ingest_roadmap, settled_hook
from backend.tutor.ingest.net import make_net
from backend.tutor.jobs.worker import JobContext
from backend.tutor.mcp.servers import ingestion
from backend.tutor.mcp.servers.ingestion import build_ingestion_server, top_choices
from backend.tutor.settings import Settings

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
K8S = (FIXTURES / "roadmaps" / "kubernetes.txt").read_text()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    monkeypatch.setenv("FAKE_LLM_REPLIES_DIR", str(FIXTURES / "llm_replies"))


@pytest.fixture
def settings(tmp_path):
    return Settings(web_dist=tmp_path / "no-dist", net_fixtures_dir=FIXTURES / "net", ingest_workers=0,
                    ingest_agent=False)


@pytest.fixture
def net(settings, engine):
    return make_net(settings, engine)


@pytest.fixture
def clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
        s.execute(text("DELETE FROM mcp_idempotency"))


@pytest.fixture
def server(engine, net, settings, clean):
    return build_ingestion_server(engine, net, settings, on_settled=settled_hook(engine, settings))


def _args(slug="k8s", **extra):
    return {"slug": slug, "title": "K8s", "source_text": K8S, **extra}


def _row(engine, slug="k8s"):
    with session_scope(engine) as s:
        return s.execute(text("SELECT id, status FROM roadmaps WHERE slug = :s"), {"s": slug}).one_or_none()


def _jobs(engine):
    with session_scope(engine) as s:
        return s.execute(text("SELECT count(*) FROM jobs")).scalar()


def _titles(engine, slug="k8s"):
    with session_scope(engine) as s:
        spec = s.execute(text("SELECT spec FROM roadmaps WHERE slug = :s"), {"s": slug}).scalar()
    return [r["title"] for st in spec["stages"] for r in st["resources"]]


async def test_create_roadmap_elicits_and_approves_on_accept(server, engine):
    seen = []

    async def handler(message, response_type, params, context):
        seen.append(message)
        return ElicitResult(action="accept", content={"approve": True})

    async with Client(server, elicitation_handler=handler) as c:
        data = (await c.call_tool("create_roadmap", _args())).data
    assert data["status"] == "ingesting"
    assert len(seen) == 1 and len(seen[0]) <= 4100
    for title in _titles(engine):
        assert title in seen[0]
    assert _row(engine).status == "ingesting" and _jobs(engine) > 0


async def test_create_roadmap_without_elicitation_stays_parsed(server, engine):
    async def decline(message, response_type, params, context):
        return ElicitResult(action="decline")

    async def cancel(message, response_type, params, context):
        return ElicitResult(action="cancel")

    async def not_approved(message, response_type, params, context):
        return ElicitResult(action="accept", content={"approve": False})

    for i, handler in enumerate((None, decline, cancel, not_approved)):
        slug = f"k8s-{i}"
        async with Client(server, elicitation_handler=handler) as c:
            data = (await c.call_tool("create_roadmap", _args(slug))).data
        assert data["status"] == "parsed" and "approve_roadmap" in data["next"], slug
        assert _row(engine, slug).status == "parsed"
    assert _jobs(engine) == 0


async def test_approve_roadmap_for_clients_without_elicitation(server, engine):
    async with Client(server) as c:
        await c.call_tool("create_roadmap", _args())
        data = (await c.call_tool("approve_roadmap", {"slug": "k8s"})).data
        assert data["status"] == "ingesting"
        with pytest.raises(ToolError, match="only be approved while parsed"):
            await c.call_tool("approve_roadmap", {"slug": "k8s"})
    assert _jobs(engine) > 0


async def test_wait_for_ingestion_reports_progress(server, engine, net, settings, monkeypatch):
    monkeypatch.setattr(ingestion, "POLL_S", 0.05)
    progress = []

    async def handler(message, response_type, params, context):
        return ElicitResult(action="accept", content={"approve": True})

    async def on_progress(p, total, msg):
        progress.append((p, total, msg))

    async with Client(server, elicitation_handler=handler, progress_handler=on_progress) as c:
        await c.call_tool("create_roadmap", _args())
        rid = _row(engine).id
        index = PlatformIndex(QdrantClient(":memory:"), "mcp_ing_chunks", HashEmbedder())
        ctx = JobContext(engine=engine, net=net, settings=settings, index=index)
        t = threading.Thread(target=lambda: ingest_roadmap(ctx, rid, until_idle=True))
        t.start()
        try:
            data = (await c.call_tool("wait_for_ingestion", {"slug": "k8s", "timeout_s": 60})).data
        finally:
            await asyncio.to_thread(t.join)
        report = (await c.call_tool("ingestion_report", {"slug": "k8s"})).data
        status = (await c.call_tool("ingestion_status", {"slug": "k8s"})).data
    assert len(progress) >= 2
    assert all("resources done" in m for _, _, m in progress)
    assert data["status"] in ("ready", "ready_with_gaps", "partial")
    assert status["status"] == data["status"] and "jobs" in status
    assert report


async def test_unknown_slug_and_duplicate_slug_are_tool_errors(server, engine):
    async with Client(server) as c:
        for tool in ("ingestion_status", "approve_roadmap", "ingestion_report"):
            with pytest.raises(ToolError, match="No roadmap 'nope'"):
                await c.call_tool(tool, {"slug": "nope"})
        await c.call_tool("create_roadmap", _args())
        with pytest.raises(ToolError, match="A roadmap with slug 'k8s' already exists"):
            await c.call_tool("create_roadmap", _args())


async def test_llm_unavailable_is_tool_error(server, monkeypatch):
    from backend.core.llm import LLMUnavailable

    def boom(*a, **k):
        raise LLMUnavailable("model down")

    monkeypatch.setattr("backend.tutor.ingest.service.parse_roadmap", boom)
    async with Client(server) as c:
        with pytest.raises(ToolError, match="model down"):
            await c.call_tool("create_roadmap", _args())


async def test_create_roadmap_replay_with_same_key(server, engine):
    async with Client(server) as c:
        first = (await c.call_tool("create_roadmap", _args(idempotency_key="abc"))).data
        calls = len(fake_llm.requests)
        second = (await c.call_tool("create_roadmap", _args(idempotency_key="abc"))).data
    assert first == second and first["status"] == "parsed"
    assert len(fake_llm.requests) == calls == 1
    with session_scope(engine) as s:
        assert s.execute(text("SELECT count(*) FROM roadmaps")).scalar() == 1


async def test_idempotency_key_reused_with_different_args(server, engine):
    async with Client(server) as c:
        await c.call_tool("create_roadmap", _args(idempotency_key="abc"))
        with pytest.raises(ToolError, match="reused with different arguments"):
            await c.call_tool("create_roadmap", _args("other-slug", idempotency_key="abc"))
    assert _row(engine, "other-slug") is None


async def test_failed_call_releases_key(server, engine, monkeypatch):
    from backend.core.llm import LLMUnavailable
    real = "backend.tutor.ingest.service.parse_roadmap"
    import backend.tutor.ingest.service as svc
    orig = svc.parse_roadmap

    def boom(*a, **k):
        raise LLMUnavailable("down")

    monkeypatch.setattr(real, boom)
    async with Client(server) as c:
        with pytest.raises(ToolError):
            await c.call_tool("create_roadmap", _args(idempotency_key="abc"))
        monkeypatch.setattr(real, orig)
        assert _row(engine) is None  # nothing left behind
        data = (await c.call_tool("create_roadmap", _args(idempotency_key="abc"))).data
        assert data["status"] == "parsed"
    with session_scope(engine) as s:
        assert s.execute(text("SELECT count(*) FROM mcp_idempotency")).scalar() == 1  # only the successful retry


async def test_running_claim_is_reported(server, engine):
    from backend.tutor.mcp.idempotency import args_hash
    with session_scope(engine) as s:
        s.execute(text("INSERT INTO mcp_idempotency (tool, key, args_hash, claim_token) VALUES ('approve_roadmap', 'k', :h, 'tok')"),
                  {"h": args_hash({"slug": "x"})})
    async with Client(server) as c:
        with pytest.raises(ToolError, match="still running"):
            await c.call_tool("approve_roadmap", {"slug": "x", "idempotency_key": "k"})


def test_top_choices_highest_score_first_tie_and_none():
    res = [{"resource_id": "a", "candidates": [{"url": "u1", "score": 1}, {"url": "u2", "score": 3},
                                               {"url": "u3", "score": 3}]},
           {"resource_id": "b", "candidates": []}]
    assert top_choices(res) == {"a": "u2", "b": None}


async def test_wait_for_ingestion_returns_at_once_when_parsed(server, engine):
    import time
    async with Client(server) as c:
        await c.call_tool("create_roadmap", _args())  # no elicitation handler: stays parsed
        t0 = time.monotonic()
        data = (await c.call_tool("wait_for_ingestion", {"slug": "k8s", "timeout_s": 60})).data
    assert data["status"] == "parsed" and time.monotonic() - t0 < 5  # POLL_S is 2s and the timeout 60s


def test_approve_resolves_before_taking_the_row_lock(engine, net, settings, monkeypatch):
    order = []
    monkeypatch.setattr(ingestion, "_resolve_choices", lambda *a: order.append("resolve") or {})
    monkeypatch.setattr(ingestion, "_lock_and_approve", lambda *a: order.append("lock") or {"status": "ingesting"})
    assert ingestion._approve_impl(engine, net, settings, None, "rm") == {"status": "ingesting"}
    assert order == ["resolve", "lock"]


async def test_resolution_runs_without_the_row_lock(server, engine, net, settings, monkeypatch):
    async with Client(server) as c:
        await c.call_tool("create_roadmap", _args())
    held = []
    real = ingestion._resolve_choices

    def watch(*a):
        with session_scope(engine) as s:  # a locked row would make NOWAIT fail
            held.append(s.execute(text("SELECT id FROM roadmaps WHERE slug = 'k8s' FOR UPDATE NOWAIT")).first())
        return real(*a)

    monkeypatch.setattr(ingestion, "_resolve_choices", watch)
    assert ingestion._approve_impl(engine, net, settings, settled_hook(engine, settings), "k8s")["status"] == "ingesting"
    assert held and held[0] is not None


def test_status_change_during_resolution_is_a_conflict(clean, engine, net, settings, monkeypatch):
    def resolve_then_change(*a):
        with session_scope(engine) as s:
            s.execute(text("UPDATE roadmaps SET status = 'failed' WHERE slug = 'k8s'"))
        return {}

    with session_scope(engine) as s:
        from backend.tutor.repo import create_roadmap as mk
        mk(s, slug="k8s", title="K", source_text="x").status = "parsed"
    monkeypatch.setattr(ingestion, "_resolve_choices", resolve_then_change)
    with pytest.raises(ValueError, match="only be approved while parsed"):
        ingestion._approve_impl(engine, net, settings, None, "k8s")


def _resources(engine, slug="k8s"):
    with session_scope(engine) as s:
        return {k: (u, st) for k, u, st in s.execute(text(
            "SELECT r.key, r.url, r.status FROM resources r JOIN roadmaps m ON m.id = r.roadmap_id "
            "WHERE m.slug = :s"), {"s": slug})}


async def test_approve_roadmap_with_choices_ingests_exactly_those(server, engine, monkeypatch):
    """R18: the approver saw these choices; nothing is looked up again."""
    async with Client(server) as c:
        await c.call_tool("create_roadmap", _args())
        with session_scope(engine) as s:
            spec = s.execute(text("SELECT spec FROM roadmaps WHERE slug = 'k8s'")).scalar()
        ids = [r["id"] for st in spec["stages"] for r in st["resources"]]
        choices = {ids[0]: "https://chosen.example/a", **{i: None for i in ids[1:]}}

        def no_resolve(*a):
            raise AssertionError("re-resolved")

        monkeypatch.setattr(ingestion, "_resolve_choices", no_resolve)
        with pytest.raises(ToolError, match="Only http"):
            await c.call_tool("approve_roadmap", {"slug": "k8s", "choices": {ids[0]: "ftp://x.example/"}})
        args = {"slug": "k8s", "choices": choices, "idempotency_key": "ch-1"}
        assert (await c.call_tool("approve_roadmap", args)).data["status"] == "ingesting"
        with pytest.raises(ToolError, match="different arguments"):  # the choices are part of the key's args
            await c.call_tool("approve_roadmap", {**args, "choices": {ids[0]: None}})
    got = _resources(engine)
    assert got[ids[0]] == ("https://chosen.example/a", "pending")
    assert all(got[i] == (None, "skipped") for i in ids[1:])


async def test_resolved_choices_keep_a_resource_s_own_url(engine, net):
    """A resource with its own URL is chosen as that URL, not skipped as having no candidate."""
    with session_scope(engine) as s:
        from backend.tutor.repo import create_roadmap as mk
        rm = mk(s, slug="own", title="O", source_text="see https://own.example/doc")
        rm.status = "parsed"
        rm.spec = {"title": "O", "goals": [], "weekly_hours": None, "stages": [
            {"id": "s1", "title": "S", "timeframe": None, "summary": "", "practice_goals": [], "skills": [],
             "resources": [{"id": "res-r1", "title": "Doc", "kind": "article", "url": "https://own.example/doc",
                            "url_hint": None, "query": "q", "skill_ids": []}]}]}
    assert ingestion.resolve_choices(engine, net, "own") == {"res-r1": "https://own.example/doc"}
