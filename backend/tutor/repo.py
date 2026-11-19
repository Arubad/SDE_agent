"""Roadmap persistence. ValueError messages are shown to users by the API."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.tutor.models import Roadmap

SLUG_RE = r"^[a-z0-9][a-z0-9-]{1,62}$"
MAX_SOURCE_CHARS = 200_000


class DuplicateRoadmap(Exception):
    pass


def create_roadmap(session: Session, *, slug: str, title: str, source_text: str) -> Roadmap:
    if not re.fullmatch(SLUG_RE, slug):
        raise ValueError(
            "Slug must be 2-63 characters: lowercase letters, digits and hyphens, "
            "starting with a letter or digit."
        )
    if not title or not title.strip():
        raise ValueError("Title must not be empty.")
    if len(source_text) > MAX_SOURCE_CHARS:
        raise ValueError(
            f"Source text is too long: {len(source_text):,} characters (the limit is {MAX_SOURCE_CHARS:,})."
        )
    for label, value in (("Title", title), ("Source text", source_text)):
        if "\x00" in value:
            raise ValueError(f"{label} must not contain NUL (null) characters.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(f"{label} contains characters that cannot be stored as valid text.") from None
    roadmap = Roadmap(slug=slug, title=title.strip(), source_text=source_text)
    session.add(roadmap)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateRoadmap(f"A roadmap with the slug '{slug}' already exists.") from exc
    return roadmap


def list_roadmaps(session: Session) -> list[Roadmap]:
    return list(session.scalars(select(Roadmap).order_by(Roadmap.created_at.desc(), Roadmap.id)))
