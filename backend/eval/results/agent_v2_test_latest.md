Set **v2**, split **test**, premium tier (Claude Haiku 5.5 generator and judge).

**multi_hop** (n=33)

| variant | correctness | faithfulness | evidence_recall | model_turns | tool_calls | input_tokens | output_tokens | p50 latency | errors |
|---|---|---|---|---|---|---|---|---|---|
| pipeline | 0.85 | 0.89 | 0.85 | 3.6 | 3.3 | 14447 | 3371 | 22.5 s | 0 |
| pipeline_noverify | 0.86 | 0.71 | 0.85 | 2.0 | 2.6 | 6002 | 1459 | 8.7 s | 0 |
| loop | 0.86 | 0.73 | 0.82 | 2.0 | 2.2 | 9969 | 1287 | 8.1 s | 0 |

**unanswerable** (n=7)

| variant | abstained | false_support | model_turns | tool_calls | input_tokens | output_tokens | p50 latency | errors |
|---|---|---|---|---|---|---|---|---|
| pipeline | 1.00 | 0.00 | 3.6 | 2.9 | 13331 | 2388 | 17.3 s | 0 |
| pipeline_noverify | 1.00 | 0.00 | 2.0 | 2.3 | 5893 | 1106 | 8.0 s | 0 |
| loop | 1.00 | 0.00 | 3.0 | 3.0 | 19500 | 850 | 7.8 s | 0 |

Paired bootstrap, loop − pipeline (10k resamples, questions answered by both):

| kind | metric | n | delta | 95% CI | significant |
|---|---|---|---|---|---|
| multi_hop | correctness | 33 | +0.015 | [-0.061, +0.091] | no |
| multi_hop | faithfulness | 33 | -0.167 | [-0.258, -0.061] | yes |
| multi_hop | evidence_recall | 33 | -0.030 | [-0.106, +0.045] | no |
| unanswerable | abstained | 7 | +0.000 | [+0.000, +0.000] | no |
