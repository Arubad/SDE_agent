import { type FormEvent, useEffect, useRef, useState } from "react";
import { type ApiError, isAuthError } from "./api";
import { putLearnerSettings, type LearnerSettings } from "./memoryApi";

/** First-run consent. Both boxes start unchecked; saving sends both answers, so it never shows again. */
export function ConsentDialog({
  onSaved,
  onUnauthorized,
}: {
  onSaved: (s: LearnerSettings) => void;
  onUnauthorized: (err: ApiError) => void;
}) {
  const [personal, setPersonal] = useState(false);
  const [raw, setRaw] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const first = useRef<HTMLInputElement | null>(null);
  useEffect(() => first.current?.focus(), []);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSaved(await putLearnerSettings({ consent_personal: personal, consent_raw_messages: raw }));
    } catch (err) {
      if (isAuthError(err)) onUnauthorized(err);
      else setError(err instanceof Error ? err.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <form className="card stack modal" role="dialog" aria-modal="true" aria-labelledby="consent-heading" onSubmit={save}>
        <h2 id="consent-heading">Your privacy</h2>
        <p>
          Progress (mastery, plans, session summaries) is always kept on this machine. You can also let the tutor
          remember personal facts and keep raw chat messages. Everything works either way.
        </p>
        <label className="option">
          <input ref={first} type="checkbox" checked={personal} onChange={(e) => setPersonal(e.target.checked)} />
          <span className="option-text">Remember personal facts</span>
        </label>
        <label className="option">
          <input type="checkbox" checked={raw} onChange={(e) => setRaw(e.target.checked)} />
          <span className="option-text">Keep raw chat messages</span>
        </label>
        <p className="muted small">You can change this any time under Memory &amp; privacy.</p>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? "Saving…" : "Save"}</button>
      </form>
    </div>
  );
}
