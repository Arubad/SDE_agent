import { useEffect, useRef, useState } from "react";
import { type ApiError, isAuthError, type Progress, type Report, roadmapPath, TERMINAL_STATUSES } from "./api";
import { AgentActivity, Proposals } from "./IngestReport";
import { reasonText, statusText } from "./format";
import { followEvents } from "./stream";

const JOB_LABELS: Record<string, string> = {
  agent_plan: "Agent: crawl plan",
  agent_recover: "Agent: recovery",
  ingest_resource: "Resources",
  fetch_page: "Pages",
  expand_playlist: "Playlists",
  video: "Videos",
  pdf: "PDFs",
  github_repo: "Repositories",
  chunk_embed: "Indexing",
  recheck: "Re-checks",
};
const JOB_STATUSES = ["queued", "running", "done", "dead"];
const RECONNECT_RETRIES = 3; // followEvents' default: backoff 1.5 s, 3 s, 6 s, then give up

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : "The progress stream failed.";
}

function JobTable({ jobs }: { jobs: Progress["jobs"] }) {
  const types = Object.keys(jobs).sort();
  if (types.length === 0) return <p className="muted small">Waiting for the first jobs…</p>;
  const extra = [...new Set(types.flatMap((t) => Object.keys(jobs[t])))].filter((s) => !JOB_STATUSES.includes(s));
  const columns = [...JOB_STATUSES, ...extra];
  return (
    <div className="table-wrap">
      <table className="jobs-table">
        <thead>
          <tr>
            <th scope="col">Jobs</th>
            {columns.map((c) => (
              <th key={c} scope="col" className="num">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {types.map((t) => (
            <tr key={t}>
              <th scope="row">{JOB_LABELS[t] ?? t}</th>
              {columns.map((c) => (
                <td key={c} className="num">
                  {jobs[t][c] ?? 0}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Live ingestion progress from the roadmap's event stream. Calls `onSettled` once a terminal status arrives.
 * The stream is aborted on unmount; a stream that ends early is reopened.
 */
export function IngestProgress({
  slug,
  lastReport,
  skillTitle,
  busy,
  onSettled,
  onUnauthorized,
  onApproveProposal,
  onDismissProposal,
}: {
  slug: string;
  lastReport: Report | null;
  skillTitle: (id: string | null) => string;
  busy: boolean;
  onSettled: (status: string) => void;
  onUnauthorized: (err: ApiError) => void;
  onApproveProposal: (id: string) => void;
  onDismissProposal: (id: string) => void;
}) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  // Callbacks change identity on every parent render; the stream effect must not restart for that.
  const handlers = useRef({ onSettled, onUnauthorized });
  handlers.current = { onSettled, onUnauthorized };

  useEffect(() => {
    const controller = new AbortController();
    followEvents(
      roadmapPath(slug, "/events"),
      (name, data) => {
        if (name === "progress" && data && typeof data === "object") {
          setProgress(data as Progress);
          setError(null);
        } else if (name === "done") {
          const status = (data as { status?: unknown } | null)?.status;
          handlers.current.onSettled(typeof status === "string" ? status : "ready");
        }
      },
      controller.signal,
      {
        onRetry: (failures, err) =>
          setError(`${messageOf(err)} Reconnecting (attempt ${failures} of ${RECONNECT_RETRIES})…`),
      },
    ).catch((err: unknown) => {
      if (controller.signal.aborted) return;
      if (isAuthError(err)) handlers.current.onUnauthorized(err);
      else setFatal(`Live progress stopped: ${messageOf(err)} Reload the page to try again.`);
    });
    return () => controller.abort();
  }, [slug]);

  const status = progress?.status ?? "ingesting";
  const resources = (progress?.resources ?? []).filter((r) => r.status !== "proposed" && r.status !== "dismissed");
  const agentBusy = Object.entries(progress?.jobs ?? {}).some(
    ([t, counts]) => t.startsWith("agent_") && ((counts.queued ?? 0) > 0 || (counts.running ?? 0) > 0),
  );

  return (
    <section className="stack" data-testid="ingest-progress" aria-labelledby="progress-heading">
      <div className="report-head">
        <h2 id="progress-heading">Ingesting</h2>
        <span className="status" aria-live="polite">
          {TERMINAL_STATUSES.includes(status) ? statusText(status) : `${statusText(status)}…`}
        </span>
      </div>
      {fatal ? (
        <p className="error" role="alert">
          {fatal}
        </p>
      ) : (
        error && (
          <p className="error" role="status">
            {error}
          </p>
        )
      )}
      {agentBusy && <p className="muted small">The ingestion agent is working on this roadmap.</p>}
      <div className="card stack">
        <JobTable jobs={progress?.jobs ?? {}} />
      </div>
      {resources.length > 0 && (
        <ul className="card progress-resources">
          {resources.map((r) => (
            <li key={r.key}>
              <span className="cell-title">{r.title}</span>
              <span className={`status status-${r.status}`}>{r.status}</span>
              {r.status === "linked_only" ? (
                <span className="reason">{statusText(r.status)}</span>
              ) : (
                r.reason && <span className="reason">{reasonText(r.reason)}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {lastReport && (
        <>
          <Proposals
            proposals={lastReport.proposals}
            skillTitle={skillTitle}
            busy={busy}
            onApprove={onApproveProposal}
            onDismiss={onDismissProposal}
          />
          <AgentActivity agent={lastReport.agent} />
        </>
      )}
    </section>
  );
}
