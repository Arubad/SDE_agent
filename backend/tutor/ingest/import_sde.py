"""Import the demo's SDE knowledge base as the built-in roadmap `sde-mastery`.

The demo's source documents (`reconstruct_documents` + `clean_documents`, read-only) become `Document` rows of
one platform roadmap, are re-chunked by the platform's parent-child chunker and indexed in the platform's own
collection (`make_index(settings, ...)`, never the demo collection). Idempotent: Resource keys and Document ids
derive deterministically from the demo `doc_id`, so a re-run updates in place and `index_document` replaces
each document's sections and points."""
from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy import Engine, select

from backend.rag.documents import clean_documents, reconstruct_documents
from backend.tutor.db import session_scope
from backend.tutor.ingest.index import PlatformIndex, index_document
from backend.tutor.models import Document, Resource, Roadmap
from backend.tutor.roadmap.spec import ResourceSpec, RoadmapSpec, SkillSpec, StageSpec

SLUG = "sde-mastery"
TITLE = "SDE Mastery"
_NS = uuid.UUID("5de00000-0000-4000-8000-000000000014")
SOURCES = {  # source_type -> (stage title, resource kind, document kind)
    "github": ("Open-source repositories", "repo", "repo_file"),
    "book": ("Books", "pdf", "pdf"),
    "web": ("Web articles", "article", "page"),
    "youtube": ("Video lectures", "video", "video"),
}


def document_uuid(demo_doc_id: str) -> uuid.UUID:
    return uuid.uuid5(_NS, demo_doc_id)


def _unique_urls(docs) -> dict[str, str]:
    """Document.url is unique per roadmap, but many demo documents share a url (every page of a book): the
    first in stable order keeps it, later ones get `#<doc_id>` appended. Empty urls always get the suffix."""
    seen, out = set(), {}
    for d in docs:
        url = d.url or ""
        if not url or url in seen:
            url = f"{url}#{d.doc_id}"
        seen.add(url)
        out[d.doc_id] = url
    return out


def _spec(docs) -> RoadmapSpec:
    by_type: dict[str, list] = {}
    for d in docs:
        by_type.setdefault(d.source_type, []).append(d)
    stages = []
    for st in SOURCES:
        if st not in by_type:
            continue
        title, rkind, _ = SOURCES[st]
        skill = f"{st}-knowledge"
        stages.append(StageSpec(
            id=st, title=title, timeframe=None, summary=f"{title} of the original SDE knowledge base.",
            skills=[SkillSpec(id=skill, title=f"{title} knowledge", objectives=[f"Learn from the {title.lower()}"],
                              prerequisites=[])],
            resources=[ResourceSpec(
                id=f"doc-{d.doc_id}", title=d.title or d.doc_id, kind=rkind,
                url=d.url if d.url.startswith("http") else None, url_hint=None, query=d.title or d.doc_id,
                skill_ids=[skill]) for d in by_type[st]],
            practice_goals=[]))
    return RoadmapSpec(title=TITLE, goals=["Prepare for software engineering interviews and work"],
                       weekly_hours=None, stages=stages)


def import_sde(engine: Engine, index: PlatformIndex, *, limit: int | None = None) -> dict:
    """Create or update the roadmap, its resources and documents, then (re)index each document.
    `limit` keeps the first N documents in (source_type, doc_id) order. Returns counts."""
    docs, report = clean_documents(reconstruct_documents())
    docs = sorted(docs, key=lambda d: (d.source_type, d.doc_id))
    if limit is not None:
        docs = docs[:limit]
    urls = _unique_urls(docs)
    for d in docs:
        d.url = urls[d.doc_id]
    spec = _spec(docs)
    keys = {f"doc-{d.doc_id}" for d in docs}
    with session_scope(engine) as s:
        rm = s.scalar(select(Roadmap).where(Roadmap.slug == SLUG))
        if rm is None:
            rm = Roadmap(slug=SLUG, title=TITLE, source_text="Imported from the demo SDE knowledge base.")
            s.add(rm)
        rm.spec, rm.status = spec.model_dump(mode="json"), "ready"
        s.flush()
        rid = rm.id
        existing = {r.key: r for r in s.scalars(select(Resource).where(Resource.roadmap_id == rid))}
        stale = [r for k, r in existing.items() if k not in keys]
        for r in stale:
            s.delete(r)  # documents and sections cascade
        s.flush()
        docs_by_id = {d.id: d for d in s.scalars(select(Document).where(Document.roadmap_id == rid))}
        rows, skills = [], {}
        for st in spec.stages:
            for rs in st.resources:
                skills[rs.id] = rs.skill_ids
        for d in docs:
            key, uid = f"doc-{d.doc_id}", document_uuid(d.doc_id)
            res = existing.get(key) or Resource(roadmap_id=rid, key=key)
            res.stage_id, res.skill_ids, res.title = d.source_type, skills[key], d.title or d.doc_id
            res.kind, res.url = SOURCES[d.source_type][1], d.url if d.url.startswith("http") else None
            res.status = "done"
            s.add(res)
            s.flush()
            row = docs_by_id.get(uid) or Document(id=uid, roadmap_id=rid)
            row.resource_id, row.url, row.kind = res.id, d.url, SOURCES[d.source_type][2]
            row.title, row.markdown, row.status = d.title or d.doc_id, d.text, "ok"
            row.meta = {"source_type": d.source_type, "demo_doc_id": d.doc_id}
            s.add(row)
            rows.append((row, skills[key]))
        s.flush()
        s.expunge_all()
    if stale:  # drop the points of removed documents; the rest is re-indexed below
        index.delete_roadmap(str(rid))
    children, per_doc = 0, {}
    for row, skill_ids in rows:
        per_doc[row.resource_id] = index_document(engine, index, row, skill_ids)
        children += per_doc[row.resource_id]
    with session_scope(engine) as s:
        for resource_id, n in per_doc.items():
            s.get(Resource, resource_id).stats = {"chunks": n}
    return {"roadmap_id": str(rid), "slug": SLUG, "documents": len(rows), "resources": len(rows),
            "stages": len(spec.stages), "skills": sum(len(st.skills) for st in spec.stages), "children": children,
            "by_source_type": dict(Counter(d.source_type for d in docs)), "cleaning": report,
            "removed_stale": len(stale)}
