"""Generate a retrieval/answer eval set from the knowledge base with Claude Haiku 5.5.

Each item: a realistic student question, the gold document, a VERBATIM evidence quote (so retrieval
hits can be scored independently of chunk boundaries), and a reference answer.

    python -m backend.eval.generate_questions --n 100
"""
import argparse
import hashlib
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor

from backend.core.llm import LLMUnavailable, haiku_json, with_backoff
from backend.eval.common import QUESTIONS_PATH, normalize
from backend.rag import documents as D

SYSTEM = """You write evaluation questions for a retrieval-augmented software engineering tutor.
You are given one passage from its knowledge base. Write ONE question that a software engineering
student might genuinely ask a tutor, which this passage answers well.

Rules:
- Phrase the question naturally in your own words. Do not copy distinctive phrases from the passage.
- The question must be specific enough that this passage is a strong source for it, not a generic
  question any textbook answers ("what is software engineering?" is too generic).
- The question must make sense on its own (no "according to the passage", no "in this chapter").
- evidence_quote: copy 8-35 consecutive words EXACTLY from the passage (character for character)
  that contain the key fact needed to answer.
- reference_answer: 1-3 sentences, grounded only in the passage.
- Set suitable=false if the passage is boilerplate (table of contents, index, license, link lists,
  front matter, mostly code without explanation) or too fragmentary to support a good question."""

SCHEMA = {
    "type": "object",
    "properties": {
        "suitable": {"type": "boolean"},
        "question": {"type": "string"},
        "evidence_quote": {"type": "string"},
        "reference_answer": {"type": "string"},
        "question_type": {"type": "string", "enum": ["factual", "conceptual", "how_to", "comparison"]},
    },
    "required": ["suitable", "question", "evidence_quote", "reference_answer", "question_type"],
    "additionalProperties": False,
}

QUOTAS = {"book": 0.45, "github": 0.45, "web": 0.10}


def sample_passage(doc: D.Document, rng: random.Random, size: int = 1400) -> str:
    text = doc.text
    if len(text) <= size:
        return text
    start = rng.randrange(0, len(text) - size)
    # align to a paragraph or sentence boundary so the passage reads naturally
    para = text.rfind("\n\n", 0, start)
    start = para + 2 if para != -1 and start - para < 400 else start
    return text[start:start + size]


def make_item(doc: D.Document, passage: str) -> dict | None:
    try:
        out = with_backoff(lambda: haiku_json(system=SYSTEM, prompt=f"Passage from: {doc.title}\n\n<passage>\n{passage}\n</passage>",
                                              schema=SCHEMA, effort="low", span_name="gen_question"))
    except LLMUnavailable as e:
        print("  skipped (LLM unavailable):", e)
        return None
    if not out["suitable"]:
        return None
    quote = out["evidence_quote"].strip()
    if len(quote.split()) < 6 or normalize(quote) not in normalize(passage):
        return None  # quote must be verbatim, or scoring would be meaningless
    qid = hashlib.sha1(out["question"].encode()).hexdigest()[:10]
    return {
        "id": qid,
        "split": "test" if int(qid, 16) % 5 < 2 else "dev",  # stable ~60/40 dev/test split
        "question": out["question"].strip(),
        "question_type": out["question_type"],
        "evidence_quote": quote,
        "reference_answer": out["reference_answer"].strip(),
        "gold_doc_id": doc.doc_id,
        "gold_title": doc.title,
        "source_type": doc.source_type,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="passages to try (some are rejected as unsuitable)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    docs, _ = D.clean_documents(D.reconstruct_documents())
    by_type = {t: [d for d in docs if d.source_type == t and len(d.text) > 600] for t in QUOTAS}
    jobs = []
    for t, share in QUOTAS.items():
        pool = by_type[t]
        for d in rng.sample(pool, min(len(pool), round(args.n * share))):
            jobs.append((d, sample_passage(d, rng)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        items = [it for it in pool.map(lambda j: make_item(*j), jobs) if it]

    seen, unique = set(), []
    for it in items:
        key = re.sub(r"\W+", " ", it["question"].lower()).strip()
        if key not in seen:
            seen.add(key)
            unique.append(it)
    QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUESTIONS_PATH.write_text("\n".join(json.dumps(i) for i in unique) + "\n")
    from collections import Counter
    print(f"wrote {len(unique)} questions from {len(jobs)} passages -> {QUESTIONS_PATH}")
    print("by source:", dict(Counter(i["source_type"] for i in unique)), "by split:", dict(Counter(i["split"] for i in unique)))


if __name__ == "__main__":
    main()
