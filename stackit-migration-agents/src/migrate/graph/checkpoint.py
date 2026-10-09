"""Checkpointer factory: SQLite by default, Postgres optional."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from migrate.config import Settings


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[BaseCheckpointSaver]:
    if settings.checkpoint_backend == "postgres":
        if settings.checkpoint_postgres_dsn is None:
            raise RuntimeError(
                "CHECKPOINT_POSTGRES_DSN is required for the postgres backend"
            )
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "Postgres checkpointing needs the extra: uv sync --extra postgres"
            ) from exc
        dsn = settings.checkpoint_postgres_dsn.get_secret_value()
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()
            yield saver
        return

    path = settings.sqlite_path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver
