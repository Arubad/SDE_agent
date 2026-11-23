"""The MCP gateway: the client side every platform agent's tool call goes through.

- One background thread runs an asyncio loop that owns one `fastmcp.Client` per server. `start()` connects
  nothing: a client is opened on first use and kept open; any transport error or timeout drops it, marks the server
  `down`, and the next use reopens it.
- Names: a tool is `<namespace>.<tool>` (canonical, after `strip_prefix`) in the registry, traces and the UI, and
  `wire_name(...)` (`^[a-zA-Z0-9_-]{1,64}$`) on the wire to Claude. `call` accepts either.
- Allow-lists: an agent sees and may call only the canonical names its registry patterns match (`fnmatch`).
- Results come back in the agent loop's `ToolCaller` shape (`backend/agent/loop.py`):
  - a tool error -> `{"error": <its text>}`;
  - structured content `{"passages": [...]}` -> passed through unchanged (citations need the raw text);
  - other structured content -> `{"data": <it>}`; no structured content -> `{"data": {"text": <joined text>}}`.
  For an `untrusted` server every string leaf of `data` (and an error's text) is fenced with `untrusted.wrap`.
  Then the whole result is capped at `results.CAP` characters of its JSON: the largest string leaves are cut and get
  ` [truncated N chars]` appended (after the fence, which is never cut). `CallContext.raw` (crawler only) skips
  the wrap and uses `results.RAW_CAP`. (Shaping lives in `results.py`.)
- External (non-inproc) servers' tool descriptions reach the model fenced and capped (DESC_CHARS), and every
  `description` inside their input schemas is cut to SCHEMA_DESC_CHARS; own (inproc) descriptions are our code.
- Timeouts: connecting has its own budget (`max(timeout_s, stdio_connect_s)` for stdio, which may download a
  package on first run; `timeout_s` otherwise), then the call gets `timeout_s`. Every timeout or transport error
  comes back as `{"error": ...}`; nothing here raises into the agent loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Mapping
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from mcp.shared.exceptions import McpError

from backend.core.redact import redact
from backend.core.trace import Trace
from backend.tutor.mcp.registry import Registry, ServerSpec, effective_enabled, missing_env
from backend.tutor.mcp.results import _finish, _shape
from backend.tutor.untrusted import wrap

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from backend.tutor.mcp.policy import UrlPolicy

RAW_AGENT = "crawler"
ERROR_CHARS = 300
RETRY_DOWN_S = 30.0  # tool_defs skips a server that failed this recently; call() always retries
DESC_CHARS, SCHEMA_DESC_CHARS = 1024, 300  # external tool descriptions (then fenced) and schema descriptions
_BAD = re.compile(r"[^a-zA-Z0-9_-]")


def _hashed(wire: str, canonical: str) -> str:
    return f"{wire[:55]}_{hashlib.sha1(canonical.encode()).hexdigest()[:8]}"


def wire_name(canonical: str, *, namespace: str | None = None) -> str:
    """The name sent to Claude. Dots in the namespace become `_`, the namespace/tool separator is `__`, any other
    character outside `[a-zA-Z0-9_-]` becomes `_`. Without `namespace` the tool is what follows the last dot
    (namespaces such as `repo.deepwiki` contain dots; tool names rarely do). A name over 64 characters, or one that
    lost characters to `_` (so two names could meet), is cut to 55 + `_` + the first 8 hex of sha1(canonical)."""
    if namespace is not None and canonical.startswith(namespace + "."):
        ns, tool = namespace, canonical[len(namespace) + 1:]
    else:
        ns, _, tool = canonical.rpartition(".")
    ns_w, tool_w = _BAD.sub("_", ns.replace(".", "_")), _BAD.sub("_", tool)
    wire = f"{ns_w}__{tool_w}" if ns else tool_w
    lossy = tool_w != tool or ns_w != ns.replace(".", "_")
    return _hashed(wire, canonical) if lossy or not wire or len(wire) > 64 else wire


@dataclass
class CallContext:
    agent: str
    policy: UrlPolicy | None = None  # Task 4; ignored here
    trace: Trace | None = None
    raw: bool = False  # crawler only: no untrusted wrap, RAW_CAP instead of CAP


@dataclass
class ServerHealth:
    name: str
    namespace: str
    transport: str
    enabled: bool
    state: Literal["ok", "down", "disabled", "missing_env"]
    tools: int = 0
    latency_ms: int | None = None
    error: str | None = None
    checked_at: str | None = None
    note: str = ""


@dataclass
class _Tool:
    spec: ServerSpec
    name: str  # as the server names it
    canonical: str
    wire: str
    description: str
    schema: dict


@dataclass
class _Conn:
    spec: ServerSpec
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: Client | None = None
    tools: list[Any] | None = None  # last listing; kept while down so known names still resolve
    state: str | None = None  # "ok" | "down" | None (never checked)
    error: str | None = None
    latency_ms: int | None = None
    checked_at: str | None = None
    failed_at: float = 0.0


class Gateway:
    def __init__(self, registry: Registry, *, engine: Engine | None = None,
                 inproc: Mapping[str, FastMCP] | None = None, env: Mapping[str, str] | None = None,
                 stdio_connect_s: float = 60.0):
        self.registry = registry
        self.stdio_connect_s = stdio_connect_s
        self.engine = engine
        self.inproc = dict(inproc or {})
        self.env = os.environ if env is None else env
        self._specs = {s.name: s for s in registry.servers}
        self._conns: dict[str, _Conn] = {}
        self._names: dict[str, _Tool] = {}  # wire and canonical -> tool
        self._closing: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # ---- loop thread -------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="mcp-gateway", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        loop = self._loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()

    def stop(self) -> None:
        if self._loop is None:
            return
        with suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._close_all(), self._loop).result(15)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(15)
        self._loop = self._thread = None
        self._conns.clear()

    def _submit(self, coro):
        if self._loop is None:
            coro.close()
            raise RuntimeError("Gateway.start() was not called")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _close_all(self) -> None:
        for conn in self._conns.values():
            self._drop(conn)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    # ---- public API --------------------------------------------------------------------------------------------
    async def tool_defs(self, agent: str) -> list[dict]:
        return await asyncio.wrap_future(self._submit(self._tool_defs(agent)))

    async def call(self, wire_or_canonical: str, args: dict, ctx: CallContext) -> dict:
        return await asyncio.wrap_future(self._submit(self._call(wire_or_canonical, args, ctx)))

    def call_sync(self, name: str, args: dict, ctx: CallContext, timeout: float | None = None) -> dict:
        """For worker threads (never the gateway's own loop thread)."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("call_sync from the gateway loop would deadlock; use call()")
        fut = self._submit(self._call(name, args, ctx))
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            return {"error": f"{name} timed out after {timeout}s"}

    async def health(self, refresh: bool = False) -> list[ServerHealth]:
        return await asyncio.wrap_future(self._submit(self._health(refresh)))

    async def check(self, name: str) -> ServerHealth:
        return await asyncio.wrap_future(self._submit(self._check(self._specs[name])))

    async def reset(self, name: str) -> None:
        """Close that server's connection and forget its tools and health (after an on/off toggle)."""
        await asyncio.wrap_future(self._submit(self._reset(name)))

    # ---- connections (all below run on the gateway loop) -------------------------------------------------------
    def _conn(self, spec: ServerSpec) -> _Conn:
        if spec.name not in self._conns:
            self._conns[spec.name] = _Conn(spec)
        return self._conns[spec.name]

    def _blocked(self, spec: ServerSpec) -> Literal["disabled", "missing_env"] | None:
        if not effective_enabled(self.engine, spec):
            return "disabled"
        return "missing_env" if missing_env(spec, self.env) else None

    def _client(self, spec: ServerSpec) -> Client:
        if spec.transport == "inproc":
            if spec.target not in self.inproc:
                raise RuntimeError(f"no in-process server {spec.target!r}")
            return Client(self.inproc[spec.target])
        if spec.transport == "stdio":
            env = {k: self.env[k] for k in spec.requires_env if self.env.get(k)}
            return Client(StdioTransport(spec.command or "", list(spec.args), env=env or None))
        return Client(StreamableHttpTransport(spec.url or "", headers=dict(spec.headers)))

    async def _connect(self, conn: _Conn, *, relist: bool = False) -> Client:
        """The open client, connecting (and listing tools) if needed. Raises; callers bound it with a timeout."""
        async with conn.lock:
            if conn.client is not None and not relist:
                return conn.client
            t0 = time.perf_counter()
            try:
                if conn.client is None:
                    conn.client = self._client(conn.spec)
                    await conn.client.__aenter__()
                else:
                    await conn.client.ping()
                conn.tools = await conn.client.list_tools()
            except BaseException as e:  # CancelledError here is the connect budget running out (or stop())
                self._fail(conn, self._reason(conn.spec, e, "connect "))
                raise
            conn.state, conn.error, conn.failed_at = "ok", None, 0.0
            conn.latency_ms, conn.checked_at = round((time.perf_counter() - t0) * 1000), _now()
            self._rebuild_names()
            return conn.client

    def _connect_s(self, spec: ServerSpec) -> float:
        return max(spec.timeout_s, self.stdio_connect_s) if spec.transport == "stdio" else spec.timeout_s

    def _reason(self, spec: ServerSpec, e: BaseException, phase: str) -> str:
        if isinstance(e, TimeoutError | asyncio.CancelledError):
            return f"{phase}timed out after {self._connect_s(spec) if phase else spec.timeout_s}s"
        return str(e) or type(e).__name__

    async def _open(self, conn: _Conn, canonical: str) -> Client | dict:
        """The connected client, or the error result for `canonical` (within the connect budget)."""
        try:
            return await asyncio.wait_for(self._connect(conn), self._connect_s(conn.spec))
        except Exception as e:  # _connect already marked the server down (unless it never got the lock)
            if isinstance(e, TimeoutError):
                return {"error": f"{canonical} {self._reason(conn.spec, e, 'connect ')}"}
            return self._unavailable(conn.spec, canonical, conn.error or self._scrub(conn.spec, str(e))[:ERROR_CHARS])

    def _unavailable(self, spec: ServerSpec, canonical: str, reason: str) -> dict:
        """The model-facing error; the reason may be server-written text (e.g. an McpError message), so it is
        fenced for an untrusted server. Health keeps the plain scrubbed reason."""
        return {"error": f"{canonical} unavailable: {wrap(reason) if spec.untrusted else reason}"}

    def _fail(self, conn: _Conn, reason: str) -> None:
        spec = conn.spec
        conn.state, conn.error = "down", self._scrub(spec, reason)[:ERROR_CHARS]
        conn.checked_at, conn.failed_at, conn.latency_ms = _now(), time.monotonic(), None
        self._drop(conn)

    def _drop(self, conn: _Conn) -> None:
        """Forget the client now and close it in the background (a stuck close never delays a result)."""
        client, conn.client = conn.client, None
        if client is not None:
            task = asyncio.get_running_loop().create_task(_quiet_close(client))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def _reset(self, name: str) -> None:
        conn = self._conns.get(name)
        if conn is None:
            return
        async with conn.lock:  # the same _Conn stays, so a call in flight reconnects into it, never into an orphan
            client = conn.client
            conn.client = conn.tools = conn.state = conn.error = conn.latency_ms = conn.checked_at = None
            if client is not None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(_quiet_close(client), conn.spec.timeout_s)
        self._rebuild_names()

    def _scrub(self, spec: ServerSpec, text: str) -> str:
        """Secrets out of an error: this server's header values and required env values (whole, and each
        whitespace-separated piece of 8+ chars, e.g. the token after `Bearer`), then `redact`. Replacing first is
        stricter than the other way round: `redact` may rewrite part of a secret so an exact match would miss.
        Values under 6 characters are not replaced."""
        pieces = set()
        for v in [*spec.headers.values(), *(self.env.get(k, "") for k in spec.requires_env)]:
            if len(v) >= 6:  # shorter values ("true") are not secrets and would mangle the message
                pieces.add(v)
            pieces.update(p for p in v.split() if len(p) >= 8)
        for p in sorted(pieces, key=len, reverse=True):
            text = text.replace(p, "[redacted]")
        return redact(text)

    # ---- names -------------------------------------------------------------------------------------------------
    def _rebuild_names(self) -> None:
        names: dict[str, _Tool] = {}
        tools = []
        for conn in self._conns.values():
            spec = conn.spec
            for t in conn.tools or ():
                short = t.name
                if spec.strip_prefix and short.startswith(spec.strip_prefix) and len(short) > len(spec.strip_prefix):
                    short = short[len(spec.strip_prefix):]
                canonical = f"{spec.namespace}.{short}"
                desc, schema = t.description or "", t.inputSchema or {"type": "object", "properties": {}}
                if spec.transport != "inproc":  # an external server wrote these: fence, and cap
                    desc, schema = wrap(desc[:DESC_CHARS]), _cap_descriptions(schema)
                tools.append(_Tool(spec, t.name, canonical, wire_name(canonical, namespace=spec.namespace),
                                   desc, schema))
        for tool in sorted(tools, key=lambda x: x.canonical):
            if tool.wire in names:  # two namespaces that meet after "." -> "_": hash the later one
                tool.wire = _hashed(tool.wire, tool.canonical)
            names[tool.wire] = names[tool.canonical] = tool
        self._names = names

    def _owners(self, name: str) -> list[tuple[ServerSpec, str]]:
        """(server, guessed canonical) for every namespace whose canonical (`ns.`) or wire (`ns__`) prefix the
        name starts with, longest first. The guess is exact for every name `wire_name` did not hash."""
        found = []
        for spec in self.registry.servers:
            ns = spec.namespace
            for prefix in (ns + ".", _BAD.sub("_", ns.replace(".", "_")) + "__"):
                if name.startswith(prefix) and len(name) > len(prefix):
                    found.append((spec, f"{ns}.{name[len(prefix):]}"))
                    break
        return sorted(found, key=lambda f: -len(f[0].namespace))

    def _allowed(self, agent: str, canonical: str) -> bool:
        return any(fnmatchcase(canonical, p) for p in self.registry.agents.get(agent, ()))

    def _could_allow(self, agent: str, namespace: str) -> bool:
        """Whether any of the agent's patterns could match some `<namespace>.<tool>` (conservative: a pattern's
        literal head and `namespace.` must agree as far as both go). False means refuse without connecting."""
        head = namespace + "."
        for p in self.registry.agents.get(agent, ()):
            lit = re.split(r"[*?\[]", p, maxsplit=1)[0]
            if lit.startswith(head) or head.startswith(lit):
                return True
        return False

    # ---- tool_defs / call --------------------------------------------------------------------------------------
    async def _tool_defs(self, agent: str) -> list[dict]:
        if not self.registry.agents.get(agent):
            return []
        live = await self._load_live()
        up = {s.name for s in live if self._conn(s).client is not None}
        tools = {t.wire: t for t in self._names.values() if t.spec.name in up and self._allowed(agent, t.canonical)}
        return [{"name": w, "description": f"[{t.spec.namespace}] {t.description}", "input_schema": t.schema}
                for w, t in sorted(tools.items())]

    async def _load_live(self) -> list[ServerSpec]:
        """Connect every enabled, configured server (concurrently, each within its timeout) except those that
        failed in the last RETRY_DOWN_S; return the enabled, configured ones."""
        live = [s for s in self.registry.servers if self._blocked(s) is None]
        due = [s for s in live if not self._recently_failed(self._conn(s))]
        await asyncio.gather(*(self._open(self._conn(s), s.namespace) for s in due))
        return live

    def _recently_failed(self, conn: _Conn) -> bool:
        return conn.client is None and conn.state == "down" and time.monotonic() - conn.failed_at < RETRY_DOWN_S

    async def _call(self, name: str, args: dict, ctx: CallContext) -> dict:
        if ctx.raw and ctx.agent != RAW_AGENT:
            return {"error": f"raw results are only for the {RAW_AGENT} agent"}
        tool = self._names.get(name)
        owners = self._owners(name)
        if tool is None:  # not listed yet (first use, after reset): list whichever servers could own it first
            candidates = [s for s, _ in owners] or list(self.registry.servers)
            candidates = [s for s in candidates if self._could_allow(ctx.agent, s.namespace) and not self._blocked(s)]
            guessed = {s.name: c for s, c in owners}
            opened = await asyncio.gather(*(self._open(self._conn(s), guessed.get(s.name, name)) for s in candidates))
            tool = self._names.get(name)
            if tool is None and owners and owners[0][0] in candidates:
                failed = opened[candidates.index(owners[0][0])]
                if isinstance(failed, dict):  # the likely owner is unreachable: report it, do not try again
                    return failed
        guess = (tool.spec, tool.canonical) if tool else (owners[0] if owners else None)
        if guess is None:
            return {"error": f"unknown tool {name}"}
        spec, canonical = guess
        if not self._allowed(ctx.agent, canonical):
            return {"error": f"tool {canonical} is not allowed for {ctx.agent}"}
        if (blocked := self._blocked(spec)) == "disabled":
            return {"error": f"{canonical}: server {spec.name} is disabled"}
        if blocked == "missing_env":
            return {"error": f"{canonical}: server {spec.name} needs {', '.join(missing_env(spec, self.env))}"}
        span = ctx.trace.span(f"mcp:{canonical}", kind="tool", tool=canonical,
                              args=redact(json.dumps(args, default=str, ensure_ascii=False))[:200]) \
            if ctx.trace else nullcontext()
        t0 = time.perf_counter()
        with span as s:
            out = await self._invoke(spec, canonical, name, args, ctx)
            if s is not None:
                s.set(ok="error" not in out, latency_ms=round((time.perf_counter() - t0) * 1000))
        return out

    async def _invoke(self, spec: ServerSpec, canonical: str, name: str, args: dict, ctx: CallContext) -> dict:
        conn = self._conn(spec)

        client = await self._open(conn, canonical)  # (re)lists tools on a new connection
        if isinstance(client, dict):
            return client
        tool = self._names.get(name) or self._names.get(canonical)
        if tool is None or tool.spec.name != spec.name:
            return {"error": f"unknown tool {name}"}
        if not self._allowed(ctx.agent, tool.canonical):  # the pre-connect check used a guessed name
            return {"error": f"tool {tool.canonical} is not allowed for {ctx.agent}"}
        try:
            result = await asyncio.wait_for(client.call_tool(tool.name, args, raise_on_error=False), spec.timeout_s)
        except McpError as e:  # the server answered with a protocol error: the connection is fine
            return _finish({"error": str(e)}, spec, ctx, canonical)
        except Exception as e:
            if conn.client is client:
                self._fail(conn, self._reason(spec, e, ""))
            if isinstance(e, TimeoutError):
                return {"error": f"{canonical} timed out after {spec.timeout_s}s"}
            return self._unavailable(spec, canonical, self._scrub(spec, str(e) or type(e).__name__)[:ERROR_CHARS])
        return _finish(_shape(result, spec), spec, ctx, canonical)

    # ---- health ------------------------------------------------------------------------------------------------
    async def _health(self, refresh: bool) -> list[ServerHealth]:
        async def one(spec: ServerSpec) -> ServerHealth:
            conn = self._conns.get(spec.name)
            if refresh or self._blocked(spec) or conn is None or conn.state is None:
                return await self._check(spec)
            return self._report(spec, conn)

        return list(await asyncio.gather(*(one(s) for s in self.registry.servers)))

    async def _check(self, spec: ServerSpec) -> ServerHealth:
        blocked = self._blocked(spec)
        if blocked:
            h = self._report(spec, None, state=blocked)
            if blocked == "missing_env":
                h.error = "missing env: " + ", ".join(missing_env(spec, self.env))
            return h
        conn = self._conn(spec)
        try:
            await asyncio.wait_for(self._connect(conn, relist=True), self._connect_s(spec))
        except Exception as e:
            if conn.state != "down":  # timed out before _connect could record it (e.g. waiting on the lock)
                self._fail(conn, self._reason(spec, e, "connect "))
        return self._report(spec, conn)

    def _report(self, spec: ServerSpec, conn: _Conn | None, state: str | None = None) -> ServerHealth:
        enabled = state != "disabled"
        if conn is None:
            return ServerHealth(spec.name, spec.namespace, spec.transport, enabled, state or "down", note=spec.note)
        live = conn.state == "ok" and conn.client is not None
        return ServerHealth(spec.name, spec.namespace, spec.transport, enabled, "ok" if live else "down",
                            tools=len(conn.tools or ()) if live else 0, latency_ms=conn.latency_ms,
                            error=None if live else conn.error, checked_at=conn.checked_at, note=spec.note)


# ---- helpers -------------------------------------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _quiet_close(client: Client) -> None:
    with suppress(Exception):
        await client.close()


def _cap_descriptions(schema):
    """A copy of a JSON schema with every string `description` cut to SCHEMA_DESC_CHARS."""
    if isinstance(schema, dict):
        return {k: v[:SCHEMA_DESC_CHARS] if k == "description" and isinstance(v, str) else _cap_descriptions(v)
                for k, v in schema.items()}
    return [_cap_descriptions(v) for v in schema] if isinstance(schema, list) else schema
