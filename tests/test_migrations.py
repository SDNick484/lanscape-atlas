from __future__ import annotations

import sqlite3

import pytest

from helpers.plan import schema_sql_v1
from lanscape_atlas.store import db
from lanscape_atlas.store.db import migrate, open_db


def test_fresh_database_reaches_v2(tmp_path):
    conn = open_db(tmp_path / "a.db")
    assert migrate(conn) == 2
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='floor_plan'").fetchone()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(device)")}
    assert "roaming" in cols


def test_v1_database_upgrades_and_keeps_rows(tmp_path):
    conn = open_db(tmp_path / "b.db")
    conn.executescript(schema_sql_v1())
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO device(first_seen,last_seen) "
        "VALUES('2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')"
    )
    conn.commit()
    assert migrate(conn) == 2
    row = conn.execute("SELECT roaming FROM device").fetchone()
    assert row[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_migrate_is_idempotent_at_v2(tmp_path):
    conn = open_db(tmp_path / "e.db")
    assert migrate(conn) == 2
    assert migrate(conn) == 2


def test_newer_database_is_refused(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute("PRAGMA user_version = 3")
    with pytest.raises(RuntimeError, match="newer than supported"):
        migrate(conn)


def test_failed_migration_leaves_version_at_1(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "d.db")
    conn.executescript(schema_sql_v1())
    conn.execute("PRAGMA user_version = 1")
    monkeypatch.setattr(db, "_load_migration", lambda n: "ALTER TABLE nope ADD COLUMN x INT;")
    with pytest.raises(sqlite3.Error):
        migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert not conn.in_transaction


def test_load_migration_returns_sql():
    assert "floor_plan" in db._load_migration(2)
