"""Blank plans, plan scale and units (spec §5.2): writes, refusals, and exact undo/redo."""

from __future__ import annotations

import json
import math

import pytest

from conftest import dump_state
from helpers.plan import SQ_POINTS, save_square_room
from lanscape_atlas.service import Placement, UndoConflict

A = "a" * 64
B = "b" * 64


def _state(db):
    """Everything but the change log itself: what an exact undo/redo must reproduce."""
    s = dump_state(db)
    del s["change"], s["batch"]
    return s


def _plan(db):
    row = db.execute("SELECT * FROM floor_plan WHERE id = 1").fetchone()
    return None if row is None else dict(row)


def _round_trip(svc, db, write):
    """Run ``write``; check undo restores the state before and redo the state after."""
    before = _state(db)
    bid = write()
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


def _blank(svc, w=12_000, h=9_000, units="imperial"):
    return svc.create_blank_plan("user", width_mm=w, height_mm=h, units=units)


# ------------------------------------------------------------- create_blank_plan


def test_blank_plan_row(svc, db, events):
    bid = _blank(svc, 12_000.4, 9_000, "metric")
    assert _plan(db) == {
        "id": 1,
        "width": 12_000,
        "height": 9_000,
        "sha256": None,
        "file_name": None,
        "updated_at": "2026-01-01T00:00:00Z",
        "width_mm": 12_000.0,
        "units": "metric",
    }
    assert events.recorded[-1].payload == {"batch_id": bid, "actor": "user"}
    rows = db.execute("SELECT entity, field, old_value FROM change WHERE batch_id = ?", (bid,))
    assert [tuple(r) for r in rows] == [("floor_plan", "*", "null")]
    sources = {r[0]: r[1] for r in db.execute("SELECT field, actor FROM field_source")}
    assert sources == {
        "width": "user",
        "height": "user",
        "width_mm": "user",
        "units": "user",
        "updated_at": "user",
    }


def test_blank_create_undo_redo_exact(svc, db):
    _round_trip(svc, db, lambda: _blank(svc))
    assert _plan(db) is None


@pytest.mark.parametrize("side", [1_000, 300_000, 1_000.0])
def test_blank_side_bounds_accepted(svc, db, side):
    _blank(svc, side, side)
    assert _plan(db)["width"] == side


@pytest.mark.parametrize("side", [999, 300_001, 999.9, 0, -5_000, math.nan, math.inf])
@pytest.mark.parametrize("which", ["width", "depth"])
def test_blank_side_bounds_refused(svc, db, side, which):
    before = dump_state(db)
    w, h = (side, 5_000) if which == "width" else (5_000, side)
    with pytest.raises(ValueError, match=which):
        _blank(svc, w, h)
    assert dump_state(db) == before


@pytest.mark.parametrize("bad", [True, "5000", None])
def test_blank_side_must_be_a_number(svc, bad):
    with pytest.raises(ValueError, match="width"):
        svc.create_blank_plan("user", width_mm=bad, height_mm=5_000, units="metric")


@pytest.mark.parametrize("units", ["", "Imperial", "furlongs", None])
def test_blank_units_refused(svc, db, units):
    with pytest.raises(ValueError, match="units"):
        _blank(svc, units=units)
    assert _plan(db) is None


def test_blank_refused_when_a_plan_exists(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    before = dump_state(db)
    with pytest.raises(ValueError, match="already exists"):
        _blank(svc)
    assert dump_state(db) == before


def test_blank_collector_actor_refused(svc):
    with pytest.raises(ValueError, match="actor"):
        svc.create_blank_plan("collector:arp", width_mm=5_000, height_mm=5_000, units="metric")


# ------------------------------------------------------------- image uploads


def test_first_image_upload_is_a_row_level_creation(svc, db):
    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    plan = _plan(db)
    assert (plan["width_mm"], plan["units"], plan["sha256"]) == (None, "imperial", A)
    rows = db.execute("SELECT field, old_value FROM change WHERE batch_id = ?", (bid,))
    assert [tuple(r) for r in rows] == [("*", "null")]


def test_first_image_upload_undo_redo_exact(svc, db):
    _round_trip(
        svc,
        db,
        lambda: svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png"),
    )


def test_image_replacement_undo_redo_exact(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    svc.set_plan_scale("user", width_mm=10_000)
    _round_trip(
        svc,
        db,
        lambda: svc.set_floor_plan("claude", width=800, height=600, sha256=B, file_name="y.png"),
    )
    assert _plan(db)["width_mm"] == 10_000


def test_image_over_blank_keeps_scale_and_units(svc, db):
    _blank(svc, 12_000, 9_000, "metric")
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    plan = _plan(db)
    assert plan == {
        "id": 1,
        "width": 1600,
        "height": 1200,
        "sha256": A,
        "file_name": "x.png",
        "updated_at": "2026-01-01T00:00:00Z",
        "width_mm": 12_000.0,
        "units": "metric",
    }


def test_image_over_blank_undo_redo_exact(svc, db, clock):
    _blank(svc)
    svc.set_plan_units("user", "metric")
    clock.advance(minutes=5)
    _round_trip(
        svc,
        db,
        lambda: svc.set_floor_plan("claude", width=1600, height=1200, sha256=A, file_name="x.png"),
    )
    assert _plan(db)["sha256"] is None


def test_image_over_blank_any_aspect_without_rooms(svc, db, make_device):
    _blank(svc, 12_000, 9_000)
    svc.set_place("user", make_device(), "Room B")  # a room with no outline does not count
    svc.set_floor_plan("user", width=1000, height=2000, sha256=A, file_name="x.png")
    assert (_plan(db)["width"], _plan(db)["height"]) == (1000, 2000)


def test_image_over_blank_wrong_aspect_refused_with_a_room(svc, db):
    _blank(svc, 12_000, 9_000)
    save_square_room(svc)
    before = dump_state(db)
    with pytest.raises(ValueError, match="crop"):
        svc.set_floor_plan("user", width=1000, height=2000, sha256=A, file_name="x.png")
    assert dump_state(db) == before


def test_image_over_blank_same_aspect_accepted_with_a_room(svc, db):
    _blank(svc, 12_000, 9_000)
    save_square_room(svc)
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    assert _plan(db)["sha256"] == A


def test_image_over_blank_wrong_aspect_refused_with_a_shape(svc, db):
    """The plan_shape check works once #10's table exists (here a stand-in table)."""
    _blank(svc, 12_000, 9_000)
    db.execute("CREATE TABLE plan_shape (id INTEGER PRIMARY KEY, kind TEXT)")
    svc.set_floor_plan("user", width=1000, height=2000, sha256=A, file_name="x.png")
    svc.undo("user", int(db.execute("SELECT max(id) FROM batch").fetchone()[0]))
    db.execute("INSERT INTO plan_shape(kind) VALUES ('wall')")
    db.commit()
    with pytest.raises(ValueError, match="aspect"):
        svc.set_floor_plan("user", width=1000, height=2000, sha256=A, file_name="x.png")


def test_image_replacement_aspect_rule_unchanged(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    with pytest.raises(ValueError, match="aspect"):
        svc.set_floor_plan("user", width=1000, height=2000, sha256=B, file_name="y.png")


# ------------------------------------------------------------- scale and units


def test_scale_undo_redo_exact_from_none_and_from_a_value(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    _round_trip(svc, db, lambda: svc.set_plan_scale("user", width_mm=8_000))
    svc.set_plan_scale("user", width_mm=8_000)
    _round_trip(svc, db, lambda: svc.set_plan_scale("claude", width_mm=9_144.5))


def test_scale_unchanged_returns_zero(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    svc.set_plan_scale("user", width_mm=8_000)
    before = dump_state(db)
    assert svc.set_plan_scale("user", width_mm=8_000.0) == 0
    assert dump_state(db) == before


def test_scale_refused_on_blank_plan(svc, db):
    _blank(svc)
    before = dump_state(db)
    with pytest.raises(ValueError, match="blank"):
        svc.set_plan_scale("user", width_mm=8_000)
    assert dump_state(db) == before


def test_scale_refused_without_plan(svc):
    with pytest.raises(ValueError, match="no floor plan"):
        svc.set_plan_scale("user", width_mm=8_000)


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf, 1_000_001, True, "8000", None])
def test_scale_value_refused(svc, db, bad):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    with pytest.raises(ValueError, match="width_mm"):
        svc.set_plan_scale("user", width_mm=bad)
    assert _plan(db)["width_mm"] is None


def test_units_undo_redo_exact(svc, db):
    _blank(svc)
    _round_trip(svc, db, lambda: svc.set_plan_units("user", "metric"))
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    _round_trip(svc, db, lambda: svc.set_plan_units("claude", "metric"))


def test_units_unchanged_or_invalid(svc, db):
    with pytest.raises(ValueError, match="no floor plan"):
        svc.set_plan_units("user", "metric")
    _blank(svc)
    assert svc.set_plan_units("user", "imperial") == 0
    with pytest.raises(ValueError, match="units"):
        svc.set_plan_units("user", "cubits")


def test_scale_conflicts_with_later_image_over_blank_undo(svc, db):
    """A later row-level replacement blocks undoing an earlier field write on the plan."""
    bid = _blank(svc)
    units = svc.set_plan_units("user", "metric")
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    for target in (bid, units):
        with pytest.raises(UndoConflict):
            svc.undo("user", target)


# ------------------------------------------------- undo of creation vs drawing


def test_undo_blank_creation_refused_while_later_room_polygon(svc, db):
    bid = _blank(svc)
    svc.save_room("user", "Room A", SQ_POINTS)
    room_batch = int(db.execute("SELECT max(id) FROM batch").fetchone()[0])
    before = dump_state(db)
    with pytest.raises(UndoConflict) as err:
        svc.undo("user", bid)
    assert err.value.conflicting == [room_batch]
    assert dump_state(db) == before
    svc.undo("user", room_batch)
    svc.undo("user", bid)
    assert _plan(db) is None


def test_undo_first_image_refused_while_later_room_polygon(svc, db):
    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    save_square_room(svc)
    with pytest.raises(UndoConflict):
        svc.undo("user", bid)


def test_undo_creation_refused_while_later_plan_shape_change(svc, db):
    """#10's plan_shape rows are matched in the change log, with or without the table."""
    bid = _blank(svc)
    shape_batch = svc.begin_batch("user")
    svc._log_change(shape_batch, "plan_shape", 1, "*", None, {"id": 1, "kind": "wall"})
    db.commit()
    with pytest.raises(UndoConflict) as err:
        svc.undo("user", bid)
    assert err.value.conflicting == [shape_batch]


def test_undo_aspect_changing_image_over_blank_refused_while_later_room(svc, db):
    _blank(svc, 12_000, 9_000)
    bid = svc.set_floor_plan("user", width=1000, height=2000, sha256=A, file_name="x.png")
    save_square_room(svc)
    with pytest.raises(UndoConflict):
        svc.undo("user", bid)


def test_undo_same_aspect_image_over_blank_allowed_with_room(svc, db):
    _blank(svc, 12_000, 9_000)
    save_square_room(svc)
    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    svc.undo("user", bid)
    assert _plan(db)["sha256"] is None


def test_device_on_blank_plan_place_and_undo(svc, db, make_device):
    _blank(svc)
    room = save_square_room(svc)
    dev = make_device(name="Device A")
    before = _state(db)
    bid = svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    assert tuple(db.execute("SELECT pos_x, pos_y FROM device WHERE id=?", (dev,)).fetchone()) == (
        0.3,
        0.3,
    )
    svc.undo("user", bid)
    assert _state(db) == before


# ------------------------------------------------------- pre-v5 first upload


def _legacy_first_upload(db, *, at="2025-12-01T00:00:00Z"):
    """The rows a schema v4 ``set_floor_plan`` wrote for a first upload."""
    with db:
        db.execute(
            "INSERT INTO floor_plan(id, width, height, sha256, file_name, updated_at)"
            " VALUES (1, 1600, 1200, ?, ?, ?)",
            (A, A + ".png", at),
        )
        bid = db.execute("INSERT INTO batch(actor, at) VALUES ('user', ?)", (at,)).lastrowid
        for field, value in (
            ("sha256", A),
            ("width", 1600),
            ("height", 1200),
            ("file_name", A + ".png"),
            ("updated_at", at),
        ):
            db.execute(
                "INSERT INTO change(batch_id, entity, entity_id, field, old_value, new_value)"
                " VALUES (?, 'floor_plan', 1, ?, 'null', ?)",
                (bid, field, json.dumps(value)),
            )
            db.execute(
                "INSERT INTO field_source(entity, entity_id, field, actor, set_at)"
                " VALUES ('floor_plan', 1, ?, 'user', ?)",
                (field, at),
            )
    return bid


def test_pre_v5_first_upload_undo_redo_exact(svc, db):
    before_upload = _state(db)
    bid = _legacy_first_upload(db)
    uploaded = _state(db)
    undo = svc.undo("user", bid)
    assert _state(db) == before_upload
    redo = svc.undo("user", undo)
    assert _state(db) == uploaded
    assert (_plan(db)["width_mm"], _plan(db)["units"]) == (None, "imperial")
    svc.undo("user", redo)
    assert _state(db) == before_upload


def test_pre_v5_first_upload_undo_refused_while_later_room(svc, db):
    bid = _legacy_first_upload(db)
    save_square_room(svc)
    with pytest.raises(UndoConflict):
        svc.undo("user", bid)


def test_pre_v5_first_upload_then_v5_scale_both_undo(svc, db):
    """A scale written from NULL is a plain field write, not mistaken for a creation."""
    bid = _legacy_first_upload(db)
    scale = svc.set_plan_scale("user", width_mm=8_000)
    with pytest.raises(UndoConflict):
        svc.undo("user", bid)
    svc.undo("user", scale)
    assert _plan(db)["width_mm"] is None
    svc.undo("user", bid)
    assert _plan(db) is None


def test_redo_of_pre_v5_undo_snapshot_defaults_new_columns(svc, db):
    """A v4 undo logged a snapshot without width_mm/units; redoing it fills them in."""
    bid = _legacy_first_upload(db)
    snap = {
        "id": 1,
        "width": 1600,
        "height": 1200,
        "sha256": A,
        "file_name": A + ".png",
        "updated_at": "2025-12-01T00:00:00Z",
        "_field_source": {"sha256": ["user", "2025-12-01T00:00:00Z"]},
    }
    with db:
        undo = db.execute(
            "INSERT INTO batch(actor, at, undoes_batch_id) VALUES ('user', 't', ?)", (bid,)
        ).lastrowid
        db.execute(
            "INSERT INTO change(batch_id, entity, entity_id, field, old_value, new_value)"
            " VALUES (?, 'floor_plan', 1, '*', ?, 'null')",
            (undo, json.dumps(snap)),
        )
        db.execute("DELETE FROM floor_plan")
        db.execute("DELETE FROM field_source WHERE entity = 'floor_plan'")
    redo = svc.undo("user", undo)
    plan = _plan(db)
    assert (plan["sha256"], plan["width_mm"], plan["units"]) == (A, None, "imperial")
    assert db.execute("SELECT field, actor FROM field_source").fetchall()[0][:] == (
        "sha256",
        "user",
    )
    svc.undo("user", redo)
    assert _plan(db) is None
