import { useState } from "react";
import type { SessionStartInterrupt } from "./chat";

const LABELS: Record<string, string> = {
  recap_last: "Recap last session",
  recap_earlier: "Recap an earlier session",
  warm_up: "Warm up",
  skip: "Skip",
};

/** The session_start interrupt: how to begin (recap the last session, an earlier one, warm up, or skip). */
export function SessionStartCard({
  value,
  busy,
  onChoose,
}: {
  value: SessionStartInterrupt;
  busy: boolean;
  onChoose: (resume: { choice: string; session_id?: string }) => void;
}) {
  const [earlier, setEarlier] = useState(value.earlier[0]?.id ?? "");
  const label = (s: { title: string | null; date: string | null }) => [s.title || "Untitled session", s.date].filter(Boolean).join(" · ");
  return (
    <section className="card stack" aria-label="Start of session">
      <h3>Welcome back</h3>
      {value.last ? (
        <p className="muted small">
          Last time: {label(value.last)}
          {value.last.summary ? `. ${value.last.summary}` : ""}
        </p>
      ) : (
        <p className="muted small">This is your first session on this roadmap.</p>
      )}
      <div className="actions">
        {value.options.map((o) =>
          o === "recap_earlier" ? (
            <span key={o} className="inline-field">
              <label className="sr-only" htmlFor="earlier-session">
                Earlier session
              </label>
              <select id="earlier-session" value={earlier} onChange={(e) => setEarlier(e.target.value)} disabled={busy}>
                {value.earlier.map((s) => (
                  <option key={s.id} value={s.id}>
                    {label(s)}
                  </option>
                ))}
              </select>
              <button type="button" className="secondary" disabled={busy || !earlier} onClick={() => onChoose({ choice: o, session_id: earlier })}>
                {LABELS[o]}
              </button>
            </span>
          ) : (
            <button key={o} type="button" className={o === "skip" ? "secondary" : undefined} disabled={busy} onClick={() => onChoose({ choice: o })}>
              {LABELS[o] ?? o}
            </button>
          ),
        )}
      </div>
    </section>
  );
}
