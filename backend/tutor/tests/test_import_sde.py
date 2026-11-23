import pytest
from sqlalchemy import func, select, text

from backend.rag.documents import Document as DemoDoc
from backend.tutor.db import session_scope
from backend.tutor.eval import sde_retrieval
from backend.tutor.ingest import import_sde as imp
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.index import PlatformIndex
from backend.tutor.models import Document, Resource, Roadmap, Section
from backend.tutor.roadmap.spec import RoadmapSpec

EMB = HashEmbedder()


def _body(topic: str) -> str:
    return (f"# {topic}\n\n" + " ".join(f"The {topic} guide explains point {i} about {topic} systems in detail."
                                        for i in range(12))
            + f"\n\n## Details of {topic}\n\n" + " ".join(f"{topic} detail {i} covers a distinct {topic} concern."
                                                          for i in range(12)))


def _docs():
    kinds = ["github", "book", "web", "youtube", "github"]
    return [DemoDoc(doc_id=f"demo-{i}", text=_body(f"topic{i}"), title=f"Doc {i}", source_type=kinds[i],
                    url=("https://example.com/%d" % i) if kinds[i] != "book" else "book.pdf", meta={})
            for i in range(5)]


@pytest.fixture
def clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))


@pytest.fixture
def index():
    from qdrant_client import QdrantClient
    return PlatformIndex(QdrantClient(":memory:"), "test_sde", EMB)


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setattr(imp, "reconstruct_documents", lambda *a, **k: _docs())


def _counts(engine):
    with session_scope(engine) as s:
        return (s.scalar(select(func.count()).select_from(Roadmap)), s.scalar(select(func.count()).select_from(Resource)),
                s.scalar(select(func.count()).select_from(Document)), s.scalar(select(func.count()).select_from(Section)))


def test_import_sde_small(engine, clean, index, demo):
    out = imp.import_sde(engine, index, limit=5)
    assert out["documents"] == 5 and out["resources"] == 5 and out["stages"] == 4 and out["children"] > 0
    with session_scope(engine) as s:
        rm = s.scalar(select(Roadmap).where(Roadmap.slug == "sde-mastery"))
        assert rm.status == "ready"
        spec = RoadmapSpec.model_validate(rm.spec)
        assert {st.id for st in spec.stages} == {"github", "book", "web", "youtube"}
        assert all(len(st.skills) == 1 for st in spec.stages)
        assert sum(len(st.resources) for st in spec.stages) == 5
        assert s.scalar(select(func.count()).select_from(Resource).where(Resource.status == "done")) == 5
    first = _counts(engine)
    n_points = index.client.count(index.collection, exact=True).count
    again = imp.import_sde(engine, index, limit=5)
    assert _counts(engine) == first and again["children"] == out["children"]
    assert index.client.count(index.collection, exact=True).count == n_points
    assert imp.import_sde(engine, index, limit=3)["documents"] == 3  # shrinking drops the rest
    assert _counts(engine)[1:3] == (3, 3)
    assert index.client.count(index.collection, exact=True).count < n_points


def test_score_hits_child_vs_parent():
    from types import SimpleNamespace as NS
    hits = [NS(text="irrelevant words here", payload={"parent_id": "p1"}),
            NS(text="the quick brown fox jumps over the lazy dog", payload={"parent_id": "p2"})]
    parents = {"p1": "irrelevant words here plus the needle sentence about gradient descent steps is here",
               "p2": "the quick brown fox jumps over the lazy dog"}
    q = ["the needle sentence about gradient descent steps is here"]
    assert sde_retrieval.first_rank(hits, q, parents, "child") is None
    assert sde_retrieval.first_rank(hits, q, parents, "parent") == 1
    assert sde_retrieval.first_rank(hits, ["quick brown fox jumps over the lazy dog"], parents, "child") == 2
    s = sde_retrieval.summarize([1, 2, None, 6, 12])
    assert s["hit@1"] == 0.2 and s["hit@5"] == 0.4 and s["n"] == 5
    assert s["mrr@10"] == pytest.approx((1 + 0.5 + 0 + 1 / 6 + 0) / 5, abs=1e-4)


def test_score_questions_on_fixture_index(engine, clean, index, demo):
    imp.import_sde(engine, index, limit=5)
    with session_scope(engine) as s:
        rid = str(s.scalar(select(Roadmap.id).where(Roadmap.slug == "sde-mastery")))
    qs = [{"id": "q1", "question": "topic3 guide explains point 4 about topic3 systems",
           "evidence": [{"quote": "The topic3 guide explains point 4 about topic3 systems in detail."}]},
          {"id": "q2", "question": "nothing", "kind": "unanswerable", "evidence": []}]
    res = sde_retrieval.score_questions(engine, index, rid, qs)
    assert res["child"]["n"] == res["parent"]["n"] == 1 and res["skipped_no_evidence"] == 1
    assert res["child"]["hit@5"] == 1.0 and res["parent"]["hit@5"] >= res["child"]["hit@5"]


def test_v2_row_parsed_from_markdown(tmp_path):
    (tmp_path / "retrieval_dev_latest.md").write_text(
        "Split: **dev** (n=12)\n\n| config | hit@1 | hit@5 | MRR@10 | doc hit@5 | p50 latency |\n|---|---|---|---|---|---|\n"
        "| v2_dense | 0.10 | 0.20 | 0.30 | 0.40 | 1 ms |\n| v2_hybrid | 0.51 | 0.62 | 0.73 | 0.84 | 2 ms |\n")
    assert sde_retrieval.v2_row("dev", tmp_path) == {"n": 12, "hit@1": 0.51, "hit@5": 0.62, "mrr@10": 0.73, "doc_hit@5": 0.84}
    assert sde_retrieval.v2_row("nope", tmp_path) is None
    real = sde_retrieval.v2_row("test")  # the demo's real file: shape and ranges only
    assert real is None or (0 <= real["hit@5"] <= 1 and real["n"] > 0)


def test_single_evidence_quote_questions_scored(engine, clean, index, demo):
    imp.import_sde(engine, index, limit=5)
    with session_scope(engine) as s:
        rid = str(s.scalar(select(Roadmap.id).where(Roadmap.slug == "sde-mastery")))
    q = [{"id": "d1", "question": "topic2 guide explains point 3 about topic2 systems",
          "evidence_quote": "The topic2 guide explains point 3 about topic2 systems in detail."}]
    res = sde_retrieval.score_questions(engine, index, rid, q)
    assert res["child"]["n"] == 1 and res["child"]["hit@5"] == 1.0


def test_index_document_dedupe_flag(engine, clean, index):
    from backend.tutor.ingest.index import index_document
    from backend.tutor.tests.test_index import BODY, _doc
    a, b = _doc(engine, "dd", md=BODY, url="https://x.dev/a"), None
    with session_scope(engine) as s:
        b = Document(roadmap_id=a.roadmap_id, url="https://x.dev/b", kind="page", title="B", markdown=BODY, meta={})
        s.add(b)
        s.flush()
        s.expunge_all()
    n1 = index_document(engine, index, a, [])
    assert index_document(engine, index, b, []) == 0  # identical body: dropped as a near-duplicate
    assert index_document(engine, index, b, [], dedupe=False) == n1
