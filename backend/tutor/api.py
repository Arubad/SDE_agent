"""Platform HTTP API: health, roadmaps, public-mode passcode guard, tracing and the SPA."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from backend.agent import access
from backend.core import config
from backend.core.trace import start_trace
from backend.tutor.db import make_engine, migrate, session_scope
from backend.tutor.models import Roadmap
from backend.tutor.repo import DuplicateRoadmap, create_roadmap, list_roadmaps
from backend.tutor.settings import Settings
from backend.tutor.telemetry import export_trace, setup_tracing

log = logging.getLogger(__name__)


class RoadmapIn(BaseModel):
    slug: str
    title: str
    source_text: str


def _roadmap_out(r: Roadmap) -> dict:
    return {
        "id": str(r.id),
        "slug": r.slug,
        "title": r.title,
        "status": r.status,
        "created_at": r.created_at.isoformat(),
    }


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


def create_app(settings: Settings, *, engine: Engine | None = None) -> FastAPI:
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
        try:
            yield
        finally:
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

    if settings.public_mode:

        @app.middleware("http")
        async def _passcode_guard(request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/") and path != "/api/health":
                fallback = request.client.host if request.client else "unknown"
                ip = access.client_ip(request.headers, fallback)
                try:
                    ok = access.check_passcode(request.headers.get("x-access-code"), ip)
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
