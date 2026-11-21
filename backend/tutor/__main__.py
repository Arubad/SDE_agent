"""Run the platform API, or work on one roadmap from the command line.

    python -m backend.tutor [--create-db]                 serve the API
    python -m backend.tutor ingest <slug> [--until-idle]  run the ingestion workers in the foreground
    python -m backend.tutor recheck <slug>                re-check every resource URL (ok / changed / dead)
    python -m backend.tutor report <slug>                 print the ingestion report as JSON
"""
from __future__ import annotations

import argparse
import json
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


def main(argv: list[str] | None = None) -> None:
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
    sub.add_parser("report", help="print a roadmap's ingestion report as JSON").add_argument("slug")
    args = parser.parse_args(argv)
    settings = load_settings()
    settings.validate()
    if args.create_db:
        from backend.tutor.db import create_database_if_missing

        create_database_if_missing(settings.database_url)
    if args.command:
        _command(args, settings)
        return
    import uvicorn

    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


def build_app(settings):
    """The served app: migrated database, ingestion handlers and the settle hook (finalize the roadmap).
    The engine lives as long as the process (`app.state.engine`)."""
    from backend.tutor.api import create_app
    from backend.tutor.db import make_engine, migrate
    from backend.tutor.ingest.handlers import HANDLERS, settled_hook

    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    return create_app(settings, engine=engine, handlers=HANDLERS, on_settled=settled_hook(engine))


if __name__ == "__main__":
    main()
