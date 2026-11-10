Split: **dev** (n=55)

| config | hit@1 | hit@5 | MRR@10 | doc hit@5 | p50 latency |
|---|---|---|---|---|---|
| legacy_llamaindex | 0.53 | 0.78 | 0.63 | 0.82 | 17 ms |
| v1_dense | 0.51 | 0.74 | 0.61 | 0.78 | 170 ms |
| v1_bm25 | 0.66 | 0.80 | 0.72 | 0.80 | 147 ms |
| v1_hybrid | 0.62 | 0.87 | 0.71 | 0.87 | 172 ms |
| v1_hybrid_rerank | 0.56 | 0.84 | 0.70 | 0.84 | 812 ms |
| v2_dense | 0.64 | 0.85 | 0.73 | 0.91 | 165 ms |
| v2_bm25 | 0.64 | 0.80 | 0.69 | 0.82 | 143 ms |
| v2_hybrid | 0.66 | 0.85 | 0.74 | 0.89 | 167 ms |
| v2_hybrid_rerank | 0.69 | 0.87 | 0.77 | 0.91 | 675 ms |
| v2_hybrid_rerank_bge | 0.73 | 0.91 | 0.82 | 0.94 | 2136 ms |
