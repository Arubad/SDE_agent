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


def test_create_database_if_missing(db_url):
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from backend.tutor.db import create_database_if_missing

    name = f"tutor_create_{uuid.uuid4().hex[:8]}"
    url = make_url(db_url).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(make_url(db_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    exists = text("SELECT 1 FROM pg_database WHERE datname = :n")
    try:
        assert create_database_if_missing(url) is True
        assert create_database_if_missing(url) is False  # idempotent
        with admin.connect() as c:
            assert c.execute(exists, {"n": name}).scalar() == 1
        probe = create_engine(url)
        with probe.connect() as c:
            assert c.execute(text("SELECT 1")).scalar() == 1
        probe.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


DOWN_URL = "postgresql+psycopg://x:y@127.0.0.1:9/z"


def test_engine_has_connect_timeout():
    from sqlalchemy import event

    from backend.tutor.db import make_engine

    eng = make_engine(DOWN_URL)
    seen = {}

    @event.listens_for(eng, "do_connect")
    def _capture(dialect, conn_rec, cargs, cparams):
        seen.update(cparams)
        raise RuntimeError("stop before connecting")

    try:
        with pytest.raises(Exception):
            eng.connect()
    finally:
        eng.dispose()
    assert seen.get("connect_timeout") == 5
    assert eng.pool._pre_ping is True


def test_migration_engine_has_connect_timeout(monkeypatch):
    import sqlalchemy

    from backend.tutor import db

    captured = []
    real = sqlalchemy.create_engine

    def spy(url, **kw):
        captured.append(kw)
        return real(url, **kw)

    monkeypatch.setattr(sqlalchemy, "create_engine", spy)
    monkeypatch.setattr(db, "create_engine", spy)
    with pytest.raises(Exception):
        migrate(DOWN_URL)
    assert captured and all(kw.get("connect_args", {}).get("connect_timeout") == 5 for kw in captured)
