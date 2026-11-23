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
root path (`discover`; when the resource's own page redirected, e.g. apex -> www, the host and root it landed
on, kept in `Resource.meta.final_url`), and each page or PDF takes a slot from `Resource.stats.pages`, reserved by one atomic
`UPDATE ... RETURNING` that fails once `crawl_max_pages` slots are taken (safe with any number of workers;
`finalize_roadmap` later rewrites `stats.pages` to the number of documents fetched).

The ingestion agent (`agent_plan`, `agent_recover`; see `agent.py`) only ever adds jobs to this queue or proposes
resources; with it switched off or its model down the pipeline below runs exactly as without it."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import time
import uuid
from io import BytesIO
from urllib.parse import quote, urlsplit

from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.tutor.db import session_scope
from backend.tutor.ingest.discover import _repo as parse_repo
from backend.tutor.ingest.discover import discover, parse_youtube
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.extract import extract_page
from backend.tutor.ingest.fetch import PRIVATE_ADDRESS, FetchResult
from backend.tutor.ingest.index import index_document
from backend.tutor.ingest.report import finalize_roadmap
from backend.tutor.ingest.segment import choose_segments
from backend.tutor.ingest.video import TranscriptRouter, video_document
from backend.tutor.jobs import queue, worker
from backend.tutor.jobs.queue import PermanentError, RetryableError
from backend.tutor.mcp.browser import RenderResult, render_page
from backend.tutor.models import Document, Job, Resource, Roadmap

log = logging.getLogger(__name__)
GITHUB_DOCS_MAX = 30
GITHUB_DISCOVERED_MAX = 5  # repos kept per resource when found on crawled pages
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


def reserve_page(engine: Engine, resource_id, cap: int, key: str = "pages") -> bool:
    """Atomically take one of the resource's `cap` slots of the `stats[key]` counter ("pages" by default);
    False once they are all taken."""
    with session_scope(engine) as s:
        row = s.execute(text(
            "UPDATE resources SET stats = jsonb_set(stats, ARRAY[:key], "
            "to_jsonb(COALESCE((stats->>:key)::int, 0) + 1)) "
            "WHERE id = :id AND COALESCE((stats->>:key)::int, 0) < :cap RETURNING 1"),
            {"id": resource_id, "cap": cap, "key": key}).first()
    return row is not None


def new_document_id() -> uuid.UUID:
    """Id for a new Document. Random uuid4; the ingestion-agent eval rebinds this inside a run so the ids that
    reach the agent's prompt are deterministic (without touching the global uuid.uuid4 the SDK also uses)."""
    return uuid.uuid4()


def _store(engine: Engine, res: Resource, *, url: str, kind: str, owner: bool, title: str = "",
           markdown: str = "", status: str = "ok", reason: str | None = None, meta: dict | None = None,
           content_hash: str | None = None) -> uuid.UUID | None:
    """Upsert the (roadmap, url) Document; meta is merged. Returns its id, or None when another resource
    owns the URL and this job is not the URL's owner (the existing document is left alone)."""
    stmt = pg_insert(Document).values(
        id=new_document_id(), roadmap_id=res.roadmap_id, resource_id=res.id, url=url, kind=kind, title=title,
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


def _chunk(ctx, job: Job, res: Resource, doc_id: uuid.UUID | None, *extra) -> None:
    """`extra` key parts re-index a document that changed (a transcript retry); the plain key indexes it once."""
    if doc_id is not None:
        _enqueue(ctx, job, "chunk_embed", {"resource_id": str(res.id), "document_id": str(doc_id)},
                 (doc_id, *extra))


def content_hash(fr: FetchResult) -> str:
    """Hash of what is indexed: the extracted Markdown for HTML, the raw bytes otherwise."""
    if "html" in fr.content_type or not fr.content_type:
        data = extract_page(fr.body.decode("utf-8", "replace"), fr.url).markdown.encode()
    else:
        data = fr.body
    return hashlib.sha256(data).hexdigest()


def _non_ok(ctx, job: Job, res: Resource, url: str, kind: str, fr: FetchResult, owner: bool) -> dict:
    """Record a fetch that did not succeed. Only `error` is retried; walls and denials are reported."""
    if fr.outcome == "error" and job.attempts < job.max_attempts and fr.reason != PRIVATE_ADDRESS:
        raise RetryableError(fr.reason or "fetch error")  # a private address will not become public on retry
    status = "failed" if fr.outcome == "error" else "skipped"
    _store(ctx.engine, res, url=url, kind=kind, owner=owner, title=url, status=status, reason=fr.outcome,
           meta={"detail": fr.reason, "http_status": fr.status})
    if fr.outcome == "error":
        raise PermanentError(f"{url}: {fr.reason}")
    return {"outcome": fr.outcome}


def _root_path(url: str) -> str:
    path = urlsplit(url).path or "/"
    return path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"


def _scope_url(res: Resource) -> str:
    """The URL whose host and root path bound the resource's crawl: where its own page actually lives
    (`meta.final_url`, recorded when it redirected, e.g. apex -> www), else its URL."""
    return (res.meta or {}).get("final_url") or res.url


def _record_final_url(engine: Engine, res: Resource, final: str) -> None:
    """Remember (or forget) where the resource's own page redirected to; updates `res.meta` in place."""
    moved = final if (urlsplit(final).hostname, _root_path(final)) != (
        urlsplit(res.url).hostname, _root_path(res.url)) else None
    if moved == (res.meta or {}).get("final_url"):
        return
    with session_scope(engine) as s:
        if moved:
            s.execute(text("UPDATE resources SET meta = meta || jsonb_build_object('final_url', CAST(:u AS text)) "
                           "WHERE id = :id"), {"u": moved, "id": res.id})
        else:
            s.execute(text("UPDATE resources SET meta = meta - 'final_url' WHERE id = :id"), {"id": res.id})
    meta = {k: v for k, v in (res.meta or {}).items() if k != "final_url"}
    res.meta = {**meta, "final_url": moved} if moved else meta


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
    # a URL replaced by the recovery agent is a new crawl of the resource; otherwise the keys are as before
    parts = ("resource", res.id, type) + ((res.url,) if (res.meta or {}).get("replaced") else ())
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
    if fr.outcome == "needs_browser":
        rendered = _render(ctx, res, url)
        if isinstance(rendered, RenderResult):
            page = extract_page(rendered.html, rendered.final_url)
            if _has_text(page):
                return _store_page(ctx, job, res, url, rendered.html, page, rendered.final_url, owner, depth,
                                   {"rendered": "browser"})
            rendered = "no text after rendering"
        return _non_ok(ctx, job, res, url, "page", dataclasses.replace(fr, reason=rendered), owner)
    if fr.outcome != "ok":
        return _non_ok(ctx, job, res, url, "page", fr, owner)
    if fr.content_type == "application/pdf" or fr.body[:5] == b"%PDF-":
        return _store_pdf(ctx, job, res, url, fr, owner)
    html = fr.body.decode("utf-8", "replace")
    # every hop was address-checked by the fetcher
    return _store_page(ctx, job, res, url, html, extract_page(html, fr.url), fr.url, owner, depth, {})


def _has_text(page) -> bool:
    return bool(page.markdown.strip()) and page.word_count > 0


def _render(ctx, res: Resource, url: str) -> RenderResult | str:
    """A `needs_browser` page rendered in the gateway's browser, or why not (kept as the skipped page's detail)."""
    if not ctx.settings.browser_render:
        return "browser rendering is off"
    if ctx.gateway is None:
        return "browser unavailable: no gateway"
    return render_page(ctx.gateway, url, scope_host=(urlsplit(_scope_url(res)).hostname or "").lower(),
                       resolver=ctx.net.fetcher.resolver)


def _store_page(ctx, job: Job, res: Resource, url: str, html: str, page, final_url: str, owner: bool, depth: int,
                meta: dict) -> dict:
    """Store a fetched (or browser-rendered) HTML page, enqueue what it links to and, if it has text, index it.
    `final_url` (where the page ended up) has passed the address checks: the fetcher's, or the gateway's policy."""
    has_text = _has_text(page)
    doc_id = _store(ctx.engine, res, url=url, kind="page", owner=owner, title=page.title or url,
                    markdown=page.markdown, status="ok" if has_text else "skipped",
                    reason=None if has_text else "no_text",
                    content_hash=hashlib.sha256(page.markdown.encode()).hexdigest(),  # == content_hash(fr)
                    meta={"depth": depth, "final_url": final_url, "words": page.word_count, "lang": page.lang,
                          **meta})
    if owner and depth == 0:
        _record_final_url(ctx.engine, res, final_url)
    scope = _scope_url(res)
    found = discover(html, final_url, focus=list(res.focus or []), root=_root_path(scope))
    queued = _enqueue_discoveries(ctx, job, res, found, depth, scope)
    if has_text:
        _chunk(ctx, job, res, doc_id)
    return {"words": page.word_count, **queued}


def _enqueue_discoveries(ctx, job: Job, res: Resource, found, depth: int, scope: str) -> dict:
    """Enqueue what a crawled page links to. Videos and playlists are free; PDFs, GitHub repos and same-site
    pages (on `scope`'s host; see `_scope_url`) each reserve one of the resource's `crawl_max_pages` slots, taken
    in that order (pages, PDFs, repos) so a full budget never starves the docs crawl; once none is left nothing
    more is enqueued."""
    rid, rs = str(res.id), job.roadmap_id
    for vid in found.videos:
        _enqueue(ctx, job, "video", {"resource_id": rid, "video_id": vid}, (vid,))
    for lst in found.playlists:
        _enqueue(ctx, job, "expand_playlist", {"resource_id": rid, "list_id": lst}, (lst,))
    cap, full, links = ctx.settings.crawl_max_pages, False, 0
    if depth < ctx.settings.crawl_max_depth:  # same-site pages first: they are the knowledge base
        host = (urlsplit(scope).hostname or "").lower()
        own = {queue.normalize_url(res.url), queue.normalize_url(scope)}
        for link in found.links:
            if (urlsplit(link).hostname or "").lower() != host or queue.normalize_url(link) in own:
                continue
            if _key_exists(ctx.engine, "fetch_page", rs, link):  # also makes a retried job reserve nothing twice
                continue
            if not reserve_page(ctx.engine, res.id, cap):
                full = True
                break
            _enqueue(ctx, job, "fetch_page", {"resource_id": rid, "url": link, "depth": depth + 1}, (link,))
            links += 1
    for pdf in found.pdfs:
        if full:
            break
        if _key_exists(ctx.engine, "pdf", rs, pdf):
            continue
        if not reserve_page(ctx.engine, res.id, cap):
            full = True
            break
        _enqueue(ctx, job, "pdf", {"resource_id": rid, "url": pdf}, (pdf,))
    # a discovered repo costs one page slot and one of GITHUB_DISCOVERED_MAX per resource (extras are dropped
    # silently); it ingests its README only
    for repo in found.repos:
        if full:
            break
        if _key_exists(ctx.engine, "github_repo", rs, repo):
            continue
        if not reserve_page(ctx.engine, res.id, GITHUB_DISCOVERED_MAX, key="repos"):
            break
        if not reserve_page(ctx.engine, res.id, cap):
            full = True
            break
        _enqueue(ctx, job, "github_repo", {"resource_id": rid, "repo": repo}, (repo,))
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


def make_router(ctx, route: str | None = None) -> TranscriptRouter:
    """Uses the process-wide captions throttle and breaker (workers are threads in one process). `route` limits
    it to that one route, which must be enabled (a retry requested by the recovery agent)."""
    settings = ctx.settings
    if route is not None:
        if route not in settings.transcript_routes:
            raise PermanentError(f"transcript route {route!r} is not enabled")
        settings = dataclasses.replace(settings, transcript_routes=(route,))
    return TranscriptRouter(settings, ctx.net.youtube)


def _embedder(ctx):
    return ctx.index.embedder if ctx.index is not None else HashEmbedder()


RAW_MAX_CUES, RAW_MAX_COMMENTS, RAW_MAX_TEXT = 12000, 20, 5000


def _raw_video(meta, comments: list[str], cues) -> dict:
    """What the eval's capture tool needs to rebuild a fixture (description, creator chapters, top comments, cues),
    kept in Document.meta['raw'], bounded: <= RAW_MAX_CUES cues, RAW_MAX_COMMENTS comments, RAW_MAX_TEXT chars each."""
    return {"description": meta.description[:RAW_MAX_TEXT],
            "chapters": [{"start_s": c.start_s, "title": c.title} for c in meta.chapters],
            "comments": [c[:RAW_MAX_TEXT] for c in comments[:RAW_MAX_COMMENTS]],
            "cues": [[c.start, c.dur, c.text] for c in cues[:RAW_MAX_CUES]]}


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
    retry = p.get("route")
    route, cues, reason = make_router(ctx, retry).get(vid, meta)
    try:
        comments = yt.top_comments(vid)
    except Exception:  # comments only help chapter detection
        comments = []
    seg_route, chapters = choose_segments(meta, comments, cues, _embedder(ctx))
    doc_id = _store(ctx.engine, res, url=url, kind="video", owner=owner, title=meta.title or vid,
                    markdown=video_document(meta, route, cues, seg_route, chapters), reason=reason,
                    meta={"video_id": vid, "channel": meta.channel, "duration_s": meta.duration_s,
                          "transcript_route": route, "segment_route": seg_route, "reason": reason,
                          "chapters": len(chapters), "playlist_id": p.get("playlist_id"),
                          "raw": _raw_video(meta, comments, cues)})
    _chunk(ctx, job, res, doc_id, *(("retry", retry, job.id) if retry else ()))
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
    """A resource whose own URL is a repo ingests README.md plus up to GITHUB_DOCS_MAX `docs/**/*.md` files and
    takes no page slot. A repo discovered on a crawled page (non-owner) ingests README.md only; its page slot
    was reserved when it was enqueued."""
    p = job.payload
    res, repo, owner = _resource(ctx.engine, p["resource_id"]), p["repo"], p.get("owner", False)
    stored = 0
    for path in ["README.md", *(_github_docs(ctx, repo) if owner else [])]:
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


# -- the ingestion agent ----------------------------------------------------------------------------
def _run_agent(ctx, job: Job, mode: str, round: int) -> dict:
    """Run the agent once (first attempt only: a retried job does not pay for the model twice). Never raises:
    `run_ingest_agent` records its own failures; this guards against anything that escapes it."""
    from backend.tutor.ingest.agent import record_error, run_ingest_agent

    if not ctx.settings.ingest_agent or job.attempts > 1:
        return {"agent": "skipped"}
    try:
        run = run_ingest_agent(ctx, job.roadmap_id, mode, round)
        return {"agent": run.stop, "steps": run.steps, "actions": run.actions}
    except Exception as e:
        log.exception("ingestion agent (%s) failed for roadmap %s", mode, job.roadmap_id)
        record_error(ctx.engine, job.roadmap_id, mode, round, e)
        return {"agent": "error"}


def agent_plan(ctx, job: Job) -> dict:
    """Plan the crawl (it may enqueue depth-1 `fetch_page` jobs, which are then claimed first), then ALWAYS
    enqueue the deterministic `ingest_resource` jobs, with the keys `service.approve` would have used."""
    out = _run_agent(ctx, job, "plan", 0)
    n = 0
    for rid in job.payload.get("resource_ids") or []:
        with Session(ctx.engine) as s:
            res = s.get(Resource, uuid.UUID(rid))
            key = res.key if res is not None and res.status == "pending" else None
        if key is not None:
            _enqueue(ctx, job, "ingest_resource", {"resource_id": rid}, (key,))
            n += 1
    return {**out, "ingest_resource": n}


def agent_recover(ctx, job: Job) -> dict:
    return _run_agent(ctx, job, "recover", int(job.payload.get("round") or 1))


HANDLERS: dict[str, worker.Handler] = {
    "ingest_resource": ingest_resource, "fetch_page": fetch_page, "expand_playlist": expand_playlist,
    "video": video, "pdf": pdf, "github_repo": github_repo, "chunk_embed": chunk_embed, "recheck": recheck,
    "agent_plan": agent_plan, "agent_recover": agent_recover,
}


# -- orchestration used by the CLI and the served app --------------------------------------------------
def _start_recovery(engine: Engine, settings, roadmap_id) -> bool:
    """If the agent is on, rounds and budget remain, and the settled roadmap has gaps: back to `ingesting` and
    one `agent_recover` job, in ONE transaction (a crash cannot leave it ingesting with nothing queued)."""
    from backend.tutor.ingest import agent_tools

    with Session(engine) as s:
        rounds = s.execute(select(func.count()).select_from(Job).where(
            Job.roadmap_id == roadmap_id, Job.type == "agent_recover")).scalar_one()
        if rounds >= settings.ingest_agent_recovery_rounds:
            return False
        if agent_tools.spent_usd(s, roadmap_id) >= settings.ingest_agent_budget_usd:
            return False
        if not agent_tools.has_gaps(agent_tools.kb_status(s, roadmap_id, settings, settled=True), settings):
            return False
    with session_scope(engine) as s:
        won = s.execute(update(Roadmap).where(Roadmap.id == roadmap_id, Roadmap.status == "finalizing")
                        .values(status="ingesting")).rowcount
        if not won:
            return False
        queue.enqueue(s, type="agent_recover", roadmap_id=roadmap_id, payload={"round": rounds + 1},
                      key=queue.job_key("agent_recover", roadmap_id, str(rounds + 1)))
    return True


def settled_hook(engine: Engine, settings=None):
    """The `on_settled` callback for workers: start a recovery round when `_start_recovery` says so, else
    finalize the roadmap. Without settings (or with the agent off) it only finalizes, as before."""
    def on_settled(roadmap_id) -> None:
        if settings is not None and settings.ingest_agent:
            try:
                if _start_recovery(engine, settings, roadmap_id):
                    return
            except Exception:  # the pipeline never depends on the agent: finalize instead
                log.exception("could not start a recovery round for roadmap %s", roadmap_id)
        finalize_roadmap(engine, roadmap_id)
    return on_settled


def settle(engine: Engine, roadmap_id, settings=None) -> None:
    """Finalize a roadmap with no queued/running jobs: one still `ingesting` (e.g. re-approved with every job
    already done, or nothing to enqueue) goes through the normal CAS; one left `finalizing` is finished."""
    hook = settled_hook(engine, settings)
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
    on_settled = settled_hook(ctx.engine, ctx.settings)
    if until_idle:
        for _ in range(ctx.settings.ingest_agent_recovery_rounds + 2):  # settle() may start a recovery round
            worker.run_until_idle(ctx, HANDLERS, roadmap_id=roadmap_id, on_settled=on_settled)
            settle(ctx.engine, roadmap_id, ctx.settings)
            with Session(ctx.engine) as s:
                if not s.execute(select(Job.id).where(Job.roadmap_id == roadmap_id, Job.status == "queued",
                                                      Job.not_before <= queue.clock()).limit(1)).first():
                    break
    else:
        pool = worker.WorkerPool(ctx, HANDLERS, max(1, workers), on_settled=on_settled)
        pool.start()
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        try:
            while deadline is None or time.monotonic() < deadline:
                settle(ctx.engine, roadmap_id, ctx.settings)
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
