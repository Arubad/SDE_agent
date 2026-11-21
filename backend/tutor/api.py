"""Platform HTTP API: health, roadmaps, public-mode passcode guard, tracing and the SPA."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from backend.agent import access
from backend.core import config
from backend.core.llm import LLMUnavailable
from backend.core.trace import start_trace
from backend.tutor.db import make_engine, migrate, session_scope
from backend.tutor.models import Roadmap
from backend.tutor.ingest import service
from backend.tutor.ingest.net import make_net
from backend.tutor.jobs.worker import JobContext, WorkerPool
from backend.tutor.repo import DuplicateRoadmap, create_roadmap, get_roadmap_by_slug, list_roadmaps
from backend.tutor.roadmap.parse import normalize_spec
from backend.tutor.settings import Settings
from backend.tutor.telemetry import export_trace, setup_tracing

log = logging.getLogger(__name__)

EVENTS_INTERVAL_S = 1.0  # progress stream cadence; tests shorten it


class RoadmapIn(BaseModel):
    slug: str
    title: str
    source_text: str


class SpecIn(BaseModel):
    spec: dict


class ApproveIn(BaseModel):
    spec: dict
    choices: dict[str, str | None]


def _roadmap_out(r: Roadmap) -> dict:
    return {
        "id": str(r.id),
        "slug": r.slug,
        "title": r.title,
        "status": r.status,
        "created_at": r.created_at.isoformat(),
    }


def _lockout_key(request: Request) -> str:
    """Per-client key for the passcode lockout. X-Forwarded-For is client-controlled, so it is ignored;
    CF-Connecting-IP is set (overwritten) by the Cloudflare edge in tunnel mode."""
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    return request.client.host if request.client else "unknown"


_TUNNEL_HEADERS = ("cf-connecting-ip", "cf-ray")


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class _TracingMiddleware:
    """Runs each HTTP request inside a Trace, adds X-Trace-Id, and exports the trace after the response."""

    def __init__(self, app, get_provider):
        self.app = app
        self.get_provider = get_provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        trace = start_trace(f"{scope['method']} {scope['path']}")

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"x-trace-id", trace.id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = scope.get("route")
            if route is not None and getattr(route, "path", None):
                trace.name = f"{scope['method']} {route.path}"
            try:
                export_trace(trace, self.get_provider())
            except Exception:  # telemetry must never break a request
                log.debug("trace export failed", exc_info=True)


def create_app(settings: Settings, *, engine: Engine | None = None, handlers: dict | None = None,
               on_settled=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate()
        owns_engine = engine is None
        if owns_engine:
            migrate(settings.database_url)
            app.state.engine = make_engine(settings.database_url)
        else:
            app.state.engine = engine
        app.state.tracer_provider = setup_tracing(settings.otel_endpoint)
        app.state.net = make_net(settings, app.state.engine)
        pool = None
        if handlers and settings.ingest_workers > 0:
            ctx = JobContext(engine=app.state.engine, net=app.state.net, settings=settings, index=None)
            pool = WorkerPool(ctx, handlers, settings.ingest_workers, on_settled=on_settled,
                              tracer_provider=app.state.tracer_provider)
            pool.start()
        try:
            yield
        finally:
            if pool is not None:
                pool.stop()
            provider = app.state.tracer_provider
            if provider is not None:
                provider.shutdown()
            if owns_engine:
                app.state.engine.dispose()

    app = FastAPI(title="Roadmap Tutor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(OperationalError)
    async def _db_unavailable(request: Request, exc: OperationalError):
        log.warning("database unavailable: %s", type(exc).__name__)
        return _error(503, "Database unavailable")

    if not settings.public_mode:

        @app.middleware("http")
        async def _refuse_tunnel_traffic(request: Request, call_next):
            # A Cloudflare tunnel left running (e.g. an orphaned cloudflared after a plain `docker compose up`)
            # must never reach an API that has no passcode guard. The edge always adds these headers.
            if any(h in request.headers for h in _TUNNEL_HEADERS):
                return _error(403, "This server is not in public mode; tunnel traffic is refused.")
            return await call_next(request)

    else:

        @app.middleware("http")
        async def _passcode_guard(request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/") and path != "/api/health":
                try:
                    ok = access.check_passcode(request.headers.get("x-access-code"), _lockout_key(request))
                except access.TooManyAttempts as exc:
                    return _error(429, str(exc))
                if not ok:
                    return _error(401, "Passcode required")
            return await call_next(request)

    @app.get("/api/health")
    def health(request: Request):
        body = {"llm_provider": config.LLM_PROVIDER, "public_mode": settings.public_mode}
        try:
            with request.app.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "degraded", "db": "down", **body}, status_code=503)
        return {"status": "ok", "db": "ok", **body}

    @app.get("/api/roadmaps")
    def get_roadmaps(request: Request):
        with session_scope(request.app.state.engine) as session:
            return [_roadmap_out(r) for r in list_roadmaps(session)]

    @app.post("/api/roadmaps", status_code=201)
    def post_roadmap(body: RoadmapIn, request: Request):
        try:
            with session_scope(request.app.state.engine) as session:
                roadmap = create_roadmap(
                    session, slug=body.slug, title=body.title, source_text=body.source_text
                )
                return _roadmap_out(roadmap)  # inside the scope: created_at loads from the DB
        except DuplicateRoadmap:
            return _error(409, f"A roadmap with slug '{body.slug}' already exists")
        except ValueError as exc:
            return _error(422, str(exc))

    def _detail(session, r: Roadmap) -> dict:
        return {**_roadmap_out(r), "spec": r.spec, **service.progress(session, r)}

    @app.get("/api/roadmaps/{slug}")
    def get_roadmap(slug: str, request: Request):
        with session_scope(request.app.state.engine) as session:
            r = get_roadmap_by_slug(session, slug)
            if r is None:
                return _error(404, f"No roadmap '{slug}'")
            return _detail(session, r)

    @app.post("/api/roadmaps/{slug}/parse")
    def parse_route(slug: str, request: Request):
        try:
            # The row stays locked for the model call so a concurrent approve/parse waits for it.
            with session_scope(request.app.state.engine) as session:
                r = get_roadmap_by_slug(session, slug, lock=True)
                if r is None:
                    return _error(404, f"No roadmap '{slug}'")
                return service.parse_and_resolve(session, request.app.state.net, r)
        except service.Conflict as exc:
            return _error(409, str(exc))
        except LLMUnavailable as exc:
            return _error(503, f"Model unavailable: {exc}")
        except ValueError as exc:
            return _error(422, str(exc))

    @app.put("/api/roadmaps/{slug}/spec")
    def put_spec(slug: str, body: SpecIn, request: Request):
        try:
            with session_scope(request.app.state.engine) as session:
                r = get_roadmap_by_slug(session, slug, lock=True)
                if r is None:
                    return _error(404, f"No roadmap '{slug}'")
                return service.update_spec(session, r, body.spec)
        except service.Conflict as exc:
            return _error(409, str(exc))
        except ValueError as exc:
            return _error(422, str(exc))

    @app.post("/api/roadmaps/{slug}/approve", status_code=202)
    def approve_route(slug: str, body: ApproveIn, request: Request):
        try:
            with session_scope(request.app.state.engine) as session:
                r = get_roadmap_by_slug(session, slug, lock=True)
                if r is None:
                    return _error(404, f"No roadmap '{slug}'")
                if r.status != "parsed":  # 409 before the body is validated
                    raise service.Conflict(f"Roadmap is '{r.status}'; it can only be approved while parsed.")
                spec = normalize_spec(body.spec, r.source_text)
                service.approve(session, r, spec, body.choices)
                return {"status": "ingesting"}
        except service.Conflict as exc:
            return _error(409, str(exc))
        except ValueError as exc:
            return _error(422, str(exc))

    @app.get("/api/roadmaps/{slug}/events")
    async def events(slug: str, request: Request):
        eng = request.app.state.engine

        def snapshot() -> dict | None:
            with session_scope(eng) as session:
                r = get_roadmap_by_slug(session, slug)
                return None if r is None else service.progress(session, r)

        first = await run_in_threadpool(snapshot)
        if first is None:
            return _error(404, f"No roadmap '{slug}'")

        async def stream():
            snap = first
            while snap is not None:
                yield f"event: progress\ndata: {json.dumps(snap)}\n\n"
                if snap["status"] in service.TERMINAL_STATUSES:
                    yield f"event: done\ndata: {json.dumps({'status': snap['status']})}\n\n"
                    return
                await asyncio.sleep(EVENTS_INTERVAL_S)
                snap = await run_in_threadpool(snapshot)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/roadmaps/{slug}/report")
    def report_route(slug: str, request: Request):
        with session_scope(request.app.state.engine) as session:
            r = get_roadmap_by_slug(session, slug)
            if r is None:
                return _error(404, f"No roadmap '{slug}'")
            if r.status not in service.TERMINAL_STATUSES:
                return _error(409, f"Roadmap is '{r.status}'; the report is available once ingestion has settled.")
            try:  # Task 10 provides this module
                from backend.tutor.ingest.report import build_report
            except ModuleNotFoundError as exc:
                if exc.name != "backend.tutor.ingest.report":
                    raise
                return _error(501, "Report not available yet")
            return build_report(session, r)

    dist = Path(settings.web_dist).resolve()
    index = dist / "index.html"
    if index.is_file():

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            if full_path == "api" or full_path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
            return FileResponse(index)

    app.add_middleware(_TracingMiddleware, get_provider=lambda: getattr(app.state, "tracer_provider", None))
    return app
