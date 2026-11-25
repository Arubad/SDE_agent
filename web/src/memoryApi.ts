// Client for the Memory & privacy API (backend/tutor/memory_api.py).
import { ApiError, request } from "./api";
import { getCode } from "./storage";

export interface LearnerSettings {
  consent_personal: boolean | null;
  consent_raw_messages: boolean | null;
  retention_days: number;
}
export type SettingsPatch = Partial<LearnerSettings> & { confirm_purge?: boolean };

export interface Fact {
  id: string;
  kind: "fact" | "preference" | "goal";
  text: string;
  roadmap: string | null;
  created_at: string | null;
  updated_at: string | null;
}
export const FACT_KINDS: Fact["kind"][] = ["fact", "preference", "goal"];
export const EXPORT_NAME = "roadmap-tutor-export.json";

export const getLearnerSettings = () => request<LearnerSettings>("/api/learner/settings");
export const putLearnerSettings = (patch: SettingsPatch) =>
  request<LearnerSettings>("/api/learner/settings", { method: "PUT", body: JSON.stringify(patch) });

export const listFacts = () => request<Fact[]>("/api/learner/facts?roadmap=all");
export const editFact = (id: string, text: string, kind: Fact["kind"]) =>
  request<Fact>(`/api/learner/facts/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ text, kind }) });
export const deleteFact = (id: string) => request<null>(`/api/learner/facts/${encodeURIComponent(id)}`, { method: "DELETE" });

export const deleteSession = (id: string) => request<null>(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
export const resetProgress = (slug: string, skillId: string) =>
  request<null>(`/api/roadmaps/${encodeURIComponent(slug)}/progress/${encodeURIComponent(skillId)}`, { method: "DELETE" });

export type ConsentOutcome = { kind: "saved"; settings: LearnerSettings } | { kind: "confirm"; message: string };

/** Saves a settings change. A 409 means the change would purge remembered facts: the caller shows the server's
 *  message and, once the learner agrees, calls again with `confirmed` to resend it with `confirm_purge`. */
export async function applyConsent(
  put: (patch: SettingsPatch) => Promise<LearnerSettings>,
  patch: SettingsPatch,
  confirmed = false,
): Promise<ConsentOutcome> {
  try {
    return { kind: "saved", settings: await put(confirmed ? { ...patch, confirm_purge: true } : patch) };
  } catch (err) {
    if (!confirmed && err instanceof ApiError && err.status === 409) return { kind: "confirm", message: err.message };
    throw err;
  }
}

/** Downloads the full export. A plain link cannot send the access code, so it is fetched as a blob. */
export async function downloadExport(): Promise<void> {
  const headers = new Headers();
  const code = getCode();
  if (code) headers.set("X-Access-Code", code);
  const res = await fetch("/api/learner/export", { headers });
  if (!res.ok) throw new ApiError(res.status, `Export failed (HTTP ${res.status}).`);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = EXPORT_NAME;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
