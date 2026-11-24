"""Shared helpers for the tutor graph tests: a fake gateway, deps, a session fixture and event collectors.

Import the fixtures into a test module (`from backend.tutor.tests.tutor_helpers import tutor_session, ...`).
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text

from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.mcp.policy import search_result_urls
from backend.tutor.models import TutorSession
from backend.tutor.repo import create_roadmap
from backend.tutor.settings import Settings
from backend.tutor.tutor.graph import TutorDeps, build_tutor_graph
from backend.tutor.tutor.runtime import TutorRuntime
from backend.tutor.untrusted import unwrap


def _skill(sid, prereqs=()):
    return {"id": sid, "title": sid.upper(), "objectives": [f"understand {sid}"], "prerequisites": list(prereqs)}


SPEC = {"title": "Caching", "goals": ["learn caching"], "weekly_hours": 3, "stages": [
    {"id": "s1", "title": "Basics", "timeframe": None, "summary": "", "resources": [], "practice_goals": [],
     "skills": [_skill("cache-basics"), _skill("eviction", ["cache-basics"])]},
    {"id": "s2", "title": "Distributed", "timeframe": None, "summary": "", "resources": [], "practice_goals": [],
     "skills": [_skill("write-back", ["eviction"])]}]}


@dataclass
class FakeGateway:
    """`tool_defs`, `call` and `release`, recording every call and every released lease. `results` maps a tool name
    (as called: wire or canonical) to its result (a dict, or a callable of the args); a copy is returned. Like the
    real gateway, a `url_sources` result's `results[*].url` (fence stripped) become allowed sources of the call's
    policy."""

    defs: dict[str, list[dict]] = field(default_factory=dict)  # agent -> tool definitions
    results: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict, Any]] = field(default_factory=list)  # (name, args, ctx)
    released: list[str] = field(default_factory=list)
    url_sources: tuple[str, ...] = ("web.search", "web__search")

    async def tool_defs(self, agent: str) -> list[dict]:
        return list(self.defs.get(agent, []))

    async def call(self, name: str, args: dict, ctx) -> dict:
        self.calls.append((name, args, ctx))
        result = self.results.get(name, {"error": f"unknown tool {name}"})
        out = copy.deepcopy(result(args) if callable(result) else result)
        policy = getattr(ctx, "policy", None)
        if policy is not None and name in self.url_sources and "data" in out:
            policy.allow_sources([unwrap(u) for u in search_result_urls(out["data"])])
        return out

    async def release(self, lease: str) -> None:
        self.released.append(lease)


def make_deps(engine=None, **overrides) -> TutorDeps:
    values = dict(engine=engine, gateway=FakeGateway(), index=None, embedder=HashEmbedder(), settings=Settings(),
                  saver=InMemorySaver(), enqueue=None, tracer_provider=None)
    return TutorDeps(**{**values, **overrides})


def make_runtime(engine, groups=None, **overrides) -> TutorRuntime:
    deps = make_deps(engine, **overrides)
    return TutorRuntime(deps, build_tutor_graph(deps, groups))


def new_session(engine, *, mode: str = "tutor", status: str = "active", spec: dict = SPEC) -> str:
    """A ready roadmap with a small spec (unique slug) and a session on it; returns the session id."""
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=f"rm-{uuid.uuid4().hex[:8]}", title="Caching", source_text="x")
        rm.spec, rm.status = spec, "ready"
        s.flush()
        row = TutorSession(roadmap_id=rm.id, mode=mode, status=status)
        s.add(row)
        s.flush()
        return str(row.id)


@pytest.fixture
def clear_memo(engine):
    """The coordinator's classifications are memoised; start every test without earlier answers."""
    with engine.begin() as c:
        c.execute(text("DELETE FROM llm_memo"))


async def run(runtime: TutorRuntime, session_id: str, turn: dict) -> list[dict]:
    return [e async for e in runtime.run_turn(session_id, turn)]


def nodes(events: list[dict]) -> list[str]:
    return [e["name"] for e in events if e["type"] == "node"]


def interrupts(events: list[dict]) -> list[Any]:
    return [e["value"] for e in events if e["type"] == "interrupt"]


def replies(events: list[dict]) -> list[str]:
    return [e["text"] for e in events if e["type"] == "reply"]


def route_requests(requests: list[dict]) -> list[dict]:
    """The fake model requests whose output schema is the coordinator's `TutorRoute`."""
    return [r for r in requests
            if ((r.get("output_config") or {}).get("format") or {}).get("schema", {}).get("title") == "TutorRoute"]


def add_ended_session(engine, session_id: str, *, summary: str | None = None, title: str | None = None,
                      ended_at: datetime | None = None) -> str:
    """An ended session on the same roadmap as `session_id`; returns its id."""
    with session_scope(engine) as s:
        rm_id = s.get(TutorSession, uuid.UUID(session_id)).roadmap_id
        row = TutorSession(roadmap_id=rm_id, mode="tutor", status="ended", summary=summary, title=title,
                           ended_at=ended_at or datetime.now(timezone.utc))
        s.add(row)
        s.flush()
        return str(row.id)


class Enqueued(list):
    """A `JobContext.enqueue`-shaped callable recording `(type, payload, key_parts, roadmap_id)`."""

    def __call__(self, type, payload, key_parts=(), *, roadmap_id):
        self.append((type, payload, tuple(key_parts), roadmap_id))
