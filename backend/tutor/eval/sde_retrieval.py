"""Retrieval on the platform index over the demo's questions: the platform's parent-child chunks vs the demo's v2.

For each answerable question of `backend/eval/data/questions_v2.jsonl` in the split (unanswerable questions have
no evidence quote and are skipped), run `PlatformIndex.search(question, roadmap_id=sde-mastery, top_k=10)` and
score with the demo's evidence-quote matcher (`backend.eval.common.contains_evidence`, imported, not copied):

  child    a hit counts when its child text contains an evidence quote
  parent   a hit counts when its parent section text (Postgres) contains an evidence quote

Reported: hit@1, hit@5, MRR@10 and n. The v2 row is the `v2_hybrid` line of the demo's
`backend/eval/results/retrieval_<split>_latest.md` (hit@1, hit@5, MRR@10, doc hit@5; the demo scores 1 hit per
chunk it returns on its own chunking, v2 doc hit@5 is looser), shown for reference, not a controlled A/B.
No LLM calls. Needs real bge embeddings (EMBEDDER=bge, the default) and the roadmap imported first:

    python -m backend.tutor import-sde
    python -m backend.tutor.eval.sde_retrieval --split test
    python -m backend.tutor.eval.sde_retrieval --split test --import-first   # one process, same index"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, select

from backend.core import config
from backend.eval.common import contains_evidence, load_questions_v2
from backend.tutor.db import session_scope
from backend.tutor.ingest.index import PlatformIndex, get_parents
from backend.tutor.models import Roadmap

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
TOP_K = 10


def first_rank(hits, quotes: list[str], parent_texts: dict[str, str], level: str) -> int | None:
    """1-based rank of the first hit matching any quote at `level` ('child' or 'parent'), else None."""
    for rank, h in enumerate(hits, 1):
        text = h.text if level == "child" else parent_texts.get(h.payload.get("parent_id", ""), "")
        if any(contains_evidence(text, q) for q in quotes):
            return rank
    return None


def summarize(ranks: list[int | None]) -> dict:
    n = len(ranks)

    def mean(xs):
        return round(sum(xs) / n, 4) if n else 0.0
    return {"n": n, "hit@1": mean([r == 1 for r in ranks]), "hit@5": mean([r is not None and r <= 5 for r in ranks]),
            "mrr@10": mean([1 / r if r and r <= 10 else 0 for r in ranks])}


def score_questions(engine: Engine, index: PlatformIndex, roadmap_id: str, questions: list[dict]) -> dict:
    child, parent, skipped, per_q = [], [], 0, []
    for q in questions:
        quotes = [e["quote"] for e in q.get("evidence", []) if e.get("quote")]
        if not quotes:
            skipped += 1
            continue
        hits = index.search(q["question"], roadmap_id=roadmap_id, top_k=TOP_K)
        pids = list(dict.fromkeys(h.payload.get("parent_id", "") for h in hits))
        ptext = {s.id: s.text for s in get_parents(engine, pids)}
        rc, rp = first_rank(hits, quotes, ptext, "child"), first_rank(hits, quotes, ptext, "parent")
        child.append(rc)
        parent.append(rp)
        per_q.append({"id": q.get("id"), "child_rank": rc, "parent_rank": rp})
    return {"child": summarize(child), "parent": summarize(parent), "skipped_no_evidence": skipped,
            "questions": per_q}


def v2_row(split: str, results_dir: Path | None = None) -> dict | None:
    """The `v2_hybrid` row of the demo's retrieval result file for the split (None when absent)."""
    path = (results_dir or config.BACKEND_DIR / "eval" / "results") / f"retrieval_{split}_latest.md"
    if not path.exists():
        return None
    md = path.read_text(encoding="utf-8")
    m = re.search(r"\(n=(\d+)\)", md)
    for line in md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == "v2_hybrid" and len(cells) >= 5:
            return {"n": int(m.group(1)) if m else None, "hit@1": float(cells[1]), "hit@5": float(cells[2]),
                    "mrr@10": float(cells[3]), "doc_hit@5": float(cells[4])}
    return None


def format_markdown(result: dict) -> str:
    c, p, v = result["child"], result["parent"], result.get("v2_hybrid")
    rows = [("platform child (hybrid)", c), ("platform parent (hybrid)", p)]
    if v:
        rows.append(("demo v2_hybrid", v))
    lines = [f"Split: **{result['split']}** (n={c['n']}, {result['skipped_no_evidence']} without evidence skipped; "
             f"embedder `{result['embedder']}`, collection `{result['collection']}`)", "",
             "| config | hit@1 | hit@5 | MRR@10 |", "|---|---|---|---|"]
    lines += [f"| {name} | {r['hit@1']:.2f} | {r['hit@5']:.2f} | {r['mrr@10']:.2f} |" for name, r in rows]
    lines += ["", "The demo row is its own chunking and index (`sde_kb_v2`); the platform rows re-chunk the same "
              "documents with the parent-child scheme, so the comparison is indicative, not a controlled A/B."]
    return "\n".join(lines) + "\n"


def write_results(result: dict, out_dir: Path = RESULTS_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    js, md = out_dir / f"sde_retrieval_{result['split']}_{stamp}.json", out_dir / f"sde_retrieval_{result['split']}_latest.md"
    js.write_text(json.dumps(result, indent=2), encoding="utf-8")
    md.write_text(format_markdown(result), encoding="utf-8")
    return js, md


def main(argv: list[str] | None = None) -> int:
    from backend.tutor.db import make_engine, migrate
    from backend.tutor.ingest.embed import make_embedder
    from backend.tutor.ingest.import_sde import SLUG, import_sde
    from backend.tutor.ingest.index import make_index
    from backend.tutor.settings import load_settings

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--split", default="test")
    p.add_argument("--import-first", action="store_true",
                   help="import sde-mastery into the same in-process index first (e.g. PLATFORM_QDRANT_URL=:memory:)")
    p.add_argument("--limit", type=int, help="with --import-first: import only the first N documents")
    p.add_argument("--max-questions", type=int, help="score only the first N questions")
    p.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    args = p.parse_args(argv)
    settings = load_settings()
    settings.validate()
    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        index = make_index(settings, make_embedder(settings))
        if args.import_first:
            print(json.dumps(import_sde(engine, index, limit=args.limit)))
        with session_scope(engine) as s:
            rm = s.scalar(select(Roadmap).where(Roadmap.slug == SLUG))
            rid = str(rm.id) if rm else None
        if rid is None:
            p.error("roadmap 'sde-mastery' not found: run `python -m backend.tutor import-sde` (or use --import-first)")
        questions = load_questions_v2(args.split)[: args.max_questions]
        result = {"split": args.split, "roadmap_id": rid, "embedder": settings.embedder,
                  "collection": settings.collection, "qdrant_url": settings.qdrant_url,
                  **score_questions(engine, index, rid, questions), "v2_hybrid": v2_row(args.split)}
        result["questions_total"] = len(questions)
        js, md = write_results(result, args.out_dir)
        print(format_markdown(result))
        print(f"wrote {js} and {md}")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
