"""Ingestion-time section summaries: batching, validation, budget, the disabled switch, retries and the report."""
import uuid

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.core import fake_llm
from backend.core.fake_llm import FakeReply
from backend.core.llm import LLMUnavailable
from backend.tutor.db import session_scope
from backend.tutor.ingest import handlers as H
from backend.tutor.ingest import summaries
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.index import PlatformIndex
from backend.tutor.ingest.report import build_report
from backend.tutor.ingest.summaries import BATCH, SectionNotes, summarize_document
from backend.tutor.jobs import queue
from backend.tutor.jobs.queue import RetryableError
from backend.tutor.jobs.worker import JobContext
from backend.tutor.models import Document, Job, Roadmap, Section
from backend.tutor.repo import create_roadmap
from backend.tutor.settings import Settings

MCQ = {"type": "mcq", "prompt": "Which?", "options": ["a", "b"], "answer": "a", "explanation": "e", "concept": "c"}
SHORT = {"type": "short", "prompt": "Why?", "options": [], "answer": "because", "explanation": "e", "concept": "c"}


@pytest.fixture(autouse=True)
def _clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))


def _ctx(engine, **kw):
    return JobContext(engine=engine, settings=Settings(qdrant_url=":memory:", embedder="hash", **kw),
                      index=PlatformIndex(QdrantClient(":memory:"), "sum_chunks", HashEmbedder()))


def _doc(engine, n=10, kind="page", slug="sum"):
    """-> (document, section ids in order)."""
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=slug, title=slug, source_text="x")
        d = Document(roadmap_id=rm.id, url=f"https://x.dev/{slug}", kind=kind, title="Doc", markdown="m", meta={})
        s.add(d)
        s.flush()
        ids = [f"{d.id}:{i}" for i in range(n)]
        s.add_all([Section(id=sid, roadmap_id=rm.id, document_id=d.id, path=f"A > S{i}", text=f"text {i}", ord=i)
                   for i, sid in enumerate(ids)])
        s.flush()
        s.expunge_all()
        return d, ids


def _job(doc, id_=1):
    return Job(id=id_, roadmap_id=doc.roadmap_id, type="summarize_document", key=f"k{id_}", attempts=1,
               payload={"document_id": str(doc.id)})


def _section(engine, sid):
    with Session(engine) as s:
        return s.get(Section, sid)


def _reply(ids, card=None, questions=(MCQ, SHORT)):
    return FakeReply(json={"card": card, "sections": [
        {"id": i, "summary": f"S {i}", "questions": list(questions)} for i in ids]})


def test_schema_is_strict_and_titled():
    schema = SectionNotes.json_schema()
    assert schema["title"] == "SectionNotes"
    for node in (schema, *schema["$defs"].values()):
        assert node["additionalProperties"] is False and node["required"] == list(node["properties"])


def test_summaries_stored_per_section_in_batches(engine):
    doc, ids = _doc(engine)
    with fake_llm.script([_reply(ids[:BATCH]), _reply(ids[BATCH:])]):
        out = summarize_document(_ctx(engine), _job(doc))
    assert out["sections"] == 10 and out["cost_usd"] == 0.0 and len(fake_llm.requests) == 2
    assert _section(engine, ids[0]).summary == f"S {ids[0]}" and len(_section(engine, ids[0]).quiz) == 2
    assert _section(engine, ids[9]).summary == f"S {ids[9]}"
    prompt = fake_llm.requests[0]["messages"][0]["content"]
    assert "<untrusted_web_content>text 0</untrusted_web_content>" in prompt and "A > S0" in prompt
    assert "text 8" not in prompt and "text 8" in fake_llm.requests[1]["messages"][0]["content"]


def test_unknown_missing_ids_and_bad_questions(engine):
    doc, ids = _doc(engine, 3)
    bad = [{**MCQ, "options": ["a"]}, {**MCQ, "answer": "z"}, {**SHORT, "answer": " "}, {**SHORT, "type": "essay"}]
    reply = FakeReply(json={"card": None, "sections": [
        {"id": "nope", "summary": "x", "questions": []},
        {"id": ids[0], "summary": "one", "questions": [*bad, MCQ, SHORT, MCQ]},
        {"id": ids[1], "summary": "two", "questions": [MCQ]}]})
    with fake_llm.script([reply]):
        out = summarize_document(_ctx(engine), _job(doc))
    assert out["sections"] == 2
    assert _section(engine, ids[0]).quiz == [MCQ, SHORT]  # invalid dropped, at most 2 kept
    assert _section(engine, ids[1]).quiz == [MCQ]
    assert _section(engine, ids[2]).summary is None and _section(engine, ids[2]).quiz is None
    assert _section(engine, "nope") is None


def test_card_only_for_video(engine):
    vdoc, vids = _doc(engine, 2, kind="video", slug="vid")
    pdoc, pids = _doc(engine, 2, slug="pg")
    with fake_llm.script([_reply(vids, card="A video about X."), _reply(pids, card="ignored")]):
        summarize_document(_ctx(engine), _job(vdoc))
        summarize_document(_ctx(engine), _job(pdoc, 2))
    assert "Also write the card" in fake_llm.requests[0]["messages"][0]["content"]
    assert "Set card to null" in fake_llm.requests[1]["messages"][0]["content"]
    with Session(engine) as s:
        assert s.get(Document, vdoc.id).summary == "A video about X." and s.get(Document, pdoc.id).summary is None


def test_budget_checked_before_each_batch(engine, monkeypatch):
    doc, ids = _doc(engine)
    spend = iter([0.0, 0.51])
    monkeypatch.setattr(summaries, "summary_spend", lambda *a: next(spend))
    with fake_llm.script([_reply(ids[:BATCH])]):
        out = summarize_document(_ctx(engine), _job(doc))
    assert out == {"sections": BATCH, "skipped": "budget", "cost_usd": 0.0} and len(fake_llm.requests) == 1
    assert _section(engine, ids[BATCH]).summary is None


def test_budget_cap_skips_without_a_call(engine, monkeypatch):
    doc, _ = _doc(engine)
    monkeypatch.setattr(summaries, "summary_spend", lambda *a: 0.51)
    out = summarize_document(_ctx(engine), _job(doc))
    assert out["skipped"] == "budget" and out["sections"] == 0 and fake_llm.requests == []


def test_summary_spend_sums_job_results(engine):
    doc, _ = _doc(engine)
    with session_scope(engine) as s:
        for i, (typ, cost) in enumerate([("summarize_document", 0.1), ("summarize_document", 0.25),
                                         ("chunk_embed", 9.0)]):
            queue.enqueue(s, type=typ, roadmap_id=doc.roadmap_id, payload={}, key=f"sp{i}")
        for j in s.scalars(select(Job).where(Job.roadmap_id == doc.roadmap_id)):
            j.result = {"cost_usd": {"summarize_document": 0.1, "chunk_embed": 9.0}[j.type]}
        s.flush()
        first = s.scalars(select(Job).where(Job.key == "sp1")).one()
        first.result = {"cost_usd": 0.25}
    assert summaries.summary_spend(engine, doc.roadmap_id) == pytest.approx(0.35)
    assert summaries.summary_spend(engine, uuid.uuid4()) == 0.0


def test_disabled_setting_skips(engine):
    doc, ids = _doc(engine)
    out = summarize_document(_ctx(engine, ingest_summaries=False), _job(doc))
    assert out == {"skipped": "disabled", "cost_usd": 0.0} and fake_llm.requests == []
    assert _section(engine, ids[0]).summary is None


def test_document_without_sections(engine):
    doc, _ = _doc(engine, 0)
    assert summarize_document(_ctx(engine), _job(doc))["skipped"] == "no_sections"


def test_model_unavailable_is_retryable(engine, monkeypatch):
    doc, _ = _doc(engine, 2)

    def down(**kw):
        raise LLMUnavailable("down")

    monkeypatch.setattr(summaries, "haiku_json", down)
    with pytest.raises(RetryableError):
        summarize_document(_ctx(engine), _job(doc))


def test_chunk_embed_enqueues_summaries_once_indexed(engine):
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug="ce", title="ce", source_text="x")
        from backend.tutor.models import Resource
        res = Resource(roadmap_id=rm.id, key="r", stage_id="s1", title="R", kind="doc", url="https://x.dev/ce", skill_ids=[])
        s.add(res)
        s.flush()
        body = "# T\n\n" + " ".join(f"Sentence {i} explains how services route traffic." for i in range(40))
        d = Document(roadmap_id=rm.id, resource_id=res.id, url="https://x.dev/ce", kind="page", title="D",
                     markdown=body, meta={})
        empty = Document(roadmap_id=rm.id, resource_id=res.id, url="https://x.dev/empty", kind="page", title="E",
                         markdown="", meta={})
        s.add_all([d, empty])
        s.flush()
        rid, did, eid = rm.id, d.id, empty.id
    ctx = _ctx(engine, ingest_summaries=False)  # the job is queued even when the setting is off
    for doc_id in (did, eid):
        parent = ctx.enqueue("chunk_embed", {"document_id": str(doc_id)}, (doc_id,), roadmap_id=rid)
        H.chunk_embed(ctx, parent)
    with Session(engine) as s:
        jobs = list(s.scalars(select(Job).where(Job.type == "summarize_document")))
    assert [j.payload for j in jobs] == [{"document_id": str(did)}]
    assert jobs[0].key == queue.job_key("summarize_document", rid, "summarize_document", did)


def test_report_counts_summaries(engine):
    doc, ids = _doc(engine, 4)
    with session_scope(engine) as s:
        for sid in ids[:3]:
            s.get(Section, sid).summary = "x"
        s.flush()
    with Session(engine) as s:
        rm = s.get(Roadmap, doc.roadmap_id)
        assert build_report(s, rm)["summaries"] == {"sections": 3, "of": 4, "skipped_budget": False}
    with session_scope(engine) as s:
        queue.enqueue(s, type="summarize_document", roadmap_id=doc.roadmap_id, payload={}, key="b")
        s.scalars(select(Job).where(Job.key == "b")).one().result = {"skipped": "budget", "cost_usd": 0.5}
    with Session(engine) as s:
        assert build_report(s, s.get(Roadmap, doc.roadmap_id))["summaries"]["skipped_budget"] is True


def test_retry_after_partial_batch_pays_only_for_the_rest(engine, monkeypatch):
    from backend.core.trace import start_trace

    doc, ids = _doc(engine)
    ctx = _ctx(engine)
    job = ctx.enqueue("summarize_document", {"document_id": str(doc.id)}, (doc.id,), roadmap_id=doc.roadmap_id)
    # the fake model has no price: charge 0.1 per completed model call
    monkeypatch.setattr(summaries, "_cost", lambda trace: 0.1 * sum(1 for sp in trace.spans if sp.status == "ok"))
    start_trace("attempt1")
    with fake_llm.script([_reply(ids[:BATCH]), FakeReply(stop_reason="refusal")]):
        with pytest.raises(RetryableError):
            summarize_document(ctx, job)
    spent = summaries.summary_spend(engine, doc.roadmap_id)
    assert spent == pytest.approx(0.1)  # the failed attempt's first batch is counted though the job has no result
    assert _section(engine, ids[0]).summary and _section(engine, ids[BATCH]).summary is None
    fake_llm.requests.clear()
    start_trace("attempt2")
    with fake_llm.script([_reply(ids[BATCH:])]):
        out = summarize_document(ctx, job)
    assert out["sections"] == 2 and len(fake_llm.requests) == 1
    prompt = fake_llm.requests[0]["messages"][0]["content"]
    assert f"Section id: {ids[BATCH]}" in prompt and f"Section id: {ids[0]}" not in prompt
    assert _section(engine, ids[9]).summary == f"S {ids[9]}"
    assert out["cost_usd"] == pytest.approx(0.1)  # this attempt only; the worker stores it as the result
    assert summaries.summary_spend(engine, doc.roadmap_id) == pytest.approx(0.1)  # attempt 1's, kept in the payload
