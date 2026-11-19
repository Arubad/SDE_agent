"""Platform settings, read from the environment (backend/.env is loaded by backend.core.config)."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.core import config

REPO_ROOT = Path(__file__).resolve().parents[2]
_TRUE = {"1", "true", "yes", "on"}


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://tutor:tutor@127.0.0.1:5432/tutor"
    qdrant_url: str = "http://127.0.0.1:6333"
    collection: str = "roadmap_chunks"
    public_mode: bool = False
    otel_endpoint: str | None = None
    web_dist: Path = REPO_ROOT / "web" / "dist"
    host: str = "127.0.0.1"
    port: int = 8000

    def validate(self) -> None:
        if self.public_mode and not config.PREMIUM_PASSCODE:
            raise SettingsError("PUBLIC_MODE=true requires PREMIUM_PASSCODE to be set")


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    d = Settings()
    return Settings(
        database_url=env.get("DATABASE_URL") or d.database_url,
        qdrant_url=env.get("PLATFORM_QDRANT_URL") or d.qdrant_url,
        collection=env.get("PLATFORM_COLLECTION") or d.collection,
        public_mode=env.get("PUBLIC_MODE", "").strip().lower() in _TRUE,
        otel_endpoint=env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or None,
        web_dist=Path(env["WEB_DIST"]) if env.get("WEB_DIST") else d.web_dist,
        host=env.get("HOST") or d.host,
        port=int(env.get("PORT") or d.port),
    )
