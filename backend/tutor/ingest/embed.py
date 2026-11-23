"""Embedders. `HashEmbedder` is the deterministic, dependency-free embedder used by tests,
by `EMBEDDER=hash`, and by topic-shift chapter detection (no model download, no LLM). `BgeEmbedder`
wraps the demo's BGE dense + fastembed sparse models, imported lazily. Both satisfy `Embedder`."""
from __future__ import annotations

import math
import re
import zlib
from typing import Protocol

from qdrant_client.models import SparseVector

_WORD = re.compile(r"\w+", re.UNICODE)
SPARSE_BUCKETS = 1 << 18


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def sparse(self, texts: list[str]) -> list[SparseVector]: ...
    def sparse_query(self, text: str) -> SparseVector: ...


class HashEmbedder:
    """Hashed bag of words: each lower-cased word adds 1 to bucket crc32(word) % dim; the vector is
    L2-normalized (all zeros for text with no words). Stable across processes."""

    def __init__(self, dim: int = 64):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for w in _WORD.findall(text.lower()):
            vec[zlib.crc32(w.encode("utf-8")) % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)

    def sparse(self, texts: list[str]) -> list[SparseVector]:
        return [self._sparse_one(t) for t in texts]

    def sparse_query(self, text: str) -> SparseVector:
        return self._sparse_one(text)

    @staticmethod
    def _sparse_one(text: str) -> SparseVector:
        """Hashed term frequencies: bucket crc32(word) % 2^18 -> count (collisions add)."""
        tf: dict[int, float] = {}
        for w in _WORD.findall(text.lower()):
            i = zlib.crc32(w.encode("utf-8")) % SPARSE_BUCKETS
            tf[i] = tf.get(i, 0.0) + 1.0
        idx = sorted(tf)
        return SparseVector(indices=idx, values=[tf[i] for i in idx])


class BgeEmbedder:
    """The demo's models (BGE dense, fastembed sparse), loaded on first use only."""

    def __init__(self):
        from backend.core import config

        self.dim = config.DENSE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        from backend.rag import store

        return store.embed_passages(texts)

    def embed_query(self, text: str) -> list[float]:
        from backend.rag import store

        return store.embed_query(text)

    @staticmethod
    def _sv(v) -> SparseVector:
        return SparseVector(indices=v.indices.tolist(), values=v.values.tolist())

    def sparse(self, texts: list[str]) -> list[SparseVector]:
        from backend.rag import store

        return [self._sv(v) for v in store.sparse_model().embed(texts)]

    def sparse_query(self, text: str) -> SparseVector:
        from backend.rag import store

        return self._sv(next(store.sparse_model().query_embed(text)))


def make_embedder(settings) -> Embedder:
    return HashEmbedder() if settings.embedder == "hash" else BgeEmbedder()
