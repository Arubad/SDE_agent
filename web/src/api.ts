import { clearCode, getCode, setCode } from "./storage";

export interface Health {
  status: "ok" | "degraded";
  db: "ok" | "down";
  llm_provider: string;
  public_mode: boolean;
}

export interface Roadmap {
  id: string;
  slug: string;
  title: string;
  status: string;
  created_at: string;
}

export interface RoadmapInput {
  slug: string;
  title: string;
  source_text: string;
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function errorMessage(status: number, body: unknown): string {
  if (body && typeof body === "object") {
    const { error, detail } = body as { error?: unknown; detail?: unknown };
    if (typeof error === "string" && error) return error;
    // FastAPI's own validation shape: {"detail": [...]}
    if (Array.isArray(detail)) return "Some required fields are missing or invalid.";
  }
  return `Request failed (HTTP ${status}).`;
}

// `candidateCode` is sent instead of the stored code and is saved only if the request succeeds,
// so a mistyped passcode is never kept or re-sent. Any 401 clears the stored code.
async function request<T>(path: string, init: RequestInit = {}, candidateCode?: string): Promise<T> {
  const headers = new Headers(init.headers);
  const code = candidateCode ?? getCode();
  if (code) headers.set("X-Access-Code", code);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");

  const res = await fetch(path, { ...init, headers });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401) clearCode();
    throw new ApiError(res.status, errorMessage(res.status, body));
  }
  if (candidateCode) setCode(candidateCode);
  return body as T;
}

export function getHealth(): Promise<Health> {
  return request<Health>("/api/health");
}

/** Pass `candidateCode` to try a passcode; it is stored only if the server accepts it. */
export function listRoadmaps(candidateCode?: string): Promise<Roadmap[]> {
  return request<Roadmap[]>("/api/roadmaps", {}, candidateCode);
}

export function createRoadmap(input: RoadmapInput): Promise<Roadmap> {
  return request<Roadmap>("/api/roadmaps", { method: "POST", body: JSON.stringify(input) });
}
