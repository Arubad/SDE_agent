"""SQLAlchemy models for the tutor platform."""
from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Roadmap(Base):
    """`status`: draft -> parsed -> ingesting -> finalizing -> ready | ready_with_gaps | failed.
    `spec` holds the RoadmapSpec JSON."""

    __tablename__ = "roadmaps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _roadmap_fk() -> ForeignKey:
    return ForeignKey("roadmaps.id", ondelete="CASCADE")


class Resource(Base):
    """A resource from the roadmap spec.

    `kind`: video|playlist|doc|article|course_site|interactive|pdf|repo|other.
    `status` is free text, no DB constraint; values include pending, ingesting, done, partial, linked_only,
    skipped, failed, and for suggestions `proposed` (awaiting the user) and `dismissed`.
    `proposed_by` is `agent` or `user` for suggested resources (None for the roadmap's own); `meta.replaced`
    lists URLs the recovery agent replaced.
    """

    __tablename__ = "resources"
    __table_args__ = (UniqueConstraint("roadmap_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    roadmap_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _roadmap_fk(), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)  # the resource id in the spec
    stage_id: Mapped[str] = mapped_column(Text, nullable=False)
    skill_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    focus: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")  # pages, videos, chunks
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    proposed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Job(Base):
    """A durable queue job. `status` (free text): queued|running|done|failed|dead|superseded (a dead job whose
    resource URL the recovery agent replaced; terminal, not reported). `key` is unique (idempotency)."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_not_before", "status", "not_before"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    roadmap_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _roadmap_fk(), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued", server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("jobs.id"), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Document(Base):
    """A fetched document. `kind`: page|video|pdf|repo_file. `status` (free text): ok, skipped, failed,
    linked_only, ... (later tasks may add more); `reason` explains non-ok states."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("roadmap_id", "url"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    roadmap_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _roadmap_fk(), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    markdown: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ok", server_default="ok")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Section(Base):
    """A retrievable chunk of a document. `id` is a stable text id; `path` is the heading breadcrumb;
    `start_s`/`end_s` are set for video transcripts."""

    __tablename__ = "sections"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    roadmap_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _roadmap_fk(), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    skill_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    start_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    ord: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class HttpCache(Base):
    __tablename__ = "http_cache"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentAction(Base):
    """One ingestion-agent tool call (or, with `tool="run"`, one run: `args` = {stop, steps, actions}, `summary`
    = the final notes). `mode`: plan|recover; `outcome`: ok|refused|error. `cost_usd` holds the run's model cost on
    its `run` row, so the per-roadmap budget is the sum over the roadmap's rows."""

    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    roadmap_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _roadmap_fk(), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class McpServerState(Base):
    """A per-server on/off override; no row means the registry's `enabled_default` applies."""

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LearnerSettings(Base):
    """Singleton row (id=1). `consent_*` null means the learner has not been asked yet."""

    __tablename__ = "learner_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consent_personal: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    consent_raw_messages: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90, server_default="90")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SkillProgress(Base):
    """Per-skill learning progress; always kept, whatever the consent."""

    __tablename__ = "skill_progress"
    __table_args__ = (CheckConstraint("mastery >= 0 AND mastery <= 1", name="ck_skill_progress_mastery"),)

    roadmap_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _roadmap_fk(), primary_key=True)
    skill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    mastery: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    weak_spots: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlock_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LearnerFact(Base):
    """A semantic fact about the learner. `roadmap_id` null = about the learner in general. `embedding` has no
    fixed dimension; `embedder` names the model that made it, and searches only compare the same embedder."""

    __tablename__ = "learner_facts"
    __table_args__ = (CheckConstraint("kind IN ('fact', 'preference', 'goal')", name="ck_learner_facts_kind"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    roadmap_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), _roadmap_fk(), nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(), nullable=False)
    embedder: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class McpIdempotency(Base):
    """A claimed MCP write. `result` is null while the call runs; a failed call deletes its claim."""

    __tablename__ = "mcp_idempotency"

    tool: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    args_hash: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
