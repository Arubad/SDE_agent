"""The learner store: consent-gated memory facts, learning progress and skill locks.

One implementation shared by the `learner` MCP server and the tutor's own deterministic code (planner, assessor,
curator, Memory API). Every function is sync and opens its own `session_scope`, so a caller on an event loop
runs it in a thread.

Facts are personal data. Writes (`remember`, `update_fact`) need `consent_personal` and redact before embedding
or storing; reads (`recall`, `list_facts`) return nothing while consent is off. `forget` and `purge_facts` never
need consent: deleting is always allowed. Progress (`skill_progress`) is learning state and is kept whatever the
consent."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import case, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.core.redact import redact
from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import Embedder
from backend.tutor.models import LearnerFact, LearnerSettings, Roadmap, SkillProgress
from backend.tutor.repo import get_roadmap_by_slug

MAX_FACT_CHARS = 1000
DEDUPE_COSINE = 0.9
DEFAULT_UNLOCK = 0.7
CONSENT_OFF = "Personal memory is off. Turn it on in Memory & privacy."


def embedder_name(embedder: Embedder) -> str:
    """The stable name stored with each embedding; searches only compare facts of the same embedder."""
    return f"{type(embedder).__name__}:{embedder.dim}"


def _safe_distance(vec: list[float], name: str):
    """Cosine distance that is only computed for rows of this embedder. SQL gives no evaluation order for
    AND, so the WHERE filter alone cannot keep `<=>` away from vectors of another dimension."""
    return case((LearnerFact.embedder == name, LearnerFact.embedding.cosine_distance(vec)), else_=None)


def get_settings(engine: Engine) -> LearnerSettings | None:
    """The singleton settings row (detached), or None when it does not exist."""
    with session_scope(engine) as s:
        return s.get(LearnerSettings, 1)


def consented(engine: Engine) -> bool:
    """Personal-memory consent. Consent to keep raw messages is a separate field of `get_settings`."""
    settings = get_settings(engine)
    return settings is not None and settings.consent_personal is True


def _roadmap(s: Session, slug: str) -> Roadmap:
    rm = get_roadmap_by_slug(s, slug)
    if rm is None:
        raise ValueError(f"No roadmap '{slug}'")
    return rm


def skills(spec: dict) -> list[dict]:
    """The spec's skills in stage order."""
    if not spec or not spec.get("stages"):
        raise ValueError("The roadmap has no approved spec")
    return [sk for st in spec["stages"] for sk in st.get("skills", [])]


def _roadmap_skills(rm: Roadmap) -> list[dict]:
    if not rm.spec or not rm.spec.get("stages"):
        raise ValueError(f"Roadmap '{rm.slug}' has no approved spec")
    return skills(rm.spec)


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


def _clean(text: str) -> str:
    if len(text) > MAX_FACT_CHARS:
        raise ValueError(f"A memory is at most {MAX_FACT_CHARS} characters")
    return redact(text)


def _require_consent(s: Session) -> None:
    settings = s.get(LearnerSettings, 1)
    if settings is None or settings.consent_personal is not True:
        raise ValueError(CONSENT_OFF)


def _push_history(fact: LearnerFact, now: datetime) -> dict:
    prov = dict(fact.provenance or {})
    prov["history"] = [*prov.get("history", []), {"text": fact.text, "at": now.isoformat()}]
    return prov


def remember(engine: Engine, embedder: Embedder, text: str, kind: str, roadmap: str | None,
             provenance: dict | None = None) -> dict:
    """Store a fact (needs consent). The text is redacted first; a near-identical earlier fact of the same kind
    and roadmap (cosine >= DEDUPE_COSINE) is updated instead, its old text kept in `provenance["history"]`.
    `provenance` is stored on a new fact (default `{"source": "tutor"}`). Returns {"id", "action"}."""
    name = embedder_name(embedder)
    with session_scope(engine) as s:
        _require_consent(s)
        clean = _clean(text)
        rid = _roadmap(s, roadmap).id if roadmap else None
        vec = embedder.embed([clean])[0]
        same = (LearnerFact.kind == kind, LearnerFact.embedder == name,
                LearnerFact.roadmap_id == rid if rid else LearnerFact.roadmap_id.is_(None))
        dist = _safe_distance(vec, name)
        near = s.execute(select(LearnerFact, dist.label("d")).where(*same).order_by(dist).limit(1)).first()
        now = datetime.now(timezone.utc)
        if near is not None and 1 - near.d >= DEDUPE_COSINE:
            fact = near[0]
            fact.provenance = _push_history(fact, now)
            fact.text, fact.embedding, fact.updated_at = clean, vec, now
            return {"id": str(fact.id), "action": "updated"}
        fact = LearnerFact(id=uuid.uuid4(), roadmap_id=rid, kind=kind, text=clean, embedding=vec,
                           embedder=name, provenance=provenance or {"source": "tutor"})
        s.add(fact)
        return {"id": str(fact.id), "action": "inserted"}


def recall(engine: Engine, embedder: Embedder, query: str, top_k: int, roadmap: str | None) -> list[dict]:
    """The facts closest to the query: [{"id", "kind", "text", "score"}]. With a roadmap, its facts and the
    general ones; without, only the general ones. Empty while consent is off."""
    if not consented(engine):
        return []
    name = embedder_name(embedder)
    vec = embedder.embed_query(redact(query))
    with session_scope(engine) as s:
        where = [LearnerFact.embedder == name]
        if roadmap:
            where.append(LearnerFact.roadmap_id.in_([_roadmap(s, roadmap).id]) | LearnerFact.roadmap_id.is_(None))
        else:
            where.append(LearnerFact.roadmap_id.is_(None))
        dist = _safe_distance(vec, name)
        rows = s.execute(select(LearnerFact, dist.label("d")).where(*where).order_by(dist).limit(top_k)).all()
        return [{"id": str(f.id), "kind": f.kind, "text": f.text, "score": 1 - d} for f, d in rows]


def forget(engine: Engine, fact_id: str) -> dict:
    """Delete one fact: {"deleted": bool}. Never needs consent."""
    try:
        fid = uuid.UUID(fact_id)
    except ValueError:
        return {"deleted": False}
    with session_scope(engine) as s:
        return {"deleted": s.execute(delete(LearnerFact).where(LearnerFact.id == fid)).rowcount > 0}


def list_facts(engine: Engine, roadmap: str | None) -> list[dict]:
    """The facts of one roadmap, or the general ones when no roadmap is given. Empty while consent is off."""
    if not consented(engine):
        return []
    with session_scope(engine) as s:
        where = LearnerFact.roadmap_id == _roadmap(s, roadmap).id if roadmap else LearnerFact.roadmap_id.is_(None)
        rows = s.scalars(select(LearnerFact).where(where).order_by(LearnerFact.created_at)).all()
        return [{"id": str(f.id), "kind": f.kind, "text": f.text,
                 "created_at": f.created_at.isoformat(), "updated_at": f.updated_at.isoformat()}
                for f in rows]


def update_fact(engine: Engine, embedder: Embedder, fact_id: str, text: str, kind: str) -> dict:
    """Replace a fact's text and kind (needs consent): capped, redacted, re-embedded with the current embedder,
    the old text appended to `provenance["history"]`. Returns {"id", "action": "updated"}."""
    with session_scope(engine) as s:
        _require_consent(s)
        clean = _clean(text)
        try:
            fid = uuid.UUID(fact_id)
        except ValueError:
            raise ValueError(f"No fact '{fact_id}'") from None
        fact = s.get(LearnerFact, fid)
        if fact is None:
            raise ValueError(f"No fact '{fact_id}'")
        now = datetime.now(timezone.utc)
        fact.provenance = _push_history(fact, now)
        fact.text, fact.kind, fact.updated_at = clean, kind, now
        fact.embedding, fact.embedder = embedder.embed([clean])[0], embedder_name(embedder)
        return {"id": str(fact.id), "action": "updated"}


def purge_facts_in(s: Session) -> int:
    """`purge_facts` inside the caller's transaction, so a purge can commit together with a settings change."""
    return s.execute(delete(LearnerFact)).rowcount


def purge_facts(engine: Engine) -> int:
    """Delete every fact and return how many. It neither checks nor changes consent; the caller does."""
    with session_scope(engine) as s:
        return purge_facts_in(s)


def get_progress(engine: Engine, roadmap: str) -> list[dict]:
    """Every skill of the roadmap in order, with its mastery, weak spots, lock state and next review."""
    with session_scope(engine) as s:
        rm = _roadmap(s, roadmap)
        sks = _roadmap_skills(rm)
        rows = _progress_rows(s, rm)
        return [_entry(sk, rows) for sk in sks]


def set_mastery(engine: Engine, roadmap: str, skill_id: str, mastery: float, weak_spots: list[str] | None,
                next_review_at: datetime | None = None) -> dict:
    """Record mastery (0..1) of a skill; `weak_spots=None` and `next_review_at=None` keep the existing values."""
    with session_scope(engine) as s:
        rm = _roadmap(s, roadmap)
        skill = next((sk for sk in _roadmap_skills(rm) if sk["id"] == skill_id), None)
        if skill is None:
            raise ValueError(f"No skill '{skill_id}' in roadmap '{roadmap}'")
        row = s.get(SkillProgress, (rm.id, skill_id))
        if row is None:
            s.add(SkillProgress(roadmap_id=rm.id, skill_id=skill_id, mastery=mastery,
                                weak_spots=list(weak_spots or []), next_review_at=next_review_at))
        else:
            row.mastery = mastery
            if weak_spots is not None:
                row.weak_spots = list(weak_spots)
            if next_review_at is not None:
                row.next_review_at = next_review_at
            row.updated_at = datetime.now(timezone.utc)
        s.flush()
        return _entry(skill, _progress_rows(s, rm))
