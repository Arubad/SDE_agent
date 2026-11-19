import { expect, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8766" });

test("passcode screen gates the app until the right code is entered", async ({ page }) => {
  await page.goto("/");
  const code = page.getByLabel("Passcode");
  await expect(code).toBeVisible();
  await expect(page.getByRole("heading", { name: "Roadmaps", exact: true })).toHaveCount(0);

  await code.fill("wrong-code");
  await page.getByRole("button", { name: "Unlock" }).click();
  await expect(page.getByRole("alert")).toContainText("Incorrect passcode");
  await expect(page.getByRole("heading", { name: "Roadmaps", exact: true })).toHaveCount(0);

  await code.fill("e2e-pass-123");
  await page.getByRole("button", { name: "Unlock" }).click();
  await expect(page.getByRole("heading", { name: "Roadmaps", exact: true })).toBeVisible();
  await expect(page.getByLabel("Passcode")).toHaveCount(0);
});

test("a rejected passcode is not kept or re-sent after a reload", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Passcode").fill("typo-code");
  await page.getByRole("button", { name: "Unlock" }).click();
  await expect(page.getByRole("alert")).toContainText("Incorrect passcode");

  const sentCodes: (string | undefined)[] = [];
  page.on("request", (req) => {
    if (new URL(req.url()).pathname.startsWith("/api/")) sentCodes.push(req.headers()["x-access-code"]);
  });
  await page.reload();

  await expect(page.getByLabel("Passcode")).toBeVisible();
  await expect(page.getByLabel("Passcode")).toBeFocused();
  expect(await page.evaluate(() => Object.keys(localStorage).length)).toBe(0);
  expect(sentCodes.length).toBeGreaterThan(0);
  expect(sentCodes.filter(Boolean)).toEqual([]);
});
