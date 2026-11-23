import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => void data.delete(k),
    setItem: (k, v) => void data.set(k, String(v)),
  };
}

// Fresh modules per test so the in-memory fallback in storage.ts starts empty.
async function load() {
  vi.resetModules();
  const stream = await import("./stream");
  const api = await import("./api");
  const storage = await import("./storage");
  return { ...stream, ...api, ...storage };
}

/** A streamed response that delivers `chunks` one read at a time; `cancelled` flips when the reader cancels. */
function sseResponse(chunks: string[], opts: { hang?: boolean } = {}) {
  const state = { cancelled: false };
  const encoder = new TextEncoder();
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i++]));
      } else if (!opts.hang) {
        controller.close();
      } else {
        return new Promise<void>(() => {}); // keep the connection open until it is cancelled
      }
    },
    cancel() {
      state.cancelled = true;
    },
  });
  const res = new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  return { res, state };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("localStorage", memoryStorage());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamEvents", () => {
  it("streams progress with the access code", async () => {
    const { setCode, streamEvents } = await load();
    setCode("secret-1");
    // Two events, split mid-field and mid-line across reads, with CRLF endings, a comment and multi-line data.
    const { res } = sseResponse([
      ": keep-alive\r\n\r\nevent: prog",
      'ress\r\ndata: {"status": "ingesting",\r\ndata:  "jobs": {}}\r',
      "\n\r\nevent: done\nda",
      'ta: {"status": "ready"}\n\n',
    ]);
    fetchMock.mockResolvedValue(res);
    const events: [string, unknown][] = [];

    await streamEvents("/api/roadmaps/k8s/events", (name, data) => events.push([name, data]), new AbortController().signal);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/roadmaps/k8s/events");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Access-Code")).toBe("secret-1");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(events).toEqual([
      ["progress", { status: "ingesting", jobs: {} }],
      ["done", { status: "ready" }],
    ]);
  });

  it("401 clears code and throws Unauthorized", async () => {
    const { ApiError, Unauthorized, getCode, setCode, streamEvents } = await load();
    setCode("stale-code");
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: "Passcode required" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const err = await streamEvents("/api/roadmaps/k8s/events", () => {}, new AbortController().signal).catch(
      (e: unknown) => e,
    );

    expect(err).toBeInstanceOf(Unauthorized);
    expect(err).toBeInstanceOf(ApiError); // so the App's existing 401 handling shows the passcode screen
    expect((err as InstanceType<typeof ApiError>).status).toBe(401);
    expect(getCode()).toBeNull();
    expect(localStorage.length).toBe(0);
  });

  it("other HTTP errors throw ApiError with the server message and keep the code", async () => {
    const { ApiError, Unauthorized, getCode, setCode, streamEvents } = await load();
    setCode("good-code");
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: "No roadmap 'x'" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const err = await streamEvents("/api/roadmaps/x/events", () => {}, new AbortController().signal).catch(
      (e: unknown) => e,
    );

    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(Unauthorized);
    expect((err as Error).message).toBe("No roadmap 'x'");
    expect(getCode()).toBe("good-code");
  });

  it("aborting ends the stream quietly and releases the body", async () => {
    const { streamEvents } = await load();
    const { res, state } = sseResponse(['event: progress\ndata: {"status": "ingesting"}\n\n'], { hang: true });
    fetchMock.mockResolvedValue(res);
    const controller = new AbortController();
    const events: string[] = [];

    const done = streamEvents(
      "/api/roadmaps/k8s/events",
      (name) => {
        events.push(name);
        controller.abort();
      },
      controller.signal,
    );

    await expect(done).resolves.toBeUndefined();
    expect(events).toEqual(["progress"]);
    expect(state.cancelled).toBe(true);
  });

  it("ignores events without data and keeps non-JSON data as text", async () => {
    const { streamEvents } = await load();
    const { res } = sseResponse(["event: ping\n\n", "data: plain text\n\n"]);
    fetchMock.mockResolvedValue(res);
    const events: [string, unknown][] = [];

    await streamEvents("/e", (name, data) => events.push([name, data]), new AbortController().signal);

    expect(events).toEqual([["message", "plain text"]]);
  });
});

describe("followEvents", () => {
  function errorResponse(status: number) {
    return new Response(JSON.stringify({ error: `HTTP ${status} from server` }), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  it("stops after N failed reconnects, with growing delays", async () => {
    const { ApiError, followEvents } = await load();
    fetchMock.mockImplementation(async () => errorResponse(500));
    const delays: number[] = [];
    const retrying: number[] = [];

    const err = await followEvents("/e", () => {}, new AbortController().signal, {
      retries: 3,
      delayMs: 1500,
      sleep: async (ms) => void delays.push(ms),
      onRetry: (attempt) => retrying.push(attempt),
    }).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as Error).message).toBe("HTTP 500 from server");
    expect(fetchMock).toHaveBeenCalledTimes(4); // the first try plus 3 reconnects
    expect(delays).toEqual([1500, 3000, 6000]);
    expect(retrying).toEqual([1, 2, 3]);
  });

  it("a 401 is not retried", async () => {
    const { Unauthorized, followEvents } = await load();
    fetchMock.mockImplementation(async () => errorResponse(401));

    const err = await followEvents("/e", () => {}, new AbortController().signal, { sleep: async () => {} }).catch(
      (e: unknown) => e,
    );

    expect(err).toBeInstanceOf(Unauthorized);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reconnects after an early end and stops at done", async () => {
    const { followEvents } = await load();
    fetchMock
      .mockResolvedValueOnce(sseResponse(['event: progress\ndata: {"n": 1}\n\n']).res)
      .mockResolvedValueOnce(errorResponse(503))
      .mockResolvedValueOnce(sseResponse(['event: done\ndata: {"status": "ready"}\n\n']).res);
    const events: string[] = [];

    await followEvents("/e", (name) => events.push(name), new AbortController().signal, {
      retries: 1,
      sleep: async () => {},
    });

    // The early end received events, so it did not count as a failure; the one 503 stayed within 1 retry.
    expect(events).toEqual(["progress", "done"]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("abort during the backoff ends quietly and clears the timer", async () => {
    const { followEvents } = await load();
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementation(async () => errorResponse(500));
      const controller = new AbortController();
      const done = followEvents("/e", () => {}, controller.signal, { delayMs: 1500 });
      await vi.waitFor(() => expect(vi.getTimerCount()).toBe(1));
      controller.abort();
      await expect(done).resolves.toBeUndefined();
      expect(vi.getTimerCount()).toBe(0);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
