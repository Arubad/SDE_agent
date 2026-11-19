# Roadmap Tutor Phase 1: deferred follow-ups

Minor findings from the Phase 1 task reviews and the final whole-branch review. None of them blocks merging.
The final reviewer triaged them as safe to leave for later phases. Items the final fix wave already
resolved are not listed.

## Settings and API
- `Settings.__repr__` includes `database_url`, which contains credentials. Use `field(repr=False)`.
- A non-numeric `PORT` raises a bare `ValueError` instead of `SettingsError`. Env overrides other than
  `PUBLIC_MODE` are untested.
- `/api/health`'s 503 body is discarded by the web client, so the provider name is lost exactly when
  diagnosing.
- Trace names fall back to the raw path for unmatched routes (401/405), so arbitrary internet paths
  become span names. Unknown non-GET paths return 405, not 404.
- `CF-Connecting-IP` is trusted from any client. That is only safe while the app is reachable just through
  the tunnel or 127.0.0.1, which is the current design.
- The create-form input is lost when a 401 swaps to the passcode screen.

## LLM layer, cost and redaction
- `example_from_schema` lacks `anyOf`/`oneOf`/`$ref`/`const`. `script()` clears the whole queue on exit,
  so nested scripts drop the outer ones.
- `test_record_then_replay` doesn't prove the replay served the file. Async recording only happens in
  `get_final_message`.
- **Recordings are written unredacted.** They are now kept off the Space. Redact them before the first
  recording is committed (Phase 6 evals).
- A malformed `PRICE_TABLE_JSON` crashes at import, on the Space too. It is parsed twice.
- There is no end-to-end test of `Trace.summary()["cost_usd"]` for a priced model.
- Phone-redaction trade-offs (per the Task 4 ruling):
  - exactly-10-digit numbers, such as epoch seconds and IDs in URLs, are redacted;
  - `415 555 0199` and `1-415-555-0199` are not.
- There are no tests for the `github_pat_`, `AKIA` or Bearer-in-JSON shapes.

## Telemetry
- A partial export failure leaves the root span un-ended, and the root isn't marked ERROR when a child
  errors.
- The guarded-exporter warning has no cause or endpoint detail, and the OTLP logger is silenced process-wide.
- The 10 s healthcheck creates a `/api/health` trace each time. Exclude health from tracing.

## Database
- `IntegrityError` is mapped to `DuplicateRoadmap` unconditionally. Check the constraint name.
- `create_roadmap` rolls back the caller's whole session on a duplicate. A savepoint would isolate it.
- Tests don't assert error message text. A failing migration round-trip test can leave the shared test DB
  downgraded.
- `SLUG_RE` allows `a-` and `a--b`. This was mandated by the plan.

## Containers and CI
- The node build stage lacks `--platform=$BUILDPLATFORM`, so the multi-arch CI build is slow. The
  multi-arch build is also tied to the compose-smoke job and has no layer cache.
- `.dockerignore` misses `**/.env.*` variants, nested `.DS_Store` and `.git`.
- Base images aren't digest-pinned, and neither is torch.
- Phoenix has no volume, and Qdrant has no readiness wait.
- `docker compose --profile observability down` stops the whole stack. `docker compose stop phoenix` stops
  only Phoenix.
- Playwright's CI retry, combined with the passcode spec's wrong attempts, works but is close to the lockout
  limit.
