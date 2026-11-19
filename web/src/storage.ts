// Passcode storage. localStorage can be missing or throw (private mode, blocked site data),
// so every access is guarded and an in-memory copy keeps the session working.
const KEY = "roadmap-tutor.access-code";

let memory: string | null = null;

export function getCode(): string | null {
  try {
    const stored = localStorage.getItem(KEY);
    if (stored !== null) return stored;
  } catch {
    // fall through to the in-memory value
  }
  return memory;
}

export function setCode(code: string): void {
  memory = code;
  try {
    localStorage.setItem(KEY, code);
  } catch {
    // in-memory value is enough for this session
  }
}

export function clearCode(): void {
  memory = null;
  try {
    localStorage.removeItem(KEY);
  } catch {
    // nothing stored there to clear
  }
}
