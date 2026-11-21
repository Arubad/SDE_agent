"""Roadmap review/approve service: parse + resolve, then approve into Resource rows and ingest jobs."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.tutor.jobs import queue
from backend.tutor.models import Resource, Roadmap
from backend.tutor.roadmap.parse import normalize_spec, parse_roadmap
from backend.tutor.roadmap.resolve import resolve_spec
from backend.tutor.roadmap.spec import RoadmapSpec

TERMINAL_STATUSES = ("ready", "ready_with_gaps", "failed")
PARSE_STATUSES = ("draft", "parsed")


class Conflict(Exception):
    """The roadmap is in a state that does not allow this operation (HTTP 409)."""


def parse_and_resolve(session: Session, net, roadmap: Roadmap) -> dict:
    if roadmap.status not in PARSE_STATUSES:
        raise Conflict(f"Roadmap is '{roadmap.status}'; it can only be parsed while draft or parsed.")
    spec = parse_roadmap(roadmap.source_text)
    resolution = resolve_spec(spec, net)
    roadmap.spec = spec.model_dump(mode="json")
    roadmap.status = "parsed"
    session.flush()
    return {"spec": roadmap.spec, "resolution": [r.model_dump(mode="json") for r in resolution]}


def update_spec(session: Session, roadmap: Roadmap, raw_spec: dict) -> dict:
    if roadmap.status != "parsed":
        raise Conflict(f"Roadmap is '{roadmap.status}'; the spec can only be edited while parsed.")
    roadmap.spec = normalize_spec(raw_spec, roadmap.source_text).model_dump(mode="json")
    session.flush()
    return roadmap.spec


def validate_crawl_url(url: str) -> None:
    """Raise ValueError unless `url` is an http(s) URL the crawler may fetch.

    Only IP literals and `localhost` are checked: no DNS happens in the API. A hostname that resolves to a
    private address (or is rebound after this check) is NOT caught here; the fetcher must enforce that at connect
    time."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http(s) URLs are allowed: {url!r}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(f"URL points at a local address: {url!r}")
    addr = None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:  # legacy forms the OS resolver also accepts: 2130706433, 0x7f.1, 127.1 (no DNS involved)
            addr = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return  # an ordinary hostname
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if (addr.is_loopback or addr.is_link_local or addr.is_private or addr.is_reserved or addr.is_multicast
            or addr.is_unspecified):
        raise ValueError(f"URL points at a private or reserved address: {url!r}")


def approve(session: Session, roadmap: Roadmap, spec: RoadmapSpec, choices: dict[str, str | None]) -> None:
    if roadmap.status != "parsed":
        raise Conflict(f"Roadmap is '{roadmap.status}'; it can only be approved while parsed.")
    resources = [(stage.id, r) for stage in spec.stages for r in stage.resources]
    known = {r.id for _, r in resources}
    unknown = sorted(set(choices) - known)
    if unknown:
        raise ValueError(f"Unknown resource id(s) in choices: {', '.join(unknown)}")
    # An explicit None skips the resource; an absent key falls back to the URL given in the source text.
    chosen = {r.id: (choices[r.id] if r.id in choices else r.url) for _, r in resources}
    for url in chosen.values():
        if url:
            validate_crawl_url(url)

    roadmap.spec = spec.model_dump(mode="json")
    rows = []
    for stage_id, r in resources:
        url = chosen[r.id] or None
        if not url:
            status, reason = "skipped", "no_url"
        elif r.kind == "interactive":
            status, reason = "linked_only", None
        else:
            status, reason = "pending", None
        rows.append(dict(roadmap_id=roadmap.id, key=r.id, stage_id=stage_id, skill_ids=r.skill_ids, title=r.title,
                         kind=r.kind, url=url, focus=r.focus, status=status, reason=reason))
    if rows:
        session.execute(pg_insert(Resource).values(rows).on_conflict_do_nothing(index_elements=["roadmap_id", "key"]))
    stored = session.scalars(select(Resource).where(Resource.roadmap_id == roadmap.id)
                             .execution_options(populate_existing=True)).all()
    for res in stored:
        if res.url and res.kind != "interactive" and res.status == "pending":
            queue.enqueue(session, type="ingest_resource", roadmap_id=roadmap.id,
                          payload={"resource_id": str(res.id)},
                          key=queue.job_key("ingest_resource", roadmap.id, res.key))
    roadmap.status = "ingesting"
    session.flush()


def progress(session: Session, roadmap: Roadmap) -> dict:
    session.refresh(roadmap)
    res = session.scalars(select(Resource).where(Resource.roadmap_id == roadmap.id).order_by(Resource.key)).all()
    return {
        "jobs": queue.roadmap_job_counts(session, roadmap.id),
        "resources": [{"key": r.key, "title": r.title, "kind": r.kind, "url": r.url, "status": r.status,
                       "reason": r.reason, "stats": r.stats} for r in res],
        "status": roadmap.status,
    }
