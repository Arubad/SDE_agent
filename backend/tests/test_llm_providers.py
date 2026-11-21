"""LLM providers: fake (scripted / schema-shaped replies), record and replay. No network anywhere."""
import asyncio
import json

import pytest

from backend.agent.loop import run_tool_loop
from backend.core import config, fake_llm, llm
from backend.core.fake_llm import FakeAnthropic, FakeAsyncAnthropic, FakeReply
from backend.core.llm_replay import ReplayMiss, request_key
from backend.core.trace import Trace

SCHEMA = {"type": "object", "required": ["route", "k"],
          "properties": {"route": {"enum": ["kb", "web"]}, "k": {"type": "integer"}}}


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    fake_llm.reset()
    yield
    fake_llm.reset()


def test_haiku_json_fake_returns_schema_instance():
    assert llm.haiku_json(system="s", prompt="p", schema=SCHEMA) == {"route": "kb", "k": 0}


def test_scripted_reply_is_used_once():
    with fake_llm.script([FakeReply(json={"route": "web", "k": 2})]):
        assert llm.haiku_json(system="s", prompt="p", schema=SCHEMA) == {"route": "web", "k": 2}
        assert llm.haiku_json(system="s", prompt="p", schema=SCHEMA) == {"route": "kb", "k": 0}


def test_loop_runs_on_fake_provider():
    passage = {"chunk_id": "c1", "doc_id": "d1", "title": "T1", "section": "", "source_type": "github",
               "url": "https://x/1", "score": 1.0, "text": "text 1", "timestamp": None}

    async def call_tool(name, args):
        return {"passages": [passage]}

    async def emit(_):
        pass

    replies = [FakeReply(tool_uses=[("search_knowledge_base", {"query": "x"})]), FakeReply(text="done")]
    with fake_llm.script(replies):
        result = asyncio.run(run_tool_loop([{"role": "user", "content": "q"}], system="s", tool_defs=[],
                                           call_tool=call_tool, check_call=lambda n, a, seen: None, trace=Trace(),
                                           emit=emit, max_steps=4))
    assert result.text == "done" and result.steps == 2
    assert len(fake_llm.requests) == 2


def test_record_then_replay(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "_foundry_client", lambda: FakeAnthropic())
    monkeypatch.setattr(config, "LLM_PROVIDER", "record")
    recorded = llm.haiku_json(system="s", prompt="p", schema=SCHEMA)
    assert len(list(tmp_path.glob("*.json"))) == 1

    monkeypatch.setattr(config, "LLM_PROVIDER", "replay")
    assert llm.haiku_json(system="s", prompt="p", schema=SCHEMA) == recorded
    with pytest.raises(ReplayMiss):
        llm.haiku_json(system="s", prompt="another prompt", schema=SCHEMA)


def test_async_stream_record_then_replay(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "_foundry_async_client", lambda: FakeAsyncAnthropic())
    kwargs = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "q"}]}

    async def run_stream():
        client = llm.haiku_async_client()
        try:
            async with client.messages.stream(**kwargs) as stream:
                chunks = [t async for t in stream.text_stream]
                final = await stream.get_final_message()
        finally:
            await client.close()
        return chunks, final

    monkeypatch.setattr(config, "LLM_PROVIDER", "record")
    with fake_llm.script([FakeReply(text="streamed answer")]):
        asyncio.run(run_stream())
    assert len(list(tmp_path.glob("*.json"))) == 1

    monkeypatch.setattr(config, "LLM_PROVIDER", "replay")
    chunks, final = asyncio.run(run_stream())
    assert chunks == ["streamed answer"]
    assert final.content[0].text == "streamed answer" and final.stop_reason == "end_turn"


def test_request_key_ignores_cache_control():
    plain = {"model": "m", "system": [{"type": "text", "text": "s"}], "messages": [{"role": "user", "content": "q"}]}
    cached = {"model": "m", "cache_control": {"type": "ephemeral"},
              "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
              "messages": [{"role": "user", "content": "q"}]}
    assert request_key(plain) == request_key(cached)
    assert request_key(plain) != request_key({**plain, "messages": [{"role": "user", "content": "other"}]})


SECRET_REQUEST = {"model": "m", "max_tokens": 10, "system": "key sk-ant-api03-abcdefghijklmnop1234",
                  "messages": [{"role": "user", "content": "mail jane.doe@example.com about it"}]}


def _written(tmp_path) -> str:
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


def test_recordings_are_redacted_on_write(monkeypatch, tmp_path):
    """Sync client: request and response are redacted in the file; the key is of the unredacted request."""
    monkeypatch.setattr(config, "LLM_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "_foundry_client", lambda: FakeAnthropic())
    monkeypatch.setattr(config, "LLM_PROVIDER", "record")
    with fake_llm.script([FakeReply(text="write to bob@corp.example with sk-ant-api03-zzzzzzzzzzzzzz")]):
        recorded = llm.haiku_client().messages.create(**SECRET_REQUEST)
    blob = _written(tmp_path)
    for leaked in ("sk-ant-api03", "jane.doe@example.com", "bob@corp.example"):
        assert leaked not in blob
    assert (tmp_path / f"{request_key(SECRET_REQUEST)}.json").exists()

    monkeypatch.setattr(config, "LLM_PROVIDER", "replay")
    hit = llm.haiku_client().messages.create(**SECRET_REQUEST)  # the same unredacted request still matches
    assert hit.content[0].text == "write to [email] with [secret]"
    assert recorded.content[0].text.startswith("write to bob@")  # the live caller still sees the real reply


def test_async_recordings_are_redacted_on_write(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "_foundry_async_client", lambda: FakeAsyncAnthropic())

    async def run_stream():
        client = llm.haiku_async_client()
        try:
            async with client.messages.stream(**SECRET_REQUEST) as stream:
                return await stream.get_final_message()
        finally:
            await client.close()

    monkeypatch.setattr(config, "LLM_PROVIDER", "record")
    with fake_llm.script([FakeReply(text="ping bob@corp.example")]):
        asyncio.run(run_stream())
    blob = _written(tmp_path)
    for leaked in ("sk-ant-api03", "jane.doe@example.com", "bob@corp.example"):
        assert leaked not in blob
    monkeypatch.setattr(config, "LLM_PROVIDER", "replay")
    assert asyncio.run(run_stream()).content[0].text == "ping [email]"


def test_legacy_bare_message_recordings_still_load(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_RECORDINGS_DIR", str(tmp_path))
    message = FakeAnthropic().messages.create(**SECRET_REQUEST)
    (tmp_path / f"{request_key(SECRET_REQUEST)}.json").write_text(message.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(config, "LLM_PROVIDER", "replay")
    assert llm.haiku_client().messages.create(**SECRET_REQUEST).content[0].text == message.content[0].text


def test_redacted_response_is_flagged_warned_and_reported(monkeypatch, tmp_path, caplog):
    from backend.core import llm_replay

    monkeypatch.setattr(config, "LLM_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "_foundry_client", lambda: FakeAnthropic())
    monkeypatch.setattr(config, "LLM_PROVIDER", "record")
    llm_replay.redacted_recordings.clear()
    with fake_llm.script([FakeReply(text="mail bob@corp.example"), FakeReply(text="clean reply")]):
        with caplog.at_level("WARNING"):
            llm.haiku_client().messages.create(**SECRET_REQUEST)
        llm.haiku_client().messages.create(**{**SECRET_REQUEST, "max_tokens": 11})
    flagged = [json.loads(p.read_text()).get("redacted_response") for p in sorted(tmp_path.glob("*.json"))]
    assert sorted(map(bool, flagged)) == [False, True]
    assert any("redacted" in r.message and str(tmp_path) in r.message for r in caplog.records)
    assert len(llm_replay.redacted_recordings) == 1
    llm_replay.redacted_recordings.clear()
    monkeypatch.setattr(config, "LLM_PROVIDER", "replay")
    llm.haiku_client().messages.create(**SECRET_REQUEST)
    llm.haiku_client().messages.create(**{**SECRET_REQUEST, "max_tokens": 11})
    assert len(llm_replay.redacted_recordings) == 1  # only the flagged one is reported on replay
