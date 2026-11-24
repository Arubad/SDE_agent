import { type FormEvent, useCallback, useEffect, useState } from "react";
import { ApiError, createRoadmap, getHealth, type Health, isAuthError, listRoadmaps, type Roadmap } from "./api";
import { statusText } from "./format";
import { ConsentDialog } from "./ConsentDialog";
import { getLearnerSettings } from "./memoryApi";
import { Memory } from "./Memory";
import { RoadmapDetail } from "./RoadmapDetail";
import { Settings } from "./Settings";

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

function PasscodeScreen({
  initialError,
  onUnlocked,
}: {
  initialError: string | null;
  onUnlocked: (roadmaps: Roadmap[]) => void;
}) {
  const [code, setCodeInput] = useState("");
  const [error, setError] = useState<string | null>(initialError);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onUnlocked(await listRoadmaps(code.trim()));
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
            autoFocus
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
  onUnauthorized: (err: ApiError) => void;
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
      if (isAuthError(err)) onUnauthorized(err);
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

// The open roadmap lives in the URL hash (#/roadmaps/<slug>), so reloads and the back button work without a router.
const SETTINGS_HASH = "#/settings";
const MEMORY_HASH = "#/memory";
const isMemoryHash = (h: string) => h === MEMORY_HASH || h === "#memory";
const ROUTE = /^#\/roadmaps\/([^/]+)$/;

function slugFromHash(): string | null {
  const m = ROUTE.exec(window.location.hash);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return null;
  }
}

function useSelectedSlug(): [string | null, (slug: string | null) => void] {
  const [slug, setSlug] = useState<string | null>(slugFromHash);
  useEffect(() => {
    const onHash = () => setSlug(slugFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const select = useCallback((next: string | null) => {
    window.location.hash = next ? `#/roadmaps/${encodeURIComponent(next)}` : "";
    setSlug(next);
  }, []);
  return [slug, select];
}

function RoadmapList({ roadmaps, onOpen }: { roadmaps: Roadmap[]; onOpen: (slug: string) => void }) {
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
            <span className="status">{statusText(r.status)}</span>
            <time dateTime={r.created_at}>{new Date(r.created_at).toLocaleDateString()}</time>
            <button type="button" className="secondary small-button" onClick={() => onOpen(r.slug)}>
              Open
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}

export function App() {
  const [health, setHealth] = useState<HealthState>(null);
  const [roadmaps, setRoadmaps] = useState<Roadmap[] | null>(null);
  // null: unlocked. Otherwise the passcode screen shows, with the lockout message for a 429.
  const [lock, setLock] = useState<{ error: string | null } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, select] = useSelectedSlug();
  const [onSettings, setOnSettings] = useState(() => window.location.hash === SETTINGS_HASH);
  const [onMemory, setOnMemory] = useState(() => isMemoryHash(window.location.hash));
  const [askConsent, setAskConsent] = useState(false);
  const unlocked = roadmaps !== null && lock === null;
  useEffect(() => {
    const onHash = () => {
      setOnSettings(window.location.hash === SETTINGS_HASH);
      setOnMemory(isMemoryHash(window.location.hash));
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const showPasscode = useCallback((err: ApiError) => {
    setLock({ error: err.status === 429 ? err.message : null });
  }, []);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setRoadmaps(await listRoadmaps());
      setLock(null);
    } catch (err) {
      if (isAuthError(err)) showPasscode(err);
      else setLoadError(messageOf(err));
    }
  }, [showPasscode]);

  // First run: ask once either consent is still unanswered. Only after unlock, since the API needs the passcode.
  useEffect(() => {
    if (!unlocked) return;
    getLearnerSettings().then(
      (s) => setAskConsent(s.consent_personal === null || s.consent_raw_messages === null),
      () => undefined,
    );
  }, [unlocked]);

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
        <nav className="topnav" aria-label="Main">
          <a href="#">Roadmaps</a>
          <a href={MEMORY_HASH}>Memory &amp; privacy</a>
          <a href={SETTINGS_HASH}>Settings</a>
          <HealthBadge health={health} />
        </nav>
      </header>
      <main className="page">
        {lock ? (
          <PasscodeScreen
            initialError={lock.error}
            onUnlocked={(list) => {
              setRoadmaps(list);
              setLoadError(null);
              setLock(null);
            }}
          />
        ) : onMemory ? (
          <Memory
            onUnauthorized={showPasscode}
            onBack={() => {
              window.location.hash = "";
            }}
          />
        ) : onSettings ? (
          <Settings
            onUnauthorized={showPasscode}
            onBack={() => {
              window.location.hash = "";
            }}
          />
        ) : selected ? (
          <RoadmapDetail
            key={selected}
            slug={selected}
            onUnauthorized={showPasscode}
            onOpenRoadmap={select}
            onBack={() => {
              select(null);
              void load(); // statuses may have moved on
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
                {roadmaps === null ? <p className="muted">Loading…</p> : <RoadmapList roadmaps={roadmaps} onOpen={select} />}
              </section>
              <CreateRoadmapForm
                onCreated={(r) => setRoadmaps((prev) => [r, ...(prev ?? [])])}
                onUnauthorized={showPasscode}
              />
            </div>
          </>
        )}
      </main>
      {askConsent && unlocked && <ConsentDialog onSaved={() => setAskConsent(false)} onUnauthorized={showPasscode} />}
    </>
  );
}
