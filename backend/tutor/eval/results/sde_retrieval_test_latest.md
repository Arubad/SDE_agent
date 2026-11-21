Split: **test**; embedder `bge`, collection `roadmap_chunks`

### Headline: demo questions (`questions.jsonl`, single `evidence_quote`, n=33), demo-identical scoring, with the demo's v2_hybrid row

| config | hit@1 | hit@5 | MRR@10 |
|---|---|---|---|
| platform child (hybrid) | 0.61 | 0.88 | 0.72 |
| platform parent (hybrid) | 0.67 | 0.94 | 0.77 |
| demo v2_hybrid | 0.79 | 0.97 | 0.85 |

### Second set: `questions_v2.jsonl` (multi-hop, hit if ANY evidence quote matches, n=33, 7 unanswerable skipped; no demo row)

| config | hit@1 | hit@5 | MRR@10 |
|---|---|---|---|
| platform child (hybrid) | 0.55 | 0.97 | 0.72 |
| platform parent (hybrid) | 0.70 | 1.00 | 0.82 |

The demo row is its own chunking and index (`sde_kb_v2`); the platform rows re-chunk the same documents with the parent-child scheme, so the comparison is indicative, not a controlled A/B.
