from __future__ import annotations

import sqlite3

import pytest

from helpers.plan import schema_sql_v1
from lanscape_atlas.store import db
from lanscape_atlas.store.db import migrate, open_db


def test_fresh_database_reaches_v4(tmp_path):
    conn = open_db(tmp_path / "a.db")
    assert migrate(conn) == 4
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
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
    assert migrate(conn) == 4
    row = conn.execute("SELECT roaming FROM device").fetchone()
    assert row[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_migrate_is_idempotent_at_v4(tmp_path):
    conn = open_db(tmp_path / "e.db")
    assert migrate(conn) == 4
    assert migrate(conn) == 4
    assert conn.execute("SELECT count(*) FROM ble_salt").fetchone()[0] == 1


def test_newer_database_is_refused(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute("PRAGMA user_version = 5")
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


BLE_TABLES = ("ble_identity", "ble_scanner", "ble_sighting")


def _v2_db(path):
    conn = open_db(path)
    conn.executescript(schema_sql_v1())
    conn.executescript(db._load_migration(2))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    return conn


def test_v2_to_v3_keeps_data(tmp_path):
    conn = _v2_db(tmp_path / "f.db")
    conn.execute("INSERT INTO place(name) VALUES('Room A')")
    conn.execute(
        "INSERT INTO device(first_seen,last_seen) "
        "VALUES('2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')"
    )
    conn.commit()
    assert migrate(conn) == 4
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert conn.execute("SELECT name FROM place").fetchall()[0][0] == "Room A"
    assert conn.execute("SELECT count(*) FROM device").fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(BLE_TABLES) <= names


def test_fresh_equals_upgraded_v3(tmp_path):
    fresh = open_db(tmp_path / "g.db")
    migrate(fresh)
    upgraded = _v2_db(tmp_path / "h.db")
    migrate(upgraded)
    q = (
        "SELECT name, sql FROM sqlite_master WHERE name IN "
        "('ble_identity','ble_scanner','ble_sighting') "
        "OR tbl_name IN ('ble_identity','ble_scanner','ble_sighting') ORDER BY name"
    )
    got = fresh.execute(q).fetchall()
    assert got == upgraded.execute(q).fetchall()
    assert {r[0] for r in got} >= set(BLE_TABLES)


def test_sighting_cascades_with_device(db, make_device):
    dev = make_device()
    db.execute("PRAGMA foreign_keys = ON")
    with db:
        db.execute(
            "INSERT INTO ble_scanner(source,first_seen,last_seen) "
            "VALUES('00:00:5e:00:53:01','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
        db.execute(
            "INSERT INTO ble_sighting VALUES"
            "(?,1,'2026-01-01T00:00Z',NULL,-60,3,'2026-01-01T00:00:10Z')",
            (dev,),
        )
    assert db.execute("SELECT count(*) FROM ble_sighting").fetchone()[0] == 1
    with db:
        db.execute("DELETE FROM device WHERE id=?", (dev,))
    assert db.execute("SELECT count(*) FROM ble_sighting").fetchone()[0] == 0


HIDDEN_TABLES = ("ble_salt", "ble_hidden")


def _v3_db(path):
    conn = _v2_db(path)
    conn.executescript(db._load_migration(3))
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    return conn


def test_v3_to_v4_keeps_data_and_creates_one_salt(tmp_path):
    conn = _v3_db(tmp_path / "i.db")
    conn.execute(
        "INSERT INTO device(first_seen,last_seen) "
        "VALUES('2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ble_identity(device_id,kind,value,created_at) "
        "VALUES(1,'address','00:00:5e:00:53:01','2026-10-01T00:00:00Z')"
    )
    conn.commit()
    assert migrate(conn) == 4
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM device").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM ble_identity").fetchone()[0] == "00:00:5e:00:53:01"
    salts = conn.execute("SELECT id, salt FROM ble_salt").fetchall()
    assert len(salts) == 1 and salts[0][0] == 1
    assert isinstance(salts[0][1], bytes) and len(salts[0][1]) == 32
    assert conn.execute("SELECT count(*) FROM ble_hidden").fetchone()[0] == 0
    assert migrate(conn) == 4  # idempotent: still one salt
    assert conn.execute("SELECT count(*) FROM ble_salt").fetchone()[0] == 1


def test_salt_is_single_row_and_random(tmp_path):
    a = open_db(tmp_path / "j.db")
    b = open_db(tmp_path / "k.db")
    migrate(a)
    migrate(b)
    salt = a.execute("SELECT salt FROM ble_salt").fetchone()[0]
    assert salt != b.execute("SELECT salt FROM ble_salt").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        a.execute("INSERT INTO ble_salt VALUES (2, x'00')")


def test_hidden_digest_is_unique(db):
    db.execute("INSERT INTO ble_hidden(digest,label,hidden_at) VALUES('ab','Tag','t')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO ble_hidden(digest,label,hidden_at) VALUES('ab','Other','t')")


def test_fresh_equals_upgraded_v4(tmp_path):
    fresh = open_db(tmp_path / "l.db")
    migrate(fresh)
    upgraded = _v3_db(tmp_path / "m.db")
    migrate(upgraded)
    q = (
        "SELECT name, sql FROM sqlite_master WHERE tbl_name IN ('ble_salt','ble_hidden')"
        " ORDER BY name"
    )
    got = fresh.execute(q).fetchall()
    assert got == upgraded.execute(q).fetchall()
    assert {r[0] for r in got} >= set(HIDDEN_TABLES)
