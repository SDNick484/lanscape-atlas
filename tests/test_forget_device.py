"""Service.forget_device (issue #21): cascade, refusals, undo and its conflicts."""

from __future__ import annotations

import pytest

from conftest import T0, dump_state
from helpers.plan import save_square_room
from lanscape_atlas.events import EVENT_CHANGE_APPLIED
from lanscape_atlas.observation import Observation
from lanscape_atlas.service import UndoConflict

MAC = "02:00:5e:00:53:41"
IP = "192.0.2.41"


def obs(**kw):
    base = dict(
        source="arp", seen_at=T0, iface_kind=None, mac=None, ipv4=None, hostname=None, attrs={}
    )
    return Observation(**{**base, **kw})


@pytest.fixture
def seen(ing, db):
    """One discovered device (id 1) with an interface, a lease, a service and a layout."""
    ing.ingest(
        [
            obs(
                source="asusrouter",
                mac=MAC,
                ipv4=IP,
                hostname="android-host",
                attrs={"services": [{"port": 8009, "proto": "tcp", "name": "cast"}]},
            )
        ]
    )
    assert db.execute("select count(*) from device").fetchone()[0] == 1
    with db:
        db.execute("insert into topology_layout(device_id, x, y) values (1, 10.5, 20.5)")
    return 1


def counts(db):
    return {
        t: db.execute(f"select count(*) from {t}").fetchone()[0]
        for t in ("device", "interface", "ip_lease", "service", "topology_layout", "field_source")
    }


def stable(db):
    state = dump_state(db)
    del state["batch"], state["change"]
    return state


def test_forget_removes_device_and_children_in_one_batch(svc, db, events, seen):
    before = counts(db)
    assert before["interface"] == before["ip_lease"] == before["service"] == 1
    b = svc.forget_device("user", seen)
    assert counts(db) == dict.fromkeys(before, 0)
    rows = db.execute("select entity, field, new_value from change where batch_id = ?", (b,))
    got = [(r["entity"], r["field"], r["new_value"]) for r in rows]
    assert got == [("interface", "*", "null"), ("service", "*", "null"), ("device", "*", "null")]
    assert db.execute("select actor from batch where id = ?", (b,)).fetchone()[0] == "user"
    assert events.recorded[-1].name == EVENT_CHANGE_APPLIED
    assert events.recorded[-1].payload == {"batch_id": b, "actor": "user"}


def test_forget_undo_restores_values_ids_and_provenance(svc, db, seen):
    svc.update_device("user", seen, {"name": "Mine", "owner": "Sam"})
    svc.set_place("user", seen, "Den")
    before = stable(db)
    b = svc.forget_device("user", seen)
    assert stable(db) != before
    svc.undo("user", b)
    assert stable(db) == before
    # provenance came back with the values
    src = dict(
        db.execute(
            "select field, actor from field_source where entity = 'device' and entity_id = ?",
            (seen,),
        ).fetchall()
    )
    assert src["name"] == "user" and src["owner"] == "user"


def test_forget_undo_of_undo_forgets_again(svc, db, seen):
    before = stable(db)
    b = svc.forget_device("user", seen)
    u = svc.undo("user", b)
    assert stable(db) == before
    u2 = svc.undo("user", u)
    assert counts(db)["device"] == 0 and counts(db)["ip_lease"] == 0
    svc.undo("user", u2)  # and back once more
    assert stable(db) == before


def test_forget_placement_and_point_are_restored(svc, db, seen):
    place = save_square_room(svc)
    svc.update_device("user", seen, {"place_id": place})
    db.execute("update device set pos_x = 0.5, pos_y = 0.5 where id = ?", (seen,))
    db.commit()
    before = stable(db)
    b = svc.forget_device("user", seen)
    svc.undo("user", b)
    assert stable(db) == before


def test_forget_refuses_links_ble_and_scanner_and_writes_nothing(svc, db, make_device, seen):
    other = make_device(name="Switch")
    svc.add_link("user", seen, other, "wired")
    before = dump_state(db)
    with pytest.raises(ValueError, match="has links"):
        svc.forget_device("user", seen)
    assert dump_state(db) == before

    with db:
        db.execute("delete from link")
        db.execute(
            "insert into ble_identity(device_id, kind, value, created_at) values (?, 'address',"
            " '00:00:5e:00:53:01', 'x')",
            (seen,),
        )
    with pytest.raises(ValueError, match="Bluetooth identities"):
        svc.forget_device("user", seen)

    with db:
        db.execute("delete from ble_identity")
        db.execute(
            "insert into ble_scanner(source, name, device_id, first_seen, last_seen) values"
            " ('00:00:5e:00:53:aa', 'p', ?, 'x', 'x')",
            (seen,),
        )
    with pytest.raises(ValueError, match="Bluetooth scanner"):
        svc.forget_device("user", seen)
    assert counts(db)["device"] == 2


def test_forget_unknown_device_and_bad_actor(svc, seen):
    with pytest.raises(KeyError):
        svc.forget_device("user", 999)
    with pytest.raises(ValueError):
        svc.forget_device("nobody", seen)


def test_system_actor_can_forget(svc, db, seen):
    b = svc.forget_device("system", seen)
    assert db.execute("select actor from batch where id = ?", (b,)).fetchone()[0] == "system"


def test_seen_again_comes_back_new_and_undo_conflicts(svc, ing, db, seen):
    svc.update_device("user", seen, {"trust": "known", "name": "Mine"})
    b = svc.forget_device("user", seen)
    ing.ingest([obs(source="asusrouter", mac=MAC, ipv4=IP, hostname="android-host")])
    row = db.execute(
        "select d.id, d.trust, d.name from device d join interface i on i.device_id = d.id"
        " where i.address = ?",
        (MAC,),
    ).fetchone()
    assert row["id"] != seen and row["trust"] == "new"
    before = dump_state(db)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b)
    assert "seen again" in str(exc.value) and exc.value.conflicting == []
    assert dump_state(db) == before


def test_undo_conflicts_when_room_was_deleted(svc, db, seen):
    place = save_square_room(svc)
    svc.update_device("user", seen, {"place_id": place})
    b = svc.forget_device("user", seen)
    with db:  # rooms are deleted only by undo; simulate one that went away
        db.execute("delete from place where id = ?", (place,))
    with pytest.raises(UndoConflict, match="room"):
        svc.undo("user", b)


def test_interface_id_reuse_gets_a_fresh_id(svc, ing, db, seen):
    b = svc.forget_device("user", seen)
    # a different device takes the freed interface/lease ids (not AUTOINCREMENT)
    ing.ingest([obs(source="asusrouter", mac="02:00:5e:00:53:42", ipv4="192.0.2.42")])
    svc.undo("user", b)
    assert counts(db)["device"] == 2 and counts(db)["interface"] == 2
    assert counts(db)["ip_lease"] == 2
    assert (
        db.execute(
            "select count(*) from ip_lease l join interface i on i.id = l.interface_id"
        ).fetchone()[0]
        == 2
    )
