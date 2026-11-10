"""Shared eval helpers: dataset IO and chunk-boundary-independent relevance matching."""
import json
import re
from pathlib import Path

from backend.core import config

EVAL_DIR = config.BACKEND_DIR / "eval"
QUESTIONS_PATH = EVAL_DIR / "data" / "questions.jsonl"
QUESTIONS_V2_PATH = EVAL_DIR / "data" / "questions_v2.jsonl"  # multi_hop + unanswerable
RESULTS_DIR = EVAL_DIR / "results"


def normalize(text: str) -> str:
    # lowercase, unify whitespace, undo PDF line-break hyphenation, drop markdown emphasis
    text = text.lower().replace("-\n", "").replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize(text))


def contains_evidence(chunk_text: str, quote: str, min_recall: float = 0.8) -> bool:
    """True if the chunk contains the evidence quote, or >=80% of its words in order-insensitive form.

    The fuzzy fallback handles quotes that straddle a chunk boundary or differ in PDF whitespace.
    """
    if normalize(quote) in normalize(chunk_text):
        return True
    q = _words(quote)
    if not q:
        return False
    chunk_words = set(_words(chunk_text))
    return sum(w in chunk_words for w in q) / len(q) >= min_recall and _longest_run(q, _words(chunk_text)) >= min(6, len(q))


def _longest_run(q: list[str], c: list[str]) -> int:
    """Length of the longest run of consecutive quote words appearing consecutively in the chunk."""
    best = 0
    positions: dict[str, list[int]] = {}
    for i, w in enumerate(c):
        positions.setdefault(w, []).append(i)
    for start in range(len(q)):
        for p in positions.get(q[start], []):
            n = 0
            while start + n < len(q) and p + n < len(c) and q[start + n] == c[p + n]:
                n += 1
            best = max(best, n)
    return best


def load_questions(split: str | None = None) -> list[dict]:
    items = [json.loads(l) for l in QUESTIONS_PATH.read_text().splitlines() if l.strip()]
    return [i for i in items if split in (None, "all") or i["split"] == split]


def load_questions_v2(split: str | None = None, kind: str | None = None) -> list[dict]:
    items = [json.loads(l) for l in QUESTIONS_V2_PATH.read_text().splitlines() if l.strip()]
    return [i for i in items
            if (split in (None, "all") or i["split"] == split) and (kind is None or i["kind"] == kind)]


def paired_bootstrap_delta(a, b, n_boot: int = 10000, seed: int = 0) -> dict:
    """95% CI for mean(b - a) by resampling paired items (same questions answered by both variants)."""
    import random

    if len(a) != len(b) or not a:
        raise ValueError("paired_bootstrap_delta needs two non-empty sequences of equal length")
    diffs = [float(y) - float(x) for x, y in zip(a, b)]
    rng = random.Random(seed)
    boots = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n_boot))
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]
    return {"delta": round(sum(diffs) / len(diffs), 3), "ci95": [round(lo, 3), round(hi, 3)],
            "significant": lo > 0 or hi < 0}
