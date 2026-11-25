"""What the model-facing agents (teacher, researcher) share around `run_tool_loop`: the source URLs a turn's tool
results named (`Seen`), the seeded URL policy, and the budgeted, retried model turn."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from backend.tutor.ingest.fetch import system_resolver
from backend.tutor.mcp.gateway import wire_name
from backend.tutor.mcp.policy import UrlPolicy, page_urls, search_result_urls, text_urls
from backend.tutor.mcp.registry import DEFAULT_URL_SOURCES
from backend.tutor.tutor.budget import Budget, with_llm_retry
from backend.tutor.untrusted import unwrap

FETCH_TOOLS = frozenset({"web.fetch", wire_name("web.fetch")})
BLANK = "about:blank"
_CITATION = re.compile(r"\[\d+\]")
_EXCERPT_KEYS = ("snippet", "markdown", "content", "text", "summary", "description")


def seeded_policy(deps, urls: Iterable[str]) -> UrlPolicy:
    """A URL policy whose hosts are seeded from `urls` by host, without DNS; `refusal` resolves at use time."""
    policy = UrlPolicy(resolver=deps.url_resolver or system_resolver)
    policy.allow_hosts(list(urls))
    return policy


def budgeted_model_turn(step, budget: Budget, trace, emit: Callable) -> Callable:
    """A `run_tool_loop` model turn: `Budget.check` before each step, then `step` under `with_llm_retry`; a retry
    first emits a reset `reply_delta` so the UI drops partial text."""

    async def model_turn(messages: list[dict], allow_tools: bool):
        budget.check(trace)
        attempts = 0

        async def once():
            nonlocal attempts
            if attempts:
                await emit({"type": "reply_delta", "delta": "", "reset": True})
            attempts += 1
            return await step(messages, allow_tools)

        return await with_llm_retry(once)

    return model_turn


def url_source_names(gateway) -> frozenset[str]:
    """The registry's `url_sources` (search tools), by canonical and by wire name: the tools whose result URLs are
    sources (R21). A gateway without a registry (a test double) may carry `url_sources` itself."""
    registry = getattr(gateway, "registry", None)
    canonical = registry.url_sources if registry is not None else getattr(gateway, "url_sources",
                                                                          DEFAULT_URL_SOURCES)
    return frozenset({*canonical, *(wire_name(c) for c in canonical)})


def url_key(url: str) -> str:
    return url.rstrip("/")


def namespace(name: str) -> str:
    """`kb` of `kb__search` / `kb.search`, `docs` of `docs_mslearn__x` / `docs.mslearn.x`."""
    return re.split(r"[._]", name, maxsplit=1)[0]


def _strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    items = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else ()
    return [s for v in items for s in _strings(v)]


def _dicts(obj) -> list[dict]:
    items = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else ()
    return ([obj] if isinstance(obj, dict) else []) + [d for v in items for d in _dicts(v)]


class Seen:
    """The source URLs this turn's tool results named (R21, amending D11), first seen first, and per URL a title
    and an excerpt (the text that came with it). Sources come only from:
    - a `url_sources` tool (search): its `results[*].url`, else the URLs in its text (also in `search`, the
      search-result URLs);
    - `web.fetch`: its `final_url` (the page's own address; titled and excerpted by the page);
    - `browser.*`: its `Page URL:` lines.
    Never from URLs inside a fetched page's body or a browser snapshot: a page can name any host."""

    def __init__(self, url_sources: Iterable[str] = DEFAULT_URL_SOURCES) -> None:
        self.url_sources = frozenset(url_sources)
        self.urls: dict[str, str] = {}  # _key -> URL
        self.search: list[str] = []
        self.titles: dict[str, str] = {}
        self.excerpts: dict[str, str] = {}

    def _add(self, url: str, title: Any = None, excerpt: Any = None) -> None:
        k = url_key(url)
        self.urls.setdefault(k, url)
        if isinstance(title, str) and title:
            self.titles.setdefault(k, title)
        if isinstance(excerpt, str) and excerpt:
            self.excerpts.setdefault(k, excerpt)

    def add(self, name: str, data: Any) -> None:
        """Record the sources of tool `name`'s (wire or canonical) non-error result `data`."""
        if name in self.url_sources:
            found = [unwrap(u) for u in search_result_urls(data)]
            if found:
                self.search.extend(found)
                for d in _dicts(data):
                    if isinstance(d.get("url"), str) and unwrap(d["url"]) in found:
                        text = next((d[k] for k in _EXCERPT_KEYS if isinstance(d.get(k), str) and d[k]), None)
                        self._add(unwrap(d["url"]), unwrap(d.get("title")), unwrap(text))
                for url in found:
                    self._add(url)
                return
            for s in map(unwrap, _strings(data)):
                for url in text_urls(s):
                    self._add(url, excerpt=s if s.strip() != url else None)  # a bare URL leaf is no excerpt
        elif name in FETCH_TOOLS:
            if isinstance(data, dict) and isinstance(data.get("final_url"), str):
                text = next((data[k] for k in _EXCERPT_KEYS if isinstance(data.get(k), str) and data[k]), None)
                self._add(unwrap(data["final_url"]), unwrap(data.get("title")), unwrap(text))
        elif namespace(name) == "browser":
            for s in map(unwrap, _strings(data)):
                for url in page_urls(s):
                    if url != BLANK:
                        self._add(url, excerpt=s)

    def web_sources(self, answer: str) -> list[dict]:
        """The seen URLs the answer mentions, in first-mention order, deduplicated. Citation markers are dropped
        first: `render_citations` appends `[n]` straight after a block's text, URL included."""
        out, done = [], set()
        for url in text_urls(_CITATION.sub(" ", answer)):
            k = url_key(url)
            if k in self.urls and k not in done:
                done.add(k)
                out.append({"url": url, "title": self.titles.get(k) or url})
        return out
