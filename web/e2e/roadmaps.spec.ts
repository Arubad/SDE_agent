import { expect, test } from "@playwright/test";
import { declineConsent } from "./helpers";

// The E2E database persists between runs, so each run uses its own slug.
const slug = `my-roadmap-${Date.now()}`;

async function fillForm(page: import("@playwright/test").Page) {
  await page.getByLabel("Slug").fill(slug);
  await page.getByLabel("Title").fill("My Roadmap");
  await page.getByLabel("Paste your roadmap").fill("Week 1: arrays and hashing\nWeek 2: two pointers");
  await page.getByRole("button", { name: "Create roadmap" }).click();
}

test.beforeAll(async ({ request }) => declineConsent(request));
test.describe.configure({ mode: "serial" });

test("creating a roadmap adds it to the list", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Roadmaps", exact: true })).toBeVisible();
  await fillForm(page);

  const row = page.getByRole("listitem").filter({ hasText: slug });
  await expect(row).toBeVisible();
  await expect(row).toContainText("My Roadmap");

  // It is persisted, not just held in page state.
  await page.reload();
  await expect(page.getByRole("listitem").filter({ hasText: slug })).toContainText("My Roadmap");
});

test("creating the same slug again shows the server's error", async ({ page }) => {
  await page.goto("/");
  await fillForm(page);
  await expect(page.getByRole("alert")).toContainText("already exists");
});

test("health badge shows the LLM provider", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("health-badge")).toContainText("fake");
  await expect(page.getByTestId("health-badge")).toContainText("db ok");
});

test("the page fits a 375 px wide screen", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 740 });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Create roadmap" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
