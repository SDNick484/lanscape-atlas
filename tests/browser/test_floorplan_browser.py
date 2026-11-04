"""Playwright pass over /floorplan. Skipped when Playwright or its browser is unavailable."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from helpers.floorplan_fixtures import dev_position, drag_outside_room, drag_to_room_centre

pytest.importorskip("playwright.sync_api")


def _add_device(db_path: Path, name: str, *, online: int = 1) -> int:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO device(name, first_seen, last_seen, trust, online)"
                " VALUES (?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'new', ?)",
                (name, online),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid
    finally:
        conn.close()


def _room_id(db_path: Path, name: str = "Room A") -> int:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = conn.execute("SELECT id FROM place WHERE name = ?", (name,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _api(base: str, method: str, path: str, **kw: Any) -> httpx.Response:
    with httpx.Client(base_url=base) as c:
        c.get("/topology")
        headers = {"X-CSRF-Token": c.cookies.get("atlas_csrf", "")}
        return c.request(method, path, headers=headers, **kw)


def _place(base: str, device_id: int, room: int, x: float, y: float, based_on: int = 0) -> int:
    body = {
        "placements": [
            {"device_id": device_id, "place_id": room, "x": x, "y": y, "roaming": False}
        ],
        "based_on": {str(device_id): based_on},
    }
    return _api(base, "POST", "/floorplan/placements", json=body).status_code


def test_drag_inside_room_saves(page: Any, synthetic_plan_and_room: str, tmp_path: Path) -> None:
    base = synthetic_plan_and_room
    dev = _add_device(tmp_path / "live.db", "Device One")
    page.goto(f"{base}/floorplan")
    tray = page.locator(f"[data-tray-device='{dev}']")
    tray.scroll_into_view_if_needed()
    assert page.locator(f"[data-device-id='{dev}']").count() == 0
    drag_to_room_centre(page, tray)
    assert page.locator(f"[data-device-id='{dev}']").count() == 1
    page.click("#save-changes")
    page.wait_for_selector(".flash")
    assert page.locator(".flash").inner_text() == "Changes saved."
    assert page.locator(f"[data-device-id='{dev}']").count() == 1


def test_drag_outside_room_reverts(page: Any, synthetic_plan_and_room: str, tmp_path: Path) -> None:
    base = synthetic_plan_and_room
    dev = _add_device(tmp_path / "live.db", "Device One")
    assert _place(base, dev, _room_id(tmp_path / "live.db"), 0.3, 0.3) == 200
    page.goto(f"{base}/floorplan")
    marker = page.locator(f"[data-device-id='{dev}']")
    before = dev_position(page, dev)
    assert before is not None
    drag_outside_room(page, marker)
    assert dev_position(page, dev) == before
    assert page.locator("#save-changes").is_disabled()
    assert "stays inside" in page.locator("#fp-status").inner_text()


def test_offline_marker_and_markup_room_name_render_safely(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    base = synthetic_plan_and_room
    db = tmp_path / "live.db"
    dev = _add_device(db, "Sleeper", online=0)
    assert _place(base, dev, _room_id(db), 0.3, 0.3) == 200
    poly = [[0.6, 0.6], [0.9, 0.6], [0.9, 0.9], [0.6, 0.9]]
    resp = _api(
        base,
        "POST",
        "/floorplan/rooms",
        data={"name": "<b>x</b>", "polygon": json.dumps(poly)},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303)
    page.goto(f"{base}/floorplan")
    assert "offline" in (page.locator(f"[data-device-id='{dev}']").get_attribute("class") or "")
    labels = page.locator("#plan-svg .fp-room-label")
    texts = [labels.nth(i).text_content() for i in range(labels.count())]
    assert "<b>x</b>" in texts
    assert page.locator("#plan-svg b").count() == 0


def test_stale_save_shows_conflict(page: Any, synthetic_plan_and_room: str, tmp_path: Path) -> None:
    base = synthetic_plan_and_room
    db = tmp_path / "live.db"
    dev = _add_device(db, "Device One")
    room = _room_id(db)
    page.goto(f"{base}/floorplan")
    tray = page.locator(f"[data-tray-device='{dev}']")
    tray.scroll_into_view_if_needed()
    drag_to_room_centre(page, tray)
    # Another tab changes the same device after this page loaded.
    assert _place(base, dev, room, 0.2, 0.2) == 200
    page.click("#save-changes")
    page.wait_for_selector(".flash.error")
    assert page.url.endswith("stale=1")
    assert "reload" in page.locator(".flash").inner_text().lower()


def test_roaming_tray_stamp_and_checkbox_gating(
    page: Any, synthetic_plan_and_room: str, tmp_path: Path
) -> None:
    base = synthetic_plan_and_room
    db = tmp_path / "live.db"
    dev = _add_device(db, "Phone")
    body = {
        "placements": [
            {"device_id": dev, "place_id": _room_id(db), "x": None, "y": None, "roaming": True}
        ],
        "based_on": {str(dev): 0},
    }
    assert _api(base, "POST", "/floorplan/placements", json=body).status_code == 200
    page.goto(f"{base}/floorplan")
    item = page.locator(f"[data-tray-device='{dev}']")
    # Drawn by the script (the server markup is replaced on load): who set it, and when.
    stamp = item.locator(".set-by").text_content()
    assert stamp is not None and stamp.startswith("last set by user at 20")
    box = item.locator("input[data-roaming-for]")
    assert box.is_enabled()
    page.click("#mode-layout")
    assert page.locator(f"[data-tray-device='{dev}'] input[data-roaming-for]").is_disabled()
