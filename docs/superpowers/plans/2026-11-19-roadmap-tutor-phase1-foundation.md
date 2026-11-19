# Roadmap Tutor, Phase 1 (Foundation): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable local platform skeleton:
- `docker compose up` starts the API, Postgres (pgvector) and Qdrant, with an optional Phoenix trace viewer and
  an optional Cloudflare tunnel;
- a React + TypeScript app lists and creates roadmaps;
- every model call goes through one LLM layer with a fake provider, record/replay, prompt caching, cost
  accounting and redacted tracing;
- the Hugging Face demo is untouched.

**Architecture:**
- **Shared (`backend/core`):** the LLM layer, tracing, cost and redaction. These modules use only the demo's
  runtime dependencies, so the Space benefits without new dependencies.
- **Platform-only (`backend/tutor`):** a new package with settings, database, migrations, telemetry export and
  the FastAPI app. Its tests live in `backend/tutor/tests` and need `requirements-platform.txt`.
- **Web app:** `web/`, a Vite React TypeScript app served by the platform API.
- **Container setup:** `docker/` and `compose*.yaml`.

**Tech Stack:**
- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 + psycopg 3, Alembic, Postgres 16 with pgvector, Qdrant,
  OpenTelemetry SDK (OTLP over HTTP) to Arize Phoenix.
- **Web:** Node 22 LTS, React, Vite, TypeScript, Vitest, Playwright.
- **Infrastructure:** Docker Compose, cloudflared.

**Spec:** `docs/superpowers/specs/2026-11-19-roadmap-tutor-design.md` (read the "Who and where", "Constraints",
"Cost control and caching", "Observability", "Security" and "Hosting" sections before starting).

## Global Constraints
- **The HF Space must keep working unchanged:**
  - `requirements.txt` gets no new lines;
  - `backend/tests` must pass with only `requirements.txt` + `pytest` installed (the existing CI `unit-tests`
    job);
  - `deploy_to_hf.py` must not upload `backend/tutor/**`, `web/**`, `docker/**`, `compose*.yaml` or
    `requirements-platform.txt`.
- The pins `fastmcp>=3.4,<4` and `mcp<2` stay. `requirements-platform.txt` starts with `-r requirements.txt` and
  does not override them.
- **Every Claude call goes through `backend/core/llm.py`.** Model `config.HAIKU_MODEL` (`claude-haiku-5-5`) on
  Foundry. `tool_choice` is only `auto`/`none`; no `thinking`, `temperature` or prefill. History is append-only.
- Tests never call a real model or the network. Use `LLM_PROVIDER=fake` (or `replay`).
- **Prompt caching (spec, "Cost control and caching"):**
  - an explicit breakpoint on the system prompt with `{"type": "ephemeral", "ttl": "1h"}`;
  - top-level automatic `cache_control: {"type": "ephemeral"}` for the tail;
  - tools sorted by name;
  - no timestamps, IDs or learner data in `system`;
  - `PROMPT_CACHE=false` reproduces today's request bytes exactly.
- **Haiku 5.5 prices** (USD per million tokens, overridable): input 0.10, output 0.50, cache read 0.01, 5-minute
  cache write 0.125, 1-hour cache write 0.20.
- Secrets live only in `backend/.env`. Nothing is logged or traced before it passes `redact()`.
- **Binding and access:**
  - compose publishes ports only on `127.0.0.1`;
  - `PUBLIC_MODE=true` requires `PREMIUM_PASSCODE`, otherwise startup fails;
  - in public mode every `/api/*` route except `/api/health` needs a valid `X-Access-Code`, using the existing
    `backend/agent/access.py` lockout of 5 failures per 15 minutes.
- Images build for `linux/amd64` and `linux/arm64`.
- **Commit trailer:** every commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Work on a
  feature branch, never directly on `main`.

## Review Focus
1. **`PROMPT_CACHE=false` must give byte-identical requests to today's code** (the demo's behaviour) → Task 3
   test `test_prompt_cache_off_matches_legacy_request`.
2. **The OTLP endpoint is unreachable** (compose sets it, but the `observability` profile is off). Requests must
   still succeed and nothing may raise → Task 5 test `test_export_failure_is_swallowed`.
3. **Bad roadmap input** (uppercase or space in the slug, an empty title, a 200,001-character `source_text`,
   emoji) → 422 with a message, never 500 → Task 7 test `test_create_roadmap_validation`.
4. **Postgres down while the app runs:**
   - `/api/health` returns 503 with `"db": "down"`;
   - roadmap routes return JSON 503, not a stack trace;
   → Task 7 test `test_db_down_returns_503`.
5. **Redaction false positives:** `task-based`, `12:30–18:00`, chunk IDs, plain URLs and `[3]` citations stay
   unchanged → Task 4 test `test_redact_leaves_ordinary_text`.

---

### Task 1: Platform package, settings and deploy isolation

**Files:**
- Create: `requirements-platform.txt`
- Create: `backend/tutor/__init__.py`
- Create: `backend/tutor/settings.py`
- Create: `backend/tutor/tests/__init__.py`
- Create: `backend/tutor/tests/test_settings.py`
- Modify: `deploy_to_hf.py`
- Test: `backend/tests/test_deploy_isolation.py`

**Interfaces:**
- **Produces:**
  - `Settings`, a frozen dataclass with these fields:
    - `database_url: str` (default `postgresql+psycopg://tutor:tutor@127.0.0.1:5432/tutor`);
    - `qdrant_url: str` (env `PLATFORM_QDRANT_URL`, default `http://127.0.0.1:6333`);
    - `collection: str` (default `roadmap_chunks`);
    - `public_mode: bool` (env `PUBLIC_MODE`, default False);
    - `otel_endpoint: str | None` (env `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`);
    - `web_dist: Path` (env `WEB_DIST`, default `<repo>/web/dist`);
    - `host: str` (default `127.0.0.1`) and `port: int` (default 8000).
  - `load_settings(env: Mapping[str, str] = os.environ) -> Settings`.
  - `Settings.validate() -> None`, which raises `SettingsError` (a ValueError subclass) when `public_mode` is
    true and `backend.core.config.PREMIUM_PASSCODE` is empty.
  - In `deploy_to_hf.py`: module-level `IGNORE_PATTERNS: list[str]`, `DELETE_PATTERNS: list[str]` and
    `main() -> None`. All side effects move under `if __name__ == "__main__": main()`.

- [ ] **Step 1: Write the failing tests**

  In `backend/tests/test_deploy_isolation.py` (no platform dependencies):
  - `test_import_has_no_side_effects`: importing `deploy_to_hf` doesn't prompt or call `HfApi`. Monkeypatch
    `getpass.getpass` to raise, then import.
  - `test_platform_paths_are_ignored`: for each of `backend/tutor/x.py`, `web/src/App.tsx`, `docker/api.Dockerfile`,
    `compose.yaml`, `compose.tunnel.yaml` and `requirements-platform.txt`, some pattern in `IGNORE_PATTERNS`
    matches (use `fnmatch`).
  - `test_runtime_requirements_have_no_platform_deps`: `requirements.txt` contains none of `sqlalchemy`,
    `alembic`, `psycopg`, `langgraph` or `opentelemetry`.

  In `backend/tutor/tests/test_settings.py`:
  - `test_defaults`: `load_settings({})` gives the defaults above.
  - `test_public_mode_requires_passcode`: monkeypatch `config.PREMIUM_PASSCODE = ""`;
    `load_settings({"PUBLIC_MODE": "true"}).validate()` raises `SettingsError`. With a passcode set, it doesn't.

- [ ] **Step 2: Run them and see them fail**

  Run: `backend/venv/bin/python -m pytest backend/tests/test_deploy_isolation.py backend/tutor/tests/test_settings.py -q`
  Expected: FAIL (`deploy_to_hf` prompts or errors on import; `backend.tutor.settings` is missing).

- [ ] **Step 3: Implement**

  - Refactor `deploy_to_hf.py` into `main()`, keeping the same behaviour, and add the platform patterns to
    `IGNORE_PATTERNS`: `backend/tutor/**`, `web/**`, `docker/**`, `compose*.yaml`, `requirements-platform.txt`.
  - Write `requirements-platform.txt`: `-r requirements.txt`, then `sqlalchemy>=2.0`, `psycopg[binary]>=3.2`,
    `alembic>=1.13`, `opentelemetry-sdk>=1.27`, `opentelemetry-exporter-otlp-proto-http>=1.27`, `httpx`, `pytest`.
  - Install it into the venv.

- [ ] **Step 4: Run them and see them pass**

  Run: the same command.
  Expected: PASS. Also run `backend/venv/bin/python -m pytest backend/tests -q`: all existing tests still pass.

- [ ] **Step 5: Commit**

  Message: `Add platform package, settings and HF deploy isolation`.

---

### Task 2: LLM providers (fake, record, replay)

**Files:**
- Create: `backend/core/fake_llm.py`
- Create: `backend/core/llm_replay.py`
- Modify: `backend/core/llm.py` (`haiku_client`, `haiku_async_client`)
- Modify: `backend/core/config.py` (add `LLM_PROVIDER = _env("LLM_PROVIDER", "foundry").lower()` and
  `LLM_RECORDINGS_DIR = _env("LLM_RECORDINGS_DIR", str(BACKEND_DIR / "eval" / "recordings"))`)
- Test: `backend/tests/test_llm_providers.py`

**Interfaces:**
- **Produces (`backend/core/fake_llm.py`):**
  - `FakeReply`, a dataclass with `text: str | None = None`, `json: dict | None = None`,
    `tool_uses: list[tuple[str, dict]] = []` and `stop_reason: str | None = None`. When `stop_reason` is None it
    is inferred: `tool_use` if there are tool uses, otherwise `end_turn`.
  - `script(replies: list[FakeReply])`, a context manager that queues replies consumed in order. Once the queue
    is empty, the default reply applies.
  - `requests: list[dict]`, every call's keyword arguments in order (cleared by `reset()`).
  - `reset() -> None`.
  - `example_from_schema(schema: dict) -> dict`, a minimal instance:
    - required keys only;
    - `string` → `"fake"`, `integer` → `0`, `number` → `0.0`, `boolean` → `False`, `array` → `[]`;
    - `enum` → its first value;
    - nested objects recursively.
  - `FakeAnthropic` with `.messages.create(**kw)`, and `FakeAsyncAnthropic` with `.messages.stream(**kw)` (an
    async context manager with `.text_stream` and `await .get_final_message()`) and `async close()`. Their return
    values are message-like objects: `.content` blocks of `type` `text` (`.text`, `.citations=None`) or `tool_use`
    (`.id`, `.name`, `.input`), plus `.stop_reason`, `.model="fake"`, and `.usage` with `input_tokens`,
    `output_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens`.
  - **The default reply:**
    - when `output_config.format` is a `json_schema`, the text is `json.dumps(example_from_schema(schema))`;
    - otherwise the text is `"Fake answer."`.
- **Produces (`backend/core/llm_replay.py`):**
  - `request_key(kwargs: dict) -> str`: the SHA-256 of canonical JSON (`sort_keys=True`) of the kwargs, without
    `cache_control` anywhere at any depth. This way toggling caching doesn't invalidate recordings.
  - `ReplayMiss(RuntimeError)`.
  - `RecordingClient(inner)` / `AsyncRecordingClient(inner)` write `<dir>/<key>.json` (the message's `model_dump`).
  - `ReplayClient()` / `AsyncReplayClient()` read it and return `anthropic.types.Message.model_validate(...)`.
    The replayed stream yields the concatenated text blocks as one chunk.
- **Changes in `llm.py`:**
  - `haiku_client()` and `haiku_async_client()` dispatch on `config.LLM_PROVIDER` (`foundry` | `fake` |
    `record` | `replay`), and only `foundry`/`record` need credentials.
  - The `lru_cache` must not pin the provider across a config change. Key it on the provider, or clear it in the
    tests.

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_llm_providers.py`:
  - `test_haiku_json_fake_returns_schema_instance`: with `LLM_PROVIDER=fake`, `haiku_json(system="s", prompt="p",
    schema={"type":"object","required":["route","k"],"properties":{"route":{"enum":["kb","web"]},"k":{"type":"integer"}}})
    == {"route": "kb", "k": 0}`.
  - `test_scripted_reply_is_used_once`: `script([FakeReply(json={"route":"web","k":2})])` → the first call returns
    it and the second returns the default.
  - `test_loop_runs_on_fake_provider`: `run_tool_loop` using `default_model_turn` (no injected `model_turn`),
    scripted `[FakeReply(tool_uses=[("search_knowledge_base", {"query": "x"})]), FakeReply(text="done")]`, with a
    `call_tool` returning one passage → `result.text == "done"`, `result.steps == 2` and
    `len(fake_llm.requests) == 2`.
  - `test_record_then_replay`: monkeypatch the record inner client to `FakeAnthropic`. A `haiku_json` call
    under `record` writes one file in `tmp_path`. The same call under `replay` returns an equal dict. A different
    prompt under `replay` raises `ReplayMiss`.
  - `test_request_key_ignores_cache_control`: kwargs differing only in `cache_control` (top level and inside
    `system` blocks) give the same key.

- [ ] **Step 2: Run them and see them fail**

  Run: `backend/venv/bin/python -m pytest backend/tests/test_llm_providers.py -q`
  Expected: FAIL (`fake_llm` is missing).

- [ ] **Step 3: Implement** the modules and the dispatch as specified.

- [ ] **Step 4: Run them and see them pass**

  Run: the same command, then `backend/venv/bin/python -m pytest backend/tests -q`.
  Expected: all pass.

- [ ] **Step 5: Commit**

  Message: `Add fake, record and replay LLM providers`.

---

### Task 3: Prompt caching, usage capture and cost

**Files:**
- Create: `backend/core/cost.py`
- Modify: `backend/core/llm.py`
- Modify: `backend/agent/loop.py` (`default_model_turn`)
- Modify: `backend/agent/orchestrator.py` (`Agent.generate_claude`)
- Modify: `backend/core/trace.py` (`Trace.summary`)
- Modify: `backend/core/config.py` (add `PROMPT_CACHE = _env("PROMPT_CACHE", "true").lower() == "true"` and
  `PRICE_TABLE_JSON = _env("PRICE_TABLE_JSON")`)
- Create: `backend/tools/check_prompt_cache.py`
- Test: `backend/tests/test_prompt_cache.py`, `backend/tests/test_cost.py`

**Interfaces:**
- **Consumes:** `fake_llm.requests` and `FakeReply` (Task 2).
- **Produces in `llm.py`:**
  - `system_param(text: str) -> str | list[dict]`: when caching is on it returns
    `[{"type":"text","text":text,"cache_control":{"type":"ephemeral","ttl":"1h"}}]`, otherwise `text` unchanged.
  - `stable_tools(tools: list[dict]) -> list[dict]`: sorted by `name`, a copy.
  - `cache_kwargs() -> dict`: `{"cache_control": {"type": "ephemeral"}}` or `{}`.
  - `_record_usage` also sets `cache_read_tokens` and `cache_write_tokens` on the span, plus `cache_write_1h_tokens`
    and `cache_write_5m_tokens` from `usage.cache_creation` when present.
- **Produces in `backend/core/cost.py`:**
  - `PRICES: dict[str, dict[str, float]]` (USD per million tokens) with the Global Constraints values for
    `claude-haiku-5-5`, merged with `PRICE_TABLE_JSON` when set.
  - `span_cost(attrs: dict) -> float`: unknown models cost 0.0. With no 5m/1h split, a cache write is priced at
    the 1-hour rate.
- **Produces in `Trace.summary()`:** adds `cache_read_tokens`, `cache_write_tokens`, `cache_hit_rate` and
  `cost_usd`. The hit rate is `read / (read + write + input_tokens)`, rounded to 3 places, and 0.0 when the
  denominator is 0. The cost is the rounded sum of `span_cost` over LLM spans, to 6 places.
- **All three call sites** (`haiku_json`, `default_model_turn`, `generate_claude`) use `system_param`, `stable_tools`
  (wherever tools are passed) and `**cache_kwargs()`.

- [ ] **Step 1: Write the failing tests**
  - `test_cache_markers_on_every_call`, run under the fake provider:
    - after `haiku_json` and one `run_tool_loop` step, every recorded request has a `system` list whose last block
      has `cache_control == {"type":"ephemeral","ttl":"1h"}`, and a top-level `cache_control == {"type":"ephemeral"}`;
    - the tools in the loop request are sorted by name, given unsorted input `[{"name":"b"...},{"name":"a"...}]`.
  - `test_prompt_cache_off_matches_legacy_request`: with `config.PROMPT_CACHE = False`, the recorded `haiku_json`
    kwargs equal exactly
    `{"model", "max_tokens", "system": <str>, "messages", "output_config"}`, the same keys and values as before
    this task.
  - `test_usage_and_cache_tokens_reach_trace`: a scripted fake usage of `input_tokens=100`, `output_tokens=50`,
    `cache_read_input_tokens=900`, `cache_creation_input_tokens=0` → `summary()["cache_hit_rate"] == 0.9`.
  - In `test_cost.py`:
    - `span_cost({"model":"claude-haiku-5-5","input_tokens":1_000_000}) == 0.10`;
    - `{"output_tokens":1_000_000}` → `0.50`;
    - `{"cache_read_tokens":1_000_000}` → `0.01`;
    - `{"cache_write_1h_tokens":1_000_000}` → `0.20`;
    - an unknown model → `0.0`;
    - `PRICE_TABLE_JSON='{"m":{"input":1}}'` → `{"model":"m","input_tokens":1_000_000}` costs `1.0`.

  `FakeReply` needs an optional `usage: dict | None` field for these tests. It is a small Task 2 addition, made
  in this task.

- [ ] **Step 2: Run them and see them fail**

  Run: `backend/venv/bin/python -m pytest backend/tests/test_prompt_cache.py backend/tests/test_cost.py -q`
  Expected: FAIL.

- [ ] **Step 3: Implement** the changes as specified. Also write `backend/tools/check_prompt_cache.py`, a manual
  live check that is never run by tests:
  - it sends two identical `haiku_json` calls with a system prompt of over 600 tokens;
  - it prints both usages;
  - it exits 1 if the second call's `cache_read_input_tokens == 0`.

- [ ] **Step 4: Run them and see them pass**

  Run: the same command, then `backend/venv/bin/python -m pytest backend/tests -q`.
  Expected: all pass.

- [ ] **Step 5: Live check (controller only, with the owner's Foundry key; costs well under $0.01)**

  Run: `backend/venv/bin/python -m backend.tools.check_prompt_cache`
  Expected: the second call shows `cache_read_input_tokens > 0`.
  If it shows 0, record a ledger ruling with the observed usage, don't change the code to force it, and report
  it.

- [ ] **Step 6: Commit**

  Message: `Add prompt caching, cache usage capture and per-trace cost`.

---

### Task 4: Redaction

**Files:**
- Create: `backend/core/redact.py`
- Modify: `backend/core/trace.py` (`Trace.log` prints `redact_obj(self.summary())`)
- Test: `backend/tests/test_redact.py`

**Interfaces:**
- **Produces:**
  - `redact(text: str) -> str`.
  - `redact_obj(obj: Any) -> Any`: recursive over dicts, lists and tuples; only strings change.
  - `SECRET_ENV_NAMES: tuple[str, ...] = ("GEMINI_API_KEY", "QDRANT_API_KEY", "TESTFORGE_FOUNDRY_API_KEY",
    "PREMIUM_PASSCODE", "HF_TOKEN", "GITHUB_TOKEN", "YOUTUBE_API_KEY")`. The exact current values of these env
    vars (when 6 characters or longer) are replaced with `[secret]`.
- **Replacements:**
  - emails → `[email]`;
  - phone numbers (7 or more digits with optional `+`, spaces, dots, dashes or parentheses, at least 10 digits
    in total) → `[phone]`;
  - these key shapes, with a word boundary before them → `[secret]`: `sk-ant-…`, `sk-…` (20+ characters),
    `hf_…` (20+), `ghp_…` / `github_pat_…`, `AKIA[0-9A-Z]{16}`, `AIza[0-9A-Za-z_-]{35}`;
  - `Bearer <token>` → `Bearer [secret]`.

- [ ] **Step 1: Write the failing tests**
  - `test_redacts_pii_and_keys`: for each of an email, `+1 (415) 555-0199`, `sk-ant-api03-abcdefghijklmnop`,
    `hf_abcdefghijklmnopqrstu`, `Authorization: Bearer abc.def.ghi` and a monkeypatched `PREMIUM_PASSCODE` value
    → the output has the placeholder and not the original.
  - `test_redact_leaves_ordinary_text`: `"task-based scheduling"`, `"watch 12:30–18:00"`, `"chunk 9f2c1a7b3e"`,
    `"https://example.com/docs?page=2"`, `"see [3]"` and `"in 2026"` are unchanged.
  - `test_redact_obj_nested`: `{"a": ["x@y.com", 3, {"b": "Bearer t"}]}` →
    `{"a": ["[email]", 3, {"b": "Bearer [secret]"}]}`.

- [ ] **Step 2: Run them and see them fail**

  Run: `backend/venv/bin/python -m pytest backend/tests/test_redact.py -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them and see them pass**

  Run: the same command, plus the full `backend/tests`.
  Expected: PASS.

- [ ] **Step 5: Commit**

  Message: `Redact personal data and secrets before traces are logged`.

---

### Task 5: OpenTelemetry export (platform)

**Files:**
- Create: `backend/tutor/telemetry.py`
- Modify: `backend/core/trace.py` (add `Trace.wall_t0: float = time.time()` set in `__init__`; no behaviour change)
- Test: `backend/tutor/tests/test_telemetry.py`

**Interfaces:**
- **Consumes:** `Trace`/`Span` (`backend/core/trace.py`) and `redact_obj` (Task 4).
- **Produces:**
  - `setup_tracing(endpoint: str | None, *, service_name: str = "roadmap-tutor", exporter=None) -> TracerProvider | None`:
    None when there is neither an endpoint nor an exporter. Otherwise a provider with a `BatchSpanProcessor`
    (or a `SimpleSpanProcessor` when `exporter` is given, for tests).
  - `export_trace(trace: Trace, provider: TracerProvider | None) -> None`:
    - one root span named `trace.name`, with one child per `Span`;
    - start and end times are `wall_t0 + (span.start - trace.t0)` in nanoseconds;
    - the attributes are `redact_obj(span.attrs)` flattened to OTel-safe primitives;
    - `openinference.span.kind` is mapped from `kind` (`llm`→`LLM`, `tool`→`TOOL`, else `CHAIN`);
    - `input_tokens`/`output_tokens` are also written as `llm.token_count.prompt` / `llm.token_count.completion`;
    - span status is ERROR when the span status is `error`;
    - it never raises: exceptions are logged once and swallowed.

- [ ] **Step 1: Write the failing tests** (they use `InMemorySpanExporter`):
  - `test_export_builds_parent_child_spans`: a Trace with spans `route` (kind step) and `agent_turn` (kind llm,
    `input_tokens=5`) → 3 exported spans; both children have the root as parent; `agent_turn` has
    `openinference.span.kind == "LLM"` and `llm.token_count.prompt == 5`.
  - `test_exported_attributes_are_redacted`: a span attribute `query="mail me at a@b.com"` is exported as
    `"mail me at [email]"`.
  - `test_error_span_status`: a span that raised inside `trace.span` → exported with ERROR status.
  - `test_export_failure_is_swallowed`: an exporter whose `export` raises → `export_trace` returns normally.
    Also `setup_tracing("http://127.0.0.1:9/v1/traces")` + `export_trace` + `provider.force_flush(timeout_millis=2000)`
    doesn't raise.
  - `test_no_endpoint_is_noop`: `setup_tracing(None) is None`, and `export_trace(trace, None)` does nothing.

- [ ] **Step 2: Run them and see them fail**

  Run: `backend/venv/bin/python -m pytest backend/tutor/tests/test_telemetry.py -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them and see them pass**

  Run: the same command, plus `backend/tests`.
  Expected: PASS.

- [ ] **Step 5: Commit**

  Message: `Export request traces to OpenTelemetry`.

---

### Task 6: Database, models and migrations

**Files:**
- Create: `backend/tutor/db.py`
- Create: `backend/tutor/models.py`
- Create: `backend/tutor/repo.py`
- Create: `backend/tutor/alembic.ini`
- Create: `backend/tutor/migrations/env.py`, `script.py.mako` and `versions/0001_initial.py`
- Create: `backend/tutor/tests/conftest.py`
- Test: `backend/tutor/tests/test_db.py`

**Interfaces:**
- **Produces in `db.py`:**
  - `make_engine(url: str) -> Engine`, using `pool_pre_ping=True`;
  - `session_scope(engine) -> ContextManager[Session]`, which commits on success and rolls back on error;
  - `migrate(url: str, revision: str = "head") -> None`, which runs Alembic programmatically;
  - `downgrade(url: str, revision: str = "base") -> None`.
- **Produces in `models.py`:** `Base` and `Roadmap`. The `roadmaps` table has:
  - `id` UUID primary key, default `uuid4`;
  - `slug` text, unique, not null;
  - `title` text, not null;
  - `source_text` text, not null;
  - `spec` JSONB, nullable;
  - `status` text, not null, default `'draft'`;
  - `created_at` timestamptz, server default `now()`.
- **Produces in `repo.py`:**
  - `SLUG_RE = r"^[a-z0-9][a-z0-9-]{1,62}$"`;
  - `MAX_SOURCE_CHARS = 200_000`;
  - `DuplicateRoadmap(Exception)`;
  - `create_roadmap(session, *, slug: str, title: str, source_text: str) -> Roadmap`, which raises `ValueError`
    with a human-readable message for a bad slug, an empty or whitespace title, or source text over the limit,
    and `DuplicateRoadmap` for an existing slug;
  - `list_roadmaps(session) -> list[Roadmap]`, newest first.
- **The `0001_initial` migration:** `CREATE EXTENSION IF NOT EXISTS vector`, plus the `roadmaps` table. Its
  downgrade drops the table (and leaves the extension).
- **The `conftest.py` fixtures:**
  - `db_url` (session scope): it connects to the server in `DATABASE_URL`, creates a database
    `tutor_test_<8 hex>`, migrates it, yields its URL and drops it.
  - If `DATABASE_URL` is unset, it skips, unless `REQUIRE_DB=1`, in which case it fails.
  - `engine` comes from `db_url`.

- [ ] **Step 1: Write the failing tests** (start Postgres with `docker run -d --rm -p 127.0.0.1:5432:5432
  -e POSTGRES_USER=tutor -e POSTGRES_PASSWORD=tutor pgvector/pgvector:pg16`, then export
  `DATABASE_URL=postgresql+psycopg://tutor:tutor@127.0.0.1:5432/postgres`):
  - `test_migration_creates_schema`: after `migrate`, `pg_extension` contains `vector` and the `roadmaps` table
    exists. After `downgrade(url, "base")` the table is gone. After `migrate` again it is back.
  - `test_create_and_list_roadmaps`: two creates → `list_roadmaps` returns both, newest first.
  - `test_duplicate_slug`: the second create with the same slug raises `DuplicateRoadmap`.
  - `test_validation`: each of `slug="Bad Slug"`, `slug="a"`, `title="  "` and
    `source_text="x" * 200_001` raises `ValueError`. A source text of 200,000 characters with emoji is accepted.

- [ ] **Step 2: Run them and see them fail**

  Run: `backend/venv/bin/python -m pytest backend/tutor/tests/test_db.py -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them and see them pass**

  Run: the same command.
  Expected: PASS.

- [ ] **Step 5: Commit**

  Message: `Add Postgres schema, migrations and roadmap repository`.

---

### Task 7: Platform API

**Files:**
- Create: `backend/tutor/api.py`
- Create: `backend/tutor/__main__.py`
- Test: `backend/tutor/tests/test_api.py`

**Interfaces:**
- **Consumes:** `Settings`/`load_settings` (Task 1), `make_engine`/`session_scope`/`migrate` (Task 6),
  `create_roadmap`/`list_roadmaps`/`DuplicateRoadmap` (Task 6), `setup_tracing`/`export_trace` (Task 5),
  `start_trace` (`backend/core/trace.py`), and `access.check_passcode`/`TooManyAttempts`/`client_ip`
  (`backend/agent/access.py`).
- **Produces `create_app(settings: Settings, *, engine: Engine | None = None) -> FastAPI`:**
  - **Startup:** `settings.validate()`, `migrate(settings.database_url)` (skipped when an `engine` is injected),
    `setup_tracing(settings.otel_endpoint)`.
  - **Tracing middleware:** each request runs inside `start_trace(f"{method} {route}")`, the response carries
    `X-Trace-Id`, and `export_trace` runs after the response.
  - **Public-mode middleware** (when `settings.public_mode`): any path starting with `/api/`, other than
    `/api/health`, needs header `X-Access-Code`. A missing or wrong code gets 401
    `{"error": "Passcode required"}`; `TooManyAttempts` gets 429 `{"error": <message>}`.
  - **`GET /api/health`:** 200 `{"status":"ok","db":"ok","llm_provider":config.LLM_PROVIDER,"public_mode":bool}`.
    When `SELECT 1` fails, it returns 503 with `"status":"degraded","db":"down"`.
  - **`GET /api/roadmaps`:** 200 `[{"id","slug","title","status","created_at"}]`.
  - **`POST /api/roadmaps`** with body `{"slug","title","source_text"}`: 201 with the same shape, 409
    `{"error":"A roadmap with slug '<slug>' already exists"}`, or 422 `{"error": <ValueError message>}`.
  - **Database errors** (`sqlalchemy.exc.OperationalError`) on any route → 503 `{"error":"Database unavailable"}`.
  - **Static files:** when `settings.web_dist/index.html` exists, files under it are served, and any non-`/api`
    GET without a matching file returns `index.html` (SPA fallback).
- **Produces `python -m backend.tutor`:** runs uvicorn on `settings.host:settings.port`.

- [ ] **Step 1: Write the failing tests** (`TestClient`, the `engine` fixture from Task 6, `LLM_PROVIDER=fake`):
  - `test_health_ok`: 200, `db == "ok"`, `llm_provider == "fake"`, and an `X-Trace-Id` header is present.
  - `test_create_and_list` and `test_duplicate_returns_409`: the message names the slug.
  - `test_create_roadmap_validation`: `"Bad Slug"`, an empty title and 200,001 characters each return 422 with an
    `error` string. A missing field returns 422 (FastAPI's shape is fine).
  - `test_public_mode_guard`, with `public_mode=True` and a monkeypatched `config.PREMIUM_PASSCODE="pw123456"`:
    - `/api/health` → 200 with no code;
    - `/api/roadmaps` with no code → 401, with a wrong code → 401, with the right code → 200;
    - after 5 wrong codes from the same IP → 429;
    - reset `access._failures` between tests.
  - `test_db_down_returns_503`: an app with `engine=make_engine("postgresql+psycopg://x:y@127.0.0.1:9/z")` →
    `/api/health` returns 503 with `db == "down"`, and `/api/roadmaps` returns 503 with JSON
    `{"error":"Database unavailable"}`.
  - `test_spa_fallback`: `web_dist=tmp_path` holding `index.html` ("<h1>app</h1>") → `GET /roadmaps/abc` returns
    that HTML, and `GET /api/nope` returns 404 JSON, not HTML.
  - `test_startup_refuses_public_mode_without_passcode`: entering `TestClient(create_app(...))` raises
    `SettingsError`.

- [ ] **Step 2: Run them and see them fail**

  Run: `backend/venv/bin/python -m pytest backend/tutor/tests/test_api.py -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them and see them pass**

  Run: `backend/venv/bin/python -m pytest backend/tutor/tests backend/tests -q`
  Expected: PASS.

- [ ] **Step 5: Commit**

  Message: `Add platform API with health, roadmaps and public-mode guard`.

---

### Task 8: Web app shell (React + Vite + TypeScript) with Playwright E2E

**Files:**
- Create `web/`:
  - `package.json` and `package-lock.json`;
  - `tsconfig.json`, `vite.config.ts`, `index.html`;
  - `src/main.tsx`, `src/App.tsx`, `src/api.ts`, `src/storage.ts`, `src/styles.css`;
  - `src/api.test.ts`;
  - `playwright.config.ts`, `e2e/roadmaps.spec.ts`, `e2e/passcode.spec.ts`.

**Interfaces:**
- **Consumes:** the Task 7 HTTP API.
- **Produces in `src/api.ts`:**
  - `class ApiError extends Error { status: number }`;
  - `getHealth(): Promise<Health>`;
  - `listRoadmaps(): Promise<Roadmap[]>`;
  - `createRoadmap(input: {slug: string; title: string; source_text: string}): Promise<Roadmap>`.
  - Every request sends `X-Access-Code` when `storage.getCode()` returns one. Non-2xx responses throw `ApiError`
    with the server's `error` message.
- **Produces in `src/storage.ts`:** `getCode(): string | null` and `setCode(code: string): void`. Both wrap
  `localStorage` in try/catch and fall back to an in-memory value.
- **UI** (the design choices are the builder's, within the spec's frontend section):
  - a header with the app name and a health badge (LLM provider; database ok or down);
  - a Roadmaps page with the list (title, slug, status, created date) and a create form (slug, title, a
    "Paste your roadmap" textarea);
  - the server's error message shown inline;
  - a passcode screen whenever the API returns 401;
  - colour tokens on `:root` with a dark-mode override;
  - usable at 375 px width.
- **Scripts:** `dev`, `build` (`tsc --noEmit && vite build`), `test` (vitest) and `e2e` (playwright).
- **Versions:** Node ≥ 22 in `engines`; current stable React, Vite, TypeScript, Vitest and Playwright pinned
  exactly, with the lockfile committed.
- **Dev server:** proxies `/api` to `http://127.0.0.1:8000`.
- **`playwright.config.ts`:** two `webServer` entries running `python -m backend.tutor`, both with
  `LLM_PROVIDER=fake`, `WEB_DIST=web/dist`, and `DATABASE_URL` from the environment (each creates its own
  database name):
  - port 8765, normal;
  - port 8766, `PUBLIC_MODE=true`, `PREMIUM_PASSCODE=e2e-pass-123`.

- [ ] **Step 1: Write the failing tests**
  - In Vitest `api.test.ts`:
    - `sends access code when stored`;
    - `throws ApiError with server message on 409`;
    - `works when localStorage throws`: stub it to throw on access, then `setCode` and `getCode` round-trip in
      memory and requests still go out.
  - In `e2e/roadmaps.spec.ts`:
    - create `my-roadmap` / "My Roadmap" → it appears in the list;
    - creating it again shows text containing "already exists";
    - the health badge shows "fake".
  - In `e2e/passcode.spec.ts` (port 8766): the passcode screen shows → a wrong code shows an error → the right
    code shows the Roadmaps page.

- [ ] **Step 2: Run them and see them fail**

  Run: `cd web && npm ci && npm test`, then `npm run build && npx playwright install chromium && npm run e2e`
  (with Postgres from Task 6 running and `DATABASE_URL` exported).
  Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them and see them pass**

  Run: the same commands.
  Expected: Vitest and Playwright pass, and `npm run build` succeeds with no type errors.

- [ ] **Step 5: Commit**

  Message: `Add React web app shell with Playwright end-to-end tests`.

---

### Task 9: Docker images, compose stack and tunnel

**Files:**
- Create: `docker/api.Dockerfile`
- Create: `compose.yaml`
- Create: `compose.tunnel.yaml`
- Create: `docs/platform/running-locally.md`
- Modify: `README.md` (add a short "Roadmap Tutor platform (in development)" section after the demo sections,
  linking the doc; the HF YAML frontmatter stays first and unchanged)
- Modify: `.dockerignore` (exclude `web/node_modules`, `web/dist`, `**/.env`)

**Interfaces:**
- **Consumes:** `python -m backend.tutor` (Task 7) and `web` `npm run build` (Task 8).
- **`docker/api.Dockerfile`:**
  - a multi-stage build: `node:22-slim` builds `web/dist`, then `python:3.12-slim` with CPU-only torch and
    `requirements-platform.txt`;
  - copies `backend/` and `web/dist`;
  - runs as a non-root user;
  - `ENV HOST=0.0.0.0 PORT=8000 WEB_DIST=/app/web/dist`;
  - a `HEALTHCHECK` that fetches `/api/health` with Python's urllib;
  - `CMD ["python","-m","backend.tutor"]`.
- **`compose.yaml` services** (pin the image tags; ports only on 127.0.0.1):
  - **`api`:**
    - builds from `docker/api.Dockerfile`;
    - `env_file: backend/.env` (required: false);
    - environment `DATABASE_URL=postgresql+psycopg://tutor:tutor@postgres:5432/tutor`,
      `PLATFORM_QDRANT_URL=http://qdrant:6333` and
      `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://phoenix:6006/v1/traces`;
    - `127.0.0.1:8000:8000`;
    - `depends_on` postgres (healthy) and qdrant.
  - **`postgres`:** `pgvector/pgvector:pg16`, user, password and db `tutor`, volume `pgdata`, a `pg_isready`
    healthcheck, no host port.
  - **`qdrant`:** `qdrant/qdrant:<pinned>`, volume `qdrant_data`, `127.0.0.1:6333:6333`.
  - **`phoenix`:** profile `observability`, `arizephoenix/phoenix:<pinned>`, `127.0.0.1:6006:6006`.
- **`compose.tunnel.yaml`:**
  - sets `PUBLIC_MODE=true` on `api`;
  - adds `cloudflared` (`cloudflare/cloudflared:<pinned>`) with
    `command: tunnel --no-autoupdate --url http://api:8000`.
  - Usage: `docker compose -f compose.yaml -f compose.tunnel.yaml up`.
- **`docs/platform/running-locally.md`:**
  - prerequisites;
  - `cp backend/.env.example backend/.env`;
  - `docker compose up -d --wait`;
  - opening `http://127.0.0.1:8000`;
  - the `observability` profile, with Phoenix at `http://127.0.0.1:6006`;
  - the tunnel: finding the `trycloudflare.com` URL with `docker compose logs cloudflared`, that the passcode is
    required, and how to stop it;
  - and the "Why not hosted?" paragraph, from the spec's Hosting section.

- [ ] **Step 1: Write the failing check**

  Run: `docker compose config -q && docker compose -f compose.yaml -f compose.tunnel.yaml config | grep -q 'PUBLIC_MODE: "true"'`
  Expected: FAIL (no compose file).

- [ ] **Step 2: Implement** the files.

- [ ] **Step 3: Verify the config**

  Run: the same command.
  Expected: exit 0. Also, `docker compose config` shows no published port bound to anything but `127.0.0.1`
  (grep `published` together with `host_ip`).

- [ ] **Step 4: Verify the running stack**

  Run: `docker compose up -d --build --wait && curl -fsS http://127.0.0.1:8000/api/health && docker compose down`
  Expected: JSON with `"status":"ok"` and `"db":"ok"`.

  Then run:
  `docker compose --profile observability up -d --wait && curl -fsS http://127.0.0.1:8000/api/health >/dev/null && sleep 5 && curl -fsS http://127.0.0.1:6006 >/dev/null && docker compose --profile observability down`
  Expected: exit 0, and the `GET /api/health` trace is visible in Phoenix (check by hand once and note it in the
  ledger).

- [ ] **Step 5: Verify the multi-arch build**

  Run: `docker buildx build --platform linux/amd64,linux/arm64 -f docker/api.Dockerfile .`
  Expected: success. If the local builder can't do multi-arch, CI (Task 10) is the gate; note that in the
  ledger.

- [ ] **Step 6: Commit**

  Message: `Add compose stack, API image and Cloudflare tunnel profile`.

---

### Task 10: CI for the platform

**Files:**
- Modify: `.github/workflows/ci.yml` (add jobs only; `unit-tests` and `retrieval-quality-gate` stay byte-identical)

**Interfaces:**
- **Consumes:** every earlier task.
- **New jobs:**
  - **`platform-tests`:**
    - a `pgvector/pgvector:pg16` service on 5432, user and password `tutor`;
    - install CPU torch, then `requirements-platform.txt`;
    - env `REQUIRE_DB=1`, `DATABASE_URL=postgresql+psycopg://tutor:tutor@localhost:5432/postgres` and
      `LLM_PROVIDER=fake`;
    - run `python -m pytest backend/tests backend/tutor/tests -q`.
  - **`web`:**
    - the same Postgres service and Python setup;
    - `actions/setup-node` with Node 22;
    - `npm ci`, `npm test`, `npm run build`, `npx playwright install --with-deps chromium`, `npm run e2e`, all in
      `web/`;
    - upload the Playwright report on failure.
  - **`compose-smoke`:**
    - `docker compose up -d --build --wait`, then `curl -fsS http://127.0.0.1:8000/api/health`, then
      `docker compose down -v`;
    - `docker/setup-qemu-action` + `docker/setup-buildx-action`, then a `buildx build --platform linux/amd64,linux/arm64`
      of `docker/api.Dockerfile` without pushing.
- **Triggers:** add the feature branch name to `on.push.branches`. Pull requests are already covered.

- [ ] **Step 1: Validate the workflow locally**

  Run: `actionlint .github/workflows/ci.yml` (install it with `brew install actionlint` if it's missing).
  Expected: no errors.

- [ ] **Step 2: Push the branch** (controller only, after the owner's go-ahead on pushing) **and watch the run**

  Run: `gh run watch` on the branch's run.
  Expected: all five jobs green. `retrieval-quality-gate` may skip if the secrets are unset; that's
  pre-existing behaviour.

- [ ] **Step 3: Commit**

  Message: `Run platform, web and compose checks in CI`.

---

## Done when
- [ ] `backend/venv/bin/python -m pytest backend/tests -q` passes with only `requirements.txt` + `pytest`
  installed. Verify in a fresh venv once.
- [ ] `pytest backend/tests backend/tutor/tests` passes with Postgres running.
- [ ] `npm test` and `npm run e2e` pass.
- [ ] `docker compose up -d --wait` serves the web app at `http://127.0.0.1:8000`. Creating a roadmap there
  works, and with the `observability` profile its trace appears in Phoenix.
- [ ] The tunnel overlay serves the app publicly behind the passcode (manual check by the owner).
- [ ] The live prompt-cache check shows cache reads (or a ledgered ruling explains why not).
- [ ] `deploy_to_hf.py`'s ignore list keeps all platform files off the Space. Nothing in this phase redeploys
  the Space.
