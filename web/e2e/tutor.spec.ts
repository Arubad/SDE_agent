import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

// Runs against the tutor E2E server (own database, fake LLM, ingestion summaries off): consent, a fixture
// ingest to a ready roadmap, a tutor session with a quiz, the curator's summary on the next session, export.
const here = path.dirname(fileURLToPath(import.meta.url));
const fixtures = path.resolve(here, "../../backend/tutor/tests/fixtures");
const roadmapText = readFileSync(path.join(fixtures, "roadmaps/kubernetes.txt"), "utf-8");
const quiz = JSON.parse(readFileSync(path.join(fixtures, "llm_replies/Quiz.json"), "utf-8")) as {
  questions: { prompt: string; answer: string }[];
};
const curated = JSON.parse(readFileSync(path.join(fixtures, "llm_replies/SessionCurate.json"), "utf-8")) as {
  title: string;
  summary: string;
};
const slug = `tutor-${Date.now()}`;
const SETTLE = { timeout: 90_000 };

test.use({ baseURL: "http://127.0.0.1:8767" });
test.describe.configure({ mode: "serial", retries: 0 });

test("consent, ingest, session with a quiz, curated summary on the next session, export", async ({ page, request }) => {
  test.setTimeout(240_000);

  // 1. First run: the consent dialog. The answer lives in the database, so a re-run against a reused server
  // finds it already answered and skips this part.
  const before = (await (await request.get("/api/learner/settings")).json()) as { consent_personal: boolean | null };
  await page.goto("/");
  if (before.consent_personal === null) {
    const dialog = page.getByRole("dialog", { name: "Your privacy" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel("Remember personal facts")).not.toBeChecked();
    await expect(dialog.getByLabel("Keep raw chat messages")).not.toBeChecked();
    await dialog.getByRole("button", { name: "Save" }).click();
    await expect(dialog).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole("heading", { name: "Roadmaps", exact: true })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0); // never again once answered
  }

  // 2. A ready roadmap through the fixture ingest.
  await page.getByLabel("Slug").fill(slug);
  await page.getByLabel("Title").fill("Kubernetes");
  await page.getByLabel("Paste your roadmap").fill(roadmapText);
  await page.getByRole("button", { name: "Create roadmap" }).click();
  await page.getByRole("listitem").filter({ hasText: slug }).getByRole("button", { name: "Open" }).click();
  await page.getByRole("button", { name: "Parse roadmap" }).click();
  await page.getByRole("button", { name: "Approve and ingest" }).click();
  await expect(page.getByTestId("ingest-report")).toBeVisible(SETTLE);
  await expect(page.getByRole("tab", { name: "Tutor" })).toBeEnabled();

  // 3. Start a session; today's card shows.
  await page.getByRole("tab", { name: "Tutor" }).click();
  await page.getByRole("button", { name: "Start", exact: true }).click();
  await expect(page.getByRole("region", { name: "Today" })).toBeVisible(SETTLE);

  // 4. A question gets the fake answer and a timeline row.
  const composer = page.getByRole("textbox", { name: "Message" });
  await composer.fill("What is a Pod?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("log", { name: "Conversation" }).getByText("Fake answer.").first()).toBeVisible(SETTLE);
  const timeline = page.getByRole("region", { name: "Agent timeline" });
  await expect(timeline.getByText(/^Turn \d+/).first()).toBeVisible();

  // 5. Quiz me: the card from Quiz.json, with the composer still usable while it is pending.
  await page.getByRole("button", { name: "Quiz me" }).click();
  const card = page.getByRole("form", { name: "Quiz" });
  await expect(card).toBeVisible(SETTLE);
  for (const q of quiz.questions) await expect(card.getByText(q.prompt)).toBeVisible();
  await expect(composer).toBeEnabled();

  // 6. Answer correctly; the result shows and the skill map has a skill in progress.
  for (const q of quiz.questions) await card.getByRole("radio", { name: q.answer, exact: true }).check();
  await card.getByRole("button", { name: "Submit answers" }).click();
  await expect(page.getByText(/Quiz graded: mastery 0% → 35%/)).toBeVisible(SETTLE);
  await page.getByRole("tab", { name: "Skill map" }).click();
  const list = page.getByRole("region", { name: "Skills list" });
  await expect(list.locator('li[data-state="learning"]')).toHaveCount(1);
  await expect(page.getByRole("img", { name: /Skill map:/ })).toBeVisible();
  await list.locator('li[data-state="learning"] button').click();
  await expect(page.getByRole("complementary", { name: /^Skill:/ }).getByText("Mastery: 35%")).toBeVisible();

  // 7. End the session; the curator runs on the worker and its summary lands on the Sessions tab.
  await page.getByRole("tab", { name: "Tutor" }).click();
  await page.getByRole("button", { name: "End session" }).click();
  await expect(page.getByText("Ended session (read-only)")).toBeVisible(SETTLE);
  await page.getByRole("tab", { name: "Sessions" }).click();
  await expect(async () => {
    await page.getByRole("tab", { name: "Overview" }).click();
    await page.getByRole("tab", { name: "Sessions" }).click();
    await expect(page.getByText(curated.summary)).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 90_000, intervals: [1_000, 2_000] });

  // 8. A new session offers to resume with that summary.
  await page.getByRole("tab", { name: "Tutor" }).click();
  // With no active session the Tutor tab opens on the start panel.
  await page.getByRole("button", { name: "Start", exact: true }).click();
  const welcome = page.getByRole("region", { name: "Start of session" });
  await expect(welcome).toContainText(curated.title, SETTLE);
  await expect(welcome).toContainText(curated.summary);

  // 9. Memory & privacy: the export downloads. The active session shows "End it first".
  await page.getByRole("link", { name: "Memory & privacy" }).click();
  await expect(page.getByRole("heading", { name: "Memory & privacy" })).toBeVisible();
  await expect(page.getByText("End it first").first()).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export" }).click();
  expect((await download).suggestedFilename()).toBe("roadmap-tutor-export.json");
});
