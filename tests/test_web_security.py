"""Ruling 43: whole-app Host allowlist (every mode); Origin check on every unsafe call."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import FixedClock
from lanscape_atlas.config import Settings
from lanscape_atlas.web.app import (
    REJECTED_HOSTS_LOGGED_MAX,
    allowed_host_patterns,
    create_app,
    mcp_transport_security,
)

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


@pytest.mark.parametrize("path", ["/", "/inbox", "/topology/data", "/api/v1/devices", "/mcp"])
def test_token_mode_keeps_host_check(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, path: str
) -> None:
    """The token guards only /api and /mcp, so it must not switch the Host check off."""
    app = _app(tmp_path, db, clock, token=TOKEN)
    with TestClient(app) as c:
        for host in ("testserver", "atlas.example.com", "evil.example:8720"):
            assert c.get(path, headers={**AUTH, "Host": host}).status_code == 421, host
        assert c.get("/inbox", headers={"Host": "localhost:8720"}).status_code == 200
        assert c.get("/api/v1/devices", headers={**AUTH, "Host": "127.0.0.1"}).status_code == 200


def test_token_mode_accepts_allowed_hosts_and_star(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(tmp_path, db, clock, token=TOKEN, allowed_hosts=("atlas.example.com",))
    with TestClient(app) as c:
        h = {"Host": "atlas.example.com"}
        assert c.get("/inbox", headers=h).status_code == 200
        assert c.get("/api/v1/devices", headers={**AUTH, **h}).status_code == 200
        assert c.get("/api/v1/devices", headers=h).status_code == 401
        assert c.get("/inbox", headers={"Host": "other.example"}).status_code == 421
    app = _app(tmp_path, db, clock, token=TOKEN, allowed_hosts=("*",))
    with TestClient(app) as c:
        assert c.get("/inbox", headers={"Host": "anything.example"}).status_code == 200


@pytest.mark.parametrize("token", [None, TOKEN])
def test_rejected_host_response_explains_the_fix(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, token: str | None
) -> None:
    app = _app(tmp_path, db, clock, token=token)
    with TestClient(app) as c:
        r = c.get("/inbox", headers={"Host": "atlas.example.com:8720"})
        assert r.status_code == 421
        assert r.headers["content-type"].startswith("text/plain")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert 'Host "atlas.example.com:8720"' in r.text
        assert "allowed_hosts" in r.text and "config.toml" in r.text
        # The token is checked after the Host, and is never echoed.
        r = c.get("/api/v1/devices", headers={**AUTH, "Host": "atlas.example.com"})
        assert r.status_code == 421 and TOKEN not in r.text


def test_rejected_host_is_echoed_as_plain_safe_text(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _app(tmp_path, db, clock)
    with TestClient(app) as c:
        r = c.get("/", headers={"Host": '<script>alert(1)</script>"x'})
        assert r.status_code == 421
        assert r.headers["content-type"].startswith("text/plain")
        assert not set(r.text) & set("<>'\\/()")
        first = r.text.splitlines()[0]
        assert (
            first == 'Request refused: Host "?script?alert?1???script??x" is not in allowed_hosts.'
        )
        r = c.get("/", headers={"Host": "a" * 5000 + ".example"})
        assert r.status_code == 421 and len(r.text) < 1000


def test_rejected_hosts_are_logged_once_each_and_bounded(
    tmp_path: Path,
    db: sqlite3.Connection,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _app(tmp_path, db, clock, token=TOKEN)
    caplog.set_level(logging.WARNING, logger="lanscape_atlas.web.app")

    def warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "lanscape_atlas.web.app"]

    with TestClient(app) as c:
        for _ in range(3):
            c.get("/", headers={"Host": "atlas.example.com"})
            c.get("/inbox", headers={"Host": "ATLAS.example.com"})
        assert len(warnings()) == 1
        assert "atlas.example.com" in warnings()[0] and "allowed_hosts" in warnings()[0]
        c.get("/", headers={"Host": "evil.example<b>"})
        assert len(warnings()) == 2 and "<" not in warnings()[1]
        assert c.get("/", headers={"Host": "localhost"}).status_code == 200
        assert len(warnings()) == 2
        for i in range(REJECTED_HOSTS_LOGGED_MAX * 3):
            assert c.get("/", headers={"Host": f"h{i}.example"}).status_code == 421
        # Each remembered name once, plus one line saying the rest are not logged.
        assert len(warnings()) == REJECTED_HOSTS_LOGGED_MAX + 1
        assert "not logged" in warnings()[-1]


def test_mcp_lists_follow_the_host_allowlist_in_every_mode(tmp_path: Path) -> None:
    for token in (None, TOKEN):
        s = Settings(data_dir=tmp_path, api_token=token, allowed_hosts=("atlas.example.com",))
        sec = mcp_transport_security(s)
        assert sec.enable_dns_rebinding_protection
        assert sec.allowed_hosts == allowed_host_patterns(s)
        assert "https://atlas.example.com" in sec.allowed_origins
        s = Settings(data_dir=tmp_path, api_token=token, allowed_hosts=("*",))
        assert allowed_host_patterns(s) is None
        assert not mcp_transport_security(s).enable_dns_rebinding_protection


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
    app = _app(tmp_path, db, clock, token=TOKEN, allowed_hosts=("atlas.example.com",))
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


# ------------------------------------------------- Origin check on form routes

PLANTED = "p" * 43  # a cookie another app on the same host (other port) could set


def _trust(app: FastAPI, device_id: int) -> str:
    row = app.state.conn.execute("select trust from device where id = ?", (device_id,)).fetchone()
    return str(row[0])


def _name(app: FastAPI, device_id: int) -> str:
    row = app.state.conn.execute("select name from device where id = ?", (device_id,)).fetchone()
    return str(row[0])


def _batches(app: FastAPI) -> int:
    return int(app.state.conn.execute("select count(*) from batch").fetchone()[0])


@pytest.mark.parametrize("token", [None, TOKEN])
@pytest.mark.parametrize(
    "origin",
    ["http://192.0.2.5:9000", "https://192.0.2.5:9443", "http://192.0.2.5", "null"],
)
def test_cross_origin_form_posts_refused_despite_planted_csrf_cookie(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, token: str | None, origin: str
) -> None:
    """Cookies are not port-scoped: the double-submit token alone does not prove origin."""
    app = _app(tmp_path, db, clock, token=token, bind_host="192.0.2.5")
    svc = app.state.service
    device_id, _ = svc.create_placeholder_device("user", "Switch", "switch")
    batch = svc.update_device("user", device_id, {"notes": "x"})
    trust, before = _trust(app, device_id), _batches(app)
    posts = [
        (f"/inbox/{device_id}/unwanted", {}),
        (f"/devices/{device_id}", {"name": "Pwned", "type": "switch"}),
        (f"/devices/{device_id}/deep-scan", {}),
        (f"/history/{batch}/undo", {}),
        ("/find/hide", {"identity": "x"}),
        ("/interfaces/1/claim", {"device_id": str(device_id)}),
        ("/status/ble-hidden/1/unhide", {}),
    ]
    with TestClient(app, base_url="http://192.0.2.5:8720") as c:
        c.cookies.set("atlas_csrf", PLANTED)
        for path, data in posts:
            r = c.post(
                path,
                data={"csrf": PLANTED, **data},
                headers={"Origin": origin},
                follow_redirects=False,
            )
            assert r.status_code == 403, path
            assert r.json() == {"detail": "cross-origin request refused"}, path
        assert _batches(app) == before
        assert _trust(app, device_id) == trust and _name(app, device_id) == "Switch"
        assert not app.state.deep_scan_tasks


def test_planted_cookie_alone_would_pass_the_csrf_check(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    """Why the Origin check is needed: without an Origin header the planted pair is accepted."""
    app = _app(tmp_path, db, clock, bind_host="192.0.2.5")
    device_id, _ = app.state.service.create_placeholder_device("user", "Switch", "switch")
    with TestClient(app, base_url="http://192.0.2.5:8720") as c:
        c.cookies.set("atlas_csrf", PLANTED)
        r = c.post(f"/inbox/{device_id}/unwanted", data={"csrf": PLANTED}, follow_redirects=False)
        assert r.status_code == 303
        assert _trust(app, device_id) == "unwanted"


@pytest.mark.parametrize("token", [None, TOKEN])
def test_same_origin_and_originless_form_posts_still_work(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, token: str | None
) -> None:
    app = _app(tmp_path, db, clock, token=token, bind_host="192.0.2.5")
    svc = app.state.service
    device_id, _ = svc.create_placeholder_device("user", "Switch", "switch")
    with TestClient(app, base_url="http://192.0.2.5:8720") as c:
        assert c.get("/inbox").status_code == 200
        csrf = c.cookies["atlas_csrf"]
        same = {"Origin": "http://192.0.2.5:8720"}
        r = c.post(
            f"/inbox/{device_id}/unwanted",
            data={"csrf": csrf},
            headers=same,
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        assert _trust(app, device_id) == "unwanted"
        batch = app.state.conn.execute("select max(id) from batch").fetchone()[0]
        # No Origin header at all (curl, old clients): unchanged, the CSRF token decides.
        r = c.post(f"/history/{batch}/undo", data={"csrf": csrf}, follow_redirects=False)
        assert r.status_code == 303, r.text
        assert _trust(app, device_id) != "unwanted"
        r = c.post(f"/history/{batch}/undo", data={"csrf": "wrong"}, follow_redirects=False)
        assert r.status_code == 403 and "CSRF" in r.text


def test_origin_check_leaves_the_mcp_mount_to_its_own_check(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    """/mcp is not under the app-wide Origin rule: the SDK checks Origin against the allowlist."""
    app = _app(tmp_path, db, clock, token=TOKEN, allowed_hosts=("atlas.example.com", "testserver"))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    h = {**AUTH, "Accept": "application/json, text/event-stream"}
    with TestClient(app) as c:
        # Another allow-listed origin than the Host: fine for /mcp, refused for /api.
        ok = {**h, "Origin": "https://atlas.example.com"}
        assert c.post("/mcp", json=body, headers=ok).status_code == 200
        assert c.post("/api/v1/changes/1/undo", headers=ok).status_code == 403
        r = c.post("/mcp", json=body, headers={**h, "Origin": "http://evil.example"})
        assert r.status_code == 403
        assert c.post("/mcp", json=body, headers=h).status_code == 200
