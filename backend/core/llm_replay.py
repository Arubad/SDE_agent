"""Record / replay providers: responses are stored as `<LLM_RECORDINGS_DIR>/<request_key>.json`.

`record` wraps a real client and writes each response; `replay` serves them back without network access and
raises ReplayMiss for any request that was never recorded. The key ignores `cache_control`, so turning prompt
caching on or off does not invalidate recordings.

A recording is `{"request": ..., "response": ...}`, both run through `redact_obj` (secrets, emails, phone numbers)
before they are written. The key is computed on the UNREDACTED request, so replaying the same request still hits;
replay returns the redacted response. A bare message (the older format) still loads.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from anthropic.types import Message

from backend.core import config
from backend.core.fake_llm import StaticStream
from backend.core.redact import redact_obj


class ReplayMiss(RuntimeError):
    """The replay provider has no recording for this request."""


def _canonical(value: Any) -> Any:
    if hasattr(value, "model_dump"):  # SDK content blocks passed back in the conversation history
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def request_key(kwargs: dict) -> str:
    blob = json.dumps(_canonical(kwargs), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _path(kwargs: dict) -> Path:
    return Path(config.LLM_RECORDINGS_DIR) / f"{request_key(kwargs)}.json"


def _save(kwargs: dict, message) -> None:
    path = _path(kwargs)  # keyed on the unredacted request
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"request": redact_obj(_canonical(kwargs)), "response": redact_obj(message.model_dump(mode="json"))}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")


def _load(kwargs: dict) -> Message:
    path = _path(kwargs)
    if not path.exists():
        raise ReplayMiss(f"no recording {path.name} in {path.parent}; re-run with LLM_PROVIDER=record")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Message.model_validate(data["response"] if "response" in data and "request" in data else data)


# --- record -----------------------------------------------------------------------

class _RecordingMessages:
    def __init__(self, inner):
        self._inner = inner

    def create(self, **kwargs):
        message = self._inner.messages.create(**kwargs)
        _save(kwargs, message)
        return message


class RecordingClient:
    def __init__(self, inner):
        self.messages = _RecordingMessages(inner)


class _RecordingStream:
    def __init__(self, manager, kwargs: dict):
        self._manager, self._kwargs, self._stream = manager, kwargs, None

    async def __aenter__(self) -> _RecordingStream:
        self._stream = await self._manager.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._manager.__aexit__(*exc)

    @property
    def text_stream(self):
        return self._stream.text_stream

    async def get_final_message(self):
        message = await self._stream.get_final_message()
        _save(self._kwargs, message)
        return message


class _AsyncRecordingMessages:
    def __init__(self, inner):
        self._inner = inner

    def stream(self, **kwargs) -> _RecordingStream:
        return _RecordingStream(self._inner.messages.stream(**kwargs), kwargs)


class AsyncRecordingClient:
    def __init__(self, inner):
        self._inner = inner
        self.messages = _AsyncRecordingMessages(inner)

    async def close(self) -> None:
        await self._inner.close()


# --- replay -----------------------------------------------------------------------

class _ReplayMessages:
    def create(self, **kwargs) -> Message:
        return _load(kwargs)


class ReplayClient:
    def __init__(self):
        self.messages = _ReplayMessages()


class _AsyncReplayMessages:
    def stream(self, **kwargs) -> StaticStream:
        return StaticStream(_load(kwargs))


class AsyncReplayClient:
    def __init__(self):
        self.messages = _AsyncReplayMessages()

    async def close(self) -> None:
        return None
