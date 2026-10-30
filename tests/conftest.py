"""Shared fixtures: migrated temp DB, fixed clock, event bus, Service, device factory."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import pytest

from lanscape_atlas.clock import iso
from lanscape_atlas.events import EventBus
from lanscape_atlas.ingest import Ingestor
from lanscape_atlas.oui import OuiDB
from lanscape_atlas.service import Service
from lanscape_atlas.store.db import migrate, open_db


class FixedClock:
    """Callable returning a tz-aware UTC datetime that only moves when advanced."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "atlas.db")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))


@pytest.fixture
def events() -> EventBus:
    return EventBus(record=True)


@pytest.fixture
def svc(db: sqlite3.Connection, events: EventBus, clock: FixedClock) -> Service:
    return Service(db, events, clock)


@pytest.fixture
def make_device(db: sqlite3.Connection, clock: FixedClock) -> Callable[..., int]:
    """Insert a device row directly (bypassing Service); kwargs override columns."""

    def _make(**overrides: Any) -> int:
        now = iso(clock())
        row: dict[str, Any] = {"first_seen": now, "last_seen": now, "trust": "new"}
        row.update(overrides)
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        with db:
            cur = db.execute(f"insert into device({cols}) values({marks})", tuple(row.values()))
        assert cur.lastrowid is not None
        return cur.lastrowid

    return _make


# --------------------------------------------------------------- ingest helpers

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def dump_state(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """All rows of every table except ``observation`` (and sqlite internals), sorted."""
    names = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type = 'table'"
            " and name not like 'sqlite_%' and name != 'observation' order by name"
        )
    ]
    return {
        n: sorted((tuple(r) for r in conn.execute(f"select * from {n}")), key=repr) for n in names
    }


@pytest.fixture
def bus(events: EventBus) -> EventBus:
    """Alias for the recording ``events`` fixture."""
    return events


@pytest.fixture
def oui() -> OuiDB:
    with as_file(files("lanscape_atlas") / "data" / "oui_sample.csv") as p:
        return OuiDB.load(p)


@pytest.fixture
def ing(
    db: sqlite3.Connection, svc: Service, events: EventBus, oui: OuiDB, clock: FixedClock
) -> Ingestor:
    return Ingestor(db, svc, events, oui, clock)
