"""Engine and session helpers."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from atlas.store import models  # noqa: F401  (register tables)


def make_engine(url: str) -> Engine:
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if ":memory:" in url:
        # One shared connection, otherwise every thread (Strands runs tools on worker threads) gets an empty DB.
        kwargs["poolclass"] = StaticPool
    elif url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)
    return engine


def add_missing_columns(engine: Engine) -> list[str]:
    """Tiny forward-only migration: add columns that newer code defines but an existing database lacks.

    create_all() creates missing tables but never alters existing ones. New columns are nullable or have a
    scalar default, so ALTER TABLE ADD COLUMN is safe. Returns the "table.column" names that were added.
    """
    added: list[str] = []
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            existing = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = getattr(col.default, "arg", None)
                if isinstance(default, (bool, int, float, str)):
                    ddl += f" DEFAULT {int(default) if isinstance(default, bool) else repr(default)}"
                conn.exec_driver_sql(ddl)
                added.append(f"{table.name}.{col.name}")
    return added


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    if url is None:
        from atlas.config import get_settings

        url = get_settings().db_url
    return make_engine(url)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    with Session(engine or get_engine(), expire_on_commit=False) as session:
        yield session
