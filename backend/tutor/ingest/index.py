"""The multitenant platform index: one Qdrant collection (`Settings.collection` on `Settings.qdrant_url`)
shared by all roadmaps and filtered by the `roadmap_id` tenant payload index. Never touches the demo
collection or `config.QDRANT_URL`."""
from __future__ import annotations

import warnings
from collections.abc import Sequence

from qdrant_client import QdrantClient
from qdrant_client import models as qm
from sqlalchemy import Engine, delete, select

from backend.rag.documents import _shingles
from backend.rag.store import Hit
from backend.tutor.db import session_scope
from backend.tutor.ingest.chunking import SEP, ChildChunk, chunk_markdown
from backend.tutor.models import Section

DENSE = "dense"
SPARSE = "bm25"
DUP_JACCARD = 0.9
BATCH = 64


def _eq(key: str, value: str) -> qm.FieldCondition:
    return qm.FieldCondition(key=key, match=qm.MatchValue(value=value))


class PlatformIndex:
    def __init__(self, client: QdrantClient, collection: str, embedder):
        self.client, self.collection, self.embedder = client, collection, embedder
        self._ready = False

    def ensure(self) -> None:
        if self._ready:
            return
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config={DENSE: qm.VectorParams(size=self.embedder.dim, distance=qm.Distance.COSINE)},
                sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
            )
            with warnings.catch_warnings():  # local/in-memory clients warn that payload indexes are no-ops
                warnings.filterwarnings("ignore", message="Payload indexes have no effect", category=UserWarning)
                self.client.create_payload_index(
                    self.collection, "roadmap_id",
                    qm.KeywordIndexParams(type=qm.KeywordIndexType.KEYWORD, is_tenant=True))
                for field in ("skill_ids", "document_id"):
                    self.client.create_payload_index(self.collection, field, qm.PayloadSchemaType.KEYWORD)
        self._ready = True

    def upsert(self, roadmap_id: str, document_id: str, skill_ids: Sequence[str],
               children: list[ChildChunk], meta: dict) -> int:
        """Replace the document's points: delete the old ones, then upsert the new."""
        self.ensure()
        points = []
        for i in range(0, len(children), BATCH):  # embed everything before touching the old points
            part = children[i:i + BATCH]
            texts = [c.embed_text for c in part]
            dense, sparse = self.embedder.embed(texts), self.embedder.sparse(texts)
            points += [
                qm.PointStruct(id=c.id, vector={DENSE: d, SPARSE: sp}, payload={
                    "roadmap_id": roadmap_id, "document_id": document_id, "parent_id": c.parent_id,
                    "skill_ids": list(skill_ids), "text": c.text, "path": SEP.join(c.path or []),
                    "url": meta.get("url", ""), "title": meta.get("title", ""), "kind": meta.get("kind", ""),
                    "start_s": c.start_s, "end_s": c.end_s})
                for c, d, sp in zip(part, dense, sparse)]
        self.client.delete(self.collection, points_selector=qm.FilterSelector(
            filter=qm.Filter(must=[_eq("roadmap_id", roadmap_id), _eq("document_id", document_id)])), wait=True)
        for i in range(0, len(points), BATCH):
            self.client.upsert(self.collection, points=points[i:i + BATCH], wait=True)
        return len(children)

    def search(self, query: str, *, roadmap_id: str, skill_id: str | None = None, top_k: int = 8,
               mode: str = "hybrid") -> list[Hit]:
        self.ensure()
        must = [_eq("roadmap_id", roadmap_id)]  # tenant filter: always applied
        if skill_id:
            must.append(_eq("skill_ids", skill_id))
        flt = qm.Filter(must=must)
        if mode == "dense":
            res = self.client.query_points(self.collection, query=self.embedder.embed_query(query), using=DENSE,
                                           limit=top_k, query_filter=flt, with_payload=True)
        elif mode == "sparse":
            res = self.client.query_points(self.collection, query=self.embedder.sparse_query(query), using=SPARSE,
                                           limit=top_k, query_filter=flt, with_payload=True)
        elif mode == "hybrid":
            n = max(top_k, 20)
            res = self.client.query_points(
                self.collection,
                prefetch=[qm.Prefetch(query=self.embedder.embed_query(query), using=DENSE, limit=n, filter=flt),
                          qm.Prefetch(query=self.embedder.sparse_query(query), using=SPARSE, limit=n, filter=flt)],
                query=qm.FusionQuery(fusion=qm.Fusion.RRF), limit=top_k, with_payload=True)
        else:
            raise ValueError(f"unknown search mode {mode!r}")
        return [Hit(str(p.id), p.score, p.payload or {}) for p in res.points]

    def delete_roadmap(self, roadmap_id: str) -> None:
        self.ensure()
        self.client.delete(self.collection, points_selector=qm.FilterSelector(
            filter=qm.Filter(must=[_eq("roadmap_id", roadmap_id)])), wait=True)

    def other_texts(self, roadmap_id: str, exclude_document_id: str) -> list[str]:
        """Child texts of the roadmap's OTHER documents (payload scroll, no vectors)."""
        self.ensure()
        flt = qm.Filter(must=[_eq("roadmap_id", roadmap_id)], must_not=[_eq("document_id", exclude_document_id)])
        out, offset = [], None
        while True:
            pts, offset = self.client.scroll(self.collection, scroll_filter=flt, limit=256, offset=offset,
                                             with_payload=["text"], with_vectors=False)
            out += [(p.payload or {}).get("text", "") for p in pts]
            if offset is None:
                return out


def make_index(settings, embedder) -> PlatformIndex:
    client = QdrantClient(":memory:") if settings.qdrant_url == ":memory:" else QdrantClient(
        url=settings.qdrant_url, timeout=60, check_compatibility=False)  # no request until first use
    return PlatformIndex(client, settings.collection, embedder)


def _drop_near_duplicates(children: list[ChildChunk], others: list[str]) -> list[ChildChunk]:
    """Drop children whose 5-shingle Jaccard against any other document's child in the roadmap is >= 0.9.
    Same-document chunks are never compared, so re-indexing a document is idempotent. Cost is
    O(children x roadmap children) set ops, bounded by roadmap size."""
    pool = [s for s in map(_shingles, others) if s]
    kept = []
    for c in children:
        sh = _shingles(c.text)
        if sh and any(len(sh & o) / len(sh | o) >= DUP_JACCARD for o in pool):
            continue
        kept.append(c)
    return kept


def index_document(engine: Engine, index: PlatformIndex, document, skill_ids: Sequence[str], *,
                   dedupe: bool = True) -> int:
    """Chunk the document; replace its Section rows, then its Qdrant points; return the child count.
    A video's `meta["duration_s"]` (when > 0) becomes the end of its last chapter. `dedupe=False` skips the
    cross-document near-duplicate scan (a bulk import of already-deduplicated documents: the scan scrolls every
    other child of the roadmap per document, O(n^2) overall)."""
    doc_id, rid = str(document.id), str(document.roadmap_id)
    duration = (document.meta or {}).get("duration_s")
    parents, children = chunk_markdown(document.markdown or "", doc_title=document.title or document.url,
                                       doc_key=doc_id, embedder=index.embedder,
                                       final_end_s=float(duration) if duration else None)
    if dedupe:
        children = _drop_near_duplicates(children, index.other_texts(rid, doc_id))
    live = {c.parent_id for c in children}
    parents = [p for p in parents if p.id in live]
    with session_scope(engine) as s:
        s.execute(delete(Section).where(Section.document_id == document.id))
        s.add_all([Section(id=p.id, roadmap_id=document.roadmap_id, document_id=document.id,
                           skill_ids=list(skill_ids), path=SEP.join(p.path), text=p.text, tokens=p.tokens,
                           start_s=p.start_s, end_s=p.end_s, ord=p.ord) for p in parents])
    return index.upsert(rid, doc_id, skill_ids, children,
                        {"url": document.url, "title": document.title, "kind": document.kind})


def get_parents(engine: Engine, parent_ids: Sequence[str]) -> list[Section]:
    """Sections for the given ids, in the given order (unknown ids skipped)."""
    with session_scope(engine) as s:
        rows = {r.id: r for r in s.scalars(select(Section).where(Section.id.in_(list(parent_ids))))}
        s.expunge_all()
    return [rows[i] for i in parent_ids if i in rows]
