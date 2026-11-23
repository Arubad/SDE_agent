# Roadmap Tutor, Phase 3 (MCP): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The platform speaks MCP in both directions:
- **its own servers:** `knowledge` (roadmap search), `learner` (memory and progress), `ingestion` (start, watch
  and report an ingestion, with progress notifications and elicitation for approval) and `web` (search and a
  guarded fetch). They are served over Streamable HTTP and stdio, so Claude Desktop and Claude Code can use them;
- **a gateway (client side):** it reads a registry of servers (own and external), connects, namespaces tools,
  applies per-agent allow-lists, timeouts, the URL policy and the untrusted-content wrapping, and reports health;
- **external servers:** Playwright MCP (browser), DeepWiki, Microsoft Learn, arXiv, Wikipedia, plus optional
  Context7 and GitHub, and the official Fetch server (registered, off by default);
- **the crawler renders `needs_browser` pages** through the gateway's browser instead of skipping them;
- **a Settings page** shows each server's health and switches it on or off.

**Architecture:**
- **New package `backend/tutor/mcp/`:**
  - `registry.py`: the server registry file and per-server on/off state;
  - `gateway.py`: connections, naming, allow-lists, calls and health;
  - `policy.py`: the agent URL rule from the spec's "Security" section;
  - `servers/`: the four own servers, each built by a factory from injected dependencies;
  - `http.py`: mounting the own servers on the platform API.
- **The gateway runs on its own event-loop thread.** Async callers (the Phase 4 tutor) and sync callers (ingestion
  worker threads) both reach it, and connections stay open between calls.
- **Own servers are reached in-process** (FastMCP in-memory transport). External servers are reached over stdio
  (`uvx`) or Streamable HTTP. One code path either way: `fastmcp.Client`.
- **Unchanged:**
  - the HF demo's `backend/mcp_server.py`, `backend/agent/*` and `backend/server.py`. The platform
    `knowledge` server is new code that reuses the demo's `Passage` model;
  - the Phase 2 ingestion agent's in-process tools, so its committed record/replay recordings stay valid.

**Tech Stack:** Python 3.12, FastMCP 3.4.x / `mcp` 1.30 (pins unchanged), FastAPI, SQLAlchemy 2, Alembic,
Postgres 16 + pgvector, Qdrant, `uv`/`uvx` for stdio servers, Microsoft Playwright MCP (container), React + Vite +
TS, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-19-roadmap-tutor-design.md`. Before starting, read:
- "Constraints" (the MCP pin rule);
- "MCP";
- "Security" (which URLs may be fetched, per actor);
- "Memory" (what consent covers);
- "Cost control and caching" (TTL caches for web search and fetched pages);
- "Phase 2 amendments" (Playwright rendering deferred to this phase; SearXNG wrapped as MCP here).

**Capability check, done while writing this plan (2026-11-24):** the installed `mcp` 1.30.0 and `fastmcp` 3.4.11
have:
- `Context.report_progress` and `Context.elicit` on the server side;
- `progress_handler` and `elicitation_handler` on `fastmcp.Client`, plus a per-call `progress_handler` and
  `timeout`;
- stdio and Streamable HTTP client transports.

Nothing has to be dropped. Task 1 pins this with tests, so a future dependency change that loses a feature fails CI.

## Rulings made while planning (the owner can overturn any of these at review)
1. **The demo's `backend/mcp_server.py` is not generalized in place.** The platform `knowledge` server is a new
   module that imports the demo's `Passage` model. Why: the demo server is the HF Space's tool layer, and the spec
   only allows refactors that keep the demo identical. Cost if wrong: one later refactor to merge the two.
2. **SearXNG is wrapped by our own `web` server, in-process, not a separate `searxng-mcp` container.** It reuses
   Phase 2's `SearxngSearch`, and adds `web.fetch` on Phase 2's `Fetcher`, which checks robots.txt, rate limits and
   the private-address guard on **every redirect hop**. Cost if wrong: a small compose service later.
3. **The official Fetch server is registered but off by default.** It follows redirects without our private-address
   check, so `web.fetch` is the default page reader for agents. The README says why.
4. **Playwright MCP runs as a container on its own Docker network** (`browser`), shared only with `api`. It cannot
   reach Postgres, Qdrant or SearXNG. The gateway checks every navigation target and the page's final URL.
   Residual risk (sub-resources, LAN addresses) is written down in `docs/platform/mcp.md`.
5. **Colab MCP is left to Phase 5**, with the sandbox that uses it.
6. **The learner server's schema is fixed here** (spec: "interfaces that cross phases are fixed in the phase that
   introduces them"):
   - consent settings, per-skill progress and semantic facts with pgvector;
   - the mastery update formula, episodes and the curator stay in Phase 4;
   - flashcards stay in Phase 5.
7. **The ingestion agent keeps its in-process tools.** Moving them behind the gateway would change their outputs and
   invalidate the committed `ingest_agent_eval --strict` recordings. Phase 4's tutor agents are the gateway's first
   model-facing consumers.
8. **The stdio ingestion server starts its own worker pool** (`--no-workers` turns it off). `SKIP LOCKED` makes a
   second pool alongside the API safe.

## Global Constraints
- **The HF Space must keep working unchanged:**
  - `requirements.txt` gets no new lines;
  - `app.py`, `deploy_to_hf.py`, `backend/server.py`, `backend/mcp_server.py` and `backend/agent/*` are not
    modified;
  - `backend/tests` passes with only `requirements.txt` + `pytest`;
  - new code lives under `backend/tutor/**`, which `deploy_to_hf.py` already ignores.
- **Pins:** `fastmcp>=3.4,<4` and `mcp<2` are untouched (spec: "If one is missing, that feature is dropped and the
  README says so; the pin is not raised"). New platform dependencies go only in `requirements-platform.txt`:
  `pgvector>=0.3` and `uv>=0.5`.
- **Every Claude call goes through `backend/core/llm.py`.** This phase adds no model call except the existing
  roadmap parse, reached through the ingestion server.
- **No network in tests.** External servers are faked with in-memory FastMCP servers that have the same tool names.
  Live checks are manual commands (`python -m backend.tutor mcp-check`), never CI. `LLM_PROVIDER=fake` is always
  set.
- **Tool names sent to Claude must match `^[a-zA-Z0-9_-]{1,64}$`.** The canonical name `web.search` is for the
  registry, traces and the UI; the wire name sent to the model is `web__search` (see Task 3).
- **Untrusted content:**
  - every string a server marked `untrusted` (all external servers, `web`, `knowledge`) returns reaches the model
    fenced by `wrap()` (moved, unchanged, from `backend/tutor/ingest/agent_tools.py` to
    `backend/tutor/untrusted.py`);
  - results are capped at 20,000 characters per call, with a `[truncated N chars]` marker.
- **The agent URL rule (spec "Security"):** agents may open only URLs from a search result, the user's messages or
  the roadmap's resources, plus same-site navigation from those pages. Private, loopback and link-local addresses
  are always refused (`backend/tutor/ingest/fetch.py:address_refusal` with `public_resolver`).
- **Secrets:**
  - registry headers and env values (`GITHUB_TOKEN`, `CONTEXT7_API_KEY`, `PREMIUM_PASSCODE`) never appear in
    health output, API responses, logs or trace attributes;
  - errors pass through `backend/core/redact.py:redact` before they are stored or shown.
- **Binding:** every new port binds to `127.0.0.1`. The local `/mcp/*` endpoints accept only a loopback `Host`
  header and no cross-site `Origin`. In `PUBLIC_MODE` they also need `Authorization: Bearer <PREMIUM_PASSCODE>`.
- **Tests that need Postgres** use the `db_url` and `engine` fixtures in `backend/tutor/tests/conftest.py`. They
  are skipped without `DATABASE_URL`, and CI sets `REQUIRE_DB=1`.
- **Commit trailer:** every commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Work on the
  branch `phase3-mcp`, never directly on `main`.

## Review Focus
1. **An external server is down, slow, or hangs on connect.** This happens on a `uvx` first-run download or a
   remote outage. A call returns an error result within that server's `timeout_s`, health shows `down` with a
   redacted reason, the other servers keep working, and API startup is never blocked → Task 3 test
   `test_hanging_server_times_out_and_others_work`.
2. **A search result or a page sends the browser (or a fetch) somewhere it must not go:** a private address, the
   API itself, or an unrelated site. The navigation's content is discarded, the browser is reset to `about:blank`,
   and the model gets an error naming the policy → Task 4 test `test_browser_final_url_off_policy_is_discarded`
   and Task 5 test `test_web_fetch_refuses_private_redirect`.
3. **Tool names from different servers collide, contain characters the Claude API rejects, or are longer than 64
   characters.** Wire names stay valid, unique and reversible → Task 3 test `test_wire_names_valid_unique_roundtrip`.
4. **A web page open in the learner's own browser calls the unauthenticated local `/mcp/*` endpoints**
   (DNS rebinding or a cross-site POST). A foreign `Host` or a cross-site `Origin` gets 403 → Task 9 test
   `test_mcp_rejects_foreign_host_and_origin`.
5. **An MCP client without elicitation support, or a user who declines or cancels.** The roadmap stays `parsed`,
   nothing is ingested, and the tool result says how to approve → Task 8 test
   `test_create_roadmap_without_elicitation_stays_parsed`.

---

### Task 1: Pin the MCP capabilities the gateway relies on

**Files:**
- Create: `backend/tutor/tests/test_mcp_capabilities.py`
- Create: `docs/platform/mcp.md` (section "Capability check" only; later tasks add to it)

**Interfaces:**
- Produces: nothing importable. The tests fail if a dependency change removes a feature the later tasks rely on.

- [ ] **Step 1: Create the branch** `git checkout -b phase3-mcp` from `main`.
- [ ] **Step 2: Write the tests.** Each test builds a throwaway `FastMCP("cap")` with one tool:

```python
async def test_progress_notifications_reach_client():
    # tool: for i in range(3): await ctx.report_progress(i + 1, 3, f"step {i + 1}")
    seen = []
    async with Client(server, progress_handler=lambda p, t, m: seen.append((p, t, m))) as c:
        await c.call_tool("work", {})
    assert seen == [(1, 3, "step 1"), (2, 3, "step 2"), (3, 3, "step 3")]

async def test_elicitation_accept_decline_and_unsupported():
    # tool: r = await ctx.elicit("Approve?", response_type=Approve)  (Approve: pydantic model, approve: bool)
    #       returns r.action plus r.data.approve when accepted
    # accept  -> handler returns Approve(approve=True)       -> result "accept:True"
    # decline -> handler returns ElicitResult(action="decline") -> result "decline"
    # client with no elicitation_handler -> the tool's ctx.elicit raises; the tool catches it -> "unsupported"

async def test_progress_and_elicitation_over_streamable_http():
    # the same server mounted with http_app(path="/", stateless_http=False) under a FastAPI app whose lifespan
    # runs the MCP app's lifespan; uvicorn on a free 127.0.0.1 port in a thread;
    # Client(StreamableHttpTransport(url)) sees the progress and answers the elicitation

async def test_call_timeout_is_enforced():
    # tool sleeps 5 s; call_tool(..., timeout=0.2) raises within 1 s
```

- [ ] **Step 3: Run** `backend/venv/bin/python -m pytest backend/tutor/tests/test_mcp_capabilities.py -v`.
  Expected: 4 PASS. If one fails because the feature is missing (not a test bug), mark that test `xfail` with the
  reason, and write in `docs/platform/mcp.md` which feature is dropped and which task stops using it. Do not raise
  the pin.
- [ ] **Step 4: Write `docs/platform/mcp.md` § "Capability check"** with the versions tested and the four results.
- [ ] **Step 5: Commit** "Pin the MCP features the gateway uses (progress, elicitation, timeouts)".

---

### Task 2: Server registry and per-server on/off state

**Files:**
- Create: `backend/tutor/mcp/__init__.py`, `backend/tutor/mcp/registry.py`, `backend/tutor/mcp/servers.json`
- Create: `backend/tutor/migrations/versions/0004_mcp.py`
- Modify: `backend/tutor/models.py` (model `McpServerState`), `backend/tutor/settings.py`
  (`mcp_registry: Path`, env `MCP_REGISTRY`, default `REPO_ROOT / "backend/tutor/mcp/servers.json"`)
- Test: `backend/tutor/tests/test_mcp_registry.py`

**Interfaces:**
- Produces:
  - `ServerSpec` (frozen dataclass), with fields:
    - `name: str`, `namespace: str`, `transport: Literal["inproc", "stdio", "http"]`;
    - `target: str | None` (an inproc server name), `command: str | None`, `args: tuple[str, ...]`, `url: str | None`;
    - `headers: dict[str, str]` (values after `${VAR}` expansion; `repr=False`);
    - `requires_env: tuple[str, ...]`, `enabled_default: bool`, `untrusted: bool = True`, `timeout_s: float = 20.0`;
    - `strip_prefix: str | None` (removed from a tool's name before namespacing, e.g. `browser_`);
    - `note: str = ""` (shown in Settings).
  - `Registry` (dataclass): `servers: tuple[ServerSpec, ...]`, `agents: dict[str, tuple[str, ...]]` (glob patterns
    over canonical tool names).
  - `load_registry(path: Path, env: Mapping[str, str]) -> Registry`. It raises `RegistryError` on:
    - a duplicate name or namespace;
    - an unknown transport;
    - a stdio entry whose `uvx` package argument is not pinned with `==`;
    - an `agents` pattern matching no namespace.
  - `missing_env(spec, env) -> list[str]`.
  - `effective_enabled(engine, spec) -> bool`: the DB override if present, else `enabled_default`.
  - `set_enabled(engine, name: str, enabled: bool) -> None`.
  - Table `mcp_servers(name text pk, enabled bool not null, updated_at timestamptz)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_committed_registry_loads`: the committed `servers.json` loads with an empty env;
  - `test_env_expansion_and_default`: `${PLAYWRIGHT_MCP_URL:-http://127.0.0.1:8931/mcp}` expands both ways;
  - `test_unpinned_uvx_rejected`;
  - `test_duplicate_namespace_rejected`;
  - `test_missing_env_reported`: a test-built `github`-like entry with `requires_env: ["GITHUB_TOKEN"]` and no
    `GITHUB_TOKEN` → `["GITHUB_TOKEN"]` (the real entry arrives in Task 10);
  - `test_spec_repr_hides_headers`: `"ghp_secret" not in repr(spec)`;
  - `test_enabled_override_roundtrip` (needs the DB): no row → default; `set_enabled(False)` → False; a second
    `set_enabled` upserts.
- [ ] **Step 2: Run them.** Expected: FAIL (module missing).
- [ ] **Step 3: Implement `registry.py` and migration `0004_mcp`.** Commit `servers.json` with the own servers only
  for now (Task 10 adds the external ones): `kb` (`target: "knowledge"`), `learner`, `ingest` (`target:
  "ingestion"`), `web`, all `transport: "inproc"`, and the `agents` block:

```json
"agents": {
  "teacher":    ["kb.*", "web.search", "web.fetch", "browser.navigate", "browser.snapshot", "browser.navigate_back",
                 "docs.*", "repo.*", "papers.*", "ref.*", "learner.recall", "learner.get_progress"],
  "researcher": ["kb.search", "web.search", "web.fetch", "browser.navigate", "browser.snapshot", "docs.*", "repo.*",
                 "papers.*", "ref.*"],
  "crawler":    ["browser.navigate", "browser.evaluate", "browser.close"]
}
```

  Patterns that match no namespace *yet* are allowed when the registry is loaded with `allow_unknown=True`. The
  committed file is loaded that way until Task 10 completes it; Task 10 removes the flag from the test.
- [ ] **Step 4: Run** `pytest backend/tutor/tests/test_mcp_registry.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** "MCP server registry with per-server on/off state".

---

### Task 3: The gateway: connect, name, allow, call, health

**Files:**
- Create: `backend/tutor/untrusted.py` (move `wrap`, `unwrap`, `defenses_off`, `TAG` and the escape table out of
  `backend/tutor/ingest/agent_tools.py`, which re-imports them; behaviour byte-identical)
- Create: `backend/tutor/mcp/gateway.py`
- Test: `backend/tutor/tests/test_mcp_gateway.py`

**Interfaces:**
- Consumes: `Registry`, `ServerSpec`, `effective_enabled`, `missing_env` (Task 2).
- Produces:
  - `wire_name(canonical: str) -> str`: dots in the namespace become `_`, the namespace/tool separator is `__`,
    anything outside `[a-zA-Z0-9_-]` becomes `_`. A result over 64 characters is cut to 55 plus `_` plus the first
    8 hex characters of `sha1(canonical)`.
  - `CallContext` (dataclass): `agent: str`, `policy: "UrlPolicy | None" = None` (Task 4), `trace: Trace | None =
    None`.
  - `ServerHealth` (dataclass, JSON-safe):
    - `name`, `namespace`, `transport`, `enabled: bool`;
    - `state: Literal["ok", "down", "disabled", "missing_env"]`;
    - `tools: int`, `latency_ms: int | None`, `error: str | None` (redacted, ≤ 300 chars), `checked_at: str | None`,
      `note: str`.
  - `Gateway(registry, *, engine, inproc: Mapping[str, FastMCP], env: Mapping[str, str])`, with methods:
    - `start() -> None`: starts the loop thread and connects nothing (connections are lazy);
    - `stop() -> None`;
    - `async tool_defs(agent: str) -> list[dict]`: Anthropic tool definitions with wire names, sorted by wire name;
      `description` is prefixed `[<namespace>] `; only tools the agent's patterns allow, from enabled, connectable
      servers;
    - `async call(wire_or_canonical: str, args: dict, ctx: CallContext) -> dict`: returns the loop's `ToolCaller`
      shape, `{"passages": [...]}`, `{"data": ...}` or `{"error": "..."}`;
    - `call_sync(name, args, ctx, timeout: float | None = None) -> dict`: for worker threads;
    - `async health(refresh: bool = False) -> list[ServerHealth]`;
    - `async check(name: str) -> ServerHealth`.

- [ ] **Step 1: Write the failing tests.** Fake servers are in-memory FastMCP instances passed through `inproc`, with
  registry entries built in the test:
  - `test_wire_names_valid_unique_roundtrip`: for 200 generated canonical names (dots, unicode, 90-char names, two
    names differing only after character 60), every wire name matches `^[a-zA-Z0-9_-]{1,64}$`, the names are
    unique, and the gateway maps each back to its canonical name.
  - `test_namespacing_and_strip_prefix`: server `browser` with `strip_prefix="browser_"` and tool `browser_navigate`
    → canonical `browser.navigate`, wire `browser__navigate`.
  - `test_allow_list_filters_defs_and_refuses_calls`: the `crawler` agent sees no `web.*` tool. Calling one returns
    `{"error": "tool web.search is not allowed for crawler"}` without reaching the server.
  - `test_disabled_and_missing_env_servers_are_absent`: their tools are not in `tool_defs`; health states
    `disabled` and `missing_env`.
  - `test_hanging_server_times_out_and_others_work`: a server whose tool sleeps 30 s, with `timeout_s=0.5` → error
    within 2 s, then health `down`. A call to another server succeeds. `Gateway.start()` returns in < 0.5 s even
    when a stdio server's command does not exist.
  - `test_untrusted_strings_are_wrapped_and_capped`: a 50,000-character text result comes back wrapped and ≤ 20,000
    + marker. For a server with `untrusted=False`, the text is unwrapped.
  - `test_passages_shape_passes_through`: structured content `{"passages": [{...}]}` → `{"passages": [...]}`
    unchanged (citations need raw text; the loop's `UNTRUSTED_RULE` covers them).
  - `test_tool_error_becomes_error_result`: a tool raising `ValueError("bad")` → `{"error": ...}` containing "bad".
  - `test_health_never_shows_secrets`: a header `Authorization: Bearer ghp_x123456` and a connect error that echoes
    it → no `ghp_x123456` in `json.dumps(asdict(h))`.
  - `test_reconnects_after_server_failure`: the first call fails with a transport error, the next call reconnects
    and succeeds.
  - `test_call_sync_from_worker_thread`: `call_sync` from a `threading.Thread` returns the same result as `call`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement `gateway.py`:**
  - One background thread runs an asyncio loop. `call` and `health` run there via `asyncio.run_coroutine_threadsafe`,
    awaited with `asyncio.wrap_future`, or `.result()` for `call_sync`.
  - One `fastmcp.Client` per server, opened lazily and kept open. The transport is chosen by `spec.transport`:
    - `inproc`: the FastMCP object from `inproc[spec.target]`;
    - `stdio`: `StdioTransport(command, args, env=…)`;
    - `http`: `StreamableHttpTransport(url, headers=…)`.
  - Any transport or timeout error closes the client and marks the server `down`. The next call reopens it.
  - Tool lists are cached per connection.
  - Each call is a trace span `mcp:<canonical>` (kind `tool`). Its attributes are the tool name, the arguments
    (`redact`ed, ≤ 200 chars), `ok` and the latency.
  - The untrusted wrap applies to every string leaf of `data`, and to joined text content when there is no
    structured content.
- [ ] **Step 4: Run** `pytest backend/tutor/tests/test_mcp_gateway.py backend/tutor/tests/test_ingest_agent*.py -v`.
  Expected: PASS (the agent tests prove the `wrap` move changed nothing).
- [ ] **Step 5: Commit** "MCP gateway: namespacing, allow-lists, timeouts, untrusted wrapping, health".

---

### Task 4: The agent URL policy, enforced in the gateway

**Files:**
- Create: `backend/tutor/mcp/policy.py`
- Modify: `backend/tutor/mcp/gateway.py`
- Test: `backend/tutor/tests/test_mcp_policy.py`

**Interfaces:**
- Consumes: `fetch.address_refusal`, `fetch.public_resolver`, `queue.normalize_url`.
- Produces:
  - `UrlPolicy(*, resolver=public_resolver)`, with methods:
    - `allow_sources(urls: Iterable[str]) -> None`: adds roadmap resources, the user's URLs, or search results;
    - `allow_text(text: str) -> None`: adds the URLs found in a user message;
    - `refusal(url: str) -> str | None`.
  - A URL is allowed if all of these hold:
    - its scheme is `http`/`https`;
    - `address_refusal(host)` is None;
    - it is an allowed URL, or on the same host as one (same-site navigation).
  - `Gateway.call` with a `ctx.policy`:
    - checks every argument named `url` or ending in `_url` before calling, returning
      `{"error": "refused by URL policy: <reason>"}`;
    - after a `web.search` call, adds each result's URL to `ctx.policy`;
    - after any `browser.*` call, reads `Page URL: <url>` from the result text. If that URL is refused, it calls
      `browser.navigate {"url": "about:blank"}` and returns `{"error": "navigation left the allowed sites: <reason>"}`
      instead of the page.

- [ ] **Step 1: Write the failing tests:**
  - `test_unlisted_url_refused`;
  - `test_same_site_navigation_allowed`;
  - `test_private_and_loopback_refused_even_if_listed`: `http://127.0.0.1:8000/api/health` and
    `http://169.254.169.254/`, even after `allow_sources`;
  - `test_search_results_become_allowed`: a fake `web.search` returns `https://example.org/a` → a later
    `web.fetch` of `https://example.org/b` is allowed;
  - `test_browser_final_url_off_policy_is_discarded`: a fake `browser_navigate` replies with page text and
    `- Page URL: http://10.0.0.5/admin` → the result is an error, the fake server records a follow-up navigation to
    `about:blank`, and the page text is not in the result;
  - `test_no_policy_means_no_url_checks`: crawler calls carry their own policy (Task 11).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement `policy.py` and the gateway hooks.**
- [ ] **Step 4: Run** `pytest backend/tutor/tests/test_mcp_policy.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** "Enforce the agent URL rule on every gateway call and browser navigation".

---

### Task 5: Own `web` server: search and a guarded fetch

**Files:**
- Create: `backend/tutor/mcp/servers/__init__.py`, `backend/tutor/mcp/servers/web.py`
- Test: `backend/tutor/tests/test_mcp_web_server.py`

**Interfaces:**
- Consumes: `Net` (`net.search`, `net.fetcher`), `extract_page`, `SearchUnavailable`.
- Produces: `build_web_server(net: Net, *, ttl_s: float = 900) -> FastMCP` (name `web`), with tools:
  - `search(query: str, category: Literal["general", "videos", "it", "science"] = "general", max_results: int =
    Field(8, ge=1, le=20)) -> {"results": [{"title", "url", "snippet", "engine"}]}`;
  - `fetch(url: str, max_chars: int = Field(20000, ge=1000, le=50000)) -> {"url", "final_url", "title",
    "markdown"}`. A non-`ok` fetch outcome raises `ValueError(f"{outcome}: {reason}")`, e.g. `login_wall`,
    `robots_disallowed` or `private_address`.
  - Both results are cached in memory for `ttl_s`, keyed by arguments (LRU, 256 entries). `fetch` also benefits from
    the Fetcher's ETag-aware HTTP cache.

- [ ] **Step 1: Write the failing tests** (fixture `Net` or `httpx.MockTransport`):
  - `test_search_returns_results`;
  - `test_search_unavailable_is_tool_error`;
  - `test_fetch_returns_markdown`;
  - `test_web_fetch_refuses_private_redirect`: a public URL 302s to `http://127.0.0.1/` → a tool error naming
    `private_address`, with no body text;
  - `test_fetch_respects_robots`;
  - `test_results_cached_within_ttl`: the second identical call makes no HTTP request; after the TTL (a fake clock
    is injected) it does.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Blocking `Net` calls run in `anyio.to_thread.run_sync`.
- [ ] **Step 4: Run** `pytest backend/tutor/tests/test_mcp_web_server.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** "Own web MCP server: SearXNG search and a guarded fetch".

---

### Task 6: Own `knowledge` server, by roadmap

**Files:**
- Create: `backend/tutor/mcp/servers/knowledge.py`
- Test: `backend/tutor/tests/test_mcp_knowledge_server.py`

**Interfaces:**
- Consumes: `PlatformIndex.search` (returns `Hit(chunk_id, score, payload)` with payload keys `parent_id`, `text`,
  `path`, `url`, `title`, `kind`, `start_s`, `end_s`, `skill_ids`, `document_id`), `get_parents`,
  `get_roadmap_by_slug`, `build_report`, `backend.mcp_server.Passage`.
- Produces: `build_knowledge_server(engine: Engine, index: PlatformIndex) -> FastMCP` (name `knowledge`).
  - **Tools:**
    - `list_roadmaps() -> [{"slug", "title", "status"}]`;
    - `search(roadmap: str, query: str, skill_id: str | None = None, top_k: int = Field(5, ge=1, le=10)) ->
      {"query", "roadmap", "passages": [Passage]}`. It retrieves `max(20, 4*top_k)` children, groups them by
      `parent_id` in score order, and returns up to `top_k` **parent** sections:
      - `text` is the parent's text;
      - `section` is the breadcrumb path;
      - `chunk_id` is the parent id;
      - `doc_id` is the document id;
      - `timestamp` is `"mm:ss–mm:ss"` when the parent has `start_s`.
    - `get_section(roadmap: str, section_id: str) -> Passage`;
    - `list_resources(roadmap: str) -> [{"id", "title", "url", "kind", "status", "skills"}]`.
  - **Resources:** `roadmap://{slug}/spec` (the approved spec JSON) and `roadmap://{slug}/report` (`build_report`).
  - **Prompts:** `explain_concept(roadmap, concept, level)` and `quiz_me(roadmap, skill)`. Their text tells the model
    to call `search` first and cite.
  - An unknown roadmap raises `ValueError("No roadmap '<slug>'")`. A `section_id` from another roadmap is treated as
    unknown (tenant isolation).

- [ ] **Step 1: Write the failing tests.** They need the DB, a Qdrant `:memory:` index, the hash embedder and a
  small indexed roadmap built through `index_document`:
  - `test_search_returns_parent_sections_deduped`: three children of one parent → one passage with the parent text;
  - `test_search_is_tenant_isolated`: roadmap A's query never returns roadmap B's sections;
  - `test_get_section_from_other_roadmap_is_unknown`;
  - `test_video_passage_has_timestamp`: start 750 s, end 1080 s → `"12:30–18:00"`;
  - `test_report_resource_matches_build_report`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest backend/tutor/tests/test_mcp_knowledge_server.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** "Own knowledge MCP server: per-roadmap search over parent sections".

---

### Task 7: Learner schema and the `learner` server

**Files:**
- Create: `backend/tutor/migrations/versions/0005_learner.py`, `backend/tutor/mcp/servers/learner.py`
- Modify: `backend/tutor/models.py` (`LearnerSettings`, `SkillProgress`, `LearnerFact`), `requirements-platform.txt`
  (`pgvector>=0.3`)
- Test: `backend/tutor/tests/test_mcp_learner_server.py`

**Interfaces:**
- Consumes: `Embedder` (`make_embedder(settings)`; the hash embedder in tests), `redact`, `RoadmapSpec` (skills
  and prerequisites from `roadmap.spec`).
- Produces:
  - **Tables:**
    - `learner_settings`: singleton `id=1`; `consent_personal bool null` (null = not asked yet);
      `consent_raw_messages bool null`; `retention_days int default 90`; `updated_at`.
    - `skill_progress`: pk `(roadmap_id, skill_id)`; `mastery float not null default 0` with a check 0..1;
      `weak_spots jsonb default []`; `next_review_at timestamptz null`; `unlock_threshold float null`; `updated_at`.
    - `learner_facts`: `id uuid pk`; `roadmap_id uuid null` (null = about the learner in general);
      `kind text check in ('fact', 'preference', 'goal')`; `text text`; `embedding vector` (no fixed dimension;
      plus `embedder text`); `provenance jsonb`; `created_at`, `updated_at`.
  - `build_learner_server(engine: Engine, embedder: Embedder) -> FastMCP` (name `learner`), with tools:
    - `remember(text: str, kind="fact", roadmap: str | None = None) -> {"id", "action": "inserted"|"updated"}`:
      - refused with `ValueError("Personal memory is off. Turn it on in Memory & privacy.")` unless
        `consent_personal is True`;
      - the text is `redact`ed before it is embedded or stored;
      - a fact with cosine ≥ 0.9 to an existing fact of the same `kind`, `roadmap` and `embedder` updates that fact
        instead of inserting (spec);
    - `recall(query: str, top_k: int = Field(5, ge=1, le=20), roadmap: str | None = None) -> [{"id", "kind",
      "text", "score"}]`;
    - `forget(fact_id: str) -> {"deleted": bool}`: a hard `DELETE`, so it is gone from every later `recall`;
    - `list_facts(roadmap: str | None = None) -> [...]`;
    - `get_progress(roadmap: str) -> [{"skill_id", "title", "mastery", "weak_spots", "locked", "next_review_at"}]`.
      `locked` is true when any prerequisite's mastery is below its skill's `unlock_threshold or 0.7`. Skills with
      no row have mastery 0;
    - `set_mastery(roadmap: str, skill_id: str, mastery: float = Field(ge=0, le=1), weak_spots: list[str] | None =
      None) -> {...}`: an unknown skill raises.
  - Learning progress (`skill_progress`) is stored whatever the consent, because the spec says it is always kept.

- [ ] **Step 1: Write the failing tests** (DB):
  - `test_remember_refused_without_consent`: also when `consent_personal` is null;
  - `test_remember_redacts_before_storing`: `"my email is a@b.co"` → the stored text contains no `a@b.co`;
  - `test_near_duplicate_updates_instead_of_inserting`: two near-identical texts → one row, `action == "updated"`;
  - `test_forget_really_forgets`: after `forget`, `recall` of the exact text returns nothing, and the row count is 0;
  - `test_locked_until_prerequisite_reaches_threshold`: prerequisite mastery 0.69 → locked; at 0.7 → unlocked; a
    per-skill threshold of 0.5 is honoured;
  - `test_set_mastery_bounds_and_unknown_skill`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement the migration, the models and the server.** Cosine similarity is computed in SQL with
  pgvector's `<=>`, filtered by `embedder`.
- [ ] **Step 4: Run** `pytest backend/tutor/tests/test_mcp_learner_server.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** "Learner schema and MCP server: consent-gated facts, progress and locks".

---

### Task 8: Own `ingestion` server, with progress and elicitation

**Files:**
- Create: `backend/tutor/mcp/servers/ingestion.py`
- Test: `backend/tutor/tests/test_mcp_ingestion_server.py`

**Interfaces:**
- Consumes: `create_roadmap`, `service.parse_and_resolve`, `normalize_spec`, `service.approve`, `service.progress`,
  `build_report`, `maybe_settle`, `settled_hook`.
- Produces: `build_ingestion_server(engine, net, settings, *, on_settled) -> FastMCP` (name `ingestion`), with tools:
  - `create_roadmap(slug: str, title: str, source_text: str) -> dict`. It creates the roadmap and parses it (one
    Haiku call; the fake provider in tests), then asks for approval with `ctx.elicit(message, response_type=Approve)`.
    The message lists stages, skills and each resource with its top candidate URL.
    - On accept with `approve=True`, it approves with `choices` = each resolved resource's top-scored candidate, so
      unresolved resources are skipped, and returns `{"status": "ingesting", ...}`.
    - On decline, cancel, or a client that cannot elicit, it returns `{"status": "parsed", "next": "Review and
      approve it in the app at /roadmaps/<slug>, or call approve_roadmap."}`.
  - `approve_roadmap(slug: str) -> dict`: the same top-candidate choices, for clients without elicitation. The
    tool's docstring says the human approves this call in their MCP client.
  - `ingestion_status(slug: str) -> dict`: `service.progress` plus `status`.
  - `wait_for_ingestion(slug: str, timeout_s: int = Field(600, ge=10, le=3600)) -> dict`. Every 2 s until the status
    is `ready`, `partial` or `failed`, or the timeout passes, it reports progress:
    `ctx.report_progress(done_jobs, total_jobs, "<status>: <n> resources done")`.
  - `ingestion_report(slug: str) -> dict`: `build_report`.

- [ ] **Step 1: Write the failing tests** (DB, fake LLM, fixture `Net`; the client uses an in-memory transport):
  - `test_create_roadmap_elicits_and_approves_on_accept`: the elicitation message contains every resource title →
    status `ingesting`, jobs enqueued;
  - `test_create_roadmap_without_elicitation_stays_parsed`: three cases (no handler, decline, cancel) → status stays
    `parsed`, zero jobs, `next` mentions `approve_roadmap`;
  - `test_wait_for_ingestion_reports_progress`: workers run inline (`ingest_roadmap(..., until_idle=True)` in a
    thread) → at least 2 progress notifications, and the final status is `ready` or `partial`;
  - `test_unknown_slug_and_duplicate_slug_are_tool_errors`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The DB and the model call run in `anyio.to_thread.run_sync`.
- [ ] **Step 4: Run** `pytest backend/tutor/tests/test_mcp_ingestion_server.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** "Own ingestion MCP server: elicited approval and progress notifications".

---

### Task 9: Serve the own servers (HTTP and stdio) and start the gateway with the API

**Files:**
- Create: `backend/tutor/mcp/http.py`, `backend/tutor/mcp/build.py`
- Modify: `backend/tutor/api.py` (lifespan: build the servers, start and stop the gateway as `app.state.gateway`, run
  the MCP apps' lifespans, mount `/mcp/<name>/`), `backend/tutor/jobs/worker.py` (`JobContext.gateway: Gateway |
  None = None`), `backend/tutor/__main__.py` (subcommand `mcp <knowledge|learner|ingestion|web> [--no-workers]` on
  stdio)
- Modify: `docs/platform/mcp.md` (§ "Use the platform from Claude Desktop or Claude Code", with `claude mcp add
  --transport http roadmap-knowledge http://127.0.0.1:8000/mcp/knowledge/` and the stdio JSON config)
- Test: `backend/tutor/tests/test_mcp_http.py`

**Interfaces:**
- Consumes: the four server factories (Tasks 5–8), `Gateway` (Task 3), `access.check_passcode`.
- Produces:
  - `build_own_servers(engine, net, index, embedder, settings, on_settled) -> dict[str, FastMCP]`, keyed
    `knowledge`, `learner`, `ingestion`, `web`;
  - `mount_mcp(app: FastAPI, servers: dict[str, FastMCP], settings) -> list[Lifespan]`. Each server's `http_app(path="/",
    stateless_http=False)` goes under `/mcp/<name>`, wrapped in `McpGuard` (ASGI):
    - a `Host` not in `{127.0.0.1, localhost, [::1]}` (any port) → 403, except in `PUBLIC_MODE`, where the tunnel
      host is allowed and the Bearer check applies;
    - an `Origin` header that is present and not loopback → 403;
    - in `PUBLIC_MODE`, `Authorization: Bearer <PREMIUM_PASSCODE>` is required (constant-time compare and the
      existing lockout) → otherwise 401.
  - `app.state.gateway` is started in the lifespan with `inproc = build_own_servers(...)`. Startup never waits on an
    external server.

- [ ] **Step 1: Write the failing tests:**
  - `test_mcp_rejects_foreign_host_and_origin`: `Host: evil.example` → 403; `Origin: https://evil.example` → 403;
    loopback Host and no Origin → the MCP initialize succeeds;
  - `test_public_mode_requires_bearer`: no or wrong token → 401; the right token → OK;
  - `test_knowledge_over_http_end_to_end`: uvicorn on a free port, then `Client(StreamableHttpTransport(...))` lists
    the knowledge tools;
  - `test_app_starts_with_unreachable_external_server`: a registry with an http server at `http://127.0.0.1:1/mcp`
    → `/api/health` answers within 2 s;
  - `test_stdio_entrypoint_lists_tools`: `python -m backend.tutor mcp web --no-workers` through `StdioTransport`
    lists `search` and `fetch`, and its stdout carries protocol only.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the platform suite** `DATABASE_URL=… pytest backend/tutor -q` and the demo suite `pytest
  backend/tests -q`. Expected: all PASS.
- [ ] **Step 5: Commit** "Serve the platform MCP servers over HTTP and stdio; start the gateway with the API".

---

### Task 10: External servers: registry entries, Playwright container, `uvx`, live check

**Files:**
- Modify: `backend/tutor/mcp/servers.json`, `compose.yaml`, `docker/api.Dockerfile` (installs `uv`;
  `UV_CACHE_DIR=/home/tutor/.cache/uv`), `requirements-platform.txt` (`uv>=0.5`), `backend/.env.example`
  (`PLAYWRIGHT_MCP_URL`, `CONTEXT7_API_KEY`, `GITHUB_TOKEN` note), `backend/tutor/__main__.py` (subcommand
  `mcp-check [--json]`)
- Modify: `backend/tutor/tests/test_mcp_registry.py` (drop `allow_unknown`)

**Interfaces:**
- Produces registry entries. At implementation time, pin every `uvx` package and the Playwright image to the release
  current on PyPI or MCR, and check each server's tool names against that release.

| name | namespace | transport | target | default | notes |
|---|---|---|---|---|---|
| `playwright` | `browser` | http | `${PLAYWRIGHT_MCP_URL:-http://127.0.0.1:8931/mcp}` | on | `strip_prefix: "browser_"`, `timeout_s: 45` |
| `deepwiki` | `repo.deepwiki` | http | `https://mcp.deepwiki.com/mcp` | on | remote, no auth |
| `mslearn` | `docs.mslearn` | http | `https://learn.microsoft.com/api/mcp` | on | remote, no auth |
| `arxiv` | `papers.arxiv` | stdio | `uvx arxiv-mcp-server==<pin>` | on | `--storage-path` under the cache dir |
| `wikipedia` | `ref.wikipedia` | stdio | `uvx wikipedia-mcp==<pin>` | on | |
| `context7` | `docs.context7` | http | `https://mcp.context7.com/mcp` | on | `requires_env: CONTEXT7_API_KEY`, sent as header `CONTEXT7_API_KEY` |
| `github` | `repo.github` | http | `https://api.githubcopilot.com/mcp/` | on | `requires_env: GITHUB_TOKEN`; headers `Authorization: Bearer ${GITHUB_TOKEN}`, `X-MCP-Readonly: true` |
| `fetch` | `fetch` | stdio | `uvx mcp-server-fetch==<pin>` | **off** | `note`: "follows redirects without the private-address check; web.fetch is used instead" |

- `compose.yaml` service `playwright-mcp`:
  - image `mcr.microsoft.com/playwright/mcp:<pinned tag>`;
  - command `--headless --browser chromium --no-sandbox --isolated --host 0.0.0.0 --port 8931`, plus the flag that
    lets the `playwright-mcp` host name through the server's Host check, and `--blocked-origins
    "http://api:8000;http://host.docker.internal;http://localhost;http://127.0.0.1"`. Check the flag names against
    `docker run --rm <image> --help` for the pinned tag, and record them in a comment;
  - networks `browser` only; port `127.0.0.1:8931:8931`;
  - `api` joins `default` and `browser`, with `PLAYWRIGHT_MCP_URL: http://playwright-mcp:8931/mcp`.
- `mcp-check` connects to every enabled server and prints one line per server (`name state tools latency error`).
  It exits 0 even when some are down; it is a report, not a gate.

- [ ] **Step 1: Make the registry test strict.** Run `test_committed_registry_loads` without `allow_unknown`: every
  `agents` pattern matches a namespace, every stdio `uvx` package is pinned. Expected: FAIL until the entries exist.
- [ ] **Step 2: Add the entries, the compose service, the network and the Dockerfile change.**
- [ ] **Step 3: Run** `pytest backend/tutor/tests/test_mcp_registry.py -v` (PASS) and `docker compose config -q`
  (exit 0).
- [ ] **Step 4: Live check (manual, needs the network):** `docker compose up -d playwright-mcp`, then
  `backend/venv/bin/python -m backend.tutor mcp-check`. Paste the output into the task report. A server that is
  down is reported, not fixed by loosening a guard.
- [ ] **Step 5: Commit** "Register external MCP servers; Playwright MCP on an isolated network; mcp-check".

---

### Task 11: The crawler renders `needs_browser` pages through the gateway

**Files:**
- Create: `backend/tutor/mcp/browser.py`
- Modify: `backend/tutor/ingest/handlers.py` (`fetch_page`: on outcome `needs_browser`, try `render_page` before
  `_non_ok`), `backend/tutor/ingest/report.py` (count `rendered_by_browser` in the resource stats and in the
  summary), `backend/tutor/settings.py` (`browser_render: bool = True`, env `BROWSER_RENDER`), `web/src/api.ts` and
  `web/src/IngestReport.tsx` (show the count)
- Test: `backend/tutor/tests/test_browser_render.py`

**Interfaces:**
- Consumes: `Gateway.call_sync`, `CallContext(agent="crawler", policy=…)`, `UrlPolicy`, `extract_page`.
- Produces: `render_page(gateway: Gateway, url: str, *, scope_host: str, timeout_s: float = 45) -> RenderResult |
  str`. It returns a `RenderResult(final_url: str, html: str)` on success, or a short reason string. The steps:
  1. `browser.navigate {"url": url}` under a policy whose sources are `[url]`. The policy's same-host rule is the
     crawler's same-site rule.
  2. `browser.evaluate {"function": "() => document.documentElement.outerHTML"}`.
  3. `browser.close`.

  The final URL's host must equal `scope_host`, otherwise the reason is `"left the site"`. HTML over 5 MB gives
  `"too_large"`. The page is stored with `meta.rendered = "browser"` and enters discovery and chunking like an HTTP
  page. Without a gateway, with `browser_render` off, with the browser server disabled or down, or with a render
  that yields no text, the page stays `skipped`/`needs_browser` as in Phase 2, and the detail carries the reason.

- [ ] **Step 1: Write the failing tests.** A fake `playwright` in-memory server has tools `browser_navigate`,
  `browser_evaluate` and `browser_close`, which return canned HTML and a `Page URL:` line:
  - `test_needs_browser_page_is_rendered_and_chunked`: the document is `ok` with `meta.rendered == "browser"`,
    sections exist, and the report counts 1 `rendered_by_browser`;
  - `test_render_leaving_site_is_skipped`: the final URL is on another host → `skipped`, detail `left the site`, and
    no text stored;
  - `test_browser_down_keeps_phase2_behaviour`: the server is disabled → `needs_browser` as before;
  - `test_rendered_links_are_discovered_within_caps`: links in the rendered HTML enqueue `fetch_page` jobs within
    `crawl_max_pages`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest backend/tutor -q` and `cd web && npx vitest run`. Expected: PASS.
- [ ] **Step 5: Commit** "Render JS-only pages through the gateway's browser during ingestion".

---

### Task 12: Settings page: server health and on/off

**Files:**
- Modify: `backend/tutor/api.py`, with routes:
  - `GET /api/mcp/servers` → `[ServerHealth]`;
  - `POST /api/mcp/servers/{name}/check` → `ServerHealth`;
  - `PUT /api/mcp/servers/{name}` with body `{"enabled": bool}` → `ServerHealth`. It calls `set_enabled` and makes
    the gateway drop that server's connection.
- Create: `web/src/Settings.tsx`
- Modify: `web/src/App.tsx` (nav link "Settings"), `web/src/api.ts` (`listServers`, `checkServer`,
  `setServerEnabled`, type `ServerHealth`), `web/src/format.ts` (`serverStateLabel`)
- Test: `backend/tutor/tests/test_api_mcp.py`, `web/src/api.test.ts`, `web/src/format.test.ts`

**Interfaces:**
- Consumes: `Gateway.health`, `Gateway.check`, `set_enabled`.
- Produces: the page lists each server with:
  - its name, namespace and transport;
  - a state badge (`ok` / `down` / `off` / `needs <ENV>`);
  - its tool count, its latency, and its error (redacted);
  - its `note`;
  - an on/off toggle and a "Check now" button.

- [ ] **Step 1: Write the failing tests:**
  - `test_list_servers_hides_secrets`: a registry with a GitHub token → the token is absent from the response body;
  - `test_toggle_persists_and_updates_health`: `PUT enabled=false` → state `disabled`, and it survives an app
    restart;
  - `test_unknown_server_404`;
  - `test_public_mode_guards_mcp_api`: the existing passcode guard covers `/api/mcp/*`;
  - Vitest: `setServerEnabled sends PUT with the access code`, and `serverStateLabel` for the four states.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement the routes and the page.** Follow the existing pages' markup and `styles.css` classes.
- [ ] **Step 4: Run** `pytest backend/tutor/tests/test_api_mcp.py -v` and `cd web && npx vitest run && npm run
  build`. Expected: PASS.
- [ ] **Step 5: Commit** "Settings page: MCP server health and on/off".

---

### Task 13: Docs, README and the live end-to-end check

**Files:**
- Modify: `docs/platform/mcp.md`, `README.md` (platform section: MCP), `docs/platform/running-locally.md`
  (Playwright MCP service, `uv`, `mcp-check`)

**Interfaces:**
- Produces documentation only. Every claim in it is either backed by a test or by output pasted from the live check.

- [ ] **Step 1: Live check (manual, needs the network and the running stack):**
  1. `docker compose up -d playwright-mcp`, then start the API.
  2. `python -m backend.tutor mcp-check`.
  3. From `fastmcp.Client` over HTTP: `knowledge.search` on the `kubernetes` roadmap, `web.search "kubernetes
     pod lifecycle"`, `web.fetch` of one result, `browser.navigate` + `browser.snapshot` of `https://kubernetes.io/`,
     and one call each to DeepWiki, Microsoft Learn, arXiv and Wikipedia.
  4. `claude mcp add --transport http roadmap-knowledge http://127.0.0.1:8000/mcp/knowledge/`, then list its tools.

  Record the outcomes, including failures, in `docs/platform/mcp.md` § "Live check (2026-11-…)".
- [ ] **Step 2: Write `docs/platform/mcp.md`:**
  - the server map (own and external, with namespaces and defaults);
  - the per-agent allow-lists;
  - the URL rule and how each actor is checked;
  - the Playwright threat model: an isolated network and blocked origins; residual risk from sub-resources and LAN
    addresses; final-URL discard;
  - why Fetch is off;
  - how to add a server to the registry;
  - the capability check results.
- [ ] **Step 3: README:** a short MCP subsection in the platform part. It covers the own servers usable from Claude
  Desktop and Claude Code, the gateway, the external servers and their costs (all free; Context7 and GitHub need
  keys), the known limits, and a link to `docs/platform/mcp.md`. The HF frontmatter and the demo section are
  untouched.
- [ ] **Step 4: Run the full suite:**
  - `DATABASE_URL=postgresql+psycopg://tutor:tutor@localhost:5432/postgres backend/venv/bin/python -m pytest -q`;
  - `cd web && npx vitest run`.

  Expected: all PASS.
- [ ] **Step 5: Commit** "Docs: MCP servers, gateway, threat model and live check".
