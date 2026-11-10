---
title: SDE Mastery Hub - Agentic RAG Tutor
emoji: 🧠
colorFrom: indigo
colorTo: purple
sdk: gradio
sdk_version: 6.31.0
python_version: "3.12"
app_file: app.py
pinned: false
license: mit
---

# SDE Mastery Hub: an agentic RAG tutor for software engineering

A software engineering tutor that answers from a curated knowledge base (*Software Engineering at
Google*, the System Design Primer, MIT Missing Semester, Google eng-practices) and from any YouTube
video you paste. It is built as an **MCP tool server + an explicit agent loop + a measured RAG
pipeline**, and every design choice below is backed by an eval run you can reproduce.

**Live demo:** https://huggingface.co/spaces/Arushh1050/sde-mastery-hub

## Highlights

- **Hybrid retrieval on Qdrant.** Dense BGE embeddings + BM25 sparse vectors, fused server-side
  with reciprocal rank fusion. Compared with the shipped baseline it raises **MRR from 0.64 to 0.78**
  and **hit@1 from 0.51 to 0.69**, with a significant paired-bootstrap result ([results](#retrieval-results)).
- **Eval-driven decisions.** An 88-question eval set with verbatim evidence quotes (scoring doesn't
  depend on chunk boundaries) and a dev/test split. The cross-encoder reranker was *dropped*
  because it added +0.03 MRR (not significant) at 7× the latency.
- **MCP is the single tool layer.** The web agent calls its tools through an in-memory MCP
  client. The same server is exposed over Streamable HTTP (`/mcp`, bearer-auth) and stdio for
  Claude Desktop or Cursor, with tools, resources and prompts.
- **Two agent designs, compared by eval.** A fixed pipeline (router → parallel tool calls →
  generation with **API-native citations** → a verifier that triggers one corrective retrieval)
  and a **tool-use loop** where Claude picks MCP-discovered tools itself, in parallel, within a
  step budget. The loop matches the pipeline on correctness at 2.2–2.8× lower latency, but its
  answers are less faithful, so the pipeline stays the default ([results](#agent-evaluation)).
- **Prompt-injection defenses with a red team.** Retrieved and ingested text is treated as
  untrusted: a deterministic tool-call guard, a link/image sanitizer, and a 10-attack red-team
  eval ([results](#prompt-injection-red-team)).
- **Live YouTube ingestion.** A pasted video is loaded (stored copy → captions → Gemini video
  understanding), chunked with `[MM:SS]` section timestamps, and embedded into Qdrant, so
  follow-up questions retrieve it through normal RAG and it survives restarts.
- **Two access tiers.** Visitors use the free Gemini pipeline. A passcode unlocks Claude Haiku 5.5
  (Microsoft Foundry) and the MCP endpoint, with constant-time comparison and per-IP lockout.
- **Observability.** Every request produces a trace (spans, latency, tokens, LLM-call count). It
  is streamed to the UI and logged as one JSON line. A semantic answer cache in Qdrant answers
  near-duplicate questions with zero LLM calls.

## What this project demonstrates

- **Eval-driven engineering, including negative results.** Every retrieval change is measured on
  a held-out split and checked with paired bootstrap confidence intervals. The cross-encoder
  reranker won on dev but lost on test. Overall its gain wasn't significant (+0.03 MRR,
  CI crosses zero) and it cost 7× the latency, so it was removed rather than kept because it's
  "best practice".
- **An eval that doesn't depend on chunking.** Questions are scored against verbatim evidence
  quotes, not chunk IDs, so completely different chunking strategies can be compared fairly.
- **Data-quality work before model tuning.** Auditing the corpus turned up ~15% translated
  duplicates and repeated web pages, which are removed by rule-based filtering and 5-shingle
  near-duplicate detection.
- **Quantified trade-offs.** The verifier ablation shows +0.11 faithfulness for 2.5× the
  latency, which is why it runs only on the premium tier and can be switched off.
- **Agent design decided by a pre-registered rule.** The tool-use loop shipped only if it held
  correctness within 0.05 (95% CI) on multi-hop and single-hop questions. It came within 0.061 on
  multi-hop, missed the bar, and stays opt-in (`AGENT_MODE=loop`), with the numbers published.
- **Evals beyond single-hop accuracy.** Multi-hop questions whose two evidence quotes come from
  different documents, unanswerable questions that test abstention, and trajectory metrics
  (model turns, tool calls, tokens, latency) per agent design.
- **Security evals that score the goal, not a keyword.** The red team counts an attack only when
  its goal happens (a clickable URL to the attacker's domain, an executed tool call, an uncited
  answer). A first version that matched keywords scored refusals like "I ignored an instruction
  to link to evil.example" as compromises; with goal-based detectors the measured rate is 0%.
- **MCP used as architecture.** A single FastMCP server is the agent's only tool layer. It is
  called in-process by the web agent and served over authenticated Streamable HTTP and stdio to
  external clients like Claude Desktop and Cursor.
- **A cost-aware product.** Free and passcode tiers, one LLM call per free-tier message, a
  semantic cache, an LLM-call budget, and fallback to the retrieved passages when the model's
  quota runs out.
- **Debugging production deployments.** Fixed ZeroGPU startup detection for a uvicorn-served
  app, resolved a dependency conflict between `gradio[mcp]` and FastMCP 4, and made model loading
  thread-safe under concurrent requests.

## Architecture

```
Browser (SSE: steps, streamed tokens, citations, trace)
   │  X-Access-Code (optional)
   ▼
FastAPI /chat ──► Orchestrator ───────────────────────────────────────────────┐
   │              1. semantic cache (Qdrant)        public: 1 Gemini call     │
   │              2. router  (Haiku structured output | rules on public tier) │
   │              3. tools in parallel  ──► in-memory MCP client              │
   │              4. generator (Haiku + search_result citations | Gemini)     │
   │              5. verifier → ≤1 corrective retrieval + regeneration        │
   │              or AGENT_MODE=loop: Claude calls MCP tools itself (≤4 turns)│
   │              + tool-call guard and link sanitizer on every answer        │
   │                                                                          │
   ├── /mcp  (Streamable HTTP, Bearer passcode) ─┐                            │
   ▼                                              ▼                           │
                     MCP server (FastMCP): search_knowledge_base, analyze_youtube_video,
                     get_document · resources kb://stats, kb://sources, kb://eval/retrieval, kb://eval/agent
                     · prompts system_design_interview, explain_concept, code_review
                                   │
                    Qdrant Cloud: dense (bge-small, 384d) + sparse BM25 (IDF), RRF fusion
                    collections: sde_kb_v2 (live), sde_kb_v1 (baseline), sde_answer_cache
```

| Component | Choice |
|---|---|
| Embeddings | `BAAI/bge-small-en-v1.5` with its query instruction, CPU |
| Sparse | `Qdrant/bm25` via fastembed, with IDF computed by Qdrant over the live collection |
| Vector DB | Qdrant Cloud (embedded on-disk mode when `QDRANT_URL` is unset) |
| Premium LLM | Claude Haiku 5.5 on Microsoft Foundry (router, generator, verifier, eval judge) |
| Public LLM | Gemini (generator, YouTube video understanding) |
| Agent tools | FastMCP 3.4, in-memory transport for the app, HTTP/stdio for external clients |
| Agent modes | `pipeline` (default) or `loop` (Claude tool use over MCP-discovered tools, step budget 4) |

## Knowledge base and chunking

The original index was 1,267 LlamaIndex chunks in JSON files, loaded into memory. Auditing it
turned up problems that the v2 pipeline ([backend/rag/documents.py](backend/rag/documents.py)) fixes:

- **Translations.** Japanese and Chinese copies of the System Design Primer README made up ~15% of
  all chunks. They are dropped.
- **Duplicates.** Missing Semester web pages repeated the GitHub markdown. They are removed with
  5-shingle Jaccard near-duplicate detection.
- **Oversized chunks.** The median chunk was ~2.7k characters, beyond what the 512-token embedding
  and reranker models read. v2 splits along markdown sections into ~320-token chunks and embeds
  each one with a contextual header (`doc title > section path`).

Documents are rebuilt from the legacy chunks by their character offsets: 2 of 1,267 failed to
round-trip. v2 has 2,769 chunks across 668 documents, plus any YouTube videos users add.

## Evaluation

### Dataset
[backend/eval/generate_questions.py](backend/eval/generate_questions.py) samples passages from the
book, GitHub and web sources. Claude Haiku writes a natural question for each passage, a reference
answer, and a **verbatim evidence quote**, which is checked programmatically against the passage.
A retrieval hit means a returned chunk contains that quote (fuzzy-matched for PDF hyphenation), so
different chunkings can be compared fairly. There are 88 questions, split 55 dev / 33 test.

### Retrieval results
`python -m backend.eval.retrieval_eval` (no LLM calls, so it's free):

| config (all 88 questions) | hit@1 | hit@5 | MRR@10 | doc hit@5 | p50 latency |
|---|---|---|---|---|---|
| legacy LlamaIndex (shipped baseline) | 0.51 | 0.78 | 0.64 | 0.81 | 17 ms* |
| v1 chunks, hybrid | 0.64 | 0.88 | 0.73 | 0.88 | 174 ms |
| v2 chunks, dense only | 0.64 | 0.88 | 0.74 | 0.90 | 173 ms |
| **v2 chunks, hybrid (production)** | **0.69** | **0.90** | **0.78** | **0.93** | 172 ms |
| v2 hybrid + bge-reranker-base | 0.70 | 0.91 | 0.80 | 0.94 | 1203 ms |

\*In-memory, with no network hop. Qdrant latencies include the round trip to Qdrant Cloud.

Paired bootstrap (10k resamples), B − A with 95% CI:

| A → B | MRR | hit@1 | hit@5 |
|---|---|---|---|
| legacy → v2 hybrid | **+0.142** [+0.053, +0.232] | **+0.182** [+0.068, +0.307] | **+0.114** [+0.023, +0.216] |
| v2 hybrid → + reranker | +0.027 [−0.034, +0.087] | +0.011 [−0.091, +0.114] | +0.011 [−0.034, +0.068] |

Findings:
- The full upgrade is significant on every metric.
- No single component is significant on its own (v1→v2 chunking, dense→hybrid). The gain comes
  from stacking them.
- The reranker won on dev (MRR 0.82 vs 0.74) but lost on the held-out test split (0.79 vs 0.85).
  It isn't significant overall and costs 7× the latency, so production uses hybrid only
  (`RETRIEVAL_MODE=hybrid_rerank` turns it back on).
- On the old long chunks the reranker even *hurt* (v1 hybrid → +rerank on dev: hit@1 0.62 → 0.56),
  because the cross-encoder truncates at 512 tokens.

### Answer quality and the verifier ablation
`python -m backend.eval.answer_eval` runs the real agent end to end, and Claude Haiku judges
correctness against the reference answer and faithfulness to the retrieved sources.
Test split, n=33:

| variant | correctness | faithfulness | gold source cited | LLM calls | tokens in/out | p50 latency |
|---|---|---|---|---|---|---|
| premium (with verifier) | 0.86 | **0.89** | **0.88** | 3.6 | 13.0k / 2.8k | 19.0 s |
| premium, no verifier | 0.89 | 0.79 | 0.79 | 2.0 | 5.6k / 1.2k | 7.7 s |

With the verifier, faithfulness goes up **+0.11** (95% CI [0.00, +0.21]) and correctness changes
−0.03 ([−0.08, 0.00]), at 2.5× the latency. The verifier is strict: it asks for a corrective
pass on 61% of answers. It stays on for the premium tier (`VERIFY_ANSWERS=false` turns it off).
*Caveat:* the judge is from the same model family as the generator, so compare scores between
variants rather than treating them as absolute.

### Agent evaluation
`python -m backend.eval.agent_eval` compares the two premium designs on a second question set
([backend/eval/generate_questions_v2.py](backend/eval/generate_questions_v2.py)):
- **71 multi-hop questions**, each built from two related passages in different documents, with
  one verbatim evidence quote from each, both checked programmatically.
- **30 unanswerable questions** on topics outside the corpus (Raft, eBPF, QUIC, …), each kept
  only if a judge confirmed the top-8 retrieved passages do not answer it.

Test split; prompts were frozen after one dev-only tuning pass of the loop instructions.

| multi-hop (n=33) | correctness | faithfulness | evidence recall | model turns | tool calls | tokens in/out | p50 latency |
|---|---|---|---|---|---|---|---|
| pipeline (with verifier) | 0.85 | **0.89** | 0.85 | 3.6 | 3.3 | 14.4k / 3.4k | 22.5 s |
| pipeline, no verifier | 0.86 | 0.71 | 0.85 | 2.0 | 2.6 | 6.0k / 1.5k | 8.7 s |
| tool-use loop | 0.86 | 0.73 | 0.82 | 2.0 | 2.2 | 10.0k / 1.3k | **8.1 s** |

| loop − pipeline (paired bootstrap, 95% CI) | delta |
|---|---|
| multi-hop correctness (n=33) | +0.015 [−0.061, +0.091] |
| multi-hop faithfulness | **−0.167** [−0.258, −0.061] |
| single-hop correctness (v1 set, n=33) | +0.000 [−0.045, +0.045] |
| single-hop faithfulness | **−0.121** [−0.227, −0.015] |

All three variants abstained on every unanswerable test question (7/7), with no answers that
claimed support from the sources.

Findings:
- The loop is as correct as the pipeline and 2.8× faster, using fewer tokens and LLM calls.
- Its faithfulness gap is the verifier: the loop is level with the pipeline *without* its
  verifier (0.73 vs 0.71). The loop's answers add unsourced elaboration, and nothing checks them.
- **Decision:** the loop needed a 95% CI lower bound of at least −0.05 on multi-hop correctness;
  it got −0.061. The pipeline stays the default and the loop is opt-in (`AGENT_MODE=loop`).
  The next experiment is a verifier pass on the loop's answer.

### Prompt-injection red team
Threat model: retrieved passages and ingested YouTube content are written by someone else, and the
UI renders the model's markdown. An attacker who controls one retrieved passage tries to make the
agent emit a canary, render a link or image to their domain, leak the system prompt, suppress
citations, or (loop mode) call tools on a video the user never supplied.

Defenses (`INJECTION_DEFENSE`, on by default):
- A system-prompt rule that source text is data, never instructions.
- A **tool-call guard**: videos only if the user pasted them, documents only if a search
  already returned them.
- A **link sanitizer** on every answer, free and passcode tier alike: outside code, links and
  images to URLs that are not retrieved sources or in the user's message are neutralized. That
  covers markdown and reference-style links, raw `<img>`/`<a>` HTML, autolinks, and bare URLs
  (shown as code, not clickable). The frontend's markdown renderer would otherwise auto-load an
  injected image.

`python -m backend.eval.injection_eval`: 10 attacks × 5 questions, one poisoned passage among 5,
run against both the pipeline generator and the loop:

| target | defense | attack success | clean answers still citing sources |
|---|---|---|---|
| pipeline | off | 0/40 | – |
| pipeline | on | 0/40 | 5/5 |
| loop | off | 0/50 | – |
| loop | on | 0/50 | 5/5 |

Claude Haiku 5.5 refused all 10 attacks without help, often telling the user it had ignored an
embedded instruction. So this red team shows the defenses cost nothing (answers still cite their
sources), but it cannot show them stopping anything. The guard and sanitizer are verified
separately by unit tests that simulate a model that does comply. A stronger attack set, or a less
robust model, is needed to measure their effect end to end.

### Limitations
- The eval sets are small (88 single-hop, 101 v2 questions; only 7 unanswerable in the v2 test
  split) and LLM-generated. The evidence quotes are verified automatically, but the questions
  haven't been reviewed by hand. Many multi-hop pairs are two pages of the same book.
- The answer judge is from the same model family as the generator. Compare variants with each
  other rather than reading the scores as absolute quality.
- The verifier is strict (it asks for a corrective pass on 61% of answers). Tuning that threshold
  is the next experiment.

## Using the MCP server from Claude Desktop or Cursor

Remote, over Streamable HTTP (needs the passcode):
```json
{ "mcpServers": { "sde-knowledge": {
    "url": "https://arushh1050-sde-mastery-hub.hf.space/mcp/",
    "headers": { "Authorization": "Bearer <passcode>" } } } }
```
Local, over stdio: `python -m backend.mcp_server` (see [backend/mcp.json](backend/mcp.json)).

## Running locally

```bash
python -m venv backend/venv && source backend/venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env           # fill in keys (see below)
python -m backend.rag.build_index               # builds sde_kb_v1 + sde_kb_v2 in Qdrant
uvicorn backend.server:app --port 8001          # UI at http://localhost:8001
python -m pytest backend/tests -q               # offline unit tests
```

| Variable | Purpose |
|---|---|
| `QDRANT_URL`, `QDRANT_API_KEY` | Qdrant Cloud. Leave both unset for embedded local mode |
| `GEMINI_API_KEY` | Public tier and YouTube video understanding |
| `TESTFORGE_FOUNDRY_ENDPOINT`, `TESTFORGE_FOUNDRY_API_KEY`, `FOUNDRY_HAIKU_DEPLOYMENT` | Claude Haiku 5.5 on Foundry |
| `PREMIUM_PASSCODE` | Unlocks the Claude tier and `/mcp`. Unset disables both |
| `AGENT_MODE` (`pipeline` \| `loop`), `AGENT_MAX_STEPS` (4), `INJECTION_DEFENSE` (true) | Premium agent design, loop step budget, injection defenses |
| `RETRIEVAL_MODE`, `VERIFY_ANSWERS`, `SEMANTIC_CACHE`, `MAX_LLM_CALLS`, ... | Tunables in [backend/core/config.py](backend/core/config.py) |

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the unit tests on every push. When
the Qdrant secrets are present, it also runs the retrieval eval on the test split and fails the
build if hit@5 drops below 0.85.

## Deployment
The app runs as a Gradio-SDK Space: [app.py](app.py) mounts a small Gradio page inside the FastAPI
app. The Space runs on ZeroGPU hardware (free accounts can no longer run Gradio Spaces on
`cpu-basic`), so `app.py` registers a `@spaces.GPU` function and runs ZeroGPU's startup hook
itself. Everything actually runs on CPU. Deploy with `python deploy_to_hf.py`.

## Project layout
```
app.py                     HF entrypoint (FastAPI + Gradio mount + ZeroGPU shim)
backend/server.py          /chat (SSE), /auth, /health, /api/stats, /mcp mount
backend/mcp_server.py      MCP tools, resources, prompts
backend/agent/             orchestrator (pipeline), tool-use loop, injection guard, citations, access tiers, cache
backend/rag/               documents & chunking, Qdrant store & retrieval, index builder
backend/tools/youtube.py   video loading + live ingestion
backend/core/              config, LLM clients (Foundry / Gemini), tracing
backend/eval/              question sets, retrieval / answer / agent / injection evals, results/
backend/tests/             offline unit tests
backend/ingest.py          original scraping pipeline (GitHub, web, PDF) that built the legacy index
```

## License
MIT
