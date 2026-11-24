"""Own `web` MCP server: SearXNG search and a guarded page fetch.

The fetch goes through Phase 2's `Fetcher` (robots.txt, politeness, private-address guard on every redirect
hop), so this server is safe to expose to an agent. Results are cached in memory per tool."""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Annotated, Any, Literal

import anyio.to_thread
from fastmcp import FastMCP
from pydantic import Field

from backend.tutor.ingest.extract import extract_page
from backend.tutor.ingest.net import Net, SearchUnavailable

CACHE_SIZE = 256


class _Cache:
    """LRU with per-entry expiry; errors are never stored (callers only `put` successes)."""

    def __init__(self, ttl_s: float, clock: Callable[[], float]):
        self._ttl, self._clock = ttl_s, clock
        self._items: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()

    def get(self, key: tuple) -> Any | None:
        hit = self._items.get(key)
        if hit is None:
            return None
        if self._clock() - hit[0] >= self._ttl:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return hit[1]

    def put(self, key: tuple, value: Any) -> None:
        self._items[key] = (self._clock(), value)
        self._items.move_to_end(key)
        while len(self._items) > CACHE_SIZE:
            self._items.popitem(last=False)


def build_web_server(net: Net, *, ttl_s: float = 900,
                     clock: Callable[[], float] = time.monotonic) -> FastMCP:
    server = FastMCP("web")
    searches, fetches = _Cache(ttl_s, clock), _Cache(ttl_s, clock)

    @server.tool
    async def search(
        query: str,
        category: Literal["general", "videos", "it", "science"] = "general",
        max_results: Annotated[int, Field(ge=1, le=20)] = 8,
    ) -> dict:
        """Search the web (via SearXNG). Returns {"results": [{"title", "url", "snippet", "engine"}]}, best
        first. Use category "videos" for video results, "it" for programming and "science" for papers."""
        key = (query, category, max_results)
        if (hit := searches.get(key)) is not None:
            return hit
        if net.search is None:
            raise ValueError("search unavailable: SearXNG is not configured")
        try:
            found = await anyio.to_thread.run_sync(lambda: net.search.search(query, category=category))
        except SearchUnavailable as e:
            raise ValueError(f"search unavailable: {e}") from e
        out = {"results": [{"title": r.title, "url": r.url, "snippet": r.snippet, "engine": r.engine}
                           for r in found[:max_results]]}
        searches.put(key, out)
        return out

    @server.tool
    async def fetch(
        url: str,
        max_chars: Annotated[int, Field(ge=1000, le=50000)] = 20000,
    ) -> dict:
        """Fetch a public web page and return its main content as markdown: {"url", "final_url", "title",
        "markdown"}. Honours robots.txt and refuses private or internal addresses. HTML pages only; PDFs and
        other file types are refused. Login-walled or script-only pages fail with an explanatory error."""
        key = (url, max_chars)
        if (hit := fetches.get(key)) is not None:
            return hit
        fr = await anyio.to_thread.run_sync(lambda: net.fetcher.get(url))
        if fr.outcome != "ok":
            raise ValueError(f"{fr.outcome}: {fr.reason}")
        ctype = (fr.content_type or "").lower()
        if "html" not in ctype or fr.body[:5] == b"%PDF-":
            raise ValueError(f"unsupported_type: {fr.content_type or 'unknown'} is not an HTML page")
        page = extract_page(fr.body.decode("utf-8", errors="replace"), fr.url)
        out = {"url": url, "final_url": fr.url, "title": page.title, "markdown": page.markdown[:max_chars]}
        fetches.put(key, out)
        return out

    return server
