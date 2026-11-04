"""Phase 2 Task 6: Service.place_devices (containment, stale writes, undo)."""

from __future__ import annotations

import json

import pytest

from conftest import dump_state
from helpers.plan import save_square_room, svc_last
from lanscape_atlas.service import Placement, StaleWrite, queries


def _curated(db):
    """dump_state without the append-only change log tables."""
    state = dump_state(db)
    del state["batch"], state["change"]
    return state


def _placement(db, dev):
    return tuple(
        db.execute(
            "SELECT place_id, pos_x, pos_y, roaming FROM device WHERE id=?", (dev,)
        ).fetchone()
    )


# ------------------------------------------------------------------ brief tests


def test_fixed_placement_inside_room_saves(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    assert tuple(
        db.execute("SELECT place_id, pos_x, pos_y FROM device WHERE id=?", (dev,)).fetchone()
    ) == (room, 0.3, 0.3)


def test_point_outside_room_refused_nothing_written(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    before = dump_state(db)
    with pytest.raises(ValueError, match="outside"):
        svc.place_devices("user", [Placement(dev, room, 0.9, 0.9, False)], based_on={dev: 0})
    assert dump_state(db) == before


def test_roaming_clears_point(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    svc.place_devices(
        "user", [Placement(dev, room, None, None, True)], based_on={dev: svc_last(db, dev)}
    )
    assert tuple(
        db.execute("SELECT roaming, pos_x, pos_y FROM device WHERE id=?", (dev,)).fetchone()
    ) == (1, None, None)


def test_stale_write_refused_with_conflicting_ids(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    seen = svc_last(db, dev)
    assert seen == 0
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: seen})
    before = dump_state(db)
    with pytest.raises(StaleWrite) as exc:
        svc.place_devices("user", [Placement(dev, room, 0.2, 0.2, False)], based_on={dev: seen})
    assert exc.value.conflicting == [dev]
    assert dump_state(db) == before


def test_batch_of_moves_is_one_batch(svc, db, make_device):
    room = save_square_room(svc)
    a, b = make_device(), make_device()
    bid = svc.place_devices(
        "user",
        [Placement(a, room, 0.2, 0.2, False), Placement(b, room, 0.3, 0.3, False)],
        based_on={a: 0, b: 0},
    )
    assert db.execute("SELECT COUNT(*) FROM change WHERE batch_id=?", (bid,)).fetchone()[0] >= 4
    assert {
        r[0] for r in db.execute("SELECT DISTINCT batch_id FROM change WHERE entity='device'")
    } == {bid}


def test_undo_restores_place_point_roaming_exactly(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    before = _curated(db)
    bid = svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    svc.undo("user", bid)
    assert _curated(db) == before


def test_collector_actor_refused(svc, make_device):
    room = save_square_room(svc)
    dev = make_device()
    with pytest.raises(ValueError):
        svc.place_devices(
            "collector:arp", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0}
        )


# ------------------------------------------------------------------ extra coverage


def test_claude_actor_accepted_and_provenance_recorded(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("claude", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    actors = {
        r[0]: r[1]
        for r in db.execute(
            "SELECT field, actor FROM field_source WHERE entity='device' AND entity_id=?", (dev,)
        )
    }
    assert actors == {"place_id": "claude", "pos_x": "claude", "pos_y": "claude"}


def test_publishes_change_applied(svc, events, make_device):
    room = save_square_room(svc)
    dev = make_device()
    bid = svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    assert events.recorded[-1].payload == {"batch_id": bid, "actor": "user"}


def test_only_changed_fields_logged(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    bid = svc.place_devices(
        "user", [Placement(dev, room, 0.3, 0.4, False)], based_on={dev: svc_last(db, dev)}
    )
    fields = [r[0] for r in db.execute("SELECT field FROM change WHERE batch_id=?", (bid,))]
    assert fields == ["pos_y"]


def test_unchanged_returns_zero_and_writes_nothing(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    before = dump_state(db)
    assert (
        svc.place_devices(
            "user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: svc_last(db, dev)}
        )
        == 0
    )
    assert dump_state(db) == before


def test_point_on_edge_counts_inside(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.1, 0.3, False)], based_on={dev: 0})
    assert _placement(db, dev) == (room, 0.1, 0.3, 0)


@pytest.mark.parametrize(
    "x, y",
    [(None, None), (0.3, None), (None, 0.3), (0.3, 0.3)],
)
def test_roaming_requires_place_and_no_point(svc, db, make_device, x, y):
    room = save_square_room(svc)
    dev = make_device()
    before = dump_state(db)
    if (x, y) == (None, None):
        bad = [Placement(dev, None, None, None, True)]
    else:
        bad = [Placement(dev, room, x, y, True)]
    with pytest.raises(ValueError, match="roaming"):
        svc.place_devices("user", bad, based_on={dev: 0})
    assert dump_state(db) == before


def test_point_without_place_refused(svc, db, make_device):
    dev = make_device()
    with pytest.raises(ValueError):
        svc.place_devices("user", [Placement(dev, None, 0.3, 0.3, False)], based_on={dev: 0})


def test_half_point_refused(svc, make_device):
    room = save_square_room(svc)
    dev = make_device()
    with pytest.raises(ValueError):
        svc.place_devices("user", [Placement(dev, room, 0.3, None, False)], based_on={dev: 0})


def test_place_without_polygon_refused_for_point(svc, db, make_device):
    dev = make_device()
    svc.set_place("user", dev, "Room B")
    place = db.execute("SELECT id FROM place WHERE name='Room B'").fetchone()[0]
    with pytest.raises(ValueError, match="polygon"):
        svc.place_devices(
            "user", [Placement(dev, place, 0.3, 0.3, False)], based_on={dev: svc_last(db, dev)}
        )


def test_unknown_place_refused(svc, make_device):
    dev = make_device()
    with pytest.raises(ValueError):
        svc.place_devices("user", [Placement(dev, 999, 0.3, 0.3, False)], based_on={dev: 0})


def test_unknown_device_raises_keyerror(svc):
    room = save_square_room(svc)
    with pytest.raises(KeyError):
        svc.place_devices("user", [Placement(999, room, 0.3, 0.3, False)], based_on={999: 0})


def test_duplicate_device_refused(svc, make_device):
    room = save_square_room(svc)
    dev = make_device()
    with pytest.raises(ValueError, match="more than once"):
        svc.place_devices(
            "user",
            [Placement(dev, room, 0.3, 0.3, False), Placement(dev, room, 0.2, 0.2, False)],
            based_on={dev: 0},
        )


def test_one_bad_placement_writes_nothing(svc, db, make_device):
    room = save_square_room(svc)
    a, b = make_device(), make_device()
    before = dump_state(db)
    with pytest.raises(ValueError, match="outside"):
        svc.place_devices(
            "user",
            [Placement(a, room, 0.3, 0.3, False), Placement(b, room, 0.9, 0.9, False)],
            based_on={a: 0, b: 0},
        )
    assert dump_state(db) == before


def test_stale_reports_all_conflicting_ids_sorted(svc, db, make_device):
    room = save_square_room(svc)
    a, b, c = make_device(), make_device(), make_device()
    svc.place_devices(
        "user",
        [Placement(b, room, 0.2, 0.2, False), Placement(a, room, 0.3, 0.3, False)],
        based_on={a: 0, b: 0},
    )
    with pytest.raises(StaleWrite) as exc:
        svc.place_devices(
            "user",
            [
                Placement(b, room, 0.4, 0.4, False),
                Placement(c, room, 0.4, 0.4, False),
                Placement(a, room, 0.4, 0.4, False),
            ],
            based_on={a: 0, b: 0, c: 0},
        )
    assert exc.value.conflicting == [a, b]


def test_missing_based_on_entry_counts_as_zero(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    with pytest.raises(StaleWrite):
        svc.place_devices("user", [Placement(dev, room, 0.2, 0.2, False)], based_on={})


def test_non_placement_write_is_not_stale(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    seen = svc_last(db, dev)
    svc.update_device("user", dev, {"notes": "edited elsewhere"})
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: seen})
    assert _placement(db, dev) == (room, 0.3, 0.3, 0)


def test_undo_batch_counts_for_staleness(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    bid = svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    u = svc.undo("user", bid)
    assert svc_last(db, dev) == u
    with pytest.raises(StaleWrite):
        svc.place_devices("user", [Placement(dev, room, 0.2, 0.2, False)], based_on={dev: bid})


def test_undo_of_roaming_on_restores_point_exactly(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    before = _curated(db)
    bid = svc.place_devices(
        "user", [Placement(dev, room, None, None, True)], based_on={dev: svc_last(db, dev)}
    )
    assert _placement(db, dev) == (room, None, None, 1)
    u = svc.undo("user", bid)
    assert _curated(db) == before
    assert _placement(db, dev) == (room, 0.3, 0.3, 0)
    svc.undo("user", u)  # redo
    assert _placement(db, dev) == (room, None, None, 1)


def test_unplace_clears_everything_and_undoes(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, None, None, True)], based_on={dev: 0})
    before = _curated(db)
    bid = svc.place_devices(
        "user", [Placement(dev, None, None, None, False)], based_on={dev: svc_last(db, dev)}
    )
    assert _placement(db, dev) == (None, None, None, 0)
    svc.undo("user", bid)
    assert _curated(db) == before


def test_room_assignment_without_point_allowed(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, None, None, False)], based_on={dev: 0})
    assert _placement(db, dev) == (room, None, None, 0)


def test_later_placement_blocks_undo(svc, db, make_device):
    from lanscape_atlas.service import UndoConflict

    room = save_square_room(svc)
    dev = make_device()
    bid = svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    svc.place_devices(
        "user", [Placement(dev, room, 0.2, 0.2, False)], based_on={dev: svc_last(db, dev)}
    )
    with pytest.raises(UndoConflict):
        svc.undo("user", bid)


def test_roaming_value_logged_as_int(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    bid = svc.place_devices("user", [Placement(dev, room, None, None, True)], based_on={dev: 0})
    row = db.execute(
        "SELECT old_value, new_value FROM change WHERE batch_id=? AND field='roaming'", (bid,)
    ).fetchone()
    assert (json.loads(row[0]), json.loads(row[1])) == (0, 1)


@pytest.mark.parametrize(
    "bad",
    [
        Placement(True, 1, 0.3, 0.3, False),  # type: ignore[arg-type]
        Placement(1, "1", 0.3, 0.3, False),  # type: ignore[arg-type]
        Placement(1, 1, "0.3", 0.3, False),  # type: ignore[arg-type]
        Placement(1, 1, 0.3, 0.3, 1),  # type: ignore[arg-type]
    ],
)
def test_bad_types_refused(svc, make_device, bad):
    save_square_room(svc)
    make_device()
    with pytest.raises(ValueError):
        svc.place_devices("user", [bad], based_on={1: 0})


def test_bad_based_on_refused(svc, make_device):
    room = save_square_room(svc)
    dev = make_device()
    with pytest.raises(ValueError):
        svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: "0"})  # type: ignore[dict-item]


# ------------------------------------------------------------------ queries


def test_last_placement_batch(svc, db, make_device):
    room = save_square_room(svc)
    dev, other = make_device(), make_device()
    assert queries.last_placement_batch(db, dev) == 0
    svc.update_device("user", dev, {"notes": "x"})
    assert queries.last_placement_batch(db, dev) == 0
    bid = svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    svc.place_devices("user", [Placement(other, room, 0.2, 0.2, False)], based_on={other: 0})
    assert queries.last_placement_batch(db, dev) == bid
    via_set_place = svc.set_place("user", dev, "Room B")
    assert queries.last_placement_batch(db, dev) == via_set_place


# ------------------------------------------------------------------ Ruling 2


def test_device_snapshot_carries_roaming(svc, db, make_device):
    dev = make_device(roaming=1)
    assert svc._snapshot("device", dev)["roaming"] == 1


def test_legacy_device_snapshot_without_roaming_reinserts(svc, db):
    """A v1 change row (no ``roaming`` key) still undoes: roaming defaults to 0."""
    dev, created = svc.create_placeholder_device("user", "Switch", "switch")
    u = svc.undo("user", created)  # deletes the device, logs its snapshot
    with db:
        raw = db.execute(
            "SELECT id, old_value FROM change WHERE batch_id=? AND field='*'", (u,)
        ).fetchone()
        snap = json.loads(raw[1])
        snap.pop("roaming")
        db.execute("UPDATE change SET old_value=? WHERE id=?", (json.dumps(snap), raw[0]))
    svc.undo("user", u)
    assert db.execute("SELECT roaming FROM device WHERE id=?", (dev,)).fetchone()[0] == 0
