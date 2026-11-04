"""Phase 2 Task 6 fix round 1: placement invariants across every write path and undo.

Spec section 3: a roaming device has no point; a point has a place_id; a fixed
device's point lies inside its room's polygon; undo restores place_id, pos_x,
pos_y and roaming exactly unless it would restore a point outside the room's
current polygon (then ``UndoConflict``).
"""

from __future__ import annotations

import pytest

from conftest import dump_state
from helpers.plan import save_square_room, svc_last
from lanscape_atlas.service import Placement, StaleWrite, UndoConflict, queries

SQ_B = [[0.6, 0.6], [0.9, 0.6], [0.9, 0.9], [0.6, 0.9]]
# Room A shrunk to its upper-right quarter: (0.15, 0.15) falls outside, (0.4, 0.4) stays in.
SHRUNK_A = [[0.3, 0.3], [0.5, 0.3], [0.5, 0.5], [0.3, 0.5]]
GROWN_A = [[0.1, 0.1], [0.7, 0.1], [0.7, 0.7], [0.1, 0.7]]


def _curated(db):
    state = dump_state(db)
    del state["batch"], state["change"]
    return state


def _placement(db, dev):
    return tuple(
        db.execute(
            "SELECT place_id, pos_x, pos_y, roaming FROM device WHERE id=?", (dev,)
        ).fetchone()
    )


def _place_id(db, name):
    return db.execute("SELECT id FROM place WHERE name=?", (name,)).fetchone()[0]


def _room_b(svc, db):
    svc.save_room("user", "Room B", SQ_B)
    return _place_id(db, "Room B")


def _fields(db, batch_id, dev):
    return sorted(
        r[0]
        for r in db.execute(
            "SELECT field FROM change WHERE batch_id=? AND entity='device' AND entity_id=?",
            (batch_id, dev),
        )
    )


def _place(svc, db, dev, room, x, y, roaming=False):
    return svc.place_devices(
        "user", [Placement(dev, room, x, y, roaming)], based_on={dev: svc_last(db, dev)}
    )


# ------------------------------------------------------------- F1 to F3: helpers


def test_save_square_room_returns_place_id_not_batch_id(svc, db, make_device):
    dev = make_device()
    svc.update_device("user", dev, {"notes": "an earlier batch"})
    room = save_square_room(svc)
    assert room == _place_id(db, "Room A")
    assert room != db.execute("SELECT MAX(id) FROM batch").fetchone()[0]
    assert save_square_room(svc) == room  # unchanged re-save still names the place


def test_stale_setup_body_is_stale(svc, db, stale_setup):
    body = stale_setup.body
    placements = [Placement(**p) for p in body["placements"]]
    based_on = {int(k): v for k, v in body["based_on"].items()}
    before = dump_state(db)
    with pytest.raises(StaleWrite) as exc:
        svc.place_devices("user", placements, based_on)
    assert exc.value.conflicting == [p.device_id for p in placements]
    assert dump_state(db) == before


def test_plan_seeded_creates_a_1600x1200_plan(db, plan_seeded):
    row = db.execute("SELECT width, height, sha256, file_name FROM floor_plan").fetchall()
    assert len(row) == 1
    width, height, sha256, file_name = row[0]
    assert (width, height) == (1600, 1200)
    assert sha256 and file_name


# ------------------------------------------------------------- F4: update_device


@pytest.mark.parametrize(
    "fields",
    [{"pos_x": 0.3}, {"pos_y": 0.3}, {"pos_x": None}, {"pos_x": 0.3, "pos_y": 0.3}],
)
def test_update_device_refuses_positions(svc, db, make_device, fields):
    save_square_room(svc)
    dev = make_device()
    before = dump_state(db)
    with pytest.raises(ValueError, match="place_devices"):
        svc.update_device("user", dev, {"notes": "x", **fields})
    assert dump_state(db) == before


def test_update_device_place_change_clears_point_in_same_batch(svc, db, make_device):
    room = save_square_room(svc)
    room_b = _room_b(svc, db)
    dev = make_device()
    _place(svc, db, dev, room, 0.3, 0.3)
    before = _curated(db)
    bid = svc.update_device("user", dev, {"place_id": room_b})
    assert _placement(db, dev) == (room_b, None, None, 0)
    assert _fields(db, bid, dev) == ["place_id", "pos_x", "pos_y"]
    assert svc_last(db, dev) == bid
    svc.undo("user", bid)
    assert _curated(db) == before


def test_update_device_place_none_clears_point(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, 0.3, 0.3)
    before = _curated(db)
    bid = svc.update_device("user", dev, {"place_id": None})
    assert _placement(db, dev) == (None, None, None, 0)
    svc.undo("user", bid)
    assert _curated(db) == before


def test_update_device_place_none_clears_roaming(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, None, None, roaming=True)
    before = _curated(db)
    bid = svc.update_device("user", dev, {"place_id": None})
    assert _placement(db, dev) == (None, None, None, 0)
    assert _fields(db, bid, dev) == ["place_id", "roaming"]
    svc.undo("user", bid)
    assert _curated(db) == before
    assert _placement(db, dev) == (room, None, None, 1)


def test_update_device_other_room_keeps_roaming(svc, db, make_device):
    room = save_square_room(svc)
    room_b = _room_b(svc, db)
    dev = make_device()
    _place(svc, db, dev, room, None, None, roaming=True)
    svc.update_device("user", dev, {"place_id": room_b})
    assert _placement(db, dev) == (room_b, None, None, 1)


def test_update_device_same_place_keeps_point(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, 0.3, 0.3)
    bid = svc.update_device("user", dev, {"place_id": room, "notes": "same room"})
    assert _placement(db, dev) == (room, 0.3, 0.3, 0)
    assert _fields(db, bid, dev) == ["notes"]


# ------------------------------------------------------------- F5: set_place


def test_set_place_other_room_clears_point_and_undo_restores(svc, db, make_device):
    room = save_square_room(svc)
    room_b = _room_b(svc, db)
    dev = make_device()
    _place(svc, db, dev, room, 0.3, 0.3)
    before = _curated(db)
    bid = svc.set_place("user", dev, "Room B")
    assert _placement(db, dev) == (room_b, None, None, 0)
    assert _fields(db, bid, dev) == ["place_id", "pos_x", "pos_y"]
    svc.undo("user", bid)
    assert _curated(db) == before
    assert _placement(db, dev) == (room, 0.3, 0.3, 0)


def test_set_place_new_place_clears_point(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, 0.3, 0.3)
    before = _curated(db)
    bid = svc.set_place("user", dev, "Room B")
    assert _placement(db, dev) == (_place_id(db, "Room B"), None, None, 0)
    svc.undo("user", bid)
    assert _curated(db) == before


def test_set_place_keeps_roaming(svc, db, make_device):
    room = save_square_room(svc)
    room_b = _room_b(svc, db)
    dev = make_device()
    _place(svc, db, dev, room, None, None, roaming=True)
    svc.set_place("user", dev, "Room B")
    assert _placement(db, dev) == (room_b, None, None, 1)


def test_set_place_same_room_keeps_point(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, 0.3, 0.3)
    assert svc.set_place("user", dev, "Room A") == 0
    assert _placement(db, dev) == (room, 0.3, 0.3, 0)


# ------------------------------------------------------------- F7: redraw


def test_redraw_clears_stranded_points_and_undo_restores_both(svc, db, make_device):
    room = save_square_room(svc)
    stranded, kept, roamer = make_device(), make_device(), make_device()
    svc.place_devices(
        "user",
        [
            Placement(stranded, room, 0.15, 0.15),
            Placement(kept, room, 0.4, 0.4),
            Placement(roamer, room, None, None, True),
        ],
        based_on={},
    )
    before = _curated(db)
    bid = svc.save_room("user", "Room A", SHRUNK_A)
    assert _placement(db, stranded) == (room, None, None, 0)
    assert _placement(db, kept) == (room, 0.4, 0.4, 0)
    assert _placement(db, roamer) == (room, None, None, 1)
    assert _fields(db, bid, stranded) == ["pos_x", "pos_y"]
    assert _fields(db, bid, kept) == []
    assert svc_last(db, stranded) == bid
    svc.undo("user", bid)
    assert _curated(db) == before
    assert _placement(db, stranded) == (room, 0.15, 0.15, 0)


def test_redraw_that_strands_nothing_logs_only_polygon(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, 0.4, 0.4)
    bid = svc.save_room("user", "Room A", SHRUNK_A)
    fields = [r[0] for r in db.execute("SELECT field FROM change WHERE batch_id=?", (bid,))]
    assert fields == ["polygon"]


# ------------------------------------------------------------- F8: undo vs current polygon


def test_undo_refused_when_restored_point_is_outside_current_polygon(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, 0.15, 0.15)
    move = _place(svc, db, dev, room, 0.4, 0.4)
    redraw = svc.save_room("user", "Room A", SHRUNK_A)  # (0.4, 0.4) stays inside
    assert _placement(db, dev) == (room, 0.4, 0.4, 0)
    before = dump_state(db)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", move)
    assert exc.value.conflicting == [redraw]
    assert dump_state(db) == before


def test_undo_allowed_again_once_redraw_is_undone(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, 0.15, 0.15)
    move = _place(svc, db, dev, room, 0.4, 0.4)
    redraw = svc.save_room("user", "Room A", SHRUNK_A)
    svc.undo("user", redraw)
    svc.undo("user", move)
    assert _placement(db, dev) == (room, 0.15, 0.15, 0)


def test_undo_of_redraw_refused_when_it_would_strand_a_later_point(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    grow = svc.save_room("user", "Room A", GROWN_A)
    later = _place(svc, db, dev, room, 0.6, 0.6)  # inside the grown room only
    before = dump_state(db)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", grow)
    assert exc.value.conflicting == [later]
    assert dump_state(db) == before


def test_undo_of_room_change_refused_when_later_point_is_outside_old_room(svc, db, make_device):
    room = save_square_room(svc)
    room_b = _room_b(svc, db)
    dev = make_device()
    _place(svc, db, dev, room, None, None, roaming=True)
    moved = svc.update_device("user", dev, {"place_id": room_b})  # roaming kept, no point
    fixed = _place(svc, db, dev, room_b, 0.7, 0.7)  # writes roaming/pos, not place_id
    before = dump_state(db)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", moved)
    assert exc.value.conflicting == [fixed]
    assert dump_state(db) == before


def test_last_placement_batch_counts_redraw_clear(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    _place(svc, db, dev, room, 0.15, 0.15)
    bid = svc.save_room("user", "Room A", SHRUNK_A)
    assert queries.last_placement_batch(db, dev) == bid
