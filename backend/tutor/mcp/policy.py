"""The agent URL rule (spec "Security"): an agent may open only URLs from a search result, the user's messages or
the roadmap's resources, plus same-site navigation from those pages. Private, loopback and link-local addresses
are always refused, listed or not.

`UrlPolicy` holds one run's allowed sources; the gateway consults it (see `gateway.Gateway._gate` and `_after`).
A URL passes `refusal` when all of these hold:
- it is an absolute http(s) URL with a host, no userinfo and no backslash (browsers read `\\` as `/`, so
  `https://evil.example\\@good.example/` would reach evil.example while `urlsplit` says good.example);
- `fetch.address_refusal(host, resolver)` is None;
- its host (lowercased, trailing dot dropped) is the host of an allowed URL. That covers the allowed URL itself
  (in any `queue.normalize_url`-equivalent spelling) and same-site navigation; ports and paths are not compared.
Sources that fail the first two checks are never added, so a refused search result allows nothing.

Refusal reasons are fixed strings: they reach the model, and the URL itself may be page-written text."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from backend.tutor.ingest.fetch import PRIVATE_ADDRESS, Resolver, address_refusal, public_resolver

MAX_URL = 2048
NOT_HTTP = "only absolute http(s) URLs with a host"
NOT_LISTED = "not from a search result, the user's messages or the roadmap (or the same site as one)"
PRIVATE = "private, loopback or link-local address"
NO_DNS = "the host does not resolve"
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_TRAILING = ").,;:!?]}>—'\""
_PAGE_URL = re.compile(r"^[ \t]*(?:- )?Page URL:[ \t]*(\S+)", re.M)


def arg_urls(args: Mapping) -> list[str]:
    """The URLs a tool call carries: every top-level argument named `url` or ending in `_url` whose value is a
    string, or the strings in it when it is a list."""
    out: list[str] = []
    for key, value in args.items():
        if key != "url" and not (isinstance(key, str) and key.endswith("_url")):
            continue
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str))
    return out


def page_urls(text: str) -> list[str]:
    """Every `Page URL: <url>` (or `- Page URL: <url>`) line in a browser result."""
    return _PAGE_URL.findall(text)


def search_result_urls(data) -> list[str]:
    """`data["results"][*]["url"]` of a web.search result (R4), the string ones only."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    return [r["url"] for r in results if isinstance(r, dict) and isinstance(r.get("url"), str)]


def text_urls(text: str) -> list[str]:
    """http(s) URLs in free text, trailing punctuation trimmed."""
    return [m.group().rstrip(_TRAILING) for m in _URL_RE.finditer(text or "")]


def _host(url: str) -> str | None:
    """The host of an acceptable http(s) URL, else None (see the module docstring)."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or "\\" in url \
            or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port  # .port raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    return host.lower().rstrip(".") or None


class UrlPolicy:
    def __init__(self, *, resolver: Resolver = public_resolver):
        self._resolver = resolver
        self._hosts: set[str] = set()

    def allow_sources(self, urls: Iterable[str]) -> None:
        """Roadmap resources, the user's URLs or search results; ones that could never be opened are skipped."""
        for url in urls:
            host = _host(url)
            if host is not None and self._address(host) is None:
                self._hosts.add(host)

    def allow_text(self, text: str) -> None:
        """The URLs in a user message."""
        self.allow_sources(text_urls(text))

    def refusal(self, url: str) -> str | None:
        host = _host(url)
        if host is None:
            return NOT_HTTP
        if (why := self._address(host)) is not None:
            return why
        return None if host in self._hosts else NOT_LISTED

    def _address(self, host: str) -> str | None:
        why = address_refusal(host.strip("[]"), self._resolver)
        if why is None:
            return None
        return PRIVATE if why == PRIVATE_ADDRESS else NO_DNS
