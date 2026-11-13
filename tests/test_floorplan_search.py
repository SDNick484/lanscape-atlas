"""``GET /floorplan/search`` (spec §4.2): ranked devices plus rooms, each tagged ``kind``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lanscape_atlas.service import Service


def _search(client: TestClient, q: str) -> Any:
    return client.get("/floorplan/search", params={"q": q})


def _room(svc: Service, name: str) -> int:
    svc.save_room("user", name, [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4]])
    row = svc.conn.execute("SELECT id FROM place WHERE name = ?", (name,)).fetchone()
    return int(row[0])


def test_devices_and_rooms_in_one_result_set(
    client: TestClient, svc: Service, make_device: Callable[..., int]
) -> None:
    dev = make_device(name="Office Lamp", type="lamp")
    room = _room(svc, "Office")
    _room(svc, "Room A")
    r = _search(client, "office")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == [
        {"kind": "device", "device_id": dev, "name": "Office Lamp", "type": "lamp"},
        {"kind": "room", "place_id": room, "name": "Office"},
    ]


def test_devices_use_the_shared_ranking(
    client: TestClient, svc: Service, make_device: Callable[..., int]
) -> None:
    later = make_device(name="My Frob")
    first = make_device(name="Frob Box")
    got = [d["device_id"] for d in _search(client, "frob").json() if d["kind"] == "device"]
    assert got == [first, later]  # name-prefix before the rest, as on Topology


def test_room_match_ignores_case_and_ranks_prefix_first(client: TestClient, svc: Service) -> None:
    inner = _room(svc, "Big room b")
    prefix = _room(svc, "ROOM A")
    _room(svc, "Hall")
    got = _search(client, "rOoM").json()
    assert [(d["kind"], d["place_id"]) for d in got] == [("room", prefix), ("room", inner)]


def test_room_name_with_markup_is_returned_as_data(client: TestClient, svc: Service) -> None:
    room = _room(svc, "<b>x</b>")
    assert _search(client, "<b>").json() == [{"kind": "room", "place_id": room, "name": "<b>x</b>"}]


def test_empty_query_and_length_cap(client: TestClient, svc: Service) -> None:
    _room(svc, "Room A")
    assert _search(client, "").json() == []
    assert _search(client, "   ").json() == []
    assert client.get("/floorplan/search").json() == []
    assert _search(client, "a" * 100).status_code == 200
    assert _search(client, "a" * 101).status_code == 422


@pytest.mark.parametrize("q", ["%", "_", "m%a", "m_a"])
def test_percent_and_underscore_are_literal(
    client: TestClient, svc: Service, make_device: Callable[..., int], q: str
) -> None:
    _room(svc, "Room A")
    make_device(name="Room A Lamp")
    literal = _room(svc, "50% m%a m_a_room")
    got = _search(client, q).json()
    assert [(d["kind"], d.get("place_id")) for d in got] == [("room", literal)]


def test_room_results_are_capped(client: TestClient, svc: Service) -> None:
    for i in range(25):
        _room(svc, f"Room {i:02d}")
    got = _search(client, "room").json()
    assert [d["name"] for d in got] == [f"Room {i:02d}" for i in range(20)]


def test_search_needs_no_token_and_is_get_only(client: TestClient) -> None:
    assert _search(client, "x").status_code == 200
    assert client.post("/floorplan/search").status_code in (403, 405)
