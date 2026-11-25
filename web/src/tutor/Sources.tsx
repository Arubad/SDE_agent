import { ExternalLink } from "../ExternalLink";
import type { Sources as SourceSet } from "./chat";

/** The sources behind one reply: the roadmap passages the [n] markers point to, and the web pages it used. */
export function Sources({ sources }: { sources: SourceSet }) {
  const { roadmap, web } = sources;
  if (roadmap.length === 0 && web.length === 0) return null;
  return (
    <div className="sources small">
      {roadmap.length > 0 && (
        <section aria-label="Roadmap sources">
          <h4>Roadmap</h4>
          <ol>
            {roadmap.map((r, i) => (
              <li key={i} value={r.n ?? i + 1}>
                {r.url ? <ExternalLink url={r.url} label={r.title || undefined} /> : <span>{r.title || "Roadmap passage"}</span>}
                {r.section && <span className="muted"> · {r.section}</span>}
              </li>
            ))}
          </ol>
        </section>
      )}
      {web.length > 0 && (
        <section aria-label="Web sources">
          <h4>Web</h4>
          <ul>
            {web.map((w, i) => (
              <li key={i}>
                <ExternalLink url={w.url} label={w.title || undefined} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
