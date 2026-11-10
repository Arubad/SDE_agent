Set **v1**, split **test**, premium tier (Claude Haiku 5.5 generator and judge).

**single_hop** (n=33)

| variant | correctness | faithfulness | evidence_recall | model_turns | tool_calls | input_tokens | output_tokens | p50 latency | errors |
|---|---|---|---|---|---|---|---|---|---|
| pipeline | 0.94 | 0.89 | 0.97 | 3.5 | 3.0 | 12561 | 2682 | 17.5 s | 0 |
| loop | 0.94 | 0.77 | 1.00 | 2.0 | 2.0 | 9362 | 1074 | 7.5 s | 0 |

Paired bootstrap, loop − pipeline (10k resamples, questions answered by both):

| kind | metric | n | delta | 95% CI | significant |
|---|---|---|---|---|---|
| single_hop | correctness | 33 | +0.000 | [-0.045, +0.045] | no |
| single_hop | faithfulness | 33 | -0.121 | [-0.227, -0.015] | yes |
| single_hop | evidence_recall | 33 | +0.030 | [+0.000, +0.091] | no |
