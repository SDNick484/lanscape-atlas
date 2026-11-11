"""Playwright pass over /find and the floor plan's Bluetooth highlight (synthetic data)."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, NamedTuple

import httpx
import pytest

from conftest import FixedClock
from helpers.floorplan_fixtures import dev_position, drag_to_room_centre
from helpers.plan import SQ_POINTS, png_bytes
from lanscape_atlas.bluetooth.aggregator import BLE_MINUTE_FMT, Candidate, MinuteRow
from lanscape_atlas.bluetooth.match import Identity
from lanscape_atlas.collectors.bluetooth_ha import ClientStatus
from lanscape_atlas.config import Settings
from lanscape_atlas.service import Placement, Service
from lanscape_atlas.web.app import create_app

pytest.importorskip("playwright.sync_api")

ROOM_B_POINTS = [[0.6, 0.6], [0.9, 0.6], [0.9, 0.9], [0.6, 0.9]]
SCANNER_B = "00:00:5e:00:53:a2"


class World(NamedTuple):
    base: str
    svc: Service
    phone: int
    remote: int
    spare: int
    room_a: int
    room_b: int
    app: Any


@pytest.fixture
def world(tmp_path: Any, clock: FixedClock) -> Iterator[World]:
    uvicorn = pytest.importorskip("uvicorn")
    from lanscape_atlas.store.db import migrate, open_db

    conn = open_db(tmp_path / "live.db")
    migrate(conn)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = Settings(data_dir=tmp_path, allowed_hosts=(f"127.0.0.1:{port}",))
    app = create_app(settings, conn=conn, clock=clock, start_background=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base) as c:
        c.get("/topology")
        h = {"X-CSRF-Token": c.cookies.get("atlas_csrf", "")}
        c.post("/floorplan/plan", files={"file": ("p.png", png_bytes(1600, 1200))}, headers=h)
        c.post(
            "/floorplan/rooms", data={"name": "Room A", "polygon": json.dumps(SQ_POINTS)}, headers=h
        )
        c.post(
            "/floorplan/rooms",
            data={"name": "Room B", "polygon": json.dumps(ROOM_B_POINTS)},
            headers=h,
        )

    svc: Service = app.state.service
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM place")}
    room_a, room_b = ids["Room A"], ids["Room B"]

    def device(name: str) -> int:
        return svc.create_placeholder_device("user", name, "other")[0]

    phone, remote, spare, proxy = (device(n) for n in ("Phone", "Remote", "Spare", "Proxy B"))
    svc.claim_ble(
        "user", Identity("ibeacon", "00000000-0000-4000-8000-000000000001/1/1"), device_id=phone
    )
    svc.claim_ble("user", Identity("address", "00:00:5e:00:53:10"), device_id=remote)
    # The phone is placed in Room A but heard by the scanner that sits in Room B.
    svc.place_devices("user", [Placement(phone, room_a, 0.3, 0.3)], based_on={phone: 0})
    svc.place_devices("user", [Placement(proxy, room_b, 0.75, 0.75)], based_on={proxy: 0})
    svc.upsert_scanners({SCANNER_B: "ble-proxy-b"}, clock())
    scanner_id = conn.execute("SELECT id FROM ble_scanner").fetchone()[0]
    svc.link_scanner("user", scanner_id, proxy)
    now = clock()
    minute = (now - timedelta(minutes=1)).strftime(BLE_MINUTE_FMT)
    rows = [
        MinuteRow(d, SCANNER_B, minute, -62, 12, now - timedelta(seconds=70))
        for d in (phone, remote)
    ]
    assert svc.record_ble_minutes(rows) == 2
    yield World(base, svc, phone, remote, spare, room_a, room_b, app)
    server.should_exit = True
    thread.join(timeout=5)
    conn.close()


def test_find_page_lists_both_claimed_devices(page: Any, world: World) -> None:
    page.goto(f"{world.base}/find")
    for dev in (world.phone, world.remote):
        row = page.locator(f"#find-{dev}")
        assert row.count() == 1
        assert "Room B" in row.inner_text()
    names = page.locator("#find-results a.name").all_inner_texts()
    assert sorted(names) == ["Phone", "Remote"]
    # No Home Assistant in this app: the page says so instead of listing candidates.
    assert "needs Home Assistant" in page.locator("body").inner_text()


def test_floorplan_heard_room_and_heard_only_tray(page: Any, world: World) -> None:
    page.goto(f"{world.base}/floorplan")
    marker = page.locator(f"[data-device-id='{world.phone}']")
    assert marker.count() == 1
    marker.click()
    room_b = page.locator(f"polygon[data-room-id='{world.room_b}']")
    room_a = page.locator(f"polygon[data-room-id='{world.room_a}']")
    page.wait_for_function(
        "id => document.querySelector(`polygon[data-room-id='${id}']`)"
        ".classList.contains('room--heard')",
        arg=world.room_b,
    )
    assert "room--heard" in (room_b.get_attribute("class") or "")
    assert "room--heard" not in (room_a.get_attribute("class") or "")
    # Room B's tray lists the phone as a heard-only entry; it is not draggable.
    entry = page.locator(f"[data-heard-device='{world.phone}']")
    assert entry.count() == 1
    assert "heard" in entry.inner_text()
    assert (
        entry.locator("xpath=ancestor::div[contains(@class,'fp-group')]/h3").text_content()
        == "Room B"
    )
    assert page.locator(f"[data-tray-device='{world.phone}']").count() == 0
    before = dev_position(page, world.phone)
    box = entry.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + 5, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] - 300, box["y"] + 40, steps=8)
    page.mouse.up()
    assert dev_position(page, world.phone) == before
    assert page.locator("#save-changes").is_disabled()


def test_real_tray_entry_still_drags(page: Any, world: World) -> None:
    page.goto(f"{world.base}/floorplan")
    tray = page.locator(f"[data-tray-device='{world.spare}']")
    tray.scroll_into_view_if_needed()
    assert page.locator(f"[data-device-id='{world.spare}']").count() == 0
    drag_to_room_centre(page, tray)
    assert page.locator(f"[data-device-id='{world.spare}']").count() == 1


class _Candidates:
    def __init__(self, items: list[Candidate]) -> None:
        self.items = items

    def candidates(self) -> list[Candidate]:
        return list(self.items)


class _StubRuntime:
    def __init__(self, items: list[Candidate]) -> None:
        self.aggregator = _Candidates(items)

    def status(self) -> ClientStatus:
        return ClientStatus(True, None, None)


def test_shared_claim_form_claims_the_selected_candidate(
    page: Any, world: World, clock: FixedClock
) -> None:
    now = clock()
    tags = [Identity("address", f"00:00:5e:00:53:{i}") for i in ("31", "32")]
    world.app.state.bluetooth = _StubRuntime(
        [Candidate(t, None, -50, SCANNER_B, now, now, 3, None) for t in tags]
    )
    page.goto(f"{world.base}/find")
    scanner_cells = page.locator("td.scanner").all_inner_texts()
    assert [" ".join(c.split()) for c in scanner_cells] == ["ble-proxy-b Room B"] * 2
    assert page.locator("select[name=device_id]").count() == 1
    page.locator(f"input[type=radio][value='{tags[1].key()}']").check()
    page.locator("select[name=device_id]").select_option(str(world.spare))
    page.get_by_role("button", name="Claim").click()
    page.wait_for_url("**/find?claimed=*")
    assert "Claimed as Spare" in page.locator(".flash.ok").inner_text()
    row = world.svc.conn.execute(
        "SELECT kind, value, device_id FROM ble_identity WHERE device_id = ?", (world.spare,)
    ).fetchone()
    assert (row["kind"], row["value"]) == ("address", tags[1].value)
