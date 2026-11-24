"""Idempotent MCP writes: a client retry with the same key gets the stored result instead of a second write.

A key is claimed with INSERT ... ON CONFLICT DO NOTHING (`result` null = running). Every claim carries a fresh
token; success stores the result and failure deletes the claim, both only while the caller's token is still the
current one. A claim still running after STALE_CLAIM_S is abandoned: the next call re-claims it atomically under
a new token and runs, so the slow first call, if it ever finishes or fails, can neither overwrite nor delete
the live claim."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import NamedTuple

import anyio.to_thread
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.tutor.db import session_scope

STALE_CLAIM_S = 900  # a claim still running after this long is taken to be abandoned (its process died)
REUSED = "idempotency key reused with different arguments"
RUNNING = "a request with this idempotency key is still running"


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Claim(NamedTuple):
    """Exactly one field is set: `token` (the caller won the claim and runs the work) or `stored` (replay it)."""
    token: str | None
    stored: dict | None


def _claim(engine: Engine, tool: str, key: str, digest: str) -> Claim:
    """Claim the key, with a fresh token the caller must present to store or release it, or return the stored
    result. A stale claim is re-claimed under a new token, which locks the slow first holder out."""
    token = uuid.uuid4().hex
    with session_scope(engine) as s:
        won = s.execute(
            text("INSERT INTO mcp_idempotency (tool, key, args_hash, claim_token) VALUES (:t, :k, :h, :c) "
                 "ON CONFLICT DO NOTHING"), {"t": tool, "k": key, "h": digest, "c": token}).rowcount
        if won:
            return Claim(token, None)
        row = s.execute(text("SELECT args_hash, result FROM mcp_idempotency WHERE tool = :t AND key = :k"),
                        {"t": tool, "k": key}).one()
    if row.args_hash != digest:
        raise ValueError(REUSED)
    if row.result is None:
        with session_scope(engine) as s:
            took = s.execute(
                text("UPDATE mcp_idempotency SET created_at = now(), claim_token = :c "
                     "WHERE tool = :t AND key = :k "
                     "AND result IS NULL AND created_at < now() - make_interval(secs => :s)"),
                {"t": tool, "k": key, "s": STALE_CLAIM_S, "c": token}).rowcount
        if took:
            return Claim(token, None)
        raise ValueError(RUNNING)
    return Claim(None, row.result)


def _store(engine: Engine, tool: str, key: str, token: str, result: dict) -> None:
    with session_scope(engine) as s:
        s.execute(text("UPDATE mcp_idempotency SET result = CAST(:r AS jsonb) "
                       "WHERE tool = :t AND key = :k AND claim_token = :c"),
                  {"r": json.dumps(result), "t": tool, "k": key, "c": token})


def _release(engine: Engine, tool: str, key: str, token: str) -> None:
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM mcp_idempotency WHERE tool = :t AND key = :k AND claim_token = :c"),
                  {"t": tool, "k": key, "c": token})


def run_idempotent(engine: Engine, tool: str, key: str | None, args: dict, fn: Callable[[], dict]) -> dict:
    """`args` must not contain the key. `fn`'s result must be JSON-serialisable."""
    if key is None:
        return fn()
    claim = _claim(engine, tool, key, args_hash(args))
    if claim.token is None:
        return claim.stored
    try:
        result = fn()
        _store(engine, tool, key, claim.token, result)
    except BaseException:
        _release(engine, tool, key, claim.token)
        raise
    return result


async def arun_idempotent(engine: Engine, tool: str, key: str | None, args: dict,
                          fn: Callable[[], Awaitable[dict]]) -> dict:
    """`run_idempotent` for an async `fn` (the DB calls run in a thread)."""
    if key is None:
        return await fn()
    claim = await anyio.to_thread.run_sync(_claim, engine, tool, key, args_hash(args))
    if claim.token is None:
        return claim.stored
    try:
        result = await fn()
        await anyio.to_thread.run_sync(_store, engine, tool, key, claim.token, result)
    except BaseException:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(_release, engine, tool, key, claim.token)
        raise
    return result
