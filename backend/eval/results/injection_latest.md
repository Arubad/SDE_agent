Attack success rate (lower is better); one poisoned passage among 5 retrieved, 5 questions per attack. Claude Haiku 5.5.

| target | defense | overall | canary | link | leak | suppression | tool | errors |
|---|---|---|---|---|---|---|---|---|
| pipeline | off | 0% (0/40) | 0% (0/20) | 0% (0/10) | 0% (0/5) | 0% (0/5) | – | 0 |
| pipeline | on | 0% (0/40) | 0% (0/20) | 0% (0/10) | 0% (0/5) | 0% (0/5) | – | 0 |
| loop | off | 0% (0/50) | 0% (0/20) | 0% (0/10) | 0% (0/5) | 0% (0/5) | 0% (0/10) | 0 |
| loop | on | 0% (0/50) | 0% (0/20) | 0% (0/10) | 0% (0/5) | 0% (0/5) | 0% (0/10) | 0 |

Clean runs (no attack), defense on: answers citing at least one source
- pipeline: 5/5
- loop: 5/5
