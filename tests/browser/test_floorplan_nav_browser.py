"""Playwright pass over the floor plan's zoom and pan (spec §4.1). Synthetic data only.

Expected plan coordinates are worked out from what is on screen (the SVG layer's
screen transform, checked against the plan image's box) under whatever zoom and pan
the page has applied, not from the page's own conversion function. Room A is the
square (0.1, 0.1)-(0.5, 0.5).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("playwright.sync_api")

TOL = 0.005  # stored coordinates are rounded to 4 places; allow for pixel snapping


def _add_device(db_path: Path, name: str) -> int:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO device(name, first_seen, last_seen, trust, online)"
                " VALUES (?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'new', 1)",
                (name,),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid
    finally:
        conn.close()


def _query(db_path: Path, sql: str, args: tuple[Any, ...] = ()) -> Any:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _room_a(db_path: Path) -> int:
    return int(_query(db_path, "SELECT id FROM place WHERE name = 'Room A'")[0])


def _place(base: str, device_id: int, room: int, x: float, y: float) -> None:
    body = {
        "placements": [
            {"device_id": device_id, "place_id": room, "x": x, "y": y, "roaming": False}
        ],
        "based_on": {str(device_id): 0},
    }
    with httpx.Client(base_url=base) as c:
        c.get("/topology")
        h = {"X-CSRF-Token": c.cookies.get("atlas_csrf", "")}
        assert c.post("/floorplan/placements", json=body, headers=h).status_code == 200


def _zoom(page: Any) -> float:
    return float(page.locator("#floorplan").get_attribute("data-zoom") or "nan")


def _set_zoom(page: Any, z: float) -> None:
    """Zoom about the stage centre through the page's own view API."""
    page.evaluate("z => window.LanscapeFP.view.zoomTo(z)", z)
    assert _zoom(page) == pytest.approx(z)


class PlanMap:
    """Plan <-> client mapping as the SVG layer draws it (its screen CTM), checked
    against the plan image's on-screen box so the two layers are known to line up."""

    def __init__(self, page: Any) -> None:
        got = page.evaluate(
            """() => {
            const svg = document.getElementById('plan-svg');
            const vb = svg.viewBox.baseVal;
            const m = svg.getScreenCTM();
            const pt = (x, y) => new DOMPoint(x, y).matrixTransform(m);
            const a = pt(0, 0), b = pt(vb.width, vb.height);
            const img = document.getElementById('plan-img').getBoundingClientRect();
            return {x0: a.x, y0: a.y, x1: b.x, y1: b.y,
                    img: [img.left, img.top, img.right, img.bottom]};
        }"""
        )
        self.x0, self.y0 = got["x0"], got["y0"]
        self.sx = got["x1"] - got["x0"]
        self.sy = got["y1"] - got["y0"]
        img = got["img"]
        assert [self.x0, self.y0, got["x1"], got["y1"]] == pytest.approx(img, abs=0.5)

    def client(self, x: float, y: float) -> tuple[float, float]:
        return self.x0 + x * self.sx, self.y0 + y * self.sy

    def plan(self, cx: float, cy: float) -> tuple[float, float]:
        return (cx - self.x0) / self.sx, (cy - self.y0) / self.sy


def _stage_box(page: Any) -> dict[str, float]:
    box = page.locator("#floorplan").bounding_box()
    assert box is not None
    return dict(box)


def _visible(page: Any, cx: float, cy: float) -> bool:
    b = _stage_box(page)
    return b["x"] < cx < b["x"] + b["width"] and b["y"] < cy < b["y"] + b["height"]


def _drag_tray_to(page: Any, device_id: int, cx: float, cy: float) -> None:
    tray = page.locator(f"[data-tray-device='{device_id}']")
    tray.scroll_into_view_if_needed()
    box = tray.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(cx, cy, steps=10)
    page.mouse.up()


def _pan(page: Any, fx: float, fy: float, dx: float, dy: float) -> None:
    page.mouse.move(fx, fy)
    page.mouse.down()
    page.mouse.move(fx + dx, fy + dy, steps=8)
    page.mouse.up()


def _save_and_wait(page: Any) -> None:
    page.click("#save-changes")
    page.wait_for_selector(".flash")
    assert page.locator(".flash").inner_text() == "Changes saved."


def _stored_point(db: Path, device_id: int) -> tuple[float, float]:
    row = _query(db, "SELECT pos_x, pos_y FROM device WHERE id = ?", (device_id,))
    return float(row[0]), float(row[1])


# ------------------------------------------------------------------ zoom controls


def test_zoom_buttons_change_scale(page: Any, synthetic_plan_and_room: str) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    assert _zoom(page) == 1
    width_1x = PlanMap(page).sx
    page.click("#zoom-in")
    assert _zoom(page) == pytest.approx(1.25)
    assert PlanMap(page).sx == pytest.approx(width_1x * 1.25, rel=1e-3)
    page.click("#zoom-in")
    page.click("#zoom-out")
    assert _zoom(page) == pytest.approx(1.25)
    page.click("#zoom-fit")
    assert _zoom(page) == 1
    assert PlanMap(page).sx == pytest.approx(width_1x, rel=1e-3)
    # Clamped to 1x (fit) .. 8x.
    page.click("#zoom-out")
    assert _zoom(page) == 1
    for _ in range(15):
        page.click("#zoom-in")
    assert _zoom(page) == 8


def test_button_zoom_keeps_view_centre(page: Any, synthetic_plan_and_room: str) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    b = _stage_box(page)
    centre = (b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    before = PlanMap(page).plan(*centre)
    page.click("#zoom-in")
    page.click("#zoom-in")
    after = PlanMap(page).plan(*centre)
    assert after == pytest.approx(before, abs=1e-3)


def test_keyboard_zoom_when_stage_focused(page: Any, synthetic_plan_and_room: str) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    page.focus("#floorplan")
    page.keyboard.press("+")
    assert _zoom(page) == pytest.approx(1.25)
    page.keyboard.press("=")
    assert _zoom(page) == pytest.approx(1.5625)
    page.keyboard.press("-")
    assert _zoom(page) == pytest.approx(1.25)
    page.keyboard.press("0")
    assert _zoom(page) == 1
    # Typing in the tray filter never zooms.
    page.focus("#tray-filter")
    page.keyboard.press("+")
    assert _zoom(page) == 1


def test_wheel_over_stage_zooms_about_pointer(page: Any, synthetic_plan_and_room: str) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    cx, cy = PlanMap(page).client(0.3, 0.3)
    page.mouse.move(cx, cy)
    page.mouse.wheel(0, -200)
    page.wait_for_function("Number(document.getElementById('floorplan').dataset.zoom) > 1")
    assert PlanMap(page).plan(cx, cy) == pytest.approx((0.3, 0.3), abs=2e-3)
    # Page itself did not scroll.
    assert page.evaluate("window.scrollY") == 0


def test_wheel_over_tray_scrolls_tray_not_plan(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    for i in range(60):
        _add_device(db, f"Device {i:02d}")
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    tray = page.locator("#tray")
    assert tray.evaluate("el => el.scrollHeight > el.clientHeight")
    box = tray.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 300)
    page.wait_for_function("document.getElementById('tray').scrollTop > 0")
    assert _zoom(page) == 1


def test_touch_pinch_zooms(page: Any, synthetic_plan_and_room: str) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    zoom = page.evaluate(
        """() => {
        const svg = document.getElementById('plan-svg');
        const r = svg.getBoundingClientRect();
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        const ev = (type, id, x) => new PointerEvent(type, {
            pointerId: id, pointerType: 'touch', isPrimary: id === 1, clientX: x,
            clientY: cy, button: 0, buttons: type === 'pointerup' ? 0 : 1,
            bubbles: true, cancelable: true});
        svg.dispatchEvent(ev('pointerdown', 1, cx - 20));
        svg.dispatchEvent(ev('pointerdown', 2, cx + 20));
        document.dispatchEvent(ev('pointermove', 1, cx - 40));
        document.dispatchEvent(ev('pointermove', 2, cx + 40));
        document.dispatchEvent(ev('pointerup', 1, cx - 40));
        document.dispatchEvent(ev('pointerup', 2, cx + 40));
        return Number(document.getElementById('floorplan').dataset.zoom);
    }"""
    )
    assert zoom == pytest.approx(2.0, rel=1e-3)


# ---------------------------------------------------------------------------- pan


def test_drag_on_empty_space_pans_and_adds_no_corner(
    page: Any, synthetic_plan_and_room: str
) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    page.click("#mode-layout")
    _set_zoom(page, 2)
    b = _stage_box(page)
    fx, fy = b["x"] + b["width"] * 0.6, b["y"] + b["height"] * 0.6
    before = PlanMap(page)
    _pan(page, fx, fy, 80, 60)
    after = PlanMap(page)
    assert after.x0 - before.x0 == pytest.approx(80, abs=1)
    assert after.y0 - before.y0 == pytest.approx(60, abs=1)
    assert page.locator(".fp-draft circle").count() == 0
    assert page.locator("#room-close").is_disabled()
    # A press that barely moves is still a click: it adds a corner.
    _pan(page, fx, fy, 2, 1)
    assert page.locator(".fp-draft circle").count() == 1


def test_pan_is_clamped_to_the_plan(page: Any, synthetic_plan_and_room: str) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    b = _stage_box(page)
    # At fit there is nothing to pan.
    before = PlanMap(page)
    _pan(page, b["x"] + b["width"] * 0.7, b["y"] + b["height"] * 0.7, 120, 90)
    assert PlanMap(page).x0 == pytest.approx(before.x0, abs=0.5)
    _set_zoom(page, 2)
    _pan(page, b["x"] + b["width"] * 0.7, b["y"] + b["height"] * 0.7, 2000, 2000)
    m = PlanMap(page)
    # The plan's top-left corner stops at the stage's top-left corner.
    left, top = m.client(0, 0)
    assert left == pytest.approx(b["x"] + 1, abs=1.5)
    assert top == pytest.approx(b["y"] + 1, abs=1.5)


def test_space_drag_pans_over_a_marker(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    dev = _add_device(db, "Device One")
    _place(synthetic_plan_and_room, dev, _room_a(db), 0.3, 0.3)
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    _set_zoom(page, 2)
    marker = page.locator(f"[data-device-id='{dev}']")
    box = marker.bounding_box()
    assert box is not None
    before = PlanMap(page)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.keyboard.down(" ")
    _pan(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, 50, 40)
    page.keyboard.up(" ")
    after = PlanMap(page)
    assert after.x0 - before.x0 == pytest.approx(50, abs=1)
    assert page.locator("#save-changes").is_disabled()
    assert page.evaluate("window.scrollY") == 0


# ------------------------------------------------- placement and drawing under zoom


def test_drop_at_3x_after_pan_saves_the_intended_point(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    dev = _add_device(db, "Device One")
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    for _ in range(5):  # 1.25 ** 5 ~= 3.05
        page.click("#zoom-in")
    assert _zoom(page) >= 3
    b = _stage_box(page)
    _pan(page, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, 200, 150)
    m = PlanMap(page)
    target = (0.42, 0.37)
    cx, cy = m.client(*target)
    assert _visible(page, cx, cy)
    _drag_tray_to(page, dev, cx, cy)
    _save_and_wait(page)
    assert _stored_point(db, dev) == pytest.approx(target, abs=TOL)


def test_resize_while_zoomed_keeps_drops_accurate(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    dev = _add_device(db, "Device One")
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    _set_zoom(page, 3)
    b = _stage_box(page)
    _pan(page, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, 200, 150)
    page.set_viewport_size({"width": 1100, "height": 800})
    page.wait_for_timeout(100)
    m = PlanMap(page)
    target = (0.4, 0.35)
    cx, cy = m.client(*target)
    assert _visible(page, cx, cy)
    _drag_tray_to(page, dev, cx, cy)
    _save_and_wait(page)
    assert _stored_point(db, dev) == pytest.approx(target, abs=TOL)


def test_drop_outside_the_visible_stage_is_ignored(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    dev = _add_device(db, "Device One")
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    _set_zoom(page, 3)
    b = _stage_box(page)
    # Just below the stage: under the zoomed plan's (clipped) area, but not visible.
    _drag_tray_to(page, dev, b["x"] + b["width"] / 2, b["y"] + b["height"] + 10)
    assert page.locator(f"[data-device-id='{dev}']").count() == 0
    assert page.locator("#save-changes").is_disabled()


def test_marker_drag_at_zoom_saves_the_intended_point(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    dev = _add_device(db, "Device One")
    _place(synthetic_plan_and_room, dev, _room_a(db), 0.4, 0.4)
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    _set_zoom(page, 2.5)
    m = PlanMap(page)
    sx, sy = m.client(0.4, 0.4)
    tx, ty = m.client(0.46, 0.43)
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(tx, ty, steps=8)
    page.mouse.up()
    _save_and_wait(page)
    assert _stored_point(db, dev) == pytest.approx((0.46, 0.43), abs=TOL)


def test_nudge_at_4x_moves_the_same_plan_distance(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    dev = _add_device(db, "Device One")
    _place(synthetic_plan_and_room, dev, _room_a(db), 0.3, 0.3)
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    _set_zoom(page, 4)
    page.focus(f"[data-device-id='{dev}']")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowDown")
    _save_and_wait(page)
    assert _stored_point(db, dev) == pytest.approx((0.305, 0.305), abs=1e-9)


def test_layout_corners_at_2x_are_stored(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    page.click("#mode-layout")
    _set_zoom(page, 2)
    m = PlanMap(page)
    corners = [(0.32, 0.31), (0.46, 0.31), (0.46, 0.44), (0.32, 0.44)]
    for x, y in corners:
        cx, cy = m.client(x, y)
        assert _visible(page, cx, cy)
        page.mouse.click(cx, cy)
    page.click("#room-close")
    page.fill("#room-name", "Room B")
    page.click("#room-save")
    page.wait_for_selector(".flash")
    row = _query(db, "SELECT polygon FROM place WHERE name = 'Room B'")
    stored = json.loads(row[0])
    assert len(stored) == 4
    for got, want in zip(stored, corners, strict=True):
        assert got == pytest.approx(list(want), abs=TOL)


# ------------------------------------------------------- constant on-screen sizes


def test_markers_and_labels_keep_their_screen_size(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    dev = _add_device(db, "Device One")
    _place(synthetic_plan_and_room, dev, _room_a(db), 0.3, 0.3)
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    marker = page.locator(f"[data-device-id='{dev}']")
    label = page.locator(".fp-room-label").first

    def sizes() -> tuple[float, float, float]:
        mb = marker.bounding_box()
        assert mb is not None
        lh = label.evaluate("el => el.getBoundingClientRect().height")
        # On-screen stroke width: the computed width times the view's CSS scale.
        stroke = page.evaluate(
            "() => parseFloat(getComputedStyle(document.querySelector('polygon.fp-room'))"
            ".strokeWidth) * Number(document.getElementById('floorplan').dataset.zoom)"
        )
        return mb["width"], float(lh), float(stroke)

    w1, h1, s1 = sizes()
    _set_zoom(page, 8)
    w8, h8, s8 = sizes()
    assert w8 == pytest.approx(w1, abs=1)
    assert h8 == pytest.approx(h1, abs=1.5)
    assert s8 == pytest.approx(s1, abs=0.01)
    # The selected marker (thicker border) also stays put after a redraw.
    page.focus(f"[data-device-id='{dev}']")
    page.keyboard.press("ArrowRight")
    w8b = marker.bounding_box()
    # Its box includes the stroke, 4 px instead of 2 px when selected.
    assert w8b is not None and w8b["width"] == pytest.approx(w1 + 2, abs=1)


# ------------------------------------------------------------------- search (§4.2)

ROOM_B = [[0.6, 0.6], [0.9, 0.6], [0.9, 0.9], [0.6, 0.9]]


def _add_room_b(base: str) -> None:
    with httpx.Client(base_url=base) as c:
        c.get("/topology")
        h = {"X-CSRF-Token": c.cookies.get("atlas_csrf", "")}
        c.post(
            "/floorplan/rooms", data={"name": "Room B", "polygon": json.dumps(ROOM_B)}, headers=h
        )


def _pick(page: Any, text: str, option: str) -> None:
    page.fill("#fp-search", text)
    opt = page.locator("#fp-search-list [role=option]", has_text=option).first
    opt.wait_for()
    opt.click()


def test_search_for_a_placed_device_centres_it(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    dev = _add_device(db, "Frobnicator")
    _place(synthetic_plan_and_room, dev, _room_a(db), 0.3, 0.2)
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    page.fill("#fp-search", "frobn")
    page.wait_for_selector("#fp-search-list [role=option]")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    assert _zoom(page) >= 3
    marker = page.locator(f"[data-device-id='{dev}']")
    box = marker.bounding_box()
    stage = _stage_box(page)
    assert box is not None
    assert box["x"] + box["width"] / 2 == pytest.approx(stage["x"] + stage["width"] / 2, abs=2)
    assert box["y"] + box["height"] / 2 == pytest.approx(stage["y"] + stage["height"] / 2, abs=2)
    cls = marker.get_attribute("class") or ""
    assert "selected" in cls and "fp-pulse" in cls
    assert page.locator("#fp-sel-name").text_content() == "Frobnicator"
    assert page.locator("#fp-search-list").is_hidden()
    page.wait_for_function(
        f"!document.querySelector(\"[data-device-id='{dev}']\").classList.contains('fp-pulse')"
    )


def test_search_for_a_room_highlights_it(page: Any, synthetic_plan_and_room: str) -> None:
    _add_room_b(synthetic_plan_and_room)
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    _pick(page, "room b", "Room B")
    room_b = page.locator("polygon.fp-room").nth(1)  # rooms are drawn in id order
    assert "fp-room--found" in (room_b.get_attribute("class") or "")
    assert "fp-room--found" not in (
        page.locator("polygon.fp-room").first.get_attribute("class") or ""
    )
    assert _zoom(page) > 1
    # The whole room is in view: its corners are inside the stage.
    m = PlanMap(page)
    for x, y in ((0.6, 0.6), (0.9, 0.9)):
        assert _visible(page, *m.client(x, y))
    # Typing a new query and clearing it drops the highlight.
    page.fill("#fp-search", "")
    page.wait_for_function("!document.querySelector('.fp-room--found')")


def test_search_for_a_roaming_device_fits_its_room_and_tray_entry(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    for i in range(40):
        _add_device(db, f"Filler {i:02d}")
    phone = _add_device(db, "Zed Phone")
    body = {
        "placements": [
            {"device_id": phone, "place_id": _room_a(db), "x": None, "y": None, "roaming": True}
        ],
        "based_on": {str(phone): 0},
    }
    with httpx.Client(base_url=synthetic_plan_and_room) as c:
        c.get("/topology")
        h = {"X-CSRF-Token": c.cookies.get("atlas_csrf", "")}
        assert c.post("/floorplan/placements", json=body, headers=h).status_code == 200
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    page.fill("#tray-filter", "filler")  # hides the entry; picking it must bring it back
    _pick(page, "zed", "Zed Phone")
    entry = page.locator(f"[data-tray-device='{phone}']")
    assert "fp-found" in (entry.get_attribute("class") or "")
    assert entry.is_visible() and entry.evaluate(
        "el => { const t = document.getElementById('tray').getBoundingClientRect();"
        " const r = el.getBoundingClientRect(); return r.top >= t.top && r.bottom <= t.bottom; }"
    )
    room_a = page.locator("polygon.fp-room").first
    assert "fp-room--found" in (room_a.get_attribute("class") or "")
    assert _zoom(page) > 1


def test_search_for_an_unassigned_device_highlights_its_tray_entry(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    db = tmp_path / "live.db"
    for i in range(40):
        _add_device(db, f"Filler {i:02d}")
    lone = _add_device(db, "Zed Sensor")
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    _pick(page, "zed sen", "Zed Sensor")
    entry = page.locator(f"[data-tray-device='{lone}']")
    assert "fp-found" in (entry.get_attribute("class") or "")
    assert entry.evaluate(
        "el => { const t = document.getElementById('tray').getBoundingClientRect();"
        " const r = el.getBoundingClientRect(); return r.top >= t.top && r.bottom <= t.bottom; }"
    )
    assert _zoom(page) == 1
    assert page.locator(".fp-room--found").count() == 0


def test_search_escape_closes_and_markup_names_are_text(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    _add_device(tmp_path / "live.db", "<i>odd</i>")
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    page.fill("#fp-search", "odd")
    opt = page.locator("#fp-search-list [role=option]").first
    opt.wait_for()
    assert "<i>odd</i>" in (opt.text_content() or "")
    assert page.locator("#fp-search-list i").count() == 0
    page.keyboard.press("Escape")
    assert page.locator("#fp-search-list").is_hidden()
    assert page.locator("#fp-search").get_attribute("aria-expanded") == "false"
