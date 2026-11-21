"""Platform settings, read from the environment (backend/.env is loaded by backend.core.config)."""
from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from backend.core import config

REPO_ROOT = Path(__file__).resolve().parents[2]
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}
TRANSCRIPT_ROUTES = ("captions", "ytdlp_cookies", "gemini")
EMBEDDERS = ("bge", "hash")


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default="postgresql+psycopg://tutor:tutor@127.0.0.1:5432/tutor", repr=False
    )
    qdrant_url: str = "http://127.0.0.1:6333"
    collection: str = "roadmap_chunks"
    public_mode: bool = False
    otel_endpoint: str | None = None
    web_dist: Path = REPO_ROOT / "web" / "dist"
    host: str = "127.0.0.1"
    port: int = 8000
    searxng_url: str | None = "http://127.0.0.1:8888"
    youtube_api_key: str | None = field(default=None, repr=False)
    transcript_routes: tuple[str, ...] = ("captions",)
    ytdlp_cookies_browser: str | None = None
    ingest_workers: int = 2  # 0 is valid: the API then starts no workers
    crawl_max_depth: int = 2
    crawl_max_pages: int = 50
    domain_interval_s: float = 1.0
    playlist_max_videos: int = 50
    net_fixtures_dir: Path | None = None
    embedder: str = "bge"  # "bge" or "hash"
    github_token: str | None = field(default=None, repr=False)  # GITHUB_TOKEN: GitHub API rate limits
    ingest_agent: bool = True  # INGEST_AGENT: the Haiku planner/recoverer around the job queue
    ingest_agent_budget_usd: float = 0.05  # per roadmap, all agent runs together
    ingest_agent_max_steps: int = 12
    ingest_agent_recovery_rounds: int = 2
    min_chunks_per_skill: int = 5  # a skill with fewer indexed chunks is a gap the recoverer looks at

    def validate(self) -> None:
        if self.public_mode and not config.PREMIUM_PASSCODE:
            raise SettingsError("PUBLIC_MODE=true requires PREMIUM_PASSCODE to be set")


def _parse_bool(name: str, raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise SettingsError(f"{name} must be one of true/false, 1/0, yes/no or on/off (got {raw!r})")


def _parse_int(name: str, raw: str | None, default: int, minimum: int) -> int:
    if not (raw or "").strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer (got {raw!r})") from None
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum} (got {value})")
    return value


def _parse_interval(name: str, raw: str | None, default: float) -> float:
    if not (raw or "").strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number (got {raw!r})") from None
    if not math.isfinite(value) or value < 0:  # rejects inf and NaN too
        raise SettingsError(f"{name} must be a finite number >= 0 (got {raw!r})")
    return value


def _parse_routes(raw: str | None, cookies_browser: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not (raw or "").strip():
        return default
    routes = tuple(dict.fromkeys(r.strip() for r in raw.split(",") if r.strip()))
    unknown = [r for r in routes if r not in TRANSCRIPT_ROUTES]
    if unknown or not routes:
        raise SettingsError(f"TRANSCRIPT_ROUTES must be a comma list of {TRANSCRIPT_ROUTES} (got {raw!r})")
    if "ytdlp_cookies" in routes and not cookies_browser:
        raise SettingsError("TRANSCRIPT_ROUTES=ytdlp_cookies requires YTDLP_COOKIES_BROWSER to be set")
    return routes


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    d = Settings()
    embedder = (env.get("EMBEDDER") or d.embedder).strip().lower()
    if embedder not in EMBEDDERS:
        raise SettingsError(f"EMBEDDER must be one of {EMBEDDERS} (got {env.get('EMBEDDER')!r})")
    cookies_browser = (env.get("YTDLP_COOKIES_BROWSER") or "").strip() or None
    return Settings(
        database_url=env.get("DATABASE_URL") or d.database_url,
        qdrant_url=env.get("PLATFORM_QDRANT_URL") or d.qdrant_url,
        collection=env.get("PLATFORM_COLLECTION") or d.collection,
        public_mode=_parse_bool("PUBLIC_MODE", env.get("PUBLIC_MODE")),
        otel_endpoint=env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or None,
        web_dist=Path(env["WEB_DIST"]) if env.get("WEB_DIST") else d.web_dist,
        host=env.get("HOST") or d.host,
        port=_parse_int("PORT", env.get("PORT"), d.port, 1),
        searxng_url=(env["SEARXNG_URL"].strip() or None) if "SEARXNG_URL" in env else d.searxng_url,
        youtube_api_key=env.get("YOUTUBE_API_KEY") or None,
        transcript_routes=_parse_routes(env.get("TRANSCRIPT_ROUTES"), cookies_browser, d.transcript_routes),
        ytdlp_cookies_browser=cookies_browser,
        ingest_workers=_parse_int("INGEST_WORKERS", env.get("INGEST_WORKERS"), d.ingest_workers, 0),
        crawl_max_depth=_parse_int("CRAWL_MAX_DEPTH", env.get("CRAWL_MAX_DEPTH"), d.crawl_max_depth, 0),
        crawl_max_pages=_parse_int("CRAWL_MAX_PAGES", env.get("CRAWL_MAX_PAGES"), d.crawl_max_pages, 1),
        domain_interval_s=_parse_interval(
            "DOMAIN_INTERVAL_S", env.get("DOMAIN_INTERVAL_S"), d.domain_interval_s
        ),
        playlist_max_videos=_parse_int(
            "PLAYLIST_MAX_VIDEOS", env.get("PLAYLIST_MAX_VIDEOS"), d.playlist_max_videos, 1
        ),
        net_fixtures_dir=Path(env["NET_FIXTURES_DIR"]) if env.get("NET_FIXTURES_DIR") else None,
        embedder=embedder,
        github_token=(env.get("GITHUB_TOKEN") or "").strip() or None,
        ingest_agent=(_parse_bool("INGEST_AGENT", env.get("INGEST_AGENT"))
                      if (env.get("INGEST_AGENT") or "").strip() else d.ingest_agent),
    )
