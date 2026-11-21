import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

// Runs against the main E2E server: fixture net, the Kubernetes parse reply from FAKE_LLM_REPLIES_DIR, and the
// ingestion agent scripted (e2e/fixtures/agent-script.jsonl) to propose one page in its first recovery round.
const here = path.dirname(fileURLToPath(import.meta.url));
const roadmapText = readFileSync(
  path.resolve(here, "../../backend/tutor/tests/fixtures/roadmaps/kubernetes.txt"),
  "utf-8",
);
const slug = `k8s-${Date.now()}`;
const SETTLE = { timeout: 90_000 };

// No retries here, even in CI: the agent's FAKE_LLM_SCRIPT is single-use per server process, and Playwright keeps
// the webServer across retries, so a retried run would find the script spent and get no proposal. To re-run
// locally, restart the main E2E server (stop any reused one on port 8765).
test.describe.configure({ mode: "serial", retries: 0 });

test("parse, review, approve, then watch ingestion settle into a report", async ({ page }) => {
  test.setTimeout(150_000);
  await page.goto("/");
  await page.getByLabel("Slug").fill(slug);
  await page.getByLabel("Title").fill("Kubernetes");
  await page.getByLabel("Paste your roadmap").fill(roadmapText);
  await page.getByRole("button", { name: "Create roadmap" }).click();
  await page.getByRole("listitem").filter({ hasText: slug }).getByRole("button", { name: "Open" }).click();

  await page.getByRole("button", { name: "Parse roadmap" }).click();
  const review = page.getByTestId("spec-review");
  await expect(review).toBeVisible();
  await expect(review.getByTestId("stage")).toHaveCount(4);
  await expect(review.getByLabel("Stage title").first()).toHaveValue("Mental Models & Local Cluster Setup");
  await expect(review.getByText("Linked for you, not ingested")).toBeVisible();

  // Resolved resources come preselected with their top candidate.
  const course = review.getByRole("group", { name: "Kubernetes Tutorial for Beginners [FULL COURSE in 4 Hours]" });
  const top = course.getByRole("radio", { name: /youtube\.com · 0\.85/ });
  await expect(top).toBeChecked();
  const approve = page.getByRole("button", { name: "Approve and ingest" });
  await expect(approve).toBeEnabled();

  // A chosen "paste" with no URL blocks approval until it is filled in or another option is picked.
  await course.getByRole("radio", { name: "Paste a URL" }).check();
  await expect(approve).toBeDisabled();
  await course.getByLabel("URL to ingest").fill("ftp://not-allowed.example");
  await expect(approve).toBeDisabled();
  await top.check();
  await expect(approve).toBeEnabled();

  // Skip one resource; titles are editable inline.
  const cka = review.getByRole("group", { name: "Certified Kubernetes Administrator Exam Preparation (2026 Update)" });
  await cka.getByRole("radio", { name: "Skip" }).check();
  await review.getByLabel("Stage title").nth(3).fill("Troubleshooting & Capstone");
  await approve.click();

  await expect(page.getByTestId("ingest-progress")).toBeVisible();
  await expect(page.getByTestId("ingest-progress").getByRole("heading", { name: "Ingesting" })).toBeVisible();

  const report = page.getByTestId("ingest-report");
  await expect(report).toBeVisible(SETTLE);
  await expect(report.getByTestId("report-summary")).toContainText("videos:");
  await expect(report.getByRole("row").filter({ hasText: "Killercoda" })).toContainText("Linked for you, not ingested");
  await expect(report.getByRole("row").filter({ hasText: "Certified Kubernetes" })).toContainText("No URL chosen");
  // The concepts page links to a page the fixture net does not have.
  await expect(report.getByTestId("skipped")).toContainText("Page not found (HTTP 404)");

  const agent = report.getByTestId("agent-activity");
  await expect(agent).toContainText("propose_resource");
  await expect(agent).toContainText("web_search");
  await expect(agent).toContainText("Run cost");
});

test("agent proposal can be approved", async ({ page }) => {
  test.setTimeout(150_000);
  await page.goto(`/#/roadmaps/${slug}`);
  const report = page.getByTestId("ingest-report");
  await expect(report).toBeVisible(SETTLE);

  const proposal = report.getByTestId("proposal");
  await expect(proposal).toHaveCount(1);
  await expect(proposal).toContainText("Kubernetes introduction");
  await expect(proposal).toContainText("example.test");
  await expect(proposal).toContainText("No resource covers debugging");
  await expect(proposal).toContainText("Debugging");
  await proposal.getByRole("button", { name: "Approve" }).click();

  const progress = page.getByTestId("ingest-progress");
  await expect(progress).toBeVisible();
  await expect(progress.getByRole("listitem").filter({ hasText: "Kubernetes introduction" })).toBeVisible();

  await expect(report).toBeVisible(SETTLE);
  await expect(report.getByRole("row").filter({ hasText: "Kubernetes introduction" })).toContainText("done");
  await expect(report.getByTestId("proposal")).toHaveCount(0);

  // The report (tables included) fits a phone screen.
  await page.setViewportSize({ width: 375, height: 740 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("a 401 on the progress stream returns to the passcode screen", async ({ page }) => {
  const fake = `ingesting-${Date.now()}`;
  await page.route(`**/api/roadmaps/${fake}`, (route) =>
    route.fulfill({
      json: { id: "x", slug: fake, title: "Streaming", status: "ingesting", created_at: new Date().toISOString(),
              spec: null, jobs: {}, resources: [] },
    }),
  );
  let streamRequests = 0;
  await page.route(`**/api/roadmaps/${fake}/events`, (route) => {
    streamRequests += 1;
    return route.fulfill({ status: 401, json: { error: "Passcode required" } });
  });
  await page.goto(`/#/roadmaps/${fake}`);
  await expect(page.getByRole("heading", { name: "Enter passcode" })).toBeVisible();
  expect(streamRequests).toBe(1); // no reconnect loop after a 401
});
