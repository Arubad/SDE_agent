"""Own `knowledge` MCP server: per-roadmap search over parent sections.

Search retrieves child chunks, groups them under their parent section and returns the parents (the context a
teacher actually needs). Every query is scoped to one roadmap; a section id from another roadmap is unknown."""
from __future__ import annotations

import json
from typing import Annotated, Any

import anyio.to_thread
from fastmcp import FastMCP
from fastmcp.resources import ResourceContent, ResourceResult
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.engine import Engine

from backend.mcp_server import Passage
from backend.tutor.db import session_scope
from backend.tutor.ingest.index import PlatformIndex, get_parents
from backend.tutor.ingest.report import build_report
from backend.tutor.models import Document, Resource, Roadmap, Section
from backend.tutor.repo import get_roadmap_by_slug


def _clock(seconds: float, hours: bool) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if hours else f"{s // 60:02d}:{s % 60:02d}"


def _timestamp(start_s: float | None, end_s: float | None) -> str | None:
    if start_s is None:
        return None
    hours = start_s >= 3600 or (end_s or 0) >= 3600
    if end_s is None:
        return _clock(start_s, hours)
    return f"{_clock(start_s, hours)}–{_clock(end_s, hours)}"


def _source_type(kind: str | None, url: str) -> str:
    if kind == "video":
        return "youtube"
    if kind == "repo" or "github.com" in url.lower():
        return "github"
    if kind == "pdf":
        return "book"
    return "web"


def _passage(sec: Section, payload: dict, score: float) -> dict | None:
    url = payload.get("url") or ""
    title = payload.get("title") or url
    if not (sec.id and url and title and sec.text):
        return None
    return Passage(
        chunk_id=sec.id, doc_id=str(sec.document_id), title=title, section=sec.path or "",
        source_type=_source_type(payload.get("kind"), url), url=url, score=score, text=sec.text,
        timestamp=_timestamp(sec.start_s, sec.end_s),
    ).model_dump()


def _json(value: Any) -> ResourceResult:
    return ResourceResult([ResourceContent(json.dumps(value, default=str), mime_type="application/json")])


def build_knowledge_server(engine: Engine, index: PlatformIndex) -> FastMCP:
    server = FastMCP("knowledge")

    def _roadmap(slug: str) -> tuple[str, str]:
        with session_scope(engine) as s:
            rm = get_roadmap_by_slug(s, slug)
            if rm is None:
                raise ValueError(f"No roadmap '{slug}'")
            return str(rm.id), rm.title

    def _search(roadmap: str, query: str, skill_id: str | None, top_k: int) -> dict:
        rid, _ = _roadmap(roadmap)
        hits = index.search(query, roadmap_id=rid, skill_id=skill_id, top_k=max(20, 4 * top_k))
        best: dict[str, tuple[float, dict]] = {}  # insertion order = best-scoring parent first
        for h in sorted(hits, key=lambda h: -h.score):
            pid = h.payload.get("parent_id")
            if pid and pid not in best:
                best[pid] = (h.score, h.payload)
        sections = {s.id: s for s in get_parents(engine, list(best))}
        passages: list[dict] = []
        for pid, (score, payload) in best.items():
            sec = sections.get(pid)
            if sec is None or str(sec.roadmap_id) != rid:
                continue
            p = _passage(sec, payload, score)
            if p:
                passages.append(p)
            if len(passages) == top_k:
                break
        return {"query": query, "roadmap": roadmap, "passages": passages}

    def _get_section(roadmap: str, section_id: str) -> dict:
        rid, _ = _roadmap(roadmap)
        with session_scope(engine) as s:
            sec = s.get(Section, section_id)
            if sec is None or str(sec.roadmap_id) != rid:
                raise ValueError(f"No section '{section_id}' in roadmap '{roadmap}'")
            doc = s.get(Document, sec.document_id)
            s.expunge_all()
        url = (doc.url if doc else "") or ""
        payload = {"url": url, "title": (doc.title if doc else "") or url, "kind": doc.kind if doc else None}
        p = _passage(sec, payload, 1.0)
        if p is None:
            raise ValueError(f"No section '{section_id}' in roadmap '{roadmap}'")
        return p

    @server.tool
    async def list_roadmaps() -> list[dict]:
        """List the available roadmaps: [{"slug", "title", "status"}]."""
        def run() -> list[dict]:
            with session_scope(engine) as s:
                rows = s.scalars(select(Roadmap).order_by(Roadmap.created_at, Roadmap.slug)).all()
                return [{"slug": r.slug, "title": r.title, "status": r.status} for r in rows]
        return await anyio.to_thread.run_sync(run)

    @server.tool
    async def search(
        roadmap: str,
        query: str,
        skill_id: str | None = None,
        top_k: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> dict:
        """Search one roadmap's ingested material. Returns {"query", "roadmap", "passages": [...]}: whole parent
        sections (breadcrumb in `section`, video position in `timestamp`), best first. Cite the `url`."""
        return await anyio.to_thread.run_sync(_search, roadmap, query, skill_id, top_k)

    @server.tool
    async def get_section(roadmap: str, section_id: str) -> dict:
        """Fetch one section by its id (a `chunk_id` from search) as a passage. The section must belong to
        the given roadmap."""
        return await anyio.to_thread.run_sync(_get_section, roadmap, section_id)

    @server.tool
    async def list_resources(roadmap: str) -> list[dict]:
        """List a roadmap's resources: [{"id", "title", "url", "kind", "status", "skills"}]."""
        def run() -> list[dict]:
            with session_scope(engine) as s:
                rm = get_roadmap_by_slug(s, roadmap)
                if rm is None:
                    raise ValueError(f"No roadmap '{roadmap}'")
                rows = s.scalars(select(Resource).where(Resource.roadmap_id == rm.id).order_by(Resource.key)).all()
                return [{"id": r.key, "title": r.title, "url": r.url, "kind": r.kind, "status": r.status,
                         "skills": list(r.skill_ids or [])} for r in rows]
        return await anyio.to_thread.run_sync(run)

    @server.resource("roadmap://{slug}/spec", mime_type="application/json")
    async def spec(slug: str) -> ResourceResult:
        """The roadmap's approved spec (JSON)."""
        def run() -> Any:
            with session_scope(engine) as s:
                rm = get_roadmap_by_slug(s, slug)
                if rm is None:
                    raise ValueError(f"No roadmap '{slug}'")
                return rm.spec
        return _json(await anyio.to_thread.run_sync(run))

    @server.resource("roadmap://{slug}/report", mime_type="application/json")
    async def report(slug: str) -> ResourceResult:
        """The ingestion report for the roadmap (JSON)."""
        def run() -> Any:
            with session_scope(engine) as s:
                rm = get_roadmap_by_slug(s, slug)
                if rm is None:
                    raise ValueError(f"No roadmap '{slug}'")
                return build_report(s, rm)
        return _json(await anyio.to_thread.run_sync(run))

    @server.prompt
    def explain_concept(roadmap: str, concept: str, level: str = "beginner") -> str:
        """Explain a concept from a roadmap at a given level, grounded in its material."""
        return (f"Explain '{concept}' at a {level} level for the learner's roadmap '{roadmap}'. First call the "
                f"`search` tool with roadmap='{roadmap}' to find the relevant sections. Base the explanation on "
                "what it returns and cite each source by its url (and timestamp for videos).")

    @server.prompt
    def quiz_me(roadmap: str, skill: str) -> str:
        """Quiz the learner on a skill from a roadmap."""
        return (f"Quiz the learner on the skill '{skill}' in roadmap '{roadmap}'. First call the `search` tool "
                f"with roadmap='{roadmap}' and skill_id='{skill}' to gather the material. Ask one question at a "
                "time, grounded in what it returns, and cite the source url when you give feedback.")

    return server
