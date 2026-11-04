"""HTTP API under /api/v1/floorplan."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from helpers.plan import SQ_POINTS

STALE_MESSAGE = "Some devices changed since this page loaded. Reload to see the latest values."


def _body(dev: int, room: int, x: float = 0.3, y: float = 0.3, **over: Any) -> dict[str, Any]:
    p = {"device_id": dev, "place_id": room, "x": x, "y": y, "roaming": False}
    p.update(over)
    return {"placements": [p], "based_on": {str(dev): 0}}


def test_get_returns_view_shape(client: TestClient) -> None:
    r = client.get("/api/v1/floorplan")
    assert r.status_code == 200
    assert set(r.json()) >= {"plan", "rooms", "placed", "roaming", "unplaced"}


def test_placement_happy_path_and_actor(
    client: TestClient, db: sqlite3.Connection, seeded_room: int, seeded_device: int
) -> None:
    r = client.post("/api/v1/floorplan/placements", json=_body(seeded_device, seeded_room))
    assert r.status_code == 200
    batch = r.json()["batch_id"]
    assert batch > 0
    actors = {row[0] for row in db.execute("SELECT actor FROM batch WHERE id = ?", (batch,))}
    assert actors == {"user"}
    assert [d["device_id"] for d in client.get("/api/v1/floorplan").json()["placed"]] == [
        seeded_device
    ]


def test_placement_outside_room_is_422(
    client: TestClient, seeded_room: int, seeded_device: int
) -> None:
    r = client.post(
        "/api/v1/floorplan/placements", json=_body(seeded_device, seeded_room, 0.9, 0.9)
    )
    assert r.status_code == 422


def test_unknown_device_is_404(client: TestClient, seeded_room: int) -> None:
    r = client.post("/api/v1/floorplan/placements", json=_body(99999, seeded_room))
    assert r.status_code == 404


def test_stale_placement_is_409_with_ids(
    client: TestClient, seeded_room: int, seeded_device: int
) -> None:
    body = _body(seeded_device, seeded_room)
    assert client.post("/api/v1/floorplan/placements", json=body).status_code == 200
    r = client.post("/api/v1/floorplan/placements", json=body)
    assert r.status_code == 409
    assert r.json() == {
        "detail": "conflict",
        "conflicting": [seeded_device],
        "message": STALE_MESSAGE,
    }


def test_noop_placement_returns_batch_zero(
    client: TestClient, seeded_room: int, seeded_device: int
) -> None:
    body = _body(seeded_device, seeded_room)
    assert client.post("/api/v1/floorplan/placements", json=body).status_code == 200
    from helpers.plan import svc_last

    seen = svc_last(client.app.state.service.conn, seeded_device)  # type: ignore[attr-defined]
    body["based_on"] = {str(seeded_device): seen}
    r = client.post("/api/v1/floorplan/placements", json=body)
    assert r.status_code == 200
    assert r.json() == {"batch_id": 0}


def test_put_room_happy_path(client: TestClient) -> None:
    r = client.put("/api/v1/floorplan/rooms", json={"name": "Room B", "polygon": SQ_POINTS})
    assert r.status_code == 200
    assert r.json()["batch_id"] > 0
    names = [x["name"] for x in client.get("/api/v1/floorplan").json()["rooms"]]
    assert "Room B" in names


def test_put_room_bad_polygon_is_422_and_writes_nothing(
    client: TestClient, db: sqlite3.Connection
) -> None:
    before = db.execute("SELECT count(*) FROM place").fetchone()[0]
    for poly in (
        [[0, 0], [1, 1]],
        [[0, 0], [1, 0], [1.5, 1]],
        "nope",
        [[0, 0], [1, 0], [0.5, -0.1]],
    ):
        r = client.put("/api/v1/floorplan/rooms", json={"name": "Bad", "polygon": poly})
        assert r.status_code == 422, poly
    nan = client.put(
        "/api/v1/floorplan/rooms",
        content='{"name": "Bad", "polygon": [[0, 0], [1, 0], [NaN, 1]]}',
        headers={"Content-Type": "application/json"},
    )
    assert nan.status_code == 422
    assert db.execute("SELECT count(*) FROM place").fetchone()[0] == before


def test_strict_types_rejected(client: TestClient, seeded_room: int, seeded_device: int) -> None:
    url = "/api/v1/floorplan/placements"
    assert (
        client.post(url, json=_body(seeded_device, seeded_room, roaming="yes")).status_code == 422
    )
    assert client.post(url, json=_body(True, seeded_room)).status_code == 422  # type: ignore[arg-type]
    b = _body(seeded_device, seeded_room)
    b["based_on"] = {"abc": 0}
    assert client.post(url, json=b).status_code == 422
    b["based_on"] = {str(seeded_device): True}
    assert client.post(url, json=b).status_code == 422


def test_unknown_field_is_422(client: TestClient, seeded_room: int, seeded_device: int) -> None:
    b = _body(seeded_device, seeded_room)
    b["actor"] = "claude"
    assert client.post("/api/v1/floorplan/placements", json=b).status_code == 422
    b = _body(seeded_device, seeded_room)
    b["placements"][0]["extra"] = 1
    assert client.post("/api/v1/floorplan/placements", json=b).status_code == 422
    r = client.put(
        "/api/v1/floorplan/rooms", json={"name": "X", "polygon": SQ_POINTS, "actor": "claude"}
    )
    assert r.status_code == 422


def test_token_required_when_configured(client_with_token: TestClient) -> None:
    assert client_with_token.get("/api/v1/floorplan").status_code == 200
    anon = TestClient(client_with_token.app)
    assert anon.get("/api/v1/floorplan", headers={"Host": "testserver"}).status_code == 401
