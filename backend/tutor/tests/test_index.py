import uuid

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import func, select, text

from backend.core import config
from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import HashEmbedder, make_embedder
from backend.tutor.ingest.index import PlatformIndex, get_parents, index_document, make_index
from backend.tutor.models import Document, Section
from backend.tutor.repo import create_roadmap
from backend.tutor.settings import Settings

EMB = HashEmbedder()
BODY = ("# Services\n\nKubernetes services expose pods inside the cluster. " * 1
        + "\n\n## ClusterIP\n\n" + " ".join(f"ClusterIP sentence {i} routes traffic to pods internally." for i in range(30))
        + "\n\n## Ingress\n\n" + " ".join(f"Ingress rule {i} sends external http requests to services." for i in range(30)))


@pytest.fixture
def index():
    return PlatformIndex(QdrantClient(":memory:"), "test_chunks", EMB)


@pytest.fixture
def clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))


def _doc(engine, slug, md=BODY, url="https://x.dev/a"):
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=slug, title=slug, source_text="x")
        d = Document(roadmap_id=rm.id, url=url, kind="page", title="Doc", markdown=md, meta={})
        s.add(d)
        s.flush()
        s.expunge_all()
        return d


def _count(index, **flt):
    from qdrant_client import models as qm
    must = [qm.FieldCondition(key=k, match=qm.MatchValue(value=v)) for k, v in flt.items()]
    return index.client.count(index.collection, count_filter=qm.Filter(must=must) if must else None, exact=True).count


def test_search_is_tenant_isolated(engine, clean, index):
    a = _doc(engine, "ra")
    b = _doc(engine, "rb")
    na = index_document(engine, index, a, [])
    nb = index_document(engine, index, b, [])
    assert na > 0 and nb > 0  # near-dup drop only compares across OTHER documents of the SAME roadmap
    for mode in ("hybrid", "dense", "sparse"):
        hits = index.search("clusterip traffic to pods", roadmap_id=str(a.roadmap_id), mode=mode)
        assert hits and all(h.payload["roadmap_id"] == str(a.roadmap_id) for h in hits)
    assert index.search("anything", roadmap_id=str(uuid.uuid4())) == []
    index.delete_roadmap(str(a.roadmap_id))
    assert _count(index, roadmap_id=str(a.roadmap_id)) == 0
    assert _count(index, roadmap_id=str(b.roadmap_id)) == nb


def test_reingest_does_not_duplicate(engine, clean, index):
    d = _doc(engine, "re")
    n1 = index_document(engine, index, d, ["s1"])
    pts, secs = _count(index), None
    with session_scope(engine) as s:
        secs = s.scalar(select(func.count()).select_from(Section))
    n2 = index_document(engine, index, d, ["s1"])
    assert n1 == n2 == pts == _count(index)
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(Section)) == secs > 0


def test_near_duplicate_across_documents_dropped(engine, clean, index):
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug="dup", title="d", source_text="x")
        d1 = Document(roadmap_id=rm.id, url="https://x.dev/1", kind="page", title="A", markdown=BODY, meta={})
        d2 = Document(roadmap_id=rm.id, url="https://x.dev/2", kind="page", title="B", markdown=BODY, meta={})
        s.add_all([d1, d2])
        s.flush()
        s.expunge_all()
    assert index_document(engine, index, d1, []) > 0
    assert index_document(engine, index, d2, []) == 0
    assert index_document(engine, index, d1, []) > 0  # re-index of the first is not self-dropped


def test_skill_filter_and_parents(engine, clean, index):
    d = _doc(engine, "sk")
    index_document(engine, index, d, ["k8s.services"])
    rid = str(d.roadmap_id)
    assert index.search("ingress", roadmap_id=rid, skill_id="k8s.services")
    assert index.search("ingress", roadmap_id=rid, skill_id="other") == []
    hit = index.search("ingress rule", roadmap_id=rid)[0]
    assert hit.payload["document_id"] == str(d.id) and hit.payload["url"] == "https://x.dev/a"
    parents = get_parents(engine, [hit.payload["parent_id"]])
    assert len(parents) == 1 and parents[0].text and parents[0].skill_ids == ["k8s.services"]


def test_platform_index_never_uses_demo_collection(monkeypatch):
    monkeypatch.setattr(config, "QDRANT_URL", "http://sentinel.invalid:1")
    monkeypatch.setattr(config, "QDRANT_API_KEY", "sentinel")
    from backend.rag import store
    monkeypatch.setattr(store, "qdrant", lambda: pytest.fail("demo client used"))
    idx = make_index(Settings(qdrant_url=":memory:", collection="roadmap_chunks"), EMB)
    idx.ensure()
    assert idx.collection == "roadmap_chunks" and idx.client.collection_exists("roadmap_chunks")
    assert not idx.client.collection_exists("sde_kb_v2")


def test_make_embedder_hash_and_bge_is_lazy():
    assert isinstance(make_embedder(Settings(embedder="hash")), HashEmbedder)
    bge = make_embedder(Settings(embedder="bge"))  # must not load any model
    assert bge.dim == config.DENSE_DIM


def test_reindex_shortened_document_removes_stale(engine, clean, index):
    d = _doc(engine, "short")
    n1 = index_document(engine, index, d, [])
    d.markdown = "# Services\n\nOnly a short paragraph remains here."
    n2 = index_document(engine, index, d, [])
    assert 0 < n2 < n1
    assert _count(index, document_id=str(d.id)) == n2
    with session_scope(engine) as s:
        rows = s.scalars(select(Section).where(Section.document_id == d.id)).all()
    assert len(rows) == 1 and "short paragraph" in rows[0].text


def test_video_duration_ends_last_chapter(engine, clean, index):
    md = ("# Tour\n\n## [00:00] Intro\n\n" + "Intro words about pods. " * 10
          + "\n\n## [01:00] Outro\n\n" + "Closing words about services. " * 10)
    d = _doc(engine, "dur", md=md, url="https://www.youtube.com/watch?v=abcdefghijk")
    d.kind, d.meta = "video", {"duration_s": 150.0}
    index_document(engine, index, d, [])
    with session_scope(engine) as s:
        rows = s.scalars(select(Section).where(Section.document_id == d.id).order_by(Section.ord)).all()
    assert [(r.start_s, r.end_s) for r in rows] == [(0.0, 60.0), (60.0, 150.0)]
