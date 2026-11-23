import { hostOf, safeHref } from "./format";

/** A link to a web-derived URL: only http(s) hrefs, opened without an opener or referrer; otherwise plain text. */
export function ExternalLink({ url, label }: { url: string | null; label?: string }) {
  const href = safeHref(url);
  const text = label ?? (hostOf(url) || url || "");
  if (!href) return <span className="ext">{text}</span>;
  return (
    <a className="ext" href={href} target="_blank" rel="noopener noreferrer">
      {text}
    </a>
  );
}
