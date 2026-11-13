"""Playwright pass over blank plans, scale and units (spec §5.3). Synthetic data only.

Plan -> client positions come from the SVG overlay's screen transform (what is drawn),
not from the page's own conversion function.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from helpers.floorplan_fixtures import drag_to_room_centre
from helpers.plan import SQ_POINTS

pytest.importorskip("playwright.sync_api")


def _query(db_path: Path, sql: str, args: tuple[Any, ...] = ()) -> Any:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _plan(db_path: Path) -> dict[str, Any] | None:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM floor_plan").fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def _post(base: str, path: str, **kw: Any) -> httpx.Response:
    with httpx.Client(base_url=base) as c:
        c.get("/topology")
        return c.post(path, headers={"X-CSRF-Token": c.cookies.get("atlas_csrf", "")}, **kw)


def _blank_via_form(page: Any, base: str, width: str, depth: str, units: str) -> None:
    page.goto(f"{base}/floorplan")
    page.fill("#blank-width", width)
    page.fill("#blank-depth", depth)
    page.select_option("#blank-units", units)
    page.click("#blank-form button[type=submit]")
    page.wait_for_selector(".flash")


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


def _ft_in(mm: float) -> str:
    """The page's imperial format: nearest inch."""
    total = round(mm / 25.4)
    return f"{total // 12}' {total % 12}\""


def _grid(page: Any) -> dict[str, Any]:
    got: dict[str, Any] = page.evaluate(
        """() => {
        const g = document.getElementById('plan-grid');
        const minor = g.querySelector('.fp-grid-minor');
        return {minor: minor.querySelectorAll('line').length,
                major: g.querySelectorAll('.fp-grid-major line').length,
                minorShown: getComputedStyle(minor).display !== 'none',
                mode: document.getElementById('floorplan').dataset.grid};
    }"""
    )
    return got


# ------------------------------------------------------------------- blank plan


def test_create_blank_plan_shows_grid_and_opens_layout(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    _blank_via_form(page, live_server, "40", "30", "imperial")
    plan = _plan(tmp_path / "live.db")
    assert plan is not None
    assert (plan["width"], plan["height"], plan["sha256"], plan["units"]) == (
        12_192,
        9_144,
        None,
        "imperial",
    )
    assert page.locator("#plan-img").count() == 0
    # 1 ft squares: 41 x 31 lines, of which every 10th (0, 10, 20, ... ft) is heavy.
    grid = _grid(page)
    assert (grid["minor"], grid["major"]) == (36 + 27, 5 + 4)
    assert grid["minorShown"] and grid["mode"] == "fine"
    # Creating the plan opens Layout mode, ready to draw a room.
    assert page.locator("#mode-layout").get_attribute("aria-pressed") == "true"
    assert page.locator("#room-draw").is_visible()
    assert page.locator("#fp-size").inner_text() == "40' 0\" × 30' 0\""
    assert page.locator("#mode-scale").count() == 0
    # The grid and the overlay are one box.
    grid_box = page.locator("#plan-grid").bounding_box()
    svg_box = page.locator("#plan-svg").bounding_box()
    assert grid_box is not None and svg_box is not None
    assert [grid_box[k] for k in ("x", "y", "width", "height")] == pytest.approx(
        [svg_box[k] for k in ("x", "y", "width", "height")], abs=0.5
    )


def test_grid_hides_fine_lines_when_squares_are_small(
    page: Any, live_server: str, tmp_path: Path
) -> None:
    # 300 m of 0.5 m squares across the stage: about 1.5 px each at fit.
    _blank_via_form(page, live_server, "300", "100", "metric")
    grid = _grid(page)
    assert (grid["minor"], grid["major"]) == (601 - 61 + 201 - 21, 61 + 21)
    assert not grid["minorShown"] and grid["mode"] == "coarse"
    page.evaluate("window.LanscapeFP.view.zoomTo(8)")
    grid = _grid(page)
    assert grid["minorShown"] and grid["mode"] == "fine"
    page.click("#zoom-fit")
    assert _grid(page)["mode"] == "coarse"


def test_zero_width_blank_plan_refused(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank_via_form(page, live_server, "0", "30", "imperial")
    flash = page.locator(".flash.error")
    assert "width must be between 1 m and 300 m" in flash.inner_text()
    assert _plan(tmp_path / "live.db") is None
    assert page.locator("#blank-form").is_visible()


def test_toggle_units(page: Any, live_server: str, tmp_path: Path) -> None:
    _blank_via_form(page, live_server, "40", "30", "imperial")
    assert page.locator("#units-imperial").get_attribute("aria-pressed") == "true"
    page.click("#units-metric")
    page.wait_for_selector(".flash")
    assert page.locator(".flash").inner_text() == "Units changed."
    assert _plan(tmp_path / "live.db")["units"] == "metric"  # type: ignore[index]
    assert page.locator("#units-metric").get_attribute("aria-pressed") == "true"
    assert page.locator("#fp-size").inner_text() == "12.19 m × 9.14 m"
    # 0.5 m squares now: 25 x 19 lines, every 10th heavy (0, 5, 10 m).
    assert (_grid(page)["minor"], _grid(page)["major"]) == (25 - 3 + 19 - 2, 3 + 2)
    page.click("#units-imperial")
    page.wait_for_selector(".flash")
    assert _plan(tmp_path / "live.db")["units"] == "imperial"  # type: ignore[index]


def test_place_device_on_blank_plan_and_undo(page: Any, live_server: str, tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    assert (
        _post(
            live_server,
            "/floorplan/blank",
            data={"width": "12", "depth": "9", "units": "metric"},
            follow_redirects=False,
        ).status_code
        == 303
    )
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
    tray = page.locator(f"[data-tray-device='{dev}']")
    tray.scroll_into_view_if_needed()
    drag_to_room_centre(page, tray)
    page.click("#save-changes")
    page.wait_for_selector(".flash")
    assert page.locator(".flash").inner_text() == "Changes saved."
    x, y = _query(db, "SELECT pos_x, pos_y FROM device WHERE id = ?", (dev,))
    assert x == pytest.approx(0.3, abs=0.01) and y == pytest.approx(0.3, abs=0.01)
    batch = int(_query(db, "SELECT max(batch_id) FROM change WHERE entity = 'device'")[0])
    page.goto(f"{live_server}/history")
    page.click(f"#batch-{batch} button[type=submit]")
    page.wait_for_selector(f"#batch-{batch} .tag.unwanted")
    assert _query(db, "SELECT place_id, pos_x, pos_y FROM device WHERE id = ?", (dev,))[:] == (
        None,
        None,
        None,
    )
    page.goto(f"{live_server}/floorplan")
    assert page.locator(f"[data-device-id='{dev}']").count() == 0
    assert page.locator(f"[data-tray-device='{dev}']").count() == 1


# ----------------------------------------------------------------------- scale


def _draw_scale_line(page: Any, a: tuple[float, float], b: tuple[float, float]) -> None:
    page.click("#mode-scale")
    assert page.locator("#scale-draw").is_visible()
    assert page.locator("#scale-length").is_disabled()
    page.mouse.click(*_client(page, *a))
    page.mouse.click(*_client(page, *b))
    assert page.locator("#scale-length").is_enabled()


def test_set_scale_on_image_plan(page: Any, synthetic_plan_and_room: str, tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    base = synthetic_plan_and_room
    page.goto(f"{base}/floorplan")
    assert page.locator("#units-form").count() == 0  # no scale yet
    # Half the plan's width (1600 px image) is 5 m, so the plan is 10 m wide.
    _draw_scale_line(page, (0.2, 0.7), (0.7, 0.7))
    page.fill("#scale-length", "5 m")
    page.click("#scale-save")
    page.wait_for_selector(".flash")
    assert page.locator(".flash").inner_text() == "Scale saved."
    width_mm = _plan(db)["width_mm"]  # type: ignore[index]
    assert width_mm == pytest.approx(10_000, rel=5e-3)
    assert page.locator("#units-form").count() == 1
    # The size shown follows the stored scale (about 10 m x 7.5 m), in feet and inches.
    size = f"{_ft_in(width_mm)} × {_ft_in(width_mm * 1200 / 1600)}"
    assert page.locator("#fp-size").inner_text() == size
    # Measuring again shows the line's length at the current scale; a vertical line
    # is aspect-corrected: 0.3 of the ~7.5 m depth is ~2.25 m (88.6 in).
    _draw_scale_line(page, (0.8, 0.2), (0.8, 0.5))
    m = re.search(r"It measures (\d+)' (\d+)\"", page.locator("#fp-status").inner_text())
    assert m is not None
    assert int(m[1]) * 12 + int(m[2]) == pytest.approx(2250 / 25.4, abs=1.5)
    page.fill("#scale-length", "14'6\"")
    page.keyboard.press("Enter")
    page.wait_for_selector(".flash")
    # 14' 6" = 4419.6 mm over 0.3 of the depth (0.3 * 1200/1600 of the width).
    want = 4419.6 / (0.3 * 1200 / 1600)
    assert _plan(db)["width_mm"] == pytest.approx(want, rel=5e-3)  # type: ignore[index]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("0", "Add a unit"),
        ("0 m", "greater than zero"),
        ("-4 m", "greater than zero"),
        ("5'13\"", "less than 12"),
        ("", "Type a length"),
        ("   ", "Type a length"),
        ("about four metres", "Not a length"),
    ],
)
def test_scale_length_parser_rejects(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path, text: str, message: str
) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    _draw_scale_line(page, (0.2, 0.7), (0.7, 0.7))
    page.fill("#scale-length", text)
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if r.method == "POST" else None)
    page.click("#scale-save")
    error = page.locator("#scale-error")
    assert error.is_visible() and message in error.inner_text()
    assert message in page.locator("#fp-status").inner_text()
    assert requests == []
    assert _plan(tmp_path / "live.db")["width_mm"] is None  # type: ignore[index]


def test_length_parser_and_formatter(page: Any, synthetic_plan_and_room: str) -> None:
    page.goto(f"{synthetic_plan_and_room}/floorplan")
    parsed = page.evaluate(
        """texts => texts.map(t => window.LanscapeFP.units.parse(t).mm)""",
        [
            "14'6\"",
            "14' 6\"",
            "14 ft",
            "14ft 6in",
            "174 in",
            "4.4 m",
            "440 cm",
            "4400 mm",
            "14.5 ft",
        ],
    )
    assert parsed == pytest.approx(
        [4419.6, 4419.6, 4267.2, 4419.6, 4419.6, 4400, 4400, 4400, 4419.6]
    )
    formatted = page.evaluate(
        """() => {
        const f = window.LanscapeFP.units.format;
        return [f(4419.6, 'imperial'), f(4420, 'metric'), f(304.8 * 12 - 10, 'imperial'),
                f(12345, 'metric'), f(3, 'metric')];
    }"""
    )
    assert formatted == [
        "14' 6\"",
        "4.42 m",
        "11' 12\"".replace("11' 12", "12' 0"),
        "12.35 m",
        "0.00 m",
    ]
