"""The tutor graph's state, its routes and actions, and the events a turn streams.

Everything in `TutorState` is JSON-plain (D7): the Postgres checkpointer stores it as is.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

# What the coordinator classifies free text into. No route contains "flashcard": only the user enters that mode.
Route = Literal["continue_lesson", "question", "quiz_me", "exercise", "new_topic", "recap"]
# What a UI button sends; routed without a model call.
Action = Literal["start", "continue", "quiz", "recap", "plan", "research"]
Mode = Literal["tutor", "qa"]


class TutorState(TypedDict, total=False):
    roadmap: str  # the roadmap slug
    session_id: str
    mode: Mode
    # {"role": "user"|"assistant", "content": str}. Overwrite semantics (no reducer): a node returns the full new
    # list. The coordinator appends the (redacted) user text of a text turn; replying nodes append their answer.
    messages: list[dict]
    summary: str
    # This turn's input, set by the runtime: {"text": str} (redacted) or {"action": Action, "topic"?: str}
    # (`topic` only with `research`).
    input: dict
    # The coordinator's decision: the classified Route for a text turn, the Action's name for a button turn.
    route: str
    started: bool
    today: dict | None
    quiz: dict | None
    quiz_focus: str | None
    asked: list[str]  # question keys asked this session
    mastery_log: list[dict]
    proposal: dict | None
    session_next: str | None  # session_start's own routing: "planner", "assessor" or None (end the turn)
    allowed_urls: list[str]
    notice: str | None


# A turn streams plain dicts, in the order they happen; the last one is always `done`:
#   {"type": "node", "name": str}                       a node started
#   {"type": "reply_delta", "delta": str, "reset": bool} streamed answer text; `reset`: drop what came so far
#   {"type": "reply", "text": str, "sources": {"roadmap": [...], "web": [...]}, "notice": str | None}
#   {"type": "interrupt", "value": <interrupt value>}   the run paused; resume with {"resume": ...}
#   {"type": "error", "message": str}                   a budget or an outage ended the turn
#   {"type": "done", "trace": <redacted trace summary>}
# Later tasks add `plan`, `quiz_result` and `roadmap_created`; they pass through unchanged.
TurnEvent = dict[str, Any]


def reply_event(text: str, *, roadmap_sources: list | None = None, web_sources: list | None = None,
                notice: str | None = None) -> TurnEvent:
    return {"type": "reply", "text": text,
            "sources": {"roadmap": list(roadmap_sources or []), "web": list(web_sources or [])}, "notice": notice}
