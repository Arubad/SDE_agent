# Ingestion eval

Run 20261123T142354Z (full; embedder bge; no LLM calls). 6 pages and 9 videos scored, 0 skipped.

| Metric | Value |
|---|---|
| Video discovery precision / recall | 1.0 / 1.0 |
| Playlist discovery precision / recall | 1.0 / 1.0 |
| Extraction: must_contain found | 0.9444 |
| Extraction: must_not_contain leakage | 0.0 |
| Segment route accuracy | 1.0 (9 videos) |

Boundary F1 at +-30 s, creator chapters hidden, per route that fired:

| Route | F1 | Precision | Recall | Videos |
|---|---|---|---|---|
| transcript_split | 0.2358 | 0.2855 | 0.2569 | 6 |
