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

/** A 401: the stored passcode was missing or wrong and has been cleared. The App shows the passcode screen. */
export class Unauthorized extends ApiError {
  constructor(message: string) {
    super(401, message);
    this.name = "Unauthorized";
  }
}

/** 401 (no or wrong passcode) and 429 (passcode lockout) both send the learner to the passcode screen. */
export function isAuthError(err: unknown): err is ApiError {
  return err instanceof ApiError && (err.status === 401 || err.status === 429);
}

export function errorMessage(status: number, body: unknown): string {
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
export async function request<T>(path: string, init: RequestInit = {}, candidateCode?: string): Promise<T> {
  const headers = new Headers(init.headers);
  const code = candidateCode ?? getCode();
  if (code) headers.set("X-Access-Code", code);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");

  const res = await fetch(path, { ...init, headers });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401) {
      clearCode();
      throw new Unauthorized(errorMessage(res.status, body));
    }
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

// --- Roadmap review and ingestion (shapes from backend/tutor/api.py) ---------------------------------

export type ResourceKind =
  | "video"
  | "playlist"
  | "doc"
  | "article"
  | "course_site"
  | "interactive"
  | "pdf"
  | "repo"
  | "other";

export interface SkillSpec {
  id: string;
  title: string;
  objectives: string[];
  prerequisites: string[];
}

export interface ResourceSpec {
  id: string;
  title: string;
  kind: ResourceKind;
  url: string | null;
  url_hint: string | null;
  query: string;
  note?: string | null;
  focus?: string[];
  skill_ids: string[];
}

export interface StageSpec {
  id: string;
  title: string;
  timeframe: string | null;
  summary: string;
  skills: SkillSpec[];
  resources: ResourceSpec[];
  practice_goals: string[];
}

export interface RoadmapSpec {
  title: string;
  goals: string[];
  weekly_hours: number | null;
  stages: StageSpec[];
}

export interface Candidate {
  url: string;
  title: string;
  source: "url_hint" | "search";
  score: number;
}

export interface Resolution {
  resource_id: string;
  status: "has_url" | "resolved" | "needs_url" | "linked_only";
  candidates: Candidate[];
  message: string | null;
}

export interface ParseResult {
  spec: RoadmapSpec;
  resolution: Resolution[];
}

/** Job counts by type, then by status (queued, running, done, dead, ...). */
export type JobCounts = Record<string, Record<string, number>>;

export interface ProgressResource {
  key: string;
  title: string;
  kind: string;
  url: string | null;
  status: string;
  reason: string | null;
  stats: Record<string, unknown> | null;
}

export interface Progress {
  jobs: JobCounts;
  resources: ProgressResource[];
  status: string;
}

export interface RoadmapDetail extends Roadmap, Progress {
  spec: RoadmapSpec | null;
}

export interface ReportResource {
  id: string;
  title: string;
  kind: string;
  url: string | null;
  status: string;
  reason: string | null;
  pages: number;
  videos: number;
  chunks: number;
  /** Pages rendered by the gateway's browser; present only when some were. */
  rendered_by_browser?: number;
}

export interface AgentRun {
  mode: string;
  round: number;
  stop: string | null;
  steps: number;
  cost_usd: number;
  notes: string | null;
}

export interface AgentAction {
  tool: string;
  args: unknown;
  outcome: string;
  summary: string | null;
}

export interface Proposal {
  id: string;
  skill_id: string | null;
  title: string;
  url: string | null;
  reason: string | null;
}

export interface Report {
  status: string;
  resources: ReportResource[];
  videos: { total: number; segments: Record<string, number>; transcripts: Record<string, unknown> };
  /** `url` is null when the report could not tie a failure to a URL (e.g. a dead job whose document is gone). */
  skipped: { url: string | null; reason: string }[];
  /** Pages rendered by the gateway's browser, over the whole roadmap (absent from older servers). */
  rendered_by_browser?: number;
  summary: string;
  agent?: { runs: AgentRun[]; actions: AgentAction[] };
  proposals?: Proposal[];
}

export interface ProposalResult {
  id: string;
  status: string;
  roadmap_status: string;
}

/** A resource id -> the URL to ingest, or null to skip it. */
export type Choices = Record<string, string | null>;

export const TERMINAL_STATUSES = ["ready", "ready_with_gaps", "failed"];

export function roadmapPath(slug: string, rest = ""): string {
  return `/api/roadmaps/${encodeURIComponent(slug)}${rest}`;
}

function post<T>(path: string, body: unknown = {}): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) });
}

export function getRoadmap(slug: string): Promise<RoadmapDetail> {
  return request<RoadmapDetail>(roadmapPath(slug));
}

export function parseRoadmap(slug: string): Promise<ParseResult> {
  return post<ParseResult>(roadmapPath(slug, "/parse"));
}

export function approveRoadmap(slug: string, spec: RoadmapSpec, choices: Choices): Promise<{ status: string }> {
  return post(roadmapPath(slug, "/approve"), { spec, choices });
}

export function getReport(slug: string): Promise<Report> {
  return request<Report>(roadmapPath(slug, "/report"));
}

export function approveProposal(slug: string, id: string): Promise<ProposalResult> {
  return post<ProposalResult>(roadmapPath(slug, `/resources/${encodeURIComponent(id)}/approve`));
}

export function dismissProposal(slug: string, id: string): Promise<ProposalResult> {
  return post<ProposalResult>(roadmapPath(slug, `/resources/${encodeURIComponent(id)}/dismiss`));
}

// --- MCP servers (shapes from backend/tutor/mcp/gateway.py:ServerHealth; never carries secrets) ----------

export interface ServerHealth {
  name: string;
  namespace: string;
  transport: string;
  enabled: boolean;
  state: "ok" | "down" | "disabled" | "missing_env";
  tools: number;
  latency_ms: number | null;
  error: string | null;
  checked_at: string | null;
  note: string;
}

export function listServers(): Promise<ServerHealth[]> {
  return request<ServerHealth[]>("/api/mcp/servers");
}

export function checkServer(name: string): Promise<ServerHealth> {
  return request<ServerHealth>(`/api/mcp/servers/${encodeURIComponent(name)}/check`, { method: "POST" });
}

export function setServerEnabled(name: string, enabled: boolean): Promise<ServerHealth> {
  return request<ServerHealth>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}
