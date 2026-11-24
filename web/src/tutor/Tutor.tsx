import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { type ApiError, isAuthError } from "../api";
import {
  createSession,
  endSession,
  getSession,
  listSessions,
  type Mode,
  type SessionDetail,
  turnPath,
} from "../tutorApi";
import { ApprovalCard } from "./ApprovalCard";
import {
  type ApprovalInterrupt,
  type ChatMessage,
  controlsLocked,
  initial,
  type QuizInterrupt,
  reduce,
  runTurn,
  type SessionStartInterrupt,
  turnErrorText,
  type TurnBody,
} from "./chat";
import { QuizCard } from "./QuizCard";
import { RichText } from "./RichText";
import { SessionStartCard } from "./SessionStartCard";
import { Sources } from "./Sources";
import { Timeline } from "./Timeline";
import { TodayCard } from "./TodayCard";

function Message({ m }: { m: ChatMessage }) {
  if (m.role === "result" && m.quiz) {
    const { grades, mastery } = m.quiz;
    return (
      <div className="msg msg-result card stack">
        <strong>
          Quiz graded: mastery {Math.round(mastery.before * 100)}% → {Math.round(mastery.after * 100)}%
        </strong>
        {grades.map((g) => (
          <div key={g.id} className="small">
            <p className="para">
              <strong>{g.score >= 1 ? "Correct" : g.score > 0 ? "Partly right" : "Not yet"}.</strong> {g.feedback}
            </p>
            {g.answer && <p className="muted para">Your answer: {g.answer}</p>}
            {g.explanation && <p className="muted para">{g.explanation}</p>}
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className={`msg msg-${m.role}`}>
      <RichText text={m.text} />
      {m.notice && <p className="notice small">{m.notice}</p>}
      {m.sources && <Sources sources={m.sources} />}
    </div>
  );
}

function StartPanel({ busy, onStart }: { busy: boolean; onStart: (mode: Mode) => void }) {
  const [mode, setMode] = useState<Mode>("tutor");
  return (
    <section className="card stack" aria-label="Start a session">
      <h3>Start a session</h3>
      <fieldset className="modes" disabled={busy}>
        <legend className="sr-only">Mode</legend>
        <label className="option">
          <input type="radio" name="mode" checked={mode === "tutor"} onChange={() => setMode("tutor")} />
          <span className="option-text">
            <strong>Tutor</strong>
            <span className="muted">Follows your plan, quizzes you and tracks mastery.</span>
          </span>
        </label>
        <label className="option">
          <input type="radio" name="mode" checked={mode === "qa"} onChange={() => setMode("qa")} />
          <span className="option-text">
            <strong>Free Q&amp;A</strong>
            <span className="muted">Ask anything about this roadmap's material.</span>
          </span>
        </label>
      </fieldset>
      <button type="button" disabled={busy} onClick={() => onStart(mode)}>
        {busy ? "Starting…" : "Start"}
      </button>
    </section>
  );
}

/** The tutor chat for one roadmap: opens the active session (or a chosen one), streams turns, shows cards. */
export function Tutor({
  slug,
  sessionId,
  onNewSession,
  onOpenRoadmap,
  onUnauthorized,
}: {
  slug: string;
  /** A session picked from the Sessions tab; null opens the active one, or the start panel. */
  sessionId: string | null;
  onNewSession: () => void;
  onOpenRoadmap?: (slug: string) => void;
  onUnauthorized: (err: ApiError) => void;
}) {
  const [chat, dispatch] = useReducer(reduce, initial);
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [researching, setResearching] = useState(false);
  const [topic, setTopic] = useState("");
  const abort = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (isAuthError(err)) onUnauthorized(err);
      else setFatal(turnErrorText(err));
    },
    [onUnauthorized],
  );

  const load = useCallback(async (id: string) => {
    const detail = await getSession(id);
    setSession(detail);
    dispatch({ type: "loaded", session: detail });
    return detail;
  }, []);

  // Open the requested session, else the active one, else show the start panel.
  useEffect(() => {
    let live = true;
    setLoading(true);
    setFatal(null);
    dispatch({ type: "reset" });
    (async () => {
      try {
        const id = sessionId ?? (await listSessions(slug)).find((s) => s.status === "active")?.id ?? null;
        if (!live) return;
        if (id) await load(id);
        else setSession(null);
      } catch (err) {
        if (live) fail(err);
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => {
      live = false;
      abort.current?.abort();
    };
  }, [slug, sessionId, load, fail]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [chat.messages.length, chat.streaming, chat.pending]);

  async function send(sid: string, body: TurnBody, shownText?: string) {
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setResearching(false);
    if (shownText) dispatch({ type: "user", text: shownText });
    let finished = false;
    try {
      finished = await runTurn(turnPath(sid), body, dispatch, controller.signal);
    } catch (err) {
      if (isAuthError(err)) return onUnauthorized(err);
      dispatch({ type: "error", message: turnErrorText(err) });
    } finally {
      setBusy(false);
    }
    if (!finished && !controller.signal.aborted) {
      try {
        await load(sid); // the server's state is the truth after a failed turn
      } catch (err) {
        if (isAuthError(err)) onUnauthorized(err);
      }
    }
  }

  async function start(mode: Mode) {
    setStarting(true);
    setFatal(null);
    try {
      const created = await createSession(slug, mode);
      dispatch({ type: "loaded", session: { messages: [], today: null, pending: null } });
      const detail = await getSession(created.id);
      setSession(detail);
      setStarting(false);
      await send(created.id, { action: "start" });
    } catch (err) {
      fail(err);
    } finally {
      setStarting(false);
    }
  }

  async function end() {
    if (!session) return;
    abort.current?.abort();
    setBusy(true);
    try {
      await endSession(session.id);
      await load(session.id);
    } catch (err) {
      if (isAuthError(err)) onUnauthorized(err);
      else dispatch({ type: "error", message: turnErrorText(err) });
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="muted">Loading…</p>;
  if (!session) {
    return (
      <div className="stack">
        {fatal && <p className="error" role="alert">{fatal}</p>}
        <StartPanel busy={starting} onStart={(m) => void start(m)} />
      </div>
    );
  }

  const ended = session.status === "ended";
  const qa = session.mode === "qa";
  const locked = controlsLocked(busy, chat.pending);
  const pending = ended ? null : chat.pending;
  const act = (action: string) => void send(session.id, { action });
  const resume = (value: unknown) => void send(session.id, { resume: value });

  return (
    <div className="stack tutor">
      <div className="report-head">
        <span className="badge">{qa ? "Free Q&A" : "Tutor"}</span>
        {ended && <span className="muted small">Ended session (read-only)</span>}
      </div>
      {fatal && <p className="error" role="alert">{fatal}</p>}
      {!qa && !ended && chat.today && <TodayCard today={chat.today} />}

      <div className="chat" role="log" aria-label="Conversation">
        {chat.messages.length === 0 && !chat.streaming && <p className="muted">No messages yet.</p>}
        {chat.messages.map((m, i) => (
          <Message key={i} m={m} />
        ))}
        {chat.streaming && (
          <div className="msg msg-assistant" aria-live="polite">
            <RichText text={chat.streaming} />
          </div>
        )}
        <div ref={endRef} />
      </div>

      {chat.error && <p className="error" role="alert">{chat.error}</p>}
      {chat.created && (
        <p className="card small">
          Created a roadmap:{" "}
          {onOpenRoadmap ? (
            <button type="button" className="link-button" onClick={() => onOpenRoadmap(chat.created!)}>
              {chat.created}
            </button>
          ) : (
            <strong>{chat.created}</strong>
          )}
        </p>
      )}

      {pending && !ended && <p className="muted small">Sending something else skips this card.</p>}
      {pending?.kind === "session_start" && <SessionStartCard value={pending as SessionStartInterrupt} busy={busy} onChoose={resume} />}
      {pending?.kind === "quiz" && <QuizCard key={(pending as QuizInterrupt).skill_id} value={pending as QuizInterrupt} busy={busy} onSubmit={resume} />}
      {pending?.kind === "approval" && <ApprovalCard key={(pending as ApprovalInterrupt).id} value={pending as ApprovalInterrupt} busy={busy} onRule={resume} />}

      {ended ? (
        <button
          type="button"
          onClick={() => {
            abort.current?.abort();
            dispatch({ type: "reset" });
            setSession(null);
            setFatal(null);
            onNewSession();
          }}
        >
          Start a new session
        </button>
      ) : (
        <div className="composer stack">
          <form
            className="composer-row"
            onSubmit={(e) => {
              e.preventDefault();
              const text = draft.trim();
              if (!text || locked) return;
              setDraft("");
              void send(session.id, { text }, text);
            }}
          >
            <label className="sr-only" htmlFor="chat-text">
              Message
            </label>
            <textarea id="chat-text" rows={2} value={draft} disabled={locked} placeholder="Ask or answer here" onChange={(e) => setDraft(e.target.value)} />
            <button type="submit" disabled={locked || !draft.trim()}>
              Send
            </button>
          </form>
          <div className="actions">
            {!qa && (
              <>
                <button type="button" className="secondary" disabled={locked} onClick={() => act("continue")}>
                  Continue
                </button>
                <button type="button" className="secondary" disabled={locked} onClick={() => act("quiz")}>
                  Quiz me
                </button>
                <button type="button" className="secondary" disabled={locked} onClick={() => act("recap")}>
                  Recap
                </button>
                <button type="button" className="secondary" disabled={locked} onClick={() => act("plan")}>
                  Plan
                </button>
              </>
            )}
            <button type="button" className="secondary" disabled={locked} onClick={() => setResearching((r) => !r)}>
              Teach me something new
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={() => void end()}>
              End session
            </button>
          </div>
          {researching && !locked && (
            <form
              className="composer-row"
              onSubmit={(e) => {
                e.preventDefault();
                const t = topic.trim();
                if (!t) return;
                setTopic("");
                void send(session.id, { action: "research", topic: t });
              }}
            >
              <label className="sr-only" htmlFor="research-topic">
                Topic
              </label>
              <input id="research-topic" value={topic} placeholder="What should I teach you?" onChange={(e) => setTopic(e.target.value)} />
              <button type="submit" disabled={!topic.trim()}>
                Research
              </button>
            </form>
          )}
        </div>
      )}

      <Timeline turns={chat.timeline} />
    </div>
  );
}
