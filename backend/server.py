"""FastAPI app: chat agent (SSE), passcode tiers, and the MCP server over Streamable HTTP at /mcp."""
from __future__ import annotations

import asyncio
import json
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from backend.agent import access
from backend.agent.orchestrator import run_agent
from backend.core import config
from backend.core.llm import LLMUnavailable
from backend.core.trace import start_trace
from backend.mcp_server import mcp
from backend.rag import store

FRONTEND_FILE = config.REPO_DIR / "frontend" / "index.html"


class BearerAuth:
    """ASGI guard for /mcp: requires `Authorization: Bearer <PREMIUM_PASSCODE>`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = headers.get("authorization", "").removeprefix("Bearer ").strip()
            ip = access.client_ip(headers, (scope.get("client") or ["unknown"])[0])
            try:
                ok = access.check_passcode(token, ip)
            except access.TooManyAttempts as e:
                return await JSONResponse({"error": str(e)}, status_code=429)(scope, receive, send)
            if not ok:
                return await JSONResponse({"error": "MCP endpoint requires Authorization: Bearer <passcode>"},
                                          status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)


mcp_app = mcp.http_app(path="/", stateless_http=True)


@asynccontextmanager
async def lifespan(app):
    # Load embedding / BM25 / reranker models before the first request instead of during it
    def warm():
        store.embed_query("warm up")
        next(store.sparse_model().query_embed("warm up"))
        if config.RETRIEVAL_MODE.endswith("_rerank"):
            store.reranker(config.RERANKER_MODEL).predict([("warm", "up")])
    await asyncio.to_thread(warm)
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(title="SDE Mastery Hub", lifespan=lifespan)
app.mount("/mcp", BearerAuth(mcp_app))


@app.middleware("http")
async def cors_and_pna_middleware(request: Request, call_next):
    origin = request.headers.get("origin", "*")
    if request.method == "OPTIONS":
        response = Response(status_code=200)
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = request.headers.get("access-control-request-headers", "*")
        response.headers["Access-Control-Max-Age"] = "86400"
    else:
        response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = origin
    if "access-control-request-private-network" in request.headers:
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


@app.get("/")
async def serve_index():
    if FRONTEND_FILE.exists():
        return FileResponse(FRONTEND_FILE)
    return {"message": "SDE Mastery Hub API online."}


@app.get("/health")
async def health():
    return {"status": "ok", "premium_available": access.premium_enabled(), "collection": config.KB_COLLECTION}


@app.get("/api/stats")
async def stats():
    return await asyncio.to_thread(store.collection_stats)


class AuthRequest(BaseModel):
    passcode: str


def _ip(request: Request) -> str:
    return access.client_ip(request.headers, request.client.host if request.client else "unknown")


def _tier(request: Request) -> str:
    code = request.headers.get("x-access-code")
    if not code:
        return "public"
    try:
        return "premium" if access.check_passcode(code, _ip(request)) else "public"
    except access.TooManyAttempts as e:
        raise HTTPException(status_code=429, detail=str(e))


@app.post("/auth")
async def auth(req: AuthRequest, request: Request):
    if not access.premium_enabled():
        return {"premium": False, "detail": "The premium tier is not configured on this server."}
    try:
        ok = access.check_passcode(req.passcode, _ip(request))
    except access.TooManyAttempts as e:
        raise HTTPException(status_code=429, detail=str(e))
    return {"premium": ok, "detail": "Claude Haiku tier unlocked" if ok else "Wrong passcode"}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    history: list = Field(default_factory=list)
    stream: bool = True


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    tier = _tier(request)
    queue: asyncio.Queue = asyncio.Queue()
    steps: list[dict] = []

    async def emit(event: dict) -> None:
        if event.get("type") != "reply_delta":
            steps.append(event)
        await queue.put(event)

    async def worker():
        trace = start_trace(f"chat:{tier}")
        try:
            result = await asyncio.wait_for(run_agent(req.message, req.history, tier, trace, emit),
                                            timeout=config.REQUEST_TIMEOUT_S)
            await emit({"type": "reply", "reply": result.reply, "tier": result.tier, "route": result.route,
                        "cached": result.cached, "sources": result.sources, "verification": result.verification,
                        "steps": list(steps)})
        except asyncio.TimeoutError:
            await emit({"type": "error", "detail": f"The agent took longer than {config.REQUEST_TIMEOUT_S:.0f}s and was stopped."})
        except LLMUnavailable as e:
            hint = " Enter a passcode to use the Claude tier, or try again later." if tier == "public" else ""
            await emit({"type": "error", "detail": f"The language model is unavailable right now ({str(e)[:160]}).{hint}"})
        except Exception as e:
            traceback.print_exc()
            await emit({"type": "error", "detail": f"{type(e).__name__}: {str(e)[:300]}"})
        finally:
            trace.log()
            await queue.put({"type": "trace", "trace": trace.summary()})
            await queue.put({"type": "done"})

    task = asyncio.create_task(worker())

    if not req.stream:
        await task
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        reply = next((e for e in events if e["type"] == "reply"), None)
        if reply is None:
            err = next((e for e in events if e["type"] == "error"), {"detail": "unknown error"})
            raise HTTPException(status_code=503, detail=err["detail"])
        reply["trace"] = next((e["trace"] for e in events if e["type"] == "trace"), None)
        return reply

    async def sse():
        yield f"data: {json.dumps({'type': 'session', 'tier': tier})}\n\n"
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] == "done":
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)))
