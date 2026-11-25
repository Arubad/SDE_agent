"""The turn runtime: one tutor turn in, a stream of `TurnEvent`s out.

- Ending a session (`end_session`, `create_session`) takes the turn lock of the session it ends, so it waits for
  a running turn (END_WAIT_S, else `Busy`) and no turn runs on an ended session.
- One turn at a time per session: a per-session `asyncio.Lock` is held for the whole turn; a second turn raises
  `Busy` on its first `__anext__`, before anything else happens.
- The session row gives the graph its `roadmap` (slug), `session_id` and `mode`. Unknown -> `LookupError`; ended ->
  `SessionEnded`. Every entry point canonicalises the id first (`canonical_id`, M3; not a UUID -> `LookupError`),
  so the lock and the checkpoint thread have one key per session.
- Turns: `{"text": str}` (redacted before it enters graph state), `{"action": Action}` (plus `"topic"` with
  `research`), or `{"resume": value}`, which needs a pending interrupt whose `kind` has a validator in RESUME_SHAPES
  that accepts `value` (a validator may take `(value, pending)`); otherwise `BadResume`, before the graph runs, so state is unchanged. The value is then
  `redact_obj`ed (its strings; dict keys are not learner text) before the graph sees it.
- A text or action turn while an interrupt is pending abandons it (R5): the new input starts a fresh run from
  START; the state before the interrupt is kept, and `pending()` is None afterwards. LangGraph does exactly this
  when a thread with pending tasks gets new (non-Command) input, so no state surgery is needed; a test pins it.
- `BudgetExceeded` and `LLMUnavailable` (after the nodes' own retries) end the turn with an `error` event. Either
  way, and always last, `done` carries the redacted trace summary; the session's `cost_usd` grows by the turn's
  cost, its `last_turn_at` is stamped, and the trace is exported. Other exceptions propagate (cost and export still
  happen, shielded, so also when the consumer is cancelled, e.g. by a client disconnect). The checkpointer keeps the state of the last completed
  super-step, so the next turn starts from there.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
import weakref
from contextlib import AsyncExitStack
from datetime import datetime
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Any, get_args

import anyio
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy import func, select, update

from backend.core.llm import LLMUnavailable
from backend.core.redact import redact, redact_obj
from backend.core.trace import Trace, start_trace
from backend.tutor.models import Roadmap, TutorSession
from backend.tutor.telemetry import export_trace
from backend.tutor.tutor.assessor import resume_ok as quiz_resume_ok
from backend.tutor.tutor.budget import OUTAGE_MESSAGE, BudgetExceeded
from backend.tutor.tutor.graph import TutorDeps
from backend.tutor.tutor import sessions
from backend.tutor.tutor.session_start import resume_ok
from backend.tutor.tutor.state import Action, TurnEvent

# Interrupt value `kind` -> a check of a resume value's shape. Later tasks register their kinds here.
# A validator takes `(value)` or `(value, pending)`; the latter also sees the pending interrupt value, for answers
# that must match what was offered (e.g. a choice among the card's options).
def approval_ok(value: Any, pending: Any) -> bool:
    """An approval interrupt resumes with exactly `{"approved": bool, "card": <the pending card's id>}` (D8, R10):
    an answer to an earlier card is not an answer to this one."""
    return (isinstance(value, dict) and set(value) == {"approved", "card"} and isinstance(value["approved"], bool)
            and isinstance(pending, dict) and isinstance(pending.get("id"), str) and value["card"] == pending["id"])


RESUME_SHAPES: dict[str, Callable[..., bool]] = {"session_start": resume_ok, "approval": approval_ok,
                                                  "quiz": quiz_resume_ok}


def _accepts(valid: Callable[..., bool], value: Any, pending: Any) -> bool:
    try:
        takes_pending = len(inspect.signature(valid).parameters) >= 2
    except (TypeError, ValueError):
        takes_pending = False
    return valid(value, pending) if takes_pending else valid(value)


END_WAIT_S = 120.0  # how long ending a session waits for its running turn before giving up with `Busy`


class Busy(Exception):
    """Another turn of this session is running."""


class BadResume(ValueError):
    """A resume with nothing pending, or whose value does not fit the pending interrupt's kind."""


class SessionEnded(Exception):
    """The session has ended; it takes no more turns."""


def canonical_id(session_id: str) -> str:
    """The session id as one spelling (M3): the turn lock and the checkpoint thread are keyed on it, so an upper-case
    or braced id is the same session. Not a UUID -> `LookupError`."""
    try:
        return str(uuid.UUID(str(session_id)))
    except ValueError:
        raise LookupError(session_id) from None


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def _input(turn: dict) -> dict:
    """The graph's `input` for a text or action turn; user text is redacted here, before it enters state."""
    if set(turn) == {"text"} and isinstance(turn["text"], str):
        return {"text": redact(turn["text"])}
    action = turn.get("action")
    if action in get_args(Action) and set(turn) <= {"action", "topic"}:
        if "topic" not in turn:
            return {"action": action}
        if action == "research" and isinstance(turn["topic"], str):
            return {"action": action, "topic": redact(turn["topic"])}
    raise ValueError(f"not a tutor turn: {sorted(turn)}")


def _first_interrupt(snapshot) -> Any | None:
    interrupts = getattr(snapshot, "interrupts", None) or tuple(
        i for task in snapshot.tasks for i in task.interrupts)
    return interrupts[0].value if interrupts else None


class TutorRuntime:
    def __init__(self, deps: TutorDeps, graph: CompiledStateGraph):
        self.deps = deps
        self.graph = graph
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        return lock

    async def _end_locked(self, session_ids: list[uuid.UUID], work: Callable[[], Any],
                          wait_s: float | None = None) -> Any:
        """Run sync `work` in a thread once no turn of any of `session_ids` is running (each turn lock is held
        meanwhile, so a turn arriving now gets `Busy`, then `SessionEnded`). Waits at most `wait_s` (default
        END_WAIT_S) per lock."""
        async with AsyncExitStack() as stack:
            for sid in sorted(session_ids):
                lock = self._lock(str(sid))
                try:
                    await asyncio.wait_for(lock.acquire(), END_WAIT_S if wait_s is None else wait_s)
                except TimeoutError:
                    raise Busy(str(sid)) from None
                stack.callback(lock.release)
            return await asyncio.to_thread(work)

    async def end_session(self, session_id: str, *, wait_s: float | None = None,
                          idle_before: datetime | None = None) -> TutorSession:
        """End a session after its running turn (if any) finishes, and queue its curation. `wait_s` bounds the wait
        for that turn (default END_WAIT_S, else `Busy`); with `idle_before` the session is ended only if it is still
        idle since before then, checked under the lock (the retention loop's idle end, R19)."""
        sid = sessions.parse_id(session_id)
        return await self._end_locked(
            [sid], lambda: sessions.end_session(self.deps.engine, sid, self.deps.enqueue, idle_before=idle_before),
            wait_s)

    async def create_session(self, slug: str, mode: str) -> TutorSession:
        """A new session; the roadmap's active session is ended first, after its running turn finishes."""
        active = await asyncio.to_thread(sessions.active_session_ids, self.deps.engine, slug)
        return await self._end_locked(
            active, lambda: sessions.create_session(self.deps.engine, slug, mode, enqueue=self.deps.enqueue))

    async def run_turn(self, session_id: str, turn: dict) -> AsyncIterator[TurnEvent]:
        session_id = canonical_id(session_id)
        lock = self._lock(session_id)
        if lock.locked():
            raise Busy(session_id)
        async with lock:
            row = await asyncio.to_thread(self._session, session_id)  # under the lock: an ended session is refused
            cfg = _config(session_id)
            if "resume" in turn:
                if set(turn) != {"resume"}:
                    raise ValueError(f"not a tutor turn: {sorted(turn)}")
                await self._check_resume(cfg, turn["resume"])
                # Learner text in a resume (e.g. quiz answers) is redacted before it reaches the checkpoint.
                graph_input: Any = Command(resume=redact_obj(turn["resume"]))
            else:
                graph_input = {"roadmap": row["roadmap"], "session_id": session_id, "mode": row["mode"],
                               "input": _input(turn)}
            trace = start_trace("tutor_turn")
            finished = False
            try:
                try:
                    async with aclosing(self._stream(graph_input, cfg)) as events:
                        async for event in events:
                            yield event
                except BudgetExceeded as e:
                    yield {"type": "error", "message": e.message}
                except LLMUnavailable:
                    yield {"type": "error", "message": OUTAGE_MESSAGE}
                finished = True
                await self._finish(session_id, trace)
                yield {"type": "done", "trace": redact_obj(trace.summary())}
            finally:
                if not finished:  # an exception propagates, or the consumer left early
                    # Shielded: a consumer cancelled mid-turn (a client disconnect cancels the response's scope)
                    # still records the cost and exports the trace.
                    with anyio.CancelScope(shield=True):
                        await self._finish(session_id, trace)

    async def _stream(self, graph_input: Any, cfg: dict) -> AsyncIterator[TurnEvent]:
        """Custom events as the nodes emit them, plus one `interrupt` event when the run pauses. A consumer that
        leaves early closes the graph's stream with it (aclosing), so no run is left behind."""
        stream = self.graph.astream(graph_input, cfg, stream_mode=["custom", "updates"])
        async with aclosing(stream):
            async for mode, chunk in stream:
                if mode == "custom":
                    yield chunk
                elif "__interrupt__" in chunk:
                    yield {"type": "interrupt", "value": chunk["__interrupt__"][0].value}

    async def _check_resume(self, cfg: dict, value: Any) -> None:
        pending = _first_interrupt(await self.graph.aget_state(cfg))
        if pending is None:
            raise BadResume("nothing is waiting for an answer")
        kind = pending.get("kind") if isinstance(pending, dict) else None
        valid = RESUME_SHAPES.get(kind)
        if valid is None or not _accepts(valid, value, pending):
            raise BadResume(f"that answer does not fit the pending {kind!r} question")

    async def _finish(self, session_id: str, trace: Trace) -> None:
        await asyncio.to_thread(self._record_turn, session_id, trace.summary()["cost_usd"])
        export_trace(trace, self.deps.tracer_provider)

    def _session(self, session_id: str) -> dict:
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise LookupError(session_id) from None
        with self.deps.engine.connect() as conn:
            row = conn.execute(
                select(TutorSession.mode, TutorSession.status, Roadmap.slug)
                .join(Roadmap, Roadmap.id == TutorSession.roadmap_id).where(TutorSession.id == sid)
            ).first()
        if row is None:
            raise LookupError(session_id)
        if row.status == "ended":
            raise SessionEnded(session_id)
        return {"mode": row.mode, "roadmap": row.slug}

    def _record_turn(self, session_id: str, cost: float) -> None:
        """The turn's cost, and `last_turn_at` (the retention loop ends sessions idle since then, R19)."""
        with self.deps.engine.begin() as conn:
            conn.execute(update(TutorSession).where(TutorSession.id == uuid.UUID(session_id))
                         .values(cost_usd=TutorSession.cost_usd + cost, last_turn_at=func.now()))

    async def pending(self, session_id: str) -> dict | None:
        return _first_interrupt(await self.graph.aget_state(_config(canonical_id(session_id))))

    async def state(self, session_id: str) -> dict:
        snapshot = await self.graph.aget_state(_config(canonical_id(session_id)))
        values = snapshot.values or {}
        return {"messages": values.get("messages", []), "summary": values.get("summary", ""),
                "today": values.get("today"), "pending": _first_interrupt(snapshot)}
