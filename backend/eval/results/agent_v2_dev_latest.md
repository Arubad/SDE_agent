Set **v2**, split **dev**, premium tier (Claude Haiku 5.5 generator and judge).

**multi_hop** (n=38)

| variant | correctness | faithfulness | evidence_recall | model_turns | tool_calls | input_tokens | output_tokens | p50 latency | errors |
|---|---|---|---|---|---|---|---|---|---|
| loop | 0.93 | 0.74 | 0.85 | 2.0 | 2.1 | 10133 | 1298 | 8.5 s | 0 |

**unanswerable** (n=23)

| variant | abstained | false_support | model_turns | tool_calls | input_tokens | output_tokens | p50 latency | errors |
|---|---|---|---|---|---|---|---|---|
| loop | 1.00 | 0.00 | 2.9 | 2.9 | 18816 | 818 | 8.4 s | 0 |
