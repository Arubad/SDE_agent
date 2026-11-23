# MCP in the roadmap tutor

The platform speaks MCP in two directions. Its own four servers (`knowledge`, `learner`, `ingestion`, `web`) can be
plugged into Claude Desktop or Claude Code ([below](#use-the-platform-from-claude-desktop-or-claude-code)). And the
platform's agents (the tutor's teacher, the researcher, the crawler) call tools, their own and third-party ones,
through one gateway (`backend/tutor/mcp/gateway.py`) that enforces who may call what, which URLs a browser may open and
how untrusted text reaches the model. Every claim on this page is backed by a test in `backend/tutor/tests` or by the
[live check](#live-check-2026-11-24) at the end.

## Server map

The registry is `backend/tutor/mcp/servers.json` (override the path with `MCP_REGISTRY`). The canonical tool name is
`<namespace>.<tool>`; with a `strip_prefix` the server's own prefix is dropped (`browser_navigate` is `browser.navigate`).

| Server | Namespace | Transport | Default | Notes |
| --- | --- | --- | --- | --- |
| `kb` | `kb` | in-process | on | `search`, `get_section`, `list_resources`, `list_roadmaps` over the indexed roadmaps |
| `learner` | `learner` | in-process | on | `remember`, `recall`, `forget`, `list_facts`, `get_progress`, `set_mastery` |
| `ingest` | `ingest` | in-process | on | `create_roadmap`, `approve_roadmap`, `ingestion_status`, `wait_for_ingestion`, `ingestion_report` |
| `web` | `web` | in-process | on | `search` (SearXNG), `fetch` (the crawler's guarded fetcher) |
| `playwright` | `browser` | HTTP, the `playwright-mcp` container | on | headless Chromium; `timeout_s` 45 |
| `deepwiki` | `repo.deepwiki` | remote HTTP | on | generated docs for public GitHub repositories; no key |
| `mslearn` | `docs.mslearn` | remote HTTP | on | Microsoft Learn documentation; no key |
| `arxiv` | `papers.arxiv` | stdio via `uvx` (`arxiv-mcp-server==0.8.2`) | on | papers; storage under `ARXIV_STORAGE_PATH` |
| `wikipedia` | `ref.wikipedia` | stdio via `uvx` (`wikipedia-mcp==2.0.1`) | on | `wikipedia-mcp` registers every tool twice; the `wikipedia_*` copies are denied |
| `context7` | `docs.context7` | remote HTTP | on, but needs `CONTEXT7_API_KEY` | `missing_env` without the key |
| `github` | `repo.github` | remote HTTP | on, but needs `GITHUB_TOKEN` | sent with `X-MCP-Readonly: true`; `missing_env` without the token |
| `fetch` | `fetch` | stdio via `uvx` | **off** | see [Why Fetch is off](#why-fetch-is-off) |

Everything is free; only Context7 and GitHub need a key. The Settings page lists these servers with their health and
an on/off switch (`GET /api/mcp/servers`; the switch is stored in the database and survives restarts).

## Calls, names and limits

- The canonical name `ns.tool` is what the registry, traces and the UI use. The name sent to Claude must match
  `^[a-zA-Z0-9_-]{1,64}$`, so `.` becomes `_`, namespace and tool are joined by `__`, and a hashed suffix is added when
  a name is too long or the mapping would lose information.
- Order inside one call: name resolution, allow/deny, URL policy, ask approval, connect and call. Nothing reaches a
  server when a step refuses.
- Every string from an untrusted server (all external servers and all four own ones) is fenced with the untrusted
  wrapper and the whole result is capped at 20,000 characters with a `[truncated N chars]` marker (the largest strings
  are cut first). External tool descriptions are fenced and capped too (1,024 characters; schema descriptions 300). The
  own servers' `passages` are the exception: they pass unfenced because they are citations, but are validated and
  capped. Text from a server that fails to connect is fenced as well.
- Each server has a `timeout_s` (default 20) and a separate connect budget (for stdio, at least 60 s, for a cold
  `uvx` download). A server that is down returns an error result and shows `down`; the others keep working. Startup
  never waits for any server.
- Secrets: health output and errors are redacted, every header value and every `requires_env` value is shown as
  `[redacted]`, and the registry refuses a `${VAR}` without a default in `headers`, `url` or `args` unless the
  variable is in `requires_env`. A stdio child gets only the MCP default environment plus its own `requires_env`
  keys, so API keys in the API's environment do not reach third-party servers.

## Agent allow-lists and rules

Each agent has a list of globs over canonical names; a tool outside the list is not offered and is refused if
called.

| Agent | Allowed |
| --- | --- |
| `teacher` | `kb.*`, `web.search`, `web.fetch`, `browser.navigate`, `browser.snapshot`, `browser.navigate_back`, `docs.*`, `repo.*`, `papers.*`, `ref.*`, `learner.recall`, `learner.get_progress` |
| `researcher` | `kb.search`, `web.search`, `web.fetch`, `browser.navigate`, `browser.snapshot`, `docs.*`, `repo.*`, `papers.*`, `ref.*` |
| `crawler` | `browser.navigate`, `browser.evaluate`, `browser.close` (raw results, no fencing, a 5 MB cap; used only by ingestion) |

On top of that the registry has rules, and **deny beats ask beats allow**:

- **deny**: `ref.wikipedia.wikipedia_*` (the duplicate tools).
- **ask**: `ingest.create_roadmap`, `ingest.approve_roadmap`, `learner.forget`. An `ask` tool runs only after an
  approver has seen the raw payload (`{tool, server, agent, args}`) and said yes. With no approver the call is
  refused.

## Idempotency keys on the ingestion write tools

`create_roadmap` and `approve_roadmap` take an optional `idempotency_key`. The same key with the same arguments replays the
first result; the same key with different arguments is an error; a call that failed releases its key; a claim older
than 15 minutes can be claimed again.

## The URL rule

A model-driven agent may open only URLs that came from a search result, from the user's messages or from the
roadmap's resources, plus same-site navigation from those pages. Private, loopback and link-local addresses are
always refused, resolved through real DNS off the gateway's event loop. How it is checked:

- **Arguments**: every `url` or `*_url` argument is checked before the call. Results of `web.search` add their URLs
  to the agent's allowed list (read from the raw result, before fencing). Those URLs reach the model fenced; when one
  comes back as an argument with the fence around it, the gateway strips that one fence first, and the check, the
  approver and the server all get the same stripped value.
- **`web.fetch` redirects**: the fetcher address-checks every redirect hop, and the gateway also checks the result's
  `final_url` against the policy. A fetch that ended on another site is an error result; its content is discarded.
- **Browser results**: after every browser call the `Page URL:` line of the result is checked. A browser result
  without one (except `close` and `evaluate`) is refused. A page that is off-policy gets the browser reset to
  `about:blank` and an error result: the content is discarded, never shown.
- **Ports are not compared**: any port on an allowed public host passes.
- **The crawler** is a different actor: it renders pages whose HTML has no text (`needs_browser`) through the
  browser under a same-site policy for the page it is already crawling; the rendered HTML only feeds extraction and
  storage, never a model, and the crawl's caps and scope are unchanged. (`BROWSER_RENDER=false` turns this off.)

## Playwright threat model

A browser that can open any page is the riskiest tool here. The `playwright-mcp` service in `compose.yaml`
(`mcr.microsoft.com/playwright/mcp` v0.0.86) is contained like this:

- **Isolated network.** It lives on its own Docker network `browser`, shared only with `api`. It cannot resolve
  `postgres`, `qdrant`, `searxng` or `phoenix` by name. Its port is published on `127.0.0.1:8931` only.
- **Blocked origins.** `--blocked-origins` lists `api`, `host.docker.internal`, `gateway.docker.internal`,
  `localhost` and `127.0.0.1`, each over http and https and with a port wildcard (a bare origin would block port 80
  only). In the live check, navigations to `host.docker.internal:6333`, `api:8000`, `gateway.docker.internal:5432`,
  `localhost:8931`, `127.0.0.1:8888` and `https://host.docker.internal:6333` all failed with
  `net::ERR_BLOCKED_BY_CLIENT`, and `https://example.com` loaded.
- **Gateway checks on top** (above): the address check before navigating and the final `Page URL:` check, which
  discards the page and resets the browser.

Residual risk, stated plainly:

- Blocked origins do not cover redirects (Playwright says so). A public page that redirects to an internal address
  is caught only by the gateway's final `Page URL:` check, which runs after the request was made.
- The API stays reachable by its IP on the `browser` network. Outside public mode its `/api/*` routes answer only
  a loopback `Host` and refuse a cross-site `Origin` (403; `/api/health` and the web app's static files are exempt),
  so a script on a rendered page, or a DNS-rebinding site in the learner's browser, cannot call them (for example to
  approve an agent's proposal or start a paid roadmap parse). In public mode the passcode guard covers them instead.
- LAN addresses, `[::1]` and `0.0.0.0` are covered only by the gateway's checks, not by blocked origins.
- Sub-resource requests a page makes (images, scripts, `fetch` calls) are not seen by the gateway. A live
  `evaluate` that fetched an internal address failed, but that may be mixed-content blocking, not the blocklist.
- One shared browser serves all agents. Phase 4 has to isolate it before tutor agents use it.
- Rendering for the crawler re-requests the page without the crawler's per-host interval, and its sub-resources load
  under Chromium's user agent without robots checks. The page itself was robots-checked by the HTTP fetch first.
- In v0.0.86 `navigate` links the page snapshot as a file; only `snapshot` returns it inline. Agents navigate, then
  snapshot.

## Why Fetch is off

The official Fetch server follows redirects without the private-address check. The platform's `web.fetch` is the
default page reader instead: it obeys robots.txt, keeps a per-host interval and applies the private-address guard on
every redirect hop. The `fetch` entry stays in the registry, disabled, with that note.

## Adding a server to the registry

Edit `backend/tutor/mcp/servers.json` (or point `MCP_REGISTRY` at another file) and add an object to `servers`:

```json
{"name": "example", "namespace": "docs.example", "transport": "http",
 "url": "https://example.com/mcp",
 "headers": {"Authorization": "Bearer ${EXAMPLE_TOKEN}"}, "requires_env": ["EXAMPLE_TOKEN"],
 "timeout_s": 20, "enabled": true, "note": "What it is for."}
```

- `transport` is `http` (needs `url`), `stdio` (needs `command`, optional `args`) or `inproc` (the platform's own).
  Pin stdio packages to an exact version (`uvx pkg==x.y.z`).
- Allowed keys: `name`, `namespace`, `transport`, `target`, `command`, `args`, `url`, `headers`, `requires_env`,
  `enabled`, `untrusted`, `timeout_s`, `strip_prefix`, `note`. `${VAR}` and `${VAR:-default}` are expanded from the
  environment; a `${VAR}` without a default must be listed in `requires_env`, or the registry fails to load.
- Servers are untrusted by default; leave it that way for anything you do not run.
- Nothing is reachable by an agent until it is allowed: add globs for the namespace to the agents in `agents`, and
  add `deny` or `ask` rules for tools that write or are risky.
- Check it with `python -m backend.tutor mcp-check` (see [Running locally](running-locally.md#mcp-servers)); then it
  shows in the Settings page with the others.

## Capability check

The gateway (Phase 3) relies on four MCP features. `backend/tutor/tests/test_mcp_capabilities.py` pins them
against the versions in `requirements.txt`: `mcp` 1.30.0 and `fastmcp` 3.4.11. All four pass, so nothing is
dropped and no pin was raised.

| Feature | Result |
| --- | --- |
| Progress notifications (`ctx.report_progress` reaches the client's `progress_handler`) | PASS |
| Elicitation: accept, decline, and a client with no handler (the tool's `ctx.elicit` raises) | PASS |
| Progress and elicitation over Streamable HTTP (`http_app(path="/", stateless_http=False)` under FastAPI, served by uvicorn) | PASS |
| Per-call timeout (`call_tool(..., timeout=0.2)` on a 5 s tool raises in under 1 s) | PASS |

If a dependency bump breaks one of these, the test fails first; do not raise the pins to work around it.

## Use the platform from Claude Desktop or Claude Code

The platform's own servers are MCP servers you can plug into any MCP client:

| Server | Tools |
| --- | --- |
| `knowledge` | `list_roadmaps`, `search`, `get_section`, `list_resources` (plus the `spec` and `report` resources) |
| `learner` | `get_progress`, `set_mastery`, `remember`, `recall`, `list_facts`, `forget` |
| `ingestion` | `create_roadmap`, `approve_roadmap`, `ingestion_status`, `wait_for_ingestion`, `ingestion_report` |
| `web` | `search`, `fetch` |

### Over HTTP (the API is running)

`python -m backend.tutor` serves each one at `http://127.0.0.1:8000/mcp/<name>/` (Streamable HTTP; keep the
trailing slash). Register them with Claude Code:

```bash
claude mcp add --transport http roadmap-knowledge http://127.0.0.1:8000/mcp/knowledge/
claude mcp add --transport http roadmap-learner http://127.0.0.1:8000/mcp/learner/
claude mcp add --transport http roadmap-ingestion http://127.0.0.1:8000/mcp/ingestion/
claude mcp add --transport http roadmap-web http://127.0.0.1:8000/mcp/web/
```

The endpoints accept only a loopback `Host` header (`127.0.0.1`, `localhost` or `[::1]`, any port) and refuse a
cross-site `Origin` (403), so a web page cannot reach them through DNS rebinding. With `PUBLIC_MODE=true` the
tunnel's host is accepted, but every request needs `Authorization: Bearer <PREMIUM_PASSCODE>` (401 without it, and
the API's passcode lockout applies: 429 after too many wrong tries):

```bash
claude mcp add --transport http roadmap-knowledge https://<tunnel-host>/mcp/knowledge/ \
  --header "Authorization: Bearer $PREMIUM_PASSCODE"
```

### Over stdio (no API needed)

`python -m backend.tutor mcp <knowledge|learner|ingestion|web>` serves one server on stdin/stdout with the same
dependencies as the API (it migrates the database named in `DATABASE_URL`, and reads the same environment). Logs go
to stderr; stdout carries the protocol only. `ingestion` also runs the job workers in that process; pass
`--no-workers` when the API (or another process) runs them already. For Claude Desktop
(`claude_desktop_config.json`), with `<repo>` the absolute path of this repository:

```json
{
  "mcpServers": {
    "roadmap-knowledge": {
      "command": "<repo>/backend/venv/bin/python",
      "args": ["-m", "backend.tutor", "mcp", "knowledge"],
      "cwd": "<repo>",
      "env": {"PYTHONPATH": "<repo>"}
    },
    "roadmap-ingestion": {
      "command": "<repo>/backend/venv/bin/python",
      "args": ["-m", "backend.tutor", "mcp", "ingestion", "--no-workers"],
      "cwd": "<repo>",
      "env": {"PYTHONPATH": "<repo>"}
    }
  }
}
```

`PYTHONPATH` makes `backend.tutor` importable even for a client that ignores `cwd`; `backend/.env` is found from
the package's own path either way. Claude Code takes the same command:

```bash
claude mcp add roadmap-knowledge -e PYTHONPATH=<repo> -- <repo>/backend/venv/bin/python -m backend.tutor mcp knowledge
```

## Known limits

- Facts the learner stored stay recallable after consent is revoked; Phase 4 decides between hiding and purging.
- Browser isolation per agent (see the threat model), and the residual risks listed there.
- `/mcp/*` is for a client on this machine; do not expose it except through the passcode-protected tunnel.

## Live check (2026-11-24)

Run by hand against the running stack (Postgres with the real `kubernetes` roadmap, Qdrant, SearXNG, the
`playwright-mcp` container, and the API started with `python -m backend.tutor`). Read-only calls only. Not CI: it
needs the network.

**`python -m backend.tutor mcp-check`** (about 7.6 s; 10 of 12 servers ok):

```
name        state        tools  latency_ms  error
kb          ok           4      117         -
learner     ok           6      107         -
ingest      ok           5      105         -
web         ok           2      103         -
playwright  ok           25     125         -
deepwiki    ok           3      2529        -
mslearn     ok           3      916         -
arxiv       ok           19     1338        -
wikipedia   ok           22     1980        -
context7    missing_env  0      -           missing env: CONTEXT7_API_KEY
github      ok           29     3009        -
fetch       disabled     0      -           -
```

`github` was ok because `GITHUB_TOKEN` is set in this machine's environment; `context7` needs its key. The
`wikipedia` and `arxiv` stdio servers print their startup logs to stderr during the check.

**Own servers over HTTP** (`fastmcp.Client(StreamableHttpTransport("http://127.0.0.1:8000/mcp/<name>/"))`), run
instead of `claude mcp add` so the live check did not write the Claude Code configuration:

- `tools/list` on `/mcp/knowledge/`, `/learner/`, `/ingestion/` and `/web/` returned the tools in the table above.
- `list_roadmaps` returned `kubernetes` and `kubernetes-run2`, both `ready`.
- `knowledge.search` with roadmap `kubernetes`, query "pod lifecycle": passages from
  `https://kubernetes.io/docs/concepts/workloads/pods/` (a section titled "Pods (part 4)"; score 0.7).
- `web.search` "kubernetes pod lifecycle": results (first: a YouTube video). `web.fetch` of that first result
  succeeded but returned only the page footer text (YouTube serves no content to the fetcher). That is a weak
  result, not a failure of the tool; fetch of a documentation page is covered by the crawler's tests and its live
  ingestion.

**External servers through the gateway**, in-process (they are not served at `/mcp/*`), built with the same
`build_gateway` the API uses, agent `teacher`, with a URL policy seeded from a `web.search` result:

| Call | Result |
| --- | --- |
| `web.search` "kubernetes pod lifecycle" | results; the first, `https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/`, was added to the policy |
| `browser.navigate` to that URL | loaded: `Page URL: .../pod-lifecycle/`, title "Pod Lifecycle \| Kubernetes" |
| `browser.snapshot` | the page's accessibility tree, inline, fenced as untrusted |
| `browser.navigate` to `http://127.0.0.1:8000/` | refused: `refused by URL policy: private, loopback or link-local address` |
| `repo.deepwiki.read_wiki_structure` (`kubernetes/kubernetes`) | ok: the page list |
| `docs.mslearn.microsoft_docs_search` | ok: results about AKS reliability |
| `papers.arxiv.search_papers` | ok: 2 papers |
| `ref.wikipedia.search_wikipedia` "Kubernetes" | ok: results |
| `ref.wikipedia.wikipedia_search` | refused: `tool ref.wikipedia.wikipedia_search is not allowed for teacher` (the deny rule) |

Outcomes that were not clean:

- A first `web.search` returned no results (`{"results": []}`) and a second attempt returned results. SearXNG's
  engines throttle a self-hosted instance (see Running locally), so treat an empty search as temporary.
- The first browser run used `https://kubernetes.io/` with the URL added to the policy by hand (the search returned
  nothing); it loaded and snapshotted. The run in the table used a search result, as an agent would.
- `web.search` results come back fenced (`<untrusted_web_content>` around the URL too). The first attempt to
  navigate with the fenced string was refused (`only absolute http(s) URLs with a host`). That was a gateway bug,
  since fixed: the gateway now strips one exact fence from every `url` / `*_url` argument before checking and
  calling, so a URL passed back as it was shown works (`test_mcp_policy.py`).
- The tool name for DeepWiki's page list is `read_wiki_structure`; a first guess (`ask_question`) was
  `unknown tool`. Use the Settings page or `tools/list` to see names.

**Not exercised**: `claude mcp add` (it writes the owner's Claude Code configuration; the commands are documented
above and the endpoints answered the same way to `fastmcp.Client`), Context7 (no key), the write tools
(`create_roadmap`, `approve_roadmap`, `remember`, `set_mastery`, `forget`) against the real database, and the ask
flow (covered by tests).

**Settings page data** (`GET /api/mcp/servers`): 12 rows with the same states as `mcp-check`: ten `ok`, `context7`
`missing_env` and `fetch` `disabled` (`enabled: false`). Each row carries `name`, `namespace`, `transport`,
`enabled`, `state`, `tools`, `latency_ms`, `error`, `note`, `checked_at`, and no headers or environment values; a
search of the response for token-like strings and `Bearer` found nothing. `GET /` served the built web app
(`web/dist`, HTTP 200, title "Roadmap Tutor").
