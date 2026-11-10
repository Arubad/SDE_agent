"""End-to-end answer evaluation of the agent, judged by Claude Haiku 5.5.

Runs the real agent (router -> MCP tools -> generator [-> verifier]) on eval questions and scores:
  correctness   (0-1) answer agrees with the reference answer         [LLM judge]
  faithfulness  (0-1) answer's claims are supported by retrieved text  [LLM judge]
  gold_cited    cited sources include the document the question came from
  plus latency, LLM calls and tokens per answer.

Variants form an ablation, e.g. premium (with verifier) vs premium_noverify.
Caveat: the judge is the same model family as the generator; treat scores as relative, not absolute.

    python -m backend.eval.answer_eval --split test --variants premium premium_noverify
"""
import argparse
import asyncio
import json
import statistics
from datetime import datetime, timezone

from backend.agent.orchestrator import run_agent
from backend.core import config
from backend.core.llm import LLMUnavailable, haiku_json
from backend.core.trace import start_trace
from backend.eval.common import RESULTS_DIR, load_questions
from backend.rag.store import qdrant

VARIANTS = {
    "premium": {"tier": "premium", "verify": True},
    "premium_noverify": {"tier": "premium", "verify": False},
    "public": {"tier": "public", "verify": False},
}

JUDGE_SYSTEM = """You grade answers from a software engineering tutor. Be strict and consistent.
correctness: does the answer convey the facts in the reference answer without contradicting it?
  1.0 fully, 0.5 partially or with a notable omission/error, 0.0 wrong or missing.
faithfulness: are the answer's factual claims supported by the retrieved sources (cited or not)? Claims explicitly
  labeled "(general knowledge)" are exempt. 1.0 all supported, 0.5 some unsupported, 0.0 mostly unsupported.
Give a one-sentence rationale."""

JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["correctness", "faithfulness", "rationale"],
    "properties": {"correctness": {"type": "number", "enum": [0, 0.5, 1]},
                   "faithfulness": {"type": "number", "enum": [0, 0.5, 1]},
                   "rationale": {"type": "string"}},
}


def chunk_texts(chunk_ids: list[str]) -> list[str]:
    if not chunk_ids:
        return []
    pts = qdrant().retrieve(config.KB_COLLECTION, ids=chunk_ids, with_payload=["text"])
    return [p.payload.get("text", "") for p in pts]


async def answer_one(q: dict, variant: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        trace = start_trace("eval")

        async def emit(_):
            return None

        try:
            res = await asyncio.wait_for(run_agent(q["question"], [], variant["tier"], trace, emit), timeout=240)
        except (LLMUnavailable, asyncio.TimeoutError) as e:
            return {"id": q["id"], "error": str(e)[:200]}
        summary = trace.summary()
        sources = res.sources
        texts = await asyncio.to_thread(chunk_texts, res.context_ids)
        ctx = "\n\n".join(f"<source>\n{t}\n</source>" for t in texts) or "(no sources were retrieved)"
        prompt = (f"Question: {q['question']}\n\nReference answer: {q['reference_answer']}\n\n"
                  f"All sources retrieved for the answer:\n{ctx}\n\nAnswer to grade:\n{res.reply}")
        verdict = await asyncio.to_thread(haiku_json, system=JUDGE_SYSTEM, prompt=prompt, schema=JUDGE_SCHEMA,
                                          effort="medium", span_name="judge")
        return {
            "id": q["id"], "correctness": verdict["correctness"], "faithfulness": verdict["faithfulness"],
            "rationale": verdict["rationale"],
            "gold_cited": any(s.get("doc_id") == q["gold_doc_id"] for s in sources),
            "n_sources": len(sources), "route": res.route,
            "verdict": (res.verification or {}).get("verdict"), "retried": bool((res.verification or {}).get("retried")),
            # summary was taken before the judge call, so these are the agent's own numbers
            "latency_ms": summary["total_ms"], "llm_calls": summary["llm_calls"],
            "input_tokens": sum(s.get("input_tokens", 0) for s in summary["spans"] if s["name"] != "judge"),
            "output_tokens": sum(s.get("output_tokens", 0) for s in summary["spans"] if s["name"] != "judge"),
        }


async def run_variant(name: str, questions: list[dict], concurrency: int) -> dict:
    v = VARIANTS[name]
    config.VERIFY_ANSWERS = v["verify"]
    config.SEMANTIC_CACHE = False  # never let cached answers leak between variants
    sem = asyncio.Semaphore(concurrency)
    rows = await asyncio.gather(*(answer_one(q, v, sem) for q in questions))
    ok = [r for r in rows if "error" not in r]
    mean = lambda k: round(statistics.mean(r[k] for r in ok), 3) if ok else None
    return {
        "variant": name, "n": len(ok), "errors": len(rows) - len(ok),
        "correctness": mean("correctness"), "faithfulness": mean("faithfulness"), "gold_cited": mean("gold_cited"),
        "retry_rate": mean("retried"), "avg_llm_calls": mean("llm_calls"),
        "avg_input_tokens": round(mean("input_tokens") or 0), "avg_output_tokens": round(mean("output_tokens") or 0),
        "p50_latency_s": round(statistics.median(r["latency_ms"] for r in ok) / 1000, 1) if ok else None,
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test", choices=["all", "dev", "test"])
    ap.add_argument("--variants", nargs="+", default=["premium", "premium_noverify"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    questions = load_questions(args.split)[: args.limit]
    results = [asyncio.run(run_variant(v, questions, args.concurrency)) for v in args.variants]

    lines = [f"Split: **{args.split}** (n={len(questions)}), judge: Claude Haiku 5.5", "",
             "| variant | correctness | faithfulness | gold source cited | retry rate | LLM calls | tokens in/out | p50 latency |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['variant']} | {r['correctness']:.2f} | {r['faithfulness']:.2f} | {r['gold_cited']:.2f} | "
                     f"{r['retry_rate']:.2f} | {r['avg_llm_calls']:.1f} | {r['avg_input_tokens']}/{r['avg_output_tokens']} | "
                     f"{r['p50_latency_s']} s |" + (f" ({r['errors']} errors)" if r["errors"] else ""))
    md = "\n".join(lines)
    print(md)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RESULTS_DIR / f"answers_{args.split}_{stamp}.json").write_text(json.dumps(results, indent=2, default=str))
    (RESULTS_DIR / f"answers_{args.split}_latest.md").write_text(md + "\n")


if __name__ == "__main__":
    main()
