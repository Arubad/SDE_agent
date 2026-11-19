import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const webDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(webDir, "..");
const pgBase = (process.env.E2E_PG_URL ?? "postgresql+psycopg://tutor:tutor@127.0.0.1:5432").replace(/\/+$/, "");
const command = `${process.env.PYTHON ?? "backend/venv/bin/python"} -m backend.tutor --create-db`;

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
    server(8765, "tutor_e2e_main", { PUBLIC_MODE: "false" }),
    server(8766, "tutor_e2e_public", { PUBLIC_MODE: "true", PREMIUM_PASSCODE: "e2e-pass-123" }),
  ],
});
