"""Render one page in the gateway's browser (Playwright MCP) for the crawler: pages whose HTML has no text
(`needs_browser`) get a second chance before they are reported as skipped.

`render_page` is the only place that builds the crawler's `CallContext` (`agent="crawler"`, `raw=True`: no
untrusted fence, the 5 MB `RAW_CAP`), always with a URL policy whose only source is the page itself, so the
policy's same-host rule is the crawler's same-site rule, and a browser lease of its own (`crawler:<hex>`). The
steps:
1. `browser.navigate {"url": url}`; the final URL is the result's `Page URL:` line (the gateway already refused,
   and reset the tab to about:blank, when that line is off-policy);
2. the final URL's host must be `scope_host` (the resource's crawl scope), else `"left the site"`;
3. `browser.evaluate` of `EVALUATE_FUNCTION`; Playwright MCP answers `### Result\\n<JSON value>`;
4. `browser.close`, always (its errors are ignored), then the lease is released, always.

The result is a `RenderResult`, or a short reason string; nothing here raises for a render that failed.
The browser has one tab and evaluate reads whatever page it shows, so the lease keeps every other session (another
render, a tutor turn) out of it for the whole render. A render that finds it held waits up to the browser call's
timeout, then gets the gateway's BROWSER_BUSY, reported as `BUSY` (`is_busy`): the crawler retries that one later
instead of skipping the page. Each navigate/evaluate is given `2 * timeout_s + 5` s from here (the lease wait,
then the call), so a busy browser reads as `BUSY`, not as a timeout."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

from backend.core.redact import redact
from backend.tutor.ingest.fetch import Resolver, system_resolver
from backend.tutor.mcp.gateway import BROWSER_BUSY, RAW_AGENT, CallContext
from backend.tutor.mcp.policy import UrlPolicy, page_urls
from backend.tutor.mcp.results import RAW_CAP, _leaves

if TYPE_CHECKING:
    from backend.tutor.mcp.gateway import Gateway

EVALUATE_FUNCTION = "() => document.documentElement.outerHTML"
CLOSE_TIMEOUT_S = 10.0
REASON_CHARS = 200
LEFT_SITE = "left the site"
TOO_LARGE = "too_large"
NO_HTML = "no html"
_LEFT_PREFIX = "navigation left the allowed sites"
_RESULT = re.compile(r"^### Result[ \t]*\n(.*?)(?=^###|\Z)", re.M | re.S)
_TRUNCATED = re.compile(r" \[truncated \d+ chars\]$")


@dataclass(frozen=True)
class RenderResult:
    final_url: str
    html: str


def _text(out: dict) -> str:
    """Every string in a gateway result's `data`, joined (Playwright MCP returns text content only)."""
    data = out.get("data")
    if isinstance(data, str):
        return data
    return "\n".join(c[k] for c, k in _leaves(data, [])) if isinstance(data, dict | list) else ""


def _unavailable(error: str) -> str:
    return f"browser unavailable: {redact(' '.join(str(error).split()))[:REASON_CHARS]}"


BUSY = _unavailable(BROWSER_BUSY)  # another session held the browser for the whole wait: worth retrying


def is_busy(reason: object) -> bool:
    """Whether a `render_page` reason is `BUSY` (the browser was in use, not broken)."""
    return reason == BUSY


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _html(text: str) -> str | None:
    """The string value after `### Result` (up to the next `###` heading), else None."""
    m = _RESULT.search(text)
    if m is None:
        return None
    try:
        value = json.loads(m.group(1).strip())
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def render_page(gateway: Gateway, url: str, *, scope_host: str, timeout_s: float = 45,
                resolver: Resolver = system_resolver) -> RenderResult | str:
    """Render `url` in the gateway's browser (see the module docstring). `resolver` is the URL policy's DNS (the
    crawler passes its fetcher's)."""
    policy = UrlPolicy(resolver=resolver)
    policy.allow_sources([url])
    ctx = CallContext(agent=RAW_AGENT, policy=policy, raw=True, lease=f"crawler:{uuid4().hex}")
    try:
        return _render(gateway, url, ctx, scope_host.lower().rstrip("."), timeout_s)
    except Exception as e:  # never raise into the crawler: a render failure is a reason
        return _unavailable(f"{type(e).__name__}: {e}")
    finally:
        try:
            gateway.call_sync("browser.close", {}, ctx, timeout=CLOSE_TIMEOUT_S)
        except Exception:
            pass
        try:
            gateway.release_sync(ctx.lease)
        except Exception:
            pass


def _render(gateway: Gateway, url: str, ctx: CallContext, scope_host: str, timeout_s: float) -> RenderResult | str:
    timeout_s = 2 * timeout_s + 5  # the gateway's lease wait, then the call itself
    nav = gateway.call_sync("browser.navigate", {"url": url}, ctx, timeout=timeout_s)
    if "error" in nav:
        return LEFT_SITE if str(nav["error"]).startswith(_LEFT_PREFIX) else _unavailable(nav["error"])
    finals = [u for u in page_urls(_text(nav)) if u != "about:blank"]
    if not finals:
        return _unavailable("no page URL reported")
    if any(_host(u) != scope_host for u in finals):
        return LEFT_SITE
    ev = gateway.call_sync("browser.evaluate", {"function": EVALUATE_FUNCTION}, ctx, timeout=timeout_s)
    if "error" in ev:
        return _unavailable(ev["error"])
    text = _text(ev)
    html = _html(text)
    if html is None:  # unreadable JSON at the end of a result the gateway cut at RAW_CAP: the page was too large
        return TOO_LARGE if _TRUNCATED.search(text) else NO_HTML
    if len(html.encode("utf-8", "replace")) > RAW_CAP:
        return TOO_LARGE
    return RenderResult(final_url=finals[0], html=html)
