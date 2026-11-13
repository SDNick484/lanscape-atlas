"""POST /floorplan/shapes and the page's shape data (issue #10). Synthetic shapes only."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lanscape_atlas.service import NewShape, Service
from lanscape_atlas.service import queries as q

DATA_RE = re.compile(r'<script type="application/json" id="fp-data">(.*?)</script>', re.S)
WALL = {"points": [[0.1, 0.1], [0.4, 0.1]], "closed": False}
LABEL = {"x": 0.5, "y": 0.5, "size": 0.02}
DOOR = {"x": 0.25, "y": 0.1, "angle": 0.0, "width": 0.0667, "flip": 1}


def _data(html: str) -> dict[str, Any]:
    m = DATA_RE.search(html)
    assert m
    data: dict[str, Any] = json.loads(m.group(1))
    return data


def _count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT count(*) FROM plan_shape").fetchone()[0])


@pytest.fixture
def blank(svc: Service) -> None:
    svc.create_blank_plan("user", width_mm=12_000, height_mm=9_000, units="metric")


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "creates": [{"kind": "wall", "geometry": WALL}],
        "updates": [],
        "deletes": [],
        "based_on": 0,
    }
    body.update(over)
    return body


def test_save_creates_shapes(client_csrf: TestClient, db: sqlite3.Connection, blank: None) -> None:
    body = _body(creates=[{"kind": "wall", "geometry": WALL}, {"kind": "door", "geometry": DOOR}])
    r = client_csrf.post("/floorplan/shapes", json=body)
    assert r.status_code == 200
    bid = r.json()["batch_id"]
    assert bid > 0 and _count(db) == 2
    assert db.execute("SELECT actor FROM batch WHERE id = ?", (bid,)).fetchone()[0] == "user"


def test_save_furniture_and_label_and_the_page_carries_them(
    client_csrf: TestClient, db: sqlite3.Connection, blank: None
) -> None:
    rect = {"rect": {"cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.1, "angle": 20}}
    body = _body(
        creates=[
            {"kind": "furniture", "geometry": rect, "label": "Couch"},
            {"kind": "furniture", "geometry": {"points": [[0.1, 0.1], [0.3, 0.1], [0.3, 0.3]]}},
            {"kind": "label", "geometry": LABEL, "label": "Patio"},
        ]
    )
    r = client_csrf.post("/floorplan/shapes", json=body)
    assert r.status_code == 200 and _count(db) == 3
    shapes = _data(client_csrf.get("/floorplan").text)["shapes"]
    assert [(s["kind"], s["label"]) for s in shapes] == [
        ("furniture", "Couch"),
        ("furniture", None),
        ("label", "Patio"),
    ]


def test_save_updates_and_deletes(
    client_csrf: TestClient, svc: Service, db: sqlite3.Connection, blank: None
) -> None:
    seen = svc.save_shapes(
        "user",
        creates=[NewShape("wall", WALL), NewShape("door", DOOR)],
        updates=[],
        deletes=[],
        based_on=0,
    )
    wall, door = (int(r[0]) for r in db.execute("SELECT id FROM plan_shape ORDER BY id"))
    moved = {"points": [[0.1, 0.2], [0.4, 0.2]], "closed": False}
    body = _body(
        creates=[],
        updates=[{"id": wall, "geometry": moved, "label": None}],
        deletes=[door],
        based_on=seen,
    )
    r = client_csrf.post("/floorplan/shapes", json=body)
    assert r.status_code == 200, r.text
    assert [s["geometry"] for s in q.plan_shapes(db)] == [moved]


def test_page_data_carries_shapes(client_csrf: TestClient, svc: Service, blank: None) -> None:
    data = _data(client_csrf.get("/floorplan").text)
    assert (data["shapes"], data["shapes_batch"]) == ([], 0)
    bid = client_csrf.post("/floorplan/shapes", json=_body()).json()["batch_id"]
    data = _data(client_csrf.get("/floorplan").text)
    assert data["shapes_batch"] == bid
    assert [(s["kind"], s["geometry"], s["label"]) for s in data["shapes"]] == [
        ("wall", WALL, None)
    ]


def test_stale_save_is_409_and_writes_nothing(
    client_csrf: TestClient, db: sqlite3.Connection, blank: None
) -> None:
    first = client_csrf.post("/floorplan/shapes", json=_body()).json()["batch_id"]
    before = _count(db)
    r = client_csrf.post("/floorplan/shapes", json=_body(based_on=0))
    assert r.status_code == 409
    body = r.json()
    assert body["conflicting"] == [first]
    assert "drawing" in body["message"].lower() and "reload" in body["message"].lower()
    assert "device" not in body["message"].lower()
    assert _count(db) == before


def test_stale_shapes_flash(client: TestClient, blank: None) -> None:
    html = client.get("/floorplan?stale=shapes").text
    assert 'class="flash error"' in html and "drawing changed" in html
    html = client.get("/floorplan?saved=shapes").text
    assert "Drawing saved." in html


@pytest.mark.parametrize(
    "body",
    [
        _body(actor="claude"),
        _body(creates=[{"kind": "wall", "geometry": WALL, "id": 5}]),
        _body(updates=[{"id": 1, "geometry": WALL, "kind": "door"}]),
        {"creates": [], "updates": [], "deletes": []},  # based_on is required
        _body(based_on="0"),
        _body(based_on=1.0),
        _body(based_on=True),
        _body(deletes=["1"]),
        _body(deletes=[True]),
        _body(updates=[{"id": "1", "geometry": WALL}]),
        _body(creates=[{"kind": 1, "geometry": WALL}]),
        _body(creates=[{"kind": "wall", "geometry": [[0, 0], [1, 1]]}]),
        _body(creates=[{"kind": "wall", "geometry": WALL, "label": 3}]),
        _body(creates=[{"kind": "wall", "geometry": WALL, "label": "x" * 81}]),
    ],
)
def test_body_is_strict(
    client_csrf: TestClient, db: sqlite3.Connection, blank: None, body: Any
) -> None:
    r = client_csrf.post("/floorplan/shapes", json=body)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")
    assert _count(db) == 0


def test_too_many_items_refused(
    client_csrf: TestClient, db: sqlite3.Connection, blank: None
) -> None:
    r = client_csrf.post("/floorplan/shapes", json=_body(deletes=list(range(1, 5_002))))
    assert r.status_code == 422 and _count(db) == 0


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_body(creates=[{"kind": "window", "geometry": {"x": 0.5}}]), "kind"),
        (_body(creates=[{"kind": "furniture", "geometry": {"x": 0.5}}]), "exactly one"),
        (
            _body(creates=[{"kind": "label", "geometry": {"x": 0.5, "y": 0.5, "size": 0.02}}]),
            "label",
        ),
        (_body(creates=[{"kind": "wall", "geometry": {**WALL, "closed": 1}}]), "closed"),
        (
            _body(
                creates=[
                    {"kind": "wall", "geometry": {"points": [[0, 0], [2, 0]], "closed": False}}
                ]
            ),
            r"[0, 1]",
        ),
        (_body(creates=[{"kind": "wall", "geometry": WALL, "label": "Wall"}]), "no label"),
        (
            _body(
                creates=[
                    {
                        "kind": "furniture",
                        "geometry": {
                            "rect": {"cx": 0.99, "cy": 0.5, "w": 0.1, "h": 0.1, "angle": 0}
                        },
                    }
                ]
            ),
            "corner is outside",
        ),
        (_body(creates=[{"kind": "label", "geometry": LABEL, "label": " "}]), "needs a label"),
        (_body(creates=[{"kind": "label", "geometry": LABEL, "label": "x" * 81}]), "80 characters"),
    ],
)
def test_service_refusals_are_json_422(
    client_csrf: TestClient, db: sqlite3.Connection, blank: None, body: Any, message: str
) -> None:
    r = client_csrf.post("/floorplan/shapes", json=body)
    assert r.status_code == 422
    assert message in r.json()["detail"]
    assert _count(db) == 0


def test_unknown_id_is_json_404(client_csrf: TestClient, blank: None) -> None:
    r = client_csrf.post("/floorplan/shapes", json=_body(creates=[], deletes=[4242]))
    assert r.status_code == 404 and "4242" in r.json()["detail"]


def test_without_plan_is_json_422(client_csrf: TestClient, db: sqlite3.Connection) -> None:
    r = client_csrf.post("/floorplan/shapes", json=_body())
    assert r.status_code == 422 and "no floor plan" in r.json()["detail"]


def test_needs_csrf_header_and_same_origin(
    client: TestClient, client_csrf: TestClient, db: sqlite3.Connection, blank: None
) -> None:
    token = client_csrf.headers.pop("X-CSRF-Token")
    r = client.post("/floorplan/shapes", json=_body())
    assert r.status_code == 403 and r.json()["detail"]
    r = client.post("/floorplan/shapes", json=_body(), headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    h = {"X-CSRF-Token": token, "Origin": "http://evil.example"}
    assert client.post("/floorplan/shapes", json=_body(), headers=h).status_code == 403
    assert _count(db) == 0
    h["Origin"] = "http://testserver"
    assert client.post("/floorplan/shapes", json=_body(), headers=h).status_code == 200
    assert _count(db) == 1


def test_api_view_has_shapes(client_with_token: TestClient, svc: Service, blank: None) -> None:
    svc.save_shapes("user", creates=[NewShape("wall", WALL)], updates=[], deletes=[], based_on=0)
    view = client_with_token.get("/api/v1/floorplan").json()
    assert [s["kind"] for s in view["shapes"]] == ["wall"] and view["shapes_batch"] > 0


def test_no_shapes_api_write_route(client_with_token: TestClient, blank: None) -> None:
    """Drawing is page-only (spec §2): no JSON API write for shapes."""
    r = client_with_token.post("/api/v1/floorplan/shapes", json=_body())
    assert r.status_code in (404, 405)


# ------------------------------------------------------------------ Draw mode markup


def test_page_has_draw_mode(client: TestClient, blank: None) -> None:
    html = client.get("/floorplan").text
    assert 'id="mode-draw"' in html and 'id="shape-draw"' in html
    for tool in ("select", "wall", "polyline", "door"):
        assert f'data-tool="{tool}"' in html
    assert 'id="snap-toggle" checked' in html and 'id="lengths-toggle" checked' in html
    assert 'src="/static/floorplan-draw.js"' in html
    assert html.index("floorplan-draw.js") < html.index("/static/floorplan.js")


def test_lengths_toggle_only_with_a_scale(client: TestClient, svc: Service) -> None:
    svc.set_floor_plan("user", width=1600, height=1200, sha256="a" * 64, file_name="p.png")
    html = client.get("/floorplan").text
    assert 'id="mode-draw"' in html and 'id="lengths-toggle"' not in html


def test_page_without_plan_has_no_draw_mode(client: TestClient) -> None:
    html = client.get("/floorplan").text
    assert 'id="mode-draw"' not in html and 'id="shape-draw"' not in html
