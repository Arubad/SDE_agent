"""Semantic answer cache in Qdrant: near-duplicate first-turn questions skip the LLM entirely.

Keyed by tier (premium/public answers come from different pipelines) and expired by TTL.
Only stand-alone questions are cached (no chat history, no YouTube links) so a cached answer
can never be wrong for its context.
"""
from __future__ import annotations

import time

from qdrant_client import models as qm

from backend.core import config
from backend.core.trace import current_trace
from backend.rag.documents import stable_id
from backend.rag.store import embed_query, qdrant

_ready = False


def _ensure() -> None:
    global _ready
    if _ready:
        return
    client = qdrant()
    if not client.collection_exists(config.CACHE_COLLECTION):
        client.create_collection(config.CACHE_COLLECTION, vectors_config=qm.VectorParams(
            size=config.DENSE_DIM, distance=qm.Distance.COSINE))
        if config.QDRANT_URL:
            client.create_payload_index(config.CACHE_COLLECTION, "tier", qm.PayloadSchemaType.KEYWORD)
            client.create_payload_index(config.CACHE_COLLECTION, "created_at", qm.PayloadSchemaType.FLOAT)
    _ready = True


def lookup(question: str, tier: str) -> dict | None:
    if not config.SEMANTIC_CACHE:
        return None
    with current_trace().span("cache_lookup", kind="cache") as span:
        _ensure()
        res = qdrant().query_points(
            config.CACHE_COLLECTION, query=embed_query(question), limit=1, with_payload=True,
            score_threshold=config.CACHE_THRESHOLD,
            query_filter=qm.Filter(must=[
                qm.FieldCondition(key="tier", match=qm.MatchValue(value=tier)),
                qm.FieldCondition(key="created_at", range=qm.Range(gte=time.time() - config.CACHE_TTL_S)),
            ]),
        )
        if not res.points:
            span.set(hit=False)
            return None
        hit = res.points[0]
        span.set(hit=True, similarity=round(hit.score, 4), cached_question=hit.payload.get("question", "")[:120])
        return {**hit.payload, "similarity": hit.score}


def store(question: str, tier: str, answer: dict) -> None:
    if not config.SEMANTIC_CACHE:
        return
    _ensure()
    qdrant().upsert(config.CACHE_COLLECTION, points=[qm.PointStruct(
        id=stable_id("cache", tier, question.strip().lower()), vector=embed_query(question),
        payload={"question": question, "tier": tier, "created_at": time.time(), **answer},
    )])
