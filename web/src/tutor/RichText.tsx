import { ExternalLink } from "../ExternalLink";

const URL_RE = /https?:\/\/[^\s<>"']+/g;

/** Split a paragraph into text and http(s) links; trailing sentence punctuation stays outside the link. */
function linkify(line: string, key: string) {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const m of line.matchAll(URL_RE)) {
    let url = m[0];
    const trail = /[.,;:!?)\]]+$/.exec(url)?.[0] ?? "";
    url = url.slice(0, url.length - trail.length);
    const at = m.index ?? 0;
    if (at > last) out.push(line.slice(last, at));
    out.push(<ExternalLink key={`${key}-${at}`} url={url} label={url} />);
    last = at + url.length;
  }
  if (last < line.length) out.push(line.slice(last));
  return out;
}

/** Assistant or user text as plain-text paragraphs; bare links become anchors. Never HTML. */
export function RichText({ text }: { text: string }) {
  const paragraphs = text.split(/\n{2,}/).filter((p) => p.trim());
  return (
    <>
      {paragraphs.map((p, i) => (
        <p key={i} className="para">
          {linkify(p.trim(), String(i))}
        </p>
      ))}
    </>
  );
}
