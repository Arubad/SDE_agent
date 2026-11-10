"""YouTube tools: load a video's content and embed it into the live knowledge base.

Order of attempts for a video:
  1. Already ingested?  -> return the stored chunks (no network, no LLM)
  2. Captions via youtube-transcript-api (exact, with timestamps; blocked from most cloud IPs)
  3. Gemini native video understanding (watches the public video; costs one Gemini call)
Whatever was obtained is chunked, embedded and upserted into Qdrant, so follow-up questions are
answered through normal RAG retrieval and survive restarts.
"""
from __future__ import annotations

import re

from backend.core import config
from backend.core.llm import LLMUnavailable, gemini_generate
from backend.core.trace import current_trace
from backend.rag import store
from backend.rag.documents import Document, chunk_documents

_ID_RE = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})")

VIDEO_PROMPT = (
    "Watch this YouTube video and write a detailed, faithful account of it for a software engineering "
    "tutor's knowledge base. Use markdown with one '## [MM:SS] <topic>' heading per segment, in order. "
    "Under each heading cover the key technical claims, definitions, examples, and any code or diagrams "
    "shown on screen. Only report what is actually in the video. Start with a line '# <video title>'."
)


class VideoUnavailable(RuntimeError):
    pass


def extract_video_id(value: str) -> str:
    value = value.strip()
    m = _ID_RE.search(value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    raise VideoUnavailable(f"Not a YouTube URL or video id: {value!r}")


def find_video_urls(text: str) -> list[str]:
    ids = dict.fromkeys(m.group(1) for m in _ID_RE.finditer(text))
    return [f"https://www.youtube.com/watch?v={i}" for i in ids]


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _fetch_transcript(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        segments = [(s["start"], s["text"]) for s in YouTubeTranscriptApi.get_transcript(video_id)]
    else:
        segments = [(s.start, s.text) for s in YouTubeTranscriptApi().fetch(video_id)]
    # Group into ~60s sections with timestamp headings, so chunks carry their position in the video
    lines, bucket_start, buf = [], None, []
    for start, text in segments:
        if bucket_start is None:
            bucket_start = start
        buf.append(text.replace("\n", " "))
        if start - bucket_start >= 60:
            lines.append(f"## [{_mmss(bucket_start)}]\n{' '.join(buf)}")
            bucket_start, buf = None, []
    if buf:
        lines.append(f"## [{_mmss(bucket_start or 0)}]\n{' '.join(buf)}")
    return "\n\n".join(lines)


def section_timestamp(section: str) -> str | None:
    """Timestamp of the innermost heading, e.g. 'Title > 02:43 Neurons' -> '02:43'."""
    m = re.match(r"\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?", section.split(" > ")[-1]) if section else None
    return m.group(1) if m else None


def _doc_id(video_id: str) -> str:
    return f"youtube:{video_id}"


def load_video(video_url: str, *, allow_gemini: bool = True) -> dict:
    """Return {video_id, url, method, title, chunks, newly_ingested}. Raises VideoUnavailable."""
    video_id = extract_video_id(video_url)
    url = f"https://www.youtube.com/watch?v={video_id}"
    trace = current_trace()

    with trace.span("youtube_lookup", kind="tool", video_id=video_id) as span:
        if store.has_document(_doc_id(video_id)):
            chunks = store.document_chunks(_doc_id(video_id))
            span.set(method="knowledge_base", chunks=len(chunks))
            return {"video_id": video_id, "url": url, "method": "knowledge_base",
                    "title": chunks[0]["title"] if chunks else url, "chunks": chunks, "newly_ingested": False}

    errors = []
    text, method = None, None
    with trace.span("youtube_transcript", kind="tool", video_id=video_id) as span:
        try:
            text, method = _fetch_transcript(video_id), "transcript"
        except Exception as e:  # IP blocks, disabled captions, private videos...
            errors.append(f"captions: {str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__}")
            span.set(failed=errors[-1][:200])

    if text is None and allow_gemini:
        from google.genai import types
        try:
            text = gemini_generate(
                types.Content(parts=[types.Part(file_data=types.FileData(file_uri=url)), types.Part(text=VIDEO_PROMPT)]),
                video=True, span_name="gemini_watch_video")
            method = "gemini_video"
        except Exception as e:  # LLMUnavailable (quota) or a provider error (private/unsupported video)
            errors.append(f"gemini: {str(e)[:200]}")

    if text is None:
        raise VideoUnavailable("; ".join(errors) or "no method available")

    title_match = re.match(r"#\s+(.+)", text.strip())
    title = f"YouTube: {title_match.group(1).strip()}" if title_match else f"YouTube video {video_id}"
    doc = Document(doc_id=_doc_id(video_id), text=text, title=title, source_type="youtube", url=url,
                   meta={"video_id": video_id, "method": method})
    chunks = chunk_documents([doc])
    for c in chunks:  # transcripts/analyses are markdown with "## [MM:SS] topic" headings
        c.meta["timestamp"] = section_timestamp(c.section)
    with trace.span("youtube_ingest", kind="tool", chunks=len(chunks)):
        store.upsert_chunks(config.KB_COLLECTION, chunks)
    return {"video_id": video_id, "url": url, "method": method, "title": title,
            "chunks": [{**c.payload(), "score": 1.0} for c in chunks], "newly_ingested": True}
