import pytest
from sqlalchemy import inspect, text

from backend.tutor.db import downgrade, migrate, session_scope
from backend.tutor.repo import DuplicateRoadmap, create_roadmap, list_roadmaps


@pytest.fixture
def session(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
    with session_scope(engine) as s:
        yield s


def test_migration_creates_schema(db_url, engine):
    with engine.connect() as c:
        assert c.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar() == 1
    assert inspect(engine).has_table("roadmaps")
    downgrade(db_url, "base")
    assert not inspect(engine).has_table("roadmaps")
    migrate(db_url)
    assert inspect(engine).has_table("roadmaps")


def test_create_and_list_roadmaps(session):
    a = create_roadmap(session, slug="first", title="First", source_text="a")
    session.commit()  # now() is per-transaction, so commit to get distinct timestamps
    b = create_roadmap(session, slug="second", title="Second", source_text="b")
    session.commit()
    assert [r.slug for r in list_roadmaps(session)] == ["second", "first"]
    assert a.status == "draft" and b.id != a.id


def test_duplicate_slug(session):
    create_roadmap(session, slug="dup", title="One", source_text="a")
    session.commit()
    with pytest.raises(DuplicateRoadmap):
        create_roadmap(session, slug="dup", title="Two", source_text="b")
    assert len(list_roadmaps(session)) == 1  # session still usable


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slug": "Bad Slug"},
        {"slug": "a"},
        {"title": "  "},
        {"source_text": "x" * 200_001},
    ],
)
def test_validation(session, kwargs):
    args = {"slug": "ok-slug", "title": "T", "source_text": "s"} | kwargs
    with pytest.raises(ValueError):
        create_roadmap(session, **args)


def test_large_emoji_source_accepted(session):
    r = create_roadmap(session, slug="emoji", title="E", source_text="😀" * 200_000)
    assert len(r.source_text) == 200_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "bad\x00title"},
        {"source_text": "bad\x00text"},
        {"title": "lone \ud800 surrogate"},
        {"source_text": "lone \ud800 surrogate"},
    ],
)
def test_unstorable_characters_rejected_and_session_usable(session, kwargs):
    args = {"slug": "ok-slug", "title": "T", "source_text": "s"} | kwargs
    with pytest.raises(ValueError):
        create_roadmap(session, **args)
    r = create_roadmap(session, slug="after-bad", title="Fine", source_text="fine")
    assert r.slug == "after-bad"
