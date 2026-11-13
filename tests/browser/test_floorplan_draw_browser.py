"""Playwright pass over Draw mode: walls, polylines, doors (spec §6.3, issue #10).

Synthetic plans and shapes only. Plan -> client positions come from the SVG overlay's
screen transform (what is drawn), not from the page's own conversion function. The
blank plan is 40 ft x 30 ft (12,192 x 9,144 mm), so it is not square: angles and
lengths are checked in aspect-corrected space.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from helpers.floorplan_fixtures import drag_to_room_centre
from helpers.plan import SQ_POINTS
from lanscape_atlas.floorplan.geometry import plan_length_mm, rect_corners

pytest.importorskip("playwright.sync_api")

W_MM, H_MM = 12_192, 9_144
ASPECT = H_MM / W_MM


# ------------------------------------------------------------------- helpers


def _db(tmp_path: Path) -> Path:
    return tmp_path / "live.db"


def _rows(db: Path, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
    conn = sqlite3.connect(db, timeout=5.0)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _shapes(db: Path) -> list[dict[str, Any]]:
    return [
        {"id": r[0], "kind": r[1], "geometry": json.loads(r[2])}
        for r in _rows(db, "SELECT id, kind, geometry FROM plan_shape ORDER BY id")
    ]


def _post(base: str, path: str, **kw: Any) -> httpx.Response:
    with httpx.Client(base_url=base) as c:
        c.get("/topology")
        return c.post(path, headers={"X-CSRF-Token": c.cookies.get("atlas_csrf", "")}, **kw)


def _blank(base: str, units: str = "imperial") -> None:
    size = {"width": "40", "depth": "30"} if units == "imperial" else {"width": "12", "depth": "9"}
    r = _post(base, "/floorplan/blank", data={**size, "units": units}, follow_redirects=False)
    assert r.status_code == 303


def _seed(base: str, db: Path, *shapes: tuple[str, dict[str, Any]]) -> list[int]:
    """Create shapes through the page route; returns their ids."""
    based_on = _rows(
        db, "SELECT coalesce(max(batch_id), 0) FROM change WHERE entity = 'plan_shape'"
    )
    body = {
        "creates": [{"kind": k, "geometry": g} for k, g in shapes],
        "updates": [],
        "deletes": [],
        "based_on": based_on[0][0],
    }
    r = _post(base, "/floorplan/shapes", json=body)
    assert r.status_code == 200, r.text
    return [s["id"] for s in _shapes(db)][-len(shapes) :]


def _client(page: Any, x: float, y: float) -> tuple[float, float]:
    """Client pixels of normalized plan point (x, y), from the overlay's screen CTM."""
    got = page.evaluate(
        """([x, y]) => {
        const svg = document.getElementById('plan-svg');
        const vb = svg.viewBox.baseVal;
        const p = new DOMPoint(x * vb.width, y * vb.height).matrixTransform(svg.getScreenCTM());
        return [p.x, p.y];
    }""",
        [x, y],
    )
    return float(got[0]), float(got[1])


def _click(page: Any, x: float, y: float, dx: float = 0, dy: float = 0) -> None:
    cx, cy = _client(page, x, y)
    page.mouse.click(cx + dx, cy + dy)


def _move(page: Any, x: float, y: float) -> None:
    page.mouse.move(*_client(page, x, y), steps=4)


def _open_draw(page: Any, base: str, tool: str = "select") -> None:
    page.goto(f"{base}/floorplan")
    page.click("#mode-draw")
    assert page.locator("#shape-draw").is_visible()
    page.click(f"#tool-{tool}")
    assert page.locator(f"#tool-{tool}").get_attribute("aria-pressed") == "true"


def _save(page: Any) -> None:
    page.click("#save-changes")
    page.wait_for_selector(".flash")
    assert page.locator(".flash").inner_text() == "Drawing saved."


def _dirty(page: Any) -> int:
    return int(page.evaluate("document.getElementById('save-changes').disabled ? 0 : 1"))


def _flat(points: Any) -> list[float]:
    return [float(v) for p in points for v in p]


def _len_mm(a: list[float], b: list[float]) -> float:
    return plan_length_mm(b[0] - a[0], b[1] - a[1], width=W_MM, height=H_MM, width_mm=W_MM)


def _corrected_angle(a: list[float], b: list[float]) -> float:
    return math.degrees(math.atan2((b[1] - a[1]) * ASPECT, b[0] - a[0]))


# ---------------------------------------------------------------- walls


def test_two_click_wall_with_typed_length(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    _click(page, 0.2, 0.5)
    _move(page, 0.6, 0.45)
    # Typing a digit sends it to the length input near the cursor.
    page.keyboard.type("10'")
    assert page.locator("#draw-length").is_visible()
    assert page.locator("#draw-length").input_value() == "10'"
    page.keyboard.press("Enter")
    assert page.locator(".fp-wall").count() == 1
    assert not page.locator("#draw-length").is_visible()
    _save(page)
    (wall,) = _shapes(_db(tmp_path))
    a, b = wall["geometry"]["points"]
    assert wall["kind"] == "wall" and wall["geometry"]["closed"] is False
    assert _len_mm(a, b) == pytest.approx(3048, abs=1)
    # Toward the pointer: right and a little up.
    assert b[0] > a[0] and b[1] < a[1]
    # Back in Draw mode after saving, with the wall and its length label.
    assert page.locator("#mode-draw").get_attribute("aria-pressed") == "true"
    assert page.locator(".fp-length").all_text_contents() == ["10' 0\""]


def test_typed_length_needs_a_unit_and_the_plan(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    _click(page, 0.9, 0.5)
    _move(page, 0.95, 0.5)
    page.keyboard.type("10")
    page.keyboard.press("Enter")
    assert "Add a unit" in page.locator("#fp-status").inner_text()
    page.fill("#draw-length", "30'")  # 0.9 + 30/40 runs off the plan
    page.keyboard.press("Enter")
    assert "runs off the plan" in page.locator("#fp-status").inner_text()
    assert page.locator(".fp-wall").count() == 0 and _dirty(page) == 0
    page.keyboard.press("Escape")
    assert not page.locator("#draw-length").is_visible()


def test_live_length_while_drawing(page: Any, live_server: str) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    _click(page, 0.25, 0.5)  # 10 ft from the left edge, on the grid
    _move(page, 0.5, 0.5)  # 20 ft
    assert page.locator(".fp-live-length").text_content() == "10' 0\""


def test_polyline_closes_on_first_point(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "polyline")
    corners = [(0.2, 0.2), (0.5, 0.2), (0.5, 0.6)]  # on the 1 ft grid
    for x, y in corners:
        _click(page, x, y)
    assert page.locator(".fp-draw-overlay .fp-draft-start").count() == 1
    _click(page, *corners[0], dx=3, dy=-2)
    assert page.locator(".fp-wall").count() == 1
    _save(page)
    (wall,) = _shapes(_db(tmp_path))
    assert wall["geometry"]["closed"] is True
    assert _flat(wall["geometry"]["points"]) == pytest.approx(_flat(corners), abs=1e-6)
    assert len(page.locator(".fp-length").all_text_contents()) == 3


def test_polyline_ends_on_double_click_and_enter(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "polyline")
    _click(page, 0.2, 0.2)
    _click(page, 0.4, 0.2)
    cx, cy = _client(page, 0.4, 0.4)
    page.mouse.dblclick(cx, cy)
    _click(page, 0.6, 0.6)
    _click(page, 0.8, 0.6)
    page.keyboard.press("Enter")
    _click(page, 0.1, 0.9)
    page.keyboard.press("Escape")  # cancels the third
    _save(page)
    walls = [s["geometry"] for s in _shapes(_db(tmp_path))]
    assert [w["closed"] for w in walls] == [False, False]
    assert _flat(walls[0]["points"]) == pytest.approx(_flat([[0.2, 0.2], [0.4, 0.2], [0.4, 0.4]]))
    assert _flat(walls[1]["points"]) == pytest.approx(_flat([[0.6, 0.6], [0.8, 0.6]]))


def test_double_click_on_one_spot_makes_no_zero_length_wall(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    cx, cy = _client(page, 0.3, 0.3)
    page.mouse.dblclick(cx, cy)
    page.mouse.dblclick(cx + 1, cy + 1)
    assert page.locator(".fp-wall").count() == 0 and _dirty(page) == 0
    # The draft is still waiting for a real second point.
    _click(page, 0.6, 0.3)
    assert page.locator(".fp-wall").count() == 1
    _save(page)
    (wall,) = _shapes(_db(tmp_path))
    a, b = wall["geometry"]["points"]
    assert a != b


def test_shift_locks_a_wall_exactly_horizontal(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    page.click("#snap-toggle")  # off: the lock alone must make it exact
    _click(page, 0.213, 0.377)
    page.keyboard.down("Shift")
    _move(page, 0.61, 0.401)
    _click(page, 0.61, 0.401)
    page.keyboard.up("Shift")
    _save(page)
    (wall,) = _shapes(_db(tmp_path))
    a, b = wall["geometry"]["points"]
    assert b[1] == a[1]  # exactly
    assert b[0] == pytest.approx(0.61, abs=0.003)


def test_shift_lock_at_45_is_aspect_corrected(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    page.click("#snap-toggle")
    _click(page, 0.2, 0.2)
    page.keyboard.down("Shift")
    _click(page, 0.5, 0.56)
    page.keyboard.up("Shift")
    _save(page)
    (wall,) = _shapes(_db(tmp_path))
    a, b = wall["geometry"]["points"]
    assert _corrected_angle(a, b) == pytest.approx(45, abs=0.01)


def test_snap_to_an_existing_endpoint(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    end = [0.2137, 0.3141]  # off the grid
    _seed(live_server, _db(tmp_path), ("wall", {"points": [[0.6, 0.3141], end], "closed": False}))
    _open_draw(page, live_server, "wall")
    _move(page, end[0], end[1])
    assert page.locator(".fp-snap").count() == 1
    _click(page, *end, dx=5, dy=-4)
    _click(page, 0.2, 0.8)
    _save(page)
    new = _shapes(_db(tmp_path))[-1]["geometry"]["points"]
    assert new[0] == end


def test_alt_suppresses_snapping(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    end = [0.2137, 0.3141]
    _seed(live_server, _db(tmp_path), ("wall", {"points": [[0.6, 0.3141], end], "closed": False}))
    _open_draw(page, live_server, "wall")
    page.keyboard.down("Alt")
    _click(page, *end, dx=5, dy=-4)
    page.keyboard.up("Alt")
    _click(page, 0.2, 0.8)
    _save(page)
    first = _shapes(_db(tmp_path))[-1]["geometry"]["points"][0]
    assert first != end
    # Neither a corner nor the 1 ft grid (0.025 x 0.0333...).
    assert abs(first[0] / 0.025 - round(first[0] / 0.025)) > 1e-3


def test_grid_snap_on_a_blank_plan(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server, "metric")  # 12 m x 9 m, 0.5 m grid
    _open_draw(page, live_server, "wall")
    _click(page, 0.2051, 0.3049)
    _click(page, 0.6012, 0.3071)
    _save(page)
    (wall,) = _shapes(_db(tmp_path))
    for x, y in wall["geometry"]["points"]:
        assert x * 12_000 / 500 == pytest.approx(round(x * 12_000 / 500), abs=1e-3)
        assert y * 9_000 / 500 == pytest.approx(round(y * 9_000 / 500), abs=1e-3)


def test_draw_room_snaps_to_a_wall_endpoint(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    end = [0.2137, 0.3141]
    _seed(live_server, _db(tmp_path), ("wall", {"points": [end, [0.6, 0.3141]], "closed": False}))
    page.goto(f"{live_server}/floorplan")
    page.click("#mode-layout")
    _click(page, *end, dx=4, dy=4)
    _click(page, 0.6, 0.6)
    _click(page, 0.2, 0.6)
    page.click("#room-close")
    page.fill("#room-name", "Room A")
    page.click("#room-save")
    page.wait_for_selector(".flash")
    polygon = json.loads(_rows(_db(tmp_path), "SELECT polygon FROM place")[0][0])
    assert polygon[0] == end
    # Corners away from walls are stored as before (4 places).
    assert polygon[1] == [round(v, 4) for v in polygon[1]]


# ---------------------------------------------------------------- doors


def test_door_on_a_wall_takes_its_angle(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    a, b = [0.2, 0.2], [0.6, 0.5]
    _seed(live_server, _db(tmp_path), ("wall", {"points": [a, b], "closed": False}))
    _open_draw(page, live_server, "door")
    _move(page, 0.4, 0.35)
    assert page.locator(".fp-door-preview").count() == 1
    _click(page, 0.4, 0.35)
    assert page.locator(".fp-door:not(.fp-door-preview)").count() == 1
    _save(page)
    door = _shapes(_db(tmp_path))[-1]
    g = door["geometry"]
    assert door["kind"] == "door"
    assert g["angle"] == pytest.approx(_corrected_angle(a, b), abs=1e-3)
    assert g["width"] == pytest.approx(812.8 / W_MM, abs=1e-6)  # 32 in
    assert g["flip"] == 0
    # The centre lies on the wall line, near where the pointer was.
    assert plan_length_mm(
        g["x"] - 0.4, g["y"] - 0.35, width=W_MM, height=H_MM, width_mm=W_MM
    ) == pytest.approx(0, abs=40)
    cross = (b[0] - a[0]) * (g["y"] - a[1]) - (b[1] - a[1]) * (g["x"] - a[0])
    assert cross == pytest.approx(0, abs=1e-5)
    # The wall is drawn with a gap where the door is: two pieces.
    d = page.locator(".fp-wall").get_attribute("d") or ""
    assert d.count("M") == 2


def test_door_needs_a_wall_and_metric_default(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server, "metric")
    _seed(
        live_server, _db(tmp_path), ("wall", {"points": [[0.1, 0.5], [0.9, 0.5]], "closed": False})
    )
    _open_draw(page, live_server, "door")
    _click(page, 0.5, 0.2)
    assert "Point at a wall" in page.locator("#fp-status").inner_text()
    assert _dirty(page) == 0
    _click(page, 0.5, 0.5)
    _save(page)
    g = _shapes(_db(tmp_path))[-1]["geometry"]
    assert g["width"] == pytest.approx(800 / 12_000, abs=1e-6)
    assert g["angle"] == 0


def test_door_flip_and_width_handles(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    (door,) = _seed(
        live_server,
        _db(tmp_path),
        ("door", {"x": 0.5, "y": 0.5, "angle": 0, "width": 0.1, "flip": 0}),
    )
    _open_draw(page, live_server, "select")
    assert page.locator("#door-flip").is_disabled()
    _click(page, 0.5, 0.5)
    assert page.locator(".fp-door.selected").count() == 1
    assert page.locator(".fp-handle").count() == 2
    page.click("#door-flip")
    page.click("#door-flip")
    page.click("#door-flip")
    # Drag the right-hand end from x 0.55 to 0.6: the opening grows to 0.2.
    page.mouse.move(*_client(page, 0.55, 0.5))
    page.mouse.down()
    page.mouse.move(*_client(page, 0.6, 0.5), steps=6)
    page.mouse.up()
    _save(page)
    g = _shapes(_db(tmp_path))[0]["geometry"]
    assert _shapes(_db(tmp_path))[0]["id"] == door
    assert g["flip"] == 3
    assert g["width"] == pytest.approx(0.2, abs=0.003)
    assert (g["x"], g["y"]) == (0.5, 0.5)


# ---------------------------------------------------------------- select


def test_select_move_and_delete(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    db = _db(tmp_path)
    wall, door = _seed(
        live_server,
        db,
        ("wall", {"points": [[0.2, 0.2], [0.4, 0.2]], "closed": False}),
        ("door", {"x": 0.7, "y": 0.7, "angle": 90, "width": 0.05, "flip": 0}),
    )
    _open_draw(page, live_server, "select")
    _click(page, 0.3, 0.2)
    assert page.locator(f".fp-wall.selected[data-shape-id='{wall}']").count() == 1
    # Drag the wall by its middle: +0.1 across, +0.1 down.
    page.mouse.move(*_client(page, 0.3, 0.2))
    page.mouse.down()
    page.mouse.move(*_client(page, 0.4, 0.3), steps=8)
    page.mouse.up()
    page.keyboard.press("ArrowRight")  # nudge 0.005
    # Drag a corner: the wall's end goes to (0.6, 0.5).
    page.mouse.move(*_client(page, 0.505, 0.3))
    page.mouse.down()
    page.mouse.move(*_client(page, 0.6, 0.5), steps=8)
    page.mouse.up()
    _click(page, 0.7, 0.7)
    assert page.locator(f".fp-door.selected[data-shape-id='{door}']").count() == 1
    page.keyboard.press("Delete")
    assert page.locator(".fp-door").count() == 0
    assert "unsaved" in page.locator("#fp-status").inner_text()
    _save(page)
    (moved,) = _shapes(db)
    assert moved["id"] == wall
    p0, p1 = moved["geometry"]["points"]
    assert p0 == pytest.approx([0.305, 0.3], abs=0.004)
    assert p1 == pytest.approx([0.6, 0.5], abs=0.004)


def test_corner_dragged_onto_its_neighbour_moves_back(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    _blank(live_server)
    _seed(
        live_server, _db(tmp_path), ("wall", {"points": [[0.2, 0.2], [0.5, 0.2]], "closed": False})
    )
    _open_draw(page, live_server, "select")
    _click(page, 0.35, 0.2)
    page.mouse.move(*_client(page, 0.5, 0.2))
    page.mouse.down()
    page.mouse.move(*_client(page, 0.2, 0.2), steps=8)
    page.mouse.up()
    assert "moved back" in page.locator("#fp-status").inner_text()
    assert _dirty(page) == 0


def test_points_clamp_to_the_plan_at_high_zoom(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    page.click("#snap-toggle")
    page.evaluate("window.LanscapeFP.view.showPoint(1, 1, 8)")
    stage = page.locator("#floorplan").bounding_box()
    assert stage is not None
    right, bottom = stage["x"] + stage["width"], stage["y"] + stage["height"]
    # A click near the bottom-right corner, then one past the plan's edge (dispatched:
    # no element is there to hit).
    page.mouse.click(right - 150, bottom - 40)
    page.evaluate(
        """([x, y]) => {
        const svg = document.getElementById('plan-svg');
        svg.dispatchEvent(new MouseEvent('pointermove', {clientX: x, clientY: y, bubbles: true}));
        svg.dispatchEvent(new MouseEvent('click', {clientX: x, clientY: y, bubbles: true}));
    }""",
        [right + 60, bottom + 60],
    )
    assert page.locator(".fp-wall").count() == 1
    page.click("#tool-select")
    # Drag the first corner far outside the stage: it stops at the edge.
    page.mouse.click(right - 150, bottom - 40)
    assert page.locator(".fp-wall.selected").count() == 1
    page.mouse.move(right - 150, bottom - 40)
    page.mouse.down()
    page.mouse.move(right + 200, bottom - 40, steps=8)
    page.mouse.up()
    _save(page)
    (wall,) = _shapes(_db(tmp_path))
    pts = wall["geometry"]["points"]
    assert all(0 <= v <= 1 for p in pts for v in p)
    assert pts[1] == [1.0, 1.0]
    assert pts[0][0] == 1.0


# ---------------------------------------------------------------- saving


def test_save_reload_persist_and_history_undo(page: Any, live_server: str, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    _click(page, 0.2, 0.2)
    _click(page, 0.5, 0.2)
    page.click("#tool-polyline")
    for x, y in [(0.2, 0.6), (0.4, 0.6), (0.4, 0.8)]:
        _click(page, x, y)
    page.keyboard.press("Enter")
    _save(page)
    assert len(_shapes(db)) == 2
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-wall").count() == 2
    assert page.locator(".fp-length").count() == 3
    assert page.locator("#mode-draw").get_attribute("aria-pressed") == "false"
    batch = int(_rows(db, "SELECT max(batch_id) FROM change WHERE entity = 'plan_shape'")[0][0])
    page.goto(f"{live_server}/history")
    page.click(f"#batch-{batch} button[type=submit]")
    page.wait_for_selector(f"#batch-{batch} .tag.unwanted")
    assert _shapes(db) == []
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-wall").count() == 0


def test_discard_reverts_and_unload_guard(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    _click(page, 0.2, 0.2)
    _click(page, 0.5, 0.2)
    dialogs: list[str] = []

    def dismiss(d: Any) -> None:
        dialogs.append(d.type)
        d.dismiss()

    page.on("dialog", dismiss)
    page.evaluate("window.location.href = '/topology'")
    page.wait_for_timeout(300)
    assert dialogs == ["beforeunload"]
    assert page.locator(".fp-wall").count() == 1
    page.click("#discard-changes")
    page.wait_for_load_state()
    assert page.locator(".fp-wall").count() == 0
    assert _shapes(_db(tmp_path)) == []


def test_stale_save_shows_reload_message_and_writes_nothing(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    db = _db(tmp_path)
    _blank(live_server)
    _open_draw(page, live_server, "wall")
    _click(page, 0.2, 0.2)
    _click(page, 0.5, 0.2)
    page.click("#tool-door")
    _click(page, 0.35, 0.2)
    # Another tab saves a drawing meanwhile.
    _seed(live_server, db, ("wall", {"points": [[0.1, 0.9], [0.9, 0.9]], "closed": False}))
    page.click("#save-changes")
    page.wait_for_selector(".flash.error")
    assert "drawing changed" in page.locator(".flash").inner_text()
    assert [s["geometry"]["points"] for s in _shapes(db)] == [[[0.1, 0.9], [0.9, 0.9]]]
    assert page.locator(".fp-wall").count() == 1  # the reloaded page shows the server's


def test_placement_and_shape_edits_are_never_pending_together(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    db = _db(tmp_path)
    _blank(live_server)
    room = {"name": "Room A", "polygon": json.dumps(SQ_POINTS)}
    assert _post(live_server, "/floorplan/rooms", data=room, follow_redirects=False).is_redirect
    conn = sqlite3.connect(db, timeout=5.0)
    with conn:
        dev = conn.execute(
            "INSERT INTO device(name, first_seen, last_seen, trust, online)"
            " VALUES ('Device One', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'new', 1)"
        ).lastrowid
    conn.close()
    page.goto(f"{live_server}/floorplan")
    # A pending placement locks Draw mode out.
    drag_to_room_centre(page, page.locator(f"[data-tray-device='{dev}']"))
    assert page.locator("#mode-draw").is_disabled()
    assert page.locator("#mode-draw").get_attribute("title") == "Save or discard your changes first"
    page.click("#discard-changes")
    page.wait_for_load_state()
    # A pending shape keeps the page in Draw mode: no placing until it is saved.
    _open_draw(page, live_server, "wall")
    _click(page, 0.6, 0.6)
    _click(page, 0.9, 0.6)
    for b in ("#mode-draw", "#mode-layout"):
        assert page.locator(b).is_disabled()
    drag_to_room_centre(page, page.locator(f"[data-tray-device='{dev}']"))
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    _save(page)
    assert [p.rsplit("/", 1)[-1] for p in posts] == ["shapes"]
    assert len(_shapes(db)) == 1
    assert _rows(db, "SELECT place_id FROM device WHERE id = ?", (dev,))[0][0] is None
    # After saving, Draw can be left and placing works again.
    page.click("#mode-draw")
    assert page.locator("#mode-draw").get_attribute("aria-pressed") == "false"


# ------------------------------------------------------------- display


def test_lengths_toggle_and_no_scale(
    page: Any, live_server: str, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    # An image plan without a scale: no lengths, no typed length, doors still work.
    _seed(
        live_server, _db(tmp_path), ("wall", {"points": [[0.1, 0.7], [0.9, 0.7]], "closed": False})
    )
    page.goto(f"{live_server}/floorplan")
    assert page.locator("#lengths-toggle").count() == 0
    assert page.locator(".fp-wall").count() == 1 and page.locator(".fp-length").count() == 0
    page.click("#mode-draw")
    page.click("#tool-wall")
    _click(page, 0.2, 0.8)
    page.keyboard.type("10'")
    assert not page.locator("#draw-length").is_visible()
    page.keyboard.press("Escape")
    page.click("#tool-door")
    _click(page, 0.5, 0.7)
    _save(page)
    g = _shapes(_db(tmp_path))[-1]["geometry"]
    assert g["width"] == pytest.approx(0.04)
    # With a scale the labels show, and the toggle hides them.
    r = _post(live_server, "/floorplan/scale", json={"width_mm": 10_000})
    assert r.status_code == 200
    page.goto(f"{live_server}/floorplan")
    assert page.locator("#lengths-toggle").is_checked()
    assert page.locator(".fp-length").all_text_contents() == ["26' 3\""]  # 0.8 x 10 m
    page.click("#lengths-toggle")
    assert page.locator(".fp-length").count() == 0


def test_layer_order_and_no_pointer_events_outside_draw(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    _blank(live_server)
    _seed(
        live_server, _db(tmp_path), ("wall", {"points": [[0.1, 0.3], [0.9, 0.3]], "closed": False})
    )
    room = {"name": "Room A", "polygon": json.dumps(SQ_POINTS)}
    assert _post(live_server, "/floorplan/rooms", data=room, follow_redirects=False).is_redirect
    page.goto(f"{live_server}/floorplan")
    order = page.evaluate(
        "[...document.getElementById('plan-svg').children].map(g => g.getAttribute('class'))"
    )
    assert order == [
        "fp-shapes",
        "fp-rooms",
        "fp-labels",
        "fp-texts",
        "fp-lengths",
        "fp-devices",
        "fp-draft",
        "fp-draw-overlay",
    ]
    # The wall is under the pointer but never the event target.
    for x in (0.3, 0.7):
        cx, cy = _client(page, x, 0.3)
        hit = page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y).getAttribute('class')", [cx, cy]
        )
        assert hit in ("fp-room", "fp-svg")

    # Its stroke stays the same on screen at any zoom.
    def stroke() -> float:
        return float(
            page.evaluate(
                """() => {
                const w = document.querySelector('.fp-wall');
                const sw = parseFloat(getComputedStyle(w).strokeWidth);
                const svg = document.getElementById('plan-svg');
                return sw * svg.getScreenCTM().a / (svg.viewBox.baseVal.width / svg.clientWidth);
            }"""
            )
        )

    at1 = stroke()
    page.evaluate("window.LanscapeFP.view.zoomTo(6)")
    assert stroke() == pytest.approx(at1, rel=0.05)


def test_angle_lock_geometry_is_aspect_corrected(page: Any, live_server: str) -> None:
    _blank(live_server)
    page.goto(f"{live_server}/floorplan")
    got = page.evaluate(
        """() => {
        const g = window.LanscapeFP.drawing.geom;
        return {
          diag: g.lockAngle([0.2, 0.2], [0.5, 0.5], 0.5),
          flat: g.lockAngle([0.2, 0.2], [0.5, 0.3], 0.5),
          tall: g.lockAngle([0.2, 0.2], [0.21, 0.5], 0.5),
          back: g.lockAngle([0.7, 0.4], [0.1, 0.38], 2),
          edge: g.lockAngle([0.9, 0.9], [1.5, 1.5], 1),
          same: g.lockAngle([0.3, 0.3], [0.3, 0.3], 0.5),
          angle: g.angleDeg(0.3, 0.3, 0.5),
          len: g.lengthMm([0, 0], [0.5, 1], 10000, 0.5),
          at: g.pointAtLength([0.1, 0.1], [0.9, 0.1], 3048, 12192, 0.75),
        };
    }"""
    )
    # On a plan twice as wide as deep, normalized (0.3, 0.3) is 26.6 degrees: it snaps
    # to 0 or 45 in corrected space, never to the normalized diagonal.
    dx, dy = got["diag"][0] - 0.2, (got["diag"][1] - 0.2) * 0.5
    assert math.degrees(math.atan2(dy, dx)) == pytest.approx(45)
    assert got["diag"] == pytest.approx([0.425, 0.65])
    assert got["flat"][1] == 0.2 and got["flat"][0] == pytest.approx(0.5)
    assert got["tall"][0] == 0.2 and got["tall"][1] == pytest.approx(0.5)
    assert got["back"][1] == 0.4 and got["back"][0] == pytest.approx(0.1)
    assert got["edge"] == pytest.approx([1.0, 1.0])
    assert got["same"] == [0.3, 0.3]
    assert got["angle"] == pytest.approx(math.degrees(math.atan2(0.15, 0.3)))
    assert got["len"] == pytest.approx(math.hypot(5000, 5000))
    assert got["at"] == pytest.approx([0.1 + 3048 / 12192, 0.1])


# ------------------------------------------- furniture, labels, areas (issue #11)


def _full(db: Path) -> list[dict[str, Any]]:
    return [
        {"id": r[0], "kind": r[1], "geometry": json.loads(r[2]), "label": r[3]}
        for r in _rows(db, "SELECT id, kind, geometry, label FROM plan_shape ORDER BY id")
    ]


def _seed_named(base: str, db: Path, kind: str, geometry: dict[str, Any], label: str) -> int:
    based_on = _rows(
        db, "SELECT coalesce(max(batch_id), 0) FROM change WHERE entity = 'plan_shape'"
    )
    body = {
        "creates": [{"kind": kind, "geometry": geometry, "label": label}],
        "updates": [],
        "deletes": [],
        "based_on": based_on[0][0],
    }
    r = _post(base, "/floorplan/shapes", json=body)
    assert r.status_code == 200, r.text
    return int(_full(db)[-1]["id"])


def _centre(page: Any, selector: str) -> tuple[float, float]:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _drag(page: Any, a: tuple[float, float], b: tuple[float, float]) -> None:
    page.mouse.move(*a)
    page.mouse.down()
    page.mouse.move(*b, steps=10)
    page.mouse.up()


def _rect_fits(rect: dict[str, float]) -> bool:
    return all(-2e-6 <= v <= 1 + 2e-6 for c in rect_corners(rect, aspect=ASPECT) for v in c)


def test_draw_rotate_and_resize_a_rectangle(page: Any, live_server: str, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _blank(live_server)  # 40 ft x 30 ft
    _open_draw(page, live_server, "furniture-rect")
    _drag(page, _client(page, 0.3, 0.3), _client(page, 0.5, 0.5))
    assert page.locator(".fp-furniture.unsaved").count() == 1
    assert page.locator(".fp-furniture-dims").text_content() == "8' 0\" \u00d7 6' 0\""
    page.fill("#shape-text", "Couch")
    assert page.locator(".fp-furniture-name").text_content() == "Couch"
    page.click("#tool-select")
    # Rotate: the handle above the top edge, dragged to the right of the centre, turns
    # the rectangle 90 degrees (aspect-corrected).
    cx, cy = _client(page, 0.4, 0.4)
    _drag(page, _centre(page, ".fp-handle-rotate"), (cx + 150, cy))
    # Resize: corner 2 turned to the lower left; dragging it down lengthens the width.
    corner = _centre(page, ".fp-handle[data-handle='2']")
    _drag(page, corner, (corner[0], corner[1] + 40))
    _save(page)
    (shape,) = _full(db)
    rect = shape["geometry"]["rect"]
    assert (shape["kind"], shape["label"]) == ("furniture", "Couch")
    assert rect["angle"] == pytest.approx(90.0, abs=0.01)
    assert rect["w"] > 0.2 + 0.01 and rect["h"] == pytest.approx(0.15, abs=0.004)
    # The opposite corner stayed where it was: local corner 0 of the turned rectangle.
    before = rect_corners({"cx": 0.4, "cy": 0.4, "w": 0.2, "h": 0.15, "angle": 90.0}, aspect=ASPECT)
    after = rect_corners(rect, aspect=ASPECT)
    assert after[0] == pytest.approx(before[0], abs=0.004)
    assert _rect_fits(rect)
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-furniture-name").text_content() == "Couch"
    assert page.locator(".fp-furniture-dims").count() == 1


def test_rectangle_drag_too_small_makes_nothing(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    _blank(live_server)
    _open_draw(page, live_server, "furniture-rect")
    cx, cy = _client(page, 0.4, 0.4)
    _drag(page, (cx, cy), (cx + 3, cy + 3))
    assert page.locator(".fp-furniture").count() == 0 and _dirty(page) == 0
    assert "Drag from one corner" in page.locator("#fp-status").inner_text()


def test_furniture_outline_by_clicks(page: Any, live_server: str, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _blank(live_server)
    _open_draw(page, live_server, "furniture-outline")
    for x, y in [(0.2, 0.6), (0.5, 0.6), (0.5, 0.7)]:
        _click(page, x, y)
    _move(page, 0.35, 0.8)
    assert page.locator(".fp-wall-draft").count() == 1
    _click(page, 0.35, 0.8)
    _click(page, 0.2, 0.6)  # the first corner closes it
    assert page.locator(".fp-furniture").count() == 1
    page.fill("#shape-text", "Island")
    # A polygon with fewer than 3 corners is no outline.
    _click(page, 0.7, 0.7)
    _click(page, 0.8, 0.7)
    page.keyboard.press("Enter")
    assert page.locator(".fp-furniture").count() == 1
    assert "at least 3" in page.locator("#fp-status").inner_text()
    _save(page)
    (shape,) = _full(db)
    assert (shape["kind"], shape["label"]) == ("furniture", "Island")
    assert len(shape["geometry"]["points"]) == 4 and "rect" not in shape["geometry"]
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-furniture-dims").count() == 0  # outlines show no dimensions


def test_place_a_label(page: Any, live_server: str, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _blank(live_server)
    _open_draw(page, live_server, "label")
    _click(page, 0.5, 0.2)
    assert page.locator("#draw-text").is_visible()
    page.keyboard.press("Enter")  # no text: nothing is placed
    assert page.locator(".fp-label").count() == 0
    page.keyboard.type("  Patio  ")
    page.keyboard.press("Enter")
    assert page.locator("#draw-text").is_hidden()
    assert page.locator(".fp-label").text_content() == "Patio"
    _click(page, 0.7, 0.4)
    page.keyboard.type("Never placed")
    page.keyboard.press("Escape")
    assert page.locator(".fp-label").count() == 1
    # Edit its text in Select: an empty box keeps the old text.
    page.click("#tool-select")
    _click(page, 0.5, 0.2)
    assert page.locator(".fp-label.selected").count() == 1
    page.fill("#shape-text", "")
    assert page.locator(".fp-label").text_content() == "Patio"
    page.fill("#shape-text", "Deck")
    _save(page)
    (shape,) = _full(db)
    assert (shape["kind"], shape["label"]) == ("label", "Deck")
    assert shape["geometry"]["size"] == 0.02
    assert shape["geometry"]["x"] == pytest.approx(0.5, abs=0.003)
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-label").text_content() == "Deck"


def test_label_and_furniture_move_delete_and_layers(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    db = _db(tmp_path)
    _blank(live_server)
    rect = {"rect": {"cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.1, "angle": 0.0}}
    rid = _seed_named(live_server, db, "furniture", rect, "Table")
    lid = _seed_named(live_server, db, "label", {"x": 0.2, "y": 0.2, "size": 0.02}, "Hall")
    _seed(live_server, db, ("wall", {"points": [[0.1, 0.5], [0.9, 0.5]], "closed": False}))
    page.goto(f"{live_server}/floorplan")
    # Furniture is the lowest layer, walls over it; the label is in the text layer.
    kinds = page.evaluate(
        "[...document.querySelectorAll('.fp-shapes > *')]"
        ".map(e => e.getAttribute('class').split(' ')[0])"
    )
    assert kinds == ["fp-furniture", "fp-wall"]
    assert page.locator(".fp-texts .fp-label").count() == 1
    page.click("#mode-draw")
    _click(page, 0.2, 0.2)
    assert page.locator(f".fp-label.selected[data-shape-id='{lid}']").count() == 1
    page.keyboard.press("Delete")
    assert page.locator(".fp-label").count() == 0
    # A press inside the furniture (off the wall) picks it; the wall wins where they meet.
    _click(page, 0.42, 0.53)
    assert page.locator(f".fp-furniture.selected[data-shape-id='{rid}']").count() == 1
    _click(page, 0.3, 0.5)
    assert page.locator(".fp-wall.selected").count() == 1
    _click(page, 0.42, 0.53)
    _drag(page, _client(page, 0.42, 0.53), _client(page, 0.52, 0.63))
    _save(page)
    shapes = {s["kind"]: s for s in _full(db)}
    assert set(shapes) == {"furniture", "wall"}
    moved = shapes["furniture"]["geometry"]["rect"]
    assert moved["cx"] == pytest.approx(0.6, abs=0.004) and moved["cy"] == pytest.approx(
        0.6, abs=0.004
    )
    assert shapes["furniture"]["label"] == "Table"


def test_names_with_markup_render_as_text(page: Any, live_server: str, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _blank(live_server)
    evil = '<img src=x onerror="window.__pwned = 1"><b>x</b>'
    rect = {"rect": {"cx": 0.4, "cy": 0.5, "w": 0.2, "h": 0.1, "angle": 0.0}}
    _seed_named(live_server, db, "furniture", rect, evil)
    _seed_named(live_server, db, "label", {"x": 0.7, "y": 0.2, "size": 0.02}, evil)
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-furniture-name").text_content() == evil
    assert page.locator(".fp-label").text_content() == evil
    assert page.locator("#plan-svg img, #plan-svg b, .fp-stage img[src='x']").count() == 0
    page.click("#mode-draw")
    page.click("#tool-select")
    _click(page, 0.7, 0.2)
    assert page.locator("#shape-text").input_value() == evil
    page.wait_for_timeout(150)
    assert page.evaluate("window.__pwned === undefined")
    assert page.locator("#shape-text").evaluate("e => e.parentElement.querySelector('b') === null")


def test_rotating_near_the_edge_stops_before_a_corner_leaves_the_plan(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    """The page never stores a rectangle with a corner off the plan; the server refuses one."""
    db = _db(tmp_path)
    _blank(live_server)  # 40 ft x 30 ft: aspect 0.75
    wide = {"rect": {"cx": 0.5, "cy": 0.5, "w": 0.9, "h": 0.05, "angle": 0.0}}
    _seed_named(live_server, db, "furniture", wide, "Bench")
    _open_draw(page, live_server, "select")
    _click(page, 0.5, 0.5)
    cx, cy = _client(page, 0.5, 0.5)
    # Turning it a quarter would need 0.9 / 0.75 = 1.2 plan heights.
    _drag(page, _centre(page, ".fp-handle-rotate"), (cx + 200, cy))
    _save(page)
    (shape,) = _full(db)
    rect = shape["geometry"]["rect"]
    assert 0 < abs(rect["angle"]) < 90 and _rect_fits(rect)
    assert abs(rect["angle"]) > 30  # it turned as far as it could, not just a hair
    # Moving it can't push a corner off the plan either.
    page.goto(f"{live_server}/floorplan")
    page.click("#mode-draw")
    page.click("#tool-select")
    _click(page, 0.5, 0.5)
    c2 = _client(page, 0.5, 0.5)
    _drag(page, c2, (c2[0] + 400, c2[1] + 400))
    _save(page)
    (shape,) = _full(db)
    assert _rect_fits(shape["geometry"]["rect"])


def test_room_shows_its_area_on_a_blank_plan(page: Any, live_server: str) -> None:
    _blank(live_server)  # 40 ft x 30 ft
    room = {"name": "Room A", "polygon": json.dumps(SQ_POINTS)}  # 0.4 x 0.4 of it
    assert _post(live_server, "/floorplan/rooms", data=room, follow_redirects=False).is_redirect
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-room-label").text_content() == "Room A"
    assert page.locator(".fp-room-area").text_content() == "\u2248 192 sq ft"  # 16 ft x 12 ft


def test_room_area_in_metres(page: Any, live_server: str) -> None:
    _blank(live_server, "metric")  # 12 m x 9 m
    room = {"name": "Room A", "polygon": json.dumps(SQ_POINTS)}
    assert _post(live_server, "/floorplan/rooms", data=room, follow_redirects=False).is_redirect
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-room-area").text_content() == "\u2248 17.3 m\u00b2"  # 4.8 x 3.6


def test_self_intersecting_room_shows_no_area(page: Any, live_server: str) -> None:
    """A bow-tie room has no meaningful area: the page shows none (never a negative one)."""
    _blank(live_server)
    bowtie = [[0.1, 0.1], [0.5, 0.5], [0.5, 0.1], [0.1, 0.5]]
    room = {"name": "Room B", "polygon": json.dumps(bowtie)}
    assert _post(live_server, "/floorplan/rooms", data=room, follow_redirects=False).is_redirect
    page.goto(f"{live_server}/floorplan")
    assert page.locator(".fp-room-label").text_content() == "Room B"
    assert page.locator(".fp-room-area").count() == 0
    assert "-" not in page.locator("#plan-svg").text_content()


def test_no_area_or_dimensions_without_a_scale(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    assert page.locator(".fp-room-label").count() == 1
    assert page.locator(".fp-room-area").count() == 0


def test_area_and_label_sizes_stay_constant_on_screen(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    db = _db(tmp_path)
    _blank(live_server)
    room = {"name": "Room A", "polygon": json.dumps(SQ_POINTS)}
    assert _post(live_server, "/floorplan/rooms", data=room, follow_redirects=False).is_redirect
    _seed_named(live_server, db, "label", {"x": 0.7, "y": 0.2, "size": 0.02}, "Patio")
    page.goto(f"{live_server}/floorplan")

    def on_screen(sel: str) -> float:
        return float(
            page.evaluate(
                """(sel) => {
                const t = document.querySelector(sel);
                const svg = document.getElementById('plan-svg');
                return parseFloat(t.getAttribute('font-size')) * svg.getScreenCTM().a;
            }""",
                sel,
            )
        )

    before = (on_screen(".fp-room-area"), on_screen(".fp-label"))
    page.evaluate("window.LanscapeFP.view.zoomTo(5)")
    after = (on_screen(".fp-room-area"), on_screen(".fp-label"))
    assert after == pytest.approx(before, rel=0.05)


def test_area_formatter_and_js_geometry_match_the_server(page: Any, live_server: str) -> None:
    _blank(live_server)
    page.goto(f"{live_server}/floorplan")
    got = page.evaluate(
        """() => {
        const u = window.LanscapeFP.units, g = window.LanscapeFP.drawing.geom;
        const sq = [[0.25, 0.2], [0.75, 0.2], [0.75, 0.8], [0.25, 0.8]];
        return {
          imperial: u.formatArea(180 * 304.8 * 304.8, 'imperial'),
          rounds: u.formatArea(180.4 * 304.8 * 304.8, 'imperial'),
          metric: u.formatArea(16.7e6, 'metric'),
          tenth: u.formatArea(16.74e6, 'metric'),
          bad: u.formatArea(NaN, 'metric'),
          area: g.polygonAreaMm2(sq, 0.75, 12000),
          rev: g.polygonAreaMm2(sq.slice().reverse(), 0.75, 12000),
          bow: g.polygonAreaMm2([[0.1, 0.1], [0.5, 0.5], [0.5, 0.1], [0.1, 0.5]], 0.75, 12000),
          corners: g.rectCorners({cx: 0.5, cy: 0.5, w: 0.4, h: 0.2, angle: 90}, 0.5),
          fits: g.rectFits({cx: 0.5, cy: 0.1, w: 0.4, h: 0.2, angle: 90}, 0.5),
        };
    }"""
    )
    assert got["imperial"] == "≈ 180 sq ft" and got["rounds"] == "≈ 180 sq ft"
    assert got["metric"] == "≈ 16.7 m²" and got["tenth"] == "≈ 16.7 m²"
    assert got["bad"] == ""
    assert got["area"] == pytest.approx(32.4e6) == got["rev"]  # the server's test value
    assert got["bow"] is None
    ys = sorted({round(c[1], 9) for c in got["corners"]})
    assert ys == [0.1, 0.9] and got["fits"] is False
