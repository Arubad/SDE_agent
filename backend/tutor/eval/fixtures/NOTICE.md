# Notice for the committed eval fixtures

`backend/tutor/eval/fixtures/ingestion/` holds pages of the Kubernetes documentation (kubernetes.io), which is
licensed under Creative Commons Attribution 4.0 (CC BY 4.0): https://creativecommons.org/licenses/by/4.0/.
Copyright The Kubernetes Authors; see https://kubernetes.io/docs/contribute/style/content-organization/ and
https://github.com/kubernetes/website/blob/main/LICENSE. The pages are stored as fetched (HTML), possibly trimmed,
for use as an evaluation fixture. No other changes are made to their text.

The six pages here were captured with `python -m backend.tutor.eval.capture_fixtures --roadmap kubernetes` from
the live ingestion of 2026-11-23 (see `docs/platform/ingestion-report-kubernetes.md`), as fetched on that day.

Third-party pages, video transcripts and YouTube data are NOT redistributed in this repository: they are written
to the git-ignored `backend/tutor/eval/cache/` and the eval reports them as `skipped: not cached` when absent.
