# Ingestion report: Kubernetes roadmap (live run, 2026-11-23)

The owner's Kubernetes roadmap (`backend/tutor/tests/fixtures/roadmaps/kubernetes.txt`, pasted as-is) was ingested
end to end on a laptop. The setup:

- **Services:** Postgres 16, Qdrant and SearXNG (DuckDuckGo, Brave and YouTube engines).
- **Model:** Claude Haiku 5.5 via Foundry, for parsing and for the ingestion agent.
- **Embeddings:** local bge.
- **Transcripts:** the default captions route only.
- **Agent:** on, with its $0.05 budget.
- **Keys:** `YOUTUBE_API_KEY` and a fine-grained `GITHUB_TOKEN` were set.

This is the second run. The first run was stopped part-way because of a crawler defect, described at the end. The
numbers below are from the second run, on commit `5df03f7`.

## Summary

```
status: ready
13 resources: 12 done, 1 linked_only, 0 skipped
documents: 58 pages (3,114 chunks), 9 videos (632 chunks), 4 repo READMEs (57 chunks)
9 videos: 5 creator chapters, 0 comment timestamps, 3 transcript-split, 1 whole; transcripts 9/9 (captions 9)
Agent: 23 actions over a plan pass and 2 recovery rounds, $0.0075
```

Wall-clock time was about 15 minutes from approval to `ready`, including the two videos approved after the
recovery round.

## Parse and resolve

Haiku turned the pasted text into 4 stages, 17 skills and 8 resources. Each resource was resolved by searching
(SearXNG) for the title the roadmap names. I approved the top candidate for each, with three exceptions:

| Resource | Resolver | What I approved |
|---|---|---|
| Kubernetes Roadmap – Complete Step-by-Step Learning Path (video) | resolved | top candidate `S8eX0MxfnB4` |
| Kubernetes Tutorial for Beginners [FULL COURSE in 4 Hours] (video) | **needs_url**, no candidates | **pasted** `X48VuDVv0do`. The first run's search had found it; this run's YouTube search returned nothing. |
| Kubernetes Official Documentation – Concepts & Tutorials (doc) | resolved to `/docs/` | **pasted** `https://kubernetes.io/docs/concepts/`. The roadmap points at the "Components of a Cluster" and "Workloads and Pods" pages, and both live under Concepts. |
| Killercoda Interactive Environments (interactive) | linked_only | `https://killercoda.com/courses/kubernetes`. It is linked only: interactive sites are not crawled. |
| The Kubernetes Course 2025 (video) | resolved | top candidate `EV47Oxwet6Y` |
| Kubernetes Tutorial: From Zero to Deployed (video) | resolved | top candidate `IluVqhoCChw` |
| Kubernetes Official Tutorials (doc) | resolved to `/docs/tutorials/` | **chose** `/docs/tutorials/stateless-application/`, the walkthrough the roadmap names. It was the top candidate in the first run, where the resolver marked it needs_url (score 0.70). |
| Certified Kubernetes Administrator Exam Preparation (video) | resolved | top candidate `l57xKN6OBhY` |

The two parses did not produce identical specs (the resource ids differed, `res-p1-r1` vs `res-r1`), and SearXNG
returned different candidates between runs. Both are expected: the model is not deterministic and the search engines
change. The resources and skills found were the same.

## Resources

| Resource | Kind | Status | Pages | Videos | Chunks |
|---|---|---|---:|---:|---:|
| Kubernetes Roadmap – Complete Step-by-Step Learning Path | video | done | 0 | 1 | 22 |
| Kubernetes Tutorial for Beginners [FULL COURSE in 4 Hours] | video | done | 0 | 1 | 127 |
| Kubernetes Official Documentation – Concepts & Tutorials | doc | done | 50 | 2 | 3,020 |
| Killercoda Interactive Environments | interactive | linked_only | 0 | 0 | 0 |
| The Kubernetes Course 2025 | video | done | 0 | 1 | 270 |
| Kubernetes Tutorial: From Zero to Deployed (100% FREE) | video | done | 0 | 1 | 31 |
| Kubernetes Official Tutorials (stateless application) | doc | done | 8 | 0 | 101 |
| Certified Kubernetes Administrator Exam Preparation | video | done | 0 | 1 | 98 |
| *agent:* StatefulSets (kubernetes.io) | article | done | 1 | 0 | 43 |
| *agent:* Resource Management for Pods and Containers (kubernetes.io) | article | done | 1 | 0 | 23 |
| *agent:* Debug Running Pods (kubernetes.io) | article | done | 2 | 0 | 35 |
| *agent:* Kubernetes Monitoring Basics (video `tgVlRCYLRRM`) | video | done | 0 | 1 | 1 |
| *agent:* Kubernetes Requests and Limits (video `1nAsxJSsfkw`) | video | done | 0 | 1 | 32 |

The Concepts crawl used its whole 50-page budget, following same-site links to depth 2, and found 2 embedded
videos. **2,642 of its 3,020 chunks came from one page:** `/docs/concepts/_print/`, the "Print entire section" view,
which repeats the whole section on a single 304k-word page (see "Defects found" below). The tutorial crawl and one agent page linked 4 GitHub repos between them (`kubernetes/website`,
`kubernetes/kubernetes`, `kubernetes-sigs/metrics-server` and `GoogleContainerTools/distroless`). Each contributed
its README only.

## Videos

| Video | Resource | Segments from | Transcript |
|---|---|---|---|
| `S8eX0MxfnB4` | Roadmap video | creator chapters | captions |
| `X48VuDVv0do` | 4-hour beginner course | creator chapters | captions |
| `IluVqhoCChw` | Zero to Deployed | creator chapters | captions |
| `EV47Oxwet6Y` | The Kubernetes Course 2025 | creator chapters | captions |
| `1nAsxJSsfkw` | Requests and Limits (agent) | creator chapters | captions |
| `l57xKN6OBhY` | CKA exam preparation | transcript split, **should have been creator chapters** (see below) | captions |
| `wqsUfvRyYpw` | embedded on a Concepts page | transcript split (no timestamps in the description) | captions |
| `EzSkU3Oecuw` | embedded on a Concepts page | transcript split (no timestamps in the description) | captions |
| `tgVlRCYLRRM` | Monitoring Basics (agent) | whole video | captions |

Notes:
- No video used comment timestamps. Six videos had top comments (the YouTube key works), but none of those comments
  held a valid chapter list.
- `l57xKN6OBhY` (freeCodeCamp) lists its chapters as `⌨️ 0:06:23 Kubernetes Fundamentals and Lab`. The timestamp
  parser accepted a timestamp only at the start of a line, so the emoji prefix made it miss all of them. This is
  fixed after the run (emoji and bullet prefixes are accepted). The ingestion eval re-scores segmentation from the
  captured raw data, so it measures the fixed parser. This run's index still holds the transcript split for that
  video until the roadmap is re-ingested.
- `tgVlRCYLRRM` is 85 seconds long, but its description lists chapters up to 4:45. The parser correctly rejected
  chapters that run past the end of the video and kept it as one segment (1 chunk).

## Skipped

Nothing was skipped. No login walls, paywalls, bot protection, robots.txt refusals or dead links came up.
Killercoda is `linked_only` by design: interactive sites are listed for the learner, not crawled.

## Ingestion agent

The agent ran three times, $0.0075 in total against the $0.05 budget:

| Run | Steps | Cost | Outcome |
|---|---:|---:|---|
| plan (before the crawl) | 4 | $0.0029 | proposed 3 kubernetes.io pages for skills no resource covered: StatefulSet (s12), debugging pods (s15), resource requests and limits (s16) |
| recover, round 1 | 4 | $0.0025 | proposed nothing; reported s17 (cluster observability) at 0 chunks and Killercoda as a gap it could not fill |
| recover, round 2 | 5 | $0.0021 | proposed 2 videos: observability (s17) and requests and limits (s16) |

The URL policy worked as designed:
- **Refused:** 6 of the agent's 23 actions. Its first three proposals used kubernetes.io URLs typed from memory, and
  two page peeks used URLs that came from neither a search result nor the roadmap. The agent then searched and
  proposed the URLs the search returned.
- **Error:** one proposal of a URL already in the roadmap.

Six of its searches returned 0 results. SearXNG's engines are flaky under repeated queries, and that is what limited
round 1.

**Proposals approved (all 5):**
- **StatefulSets, Resource Management and Debug Running Pods:** sensible. Each covers a roadmap skill with an
  official page.
- **Requests and Limits video:** sensible. 32 chunks, with creator chapters.
- **Monitoring Basics video:** weak in hindsight. It is 85 seconds long and gave 1 chunk. The agent had flagged that
  it had not checked the video's transcript or chapters. A duration check before proposing would have caught it.

None were dismissed.

## Cost

| Item | Cost |
|---|---:|
| Parse (Haiku), per run | ≈ $0.001 (about 1.8k input and 1.8k output tokens). Estimated from sizes: parse token usage is recorded only on tracing spans, and tracing was off. |
| Ingestion agent, run 2 | $0.0075 (recorded per run) |
| Ingestion agent, run 1 (stopped) | $0.0032 (plan pass only) |
| Embeddings, extraction, search, YouTube Data API | $0 (local models; the YouTube quota used is well under 1%) |

The total model spend for both runs was about $0.013.

## Ingestion eval

The labels in `backend/tutor/eval/data/ingestion_labels.json` were written by hand from the raw sources (the
fetched HTML, video descriptions and comments), not from the pipeline's output:
- **Pages:** 6 kubernetes.io pages, committed under `eval/fixtures/ingestion/` (CC BY 4.0, see `NOTICE.md`).
- **Videos:** all 9, kept in the git-ignored `eval/cache/`, because YouTube data is not redistributed.

Results, run `20261123T142354Z`, full mode, bge, no LLM calls:

| Metric | Value |
|---|---|
| Extraction: must_contain found | 0.94 (mean over 6 pages; 12 of 13 phrases) |
| Extraction: must_not_contain leakage | 0.0 |
| Segment route accuracy | 1.0 (9 videos; creator 6, transcript split 2, whole 1) |
| Boundary F1 at ±30 s, creator chapters hidden, transcript split | **0.24** (precision 0.29, recall 0.26, 6 videos) |
| Video / playlist discovery | 1.0 / 1.0, but vacuous: no labelled page embeds a video (see below) |

What these numbers say:
- **Extraction** keeps main content and drops navigation, footers and the edit or issue links. The one miss is
  real: the first paragraph of the `/docs/concepts/` section page, under its `<h1>`, is lost, and the extracted
  text starts with the first child page's summary.
- **Route choice** follows the spec's order on every video, once the emoji-prefix fix below is in.
- **The transcript split is weak.** When a video's own chapters are hidden, the topic-shift split recovers only
  about a quarter of the creator's chapter boundaries within ±30 s. It is a fallback for videos without chapters
  and should not be read as an equal substitute. A negative result.
- **Discovery is untested here.** The only crawled page that embedded videos was the print view, which is no
  longer crawled and was too large to commit (4.2 MB). The discovery metric is covered by the unit tests' fixture
  net, not by this live sample.

CI runs the same eval with `--ci`: the committed pages only and a hash embedder. Videos are reported as
`skipped: not cached`.

## Defects found by this run

Each was fixed and reviewed on the branch after the run. The local index still reflects run 2, so the roadmap
should be re-ingested before Phase 3 uses it.

1. **Repos linked from crawled pages were unbounded** (run 1, below). Fixed in `2b46fd3` and `5df03f7`.
2. **Print views were crawled** (`bb5de7e`). Docsy's "Print entire section" link (`/_print/`) was followed like any
   other page, so most of the Concepts index was a duplicate. Links whose path has a `_print` segment are no
   longer followed.
3. **Chapter timestamps after an emoji were missed** (`812227b`). Lines like `⌨️ 0:06:23 Title` were rejected.
   Timestamps may now follow a short prefix of symbols, such as emoji, bullets or arrows. Prose such as "At 3:00
   we…" still does not match.

Smaller issues, logged for the branch review:
- a proposed title kept the HTML entity `&amp;`;
- the section-index lead paragraph is lost in extraction;
- videos discovered on crawled pages have no per-resource cap. Only 2 were found here.

## Run 1: the crawler defect it found

The first run used the same roadmap and choices on commit `2f12586`. It was stopped after about 30 minutes:
- **What happened:** crawling `/docs/concepts/` had found **129 third-party GitHub repos** linked from those pages
  (ingress controllers, CSI drivers, Apache APISIX, Marathon and others). Each was pulling its README plus up to 30
  `docs/` files, which made 1,065 of 1,130 documents off-topic repo files, with about 2 hours of embedding left.
- **Why:** pages found by a crawl shared the resource's 50-page budget, but discovered repos did not.
- **Fix** (`2b46fd3`, `5df03f7`):
  - Same-site links reserve their slots first, then PDFs, then repos.
  - A repo found on a page costs one page slot, is capped at 5 per resource, and contributes its README only.
  - A resource whose own URL is a repo still gets its README and `docs/`.

In run 2 the Concepts crawl filled its budget with Kubernetes pages, and 4 READMEs came in from other resources.
