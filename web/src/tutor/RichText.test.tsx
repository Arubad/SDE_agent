import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { allowedLinks, DraftText, RichText, urlsIn } from "./RichText";

const html = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("RichText (R16)", () => {
  it("never links a code span, even an allowed URL inside one", () => {
    const out = html(<RichText text="See `https://x.example` now" allowed={["https://x.example"]} />);
    expect(out).not.toContain("<a");
    expect(out).toContain("<code>https://x.example</code>");
  });

  it("does not link a backend-neutralised URL (backtick-wrapped) nor swallow the backtick", () => {
    const out = html(<RichText text="Log in at `https://evil.example/login`." />);
    expect(out).not.toContain("<a");
    expect(out).toContain("<code>https://evil.example/login</code>.");
  });

  it("renders a URL outside the allowed set as plain text", () => {
    const out = html(<RichText text="Try https://evil.example/x." allowed={["https://good.example/"]} />);
    expect(out).not.toContain("<a");
    expect(out).toContain("https://evil.example/x.");
  });

  it("links an allowed URL through ExternalLink, comparing after a trailing slash", () => {
    const out = html(<RichText text="Read https://good.example/doc/ today." allowed={["https://good.example/doc"]} />);
    expect(out).toContain('<a class="ext" href="https://good.example/doc/" target="_blank" rel="noopener noreferrer">');
    expect(out).toMatch(/<\/a> today\.<\/p>$/);
  });

  it("streaming text renders no anchor at all", () => {
    const out = html(<DraftText text={"Go to https://evil.example/login\n\nand `x`"} />);
    expect(out).not.toContain("<a");
    expect(out).toContain("https://evil.example/login");
  });
});

describe("allowedLinks", () => {
  it("is the learner's URLs plus the reply's roadmap and web source URLs", () => {
    const m = {
      role: "assistant" as const,
      text: "",
      sources: { roadmap: [{ n: 1, url: "https://r.example/a" }, { n: 2 }], web: [{ url: "https://w.example/b", title: "" }] },
    };
    expect(allowedLinks(m, ["https://me.example"])).toEqual(["https://me.example", "https://r.example/a", "https://w.example/b"]);
    expect(allowedLinks({ role: "user", text: "" }, ["https://me.example"])).toEqual(["https://me.example"]);
  });

  it("urlsIn skips code spans and trailing punctuation", () => {
    expect(urlsIn("see https://a.example/x, and `https://b.example`")).toEqual(["https://a.example/x"]);
  });
});
