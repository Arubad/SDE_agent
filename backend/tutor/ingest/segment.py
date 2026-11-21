"""Chapter detection for a video, in order: creator timestamps (description or structured chapters),
timestamps in top comments, a topic-shift split of the transcript (local embeddings, no LLM),
and finally the whole video as one chapter."""
from __future__ import annotations

import math
import re

from backend.tutor.ingest.net import Chapter, Cue, VideoMeta

_TS_LINE = re.compile(
    r"^\s*[\(\[]?\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s*[\)\]]?\s*[-–—:|.]?\s*(.*?)\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
TITLE_MAX = 60


def _to_seconds(stamp: str) -> int:
    secs = 0
    for part in stamp.split(":"):
        secs = secs * 60 + int(part)
    return secs


def parse_timestamps(text: str, duration_s: float | None) -> list[Chapter]:
    """Chapters from lines like `0:00 Intro`, `(12:30) Services`, `1:02:03 - Helm`. Valid only with
    >= 3 entries, the first at 0:00, strictly increasing and (when known) within the duration."""
    found: list[Chapter] = []
    for line in (text or "").splitlines():
        m = _TS_LINE.match(line)
        if m and m.group(2):
            found.append(Chapter(float(_to_seconds(m.group(1))), m.group(2)))
    if len(found) < 3 or found[0].start_s != 0:
        return []
    if any(b.start_s <= a.start_s for a, b in zip(found, found[1:])):
        return []
    if duration_s and found[-1].start_s > duration_s:
        return []
    return found


def _title(text: str) -> str:
    first = _SENTENCE_END.split(text.strip(), maxsplit=1)[0].strip()
    return first[:TITLE_MAX].rstrip()


def topic_shift_split(cues: list[Cue], embedder, *, window_s: float = 60, min_seg_s: float = 120,
                      max_segments: int = 30) -> list[Chapter]:
    """Group cues into `window_s` windows, embed them, and break where the cosine similarity of
    adjacent windows is below mean - 1.0*std, keeping segments >= `min_seg_s` long. Candidate breaks
    are taken weakest-similarity first; at most `max_segments` segments result."""
    if not cues:
        return []
    cues = sorted(cues, key=lambda c: c.start)
    windows: dict[int, list[Cue]] = {}
    for c in cues:
        windows.setdefault(int(c.start // window_s), []).append(c)
    keys = sorted(windows)
    starts = [windows[k][0].start for k in keys]
    texts = [" ".join(c.text for c in windows[k]) for k in keys]
    first_start = starts[0]
    if len(keys) >= 3:
        vecs = embedder.embed(texts)
        sims = [sum(a * b for a, b in zip(u, v)) for u, v in zip(vecs, vecs[1:])]  # unit vectors
        mean = sum(sims) / len(sims)
        std = math.sqrt(sum((s - mean) ** 2 for s in sims) / len(sims))
        threshold = mean - 1.0 * std
        accepted = [first_start]
        for sim, i in sorted((s, i + 1) for i, s in enumerate(sims) if s < threshold):
            if len(accepted) >= max_segments:
                break
            if all(abs(starts[i] - a) >= min_seg_s for a in accepted):
                accepted.append(starts[i])
        starts_sorted = sorted(accepted)
    else:
        starts_sorted = [first_start]
    out = []
    for i, s in enumerate(starts_sorted):
        end = starts_sorted[i + 1] if i + 1 < len(starts_sorted) else math.inf
        body = " ".join(c.text for c in cues if s <= c.start < end)
        out.append(Chapter(s, _title(body) or f"Part {i + 1}"))
    return out


def choose_segments(meta: VideoMeta, comments: list[str], cues: list[Cue],
                    embedder) -> tuple[str, list[Chapter]]:
    """Returns (route, chapters); route is creator, comments, transcript_split or whole."""
    creator = list(meta.chapters) or parse_timestamps(meta.description, meta.duration_s)
    if creator:
        return "creator", creator
    for comment in comments:
        found = parse_timestamps(comment, meta.duration_s)
        if found:
            return "comments", found
    if cues:
        split = topic_shift_split(cues, embedder)
        if len(split) >= 2:
            return "transcript_split", split
    return "whole", [Chapter(0.0, meta.title)]
