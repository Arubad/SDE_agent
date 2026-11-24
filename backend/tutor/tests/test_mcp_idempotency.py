import pytest
from sqlalchemy import text

from backend.tutor.db import session_scope
from backend.tutor.mcp.idempotency import args_hash, run_idempotent


@pytest.fixture(autouse=True)
def clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM mcp_idempotency"))


def test_no_key_just_runs(engine):
    calls = []
    assert run_idempotent(engine, "t", None, {"a": 1}, lambda: calls.append(1) or {"n": len(calls)}) == {"n": 1}
    assert run_idempotent(engine, "t", None, {"a": 1}, lambda: calls.append(1) or {"n": len(calls)}) == {"n": 2}


def test_same_key_same_args_replays(engine):
    calls = []
    fn = lambda: calls.append(1) or {"n": len(calls)}  # noqa: E731
    assert run_idempotent(engine, "t", "k", {"a": 1}, fn) == {"n": 1}
    assert run_idempotent(engine, "t", "k", {"a": 1}, fn) == {"n": 1}
    assert len(calls) == 1


def test_same_key_other_tool_is_independent(engine):
    assert run_idempotent(engine, "t1", "k", {}, lambda: {"v": 1}) == {"v": 1}
    assert run_idempotent(engine, "t2", "k", {}, lambda: {"v": 2}) == {"v": 2}


def test_different_args_rejected(engine):
    run_idempotent(engine, "t", "k", {"a": 1}, lambda: {"v": 1})
    with pytest.raises(ValueError, match="reused with different arguments"):
        run_idempotent(engine, "t", "k", {"a": 2}, lambda: {"v": 2})


def test_failure_releases_claim(engine):
    def boom():
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        run_idempotent(engine, "t", "k", {}, boom)
    assert run_idempotent(engine, "t", "k", {}, lambda: {"v": 1}) == {"v": 1}


def test_running_claim_reported(engine):
    with session_scope(engine) as s:
        s.execute(text("INSERT INTO mcp_idempotency (tool, key, args_hash) VALUES ('t', 'k', :h)"),
                  {"h": args_hash({})})
    with pytest.raises(ValueError, match="still running"):
        run_idempotent(engine, "t", "k", {}, lambda: {"v": 1})


def test_args_hash_is_canonical():
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})


def test_stale_claim_is_reclaimed(engine):
    with session_scope(engine) as s:
        s.execute(text("INSERT INTO mcp_idempotency (tool, key, args_hash, created_at) "
                       "VALUES ('t', 'k', :h, now() - interval '1 hour')"), {"h": args_hash({})})
    assert run_idempotent(engine, "t", "k", {}, lambda: {"v": 1}) == {"v": 1}
    assert run_idempotent(engine, "t", "k", {}, lambda: {"v": 2}) == {"v": 1}
