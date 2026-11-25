import { ExternalLink } from "../ExternalLink";
import type { ChatMessage } from "./chat";

// The backtick is excluded: the backend neutralises a disallowed URL by wrapping it in a code span (R16).
const URL_RE = /https?:\/\/[^\s<>"'`]+/g;
const CODE_RE = /`([^`\n]+)`/g;
const TRAIL_RE = /[.,;:!?)\]]+$/;

/** A URL as the link allow-list compares it: trailing `/` dropped (as the backend's `sanitize_links` does). */
export function normUrl(url: string): string {
  return url.replace(/\/+$/, "");
}

/** The http(s) URLs in `text`, trailing sentence punctuation dropped; URLs inside code spans are not counted. */
export function urlsIn(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(CODE_RE).filter((_, i) => i % 2 === 0)) {
    for (const m of part.matchAll(URL_RE)) out.push(m[0].replace(TRAIL_RE, ""));
  }
  return out;
}

/** The URLs a message may link: the learner's own URLs, plus a reply's roadmap and web source URLs (R16). */
export function allowedLinks(m: ChatMessage, learnerUrls: readonly string[]): string[] {
  const sources = m.sources ? [...m.sources.roadmap.map((r) => r.url), ...m.sources.web.map((w) => w.url)] : [];
  return [...learnerUrls, ...sources.filter((u): u is string => !!u)];
}

/** Text with http(s) URLs: a URL in `allowed` (by `normUrl`) becomes an anchor, any other stays plain text. */
function linkify(text: string, key: string, allowed: ReadonlySet<string>) {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    const trail = TRAIL_RE.exec(m[0])?.[0] ?? "";
    const url = m[0].slice(0, m[0].length - trail.length);
    if (!allowed.has(normUrl(url))) continue;
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    out.push(<ExternalLink key={`${key}-${at}`} url={url} label={url} />);
    last = at + url.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** A paragraph: code spans render as `<code>` text (never linked); the rest is linkified against `allowed`. */
function inline(line: string, key: string, allowed: ReadonlySet<string>) {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const m of line.matchAll(CODE_RE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(...linkify(line.slice(last, at), `${key}-${last}`, allowed));
    out.push(<code key={`${key}-c${at}`}>{m[1]}</code>);
    last = at + m[0].length;
  }
  if (last < line.length) out.push(...linkify(line.slice(last), `${key}-${last}`, allowed));
  return out;
}

function paragraphs(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);
}

/**
 * Assistant or user text as plain-text paragraphs. Never HTML. Only URLs in `allowed` (the reply's sources and
 * the learner's own URLs, R16) become anchors; code spans render as `<code>`.
 */
export function RichText({ text, allowed = [] }: { text: string; allowed?: Iterable<string> }) {
  const ok = new Set([...allowed].map(normUrl));
  return (
    <>
      {paragraphs(text).map((p, i) => (
        <p key={i} className="para">
          {inline(p, String(i), ok)}
        </p>
      ))}
    </>
  );
}

/** A reply still streaming: raw, unsanitised model text, so plain paragraphs with no links at all (R16). */
export function DraftText({ text }: { text: string }) {
  return (
    <>
      {paragraphs(text).map((p, i) => (
        <p key={i} className="para">
          {p}
        </p>
      ))}
    </>
  );
}
