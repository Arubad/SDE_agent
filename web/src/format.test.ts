import { describe, expect, it } from "vitest";
import { hostOf, pagesText, reasonText, safeHref, skippedUrlText } from "./format";

describe("format", () => {
  it("maps reason codes to readable text and falls back to the code", () => {
    expect(reasonText("needs_browser")).toBe("Needs a browser to render, and the browser could not render it");
    for (const code of [
      "not_found",
      "robots_denied",
      "login_wall",
      "paywall",
      "bot_protection",
      "too_large",
      "unsupported_type",
      "private_address",
      "no_url",
      "error",
      "captions_blocked",
      "no_captions",
    ]) {
      expect(reasonText(code)).not.toBe(code);
    }
    expect(reasonText("some_new_code")).toBe("some_new_code");
    expect(reasonText(null)).toBe("");
  });

  it("shows how many of a resource's pages the browser rendered, only when some were", () => {
    expect(pagesText(4, 1)).toBe("4 (1 rendered)");
    expect(pagesText(4, 0)).toBe("4");
    expect(pagesText(0, undefined)).toBe("0");
    expect(pagesText(2, null)).toBe("2");
  });

  it("labels a skipped item that has no URL (the report sends null for one)", () => {
    expect(skippedUrlText("https://flow.test/gone")).toBe("https://flow.test/gone");
    expect(skippedUrlText(null)).toBe("(no URL recorded)");
    expect(skippedUrlText(undefined)).toBe("(no URL recorded)");
  });

  it("allows only http(s) hrefs", () => {
    expect(safeHref("https://kubernetes.io/docs/")).toBe("https://kubernetes.io/docs/");
    expect(safeHref("http://example.test/a")).toBe("http://example.test/a");
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref(" JavaScript:alert(1)")).toBeUndefined();
    expect(safeHref("data:text/html,<b>x</b>")).toBeUndefined();
    expect(safeHref("not a url")).toBeUndefined();
    expect(safeHref(null)).toBeUndefined();
  });

  it("shows the host of a URL", () => {
    expect(hostOf("https://www.youtube.com/watch?v=x")).toBe("youtube.com");
    expect(hostOf("nonsense")).toBe("");
  });
});
