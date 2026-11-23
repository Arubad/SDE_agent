import { useCallback, useEffect, useState } from "react";
import { type ApiError, checkServer, isAuthError, listServers, type ServerHealth, setServerEnabled } from "./api";
import { serverStateLabel } from "./format";

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

function ServerRow({
  server,
  busy,
  onToggle,
  onCheck,
}: {
  server: ServerHealth;
  busy: boolean;
  onToggle: () => void;
  onCheck: () => void;
}) {
  const badge = server.state === "ok" ? "badge-ok" : server.state === "disabled" ? "" : "badge-down";
  return (
    <li className="card server" data-testid={`server-${server.name}`}>
      <div className="server-main">
        <strong>{server.name}</strong>
        <code className="slug">
          {server.namespace} · {server.transport}
        </code>
        <span className={`badge ${badge}`}>
          <span className="dot" aria-hidden="true" />
          {serverStateLabel(server.state, server.error)}
        </span>
      </div>
      <div className="server-meta muted">
        <span>{server.tools} tools</span>
        {server.latency_ms !== null && <span>{server.latency_ms} ms</span>}
      </div>
      {server.note && <p className="muted">{server.note}</p>}
      {server.error && <p className="error">{server.error}</p>}
      <div className="server-actions">
        <label className="toggle">
          <input type="checkbox" checked={server.enabled} disabled={busy} onChange={onToggle} />
          <span>{server.enabled ? "On" : "Off"}</span>
        </label>
        <button type="button" className="secondary small-button" disabled={busy} onClick={onCheck}>
          Check now
        </button>
      </div>
    </li>
  );
}

export function Settings({ onUnauthorized, onBack }: { onUnauthorized: (err: ApiError) => void; onBack: () => void }) {
  const [servers, setServers] = useState<ServerHealth[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (isAuthError(err)) onUnauthorized(err);
      else setError(messageOf(err));
    },
    [onUnauthorized],
  );

  useEffect(() => {
    listServers().then(setServers, fail);
  }, [fail]);

  async function run(name: string, action: () => Promise<ServerHealth>) {
    setBusy(name);
    setError(null);
    try {
      const next = await action();
      setServers((prev) => (prev ?? []).map((s) => (s.name === name ? next : s)));
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="stack detail">
      <button type="button" className="secondary back" onClick={onBack}>
        ← Roadmaps
      </button>
      <h2 className="page-title">Settings</h2>
      <p className="muted">MCP servers the tutor's agents can use. Turning one off drops its connection.</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {servers === null ? (
        <p className="muted">Loading…</p>
      ) : (
        <ul className="roadmaps">
          {servers.map((s) => (
            <ServerRow
              key={s.name}
              server={s}
              busy={busy === s.name}
              onToggle={() => void run(s.name, () => setServerEnabled(s.name, !s.enabled))}
              onCheck={() => void run(s.name, () => checkServer(s.name))}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
