"""A polite HTTP fetcher: robots.txt, per-host spacing, size cap, conditional GET, and honest
classification of walls. It never works around a protection; it reports it and moves on."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import Engine, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.tutor.models import HttpCache

USER_AGENT = "RoadmapTutor/0.2 (+local; personal study tool)"
TIMEOUT_S = 15.0
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5

OUTCOMES = (
    "ok", "not_found", "robots_denied", "login_wall", "paywall", "bot_protection",
    "needs_browser", "too_large", "unsupported_type", "error",
)
_TEXT_TYPES = {"application/xhtml+xml", "application/pdf", "application/json", "application/xml"}
_PAYWALL = re.compile(
    r"paywall|subscribe to (?:continue|read)|subscribers[- ]only|this (?:article|content) is for "
    r"(?:members|subscribers)|metered-content",
    re.I,
)
_BOT = re.compile(
    r"cf-chl|cf-browser-verification|just a moment\.\.\.|attention required! \| cloudflare|"
    r"g-recaptcha|h-captcha|hcaptcha|captcha|checking your browser|ddos-guard|px-captcha",
    re.I,
)
_ROOT_DIV = re.compile(r"""<div[^>]+id=["'](?:root|app|__next)["']""", re.I)


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL, after redirects
    status: int
    content_type: str
    body: bytes
    outcome: str
    reason: str | None = None


class HttpCacheStore:
    """Validators and bodies of earlier OK fetches (http_cache table), for conditional GETs."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def get(self, url: str) -> HttpCache | None:
        with Session(self._engine, expire_on_commit=False) as s:
            return s.get(HttpCache, url)

    def put(self, result: FetchResult, headers: dict[str, str]) -> None:
        h = {k.lower(): v for k, v in headers.items()}
        row = {
            "url": result.url, "status": result.status, "headers": h, "body": result.body,
            "etag": h.get("etag"), "last_modified": h.get("last-modified"),
        }
        stmt = pg_insert(HttpCache).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=[HttpCache.url],
            set_={**{k: v for k, v in row.items() if k != "url"}, "fetched_at": func.now()},
        )
        with Session(self._engine) as s, s.begin():
            s.execute(stmt)


@dataclass
class _Raw:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes
    truncated: bool


class _Early(Exception):
    def __init__(self, result: FetchResult):
        self.result = result


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


class Fetcher:
    def __init__(self, client: httpx.Client, *, domain_interval_s: float, user_agent: str = USER_AGENT,
                 cache: HttpCacheStore | None = None, clock=time.monotonic, sleep=time.sleep):
        self._client = client
        self._interval = domain_interval_s
        self._ua = user_agent
        self._cache = cache
        self._clock = clock
        self._sleep = sleep
        self._guard = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}  # None = everything allowed
        self._robots_locks: dict[str, threading.Lock] = {}

    # -- politeness -----------------------------------------------------------------------------
    def _lock_for(self, table: dict[str, threading.Lock], key: str) -> threading.Lock:
        with self._guard:
            return table.setdefault(key, threading.Lock())

    def _wait_turn(self, host: str) -> None:
        """Block until at least `interval` has passed since the last request to this host."""
        with self._lock_for(self._host_locks, host):
            last = self._last.get(host)
            if last is not None:
                wait = last + self._interval - self._clock()
                if wait > 0:
                    self._sleep(wait)
            self._last[host] = self._clock()

    def _robots_allow(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock_for(self._robots_locks, origin):
            if origin not in self._robots:
                self._robots[origin] = self._load_robots(origin)
            rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self._ua, url)

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        try:
            raw = self._download(f"{origin}/robots.txt", {}, check_robots=False)
        except _Early:
            return None
        rp = RobotFileParser()
        if raw.status >= 500:
            rp.disallow_all = True
            rp.modified()  # RFC 9309: robots.txt unreachable means complete disallow
        elif raw.status == 200:
            rp.parse(raw.body.decode("utf-8", "replace").splitlines())
        else:
            return None  # missing or inaccessible robots.txt: everything is allowed
        return rp

    # -- transport ------------------------------------------------------------------------------
    def _download(self, url: str, headers: dict[str, str], *, check_robots: bool) -> _Raw:
        headers = {"User-Agent": self._ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.5", **headers}
        for _ in range(MAX_REDIRECTS + 1):
            try:
                parts = urlsplit(url)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    raise _Early(FetchResult(url, 0, "", b"", "error", "unsupported URL scheme"))
                if check_robots and not self._robots_allow(url):
                    raise _Early(FetchResult(url, 0, "", b"", "robots_denied", "disallowed by robots.txt"))
                self._wait_turn(parts.netloc)
                with self._client.stream("GET", url, headers=headers, timeout=TIMEOUT_S,
                                         follow_redirects=False) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        if urlsplit(url).netloc != parts.netloc:  # never hand credentials to another host
                            headers.pop("Authorization", None)
                        headers.pop("If-None-Match", None)
                        headers.pop("If-Modified-Since", None)
                        continue
                    body, truncated = bytearray(), False
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            body, truncated = body[:MAX_BYTES], True
                            break
                    return _Raw(url, resp.status_code, resp.headers, bytes(body), truncated)
            except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:  # untrusted URLs: a state, not a raise
                raise _Early(FetchResult(url, 0, "", b"", "error", f"{type(e).__name__}: {e}")) from None
        raise _Early(FetchResult(url, 0, "", b"", "error", f"too many redirects (> {MAX_REDIRECTS})"))

    # -- public ---------------------------------------------------------------------------------
    def get(self, url: str, *, headers: dict[str, str] | None = None, robots: bool = True) -> FetchResult:
        """`headers` are extra request headers (an `Authorization` header is dropped on a cross-host redirect).
        `robots=False` is only for documented APIs (e.g. the GitHub REST API), never for crawled pages."""
        cached = self._cache.get(url) if self._cache else None
        conditional: dict[str, str] = {}
        if cached and cached.etag:
            conditional["If-None-Match"] = cached.etag
        if cached and cached.last_modified:
            conditional["If-Modified-Since"] = cached.last_modified
        try:
            raw = self._download(url, {**(headers or {}), **conditional}, check_robots=robots)
        except _Early as e:
            return e.result
        if raw.status == 304 and cached:
            ctype = (cached.headers or {}).get("content-type", "text/html").split(";")[0].strip().lower()
            return FetchResult(url, 200, ctype, cached.body, "ok", None)
        result = self._classify(raw)
        if result.outcome == "ok" and self._cache and (
            raw.headers.get("etag") or raw.headers.get("last-modified")
        ):
            self._cache.put(FetchResult(url, result.status, result.content_type, result.body, "ok", None),
                            dict(raw.headers))
        return result

    def _classify(self, raw: _Raw) -> FetchResult:
        ctype = raw.headers.get("content-type", "").split(";")[0].strip().lower()

        def res(outcome: str, reason: str | None = None) -> FetchResult:
            return FetchResult(raw.url, raw.status, ctype, raw.body, outcome, reason)

        status = raw.status
        text = raw.body.decode("utf-8", "replace") if (not ctype or "html" in ctype or ctype.startswith("text/")) else ""
        if raw.truncated:
            return res("too_large", f"response exceeds {MAX_BYTES} bytes")
        if status in (404, 410):
            return res("not_found", f"HTTP {status}")
        if status == 401:
            return res("login_wall", "HTTP 401: authentication required")
        if status == 402:
            return res("paywall", "HTTP 402: payment required")
        if status in (403, 429, 503) and (
            _BOT.search(text) or raw.headers.get("cf-mitigated") or raw.headers.get("server", "").lower() == "ddos-guard"
        ):
            return res("bot_protection", f"HTTP {status}: bot-protection challenge")
        if status >= 400:
            return res("error", f"HTTP {status}")
        if ctype and ctype not in _TEXT_TYPES and not ctype.startswith("text/"):
            return res("unsupported_type", f"content type {ctype}")
        if not ctype or "html" in ctype:
            visible = _visible_text(text)
            if re.search(r"""<input[^>]+type=["']?password""", text, re.I) and len(visible) < 300:
                return res("login_wall", "login form with a password field")
            if _PAYWALL.search(text):
                return res("paywall", "paywall marker in page")
            if len(visible) < 200 and ("<noscript" in text.lower() or _ROOT_DIV.search(text)):
                return res("needs_browser", "page renders client-side; no text in the HTML")
        return res("ok")
