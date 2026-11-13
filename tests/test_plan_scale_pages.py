"""Floor plan page: blank plans, scale and units (spec §5.3, §5.4). Synthetic data only."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers.plan import png_bytes
from lanscape_atlas.service import Service

DATA_RE = re.compile(r'<script type="application/json" id="fp-data">(.*?)</script>', re.S)


def _data(html: str) -> dict[str, Any]:
    m = DATA_RE.search(html)
    assert m
    data: dict[str, Any] = json.loads(m.group(1))
    return data


def _plan(db: sqlite3.Connection) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM floor_plan").fetchone()
    return None if row is None else dict(row)


def _blank(client: TestClient, width: str, depth: str, units: str) -> Any:
    return client.post(
        "/floorplan/blank",
        data={"width": width, "depth": depth, "units": units},
        follow_redirects=False,
    )


def test_page_without_plan_offers_both_choices(client: TestClient) -> None:
    html = client.get("/floorplan").text
    assert "Upload an image" in html and "Start a blank plan" in html
    assert 'id="blank-form"' in html and 'action="/floorplan/blank"' in html
    assert 'id="plan-upload"' in html and 'id="plan-grid"' not in html


@pytest.mark.parametrize(
    ("width", "depth", "units", "mm"),
    [("40", "30.5", "imperial", (12_192, 9_296)), ("12", "9", "metric", (12_000, 9_000))],
)
def test_blank_form_creates_plan_and_opens_layout(
    client_csrf: TestClient, db: sqlite3.Connection, width: str, depth: str, units: str, mm: Any
) -> None:
    r = _blank(client_csrf, width, depth, units)
    assert r.status_code == 303
    assert r.headers["location"] == "/floorplan?saved=blank&layout=1"
    plan = _plan(db)
    assert plan is not None
    assert (plan["width"], plan["height"], plan["units"]) == (*mm, units)
    assert plan["width_mm"] == mm[0] and plan["sha256"] is None
    html = client_csrf.get("/floorplan?saved=blank&layout=1").text
    assert 'id="plan-grid"' in html and 'id="plan-img"' not in html
    assert f'viewBox="0 0 {mm[0]} {mm[1]}"' in html
    assert 'id="mode-scale"' not in html  # a blank plan's scale is its size
    assert 'id="units-form"' in html and "Add an image" in html
    data = _data(html)["plan"]
    assert data == {
        "width": mm[0],
        "height": mm[1],
        "url": None,
        "blank": True,
        "width_mm": float(mm[0]),
        "units": units,
    }


@pytest.mark.parametrize(
    ("width", "depth", "units", "message"),
    [
        ("0", "30", "imperial", "width must be between"),
        ("40", "0", "metric", "depth must be between"),
        ("-12", "9", "metric", "width must be between"),
        ("3", "9", "imperial", "width must be between"),  # 0.91 m
        ("301", "9", "metric", "width must be between"),
        ("", "9", "metric", "width must be a number"),
        ("ten", "9", "metric", "width must be a number"),
        ("nan", "9", "metric", "width must be a number"),
        ("inf", "9", "metric", "width must be a number"),
        ("12", "9", "cubits", "feet or metres"),
    ],
)
def test_blank_form_refuses_bad_input(
    client_csrf: TestClient,
    db: sqlite3.Connection,
    width: str,
    depth: str,
    units: str,
    message: str,
) -> None:
    r = _blank(client_csrf, width, depth, units)
    assert r.status_code == 422
    assert message in r.text
    assert _plan(db) is None


def test_blank_form_needs_csrf(client: TestClient, db: sqlite3.Connection) -> None:
    assert _blank(client, "12", "9", "metric").status_code == 403
    assert _plan(db) is None


def test_blank_form_refused_when_plan_exists(client_csrf: TestClient, svc: Service) -> None:
    svc.create_blank_plan("user", width_mm=5_000, height_mm=5_000, units="metric")
    r = _blank(client_csrf, "12", "9", "metric")
    assert r.status_code == 422 and "already exists" in r.text


def test_blank_form_foreign_origin_refused(client_csrf: TestClient, db: sqlite3.Connection) -> None:
    r = client_csrf.post(
        "/floorplan/blank",
        data={"width": "12", "depth": "9", "units": "metric"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert _plan(db) is None


def test_units_form(client_csrf: TestClient, svc: Service, db: sqlite3.Connection) -> None:
    svc.create_blank_plan("user", width_mm=5_000, height_mm=5_000, units="imperial")
    r = client_csrf.post("/floorplan/units", data={"units": "metric"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/floorplan?saved=units"
    assert _plan(db)["units"] == "metric"  # type: ignore[index]
    r = client_csrf.post("/floorplan/units", data={"units": "cubits"})
    assert r.status_code == 422
    assert _plan(db)["units"] == "metric"  # type: ignore[index]


def test_units_form_needs_csrf(client: TestClient, svc: Service, db: sqlite3.Connection) -> None:
    svc.create_blank_plan("user", width_mm=5_000, height_mm=5_000, units="imperial")
    assert client.post("/floorplan/units", data={"units": "metric"}).status_code == 403
    assert _plan(db)["units"] == "imperial"  # type: ignore[index]


def _upload(client: TestClient) -> None:
    r = client.post(
        "/floorplan/plan",
        files={"file": ("p.png", png_bytes(1600, 1200), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_image_plan_has_scale_tool_and_units_only_with_scale(
    client_csrf: TestClient, svc: Service
) -> None:
    _upload(client_csrf)
    html = client_csrf.get("/floorplan").text
    assert 'id="mode-scale"' in html and 'id="scale-draw"' in html
    assert 'id="units-form"' not in html
    svc.set_plan_scale("user", width_mm=8_000)
    html = client_csrf.get("/floorplan").text
    assert 'id="units-form"' in html
    assert _data(html)["plan"]["width_mm"] == 8_000


def test_scale_route(client_csrf: TestClient, db: sqlite3.Connection) -> None:
    _upload(client_csrf)
    r = client_csrf.post("/floorplan/scale", json={"width_mm": 9144.5})
    assert r.status_code == 200 and r.json()["batch_id"] > 0
    assert _plan(db)["width_mm"] == 9144.5  # type: ignore[index]


@pytest.mark.parametrize(
    "body",
    [{"width_mm": 0}, {"width_mm": -3}, {"width_mm": "9000"}, {"width_mm": True}, {}, {"x": 1}],
)
def test_scale_route_refuses_bad_bodies(
    client_csrf: TestClient, db: sqlite3.Connection, body: dict[str, Any]
) -> None:
    _upload(client_csrf)
    r = client_csrf.post("/floorplan/scale", json=body)
    assert r.status_code == 422
    assert _plan(db)["width_mm"] is None  # type: ignore[index]


def test_scale_route_refused_on_blank_plan(client_csrf: TestClient, svc: Service) -> None:
    svc.create_blank_plan("user", width_mm=5_000, height_mm=5_000, units="metric")
    r = client_csrf.post("/floorplan/scale", json={"width_mm": 8000})
    assert r.status_code == 422 and "blank" in r.json()["detail"]


def test_scale_route_needs_csrf_header(client: TestClient, db: sqlite3.Connection) -> None:
    assert client.post("/floorplan/scale", json={"width_mm": 8000}).status_code == 403


def test_image_route_is_404_for_blank_plan(client: TestClient, svc: Service) -> None:
    svc.create_blank_plan("user", width_mm=5_000, height_mm=5_000, units="metric")
    for ext in ("png", "jpg", "svg"):
        assert client.get(f"/floorplan/plan.{ext}").status_code == 404


def test_api_floorplan_view_has_blank_fields(client_with_token: TestClient, svc: Service) -> None:
    svc.create_blank_plan("user", width_mm=5_000, height_mm=4_000, units="metric")
    plan = client_with_token.get("/api/v1/floorplan").json()["plan"]
    assert (plan["blank"], plan["width_mm"], plan["units"]) == (True, 5000.0, "metric")
