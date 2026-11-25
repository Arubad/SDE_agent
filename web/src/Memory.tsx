import { type FormEvent, useCallback, useEffect, useState } from "react";
import { type ApiError, isAuthError, listRoadmaps } from "./api";
import {
  applyConsent,
  deleteFact,
  deleteSession,
  downloadExport,
  editFact,
  type Fact,
  FACT_KINDS,
  getLearnerSettings,
  type LearnerSettings,
  listFacts,
  putLearnerSettings,
  resetProgress,
  type SettingsPatch,
} from "./memoryApi";
import { getSkillMap, listSessions, type SessionSummary, type SkillMapSkill } from "./tutorApi";

type Fail = (err: unknown) => void;
const messageOf = (err: unknown) => (err instanceof Error ? err.message : "Something went wrong.");

function Consent({ fail, onChanged }: { fail: Fail; onChanged: () => void }) {
  const [settings, setSettings] = useState<LearnerSettings | null>(null);
  const [days, setDays] = useState("");
  const [confirm, setConfirm] = useState<{ patch: SettingsPatch; message: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const show = (s: LearnerSettings) => {
    setSettings(s);
    setDays(String(s.retention_days));
  };
  useEffect(() => {
    getLearnerSettings().then(show, fail);
  }, [fail]);

  async function save(patch: SettingsPatch, confirmed = false) {
    setError(null);
    try {
      const out = await applyConsent(putLearnerSettings, patch, confirmed);
      if (out.kind === "confirm") return setConfirm({ patch, message: out.message });
      setConfirm(null);
      show(out.settings);
      onChanged();
    } catch (err) {
      if (isAuthError(err)) fail(err);
      else setError(messageOf(err));
    }
  }

  if (!settings) return <section className="card"><p className="muted">Loading…</p></section>;
  return (
    <section className="card stack" aria-labelledby="mem-consent">
      <h3 id="mem-consent">Consent</h3>
      <p className="muted small">Progress is always kept on this machine. These two choices cover the extras.</p>
      <label className="option">
        <input type="checkbox" checked={settings.consent_personal === true} onChange={(e) => void save({ consent_personal: e.target.checked })} />
        <span className="option-text">Remember personal facts</span>
      </label>
      <label className="option">
        <input type="checkbox" checked={settings.consent_raw_messages === true} onChange={(e) => void save({ consent_raw_messages: e.target.checked })} />
        <span className="option-text">Keep raw chat messages</span>
      </label>
      {confirm && (
        <div className="card stack" role="alert">
          <p>{confirm.message}</p>
          <div className="actions">
            <button type="button" onClick={() => void save(confirm.patch, true)}>Delete and turn off</button>
            <button type="button" className="secondary" onClick={() => setConfirm(null)}>Keep it on</button>
          </div>
        </div>
      )}
      <form
        className="row"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void save({ retention_days: Number(days) });
        }}
      >
        <label className="field">
          <span>Retention days</span>
          <input type="number" min={1} max={3650} step={1} value={days} onChange={(e) => setDays(e.target.value)} required />
          <span className="muted small">How long raw messages are kept (1 to 3650).</span>
        </label>
        <button type="submit" className="secondary small-button">Save</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}

function FactRow({ fact, fail, onChanged }: { fact: Fact; fail: Fail; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(fact.text);
  const [kind, setKind] = useState(fact.kind);
  const [error, setError] = useState<string | null>(null);

  async function run(fn: () => Promise<unknown>, done?: () => void) {
    setError(null);
    try {
      await fn();
      done?.();
      onChanged();
    } catch (err) {
      if (isAuthError(err)) fail(err);
      else setError(messageOf(err));
    }
  }

  return (
    <tr>
      <td>
        {editing ? (
          <input aria-label="Fact text" value={text} onChange={(e) => setText(e.target.value)} />
        ) : (
          <span className="para">{fact.text}</span>
        )}
        {error && <p className="error small" role="alert">{error}</p>}
      </td>
      <td>
        {editing ? (
          <select aria-label="Fact kind" value={kind} onChange={(e) => setKind(e.target.value as Fact["kind"])}>
            {FACT_KINDS.map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        ) : (
          fact.kind
        )}
      </td>
      <td>{fact.roadmap ?? "general"}</td>
      <td className="actions">
        {editing ? (
          <>
            <button type="button" className="small-button" disabled={!text.trim()} onClick={() => void run(() => editFact(fact.id, text, kind), () => setEditing(false))}>
              Save
            </button>
            <button type="button" className="secondary small-button" onClick={() => { setEditing(false); setText(fact.text); setKind(fact.kind); }}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button type="button" className="secondary small-button" onClick={() => setEditing(true)}>Edit</button>
            <button type="button" className="secondary small-button" onClick={() => void run(() => deleteFact(fact.id))}>Delete</button>
          </>
        )}
      </td>
    </tr>
  );
}

function Facts({ fail, version, onChanged }: { fail: Fail; version: number; onChanged: () => void }) {
  const [facts, setFacts] = useState<Fact[] | null>(null);
  useEffect(() => {
    listFacts().then(setFacts, fail);
  }, [fail, version]);
  return (
    <section className="card stack" aria-labelledby="mem-facts">
      <h3 id="mem-facts">Remembered facts</h3>
      {!facts ? (
        <p className="muted">Loading…</p>
      ) : facts.length === 0 ? (
        <p className="muted">Nothing remembered. Facts appear here only while personal memory is on.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Fact</th><th>Kind</th><th>Roadmap</th><th><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {facts.map((f) => (
                <FactRow key={`${f.id}:${f.updated_at}`} fact={f} fail={fail} onChanged={onChanged} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

interface RoadmapData {
  slug: string;
  title: string;
  sessions: SessionSummary[];
  skills: SkillMapSkill[];
}

function Roadmaps({ fail }: { fail: Fail }) {
  const [rows, setRows] = useState<RoadmapData[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const maps = await listRoadmaps();
      setRows(
        await Promise.all(
          maps.map(async (r) => ({
            slug: r.slug,
            title: r.title,
            sessions: await listSessions(r.slug).catch(() => []),
            skills: await getSkillMap(r.slug).then((m) => m.skills.filter((s) => s.mastery > 0), () => []), // no spec yet: no progress
          })),
        ),
      );
    } catch (err) {
      fail(err);
    }
  }, [fail]);
  useEffect(() => {
    void load();
  }, [load]);

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      await load();
    } catch (err) {
      if (isAuthError(err)) fail(err);
      else setError(messageOf(err));
    }
  }

  return (
    <>
      <section className="card stack" aria-labelledby="mem-sessions">
        <h3 id="mem-sessions">Sessions</h3>
        {error && <p className="error" role="alert">{error}</p>}
        {!rows ? <p className="muted">Loading…</p> : rows.every((r) => r.sessions.length === 0) ? <p className="muted">No sessions yet.</p> : null}
        {rows?.filter((r) => r.sessions.length > 0).map((r) => (
          <div key={r.slug} className="stack">
            <h4>{r.title}</h4>
            <ul className="sessions">
              {r.sessions.map((s) => (
                <li key={s.id} className="card">
                  <div className="report-head">
                    <strong>{s.title || "Untitled session"}</strong>
                    {s.status === "active" ? (
                      <span className="muted small">End it first</span>
                    ) : (
                      <button type="button" className="secondary small-button" onClick={() => void act(() => deleteSession(s.id))}>
                        Delete
                      </button>
                    )}
                  </div>
                  <p className="muted small">{s.started_at ? new Date(s.started_at).toLocaleString() : ""} · {s.status}</p>
                  {s.summary && <p className="small para">{s.summary}</p>}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </section>
      <section className="card stack" aria-labelledby="mem-progress">
        <h3 id="mem-progress">Skill progress</h3>
        {rows && rows.every((r) => r.skills.length === 0) && <p className="muted">No progress recorded yet.</p>}
        {rows?.filter((r) => r.skills.length > 0).map((r) => (
          <div key={r.slug} className="stack">
            <h4>{r.title}</h4>
            <ul className="skill-list">
              {r.skills.map((s) => (
                <li key={s.id}>
                  {s.title} <span className="muted">{Math.round(s.mastery * 100)}%</span>{" "}
                  <button type="button" className="secondary small-button" aria-label={`Reset ${s.title}`} onClick={() => void act(() => resetProgress(r.slug, s.id))}>
                    Reset
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </section>
    </>
  );
}

function Export({ fail }: { fail: Fail }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <section className="card stack" aria-labelledby="mem-export">
      <h3 id="mem-export">Export</h3>
      <p className="muted small">Everything stored about you, as one JSON file.</p>
      <div>
        <button
          type="button"
          onClick={() => {
            setError(null);
            downloadExport().catch((err: unknown) => (isAuthError(err) ? fail(err) : setError(messageOf(err))));
          }}
        >
          Export
        </button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}

/** The Memory & privacy page. */
export function Memory({ onUnauthorized, onBack }: { onUnauthorized: (err: ApiError) => void; onBack: () => void }) {
  // Consent changes can purge facts, so the facts table reloads after each.
  const [version, setVersion] = useState(0);
  const bump = useCallback(() => setVersion((v) => v + 1), []);
  const [loadError, setLoadError] = useState<string | null>(null);
  const fail = useCallback<Fail>((err) => {
    if (isAuthError(err)) onUnauthorized(err);
    else setLoadError(messageOf(err));
  }, [onUnauthorized]);
  return (
    <div className="stack memory">
      <button type="button" className="back secondary" onClick={onBack}>← Roadmaps</button>
      <h2 className="page-title">Memory &amp; privacy</h2>
      {loadError && <p className="error" role="alert">{loadError}</p>}
      <Consent fail={fail} onChanged={bump} />
      <Facts fail={fail} version={version} onChanged={bump} />
      <Roadmaps fail={fail} />
      <Export fail={fail} />
    </div>
  );
}
