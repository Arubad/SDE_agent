import { type FormEvent, useEffect, useState } from "react";
import { type ApiError, isAuthError } from "./api";
import { getStudySettings, putStudySettings, type StudySettings as Settings } from "./tutorApi";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Collapsible study settings for the Tutor tab: weekly hours (blank = the roadmap's default), session length,
 *  study days and an optional deadline. Server validation errors show inline. */
export function StudySettings({ slug, onUnauthorized }: { slug: string; onUnauthorized: (err: ApiError) => void }) {
  const [stored, setStored] = useState<Settings | null>(null);
  const [hours, setHours] = useState("");
  const [minutes, setMinutes] = useState("");
  const [days, setDays] = useState<number[]>([]);
  const [deadline, setDeadline] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  function show(s: Settings) {
    setStored(s);
    setHours(s.weekly_hours === null ? "" : String(s.weekly_hours));
    setMinutes(String(s.session_minutes));
    setDays(s.study_days);
    setDeadline(s.deadline ?? "");
  }

  const fail = (err: unknown) => {
    if (isAuthError(err)) onUnauthorized(err);
    else setError(err instanceof Error ? err.message : "Something went wrong.");
  };

  useEffect(() => {
    let live = true;
    getStudySettings(slug)
      .then((s) => live && show(s))
      .catch((err: unknown) => live && fail(err));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      show(
        await putStudySettings(slug, {
          weekly_hours: hours.trim() === "" ? null : Number(hours),
          session_minutes: Number(minutes),
          study_days: days,
          deadline: deadline || null,
        }),
      );
      setSaved(true);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="card study-settings">
      <summary>Study settings</summary>
      {stored && (
        <form className="stack" onSubmit={save}>
          <div className="row">
            <label className="field">
              <span>Weekly hours</span>
              <input type="number" step="0.5" min="0.5" max="80" value={hours} placeholder="Roadmap default" onChange={(e) => setHours(e.target.value)} />
              <span className="muted small">Using {stored.effective_weekly_hours} h/week. Leave blank for the roadmap's default.</span>
            </label>
            <label className="field">
              <span>Session minutes</span>
              <input type="number" min="15" max="240" value={minutes} onChange={(e) => setMinutes(e.target.value)} required />
            </label>
          </div>
          <fieldset className="days">
            <legend>Study days</legend>
            {DAYS.map((d, i) => (
              <label key={d} className="toggle">
                <input
                  type="checkbox"
                  checked={days.includes(i)}
                  onChange={(e) => setDays(e.target.checked ? [...days, i].sort() : days.filter((x) => x !== i))}
                />
                {d}
              </label>
            ))}
          </fieldset>
          <div className="row">
            <label className="field">
              <span>Deadline</span>
              <input type="date" value={deadline} onChange={(e) => setDeadline(e.target.value)} />
            </label>
            {deadline && (
              <button type="button" className="secondary small-button" onClick={() => setDeadline("")}>
                Clear deadline
              </button>
            )}
          </div>
          {error && <p className="error" role="alert">{error}</p>}
          {saved && <p className="muted small" role="status">Saved.</p>}
          <button type="submit" disabled={busy}>{busy ? "Saving…" : "Save settings"}</button>
        </form>
      )}
      {!stored && !error && <p className="muted">Loading…</p>}
      {!stored && error && <p className="error" role="alert">{error}</p>}
    </details>
  );
}
