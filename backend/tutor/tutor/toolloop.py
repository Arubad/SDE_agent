"""What the model-facing agents (teacher, researcher) share around `run_tool_loop`: the web-namespace tool results
a turn has seen (`Seen`), the seeded URL policy, and the budgeted, retried model turn."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from backend.tutor.ingest.fetch import system_resolver
from backend.tutor.mcp.policy import UrlPolicy, search_result_urls, text_urls
from backend.tutor.tutor.budget import Budget, with_llm_retry
from backend.tutor.untrusted import unwrap

WEB_NAMESPACES = frozenset({"web", "browser", "docs", "repo", "papers", "ref"})  # D11's web tool results
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
    """URLs in this turn's web-namespace tool results: every URL (first seen first), the search-result URLs, and
    per URL a title (from a search result) and an excerpt (the text that came with it)."""

    def __init__(self) -> None:
        self.urls: dict[str, str] = {}  # _key -> URL
        self.search: list[str] = []
        self.titles: dict[str, str] = {}
        self.excerpts: dict[str, str] = {}

    def _add(self, url: str) -> None:
        self.urls.setdefault(url_key(url), url)

    def add(self, data: Any) -> None:
        for url in search_result_urls(data):
            self.search.append(unwrap(url))
        for d in _dicts(data):
            url = next((unwrap(d[k]) for k in ("url", "final_url") if isinstance(d.get(k), str)), None)
            if not url:
                continue
            if isinstance(d.get("title"), str):
                self.titles.setdefault(url_key(url), unwrap(d["title"]))
            text = next((unwrap(d[k]) for k in _EXCERPT_KEYS if isinstance(d.get(k), str) and d[k]), None)
            if text:
                self.excerpts.setdefault(url_key(url), text)
        for s in map(unwrap, _strings(data)):
            for url in text_urls(s):
                self._add(url)
                if s.strip() != url:  # a bare URL leaf is no excerpt
                    self.excerpts.setdefault(url_key(url), s)

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
