"""Drawn shapes (spec §6.1-6.2, issue #10): validation, save_shapes, stale writes, undo.

Synthetic shapes only. Walls and doors are validated and stored; furniture and labels
are refused until issue #11 registers them.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from conftest import dump_state
from helpers.plan import SQ_POINTS, save_square_room
from lanscape_atlas.floorplan.geometry import parse_door, parse_wall
from lanscape_atlas.service import (
    NewShape,
    ShapeEdit,
    StaleShapes,
    StaleWrite,
    UndoConflict,
)
from lanscape_atlas.service import queries as q
from lanscape_atlas.service.service import SHAPE_RULES, SHAPES_MAX

A = "a" * 64

WALL = {"points": [[0.1, 0.1], [0.4, 0.1]], "closed": False}
POLY = {"points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.3]], "closed": True}
DOOR = {"x": 0.25, "y": 0.1, "angle": 0.0, "width": 0.0667, "flip": 0}


def _state(db: Any) -> dict[str, Any]:
    s = dump_state(db)
    del s["change"], s["batch"]
    return s


def _blank(svc: Any) -> int:
    bid: int = svc.create_blank_plan("user", width_mm=12_000, height_mm=9_000, units="metric")
    return bid


_LATEST: Any = object()


def _save(svc: Any, *, creates=(), updates=(), deletes=(), based_on=_LATEST, actor="user") -> int:
    """``save_shapes`` based on the newest shapes batch unless ``based_on`` is given."""
    if based_on is _LATEST:
        based_on = q.last_shapes_batch(svc.conn)
    bid: int = svc.save_shapes(
        actor,
        creates=list(creates),
        updates=list(updates),
        deletes=list(deletes),
        based_on=based_on,
    )
    return bid


def _rows(db: Any) -> list[dict[str, Any]]:
    return [
        {**dict(r), "geometry": json.loads(r["geometry"])}
        for r in db.execute("SELECT * FROM plan_shape ORDER BY id")
    ]


def _ids(db: Any) -> list[int]:
    return [int(r[0]) for r in db.execute("SELECT id FROM plan_shape ORDER BY id")]


@pytest.fixture
def plan(svc: Any) -> int:
    return _blank(svc)


# ------------------------------------------------------------------- geometry


def test_parse_wall_canonical() -> None:
    got = parse_wall({"points": [[0, 1], [0.5, 0.25]], "closed": False})
    assert got == {"points": [[0.0, 1.0], [0.5, 0.25]], "closed": False}
    assert all(isinstance(v, float) for p in got["points"] for v in p)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "object"),
        ([[0, 0], [1, 1]], "object"),
        ({"points": [[0, 0], [1, 1]]}, "keys"),
        ({"points": [[0, 0], [1, 1]], "closed": False, "extra": 1}, "keys"),
        ({"points": [[0, 0], [1, 1]], "closed": 0}, "closed"),
        ({"points": [[0, 0]], "closed": False}, "2 to 500"),
        ({"points": [[0, i / 1000] for i in range(501)], "closed": False}, "2 to 500"),
        ({"points": "0,0 1,1", "closed": False}, "list"),
        ({"points": [[0, 0], [1]], "closed": False}, "pair"),
        ({"points": [[0, 0], [1, 1, 1]], "closed": False}, "pair"),
        ({"points": [[0, 0], [1.5, 1]], "closed": False}, r"\[0, 1\]"),
        ({"points": [[-0.01, 0], [1, 1]], "closed": False}, r"\[0, 1\]"),
        ({"points": [[0, 0], [math.nan, 1]], "closed": False}, "finite"),
        ({"points": [[0, 0], [math.inf, 1]], "closed": False}, "finite"),
        ({"points": [[0, 0], [10**400, 1]], "closed": False}, "finite"),
        ({"points": [[0, 0], [True, 1]], "closed": False}, "number"),
        ({"points": [[0, 0], ["1", 1]], "closed": False}, "number"),
        ({"points": [[0.2, 0.2], [0.2, 0.2]], "closed": False}, "same point"),
        ({"points": [[0.1, 0.1], [0.3, 0.1], [0.3, 0.1], [0.5, 0.5]], "closed": False}, "same"),
        ({"points": [[0.1, 0.1], [0.3, 0.1]], "closed": True}, "3 points"),
        ({"points": [[0.1, 0.1], [0.3, 0.1], [0.3, 0.3], [0.1, 0.1]], "closed": True}, "same"),
    ],
)
def test_parse_wall_refuses(raw: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_wall(raw)


def test_parse_wall_point_counts_at_limits() -> None:
    parse_wall({"points": [[0, 0], [1, 1]], "closed": False})
    parse_wall({"points": [[0, i / 1000] for i in range(500)], "closed": False})
    parse_wall({"points": [[0, 0], [1, 0], [1, 1]], "closed": True})


def test_parse_door_canonical() -> None:
    got = parse_door({"x": 0, "y": 1, "angle": -90, "width": 0.05, "flip": 3})
    assert got == {"x": 0.0, "y": 1.0, "angle": -90.0, "width": 0.05, "flip": 3}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "object"),
        ({**DOOR, "extra": 1}, "keys"),
        ({k: v for k, v in DOOR.items() if k != "flip"}, "keys"),
        ({**DOOR, "x": 1.01}, r"\[0, 1\]"),
        ({**DOOR, "y": -0.5}, r"\[0, 1\]"),
        ({**DOOR, "x": math.nan}, "finite"),
        ({**DOOR, "angle": math.inf}, "finite"),
        ({**DOOR, "angle": 361}, "angle"),
        ({**DOOR, "angle": "0"}, "number"),
        ({**DOOR, "width": 0}, "width"),
        ({**DOOR, "width": 1.5}, "width"),
        ({**DOOR, "width": math.nan}, "finite"),
        ({**DOOR, "flip": 4}, "flip"),
        ({**DOOR, "flip": -1}, "flip"),
        ({**DOOR, "flip": 1.0}, "flip"),
        ({**DOOR, "flip": True}, "flip"),
    ],
)
def test_parse_door_refuses(raw: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_door(raw)


def test_only_walls_and_doors_are_registered() -> None:
    assert set(SHAPE_RULES) == {"wall", "door"}


# ---------------------------------------------------------------- save_shapes


def test_create_wall_and_door(svc: Any, db: Any, events: Any, plan: int) -> None:
    bid = _save(svc, creates=[NewShape("wall", WALL), NewShape("door", DOOR)])
    assert bid > plan
    rows = _rows(db)
    assert [(r["kind"], r["geometry"], r["label"]) for r in rows] == [
        ("wall", WALL, None),
        ("door", {**DOOR, "angle": 0.0}, None),
    ]
    assert rows[0]["created_at"] == "2026-01-01T00:00:00Z"
    assert events.recorded[-1].payload == {"batch_id": bid, "actor": "user"}
    changes = db.execute(
        "SELECT entity, entity_id, field, old_value FROM change WHERE batch_id = ? ORDER BY id",
        (bid,),
    ).fetchall()
    assert [tuple(c) for c in changes] == [
        ("plan_shape", rows[0]["id"], "*", "null"),
        ("plan_shape", rows[1]["id"], "*", "null"),
    ]
    assert q.last_shapes_batch(db) == bid


def test_geometry_stored_as_canonical_json(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", {"closed": False, "points": [[0, 0], [1, 1]]})])
    raw = db.execute("SELECT geometry FROM plan_shape").fetchone()[0]
    assert raw == '{"closed":false,"points":[[0.0,0.0],[1.0,1.0]]}'


def test_view_carries_shapes_and_batch(svc: Any, db: Any, clock: Any, plan: int) -> None:
    view = q.floor_plan_view(db, now=clock())
    assert (view["shapes"], view["shapes_batch"]) == ([], 0)
    bid = _save(svc, creates=[NewShape("wall", POLY), NewShape("door", DOOR)])
    view = q.floor_plan_view(db, now=clock())
    ids = _ids(db)
    assert view["shapes"] == [
        {"id": ids[0], "kind": "wall", "geometry": POLY, "label": None},
        {"id": ids[1], "kind": "door", "geometry": {**DOOR, "angle": 0.0}, "label": None},
    ]
    assert view["shapes_batch"] == bid


def test_update_logs_only_changed_geometry(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL), NewShape("door", DOOR)])
    wall, door = _ids(db)
    moved = {"points": [[0.1, 0.2], [0.4, 0.2]], "closed": False}
    bid = _save(svc, updates=[ShapeEdit(wall, moved), ShapeEdit(door, DOOR)])
    rows = db.execute(
        "SELECT entity_id, field, old_value, new_value FROM change WHERE batch_id = ?", (bid,)
    ).fetchall()
    assert len(rows) == 1
    assert (rows[0][0], rows[0][1]) == (wall, "geometry")
    assert json.loads(json.loads(rows[0][2])) == WALL
    assert json.loads(json.loads(rows[0][3])) == moved
    assert _rows(db)[0]["geometry"] == moved


def test_unchanged_save_returns_zero(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL)])
    (wall,) = _ids(db)
    before = dump_state(db)
    assert _save(svc, updates=[ShapeEdit(wall, WALL)]) == 0
    assert _save(svc) == 0
    assert dump_state(db) == before


def test_delete_logs_snapshot(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL)])
    (wall,) = _ids(db)
    bid = _save(svc, deletes=[wall])
    assert _ids(db) == []
    old, new = db.execute(
        "SELECT old_value, new_value FROM change WHERE batch_id = ?", (bid,)
    ).fetchone()
    snap = json.loads(old)
    assert (snap["id"], snap["kind"], json.loads(snap["geometry"])) == (wall, "wall", WALL)
    assert new == "null"


def test_claude_actor_accepted_collector_refused(svc: Any, db: Any, plan: int) -> None:
    bid = _save(svc, creates=[NewShape("wall", WALL)], actor="claude")
    assert db.execute("SELECT actor FROM batch WHERE id = ?", (bid,)).fetchone()[0] == "claude"
    with pytest.raises(ValueError, match="actor"):
        _save(svc, creates=[NewShape("wall", WALL)], actor="collector:arp")


def test_needs_a_plan(svc: Any, db: Any) -> None:
    with pytest.raises(ValueError, match="no floor plan"):
        _save(svc, creates=[NewShape("wall", WALL)], based_on=0)
    assert _ids(db) == []


@pytest.mark.parametrize("kind", ["furniture", "label"])
def test_furniture_and_label_refused_for_now(svc: Any, db: Any, plan: int, kind: str) -> None:
    geometry = {"x": 0.5, "y": 0.5, "size": 0.02}
    with pytest.raises(ValueError, match="not supported yet"):
        _save(svc, creates=[NewShape(kind, geometry, "Desk")])
    assert _ids(db) == []


@pytest.mark.parametrize("kind", ["window", "", None, 3])
def test_unknown_kind_refused(svc: Any, db: Any, plan: int, kind: Any) -> None:
    with pytest.raises(ValueError, match="kind"):
        _save(svc, creates=[NewShape(kind, WALL)])


@pytest.mark.parametrize("label", ["Wall", ""])
def test_wall_and_door_take_no_label(svc: Any, db: Any, plan: int, label: str) -> None:
    with pytest.raises(ValueError, match="label"):
        _save(svc, creates=[NewShape("wall", WALL, label)])
    _save(svc, creates=[NewShape("door", DOOR)])
    (door,) = _ids(db)
    with pytest.raises(ValueError, match="label"):
        _save(svc, updates=[ShapeEdit(door, DOOR, label)])


def test_one_bad_shape_writes_nothing(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL)])
    (wall,) = _ids(db)
    before = dump_state(db)
    bad = {"points": [[0.1, 0.1], [0.1, 0.1]], "closed": False}
    with pytest.raises(ValueError, match="same point"):
        _save(
            svc,
            creates=[NewShape("door", DOOR), NewShape("wall", bad)],
            updates=[ShapeEdit(wall, POLY)],
        )
    assert dump_state(db) == before


def test_unknown_ids_raise_keyerror_and_write_nothing(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL)])
    (wall,) = _ids(db)
    before = dump_state(db)
    with pytest.raises(KeyError, match="999"):
        _save(svc, creates=[NewShape("door", DOOR)], updates=[ShapeEdit(999, WALL)])
    with pytest.raises(KeyError, match="998"):
        _save(svc, updates=[ShapeEdit(wall, POLY)], deletes=[998])
    assert dump_state(db) == before


def test_update_cannot_change_kind(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("door", DOOR)])
    (door,) = _ids(db)
    with pytest.raises(ValueError, match="keys"):
        _save(svc, updates=[ShapeEdit(door, WALL)])


@pytest.mark.parametrize(
    ("updates", "deletes", "message"),
    [
        ([ShapeEdit(1, WALL), ShapeEdit(1, WALL)], [], "more than once"),
        ([], [1, 1], "more than once"),
        ([ShapeEdit(1, WALL)], [1], "more than once"),
    ],
)
def test_ids_listed_once(svc: Any, db: Any, plan: int, updates, deletes, message) -> None:
    _save(svc, creates=[NewShape("wall", WALL)])
    with pytest.raises(ValueError, match=message):
        _save(svc, updates=updates, deletes=deletes)


@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_bad_ids_and_based_on_refused(svc: Any, plan: int, bad: Any) -> None:
    with pytest.raises(ValueError):
        _save(svc, deletes=[bad])
    with pytest.raises(ValueError):
        _save(svc, updates=[ShapeEdit(bad, WALL)])
    with pytest.raises(ValueError, match="based_on"):
        _save(svc, creates=[NewShape("wall", WALL)], based_on=bad)


def test_bad_items_refused(svc: Any, plan: int) -> None:
    with pytest.raises(ValueError, match="NewShape"):
        _save(svc, creates=[{"kind": "wall", "geometry": WALL}])
    with pytest.raises(ValueError, match="ShapeEdit"):
        _save(svc, updates=[{"id": 1, "geometry": WALL}])


def test_cap_counts_existing_creates_and_deletes(
    svc: Any, db: Any, plan: int, monkeypatch: Any
) -> None:
    monkeypatch.setattr("lanscape_atlas.service.service.SHAPES_MAX", 3)
    _save(svc, creates=[NewShape("wall", WALL), NewShape("door", DOOR)])
    wall, door = _ids(db)
    before = dump_state(db)
    with pytest.raises(ValueError, match="at most 3 shapes"):
        _save(svc, creates=[NewShape("door", DOOR), NewShape("door", DOOR)])
    assert dump_state(db) == before
    # Deleting one in the same save makes room for it.
    _save(svc, creates=[NewShape("door", DOOR), NewShape("door", DOOR)], deletes=[door])
    assert len(_ids(db)) == 3


def test_cap_is_5000() -> None:
    assert SHAPES_MAX == 5_000


def test_cap_at_5000_real(svc: Any, db: Any, plan: int) -> None:
    walls = [
        NewShape("wall", {"points": [[i / 5000, 0.0], [i / 5000, 1.0]], "closed": False})
        for i in range(SHAPES_MAX)
    ]
    _save(svc, creates=walls)
    assert db.execute("SELECT count(*) FROM plan_shape").fetchone()[0] == SHAPES_MAX
    with pytest.raises(ValueError, match="at most 5000 shapes"):
        _save(svc, creates=[NewShape("door", DOOR)])


# ----------------------------------------------------------------- stale writes


def test_stale_write_refused(svc: Any, db: Any, plan: int) -> None:
    seen = q.last_shapes_batch(db)
    assert seen == 0
    other = _save(svc, creates=[NewShape("wall", WALL)], based_on=seen)
    before = dump_state(db)
    with pytest.raises(StaleShapes) as err:
        _save(svc, creates=[NewShape("door", DOOR)], based_on=seen)
    assert isinstance(err.value, StaleWrite)
    assert err.value.conflicting == [other]
    assert "drawing changed" in str(err.value)
    assert dump_state(db) == before
    _save(svc, creates=[NewShape("door", DOOR)], based_on=other)


def test_stale_wins_over_an_id_deleted_elsewhere(svc: Any, db: Any, plan: int) -> None:
    """A shape deleted in another tab reports the reload message, not a 404."""
    first = _save(svc, creates=[NewShape("wall", WALL)])
    (wall,) = _ids(db)
    _save(svc, deletes=[wall], based_on=first)
    with pytest.raises(StaleShapes):
        _save(svc, updates=[ShapeEdit(wall, POLY)], based_on=first)


def test_undo_counts_as_a_shapes_batch(svc: Any, db: Any, plan: int) -> None:
    bid = _save(svc, creates=[NewShape("wall", WALL)])
    undo = svc.undo("user", bid)
    assert q.last_shapes_batch(db) == undo
    with pytest.raises(StaleShapes):
        _save(svc, creates=[NewShape("door", DOOR)], based_on=bid)


def test_other_writes_are_not_stale(svc: Any, db: Any, plan: int) -> None:
    seen = _save(svc, creates=[NewShape("wall", WALL)])
    save_square_room(svc)
    svc.set_plan_units("user", "imperial")
    _save(svc, creates=[NewShape("door", DOOR)], based_on=seen)


# --------------------------------------------------------------------- undo


def _round_trip(svc: Any, db: Any, write: Any) -> int:
    before = _state(db)
    bid: int = write()
    assert bid > 0
    after = _state(db)
    assert after != before
    undo = svc.undo("user", bid)
    assert _state(db) == before
    redo = svc.undo("user", undo)
    assert _state(db) == after
    svc.undo("user", redo)
    assert _state(db) == before
    return bid


def test_create_undo_redo_exact(svc: Any, db: Any, plan: int) -> None:
    _round_trip(
        svc, db, lambda: _save(svc, creates=[NewShape("wall", POLY), NewShape("door", DOOR)])
    )


def test_redo_reinserts_same_ids(svc: Any, db: Any, plan: int) -> None:
    bid = _save(svc, creates=[NewShape("wall", POLY), NewShape("door", DOOR)])
    ids = _ids(db)
    svc.undo("user", svc.undo("user", bid))
    assert _ids(db) == ids


def test_update_undo_redo_exact(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL), NewShape("door", DOOR)])
    wall, door = _ids(db)
    _round_trip(
        svc,
        db,
        lambda: _save(svc, updates=[ShapeEdit(wall, POLY), ShapeEdit(door, {**DOOR, "flip": 2})]),
    )


def test_delete_undo_redo_exact(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL), NewShape("door", DOOR)])
    wall, door = _ids(db)
    _save(svc, updates=[ShapeEdit(wall, POLY)])  # field_source on geometry, restored too
    _round_trip(svc, db, lambda: _save(svc, deletes=[wall, door]))
    assert _ids(db) == [wall, door]


def test_mixed_save_undo_redo_exact(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL), NewShape("door", DOOR)])
    wall, door = _ids(db)
    _round_trip(
        svc,
        db,
        lambda: _save(
            svc,
            creates=[NewShape("wall", POLY)],
            updates=[ShapeEdit(wall, POLY)],
            deletes=[door],
        ),
    )


def test_undo_create_refused_when_later_batch_touched_shape(svc: Any, db: Any, plan: int) -> None:
    bid = _save(svc, creates=[NewShape("wall", WALL)])
    (wall,) = _ids(db)
    later = _save(svc, updates=[ShapeEdit(wall, POLY)])
    before = dump_state(db)
    with pytest.raises(UndoConflict) as err:
        svc.undo("user", bid)
    assert err.value.conflicting == [later]
    assert dump_state(db) == before
    svc.undo("user", later)
    svc.undo("user", bid)
    assert _ids(db) == []


def test_undo_update_refused_when_later_delete(svc: Any, db: Any, plan: int) -> None:
    _save(svc, creates=[NewShape("wall", WALL)])
    (wall,) = _ids(db)
    bid = _save(svc, updates=[ShapeEdit(wall, POLY)])
    later = _save(svc, deletes=[wall])
    with pytest.raises(UndoConflict) as err:
        svc.undo("user", bid)
    assert err.value.conflicting == [later]


def test_undo_of_other_shapes_does_not_conflict(svc: Any, db: Any, plan: int) -> None:
    bid = _save(svc, creates=[NewShape("wall", WALL)])
    _save(svc, creates=[NewShape("door", DOOR)])
    svc.undo("user", bid)
    assert [r["kind"] for r in _rows(db)] == ["door"]


# ------------------------------------------------- #9's hook: plan and shapes


def test_plan_creation_undo_refused_while_shapes_exist(svc: Any, db: Any) -> None:
    created = _blank(svc)
    shapes = _save(svc, creates=[NewShape("wall", WALL)])
    before = dump_state(db)
    with pytest.raises(UndoConflict) as err:
        svc.undo("user", created)
    assert err.value.conflicting == [shapes]
    assert dump_state(db) == before
    svc.undo("user", shapes)
    svc.undo("user", created)
    assert db.execute("SELECT count(*) FROM floor_plan").fetchone()[0] == 0


def test_first_image_undo_refused_while_shapes_exist(svc: Any, db: Any) -> None:
    created = svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    _save(svc, creates=[NewShape("door", DOOR)])
    with pytest.raises(UndoConflict):
        svc.undo("user", created)


def test_aspect_check_counts_shapes(svc: Any, db: Any) -> None:
    _blank(svc)
    _save(svc, creates=[NewShape("wall", WALL)])
    with pytest.raises(ValueError, match="aspect"):
        svc.set_floor_plan("user", width=1000, height=2000, sha256=A, file_name="x.png")
    # Same aspect is still accepted, and with the shapes gone any aspect is.
    svc.set_floor_plan("user", width=1200, height=900, sha256=A, file_name="x.png")
    svc.undo("user", int(db.execute("SELECT max(id) FROM batch").fetchone()[0]))
    (wall,) = _ids(db)
    _save(svc, deletes=[wall])
    svc.set_floor_plan("user", width=1000, height=2000, sha256=A, file_name="x.png")


def test_aspect_changing_image_undo_refused_while_shapes(svc: Any, db: Any) -> None:
    _blank(svc)
    image = svc.set_floor_plan("user", width=1000, height=2000, sha256=A, file_name="x.png")
    _save(svc, creates=[NewShape("wall", WALL)])
    with pytest.raises(UndoConflict):
        svc.undo("user", image)


def test_shapes_do_not_touch_rooms_or_placement(svc: Any, db: Any, plan: int) -> None:
    room = save_square_room(svc)
    poly_before = db.execute("SELECT polygon FROM place WHERE id = ?", (room,)).fetchone()[0]
    _save(svc, creates=[NewShape("wall", {"points": SQ_POINTS, "closed": True})])
    assert db.execute("SELECT polygon FROM place WHERE id = ?", (room,)).fetchone()[0] == (
        poly_before
    )
