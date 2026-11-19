import { type FormEvent, useCallback, useEffect, useState } from "react";
import { ApiError, createRoadmap, getHealth, type Health, listRoadmaps, type Roadmap } from "./api";
import { setCode } from "./storage";

type HealthState = Health | "down" | null;

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

function HealthBadge({ health }: { health: HealthState }) {
  if (health === null) return <span className="badge" data-testid="health-badge">checking…</span>;
  const dbOk = health !== "down" && health.db === "ok";
  const provider = health === "down" ? "unknown" : health.llm_provider;
  return (
    <span className={`badge ${dbOk ? "badge-ok" : "badge-down"}`} data-testid="health-badge">
      <span className="dot" aria-hidden="true" />
      {provider} · db {dbOk ? "ok" : "down"}
    </span>
  );
}

function PasscodeScreen({ onUnlocked }: { onUnlocked: (roadmaps: Roadmap[]) => void }) {
  const [code, setCodeInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setCode(code.trim());
    try {
      onUnlocked(await listRoadmaps());
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Incorrect passcode. Try again." : messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card passcode">
      <h2>Enter passcode</h2>
      <p className="muted">This instance is private. Enter the access passcode to continue.</p>
      <form onSubmit={submit} className="stack">
        <label className="field">
          <span>Passcode</span>
          <input
            type="password"
            autoComplete="current-password"
            value={code}
            onChange={(e) => setCodeInput(e.target.value)}
            required
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy || !code.trim()}>
          Unlock
        </button>
      </form>
    </section>
  );
}

function CreateRoadmapForm({
  onCreated,
  onUnauthorized,
}: {
  onCreated: (r: Roadmap) => void;
  onUnauthorized: () => void;
}) {
  const [slug, setSlug] = useState("");
  const [title, setTitle] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await createRoadmap({ slug, title, source_text: sourceText }));
      setSlug("");
      setTitle("");
      setSourceText("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthorized();
      else setError(messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card stack" aria-labelledby="create-heading">
      <h2 id="create-heading">New roadmap</h2>
      <div className="row">
        <label className="field">
          <span>Slug</span>
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="dsa-in-90-days"
            autoCapitalize="off"
            spellCheck={false}
            required
          />
        </label>
        <label className="field">
          <span>Title</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="DSA in 90 days" required />
        </label>
      </div>
      <label className="field">
        <span>Paste your roadmap</span>
        <textarea
          value={sourceText}
          onChange={(e) => setSourceText(e.target.value)}
          rows={8}
          placeholder={"Week 1: arrays and hashing\nWeek 2: two pointers\n…"}
          required
        />
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" disabled={busy}>
        {busy ? "Creating…" : "Create roadmap"}
      </button>
    </form>
  );
}

function RoadmapList({ roadmaps }: { roadmaps: Roadmap[] }) {
  if (roadmaps.length === 0) return <p className="muted empty">No roadmaps yet. Paste one to get started.</p>;
  return (
    <ul className="roadmaps">
      {roadmaps.map((r) => (
        <li key={r.id} className="card roadmap">
          <div className="roadmap-main">
            <strong className="roadmap-title">{r.title}</strong>
            <code className="slug">{r.slug}</code>
          </div>
          <div className="roadmap-meta">
            <span className="status">{r.status}</span>
            <time dateTime={r.created_at}>{new Date(r.created_at).toLocaleDateString()}</time>
          </div>
        </li>
      ))}
    </ul>
  );
}

export function App() {
  const [health, setHealth] = useState<HealthState>(null);
  const [roadmaps, setRoadmaps] = useState<Roadmap[] | null>(null);
  const [locked, setLocked] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setRoadmaps(await listRoadmaps());
      setLocked(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setLocked(true);
      else setLoadError(messageOf(err));
    }
  }, []);

  useEffect(() => {
    getHealth().then(setHealth, () => setHealth("down"));
    void load();
  }, [load]);

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden="true">◆</span>
          <h1>Roadmap Tutor</h1>
        </div>
        <HealthBadge health={health} />
      </header>
      <main className="page">
        {locked ? (
          <PasscodeScreen
            onUnlocked={(list) => {
              setRoadmaps(list);
              setLocked(false);
            }}
          />
        ) : (
          <>
            <h2 className="page-title">Roadmaps</h2>
            {loadError && (
              <p className="error" role="alert">
                {loadError}
              </p>
            )}
            <div className="layout">
              <section aria-label="Your roadmaps">
                {roadmaps === null ? <p className="muted">Loading…</p> : <RoadmapList roadmaps={roadmaps} />}
              </section>
              <CreateRoadmapForm
                onCreated={(r) => setRoadmaps((prev) => [r, ...(prev ?? [])])}
                onUnauthorized={() => setLocked(true)}
              />
            </div>
          </>
        )}
      </main>
    </>
  );
}
