"""Idempotent MCP writes: a client retry with the same key gets the stored result instead of a second write.

A key is claimed with INSERT ... ON CONFLICT DO NOTHING (`result` null = running). Success stores the result;
failure deletes the claim so a retry can run again. A claim whose process died stays "running" until its row is
removed by hand (the tools are quick, so this is rare)."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable

import anyio.to_thread
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.tutor.db import session_scope

REUSED = "idempotency key reused with different arguments"
RUNNING = "a request with this idempotency key is still running"


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _claim(engine: Engine, tool: str, key: str, digest: str) -> dict | None:
    """Claim the key (returns None: the caller runs the work) or return the stored result."""
    with session_scope(engine) as s:
        won = s.execute(
            text("INSERT INTO mcp_idempotency (tool, key, args_hash) VALUES (:t, :k, :h) "
                 "ON CONFLICT DO NOTHING"), {"t": tool, "k": key, "h": digest}).rowcount
        if won:
            return None
        row = s.execute(text("SELECT args_hash, result FROM mcp_idempotency WHERE tool = :t AND key = :k"),
                        {"t": tool, "k": key}).one()
    if row.args_hash != digest:
        raise ValueError(REUSED)
    if row.result is None:
        raise ValueError(RUNNING)
    return row.result


def _store(engine: Engine, tool: str, key: str, result: dict) -> None:
    with session_scope(engine) as s:
        s.execute(text("UPDATE mcp_idempotency SET result = CAST(:r AS jsonb) WHERE tool = :t AND key = :k"),
                  {"r": json.dumps(result), "t": tool, "k": key})


def _release(engine: Engine, tool: str, key: str) -> None:
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM mcp_idempotency WHERE tool = :t AND key = :k"), {"t": tool, "k": key})


def run_idempotent(engine: Engine, tool: str, key: str | None, args: dict, fn: Callable[[], dict]) -> dict:
    """`args` must not contain the key. `fn`'s result must be JSON-serialisable."""
    if key is None:
        return fn()
    stored = _claim(engine, tool, key, args_hash(args))
    if stored is not None:
        return stored
    try:
        result = fn()
        _store(engine, tool, key, result)
    except BaseException:
        _release(engine, tool, key)
        raise
    return result


async def arun_idempotent(engine: Engine, tool: str, key: str | None, args: dict,
                          fn: Callable[[], Awaitable[dict]]) -> dict:
    """`run_idempotent` for an async `fn` (the DB calls run in a thread)."""
    if key is None:
        return await fn()
    stored = await anyio.to_thread.run_sync(_claim, engine, tool, key, args_hash(args))
    if stored is not None:
        return stored
    try:
        result = await fn()
        await anyio.to_thread.run_sync(_store, engine, tool, key, result)
    except BaseException:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(_release, engine, tool, key)
        raise
    return result
