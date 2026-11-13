"""HTTP API (/api/v1): routes, error mapping, bearer auth, deep scan and topology."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import FixedClock
from lanscape_atlas.config import Settings
from lanscape_atlas.observation import Observation
from lanscape_atlas.web.app import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "fake_house.json"
TOKEN = "test-api-token-123"
# TestClient sends Host: testserver; LAN-only mode must allow it explicitly.
TEST_HOSTS = ("testserver",)


def _observations() -> list[Observation]:
    raw = json.loads(FIXTURE.read_text())
    return [Observation.from_json(json.dumps(d)) for d in raw]


def _make_app(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, token: str | None = None
) -> FastAPI:
    settings = Settings(data_dir=tmp_path, api_token=token, allowed_hosts=TEST_HOSTS)
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    res = app.state.ingestor.ingest(_observations())
    assert res.dropped == 0
    return app


@pytest.fixture
def app(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> FastAPI:
    return _make_app(tmp_path, db, clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _device_with_ip(client: TestClient) -> dict[str, Any]:
    devices = client.get("/api/v1/devices", params={"online": "true", "limit": 1000}).json()
    return next(d for d in devices if d["ipv4"])


# ---------------------------------------------------------------- devices


def test_list_devices_q_matches_ip_and_mac(client: TestClient) -> None:
    """Ruling 45: ?q= uses the find_device matcher (current IPv4, MAC, text)."""
    d = _device_with_ip(client)
    by_ip = client.get("/api/v1/devices", params={"q": d["ipv4"]}).json()
    assert [r["id"] for r in by_ip] == [d["id"]]
    mac = d["macs"][0]
    by_mac = client.get("/api/v1/devices", params={"q": mac.upper().replace(":", "-")}).json()
    assert d["id"] in {r["id"] for r in by_mac}
    tail = mac.replace(":", "")[-4:]
    dotted = client.get("/api/v1/devices", params={"q": tail[:2] + "." + tail[2:]}).json()
    assert d["id"] in {r["id"] for r in dotted}


def test_list_devices_filter_online(client: TestClient, db: sqlite3.Connection) -> None:
    all_ = client.get("/api/v1/devices", params={"limit": 1000}).json()
    assert len(all_) >= 40
    victim = all_[0]["id"]
    with db:
        db.execute("UPDATE device SET online = 0 WHERE id = ?", (victim,))
    online = client.get("/api/v1/devices", params={"online": "true", "limit": 1000}).json()
    offline = client.get("/api/v1/devices", params={"online": "false", "limit": 1000}).json()
    assert online and all(d["online"] for d in online)
    assert victim in {d["id"] for d in offline}
    assert all(not d["online"] for d in offline)
    assert len(online) + len(offline) == len(all_)
    assert {"id", "name", "ipv4", "macs", "service_count", "trust"} <= set(online[0])
    page = client.get("/api/v1/devices", params={"limit": 2, "offset": 1}).json()
    assert [d["id"] for d in page] == [d["id"] for d in all_[1:3]]


def test_get_device_and_unknown_404(client: TestClient) -> None:
    d = _device_with_ip(client)
    r = client.get(f"/api/v1/devices/{d['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == d["id"] and isinstance(body["interfaces"], list)
    assert "scan_policy" in body and "field_sources" in body


def test_get_unknown_404(client: TestClient) -> None:
    r = client.get("/api/v1/devices/999999")
    assert r.status_code == 404
    assert r.json() == {"detail": "no device with id 999999"}


def test_patch_device(client: TestClient) -> None:
    d = _device_with_ip(client)
    r = client.patch(f"/api/v1/devices/{d['id']}", json={"fields": {"name": "Hall NAS"}})
    assert r.status_code == 200
    assert r.json()["batch_id"] > 0
    view = client.get(f"/api/v1/devices/{d['id']}").json()
    assert view["name"] == "Hall NAS"
    assert view["field_sources"]["name"][0] == "user"


def test_patch_bad_field_422(client: TestClient) -> None:
    d = _device_with_ip(client)
    r = client.patch(f"/api/v1/devices/{d['id']}", json={"fields": {"online": True}})
    assert r.status_code == 422
    assert "online" in r.json()["detail"]
    r = client.patch(f"/api/v1/devices/{d['id']}", json={"fields": {"trust": "maybe"}})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert client.patch("/api/v1/devices/999999", json={"fields": {}}).status_code == 404


def test_set_place_and_claim(client: TestClient) -> None:
    d = _device_with_ip(client)
    r = client.put(f"/api/v1/devices/{d['id']}/place", json={"name": "Office"})
    assert r.status_code == 200 and r.json()["batch_id"] > 0
    assert client.get(f"/api/v1/devices/{d['id']}").json()["place"] == "Office"
    assert client.put(f"/api/v1/devices/{d['id']}/place", json={"name": " "}).status_code == 422

    devices = client.get("/api/v1/devices", params={"q": "office-laptop"}).json()
    assert len(devices) == 2
    a, b = devices
    iface = client.get(f"/api/v1/devices/{b['id']}").json()["interfaces"][0]["id"]
    r = client.post(f"/api/v1/interfaces/{iface}/claim", json={"device_id": a["id"]})
    assert r.status_code == 200 and r.json()["batch_id"] > 0
    assert client.get(f"/api/v1/devices/{b['id']}").status_code == 404
    r = client.post("/api/v1/interfaces/999999/claim", json={"device_id": a["id"]})
    assert r.status_code == 404


def test_placeholder_device(client: TestClient) -> None:
    r = client.post("/api/v1/devices/placeholder", json={"name": "Desk switch", "type": "switch"})
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] > 0
    view = client.get(f"/api/v1/devices/{body['device_id']}").json()
    assert view["name"] == "Desk switch" and view["type"] == "switch"
    r = client.post("/api/v1/devices/placeholder", json={"name": "", "type": "switch"})
    assert r.status_code == 422


# ------------------------------------------------------- services / changes


def test_services_and_changes(client: TestClient) -> None:
    svcs = client.get("/api/v1/services").json()
    assert svcs
    dev = svcs[0]["device_id"]
    mine = client.get("/api/v1/services", params={"device_id": dev}).json()
    assert mine and all(s["device_id"] == dev for s in mine)
    port = svcs[0]["port"]
    assert all(s["port"] == port for s in client.get(f"/api/v1/services?port={port}").json())

    d = _device_with_ip(client)
    client.patch(f"/api/v1/devices/{d['id']}", json={"fields": {"owner": "Kim"}})
    changes = client.get("/api/v1/changes", params={"actor": "user", "limit": 5}).json()
    assert changes and all(c["actor"] == "user" for c in changes)
    assert changes[0]["new_value"] == "Kim"
    assert len(client.get("/api/v1/changes", params={"limit": 1}).json()) == 1


def test_undo(client: TestClient) -> None:
    d = _device_with_ip(client)
    old = d["name"]
    b = client.patch(f"/api/v1/devices/{d['id']}", json={"fields": {"name": "X"}}).json()
    r = client.post(f"/api/v1/changes/{b['batch_id']}/undo")
    assert r.status_code == 200 and r.json()["batch_id"] > b["batch_id"]
    assert client.get(f"/api/v1/devices/{d['id']}").json()["name"] == old
    again = client.post(f"/api/v1/changes/{b['batch_id']}/undo")
    assert again.status_code == 422 and again.json() == {"detail": "already undone"}
    assert client.post("/api/v1/changes/999999/undo").status_code == 404


def test_undo_conflict_409_payload(client: TestClient) -> None:
    d = _device_with_ip(client)
    b1 = client.patch(f"/api/v1/devices/{d['id']}", json={"fields": {"name": "A"}}).json()
    b2 = client.patch(f"/api/v1/devices/{d['id']}", json={"fields": {"name": "B"}}).json()
    r = client.post(f"/api/v1/changes/{b1['batch_id']}/undo")
    assert r.status_code == 409
    assert r.json() == {"detail": "conflict", "conflicting": [b2["batch_id"]]}


# ----------------------------------------------------------------- deep scan


class FakeRunner:
    def __init__(self, port: int = 8443) -> None:
        self.port = port
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], timeout_s: float) -> str:
        self.calls.append(argv)
        ip = argv[-1]
        return (
            '<?xml version="1.0"?><nmaprun><host><status state="up"/>'
            f'<address addr="{ip}" addrtype="ipv4"/><ports>'
            f'<port protocol="tcp" portid="{self.port}"><state state="open"/>'
            '<service name="https" product="Fake httpd"/></port></ports></host></nmaprun>'
        )


def test_deep_scan_never_409(client: TestClient, app: FastAPI) -> None:
    runner = FakeRunner()
    app.state.deep_scan_runner = runner
    d = _device_with_ip(client)
    client.patch(f"/api/v1/devices/{d['id']}", json={"fields": {"scan_policy": "never"}})
    r = client.post(f"/api/v1/devices/{d['id']}/deep-scan")
    assert r.status_code == 409
    assert r.json() == {"detail": "scan_policy=never"}
    assert runner.calls == []
    assert client.post("/api/v1/devices/999999/deep-scan").status_code == 404


def test_deep_scan_202_then_ingests(client: TestClient, app: FastAPI) -> None:
    runner = FakeRunner(port=8443)
    app.state.deep_scan_runner = runner
    d = _device_with_ip(client)
    r = client.post(f"/api/v1/devices/{d['id']}/deep-scan")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    deadline = time.monotonic() + 5
    ports: set[int] = set()
    while time.monotonic() < deadline:
        ports = {s["port"] for s in client.get(f"/api/v1/services?device_id={d['id']}").json()}
        if 8443 in ports:
            break
        time.sleep(0.02)
    assert 8443 in ports
    assert runner.calls and runner.calls[0][-1] == d["ipv4"]


# ---------------------------------------------------------------------- auth


def test_token_required_when_configured(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _make_app(tmp_path, db, clock, token=TOKEN)

    # Routes registered after create_app are covered too.
    @app.get("/api/v1/late")
    def late() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/mcp/late")
    def mcp_late() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/page")
    def page() -> dict[str, str]:
        return {"ok": "yes"}

    good = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as c:
        for path in ("/api/v1/devices", "/api/v1/late", "/mcp/late", "/api/v1/devices/999999"):
            assert c.get(path).status_code == 401, path
            assert c.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
            assert c.get(path, headers={"Authorization": TOKEN}).status_code == 401
            assert c.get(path, headers={"Authorization": "Bearer "}).status_code == 401
        assert c.get("/api/v1/devices", headers=good).status_code == 200
        assert c.get("/api/v1/late", headers=good).status_code == 200
        assert c.get("/mcp/late", headers=good).status_code == 200
        assert c.get("/api/v1/devices/999999", headers=good).status_code == 404
        assert c.get("/page").status_code == 200
        r = c.get("/api/v1/devices")
        assert r.json() == {"detail": "unauthorized"}
        assert TOKEN not in r.text


def test_no_token_configured_is_open(client: TestClient) -> None:
    assert client.get("/api/v1/devices").status_code == 200


# ------------------------------------------------------------------ links


def test_links_add_remove(client: TestClient) -> None:
    devices = client.get("/api/v1/devices", params={"limit": 2}).json()
    a, b = devices[0]["id"], devices[1]["id"]
    r = client.post("/api/v1/links", json={"a_device_id": a, "b_device_id": b, "kind": "wired"})
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] > 0 and body["link_id"] > 0
    edges = client.get("/api/v1/topology").json()["edges"]
    mine = [e for e in edges if e["id"] == body["link_id"]]
    assert mine == [
        {"id": body["link_id"], "source": a, "target": b, "kind": "wired", "manual": True}
    ]
    bad = client.post("/api/v1/links", json={"a_device_id": a, "b_device_id": a, "kind": "wired"})
    assert bad.status_code == 422
    r = client.delete(f"/api/v1/links/{body['link_id']}")
    assert r.status_code == 200 and r.json()["batch_id"] > body["batch_id"]
    assert client.delete(f"/api/v1/links/{body['link_id']}").status_code == 404


# --------------------------------------------------------------- topology


def test_topology_shape(client: TestClient) -> None:
    topo = client.get("/api/v1/topology").json()
    assert set(topo) == {"nodes", "edges", "places"}
    devices = client.get("/api/v1/devices", params={"limit": 1000}).json()
    assert {n["id"] for n in topo["nodes"]} == {d["id"] for d in devices}
    for n in topo["nodes"]:
        assert set(n) == {
            "id",
            "name",
            "type",
            "icon",
            "online",
            "placeholder",
            "place_id",
            "x",
            "y",
        }
        assert n["x"] is None and n["y"] is None
    # Derived AP edges exist for wifi clients of the mesh nodes.
    ap_edges = [e for e in topo["edges"] if str(e["id"]).startswith("ap-")]
    assert ap_edges
    ids = {n["id"] for n in topo["nodes"]}
    for e in ap_edges:
        assert set(e) == {"id", "source", "target", "kind", "manual"}
        assert e["manual"] is False and e["kind"] in {"wifi", "wired"}
        assert e["source"] in ids and e["target"] in ids and e["source"] != e["target"]
    assert any(e["kind"] == "wifi" for e in ap_edges)


def test_layout_round_trip(client: TestClient) -> None:
    devices = client.get("/api/v1/devices", params={"limit": 2}).json()
    a, b = devices[0]["id"], devices[1]["id"]
    r = client.put(
        "/api/v1/topology/layout", json={"positions": {str(a): [10, 20.5], str(b): [-3, 4]}}
    )
    assert r.status_code == 200
    r = client.put("/api/v1/topology/layout", json={"positions": {str(a): [11, 21]}})
    assert r.status_code == 200
    nodes = {n["id"]: n for n in client.get("/api/v1/topology").json()["nodes"]}
    assert (nodes[a]["x"], nodes[a]["y"]) == (11, 21)
    assert (nodes[b]["x"], nodes[b]["y"]) == (-3, 4)
    # Layout is a preference, not a logged change.
    assert all(c["entity"] != "topology_layout" for c in client.get("/api/v1/changes").json())
    bad = client.put("/api/v1/topology/layout", json={"positions": {"999999": [1, 2]}})
    assert bad.status_code == 404
    assert client.put("/api/v1/topology/layout", json={"positions": {"x": [1]}}).status_code == 422


# -------------------------------------------------------------- collectors


def test_collectors_status(client: TestClient, db: sqlite3.Connection) -> None:
    assert client.get("/api/v1/collectors").json() == []
    with db:
        db.execute(
            "INSERT INTO collector_status(name, last_error, consecutive_failures)"
            " VALUES ('arp', 'boom ***', 2)"
        )
    rows = client.get("/api/v1/collectors").json()
    assert rows == [
        {
            "name": "arp",
            "last_ok": None,
            "last_error": "boom ***",
            "last_error_at": None,
            "consecutive_failures": 2,
            "next_run_at": None,
        }
    ]


# --------------------------------------------------------------- lifecycle


def test_create_app_opens_own_db(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, allowed_hosts=TEST_HOSTS), start_background=False)
    with TestClient(app) as c:
        assert c.get("/api/v1/devices").json() == []
    assert (tmp_path / "atlas.db").exists()
    with pytest.raises(sqlite3.ProgrammingError):
        app.state.conn.execute("SELECT 1")


def test_lifespan_starts_and_stops_scheduler(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lanscape_atlas.web import app as app_mod

    calls: list[str] = []
    passed: list[Any] = []
    sentinel = object()

    async def fake_start(
        settings: Any, runner: Any, collectors: Any, service: Any, bluetooth: Any = None
    ) -> object:
        passed.append(bluetooth)
        calls.append("start")
        return sentinel

    async def fake_stop(
        sched: Any, collectors: Any, runner: Any = None, bluetooth: Any = None
    ) -> None:
        assert sched is sentinel and runner is not None
        passed.append(bluetooth)
        calls.append("stop")

    monkeypatch.setattr(app_mod, "start_scheduler", fake_start)
    monkeypatch.setattr(app_mod, "stop_scheduler", fake_stop)
    app = create_app(Settings(data_dir=tmp_path), conn=db, clock=clock)
    with TestClient(app):
        assert calls == ["start"]
    assert calls == ["start", "stop"]
    assert app.state.bluetooth is None and passed == [None, None]
    db.execute("SELECT 1")  # a caller-supplied connection is left open

    calls.clear()
    app = create_app(Settings(data_dir=tmp_path), conn=db, clock=clock, start_background=False)
    with TestClient(app):
        pass
    assert calls == []

    # With [home_assistant] the runtime is passed to both start and stop.
    from lanscape_atlas.config import HomeAssistantSettings

    calls.clear()
    passed.clear()
    ha = HomeAssistantSettings(url="http://192.0.2.10:8123", token="t")
    app = create_app(Settings(data_dir=tmp_path, home_assistant=ha), conn=db, clock=clock)
    assert app.state.bluetooth is not None
    with TestClient(app):
        pass
    assert calls == ["start", "stop"]
    assert passed == [app.state.bluetooth, app.state.bluetooth]


def test_shutdown_cancels_pending_deep_scans(app: FastAPI) -> None:
    import asyncio

    started: list[str] = []

    async def hanging(argv: list[str], timeout_s: float) -> str:
        started.append(argv[-1])
        await asyncio.Event().wait()
        return ""

    app.state.deep_scan_runner = hanging
    with TestClient(app) as c:
        d = _device_with_ip(c)
        assert c.post(f"/api/v1/devices/{d['id']}/deep-scan").status_code == 202
        tasks = set(app.state.deep_scan_tasks.values())
        assert len(tasks) == 1
        deadline = time.monotonic() + 5
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started
    (task,) = tasks
    assert task.cancelled()
    assert not app.state.deep_scan_tasks


# ------------------------------------------------------------ fix round 1


def test_token_required_under_root_path(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _make_app(tmp_path, db, clock, token=TOKEN)

    @app.get("/mcp/late")
    def mcp_late() -> dict[str, str]:
        return {"ok": "yes"}

    good = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app, root_path="/atlas") as c:
        for path in ("/atlas/api/v1/devices", "/atlas/mcp/late"):
            assert c.get(path).status_code == 401, path
            assert c.get(path, headers=good).status_code == 200, path


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/devices/{big}"),
        ("patch", "/api/v1/devices/{big}"),
        ("post", "/api/v1/devices/{big}/deep-scan"),
        ("post", "/api/v1/changes/{big}/undo"),
        ("delete", "/api/v1/links/{big}"),
        ("post", "/api/v1/interfaces/{big}/claim"),
        ("put", "/api/v1/devices/{big}/place"),
        ("get", "/api/v1/devices/0"),
    ],
)
def test_oversized_path_ids_422(client: TestClient, method: str, path: str) -> None:
    url = path.format(big=2**63)
    body: dict[str, Any] = {"fields": {}, "device_id": 1, "name": "x"}
    kwargs: dict[str, Any] = {} if method in ("get", "delete") else {"json": body}
    r = client.request(method.upper(), url, **kwargs)
    assert r.status_code == 422, (url, r.text)


def test_oversized_body_ids_422(client: TestClient) -> None:
    big = 2**63
    r = client.post("/api/v1/links", json={"a_device_id": big, "b_device_id": 1, "kind": "wired"})
    assert r.status_code == 422
    r = client.post("/api/v1/interfaces/1/claim", json={"device_id": big})
    assert r.status_code == 422
    r = client.put("/api/v1/topology/layout", json={"positions": {str(big): [1, 2]}})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_layout_rejects_non_finite(client: TestClient, bad: str) -> None:
    d = client.get("/api/v1/devices", params={"limit": 1}).json()[0]["id"]
    raw = f'{{"positions": {{"{d}": [{bad}, 1]}}}}'
    r = client.put(
        "/api/v1/topology/layout", content=raw, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422
    nodes = {n["id"]: n for n in client.get("/api/v1/topology").json()["nodes"]}
    assert nodes[d]["x"] is None


def test_one_deep_scan_per_device(app: FastAPI) -> None:
    import asyncio

    release: list[asyncio.Event] = []
    started: list[str] = []

    async def blocking(argv: list[str], timeout_s: float) -> str:
        ev = asyncio.Event()
        release.append(ev)
        started.append(argv[-1])
        await ev.wait()
        return "<nmaprun/>"

    app.state.deep_scan_runner = blocking
    with TestClient(app) as c:
        d = _device_with_ip(c)
        url = f"/api/v1/devices/{d['id']}/deep-scan"
        assert c.post(url).status_code == 202
        r = c.post(url)
        assert r.status_code == 409
        assert r.json() == {"detail": "deep scan already running"}
        assert d["id"] in app.state.deep_scan_tasks
        deadline = time.monotonic() + 5
        while not release and time.monotonic() < deadline:
            time.sleep(0.01)
        assert release
        task = app.state.deep_scan_tasks[d["id"]]
        c.portal.call(lambda: release[0].set())  # type: ignore[union-attr]
        while not task.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert task.done()
        while d["id"] in app.state.deep_scan_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert d["id"] not in app.state.deep_scan_tasks
        # Finished: a new scan may start.
        assert c.post(url).status_code == 202
    assert len(started) == 2


def test_openapi_under_api_and_protected(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _make_app(tmp_path, db, clock, token=TOKEN)
    good = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as c:
        assert c.get("/api/openapi.json").status_code == 401
        r = c.get("/api/openapi.json", headers=good)
        assert r.status_code == 200 and "/api/v1/devices" in r.json()["paths"]
        assert c.get("/api/docs").status_code == 401
        assert c.get("/api/docs", headers=good).status_code == 200
        for gone in ("/openapi.json", "/docs", "/redoc"):
            assert c.get(gone).status_code == 404, gone


# ------------------------------------------------------------- Ruling 44


def _mover(db: sqlite3.Connection) -> int:
    """A device whose interface moved 192.0.2.80 -> 192.0.2.81 an hour apart."""
    with db:
        cur = db.execute(
            "insert into device(trust, scan_policy, online, first_seen, last_seen)"
            " values ('known', 'normal', 1, ?, ?)",
            ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        )
        dev = cur.lastrowid
        cur = db.execute(
            "insert into interface(device_id, kind, address, first_seen, last_seen)"
            " values (?, 'ethernet', '00:00:5e:00:53:80', ?, ?)",
            (dev, "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        )
        for ip, seen in (("192.0.2.80", "00:00"), ("192.0.2.81", "01:00")):
            db.execute(
                "insert into ip_lease(interface_id, ipv4, first_seen, last_seen, closed)"
                " values (?, ?, ?, ?, 0)",
                (cur.lastrowid, ip, f"2026-01-01T{seen}:00Z", f"2026-01-01T{seen}:00Z"),
            )
    assert dev is not None
    return dev


@pytest.mark.parametrize(("window", "found"), [(10, False), (120, True)])
def test_configured_ip_match_window_applies(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock, window: int, found: bool
) -> None:
    settings = Settings(data_dir=tmp_path, allowed_hosts=TEST_HOSTS, ip_match_window_min=window)
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    dev = _mover(db)
    with TestClient(app) as c:
        rows = c.get("/api/v1/devices", params={"q": "192.0.2.80"}).json()
        assert ({r["id"] for r in rows} == {dev}) is found
        assert c.get(f"/api/v1/devices/{dev}").json()["ipv4"] == "192.0.2.81"
    assert app.state.service.ip_match_window_min == window
