import asyncio
import pytest
from fastmcp import Client
from backend.agent import orchestrator as orch
from backend.core import config
from backend.core.llm import LLMUnavailable
from backend.core.trace import Trace
from backend.mcp_server import mcp

PASSAGE = {"chunk_id": "c1", "doc_id": "d1", "title": "T", "section": "", "source_type": "github",
           "url": "https://good.example/doc", "score": 1.0, "text": "t", "timestamp": None, "n": 1}

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_CACHE", False)

def go(tier="premium", message="q"):
    async def emit(_): pass
    return asyncio.run(orch.run_agent(message, [], tier, Trace(), emit))

def fake(route, text="answer[1]"):
    async def f(*a, **k): return text, [PASSAGE], [PASSAGE], None, route
    return f

def test_premium_uses_loop_only_when_mode_is_loop(monkeypatch):
    monkeypatch.setattr(orch, "_run_loop", fake("agent_loop")); monkeypatch.setattr(orch, "_run_pipeline", fake("knowledge_base"))
    monkeypatch.setattr(config, "AGENT_MODE", "loop");     assert go().route == "agent_loop"
    monkeypatch.setattr(config, "AGENT_MODE", "pipeline"); assert go().route == "knowledge_base"

def test_public_tier_never_uses_the_loop(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MODE", "loop")
    monkeypatch.setattr(orch, "_run_loop", fake("agent_loop")); monkeypatch.setattr(orch, "_run_pipeline", fake("knowledge_base"))
    assert go(tier="public").route == "knowledge_base"

def test_loop_outage_falls_back_to_pipeline(monkeypatch):
    async def down(*a, **k): raise LLMUnavailable("down")
    monkeypatch.setattr(config, "AGENT_MODE", "loop")
    monkeypatch.setattr(orch, "_run_loop", down); monkeypatch.setattr(orch, "_run_pipeline", fake("knowledge_base"))
    assert go().route == "knowledge_base"

def test_untrusted_links_removed_only_when_defense_is_on(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MODE", "loop")
    monkeypatch.setattr(orch, "_run_loop", fake("agent_loop", "see [x](https://evil.example/a) and [d](https://good.example/doc)[1]"))
    monkeypatch.setattr(config, "INJECTION_DEFENSE", True)
    reply = go().reply
    assert "evil.example" not in reply and "https://good.example/doc" in reply
    monkeypatch.setattr(config, "INJECTION_DEFENSE", False)
    assert "evil.example" in go().reply

def test_tool_defs_are_discovered_from_mcp_and_allow_listed():
    async def main():
        async with Client(mcp) as c:
            return await orch.mcp_tool_defs(c)
    defs = asyncio.run(main())
    assert {d["name"] for d in defs} == {"search_knowledge_base", "analyze_youtube_video", "get_document"}
    assert all(d["description"] and d["input_schema"]["type"] == "object" for d in defs)

def test_system_prompt_composition(monkeypatch):
    monkeypatch.setattr(config, "INJECTION_DEFENSE", True)
    assert orch.guard.UNTRUSTED_RULE in orch.system_prompt("loop") and orch.LOOP_INSTRUCTIONS in orch.system_prompt("loop")
    assert orch.LOOP_INSTRUCTIONS not in orch.system_prompt("pipeline")
    monkeypatch.setattr(config, "INJECTION_DEFENSE", False)
    assert orch.guard.UNTRUSTED_RULE not in orch.system_prompt("loop")

def test_agent_eval_resource_is_listed():
    import asyncio
    from fastmcp import Client
    from backend.mcp_server import mcp
    async def main():
        async with Client(mcp) as c:
            return [str(r.uri) for r in await c.list_resources()]
    assert "kb://eval/agent" in asyncio.run(main())
