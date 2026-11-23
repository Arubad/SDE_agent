"""Run the platform API, or work on one roadmap from the command line.

    python -m backend.tutor [--create-db]                 serve the API
    python -m backend.tutor ingest <slug> [--until-idle]  run the ingestion workers in the foreground
    python -m backend.tutor recheck <slug>                re-check every resource URL (ok / changed / dead)
    python -m backend.tutor report <slug>                 print the ingestion report as JSON
    python -m backend.tutor import-sde [--limit N]        import the demo SDE knowledge base as roadmap sde-mastery
    python -m backend.tutor mcp <server> [--no-workers]   serve one own MCP server (knowledge, learner, ingestion,
                                                          web) on stdio, for Claude Desktop / Claude Code
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _context(settings):
    from backend.tutor.db import make_engine, migrate
    from backend.tutor.ingest.embed import make_embedder
    from backend.tutor.ingest.index import make_index
    from backend.tutor.ingest.net import make_net
    from backend.tutor.jobs.worker import JobContext

    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    return JobContext(engine=engine, net=make_net(settings, engine), settings=settings,
                      index=make_index(settings, make_embedder(settings)))


def _roadmap_id(engine, slug: str):
    from backend.tutor.db import session_scope
    from backend.tutor.repo import get_roadmap_by_slug

    with session_scope(engine) as s:
        r = get_roadmap_by_slug(s, slug)
        if r is None:
            sys.exit(f"No roadmap '{slug}'")
        return r.id, r.status


def _command(args, settings) -> None:
    from sqlalchemy.orm import Session

    from backend.tutor.ingest import handlers
    from backend.tutor.ingest.report import build_report
    from backend.tutor.models import Roadmap

    ctx = _context(settings)
    try:
        rid, status = _roadmap_id(ctx.engine, args.slug)
        if args.command == "ingest":
            if status in ("draft", "parsed"):
                sys.exit(f"Roadmap '{args.slug}' is '{status}'; approve it before ingesting.")
            final = handlers.ingest_roadmap(ctx, rid, until_idle=args.until_idle,
                                            workers=settings.ingest_workers)
            print(json.dumps({"slug": args.slug, "status": final}))
        elif args.command == "recheck":
            out = handlers.recheck_roadmap(ctx, rid)
            print(json.dumps({k: v["status"] for k, v in out.items()}, indent=2))
        else:
            with Session(ctx.engine) as s:
                print(json.dumps(build_report(s, s.get(Roadmap, rid)), indent=2))
    finally:
        ctx.engine.dispose()


def _import_sde(args, settings) -> None:
    from backend.tutor.db import make_engine, migrate
    from backend.tutor.ingest.embed import make_embedder
    from backend.tutor.ingest.import_sde import import_sde
    from backend.tutor.ingest.index import make_index

    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        out = import_sde(engine, make_index(settings, make_embedder(settings)), limit=args.limit)
        print(json.dumps(out, indent=2))
    finally:
        engine.dispose()


def _protect_stdout():
    """For stdio MCP: stdout must carry the protocol only. Keep the real stdout as a private stream for the
    protocol, send everything else that would reach it (print(), logging, C extensions, child processes, which
    inherit fd 1) to stderr, and return the protocol stream."""
    import logging

    sys.stdout.flush()
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    return protocol


def _mcp(args, settings, protocol) -> None:
    """Serve one own MCP server on stdio, with the API's dependencies (migrated database, engine, net, index,
    embedder). `ingestion` also runs the job workers (and the settle hook) unless --no-workers."""
    from backend.tutor.db import make_engine, migrate
    from backend.tutor.ingest.embed import make_embedder
    from backend.tutor.ingest.handlers import HANDLERS, settled_hook
    from backend.tutor.ingest.index import make_index
    from backend.tutor.ingest.net import make_net
    from backend.tutor.jobs.worker import JobContext, WorkerPool
    from backend.tutor.mcp.build import build_own_server
    from backend.tutor.telemetry import setup_tracing

    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    pool = provider = None
    try:
        net = make_net(settings, engine)
        embedder = make_embedder(settings)
        index = make_index(settings, embedder)
        on_settled = settled_hook(engine, settings)
        server = build_own_server(args.server, engine, net, index, embedder, settings, on_settled)
        if args.server == "ingestion" and not args.no_workers and settings.ingest_workers > 0:
            provider = setup_tracing(settings.otel_endpoint)
            ctx = JobContext(engine=engine, net=net, settings=settings, index=index)
            pool = WorkerPool(ctx, HANDLERS, settings.ingest_workers, on_settled=on_settled,
                              tracer_provider=provider)
            pool.start()
        sys.stdout = protocol  # the MCP stdio transport writes to sys.stdout
        try:
            server.run(transport="stdio", show_banner=False)
        finally:
            sys.stdout = sys.stderr
    finally:
        if pool is not None:
            pool.stop()
        if provider is not None:
            provider.shutdown()
        engine.dispose()


def main(argv: list[str] | None = None) -> None:
    from backend.tutor.mcp import OWN_SERVERS
    from backend.tutor.settings import load_settings

    parser = argparse.ArgumentParser(prog="python -m backend.tutor", description="Run the roadmap tutor API.")
    parser.add_argument(
        "--create-db",
        action="store_true",
        help="create the database named in DATABASE_URL if it does not exist, then start",
    )
    sub = parser.add_subparsers(dest="command")
    ingest = sub.add_parser("ingest", help="run the ingestion workers for one roadmap in the foreground")
    ingest.add_argument("slug")
    ingest.add_argument("--until-idle", action="store_true",
                        help="run inline and stop once no job is due (instead of waiting for a final status)")
    sub.add_parser("recheck", help="re-check every resource URL of a roadmap").add_argument("slug")
    imp = sub.add_parser("import-sde", help="import the demo SDE knowledge base as the roadmap sde-mastery")
    imp.add_argument("--limit", type=int, help="import only the first N documents")
    sub.add_parser("report", help="print a roadmap's ingestion report as JSON").add_argument("slug")
    mcp = sub.add_parser("mcp", help="serve one own MCP server on stdio (for Claude Desktop / Claude Code)")
    mcp.add_argument("server", choices=OWN_SERVERS)
    mcp.add_argument("--no-workers", action="store_true",
                     help="ingestion only: do not run the job workers in this process")
    args = parser.parse_args(argv)
    protocol = _protect_stdout() if args.command == "mcp" else None
    settings = load_settings()
    settings.validate()
    if args.create_db:
        from backend.tutor.db import create_database_if_missing

        create_database_if_missing(settings.database_url)
    if args.command == "import-sde":
        _import_sde(args, settings)
        return
    if args.command == "mcp":
        _mcp(args, settings, protocol)
        return
    if args.command:
        _command(args, settings)
        return
    import uvicorn

    # An MCP client (Claude Code, Desktop) keeps a stream open on /mcp/<name>/; without a bound, uvicorn would wait
    # for it forever on Ctrl-C before running the lifespan shutdown.
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, timeout_graceful_shutdown=5)


def build_app(settings):
    """The served app: migrated database, ingestion handlers and the settle hook (finalize the roadmap).
    The engine lives as long as the process (`app.state.engine`)."""
    from backend.tutor.api import create_app
    from backend.tutor.db import make_engine, migrate
    from backend.tutor.ingest.handlers import HANDLERS, settled_hook

    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    return create_app(settings, engine=engine, handlers=HANDLERS, on_settled=settled_hook(engine, settings))


if __name__ == "__main__":
    main()
