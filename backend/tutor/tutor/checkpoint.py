"""The LangGraph Postgres checkpointer: its URL, its lifecycle and thread deletion."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

CONNECT_TIMEOUT_S = 5


def checkpoint_url(database_url: str) -> str:
    """psycopg wants a plain `postgresql://` URL: drop SQLAlchemy's `+driver` suffix, keep the rest."""
    scheme, sep, rest = database_url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}"


@asynccontextmanager
async def open_saver(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Yield a saver whose tables exist. `setup()` runs here, not in Alembic: LangGraph's own migrations use
    CREATE INDEX CONCURRENTLY, which cannot run inside a transaction. The connection is autocommit, as the
    saver requires (this is what `AsyncPostgresSaver.from_conn_string` builds, plus a connect timeout)."""
    async with await AsyncConnection.connect(
        checkpoint_url(database_url), autocommit=True, prepare_threshold=0, row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_S,
    ) as conn:
        saver = AsyncPostgresSaver(conn)
        await saver.setup()
        yield saver


async def delete_thread(saver: AsyncPostgresSaver, thread_id: str) -> None:
    await saver.adelete_thread(thread_id)
