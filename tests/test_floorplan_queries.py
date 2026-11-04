"""Phase 2 Task 7: floor_plan_view read model."""

from __future__ import annotations

from conftest import T0
from helpers.plan import SQ_POINTS, save_square_room, svc_last
from lanscape_atlas.clock import iso
from lanscape_atlas.service import Placement
from lanscape_atlas.service.queries import floor_plan_view


def test_view_splits_placed_roaming_unplaced(svc, db, make_device, clock):
    room = save_square_room(svc)
    fixed = make_device(name="Dev A")
    phone = make_device(name="Phone A")
    loose = make_device(name="Dev B")
    svc.place_devices("user", [Placement(fixed, room, 0.3, 0.3, False)], based_on={fixed: 0})
    svc.place_devices("user", [Placement(phone, room, None, None, True)], based_on={phone: 0})
    view = floor_plan_view(db, now=clock())
    assert [p["device_id"] for p in view["placed"]] == [fixed]
    assert view["roaming"][0]["device_id"] == phone
    assert view["roaming"][0]["set_by"] == "user"
    assert [u["device_id"] for u in view["unplaced"]] == [loose]


def test_rooms_include_polygon_or_none(svc, db):
    save_square_room(svc)
    db.execute("INSERT INTO place(name, floor, polygon) VALUES ('Room B', NULL, NULL)")
    view = floor_plan_view(db, now=T0)
    by_name = {r["name"]: r for r in view["rooms"]}
    assert by_name["Room A"]["polygon"] == SQ_POINTS
    assert by_name["Room B"]["polygon"] is None
    assert set(by_name["Room A"]) == {"id", "name", "polygon"}


def test_plan_none_then_metadata_without_paths(svc, db):
    assert floor_plan_view(db, now=T0)["plan"] is None
    db.execute(
        "INSERT INTO floor_plan(id, width, height, sha256, file_name, updated_at)"
        " VALUES (1, 800, 600, 'abc', 'plan.png', '2026-01-01T00:00:00Z')"
    )
    plan = floor_plan_view(db, now=T0)["plan"]
    assert plan == {
        "width": 800,
        "height": 600,
        "file_name": "plan.png",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_online_last_batch_and_placeholders(svc, db, make_device, clock):
    room = save_square_room(svc)
    on = make_device(name="Dev A", online=1)
    off = make_device(name="Dev B", online=0)
    svc.place_devices(
        "user",
        [Placement(on, room, 0.2, 0.2, False), Placement(off, room, 0.3, 0.3, False)],
        based_on={on: 0, off: 0},
    )
    view = floor_plan_view(db, now=clock())
    rows = {p["device_id"]: p for p in view["placed"]}
    assert rows[on]["online"] is True and rows[off]["online"] is False
    assert rows[on]["last_batch"] == svc_last(db, on) > 0
    assert rows[on]["place_id"] == room and rows[on]["x"] == 0.2 and rows[on]["y"] == 0.2
    # A placeholder (no interfaces) is a normal fixed device.
    assert db.execute("SELECT count(*) FROM interface").fetchone()[0] == 0


def test_unplaced_last_batch_and_read_only(svc, db, make_device, clock):
    dev = make_device(name="Dev A")
    before = db.total_changes
    view = floor_plan_view(db, now=clock())
    assert view["unplaced"] == [
        {"device_id": dev, "name": "Dev A", "place_id": None, "last_batch": 0}
    ]
    assert db.total_changes == before


def test_roaming_stamp_reflects_same_room_fixed_to_roaming_toggle(svc, db, make_device, clock):
    room = save_square_room(svc)
    dev = make_device(name="Phone A")
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    clock.advance(minutes=5)
    based_on = {dev: svc_last(db, dev)}
    svc.place_devices("claude", [Placement(dev, room, None, None, True)], based_on=based_on)
    entry = floor_plan_view(db, now=clock())["roaming"][0]
    assert entry["set_by"] == "claude"
    assert entry["set_at"] == iso(clock())


def test_unplaced_entry_carries_room_id_when_roomed_without_point(svc, db, make_device, clock):
    room = save_square_room(svc)
    dev = make_device(name="Dev A")
    svc.update_device("user", dev, {"place_id": room})
    view = floor_plan_view(db, now=clock())
    assert [u["device_id"] for u in view["unplaced"]] == [dev]
    assert view["unplaced"][0]["place_id"] == room
    assert view["placed"] == [] and view["roaming"] == []


def test_unplaced_entry_without_room_has_none_place_id(svc, db, make_device, clock):
    dev = make_device(name="Dev B")
    view = floor_plan_view(db, now=clock())
    assert [u["device_id"] for u in view["unplaced"]] == [dev]
    assert view["unplaced"][0]["place_id"] is None
