"""Embedders. `HashEmbedder` is the deterministic, dependency-free dense embedder used by tests,
by `EMBEDDER=hash`, and by topic-shift chapter detection (no model download, no LLM)."""
from __future__ import annotations

import math
import re
import zlib

_WORD = re.compile(r"\w+", re.UNICODE)


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
