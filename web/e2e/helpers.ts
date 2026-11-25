import type { APIRequestContext } from "@playwright/test";

/** Answers the first-run consent question (both off) so its modal does not cover the page under test. */
export async function declineConsent(request: APIRequestContext): Promise<void> {
  const res = await request.put("/api/learner/settings", {
    data: { consent_personal: false, consent_raw_messages: false },
  });
  if (!res.ok()) throw new Error(`could not set consent: HTTP ${res.status()}`);
}
