Split: **all** (n=88)

| config | hit@1 | hit@5 | MRR@10 | doc hit@5 | p50 latency |
|---|---|---|---|---|---|
| legacy_llamaindex | 0.51 | 0.78 | 0.64 | 0.81 | 17 ms |
| v1_hybrid | 0.64 | 0.88 | 0.73 | 0.88 | 174 ms |
| v2_dense | 0.64 | 0.88 | 0.74 | 0.90 | 173 ms |
| v2_hybrid | 0.69 | 0.90 | 0.78 | 0.93 | 172 ms |
| v2_hybrid_rerank_bge_c15 | 0.70 | 0.91 | 0.80 | 0.94 | 1203 ms |

Paired bootstrap (10k resamples), delta = B - A:

| A | B | metric | delta | 95% CI | significant |
|---|---|---|---|---|---|
| legacy_llamaindex | v2_hybrid | mrr | +0.142 | [+0.053, +0.232] | yes |
| legacy_llamaindex | v2_hybrid | hit@1 | +0.182 | [+0.068, +0.307] | yes |
| legacy_llamaindex | v2_hybrid | hit@5 | +0.114 | [+0.023, +0.216] | yes |
| v2_hybrid | v2_hybrid_rerank_bge_c15 | mrr | +0.027 | [-0.034, +0.087] | no |
| v2_hybrid | v2_hybrid_rerank_bge_c15 | hit@1 | +0.011 | [-0.091, +0.114] | no |
| v2_hybrid | v2_hybrid_rerank_bge_c15 | hit@5 | +0.011 | [-0.034, +0.068] | no |
| v1_hybrid | v2_hybrid | mrr | +0.049 | [-0.013, +0.112] | no |
| v1_hybrid | v2_hybrid | hit@1 | +0.057 | [-0.023, +0.148] | no |
| v1_hybrid | v2_hybrid | hit@5 | +0.023 | [-0.034, +0.091] | no |
| v2_dense | v2_hybrid | mrr | +0.033 | [-0.013, +0.081] | no |
| v2_dense | v2_hybrid | hit@1 | +0.057 | [-0.023, +0.148] | no |
| v2_dense | v2_hybrid | hit@5 | +0.023 | [-0.034, +0.080] | no |
