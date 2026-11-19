import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

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
  const api = await import("./api");
  const storage = await import("./storage");
  return { ...api, ...storage };
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

function sentHeaders(call = 0): Headers {
  const init = fetchMock.mock.calls[call][1] as RequestInit;
  return new Headers(init.headers);
}

describe("api", () => {
  it("sends access code when stored", async () => {
    const { listRoadmaps, setCode } = await load();
    setCode("secret-1");
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await listRoadmaps();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/roadmaps");
    expect(sentHeaders().get("X-Access-Code")).toBe("secret-1");
  });

  it("omits access code when none is stored", async () => {
    const { listRoadmaps } = await load();
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await listRoadmaps();

    expect(sentHeaders().has("X-Access-Code")).toBe(false);
  });

  it("throws ApiError with server message on 409", async () => {
    const { ApiError, createRoadmap } = await load();
    fetchMock.mockResolvedValue(
      jsonResponse(409, { error: "A roadmap with slug 'my-roadmap' already exists" }),
    );

    const err = await createRoadmap({ slug: "my-roadmap", title: "My Roadmap", source_text: "x" }).catch(
      (e: unknown) => e,
    );

    expect(err).toBeInstanceOf(ApiError);
    expect((err as InstanceType<typeof ApiError>).status).toBe(409);
    expect((err as Error).message).toBe("A roadmap with slug 'my-roadmap' already exists");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      slug: "my-roadmap",
      title: "My Roadmap",
      source_text: "x",
    });
  });

  it("gives a readable message for FastAPI's detail-shaped 422", async () => {
    const { ApiError, createRoadmap } = await load();
    fetchMock.mockResolvedValue(
      jsonResponse(422, { detail: [{ loc: ["body", "title"], msg: "Field required", type: "missing" }] }),
    );

    const err = await createRoadmap({ slug: "a", title: "", source_text: "" }).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as Error).message).toBe("Some required fields are missing or invalid.");
  });

  it("works when localStorage throws", async () => {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("denied", "SecurityError");
      },
    });
    const { getCode, listRoadmaps, setCode } = await load();

    expect(getCode()).toBeNull();
    setCode("memory-code");
    expect(getCode()).toBe("memory-code");

    fetchMock.mockResolvedValue(jsonResponse(200, []));
    await expect(listRoadmaps()).resolves.toEqual([]);
    expect(sentHeaders().get("X-Access-Code")).toBe("memory-code");
  });
});
