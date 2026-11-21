"""Propose URLs for roadmap resources that name a title but no link.

The learner reviews every proposal before anything is ingested, so this module only ranks; it never
fetches content for ingestion. Scoring is deterministic. Each candidate scores in [0, 1]:

    score = 0.60 * title_similarity + 0.25 * kind_match + 0.15 * verified_hint

* title_similarity: token-set similarity (difflib) between the candidate's title and the resource
  title. Word order and extra words on one side do not hurt (a title containing every word of the
  other scores 1.0).
* kind_match: video/playlist need a YouTube host (1 or 0); doc needs a host whose name appears in
  the resource title or query, e.g. kubernetes.io for "Kubernetes Official ..." (1 or 0); every
  other kind has no host constraint (1).
* verified_hint: 1 only for the roadmap's own url_hint when fetching it succeeds (outcome ok) and
  the fetched page's <title> has similarity >= 0.5 to the resource title. A hint that does not
  verify is still listed but earns no title or verification credit, so it never beats a decent
  search result.

Status: `resolved` when the best score is >= 0.75, else `needs_url` (candidates still listed);
interactive resources are always `linked_only` (sandboxes are linked, never crawled); an
unavailable search yields `needs_url` with a message instead of an exception."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from backend.tutor.ingest.net import Net, SearchUnavailable
from backend.tutor.roadmap.spec import ResourceSpec, RoadmapSpec

MAX_CANDIDATES = 3
RESOLVED_THRESHOLD = 0.75
HINT_VERIFY_THRESHOLD = 0.5
W_TITLE, W_KIND, W_HINT = 0.6, 0.25, 0.15
SEARCH_UNAVAILABLE = "Search is unavailable; paste a URL"
_YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "youtube-nocookie.com"}
_TITLE_TAG = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class Candidate(BaseModel):
    url: str
    title: str
    source: Literal["url_hint", "search"]
    score: float


class ResolvedResource(BaseModel):
    resource_id: str
    status: Literal["has_url", "resolved", "needs_url", "linked_only"]
    candidates: list[Candidate] = []
    message: str | None = None


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def title_similarity(a: str, b: str) -> float:
    """Token-set ratio: compare the shared words with each side's leftovers, keep the best."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    shared = " ".join(sorted(ta & tb))
    ra = " ".join(filter(None, [shared, *sorted(ta - tb)]))
    rb = " ".join(filter(None, [shared, *sorted(tb - ta)]))
    pairs = [(ra, rb)] + ([(shared, ra), (shared, rb)] if shared else [])
    return max((SequenceMatcher(None, x, y).ratio() for x, y in pairs), default=0.0)


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def _is_youtube(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS)


def kind_match(res: ResourceSpec, url: str) -> float:
    host = _host(url)
    if res.kind in ("video", "playlist"):
        return 1.0 if _is_youtube(host) else 0.0
    if res.kind == "doc":
        labels = host.split(".")
        name = labels[-2] if len(labels) >= 2 else host
        return 1.0 if name in set(_tokens(res.title) + _tokens(res.query)) else 0.0
    return 1.0


def _score(title_sim: float, kind: float, verified: bool) -> float:
    return round(W_TITLE * title_sim + W_KIND * kind + (W_HINT if verified else 0.0), 4)


def _page_title(body: bytes) -> str:
    m = _TITLE_TAG.search(body[:65536])
    return re.sub(r"\s+", " ", m.group(1).decode("utf-8", "replace")).strip() if m else ""


def _hint_candidate(res: ResourceSpec, net: Net) -> Candidate | None:
    if not res.url_hint:
        return None
    fetched = net.fetcher.get(res.url_hint)
    title = _page_title(fetched.body) if fetched.outcome == "ok" else ""
    sim = title_similarity(title, res.title) if title else 0.0
    verified = sim >= HINT_VERIFY_THRESHOLD
    score = _score(sim if verified else 0.0, kind_match(res, res.url_hint), verified)
    return Candidate(url=res.url_hint, title=title or res.url_hint, source="url_hint", score=score)


def _search_candidates(res: ResourceSpec, net: Net) -> list[Candidate]:
    category = "videos" if res.kind in ("video", "playlist") else "general"
    return [
        Candidate(url=r.url, title=r.title, source="search",
                  score=_score(title_similarity(r.title, res.title), kind_match(res, r.url), False))
        for r in net.search.search(res.query, category=category)
    ]


def _top(candidates: list[Candidate]) -> list[Candidate]:
    seen: dict[str, Candidate] = {}
    for c in candidates:  # url_hint comes first, so it wins a tie on the same URL
        seen.setdefault(c.url, c)
    return sorted(seen.values(), key=lambda c: -c.score)[:MAX_CANDIDATES]


def _resolve_one(res: ResourceSpec, net: Net) -> ResolvedResource:
    if res.url:
        return ResolvedResource(resource_id=res.id, status="has_url")
    hint = _hint_candidate(res, net)
    found = [hint] if hint else []
    if net.search is None:
        return ResolvedResource(resource_id=res.id, status="needs_url", candidates=_top(found),
                                message=SEARCH_UNAVAILABLE)
    try:
        found += _search_candidates(res, net)
    except SearchUnavailable:
        return ResolvedResource(resource_id=res.id, status="needs_url", candidates=_top(found),
                                message=SEARCH_UNAVAILABLE)
    candidates = _top(found)
    if res.kind == "interactive":
        status = "linked_only"
    else:
        status = "resolved" if candidates and candidates[0].score >= RESOLVED_THRESHOLD else "needs_url"
    return ResolvedResource(resource_id=res.id, status=status, candidates=candidates)


def resolve_spec(spec: RoadmapSpec, net: Net) -> list[ResolvedResource]:
    return [_resolve_one(r, net) for stage in spec.stages for r in stage.resources]
