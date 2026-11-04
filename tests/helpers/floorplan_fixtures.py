"""Shared floor plan fixtures, re-exported through ``tests/conftest.py``.

The client and MCP harnesses are copied from tests/test_web_security.py,
tests/test_topology.py and tests/test_mcp.py. Task-dependent service calls are
imported lazily so collection works before those tasks land.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import namedtuple
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client

from conftest import FixedClock
from helpers.plan import save_square_room
from lanscape_atlas.config import Settings
from lanscape_atlas.service import Service
from lanscape_atlas.web.app import create_app

TOKEN = "test-api-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_app(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, *, token: str | None = None
) -> FastAPI:
    # TestClient sends Host: testserver; LAN-only mode must allow it explicitly.
    settings = Settings(data_dir=tmp_path, api_token=token, allowed_hosts=("testserver",))
    return create_app(settings, conn=db, clock=clock, start_background=False)


@pytest.fixture
def client(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> Iterator[TestClient]:
    with TestClient(_make_app(tmp_path, db, clock)) as c:
        yield c


@pytest.fixture
def client_with_token(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> Iterator[TestClient]:
    with TestClient(_make_app(tmp_path, db, clock, token=TOKEN), headers=AUTH) as c:
        yield c


@pytest.fixture
def client_csrf(client: TestClient) -> TestClient:
    """A browser-style client that has fetched a CSRF cookie and sends the matching header."""
    if "atlas_csrf" not in client.cookies:
        assert client.get("/topology").status_code == 200
    token = client.cookies.get("atlas_csrf")
    assert token
    client.headers["X-CSRF-Token"] = token
    return client


@pytest.fixture
def mcp_client(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> Callable[[], Any]:
    """Factory for the in-process MCP session (as in tests/test_mcp.py).

    Use ``async with mcp_client() as c: await c.call_tool(name, args)``. Not an async
    fixture: the SDK client's task group must enter and exit in one task.
    """
    app = _make_app(tmp_path, db, clock)

    @asynccontextmanager
    async def _session() -> AsyncIterator[Client]:
        async with Client(app.state.mcp) as c:
            yield c

    return _session


@pytest.fixture
def seeded_room(svc: Service) -> int:
    return save_square_room(svc)


@pytest.fixture
def seeded_device(make_device: Callable[..., int]) -> int:
    return make_device()


RoomAndDevice = namedtuple("RoomAndDevice", ["db", "room", "dev"])


@pytest.fixture
def room_and_device(db: sqlite3.Connection, seeded_room: int, seeded_device: int) -> Any:
    return RoomAndDevice(db, seeded_room, seeded_device)


@pytest.fixture
def plan_seeded(svc: Service, db: sqlite3.Connection) -> None:
    """A ``floor_plan`` row exists (1600x1200 plan). No image file is written."""
    import hashlib

    from helpers.plan import png_bytes

    data = png_bytes(1600, 1200)
    svc.set_floor_plan(
        "user",
        width=1600,
        height=1200,
        sha256=hashlib.sha256(data).hexdigest(),
        file_name="plan.png",
    )


@pytest.fixture
def saved_room_named(svc: Service) -> Callable[[str], int]:
    def _save(name: str) -> int:
        return save_square_room(svc, name)

    return _save


StaleSetup = namedtuple("StaleSetup", ["body"])


@pytest.fixture
def stale_setup(svc: Service, room_and_device: Any) -> Any:
    """A placement payload whose ``based_on`` batch is already stale.

    The body has the placements API shape (``{"placements": [...], "based_on":
    {"<device_id>": batch}}``). ``based_on`` is the device's first placement
    batch; a second placement batch (a placement-field change, so it counts for
    ``last_placement_batch``) follows it.
    """
    from helpers.plan import svc_last
    from lanscape_atlas.service import Placement

    rd = room_and_device
    svc.place_devices("user", [Placement(rd.dev, rd.room, 0.3, 0.3)], based_on={rd.dev: 0})
    seen = svc_last(rd.db, rd.dev)
    svc.place_devices("user", [Placement(rd.dev, rd.room, 0.2, 0.2)], based_on={rd.dev: seen})
    assert svc_last(rd.db, rd.dev) > seen
    return StaleSetup(
        {
            "placements": [
                {"device_id": rd.dev, "place_id": rd.room, "x": 0.4, "y": 0.4, "roaming": False}
            ],
            "based_on": {str(rd.dev): seen},
        }
    )


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """The ``data_dir`` the ``client`` app uses (``tmp_path``), with ``floorplan/`` made."""
    (tmp_path / "floorplan").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


@pytest.fixture
def git_add() -> Callable[[Path, str], None]:
    def _add(repo: Path, path: str) -> None:
        subprocess.run(["git", "add", "--", path], cwd=repo, check=True)

    return _add


@pytest.fixture
def run_check_leaks() -> Callable[[Path], subprocess.CompletedProcess[str]]:
    def _run(repo: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_leaks.py")],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

    return _run


# ------------------------------------------------------------ browser (Playwright)


@pytest.fixture
def live_server(tmp_path: Path, clock: FixedClock) -> Iterator[str]:
    """Run the app under uvicorn on a free port; yields the base URL."""
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
    app.state.live_conn = conn
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    conn.close()


@pytest.fixture
def page() -> Iterator[Any]:
    """A Playwright page; the test is skipped when Playwright or a browser is unavailable."""
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"Playwright browser unavailable: {exc}")
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        yield pg
        browser.close()


@pytest.fixture
def synthetic_plan_and_room(live_server: str) -> str:
    """Upload a synthetic plan and save one square room via the running server."""
    import httpx

    from helpers.plan import SQ_POINTS, png_bytes

    with httpx.Client(base_url=live_server) as c:
        c.get("/topology")
        token = c.cookies.get("atlas_csrf", "")
        h = {"X-CSRF-Token": token}
        c.post("/floorplan/plan", files={"file": ("p.png", png_bytes(1600, 1200))}, headers=h)
        room = {"name": "Room A", "polygon": json.dumps(SQ_POINTS)}
        c.post("/floorplan/rooms", data=room, headers=h)
    return live_server


def _centre(locator: Any) -> tuple[float, float]:
    box = locator.bounding_box()
    assert box is not None
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _drag(page: Any, locator: Any, x: float, y: float) -> None:
    sx, sy = _centre(locator)
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(x, y, steps=8)
    page.mouse.up()


def drag_to_room_centre(page: Any, locator: Any) -> None:
    """Drag a marker to the centre of the first room polygon on the plan."""
    room = page.locator("[data-room-id]").first
    x, y = _centre(room)
    _drag(page, locator, x, y)


def drag_outside_room(page: Any, locator: Any) -> None:
    """Drag a marker to the plan's bottom-right corner, outside any room."""
    box = page.locator("#floorplan").bounding_box()
    assert box is not None
    _drag(page, locator, box["x"] + box["width"] - 5, box["y"] + box["height"] - 5)


def dev_position(page: Any, device_id: int) -> tuple[float, float] | None:
    """Marker position for a device in plan coordinates, or None when it is not on the plan."""
    el = page.locator(f"[data-device-id='{device_id}']")
    if el.count() == 0:
        return None
    return _centre(el.first)
