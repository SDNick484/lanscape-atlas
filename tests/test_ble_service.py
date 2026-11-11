"""Service writes for Bluetooth: sightings, scanners, claim/unclaim, scanner links, prune."""

from __future__ import annotations

import sqlite3
from datetime import timedelta, timezone
from typing import Any

import pytest

from conftest import dump_state
from lanscape_atlas.bluetooth.aggregator import MinuteRow
from lanscape_atlas.bluetooth.match import Identity
from lanscape_atlas.clock import iso
from lanscape_atlas.events import EVENT_CHANGE_APPLIED
from lanscape_atlas.service import Service, UndoConflict

ADDR = "00:00:5e:00:53:01"
ADDR2 = "00:00:5e:00:53:02"
SCANNER = "00:00:5e:00:53:aa"
SCANNER2 = "00:00:5e:00:53:bb"
BEACON = "00000000-0000-4000-8000-000000000001"
IDENT = Identity("address", ADDR)
BEACON_IDENT = Identity("ibeacon", f"{BEACON}/1/2")
_MINUTE_FMT = "%Y-%m-%dT%H:%MZ"


def _place(db: sqlite3.Connection, name: str) -> int:
    with db:
        cur = db.execute("insert into place(name) values (?)", (name,))
    assert cur.lastrowid is not None
    return cur.lastrowid


def _minute(clock: Any, offset_min: int = 0) -> str:
    return (clock() + timedelta(minutes=offset_min)).strftime(_MINUTE_FMT)


def _row(clock: Any, device_id: int, *, source: str = SCANNER, offset_min: int = 0, **kw: Any):
    vals: dict[str, Any] = {"max_rssi": -70, "adverts": 3, "last_at": clock()}
    vals.update(kw)
    return MinuteRow(device_id=device_id, source=source, minute=_minute(clock, offset_min), **vals)


def _sightings(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in db.execute("select * from ble_sighting order by device_id, scanner_id, minute")
    ]


def _scanner(db: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    row = db.execute("select * from ble_scanner where source = ?", (source,)).fetchone()
    return None if row is None else dict(row)


def _identities(db: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in db.execute("select id, device_id, kind, value from ble_identity order by id")
    ]


def _source(db: sqlite3.Connection, entity: str, entity_id: int, field: str):
    row = db.execute(
        "select actor, set_at from field_source where entity=? and entity_id=? and field=?",
        (entity, entity_id, field),
    ).fetchone()
    return None if row is None else tuple(row)


def _curated(db: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    state = dump_state(db)
    del state["batch"], state["change"]
    return state


# ------------------------------------------------------------------ sightings


def test_record_minutes_upserts_max_and_sum(svc: Service, db, clock, make_device) -> None:
    d = make_device(name="tag")
    t0 = clock()
    assert svc.record_ble_minutes([_row(clock, d, max_rssi=-70, adverts=3, last_at=t0)]) == 1
    later = t0 + timedelta(seconds=40)
    earlier = t0 - timedelta(seconds=10)
    assert (
        svc.record_ble_minutes(
            [
                _row(clock, d, max_rssi=-60, adverts=2, last_at=later),
                _row(clock, d, max_rssi=-90, adverts=1, last_at=earlier),
            ]
        )
        == 2
    )
    rows = _sightings(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["minute"] == _minute(clock)
    assert r["max_rssi"] == -60
    assert r["adverts"] == 6
    assert r["last_at"] == iso(later)
    assert not db.in_transaction


def test_record_minutes_snapshots_scanner_room(svc: Service, db, clock, make_device) -> None:
    room_a = _place(db, "Room A")
    room_b = _place(db, "Room B")
    proxy = make_device(name="proxy", place_id=room_a)
    tag = make_device(name="tag")
    svc.upsert_scanners({SCANNER: "proxy"}, clock())
    scanner_id = _scanner(db, SCANNER)["id"]  # type: ignore[index]
    svc.link_scanner("user", scanner_id, proxy)
    svc.record_ble_minutes([_row(clock, tag)])
    with db:
        db.execute("update device set place_id = ? where id = ?", (room_b, proxy))
    svc.record_ble_minutes([_row(clock, tag)])  # same minute: keeps the first snapshot
    clock.advance(minutes=1)
    svc.record_ble_minutes([_row(clock, tag)])
    rows = _sightings(db)
    assert [r["place_id"] for r in rows] == [room_a, room_b]


def test_record_minutes_unlinked_scanner_has_no_room(svc: Service, db, clock, make_device) -> None:
    tag = make_device(name="tag")
    svc.record_ble_minutes([_row(clock, tag)])
    assert _sightings(db)[0]["place_id"] is None


def test_record_minutes_creates_unknown_scanner(svc: Service, db, clock, make_device) -> None:
    tag = make_device(name="tag")
    svc.record_ble_minutes([_row(clock, tag, source=SCANNER2)])
    sc = _scanner(db, SCANNER2)
    assert sc is not None
    assert sc["name"] is None and sc["device_id"] is None
    assert sc["first_seen"] == iso(clock())
    assert _sightings(db)[0]["scanner_id"] == sc["id"]
    clock.advance(minutes=3)
    svc.record_ble_minutes([_row(clock, tag, source=SCANNER2)])
    sc2 = _scanner(db, SCANNER2)
    assert sc2 is not None and sc2["id"] == sc["id"]
    assert sc2["first_seen"] == sc["first_seen"]
    assert sc2["last_seen"] == iso(clock())


def test_record_minutes_skips_missing_device(svc: Service, db, clock, make_device) -> None:
    tag = make_device(name="tag")
    assert svc.record_ble_minutes([_row(clock, 999), _row(clock, tag)]) == 1
    assert [r["device_id"] for r in _sightings(db)] == [tag]


def test_record_minutes_not_change_logged(svc: Service, db, clock, make_device, events) -> None:
    tag = make_device(name="tag")
    svc.record_ble_minutes([_row(clock, tag)])
    assert db.execute("select count(*) from batch").fetchone()[0] == 0
    assert db.execute("select count(*) from change").fetchone()[0] == 0
    assert events.recorded == []


def test_upsert_scanners_updates_name(svc: Service, db, clock) -> None:
    t0 = clock()
    svc.upsert_scanners({SCANNER: "hall adapter", SCANNER2: None}, t0)
    sc = _scanner(db, SCANNER)
    assert sc is not None
    assert sc["name"] == "hall adapter"
    assert sc["first_seen"] == sc["last_seen"] == iso(t0)
    assert _scanner(db, SCANNER2)["name"] is None  # type: ignore[index]
    t1 = clock.advance(minutes=5)
    svc.upsert_scanners({SCANNER: "hall proxy", SCANNER2: "kitchen"}, t1)
    sc = _scanner(db, SCANNER)
    assert sc is not None
    assert sc["name"] == "hall proxy"
    assert sc["first_seen"] == iso(t0) and sc["last_seen"] == iso(t1)
    assert _scanner(db, SCANNER2)["name"] == "kitchen"  # type: ignore[index]
    # A missing name never wipes a known one.
    svc.upsert_scanners({SCANNER: None}, t1)
    assert _scanner(db, SCANNER)["name"] == "hall proxy"  # type: ignore[index]
    assert db.execute("select count(*) from change").fetchone()[0] == 0


# ------------------------------------------------------------------ claim / unclaim


def test_claim_onto_existing_device(svc: Service, db, clock, make_device, events) -> None:
    d = make_device(name="keys")
    device_id, batch = svc.claim_ble("user", IDENT, device_id=d)
    assert device_id == d and batch > 0
    rows = db.execute("select * from ble_identity").fetchall()
    assert len(rows) == 1
    assert (rows[0]["device_id"], rows[0]["kind"], rows[0]["value"]) == (d, "address", ADDR)
    assert rows[0]["created_at"] == iso(clock())
    changes = db.execute(
        "select entity, entity_id, field, old_value from change where batch_id = ?", (batch,)
    ).fetchall()
    assert [tuple(c) for c in changes] == [("ble_identity", rows[0]["id"], "*", "null")]
    assert events.recorded[-1].name == EVENT_CHANGE_APPLIED
    assert events.recorded[-1].payload == {"batch_id": batch, "actor": "user"}


def test_claim_as_new_device(svc: Service, db, clock) -> None:
    device_id, batch = svc.claim_ble("claude", BEACON_IDENT, new_name="Keys", new_type="tag")
    dev = db.execute("select name, type, trust from device where id = ?", (device_id,)).fetchone()
    assert tuple(dev) == ("Keys", "tag", "known")
    assert _source(db, "device", device_id, "name")[0] == "claude"  # type: ignore[index]
    assert _source(db, "device", device_id, "type")[0] == "claude"  # type: ignore[index]
    assert _identities(db) == [(1, device_id, "ibeacon", f"{BEACON}/1/2")]
    changes = [
        (r["entity"], r["field"])
        for r in db.execute("select * from change where batch_id = ? order by id", (batch,))
    ]
    assert changes == [
        ("device", "*"),
        ("device", "name"),
        ("device", "type"),
        ("ble_identity", "*"),
    ]
    assert db.execute("select count(*) from batch").fetchone()[0] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"device_id": 1, "new_name": "x", "new_type": "tag"},
        {"new_name": "x"},
        {"new_name": "x", "new_type": ""},
        {"new_name": "  ", "new_type": "tag"},
        {"device_id": 1, "new_type": "tag"},
    ],
)
def test_claim_requires_exactly_one_target(svc: Service, db, make_device, kwargs) -> None:
    make_device(name="d")
    before = dump_state(db)
    with pytest.raises(ValueError):
        svc.claim_ble("user", IDENT, **kwargs)
    assert dump_state(db) == before


def test_claim_rejects_bad_actor_and_non_canonical_identity(svc: Service, db, make_device) -> None:
    d = make_device(name="d")
    before = dump_state(db)
    with pytest.raises(ValueError):
        svc.claim_ble("collector:ha", IDENT, device_id=d)
    with pytest.raises(ValueError):
        svc.claim_ble("user", Identity("address", ADDR.upper()), device_id=d)
    with pytest.raises(ValueError):
        svc.claim_ble("user", Identity("ibeacon", "garbage"), device_id=d)
    assert dump_state(db) == before


def test_claim_unknown_device(svc: Service, db) -> None:
    before = dump_state(db)
    with pytest.raises(KeyError):
        svc.claim_ble("user", IDENT, device_id=999)
    assert dump_state(db) == before
    assert not db.in_transaction


def test_claim_duplicate_identity(svc: Service, db, make_device) -> None:
    d = make_device(name="a")
    e = make_device(name="b")
    svc.claim_ble("user", IDENT, device_id=d)
    before = dump_state(db)
    with pytest.raises(ValueError, match=IDENT.key()):
        svc.claim_ble("user", IDENT, device_id=e)
    with pytest.raises(ValueError, match=IDENT.key()):
        svc.claim_ble("user", IDENT, new_name="dup", new_type="tag")
    assert dump_state(db) == before
    assert not db.in_transaction


def test_unclaim_unknown_identity(svc: Service, db) -> None:
    with pytest.raises(KeyError):
        svc.unclaim_ble("user", 42)
    assert db.execute("select count(*) from batch").fetchone()[0] == 0


def test_unclaim_then_undo_restores_same_id(svc: Service, db, clock, make_device) -> None:
    d = make_device(name="keys")
    svc.claim_ble("user", IDENT, device_id=d)
    svc.record_ble_minutes([_row(clock, d)])
    identity_id = _identities(db)[0][0]
    before = tuple(db.execute("select * from ble_identity").fetchone())
    clock.advance(minutes=2)
    b = svc.unclaim_ble("claude", identity_id)
    assert _identities(db) == []
    assert len(_sightings(db)) == 1  # sightings stay with the device
    svc.undo("user", b)
    assert tuple(db.execute("select * from ble_identity").fetchone()) == before
    # redo removes it again
    u = db.execute("select max(id) from batch").fetchone()[0]
    svc.undo("user", u)
    assert _identities(db) == []


def test_undo_claim_onto_existing(svc: Service, db, clock, make_device) -> None:
    d = make_device(name="keys")
    before = _curated(db)
    _, b = svc.claim_ble("user", IDENT, device_id=d)
    svc.undo("user", b)
    assert _curated(db) == before


def test_undo_claim_new_device_with_sightings(svc: Service, db, clock, make_device) -> None:
    before = _curated(db)
    device_id, b = svc.claim_ble("user", IDENT, new_name="Keys", new_type="tag")
    svc.record_ble_minutes([_row(clock, device_id)])
    assert len(_sightings(db)) == 1
    svc.undo("user", b)
    after = _curated(db)
    # The scanner row was created by record_ble_minutes (collector data) and stays.
    assert after.pop("ble_scanner") and before.pop("ble_scanner") == []
    assert after == before
    assert db.execute("select count(*) from device").fetchone()[0] == 0
    assert _identities(db) == []
    assert _sightings(db) == []


def test_redo_claim_new_device(svc: Service, db, clock) -> None:
    device_id, b = svc.claim_ble("claude", BEACON_IDENT, new_name="Keys", new_type="tag")
    claimed = _curated(db)
    clock.advance(minutes=1)
    u = svc.undo("user", b)
    clock.advance(minutes=1)
    svc.undo("user", u)
    assert _curated(db) == claimed
    assert _source(db, "device", device_id, "name")[0] == "claude"  # type: ignore[index]


def test_undo_unclaim_conflicts_with_reclaim(svc: Service, db, make_device) -> None:
    d = make_device(name="a")
    e = make_device(name="b")
    svc.claim_ble("user", IDENT, device_id=d)
    identity_id = _identities(db)[0][0]
    unclaim = svc.unclaim_ble("user", identity_id)
    _, reclaim = svc.claim_ble("user", IDENT, device_id=e)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", unclaim)
    assert exc.value.conflicting == [reclaim]
    assert not db.in_transaction


def test_undo_claim_conflicts_with_later_unclaim(svc: Service, db, make_device) -> None:
    d = make_device(name="a")
    _, claim = svc.claim_ble("user", IDENT, device_id=d)
    unclaim = svc.unclaim_ble("user", _identities(db)[0][0])
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", claim)
    assert exc.value.conflicting == [unclaim]


def test_undo_new_device_conflicts_with_later_identity(svc: Service, db) -> None:
    device_id, b = svc.claim_ble("user", IDENT, new_name="Keys", new_type="tag")
    _, b2 = svc.claim_ble("user", BEACON_IDENT, device_id=device_id)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b)
    assert exc.value.conflicting == [b2]


def test_undo_new_device_conflicts_with_later_scanner_link(svc: Service, db, clock) -> None:
    device_id, b = svc.claim_ble("user", IDENT, new_name="Proxy", new_type="proxy")
    svc.upsert_scanners({SCANNER: "proxy"}, clock())
    scanner_id = _scanner(db, SCANNER)["id"]  # type: ignore[index]
    link = svc.link_scanner("user", scanner_id, device_id)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b)
    assert exc.value.conflicting == [link]


def test_delete_device_refuses_while_identity_remains(svc: Service, db, make_device) -> None:
    d = make_device(name="a")
    svc.claim_ble("user", IDENT, device_id=d)
    with pytest.raises(ValueError, match="ble_identity"), svc._write_txn():
        svc._delete_entity("device", d)
    assert db.execute("select count(*) from device").fetchone()[0] == 1


def test_delete_device_refuses_while_scanner_linked(svc: Service, db, clock, make_device) -> None:
    d = make_device(name="proxy")
    svc.upsert_scanners({SCANNER: "proxy"}, clock())
    svc.link_scanner("user", _scanner(db, SCANNER)["id"], d)  # type: ignore[index]
    with pytest.raises(ValueError, match="ble_scanner"), svc._write_txn():
        svc._delete_entity("device", d)
    assert _scanner(db, SCANNER)["device_id"] == d  # type: ignore[index]


def _iface(db: sqlite3.Connection, clock: Any, device_id: int, mac: str) -> int:
    with db:
        cur = db.execute(
            "insert into interface(device_id, kind, address, first_seen, last_seen)"
            " values (?, 'wifi', ?, ?, ?)",
            (device_id, mac, iso(clock()), iso(clock())),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_undo_unclaim_conflicts_when_device_merged_away(
    svc: Service, db, clock, make_device
) -> None:
    source = make_device(name=None)
    target = make_device(name=None)
    rid = _iface(db, clock, source, "02:00:5e:00:53:41")
    svc.claim_ble("user", IDENT, device_id=source)
    unclaim = svc.unclaim_ble("user", _identities(db)[0][0])
    merge = svc.claim_interface("user", rid, target)
    assert db.execute("select 1 from device where id = ?", (source,)).fetchone() is None
    before = dump_state(db)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", unclaim)
    assert exc.value.conflicting == [merge]
    assert dump_state(db) == before
    assert not db.in_transaction


def test_undo_unlink_conflicts_when_device_merged_away(
    svc: Service, db, clock, make_device
) -> None:
    source = make_device(name=None)
    target = make_device(name=None)
    rid = _iface(db, clock, source, "02:00:5e:00:53:41")
    svc.upsert_scanners({SCANNER: "proxy"}, clock())
    scanner_id = _scanner(db, SCANNER)["id"]  # type: ignore[index]
    svc.link_scanner("user", scanner_id, source)
    unlink = svc.link_scanner("user", scanner_id, None)
    merge = svc.claim_interface("user", rid, target)
    before = dump_state(db)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", unlink)
    assert exc.value.conflicting == [merge]
    assert dump_state(db) == before


def test_undo_unclaim_after_device_merge_was_undone(svc: Service, db, clock, make_device) -> None:
    source = make_device(name=None)
    target = make_device(name=None)
    rid = _iface(db, clock, source, "02:00:5e:00:53:41")
    svc.claim_ble("user", IDENT, device_id=source)
    identity_id = _identities(db)[0][0]
    unclaim = svc.unclaim_ble("user", identity_id)
    merge = svc.claim_interface("user", rid, target)
    svc.undo("user", merge)  # the device is back: the deleting batch is no longer live
    svc.undo("user", unclaim)
    assert _identities(db) == [(identity_id, source, "address", ADDR)]


def test_undo_unclaim_when_device_deleted_outside_change_log(svc: Service, db, make_device) -> None:
    d = make_device(name="keys")
    svc.claim_ble("user", IDENT, device_id=d)
    unclaim = svc.unclaim_ble("user", _identities(db)[0][0])
    with db:
        db.execute("delete from device where id = ?", (d,))
    before = dump_state(db)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", unclaim)
    assert exc.value.conflicting == []
    assert f"device {d} no longer exists" in str(exc.value)
    assert dump_state(db) == before
    assert not db.in_transaction


# ------------------------------------------------------------------ scanner links


def test_link_scanner_and_undo(svc: Service, db, clock, make_device, events) -> None:
    d = make_device(name="proxy")
    e = make_device(name="other")
    svc.upsert_scanners({SCANNER: "proxy"}, clock())
    scanner_id = _scanner(db, SCANNER)["id"]  # type: ignore[index]
    b1 = svc.link_scanner("user", scanner_id, d)
    assert b1 > 0
    assert _scanner(db, SCANNER)["device_id"] == d  # type: ignore[index]
    assert _source(db, "ble_scanner", scanner_id, "device_id")[0] == "user"  # type: ignore[index]
    assert events.recorded[-1].payload == {"batch_id": b1, "actor": "user"}
    src1 = _source(db, "ble_scanner", scanner_id, "device_id")
    clock.advance(minutes=1)
    b2 = svc.link_scanner("claude", scanner_id, e)
    clock.advance(minutes=1)
    b3 = svc.link_scanner("claude", scanner_id, None)
    assert _scanner(db, SCANNER)["device_id"] is None  # type: ignore[index]
    svc.undo("user", b3)
    assert _scanner(db, SCANNER)["device_id"] == e  # type: ignore[index]
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b1)
    assert exc.value.conflicting == [b2]
    svc.undo("user", b2)
    assert _scanner(db, SCANNER)["device_id"] == d  # type: ignore[index]
    assert _source(db, "ble_scanner", scanner_id, "device_id") == src1
    svc.undo("user", b1)
    assert _scanner(db, SCANNER)["device_id"] is None  # type: ignore[index]
    assert _source(db, "ble_scanner", scanner_id, "device_id") is None


def test_link_scanner_unchanged_returns_0(svc: Service, db, clock, make_device, events) -> None:
    d = make_device(name="proxy")
    svc.upsert_scanners({SCANNER: "proxy"}, clock())
    scanner_id = _scanner(db, SCANNER)["id"]  # type: ignore[index]
    assert svc.link_scanner("user", scanner_id, None) == 0
    svc.link_scanner("user", scanner_id, d)
    n = db.execute("select count(*) from batch").fetchone()[0]
    events.recorded.clear()
    assert svc.link_scanner("user", scanner_id, d) == 0
    assert db.execute("select count(*) from batch").fetchone()[0] == n
    assert events.recorded == []


def test_link_scanner_unknown_ids(svc: Service, db, clock, make_device) -> None:
    d = make_device(name="proxy")
    with pytest.raises(KeyError, match="no ble_scanner with id 7"):
        svc.link_scanner("user", 7, d)
    svc.upsert_scanners({SCANNER: "proxy"}, clock())
    scanner_id = _scanner(db, SCANNER)["id"]  # type: ignore[index]
    with pytest.raises(KeyError):
        svc.link_scanner("user", scanner_id, 999)
    with pytest.raises(ValueError):
        svc.link_scanner("collector:ha", scanner_id, d)
    assert db.execute("select count(*) from batch").fetchone()[0] == 0


# ------------------------------------------------------------------ prune


def test_prune_ble_respects_cutoff(svc: Service, db, clock, make_device) -> None:
    tag = make_device(name="tag")
    start = clock()
    svc.record_ble_minutes([_row(clock, tag)])  # day 0, 00:00
    clock.advance(days=1)
    svc.record_ble_minutes([_row(clock, tag)])  # day 1, 00:00
    clock.advance(minutes=1)
    svc.record_ble_minutes([_row(clock, tag)])  # day 1, 00:01
    clock.now = start + timedelta(days=15)
    # cutoff = day 1 00:00 exactly: strictly older rows go
    assert svc.prune_ble(14) == 1
    assert [r["minute"] for r in _sightings(db)] == [
        (start + timedelta(days=1)).strftime(_MINUTE_FMT),
        (start + timedelta(days=1, minutes=1)).strftime(_MINUTE_FMT),
    ]
    assert svc.prune_ble(0) == 2
    assert _sightings(db) == []
    with pytest.raises(ValueError):
        svc.prune_ble(-1)


def test_prune_ble_cutoff_in_utc_for_non_utc_clock(svc: Service, db, clock, make_device) -> None:
    tag = make_device(name="tag")
    start = clock()
    svc.record_ble_minutes([_row(clock, tag)])  # 00:00Z
    clock.advance(minutes=1)
    svc.record_ble_minutes([_row(clock, tag)])  # 00:01Z
    # start + 1 day + 90 s, expressed at UTC-8: the cutoff must be 00:01Z, not local time.
    clock.now = (start + timedelta(days=1, seconds=90)).astimezone(timezone(timedelta(hours=-8)))
    assert svc.prune_ble(1) == 1
    assert [r["minute"] for r in _sightings(db)] == [
        (start + timedelta(minutes=1)).strftime(_MINUTE_FMT)
    ]


# ------------------------------------------------------------------ invariants


def test_bluetooth_never_touches_placement(svc: Service, db, clock, make_device) -> None:
    room = _place(db, "Room A")
    d = make_device(name="tag", place_id=room, pos_x=0.25, pos_y=0.5, online=1)
    cols = "place_id, pos_x, pos_y, roaming, online"
    before = tuple(db.execute(f"select {cols} from device where id = ?", (d,)).fetchone())
    proxy = make_device(name="proxy", place_id=room)
    svc.upsert_scanners({SCANNER: "proxy"}, clock())
    svc.link_scanner("user", _scanner(db, SCANNER)["id"], proxy)  # type: ignore[index]
    svc.claim_ble("user", IDENT, device_id=d)
    svc.record_ble_minutes([_row(clock, d)])
    svc.unclaim_ble("user", _identities(db)[0][0])
    after = tuple(db.execute(f"select {cols} from device where id = ?", (d,)).fetchone())
    assert after == before
