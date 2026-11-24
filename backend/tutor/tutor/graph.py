"""The tutor's LangGraph: a coordinator that routes each turn to one node group. LangGraph is orchestration only;
every model call goes through `backend/core/llm.py`.

Shape: START -> coordinator -> <group> ... -> END.
- Buttons (`input.action`) route by ACTION_MAP with no model call; free text (`input.text`) is classified by
  `coordinator.classify` and routes by ROUTE_MAP. In `qa` mode every route but `new_topic` goes to the teacher.
- Targets are *groups*, not nodes. A group is a sequence of nodes added by one adder function,
  `add_<group>(builder, deps) -> Group`, registered in GROUPS. The adder adds its nodes (each wrapped with `node`)
  and its internal edges, and returns its entry node and its exit node. `build_tutor_graph` points the routes at
  the entry and connects the exit to FOLLOWS[group] (another group's entry) or END. An exit of None means the group
  wires its own way out (e.g. a conditional edge).
- To replace a placeholder (Tasks 7-11), change that group's adder in GROUPS, e.g. a `session_start` group of
  `session_start_prepare` -> `session_start_ask` returns `Group("session_start_prepare", "session_start_ask")`.
  Routes and FOLLOWS keep naming the group.
- `node(name, fn)` wraps every node: it emits `{"type": "node", "name"}` and runs `fn` inside the
  `node:<name>` trace span. An interrupt node's `fn` must call `interrupt()` first (D8): LangGraph re-runs it from
  the top on resume, so work leading up to the interrupt belongs in the node before it, written to state.
- Nodes stream events with `emit(...)` (LangGraph's custom stream); see `state.TurnEvent`.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.core.trace import current_trace
from backend.tutor.tutor.coordinator import classify
from backend.tutor.tutor.state import TurnEvent, TutorState, reply_event

EXERCISE_TEXT = "Code exercises arrive in a later version; here is the concept instead."
PLACEHOLDER_TEXT = "(not built yet)"

ROUTE_MAP = {"continue_lesson": "teacher", "question": "teacher", "quiz_me": "assessor",
             "exercise": "exercise_stub", "new_topic": "researcher", "recap": "session_start"}
ACTION_MAP = {"start": "session_start", "continue": "teacher", "quiz": "assessor", "recap": "session_start",
              "plan": "planner", "research": "researcher"}
QA_ROUTES = {"new_topic"}  # in qa mode, the routes that keep their target; the rest go to the teacher
FOLLOWS = {"exercise_stub": "teacher"}  # a group's exit continues into another group (default END)


@dataclass
class TutorDeps:
    engine: Any
    gateway: Any
    index: Any
    embedder: Any
    settings: Any
    saver: Any
    enqueue: Callable[..., Any] | None = None  # `JobContext.enqueue`-shaped, or None
    tracer_provider: Any = None  # None: traces are not exported


@dataclass(frozen=True)
class Group:
    entry: str
    exit: str | None  # connected to FOLLOWS[group] or END by build_tutor_graph; None: the group wires its exits


NodeFn = Callable[[TutorState], Awaitable[dict | None]]
Adder = Callable[[StateGraph, TutorDeps], Group]


def emit(event: TurnEvent) -> None:
    get_stream_writer()(event)


def node(name: str, fn: NodeFn) -> NodeFn:
    """`fn` wrapped: a `node` event, then `fn` inside the `node:<name>` span."""

    async def run(state: TutorState) -> dict | None:
        emit({"type": "node", "name": name})
        with current_trace().span(f"node:{name}", kind="node"):
            return await fn(state)

    run.__name__ = name
    return run


def single(builder: StateGraph, name: str, fn: NodeFn) -> Group:
    builder.add_node(name, node(name, fn))
    return Group(name, name)


def route_target(state: TutorState) -> str:
    """The group the coordinator's decision leads to."""
    inp = state.get("input") or {}
    if "action" in inp:
        return ACTION_MAP[inp["action"]]
    route = state["route"]
    if state.get("mode") == "qa" and route not in QA_ROUTES:
        return "teacher"
    return ROUTE_MAP[route]


def add_coordinator(builder: StateGraph, deps: TutorDeps) -> Group:
    async def coordinator(state: TutorState) -> dict:
        inp = state.get("input") or {}
        if "action" in inp:
            return {"route": inp["action"]}
        messages = list(state.get("messages") or [])
        route = await classify(deps, inp["text"], messages)
        return {"route": route, "messages": [*messages, {"role": "user", "content": inp["text"]}]}

    return single(builder, "coordinator", coordinator)


def add_exercise_stub(builder: StateGraph, deps: TutorDeps) -> Group:
    async def exercise_stub(state: TutorState) -> None:
        emit(reply_event(EXERCISE_TEXT))

    return single(builder, "exercise_stub", exercise_stub)


def _placeholder(name: str) -> Adder:
    def add(builder: StateGraph, deps: TutorDeps) -> Group:
        async def fn(state: TutorState) -> None:
            emit(reply_event(PLACEHOLDER_TEXT))

        return single(builder, name, fn)

    add.__name__ = f"add_{name}"
    return add


GROUPS: dict[str, Adder] = {
    "session_start": _placeholder("session_start"),
    "planner": _placeholder("planner"),
    "teacher": _placeholder("teacher"),
    "assessor": _placeholder("assessor"),
    "researcher": _placeholder("researcher"),
    "exercise_stub": add_exercise_stub,
}


def build_tutor_graph(deps: TutorDeps, groups: Mapping[str, Adder] | None = None) -> CompiledStateGraph:
    """`groups` overrides GROUPS entries by name (tests use it to plug in nodes)."""
    builder = StateGraph(TutorState)
    coordinator = add_coordinator(builder, deps)
    entries: dict[str, str] = {}
    exits: dict[str, str | None] = {}
    for name, add in {**GROUPS, **(groups or {})}.items():
        group = add(builder, deps)
        entries[name], exits[name] = group.entry, group.exit
    builder.add_edge(START, coordinator.entry)
    targets = set(ROUTE_MAP.values()) | set(ACTION_MAP.values())
    builder.add_conditional_edges(coordinator.exit, route_target, {t: entries[t] for t in targets})
    for name, exit_node in exits.items():
        if exit_node is not None:
            builder.add_edge(exit_node, entries[FOLLOWS[name]] if name in FOLLOWS else END)
    return builder.compile(checkpointer=deps.saver)
