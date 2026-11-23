"""Own `learner` MCP server: consent-gated memory facts, learning progress and skill locks.

Facts are personal data: `remember` needs the learner's consent and redacts before embedding or storing.
Progress (`skill_progress`) is learning state and is kept whatever the consent."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import anyio.to_thread
from fastmcp import FastMCP
from pydantic import Field
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.core.redact import redact
from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import Embedder
from backend.tutor.models import LearnerFact, LearnerSettings, Roadmap, SkillProgress
from backend.tutor.repo import get_roadmap_by_slug

DEDUPE_COSINE = 0.9
DEFAULT_UNLOCK = 0.7
CONSENT_OFF = "Personal memory is off. Turn it on in Memory & privacy."


def embedder_name(embedder: Embedder) -> str:
    """The stable name stored with each embedding; searches only compare facts of the same embedder."""
    return f"{type(embedder).__name__}:{embedder.dim}"


def _roadmap(s: Session, slug: str) -> Roadmap:
    rm = get_roadmap_by_slug(s, slug)
    if rm is None:
        raise ValueError(f"No roadmap '{slug}'")
    return rm


def _skills(rm: Roadmap) -> list[dict]:
    spec = rm.spec
    if not spec or not spec.get("stages"):
        raise ValueError(f"Roadmap '{rm.slug}' has no approved spec")
    return [sk for st in spec["stages"] for sk in st.get("skills", [])]


def _entry(skill: dict, rows: dict[str, SkillProgress]) -> dict:
    row = rows.get(skill["id"])
    threshold = (row.unlock_threshold if row else None) or DEFAULT_UNLOCK
    locked = any(
        (rows[p].mastery if p in rows else 0.0) < threshold for p in skill.get("prerequisites", [])
    )
    return {
        "skill_id": skill["id"],
        "title": skill["title"],
        "mastery": row.mastery if row else 0.0,
        "weak_spots": list(row.weak_spots or []) if row else [],
        "locked": locked,
        "next_review_at": row.next_review_at.isoformat() if row and row.next_review_at else None,
    }


def _progress_rows(s: Session, rm: Roadmap) -> dict[str, SkillProgress]:
    rows = s.scalars(select(SkillProgress).where(SkillProgress.roadmap_id == rm.id)).all()
    return {r.skill_id: r for r in rows}


def build_learner_server(engine: Engine, embedder: Embedder) -> FastMCP:
    server = FastMCP("learner")
    name = embedder_name(embedder)

    def _remember(text: str, kind: str, roadmap: str | None) -> dict:
        with session_scope(engine) as s:
            settings = s.get(LearnerSettings, 1)
            if settings is None or settings.consent_personal is not True:
                raise ValueError(CONSENT_OFF)
            rid = _roadmap(s, roadmap).id if roadmap else None
            clean = redact(text)
            vec = embedder.embed([clean])[0]
            same = (LearnerFact.kind == kind, LearnerFact.embedder == name,
                    LearnerFact.roadmap_id == rid if rid else LearnerFact.roadmap_id.is_(None))
            dist = LearnerFact.embedding.cosine_distance(vec)
            near = s.execute(
                select(LearnerFact, dist.label("d")).where(*same).order_by(dist).limit(1)
            ).first()
            now = datetime.now(timezone.utc)
            if near is not None and 1 - near.d >= DEDUPE_COSINE:
                fact = near[0]
                prov = dict(fact.provenance or {})
                prov["history"] = [*prov.get("history", []), {"text": fact.text, "at": now.isoformat()}]
                fact.text, fact.embedding, fact.provenance, fact.updated_at = clean, vec, prov, now
                return {"id": str(fact.id), "action": "updated"}
            fact = LearnerFact(id=uuid.uuid4(), roadmap_id=rid, kind=kind, text=clean, embedding=vec,
                               embedder=name, provenance={"source": "tutor"})
            s.add(fact)
            return {"id": str(fact.id), "action": "inserted"}

    def _recall(query: str, top_k: int, roadmap: str | None) -> list[dict]:
        vec = embedder.embed_query(redact(query))
        with session_scope(engine) as s:
            where = [LearnerFact.embedder == name]
            if roadmap:
                where.append(LearnerFact.roadmap_id.in_([_roadmap(s, roadmap).id]) | LearnerFact.roadmap_id.is_(None))
            else:
                where.append(LearnerFact.roadmap_id.is_(None))
            dist = LearnerFact.embedding.cosine_distance(vec)
            rows = s.execute(
                select(LearnerFact, dist.label("d")).where(*where).order_by(dist).limit(top_k)
            ).all()
            return [{"id": str(f.id), "kind": f.kind, "text": f.text, "score": 1 - d} for f, d in rows]

    def _forget(fact_id: str) -> dict:
        try:
            fid = uuid.UUID(fact_id)
        except ValueError:
            return {"deleted": False}
        with session_scope(engine) as s:
            return {"deleted": s.execute(delete(LearnerFact).where(LearnerFact.id == fid)).rowcount > 0}

    def _list(roadmap: str | None) -> list[dict]:
        with session_scope(engine) as s:
            where = LearnerFact.roadmap_id == _roadmap(s, roadmap).id if roadmap else LearnerFact.roadmap_id.is_(None)
            rows = s.scalars(select(LearnerFact).where(where).order_by(LearnerFact.created_at)).all()
            return [{"id": str(f.id), "kind": f.kind, "text": f.text,
                     "created_at": f.created_at.isoformat(), "updated_at": f.updated_at.isoformat()}
                    for f in rows]

    def _get_progress(roadmap: str) -> list[dict]:
        with session_scope(engine) as s:
            rm = _roadmap(s, roadmap)
            skills = _skills(rm)
            rows = _progress_rows(s, rm)
            return [_entry(sk, rows) for sk in skills]

    def _set_mastery(roadmap: str, skill_id: str, mastery: float, weak_spots: list[str] | None) -> dict:
        with session_scope(engine) as s:
            rm = _roadmap(s, roadmap)
            skill = next((sk for sk in _skills(rm) if sk["id"] == skill_id), None)
            if skill is None:
                raise ValueError(f"No skill '{skill_id}' in roadmap '{roadmap}'")
            row = s.get(SkillProgress, (rm.id, skill_id))
            if row is None:
                s.add(SkillProgress(roadmap_id=rm.id, skill_id=skill_id, mastery=mastery,
                                    weak_spots=list(weak_spots or [])))
            else:
                row.mastery = mastery
                if weak_spots is not None:
                    row.weak_spots = list(weak_spots)
                row.updated_at = datetime.now(timezone.utc)
            s.flush()
            return _entry(skill, _progress_rows(s, rm))

    @server.tool
    async def remember(
        text: str,
        kind: Literal["fact", "preference", "goal"] = "fact",
        roadmap: str | None = None,
    ) -> dict:
        """Remember something about the learner (needs their consent). The text is redacted first; a
        near-identical earlier memory is updated instead of duplicated. Returns {"id", "action"}."""
        return await anyio.to_thread.run_sync(_remember, text, kind, roadmap)

    @server.tool
    async def recall(
        query: str,
        top_k: Annotated[int, Field(ge=1, le=20)] = 5,
        roadmap: str | None = None,
    ) -> list[dict]:
        """Find remembered facts closest to the query: [{"id", "kind", "text", "score"}]. With a roadmap,
        its facts and the general ones; without, only the general ones."""
        return await anyio.to_thread.run_sync(_recall, query, top_k, roadmap)

    @server.tool
    async def forget(fact_id: str) -> dict:
        """Permanently delete one remembered fact: {"deleted": bool}."""
        return await anyio.to_thread.run_sync(_forget, fact_id)

    @server.tool
    async def list_facts(roadmap: str | None = None) -> list[dict]:
        """List remembered facts of one roadmap, or the general ones when no roadmap is given."""
        return await anyio.to_thread.run_sync(_list, roadmap)

    @server.tool
    async def get_progress(roadmap: str) -> list[dict]:
        """Every skill of the roadmap in order: {"skill_id", "title", "mastery", "weak_spots", "locked",
        "next_review_at"}. A skill is locked while a prerequisite's mastery is below its unlock threshold."""
        return await anyio.to_thread.run_sync(_get_progress, roadmap)

    @server.tool
    async def set_mastery(
        roadmap: str,
        skill_id: str,
        mastery: Annotated[float, Field(ge=0, le=1)],
        weak_spots: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record the learner's mastery (0..1) of a skill; weak_spots=None keeps the existing ones."""
        return await anyio.to_thread.run_sync(_set_mastery, roadmap, skill_id, mastery, weak_spots)

    return server
