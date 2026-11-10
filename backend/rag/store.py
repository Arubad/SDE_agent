"""Qdrant-backed knowledge base: dense (BGE) + sparse (BM25) vectors, hybrid RRF fusion, reranking."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from backend.core import config
from backend.core.trace import current_trace
from backend.rag.documents import Chunk

DENSE = "dense"
SPARSE = "bm25"
FILTERABLE = ("source_type", "doc_id", "url")


@lru_cache(maxsize=1)
def qdrant() -> QdrantClient:
    if config.QDRANT_URL:
        return QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY or None, timeout=60)
    # Embedded on-disk mode for local development / CI without a server
    return QdrantClient(path=config.QDRANT_LOCAL_PATH)


_model_lock = threading.Lock()
_models: dict = {}


def _load_once(key: str, factory):
    """Thread-safe lazy singleton: concurrent first requests must not each load a model."""
    if key not in _models:
        with _model_lock:
            if key not in _models:
                _models[key] = factory()
    return _models[key]


def dense_model():
    from sentence_transformers import SentenceTransformer
    return _load_once("dense", lambda: SentenceTransformer(config.DENSE_MODEL, device="cpu"))


def sparse_model():
    from fastembed import SparseTextEmbedding
    return _load_once("sparse", lambda: SparseTextEmbedding(model_name=config.SPARSE_MODEL))


def reranker(name: str):
    from sentence_transformers import CrossEncoder
    return _load_once(f"rerank:{name}", lambda: CrossEncoder(name, device="cpu", max_length=512))


def embed_passages(texts: list[str]) -> list[list[float]]:
    return dense_model().encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=len(texts) > 500).tolist()


def embed_query(text: str) -> list[float]:
    return dense_model().encode(config.BGE_QUERY_PREFIX + text, normalize_embeddings=True).tolist()


def _sparse(vec) -> qm.SparseVector:
    return qm.SparseVector(indices=vec.indices.tolist(), values=vec.values.tolist())


def ensure_collection(name: str, recreate: bool = False) -> None:
    client = qdrant()
    if recreate and client.collection_exists(name):
        client.delete_collection(name)
    if client.collection_exists(name):
        return
    client.create_collection(
        name,
        vectors_config={DENSE: qm.VectorParams(size=config.DENSE_DIM, distance=qm.Distance.COSINE)},
        # IDF is computed by Qdrant over the live collection, so BM25 stays correct as videos are added
        sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
    )
    if config.QDRANT_URL:  # payload indexes are required for filtering on Qdrant Cloud
        for field in FILTERABLE:
            client.create_payload_index(name, field, qm.PayloadSchemaType.KEYWORD)


def upsert_chunks(name: str, chunks: list[Chunk], batch: int = 128) -> int:
    ensure_collection(name)
    client = qdrant()
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        texts = [c.embed_text for c in part]
        dense = embed_passages(texts)
        sparse = list(sparse_model().embed(texts))
        client.upsert(name, points=[
            qm.PointStruct(id=c.chunk_id, vector={DENSE: d, SPARSE: _sparse(s)}, payload=c.payload())
            for c, d, s in zip(part, dense, sparse)
        ], wait=True)
    return len(chunks)


@dataclass
class Hit:
    chunk_id: str
    score: float
    payload: dict

    @property
    def text(self) -> str:
        return self.payload.get("text", "")

    def to_dict(self) -> dict:
        p = self.payload
        return {"chunk_id": self.chunk_id, "score": round(float(self.score), 4), "title": p.get("title", ""),
                "section": p.get("section", ""), "source_type": p.get("source_type", ""), "url": p.get("url", ""),
                "doc_id": p.get("doc_id", ""), "text": p.get("text", ""), "meta": p.get("meta", {})}


def _filter(source_types: Iterable[str] | None, doc_id: str | None = None) -> qm.Filter | None:
    must = []
    if source_types:
        must.append(qm.FieldCondition(key="source_type", match=qm.MatchAny(any=list(source_types))))
    if doc_id:
        must.append(qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id)))
    return qm.Filter(must=must) if must else None


def search(query: str, *, collection: str | None = None, mode: str | None = None, top_k: int | None = None,
           source_types: list[str] | None = None, candidates: int | None = None,
           reranker_name: str | None = None, doc_id: str | None = None) -> list[Hit]:
    """Retrieve top_k chunks. Modes: dense | sparse | hybrid | hybrid_rerank | dense_rerank."""
    collection = collection or config.KB_COLLECTION
    mode = mode or config.RETRIEVAL_MODE
    top_k = top_k or config.TOP_K
    candidates = candidates or config.CANDIDATES_K
    flt = _filter(source_types, doc_id)
    client = qdrant()
    trace = current_trace()

    with trace.span("retrieve", kind="retrieval", mode=mode, collection=collection) as span:
        fetch = candidates if mode.endswith("_rerank") else top_k
        if mode in ("dense", "dense_rerank"):
            res = client.query_points(collection, query=embed_query(query), using=DENSE, limit=fetch,
                                      query_filter=flt, with_payload=True)
        elif mode == "sparse":
            res = client.query_points(collection, query=_sparse(next(sparse_model().query_embed(query))),
                                      using=SPARSE, limit=fetch, query_filter=flt, with_payload=True)
        elif mode in ("hybrid", "hybrid_rerank"):
            res = client.query_points(
                collection,
                prefetch=[
                    qm.Prefetch(query=embed_query(query), using=DENSE, limit=max(fetch, 20), filter=flt),
                    qm.Prefetch(query=_sparse(next(sparse_model().query_embed(query))), using=SPARSE,
                                limit=max(fetch, 20), filter=flt),
                ],
                query=qm.FusionQuery(fusion=qm.Fusion.RRF), limit=fetch, with_payload=True,
            )
        else:
            raise ValueError(f"unknown retrieval mode {mode!r}")
        hits = [Hit(str(p.id), p.score, p.payload or {}) for p in res.points]

        if mode.endswith("_rerank") and hits:
            t = time.perf_counter()
            model = reranker(reranker_name or config.RERANKER_MODEL)
            pairs = [(query, f"{h.payload.get('title', '')} {h.payload.get('section', '')}\n{h.text}") for h in hits]
            scores = model.predict(pairs, batch_size=16)
            hits = [Hit(h.chunk_id, float(s), h.payload) for h, s in sorted(zip(hits, scores), key=lambda x: -x[1])]
            span.set(rerank_ms=round((time.perf_counter() - t) * 1000))
        hits = hits[:top_k]
        span.set(hits=len(hits))
        return hits


def collection_stats(collection: str | None = None) -> dict:
    collection = collection or config.KB_COLLECTION
    client = qdrant()
    if not client.collection_exists(collection):
        return {"collection": collection, "exists": False}
    total = client.count(collection, exact=True).count
    by_type = {}
    for st in ("github", "book", "web", "youtube"):
        by_type[st] = client.count(collection, count_filter=_filter([st]), exact=True).count
    return {"collection": collection, "exists": True, "chunks": total, "by_source_type": by_type}


def list_documents(collection: str | None = None, source_type: str | None = None, limit: int = 200) -> list[dict]:
    """Distinct documents in the KB (title, url, type), via payload scroll."""
    collection = collection or config.KB_COLLECTION
    seen, out, offset = set(), [], None
    while len(out) < limit:
        points, offset = qdrant().scroll(collection, scroll_filter=_filter([source_type] if source_type else None),
                                         limit=256, offset=offset, with_payload=["doc_id", "title", "url", "source_type"])
        for p in points:
            pl = p.payload or {}
            if pl.get("doc_id") not in seen:
                seen.add(pl.get("doc_id"))
                out.append({k: pl.get(k) for k in ("title", "url", "source_type", "doc_id")})
        if offset is None:
            break
    return out[:limit]


def has_document(doc_id: str, collection: str | None = None) -> bool:
    collection = collection or config.KB_COLLECTION
    if not qdrant().collection_exists(collection):
        return False
    return qdrant().count(collection, count_filter=_filter(None, doc_id), exact=True).count > 0


def document_chunks(doc_id: str, collection: str | None = None, limit: int = 400) -> list[dict]:
    collection = collection or config.KB_COLLECTION
    points, _ = qdrant().scroll(collection, scroll_filter=_filter(None, doc_id), limit=limit, with_payload=True)
    return [Hit(str(p.id), 1.0, p.payload or {}).to_dict() for p in points]
