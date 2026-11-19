"""Run the platform API: `python -m backend.tutor [--create-db]`."""
from __future__ import annotations

import argparse

import uvicorn

from backend.tutor.api import create_app
from backend.tutor.db import create_database_if_missing
from backend.tutor.settings import load_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.tutor", description="Run the roadmap tutor API.")
    parser.add_argument(
        "--create-db",
        action="store_true",
        help="create the database named in DATABASE_URL if it does not exist, then start",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    settings.validate()
    if args.create_db:
        create_database_if_missing(settings.database_url)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
