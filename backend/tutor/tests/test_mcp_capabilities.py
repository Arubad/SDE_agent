"""Pin the MCP features the Phase 3 gateway relies on (mcp 1.30.0 / fastmcp 3.4.11).

If a dependency change drops progress notifications, elicitation or per-call timeouts, these fail first.
"""
import asyncio
import socket
import threading
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn
from fastapi import FastAPI
from fastmcp import Client, Context, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import BaseModel

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Approve(BaseModel):
    approve: bool


def _server() -> FastMCP:
    server = FastMCP("cap")

    @server.tool
    async def work(ctx: Context) -> str:
        for i in range(3):
            await ctx.report_progress(i + 1, 3, f"step {i + 1}")
        return "done"

    @server.tool
    async def ask(ctx: Context) -> str:
        try:
            r = await ctx.elicit("Approve?", response_type=Approve)
        except Exception:
            return "unsupported"
        return f"accept:{r.data.approve}" if r.action == "accept" else r.action

    @server.tool
    async def slow() -> str:
        await asyncio.sleep(5)
        return "late"

    return server


STEPS = [(1, 3, "step 1"), (2, 3, "step 2"), (3, 3, "step 3")]


async def test_progress_notifications_reach_client():
    seen = []

    async def on_progress(p, t, m):
        seen.append((p, t, m))

    async with Client(_server(), progress_handler=on_progress) as c:
        await c.call_tool("work", {})
    assert seen == STEPS


async def test_elicitation_accept_decline_and_unsupported():
    async def accept(message, response_type, params, context):
        return Approve(approve=True)

    async def decline(message, response_type, params, context):
        return ElicitResult(action="decline")

    async with Client(_server(), elicitation_handler=accept) as c:
        assert (await c.call_tool("ask", {})).data == "accept:True"
    async with Client(_server(), elicitation_handler=decline) as c:
        assert (await c.call_tool("ask", {})).data == "decline"
    async with Client(_server()) as c:
        assert (await c.call_tool("ask", {})).data == "unsupported"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _serve_http(server: FastMCP):
    mcp_app = server.http_app(path="/", stateless_http=False)
    app = FastAPI(lifespan=mcp_app.lifespan)
    app.mount("/mcp", mcp_app)
    port = _free_port()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp/"
    finally:
        uv.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        assert not thread.is_alive()


async def test_progress_and_elicitation_over_streamable_http():
    seen = []

    async def on_progress(p, t, m):
        seen.append((p, t, m))

    async def accept(message, response_type, params, context):
        return Approve(approve=True)

    async with _serve_http(_server()) as url:
        async with Client(StreamableHttpTransport(url), progress_handler=on_progress,
                          elicitation_handler=accept) as c:
            await c.call_tool("work", {})
            assert (await c.call_tool("ask", {})).data == "accept:True"
    assert seen == STEPS


async def test_call_timeout_is_enforced():
    async with Client(_server()) as c:
        t0 = time.monotonic()
        with pytest.raises(Exception):
            await c.call_tool("slow", {}, timeout=0.2)
        assert time.monotonic() - t0 < 1
