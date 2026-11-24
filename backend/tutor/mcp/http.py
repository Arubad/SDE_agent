"""Serve the own MCP servers in the API under /mcp/<name>/ (Streamable HTTP, stateful), behind McpGuard.

The servers depend on the engine, net and index that exist only inside the API's lifespan, while mounts must be
registered when the app is created. So `mount_mcp` registers, per name, a thin dispatcher that looks the server's
HTTP app up in `app.state.mcp_http` at request time (503 until startup has finished), and returns `serve`, the
context manager the lifespan enters once the servers are built: it creates each server's HTTP app, runs its
lifespan (the session manager) for as long as the API runs and publishes it in `app.state.mcp_http` (and the
servers in `app.state.mcp_servers`). Serve instances of their own: never the gateway's inproc ones (another loop).
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.agent import access

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# host[:port], where host is a bracketed IPv6 literal or a plain name / IPv4 address; anything else is refused
_HOST = re.compile(r"^(?:\[(?P<v6>[0-9a-fA-F:.]+)\]|(?P<name>[A-Za-z0-9._-]+))(?::[0-9]{1,5})?$")

LockoutKey = Callable[[Request], str]
Serve = Callable[[Mapping[str, FastMCP]], AbstractAsyncContextManager[None]]


def host_name(value: str | None) -> str | None:
    """The hostname of a Host header, lowercased and without the port; None when it is missing or malformed."""
    m = _HOST.match((value or "").strip())
    if m is None:
        return None
    return (m.group("v6") or m.group("name")).lower()


def loopback_origin(origin: str) -> bool:
    """`http(s)://` plus a loopback host, any port, nothing else."""
    try:
        u = urlsplit(origin.strip())
        u.port  # noqa: B018  (raises on a malformed port)
    except ValueError:
        return False
    return (u.scheme.lower() in ("http", "https") and u.hostname in LOOPBACK_HOSTS and u.username is None
            and u.path in ("", "/") and not u.query and not u.fragment)


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


class McpGuard:
    """ASGI guard in front of an MCP endpoint (DNS rebinding and cross-site protection, plus the public-mode token):

    - the Host header must name a loopback host (any port), except in public mode, where the tunnel's host is
      expected and the Bearer check applies instead;
    - an Origin header, when present, must be loopback (any port) -- in every mode;
    - in public mode, `Authorization: Bearer <PREMIUM_PASSCODE>` is required (constant-time compare and the API's
      passcode lockout, under the same per-client key) -> 401, or 429 while locked out.
    Lifespan scopes pass through; websockets are refused (MCP does not use them)."""

    def __init__(self, app: ASGIApp, *, public_mode: bool, lockout_key: LockoutKey):
        self.app = app
        self.public_mode = public_mode
        self.lockout_key = lockout_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        refusal = self._refusal(Request(scope))
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _refusal(self, request: Request) -> Response | None:
        headers = request.headers
        if not self.public_mode and host_name(headers.get("host")) not in LOOPBACK_HOSTS:
            return _error(403, "This MCP endpoint accepts only a loopback Host header.")
        origin = headers.get("origin")
        if origin is not None and not loopback_origin(origin):
            return _error(403, "Cross-site requests to this MCP endpoint are refused.")
        if self.public_mode:
            scheme, _, token = (headers.get("authorization") or "").partition(" ")
            code = token.strip() if scheme.lower() == "bearer" else None
            try:
                ok = access.check_passcode(code, self.lockout_key(request))
            except access.TooManyAttempts as exc:
                return _error(429, str(exc))
            if not ok:
                return _error(401, "Authorization: Bearer <passcode> required", {"WWW-Authenticate": "Bearer"})
        return None


class _Dispatch:
    """Forward to the server's HTTP app built in the lifespan; 503 while it does not exist (yet)."""

    def __init__(self, app: FastAPI, name: str):
        self.app, self.name = app, name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        target = (getattr(self.app.state, "mcp_http", None) or {}).get(self.name)
        if target is None:
            await _error(503, f"MCP server '{self.name}' is not running.")(scope, receive, send)
            return
        await target(scope, receive, send)


def mount_mcp(app: FastAPI, names: Iterable[str], settings, *, lockout_key: LockoutKey) -> Serve:
    """Mount `/mcp/<name>` for each name, behind McpGuard; returns `serve(servers)` for the lifespan to enter."""
    names = tuple(names)
    for name in names:
        app.mount(f"/mcp/{name}", McpGuard(_Dispatch(app, name), public_mode=settings.public_mode,
                                           lockout_key=lockout_key))

    @asynccontextmanager
    async def serve(servers: Mapping[str, FastMCP]) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            apps = {}
            for name in names:
                http_app = servers[name].http_app(path="/", stateless_http=False)
                await stack.enter_async_context(http_app.lifespan(http_app))
                apps[name] = http_app
            app.state.mcp_http, app.state.mcp_servers = apps, dict(servers)
            try:
                yield
            finally:
                app.state.mcp_http, app.state.mcp_servers = {}, {}

    return serve
