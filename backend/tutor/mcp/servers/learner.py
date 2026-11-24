"""Own `learner` MCP server: consent-gated memory facts, learning progress and skill locks.

A thin wrapper over `backend/tutor/learner/store.py`, which holds the logic (the tutor calls it directly).
Facts are personal data: `remember` needs the learner's consent and redacts before embedding or storing, and
`recall` and `list_facts` return nothing while consent is off. Progress (`skill_progress`) is learning state
and is kept whatever the consent."""
from __future__ import annotations

from typing import Annotated, Any, Literal

import anyio.to_thread
from fastmcp import FastMCP
from pydantic import Field
from sqlalchemy.engine import Engine

from backend.tutor.ingest.embed import Embedder
from backend.tutor.learner import store


def build_learner_server(engine: Engine, embedder: Embedder) -> FastMCP:
    server = FastMCP("learner")

    @server.tool
    async def remember(
        text: str,
        kind: Literal["fact", "preference", "goal"] = "fact",
        roadmap: str | None = None,
    ) -> dict:
        """Remember something about the learner (needs their consent). The text is redacted first; a
        near-identical earlier memory is updated instead of duplicated; a memory is at most 1000 characters.
        Returns {"id", "action"}."""
        return await anyio.to_thread.run_sync(store.remember, engine, embedder, text, kind, roadmap)

    @server.tool
    async def recall(
        query: str,
        top_k: Annotated[int, Field(ge=1, le=20)] = 5,
        roadmap: str | None = None,
    ) -> list[dict]:
        """Find remembered facts closest to the query: [{"id", "kind", "text", "score"}]. With a roadmap,
        its facts and the general ones; without, only the general ones."""
        return await anyio.to_thread.run_sync(store.recall, engine, embedder, query, top_k, roadmap)

    @server.tool
    async def forget(fact_id: str) -> dict:
        """Permanently delete one remembered fact: {"deleted": bool}."""
        return await anyio.to_thread.run_sync(store.forget, engine, fact_id)

    @server.tool
    async def list_facts(roadmap: str | None = None) -> list[dict]:
        """List remembered facts of one roadmap, or the general ones when no roadmap is given. Empty while
        personal memory is off."""
        return await anyio.to_thread.run_sync(store.list_facts, engine, roadmap)

    @server.tool
    async def get_progress(roadmap: str) -> list[dict]:
        """Every skill of the roadmap in order: {"skill_id", "title", "mastery", "weak_spots", "locked",
        "next_review_at"}. A skill is locked while a prerequisite's mastery is below its unlock threshold."""
        return await anyio.to_thread.run_sync(store.get_progress, engine, roadmap)

    @server.tool
    async def set_mastery(
        roadmap: str,
        skill_id: str,
        mastery: Annotated[float, Field(ge=0, le=1)],
        weak_spots: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record the learner's mastery (0..1) of a skill; weak_spots=None keeps the existing ones."""
        return await anyio.to_thread.run_sync(store.set_mastery, engine, roadmap, skill_id, mastery, weak_spots)

    return server
