"""Ruling 43: whole-app Host allowlist in LAN-only mode; Origin check on unsafe calls."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import FixedClock
from lanscape_atlas.config import Settings
from lanscape_atlas.web.app import create_app

TOKEN = "test-api-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _app(
    tmp_path: Path,
    db: sqlite3.Connection,
    clock: FixedClock,
    *,
    token: str | None = None,
    allowed_hosts: tuple[str, ...] = (),
    bind_host: str = "127.0.0.1",
) -> FastAPI:
    settings = Settings(
        data_dir=tmp_path, api_token=token, allowed_hosts=allowed_hosts, bind_host=bind_host
    )
    return create_app(settings, conn=db, clock=clock, start_background=False)


def _batch(app: FastAPI) -> int:
    svc = app.state.service
    device_id, _ = svc.create_placeholder_device("user", "Switch", "switch")
    batch: int = svc.update_device("user", device_id, {"notes": "x"})
    return batch


# ------------------------------------------------------------- Host allowlist


@pytest.mark.parametrize("path", ["/", "/inbox", "/api/v1/devices", "/topology/data"])
def test_lan_mode_rejects_unlisted_host(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, path: str
) -> None:
    app = _app(tmp_path, db, clock)
    with TestClient(app) as c:
        # TestClient's default Host is "testserver": not allowed by default.
        assert c.get(path).status_code == 421
        assert c.get(path, headers={"Host": "evil.example"}).status_code == 421
        assert c.get(path, headers={"Host": "evil.example:8720"}).status_code == 421


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8720", "127.0.0.1", "127.0.0.1:8720", "[::1]:8720"]
)
def test_lan_mode_accepts_local_hosts(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, host: str
) -> None:
    app = _app(tmp_path, db, clock)
    with TestClient(app) as c:
        assert c.get("/api/v1/devices", headers={"Host": host}).status_code == 200
        assert c.get("/", headers={"Host": host}).status_code == 200


def test_lan_mode_accepts_bind_host_and_allowed_hosts(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(
        tmp_path,
        db,
        clock,
        bind_host="192.0.2.5",
        allowed_hosts=("atlas.example.com", "atlas.lan:*"),
    )
    with TestClient(app) as c:
        ok = ["192.0.2.5", "192.0.2.5:8720", "atlas.example.com", "atlas.lan:8720", "atlas.lan:1"]
        for host in ok:
            assert c.get("/inbox", headers={"Host": host}).status_code == 200, host
        # Bare entries match only a Host without a port (same semantics as /mcp).
        for host in ["atlas.example.com:8720", "atlas.lan", "atlas.lan.evil.example:80"]:
            assert c.get("/inbox", headers={"Host": host}).status_code == 421, host


def test_wildcard_bind_is_not_an_allowed_host(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(tmp_path, db, clock, bind_host="0.0.0.0")  # noqa: S104
    with TestClient(app) as c:
        assert c.get("/", headers={"Host": "0.0.0.0:8720"}).status_code == 421


def test_star_disables_host_check(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(tmp_path, db, clock, allowed_hosts=("*",))
    with TestClient(app) as c:
        assert c.get("/inbox", headers={"Host": "anything.example"}).status_code == 200


def test_token_mode_skips_host_check(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(tmp_path, db, clock, token=TOKEN)
    with TestClient(app) as c:
        assert c.get("/inbox", headers={"Host": "atlas.example.com"}).status_code == 200
        assert c.get("/api/v1/devices", headers=AUTH).status_code == 200


# --------------------------------------------------------------- Origin check


@pytest.mark.parametrize("token", [None, TOKEN])
def test_cross_origin_undo_refused(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, token: str | None
) -> None:
    app = _app(tmp_path, db, clock, token=token, allowed_hosts=("testserver",))
    b = _batch(app)
    auth = AUTH if token else {}
    with TestClient(app) as c:
        for origin in ("http://evil.example", "null", "http://testserver.evil.example"):
            r = c.post(f"/api/v1/changes/{b}/undo", headers={**auth, "Origin": origin})
            assert r.status_code == 403, origin
            assert r.json() == {"detail": "cross-origin request refused"}
        assert app.state.conn.execute("select count(*) from batch").fetchone()[0] == 2
        # Same origin and no Origin at all (curl, MCP clients) both pass.
        r = c.post(f"/api/v1/changes/{b}/undo", headers={**auth, "Origin": "http://testserver"})
        assert r.status_code == 200, r.text
        u = r.json()["batch_id"]
        assert c.post(f"/api/v1/changes/{u}/undo", headers=auth).status_code == 200


def test_cross_origin_deep_scan_refused(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(tmp_path, db, clock, allowed_hosts=("testserver",))
    with TestClient(app) as c:
        r = c.post("/api/v1/devices/1/deep-scan", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        assert not app.state.deep_scan_tasks


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_cross_origin_unsafe_methods_on_topology_and_api(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, method: str
) -> None:
    app = _app(tmp_path, db, clock, allowed_hosts=("testserver",))
    evil = {"Origin": "https://evil.example"}
    with TestClient(app) as c:
        for path in ("/api/v1/devices/1", "/topology/layout"):
            assert c.request(method, path, headers=evil).status_code == 403, path


def test_origin_check_matches_default_ports(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(tmp_path, db, clock, token=TOKEN)
    b = _batch(app)
    with TestClient(app) as c:
        h = {**AUTH, "Host": "atlas.example.com", "Origin": "https://atlas.example.com:443"}
        assert c.post(f"/api/v1/changes/{b}/undo", headers=h).status_code == 200
        h = {**AUTH, "Host": "atlas.example.com", "Origin": "https://atlas.example.com:8443"}
        assert c.post("/api/v1/changes/1/undo", headers=h).status_code == 403


def test_safe_methods_ignore_origin(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(tmp_path, db, clock, allowed_hosts=("testserver",))
    with TestClient(app) as c:
        r = c.get("/api/v1/devices", headers={"Origin": "http://evil.example"})
        assert r.status_code == 200
