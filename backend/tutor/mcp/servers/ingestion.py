"""Own `ingestion` MCP server: create, approve and follow a roadmap's ingestion.

`create_roadmap` parses the roadmap (one model call) and asks the human to approve through MCP elicitation; a
client that cannot elicit, or a decline, leaves the roadmap `parsed` for the app or `approve_roadmap`. Writes
take an optional `idempotency_key` (see `idempotency.py`). The model call and the DB work run in threads; the
elicitation is awaited on the server's loop."""
from __future__ import annotations

import anyio
import anyio.to_thread
from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from backend.core.llm import LLMUnavailable
from backend.tutor.db import session_scope
from backend.tutor.ingest import service
from backend.tutor.ingest.report import build_report
from backend.tutor.jobs.worker import maybe_settle
from backend.tutor.mcp.idempotency import arun_idempotent, run_idempotent
from backend.tutor.repo import DuplicateRoadmap, create_roadmap as repo_create_roadmap, get_roadmap_by_slug
from backend.tutor.roadmap.parse import normalize_spec
from backend.tutor.roadmap.resolve import resolve_spec
from backend.tutor.roadmap.spec import RoadmapSpec

POLL_S = 2.0
MAX_MESSAGE_CHARS = 4000
# Resource states that mean its jobs have not finished; everything else counts as "done" in progress messages.
OPEN_RESOURCE_STATES = ("pending", "ingesting")


class Approve(BaseModel):
    approve: bool


def _next(slug: str) -> str:
    return f"Review and approve it in the app at /roadmaps/{slug}, or call approve_roadmap."


def top_choices(resolution: list[dict]) -> dict[str, str | None]:
    """Each resource's highest-scored candidate URL (ties: the first); no candidate -> None (skipped)."""
    out: dict[str, str | None] = {}
    for item in resolution:
        best = None
        for c in item.get("candidates") or []:
            if best is None or c["score"] > best["score"]:
                best = c
        out[item["resource_id"]] = best["url"] if best else None
    return out


def approval_message(spec: dict, choices: dict[str, str | None]) -> str:
    lines = [f"Approve ingesting '{spec.get('title', '')}'?"]
    for stage in spec.get("stages", []):
        lines.append(f"\n{stage.get('title', '')}")
        skills = ", ".join(sk.get("title", "") for sk in stage.get("skills", []))
        if skills:
            lines.append(f"Skills: {skills}")
        for res in stage.get("resources", []):
            url = choices.get(res.get("id")) or "no URL found (skipped)"
            lines.append(f"- {res.get('title', '')}: {url}")
    text = ""
    for i, line in enumerate(lines):
        if len(text) + len(line) + 1 > MAX_MESSAGE_CHARS:
            return text + f"\n…and {len(lines) - i} more"
        text += line + "\n"
    return text.rstrip("\n")


def build_ingestion_server(engine: Engine, net, settings, *, on_settled) -> FastMCP:
    server = FastMCP("ingestion")

    def _find(session, slug: str, lock: bool = False):
        rm = get_roadmap_by_slug(session, slug, lock=lock)
        if rm is None:
            raise ValueError(f"No roadmap '{slug}'")
        return rm

    def _approve(slug: str, choices: dict[str, str | None] | None) -> dict:
        """Mirror the HTTP approve route. Without `choices` the resources are resolved again (no model call)."""
        try:
            with session_scope(engine) as s:
                r = _find(s, slug, lock=True)
                if r.status != "parsed":
                    raise service.Conflict(
                        f"Roadmap is '{r.status}'; it can only be approved while parsed.")
                spec = normalize_spec(r.spec, r.source_text)
                if choices is None:
                    choices = top_choices([x.model_dump(mode="json") for x in resolve_spec(spec, net)])
                service.approve(s, r, spec, choices, agent=settings.ingest_agent)
                roadmap_id = r.id
        except service.Conflict as exc:
            raise ValueError(str(exc)) from exc
        maybe_settle(engine, roadmap_id, on_settled)
        return {"status": "ingesting", "slug": slug}

    def _create_and_parse(slug: str, title: str, source_text: str) -> dict:
        try:
            with session_scope(engine) as s:
                repo_create_roadmap(s, slug=slug, title=title, source_text=source_text)
        except DuplicateRoadmap:
            raise ValueError(f"A roadmap with slug '{slug}' already exists") from None
        try:
            with session_scope(engine) as s:
                return service.parse_and_resolve(s, net, _find(s, slug, lock=True))
        except (service.Conflict, LLMUnavailable) as exc:
            raise ValueError(str(exc)) from exc

    @server.tool
    async def create_roadmap(slug: str, title: str, source_text: str, ctx: Context,
                             idempotency_key: str | None = Field(None, max_length=128)) -> dict:
        """Create a roadmap from pasted text and parse it (one model call). The human is then asked to approve
        ingesting the top-scored resource for each item; without approval it stays parsed for the app or
        approve_roadmap. The human approves this call in their MCP client."""
        async def work() -> dict:
            parsed = await anyio.to_thread.run_sync(_create_and_parse, slug, title, source_text)
            choices = top_choices(parsed["resolution"])
            try:
                answer = await ctx.elicit(approval_message(parsed["spec"], choices), response_type=Approve)
                approved = answer.action == "accept" and bool(answer.data.approve)
            except Exception:  # the client cannot elicit: same as a decline
                approved = False
            if not approved:
                return {"status": "parsed", "slug": slug, "next": _next(slug)}
            return await anyio.to_thread.run_sync(_approve, slug, choices)

        args = {"slug": slug, "title": title, "source_text": source_text}
        return await arun_idempotent(engine, "create_roadmap", idempotency_key, args, work)

    @server.tool
    async def approve_roadmap(slug: str, idempotency_key: str | None = Field(None, max_length=128)) -> dict:
        """Approve a parsed roadmap, ingesting the top-scored resource for each item (for clients without
        elicitation). The human approves this call in their MCP client."""
        return await anyio.to_thread.run_sync(
            run_idempotent, engine, "approve_roadmap", idempotency_key, {"slug": slug},
            lambda: _approve(slug, None))

    def _status(slug: str) -> dict:
        with session_scope(engine) as s:
            return service.progress(s, _find(s, slug))

    @server.tool
    async def ingestion_status(slug: str) -> dict:
        """Job counts, per-resource status and the roadmap status."""
        return await anyio.to_thread.run_sync(_status, slug)

    @server.tool
    async def wait_for_ingestion(slug: str, ctx: Context,
                                 timeout_s: int = Field(600, ge=10, le=3600)) -> dict:
        """Wait until ingestion is finished (ready, ready_with_gaps or failed) or the timeout passes, reporting
        progress. done_jobs counts jobs in a final state (done or dead)."""
        deadline = anyio.current_time() + timeout_s
        while True:
            prog = await anyio.to_thread.run_sync(_status, slug)
            by_state: dict[str, int] = {}
            for states in prog["jobs"].values():
                for state, n in states.items():
                    by_state[state] = by_state.get(state, 0) + n
            total = sum(by_state.values())
            done = by_state.get("done", 0) + by_state.get("dead", 0)
            finished = sum(1 for r in prog["resources"] if r["status"] not in OPEN_RESOURCE_STATES)
            await ctx.report_progress(done, total, f"{prog['status']}: {finished} resources done")
            if prog["status"] in service.TERMINAL_STATUSES or anyio.current_time() >= deadline:
                return prog
            await anyio.sleep(POLL_S)

    @server.tool
    async def ingestion_report(slug: str) -> dict:
        """The honest ingestion report: what was indexed, what was skipped and why."""
        def run() -> dict:
            with session_scope(engine) as s:
                return build_report(s, _find(s, slug))
        return await anyio.to_thread.run_sync(run)

    return server
