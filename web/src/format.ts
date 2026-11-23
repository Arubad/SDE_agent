// Display helpers for web-derived strings. Everything here returns plain text for React to render as text.

const REASONS: Record<string, string> = {
  not_found: "Page not found (HTTP 404)",
  gone: "Page removed (HTTP 410)",
  robots_denied: "The site's robots.txt does not allow crawling it",
  login_wall: "Behind a login",
  paywall: "Behind a paywall",
  bot_protection: "Blocked by the site's bot protection",
  needs_browser: "Needs a browser to render; browser support arrives with Phase 3",
  too_large: "Too large to ingest",
  unsupported_type: "Unsupported file type",
  private_address: "Points at a private network address",
  dns_failure: "The site's address could not be found",
  no_url: "No URL chosen",
  no_text: "No readable text on the page",
  no_content: "Nothing could be ingested",
  error: "Could not be fetched",
  index_error: "Could not be indexed",
  captions_blocked: "YouTube blocked the captions request",
  no_captions: "The video has no captions",
  video_unavailable: "The video is unavailable",
  empty_playlist: "The playlist is empty",
  pdf_unreadable: "The PDF could not be read",
};

/** A human-readable reason for a reason code; unknown codes are shown as they are. */
export function reasonText(code: string | null | undefined): string {
  if (!code) return "";
  return REASONS[code] ?? code;
}

/** The URL of a skipped item, or a placeholder when the report has none (a dead job whose document is gone). */
export function skippedUrlText(url: string | null | undefined): string {
  return url || "(no URL recorded)";
}

/** `url` if it is an absolute http(s) URL, else undefined (never a javascript:, data: or relative href). */
export function safeHref(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? url : undefined;
  } catch {
    return undefined;
  }
}

export function hostOf(url: string | null | undefined): string {
  if (!url) return "";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

const STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  parsed: "Ready for review",
  ingesting: "Ingesting",
  finalizing: "Finishing",
  ready: "Ready",
  ready_with_gaps: "Ready, with gaps",
  failed: "Failed",
  linked_only: "Linked for you, not ingested",
};

export function statusText(status: string): string {
  return STATUS_LABELS[status] ?? status;
}
