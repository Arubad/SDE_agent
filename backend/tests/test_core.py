"""Offline unit tests (no network, no LLM, no Qdrant):  python -m pytest backend/tests -q"""
import pytest

from backend.agent import access
from backend.agent.orchestrator import Agent, _merge_passages, format_reply
from backend.core import config
from backend.core.trace import Trace
from backend.eval.common import contains_evidence
from backend.rag.documents import Document, _markdown_sections, chunk_documents, clean_documents
from backend.tools.youtube import VideoUnavailable, extract_video_id, find_video_urls, section_timestamp


# --- eval matching -------------------------------------------------------------------------

def test_evidence_exact_and_whitespace_insensitive():
    chunk = "Evolving a system in place is usually\ncheaper than replacing it with a new one."
    assert contains_evidence(chunk, "evolving a system in place is usually cheaper than replacing it")


def test_evidence_survives_pdf_hyphenation_and_boundary_cut():
    chunk = "A good metric is a reason-\nable proxy to the signal you're trying to measure"
    assert contains_evidence(chunk, "A good metric is a reasonable proxy to the signal you're trying to measure, and it is traceable")


def test_evidence_rejects_bag_of_words_coincidence():
    chunk = "measure the signal proxy metric good reasonable trying"  # same words, no real overlap
    assert not contains_evidence(chunk, "A good metric is a reasonable proxy to the signal you are trying to measure")


# --- documents / chunking ----------------------------------------------------------------

def test_markdown_sections_track_heading_path_and_ignore_code_fences():
    md = "# Title\nintro\n## Caching\ntext\n```bash\n# not a heading\n```\n### Write-through\nmore"
    sections = _markdown_sections(md)
    assert [s for s, _ in sections] == ["Title", "Title > Caching", "Title > Caching > Write-through"]
    assert "# not a heading" in sections[1][1]


def test_cleaning_drops_translations_and_boilerplate():
    docs = [
        Document("a", "x" * 200, "primer/README.md", "github", "u", {"file_path": "README.md"}),
        Document("b", "x" * 200, "primer/README-ja.md", "github", "u", {"file_path": "README-ja.md"}),
        Document("c", "x" * 200, "primer/.github/PR.md", "github", "u", {"file_path": ".github/PULL_REQUEST_TEMPLATE.md"}),
    ]
    kept, report = clean_documents(docs)
    assert [d.doc_id for d in kept] == ["a"]
    assert report["dropped_translations"] == 1 and report["dropped_boilerplate"] == 1


def test_chunks_carry_contextual_header_and_stable_ids():
    doc = Document("d", "# Guide\n## Sharding\n" + "Shards split data. " * 30, "primer/README.md", "github", "u")
    first, second = chunk_documents([doc]), chunk_documents([doc])
    assert first and first[0].embed_text.startswith("primer/README.md > Guide > Sharding")
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]  # re-ingest overwrites, no duplicates


def test_youtube_chunks_get_timestamps():
    doc = Document("youtube:x", "# Video\n## [02:43] Neurons\n" + "A neuron holds a number. " * 10, "YouTube: Video",
                   "youtube", "https://youtu.be/x")
    chunks = chunk_documents([doc])
    assert section_timestamp(chunks[0].section) == "02:43"


# --- youtube parsing -----------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=aircAruvnKk&t=30s", "https://youtu.be/aircAruvnKk",
    "https://www.youtube.com/shorts/aircAruvnKk", "https://www.youtube.com/embed/aircAruvnKk?x=1", "aircAruvnKk",
])
def test_extract_video_id(url):
    assert extract_video_id(url) == "aircAruvnKk"


def test_extract_video_id_rejects_garbage():
    with pytest.raises(VideoUnavailable):
        extract_video_id("https://example.com/watch")


def test_find_video_urls_dedupes():
    text = "compare https://youtu.be/aircAruvnKk and https://www.youtube.com/watch?v=aircAruvnKk"
    assert find_video_urls(text) == ["https://www.youtube.com/watch?v=aircAruvnKk"]


# --- citations ---------------------------------------------------------------------------

def test_format_reply_merges_duplicate_sources_and_renumbers():
    sources = [{"n": 1, "url": "http://a", "title": "A", "section": "s"},
               {"n": 2, "url": "http://b", "title": "B"}, {"n": 3, "url": "http://b", "title": "B"}]
    text, merged = format_reply("x[1] y[2][3] z[3]", sources)
    assert "y<sup>[2]</sup>" in text and "z<sup>[2]</sup>" in text
    assert [m["n"] for m in merged] == [1, 2]
    assert "1. [A › s](http://a)" in text


def test_format_reply_without_sources_is_unchanged():
    assert format_reply("plain answer", []) == ("plain answer", [])


def test_merge_passages_interleaves_and_dedupes():
    a = [{"chunk_id": "1"}, {"chunk_id": "2"}, {"chunk_id": "3"}]
    b = [{"chunk_id": "2"}, {"chunk_id": "4"}]
    assert [p["chunk_id"] for p in _merge_passages([a, b], 10)] == ["1", "2", "4", "3"]


# --- routing -------------------------------------------------------------------------------

async def _noop(_):
    return None


def test_rule_router_routes_videos_small_talk_and_followups():
    agent = Agent("public", Trace(), _noop)
    assert agent._route_rules("hi!", [], [])["route"] == "direct"
    assert agent._route_rules("summarize", [], ["https://youtu.be/x"])["route"] == "youtube"
    hist = [{"role": "user", "parts": [{"text": "How does consistent hashing work?"}]},
            {"role": "model", "parts": [{"text": "It uses a ring."}]}]
    plan = agent._route_rules("and its failure modes?", hist, [])
    assert plan["route"] == "knowledge_base" and "consistent hashing" in plan["search_queries"][0]


# --- access control ---------------------------------------------------------------------

def test_passcode_check_and_bruteforce_lockout(monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "s3cret")
    monkeypatch.setattr(config, "PASSCODE_MAX_FAILURES", 3)
    access._failures.clear()
    assert access.check_passcode("s3cret", "1.1.1.1")
    assert not access.check_passcode("nope", "2.2.2.2")
    access.check_passcode("nope", "2.2.2.2")
    access.check_passcode("nope", "2.2.2.2")
    with pytest.raises(access.TooManyAttempts):
        access.check_passcode("s3cret", "2.2.2.2")  # locked out even with the right code
    assert access.check_passcode("s3cret", "1.1.1.1")  # other IPs unaffected


def test_premium_disabled_without_passcode(monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "")
    assert not access.check_passcode("anything", "3.3.3.3")
