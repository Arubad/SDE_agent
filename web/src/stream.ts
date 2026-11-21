// Server-sent events over `fetch`: EventSource cannot send the X-Access-Code header.
import { ApiError, errorMessage, isAuthError, Unauthorized } from "./api";
import { clearCode, getCode } from "./storage";

export type EventHandler = (name: string, data: unknown) => void;

function decode(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

/** Incremental SSE parser (the WHATWG event-stream format): feed it text as it arrives, in any split. */
export class SseParser {
  private buffer = "";
  private event = "";
  private data: string[] = [];

  constructor(private readonly onEvent: EventHandler) {}

  push(text: string): void {
    this.buffer += text;
    // A trailing "\r" may be the first half of a "\r\n" split across reads: wait for the next chunk.
    const lines = this.buffer.split(/\r\n|\r(?!$)|\n/);
    this.buffer = lines.pop() ?? "";
    for (const line of lines) this.line(line);
  }

  private line(line: string): void {
    if (line === "") {
      if (this.data.length > 0) this.onEvent(this.event || "message", decode(this.data.join("\n")));
      this.event = "";
      this.data = [];
      return;
    }
    if (line.startsWith(":")) return; // a comment (keep-alive)
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
    // `id` and `retry` are not used by this API.
  }
}

function isAbort(err: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (err instanceof DOMException && err.name === "AbortError");
}

/**
 * Stream `path`'s events into `onEvent(name, data)` (data is parsed JSON, or the raw text) until the server
 * closes the stream. Resolves quietly when `signal` aborts; the response body is always released.
 * A 401 clears the stored code and throws `Unauthorized`; any other HTTP error throws `ApiError`.
 */
export async function streamEvents(path: string, onEvent: EventHandler, signal: AbortSignal): Promise<void> {
  const headers = new Headers({ Accept: "text/event-stream" });
  const code = getCode();
  if (code) headers.set("X-Access-Code", code);

  let res: Response;
  try {
    res = await fetch(path, { headers, signal, cache: "no-store" });
  } catch (err) {
    if (isAbort(err, signal)) return;
    throw err;
  }
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => null);
    if (res.status === 401) {
      clearCode();
      throw new Unauthorized(errorMessage(res.status, body));
    }
    throw new ApiError(res.status, errorMessage(res.status, body));
  }
  if (!res.body) throw new ApiError(res.status, "The server sent no event stream.");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser(onEvent);
  let finished = false;
  const cancel = () => void reader.cancel().catch(() => {});
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) {
        finished = true; // an unterminated last event is dropped, as EventSource does
        return;
      }
      parser.push(decoder.decode(value, { stream: true }));
    }
  } catch (err) {
    if (isAbort(err, signal)) return;
    throw err;
  } finally {
    signal.removeEventListener("abort", cancel);
    if (!finished) cancel(); // aborted, or a handler threw: never leave the connection open
    reader.releaseLock();
  }
}

/** Waits `ms`, or less if `signal` aborts; the timer is always cleared. */
export function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

export interface FollowOptions {
  /** Reconnects allowed after consecutive failures before giving up (default 3). */
  retries?: number;
  /** The first backoff; it doubles per consecutive failure (default 1500 ms: 1.5 s, 3 s, 6 s). */
  delayMs?: number;
  /** Called before each reconnect that follows a failure, with the failure count. */
  onRetry?: (failures: number, err: unknown) => void;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

/**
 * `streamEvents` until a `done` event arrives, reconnecting when the stream fails or ends early.
 * A stream that delivered events and then ended is reopened after the base delay and resets the failure count;
 * an error (or an end with no events) is a failure, retried with doubling backoff up to `retries` times, then
 * thrown. A 401/429 is thrown at once. Resolves on `done` or when `signal` aborts.
 */
export async function followEvents(
  path: string,
  onEvent: EventHandler,
  signal: AbortSignal,
  { retries = 3, delayMs = 1500, onRetry, sleep = abortableSleep }: FollowOptions = {},
): Promise<void> {
  let failures = 0;
  while (!signal.aborted) {
    let settled = false;
    let received = false;
    let failure: unknown = null;
    try {
      await streamEvents(
        path,
        (name, data) => {
          received = true;
          if (name === "done") settled = true;
          onEvent(name, data);
        },
        signal,
      );
      if (!received) failure = new Error("The progress stream closed without sending anything.");
    } catch (err) {
      if (isAuthError(err)) throw err;
      failure = err;
    }
    if (settled || signal.aborted) return;
    if (failure === null) {
      failures = 0;
      await sleep(delayMs, signal);
      continue;
    }
    failures = received ? 1 : failures + 1; // a stream that worked before failing starts the count afresh
    if (failures > retries) throw failure;
    onRetry?.(failures, failure);
    await sleep(delayMs * 2 ** (failures - 1), signal);
  }
}
