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
    return engine


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
