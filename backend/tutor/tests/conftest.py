import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session")
def db_url():
    server_url = os.environ.get("DATABASE_URL")
    if not server_url:
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail("REQUIRE_DB=1 but DATABASE_URL is not set")
        pytest.skip("DATABASE_URL is not set")
    from backend.tutor.db import migrate

    name = f"tutor_test_{uuid.uuid4().hex[:8]}"
    admin_url = make_url(server_url).set(database="postgres")
    test_url = admin_url.set(database=name).render_as_string(hide_password=False)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            migrate(test_url)
            yield test_url
        finally:
            with admin.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :n AND pid <> pg_backend_pid()"
                    ),
                    {"n": name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine(db_url):
    from backend.tutor.db import make_engine

    eng = make_engine(db_url)
    yield eng
    eng.dispose()
