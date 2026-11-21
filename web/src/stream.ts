// Server-sent events over `fetch`: EventSource cannot send the X-Access-Code header.
import { ApiError, Unauthorized, errorMessage } from "./api";
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
