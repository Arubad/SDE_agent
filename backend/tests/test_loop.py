import asyncio
from types import SimpleNamespace as NS
from backend.agent.loop import run_tool_loop
from backend.core.trace import Trace

def P(i): return {"chunk_id": f"c{i}", "doc_id": f"d{i}", "title": f"T{i}", "section": "", "source_type": "github",
                  "url": f"https://x/{i}", "score": 1.0, "text": f"text {i}", "timestamp": None}
def msg(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=1, output_tokens=1), model="fake")
def text(t, cites=()):
    return NS(type="text", text=t, citations=[NS(type="search_result_location", search_result_index=i) for i in cites] or None)
def use(id, name, **inp): return NS(type="tool_use", id=id, name=name, input=inp)

def run(turns, tool_results, *, max_steps=4, check=lambda n, a, seen: None):
    seen_msgs, flags, calls = [], [], []
    async def model_turn(messages, allow_tools):
        seen_msgs.append([dict(m) for m in messages]); flags.append(allow_tools)
        return turns[len(flags) - 1]
    async def call_tool(name, args):
        calls.append((name, args)); return tool_results[len(calls) - 1]
    async def emit(_): pass
    res = asyncio.run(run_tool_loop([{"role": "user", "content": "q"}], system="s", tool_defs=[], call_tool=call_tool,
                                    check_call=check, trace=Trace(), emit=emit, max_steps=max_steps, model_turn=model_turn))
    return res, seen_msgs, flags, calls

def test_parallel_tool_calls_come_back_in_one_user_message():
    turns = [msg(use("a", "search_knowledge_base", query="x"), use("b", "search_knowledge_base", query="y"), stop="tool_use"),
             msg(text("answer", cites=[1]))]
    res, seen, flags, calls = run(turns, [{"passages": [P(1)]}, {"passages": [P(2)]}])
    last = seen[1][-1]
    assert last["role"] == "user" and [b["tool_use_id"] for b in last["content"]] == ["a", "b"]
    assert all(b["type"] == "tool_result" and b["content"][0]["type"] == "search_result" for b in last["content"])
    assert len(calls) == 2 and res.steps == 2 and res.stop == "answered"
    assert res.text.startswith("answer") and res.sources[0]["chunk_id"] == "c2"
    assert [p["chunk_id"] for p in res.passages] == ["c1", "c2"]

def test_citation_index_counts_only_search_result_blocks_across_turns():
    turns = [msg(use("a", "search_knowledge_base", query="1"), stop="tool_use"),
             msg(use("b", "search_knowledge_base", query="2"), stop="tool_use"),
             msg(use("c", "search_knowledge_base", query="3"), stop="tool_use"),
             msg(text("final", cites=[2]))]
    res, *_ = run(turns, [{"error": "boom"}, {"passages": [P(1), P(2)]}, {"passages": [P(3)]}])
    assert res.sources[0]["chunk_id"] == "c3"

def test_last_step_disables_tools_and_reports_budget():
    ask = lambda i: msg(use(f"t{i}", "search_knowledge_base", query="x"), stop="tool_use")
    res, _, flags, _ = run([ask(1), ask(2), msg(text("forced answer"))], [{"passages": [P(1)]}, {"passages": [P(2)]}], max_steps=3)
    assert flags == [True, True, False] and res.stop == "budget" and res.text == "forced answer"

def test_refused_call_is_an_error_result_and_the_tool_is_not_called():
    turns = [msg(use("a", "analyze_youtube_video", video_url="https://youtu.be/zzzzzzzzzz1"), stop="tool_use"), msg(text("ok"))]
    res, seen, _, calls = run(turns, [], check=lambda n, a, s: "video not provided by the user")
    block = seen[1][-1]["content"][0]
    assert calls == [] and block["is_error"] is True and "not provided by the user" in block["content"][0]["text"]
    assert res.tool_calls == [{"name": "analyze_youtube_video", "args": {"video_url": "https://youtu.be/zzzzzzzzzz1"}, "ok": False, "refused": True}]

def test_empty_and_failed_results_are_text_blocks():
    turns = [msg(use("a", "search_knowledge_base", query="x"), use("b", "search_knowledge_base", query="y"), stop="tool_use"), msg(text("ok"))]
    _, seen, *_ = run(turns, [{"passages": []}, {"error": "timeout"}])
    empty, failed = seen[1][-1]["content"]
    assert empty["content"] == [{"type": "text", "text": "No passages found."}] and not empty.get("is_error")
    assert failed["is_error"] is True and failed["content"][0]["text"] == "timeout"

def test_seen_doc_ids_are_passed_to_the_check():
    got = []
    turns = [msg(use("a", "search_knowledge_base", query="x"), stop="tool_use"),
             msg(use("b", "get_document", doc_id="d1"), stop="tool_use"), msg(text("ok"))]
    run(turns, [{"passages": [P(1)]}, {"passages": [P(1)]}], check=lambda n, a, seen: got.append(set(seen)))
    assert got == [set(), {"d1"}]

def test_refusal_and_max_tokens_stop_cleanly():
    res, *_ = run([msg(stop="refusal")], [])
    assert res.stop == "refusal" and res.text == "I can't help with that request."
    res, *_ = run([msg(text("partial ans"), stop="max_tokens")], [])
    assert res.stop == "truncated" and res.text == "partial ans"
