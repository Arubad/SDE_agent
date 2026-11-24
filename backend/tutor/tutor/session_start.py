"""Session start and recap: a card of the last ended session, answered from stored summaries (no model call).

Group `session_start`: `session_start_prepare` (reads only; builds the card into `state.proposal`) ->
`session_start_ask` (interrupts first, D8) -> a routing edge on `state.session_next`.
Reached by the `start` action (the session-start card) and by `recap` (button or text; the recap card).
The trigger is read from `state.input`, never from `state.route`.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from backend.tutor.models import TutorSession
from backend.tutor.tutor.graph import Group, TutorDeps, emit, node
from backend.tutor.tutor.sessions import ended_sessions
from backend.tutor.tutor.state import TutorState, reply_event

OPTIONS = ["recap_last", "recap_earlier", "warm_up", "skip"]
EARLIER_LIMIT = 5
NO_SESSION_TEXT = "There is no earlier session to recap yet."
NO_SUMMARY_TEXT = "That session's summary isn't ready yet."


def _date(row: TutorSession) -> str | None:
    return row.ended_at.date().isoformat() if row.ended_at else None


def _card(last: TutorSession, earlier: list[TutorSession], trigger: str) -> dict:
    return {"kind": "session_start", "trigger": trigger,
            "last": {"id": str(last.id), "title": last.title, "date": _date(last), "summary": last.summary},
            "earlier": [{"id": str(r.id), "title": r.title, "date": _date(r)} for r in earlier],
            "options": [o for o in OPTIONS if earlier or o != "recap_earlier"]}


def resume_ok(value: Any, pending: Any) -> bool:
    """The `session_start` resume shape: a `choice` the card offers; `recap_earlier` also names one of its sessions."""
    if not isinstance(value, dict) or not isinstance(pending, dict):
        return False
    choice = value.get("choice")
    if choice not in pending.get("options", []):
        return False
    if choice == "recap_earlier":
        return value.get("session_id") in [e["id"] for e in pending.get("earlier", [])]
    return True


def _session_summary(deps: TutorDeps, roadmap: str, session_id: str) -> str | None:
    return next((r.summary for r in ended_sessions(deps.engine, roadmap, EARLIER_LIMIT + 1)
                 if str(r.id) == session_id), None)


def add_session_start(builder: StateGraph, deps: TutorDeps) -> Group:
    async def prepare(state: TutorState) -> dict:
        is_start = (state.get("input") or {}).get("action") == "start"
        if is_start and state.get("started"):  # a reload re-sent `start`
            return {"proposal": None, "session_next": None}
        sessions = ended_sessions(deps.engine, state["roadmap"], EARLIER_LIMIT + 1)
        if not sessions:
            if is_start:
                return {"started": True, "proposal": None, "session_next": "planner"}
            emit(reply_event(NO_SESSION_TEXT))
            return {"proposal": None, "session_next": None}
        card = _card(sessions[0], sessions[1:], "start" if is_start else "recap")
        return {"started": True if is_start else state.get("started", False), "proposal": card,
                "session_next": None}

    async def ask(state: TutorState) -> dict:
        card = state["proposal"]
        answer = interrupt(card)  # first statement (D8)
        choice, update = answer["choice"], {"proposal": None, "session_next": None}
        if choice == "skip":
            return {**update, "session_next": "planner" if card["trigger"] == "start" else None}
        if choice == "warm_up":
            return {**update, "quiz_focus": "weak_spots", "session_next": "assessor"}
        if choice == "recap_last":
            summary = card["last"]["summary"]
        else:
            summary = _session_summary(deps, state["roadmap"], answer["session_id"])
        text = summary or NO_SUMMARY_TEXT
        emit(reply_event(text))
        messages = [*(state.get("messages") or []), {"role": "assistant", "content": text}]
        return {**update, "messages": messages}

    builder.add_node("session_start_prepare", node("session_start_prepare", prepare))
    builder.add_node("session_start_ask", node("session_start_ask", ask))

    def route_prepare(state: TutorState) -> str:
        if state.get("proposal"):
            return "session_start_ask"
        return state.get("session_next") or END

    def wire(builder: StateGraph, entries: dict[str, str]) -> None:
        targets = {"planner": entries["planner"], "assessor": entries["assessor"]}
        builder.add_conditional_edges("session_start_prepare", route_prepare,
                                      {END: END, "session_start_ask": "session_start_ask", **targets})
        builder.add_conditional_edges("session_start_ask", lambda s: s.get("session_next") or END,
                                      {END: END, **targets})

    return Group("session_start_prepare", None, wire)
