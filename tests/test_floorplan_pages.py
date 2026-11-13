"""The /floorplan page (Task 9): layout, placement, uploads and CSRF-protected writes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from helpers.plan import SQ_POINTS, jpeg_bytes, png_bytes, save_square_room, svc_last, svg_bytes
from lanscape_atlas.floorplan.image import MAX_BYTES
from lanscape_atlas.service import Placement, Service

XSS = "<script>alert(1)</script>"
DATA_RE = re.compile(r'<script type="application/json" id="fp-data">(.*?)</script>', re.S)


def _is_html(r: Any) -> bool:
    return str(r.headers["content-type"]).startswith("text/html")


def _upload(client: TestClient, name: str, data: bytes, mime: str = "image/png") -> Any:
    return client.post(
        "/floorplan/plan", files={"file": (name, data, mime)}, follow_redirects=False
    )


def _files(data_dir: Path) -> list[str]:
    return sorted(p.name for p in (data_dir / "floorplan").glob("*"))


def _data_block(html: str) -> dict[str, Any]:
    m = DATA_RE.search(html)
    assert m, "no floor plan data block"
    data: dict[str, Any] = json.loads(m.group(1))
    return data


def _place(svc: Service, dev: int, room: int, x: float = 0.3, y: float = 0.3) -> None:
    svc.place_devices("user", [Placement(dev, room, x, y)], based_on={dev: svc_last(svc.conn, dev)})


def _device_row(db: sqlite3.Connection, dev: int) -> sqlite3.Row:
    row: sqlite3.Row = db.execute(
        "SELECT place_id, pos_x, pos_y, roaming FROM device WHERE id = ?", (dev,)
    ).fetchone()
    return row


# ------------------------------------------------------------- brief tests


def test_room_name_is_escaped(
    client_csrf: TestClient, saved_room_named: Callable[[str], int]
) -> None:
    saved_room_named("<script>alert(1)</script>")
    body = client_csrf.get("/floorplan").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_post_without_csrf_is_403(client: TestClient) -> None:
    assert (
        client.post("/floorplan/rooms", data={"name": "Room A", "polygon": "[]"}).status_code == 403
    )


def test_placement_conflict_renders_message(client_csrf: TestClient, stale_setup: Any) -> None:
    r = client_csrf.post("/floorplan/placements", json=stale_setup.body)
    assert r.status_code == 409
    assert "reload" in r.json()["message"].lower()


def test_plan_upload_bad_type_is_422_and_no_file(
    client_csrf: TestClient, tmp_data_dir: Path
) -> None:
    r = client_csrf.post("/floorplan/plan", files={"file": ("x.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 422
    assert list((tmp_data_dir / "floorplan").glob("*")) == []


# ------------------------------------------------------------ page + nav


def test_page_without_plan_offers_upload(client: TestClient) -> None:
    r = client.get("/floorplan")
    assert r.status_code == 200 and _is_html(r)
    assert 'href="/floorplan" aria-current="page"' in r.text
    assert 'enctype="multipart/form-data"' in r.text
    assert 'id="plan-svg"' not in r.text
    assert _data_block(r.text)["plan"] is None


def test_nav_links_floor_plan_everywhere(client: TestClient) -> None:
    assert ">Floor plan</a>" in client.get("/").text


def test_page_draws_plan_rooms_and_stable_selectors(
    client_csrf: TestClient, svc: Service, make_device: Callable[..., int]
) -> None:
    assert _upload(client_csrf, "p.png", png_bytes(1600, 1200)).status_code == 303
    room = save_square_room(svc)
    dev = make_device(name="Lamp", online=1)
    _place(svc, dev, room)
    html = client_csrf.get("/floorplan").text
    assert 'id="plan-svg"' in html and 'id="save-changes"' in html and 'id="floorplan"' in html
    assert 'viewBox="0 0 1600 1200"' in html
    assert f'data-room-id="{room}"' in html
    assert f'data-device-id="{dev}"' in html
    assert 'src="/floorplan/plan.png' in html
    data = _data_block(html)
    assert data["plan"] == {"width": 1600, "height": 1200, "url": data["plan"]["url"]}
    assert data["rooms"] == [{"id": room, "name": "Room A", "polygon": SQ_POINTS}]
    assert data["placed"][0]["device_id"] == dev
    assert "file_name" not in json.dumps(data)


def test_page_loads_view_script_first_and_zoom_controls(client_csrf: TestClient) -> None:
    # No plan: no stage, so no zoom controls (the scripts still load and do nothing).
    html = client_csrf.get("/floorplan").text
    assert 'id="zoom-in"' not in html and 'id="fp-view"' not in html
    assert 'id="fp-search"' not in html
    assert _upload(client_csrf, "p.png", png_bytes(1600, 1200)).status_code == 303
    html = client_csrf.get("/floorplan").text
    view_js = html.index('src="/static/floorplan-view.js" defer')
    page_js = html.index('src="/static/floorplan.js" defer')
    search_js = html.index('src="/static/floorplan-search.js" defer')
    assert view_js < page_js < search_js
    for id_ in ("zoom-in", "zoom-out", "zoom-fit", "fp-view", "fp-search", "fp-search-list"):
        assert f'id="{id_}"' in html
    # The image and the SVG overlay share the one transformed wrapper.
    stage = html[html.index('id="fp-view"') :]
    assert stage.index('id="plan-img"') < stage.index('id="plan-svg"') < stage.index("</svg>")
    for path in (
        "/static/floorplan-view.js",
        "/static/floorplan.js",
        "/static/floorplan-search.js",
    ):
        assert client_csrf.get(path).status_code == 200


def test_offline_device_is_drawn_dimmed(
    client: TestClient, svc: Service, make_device: Callable[..., int], plan_seeded: None
) -> None:
    room = save_square_room(svc)
    off = make_device(name="Sleepy", online=0)
    on = make_device(name="Awake", online=1)
    _place(svc, off, room)
    _place(svc, on, room, 0.4, 0.4)
    html = client.get("/floorplan").text
    marker = re.search(rf'<circle[^>]*data-device-id="{off}"[^>]*>', html)
    assert marker and "offline" in marker.group(0)
    marker = re.search(rf'<circle[^>]*data-device-id="{on}"[^>]*>', html)
    assert marker and "offline" not in marker.group(0)


def test_trays_group_by_room_with_unnamed_fallback(
    client: TestClient, svc: Service, make_device: Callable[..., int]
) -> None:
    room = save_square_room(svc, "Room B")
    roamer = make_device(name="Phone")
    fixed_no_point = make_device()
    loose = make_device(name="Loose")
    svc.place_devices(
        "user",
        [
            Placement(roamer, room, None, None, roaming=True),
            Placement(fixed_no_point, room, None, None),
        ],
        based_on={},
    )
    html = client.get("/floorplan").text
    tray = html[html.index('id="tray"') :]
    assert tray.index("Room B") < tray.index("Phone") < tray.index("Unassigned")
    assert tray.index("Unassigned") < tray.index("Loose")
    assert "(unnamed)" in tray
    assert 'id="tray-filter"' in html
    # Tray items are not plan markers: [data-device-id] means "on the plan".
    assert f'data-device-id="{loose}"' not in html


def test_roaming_tray_item_shows_who_set_it_and_when(
    client: TestClient, svc: Service, make_device: Callable[..., int]
) -> None:
    room = save_square_room(svc, "Room B")
    roamer = make_device(name="Phone")
    svc.place_devices("claude", [Placement(roamer, room, None, None, roaming=True)], based_on={})
    html = client.get("/floorplan").text
    tray = html[html.index('id="tray"') :]
    m = re.search(r'<small class="set-by">(.*?)</small>', tray)
    assert m, "no roaming stamp in the tray"
    assert re.fullmatch(r"last set by claude at \d{4}-\d\d-\d\d \d\d:\d\d", m.group(1))
    # The JS draws the same text from these fields.
    placed = _data_block(html)["roaming"][0]
    assert placed["set_by"] == "claude" and placed["set_at"][:16].replace("T", " ") in m.group(1)


def test_json_data_block_cannot_break_out(
    client: TestClient, saved_room_named: Callable[[str], int]
) -> None:
    name = 'x</script><script>alert(1)</script><!--<script>"'
    saved_room_named(name)
    html = client.get("/floorplan").text
    assert "</script><script>alert(1)" not in html
    assert "<!--<script>" not in html
    assert _data_block(html)["rooms"][0]["name"] == name


# ----------------------------------------------------------------- uploads


def test_upload_png_then_image_route_headers(client_csrf: TestClient, tmp_data_dir: Path) -> None:
    data = png_bytes(1600, 1200)
    r = _upload(client_csrf, "Plan.PNG", data)
    assert r.status_code == 303 and r.headers["location"].startswith("/floorplan")
    digest = hashlib.sha256(data).hexdigest()
    assert _files(tmp_data_dir) == [f"{digest}.png"]
    r = client_csrf.get("/floorplan/plan.png")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert (
        r.headers["content-security-policy"]
        == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    )
    # Another extension than the stored one is not served.
    assert client_csrf.get("/floorplan/plan.svg").status_code == 404
    assert client_csrf.get("/floorplan/plan.exe").status_code == 404


def test_image_route_404_without_plan_or_file(client: TestClient, svc: Service) -> None:
    assert client.get("/floorplan/plan.png").status_code == 404
    svc.set_floor_plan("user", width=10, height=10, sha256="ab" * 32, file_name="plan.png")
    assert client.get("/floorplan/plan.png").status_code == 404


def test_upload_jpeg_is_normalized_to_jpg(client_csrf: TestClient, tmp_data_dir: Path) -> None:
    data = jpeg_bytes(800, 600)
    assert _upload(client_csrf, "photo.JPEG", data, "image/jpeg").status_code == 303
    assert _files(tmp_data_dir) == [f"{hashlib.sha256(data).hexdigest()}.jpg"]
    r = client_csrf.get("/floorplan/plan.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_upload_svg_served_as_svg(client_csrf: TestClient) -> None:
    assert _upload(client_csrf, "p.svg", svg_bytes(400, 300), "image/svg+xml").status_code == 303
    r = client_csrf.get("/floorplan/plan.svg")
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"]


def test_upload_unreadable_image_leaves_no_file(
    client_csrf: TestClient, tmp_data_dir: Path
) -> None:
    r = _upload(client_csrf, "p.png", b"\x89PNG not really")
    assert r.status_code == 422 and _is_html(r)
    assert "flash error" in r.text
    assert _files(tmp_data_dir) == []


def test_upload_oversized_is_422_and_no_file(client_csrf: TestClient, tmp_data_dir: Path) -> None:
    r = _upload(client_csrf, "p.png", b"\0" * (MAX_BYTES + 1))
    assert r.status_code == 422
    assert _files(tmp_data_dir) == []


def test_upload_far_oversized_body_is_refused_unparsed(
    client_csrf: TestClient, tmp_data_dir: Path
) -> None:
    r = _upload(client_csrf, "p.png", b"\0" * (MAX_BYTES + 256 * 1024))
    assert r.status_code == 422 and "10 MiB" in r.text
    assert _files(tmp_data_dir) == []


def test_upload_chunked_over_limit_without_content_length_is_422(
    client_csrf: TestClient, tmp_data_dir: Path
) -> None:
    boundary = "xBOUNDARYx"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="p.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body() -> Any:
        yield head
        for _ in range(11):  # 11 MiB in 1 MiB chunks
            yield b"\0" * (1024 * 1024)
        yield tail

    r = client_csrf.post(
        "/floorplan/plan",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        follow_redirects=False,
    )
    assert "content-length" not in r.request.headers
    assert r.request.headers["transfer-encoding"] == "chunked"
    assert r.status_code == 422 and "10 MiB" in r.text
    assert _files(tmp_data_dir) == []


def test_upload_aspect_mismatch_leaves_no_new_file(
    client_csrf: TestClient, tmp_data_dir: Path, db: sqlite3.Connection
) -> None:
    first = png_bytes(1600, 1200)
    assert _upload(client_csrf, "a.png", first).status_code == 303
    before = _files(tmp_data_dir)
    r = _upload(client_csrf, "b.png", png_bytes(1000, 1000))
    assert r.status_code == 422 and "aspect" in r.text
    assert _files(tmp_data_dir) == before
    assert tuple(db.execute("SELECT width, height FROM floor_plan").fetchone()) == (1600, 1200)


def test_upload_aspect_mismatch_keeps_preexisting_file(
    client_csrf: TestClient, tmp_data_dir: Path
) -> None:
    assert _upload(client_csrf, "a.png", png_bytes(1600, 1200)).status_code == 303
    square = png_bytes(1000, 1000)
    existing = tmp_data_dir / "floorplan" / f"{hashlib.sha256(square).hexdigest()}.png"
    existing.write_bytes(square)
    assert _upload(client_csrf, "b.png", square).status_code == 422
    assert existing.exists()


def test_upload_requires_csrf(client: TestClient, tmp_data_dir: Path) -> None:
    r = client.post("/floorplan/plan", files={"file": ("p.png", png_bytes(40, 30), "image/png")})
    assert r.status_code == 403
    assert _files(tmp_data_dir) == []


# ------------------------------------------------------------------- rooms


def test_room_form_creates_room_and_flashes(
    client_csrf: TestClient, db: sqlite3.Connection
) -> None:
    r = client_csrf.post(
        "/floorplan/rooms",
        data={"name": "Room A", "polygon": json.dumps(SQ_POINTS)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    html = client_csrf.get(r.headers["location"]).text
    assert 'class="flash ok"' in html and "Room saved" in html
    assert [r[0] for r in db.execute("SELECT name FROM place")] == ["Room A"]


def test_room_redraw_flashes_cleared_points(
    client_csrf: TestClient, svc: Service, make_device: Callable[..., int]
) -> None:
    room = save_square_room(svc)
    for xy in (0.15, 0.45):
        dev = make_device()
        _place(svc, dev, room, xy, xy)
    small = [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]
    r = client_csrf.post("/floorplan/rooms", data={"name": "Room A", "polygon": json.dumps(small)})
    assert r.status_code == 200
    assert "Room saved; 1 device point cleared" in r.text


def test_room_bad_polygon_is_422_and_writes_nothing(
    client_csrf: TestClient, db: sqlite3.Connection
) -> None:
    for poly in ("not json", "[[0, 0], [1, 1]]", "[[0, 0], [2, 0], [0, 1]]", '{"a": 1}', ""):
        r = client_csrf.post("/floorplan/rooms", data={"name": "Room A", "polygon": poly})
        assert r.status_code == 422, poly
        assert _is_html(r) and "flash error" in r.text
    r = client_csrf.post("/floorplan/rooms", data={"name": " ", "polygon": json.dumps(SQ_POINTS)})
    assert r.status_code == 422
    assert db.execute("SELECT count(*) FROM place").fetchone()[0] == 0


def test_rename_room(client: TestClient, client_csrf: TestClient, svc: Service) -> None:
    room = save_square_room(svc)
    other = save_square_room(svc, "Room B")
    url = f"/floorplan/rooms/{room}/name"
    token = client_csrf.headers.pop("X-CSRF-Token")
    assert client.post(url, data={"new_name": "Room C"}).status_code == 403
    r = client.post(url, data={"new_name": "Room C", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    assert svc.conn.execute("SELECT name FROM place WHERE id = ?", (room,)).fetchone()[0] == (
        "Room C"
    )
    r = client.post(url, data={"new_name": "Room B", "csrf": token})
    assert r.status_code == 422 and "already exists" in r.text
    assert (
        client.post(
            f"/floorplan/rooms/{other + 99}/name", data={"new_name": "Z", "csrf": token}
        ).status_code
        == 404
    )


# -------------------------------------------------------------- placements


def test_placements_json_saves(
    client_csrf: TestClient, room_and_device: Any, db: sqlite3.Connection
) -> None:
    rd = room_and_device
    body = {
        "placements": [{"device_id": rd.dev, "place_id": rd.room, "x": 0.3, "y": 0.2}],
        "based_on": {str(rd.dev): 0},
    }
    r = client_csrf.post("/floorplan/placements", json=body)
    assert r.status_code == 200 and r.json()["batch_id"] > 0
    assert tuple(_device_row(db, rd.dev)) == (rd.room, 0.3, 0.2, 0)


def test_placement_outside_room_is_json_422(
    client_csrf: TestClient, room_and_device: Any, db: sqlite3.Connection
) -> None:
    rd = room_and_device
    body = {"placements": [{"device_id": rd.dev, "place_id": rd.room, "x": 0.9, "y": 0.9}]}
    r = client_csrf.post("/floorplan/placements", json=body)
    assert r.status_code == 422
    assert "outside" in r.json()["detail"]
    assert _device_row(db, rd.dev)["place_id"] is None


def test_placements_body_is_strict_json_422(client_csrf: TestClient, room_and_device: Any) -> None:
    rd = room_and_device
    body = {"placements": [{"device_id": rd.dev, "place_id": rd.room, "actor": "claude"}]}
    r = client_csrf.post("/floorplan/placements", json=body)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    r = client_csrf.post("/floorplan/placements", json={"placements": [{"device_id": rd.dev}]})
    assert r.status_code == 422


def test_placements_need_csrf_header_and_same_origin(
    client: TestClient, client_csrf: TestClient, room_and_device: Any
) -> None:
    rd = room_and_device
    body = {"placements": [{"device_id": rd.dev, "place_id": rd.room}]}
    token = client_csrf.headers.pop("X-CSRF-Token")
    r = client.post("/floorplan/placements", json=body)
    assert r.status_code == 403 and r.json()["detail"]
    h = {"X-CSRF-Token": token, "Origin": "http://evil.example"}
    assert client.post("/floorplan/placements", json=body, headers=h).status_code == 403
    h["Origin"] = "http://testserver"
    assert client.post("/floorplan/placements", json=body, headers=h).status_code == 200


def test_unknown_device_placement_is_json_404(client_csrf: TestClient, seeded_room: int) -> None:
    body = {"placements": [{"device_id": 999999, "place_id": seeded_room}]}
    r = client_csrf.post("/floorplan/placements", json=body)
    assert r.status_code == 404 and "999999" in r.json()["detail"]


def test_stale_flash_after_reload(client: TestClient) -> None:
    html = client.get("/floorplan?stale=1").text
    assert 'class="flash error"' in html and "Reload" in html


# ------------------------------------------------------- device page roaming


def test_device_page_roaming_toggle(
    client: TestClient, client_csrf: TestClient, room_and_device: Any, svc: Service
) -> None:
    rd = room_and_device
    _place(svc, rd.dev, rd.room)
    url = f"/devices/{rd.dev}/roaming"
    page = client_csrf.get(f"/devices/{rd.dev}").text
    assert f'action="{url}"' in page
    token = client_csrf.headers.pop("X-CSRF-Token")
    assert client.post(url, data={"roaming": "on"}).status_code == 403
    r = client.post(url, data={"roaming": "on", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/devices/{rd.dev}"
    assert tuple(_device_row(rd.db, rd.dev)) == (rd.room, None, None, 1)
    assert "Roaming" in client.get(f"/devices/{rd.dev}").text
    r = client.post(url, data={"roaming": "off", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    assert tuple(_device_row(rd.db, rd.dev)) == (rd.room, None, None, 0)
    assert client.post(url, data={"roaming": "maybe", "csrf": token}).status_code == 422


def test_device_page_roaming_stale_write_renders_message(
    client_csrf: TestClient,
    room_and_device: Any,
    svc: Service,
    monkeypatch: Any,
) -> None:
    from lanscape_atlas.service import StaleWrite
    from lanscape_atlas.web.floorplan_api import STALE_MESSAGE

    rd = room_and_device
    _place(svc, rd.dev, rd.room)

    def boom(*_a: Any, **_k: Any) -> int:
        raise StaleWrite([rd.dev])

    monkeypatch.setattr(Service, "place_devices", boom)
    r = client_csrf.post(f"/devices/{rd.dev}/roaming", data={"roaming": "on"})
    assert r.status_code == 409 and _is_html(r)
    assert "flash error" in r.text and STALE_MESSAGE in r.text
    assert _device_row(rd.db, rd.dev)["roaming"] == 0


def test_device_page_roaming_needs_room(
    client_csrf: TestClient, seeded_device: int, db: sqlite3.Connection
) -> None:
    r = client_csrf.post(f"/devices/{seeded_device}/roaming", data={"roaming": "on"})
    assert r.status_code == 422 and _is_html(r)
    assert "room" in r.text and "flash error" in r.text
    assert _device_row(db, seeded_device)["roaming"] == 0


def test_floorplan_page_renders_heard_chip(
    client: TestClient, svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    from datetime import timedelta

    from lanscape_atlas.bluetooth.aggregator import BLE_MINUTE_FMT, MinuteRow

    room = save_square_room(svc)
    esp = make_device(name="Proxy")
    svc.set_place("user", esp, "Room A")
    svc.upsert_scanners({"00:00:5e:00:53:aa": "proxy"}, svc.clock())
    sid = db.execute("select id from ble_scanner").fetchone()[0]
    svc.link_scanner("user", sid, esp)
    heard = make_device(name="Keys")
    quiet = make_device(name="Quiet")
    svc.place_devices("user", [Placement(heard, room, None, None, True)], based_on={heard: 0})
    svc.place_devices("user", [Placement(quiet, room, None, None, True)], based_on={quiet: 0})
    at = svc.clock() - timedelta(minutes=3)
    svc.record_ble_minutes(
        [MinuteRow(heard, "00:00:5e:00:53:aa", at.strftime(BLE_MINUTE_FMT), -50, 2, at)]
    )
    html = client.get("/floorplan").text
    assert _data_block(html)["roaming"][0]["heard"]["place_id"] == room
    keys = html.split(f'data-tray-device="{heard}"', 1)[1].split("</li>", 1)[0]
    assert 'class="chip heard"' in keys
    quiet_li = html.split(f'data-tray-device="{quiet}"', 1)[1].split("</li>", 1)[0]
    assert "chip heard" not in quiet_li


def _tray_html(html: str, room_name: str) -> str:
    box = html.split(f"<h3>{room_name}</h3>", 1)[1]
    return box.split("</ul>", 1)[0]


def test_heard_entries_in_the_heard_rooms_tray(
    client: TestClient, svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    from datetime import timedelta

    from lanscape_atlas.bluetooth.aggregator import BLE_MINUTE_FMT, MinuteRow

    room_a = save_square_room(svc, "Room A")
    room_b = save_square_room(svc, "Room B")
    esp = make_device(name="Proxy")
    svc.set_place("user", esp, "Room B")
    svc.upsert_scanners({"00:00:5e:00:53:aa": "proxy"}, svc.clock())
    sid = db.execute("select id from ble_scanner").fetchone()[0]
    svc.link_scanner("user", sid, esp)
    placed = make_device(name="Remote")  # placed in A, heard in B
    loose = make_device(name="Loose")  # no room, heard in B
    trayed = make_device(name="Trayed")  # roaming in B, heard in B
    svc.place_devices("user", [Placement(placed, room_a, 0.2, 0.2, False)], based_on={placed: 0})
    svc.place_devices("user", [Placement(trayed, room_b, None, None, True)], based_on={trayed: 0})
    at = svc.clock() - timedelta(minutes=3)
    svc.record_ble_minutes(
        [
            MinuteRow(d, "00:00:5e:00:53:aa", at.strftime(BLE_MINUTE_FMT), -50, 2, at)
            for d in (placed, loose, trayed)
        ]
    )
    html = client.get("/floorplan").text
    room_b_tray = _tray_html(html, "Room B")
    assert f'data-heard-device="{placed}"' in room_b_tray
    assert f'data-heard-device="{loose}"' in room_b_tray
    assert f'data-heard-device="{trayed}"' not in room_b_tray  # already trayed: no duplicate
    assert room_b_tray.count('class="chip heard"') == 3  # placed, loose, trayed (own entry)
    assert room_b_tray.count("Trayed") == 1
    assert "<h3>Room A</h3>" not in html  # Room A has nothing to tray
    data = _data_block(html)
    assert data["placed"][0]["heard"]["place_id"] == room_b
    assert all(
        d["heard"]["place_id"] == room_b
        for d in data["roaming"] + data["unplaced"]
        if d["device_id"] != esp
    )
