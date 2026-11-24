import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const webDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(webDir, "..");
const pgBase = (process.env.E2E_PG_URL ?? "postgresql+psycopg://tutor:tutor@127.0.0.1:5432").replace(/\/+$/, "");
const command = `${process.env.PYTHON ?? "backend/venv/bin/python"} -m backend.tutor --create-db`;
const fixtures = path.join(repoRoot, "backend", "tutor", "tests", "fixtures");

// Ingestion runs for real inside the server, offline: the fixture net (pages, search results, YouTube), the
// parse reply keyed by schema title, a hash embedder and an in-memory Qdrant.
const ingestEnv = {
  NET_FIXTURES_DIR: path.join(fixtures, "net"),
  FAKE_LLM_REPLIES_DIR: path.join(fixtures, "llm_replies"),
  EMBEDDER: "hash",
  PLATFORM_QDRANT_URL: ":memory:",
  INGEST_WORKERS: "1",
};

function server(port: number, database: string, extraEnv: Record<string, string>) {
  return {
    command,
    cwd: repoRoot,
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      HOST: "127.0.0.1",
      PORT: String(port),
      DATABASE_URL: `${pgBase}/${database}`,
      WEB_DIST: path.join(webDir, "dist"),
      LLM_PROVIDER: "fake",
      // backend/.env would otherwise override these values (config loads it with override=True).
      SKIP_DOTENV: "1",
      ...ingestEnv,
      ...extraEnv,
    },
  };
}

export default defineConfig({
  testDir: "./e2e",
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://127.0.0.1:8765",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    // The ingestion agent is on, driven by a scripted tool-use sequence (one proposal in the first recovery
    // round; see e2e/fixtures/agent-script.jsonl). The script is consumed once per server process, so a
    // reused main server (reuseExistingServer, local only) must be restarted before ingest.spec.ts runs again.
    server(8765, "tutor_e2e_main", {
      PUBLIC_MODE: "false",
      INGEST_AGENT: "true",
      FAKE_LLM_SCRIPT: path.join(webDir, "e2e", "fixtures", "agent-script.jsonl"),
    }),
    // Wrong-passcode lockout lives in the server's memory (5 failures per IP per 15 minutes). A reused
    // public server (reuseExistingServer, local only) keeps that state between runs; passcode.spec.ts
    // makes two wrong attempts per run, so a reused server stays under the limit for two runs only.
    // Stop it (or wait 15 minutes) before a third.
    // The tutor chat spec: its own database and port, ingestion summaries off so the quiz comes from Quiz.json,
    // and the agent off. The consent answer is global and persists in the database between runs.
    server(8767, "tutor_e2e_tutor", { PUBLIC_MODE: "false", INGEST_AGENT: "false", INGEST_SUMMARIES: "false" }),
    server(8766, "tutor_e2e_public", { PUBLIC_MODE: "true", PREMIUM_PASSCODE: "e2e-pass-123", INGEST_AGENT: "false" }),
  ],
});
