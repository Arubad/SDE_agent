"""Agent orchestration.

Premium tier (passcode, Claude Haiku 5.5 on Foundry):
    cache -> router (structured output: route, standalone question, sub-queries, filters)
          -> parallel MCP tool calls (KB searches + YouTube) with per-tool timeouts
          -> generator (search_result blocks => API-native citations, streamed)
          -> verifier (structured output) -> at most one corrective retrieval + regeneration
Public tier (free Gemini key, ~1 LLM call per message to stretch the free quota):
    cache -> rule-based router -> parallel MCP tool calls -> Gemini generator with numbered citations
          -> if Gemini is out of quota: return the top retrieved passages instead of failing

Every UI timeline event corresponds to a step that actually ran; a full trace (timings, tokens,
LLM-call count) is emitted at the end and logged.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import anthropic
from fastmcp import Client

from backend.agent import cache, guard
from backend.core import config
from backend.agent.citations import render_citations, search_result_block
from backend.agent.citations import source_label as _source_label
from backend.core.llm import LLMUnavailable, gemini_generate, haiku_async_client, haiku_json
from backend.agent.loop import run_tool_loop
from backend.core.trace import Trace
from backend.mcp_server import _retrieval_label, mcp
from backend.tools.youtube import find_video_urls

Emit = Callable[[dict], Awaitable[None]]

TUTOR_PRINCIPLES = """You are an elite Software Engineering tutor. Your teaching leans on:
1. System design: practical, scalable architectures and their trade-offs over theory.
2. SRE: Google SRE principles (SLOs, error budgets, toil elimination).
3. Testing: the Testing Trophy; strong integration and end-to-end tests (e.g. Playwright).
4. Algorithms: the learner knows the basics. Any data structures / algorithms code MUST be C++."""

GROUNDING_RULES = """Grounding rules:
- Answer the question directly. Do not open with commentary about the sources or how much they cover.
- Base factual claims on the provided sources and cite them. Do not invent sources.
- If the sources leave a gap that matters, say so in one short sentence where it applies. You may add
  widely known background, but label that sentence "(general knowledge)".
- If a source is a YouTube video, refer to timestamps when the passages give them.
- Be concise and well structured: short sections, bullet points, code blocks where useful."""

LOOP_INSTRUCTIONS = """You have tools for a software engineering knowledge base. Work like this:
- Search before answering any factual question. For a question with several parts, issue the
  searches together in one turn.
- Read the results. If they do not cover part of the question, search again with different
  wording, at most twice.
- If the user shared a YouTube link, load it with analyze_youtube_video before answering.
- If the knowledge base does not cover the question, say so plainly. Do not present
  unsupported material as if it came from the sources.
- Every factual sentence in your answer must be supported by a search result. Do not pad the
  answer with advice, examples or framings the results do not contain; if you add brief
  background they do not state, label that sentence "(general knowledge)".
- When you have enough, answer the question and stop."""


def system_prompt(mode: str) -> str:
    parts = [TUTOR_PRINCIPLES, GROUNDING_RULES]
    if mode == "loop":
        parts.append(LOOP_INSTRUCTIONS)
    if config.INJECTION_DEFENSE:
        parts.append(guard.UNTRUSTED_RULE)
    return "\n\n".join(parts)


_URL = re.compile(r"https?://[^\s)>\]\"']+")


def finalize_text(text: str, passages: list[dict], message: str) -> str:
    """Strip links/images the answer was not given by a retrieved source or the user (injection exfiltration)."""
    if not config.INJECTION_DEFENSE:
        return text
    allowed = {p["url"] for p in passages if p.get("url")} | set(_URL.findall(message))
    return guard.sanitize_links(text, allowed)


SMALL_TALK = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|cool|great|bye|good (morning|evening))\W*$", re.I)


@dataclass
class AgentResult:
    reply: str
    tier: str
    route: str
    sources: list[dict] = field(default_factory=list)
    verification: dict | None = None
    cached: bool = False
    context_ids: list[str] = field(default_factory=list)  # every passage the generator saw


# --- history helpers -------------------------------------------------------------------

def _history_text(history: list[dict], turns: int) -> list[tuple[str, str]]:
    out = []
    for msg in history[-turns * 2:]:
        parts = msg.get("parts") or []
        text = parts[0].get("text", "") if parts and isinstance(parts[0], dict) else ""
        if text.strip():
            out.append(("assistant" if msg.get("role") in ("model", "assistant") else "user", text))
    return out


def _anthropic_history(history: list[dict]) -> list[dict]:
    msgs = []
    for role, text in _history_text(history, config.HISTORY_TURNS):
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + text
        else:
            msgs.append({"role": role, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


# --- tool layer (MCP) --------------------------------------------------------------------

class Tools:
    """Calls the MCP server in-process (in-memory transport): one code path for web + IDE clients."""

    def __init__(self, client: Client, trace: Trace, emit: Emit):
        self.client, self.trace, self.emit = client, trace, emit

    async def call(self, name: str, args: dict, *, timeout: float, ui: dict) -> dict | None:
        await self.emit({"type": "tool_start", **ui})
        with self.trace.span(f"mcp:{name}", kind="tool", args=json.dumps(args)[:200]) as span:
            try:
                result = await asyncio.wait_for(self.client.call_tool(name, args, raise_on_error=False), timeout)
            except asyncio.TimeoutError:
                span.status = "error"
                await self.emit({"type": "tool_error", "stage": ui.get("stage"), "title": f"{ui['title']}: timed out",
                                 "detail": f"No result within {timeout:.0f}s"})
                return None
            if result.is_error:
                msg = " ".join(getattr(c, "text", "") for c in result.content)[:300]
                span.status = "error"
                span.set(error=msg)
                await self.emit({"type": "tool_error", "stage": ui.get("stage"), "title": f"{ui['title']}: failed",
                                 "detail": msg})
                return None
            data = result.structured_content or {}
            span.set(passages=len(data.get("passages", [])))
            return data


def _merge_passages(groups: list[list[dict]], limit: int) -> list[dict]:
    """Interleave ranked lists (round-robin) and de-duplicate, so every sub-query contributes."""
    seen, merged = set(), []
    for rank in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if rank < len(g) and g[rank]["chunk_id"] not in seen:
                seen.add(g[rank]["chunk_id"])
                merged.append(g[rank])
    return merged[:limit]


def _short(p: dict) -> str:
    t = p["title"]
    return t if len(t) <= 48 else t[:45] + "…"


# --- the agent -----------------------------------------------------------------------------

class Agent:
    def __init__(self, tier: str, trace: Trace, emit: Emit):
        self.tier, self.trace, self.emit = tier, trace, emit

    def _budget_left(self, needed: int = 1) -> bool:
        return self.trace.llm_calls + needed <= config.MAX_LLM_CALLS

    # ---- routing ----
    async def route(self, message: str, history: list[dict]) -> dict:
        videos = find_video_urls(message)
        if self.tier == "premium":
            return await self._route_llm(message, history, videos)
        return self._route_rules(message, history, videos)

    def _route_rules(self, message: str, history: list[dict], videos: list[str]) -> dict:
        query = message
        prev = [t for r, t in _history_text(history, 2) if r == "user"]
        if prev and len(message.split()) < 8:  # short follow-up ("and its failure modes?") -> add context
            query = f"{prev[-1]} {message}"
        if videos:
            route = "knowledge_base_and_youtube" if len(message.split()) > 12 else "youtube"
        elif SMALL_TALK.match(message):
            route = "direct"
        else:
            route = "knowledge_base"
        return {"route": route, "standalone_question": query, "search_queries": [query],
                "source_types": [], "youtube_urls": videos, "reason": "rule-based router (public tier)"}

    async def _route_llm(self, message: str, history: list[dict], videos: list[str]) -> dict:
        convo = "\n".join(f"{r.upper()}: {t[:600]}" for r, t in _history_text(history, 3)) or "(none)"
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["route", "standalone_question", "search_queries", "source_types", "reason"],
            "properties": {
                "route": {"type": "string", "enum": ["knowledge_base", "youtube", "knowledge_base_and_youtube", "direct"]},
                "standalone_question": {"type": "string"},
                "search_queries": {"type": "array", "items": {"type": "string"}},
                "source_types": {"type": "array", "items": {"type": "string", "enum": ["github", "book", "web", "youtube"]}},
                "reason": {"type": "string"},
            },
        }
        system = (
            "You route questions for a software engineering tutor with a knowledge base (System Design Primer, "
            "Software Engineering at Google, MIT Missing Semester, Google eng-practices, ingested YouTube videos).\n"
            "- standalone_question: rewrite the latest message so it is self-contained, resolving references to "
            "the conversation (e.g. 'its failure modes' -> 'failure modes of consistent hashing').\n"
            "- search_queries: 1-3 focused keyword-rich search queries; split multi-part questions. Empty only for 'direct'.\n"
            "- route 'direct' only for greetings/small talk or pure code-writing requests needing no sources.\n"
            "- route 'youtube' / 'knowledge_base_and_youtube' only if the message contains a YouTube link "
            "(also use the KB when the user asks how the video relates to broader concepts).\n"
            "- source_types: restrict only if the user explicitly asks for a source (e.g. 'what does the Google book say'). "
            "Otherwise []. 'book' = Software Engineering at Google.\n- reason: one short sentence."
        )
        prompt = f"Conversation so far:\n{convo}\n\nLatest message:\n{message}\n\nYouTube links detected: {videos or 'none'}"
        try:
            plan = await asyncio.to_thread(haiku_json, system=system, prompt=prompt, schema=schema, effort="low",
                                           span_name="router")
        except LLMUnavailable:
            plan = self._route_rules(message, history, videos)
            plan["reason"] = "router unavailable, fell back to rules"
        # deterministic guards: a pasted video link must always be loaded, and queries must exist
        if videos and plan["route"] in ("knowledge_base", "direct"):
            plan["route"] = "knowledge_base_and_youtube"
        if not videos and "youtube" in plan["route"]:
            plan["route"] = "knowledge_base"
        if plan["route"] != "direct" and not plan["search_queries"]:
            plan["search_queries"] = [plan["standalone_question"] or message]
        plan["search_queries"] = plan["search_queries"][:3]
        plan["youtube_urls"] = videos
        return plan

    # ---- tools ----
    async def gather(self, tools: Tools, plan: dict, extra_queries: list[str] | None = None) -> list[dict]:
        tasks, video_task_idx = [], []
        if "youtube" in plan["route"]:
            for url in plan["youtube_urls"][:2]:
                video_task_idx.append(len(tasks))
                tasks.append(tools.call("analyze_youtube_video", {"video_url": url, "focus": plan["standalone_question"]},
                                        timeout=config.VIDEO_TOOL_TIMEOUT_S,
                                        ui={"stage": "youtube", "title": "Loading YouTube Video", "tool": "MCP · analyze_youtube_video",
                                            "query": url, "detail": "Stored copy → captions → Gemini video understanding"}))
        if "knowledge_base" in plan["route"]:
            for q in (extra_queries or plan["search_queries"]):
                args = {"query": q, "top_k": config.TOP_K}
                if plan.get("source_types"):
                    args["source_types"] = plan["source_types"]
                tasks.append(tools.call("search_knowledge_base", args, timeout=config.TOOL_TIMEOUT_S,
                                        ui={"stage": "rag", "title": "Searching Knowledge Base", "tool": "MCP · search_knowledge_base",
                                            "query": q, "detail": _retrieval_label()}))
        results = await asyncio.gather(*tasks)

        groups = []
        for i, res in enumerate(results):
            if not res:
                continue
            passages = res.get("passages", [])
            if i in video_task_idx:
                how = {"knowledge_base": "already in knowledge base", "transcript": "captions",
                       "gemini_video": "Gemini watched the video"}[res["method"]]
                ingested = f", embedded {res['chunk_count']} chunks into the KB" if res.get("newly_ingested") else ""
                await self.emit({"type": "tool_done", "stage": "youtube", "title": "Video Loaded",
                                 "detail": f"{res['title']} ({how}{ingested})", "sources": [res["title"][:48]]})
            else:
                await self.emit({"type": "tool_done", "stage": "rag", "title": "Passages Retrieved",
                                 "detail": f"{len(passages)} passages via {res.get('retrieval', 'hybrid search')}",
                                 "sources": list(dict.fromkeys(_short(p) for p in passages))[:4]})
            groups.append(passages)
        return _merge_passages(groups, config.MAX_CONTEXT_CHUNKS)

    # ---- generation: premium (Claude, API-native citations) ----
    async def generate_claude(self, question: str, history: list[dict], passages: list[dict],
                              correction: str | None = None) -> tuple[str, list[dict]]:
        content: list[dict] = [search_result_block(p) for p in passages]
        ask = question if passages else question + "\n\n(No knowledge-base passages were retrieved for this message.)"
        if correction:
            ask += f"\n\nA fact-check of your previous draft found these claims unsupported by the sources: {correction}\n" \
                   "Rewrite the answer so every factual claim is supported by the sources (or clearly labeled general knowledge)."
        content.append({"type": "text", "text": ask})
        messages = _anthropic_history(history) + [{"role": "user", "content": content}]
        client = haiku_async_client()
        with self.trace.span("generate", kind="llm", generator="haiku", passages=len(passages)) as span:
            try:
                async with client.messages.stream(
                    model=config.HAIKU_MODEL, max_tokens=8000, system=system_prompt("pipeline"),
                    messages=messages, output_config={"effort": "low"},
                ) as stream:
                    async for text in stream.text_stream:
                        await self.emit({"type": "reply_delta", "delta": text, "reset": False})
                    final = await stream.get_final_message()
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                raise LLMUnavailable(f"Haiku unavailable: {e}") from e
            finally:
                await client.close()
            self.trace.count_llm_call()
            span.set(model=final.model, input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens,
                     stop_reason=final.stop_reason)
        if final.stop_reason == "refusal":
            return "I can't help with that request.", []
        return render_citations(final, passages)

    # ---- generation: public (Gemini, prompt-level numbered citations) ----
    async def generate_gemini(self, question: str, history: list[dict], passages: list[dict]) -> tuple[str, list[dict]]:
        from google.genai import types
        ctx = "\n\n".join(f"[{i}] {_source_label(p)} ({p['url']})\n{p['text']}" for i, p in enumerate(passages, 1))
        contents = [types.Content(role="model" if r == "assistant" else "user", parts=[types.Part(text=t)])
                    for r, t in _history_text(history, config.HISTORY_TURNS)]
        prompt = (f"Sources:\n{ctx}\n\n" if passages else "No knowledge-base sources were retrieved.\n\n") + \
                 f"Question: {question}\n\nCite sources inline right after the claim they support, one bracket per source: [1][3]."
        contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
        text = await asyncio.to_thread(gemini_generate, contents, system=TUTOR_PRINCIPLES + "\n\n" + GROUNDING_RULES,
                                       span_name="generate")
        # models often write "[1, 2]" or "[1-3]"; normalise to "[1][2]" before numbering
        def split_group(m):
            nums = []
            for part in m.group(1).split(","):
                a, _, b = part.strip().partition("-")
                nums += range(int(a), int(b) + 1) if b else [int(a)]
            return "".join(f"[{n}]" for n in nums)
        text = re.sub(r"\[(\d+(?:\s*[-,]\s*\d+)+)\]", split_group, text)
        used = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 1 <= int(n) <= len(passages)})
        renumber = {old: new for new, old in enumerate(used, 1)}
        text = re.sub(r"\[(\d+)\]", lambda m: f"[{renumber[int(m.group(1))]}]" if int(m.group(1)) in renumber else "", text)
        return text, [{**passages[old - 1], "n": new} for old, new in renumber.items()]

    # ---- verification (premium) ----
    async def verify(self, question: str, answer: str, passages: list[dict]) -> dict:
        ctx = "\n\n".join(f"<source id={i}>\n{p['text']}\n</source>" for i, p in enumerate(passages, 1))
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["verdict", "unsupported_claims", "follow_up_query"],
                  "properties": {"verdict": {"type": "string", "enum": ["grounded", "partially_grounded", "ungrounded"]},
                                 "unsupported_claims": {"type": "array", "items": {"type": "string"}},
                                 "follow_up_query": {"type": "string"}}}
        system = ("You fact-check a tutor's answer against its sources. A claim is supported if a source states or "
                  "directly implies it. Claims explicitly labeled '(general knowledge)', code examples, and "
                  "pedagogical framing do not need support. List only substantive unsupported factual claims. "
                  "follow_up_query: a search query that could find support for the unsupported claims, or ''.")
        return await asyncio.to_thread(haiku_json, system=system, schema=schema, effort="low", span_name="verify",
                                       prompt=f"{ctx}\n\nQuestion: {question}\n\nAnswer to check:\n{answer}")


def format_reply(text: str, sources: list[dict]) -> tuple[str, list[dict]]:
    """Render [n] markers as superscripts and append a source list.

    Several cited passages often come from the same document section; they share one source entry."""
    if not sources:
        return text, []
    keys, remap, merged = {}, {}, []
    for s in sorted(sources, key=lambda s: s["n"]):
        key = (s.get("url") or s.get("chunk_id"), s.get("section", ""), s.get("timestamp"))
        if key not in keys:
            keys[key] = len(merged) + 1
            merged.append({**s, "n": keys[key]})
        remap[s["n"]] = keys[key]

    def sup(m):
        nums = list(dict.fromkeys(remap.get(int(n), int(n)) for n in re.findall(r"\d+", m.group(0))))
        return "<sup>" + "".join(f"[{n}]" for n in nums) + "</sup>"

    text = re.sub(r"(?:\[\d+\])+", sup, text)
    lines = ["", "", "---", "**Sources**"]
    for s in merged:
        label = _source_label(s)
        lines.append(f"{s['n']}. [{label}]({s['url']})" if s.get("url", "").startswith("http") else f"{s['n']}. {label}")
    return text + "\n".join(lines), merged


async def mcp_tool_defs(client) -> list[dict]:
    """Claude tool definitions discovered from the MCP server, restricted to the allow-list."""
    return [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in await client.list_tools() if t.name in guard.ALLOWED_TOOLS]


async def _run_pipeline(agent: Agent, tools: Tools, message: str, history: list[dict]):
    """Fixed pipeline: route -> gather -> generate [-> verify -> one corrective retry]."""
    trace, emit, tier = agent.trace, agent.emit, agent.tier
    with trace.span("route", kind="step") as span:
        plan = await agent.route(message, history)
        span.set(route=plan["route"], queries=plan["search_queries"])
    await emit({"type": "thought", "stage": "planning", "title": f"Plan: {plan['route'].replace('_', ' ')}",
                "detail": plan["reason"], "query": plan["standalone_question"] if plan["standalone_question"] != message else None})

    passages = await agent.gather(tools, plan) if plan["route"] != "direct" else []
    question = plan["standalone_question"] or message
    await emit({"type": "thought", "stage": "synthesizing", "title": "Writing Answer",
                "detail": f"{'Claude Haiku 5.5' if tier == 'premium' else 'Gemini'} over {len(passages)} sources"})
    verification = None
    try:
        if tier == "premium":
            text, sources = await agent.generate_claude(message, history, passages)
            if config.VERIFY_ANSWERS and passages and agent._budget_left(1):
                verification = await agent.verify(question, text, passages)
                await emit({"type": "tool_done" if verification["verdict"] == "grounded" else "thought",
                            "stage": "verify", "title": f"Fact-check: {verification['verdict'].replace('_', ' ')}",
                            "detail": "; ".join(verification["unsupported_claims"][:3]) or "All claims supported by the cited sources"})
                if verification["verdict"] != "grounded" and agent._budget_left(2):
                    extra = [verification["follow_up_query"]] if verification["follow_up_query"] else None
                    if extra:
                        more = await agent.gather(tools, {**plan, "route": "knowledge_base"}, extra_queries=extra)
                        passages = _merge_passages([passages, more], config.MAX_CONTEXT_CHUNKS + 3)
                    await emit({"type": "reply_delta", "delta": "", "reset": True})
                    text, sources = await agent.generate_claude(
                        message, history, passages, correction="; ".join(verification["unsupported_claims"]))
                    verification["retried"] = True
        else:
            text, sources = await agent.generate_gemini(message, history, passages)
    except LLMUnavailable as e:
        await emit({"type": "tool_error", "stage": "synthesizing", "title": "Model Unavailable",
                    "detail": str(e)[:200]})
        text = sources = None
        if tier == "premium":  # paid tier: degrade to the free model before giving up
            try:
                await emit({"type": "reply_delta", "delta": "", "reset": True})
                text, sources = await agent.generate_gemini(message, history, passages)
            except LLMUnavailable:
                pass
        if text is None:
            if not passages:
                raise
            text = ("**The language model is unavailable right now** (the free tier's daily quota may be used up), "
                    "so here are the most relevant knowledge-base passages instead.\n\n" +
                    "\n\n".join(f"**Passage {i}** ({_source_label(p)})\n\n> {p['text'][:600].strip()}…"
                               for i, p in enumerate(passages[:3], 1)))
            sources = [{**p, "n": i} for i, p in enumerate(passages[:3], 1)]
    return text, sources, passages, verification, plan["route"]


_LOOP_UI = {
    "search_knowledge_base": lambda a: {"stage": "rag", "title": "Searching Knowledge Base", "tool": "MCP · search_knowledge_base",
                                        "query": a.get("query"), "detail": _retrieval_label()},
    "analyze_youtube_video": lambda a: {"stage": "youtube", "title": "Loading YouTube Video", "tool": "MCP · analyze_youtube_video",
                                        "query": a.get("video_url"), "detail": "Stored copy → captions → Gemini video understanding"},
    "get_document": lambda a: {"stage": "rag", "title": "Reading Document", "tool": "MCP · get_document",
                               "query": a.get("doc_id"), "detail": "Fetching stored passages of one document"},
}


async def _run_loop(agent: Agent, client, tools: Tools, message: str, history: list[dict]):
    """Tool-use loop: Claude chooses which MCP tools to call, in parallel, within a step budget."""
    trace, emit = agent.trace, agent.emit
    tool_defs = await mcp_tool_defs(client)
    await emit({"type": "thought", "stage": "planning", "title": "Plan: agent loop",
                "detail": f"Claude Haiku 5.5 chooses tools ({len(tool_defs)} available, at most {config.AGENT_MAX_STEPS} turns)"})
    user_videos = guard.user_video_ids(message, history)

    def check_call(name: str, args: dict, seen_doc_ids: set[str]) -> str | None:
        if not config.INJECTION_DEFENSE:
            return None if name in guard.ALLOWED_TOOLS else f"unknown tool: {name}"
        return guard.allowed_tool_call(name, args, user_video_ids=user_videos, seen_doc_ids=seen_doc_ids)

    async def call_tool(name: str, args: dict) -> dict:
        timeout = config.VIDEO_TOOL_TIMEOUT_S if name == "analyze_youtube_video" else config.TOOL_TIMEOUT_S
        res = await tools.call(name, args, timeout=timeout, ui=_LOOP_UI[name](args))
        if res is None:
            return {"error": "tool failed or timed out"}
        passages = res.get("passages") or res.get("result") or []  # get_document returns a bare list
        await emit({"type": "tool_done", "stage": _LOOP_UI[name](args)["stage"], "title": "Passages Retrieved",
                    "detail": f"{len(passages)} passages from {name}",
                    "sources": list(dict.fromkeys(_short(p) for p in passages))[:4]})
        return {"passages": passages}

    messages = _anthropic_history(history) + [{"role": "user", "content": message}]
    res = await run_tool_loop(messages, system=system_prompt("loop"), tool_defs=tool_defs, call_tool=call_tool,
                              check_call=check_call, trace=trace, emit=emit, max_steps=config.AGENT_MAX_STEPS)
    refused = sum(c["refused"] for c in res.tool_calls)
    await emit({"type": "thought", "stage": "synthesizing", "title": "Answer complete",
                "detail": f"{res.steps} model turns, {len(res.tool_calls)} tool calls"
                          + (f", {refused} refused by the guard" if refused else "") + f" ({res.stop})"})
    return res.text, res.sources, res.passages, None, "agent_loop"


async def run_agent(message: str, history: list[dict], tier: str, trace: Trace, emit: Emit) -> AgentResult:
    agent = Agent(tier, trace, emit)
    cacheable = not history and not find_video_urls(message)

    if cacheable:
        hit = await asyncio.to_thread(cache.lookup, message, tier)
        if hit:
            await emit({"type": "tool_done", "stage": "cache", "title": "Semantic Cache Hit",
                        "detail": f"Similar question answered before (similarity {hit['similarity']:.3f}), no LLM call"})
            return AgentResult(reply=hit["reply"], tier=tier, route=hit.get("route", "cached"), cached=True,
                               sources=hit.get("sources", []))

    async with Client(mcp) as client:
        tools = Tools(client, trace, emit)
        if tier == "premium" and config.AGENT_MODE == "loop":
            try:
                text, sources, passages, verification, route = await _run_loop(agent, client, tools, message, history)
            except LLMUnavailable as e:
                await emit({"type": "tool_error", "stage": "planning", "title": "Agent loop unavailable",
                            "detail": f"{str(e)[:160]} (falling back to the fixed pipeline)"})
                await emit({"type": "reply_delta", "delta": "", "reset": True})
                text, sources, passages, verification, route = await _run_pipeline(agent, tools, message, history)
        else:
            text, sources, passages, verification, route = await _run_pipeline(agent, tools, message, history)

    text = finalize_text(text, passages, message)
    reply, merged = format_reply(text, sources)
    slim_sources = [{k: s.get(k) for k in ("n", "title", "section", "url", "source_type", "timestamp", "chunk_id", "doc_id")} for s in merged]
    result = AgentResult(reply=reply, tier=tier, route=route, sources=slim_sources, verification=verification,
                         context_ids=list(dict.fromkeys(p["chunk_id"] for p in passages)))
    if cacheable and route != "direct" and sources and (verification is None or verification["verdict"] != "ungrounded"):
        await asyncio.to_thread(cache.store, message, tier, {"reply": reply, "sources": slim_sources, "route": route})
    return result
