from __future__ import annotations

import json

import pytest

from conftest import dump_state

SQ = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]
SQ2 = [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6]]


def test_save_room_creates_and_stores_polygon(svc, db):
    svc.save_room("user", "Room A", SQ)
    row = db.execute("SELECT name, polygon FROM place").fetchone()
    assert row[0] == "Room A"
    assert json.loads(row[1]) == SQ


def test_save_room_stores_stable_text(svc, db):
    svc.save_room("user", "Room A", [(1, 0), (0, 1), (0.5, 0.5)])
    assert db.execute("SELECT polygon FROM place").fetchone()[0] == (
        "[[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]"
    )


def test_save_room_existing_name_updates_polygon_same_place(svc, db):
    svc.save_room("user", "Room A", SQ)
    pid = db.execute("SELECT id FROM place").fetchone()[0]
    svc.save_room("user", "Room A", SQ2)
    assert db.execute("SELECT COUNT(*) FROM place").fetchone()[0] == 1
    assert db.execute("SELECT id FROM place").fetchone()[0] == pid
    assert json.loads(db.execute("SELECT polygon FROM place").fetchone()[0]) == SQ2


@pytest.mark.parametrize("bad", [[[0.1, 0.1]], [[0.1, 2.0], [0.2, 0.2], [0.3, 0.1]]])
def test_save_room_rejects_bad_polygon_and_writes_nothing(svc, db, bad):
    before = dump_state(db)
    with pytest.raises(ValueError):
        svc.save_room("user", "Room A", bad)
    assert dump_state(db) == before


def test_save_room_rejects_blank_name_and_bad_actor(svc, db):
    before = dump_state(db)
    with pytest.raises(ValueError):
        svc.save_room("user", "  ", SQ)
    with pytest.raises(ValueError):
        svc.save_room("collector:x", "Room A", SQ)
    assert dump_state(db) == before


def test_rename_keeps_place_id_and_devices(svc, db, make_device):
    svc.save_room("user", "Room A", SQ)
    pid = db.execute("SELECT id FROM place").fetchone()[0]
    dev = make_device()
    svc.update_device("user", dev, {"place_id": pid})
    svc.rename_room("user", pid, "Room B")
    assert db.execute("SELECT place_id FROM device WHERE id=?", (dev,)).fetchone()[0] == pid
    assert db.execute("SELECT name FROM place WHERE id=?", (pid,)).fetchone()[0] == "Room B"


def test_rename_to_existing_name_refused(svc):
    svc.save_room("user", "Room A", SQ)
    svc.save_room("user", "Room B", SQ)
    with pytest.raises(ValueError, match="exists"):
        svc.rename_room("user", 1, "Room B")


def test_rename_blank_and_unknown(svc):
    svc.save_room("user", "Room A", SQ)
    with pytest.raises(ValueError):
        svc.rename_room("user", 1, " ")
    with pytest.raises(KeyError):
        svc.rename_room("user", 999, "Room C")


def test_undo_of_polygon_edit_restores_old(svc, db):
    svc.save_room("user", "Room A", SQ)
    bid = svc.save_room("user", "Room A", SQ2)
    svc.undo("user", bid)
    assert json.loads(db.execute("SELECT polygon FROM place").fetchone()[0]) == SQ


def test_undo_of_room_creation_deletes_place(svc, db):
    bid = svc.save_room("user", "Room A", SQ)
    svc.undo("user", bid)
    assert db.execute("SELECT COUNT(*) FROM place").fetchone()[0] == 0


def test_undo_of_rename_restores_name(svc, db):
    svc.save_room("user", "Room A", SQ)
    bid = svc.rename_room("user", 1, "Room B")
    svc.undo("user", bid)
    assert db.execute("SELECT name FROM place").fetchone()[0] == "Room A"


def test_save_room_strips_name_whitespace(svc, db):
    svc.save_room("user", "Room A", SQ)
    svc.save_room("user", "  Room A\t", SQ2)
    rows = db.execute("SELECT name, polygon FROM place").fetchall()
    assert len(rows) == 1 and rows[0][0] == "Room A"
    assert json.loads(rows[0][1]) == SQ2
    svc.save_room("user", " Room B ", SQ)
    names = [r[0] for r in db.execute("SELECT name FROM place ORDER BY id")]
    assert names == ["Room A", "Room B"]


def test_rename_room_strips_name_whitespace(svc, db):
    svc.save_room("user", "Room A", SQ)
    svc.save_room("user", "Room B", SQ)
    svc.rename_room("user", 1, "  Room C ")
    assert db.execute("SELECT name FROM place WHERE id = 1").fetchone()[0] == "Room C"
    with pytest.raises(ValueError, match="exists"):
        svc.rename_room("user", 1, " Room B")
    assert svc.rename_room("user", 1, " Room C ") == 0


def test_redo_of_room_creation_restores_room_and_provenance(svc, db):
    bid = svc.save_room("user", "Room A", SQ)
    before = tuple(db.execute("SELECT id, name, polygon FROM place").fetchone())
    prov = db.execute(
        "SELECT entity, entity_id, field, actor FROM field_source WHERE entity = 'place' "
        "ORDER BY field"
    ).fetchall()
    assert [p[2] for p in prov] == ["name", "polygon"]
    undo_bid = svc.undo("user", bid)
    assert db.execute("SELECT COUNT(*) FROM place").fetchone()[0] == 0
    svc.undo("user", undo_bid)  # undo the undo = redo
    assert tuple(db.execute("SELECT id, name, polygon FROM place").fetchone()) == before
    assert (
        db.execute(
            "SELECT entity, entity_id, field, actor FROM field_source WHERE entity = 'place' "
            "ORDER BY field"
        ).fetchall()
        == prov
    )
