from __future__ import annotations

import sqlite3
from importlib import resources

import pytest

from lanscape_atlas.store.db import migrate, open_db

MAC = "00:00:5e:00:53:01"


def _device(conn: sqlite3.Connection, name: str = "d") -> int:
    cur = conn.execute("insert into device(name,first_seen,last_seen) values(?,'t','t')", (name,))
    assert cur.lastrowid is not None
    return cur.lastrowid


def _iface(conn: sqlite3.Connection, dev: int, address: str = MAC, kind: str = "wifi") -> int:
    cur = conn.execute(
        "insert into interface(device_id,kind,address,is_random,first_seen,last_seen)"
        " values(?,?,?,0,'t','t')",
        (dev, kind, address),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_migrate_creates_tables(tmp_path):
    conn = open_db(tmp_path / "a.db")
    assert migrate(conn) == 2
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {
        "device",
        "interface",
        "ip_lease",
        "service",
        "place",
        "link",
        "observation",
        "field_source",
        "change",
        "batch",
        "topology_layout",
        "collector_status",
    } <= names


def test_migrate_idempotent(tmp_path):
    conn = open_db(tmp_path / "a.db")
    migrate(conn)
    _device(conn)
    assert migrate(conn) == 2
    assert conn.execute("select count(*) from device").fetchone()[0] == 1


def test_wal_and_fk(tmp_path):
    conn = open_db(tmp_path / "a.db")
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1
    assert conn.execute("pragma busy_timeout").fetchone()[0] == 5000
    assert conn.row_factory is sqlite3.Row


def test_memory_db(tmp_path):
    conn = open_db(":memory:")
    assert migrate(conn) == 2


def test_interface_unique(db):
    dev = _device(db)
    _iface(db, dev)
    with pytest.raises(sqlite3.IntegrityError):
        _iface(db, dev)
    _iface(db, dev, kind="ethernet")  # same address, different kind is fine


def test_device_defaults_and_checks(db):
    dev = _device(db)
    row = db.execute("select * from device where id=?", (dev,)).fetchone()
    assert row["trust"] == "new"
    assert row["scan_policy"] == "normal"
    assert row["online"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("update device set trust='bogus' where id=?", (dev,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("update device set scan_policy='bogus' where id=?", (dev,))


def test_interface_kind_check_and_hints_default(db):
    dev = _device(db)
    with pytest.raises(sqlite3.IntegrityError):
        _iface(db, dev, kind="carrier-pigeon")
    for kind in ("ethernet", "wifi", "bluetooth", "zwave", "unknown"):
        _iface(db, dev, address=f"a-{kind}", kind=kind)
    row = db.execute("select hints from interface limit 1").fetchone()
    assert row["hints"] == "{}"


def test_device_delete_restricted_by_interface(db):
    dev = _device(db)
    _iface(db, dev)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("delete from device where id=?", (dev,))


def test_interface_requires_device(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "insert into interface(device_id,kind,address,is_random,first_seen,last_seen)"
            " values(NULL,'wifi','x',0,'t','t')"
        )


def test_lease_cascades_and_defaults(db):
    iface = _iface(db, _device(db))
    db.execute(
        "insert into ip_lease(interface_id,ipv4,first_seen,last_seen) values(?,?,'t','t')",
        (iface, "192.0.2.10"),
    )
    assert db.execute("select closed from ip_lease").fetchone()["closed"] == 0
    db.execute("delete from interface where id=?", (iface,))
    assert db.execute("select count(*) from ip_lease").fetchone()[0] == 0


def test_service_unique_and_cascade(db):
    dev = _device(db)
    sql = (
        "insert into service(device_id,port,proto,source,first_seen,last_seen)"
        " values(?,?,?,'nmap','t','t')"
    )
    db.execute(sql, (dev, 22, "tcp"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (dev, 22, "tcp"))
    db.execute(sql, (dev, 22, "udp"))
    db.execute("delete from device where id=?", (dev,))
    assert db.execute("select count(*) from service").fetchone()[0] == 0


def test_place_unique_and_set_null(db):
    db.execute("insert into place(name) values('Kitchen')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("insert into place(name) values('Kitchen')")
    pid = db.execute("select id from place").fetchone()["id"]
    dev = _device(db)
    db.execute("update device set place_id=? where id=?", (pid, dev))
    db.execute("delete from place where id=?", (pid,))
    assert db.execute("select place_id from device").fetchone()["place_id"] is None


def test_link_checks_and_cascade(db):
    a, b = _device(db, "a"), _device(db, "b")
    sql = "insert into link(a_device_id,b_device_id,kind,source) values(?,?,?,?)"
    db.execute(sql, (a, b, "wired", "manual"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (a, b, "carrier", "manual"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (a, b, "wifi", "guess"))
    db.execute("delete from device where id=?", (a,))
    assert db.execute("select count(*) from link").fetchone()[0] == 0


def test_observation_set_null_and_default(db):
    iface = _iface(db, _device(db))
    db.execute(
        "insert into observation(source,seen_at,payload,interface_id) values('arp','t','{}',?)",
        (iface,),
    )
    assert db.execute("select attached from observation").fetchone()["attached"] == 0
    db.execute("delete from interface where id=?", (iface,))
    assert db.execute("select interface_id from observation").fetchone()["interface_id"] is None


def test_field_source_pk(db):
    sql = (
        "insert into field_source(entity,entity_id,field,actor,set_at)"
        " values('device',1,'name',?,'t')"
    )
    db.execute(sql, ("user",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, ("claude",))


def test_batch_and_change(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("insert into batch(actor,at) values(NULL,'t')")
    db.execute("insert into batch(actor,at) values('user','t')")
    db.execute("insert into batch(actor,at,undoes_batch_id) values('user','t',1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("insert into batch(actor,at,undoes_batch_id) values('user','t',99)")
    db.execute(
        "insert into change(batch_id,entity,entity_id,field,old_value,new_value,"
        "old_actor,old_set_at)"
        " values(1,'device',1,'name','a','b','collector:arp','t')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("insert into change(batch_id,entity,entity_id,field) values(99,'d',1,'n')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("insert into change(batch_id,entity,entity_id,field) values(NULL,'d',1,'n')")


def test_topology_layout_cascade(db):
    dev = _device(db)
    db.execute("insert into topology_layout(device_id,x,y) values(?,1.0,2.0)", (dev,))
    db.execute("delete from device where id=?", (dev,))
    assert db.execute("select count(*) from topology_layout").fetchone()[0] == 0


def test_collector_status_defaults(db):
    db.execute("insert into collector_status(name) values('arp')")
    row = db.execute("select * from collector_status").fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_ok"] is None


def test_indexes(db):
    idx = {r[0] for r in db.execute("select name from sqlite_master where type='index'")}

    def cols(table: str) -> list[list[str]]:
        out = []
        for r in db.execute(f"pragma index_list({table})"):
            out.append([c["name"] for c in db.execute(f"pragma index_info({r['name']})")])
        return out

    assert ["ipv4", "last_seen"] in cols("ip_lease")
    assert ["batch_id"] in cols("change")
    assert ["entity", "entity_id", "field"] in cols("change")
    assert idx


def test_schema_is_package_data():
    text = resources.files("lanscape_atlas.store").joinpath("schema.sql").read_text()
    assert "create table" in text.lower()


def test_future_version_refused(tmp_path):
    conn = open_db(tmp_path / "a.db")
    conn.execute("pragma user_version=99")
    with pytest.raises(RuntimeError):
        migrate(conn)


def test_fixtures(db, clock):
    assert db.execute("pragma user_version").fetchone()[0] == 2
    assert clock().isoformat() == "2026-01-01T00:00:00+00:00"
    clock.advance(minutes=2, seconds=30)
    assert clock().isoformat() == "2026-01-01T00:02:30+00:00"
