"""Agent eval: pipeline vs tool-use loop on single-hop, multi-hop and unanswerable questions.

Metrics (premium tier, Claude Haiku 5.5 as generator and judge):
  correctness / faithfulness   LLM judge (same rubric as answer_eval)            [answerable kinds]
  evidence_recall              share of evidence quotes present in the passages the generator saw
  abstained / false_support    says the sources don't cover it / answers anyway while citing sources [unanswerable]
  trajectory                   model turns, tool calls, tokens, latency

    python -m backend.eval.agent_eval --set v2 --split test --variants pipeline pipeline_noverify loop
    python -m backend.eval.agent_eval --set v1 --split test --variants pipeline loop
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import datetime, timezone

from backend.core import config
from backend.eval.common import RESULTS_DIR, contains_evidence, load_questions, load_questions_v2, paired_bootstrap_delta

VARIANTS = {
    "pipeline": {"mode": "pipeline", "verify": True},
    "pipeline_noverify": {"mode": "pipeline", "verify": False},
    "loop": {"mode": "loop", "verify": False},
}


def evidence_recall(quotes: list[str], texts: list[str]) -> float:
    if not quotes:
        return 0.0
    return sum(any(contains_evidence(t, q) for t in texts) for q in quotes) / len(quotes)

ABSTAIN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["states_not_covered", "rationale"],
                  "properties": {"states_not_covered": {"type": "boolean"}, "rationale": {"type": "string"}}}
ABSTAIN_SYSTEM = ("You check a tutor's answer to a question its knowledge base does not cover. Decide whether the answer "
                  "clearly tells the user that the knowledge base or its sources do not cover the question (it may still "
                  "offer clearly labeled general knowledge). Give a one-sentence rationale.")


def load_set(name: str, split: str) -> list[dict]:
    if name == "v2":
        return load_questions_v2(split)
    return [{**q, "kind": "single_hop", "evidence": [{"quote": q["evidence_quote"], "doc_id": q["gold_doc_id"]}]}
            for q in load_questions(split)]


async def run_one(q: dict, variant: str, sem: asyncio.Semaphore) -> dict:
    from backend.agent.orchestrator import run_agent
    from backend.core.llm import LLMUnavailable, haiku_json
    from backend.core.trace import start_trace
    from backend.eval.answer_eval import JUDGE_SCHEMA, JUDGE_SYSTEM, chunk_texts

    async with sem:
        trace = start_trace(f"agent_eval:{variant}")

        async def emit(_):
            return None

        base = {"id": q["id"], "kind": q["kind"], "variant": variant}
        try:
            res = await asyncio.wait_for(run_agent(q["question"], [], "premium", trace, emit), timeout=240)
        except (LLMUnavailable, asyncio.TimeoutError) as e:
            return {**base, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        summary = trace.summary()  # taken before judging, so these are the agent's own numbers
        texts = await asyncio.to_thread(chunk_texts, res.context_ids)
        row = {
            **base, "route": res.route, "n_sources": len(res.sources),
            "evidence_recall": evidence_recall([e["quote"] for e in q.get("evidence", [])], texts),
            "model_turns": sum(s["kind"] == "llm" for s in summary["spans"]),
            "tool_calls": sum(s["name"].startswith("mcp:") for s in summary["spans"]),
            "input_tokens": summary["input_tokens"], "output_tokens": summary["output_tokens"],
            "latency_ms": summary["total_ms"],
        }
        try:
            if q["kind"] == "unanswerable":
                v = await asyncio.to_thread(haiku_json, system=ABSTAIN_SYSTEM, schema=ABSTAIN_SCHEMA, effort="low",
                                            span_name="judge_abstain",
                                            prompt=f"Question: {q['question']}\n\nAnswer:\n{res.reply}")
                row.update(abstained=bool(v["states_not_covered"]),
                           false_support=(not v["states_not_covered"]) and row["n_sources"] > 0, rationale=v["rationale"])
            else:
                ctx = "\n\n".join(f"<source>\n{t}\n</source>" for t in texts) or "(no sources were retrieved)"
                v = await asyncio.to_thread(
                    haiku_json, system=JUDGE_SYSTEM, schema=JUDGE_SCHEMA, effort="medium", span_name="judge",
                    prompt=(f"Question: {q['question']}\n\nReference answer: {q['reference_answer']}\n\n"
                            f"All sources retrieved for the answer:\n{ctx}\n\nAnswer to grade:\n{res.reply}"))
                row.update(correctness=v["correctness"], faithfulness=v["faithfulness"], rationale=v["rationale"])
        except LLMUnavailable as e:
            return {**base, "error": f"judge: {str(e)[:200]}"}
        return row


async def run_variant(variant: str, questions: list[dict], concurrency: int) -> list[dict]:
    v = VARIANTS[variant]
    config.AGENT_MODE, config.VERIFY_ANSWERS, config.SEMANTIC_CACHE = v["mode"], v["verify"], False
    sem = asyncio.Semaphore(concurrency)
    return await asyncio.gather(*(run_one(q, variant, sem) for q in questions))


def _mean(rows: list[dict], key: str) -> float | None:
    vals = [float(r[key]) for r in rows if key in r]
    return round(statistics.mean(vals), 3) if vals else None


KIND_METRICS = {
    "single_hop": ["correctness", "faithfulness", "evidence_recall"],
    "multi_hop": ["correctness", "faithfulness", "evidence_recall"],
    "unanswerable": ["abstained", "false_support"],
}
TRAJECTORY = ["model_turns", "tool_calls", "input_tokens", "output_tokens"]


def to_markdown(rows: list[dict], set_name: str, split: str, variants: list[str]) -> str:
    ok = [r for r in rows if "error" not in r]
    out = [f"Set **{set_name}**, split **{split}**, premium tier (Claude Haiku 5.5 generator and judge)."]
    for kind, metrics in KIND_METRICS.items():
        krows = [r for r in ok if r["kind"] == kind]
        if not krows:
            continue
        n = len({r["id"] for r in krows})
        cols = metrics + TRAJECTORY + ["p50 latency"]
        out += ["", f"**{kind}** (n={n})", "", "| variant | " + " | ".join(cols) + " | errors |",
                "|---" * (len(cols) + 2) + "|"]
        for v in variants:
            vr = [r for r in krows if r["variant"] == v]
            if not vr:
                continue
            cells = [f"{_mean(vr, m):.2f}" if _mean(vr, m) is not None else "–" for m in metrics]
            cells += [f"{_mean(vr, t):.1f}" if t in ("model_turns", "tool_calls") else f"{round(_mean(vr, t) or 0)}"
                      for t in TRAJECTORY]
            cells.append(f"{statistics.median(r['latency_ms'] for r in vr) / 1000:.1f} s")
            errs = sum(1 for r in rows if r["kind"] == kind and r["variant"] == v and "error" in r)
            out.append(f"| {v} | " + " | ".join(cells) + f" | {errs} |")

    if "loop" in variants and "pipeline" in variants:
        out += ["", "Paired bootstrap, loop − pipeline (10k resamples, questions answered by both):", "",
                "| kind | metric | n | delta | 95% CI | significant |", "|---|---|---|---|---|---|"]
        for kind, metrics in KIND_METRICS.items():
            pipe = {r["id"]: r for r in ok if r["kind"] == kind and r["variant"] == "pipeline"}
            loop = {r["id"]: r for r in ok if r["kind"] == kind and r["variant"] == "loop"}
            ids = sorted(set(pipe) & set(loop))
            for m in (metrics if kind != "unanswerable" else ["abstained"]):
                if not ids:
                    continue
                d = paired_bootstrap_delta([float(pipe[i][m]) for i in ids], [float(loop[i][m]) for i in ids])
                out.append(f"| {kind} | {m} | {len(ids)} | {d['delta']:+.3f} | [{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}] | "
                           f"{'yes' if d['significant'] else 'no'} |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="set_name", default="v2", choices=["v1", "v2"])
    ap.add_argument("--split", default="test", choices=["all", "dev", "test"])
    ap.add_argument("--variants", nargs="+", default=["pipeline", "loop"], choices=list(VARIANTS))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    questions = load_set(args.set_name, args.split)[: args.limit]
    rows = []
    for v in args.variants:
        rows += asyncio.run(run_variant(v, questions, args.concurrency))
    md = to_markdown(rows, args.set_name, args.split, args.variants)
    print(md)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RESULTS_DIR / f"agent_{args.set_name}_{args.split}_{stamp}.json").write_text(json.dumps(rows, indent=2, default=str))
    if args.limit is None:
        (RESULTS_DIR / f"agent_{args.set_name}_{args.split}_latest.md").write_text(md + "\n")


if __name__ == "__main__":
    main()
