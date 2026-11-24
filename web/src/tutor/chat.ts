// The tutor chat's state machine and helpers: a pure reducer over the turn's SSE events, span lanes for the
// timeline, and the driver that runs one turn. Nothing here touches React.
import { ApiError } from "../api";
import { type EventHandler, streamEvents } from "../stream";

// --- shapes (backend/tutor/tutor/state.py, tutor_api.py) ---------------------------------------------------

export interface RoadmapSource {
  n?: number;
  title?: string;
  section?: string;
  url?: string;
  source_type?: string;
  text?: string;
}
export interface WebSource {
  url: string;
  title: string;
}
export interface Sources {
  roadmap: RoadmapSource[];
  web: WebSource[];
}

export interface Resource {
  title: string;
  url: string | null;
  kind: string;
}
export interface TodaySkill {
  id: string;
  title: string;
  objectives: string[];
  mastery: number;
  weak_spots: string[];
  resources: Resource[];
}
export interface Today {
  session: { index: number; date: string; skills: string[]; minutes: number } | null;
  skills: TodaySkill[];
}

export interface Grade {
  id: string;
  score: number;
  feedback: string;
  answer: string;
  explanation: string;
}
export interface QuizResult {
  skill_id: string;
  grades: Grade[];
  mastery: { before: number; after: number };
}

export interface SessionStartInterrupt {
  kind: "session_start";
  trigger: string;
  last: { id: string; title: string | null; date: string | null; summary: string | null } | null;
  earlier: { id: string; title: string | null; date: string | null }[];
  /** `recap_last`, `recap_earlier` (only with earlier sessions), `warm_up`, `skip`. */
  options: string[];
}
export interface QuizQuestion {
  id: string;
  type: "mcq" | "short";
  prompt: string;
  options?: string[] | null;
}
export interface QuizInterrupt {
  kind: "quiz";
  skill_id: string;
  questions: QuizQuestion[];
}
export interface ApprovalInterrupt {
  kind: "approval";
  id: string;
  action: string;
  summary: string;
  payload: unknown;
}
export type Interrupt = SessionStartInterrupt | QuizInterrupt | ApprovalInterrupt | { kind: string; [k: string]: unknown };

export interface Span {
  name: string;
  kind: string;
  start_ms: number;
  duration_ms: number;
  status?: string;
  [k: string]: unknown;
}
export interface TurnTrace {
  trace_id: string;
  total_ms: number;
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  spans: Span[];
  [k: string]: unknown;
}

export interface ChatMessage {
  role: "user" | "assistant" | "result";
  text: string;
  sources?: Sources;
  notice?: string | null;
  quiz?: QuizResult;
}

export interface ChatState {
  messages: ChatMessage[];
  /** The reply text streamed so far, before the final `reply` arrives. */
  streaming: string;
  pending: Interrupt | null;
  today: Today | null;
  /** One trace per finished turn, oldest first. */
  timeline: TurnTrace[];
  error: string | null;
  /** The slug of a roadmap the research action created. */
  created: string | null;
  /** An `interrupt` arrived in the turn that is running: its `done` must not clear `pending`. */
  interrupted: boolean;
}

export const initial: ChatState = {
  messages: [],
  streaming: "",
  pending: null,
  today: null,
  timeline: [],
  error: null,
  created: null,
  interrupted: false,
};

/** Events the reducer takes: the server's turn events plus four local ones (`turn_start`, `user`, `loaded`, `reset`). */
export type ChatEvent = { type: string; [k: string]: unknown };

export const DID_NOT_FINISH = "The turn didn't finish. Your session is saved; try again.";

/** The session the server holds (GET /api/sessions/{id}), as the reducer wants it. */
export interface ServerState {
  messages: unknown;
  today: Today | null;
  pending: Interrupt | null;
}

export function reduce(state: ChatState, ev: ChatEvent): ChatState {
  switch (ev.type) {
    case "turn_start":
      return { ...state, streaming: "", error: null, interrupted: false };
    case "user":
      return { ...state, messages: [...state.messages, { role: "user", text: String(ev.text) }] };
    case "reply_delta":
      return { ...state, streaming: ev.reset ? "" : state.streaming + String(ev.delta ?? "") };
    case "reply":
      return {
        ...state,
        streaming: "",
        messages: [
          ...state.messages,
          {
            role: "assistant",
            text: String(ev.text ?? ""),
            sources: (ev.sources as Sources | undefined) ?? { roadmap: [], web: [] },
            notice: (ev.notice as string | null | undefined) ?? null,
          },
        ],
      };
    case "interrupt":
      return { ...state, pending: ev.value as Interrupt, interrupted: true };
    case "plan":
      return { ...state, today: ev.today as Today };
    case "quiz_result": {
      const quiz = { skill_id: String(ev.skill_id), grades: ev.grades as Grade[], mastery: ev.mastery as QuizResult["mastery"] };
      return { ...state, messages: [...state.messages, { role: "result", text: "Quiz graded", quiz }] };
    }
    case "roadmap_created":
      return { ...state, created: String(ev.slug) };
    case "error":
      return { ...state, error: String(ev.message ?? "Something went wrong.") };
    case "done":
      return {
        ...state,
        streaming: "",
        timeline: [...state.timeline, ev.trace as TurnTrace],
        pending: state.interrupted ? state.pending : null,
        interrupted: false,
      };
    case "reset":
      return initial; // a different session: nothing of the old one (timeline, created roadmap, error) carries over
    case "loaded": {
      const s = ev.session as ServerState;
      return {
        ...state,
        messages: messagesFromServer(s.messages),
        streaming: "",
        pending: s.pending ?? null,
        today: s.today ?? state.today,
        interrupted: false,
      };
    }
    default:
      return state; // `node` and anything newer
  }
}

// --- restored messages ------------------------------------------------------------------------------------

const LEADING_BLOCK = /^\s*<(context|summary)>[\s\S]*?<\/\1>\s*/;

/** A stored user message begins with the teacher's volatile `<context>` block (and maybe a `<summary>`): drop them. */
export function stripContext(text: string): string {
  let out = text;
  for (let m = LEADING_BLOCK.exec(out); m; m = LEADING_BLOCK.exec(out)) out = out.slice(m[0].length);
  return out;
}

function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((b) => (typeof b === "string" ? b : b && typeof b === "object" && typeof (b as { text?: unknown }).text === "string" ? (b as { text: string }).text : ""))
      .join("");
  }
  return "";
}

/** The stored `{role, content}` messages as chat messages; only user and assistant turns with text are shown. */
export function messagesFromServer(raw: unknown): ChatMessage[] {
  if (!Array.isArray(raw)) return [];
  const out: ChatMessage[] = [];
  for (const m of raw) {
    if (!m || typeof m !== "object") continue;
    const { role, content } = m as { role?: unknown; content?: unknown };
    if (role !== "user" && role !== "assistant") continue;
    const text = contentText(content);
    const shown = role === "user" ? stripContext(text) : text;
    if (shown.trim()) out.push({ role, text: shown });
  }
  return out;
}

// --- timeline -----------------------------------------------------------------------------------------------

/**
 * Lane assignment for overlapping spans (greedy interval scheduling). Returns lanes: each lane is a list of
 * indexes into `spans`, in start order, whose `[start_ms, start_ms + duration_ms)` intervals do not overlap.
 * A span goes in the first lane that is free at its start, else opens a new lane, so spans that run at the same
 * time land in different lanes: `[[0, 1], [2]]` is span 0 then span 1 in lane 0, with span 2 alongside in lane 1.
 */
export function lanes(spans: { start_ms: number; duration_ms: number }[]): number[][] {
  const order = spans.map((_, i) => i).sort((a, b) => spans[a].start_ms - spans[b].start_ms || a - b);
  const out: number[][] = [];
  const ends: number[] = [];
  for (const i of order) {
    const { start_ms, duration_ms } = spans[i];
    let lane = ends.findIndex((end) => end <= start_ms);
    if (lane === -1) {
      lane = out.length;
      out.push([]);
      ends.push(0);
    }
    out[lane].push(i);
    ends[lane] = start_ms + duration_ms;
  }
  return out;
}

/** Cache reads as a share of every prompt token (fresh input, cache reads and cache writes); 0 with no tokens. */
export function cacheHitRate(t: TurnTrace): number {
  const total = t.input_tokens + t.cache_read_tokens + t.cache_write_tokens;
  return total > 0 ? t.cache_read_tokens / total : 0;
}

export function costText(usd: number): string {
  return `$${usd.toFixed(4)}`;
}

// --- running a turn -------------------------------------------------------------------------------------------

export type TurnBody = { text: string } | { action: string; topic?: string } | { resume: unknown };
type Streamer = (
  path: string,
  onEvent: EventHandler,
  signal: AbortSignal,
  init?: { method: "POST"; body: unknown },
) => Promise<void>;

/**
 * POST one turn and feed its events to `dispatch`. Resolves true when the stream ended with `done`; false when
 * it ended without one (an `error` event is dispatched, and the caller reloads the session). HTTP errors throw
 * `ApiError`; show them with `turnErrorText`.
 */
export async function runTurn(
  path: string,
  body: TurnBody,
  dispatch: (ev: ChatEvent) => void,
  signal: AbortSignal,
  stream: Streamer = streamEvents,
): Promise<boolean> {
  let finished = false;
  dispatch({ type: "turn_start" });
  await stream(
    path,
    (name, data) => {
      if (name === "done") finished = true;
      const fields = data && typeof data === "object" ? (data as object) : {};
      dispatch({ ...fields, type: name });
    },
    signal,
    { method: "POST", body },
  );
  if (!finished && !signal.aborted) dispatch({ type: "error", message: DID_NOT_FINISH });
  return finished;
}

/** Turn controls are disabled only while a turn runs: a pending card never blocks free text or an action (R5). */
export function controlsLocked(busy: boolean, _pending: Interrupt | null): boolean {
  return busy;
}

export function turnErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409 && !/ended/i.test(err.message)) return "Another turn is still running.";
    return err.message;
  }
  return err instanceof Error ? err.message : "Something went wrong.";
}
