Split: **test** (n=33)

| config | hit@1 | hit@5 | MRR@10 | doc hit@5 | p50 latency |
|---|---|---|---|---|---|
| legacy_llamaindex | 0.48 | 0.79 | 0.65 | 0.79 | 17 ms |
| v1_dense | 0.52 | 0.79 | 0.62 | 0.85 | 170 ms |
| v1_bm25 | 0.64 | 0.88 | 0.74 | 0.88 | 147 ms |
| v1_hybrid | 0.61 | 0.88 | 0.71 | 0.88 | 169 ms |
| v1_hybrid_rerank | 0.61 | 0.88 | 0.72 | 0.88 | 572 ms |
| v2_dense | 0.64 | 0.91 | 0.77 | 0.88 | 167 ms |
| v2_bm25 | 0.64 | 0.88 | 0.74 | 0.91 | 146 ms |
| v2_hybrid | 0.79 | 0.97 | 0.85 | 1.00 | 166 ms |
| v2_hybrid_rerank | 0.61 | 0.91 | 0.76 | 0.94 | 366 ms |
| v2_hybrid_rerank_bge | 0.67 | 0.91 | 0.79 | 0.94 | 1183 ms |
| v2_hybrid_rerank_bge_c15 | 0.67 | 0.91 | 0.79 | 0.94 | 1182 ms |
