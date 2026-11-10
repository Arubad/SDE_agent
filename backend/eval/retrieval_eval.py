"""Retrieval evaluation: no LLM calls, so it is free to run (locally or in CI).

Metrics per config (higher is better except latency):
  hit@1 / hit@5  - a top-k chunk contains the question's verbatim evidence quote
  mrr@10         - mean reciprocal rank of the first evidence-bearing chunk
  doc_hit@5      - a top-5 chunk comes from the gold source document
  p50_ms         - median retrieval latency (includes network to Qdrant Cloud)

    python -m backend.eval.retrieval_eval                       # full comparison table
    python -m backend.eval.retrieval_eval --configs v2_hybrid_rerank --split test --min-hit5 0.6   # CI gate
"""
import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone

from backend.eval.common import RESULTS_DIR, contains_evidence, load_questions

CONFIGS = {
    # name: (collection, mode, reranker[, candidates fetched before reranking; default 30])
    "legacy_llamaindex": (None, "legacy", None),
    "v1_dense": ("sde_kb_v1", "dense", None),
    "v1_bm25": ("sde_kb_v1", "sparse", None),
    "v1_hybrid": ("sde_kb_v1", "hybrid", None),
    "v1_hybrid_rerank": ("sde_kb_v1", "hybrid_rerank", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
    "v2_dense": ("sde_kb_v2", "dense", None),
    "v2_bm25": ("sde_kb_v2", "sparse", None),
    "v2_hybrid": ("sde_kb_v2", "hybrid", None),
    "v2_hybrid_rerank": ("sde_kb_v2", "hybrid_rerank", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
    "v2_hybrid_rerank_bge": ("sde_kb_v2", "hybrid_rerank", "BAAI/bge-reranker-base"),
    "v2_hybrid_rerank_bge_c15": ("sde_kb_v2", "hybrid_rerank", "BAAI/bge-reranker-base", 15),
}


def legacy_retriever():
    """The retriever the app shipped with: LlamaIndex JSON vector store, dense only."""
    from llama_index.core import Settings, StorageContext, load_index_from_storage
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    from backend.core import config
    Settings.embed_model = HuggingFaceEmbedding(model_name=config.DENSE_MODEL, device="cpu")
    index = load_index_from_storage(StorageContext.from_defaults(persist_dir=str(config.BACKEND_DIR / "storage")))
    r = index.as_retriever(similarity_top_k=10)
    return lambda q: [{"text": n.node.get_content(), "doc_id": n.node.ref_doc_id} for n in r.retrieve(q)]


def run_config(name: str, questions: list[dict]) -> dict:
    collection, mode, rr, *rest = CONFIGS[name]
    candidates = rest[0] if rest else None
    if mode == "legacy":
        retrieve = legacy_retriever()
    else:
        from backend.rag.store import search
        retrieve = lambda q: [h.to_dict() for h in search(q, collection=collection, mode=mode, top_k=10, reranker_name=rr, candidates=candidates)]
    retrieve(questions[0]["question"])  # warm up models / connections so latency is comparable

    rows, latencies = [], []
    for q in questions:
        t = time.perf_counter()
        hits = retrieve(q["question"])
        latencies.append((time.perf_counter() - t) * 1000)
        rank = next((i + 1 for i, h in enumerate(hits) if contains_evidence(h["text"], q["evidence_quote"])), None)
        doc_hit = any(h.get("doc_id") == q["gold_doc_id"] for h in hits[:5])
        rows.append({"id": q["id"], "rank": rank, "doc_hit5": doc_hit})
    n = len(rows)
    return {
        "config": name, "n": n,
        "hit@1": round(sum(r["rank"] == 1 for r in rows) / n, 3),
        "hit@5": round(sum(bool(r["rank"]) and r["rank"] <= 5 for r in rows) / n, 3),
        "mrr@10": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 3),
        "doc_hit@5": round(sum(r["doc_hit5"] for r in rows) / n, 3),
        "p50_ms": round(statistics.median(latencies)),
        "misses": [r["id"] for r in rows if not r["rank"] or r["rank"] > 5],
        "ranks": {r["id"]: r["rank"] for r in rows},
    }


def paired_bootstrap(a: dict, b: dict, metric: str = "mrr", n_boot: int = 10000, seed: int = 0) -> dict:
    """95% CI for metric(b) - metric(a) by resampling questions (paired: same questions for both)."""
    import random
    ids = sorted(set(a["ranks"]) & set(b["ranks"]))
    def score(rank):
        if metric == "mrr":
            return 1 / rank if rank else 0.0
        return 1.0 if rank and rank <= int(metric.split("@")[1]) else 0.0
    diffs = [score(b["ranks"][i]) - score(a["ranks"][i]) for i in ids]
    rng = random.Random(seed)
    boots = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n_boot))
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot)]
    return {"a": a["config"], "b": b["config"], "metric": metric, "delta": round(sum(diffs) / len(diffs), 3),
            "ci95": [round(lo, 3), round(hi, 3)], "significant": lo > 0 or hi < 0}


def to_markdown(results: list[dict], split: str) -> str:
    lines = [f"Split: **{split}** (n={results[0]['n']})", "",
             "| config | hit@1 | hit@5 | MRR@10 | doc hit@5 | p50 latency |", "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['config']} | {r['hit@1']:.2f} | {r['hit@5']:.2f} | {r['mrr@10']:.2f} | {r['doc_hit@5']:.2f} | {r['p50_ms']} ms |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", nargs="*", default=list(CONFIGS))
    ap.add_argument("--split", default="all", choices=["all", "dev", "test"])
    ap.add_argument("--min-hit5", type=float, help="exit non-zero if the first config's hit@5 is below this (CI gate)")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--compare", nargs=2, action="append", metavar=("A", "B"),
                    help="paired bootstrap CI of B-A (repeatable), e.g. --compare legacy_llamaindex v2_hybrid")
    args = ap.parse_args()

    questions = load_questions(args.split)
    results = []
    for name in args.configs:
        r = run_config(name, questions)
        results.append(r)
        print(f"{name:24s} hit@1={r['hit@1']:.2f} hit@5={r['hit@5']:.2f} mrr={r['mrr@10']:.2f} doc@5={r['doc_hit@5']:.2f} p50={r['p50_ms']}ms", flush=True)

    md = to_markdown(results, args.split)
    by_name = {r["config"]: r for r in results}
    comparisons = []
    for a, b in args.compare or []:
        for metric in ("mrr", "hit@1", "hit@5"):
            c = paired_bootstrap(by_name[a], by_name[b], metric)
            comparisons.append(c)
    if comparisons:
        md += "\n\nPaired bootstrap (10k resamples), delta = B - A:\n\n| A | B | metric | delta | 95% CI | significant |\n|---|---|---|---|---|---|\n"
        md += "\n".join(f"| {c['a']} | {c['b']} | {c['metric']} | {c['delta']:+.3f} | [{c['ci95'][0]:+.3f}, {c['ci95'][1]:+.3f}] | "
                         f"{'yes' if c['significant'] else 'no'} |" for c in comparisons)
    print("\n" + md)
    if not args.no_save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (RESULTS_DIR / f"retrieval_{args.split}_{stamp}.json").write_text(json.dumps(results, indent=2))
        (RESULTS_DIR / f"retrieval_{args.split}_latest.md").write_text(md + "\n")
    if args.min_hit5 is not None and results[0]["hit@5"] < args.min_hit5:
        print(f"FAIL: {results[0]['config']} hit@5 {results[0]['hit@5']} < {args.min_hit5}")
        sys.exit(1)


if __name__ == "__main__":
    main()
