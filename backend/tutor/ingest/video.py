"""Video pipeline: routed, throttled transcripts and the Markdown document the chunker consumes.

Shared state (the captions throttle and the block circuit breaker) is process-wide because ingest
workers are threads in one process; both take an injectable clock/sleep so tests never wait."""
from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from backend.core import llm
from backend.tutor.ingest.net import CaptionsBlocked, Chapter, Cue, NoCaptions, VideoMeta, YouTube
from backend.tutor.settings import Settings

CAPTIONS_GAP_S = 3.0
BREAKER_S = 30 * 60
BACKOFF_S = (2.0, 4.0)  # two retries on 429


class CircuitBreaker:
    def __init__(self, duration_s: float = BREAKER_S, clock: Callable[[], float] = time.monotonic):
        self._duration, self._clock = duration_s, clock
        self._until = 0.0
        self._lock = threading.Lock()

    def trip(self) -> None:
        with self._lock:
            self._until = self._clock() + self._duration

    def is_open(self) -> bool:
        with self._lock:
            return self._clock() < self._until

    def reset(self) -> None:
        with self._lock:
            self._until = 0.0


class Throttle:
    """Process-wide minimum gap between calls: each caller reserves the next slot, then sleeps."""

    def __init__(self, gap_s: float = CAPTIONS_GAP_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._gap, self._clock, self._sleep = gap_s, clock, sleep
        self._next = float("-inf")
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + self._gap
        if slot > now:
            self._sleep(slot - now)

    def reset(self) -> None:
        with self._lock:
            self._next = float("-inf")


CAPTIONS_BREAKER = CircuitBreaker()
CAPTIONS_THROTTLE = Throttle()

_GEMINI_LINE = re.compile(r"^\s*\[?((?:\d{1,2}:)?\d{1,2}:\d{2})\]?\s*(.+?)\s*$")
_GEMINI_PROMPT = ("Transcribe the spoken content of this video. Output one line per utterance in the "
                  "exact form `[mm:ss] text` (use `[h:mm:ss]` past one hour). No other commentary.")


def _stamp_seconds(stamp: str) -> float:
    secs = 0
    for part in stamp.split(":"):
        secs = secs * 60 + int(part)
    return float(secs)


def parse_gemini_transcript(text: str) -> list[Cue]:
    rows = []
    for line in text.splitlines():
        m = _GEMINI_LINE.match(line)
        if m:
            rows.append((_stamp_seconds(m.group(1)), m.group(2)))
    return [Cue(s, (rows[i + 1][0] - s) if i + 1 < len(rows) else 0.0, t)
            for i, (s, t) in enumerate(rows)]


def ytdlp_subtitles(video_id: str, browser: str | None) -> list[Cue]:
    """Subtitle track only (never the media), authenticated with the browser's cookies."""
    import yt_dlp

    with tempfile.TemporaryDirectory() as tmp:
        opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True,
                "writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": ["en.*", "en"],
                "subtitlesformat": "json3", "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s")}
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        files = sorted(Path(tmp).glob("*.json3"))
        if not files:
            raise NoCaptions("yt-dlp found no subtitle track")
        data = json.loads(files[0].read_text(encoding="utf-8"))
    cues = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
        if text:
            cues.append(Cue(ev.get("tStartMs", 0) / 1000, ev.get("dDurationMs", 0) / 1000, text))
    if not cues:
        raise NoCaptions("yt-dlp subtitle track was empty")
    return cues


def _is_429(e: Exception) -> bool:
    msg = str(e).lower()
    return "429" in msg or "too many requests" in msg


class TranscriptRouter:
    def __init__(self, settings: Settings, youtube: YouTube, gemini_fn=llm.gemini_generate, *,
                 ytdlp_fn=ytdlp_subtitles, breaker: CircuitBreaker | None = None,
                 throttle: Throttle | None = None, sleep: Callable[[float], None] = time.sleep):
        self._settings, self._yt = settings, youtube
        self._gemini, self._ytdlp = gemini_fn, ytdlp_fn
        self._breaker = breaker or CAPTIONS_BREAKER
        self._throttle = throttle or CAPTIONS_THROTTLE
        self._sleep = sleep

    def get(self, video_id: str, meta: VideoMeta | None) -> tuple[str | None, list[Cue], str | None]:
        """Returns (route, cues, reason); (None, [], last_reason) when every route fails."""
        reason = "captions_blocked"
        for route in self._settings.transcript_routes:
            try:
                cues, reason = self._try(route, video_id)
            except Exception:  # a route must never take the job down; record and try the next
                cues, reason = [], f"{route.split('_')[0]}_error"
            if cues:
                return route, cues, None
        return None, [], reason

    def _try(self, route: str, video_id: str) -> tuple[list[Cue], str | None]:
        if route == "captions":
            return self._captions(video_id)
        if route == "ytdlp_cookies":
            try:
                return self._ytdlp(video_id, self._settings.ytdlp_cookies_browser), None
            except NoCaptions:
                return [], "no_captions"
        if route == "gemini":
            try:
                text = self._gemini(
                    {"parts": [{"file_data": {"file_uri": f"https://www.youtube.com/watch?v={video_id}"}},
                               {"text": _GEMINI_PROMPT}]},
                    system=None, video=True, span_name="transcript_gemini")
            except llm.LLMUnavailable:
                return [], "gemini_quota"
            cues = parse_gemini_transcript(text)
            return cues, (None if cues else "gemini_empty")
        return [], f"unknown_route_{route}"

    def _captions(self, video_id: str) -> tuple[list[Cue], str | None]:
        if self._breaker.is_open():
            return [], "captions_blocked"
        for attempt in range(len(BACKOFF_S) + 1):
            self._throttle.wait()
            try:
                return self._yt.captions(video_id), None
            except NoCaptions:
                return [], "no_captions"
            except CaptionsBlocked as e:
                if _is_429(e) and attempt < len(BACKOFF_S):
                    self._sleep(BACKOFF_S[attempt])
                    continue
                self._breaker.trip()
                return [], "captions_blocked"
        return [], "captions_blocked"  # unreachable


def _stamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def video_document(meta: VideoMeta, route: str | None, cues: list[Cue], seg_route: str,
                   chapters: list[Chapter]) -> str:
    """Markdown for the chunker: `# {title}`, then one `## [mm:ss] {chapter}` heading per chapter
    (`[h:mm:ss]` from one hour on; Task 9 parses the bracket back into start_s) followed by that
    chapter's transcript text. With no transcript the description is the body."""
    lines = [f"# {meta.title}", ""]
    if not cues:
        if meta.description.strip():
            lines += [meta.description.strip(), ""]
        return "\n".join(lines).rstrip() + "\n"
    chapters = sorted(chapters, key=lambda c: c.start_s) or [Chapter(0.0, meta.title)]
    ordered = sorted(cues, key=lambda c: c.start)
    for i, ch in enumerate(chapters):
        lo = -1.0 if i == 0 else ch.start_s
        hi = chapters[i + 1].start_s if i + 1 < len(chapters) else float("inf")
        body = " ".join(c.text.strip() for c in ordered if lo <= c.start < hi and c.text.strip())
        lines += [f"## [{_stamp(ch.start_s)}] {ch.title}", "", body, ""]
    return "\n".join(lines).rstrip() + "\n"
