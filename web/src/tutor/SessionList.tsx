import { useEffect, useState } from "react";
import { type ApiError, isAuthError } from "../api";
import { listSessions, type SessionSummary } from "../tutorApi";

function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** The roadmap's sessions, newest first, with their summaries. Opening one shows it read-only, or live if active. */
export function SessionList({
  slug,
  onOpen,
  onUnauthorized,
}: {
  slug: string;
  onOpen: (id: string) => void;
  onUnauthorized: (err: ApiError) => void;
}) {
  const [rows, setRows] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listSessions(slug)
      .then((r) => live && setRows(r))
      .catch((err: unknown) => {
        if (!live) return;
        if (isAuthError(err)) onUnauthorized(err);
        else setError(err instanceof Error ? err.message : "Could not load sessions.");
      });
    return () => {
      live = false;
    };
  }, [slug, onUnauthorized]);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!rows) return <p className="muted">Loading…</p>;
  if (rows.length === 0) return <p className="muted">No sessions yet. Start one from the Tutor tab.</p>;
  return (
    <ul className="sessions">
      {rows.map((s) => (
        <li key={s.id} className="card">
          <button type="button" className="session-open secondary" onClick={() => onOpen(s.id)}>
            <strong>{s.title || "Untitled session"}</strong>
            <span className={`status status-${s.status === "active" ? "ready" : "skipped"}`}>{s.status}</span>
          </button>
          <p className="muted small">{when(s.started_at)}</p>
          {s.summary && <p className="small para">{s.summary}</p>}
        </li>
      ))}
    </ul>
  );
}
