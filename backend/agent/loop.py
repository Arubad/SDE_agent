"""Tool-use agent loop: Claude decides which tools to call, in parallel, until it answers or hits the step budget.

Passages come back to the model as search_result blocks inside tool_result, so the final answer carries
API-native citations. The API numbers citations by the position of each search_result block across the
whole conversation, so the loop keeps `passages` in exactly the order the blocks were sent.

The loop knows nothing about FastAPI or MCP: tools, the safety check and (in tests) the model are injected.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anthropic

from backend.agent.citations import render_citations, search_result_block
from backend.core import config
from backend.core.llm import LLMUnavailable, haiku_async_client
from backend.core.trace import Trace

ToolCaller = Callable[[str, dict], Awaitable[dict]]          # {"passages": [...]} or {"error": "..."}
CallCheck = Callable[[str, dict, set[str]], "str | None"]     # (name, args, seen_doc_ids) -> refusal reason
ModelTurn = Callable[[list[dict], bool], Awaitable[Any]]      # (messages, allow_tools) -> Message-like
Emit = Callable[[dict], Awaitable[None]]

REFUSAL_TEXT = "I can't help with that request."
NO_PASSAGES = "No passages found."


@dataclass
class LoopResult:
    text: str
    sources: list[dict]
    passages: list[dict]
    steps: int
    tool_calls: list[dict] = field(default_factory=list)
    stop: str = "answered"  # answered | budget | refusal | truncated


def default_model_turn(*, system: str, tool_defs: list[dict], trace: Trace, emit: Emit) -> ModelTurn:
    async def turn(messages: list[dict], allow_tools: bool):
        client = haiku_async_client()
        with trace.span("agent_turn", kind="llm", allow_tools=allow_tools) as span:
            try:
                async with client.messages.stream(
                    model=config.HAIKU_MODEL, max_tokens=8000, system=system, messages=messages, tools=tool_defs,
                    tool_choice={"type": "auto"} if allow_tools else {"type": "none"},
                    output_config={"effort": config.AGENT_EFFORT},
                ) as stream:
                    async for text in stream.text_stream:
                        await emit({"type": "reply_delta", "delta": text, "reset": False})
                    final = await stream.get_final_message()
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                raise LLMUnavailable(f"Haiku unavailable: {e}") from e
            finally:
                await client.close()
            trace.count_llm_call()
            span.set(model=final.model, input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens,
                     stop_reason=final.stop_reason)
        if final.stop_reason == "tool_use":
            await emit({"type": "reply_delta", "delta": "", "reset": True})  # text before tool calls is not the answer
        return final
    return turn


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


async def run_tool_loop(messages: list[dict], *, system: str, tool_defs: list[dict], call_tool: ToolCaller,
                        check_call: CallCheck, trace: Trace, emit: Emit, max_steps: int,
                        model_turn: ModelTurn | None = None) -> LoopResult:
    model_turn = model_turn or default_model_turn(system=system, tool_defs=tool_defs, trace=trace, emit=emit)
    messages = list(messages)
    passages: list[dict] = []
    seen_doc_ids: set[str] = set()
    tool_calls: list[dict] = []
    used_tools = False

    for step in range(1, max_steps + 1):
        allow_tools = step < max_steps
        response = await model_turn(messages, allow_tools)
        messages.append({"role": "assistant", "content": response.content})  # unchanged (thinking blocks survive)

        if response.stop_reason == "tool_use" and allow_tools:
            used_tools = True
            uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            refusals = [check_call(u.name, dict(u.input or {}), seen_doc_ids) for u in uses]
            to_run = [(u, r) for u, r in zip(uses, refusals) if r is None]
            ran = dict(zip((u.id for u, _ in to_run),
                           await asyncio.gather(*(call_tool(u.name, dict(u.input or {})) for u, _ in to_run))))

            results = []
            for u, reason in zip(uses, refusals):
                args = dict(u.input or {})
                if reason is not None:
                    tool_calls.append({"name": u.name, "args": args, "ok": False, "refused": True})
                    results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                                    "content": [_text_block(reason)]})
                    continue
                out = ran[u.id] or {"error": "tool returned nothing"}
                if "error" in out:
                    tool_calls.append({"name": u.name, "args": args, "ok": False, "refused": False})
                    results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                                    "content": [_text_block(str(out["error"]))]})
                    continue
                found = out.get("passages") or []
                tool_calls.append({"name": u.name, "args": args, "ok": True, "refused": False})
                if not found:
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": [_text_block(NO_PASSAGES)]})
                    continue
                for p in found:
                    passages.append(p)
                    if p.get("doc_id"):
                        seen_doc_ids.add(p["doc_id"])
                results.append({"type": "tool_result", "tool_use_id": u.id,
                                "content": [search_result_block(p) for p in found]})
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "refusal":
            return LoopResult(REFUSAL_TEXT, [], passages, step, tool_calls, "refusal")
        text, sources = render_citations(response, passages)
        if response.stop_reason == "max_tokens":
            stop = "truncated"
        elif step == max_steps and used_tools:
            stop = "budget"
        else:
            stop = "answered"
        return LoopResult(text, sources, passages, step, tool_calls, stop)

    raise AssertionError("unreachable: the last step never allows tools")  # pragma: no cover
