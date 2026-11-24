import pytest
from sqlalchemy import text

from backend.tutor.db import session_scope
from backend.tutor.mcp.idempotency import STALE_CLAIM_S, _claim, _release, _store, args_hash, run_idempotent


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
        s.execute(text("INSERT INTO mcp_idempotency (tool, key, args_hash, claim_token) VALUES ('t', 'k', :h, 'tok')"),
                  {"h": args_hash({})})
    with pytest.raises(ValueError, match="still running"):
        run_idempotent(engine, "t", "k", {}, lambda: {"v": 1})


def test_args_hash_is_canonical():
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})


def test_stale_claim_is_reclaimed(engine):
    with session_scope(engine) as s:
        s.execute(text("INSERT INTO mcp_idempotency (tool, key, args_hash, claim_token, created_at) "
                       "VALUES ('t', 'k', :h, 'tok', now() - interval '1 hour')"), {"h": args_hash({})})
    assert run_idempotent(engine, "t", "k", {}, lambda: {"v": 1}) == {"v": 1}
    assert run_idempotent(engine, "t", "k", {}, lambda: {"v": 2}) == {"v": 1}


def _age_claim(engine, key, seconds):
    with session_scope(engine) as s:
        s.execute(text("UPDATE mcp_idempotency SET created_at = now() - make_interval(secs => :s) WHERE key = :k"),
                  {"s": seconds, "k": key})


def _row(engine, key):
    with session_scope(engine) as s:
        return s.execute(text("SELECT claim_token, result FROM mcp_idempotency WHERE key = :k"), {"k": key}).one_or_none()


def test_stale_reclaim_cannot_release_live_claim(engine):
    t1 = _claim(engine, "t", "k", args_hash({})).token
    _age_claim(engine, "k", STALE_CLAIM_S + 1)
    t2 = _claim(engine, "t", "k", args_hash({})).token  # re-claimed as stale
    assert t1 and t2 and t1 != t2
    _release(engine, "t", "k", t1)  # the first, slow call fails late
    assert _row(engine, "k") is not None and _row(engine, "k").claim_token == t2


def test_stale_holder_cannot_store_over_live_claim(engine):
    t1 = _claim(engine, "t", "k", args_hash({})).token
    _age_claim(engine, "k", STALE_CLAIM_S + 1)
    t2 = _claim(engine, "t", "k", args_hash({})).token
    _store(engine, "t", "k", t1, {"v": "late"})  # the slow first call finishes late
    assert _row(engine, "k").result is None
    _store(engine, "t", "k", t2, {"v": "live"})
    assert _row(engine, "k").result == {"v": "live"}


def test_claim_returns_token_or_stored(engine):
    first = _claim(engine, "t", "k", args_hash({}))
    assert first.token and first.stored is None
    _store(engine, "t", "k", first.token, {"v": 1})
    again = _claim(engine, "t", "k", args_hash({}))
    assert again.token is None and again.stored == {"v": 1}
