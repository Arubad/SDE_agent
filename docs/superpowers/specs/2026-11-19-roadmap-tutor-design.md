# Roadmap Tutor: design

## Intent
SDE Mastery Hub is a RAG chatbot over one curated set of SDE resources. This work turns it into a
**roadmap tutor platform**: a learner writes down a roadmap (skills plus whatever links they want to
learn from: articles, docs, course sites with embedded YouTube videos, playlists, PDFs, GitHub repos),
an ingestion engine turns those links into a per-roadmap knowledge base, and a tutor agent takes the
learner through the roadmap session by session: it remembers where they left off, plans, teaches
from their own material plus the web, checks understanding and adapts.

Why: the owner's own use (learning new tech), real usefulness to anyone who runs it, and a portfolio
that shows production-grade agent engineering: tool calling, planning, failure recovery,
orchestration, memory, MCP, browser automation, evals, security, TypeScript, Postgres and Docker.

Note on naming: "tuned to your roadmap" means retrieval-grounded, not model fine-tuning.

## Who and where
- **One user per install, running locally** with `docker compose up` (macOS Apple Silicon is the primary
  dev machine, Linux in CI). No login, no public hosting. Several roadmaps per user.
- The existing Hugging Face Space (`app.py`, `backend/server.py`, `frontend/index.html`) stays live as the
  demo of the original SDE roadmap and must keep working unchanged. Concretely:
  - **Code:** shared modules (`backend/rag/store.py`, `backend/mcp_server.py`, `backend/agent/loop.py`, …) may be
    refactored and generalized as long as the demo behaves the same and the existing test suite and CI stay
    green. Platform-only code lives in new packages; `deploy_to_hf.py` ignores them (and `web/`) so they do
    not ship to the Space.
  - **Data:** the demo's Qdrant Cloud collection `sde_kb_v2` is **read-only** for the platform. The platform
    writes only to its own collection (local Qdrant by default).
  - **README:** one README for the whole project. The HF YAML frontmatter stays at the top, the demo keeps its
    own section, and the platform is documented after it.

## Goals
1. Accept a loosely structured roadmap and build a knowledge base from it, reporting honestly what it
   could and could not ingest.
2. A tutor that resumes each session, offers a recap, follows and adapts an approved study plan,
   teaches with citations, quizzes, reviews code, and can research new tech from the web.
3. Layered memory with privacy controls.
4. MCP everywhere: the platform exposes its own servers and consumes free external ones.
5. Evals for every agentic behaviour, with numbers in the README (including negative results).
6. Visible orchestration: a trace timeline in the app and an OpenTelemetry trace viewer.

## Non-goals
- Multi-user accounts, auth, public hosting, mobile.
- Paid services other than Claude Haiku calls (Gemini only as an opt-in free-tier transcript route).
- Model fine-tuning.
- Changing the HF demo's behaviour.

## Anti-goals (failure even if it "works")
- The agent entering flashcard review on its own.
- Ingestion failures that are silent: every resource and video ends in a reported state.
- Gray-area scraping routes (yt-dlp with cookies, audio download for Whisper) switched on by default.
- Bypassing paywalls, logins, CAPTCHAs or bot protection.
- Personal data or secrets in traces, logs or exported notes unredacted.
- A full eval run costing more than ~$2 of Haiku.
- Breaking the HF Space build (it pins `gradio[mcp]`, so `fastmcp>=3.4,<4` and `mcp<2`).

## Constraints
- Python 3.12 backend, reusing the existing code (`backend/rag`, `backend/agent`, `backend/mcp_server.py`,
  `backend/eval`). The new platform's extra dependencies go in a separate requirements file used only by
  the compose image, so the HF Space's `requirements.txt` is untouched.
- LLM: Claude Haiku 5.5 on Microsoft Foundry for every agent (`backend/core/llm.py`). Known Foundry limits:
  `tool_choice` only `auto`/`none`, no `thinking`/temperature/prefill, structured outputs and `effort`
  supported, append-only history. A deterministic **fake LLM provider** (`LLM_PROVIDER=fake`) is
  required so unit and E2E tests never call a model.
- The dependency pins `fastmcp>=3.4,<4` / `mcp<2` bind the compose image too, so there is one MCP code path.
  Phase 3 must first verify that the pinned `mcp` 1.x has progress notifications, elicitation and the client
  features the gateway needs. If one is missing, that feature is dropped and the README says so; the pin is
  not raised.
- **LangGraph is orchestration only.** Every model call goes through `backend/core/llm.py`, so it gets Foundry
  handling, the fake provider, tracing and prompt caching. Tools run through the gateway. No LangChain
  chat-model or `ToolNode` wrappers.
- Secrets only in `backend/.env` (never committed, never logged).
- Free services only, checked Nov 2026: SearXNG, DeepWiki, arXiv, Wikipedia, Fetch, Playwright MCP and
  Microsoft Learn MCP are free with no account; Context7's free tier was cut in 2026 and is optional;
  GitHub MCP is optional (free token).

## Approaches considered (decided with the owner)
- **Repo:** evolve this repo (chosen; history, evals and demo continuity) vs. a new repo.
- **Orchestration:** keep the hand-written tool loop (`backend/agent/loop.py`) for tool calling **and**
  build the multi-agent tutor as a **LangGraph** graph with a Postgres checkpointer (chosen; shows both
  the mechanics and the framework that job posts name). Rejected: hand-rolled only (no framework, more
  code) and Claude Agent SDK (less of the owner's own orchestration on show).
- **Ingestion orchestration:** a hand-written durable job queue in Postgres (my call: it is mostly
  deterministic work, and a queue with retries, idempotency and resume is a different orchestration skill
  from the LangGraph tutor).
- **Frontend:** React + Vite + TypeScript, served by the Python backend (chosen) vs. Next.js.
- **Sandbox:** one `run_code` tool with two backends: a locked-down local Docker container by default (fast,
  offline, CI-testable) and Google's official Colab MCP server when the user picks it or an exercise needs
  a GPU (chosen) vs. Colab only or Docker only.
- **Eval budget:** ≤ ~$2 per full run (chosen); the README states that the intervals are wider because of it.

## Architecture

### Services (docker compose)
| Service | Role |
|---|---|
| `api` | FastAPI app: REST + SSE, ingestion workers, tutor graph, MCP servers, serves the built web app |
| `web` | React + Vite + TS (dev server in dev; built static files served by `api` otherwise) |
| `postgres` | `pgvector/pgvector:pg16`: app data, job queue, LangGraph checkpoints, memory vectors |
| `qdrant` | Local Qdrant: chunk vectors (dense + BM25 sparse), as today; `QDRANT_URL` may point to cloud |
| `searxng` + `searxng-mcp` | Self-hosted search and its MCP server |
| `playwright-mcp` | Microsoft's Playwright MCP server (headless Chromium) used as the browser |
| `sandbox-runner` | The only service with Docker socket access; launches one fixed sandbox image with fixed flags |
| `phoenix` (profile `observability`) | Arize Phoenix trace viewer (one container, OpenTelemetry-native) |

Everything binds to `127.0.0.1`. Migrations via Alembic.

### Roadmap input
- The user pastes or uploads **Markdown or YAML**. Markdown is loose: headings become stages and skills,
  bullets with URLs become resources, free text becomes goals and notes. A YAML template is documented.
- A planner call turns it into a structured `RoadmapSpec` (stages → skills → resources, guessed
  prerequisites, goals, the user's weekly time). It is shown back for **approval and edits** before
  anything is ingested.
- The original SDE knowledge base becomes the built-in roadmap `sde-mastery`. It is **re-chunked from its
  source documents** (`backend/rag/documents.py` already reconstructs them) into the new parent-child scheme
  in the platform's own collection, so the platform works on day one. The existing retrieval questions still
  apply: their evidence-quote scoring does not depend on how the text is chunked, so v2-flat vs. platform
  parent-child is a fair comparison. The demo's CI gate (hit@5 on `sde_kb_v2`) is unchanged.
- A **second, real roadmap supplied by the owner** is the main test and demo roadmap for the ingestion,
  retrieval and tutor evals. Phase 2 evals wait for it; everything else can proceed without it.

### Ingestion engine (durable job queue)
- A Postgres `jobs` table; workers claim with `FOR UPDATE SKIP LOCKED`. Each job has an idempotency key
  (type + normalized URL + roadmap), attempts, exponential backoff, a dead-letter state, and parent/child
  links so a roadmap's ingestion is a job graph that resumes after a crash.
- Per-domain rate limits; robots.txt respected; crawl limited to the same site, depth ≤ 2 and ≤ 50 pages
  per resource by default (configurable).
- Job types:
  - `fetch_page`: HTTP first; Playwright MCP for JS-rendered pages. Main content to markdown (headings, code
    blocks and tables kept; navigation, ads, banners and footers removed).
  - `discover`: find links worth following, embedded videos (`iframe`, links, playlist URLs), PDFs and
    GitHub repos.
  - `video_meta`: YouTube Data API v3 (free key, optional) for title, description, tags, duration and
    top comments; oEmbed (title/channel only) without a key.
  - `expand_playlist`: playlist items in order.
  - `transcript`: routes in order, each switchable: (1) YouTube captions (on by default, throttled with
    retries); (2) yt-dlp with browser cookies (opt-in); (3) Gemini with the video URL (opt-in, official,
    uses the free quota); (4) local Whisper on the audio (opt-in); (5) none. The README states which
    routes are against YouTube's terms and that users are responsible for following them.
  - `segment_video`: chapters in order: description timestamps → timestamps in top comments → topic-shift
    split of the transcript (local embeddings, no LLM) → whole video as one unit.
  - `pdf`, `github_repo` (README + `docs/`), then `chunk_embed`.
- Every resource and video ends in a reported state. An **ingestion report** gives per-resource status
  and coverage, e.g. "42 videos: 18 creator chapters, 9 comment timestamps, 6 transcript-split, 9 whole;
  transcripts 30/42 (captions 24, Gemini 6)", plus a skipped list (login wall, paywall, bot protection,
  robots.txt).
- Re-check job (on demand or weekly) reports dead or changed links.

### Chunking and retrieval
- Split by heading hierarchy first, keeping a breadcrumb section path ("Primer › Caching ›
  Invalidation"); split long sections at semantic breakpoints (embedding similarity); merge tiny
  fragments; never split code blocks or tables; drop near-duplicates (existing shingle code).
- **Parent-child:** small child chunks (~200–350 tokens) are indexed; the parent section (≤ ~1,500 tokens)
  is stored in Postgres and given to the model as context.
- Video chunks carry timestamps so the tutor can say "watch 12:30–18:00".
- Existing hybrid search (bge-small dense + BM25, RRF in Qdrant) in one collection with a `roadmap_id`
  payload index (Qdrant's multitenancy pattern) and optional `skill_id` filter.

### Tutor (LangGraph)
- A `StateGraph` with a Postgres checkpointer, so a session survives restarts. Nodes:
  - **coordinator**: routes each turn. UI buttons route deterministically; free text is classified by
    a structured-output call into: continue lesson, question, quiz me, exercise, new topic, recap.
  - **session_start**: loads the learner model and the last session summary, shows where the user left
    off, and offers *recap last session / recap an earlier session / warm-up on weak spots / skip*
    (an interrupt that waits for the user's choice).
  - **planner**: builds the study plan from the approved `RoadmapSpec` (topic order from prerequisites,
    sized to the user's weekly time), shows today's plan, and replans when mastery changes. A big replan
    is shown for approval.
  - **teacher**: the existing tool loop (`run_tool_loop`) over MCP tools: roadmap search first, web
    tools for gaps and freshness. Answers cite with labelled sources (**Roadmap** vs **Web**).
  - **assessor**: generates quizzes (structured output), grades answers, updates mastery and weak spots.
  - **exercise**: sets a small coding task, runs the learner's code via `run_code`, and reviews it.
  - **researcher** ("teach me X"): web research → proposed mini roadmap → approval interrupt → enqueue
    ingestion → continue in the tutor.
  - **memory_curator**: runs after each session (see Memory).
- **Modes:** tutor (default), free Q&A, flashcards. **Only the user switches into flashcard mode** (a UI
  toggle). The coordinator's output type does not include it, and a test pins that.
- **Budgets on every run:** max steps, tokens and dollars (configurable); breaching one ends the turn with
  a clear message, never silently.
- **Failure recovery:** tool errors go back to the model as error results with their detail; provider
  outages retry with backoff, then fail visibly; ingestion jobs resume.
- **Knowledge vs web:** when both are used, a check asks whether the web contradicts the roadmap material
  and shows a notice if it does. Good web finds can be proposed as new roadmap resources (approval required).

### Skill map and mastery
- The roadmap is a graph of skills with prerequisite edges. Each skill has a mastery score (0–1, updated
  from quiz and exercise results), weak spots, the next review date and its resources.
- A skill is **locked** until its prerequisites reach 0.7 (default, overridable per skill).
- UI: a graph coloured by mastery (mastered / learning / not started / locked); clicking a skill shows
  its details.

### Memory
| Layer | Content | Where |
|---|---|---|
| Working | Current conversation; older turns compacted into a running summary to stay under a token budget (default 12k) | Graph state |
| Episodic | One summary per session ("Day 3: caching; struggled with write-back") | Postgres |
| Learner model | Mastery per skill, weak spots and misconceptions, preferences (video vs reading, pace), goals, deadline | Postgres |
| Semantic facts | Free-form facts with embeddings for recall | Postgres + pgvector |

- Agents use explicit `remember`, `recall` and `forget` tools (learner MCP server).
- The **curator** after each session writes the episode summary, extracts facts (structured output),
  merges duplicates (similarity ≥ 0.9 updates instead of inserting), replaces contradicted facts while
  keeping provenance, and clears weak spots that mastery has resolved.
- **What consent covers:** learning progress is always stored locally, because the tutor cannot work without
  it: mastery, weak spots, the study plan, session summaries and flashcards. Consent at first run covers
  **personal facts** (the semantic-facts layer and personal preferences or goals beyond the roadmap) and
  **keeping raw chat messages**. If declined, nothing personal is stored and raw messages are dropped when the
  session ends. All features still work.
- **Privacy:** all local; personal data and
  secrets (emails, phone numbers, keys, tokens) are redacted before anything is stored, traced or exported;
  the Memory page lists, edits, deletes and exports everything; raw messages expire after a retention
  period (default 90 days) while summaries stay.

### MCP
- **Own servers** (FastMCP 3.x):
  - `knowledge`: the existing server generalized by roadmap (search, get section, resources, prompts);
  - `learner`: memory, progress, mastery, flashcards;
  - `ingestion`: start a job, status, with **progress notifications**, and **elicitation** for plan
    approval when used from an MCP client.
- These also work from Claude Desktop or Claude Code.
- **MCP gateway (client side):** a registry file lists external servers. The gateway connects, lists
  tools, namespaces them (`web.search`, `docs.context7.*`), applies a per-agent allow-list, timeouts and the
  injection guard, and shows health in Settings. Each server can be switched off.
- **External servers:**
  - SearXNG MCP (search);
  - Fetch (official; page to markdown);
  - Playwright MCP (browser);
  - DeepWiki (remote, no auth; GitHub repos);
  - arXiv;
  - Wikipedia;
  - Microsoft Learn (remote, no auth);
  - Context7 (optional key);
  - GitHub MCP (optional token);
  - Colab MCP (optional, exercises).

### Code sandbox
- `run_code(language, code, stdin?, backend?)`, supporting Python and JavaScript.
- **Docker backend** (default) runs with:
  - no network, 256 MB memory, 0.5 CPU, pids ≤ 64;
  - read-only root and a 64 MB tmpfs;
  - a non-root user, all capabilities dropped, `no-new-privileges`;
  - a 10 s wall-clock limit and output capped at 64 KB.
- Only `sandbox-runner` holds the Docker socket, and it runs a fixed image with fixed flags. The threat
  model is written down in the docs.
- **Colab backend:** via the official Colab MCP server when the user has a notebook open and picks Colab,
  or the exercise needs a GPU. It is slower and not covered by CI.
- Escape tests run in CI: network access, a fork bomb, filesystem writes, an infinite loop and a memory bomb.

### Flashcards, calendar, notes
- Cards come from weak spots and missed quiz questions automatically; scheduling uses FSRS (open-source
  `fsrs` package). **Reviews happen only in the user-toggled flashcard mode;** the UI shows a due-count badge.
- **Calendar:** the study plan as an `.ics` file using the user's study days, times and minutes per day.
- **Notes:** one markdown file per session (frontmatter, `[[wikilinks]]` between skills; Obsidian-friendly)
  written to an export folder.

### Observability
- The existing `Trace` spans are exported through OpenTelemetry to Phoenix.
- An in-app **agent timeline** panel shows, per turn:
  - the graph nodes visited;
  - tool calls in parallel lanes;
  - tokens, latency and cost (from a configurable price table).
  This is the portfolio's main visual for orchestration.
- Traces and logs go through the same redaction as memory.

### Security
- The existing injection defenses extend to web content:
  - the untrusted-content rule;
  - tool allow-lists per agent;
  - the link sanitizer on output.
- **Which URLs may be fetched, per actor:**
  - **Agents** (teacher, researcher), via Fetch or Playwright MCP: only URLs from a search result, the user's
    messages or the roadmap's resources, plus same-site navigation from those pages. The gateway's
    Playwright wrapper enforces this on every navigation.
  - **The crawler:** follows same-site links inside resources the user approved, within the crawl limits.
  - **Researcher-proposed resources:** enter ingestion only after the user approves them.
  - **Ingested text** stays untrusted when it is retrieved later.
- The red-team eval adds poisoned web pages and search results at answer time, and a **poisoned page ingested
  into the knowledge base** that is later retrieved.
- Sandbox isolation as above.
- Local binding only; secrets never in traces.

### Cost control and caching
Agent loops resend the whole prefix (tools, system prompt, history) on every step. Today's code uses no
prompt caching. On Foundry, prompt caching (5-minute and 1-hour TTL, plus automatic caching) is supported.
For Haiku 5.5 the minimum cacheable prefix is 512 tokens, cache reads cost ~0.1× the input price, and cache
writes cost 1.25× (5 min) or 2× (1 h). Foundry does **not** support mid-conversation system messages or Message
Batches; compaction and message threads are beta there. The design uses four layers:

1. **Prompt caching in every call** (in `llm.py`):
   - **Byte-stable prefix:** tool definitions are sorted deterministically and shared by every agent; the
     system prompts are frozen, with no timestamps, IDs or learner data.
   - **Breakpoints:** an explicit breakpoint at the end of tools+system with a 1-hour TTL (learners pause to
     watch videos), plus top-level automatic caching for the growing conversation tail (5-minute TTL).
   - **Volatile context goes after the breakpoint:** the learner snapshot, today's plan and the retrieved
     passages go in the user turn, never in `system`.
   - **Effort is fixed per agent,** because changing it invalidates the messages cache.
   - **History stays append-only.**
   - **Measurement:** cache read and write tokens are recorded on every span and shown as a hit rate.
2. **Compute once, not per turn:**
   - At ingestion time, section summaries, video-card summaries and starter quiz questions per section are
     generated once and stored.
   - Recap reads the stored session summary with no model call.
   - The memory curator runs once per session, not per turn.
3. **Application caches (no model call):**
   - the existing semantic answer cache for factual Q&A, scoped by roadmap and mode; personalized tutoring
     turns are not cached;
   - a memo table keyed by a hash of (model, prompt, schema) for deterministic calls such as classification
     and extraction;
   - TTL caches for web search and fetched pages (ETag-aware);
   - an embedding cache keyed by content hash.
4. **Fewer and smaller calls:**
   - UI buttons route without the model;
   - classifiers run at low effort;
   - tool calls run in parallel to cut turns;
   - tool results are trimmed and only the top parents are sent;
   - concise outputs, since output tokens cost 5× input.

**Evals use record and replay.** Model responses are recorded, keyed by a request hash, so re-running an eval
after a non-model change costs nothing. Judge verdicts are cached by (transcript hash, rubric version). A cost
eval compares cost per completed tutoring turn with caching on vs. off, and the README reports it.

### Frontend (React + Vite + TS)
- **Roadmaps:** list and create (paste or upload, then approve the parsed spec).
- **Ingestion:** live progress over SSE, plus the report.
- **Skill map.**
- **Tutor chat:** the session-start card, today's plan, inline quizzes, a code-exercise editor, labelled
  sources, and the agent timeline.
- **Flashcards mode.**
- **Memory & privacy.**
- **Settings:** MCP servers and their health, transcript routes, key status, budgets.
- **Playwright E2E tests in TypeScript** against the fake LLM provider.

## Evals (≤ ~$2 per full run; dev/test split as today; numbers from the test split)
| Suite | What it measures | LLM cost |
|---|---|---|
| Ingestion | Saved fixture pages: video discovery recall and precision, chapter-route accuracy and segment-boundary agreement on ~10 known videos, content extraction quality | none |
| Retrieval | Questions auto-generated per roadmap (existing evidence-quote method): hit@k, MRR; child vs parent-child | small, once |
| Tutor | Simulated students (beginner, confused, advanced) in multi-turn sessions; rubric judge: checks understanding, no dumping, cites, follows plan; trajectory: nodes, tool calls, tokens, cost | main share |
| Memory | Scripted multi-session runs: recall of facts planted earlier, no stale facts after updates, `forget` really forgets | small |
| Routing | Coordinator intent accuracy; flashcard mode never entered by the agent | small / none |
| Red team | Poisoned web pages and search results against teacher and researcher, defenses on vs off | small |
| Judge calibration | The owner hand-grades ~20 tutor transcripts (committed) through a small review page; judge agreement reported (Cohen's kappa) | none |
| Cost | Cost per completed tutoring turn and cache hit rate, caching on vs. off | small |

Ingestion ground truth needs little hand labelling. Videos whose creators wrote chapters are the labels:
their chapters are hidden and the comment and transcript routes are scored against them. The fixture pages
come from the owner's second roadmap and are saved into the repo.

Suite sizes are chosen from the measured per-call cost so a full run fits the budget, and the README
reports the intervals. CI runs unit, integration and E2E tests on every push (fake LLM, no cost); evals
run on manual dispatch.

## Hosting: why it runs locally, and what hosting would cost
Decision: **local platform only.** The HF Space stays the demo of the original app. The README gets a
"Why not hosted?" section built from this one. Prices are approximate as of Nov 2026, and the builder
re-checks them when writing the README.

- **What a complete hosted version needs:**
  - an always-on host with about **8 GB RAM** (API with embedding models ~1.5 GB, Qdrant ~1 GB, Postgres,
    SearXNG, headless Chromium ~1 GB, Phoenix, headroom for Whisper);
  - persistent disk;
  - Docker for the sandbox;
  - HTTPS;
  - per-visitor isolation and abuse limits, because the app runs strangers' code and stores memory.
- **Why the free hosts don't fit:**
  - A **Hugging Face Space** is one container with no Docker, no persistent disk, a YouTube-blocked IP, and it
    must stay on ZeroGPU.
  - **Render's free tier** sleeps, has 512 MB RAM and no Docker.
  - **GCP's free e2-micro** has 1 GB RAM.
  - **AWS's free plan** is time-limited credits.
- **Monthly cost, roughly:**
  - **Compute:** a budget 4 vCPU / 8 GB VPS (Hetzner-class) ≈ $8–15/mo; the same on mainstream clouds
    (DigitalOcean, AWS Lightsail) ≈ $40–50/mo.
  - **Managed Postgres:** free tier (Neon) → ~$20/mo paid.
  - **Qdrant Cloud:** free 1 GB → ~$25+/mo paid. Self-hosting on the VPS costs $0 more.
  - **LLM:** Haiku 5.5 ($0.10 / $0.50 per M tokens) is small per turn, especially with caching. Public traffic
    makes it unbounded unless gated, so the passcode gate stays.
- **Free options that do exist, with caveats** (documented in the README as "run it yourself"):
  1. **Your own machine plus a free Cloudflare Tunnel:** the complete platform at a public HTTPS link while the
     laptop is on, with a home IP, which YouTube is friendlier to. Best for interviews and live demos.
  2. **Oracle Cloud Always Free (Ampere ARM VM):** Oracle documents 4 OCPU / 24 GB. Third-party reports say it
     was halved in June 2026 (unconfirmed). Even 12 GB would run the stack. Caveats: card verification at
     signup, frequent "out of capacity" in popular regions, idle instances can be reclaimed, ARM64 images are
     needed, and YouTube blocks the datacenter IP.
- **Chosen demo route: laptop + Cloudflare Tunnel.** A compose profile `tunnel` adds a `cloudflared`
  container that publishes the app at a public HTTPS URL: a free quick-tunnel URL, or a named tunnel if the
  owner adds a domain. The app learns it is exposed through a `PUBLIC_MODE=true` setting, and then:
  - every request needs the passcode, except a read-only landing page;
  - per-IP rate limits apply, reusing the existing lockout logic;
  - the code sandbox and new-roadmap ingestion are disabled unless the passcode holder enables them per
    session;
  - MCP endpoints keep their Bearer passcode;
  - nothing binds to anything except `127.0.0.1` and the tunnel.
  The default (no profile) stays local-only.
- So the honest README line is: *the complete platform runs free on your own machine. A public, always-on
  version needs roughly $10–50/month of infrastructure plus operating safeguards (sandboxing strangers' code,
  per-visitor privacy, abuse limits), so it isn't hosted.* The compose images are multi-arch (amd64 + arm64)
  so the Oracle route works.

## Delivery in phases (one implementation plan per phase)
Each phase gets its own detailed plan, written when the previous phase has merged, so it builds on real code.
Interfaces that cross phases (the core DB schema, the gateway API, the graph state) are fixed in the phase
that introduces them.
1. **Foundation:**
   - compose stack, Postgres + Alembic, a separate requirements file, the fake LLM provider;
   - OpenTelemetry export, the React app shell;
   - the HF demo untouched and its CI green.
2. **Roadmaps and ingestion:**
   - roadmap parsing and approval, the job queue;
   - crawler, video pipeline, chunking and multitenant index;
   - importing `sde-mastery`;
   - the ingestion report and the ingestion eval.
3. **MCP:** the gateway, external web servers, the generalized knowledge server, and the learner and
   ingestion servers.
4. **Tutor and memory:**
   - the LangGraph tutor (session start, planner, teacher, assessor, researcher, coordinator);
   - the skill map, the memory layers and curator, privacy controls.
5. **Practice and exports:** the sandbox (Docker + Colab), flashcard mode, `.ics` and notes export.
6. **Evals and polish:**
   - the tutor, memory, routing and red-team suites, and judge calibration;
   - the agent timeline polish;
   - the README rewrite with architecture and results.

## Left to the builder
- Exact table columns, prompt wording, the UI component libraries (graph layout, code editor),
  semantic-split thresholds and mastery-update formula (documented in code), and how each external MCP
  server is launched (container vs `uvx`/`npx`), within the constraints above.

## Phase 2 amendments (from the owner's real roadmap, 2026-11-19)
The owner's test roadmap (`backend/tutor/tests/fixtures/roadmaps/kubernetes.txt`) names its resources by title
only, with no URLs, and its line breaks were lost when pasted. That is realistic input, so the design adds:
- **Resource resolution.** The parser gives each resource a search `query`, and an optional `url_hint` that the
  model is never trusted to have right.
  - The resolver searches the self-hosted SearXNG (its YouTube engine for videos) and proposes up to 3 scored
    candidates. The approval screen lets the user pick one, paste a URL or skip.
  - Nothing is ingested without approval, and a URL the model wrote that isn't in the source is never used
    unverified.
  - SearXNG joins compose in Phase 2, used directly over its JSON API; Phase 3 wraps it as MCP.
- **Interactive sites** (e.g. Killercoda) are kept as `linked_only` resources for the tutor to recommend. They are
  never crawled.
- **Focus pages.** Sub-pages a resource names ("Components of a Cluster") are crawled first within the page cap.
- **Deferred:**
  - **Playwright rendering** of JS-only pages moves to Phase 3, since it needs the MCP gateway. Phase 2 reports
    such pages as `needs_browser`.
  - **The local Whisper route** moves to Phase 5. The routes in Phase 2 are captions (default), Gemini (opt-in)
    and yt-dlp with cookies (opt-in).
  - **Ingestion-time section and video summaries and starter quizzes** move to Phase 4, which first consumes
    them. They are still generated once per section and stored, never per turn.
- **Without `YOUTUBE_API_KEY`,** metadata and playlist expansion use yt-dlp in metadata-only mode (no download,
  no cookies). Comment timestamps need the free API key.
- **Fixture licensing.** Only kubernetes.io pages (CC BY 4.0) are committed as eval fixtures. Third-party pages
  and transcripts stay in a git-ignored local cache.
- **Ingestion agent (owner's request, 2026-11-19).** The deterministic job queue stays the engine. A Haiku tool
  loop supervises it at two moments:
  - **before the crawl (plan),** it picks which pages of a large site matter for the skills;
  - **after the crawl settles (recover, up to 2 rounds),** it reviews gaps (skipped resources, videos without a
    transcript, skills under 5 chunks). It finds replacements through search, retries transcripts on the routes
    the owner enabled, and proposes new sources.

  Limits:
  - it acts only by enqueueing jobs or proposing resources, and proposals need the user's approval;
  - its URLs follow the agent rule in "Security";
  - spend is capped at $0.05 per roadmap;
  - with the model unavailable, the pipeline completes without it.

  It has its own eval: gap-fix rate, plan and proposal precision, restraint on a healthy roadmap, and injection
  resistance with defenses on vs off.

## Phase 3 amendments (owner's architecture checklist, 2026-11-24)
These are taken only where they fit the planned work; the rest was declined.
- **Phase 3 additions:**
  - **Allow/deny/ask gate in the MCP gateway.** Registry rules, with deny beating ask and ask beating allow. An `ask`
    tool runs only after an approver has seen the raw JSON payload (tool, server, agent, arguments). By default
    `ask` covers the ingestion server's write tools and the learner's `forget`. `remember` and `set_mastery` stay
    allowed, because the tutor calls them on every turn.
  - **Idempotency keys on MCP write tools.** On the ingestion server's `create_roadmap` and `approve_roadmap`, a
    repeated key replays the stored result, and a key reused with different arguments is an error.
- **Later phases:**
  - **Phase 4:** the tutor's approval interrupts (big replans, researcher proposals, `ask` tools) show the raw action
    payload next to the human-readable summary.
  - **Phase 6:** the red-team suite is organised by the OWASP agentic-AI threat categories, and the README reports a
    pass rate per category. The tutor and ingestion evals add fault-injected trajectory scenarios: tool HTTP 500s,
    malformed tool JSON, a worker crash mid-run and an MCP server outage. The README reports the completion rate.
- **Declined:**
  - OAuth 2.1 scoped MCP auth: the `mcp` 1.x pin, and one local user;
  - SKILL.md runbooks;
  - Langfuse: Phoenix stays;
  - Temporal: the Postgres job queue stays.


## Phase 3 follow-ups for Phase 4 (from the Phase 3 reviews, 2026-11-24)
Phase 3 shipped the gateway with no model-facing caller. Phase 4's plan opens with a **gateway readiness** task that
does these before any tutor agent calls a tool:
- **Timeouts:**
  - give the own servers realistic `timeout_s` values, or per-tool timeouts: `ingest.wait_for_ingestion`,
    `ingest.create_roadmap` and the first `kb.search` (lazy model load) exceed 20 s;
  - a plain call timeout should not drop a whole in-process connection.
- **Search URLs:** treat Microsoft Learn search results as allowed URLs, or exempt its server-side fetch tool.
- **Caps:** measure the cap with `ensure_ascii=True`, as the loop sends JSON (CJK text inflates up to 6×).
- **Gateway loop:**
  - bound DNS lookups with a timeout;
  - stop running the synchronous `effective_enabled` query on the gateway loop;
  - stop `tool_defs` from stalling on a slow stdio server;
  - trace refused calls;
  - retry the `about:blank` reset once.
- **Browser isolation:** one Playwright browser and tab is shared by every agent, so give each agent its own browser
  context, or use a gateway-level lease.
- **Approvers:** run them sync or loop-agnostic, never bound to the FastAPI loop. Approval UIs show the raw payload.
- **Untrusted passages:** `kb` passages reach the model as citation blocks without the untrusted fence. The tutor's
  prompt must treat them as untrusted, and the red-team eval must cover a poisoned ingested page.
- **Ingestion writes:**
  - give idempotency claims a token, so a stale re-claim cannot release a live one;
  - stop `approve_roadmap` searching while it holds the row lock;
  - return early from `wait_for_ingestion` on a roadmap that is still only parsed.
- **Learner:** decide whether revoking consent hides or purges stored facts; cap the length of `remember`.
- **Re-check:** recheck still reports rendered (JS-only) resources as dead.
- **Known limits (documented in `docs/platform/mcp.md`):**
  - blocked origins don't cover redirects;
  - the API is reachable by its browser-network IP, though `/api/*` now needs a loopback Host;
  - sub-resource requests aren't seen by the gateway;
  - stdio servers run as the API's user (a supply-chain risk).
