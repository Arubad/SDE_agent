"""SDE Knowledge MCP server: the single tool layer for the web agent AND external MCP clients.

- The FastAPI chat agent calls these tools through an in-memory MCP client (same code path).
- The server is also exposed over Streamable HTTP at /mcp on the Space (passcode-protected bearer
  token), and over stdio for local IDEs:  python -m backend.mcp_server
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

if __package__ in (None, ""):  # allow `python backend/mcp_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anyio
from fastmcp import FastMCP
from pydantic import BaseModel, Field

from backend.core import config
from backend.rag import store
from backend.tools import youtube

SourceType = Literal["github", "book", "web", "youtube"]

mcp = FastMCP(
    name="SDE_Knowledge_Agent",
    instructions=(
        "Software engineering knowledge base: System Design Primer, Software Engineering at Google, "
        "MIT Missing Semester, Google eng-practices, plus any YouTube videos ingested by users. "
        "Use search_knowledge_base for grounded answers and cite the returned sources."
    ),
)


class Passage(BaseModel):
    chunk_id: str
    doc_id: str = ""
    title: str
    section: str = ""
    source_type: str
    url: str
    score: float
    text: str
    timestamp: str | None = None


class SearchResult(BaseModel):
    query: str
    retrieval: str = Field(description="retrieval pipeline used, e.g. hybrid(dense+bm25, RRF) + bge-reranker")
    passages: list[Passage]


class VideoResult(BaseModel):
    video_id: str
    url: str
    title: str
    method: Literal["knowledge_base", "transcript", "gemini_video"]
    newly_ingested: bool
    chunk_count: int
    passages: list[Passage]


def _passage(d: dict) -> Passage:
    meta = d.get("meta") or {}
    return Passage(chunk_id=d["chunk_id"], doc_id=d.get("doc_id", ""), title=d.get("title", ""), section=d.get("section", ""),
                   source_type=d.get("source_type", ""), url=d.get("url", ""), score=float(d.get("score", 0.0)),
                   text=d.get("text", ""), timestamp=meta.get("timestamp"))


def _retrieval_label() -> str:
    mode = config.RETRIEVAL_MODE
    label = {"dense": "dense (bge-small)", "sparse": "bm25", "hybrid": "hybrid(dense+bm25, RRF)",
             "hybrid_rerank": "hybrid(dense+bm25, RRF)"}.get(mode, mode)
    return label + (f" + {config.RERANKER_MODEL.split('/')[-1]} rerank" if mode.endswith("_rerank") else "")


@mcp.tool
async def search_knowledge_base(
    query: str,
    source_types: list[SourceType] | None = None,
    top_k: int = Field(default=5, ge=1, le=10),
    within_document: str | None = Field(default=None, description="restrict to one doc_id, e.g. 'youtube:<id>'"),
) -> SearchResult:
    """Hybrid semantic + keyword search over the curated software engineering knowledge base.

    Returns the most relevant passages with title, section, source URL and relevance score.
    Optionally restrict to source types (github, book, web, youtube) or a single document."""
    hits = await anyio.to_thread.run_sync(
        lambda: store.search(query, top_k=top_k, source_types=source_types, doc_id=within_document))
    return SearchResult(query=query, retrieval=_retrieval_label(), passages=[_passage(h.to_dict()) for h in hits])


@mcp.tool
async def analyze_youtube_video(video_url: str, focus: str = "") -> VideoResult:
    """Load the content of a public YouTube video and add it to the knowledge base.

    Uses the stored copy if the video was ingested before, otherwise captions, otherwise Gemini watches
    the video. Returns the passages most relevant to `focus` (or the opening passages if empty)."""
    try:
        video = await anyio.to_thread.run_sync(lambda: youtube.load_video(video_url))
    except youtube.VideoUnavailable as e:
        raise ValueError(f"Video could not be loaded: {e}") from e
    chunks = video["chunks"]
    if focus and len(chunks) > 6:
        hits = await anyio.to_thread.run_sync(
            lambda: store.search(focus, top_k=6, doc_id=f"youtube:{video['video_id']}"))
        selected = [h.to_dict() for h in hits]
    else:
        selected = chunks[:6]
    return VideoResult(video_id=video["video_id"], url=video["url"], title=video["title"], method=video["method"],
                       newly_ingested=video["newly_ingested"], chunk_count=len(chunks),
                       passages=[_passage({**c, "score": c.get("score", 1.0)}) for c in selected])


@mcp.tool
async def get_document(doc_id: str, max_passages: int = Field(default=20, ge=1, le=100)) -> list[Passage]:
    """Fetch the stored passages of one document (e.g. 'youtube:<id>' or a doc_id from search results)."""
    chunks = await anyio.to_thread.run_sync(lambda: store.document_chunks(doc_id, limit=max_passages))
    return [_passage(c) for c in chunks]


# --- Resources --------------------------------------------------------------------------

@mcp.resource("kb://stats", mime_type="application/json")
def kb_stats() -> dict:
    """Chunk counts per source type in the live collection."""
    return store.collection_stats()


@mcp.resource("kb://sources", mime_type="application/json")
def kb_sources() -> list[dict]:
    """Every document in the knowledge base (title, url, source type, doc_id)."""
    return store.list_documents(limit=1000)


@mcp.resource("kb://sources/{source_type}", mime_type="application/json")
def kb_sources_by_type(source_type: str) -> list[dict]:
    """Documents of one source type: github, book, web or youtube."""
    return store.list_documents(source_type=source_type, limit=1000)


@mcp.resource("kb://eval/retrieval", mime_type="text/markdown")
def retrieval_eval_report() -> str:
    """Latest retrieval evaluation table (hit@k / MRR per retrieval configuration)."""
    path = config.BACKEND_DIR / "eval" / "results" / "retrieval_test_latest.md"
    return path.read_text() if path.exists() else "No evaluation has been run yet."


@mcp.resource("kb://eval/agent", mime_type="text/markdown")
def agent_eval_report() -> str:
    """Latest agent evaluation (pipeline vs tool-use loop on multi-hop and unanswerable questions)."""
    path = config.BACKEND_DIR / "eval" / "results" / "agent_v2_test_latest.md"
    return path.read_text() if path.exists() else "No agent evaluation has been run yet."


# --- Prompts ----------------------------------------------------------------------------

@mcp.prompt
def system_design_interview(problem: str) -> str:
    """Run a mock system design interview for a problem (e.g. 'design a URL shortener')."""
    return (f"Act as a senior interviewer running a 45-minute system design interview: {problem}.\n"
            "Go step by step: clarify requirements and scale, estimate back-of-the-envelope numbers, propose a "
            "high-level design, then deep-dive into the bottleneck components and trade-offs. Ground claims in "
            "the knowledge base with search_knowledge_base and cite sources. Ask me one question at a time.")


@mcp.prompt
def explain_concept(concept: str, level: Literal["beginner", "intermediate", "senior"] = "intermediate") -> str:
    """Explain an engineering concept at a chosen depth, grounded in the knowledge base."""
    return (f"Explain '{concept}' for a {level} engineer. Search the knowledge base first, cite sources, give one "
            "concrete example, the main trade-offs, and a common misconception.")


@mcp.prompt
def code_review(code: str, language: str = "C++") -> str:
    """Review code the way Google's eng-practices guide recommends."""
    return (f"Review this {language} code using Google's code review standards (search the knowledge base for "
            "'code review standard' and cite it). Cover design, functionality, complexity, tests, naming, "
            f"comments, and style, most important issues first.\n\n```{language.lower()}\n{code}\n```")


if __name__ == "__main__":
    mcp.run()  # stdio transport for Claude Desktop / Cursor
