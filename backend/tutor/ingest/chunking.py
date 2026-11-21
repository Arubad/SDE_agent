"""Hierarchical semantic chunking: heading-aware parent sections (stored in Postgres) and small child
chunks (indexed in Qdrant). Code fences and tables are atomic. Near-duplicate dropping across documents
is the caller's job (`index.index_document`)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from backend.rag.documents import stable_id

SEP = " › "
MIN_FRAGMENT = 40
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_VIDEO = re.compile(r"^\[((?:\d+:)?\d{1,2}:\d{2})\]\s*(.*)$")
_SENT = re.compile(r"(?<=[.!?])\s+")


def approx_tokens(text: str) -> int:
    return math.ceil(len(text.split()) * 1.3)


@dataclass
class ParentSection:
    id: str
    path: list[str]
    text: str
    tokens: int
    start_s: float | None
    end_s: float | None
    ord: int


@dataclass
class ChildChunk:
    id: str
    parent_id: str
    text: str
    embed_text: str
    tokens: int
    start_s: float | None
    end_s: float | None
    path: list[str] | None = None
    ord: int = 0


@dataclass
class _Unit:
    text: str
    atomic: bool
    para_start: bool  # joined to the previous unit with a blank line, else a space


def _join(units: list[_Unit]) -> str:
    out = ""
    for i, u in enumerate(units):
        out += u.text if i == 0 else ("\n\n" if u.para_start else " ") + u.text
    return out


def _stamp_s(stamp: str) -> float:
    secs = 0
    for part in stamp.split(":"):
        secs = secs * 60 + int(part)
    return float(secs)


def _split_sections(markdown: str, doc_title: str):
    """-> [(path, start_s | None, body_lines)]. Headings inside code fences are ignored; an H1 equal to
    the document title does not repeat in the breadcrumb."""
    sections: list[list] = []
    stack: list[tuple[int, str, float | None]] = []
    cur = [[doc_title], None, []]
    in_fence, fence_ch = False, ""
    for line in markdown.splitlines():
        m = _FENCE.match(line)
        if m:
            ch = m.group(1)[0]
            if not in_fence:
                in_fence, fence_ch = True, ch
            elif ch == fence_ch:
                in_fence = False
        h = None if in_fence or m else _HEADING.match(line)
        if not h:
            cur[2].append(line)
            continue
        sections.append(cur)
        level, title = len(h.group(1)), h.group(2)
        start = None
        v = _VIDEO.match(title)
        if v:
            start, title = _stamp_s(v.group(1)), v.group(2) or v.group(1)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if not (not stack and title == doc_title):
            stack.append((level, title, start))
        inherited = next((s for _, _, s in reversed(stack) if s is not None), None)
        cur = [[doc_title] + [t for _, t, _ in stack], inherited, []]
    sections.append(cur)
    return [(p, s, b) for p, s, b in sections if "".join(b).strip() or s is not None]


def _blocks(lines: list[str]) -> list[tuple[str, bool]]:
    """Blank-line separated blocks as (text, atomic); fenced code and tables are atomic."""
    out: list[tuple[str, bool]] = []
    buf: list[str] = []
    i = 0

    def flush():
        if buf and "".join(buf).strip():
            out.append(("\n".join(buf).strip("\n"), False))
        buf.clear()

    while i < len(lines):
        line = lines[i]
        m = _FENCE.match(line)
        if m:
            flush()
            ch, n = m.group(1)[0], len(m.group(1))
            j = i + 1
            while j < len(lines) and not (re.match(rf"^\s*{re.escape(ch)}{{{n},}}\s*$", lines[j])):
                j += 1
            out.append(("\n".join(lines[i:j + 1]), True))
            i = j + 1
        elif line.lstrip().startswith("|"):
            flush()
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.append(("\n".join(lines[i:j]), True))
            i = j
        elif not line.strip():
            flush()
            i += 1
        else:
            buf.append(line)
            i += 1
    flush()
    return out


def _units(lines: list[str], cap: int) -> list[_Unit]:
    """Sentences and atomic blocks. Any non-atomic run longer than `cap` tokens is cut by words."""
    units: list[_Unit] = []
    for text, atomic in _blocks(lines):
        if atomic:
            units.append(_Unit(text, True, True))
            continue
        first = True
        for sent in _SENT.split(" ".join(text.split("\n"))):
            sent = sent.strip()
            if not sent:
                continue
            step = max(1, int(cap / 1.3) - 1)
            words = sent.split()
            for k in range(0, len(words), step):
                units.append(_Unit(" ".join(words[k:k + step]), False, first))
                first = False
    return units


def _pack(units: list[_Unit], vecs: list[list[float]], lo: int, hi: int) -> list[list[int]]:
    """Group unit indices into windows of <= hi tokens (an oversized atomic unit stands alone). Once a
    window holds >= lo tokens the cut goes where adjacent units are least similar."""
    toks = [approx_tokens(u.text) for u in units]
    groups: list[list[int]] = []
    i, n = 0, len(units)
    while i < n:
        total, k, reach_lo = 0, i, None
        while k < n and (k == i or total + toks[k] <= hi):
            total += toks[k]
            k += 1
            if reach_lo is None and total >= lo:
                reach_lo = k
        if k >= n:
            groups.append(list(range(i, n)))
            break
        # candidate cuts: after unit c-1, for c in [reach_lo or k, k]
        cands = range(reach_lo or k, k + 1)
        cut = min(cands, key=lambda c: (sum(a * b for a, b in zip(vecs[c - 1], vecs[c])), -c))
        groups.append(list(range(i, cut)))
        i = cut
    return groups


def _vecs(units: list[_Unit], embedder) -> list[list[float]]:
    return embedder.embed([u.text for u in units]) if len(units) > 1 else [[0.0]] * len(units)


def _merge_small(groups: list[list[int]], units: list[_Unit], hi: int) -> list[list[int]]:
    """Merge < MIN_FRAGMENT-token groups into a neighbour when the result still fits `hi`."""
    def tok(g):
        return approx_tokens(_join([units[i] for i in g]))

    changed = True
    while changed and len(groups) > 1:
        changed = False
        for idx, g in enumerate(groups):
            if tok(g) >= MIN_FRAGMENT:
                continue
            for nb in (idx - 1, idx + 1):
                if 0 <= nb < len(groups) and tok(groups[nb]) + tok(g) <= hi:
                    lo_i, hi_i = sorted((idx, nb))
                    groups[lo_i:hi_i + 1] = [groups[lo_i] + groups[hi_i]]
                    changed = True
                    break
            if changed:
                break
    return groups


def chunk_markdown(markdown: str, *, doc_title: str, doc_key: str, embedder, child_min: int = 200,
                   child_max: int = 350, parent_max: int = 1500
                   ) -> tuple[list[ParentSection], list[ChildChunk]]:
    sections = _split_sections(markdown, doc_title)
    chapter_starts = sorted({s for _, s, _ in sections if s is not None})
    parents: list[ParentSection] = []
    children: list[ChildChunk] = []
    for path, start_s, body in sections:
        end_s = None
        if start_s is not None:
            end_s = next((c for c in chapter_starts if c > start_s), None)
        units = _units(body, child_max)
        if not units:
            continue
        vecs = _vecs(units, embedder)
        total = approx_tokens(_join(units))
        if total <= parent_max:
            parts = [list(range(len(units)))]
        else:
            parts = _pack(units, vecs, int(parent_max * 0.6), parent_max)
        for pn, part in enumerate(parts, 1):
            ppath = list(path)
            if len(parts) > 1:
                ppath[-1] = f"{ppath[-1]} (part {pn})"
            punits = [units[i] for i in part]
            ptext = _join(punits)
            pid = stable_id("parent", doc_key, SEP.join(ppath), str(len(parents)))
            parents.append(ParentSection(pid, ppath, ptext, approx_tokens(ptext), start_s, end_s, len(parents)))
            pvecs = [vecs[i] for i in part]
            groups = _merge_small(_pack(punits, pvecs, child_min, child_max), punits, child_max)
            for g in groups:
                text = _join([punits[i] for i in g])
                cord = len(children)
                children.append(ChildChunk(
                    id=stable_id(doc_key, SEP.join(ppath), str(cord)), parent_id=pid, text=text,
                    embed_text=SEP.join(ppath) + "\n" + text, tokens=approx_tokens(text),
                    start_s=start_s, end_s=end_s, path=ppath, ord=cord))
    return parents, children
