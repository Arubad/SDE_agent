from alembic import context
from sqlalchemy import create_engine

from backend.tutor.db import CONNECT_ARGS
from backend.tutor.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(context.config.get_main_option("sqlalchemy.url"), connect_args=dict(CONNECT_ARGS))
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
