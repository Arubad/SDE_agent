"""Ingestion job handlers: crawl pages, process videos, playlists, PDFs and GitHub repos, chunk and index.

Every fetched URL ends as a `Document` row whose `status`/`reason` say what happened (`ok`; `skipped` with the
fetch outcome such as `not_found` or `robots_denied`; `failed` with `error`), so failures are states, not
exceptions. Handlers raise only `RetryableError` (a transient fetch error before the last attempt) and
`PermanentError` (the last attempt still failed); anything unexpected is retried by the worker and, once dead,
reported by `report.finalize_roadmap` from the dead job.

Job keys: a resource's own URL is keyed by the resource (`owner=True`: its Document belongs to it even when
another resource reached the same URL first); everything discovered is keyed roadmap-wide, so a page, video,
playlist, PDF or repo is processed once per roadmap.

Crawl limits: links are followed while `depth < crawl_max_depth`, only on the resource's host and under its
root path (`discover`), and each page or PDF takes a slot from `Resource.stats.pages`, reserved by one atomic
`UPDATE ... RETURNING` that fails once `crawl_max_pages` slots are taken (safe with any number of workers;
`finalize_roadmap` later rewrites `stats.pages` to the number of documents fetched)."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from io import BytesIO
from urllib.parse import quote, urlsplit

from sqlalchemy import Engine, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.tutor.db import session_scope
from backend.tutor.ingest.discover import _repo as parse_repo
from backend.tutor.ingest.discover import discover, parse_youtube
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.extract import extract_page
from backend.tutor.ingest.fetch import FetchResult
from backend.tutor.ingest.index import index_document
from backend.tutor.ingest.report import finalize_roadmap
from backend.tutor.ingest.segment import choose_segments
from backend.tutor.ingest.video import TranscriptRouter, video_document
from backend.tutor.jobs import queue, worker
from backend.tutor.jobs.queue import PermanentError, RetryableError
from backend.tutor.models import Document, Job, Resource, Roadmap

log = logging.getLogger(__name__)
GITHUB_DOCS_MAX = 30
GITHUB_DIRS_MAX = 30
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
TERMINAL = ("ready", "ready_with_gaps", "failed")


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def playlist_url(list_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={list_id}"


# -- shared helpers -------------------------------------------------------------------------------
def _resource(engine: Engine, resource_id: str) -> Resource:
    with Session(engine, expire_on_commit=False) as s:
        res = s.get(Resource, uuid.UUID(resource_id))
        if res is None:
            raise PermanentError(f"resource {resource_id} no longer exists")
        s.expunge(res)
        return res


def _key_exists(engine: Engine, type: str, roadmap_id, *parts) -> bool:
    key = queue.job_key(type, roadmap_id, *[str(p) for p in parts])
    with Session(engine) as s:
        return s.execute(select(Job.id).where(Job.key == key)).first() is not None


def _owned_elsewhere(engine: Engine, res: Resource, url: str) -> bool:
    with Session(engine) as s:
        owner = s.execute(select(Document.resource_id).where(
            Document.roadmap_id == res.roadmap_id, Document.url == url)).first()
    return owner is not None and owner[0] != res.id


def reserve_page(engine: Engine, resource_id, cap: int) -> bool:
    """Atomically take one of the resource's `cap` page slots; False once they are all taken."""
    with session_scope(engine) as s:
        row = s.execute(text(
            "UPDATE resources SET stats = jsonb_set(stats, '{pages}', "
            "to_jsonb(COALESCE((stats->>'pages')::int, 0) + 1)) "
            "WHERE id = :id AND COALESCE((stats->>'pages')::int, 0) < :cap RETURNING (stats->>'pages')::int"),
            {"id": resource_id, "cap": cap}).first()
    return row is not None


def _store(engine: Engine, res: Resource, *, url: str, kind: str, owner: bool, title: str = "",
           markdown: str = "", status: str = "ok", reason: str | None = None, meta: dict | None = None,
           content_hash: str | None = None) -> uuid.UUID | None:
    """Upsert the (roadmap, url) Document; meta is merged. Returns its id, or None when another resource
    owns the URL and this job is not the URL's owner (the existing document is left alone)."""
    stmt = pg_insert(Document).values(
        id=uuid.uuid4(), roadmap_id=res.roadmap_id, resource_id=res.id, url=url, kind=kind, title=title,
        markdown=markdown, status=status, reason=reason, meta=meta or {}, content_hash=content_hash)
    ex = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=["roadmap_id", "url"],
        set_={"resource_id": ex.resource_id, "kind": ex.kind, "title": ex.title, "markdown": ex.markdown,
              "status": ex.status, "reason": ex.reason, "content_hash": ex.content_hash,
              "meta": Document.meta.op("||")(ex.meta)},
        where=None if owner else (Document.resource_id == ex.resource_id),
    ).returning(Document.id)
    with session_scope(engine) as s:
        return s.execute(stmt).scalar_one_or_none()


def _enqueue(ctx, job: Job, type: str, payload: dict, parts: tuple) -> None:
    ctx.enqueue(type, payload, parts, job.id, roadmap_id=job.roadmap_id)


def _chunk(ctx, job: Job, res: Resource, doc_id: uuid.UUID | None) -> None:
    if doc_id is not None:
        _enqueue(ctx, job, "chunk_embed", {"resource_id": str(res.id), "document_id": str(doc_id)}, (doc_id,))


def content_hash(fr: FetchResult) -> str:
    """Hash of what is indexed: the extracted Markdown for HTML, the raw bytes otherwise."""
    if "html" in fr.content_type or not fr.content_type:
        data = extract_page(fr.body.decode("utf-8", "replace"), fr.url).markdown.encode()
    else:
        data = fr.body
    return hashlib.sha256(data).hexdigest()


def _non_ok(ctx, job: Job, res: Resource, url: str, kind: str, fr: FetchResult, owner: bool) -> dict:
    """Record a fetch that did not succeed. Only `error` is retried; walls and denials are reported."""
    if fr.outcome == "error" and job.attempts < job.max_attempts:
        raise RetryableError(fr.reason or "fetch error")
    status = "failed" if fr.outcome == "error" else "skipped"
    _store(ctx.engine, res, url=url, kind=kind, owner=owner, title=url, status=status, reason=fr.outcome,
           meta={"detail": fr.reason, "http_status": fr.status})
    if fr.outcome == "error":
        raise PermanentError(f"{url}: {fr.reason}")
    return {"outcome": fr.outcome}


def _root_path(url: str) -> str:
    path = urlsplit(url).path or "/"
    return path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"


# -- handlers -------------------------------------------------------------------------------------
def ingest_resource(ctx, job: Job) -> dict:
    res = _resource(ctx.engine, job.payload["resource_id"])
    with session_scope(ctx.engine) as s:
        s.execute(update(Resource).where(Resource.id == res.id, Resource.status == "pending")
                  .values(status="ingesting"))
    base = {"resource_id": str(res.id), "owner": True}
    vid, lst = parse_youtube(res.url)
    repo = parse_repo(res.url) if res.kind == "repo" else None
    if lst and (res.kind == "playlist" or not vid):
        type, payload = "expand_playlist", {**base, "list_id": lst}
    elif vid:
        type, payload = "video", {**base, "video_id": vid}
    elif res.kind == "pdf":
        type, payload = "pdf", {**base, "url": res.url}
    elif repo:
        type, payload = "github_repo", {**base, "repo": repo}
    else:
        type, payload = "fetch_page", {**base, "url": res.url, "depth": 0}
    parts = ("resource", res.id, type)
    if type in ("fetch_page", "pdf") and not _key_exists(ctx.engine, type, job.roadmap_id, *parts):
        reserve_page(ctx.engine, res.id, ctx.settings.crawl_max_pages)  # the resource's own page takes a slot
    _enqueue(ctx, job, type, payload, parts)
    return {"enqueued": type}


def fetch_page(ctx, job: Job) -> dict:
    p = job.payload
    res, url, owner = _resource(ctx.engine, p["resource_id"]), p["url"], p.get("owner", False)
    depth = int(p.get("depth", 0))
    if not owner and _owned_elsewhere(ctx.engine, res, url):
        return {"duplicate": True}
    fr = ctx.net.fetcher.get(url)
    if fr.outcome != "ok":
        return _non_ok(ctx, job, res, url, "page", fr, owner)
    if fr.content_type == "application/pdf" or fr.body[:5] == b"%PDF-":
        return _store_pdf(ctx, job, res, url, fr, owner)
    html = fr.body.decode("utf-8", "replace")
    page = extract_page(html, fr.url)
    has_text = bool(page.markdown.strip()) and page.word_count > 0
    doc_id = _store(ctx.engine, res, url=url, kind="page", owner=owner, title=page.title or url,
                    markdown=page.markdown, status="ok" if has_text else "skipped",
                    reason=None if has_text else "no_text",
                    content_hash=hashlib.sha256(page.markdown.encode()).hexdigest(),  # == content_hash(fr)
                    meta={"depth": depth, "final_url": fr.url, "words": page.word_count, "lang": page.lang})
    found = discover(html, fr.url, focus=list(res.focus or []), root=_root_path(res.url))
    queued = _enqueue_discoveries(ctx, job, res, found, depth)
    if has_text:
        _chunk(ctx, job, res, doc_id)
    return {"words": page.word_count, **queued}


def _enqueue_discoveries(ctx, job: Job, res: Resource, found, depth: int) -> dict:
    rid, rs = str(res.id), job.roadmap_id
    for vid in found.videos:
        _enqueue(ctx, job, "video", {"resource_id": rid, "video_id": vid}, (vid,))
    for lst in found.playlists:
        _enqueue(ctx, job, "expand_playlist", {"resource_id": rid, "list_id": lst}, (lst,))
    for repo in found.repos:
        _enqueue(ctx, job, "github_repo", {"resource_id": rid, "repo": repo}, (repo,))
    cap, full, links = ctx.settings.crawl_max_pages, False, 0
    for pdf in found.pdfs:
        if _key_exists(ctx.engine, "pdf", rs, pdf):
            continue
        if not reserve_page(ctx.engine, res.id, cap):
            full = True
            break
        _enqueue(ctx, job, "pdf", {"resource_id": rid, "url": pdf}, (pdf,))
    if depth < ctx.settings.crawl_max_depth and not full:
        host = (urlsplit(res.url).hostname or "").lower()
        own = queue._normalize(res.url)
        for link in found.links:
            if (urlsplit(link).hostname or "").lower() != host or queue._normalize(link) == own:
                continue
            if _key_exists(ctx.engine, "fetch_page", rs, link):  # also makes a retried job reserve nothing twice
                continue
            if not reserve_page(ctx.engine, res.id, cap):
                break
            _enqueue(ctx, job, "fetch_page", {"resource_id": rid, "url": link, "depth": depth + 1}, (link,))
            links += 1
    return {"videos": len(found.videos), "playlists": len(found.playlists), "links": links}


def expand_playlist(ctx, job: Job) -> dict:
    p = job.payload
    res, lst = _resource(ctx.engine, p["resource_id"]), p["list_id"]
    ids = ctx.net.youtube.playlist_items(lst, ctx.settings.playlist_max_videos)[:ctx.settings.playlist_max_videos]
    if not ids:
        _store(ctx.engine, res, url=playlist_url(lst), kind="video", owner=p.get("owner", False), title=lst,
               status="skipped", reason="empty_playlist", meta={"playlist_id": lst})
        return {"videos": 0}
    for vid in ids:
        _enqueue(ctx, job, "video", {"resource_id": str(res.id), "video_id": vid, "playlist_id": lst}, (vid,))
    return {"videos": len(ids)}


def make_router(ctx) -> TranscriptRouter:
    """Uses the process-wide captions throttle and breaker (workers are threads in one process)."""
    return TranscriptRouter(ctx.settings, ctx.net.youtube)


def _embedder(ctx):
    return ctx.index.embedder if ctx.index is not None else HashEmbedder()


def video(ctx, job: Job) -> dict:
    """Meta, transcript, chapters and the Markdown document in one job (the steps share in-memory state)."""
    p = job.payload
    res, vid, owner = _resource(ctx.engine, p["resource_id"]), p["video_id"], p.get("owner", False)
    url = watch_url(vid)
    if not owner and _owned_elsewhere(ctx.engine, res, url):
        return {"duplicate": True}
    yt = ctx.net.youtube
    meta = yt.video_meta(vid)
    if meta is None:
        _store(ctx.engine, res, url=url, kind="video", owner=owner, title=vid, status="skipped",
               reason="video_unavailable", meta={"video_id": vid})
        return {"outcome": "video_unavailable"}
    route, cues, reason = make_router(ctx).get(vid, meta)
    try:
        comments = yt.top_comments(vid)
    except Exception:  # comments only help chapter detection
        comments = []
    seg_route, chapters = choose_segments(meta, comments, cues, _embedder(ctx))
    doc_id = _store(ctx.engine, res, url=url, kind="video", owner=owner, title=meta.title or vid,
                    markdown=video_document(meta, route, cues, seg_route, chapters), reason=reason,
                    meta={"video_id": vid, "channel": meta.channel, "duration_s": meta.duration_s,
                          "transcript_route": route, "segment_route": seg_route, "reason": reason,
                          "chapters": len(chapters), "playlist_id": p.get("playlist_id")})
    _chunk(ctx, job, res, doc_id)
    return {"transcript_route": route, "segment_route": seg_route, "reason": reason}


def pdf(ctx, job: Job) -> dict:
    p = job.payload
    res, url, owner = _resource(ctx.engine, p["resource_id"]), p["url"], p.get("owner", False)
    if not owner and _owned_elsewhere(ctx.engine, res, url):
        return {"duplicate": True}
    fr = ctx.net.fetcher.get(url)
    if fr.outcome != "ok":
        return _non_ok(ctx, job, res, url, "pdf", fr, owner)
    return _store_pdf(ctx, job, res, url, fr, owner)


def _store_pdf(ctx, job: Job, res: Resource, url: str, fr: FetchResult, owner: bool) -> dict:
    from pypdf import PdfReader

    name = urlsplit(url).path.rsplit("/", 1)[-1] or url
    try:
        reader = PdfReader(BytesIO(fr.body))
        pages = [(n, (pg.extract_text() or "").strip()) for n, pg in enumerate(reader.pages, 1)]
        title = ((reader.metadata.title if reader.metadata else None) or name).strip() or name
    except Exception as e:  # malformed or encrypted: a reported state
        _store(ctx.engine, res, url=url, kind="pdf", owner=owner, title=name, status="skipped",
               reason="pdf_unreadable", meta={"detail": f"{type(e).__name__}: {e}"[:300]})
        return {"outcome": "pdf_unreadable"}
    text_pages = [(n, t) for n, t in pages if t]
    md = f"# {title}\n\n" + "\n\n".join(f"## Page {n}\n\n{t}" for n, t in text_pages)
    doc_id = _store(ctx.engine, res, url=url, kind="pdf", owner=owner, title=title, markdown=md,
                    status="ok" if text_pages else "skipped", reason=None if text_pages else "no_text",
                    content_hash=content_hash(fr), meta={"pages": len(pages)})
    if text_pages:
        _chunk(ctx, job, res, doc_id)
    return {"pages": len(pages)}


def _github_headers(ctx) -> dict[str, str]:
    token = getattr(ctx.settings, "github_token", None)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _github_get(ctx, url: str) -> FetchResult:
    """GitHub's REST API and raw file host, fetched politely (with the token if set) through the shared
    fetcher; the fetcher itself exempts exactly these two hosts from robots.txt (fetch.ROBOTS_EXEMPT_HOSTS)."""
    return ctx.net.fetcher.get(url, headers={"Accept": "application/vnd.github+json", **_github_headers(ctx)})


def _github_docs(ctx, repo: str) -> list[str]:
    """Up to GITHUB_DOCS_MAX `docs/**/*.md` paths, breadth first, via the contents API."""
    found: list[str] = []
    todo, dirs = ["docs"], 0
    while todo and dirs < GITHUB_DIRS_MAX and len(found) < GITHUB_DOCS_MAX:
        path, dirs = todo.pop(0), dirs + 1
        fr = _github_get(ctx, f"{GITHUB_API}/repos/{repo}/contents/{quote(path)}")
        if fr.outcome != "ok":
            continue
        try:
            entries = json.loads(fr.body)
        except ValueError:
            continue
        for e in sorted((e for e in entries if isinstance(e, dict)) if isinstance(entries, list) else [],
                        key=lambda e: str(e.get("path"))):
            p = str(e.get("path") or "")
            if e.get("type") == "dir" and p.startswith("docs/"):
                todo.append(p)
            elif e.get("type") == "file" and p.lower().endswith(".md") and len(found) < GITHUB_DOCS_MAX:
                found.append(p)
    return found


def github_repo(ctx, job: Job) -> dict:
    p = job.payload
    res, repo, owner = _resource(ctx.engine, p["resource_id"]), p["repo"], p.get("owner", False)
    stored = 0
    for path in ["README.md", *_github_docs(ctx, repo)]:
        url = f"https://github.com/{repo}/blob/HEAD/{path}"
        if not owner and _owned_elsewhere(ctx.engine, res, url):
            continue
        fr = _github_get(ctx, f"{GITHUB_RAW}/{repo}/HEAD/{quote(path)}")
        if fr.outcome == "error" and job.attempts < job.max_attempts:
            raise RetryableError(f"{path}: {fr.reason}")
        if fr.outcome != "ok":
            _store(ctx.engine, res, url=url, kind="repo_file", owner=owner, title=f"{repo}: {path}",
                   status="failed" if fr.outcome == "error" else "skipped", reason=fr.outcome,
                   meta={"detail": fr.reason, "repo": repo, "path": path})
            continue
        md = fr.body.decode("utf-8", "replace")
        doc_id = _store(ctx.engine, res, url=url, kind="repo_file", owner=owner, title=f"{repo}: {path}",
                        markdown=md, content_hash=content_hash(fr), meta={"repo": repo, "path": path})
        _chunk(ctx, job, res, doc_id)
        stored += 1
    return {"files": stored}


def chunk_embed(ctx, job: Job) -> dict:
    with Session(ctx.engine, expire_on_commit=False) as s:
        doc = s.get(Document, uuid.UUID(job.payload["document_id"]))
        if doc is None:
            raise PermanentError("document no longer exists")
        res = s.get(Resource, doc.resource_id)
        skills = list(res.skill_ids or []) if res else []
        s.expunge_all()
    if ctx.index is None:
        raise PermanentError("no platform index is configured")
    n = index_document(ctx.engine, ctx.index, doc, skills)
    with session_scope(ctx.engine) as s:  # idempotent: the count lives on the document, the sum is recomputed
        s.execute(text("UPDATE documents SET meta = meta || jsonb_build_object('chunks', :n) WHERE id = :id"),
                  {"n": n, "id": doc.id})
        s.execute(text(
            "UPDATE resources SET stats = jsonb_set(stats, '{chunks}', to_jsonb((SELECT COALESCE(SUM("
            "(meta->>'chunks')::int), 0) FROM documents WHERE resource_id = :rid))) WHERE id = :rid"),
            {"rid": doc.resource_id})
    return {"chunks": n}


def recheck(ctx, job: Job) -> dict:
    """Is the resource still there, and did it change? Records `{status: ok|changed|dead, at}` on the
    resource's own document (`meta.recheck`) and on `Resource.stats.recheck`."""
    res = _resource(ctx.engine, job.payload["resource_id"])
    yt = ctx.net.youtube
    vid, lst = parse_youtube(res.url)
    outcome = "ok"
    if lst and (res.kind == "playlist" or not vid):
        status = "ok" if yt.playlist_items(lst, 1) else "dead"
    elif vid:
        status = "ok" if yt.video_meta(vid) else "dead"
    else:
        fr = ctx.net.fetcher.get(res.url)
        outcome = fr.outcome
        if fr.outcome == "error" and job.attempts < job.max_attempts:
            raise RetryableError(fr.reason or "fetch error")
        if fr.outcome != "ok":
            status = "dead"
        else:
            with Session(ctx.engine) as s:
                old = s.execute(select(Document.status, Document.content_hash).where(
                    Document.roadmap_id == res.roadmap_id, Document.url == res.url)).first()
            if old is None:  # no baseline (e.g. a repo, whose documents are its files): reachable is all we know
                status = "ok"
            elif old.status != "ok":  # it failed before and is reachable now
                status = "changed"
            else:
                status = "ok" if old.content_hash == content_hash(fr) else "changed"
    record = {"status": status, "outcome": outcome, "at": queue.clock().isoformat()}
    with session_scope(ctx.engine) as s:
        s.execute(text("UPDATE documents SET meta = meta || jsonb_build_object('recheck', CAST(:r AS jsonb)) "
                       "WHERE roadmap_id = :rm AND url = :url"),
                  {"r": json.dumps(record), "rm": res.roadmap_id, "url": res.url})
        s.execute(text("UPDATE resources SET stats = stats || jsonb_build_object('recheck', CAST(:r AS jsonb)) "
                       "WHERE id = :id"), {"r": json.dumps(record), "id": res.id})
    return record


HANDLERS: dict[str, worker.Handler] = {
    "ingest_resource": ingest_resource, "fetch_page": fetch_page, "expand_playlist": expand_playlist,
    "video": video, "pdf": pdf, "github_repo": github_repo, "chunk_embed": chunk_embed, "recheck": recheck,
}


# -- orchestration used by the CLI and the served app --------------------------------------------------
def settled_hook(engine: Engine):
    """The `on_settled` callback for workers: finalize the roadmap (Task 11 extends this hook point)."""
    def on_settled(roadmap_id) -> None:
        finalize_roadmap(engine, roadmap_id)
    return on_settled


def settle(engine: Engine, roadmap_id) -> None:
    """Finalize a roadmap with no queued/running jobs: one still `ingesting` (e.g. re-approved with every job
    already done, or nothing to enqueue) goes through the normal CAS; one left `finalizing` is finished."""
    hook = settled_hook(engine)
    worker.maybe_settle(engine, roadmap_id, hook)
    with Session(engine) as s:
        status = s.get(Roadmap, roadmap_id).status
        idle = queue.is_roadmap_settled(s, roadmap_id)
    if status == "finalizing" and idle:
        hook(roadmap_id)


def ingest_roadmap(ctx, roadmap_id, *, until_idle: bool = True, workers: int = 0,
                   timeout_s: float | None = None) -> str:
    """Run the roadmap's jobs in the foreground and return its final (or current) status. `until_idle` runs
    inline and returns once nothing is due; otherwise a `workers`-thread pool runs until the status is terminal."""
    on_settled = settled_hook(ctx.engine)
    if until_idle:
        worker.run_until_idle(ctx, HANDLERS, roadmap_id=roadmap_id, on_settled=on_settled)
        settle(ctx.engine, roadmap_id)
    else:
        pool = worker.WorkerPool(ctx, HANDLERS, max(1, workers), on_settled=on_settled)
        pool.start()
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        try:
            while deadline is None or time.monotonic() < deadline:
                settle(ctx.engine, roadmap_id)
                with Session(ctx.engine) as s:
                    if s.get(Roadmap, roadmap_id).status in TERMINAL:
                        break
                time.sleep(1.0)
        finally:
            pool.stop()
    with Session(ctx.engine) as s:
        return s.get(Roadmap, roadmap_id).status


def recheck_roadmap(ctx, roadmap_id) -> dict[str, dict]:
    """Enqueue one `recheck` per resource with a URL (a fresh key each run), run them, return key -> record."""
    with Session(ctx.engine) as s:
        resources = list(s.scalars(select(Resource).where(Resource.roadmap_id == roadmap_id, Resource.url.isnot(None))
                                   .order_by(Resource.key)))
    nonce = uuid.uuid4().hex
    keys = {}
    for r in resources:  # one attempt: an error is recorded as dead rather than waiting out a backoff
        keys[r.key] = ctx.enqueue("recheck", {"resource_id": str(r.id)}, (r.id, nonce), roadmap_id=roadmap_id,
                                  max_attempts=1).id
    worker.run_until_idle(ctx, {"recheck": recheck}, roadmap_id=roadmap_id)
    with Session(ctx.engine) as s:
        jobs = {j.id: j for j in s.scalars(select(Job).where(Job.id.in_(list(keys.values()))))}
    return {k: (jobs[i].result if jobs[i].status == "done" and jobs[i].result else
                {"status": "dead", "outcome": "error", "error": jobs[i].error})
            for k, i in sorted(keys.items())}
