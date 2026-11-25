"""The MCP gateway: the client side every platform agent's tool call goes through.

- One background thread runs an asyncio loop that owns one `fastmcp.Client` per server. `start()` connects
  nothing: a client is opened on first use and kept open; any transport error or timeout drops it, marks the server
  `down`, and the next use reopens it. The one exception: a call timeout on an in-process server keeps the
  connection (one slow tool of our own is not a dead link).
- Nothing blocking runs on the loop. The servers' on/off state (a database read, `registry.effective_enabled`)
  is cached: re-read in a worker thread at most every ENABLED_TTL_S, at the start of a call, `tool_defs`, `health`
  and `check`; `reset(name)` (after the Settings switch) forces the next read and discards a read already in
  flight. Before the first read, and if a read fails, the last known state (at first the registry default) holds.
- `tool_defs` waits at most TOOL_DEFS_WAIT_S for servers to connect and lists the ones that did; a slower connect
  keeps running in the background under its own connect budget (one per server) and is listed next time.
- Names: a tool is `<namespace>.<tool>` (canonical, after `strip_prefix`) in the registry, traces and the UI, and
  `wire_name(...)` (`^[a-zA-Z0-9_-]{1,64}$`) on the wire to Claude. `call` accepts either.
- Allow-lists: an agent sees and may call only the canonical names its registry patterns match (`fnmatch`).
- The gate (`decision`): registry `rules.deny` beats `rules.ask` beats the allow-list; a denied tool is not listed
  either. Then, with a `CallContext.policy`, every `url` / `*_url` argument must pass the URL policy (`policy.py`).
  Only then is an `ask` call put to `CallContext.approve` (`{"tool", "server", "agent", "args"}`; it runs only on
  True), so nobody is asked about a call that would be refused anyway. All of it runs before anything connects
  whenever the canonical name is known without listing (every canonical name and every unhashed wire name); a
  hashed wire name not listed yet is listed first, but never called before the gate. URL checks (which may
  resolve DNS) run in a worker thread, never on the gateway loop.
- Fenced URLs: every `url` / `*_url` argument has one exact `untrusted.wrap` fence stripped first (the model passes
  a web.search URL back as it was shown); the gate, the approver and the call all get these same arguments.
- With a policy, after the call and before fencing: the URLs in a non-error result of a `registry.url_sources`
  tool (default `web.search`; its `results[*].url`, else every URL in the result's strings) become allowed
  sources, a `web.fetch` whose `final_url` (where its redirects ended) is refused becomes an error, and a
  `browser.*` result whose `Page URL:` is refused, or that reports none (except `close`, `evaluate` and errors),
  becomes an error, after an internal `browser.navigate` to `about:blank` (not gated, not policed, never shown;
  tried twice unless the first attempt timed out, and a failure is otherwise ignored).
- Results come back in the agent loop's `ToolCaller` shape (`backend/agent/loop.py`):
  - a tool error -> `{"error": <its text>}`;
  - structured content `{"passages": [...]}` -> passed through unchanged (citations need the raw text);
  - other structured content -> `{"data": <it>}`; no structured content -> `{"data": {"text": <joined text>}}`.
  For an `untrusted` server every string leaf of `data` (and an error's text) is fenced with `untrusted.wrap`.
  Then the whole result is capped at `results.CAP` characters of its JSON, measured ASCII-escaped (`\\uXXXX`, as
  it is sent): the largest string leaves are cut and get ` [truncated N chars]` appended (after the fence, which is
  never cut). `CallContext.raw` (crawler only) skips the wrap and uses `results.RAW_CAP`. (Shaping lives in
  `results.py`.)
- External (non-inproc) servers' tool descriptions reach the model fenced and capped (DESC_CHARS), and every
  `description` inside their input schemas is cut to SCHEMA_DESC_CHARS; own (inproc) descriptions are our code.
- Timeouts: connecting has its own budget (`max(timeout_s, stdio_connect_s)` for stdio, which may download a
  package on first run; `timeout_s` otherwise), then the call gets `spec.timeout_for(tool)`: the server's
  `tool_timeouts` entry for that tool (keyed by the server's own tool name) or its `timeout_s`. Every timeout or
  transport error comes back as `{"error": ...}`; nothing here raises into the agent loop.
- The browser lease: the server whose namespace is `browser` has one page, so one lease holds it at a time, and
  its calls run one at a time even within a lease (a turn's tool calls are gathered in parallel). A browser call
  runs under `CallContext.lease` (`anon:<agent>` when None; the tutor passes its session id, each crawler render a
  fresh `crawler:<hex>`). After the gate, a call waits until no browser call is in flight and the lease is its
  own, free (`release` / `release_sync`; a no-op for a lease that is not the owner), or idle for LEASE_IDLE_S; at
  most the call's own timeout, then it is refused with BROWSER_BUSY, without contacting the server. A lease that
  takes over from a previous owner first gets the internal `about:blank` navigation below, once, on its first call
  that connects (releasing does not navigate). Other servers ignore leases.
- Traces: with `CallContext.trace`, every call records one `mcp:<canonical>` span (`tool`, redacted `args`, `ok`,
  `latency_ms`). A call that never ran (the gate, a blocked server, an unknown name, a raw request from another
  agent, a busy browser) records it too, with `ok=False, refused=True, reason=<its error, redacted, [:200]>`; the
  canonical name is the best guess when the name did not resolve.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
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
from backend.tutor.mcp.policy import arg_urls, page_urls, search_result_urls, text_urls, unwrap_url_args
from backend.tutor.mcp.results import _finish, _leaves, _shape
from backend.tutor.untrusted import wrap

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from backend.tutor.mcp.policy import UrlPolicy

RAW_AGENT = "crawler"
ERROR_CHARS = 300
RETRY_DOWN_S = 30.0  # tool_defs skips a server that failed this recently; call() always retries
DESC_CHARS, SCHEMA_DESC_CHARS = 1024, 300  # external tool descriptions (then fenced) and schema descriptions
_BAD = re.compile(r"[^a-zA-Z0-9_-]")
_HASHED = re.compile(r"_[0-9a-f]{8}$")  # what `_hashed` ends a wire name with
BLANK = "about:blank"  # where the gateway sends a browser that left the allowed sites
TOOL_DEFS_WAIT_S = 5.0  # tool_defs lists what connected by then; slower servers finish connecting in the background
ENABLED_TTL_S = 5.0  # how long the servers' on/off state (a database read) is reused
LEASE_IDLE_S = 60.0  # a browser lease with no call for this long may be taken over
BROWSER_BUSY = "browser busy: another session is using it"


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
    """One agent's view of a call. `approve` is called (in a worker thread) for every `ask` tool; it may be sync,
    or async, but a coroutine it returns is awaited on the gateway's own event loop: it must not await anything
    bound to another loop (e.g. a Future of the web app's loop). Prefer a sync approver, or bridge explicitly with
    `asyncio.run_coroutine_threadsafe(..., app_loop).result()` inside a sync one."""

    agent: str
    policy: UrlPolicy | None = None  # None: no URL checks (the crawler's jobs carry their own scope)
    trace: Trace | None = None
    raw: bool = False  # crawler only: no untrusted wrap, RAW_CAP instead of CAP
    approve: Callable[[dict], bool | Awaitable[bool]] | None = None  # asked before every `ask` tool call
    lease: str | None = None  # who holds the browser for this call (None: `anon:<agent>`)


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


class _Refused(dict):
    """An error result for a call that never ran (the gate, a blocked server, an unknown name): traced as refused."""


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
        self._connecting: dict[str, asyncio.Task] = {}  # server -> a connect tool_defs stopped waiting for
        self._enabled: dict[str, bool] = {}  # server -> on/off, refreshed off the loop (_refresh_enabled)
        self._enabled_at: float | None = None
        self._enabled_gen = 0  # bumped by reset: a read that started before it is stale
        self._enabled_lock: asyncio.Lock | None = None  # made per loop in start()
        self._lease = {"owner": None, "last": 0.0, "reset": False}  # the browser's holder, when it last used it,
        # and whether its next connected call resets the page first (it took over from a previous owner)
        self._lease_busy = False  # a browser call is in flight (they run one at a time)
        self._lease_dirty = False  # some owner has held the browser: the next new owner's page is reset
        self._lease_cond: asyncio.Condition | None = None  # made per loop in start()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # ---- loop thread -------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._enabled_lock = asyncio.Lock()
        self._lease_cond = asyncio.Condition()
        self._lease["owner"], self._lease_busy = None, False
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
        self._connecting.clear()

    def _submit(self, coro):
        if self._loop is None:
            coro.close()
            raise RuntimeError("Gateway.start() was not called")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _close_all(self) -> None:
        for task in list(self._connecting.values()):
            task.cancel()
        await asyncio.gather(*self._connecting.values(), return_exceptions=True)
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

    async def release(self, lease: str) -> None:
        """Give the browser back (if `lease` holds it); the next owner's first call resets the page."""
        await asyncio.wrap_future(self._submit(self._release(lease)))

    def release_sync(self, lease: str, timeout: float | None = 15) -> None:
        """`release` for worker threads (never the gateway's own loop thread)."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("release_sync from the gateway loop would deadlock; use release()")
        self._submit(self._release(lease)).result(timeout)

    async def health(self, refresh: bool = False) -> list[ServerHealth]:
        return await asyncio.wrap_future(self._submit(self._health(refresh)))

    async def check(self, name: str) -> ServerHealth:
        return await asyncio.wrap_future(self._submit(self._check_now(self._specs[name])))

    async def reset(self, name: str) -> None:
        """Close that server's connection and forget its tools and health (after an on/off toggle)."""
        await asyncio.wrap_future(self._submit(self._reset(name)))

    # ---- connections (all below run on the gateway loop) -------------------------------------------------------
    def _conn(self, spec: ServerSpec) -> _Conn:
        if spec.name not in self._conns:
            self._conns[spec.name] = _Conn(spec)
        return self._conns[spec.name]

    def _blocked(self, spec: ServerSpec) -> Literal["disabled", "missing_env"] | None:
        """From the cached on/off state (the registry default before the first refresh); never queries."""
        if not self._enabled.get(spec.name, spec.enabled_default):
            return "disabled"
        return "missing_env" if missing_env(spec, self.env) else None

    async def _refresh_enabled(self) -> None:
        """Re-read every server's on/off state in a worker thread, at most every ENABLED_TTL_S (`reset` forces
        the next one). A failed read keeps the last known state and is retried after the TTL. A read that a
        `reset` overtook (it may hold the state from before the switch) is discarded and done again."""
        async with self._enabled_lock:
            if self._enabled_at is not None and time.monotonic() - self._enabled_at < ENABLED_TTL_S:
                return
            while True:
                gen = self._enabled_gen
                try:
                    read = await asyncio.to_thread(self._read_enabled)
                except Exception:
                    read = None
                if gen == self._enabled_gen:
                    break
            if read is not None:
                self._enabled = read
            self._enabled_at = time.monotonic()

    def _read_enabled(self) -> dict[str, bool]:
        return {s.name: effective_enabled(self.engine, s) for s in self.registry.servers}

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
        self._enabled_gen += 1  # the switch may just have changed: read it again on next use, and drop a
        self._enabled_at = None  # read already in flight (it may have seen the old state)
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

    def decision(self, agent: str, canonical: str) -> Literal["allow", "ask", "deny"]:
        """The registry `rules` over the agent's allow-list: deny, then ask, then allow. `ask` never grants a tool
        the agent's own patterns do not match."""
        if any(fnmatchcase(canonical, p) for p in self.registry.deny) or not self._allowed(agent, canonical):
            return "deny"
        return "ask" if any(fnmatchcase(canonical, p) for p in self.registry.ask) else "allow"

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
        await self._refresh_enabled()
        live = await self._load_live()
        up = {s.name for s in live if self._conn(s).client is not None}
        tools = {t.wire: t for t in self._names.values()
                 if t.spec.name in up and self.decision(agent, t.canonical) != "deny"}
        return [{"name": w, "description": f"[{t.spec.namespace}] {t.description}", "input_schema": t.schema}
                for w, t in sorted(tools.items())]

    async def _load_live(self) -> list[ServerSpec]:
        """Connect every enabled, configured server (concurrently, each within its connect budget) except those
        that failed in the last RETRY_DOWN_S; return the enabled, configured ones. Waits at most TOOL_DEFS_WAIT_S:
        a slower connect is left running (kept in `_connecting`, one per server) and lists next time."""
        live = [s for s in self.registry.servers if self._blocked(s) is None]
        due = [s for s in live if not self._recently_failed(self._conn(s)) and s.name not in self._connecting]
        tasks = {s.name: asyncio.create_task(self._open(self._conn(s), s.namespace)) for s in due}
        if tasks:
            await asyncio.wait(tasks.values(), timeout=TOOL_DEFS_WAIT_S)
        for name, task in tasks.items():
            if not task.done():
                self._connecting[name] = task
                task.add_done_callback(lambda t, n=name: self._connecting.pop(n, None))
        return live

    def _recently_failed(self, conn: _Conn) -> bool:
        return conn.client is None and conn.state == "down" and time.monotonic() - conn.failed_at < RETRY_DOWN_S

    async def _call(self, name: str, args: dict, ctx: CallContext) -> dict:
        """Resolve, gate, hold the browser lease (browser tools only) and invoke; with `ctx.trace`, one
        `mcp:<canonical>` span either way (a call that never ran is `ok=False, refused=True, reason=<its error>`)."""
        t0 = time.perf_counter()
        await self._refresh_enabled()
        args = unwrap_url_args(args)  # once: the policy check, the approver and the call all see these
        routed = await self._route(name, args, ctx)
        if isinstance(routed[0], ServerSpec):
            spec, canonical = routed
            if spec.namespace != "browser":
                return await self._traced_invoke(spec, canonical, name, args, ctx)
            lease = ctx.lease or f"anon:{ctx.agent}"
            if await self._acquire(lease, self._call_timeout(spec, name, canonical)):
                try:
                    return await self._traced_invoke(spec, canonical, name, args, ctx)
                finally:
                    await self._done(lease)
            routed = (_Refused(error=BROWSER_BUSY), canonical)  # never reached the server
        out, canonical = routed
        if ctx.trace is not None:
            with self._span(ctx, canonical, args) as s:
                _record(s, out, t0)
        return dict(out) if isinstance(out, _Refused) else out

    async def _traced_invoke(self, spec: ServerSpec, canonical: str, name: str, args: dict, ctx: CallContext) -> dict:
        t0 = time.perf_counter()  # the call's latency, not the gate's (an approver may wait for a person)
        with self._span(ctx, canonical, args) as s:
            out = await self._invoke(spec, canonical, name, args, ctx)
            if s is not None:
                _record(s, out, t0)
        return dict(out) if isinstance(out, _Refused) else out

    def _call_timeout(self, spec: ServerSpec, name: str, canonical: str) -> float:
        tool = self._names.get(name) or self._names.get(canonical)
        return spec.timeout_for(tool.name) if tool is not None and tool.spec.name == spec.name else spec.timeout_s

    # ---- the browser lease -------------------------------------------------------------------------------------
    async def _acquire(self, lease: str, timeout: float) -> bool:
        """Hold the browser, alone, for one call (True), or False if it stayed busy or another lease's for
        `timeout` seconds. Taking over from a previous owner marks the page for a reset."""
        deadline = time.monotonic() + timeout
        async with self._lease_cond:
            while True:
                now, owner = time.monotonic(), self._lease["owner"]
                idle = LEASE_IDLE_S - (now - self._lease["last"])
                if not self._lease_busy and owner == lease:
                    self._lease_busy = True
                    return True
                if not self._lease_busy and (owner is None or idle <= 0):
                    self._lease.update(owner=lease, last=now, reset=self._lease_dirty)
                    self._lease_dirty = self._lease_busy = True
                    return True
                left = deadline - now
                if left <= 0:
                    return False
                with suppress(TimeoutError):  # woken by a call ending or a release; else when it may be idle
                    await asyncio.wait_for(self._lease_cond.wait(), left if self._lease_busy else min(left, idle))

    async def _done(self, lease: str) -> None:
        self._lease_busy = False
        if self._lease["owner"] == lease:
            self._lease["last"] = time.monotonic()
        async with self._lease_cond:
            self._lease_cond.notify_all()

    async def _release(self, lease: str) -> None:
        if self._lease["owner"] != lease:
            return
        self._lease["owner"] = None
        async with self._lease_cond:
            self._lease_cond.notify_all()

    def _span(self, ctx: CallContext, canonical: str, args: dict):
        if ctx.trace is None:
            return nullcontext()
        return ctx.trace.span(f"mcp:{canonical}", kind="tool", tool=canonical,
                              args=redact(json.dumps(args, default=str, ensure_ascii=False))[:200])

    async def _route(self, name: str, args: dict, ctx: CallContext) -> tuple[ServerSpec, str] | tuple[dict, str]:
        """(server, canonical) to invoke, or (the result, best-guess canonical) for a call that does not run."""
        tool = self._names.get(name)
        owners = self._owners(name)
        best = tool.canonical if tool else owners[0][1] if owners else name
        if ctx.raw and ctx.agent != RAW_AGENT:
            return _Refused(error=f"raw results are only for the {RAW_AGENT} agent"), best
        gated = None  # (server, canonical) already gated
        if tool is None and owners and (name.startswith(owners[0][0].namespace + ".") or not _HASHED.search(name)):
            gated = (owners[0][0].name, owners[0][1])  # known without listing: gate before anything connects
            if (refused := await self._gate(owners[0][0], owners[0][1], args, ctx)) is not None:
                return refused, best
        if tool is None:  # not listed yet (first use, after reset): list whichever servers could own it first
            candidates = [s for s, _ in owners] or list(self.registry.servers)
            candidates = [s for s in candidates if self._could_allow(ctx.agent, s.namespace) and not self._blocked(s)]
            guessed = {s.name: c for s, c in owners}
            opened = await asyncio.gather(*(self._open(self._conn(s), guessed.get(s.name, name)) for s in candidates))
            tool = self._names.get(name)
            if tool is None and owners and owners[0][0] in candidates:
                failed = opened[candidates.index(owners[0][0])]
                if isinstance(failed, dict):  # the likely owner is unreachable: report it, do not try again
                    return failed, best
        guess = (tool.spec, tool.canonical) if tool else (owners[0] if owners else None)
        if guess is None:
            return _Refused(error=f"unknown tool {name}"), best
        spec, canonical = guess
        if (spec.name, canonical) != gated and (refused := await self._gate(spec, canonical, args, ctx)) is not None:
            return refused, canonical
        if (refused := self._blocked_error(spec, canonical)) is not None:
            return refused, canonical
        return spec, canonical

    async def _invoke(self, spec: ServerSpec, canonical: str, name: str, args: dict, ctx: CallContext) -> dict:
        conn = self._conn(spec)

        client = await self._open(conn, canonical)  # (re)lists tools on a new connection
        if isinstance(client, dict):
            return client
        if spec.namespace == "browser" and self._lease["reset"]:  # a new owner never sees the last one's page;
            self._lease["reset"] = False  # kept until a call connects (a failed connect cannot reset the page)
            await self._blank(conn)
        tool = self._names.get(name) or self._names.get(canonical)
        if tool is None or tool.spec.name != spec.name:
            return _Refused(error=f"unknown tool {name}")
        if tool.canonical != canonical and (refused := await self._gate(spec, tool.canonical, args, ctx)) is not None:
            return refused  # the name now resolves to another tool than the one gated (relisted meanwhile)
        timeout = spec.timeout_for(tool.name)
        try:
            result = await asyncio.wait_for(client.call_tool(tool.name, args, raise_on_error=False), timeout)
        except McpError as e:  # the server answered with a protocol error: the connection is fine
            return _finish({"error": str(e)}, spec, ctx, canonical)
        except TimeoutError:  # an in-process server is our own code on this loop: one slow tool is not a dead link
            if spec.transport != "inproc" and conn.client is client:
                self._fail(conn, f"timed out after {timeout}s")
            return {"error": f"{canonical} timed out after {timeout}s"}
        except Exception as e:
            if conn.client is client:
                self._fail(conn, self._reason(spec, e, ""))
            return self._unavailable(spec, canonical, self._scrub(spec, str(e) or type(e).__name__)[:ERROR_CHARS])
        out = _shape(result, spec)
        if ctx.policy is not None and (refused := await self._after(conn, tool, out, ctx)) is not None:
            return refused
        return _finish(out, spec, ctx, tool.canonical)

    # ---- the gate and the URL policy ---------------------------------------------------------------------------
    async def _gate(self, spec: ServerSpec, canonical: str, args: dict, ctx: CallContext) -> dict | None:
        """The refusal for this call, or None: deny, then the URL arguments (DNS off the loop), then the approver
        for an `ask` tool. Nothing connects."""
        verdict = self.decision(ctx.agent, canonical)
        if verdict == "deny":
            return _Refused(error=f"tool {canonical} is not allowed for {ctx.agent}")
        if ctx.policy is not None and (urls := arg_urls(args)):
            if (reason := await asyncio.to_thread(_first_refusal, ctx.policy, urls)) is not None:
                return _Refused(error=f"refused by URL policy: {reason}")
        if verdict == "ask":  # nobody is asked about a call that cannot run, and a blocked server is refused here
            if (refused := self._blocked_error(spec, canonical)) is not None:
                return refused  # (not left to _call: the server may be enabled before _call looks again)
            if await self._approved(spec, canonical, args, ctx):
                return None
            if ctx.approve is None:
                return _Refused(error=f"approval required for {canonical}")
            return _Refused(error=f"{canonical} was not approved")
        return None

    def _blocked_error(self, spec: ServerSpec, canonical: str) -> dict | None:
        if (blocked := self._blocked(spec)) == "disabled":
            return _Refused(error=f"{canonical}: server {spec.name} is disabled")
        if blocked == "missing_env":
            return _Refused(error=f"{canonical}: server {spec.name} needs {', '.join(missing_env(spec, self.env))}")
        return None

    async def _approved(self, spec: ServerSpec, canonical: str, args: dict, ctx: CallContext) -> bool:
        """Only a literal True approves. The approver gets a copy of the raw arguments (so it cannot change the
        call) and runs off the gateway loop; an async approver's coroutine is awaited here. Raising = refusing."""
        if ctx.approve is None:
            return False
        try:
            payload = {"tool": canonical, "server": spec.name, "agent": ctx.agent, "args": copy.deepcopy(args)}
            answer = await asyncio.to_thread(ctx.approve, payload)
            if inspect.isawaitable(answer):
                answer = await answer
        except Exception:
            return False
        return answer is True

    async def _after(self, conn: _Conn, tool: _Tool, out: dict, ctx: CallContext) -> dict | None:
        """On the raw shaped result (before fencing and capping): the URLs of a `registry.url_sources` tool's
        non-error result become allowed sources (its `results[*].url`, else every URL in its strings); a
        web.fetch whose `final_url` (after redirects) is off-policy becomes an error; a browser result whose
        `Page URL:` is off-policy, or a non-error one with no such line (fail closed; only `close` and the
        crawler's `evaluate` carry none), is replaced by an error and the page is reset. The policy calls (DNS)
        run off the loop."""
        if tool.canonical in self.registry.url_sources and "error" not in out:
            found = search_result_urls(out.get("data")) or [u for c, k in _leaves(out, []) for u in text_urls(c[k])]
            if found:
                await asyncio.to_thread(ctx.policy.allow_sources, found)
        data = out.get("data")
        if tool.canonical == "web.fetch" and isinstance(data, dict) and "final_url" in data:
            if (reason := await asyncio.to_thread(_first_refusal, ctx.policy, [data["final_url"]])) is not None:
                return {"error": f"navigation left the allowed sites: {reason}"}  # a redirect off the sites
            return None
        ns = tool.spec.namespace
        if ns != "browser":
            return None
        reported = [u for c, k in _leaves(out, []) for u in page_urls(c[k])]
        if not reported and "error" not in out and tool.canonical not in (f"{ns}.close", f"{ns}.evaluate"):
            reason = "no page URL reported"
        elif urls := [u for u in reported if u != BLANK]:
            reason = await asyncio.to_thread(_first_refusal, ctx.policy, urls)
        else:
            reason = None
        if reason is not None:
            await self._blank(conn)
            return {"error": f"navigation left the allowed sites: {reason}"}
        return None

    async def _blank(self, conn: _Conn) -> None:
        """`browser.navigate {"url": "about:blank"}`, past the gate, the URL policy and the lease, never shown to
        the model. Tried twice (a tool error or exception), but not again after a timeout; a failure is otherwise
        ignored (the result is refused either way)."""
        nav = self._names.get(f"{conn.spec.namespace}.navigate")
        client = conn.client
        if nav is None or nav.spec.name != conn.spec.name or client is None:
            return
        for _ in range(2):
            try:
                result = await asyncio.wait_for(client.call_tool(nav.name, {"url": BLANK}, raise_on_error=False),
                                                conn.spec.timeout_for(nav.name))
            except TimeoutError:
                return
            except Exception:
                continue
            if not result.is_error:
                return

    # ---- health ------------------------------------------------------------------------------------------------
    async def _health(self, refresh: bool) -> list[ServerHealth]:
        await self._refresh_enabled()

        async def one(spec: ServerSpec) -> ServerHealth:
            conn = self._conns.get(spec.name)
            if refresh or self._blocked(spec) or conn is None or conn.state is None:
                return await self._check(spec)
            return self._report(spec, conn)

        return list(await asyncio.gather(*(one(s) for s in self.registry.servers)))

    async def _check_now(self, spec: ServerSpec) -> ServerHealth:
        await self._refresh_enabled()
        return await self._check(spec)

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
def _record(span, out: dict, t0: float) -> None:
    span.set(ok="error" not in out, latency_ms=round((time.perf_counter() - t0) * 1000))
    if isinstance(out, _Refused):
        span.set(refused=True, reason=redact(out["error"])[:200])


def _first_refusal(policy: UrlPolicy, urls: list[str]) -> str | None:
    """The first URL's refusal reason (worker thread: `refusal` may resolve DNS)."""
    return next((r for u in urls if (r := policy.refusal(u)) is not None), None)


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
