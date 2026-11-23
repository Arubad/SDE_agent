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
