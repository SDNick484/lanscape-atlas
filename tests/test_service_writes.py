from __future__ import annotations

import json
import threading
import time

import pytest

from lanscape_atlas.events import EVENT_CHANGE_APPLIED
from lanscape_atlas.service import PROVENANCE_FIELDS, USER_FIELDS, Service
from lanscape_atlas.store.db import migrate, open_db

T0 = "2026-01-01T00:00:00Z"


def _changes(svc, batch_id):
    return [
        tuple(r)
        for r in svc.conn.execute(
            "select batch.actor, change.entity, change.entity_id, change.field,"
            " change.old_value, change.new_value, change.old_actor, change.old_set_at"
            " from change join batch on batch.id=change.batch_id where batch_id=?"
            " order by change.id",
            (batch_id,),
        )
    ]


def _source(svc, device_id, field):
    row = svc.conn.execute(
        "select actor, set_at from field_source where entity='device' and entity_id=? and field=?",
        (device_id, field),
    ).fetchone()
    return None if row is None else tuple(row)


def test_field_sets():
    assert USER_FIELDS == {
        "name",
        "type",
        "icon",
        "place_id",
        "pos_x",
        "pos_y",
        "owner",
        "notes",
        "trust",
        "scan_policy",
    }
    assert PROVENANCE_FIELDS == {"name", "type", "icon"}


def test_update_device_logs_change_and_source(svc, make_device):
    d = make_device(name="Espressif .23")
    b = svc.update_device("user", d, {"name": "Garage Sensor"})
    assert b > 0
    rows = svc.conn.execute(
        "select actor, field, old_value, new_value from change join batch"
        " on batch.id=change.batch_id where batch_id=?",
        (b,),
    ).fetchall()
    assert [(r[0], r[1], json.loads(r[2]), json.loads(r[3])) for r in rows] == [
        ("user", "name", "Espressif .23", "Garage Sensor")
    ]
    assert _source(svc, d, "name") == ("user", T0)
    assert svc.conn.execute("select name from device where id=?", (d,)).fetchone()[0] == (
        "Garage Sensor"
    )
    assert svc.conn.execute("select at from batch where id=?", (b,)).fetchone()[0] == T0


def test_change_records_previous_provenance(svc, make_device, clock):
    d = make_device(name="a")
    b1 = svc.update_device("claude", d, {"name": "b"})
    # first write: no prior field_source row
    assert _changes(svc, b1)[0][6:] == (None, None)
    clock.advance(minutes=5)
    b2 = svc.update_device("user", d, {"name": "c"})
    assert _changes(svc, b2)[0][6:] == ("claude", T0)
    assert _source(svc, d, "name") == ("user", "2026-01-01T00:05:00Z")


def test_values_round_trip_as_json(svc, make_device):
    # pos_x/pos_y are set through place_devices now (Ruling 19); place_id is the int field.
    with svc.conn:
        p1 = svc.conn.execute("insert into place(name) values ('Room A')").lastrowid
        p2 = svc.conn.execute("insert into place(name) values ('Room B')").lastrowid
    d = make_device(place_id=p1, notes="hi")
    b = svc.update_device("user", d, {"place_id": p2, "notes": None})
    got = {r[3]: (json.loads(r[4]), json.loads(r[5])) for r in _changes(svc, b)}
    assert got == {"place_id": (p1, p2), "notes": ("hi", None)}


def test_noop_update_creates_no_batch(svc, make_device, events):
    d = make_device(name="x")
    assert svc.update_device("user", d, {"name": "x"}) == 0
    assert svc.conn.execute("select count(*) from batch").fetchone()[0] == 0
    assert _source(svc, d, "name") is None
    assert events.recorded == []


def test_partial_noop_logs_only_changed_fields(svc, make_device):
    d = make_device(name="x", owner="o")
    b = svc.update_device("user", d, {"name": "x", "owner": "p"})
    assert [r[3] for r in _changes(svc, b)] == ["owner"]
    assert _source(svc, d, "name") is None


def test_unknown_device_raises_keyerror(svc):
    with pytest.raises(KeyError):
        svc.update_device("user", 999, {"name": "x"})


def test_rejects_unknown_field(svc, make_device):
    with pytest.raises(ValueError):
        svc.update_device("user", make_device(), {"first_seen": "x"})


@pytest.mark.parametrize(
    "fields",
    [
        {"trust": "maybe"},
        {"scan_policy": "sometimes"},
        {"pos_x": "left"},
        {"pos_y": True},
        {"place_id": 12345},
    ],
)
def test_rejects_bad_values(svc, make_device, fields):
    d = make_device(name="keep")
    with pytest.raises(ValueError):
        svc.update_device("claude", d, {"name": "changed", **fields})
    # nothing written, not even the valid field
    assert svc.conn.execute("select name from device where id=?", (d,)).fetchone()[0] == "keep"
    assert svc.conn.execute("select count(*) from batch").fetchone()[0] == 0


def test_update_publishes_change_applied_after_commit(svc, make_device, events, db):
    seen = []

    def handler(ev):
        # handler runs after commit: no open transaction, row visible
        seen.append((db.in_transaction, ev.payload))

    events.subscribe(EVENT_CHANGE_APPLIED, handler)
    d = make_device()
    b = svc.update_device("claude", d, {"type": "media"})
    assert seen == [(False, {"batch_id": b, "actor": "claude"})]


def test_collector_may_set(svc, make_device):
    d = make_device()
    assert svc.collector_may_set(d, "name") is True
    b = svc.begin_batch("collector:mdns")
    assert svc.record_collector_field(b, d, "name", "tv", "mdns") is True
    svc.conn.commit()
    assert svc.collector_may_set(d, "name") is True


def test_collector_may_not_override_user(svc, make_device):
    d = make_device()
    svc.update_device("user", d, {"name": "Mine"})
    assert svc.collector_may_set(d, "name") is False


def test_collector_may_not_override_claude(svc, make_device):
    d = make_device()
    svc.update_device("claude", d, {"icon": "tv"})
    assert svc.collector_may_set(d, "icon") is False


def test_record_collector_field_writes_value_source_and_change(svc, make_device, events):
    d = make_device(name="old")
    with svc.lock, svc.conn:
        b = svc.begin_batch("collector:mdns")
        assert svc.record_collector_field(b, d, "name", "Living Room TV", "mdns") is True
    assert svc.conn.execute("select name from device where id=?", (d,)).fetchone()[0] == (
        "Living Room TV"
    )
    assert _source(svc, d, "name") == ("collector:mdns", T0)
    assert _changes(svc, b) == [
        ("collector:mdns", "device", d, "name", '"old"', '"Living Room TV"', None, None)
    ]
    assert svc.conn.execute("select actor, at from batch where id=?", (b,)).fetchone()[:] == (
        "collector:mdns",
        T0,
    )
    # caller owns the transaction and the event; Service publishes nothing here
    assert events.recorded == []


def test_record_collector_field_refuses_user_owned(svc, make_device):
    d = make_device()
    svc.update_device("user", d, {"name": "Mine"})
    with svc.conn:
        b = svc.begin_batch("collector:dhcp")
        assert svc.record_collector_field(b, d, "name", "esp-1234", "dhcp") is False
    assert svc.conn.execute("select name from device where id=?", (d,)).fetchone()[0] == "Mine"
    assert _changes(svc, b) == []


def test_record_collector_field_unchanged_value(svc, make_device):
    d = make_device(name="same")
    with svc.conn:
        b = svc.begin_batch("collector:mdns")
        assert svc.record_collector_field(b, d, "name", "same", "mdns") is False
    assert _changes(svc, b) == []
    assert _source(svc, d, "name") is None


def test_record_collector_field_overrides_other_collector(svc, make_device, clock):
    d = make_device()
    with svc.conn:
        b1 = svc.begin_batch("collector:dhcp")
        svc.record_collector_field(b1, d, "name", "esp", "dhcp")
    clock.advance(minutes=1)
    with svc.conn:
        b2 = svc.begin_batch("collector:mdns")
        assert svc.record_collector_field(b2, d, "name", "Sensor", "mdns") is True
    assert _changes(svc, b2)[0][4:] == ('"esp"', '"Sensor"', "collector:dhcp", T0)


def test_record_collector_field_rejects_non_provenance_field(svc, make_device):
    d = make_device()
    with svc.conn:
        b = svc.begin_batch("collector:mdns")
        with pytest.raises(ValueError):
            svc.record_collector_field(b, d, "trust", "known", "mdns")


def test_set_place_creates_place_once(svc, make_device):
    d1, d2 = make_device(), make_device()
    b1 = svc.set_place("user", d1, "Office")
    b2 = svc.set_place("user", d2, "Office")
    assert svc.conn.execute("select count(*) from place").fetchone()[0] == 1
    pid = svc.conn.execute("select id from place where name='Office'").fetchone()[0]
    assert _changes(svc, b1) == [
        ("user", "place", pid, "name", "null", '"Office"', None, None),
        ("user", "device", d1, "place_id", "null", str(pid), None, None),
    ]
    assert [r[1] for r in _changes(svc, b2)] == ["device"]
    assert svc.conn.execute("select place_id from device where id=?", (d2,)).fetchone()[0] == pid
    assert _source(svc, d2, "place_id") == ("user", T0)


def test_set_place_unknown_device_creates_nothing(svc):
    with pytest.raises(KeyError):
        svc.set_place("user", 999, "Office")
    assert svc.conn.execute("select count(*) from place").fetchone()[0] == 0
    assert svc.conn.execute("select count(*) from batch").fetchone()[0] == 0


def test_set_place_same_place_is_noop(svc, make_device):
    d = make_device()
    svc.set_place("user", d, "Office")
    assert svc.set_place("user", d, "Office") == 0


def test_add_and_remove_link(svc, make_device, events):
    a, b = make_device(), make_device()
    batch = svc.add_link("user", a, b, "wired", detail="port 3")
    rows = _changes(svc, batch)
    assert len(rows) == 1
    actor, entity, link_id, field, old, new, _, _ = rows[0]
    assert (actor, entity, field, old) == ("user", "link", "*", "null")
    assert json.loads(new) == {
        "id": link_id,
        "a_device_id": a,
        "b_device_id": b,
        "kind": "wired",
        "detail": "port 3",
        "source": "manual",
        "layout": None,
    }
    batch2 = svc.remove_link("claude", link_id)
    rows2 = _changes(svc, batch2)
    assert rows2[0][1:4] == ("link", link_id, "*")
    assert json.loads(rows2[0][4]) == json.loads(new)
    assert rows2[0][5] == "null"
    assert svc.conn.execute("select count(*) from link").fetchone()[0] == 0
    assert [e.payload for e in events.recorded] == [
        {"batch_id": batch, "actor": "user"},
        {"batch_id": batch2, "actor": "claude"},
    ]


def test_add_link_validation(svc, make_device):
    a = make_device()
    with pytest.raises(ValueError):
        svc.add_link("user", a, make_device(), "carrier-pigeon")
    with pytest.raises(KeyError):
        svc.add_link("user", a, 999, "wired")
    with pytest.raises(KeyError):
        svc.remove_link("user", 999)


def test_create_placeholder_device(svc):
    d, b = svc.create_placeholder_device("user", "Office Switch", "switch")
    row = svc.conn.execute("select name, type, trust from device where id=?", (d,)).fetchone()
    assert tuple(row) == ("Office Switch", "switch", "known")
    fields = [r[3] for r in _changes(svc, b)]
    assert fields == ["*", "name", "type"]
    assert _source(svc, d, "name") == ("user", T0)
    assert _source(svc, d, "type") == ("user", T0)


def test_service_has_reentrant_lock(svc):
    with svc.lock, svc.lock:
        pass


def test_concurrent_writer_waits_not_fails(tmp_path, events, clock):
    path = tmp_path / "atlas.db"
    conn = open_db(path)
    migrate(conn)
    with conn:
        did = conn.execute(
            "insert into device(name, first_seen, last_seen) values('a', ?, ?)", (T0, T0)
        ).lastrowid
    svc = Service(conn, events, clock)
    holding = threading.Event()

    def hold() -> None:
        other = open_db(path)
        other.isolation_level = None
        other.execute("BEGIN IMMEDIATE")
        holding.set()
        time.sleep(0.5)
        other.execute("COMMIT")
        other.close()

    t = threading.Thread(target=hold)
    t.start()
    assert holding.wait(5)
    start = time.monotonic()
    b = svc.update_device("user", did, {"name": "b"})
    elapsed = time.monotonic() - start
    t.join()
    assert b > 0
    assert elapsed >= 0.4
    assert conn.execute("select name from device where id=?", (did,)).fetchone()[0] == "b"
    conn.close()
