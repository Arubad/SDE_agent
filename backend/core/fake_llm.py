"""Offline stand-in for the Anthropic client (LLM_PROVIDER=fake): scripted or schema-shaped replies, no network.

Replies are real `anthropic.types.Message` objects, so code under test (and the record provider) sees exactly
the shape the SDK returns. Every call's keyword arguments are kept in `requests` for assertions.
"""
from __future__ import annotations

import itertools
import json as _json
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from anthropic.types import Message

DEFAULT_TEXT = "Fake answer."

@dataclass
class FakeReply:
    text: str | None = None
    json: dict | None = None
    tool_uses: list[tuple[str, dict]] = field(default_factory=list)
    stop_reason: str | None = None  # None: "tool_use" if there are tool uses, else "end_turn"


requests: list[dict] = []  # every call's keyword arguments, in order
_queue: deque[FakeReply] = deque()
_ids = itertools.count(1)


@contextmanager
def script(replies: list[FakeReply]) -> Iterator[None]:
    """Queue replies, consumed one per call in order; once the queue is empty the default reply applies."""
    _queue.extend(replies)
    try:
        yield
    finally:
        _queue.clear()


def reset() -> None:
    requests.clear()
    _queue.clear()


def _example(schema: dict) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "object" or (kind is None and "properties" in schema):
        props = schema.get("properties", {})
        return {key: _example(props.get(key, {})) for key in schema.get("required", [])}
    return {"string": "fake", "integer": 0, "number": 0.0, "boolean": False, "array": []}.get(kind)


def example_from_schema(schema: dict) -> dict:
    """A minimal instance of a JSON schema: required keys only, the first enum value, zero values for scalars."""
    return _example(schema)


def _default_text(kwargs: dict) -> str:
    fmt = (kwargs.get("output_config") or {}).get("format") or {}
    if fmt.get("type") == "json_schema":
        return _json.dumps(example_from_schema(fmt["schema"]))
    return DEFAULT_TEXT


def _reply(kwargs: dict) -> Message:
    requests.append(kwargs)
    reply = _queue.popleft() if _queue else FakeReply()
    text = reply.text if reply.text is not None else (_json.dumps(reply.json) if reply.json is not None else None)
    if text is None and not reply.tool_uses:
        text = _default_text(kwargs)
    content: list[dict] = []
    if text is not None:
        content.append({"type": "text", "text": text, "citations": None})
    for name, args in reply.tool_uses:
        content.append({"type": "tool_use", "id": f"toolu_fake_{next(_ids)}", "name": name, "input": args})
    stop = reply.stop_reason or ("tool_use" if reply.tool_uses else "end_turn")
    return Message.model_validate({
        "id": f"msg_fake_{next(_ids)}", "type": "message", "role": "assistant", "model": "fake",
        "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    })


class StaticStream:
    """An `async with client.messages.stream(...)` over a finished message: the text arrives as one chunk."""

    def __init__(self, message: Message):
        self._message = message

    async def __aenter__(self) -> StaticStream:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    @property
    def text_stream(self):
        return self._chunks()

    async def _chunks(self):
        text = "".join(b.text for b in self._message.content if b.type == "text")
        if text:
            yield text

    async def get_final_message(self) -> Message:
        return self._message


class _Messages:
    def create(self, **kwargs) -> Message:
        return _reply(kwargs)


class _AsyncMessages:
    def stream(self, **kwargs) -> StaticStream:
        return StaticStream(_reply(kwargs))


class FakeAnthropic:
    def __init__(self):
        self.messages = _Messages()


class FakeAsyncAnthropic:
    def __init__(self):
        self.messages = _AsyncMessages()

    async def close(self) -> None:
        return None
