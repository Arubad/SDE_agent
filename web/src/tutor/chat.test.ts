import { describe, expect, it } from "vitest";
import { ApiError } from "../api";
import {
  type ChatState,
  cacheHitRate,
  controlsLocked,
  initial,
  lanes,
  messagesFromServer,
  reduce,
  runTurn,
  stripContext,
  turnErrorText,
  type TurnTrace,
} from "./chat";

const noSources = { roadmap: [], web: [] };
const trace = (over: Partial<TurnTrace> = {}): TurnTrace => ({
  trace_id: "t1",
  total_ms: 1200,
  llm_calls: 2,
  input_tokens: 100,
  output_tokens: 50,
  cache_read_tokens: 300,
  cache_write_tokens: 100,
  cost_usd: 0.01234,
  spans: [],
  ...over,
});

describe("reduce", () => {
  it("reply deltas accumulate then the reply replaces them", () => {
    let s = reduce(initial, { type: "reply_delta", delta: "Po", reset: false });
    s = reduce(s, { type: "reply_delta", delta: "ds", reset: false });
    expect(s.streaming).toBe("Pods");
    s = reduce(s, { type: "reply", text: "Pods.", sources: noSources, notice: null });
    expect(s.streaming).toBe("");
    expect(s.messages.at(-1)?.text).toBe("Pods.");
    expect(s.messages.at(-1)?.role).toBe("assistant");
  });

  it("a reset delta clears what streamed so far", () => {
    let s = reduce(initial, { type: "reply_delta", delta: "draft", reset: false });
    s = reduce(s, { type: "reply_delta", delta: "", reset: true });
    expect(s.streaming).toBe("");
  });

  it("a reply keeps its sources and notice", () => {
    const sources = { roadmap: [{ n: 1, title: "Pods", url: "https://k8s.io" }], web: [{ url: "https://a.dev", title: "A" }] };
    const s = reduce(initial, { type: "reply", text: "x [1]", sources, notice: "Budget low" });
    expect(s.messages[0].sources).toEqual(sources);
    expect(s.messages[0].notice).toBe("Budget low");
  });

  it("done keeps an interrupt from the same turn", () => {
    const value = { kind: "quiz", skill_id: "s", questions: [] };
    let s = reduce(initial, { type: "interrupt", value });
    s = reduce(s, { type: "done", trace: trace() });
    expect(s.pending).toEqual(value);
    expect(s.timeline).toHaveLength(1);
  });

  it("done clears a pending card when the turn raised no interrupt", () => {
    const value = { kind: "approval", id: "a", action: "x", summary: "s", payload: {} };
    let s: ChatState = { ...reduce(initial, { type: "interrupt", value }), };
    s = reduce(s, { type: "done", trace: trace() }); // the turn that raised it ends
    s = reduce(s, { type: "turn_start" }); // the resume turn begins; the card stays until it settles
    expect(s.pending).toEqual(value);
    s = reduce(s, { type: "done", trace: trace() });
    expect(s.pending).toBeNull();
  });

  it("plan sets today, quiz_result pushes a result message, error sets error", () => {
    const today = { session: null, skills: [] };
    let s = reduce(initial, { type: "plan", today, version: 2 });
    expect(s.today).toEqual(today);
    s = reduce(s, {
      type: "quiz_result",
      skill_id: "pods",
      grades: [{ id: "q1", score: 1, feedback: "ok", answer: "a", explanation: "e" }],
      mastery: { before: 0.2, after: 0.5 },
    });
    expect(s.messages.at(-1)?.role).toBe("result");
    expect(s.messages.at(-1)?.quiz?.mastery.after).toBe(0.5);
    s = reduce(s, { type: "error", message: "Budget" });
    expect(s.error).toBe("Budget");
  });

  it("roadmap_created records the slug; turn_start resets turn state and keeps messages", () => {
    let s = reduce(initial, { type: "user", text: "hi" });
    s = reduce(s, { type: "roadmap_created", slug: "new-one" });
    expect(s.created).toBe("new-one");
    s = reduce(s, { type: "error", message: "x" });
    s = reduce(s, { type: "turn_start" });
    expect(s.error).toBeNull();
    expect(s.messages).toHaveLength(1);
  });
});

describe("lanes", () => {
  const SPANS = [
    { start_ms: 0, duration_ms: 10 },
    { start_ms: 10, duration_ms: 10 },
    { start_ms: 5, duration_ms: 10 },
  ];
  it("puts overlapping spans in different lanes; lanes hold span indexes in start order", () => {
    expect(lanes(SPANS)).toEqual([[0, 1], [2]]);
  });
  it("handles no spans and a fully parallel set", () => {
    expect(lanes([])).toEqual([]);
    expect(lanes([{ start_ms: 0, duration_ms: 5 }, { start_ms: 1, duration_ms: 5 }, { start_ms: 2, duration_ms: 5 }])).toEqual([[0], [1], [2]]);
  });
});

describe("stripContext", () => {
  it("removes a leading context and summary block", () => {
    expect(stripContext("<context>\nToday: x\n</context>\n\nWhat is a pod?")).toBe("What is a pod?");
    expect(stripContext("<summary>old</summary>\n<context>c</context>\nHi")).toBe("Hi");
  });
  it("leaves other text alone, including a later tag", () => {
    expect(stripContext("Explain <context> please")).toBe("Explain <context> please");
    expect(stripContext("plain")).toBe("plain");
  });
});

describe("messagesFromServer", () => {
  it("maps user and assistant messages, strips context, drops the rest", () => {
    const out = messagesFromServer([
      { role: "user", content: "<context>c</context>\nHello" },
      { role: "assistant", content: "Hi there" },
      { role: "tool", content: "ignored" },
    ]);
    expect(out.map((m) => [m.role, m.text])).toEqual([["user", "Hello"], ["assistant", "Hi there"]]);
  });
});

describe("cacheHitRate and errors", () => {
  it("is cache reads over all prompt tokens", () => {
    expect(cacheHitRate(trace())).toBeCloseTo(0.6);
    expect(cacheHitRate(trace({ input_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 }))).toBe(0);
  });
  it("maps 409 busy and 422 to messages", () => {
    expect(turnErrorText(new ApiError(409, "Another turn is running for this session."))).toBe("Another turn is still running.");
    expect(turnErrorText(new ApiError(409, "This session has ended."))).toBe("This session has ended.");
    expect(turnErrorText(new ApiError(422, "'text' is too long"))).toBe("'text' is too long");
  });
});

describe("runTurn", () => {
  it("dispatches events and reports finished when done arrives", async () => {
    const seen: string[] = [];
    const stream = async (_p: string, on: (n: string, d: unknown) => void) => {
      on("reply", { text: "A", sources: noSources, notice: null });
      on("done", { trace: trace() });
    };
    const ok = await runTurn("/p", { text: "q" }, (ev) => seen.push(ev.type), new AbortController().signal, stream);
    expect(ok).toBe(true);
    expect(seen).toEqual(["turn_start", "reply", "done"]);
  });

  it("a stream that ends without done is a failed turn with the saved-session message", async () => {
    const seen: { type: string; message?: unknown }[] = [];
    const stream = async (_p: string, on: (n: string, d: unknown) => void) => {
      on("reply_delta", { delta: "par", reset: false });
    };
    const ok = await runTurn("/p", { action: "continue" }, (ev) => seen.push(ev), new AbortController().signal, stream);
    expect(ok).toBe(false);
    expect(seen.at(-1)).toEqual({ type: "error", message: "The turn didn't finish. Your session is saved; try again." });
  });
});

describe("reset and controls", () => {
  it("reset clears the old session's timeline, created roadmap, error and card", () => {
    let s = reduce(initial, { type: "roadmap_created", slug: "x" });
    s = reduce(s, { type: "interrupt", value: { kind: "quiz", skill_id: "s", questions: [] } });
    s = reduce(s, { type: "done", trace: trace() });
    s = reduce(s, { type: "error", message: "boom" });
    s = reduce(s, { type: "reset" });
    expect(s).toEqual(initial);
  });
  it("controls stay enabled with a pending card and lock only while busy", () => {
    const card = { kind: "quiz", skill_id: "s", questions: [] };
    expect(controlsLocked(false, card)).toBe(false);
    expect(controlsLocked(true, card)).toBe(true);
    expect(controlsLocked(true, null)).toBe(true);
  });
});
