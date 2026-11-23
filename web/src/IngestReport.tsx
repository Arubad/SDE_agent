import type { AgentAction, AgentRun, Proposal, Report } from "./api";
import { ExternalLink } from "./ExternalLink";
import { hostOf, reasonText, skippedUrlText, statusText } from "./format";

const OUTCOME_LABELS: Record<string, string> = { ok: "done", refused: "refused", error: "failed" };

function cost(runs: AgentRun[]): string {
  return `$${runs.reduce((sum, r) => sum + (Number(r.cost_usd) || 0), 0).toFixed(3)}`;
}

function runLabel(run: AgentRun): string {
  const what = run.mode === "plan" ? "Crawl plan" : `Recovery round ${run.round}`;
  return `${what}: ${run.stop ?? "?"}, ${run.steps} step${run.steps === 1 ? "" : "s"}`;
}

/** The agent's tool calls (refused ones included) and what its runs cost. Web-derived text is rendered as text. */
export function AgentActivity({ agent }: { agent: { runs: AgentRun[]; actions: AgentAction[] } | undefined }) {
  if (!agent || (agent.runs.length === 0 && agent.actions.length === 0)) return null;
  return (
    <section className="card stack" data-testid="agent-activity" aria-labelledby="agent-heading">
      <h3 id="agent-heading">Agent activity</h3>
      {agent.actions.length === 0 ? (
        <p className="muted small">The agent took no actions.</p>
      ) : (
        <ol className="actions-list">
          {agent.actions.map((a, i) => (
            <li key={i} className={`action action-${a.outcome}`}>
              <code className="tool">{a.tool}</code>
              <span className="action-summary">{a.summary ?? ""}</span>
              <span className={`outcome outcome-${a.outcome}`}>{OUTCOME_LABELS[a.outcome] ?? a.outcome}</span>
            </li>
          ))}
        </ol>
      )}
      <p className="muted small">
        Run cost: {cost(agent.runs)} over {agent.runs.length} run{agent.runs.length === 1 ? "" : "s"}
        {agent.runs.length > 0 && <> ({agent.runs.map(runLabel).join("; ")})</>}
      </p>
    </section>
  );
}

export function Proposals({
  proposals,
  skillTitle,
  busy,
  onApprove,
  onDismiss,
}: {
  proposals: Proposal[] | undefined;
  skillTitle: (id: string | null) => string;
  busy: boolean;
  onApprove: (id: string) => void;
  onDismiss: (id: string) => void;
}) {
  if (!proposals || proposals.length === 0) return null;
  return (
    <section className="card stack suggested" aria-labelledby="suggested-heading">
      <h3 id="suggested-heading">Suggested by the agent</h3>
      <p className="muted small">Nothing here is ingested unless you approve it.</p>
      <ul className="proposals">
        {proposals.map((p) => (
          <li key={p.id} className="proposal" data-testid="proposal">
            <div className="proposal-main">
              <span className="kind">{skillTitle(p.skill_id)}</span>
              <strong>{p.title}</strong>
              <ExternalLink url={p.url} label={hostOf(p.url) || undefined} />
              {p.reason && <p className="muted small">{p.reason}</p>}
            </div>
            <div className="proposal-actions">
              <button type="button" disabled={busy} onClick={() => onApprove(p.id)}>
                Approve
              </button>
              <button type="button" className="secondary" disabled={busy} onClick={() => onDismiss(p.id)}>
                Dismiss
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function resourceNote(status: string, reason: string | null): string {
  if (status === "linked_only") return statusText(status);
  return reasonText(reason);
}

export function IngestReport({
  report,
  skillTitle,
  busy,
  error,
  onApproveProposal,
  onDismissProposal,
}: {
  report: Report;
  skillTitle: (id: string | null) => string;
  busy: boolean;
  error: string | null;
  onApproveProposal: (id: string) => void;
  onDismissProposal: (id: string) => void;
}) {
  const [headline, ...rest] = report.summary.split("\n");
  return (
    <section className="stack" data-testid="ingest-report" aria-labelledby="report-heading">
      <h2 id="report-heading">Ingestion report</h2>
      <div className="card summary" data-testid="report-summary">
        <p>{headline}</p>
        {rest.map((line, i) => (
          <p key={i} className="muted small">
            {line}
          </p>
        ))}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <Proposals
        proposals={report.proposals}
        skillTitle={skillTitle}
        busy={busy}
        onApprove={onApproveProposal}
        onDismiss={onDismissProposal}
      />
      <div className="card table-wrap">
        <table className="resources-table">
          <thead>
            <tr>
              <th scope="col">Resource</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">Pages</th>
              <th scope="col" className="num">Videos</th>
              <th scope="col" className="num">Chunks</th>
            </tr>
          </thead>
          <tbody>
            {report.resources.map((r) => (
              <tr key={r.id}>
                <td>
                  <span className="kind">{r.kind}</span> <span className="cell-title">{r.title}</span>
                  {r.url && (
                    <>
                      {" "}
                      <ExternalLink url={r.url} />
                    </>
                  )}
                </td>
                <td>
                  <span className={`status status-${r.status}`}>{r.status === "linked_only" ? "linked" : r.status}</span>
                  {resourceNote(r.status, r.reason) && (
                    <span className="reason">{resourceNote(r.status, r.reason)}</span>
                  )}
                </td>
                <td className="num">{r.pages}</td>
                <td className="num">{r.videos}</td>
                <td className="num">{r.chunks}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {report.skipped.length > 0 && (
        <section className="card stack" data-testid="skipped" aria-labelledby="skipped-heading">
          <h3 id="skipped-heading">Skipped</h3>
          <ul className="skipped">
            {report.skipped.map((s, i) => (
              <li key={`${s.url ?? ""}-${i}`}>
                <span className="skipped-url">{skippedUrlText(s.url)}</span>
                <span className="reason">{reasonText(s.reason)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <AgentActivity agent={report.agent} />
    </section>
  );
}
