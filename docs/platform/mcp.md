# MCP in the roadmap tutor

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
