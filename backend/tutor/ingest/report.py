"""Final per-resource and roadmap status, and the honest ingestion report.

Failures come from two places: non-ok `Document` rows (a fetch outcome, `no_text`, `pdf_unreadable`, ...) and
dead jobs that left no document behind (a handler that kept crashing: reason `error`; a dead `chunk_embed`:
reason `index_error`). A video with no transcript is an ok, indexed document (title and description) whose
`reason` names the gap; it makes its resource `partial` but is reported in the transcript counts, not in
`skipped`."""
from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from backend.tutor.db import session_scope
from backend.tutor.models import Document, Job, Resource, Roadmap

log = logging.getLogger(__name__)

SEGMENTS = (("creator", "creator chapters"), ("comments", "comment timestamps"),
            ("transcript_split", "transcript-split"), ("whole", "whole"))
ROUTES = (("captions", "captions"), ("ytdlp_cookies", "yt-dlp"), ("gemini", "Gemini"))
DEAD_REASON = {"chunk_embed": "index_error"}


def _job_url(job: Job, resources: dict) -> str | None:
    p = job.payload or {}
    if p.get("url"):
        return p["url"]
    if p.get("video_id"):
        return f"https://www.youtube.com/watch?v={p['video_id']}"
    if p.get("list_id"):
        return f"https://www.youtube.com/playlist?list={p['list_id']}"
    if p.get("repo"):
        return f"https://github.com/{p['repo']}"
    res = resources.get(p.get("resource_id"))
    return res.url if res is not None else None


def _gather(session: Session, roadmap_id):
    """-> (resources, docs by resource id, failures by resource id as [(url, reason)])."""
    resources = list(session.scalars(select(Resource).where(Resource.roadmap_id == roadmap_id)))
    by_id = {str(r.id): r for r in resources}
    docs = list(session.scalars(select(Document).where(Document.roadmap_id == roadmap_id)))
    doc_by_id = {str(d.id): d for d in docs}
    docs_by_res: dict = {}
    for d in docs:
        docs_by_res.setdefault(d.resource_id, []).append(d)
    failures: dict = {}
    for d in docs:
        if d.status != "ok":
            failures.setdefault(d.resource_id, []).append((d.url, d.reason or d.status))
    doc_urls = {d.url for d in docs}
    dead = session.scalars(select(Job).where(Job.roadmap_id == roadmap_id, Job.status == "dead",
                                             Job.type != "recheck").order_by(Job.id))
    for job in dead:
        res = by_id.get((job.payload or {}).get("resource_id"))
        if job.type == "chunk_embed":
            doc = doc_by_id.get((job.payload or {}).get("document_id"))
            url = doc.url if doc is not None else None
        else:
            url = _job_url(job, by_id)
            if url in doc_urls:  # already reported by its document (e.g. a fetch `error` after the retries)
                continue
        if res is not None:
            failures.setdefault(res.id, []).append((url, DEAD_REASON.get(job.type, "error")))
    return resources, docs_by_res, failures


def _chunks(doc: Document) -> int:
    return int((doc.meta or {}).get("chunks") or 0)


def _dominant(reasons: list[str]) -> str | None:
    return Counter(reasons).most_common(1)[0][0] if reasons else None


def _finalize(session: Session, roadmap_id) -> None:
    roadmap = session.get(Roadmap, roadmap_id, with_for_update=True)
    resources, docs_by_res, failures = _gather(session, roadmap_id)
    crawlable = []
    for r in resources:
        if r.status == "linked_only" or not r.url:
            continue  # kept as approved: linked_only, or skipped/no_url
        crawlable.append(r)
        docs = docs_by_res.get(r.id, [])
        ok = [d for d in docs if d.status == "ok"]
        gaps = [reason for _, reason in failures.get(r.id, [])] + [d.reason for d in ok if d.reason]
        if any(_chunks(d) for d in ok):
            r.status, r.reason = ("partial", _dominant(gaps)) if gaps else ("done", None)
        else:
            r.status, r.reason = "skipped", _dominant(gaps) or "no_content"
        r.stats = {**(r.stats or {}), "pages": sum(1 for d in ok if d.kind != "video"),
                   "videos": sum(1 for d in ok if d.kind == "video"), "chunks": sum(_chunks(d) for d in ok)}
    if crawlable and all(r.status == "done" for r in crawlable):
        roadmap.status = "ready"
    elif any(r.status in ("done", "partial") for r in crawlable):
        roadmap.status = "ready_with_gaps"
    else:
        roadmap.status = "failed"
    session.flush()


def finalize_roadmap(engine: Engine, roadmap_id) -> None:
    """Set every resource's final status, then the roadmap's: `ready` (every crawlable resource done),
    `ready_with_gaps` (at least one done or partial) or `failed`. One transaction; idempotent.

    Never raises: if finalizing fails, that transaction is rolled back (nothing half-written) and the roadmap
    is set to `failed`, so it cannot stay stuck in `finalizing`."""
    try:
        with session_scope(engine) as s:
            _finalize(s, roadmap_id)
    except Exception:
        log.exception("finalizing roadmap %s failed; marking it failed", roadmap_id)
        try:
            with session_scope(engine) as s:
                s.execute(update(Roadmap).where(Roadmap.id == roadmap_id).values(status="failed"))
        except Exception:
            log.exception("could not mark roadmap %s failed", roadmap_id)


def video_summary(total: int, segments: dict, transcripts_ok: int, by_route: dict) -> str:
    """e.g. `42 videos: 18 creator chapters, 9 comment timestamps, 6 transcript-split, 9 whole; transcripts
    30/42 (captions 24, Gemini 6)`. All four segment kinds are always shown (zeros too); routes with a zero
    count are left out of the parentheses, and the parentheses go when no route has any."""
    if not total:
        return "0 videos"
    segs = ", ".join(f"{segments.get(k, 0)} {label}" for k, label in SEGMENTS)
    known = dict(ROUTES)
    order = [k for k, _ in ROUTES] + sorted(k for k in by_route if k not in known)
    routes = ", ".join(f"{known.get(k, k)} {by_route[k]}" for k in order if by_route.get(k))
    out = f"{total} videos: {segs}; transcripts {transcripts_ok}/{total}"
    return f"{out} ({routes})" if routes else out


def _spec_order(roadmap: Roadmap) -> dict[str, int]:
    keys = [r.get("id") for st in (roadmap.spec or {}).get("stages", []) for r in st.get("resources", [])]
    return {k: i for i, k in enumerate(keys)}


def build_report(session: Session, roadmap: Roadmap) -> dict:
    resources, docs_by_res, failures = _gather(session, roadmap.id)
    order = _spec_order(roadmap)
    resources.sort(key=lambda r: (order.get(r.key, len(order)), r.key))
    out_res = [{"id": r.key, "title": r.title, "kind": r.kind, "url": r.url, "status": r.status, "reason": r.reason,
                "pages": int((r.stats or {}).get("pages") or 0), "videos": int((r.stats or {}).get("videos") or 0),
                "chunks": int((r.stats or {}).get("chunks") or 0)} for r in resources]
    videos = [d for docs in docs_by_res.values() for d in docs if d.kind == "video" and d.status == "ok"]
    segments = {k: 0 for k, _ in SEGMENTS}
    by_route: dict[str, int] = {}
    for d in videos:
        seg = (d.meta or {}).get("segment_route")
        if seg in segments:
            segments[seg] += 1
        route = (d.meta or {}).get("transcript_route")
        if route:
            by_route[route] = by_route.get(route, 0) + 1
    ok = sum(by_route.values())
    skipped = sorted(({"url": u, "reason": reason} for items in failures.values() for u, reason in items),
                     key=lambda x: (x["url"] or "", x["reason"] or ""))
    return {
        "status": roadmap.status,
        "resources": out_res,
        "videos": {"total": len(videos), "segments": segments, "transcripts": {"ok": ok, "by_route": by_route}},
        "skipped": skipped,
        "summary": video_summary(len(videos), segments, ok, by_route),
    }
