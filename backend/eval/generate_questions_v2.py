"""Generate the v2 eval set: multi-hop questions (two documents needed) and unanswerable questions.

multi_hop:     built from two related passages in different documents; one verbatim evidence quote
               from each, both checked programmatically against their passage.
unanswerable:  specific questions on fixed out-of-corpus topics, kept only when a judge says the
               top-8 retrieved passages do not contain the answer.

    python -m backend.eval.generate_questions_v2 --multi-hop 110 --seed 11
"""
import argparse
import hashlib
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from backend.core import config
from backend.core.llm import LLMUnavailable, haiku_json, with_backoff
from backend.eval.common import QUESTIONS_V2_PATH, normalize


def validate_multi_hop(out: dict, passage_a: str, passage_b: str) -> bool:
    if not out.get("suitable"):
        return False
    for quote, passage in ((out.get("evidence_quote_a", ""), passage_a), (out.get("evidence_quote_b", ""), passage_b)):
        if len(quote.split()) < 6 or normalize(quote) not in normalize(passage):
            return False
    return True


def build_record(kind: str, question: str, *, reference_answer: str = "", evidence: list[dict] | None = None,
                 topic: str = "") -> dict:
    qid = hashlib.sha1(question.encode()).hexdigest()[:10]
    return {"id": qid, "split": "test" if int(qid, 16) % 5 < 2 else "dev", "kind": kind, "question": question,
            "reference_answer": reference_answer, "evidence": evidence or [], "topic": topic}


TOPICS = [
    "Raft leader election", "Paxos", "CRDTs", "Kubernetes operators", "Istio service mesh", "eBPF",
    "Rust borrow checker", "Go garbage collector", "WebAssembly", "GraphQL federation", "gRPC streaming",
    "Kafka exactly-once semantics", "Apache Flink watermarks", "Spark shuffle", "Snowflake micro-partitions",
    "ClickHouse MergeTree", "Postgres MVCC vacuum", "LSM-tree compaction", "Bloom filter sizing", "HyperLogLog",
    "QUIC", "HTTP/3 head-of-line blocking", "OAuth 2.0 PKCE", "WebAuthn", "Terraform state locking",
    "Bazel remote execution", "React Server Components", "Swift actors", "CUDA kernels",
    "vector database HNSW tuning",
]

MULTI_HOP_SYSTEM = """You write evaluation questions for a retrieval-augmented software engineering tutor.
You are given two passages from DIFFERENT documents in its knowledge base. Write ONE question that a
software engineering student might genuinely ask, which can only be answered well using facts from BOTH
passages (for example, comparing, combining, or connecting the two).

Rules:
- The question needs facts from both passages; neither passage alone is enough.
- Phrase it naturally, as a student would. Do not copy distinctive phrases from the passages.
- Never refer to "the passages", "the sources", "passage A", or "the text".
- evidence_quote_a: 8-35 consecutive words copied EXACTLY from passage A (character for character)
  containing the fact needed from A. evidence_quote_b: the same, from passage B.
- reference_answer: 2-4 sentences grounded only in the two passages.
- Set suitable=false if the passages are not genuinely complementary, or either is boilerplate
  (table of contents, license, link list, front matter, mostly code without explanation)."""

MULTI_HOP_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["suitable", "question", "evidence_quote_a", "evidence_quote_b", "reference_answer"],
    "properties": {"suitable": {"type": "boolean"}, "question": {"type": "string"},
                   "evidence_quote_a": {"type": "string"}, "evidence_quote_b": {"type": "string"},
                   "reference_answer": {"type": "string"}},
}

UNANSWERABLE_QUESTION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["question"],
                                "properties": {"question": {"type": "string"}}}
ANSWERABLE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answerable"],
                     "properties": {"answerable": {"type": "boolean"}}}


def pick_pairs(n: int, seed: int) -> list[tuple[dict, dict]]:
    """Seed chunks paired with a related chunk from a different document (dense similarity 0.55-0.90)."""
    from backend.rag import store

    rng = random.Random(seed)
    pool, offset = [], None
    while True:
        points, offset = store.qdrant().scroll(config.KB_COLLECTION, limit=512, offset=offset, with_payload=True,
                                               scroll_filter=store._filter(["github", "book", "web"]))
        pool += [store.Hit(str(p.id), 1.0, p.payload or {}).to_dict() for p in points
                 if len((p.payload or {}).get("text", "")) >= 500]
        if offset is None:
            break
    rng.shuffle(pool)
    pairs, used_docs = [], set()
    for seed_chunk in pool:
        if len(pairs) >= n:
            break
        if seed_chunk["doc_id"] in used_docs:
            continue
        for hit in store.search(seed_chunk["text"][:400], mode="dense", top_k=8):
            h = hit.to_dict()
            if h["doc_id"] != seed_chunk["doc_id"] and h["title"] != seed_chunk["title"] and 0.55 <= h["score"] <= 0.90:
                pairs.append((seed_chunk, h))
                used_docs.update({seed_chunk["doc_id"], h["doc_id"]})
                break
    return pairs


def make_multi_hop(a: dict, b: dict) -> dict | None:
    prompt = (f"Passage A (from: {a['title']}):\n<passage_a>\n{a['text']}\n</passage_a>\n\n"
              f"Passage B (from: {b['title']}):\n<passage_b>\n{b['text']}\n</passage_b>")
    try:
        out = with_backoff(lambda: haiku_json(system=MULTI_HOP_SYSTEM, prompt=prompt, schema=MULTI_HOP_SCHEMA,
                                              effort="low", span_name="gen_multi_hop"))
    except LLMUnavailable as e:
        print("  skipped (LLM unavailable):", e)
        return None
    if not validate_multi_hop(out, a["text"], b["text"]):
        return None
    evidence = [{"quote": out["evidence_quote_a"].strip(), "doc_id": a["doc_id"], "title": a["title"]},
                {"quote": out["evidence_quote_b"].strip(), "doc_id": b["doc_id"], "title": b["title"]}]
    return build_record("multi_hop", out["question"].strip(), reference_answer=out["reference_answer"].strip(),
                        evidence=evidence)


def make_unanswerable(topic: str) -> dict | None:
    from backend.rag import store

    try:
        q = with_backoff(lambda: haiku_json(
            system="Write ONE specific, technical question a software engineering student might ask about the given "
                   "topic. It must have a concrete factual answer (a mechanism, a parameter, a trade-off), not an "
                   "opinion. Do not mention any book, course or source.",
            prompt=f"Topic: {topic}", schema=UNANSWERABLE_QUESTION_SCHEMA, effort="low", span_name="gen_unanswerable"))
        question = q["question"].strip()
        passages = store.search(question, mode="hybrid", top_k=8)
        ctx = "\n\n".join(f"<source>\n{h.text}\n</source>" for h in passages)
        verdict = with_backoff(lambda: haiku_json(
            system="Decide whether the sources contain enough information to answer the question correctly and "
                   "specifically. Related background that does not answer the question does not count.",
            prompt=f"{ctx}\n\nQuestion: {question}", schema=ANSWERABLE_SCHEMA, effort="low", span_name="check_answerable"))
    except LLMUnavailable as e:
        print("  skipped (LLM unavailable):", e)
        return None
    if verdict["answerable"]:
        return None
    return build_record("unanswerable", question, topic=topic)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--multi-hop", type=int, default=110, help="passage pairs to try (some are rejected)")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    pairs = pick_pairs(args.multi_hop, args.seed)
    print(f"picked {len(pairs)} passage pairs")
    with ThreadPoolExecutor(max_workers=8) as pool:
        multi = list(pool.map(lambda p: make_multi_hop(*p), pairs))
        unans = list(pool.map(make_unanswerable, TOPICS))

    seen, records = set(), []
    for r in [x for x in multi + unans if x]:
        key = normalize(r["question"])
        if key not in seen:
            seen.add(key)
            records.append(r)
    QUESTIONS_V2_PATH.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    print(f"wrote {len(records)} questions -> {QUESTIONS_V2_PATH}")
    print("by kind:", dict(Counter(r["kind"] for r in records)),
          "by kind/split:", dict(Counter((r["kind"], r["split"]) for r in records)))


if __name__ == "__main__":
    main()
