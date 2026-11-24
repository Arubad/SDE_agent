import type { ApprovalInterrupt } from "./chat";

/** An approval interrupt: what the tutor wants to do, with the raw payload one click away. */
export function ApprovalCard({
  value,
  busy,
  onRule,
}: {
  value: ApprovalInterrupt;
  busy: boolean;
  onRule: (resume: { approved: boolean; card: string }) => void;
}) {
  return (
    <section className="card stack" aria-label="Approval needed">
      <h3>Approval needed</h3>
      <p className="para">{value.summary}</p>
      <details>
        <summary>Raw payload</summary>
        <pre className="payload">{JSON.stringify(value.payload, null, 2)}</pre>
      </details>
      <div className="actions">
        <button type="button" disabled={busy} onClick={() => onRule({ approved: true, card: value.id })}>
          Approve
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={() => onRule({ approved: false, card: value.id })}>
          Decline
        </button>
      </div>
    </section>
  );
}
