import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from qdrant_client import QdrantClient
from sqlalchemy import select, text

from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.index import PlatformIndex, index_document
from backend.tutor.ingest.report import build_report
from backend.tutor.mcp.servers.knowledge import build_knowledge_server
from backend.tutor.models import Document, Resource, Section
from backend.tutor.repo import create_roadmap, get_roadmap_by_slug

pytestmark = pytest.mark.anyio

BODY = ("# Services\n\nKubernetes services expose pods inside the cluster.\n\n## ClusterIP\n\n"
        + " ".join(f"ClusterIP sentence {i} routes traffic to pods internally." for i in range(30))
        + "\n\n## Ingress\n\n" + " ".join(f"Ingress rule {i} sends external http requests to services." for i in range(30)))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def index():
    return PlatformIndex(QdrantClient(":memory:"), "test_kn", HashEmbedder())


@pytest.fixture
def clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))


def _doc(engine, slug, md=BODY, url="https://x.dev/a", kind="page"):
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=slug, title=slug.upper(), source_text="x")
        d = Document(roadmap_id=rm.id, url=url, kind=kind, title="Doc", markdown=md, meta={})
        s.add(d)
        s.flush()
        s.expunge_all()
        return d


async def _call(server, tool, **args):
    async with Client(server) as c:
        return (await c.call_tool(tool, args)).data


async def test_search_returns_parent_sections_deduped(engine, clean, index):
    d = _doc(engine, "ra")
    index_document(engine, index, d, ["k8s"])
    out = await _call(build_knowledge_server(engine, index), "search", roadmap="ra", query="ClusterIP traffic pods")
    assert out["query"] == "ClusterIP traffic pods" and out["roadmap"] == "ra"
    ps = out["passages"]
    ids = [p["chunk_id"] for p in ps]
    assert ps and len(ids) == len(set(ids))
    with session_scope(engine) as s:
        parent = s.get(Section, ids[0])
    assert ps[0]["text"] == parent.text and ps[0]["section"] == parent.path
    assert ps[0]["doc_id"] == str(d.id) and ps[0]["url"] == "https://x.dev/a"
    assert ps[0]["title"] and ps[0]["source_type"] == "web" and ps[0]["timestamp"] is None
    assert len(ps) <= 5


async def test_search_is_tenant_isolated(engine, clean, index):
    a, b = _doc(engine, "ra"), _doc(engine, "rb", url="https://x.dev/b")
    index_document(engine, index, a, [])
    index_document(engine, index, b, [])
    server = build_knowledge_server(engine, index)
    out = await _call(server, "search", roadmap="ra", query="ingress rule http")
    assert out["passages"] and {p["doc_id"] for p in out["passages"]} == {str(a.id)}


async def test_get_section_from_other_roadmap_is_unknown(engine, clean, index):
    a, b = _doc(engine, "ra"), _doc(engine, "rb", url="https://x.dev/b")
    index_document(engine, index, a, [])
    index_document(engine, index, b, [])
    with session_scope(engine) as s:
        sid_b = s.scalars(select(Section.id).where(Section.document_id == b.id)).first()
    server = build_knowledge_server(engine, index)
    got = await _call(server, "get_section", roadmap="rb", section_id=sid_b)
    assert got["chunk_id"] == sid_b and got["text"]
    async with Client(server) as c:
        with pytest.raises(ToolError, match="No section"):
            await c.call_tool("get_section", {"roadmap": "ra", "section_id": sid_b})
        with pytest.raises(ToolError, match="No roadmap 'nope'"):
            await c.call_tool("search", {"roadmap": "nope", "query": "x"})


async def test_video_passage_has_timestamp(engine, clean, index):
    md = ("# Tour\n\n## [12:30] Services\n\n" + "Services expose pods. " * 20
          + "\n\n## [18:00] Outro\n\n" + "Closing words. " * 20)
    d = _doc(engine, "vid", md=md, url="https://www.youtube.com/watch?v=abcdefghijk", kind="video")
    index_document(engine, index, d, [])
    out = await _call(build_knowledge_server(engine, index), "search", roadmap="vid", query="services expose pods")
    top = out["passages"][0]
    assert top["timestamp"] == "12:30–18:00" and top["source_type"] == "youtube"


async def test_report_and_spec_resources(engine, clean, index):
    _doc(engine, "rp")
    with session_scope(engine) as s:
        rm = get_roadmap_by_slug(s, "rp")
        rm.spec = {"title": "T", "stages": []}
        s.add(Resource(roadmap_id=rm.id, key="r1", stage_id="s1", title="R1", kind="doc",
                       url="https://x.dev/r", skill_ids=["k8s"], status="done"))
        expected = json.loads(json.dumps(build_report(s, rm), default=str))
    server = build_knowledge_server(engine, index)
    async with Client(server) as c:
        rep = await c.read_resource("roadmap://rp/report")
        spec = await c.read_resource("roadmap://rp/spec")
    assert rep[0].mimeType == "application/json" and json.loads(rep[0].text) == expected
    assert json.loads(spec[0].text) == {"title": "T", "stages": []}
    res = await _call(server, "list_resources", roadmap="rp")
    assert res == [{"id": "r1", "title": "R1", "url": "https://x.dev/r", "kind": "doc", "status": "done",
                    "skills": ["k8s"]}]
    rms = await _call(server, "list_roadmaps")
    assert {"slug": "rp", "title": "RP", "status": "draft"} in rms


async def test_prompts_tell_model_to_search_and_cite(engine, index):
    async with Client(build_knowledge_server(engine, index)) as c:
        e = await c.get_prompt("explain_concept", {"roadmap": "ra", "concept": "Ingress", "level": "beginner"})
        q = await c.get_prompt("quiz_me", {"roadmap": "ra", "skill": "k8s"})
    for m in (e, q):
        t = m.messages[0].content.text
        assert "search" in t and "cite" in t.lower() and "ra" in t
