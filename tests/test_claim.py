"""Task 8: Service.claim_interface (Ruling 20) and its undo."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from conftest import T0, dump_state
from helpers.plan import save_square_room, svc_last
from lanscape_atlas.events import EVENT_CHANGE_APPLIED
from lanscape_atlas.observation import Observation
from lanscape_atlas.service import Placement, UndoConflict

PHONE = "00:00:5e:00:53:31"  # the phone's real (wifi) MAC
RMAC = "02:00:5e:00:53:32"  # its randomized MAC
OTHER = "00:00:5e:00:53:33"
IP = "192.0.2.31"
RIP = "192.0.2.32"


def obs(**kw):
    base = dict(
        source="arp", seen_at=T0, iface_kind=None, mac=None, ipv4=None, hostname=None, attrs={}
    )
    return Observation(**{**base, **kw})


def iface_id(db, mac):
    return db.execute("select id from interface where address = ?", (mac,)).fetchone()[0]


def owner(db, mac):
    return db.execute("select device_id from interface where address = ?", (mac,)).fetchone()[0]


def device_ids(db):
    return [r[0] for r in db.execute("select id from device order by id")]


def services(db, device_id):
    return sorted(
        (r["port"], r["proto"], r["name"])
        for r in db.execute("select * from service where device_id = ?", (device_id,))
    )


def curated(db):
    """dump_state without the ever-growing change log."""
    state = dump_state(db)
    del state["batch"], state["change"]
    return state


@pytest.fixture
def phone(ing, db):
    """Real phone device (id 1) and a separate device (id 2) for its random MAC."""
    ing.ingest(
        [
            obs(source="asusrouter", mac=PHONE, ipv4=IP, hostname="Pixel"),
            obs(
                source="asusrouter",
                mac=RMAC,
                ipv4=RIP,
                hostname="android-host",
                attrs={
                    "services": [
                        {"port": 5353, "proto": "udp", "name": "mdns-from-random"},
                        {"port": 8009, "proto": "tcp", "name": "cast"},
                    ]
                },
            ),
            obs(
                source="nmap_light",
                mac=PHONE,
                attrs={"services": [{"port": 5353, "proto": "udp", "name": "mdns"}]},
            ),
        ]
    )
    assert owner(db, PHONE) == 1 and owner(db, RMAC) == 2
    return 1, 2, iface_id(db, RMAC)


def test_claim_moves_interface_and_deletes_empty_device(svc, db, events, phone):
    target, source, rid = phone
    leases_before = db.execute("select id from ip_lease where interface_id = ?", (rid,)).fetchall()
    obs_before = db.execute("select id from observation where interface_id = ?", (rid,)).fetchall()
    src_row = dict(db.execute("select * from device where id = ?", (source,)).fetchone())
    src_row["_field_source"] = {
        r[0]: [r[1], r[2]]
        for r in db.execute(
            "select field, actor, set_at from field_source where entity='device' and entity_id=?",
            (source,),
        )
    }
    assert src_row["_field_source"]  # collector-named, so there is provenance to restore

    b = svc.claim_interface("user", rid, target)

    assert owner(db, RMAC) == target
    assert device_ids(db) == [target]
    # Leases and observations key on interface_id and so follow it.
    assert db.execute("select id from ip_lease where interface_id = ?", (rid,)).fetchall() == (
        leases_before
    )
    assert db.execute("select id from observation where interface_id = ?", (rid,)).fetchall() == (
        obs_before
    )
    # Source's field_source rows (collector-owned name) are gone with it.
    assert (
        db.execute(
            "select count(*) from field_source where entity_id = ? and entity = 'device'", (source,)
        ).fetchone()[0]
        == 0
    )
    changes = [
        (r["entity"], r["entity_id"], r["field"], r["old_value"], r["new_value"], r["old_actor"])
        for r in db.execute("select * from change where batch_id = ? order by id", (b,))
    ]
    assert changes[0] == (
        "interface",
        rid,
        "device_id",
        json.dumps(source),
        json.dumps(target),
        None,
    )
    assert changes[-1][:3] == ("device", source, "*")
    assert json.loads(changes[-1][3]) == src_row
    assert changes[-1][4] == "null"
    assert events.recorded[-1].name == EVENT_CHANGE_APPLIED
    assert events.recorded[-1].payload == {"batch_id": b, "actor": "user"}


def test_claim_service_collision_keeps_target_row(svc, db, phone):
    target, source, rid = phone
    svc.claim_interface("user", rid, target)
    assert services(db, target) == [(5353, "udp", "mdns"), (8009, "tcp", "cast")]
    assert services(db, source) == []


def test_claim_undo_restores_both_devices(ing, svc, db, phone):
    target, source, rid = phone
    before = curated(db)
    b = svc.claim_interface("user", rid, target)
    assert curated(db) != before
    svc.undo("user", b)
    assert curated(db) == before
    assert owner(db, RMAC) == source


def test_claim_undo_then_redo(svc, db, phone):
    target, source, rid = phone
    before = curated(db)
    b = svc.claim_interface("user", rid, target)
    after = curated(db)
    u = svc.undo("user", b)
    assert curated(db) == before
    svc.undo("user", u)
    assert curated(db) == after
    assert device_ids(db) == [target]


def test_claim_without_deletion_keeps_source(ing, svc, db):
    ing.ingest([obs(mac=PHONE), obs(mac=RMAC), obs(mac=OTHER)])
    # Give device 2 a second interface by claiming OTHER onto it first.
    svc.claim_interface("user", iface_id(db, OTHER), 2)
    before = curated(db)
    b = svc.claim_interface("claude", iface_id(db, RMAC), 1)
    assert device_ids(db) == [1, 2]
    assert owner(db, RMAC) == 1 and owner(db, OTHER) == 2
    assert db.execute("select count(*) from change where batch_id = ?", (b,)).fetchone()[0] == 1
    svc.undo("user", b)
    assert curated(db) == before


def test_claim_onto_current_owner_raises(svc, db, phone):
    target, source, rid = phone
    before = dump_state(db)
    with pytest.raises(ValueError, match="already"):
        svc.claim_interface("user", rid, source)
    assert dump_state(db) == before


def test_claim_unknown_target_or_interface(svc, db, phone):
    target, source, rid = phone
    before = dump_state(db)
    with pytest.raises(KeyError):
        svc.claim_interface("user", rid, 999)
    with pytest.raises(KeyError):
        svc.claim_interface("user", 999, target)
    with pytest.raises(ValueError):
        svc.claim_interface("collector:arp", rid, target)
    assert dump_state(db) == before


def _sources(db, device_id):
    return {
        r[0]: (r[1], r[2])
        for r in db.execute(
            "select field, actor, set_at from field_source where entity='device' and entity_id=?",
            (device_id,),
        )
    }


@pytest.mark.parametrize("actor", ["user", "claude"])
def test_claim_refuses_when_curated_field_conflicts(svc, db, phone, actor):
    """Ruling 42: a non-null curated source field that differs from the target's refuses."""
    target, source, rid = phone
    svc.update_device("user", target, {"name": "Pixel 8", "notes": "Nick's"})
    svc.update_device(actor, source, {"name": "Random Pixel", "notes": "my phone, randomized"})
    before = dump_state(db)
    with pytest.raises(ValueError) as ei:
        svc.claim_interface("user", rid, target)
    assert str(ei.value) == (
        f"claim would discard curated fields on device {source}: name, notes"
        " \u2014 rename or clear them first"
    )
    assert dump_state(db) == before
    assert not db.in_transaction


def test_claim_after_mark_known_succeeds(svc, db, phone):
    """Inbox "mark known" (trust) and scan_policy never block a claim."""
    target, source, rid = phone
    svc.update_device("user", source, {"trust": "known", "scan_policy": "never"})
    svc.claim_interface("user", rid, target)
    assert owner(db, RMAC) == target
    assert device_ids(db) == [target]


def test_claim_ignores_cleared_curated_fields(svc, db, phone):
    target, source, rid = phone
    svc.update_device("user", target, {"notes": "target notes"})
    svc.update_device("user", source, {"notes": "temp"})
    svc.update_device("user", source, {"notes": None})
    assert _sources(db, source)["notes"][0] == "user"
    svc.claim_interface("user", rid, target)
    assert device_ids(db) == [target]
    assert db.execute("select notes from device where id = ?", (target,)).fetchone()[0] == (
        "target notes"
    )


def test_claim_equal_curated_value_is_not_a_conflict(svc, db, phone):
    target, source, rid = phone
    svc.update_device("user", target, {"owner": "Nick"})
    svc.update_device("claude", source, {"owner": "Nick"})
    tgt_src = _sources(db, target)["owner"]
    svc.claim_interface("user", rid, target)
    assert device_ids(db) == [target]
    assert _sources(db, target)["owner"] == tgt_src


def test_claim_copies_curated_fields_onto_null_target(svc, db, clock, phone):
    """Named source onto an unnamed target: value and provenance move; undo restores exactly."""
    target, source, rid = phone
    with db:  # an unnamed target: no name, no provenance
        db.execute("update device set name = null where id = ?", (target,))
        db.execute(
            "delete from field_source where entity='device' and entity_id=? and field='name'",
            (target,),
        )
    svc.update_device("claude", source, {"name": "Nick's Pixel"})
    place = save_square_room(svc, "Kitchen")
    clock.advance(minutes=5)
    # Points go through place_devices (Ruling 19); the place_id comes from a different actor.
    svc.set_place("user", source, "Kitchen")
    clock.advance(minutes=5)
    svc.place_devices(
        "claude",
        [Placement(source, place, 0.3, 0.25)],
        based_on={source: svc_last(db, source)},
    )
    src_sources = _sources(db, source)
    assert src_sources["place_id"][0] == "user"
    before = dump_state(db)
    clock.advance(minutes=5)

    b = svc.claim_interface("user", rid, target)

    assert device_ids(db) == [target]
    row = db.execute("select * from device where id = ?", (target,)).fetchone()
    assert (row["name"], row["pos_x"], row["pos_y"], row["place_id"]) == (
        "Nick's Pixel",
        0.3,
        0.25,
        place,
    )
    tgt_sources = _sources(db, target)
    for field in ("name", "pos_x", "pos_y", "place_id"):
        assert tgt_sources[field] == src_sources[field]
    logged = {
        r["field"]
        for r in db.execute(
            "select field from change where batch_id = ? and entity = 'device' and entity_id = ?",
            (b, target),
        )
    }
    assert {"name", "pos_x", "pos_y", "place_id"} <= logged

    svc.undo("user", b)
    after = dump_state(db)
    del before["batch"], before["change"], after["batch"], after["change"]
    assert after == before


def test_claim_refuses_when_source_has_link(svc, db, phone, make_device):
    target, source, rid = phone
    sw, _ = svc.create_placeholder_device("user", "Switch", "switch")
    svc.add_link("user", source, sw, "wifi")
    before = dump_state(db)
    with pytest.raises(ValueError, match="link"):
        svc.claim_interface("user", rid, target)
    assert dump_state(db) == before


def test_claim_refuses_when_places_differ(svc, db, phone):
    target, source, rid = phone
    svc.set_place("user", target, "Office")
    svc.set_place("user", source, "Kitchen")
    # Strip the field_source so a place counts as curation even without provenance.
    with db:
        db.execute("delete from field_source where entity = 'device' and entity_id = ?", (source,))
    before = dump_state(db)
    with pytest.raises(ValueError, match="place_id"):
        svc.claim_interface("user", rid, target)
    assert dump_state(db) == before


def _layouts(db):
    return [tuple(r) for r in db.execute("select * from topology_layout order by device_id")]


def test_claim_with_layout_deletes_it_and_undo_restores(svc, db, phone):
    """Ruling 32: a saved layout never blocks a claim; undo/redo round-trip it."""
    target, source, rid = phone
    with db:
        db.execute("insert into topology_layout(device_id, x, y) values (?, 1.5, -2)", (source,))
        db.execute("insert into topology_layout(device_id, x, y) values (?, 7, 8)", (target,))
    before = curated(db)
    b = svc.claim_interface("user", rid, target)
    assert device_ids(db) == [target]
    assert _layouts(db) == [(target, 7, 8)]
    snap = json.loads(
        db.execute(
            "select old_value from change where batch_id = ? and entity = 'device' and field = '*'",
            (b,),
        ).fetchone()[0]
    )
    assert snap["_layout"] == {"x": 1.5, "y": -2}
    after = curated(db)
    u = svc.undo("user", b)
    assert curated(db) == before
    assert _layouts(db) == sorted([(source, 1.5, -2), (target, 7, 8)])
    svc.undo("user", u)
    assert curated(db) == after


def test_claim_undo_restores_layout_dump_state(svc, db, phone):
    target, source, rid = phone
    with db:
        db.execute("insert into topology_layout(device_id, x, y) values (?, 3, 4)", (source,))
    before = dump_state(db)
    b = svc.claim_interface("user", rid, target)
    svc.undo("user", b)
    after = dump_state(db)
    del before["batch"], before["change"], after["batch"], after["change"]
    assert after == before
    assert after["topology_layout"] == [(source, 3.0, 4.0)]


def test_claim_undo_restores_roaming_exactly(svc, db, phone):
    """Ruling 2: a deleted device's snapshot carries roaming, and undo restores it."""
    target, source, rid = phone
    with db:
        db.execute("update device set roaming = 1 where id = ?", (source,))
    before = dump_state(db)
    b = svc.claim_interface("user", rid, target)
    snap = json.loads(
        db.execute(
            "select old_value from change where batch_id = ? and entity = 'device' and field = '*'",
            (b,),
        ).fetchone()[0]
    )
    assert snap["roaming"] == 1
    svc.undo("user", b)
    after = dump_state(db)
    del before["batch"], before["change"], after["batch"], after["change"]
    assert after == before
    assert db.execute("select roaming from device where id = ?", (source,)).fetchone()[0] == 1


def test_later_ingest_does_not_block_undo(ing, svc, db, clock, phone):
    target, source, rid = phone
    b = svc.claim_interface("user", rid, target)
    clock.advance(minutes=2)
    ing.ingest(
        [
            obs(source="asusrouter", mac=RMAC, ipv4=RIP, seen_at=clock(), attrs={"band": "5g"}),
            obs(
                source="nmap_light",
                mac=RMAC,
                seen_at=clock(),
                attrs={"services": [{"port": 8009, "proto": "tcp", "name": "cast2"}]},
            ),
        ]
    )
    svc.undo("user", b)
    assert owner(db, RMAC) == source
    assert services(db, source) == [(5353, "udp", "mdns-from-random"), (8009, "tcp", "cast2")]
    row = db.execute("select band, last_seen from interface where id = ?", (rid,)).fetchone()
    assert tuple(row) == ("5g", "2026-01-01T00:02:00Z")


def test_later_claim_blocks_undo(ing, svc, db, phone):
    target, source, rid = phone
    ing.ingest([obs(mac=OTHER)])  # device 3
    b = svc.claim_interface("user", rid, target)
    b2 = svc.claim_interface("user", rid, 3)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b)
    assert exc.value.conflicting == [b2]
    svc.undo("user", b2)
    svc.undo("user", b)  # clear once the later claim is undone
    assert owner(db, RMAC) == source


def test_later_user_write_on_restored_device_blocks_redo(svc, db, phone):
    target, source, rid = phone
    b = svc.claim_interface("user", rid, target)
    u = svc.undo("user", b)
    b3 = svc.update_device("user", source, {"notes": "keep me"})
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", u)
    assert exc.value.conflicting == [b3]


def test_claim_onto_restored_device_blocks_redo(ing, svc, db, phone):
    target, source, rid = phone
    ing.ingest([obs(mac=OTHER)])  # device 3
    b = svc.claim_interface("user", rid, target)
    u = svc.undo("user", b)
    b3 = svc.claim_interface("user", iface_id(db, OTHER), source)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", u)
    assert b3 in exc.value.conflicting


def test_claim_then_presence_uses_new_owner(ing, svc, db, clock, phone):
    target, source, rid = phone
    svc.claim_interface("user", rid, target)
    clock.now = T0 + timedelta(minutes=20)
    ing.ingest([obs(mac=RMAC, seen_at=clock())])
    assert db.execute("select online from device where id = ?", (target,)).fetchone()[0] == 1


# ---------------------------------------------- Phase 2 fix round 1: F6 (Ruling 23a)


def _placement(db, device_id):
    return tuple(
        db.execute(
            "select place_id, pos_x, pos_y, roaming from device where id = ?", (device_id,)
        ).fetchone()
    )


def test_claim_onto_roaming_target_keeps_it_roaming_without_point(svc, db, phone):
    """A fixed source's point is not copied onto a roaming target (no point while roaming)."""
    target, source, rid = phone
    room = save_square_room(svc)
    svc.place_devices(
        "claude",
        [Placement(target, room, None, None, True), Placement(source, room, 0.3, 0.3)],
        based_on={},
    )
    before = curated(db)
    b = svc.claim_interface("user", rid, target)
    assert device_ids(db) == [target]
    assert _placement(db, target) == (room, None, None, 1)
    assert not {"pos_x", "pos_y"} & set(_sources(db, target))
    svc.undo("user", b)
    assert curated(db) == before


def test_claim_onto_fixed_target_in_same_room_copies_point(svc, db, phone):
    target, source, rid = phone
    room = save_square_room(svc)
    svc.place_devices(
        "claude",
        [Placement(target, room, None, None), Placement(source, room, 0.3, 0.25)],
        based_on={},
    )
    before = curated(db)
    b = svc.claim_interface("user", rid, target)
    assert _placement(db, target) == (room, 0.3, 0.25, 0)
    svc.undo("user", b)
    assert curated(db) == before


def test_claim_onto_unplaced_target_copies_place_and_point_together(svc, db, phone):
    target, source, rid = phone
    room = save_square_room(svc)
    svc.place_devices("claude", [Placement(source, room, 0.3, 0.25)], based_on={})
    b = svc.claim_interface("user", rid, target)
    assert _placement(db, target) == (room, 0.3, 0.25, 0)
    logged = {
        r[0]
        for r in db.execute(
            "select field from change where batch_id = ? and entity = 'device' and entity_id = ?",
            (b, target),
        )
    }
    assert {"place_id", "pos_x", "pos_y"} <= logged
