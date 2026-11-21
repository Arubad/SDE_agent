"""The ingestion agent's tools, its URL policy and its view of the knowledge base (`kb_status`).

The agent acts only by enqueueing jobs or proposing resources; nothing here writes content into the knowledge
base. Web text is untrusted: everything a page or a search engine wrote comes back inside
`<untrusted_web_content>` with any copy of that tag escaped, and never reaches a system prompt.

URL policy (checked in `check_call` AND again inside each tool): a URL the agent opens, replaces a resource with
or proposes must be an http(s) URL that passes `service.validate_crawl_url` (no private/loopback/link-local IPs,
no `localhost`) with no userinfo, and, compared in `queue.normalize_url` form, be
- a roadmap resource URL,
- a URL `web_search` returned in this run, or
- a same-host link `peek_page` found on an allowed URL.
`prioritize_pages` URLs must instead lie on the resource's own host under its root path (the scope the crawler
itself would follow). `peek_page` fetches with a guard that refuses any redirect hop off the page's host or onto a
private address before the request is made."""
from __future__ import annotations

import copy
import hashlib
import logging
import re
import unicodedata
import uuid
from urllib.parse import unquote, urlsplit

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from backend.tutor.db import session_scope
from backend.tutor.ingest import report
from backend.tutor.ingest.discover import discover, parse_youtube
from backend.tutor.ingest.discover import _repo as parse_repo
from backend.tutor.ingest.extract import extract_page
from backend.tutor.ingest.handlers import reserve_page
from backend.tutor.ingest.service import validate_crawl_url
from backend.tutor.jobs import queue
from backend.tutor.models import AgentAction, Document, Job, Resource, Roadmap

log = logging.getLogger(__name__)
TAG = "untrusted_web_content"
MAX_RESULTS, MAX_HEADINGS, PEEK_CHARS, MAX_LINKS, MAX_PROPOSALS = 8, 40, 1500, 50, 3
CATEGORIES = ("general", "videos")
NOT_ALLOWED = "URL not allowed: not from a search result or the roadmap"


_WRAPPED = re.compile(r"\s*<" + TAG + r">\s*(.*?)\s*</" + TAG + r">\s*", re.S)
# Every character NFKC turns into "<" ("<", U+FE64 small, U+FF1C fullwidth; computed so a newer Unicode cannot
# add one unnoticed). Inside untrusted content each becomes "&lt;", so no tag can ever be formed there,
# whatever padding follows. Linear time (str.translate); text without such a character is unchanged.
_LT = str.maketrans({chr(c): "&lt;" for c in range(0x110000) if unicodedata.normalize("NFKC", chr(c)) == "<"})


def wrap(text: str) -> str:
    """Untrusted web text, fenced; every "<"-like character inside it is escaped, so it cannot close the fence."""
    return f"<{TAG}>{str(text or '').translate(_LT)}</{TAG}>"


def unwrap(value):
    """A URL passed back by the model as it was shown, wrapper included: strip one exact wrapper."""
    if isinstance(value, str) and (m := _WRAPPED.fullmatch(value)):
        return m.group(1)
    return value


def _str(schema_desc: str) -> dict:
    return {"type": "string", "description": schema_desc}


_DEFS = {
    "kb_status": {"description": "Per resource its status and reason, per skill its indexed chunk count, the "
                                 "skipped URLs, videos without transcripts, and the remaining budget in USD.",
                  "input_schema": {"type": "object", "properties": {}}},
    "web_search": {"description": f"Search the web. At most {MAX_RESULTS} results of {{title, url, snippet}}.",
                   "input_schema": {"type": "object", "required": ["query"], "properties": {
                       "query": _str("search terms"),
                       "category": {"type": "string", "enum": list(CATEGORIES)}}}},
    "peek_page": {"description": "Open an allowed URL: title, heading outline, link/video counts, same-site "
                                 "links and the first 1,500 characters of text.",
                  "input_schema": {"type": "object", "required": ["url"], "properties": {"url": _str("URL")}}},
    "prioritize_pages": {"description": "Crawl these pages of a resource's own site first (depth 1). The "
                                        "resource's page cap still applies.",
                         "input_schema": {"type": "object", "required": ["resource_id", "urls"], "properties": {
                             "resource_id": _str("the resource id from the roadmap"),
                             "urls": {"type": "array", "items": {"type": "string"}}}}},
    "retry_transcript": {"description": "Try again to get a video's transcript with one enabled route.",
                         "input_schema": {"type": "object", "required": ["video_document_id", "route"],
                                          "properties": {"video_document_id": _str("document_id from kb_status"),
                                                         "route": _str("an enabled transcript route")}}},
    "replace_url": {"description": "Replace the URL of a skipped or partial resource with an allowed URL and "
                                   "ingest it again.",
                    "input_schema": {"type": "object", "required": ["resource_id", "url", "reason"], "properties": {
                        "resource_id": _str("the resource id"), "url": _str("the new URL"),
                        "reason": _str("why, in one sentence")}}},
    "propose_resource": {"description": f"Suggest a resource for a skill. The user must approve it before it is "
                                        f"ingested. At most {MAX_PROPOSALS} per round.",
                         "input_schema": {"type": "object", "required": ["skill_id", "url", "title", "reason"],
                                          "properties": {"skill_id": _str("skill id"), "url": _str("URL"),
                                                         "title": _str("title"),
                                                         "reason": _str("why, in one sentence")}}},
}
MODE_TOOLS = {
    "plan": frozenset({"kb_status", "web_search", "peek_page", "prioritize_pages", "propose_resource"}),
    "recover": frozenset({"kb_status", "web_search", "peek_page", "retry_transcript", "replace_url",
                          "propose_resource"}),
}


def tool_defs(mode: str) -> list[dict]:
    return [{"name": n, **copy.deepcopy(_DEFS[n])} for n in sorted(MODE_TOOLS[mode])]


# -- the knowledge-base view ----------------------------------------------------------------------
def spent_usd(session: Session, roadmap_id) -> float:
    return float(session.execute(select(func.coalesce(func.sum(AgentAction.cost_usd), 0))
                                 .where(AgentAction.roadmap_id == roadmap_id)).scalar_one())


def _skill_chunks(session: Session, roadmap_id) -> dict[str, int]:
    rows = session.execute(text(
        "SELECT s.skill, count(*) FROM sections, jsonb_array_elements_text(sections.skill_ids) AS s(skill) "
        "WHERE sections.roadmap_id = :r GROUP BY s.skill"), {"r": roadmap_id}).all()
    return {k: int(n) for k, n in rows}


def kb_status(session: Session, roadmap_id, settings, *, settled: bool) -> dict:
    """`settled`: resource statuses as finalizing would set them now (the roadmap is not finalized yet while
    the agent recovers); otherwise the stored statuses (before the crawl)."""
    roadmap = session.get(Roadmap, roadmap_id)
    resources, docs_by_res, failures = report.gather(session, roadmap_id)
    outcomes = report.resource_outcomes(resources, docs_by_res, failures) if settled else {}
    chunks = _skill_chunks(session, roadmap_id)
    by_id = {r.id: r for r in resources}
    out_res = []
    for r in sorted(resources, key=lambda r: r.key):
        status, reason = outcomes[r.id][:2] if r.id in outcomes else (r.status, r.reason)
        out_res.append({"id": r.key, "title": wrap(r.title) if r.proposed_by == "agent" else r.title,
                        "user_authored": r.proposed_by is None, "kind": r.kind,
                        "url": wrap(r.url) if r.url else None, "status": status,
                        "reason": reason, "skills": list(r.skill_ids or []), "focus": list(r.focus or []),
                        "crawlable": report.is_crawlable(r)})
    skills = [{"id": k["id"], "title": k["title"], "chunks": chunks.get(k["id"], 0)}
              for st in (roadmap.spec or {}).get("stages", []) for k in st.get("skills", [])]
    videos = [{"document_id": str(d.id), "video_id": wrap((d.meta or {}).get("video_id") or ""),
               "resource": by_id[d.resource_id].key if d.resource_id in by_id else None, "reason": d.reason}
              for docs in docs_by_res.values() for d in docs
              if d.kind == "video" and d.status == "ok" and not (d.meta or {}).get("transcript_route")]
    skipped = [{"url": wrap(u or ""), "reason": why} for u, why in sorted(
        ((u, why) for items in failures.values() for u, why in items), key=lambda x: (x[0] or "", x[1] or ""))]
    return {"resources": out_res, "skills": skills, "skipped": skipped[:50],
            "videos_without_transcript": sorted(videos, key=lambda v: v["document_id"]),
            "transcript_routes": list(settings.transcript_routes),
            "budget_remaining_usd": round(max(0.0, settings.ingest_agent_budget_usd - spent_usd(session, roadmap_id)),
                                          4)}


def has_gaps(status: dict, settings) -> bool:
    return (any(r["crawlable"] and r["status"] in ("skipped", "partial") for r in status["resources"])
            or bool(status["videos_without_transcript"])
            or any(k["chunks"] < settings.min_chunks_per_skill for k in status["skills"]))


# -- URL policy -----------------------------------------------------------------------------------
def safe_url(url) -> str | None:
    """The normalized URL if it may be fetched at all (http(s), no userinfo, no private address), else None."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url.strip())
        validate_crawl_url(url)
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None
    return queue.normalize_url(url.strip())


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _site(url: str) -> tuple[str, int | None]:
    """(host, explicit port): what "the same site" means for redirects, discovered links and prioritized pages."""
    try:
        return _host(url), urlsplit(url).port
    except ValueError:
        return "", -1


def _root(url: str) -> str:
    path = urlsplit(url).path or "/"
    return path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"


def _guess_kind(url: str) -> str:
    vid, lst = parse_youtube(url)
    if lst and not vid:
        return "playlist"
    if vid:
        return "video"
    if urlsplit(url).path.lower().endswith(".pdf"):
        return "pdf"
    return "repo" if parse_repo(url) else "article"


def _clip(value, n: int = 500):
    if isinstance(value, str):
        return value[:n]
    if isinstance(value, list):
        return [_clip(v, n) for v in value[:60]]
    if isinstance(value, dict):
        return {str(k)[:60]: _clip(v, n) for k, v in list(value.items())[:20]}
    return value if isinstance(value, (int, float, bool)) or value is None else str(value)[:n]


class _Tools:
    def __init__(self, ctx, roadmap_id, mode: str, round: int):
        if mode not in MODE_TOOLS:
            raise ValueError(f"unknown agent mode {mode!r}")
        self.ctx, self.engine, self.settings = ctx, ctx.engine, ctx.settings
        self.rid, self.mode, self.round = roadmap_id, mode, round
        with Session(self.engine) as s:
            self.allowed = {n for (u,) in s.execute(select(Resource.url).where(
                Resource.roadmap_id == roadmap_id, Resource.url.isnot(None),
                Resource.status.notin_(report.SUGGESTED))) if (n := safe_url(u))}

    # -- bookkeeping
    def log(self, tool: str, args: dict, outcome: str, summary: str) -> None:
        with session_scope(self.engine) as s:
            s.add(AgentAction(roadmap_id=self.rid, mode=self.mode, round=self.round, tool=tool[:60],
                              args=_clip(args), outcome=outcome, summary=summary[:300]))

    def _resource(self, s: Session, key) -> Resource | None:
        if not isinstance(key, str):
            return None
        return s.scalars(select(Resource).where(Resource.roadmap_id == self.rid, Resource.key == key)).first()

    def url_refusal(self, url) -> str | None:
        norm = safe_url(url)
        if norm is None:
            return "URL not allowed: only public http(s) URLs"
        return None if norm in self.allowed else NOT_ALLOWED

    def _prioritize_refusal(self, args: dict) -> str | None:
        urls, cap = args.get("urls"), self.settings.crawl_max_pages
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
            return "urls must be a non-empty list of URLs"
        if len(urls) > cap:
            return f"at most {cap} URLs (crawl_max_pages)"
        with Session(self.engine) as s:
            res = self._resource(s, args.get("resource_id"))
            if res is None or not report.is_crawlable(res):
                return "unknown or non-crawlable resource_id"
            site, scheme, root = _site(res.url), urlsplit(res.url).scheme, _root(res.url)
        for u in urls:
            parts = urlsplit(u)
            path = unquote(parts.path or "/")  # decoded once: "..%2fadmin" is a traversal too
            dots = any(seg in (".", "..") for seg in re.split(r"[/\\]", path)) or "%2e" in path.lower()
            if safe_url(u) is None or _site(u) != site or parts.scheme != scheme or dots:
                return "URL not allowed: outside the resource's site"
            # a URL the run has seen (search result, discovered link), or a plain page under the resource root
            if safe_url(u) not in self.allowed and (not path.startswith(root) or parts.query):
                return "URL not allowed: outside the resource's site"
        return None

    def refusal(self, name: str, args: dict) -> str | None:
        if name not in MODE_TOOLS[self.mode]:
            return f"unknown tool for {self.mode} mode: {name}"
        if name in ("peek_page", "replace_url", "propose_resource"):
            return self.url_refusal(args.get("url"))
        if name == "prioritize_pages":
            return self._prioritize_refusal(args)
        return None

    def check_call(self, name: str, args: dict, seen_doc_ids: set[str]) -> str | None:
        args = self._args(args)
        reason = self.refusal(name, args)
        if reason is not None:
            self.log(name, args, "refused", reason)
        return reason

    @staticmethod
    def _args(args: dict) -> dict:
        """URL arguments without the untrusted wrapper they were shown in (validation happens after this)."""
        out = dict(args)
        if "url" in out:
            out["url"] = unwrap(out["url"])
        if isinstance(out.get("urls"), list):
            out["urls"] = [unwrap(u) for u in out["urls"]]
        return out

    async def call_tool(self, name: str, args: dict) -> dict:
        args = self._args(args)
        reason = self.refusal(name, args)  # defence in depth: the tool checks again
        if reason is not None:
            self.log(name, args, "refused", reason)
            return {"error": reason}
        try:
            out, summary = getattr(self, "t_" + name)(**{k: v for k, v in args.items() if isinstance(k, str)})
        except TypeError as e:
            out, summary = {"error": "bad arguments"}, "bad arguments"
        except Exception as e:  # a tool failure is a result for the model, never a crash of the job
            log.exception("agent tool %s failed", name)
            out, summary = {"error": f"{name} failed: {type(e).__name__}"}, type(e).__name__
        self.log(name, args, "error" if "error" in out else "ok", out.get("error") or summary)
        return out

    # -- tools (each returns ({"data": ...} | {"error": ...}, short summary))
    def t_kb_status(self):
        with Session(self.engine) as s:
            status = kb_status(s, self.rid, self.settings, settled=self.mode == "recover")
        return {"data": status}, f"{len(status['resources'])} resources"

    def t_web_search(self, query, category="general"):
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            return {"error": "query must be 1-300 characters"}, ""
        if category not in CATEGORIES:
            return {"error": f"category must be one of {list(CATEGORIES)}"}, ""
        if self.ctx.net.search is None:
            return {"error": "web search is not configured"}, ""
        try:
            found = self.ctx.net.search.search(query.strip(), category=category)
        except Exception as e:
            return {"error": f"web search unavailable: {type(e).__name__}"}, ""
        results = []
        for r in found:
            norm = safe_url(r.url)
            if norm is None:
                continue
            self.allowed.add(norm)
            results.append({"title": wrap((r.title or "")[:200]), "url": wrap(r.url),
                            "snippet": wrap((r.snippet or "")[:500])})
            if len(results) == MAX_RESULTS:
                break
        return {"data": {"results": results}}, f"{len(results)} results"

    def t_peek_page(self, url):
        host, site = _host(url), _site(url)
        fr = self.ctx.net.fetcher.get(url, url_guard=lambda hop: safe_url(hop) is not None and _site(hop) == site)
        if fr.outcome != "ok":
            return {"error": f"could not open the page: {fr.outcome}"}, fr.outcome  # a code, never fetch text
        if fr.content_type and "html" not in fr.content_type:
            return {"data": {"url": wrap(fr.url), "note": "not an HTML page"}}, "non-html"
        html = fr.body.decode("utf-8", "replace")
        page = extract_page(html, fr.url)
        found = discover(html, fr.url, root="/")
        links = [u for u in found.links if _site(u) == site and safe_url(u) is not None][:MAX_LINKS]
        self.allowed.update(safe_url(u) for u in links)
        headings = [ln for ln in page.markdown.splitlines() if ln.startswith("#")][:MAX_HEADINGS]
        counts = {"links": len(found.links), "videos": len(found.videos), "playlists": len(found.playlists),
                  "pdfs": len(found.pdfs), "repos": len(found.repos)}
        return {"data": {"url": wrap(fr.url), "title": wrap(page.title), "headings": wrap("\n".join(headings)),
                         "counts": counts, "links": wrap("\n".join(links)),
                         "text": wrap(page.markdown[:PEEK_CHARS])}}, f"peeked {host}: {len(links)} links"

    def t_prioritize_pages(self, resource_id, urls):
        with Session(self.engine) as s:
            res = self._resource(s, resource_id)
            res_id, own = res.id, queue.normalize_url(res.url)
        cap = self.settings.crawl_max_pages - 1  # the resource's own page keeps its slot
        queued, skipped = 0, 0
        for u in dict.fromkeys(urls):
            key = queue.job_key("fetch_page", self.rid, u)
            with Session(self.engine) as s:
                exists = s.execute(select(Job.id).where(Job.key == key)).first() is not None
            if exists or queue.normalize_url(u) == own:
                skipped += 1
                continue
            if not reserve_page(self.engine, res_id, cap):
                break
            self.ctx.enqueue("fetch_page", {"resource_id": str(res_id), "url": u, "depth": 1}, (u,),
                             roadmap_id=self.rid)
            queued += 1
        return {"data": {"enqueued": queued, "skipped": skipped}}, f"enqueued {queued} pages"

    def t_retry_transcript(self, video_document_id, route):
        if route not in self.settings.transcript_routes:
            return {"error": "that transcript route is not enabled; enabled routes: "
                             + ", ".join(self.settings.transcript_routes)}, ""
        try:
            doc_id = uuid.UUID(str(video_document_id))
        except ValueError:
            return {"error": "unknown video_document_id"}, ""
        with Session(self.engine) as s:
            doc = s.get(Document, doc_id)
            if doc is None or doc.roadmap_id != self.rid or doc.kind != "video" or doc.status != "ok":
                return {"error": "unknown video_document_id"}, ""
            if (doc.meta or {}).get("transcript_route"):
                return {"error": "that video already has a transcript"}, ""
            vid, res_id, playlist = doc.meta.get("video_id"), doc.resource_id, doc.meta.get("playlist_id")
        self.ctx.enqueue("video", {"resource_id": str(res_id), "video_id": vid, "route": route,
                                   "playlist_id": playlist}, ("retry", vid, route, self.round), roadmap_id=self.rid)
        return {"data": {"enqueued": "video", "video_document_id": str(doc_id), "route": route}}, f"retry via {route}"

    def t_replace_url(self, resource_id, url, reason):
        with Session(self.engine) as s:
            res = self._resource(s, resource_id)
            if res is None or not report.is_crawlable(res):
                return {"error": "unknown or non-crawlable resource_id"}, ""
            status = report.current_outcomes(s, self.rid).get(res.id, (res.status,))[0]
        if status not in ("skipped", "partial"):
            return {"error": f"only a skipped or partial resource can be replaced (it is {status})"}, ""
        if safe_url(url) == safe_url(res.url):
            return {"error": "that is the resource's current URL"}, ""
        url = url.strip()
        with session_scope(self.engine) as s:
            res = s.get(Resource, res.id, with_for_update=True)
            s.execute(delete(Document).where(Document.resource_id == res.id, Document.status != "ok"))
            ok_pages = s.execute(select(func.count()).select_from(Document).where(
                Document.resource_id == res.id, Document.kind != "video")).scalar_one()
            meta = dict(res.meta or {})
            meta["replaced"] = [*meta.get("replaced", []), res.url]
            meta["replace_reason"] = str(reason)[:300]
            res.meta, res.url, res.status, res.reason = meta, url, "pending", None
            res.stats = {**(res.stats or {}), "pages": ok_pages}
            queue.enqueue(s, type="ingest_resource", roadmap_id=self.rid, payload={"resource_id": str(res.id)},
                          key=queue.job_key("ingest_resource", self.rid, res.key, "replace", url))
        return {"data": {"replaced": resource_id, "url": wrap(url)}}, f"replaced {resource_id} -> {_host(url)}"

    def t_propose_resource(self, skill_id, url, title, reason):
        url = url.strip()
        with Session(self.engine) as s:
            spec = (s.get(Roadmap, self.rid).spec or {})
            stage = next((st["id"] for st in spec.get("stages", [])
                          if any(k["id"] == skill_id for k in st.get("skills", []))), None)
            if stage is None:
                return {"error": "unknown skill_id"}, ""
            made = s.execute(select(func.count()).select_from(AgentAction).where(
                AgentAction.roadmap_id == self.rid, AgentAction.mode == self.mode, AgentAction.round == self.round,
                AgentAction.tool == "propose_resource", AgentAction.outcome == "ok")).scalar_one()
            if made >= MAX_PROPOSALS:
                return {"error": f"at most {MAX_PROPOSALS} proposals per round"}, ""
            urls = {safe_url(u) for (u,) in s.execute(select(Resource.url).where(Resource.roadmap_id == self.rid))}
            if safe_url(url) in urls:
                return {"error": "that URL is already in the roadmap"}, ""
        key = "agent-" + hashlib.sha1(safe_url(url).encode()).hexdigest()[:10]
        with session_scope(self.engine) as s:
            s.add(Resource(roadmap_id=self.rid, key=key, stage_id=stage, skill_ids=[skill_id],
                           title=(str(title).strip() or url)[:200], kind=_guess_kind(url), url=url,
                           status="proposed", proposed_by="agent", proposal_reason=str(reason)[:500]))
        return {"data": {"proposed": key, "status": "proposed"}}, f"proposed {key} ({_host(url)})"


def make_tools(ctx, roadmap_id, mode: str, *, round: int = 0):
    """-> (tool_defs, call_tool, check_call) for one agent run. Every call, refused or not, is logged."""
    tools = _Tools(ctx, roadmap_id, mode, round)
    return tool_defs(mode), tools.call_tool, tools.check_call


