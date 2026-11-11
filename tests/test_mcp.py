"""MCP server (spec section 8.3): tools in-process via the SDK client, plus HTTP at /mcp."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.types import CallToolResult
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from conftest import FixedClock
from lanscape_atlas.config import RouterSettings, Settings
from lanscape_atlas.mcp_server import TOOL_NAMES, build_mcp
from lanscape_atlas.observation import Observation
from lanscape_atlas.web.app import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "fake_house.json"
SECRET = "s3cret-test"
TOKEN = "test-api-token-123"

EXPECTED = {
    "find_device",
    "list_devices",
    "get_device",
    "list_services",
    "update_device",
    "claim_interface",
    "set_place",
    "run_deep_scan",
    "recent_changes",
    "undo",
    "get_floor_plan",
    "place_devices",
    "where_is",
    "ble_candidates",
    "claim_ble",
}


def _observations() -> list[Observation]:
    raw = json.loads(FIXTURE.read_text())
    return [Observation.from_json(json.dumps(d)) for d in raw]


def _make_app(
    tmp_path: Path,
    db: sqlite3.Connection,
    clock: FixedClock,
    *,
    token: str | None = None,
    router_password: str | None = None,
    allowed_hosts: tuple[str, ...] = (),
) -> FastAPI:
    router = (
        RouterSettings(host="192.0.2.1", username="admin", password=router_password)
        if router_password
        else None
    )
    settings = Settings(
        data_dir=tmp_path, api_token=token, router=router, allowed_hosts=allowed_hosts
    )
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    assert app.state.ingestor.ingest(_observations()).dropped == 0
    return app


@pytest.fixture
def app(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> FastAPI:
    return _make_app(tmp_path, db, clock)


@asynccontextmanager
async def _session(app: FastAPI) -> AsyncIterator[Client]:
    # Not an async fixture: the SDK client's task group must enter and exit in one task.
    try:
        async with Client(app.state.mcp) as c:
            yield c
    finally:
        await _cancel_scans(app)


async def _cancel_scans(app: FastAPI) -> None:
    tasks = list(app.state.deep_scan_tasks.values())
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _text(r: CallToolResult) -> str:
    return "".join(getattr(c, "text", "") for c in r.content)


def _data(r: CallToolResult) -> Any:
    assert not r.is_error, _text(r)
    return json.loads(_text(r)) if r.structured_content is None else _unwrap(r)


def _unwrap(r: CallToolResult) -> Any:
    sc = r.structured_content
    assert sc is not None
    if set(sc) == {"result"}:
        return sc["result"]
    return sc


async def _call(c: Client, name: str, **args: Any) -> CallToolResult:
    return await c.call_tool(name, args)


async def _device_with_ip(c: Client) -> dict[str, Any]:
    devices = _data(await _call(c, "list_devices", online=True, limit=200))
    found: dict[str, Any] = next(d for d in devices if d["ipv4"])
    return found


# -------------------------------------------------------------- brief tests


async def test_tool_names_exact(app: FastAPI) -> None:
    async with _session(app) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED
        assert len(tools) == 15
        assert set(TOOL_NAMES) == EXPECTED
        for t in tools:
            assert t.description and len(t.description) > 20, t.name


async def test_find_device_by_ip(app: FastAPI) -> None:
    async with _session(app) as client:
        d = await _device_with_ip(client)
        found = _data(await _call(client, "find_device", query=d["ipv4"]))
        assert [x["id"] for x in found] == [d["id"]]
        mac = d["macs"][0]
        assert d["id"] in {x["id"] for x in _data(await _call(client, "find_device", query=mac))}
        assert _data(await _call(client, "find_device", query="no-such-thing-xyz")) == []


async def test_update_device_records_claude_actor(app: FastAPI) -> None:
    async with _session(app) as client:
        d = await _device_with_ip(client)
        r = _data(
            await _call(client, "update_device", device_id=d["id"], fields={"name": "Hall NAS"})
        )
        assert r["batch_id"] > 0
        view = _data(await _call(client, "get_device", device_id=d["id"]))
        assert view["name"] == "Hall NAS"
        assert view["field_sources"]["name"][0] == "claude"
        changes = _data(await _call(client, "recent_changes", actor="claude"))
        assert changes and all(c["actor"] == "claude" for c in changes)
        assert changes[0]["batch_id"] == r["batch_id"] and changes[0]["new_value"] == "Hall NAS"


async def test_run_deep_scan_never_errors(app: FastAPI) -> None:
    async with _session(app) as client:
        calls: list[list[str]] = []

        async def runner(argv: list[str], timeout_s: float) -> str:
            calls.append(argv)
            return "<nmaprun/>"

        app.state.deep_scan_runner = runner
        d = await _device_with_ip(client)
        await _call(client, "update_device", device_id=d["id"], fields={"scan_policy": "never"})
        r = await _call(client, "run_deep_scan", device_id=d["id"])
        assert r.is_error
        assert _text(r) == "scan refused: scan_policy=never"
        assert calls == [] and d["id"] not in app.state.deep_scan_tasks
        r = await _call(client, "run_deep_scan", device_id=999999)
        assert r.is_error and _text(r) == "device 999999 not found"


async def test_undo_conflict_message(app: FastAPI) -> None:
    async with _session(app) as client:
        d = await _device_with_ip(client)
        b1 = _data(await _call(client, "update_device", device_id=d["id"], fields={"name": "A"}))
        b2 = _data(await _call(client, "update_device", device_id=d["id"], fields={"name": "B"}))
        r = await _call(client, "undo", batch_id=b1["batch_id"])
        assert r.is_error
        assert _text(r) == f"undo conflict with batches [{b2['batch_id']}]"


async def test_no_secrets_in_any_tool_output(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _make_app(tmp_path, db, clock, token=SECRET, router_password=SECRET)
    assert app.state.settings.router.password == SECRET

    async def runner(argv: list[str], timeout_s: float) -> str:
        await asyncio.Event().wait()
        return ""

    app.state.deep_scan_runner = runner
    outputs: list[str] = []
    async with Client(app.state.mcp) as c:

        async def call(name: str, **args: Any) -> CallToolResult:
            r = await _call(c, name, **args)
            outputs.append(r.model_dump_json())
            return r

        d = await _device_with_ip(c)
        devices = _data(await call("list_devices", limit=200))
        other = next(x for x in devices if x["id"] != d["id"])
        view = _data(await call("get_device", device_id=d["id"]))
        await call("find_device", query=d["ipv4"])
        await call("find_device", query=SECRET)
        await call("list_services")
        await call("list_services", device_id=d["id"])
        await call("recent_changes", limit=200)
        b = _data(await call("update_device", device_id=d["id"], fields={"notes": "n"}))
        await call("set_place", device_id=d["id"], place_name="Office")
        await call("run_deep_scan", device_id=d["id"])
        await call("run_deep_scan", device_id=d["id"])  # already running
        iface = view["interfaces"][0]["id"]
        await call("claim_interface", interface_id=iface, device_id=other["id"])
        await call("undo", batch_id=b["batch_id"])
        await call("recent_changes", limit=200)
        await call("get_device", device_id=999999)
    await _cancel_scans(app)
    assert len(outputs) == 15
    for out in outputs:
        assert SECRET not in out


# ------------------------------------------------------------- extra tests


async def test_claude_write_then_undo(app: FastAPI) -> None:
    async with _session(app) as client:
        d = await _device_with_ip(client)
        old = d["name"]
        b = _data(await _call(client, "set_place", device_id=d["id"], place_name="Garage"))
        r = _data(await _call(client, "update_device", device_id=d["id"], fields={"name": "X"}))
        u = _data(await _call(client, "undo", batch_id=r["batch_id"]))
        assert u["batch_id"] > r["batch_id"]
        view = _data(await _call(client, "get_device", device_id=d["id"]))
        assert view["name"] == old and view["place"] == "Garage"
        changes = _data(await _call(client, "recent_changes", limit=5))
        assert changes[0]["batch_id"] == u["batch_id"] and changes[0]["actor"] == "claude"
        assert b["batch_id"] < r["batch_id"]
        again = await _call(client, "undo", batch_id=r["batch_id"])
        assert again.is_error and _text(again) == "already undone"
        missing = await _call(client, "undo", batch_id=999999)
        assert missing.is_error and _text(missing) == "batch 999999 not found"


async def test_deep_scan_started_then_already_running(app: FastAPI) -> None:
    async with _session(app) as client:
        started = asyncio.Event()

        async def runner(argv: list[str], timeout_s: float) -> str:
            started.set()
            await asyncio.Event().wait()
            return ""

        app.state.deep_scan_runner = runner
        d = await _device_with_ip(client)
        r = await _call(client, "run_deep_scan", device_id=d["id"])
        assert _data(r) == {"status": "started"}
        assert d["id"] in app.state.deep_scan_tasks
        await asyncio.wait_for(started.wait(), 5)
        again = await _call(client, "run_deep_scan", device_id=d["id"])
        assert again.is_error and _text(again) == "scan refused: deep scan already running"


async def test_errors_are_plain(app: FastAPI) -> None:
    async with _session(app) as client:
        r = await _call(client, "get_device", device_id=42424242)
        assert r.is_error and _text(r) == "device 42424242 not found"
        d = await _device_with_ip(client)
        r = await _call(client, "update_device", device_id=d["id"], fields={"trust": "maybe"})
        assert r.is_error and "trust must be one of" in _text(r)
        assert "Traceback" not in _text(r) and "Error executing" not in _text(r)
        r = await _call(client, "claim_interface", interface_id=999999, device_id=d["id"])
        assert r.is_error and _text(r) == "interface 999999 not found"
        r = await _call(client, "set_place", device_id=d["id"], place_name=" ")
        assert r.is_error and _text(r) == "place_name must be a non-empty string"
        r = await _call(client, "get_device", device_id=2**63)
        assert r.is_error and _text(r) == "integer out of range"


async def test_list_devices_filters_and_caps(app: FastAPI) -> None:
    async with _session(app) as client:
        all_ = _data(await _call(client, "list_devices", limit=1000))
        assert 40 <= len(all_) <= 200
        page = _data(await _call(client, "list_devices", limit=2, offset=1))
        assert [x["id"] for x in page] == [x["id"] for x in all_[1:3]]
        default = _data(await _call(client, "list_devices"))
        assert len(default) == min(50, len(all_))
        laptops = _data(await _call(client, "list_devices", q="office-laptop"))
        assert len(laptops) == 2
        assert len(_data(await _call(client, "recent_changes", limit=10_000))) <= 200


async def test_list_services_by_port(app: FastAPI) -> None:
    async with _session(app) as client:
        services = _data(await _call(client, "list_services"))
        assert services
        port = services[0]["port"]
        by_port = _data(await _call(client, "list_services", port=port))
        assert by_port and all(s["port"] == port for s in by_port)


async def test_claim_interface_via_mcp(app: FastAPI) -> None:
    async with _session(app) as client:
        a, b = _data(await _call(client, "list_devices", q="office-laptop"))
        iface = _data(await _call(client, "get_device", device_id=b["id"]))["interfaces"][0]["id"]
        r = _data(await _call(client, "claim_interface", interface_id=iface, device_id=a["id"]))
        assert r["batch_id"] > 0
        gone = await _call(client, "get_device", device_id=b["id"])
        assert gone.is_error and _text(gone) == f"device {b['id']} not found"
        u = _data(await _call(client, "undo", batch_id=r["batch_id"]))
        assert u["batch_id"] > r["batch_id"]
        assert _data(await _call(client, "get_device", device_id=b["id"]))["id"] == b["id"]


def test_build_mcp_standalone(app: FastAPI) -> None:
    async def scanner(device_id: int) -> None:
        return None

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    try:
        server = build_mcp(app.state.service, scanner)
        # The SDK's logging.basicConfig side effect is undone.
        assert root.handlers == [] and root.level == logging.WARNING
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == EXPECTED


# ------------------------------------------------------------------- HTTP

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _rpc(method: str, params: dict[str, Any], id_: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


def _init() -> dict[str, Any]:
    return _rpc(
        "initialize",
        {
            "protocolVersion": LATEST_HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
        1,
    )


def _post(c: TestClient, path: str, body: dict[str, Any], **headers: str) -> Any:
    h = dict(MCP_HEADERS)
    h.update(headers)
    sid = h.pop("sid", None)
    if sid:
        h["mcp-session-id"] = sid
    h.setdefault("mcp-protocol-version", LATEST_HANDSHAKE_VERSION)
    return c.post(path, json=body, headers=h, follow_redirects=False)


def _json_body(r: Any) -> dict[str, Any]:
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                data: dict[str, Any] = json.loads(line[5:])
                return data
        raise AssertionError(r.text)
    out: dict[str, Any] = r.json()
    return out


@pytest.fixture
def http_app(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> FastAPI:
    return _make_app(tmp_path, db, clock, allowed_hosts=("testserver",))


@pytest.fixture
def http(http_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(http_app) as c:
        yield c


def _round_trip(c: TestClient, path: str, **headers: str) -> None:
    r = _post(c, path, _init(), **headers)
    assert r.status_code == 200, r.text
    init = _json_body(r)
    assert init["result"]["serverInfo"]["name"] == "lanscape-atlas"
    sid = r.headers.get("mcp-session-id")
    extra = dict(headers)
    if sid:
        extra["sid"] = sid
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert _post(c, path, note, **extra).status_code in (200, 202)
    r = _post(c, path, _rpc("tools/list", {}, 2), **extra)
    assert r.status_code == 200, r.text
    names = {t["name"] for t in _json_body(r)["result"]["tools"]}
    assert names == EXPECTED
    ip = c.get("/api/v1/devices", params={"online": "true"}, headers=headers).json()
    ip = next(d for d in ip if d["ipv4"])
    r = _post(
        c,
        path,
        _rpc("tools/call", {"name": "find_device", "arguments": {"query": ip["ipv4"]}}, 3),
        **extra,
    )
    assert r.status_code == 200, r.text
    result = _json_body(r)["result"]
    assert not result.get("isError")
    assert ip["id"] in {d["id"] for d in result["structuredContent"]["result"]}


def test_http_round_trip(http: TestClient) -> None:
    _round_trip(http, "/mcp")


def test_http_trailing_slash(http: TestClient) -> None:
    # "/mcp/" is served directly too (no redirect a client might not follow for POST).
    _round_trip(http, "/mcp/")


def test_http_lifespan_restarts(http_app: FastAPI) -> None:
    for _ in range(2):
        with TestClient(http_app) as c:
            assert _post(c, "/mcp", _init()).status_code == 200


def test_http_requires_token(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> None:
    app = _make_app(tmp_path, db, clock, token=TOKEN)
    good = f"Bearer {TOKEN}"
    with TestClient(app) as c:
        for path in ("/mcp", "/mcp/"):
            r = _post(c, path, _init())
            assert r.status_code == 401 and TOKEN not in r.text
            assert _post(c, path, _init(), Authorization="Bearer nope").status_code == 401
        # With the token the Host check is off (the token already defeats DNS rebinding).
        _round_trip(c, "/mcp", Authorization=good)


def test_http_host_check_without_token(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    app = _make_app(tmp_path, db, clock)
    with TestClient(app) as c:
        assert _post(c, "/mcp", _init()).status_code == 421  # Host: testserver not allowed
        r = _post(c, "/mcp", _init(), Host="127.0.0.1:8720")
        assert r.status_code == 200
        r = _post(c, "/mcp", _init(), Host="localhost")
        assert r.status_code == 200
        r = _post(c, "/mcp", _init(), Host="localhost:8720", Origin="http://evil.example")
        assert r.status_code == 403
    app = _make_app(tmp_path, db, clock, allowed_hosts=("*",))
    with TestClient(app) as c:
        assert _post(c, "/mcp", _init()).status_code == 200


# ------------------------------------------------------------ floor plan tools


def _last_batch_actor(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT actor FROM batch ORDER BY id DESC LIMIT 1").fetchone()
    return str(row[0])


def _placement(rd: Any, x: float = 0.3, y: float = 0.3) -> dict[str, Any]:
    return {"device_id": rd.dev, "place_id": rd.room, "x": x, "y": y, "roaming": False}


async def test_floor_plan_tools_listed_with_metadata(mcp_client: Any) -> None:
    async with mcp_client() as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert {"get_floor_plan", "place_devices"} <= set(TOOL_NAMES)
    gfp, pd = tools["get_floor_plan"], tools["place_devices"]
    assert gfp.title and pd.title
    assert gfp.annotations is not None and gfp.annotations.read_only_hint is True
    assert pd.annotations is not None
    assert pd.annotations.read_only_hint is False and pd.annotations.destructive_hint is False


async def test_get_floor_plan_has_no_file_names(mcp_client: Any, plan_seeded: None) -> None:
    async with mcp_client() as c:
        out = _data(await _call(c, "get_floor_plan"))
    text = json.dumps(out)
    assert "file_name" not in text and "sha256" not in text and "plan.png" not in text
    assert set(out["plan"]) == {"width", "height", "updated_at"}
    assert (out["plan"]["width"], out["plan"]["height"]) == (1600, 1200)


async def test_get_floor_plan_without_plan(mcp_client: Any) -> None:
    async with mcp_client() as c:
        out = _data(await _call(c, "get_floor_plan"))
    assert out["plan"] is None
    assert set(out) >= {"rooms", "placed", "roaming", "unplaced"}


async def test_place_devices_actor_is_claude_and_undoable(
    mcp_client: Any, room_and_device: Any
) -> None:
    rd = room_and_device
    async with mcp_client() as c:
        out = _data(
            await _call(
                c,
                "place_devices",
                placements=[_placement(rd)],
                based_on={str(rd.dev): 0},
            )
        )
        assert out["batch_id"] > 0
        assert _last_batch_actor(rd.db) == "claude"
        view = _data(await _call(c, "get_floor_plan"))
        assert [d["device_id"] for d in view["placed"]] == [rd.dev]
        _data(await _call(c, "undo", batch_id=out["batch_id"]))
        view = _data(await _call(c, "get_floor_plan"))
        assert view["placed"] == []


async def test_place_devices_stale_names_ids(mcp_client: Any, stale_setup: Any) -> None:
    body = stale_setup.body
    dev = body["placements"][0]["device_id"]
    async with mcp_client() as c:
        r = await _call(c, "place_devices", **body)
    assert r.is_error
    assert _text(r) == (
        f"placement changed since it was read for devices [{dev}]; "
        "call get_floor_plan and retry with the new last_batch values"
    )


async def test_place_devices_outside_room_writes_nothing(
    mcp_client: Any, room_and_device: Any
) -> None:
    rd = room_and_device
    before = rd.db.execute("SELECT count(*) FROM change").fetchone()[0]
    async with mcp_client() as c:
        r = await _call(
            c,
            "place_devices",
            placements=[_placement(rd, 1.5, 0.3)],
            based_on={str(rd.dev): 0},
        )
    assert r.is_error
    assert rd.db.execute("SELECT count(*) FROM change").fetchone()[0] == before


@pytest.mark.parametrize(
    ("placements", "based_on"),
    [
        ([{"device_id": 1, "place_id": None}], {"abc": 0}),
        ([{"device_id": "1", "place_id": None}], {}),
        ([{"device_id": 1, "place_id": None, "roaming": "yes"}], {}),
        ([{"device_id": 1, "place_id": None, "actor": "user"}], {}),
    ],
)
async def test_place_devices_strict_input(
    mcp_client: Any, placements: list[dict[str, Any]], based_on: dict[str, Any]
) -> None:
    async with mcp_client() as c:
        r = await _call(c, "place_devices", placements=placements, based_on=based_on)
    assert r.is_error
    if "abc" in based_on:
        assert _text(r) == "based_on keys must be device ids"


def test_instructions_and_docstring_cover_floor_plan_tools() -> None:
    from lanscape_atlas import mcp_server

    assert "get_floor_plan" in mcp_server.INSTRUCTIONS
    assert "place_devices" in mcp_server.INSTRUCTIONS
    src = Path(mcp_server.__file__).read_text()
    assert "may be omitted" in src and "counts as 0" in src


def test_error_message_undo_conflict_without_batches() -> None:
    from lanscape_atlas.mcp_server import error_message
    from lanscape_atlas.service import UndoConflict

    assert error_message(UndoConflict([7])) == "undo conflict with batches [7]"
    exc = UndoConflict([], "cannot undo: device 5 no longer exists")
    assert error_message(exc) == "undo conflict: cannot undo: device 5 no longer exists"


# -------------------------------------------------------------- bluetooth

BLE_ADDR = "00:00:5e:00:53:01"
BLE_KEY = f"address:{BLE_ADDR}"


def _with_candidate(app: FastAPI, clock: FixedClock, *, connected: bool = True) -> None:
    from test_ble_api import StubRuntime, candidate

    app.state.bluetooth = StubRuntime([candidate(clock)], connected)


def test_tool_names_include_ble() -> None:
    assert {"where_is", "ble_candidates", "claim_ble"} <= set(TOOL_NAMES)
    from lanscape_atlas.mcp_server import INSTRUCTIONS

    for name in ("where_is", "ble_candidates", "claim_ble"):
        assert name in INSTRUCTIONS
    assert "rotate" in INSTRUCTIONS


async def test_where_is(app: FastAPI, clock: FixedClock) -> None:
    from lanscape_atlas.bluetooth.aggregator import BLE_MINUTE_FMT, MinuteRow
    from lanscape_atlas.bluetooth.match import Identity

    svc = app.state.service
    dev = next(d for d in svc.conn.execute("select id from device limit 2"))["id"]
    other = next(d for d in svc.conn.execute("select id from device limit 1 offset 1"))["id"]
    svc.claim_ble("user", Identity("address", BLE_ADDR), device_id=dev)
    svc.claim_ble("user", Identity("address", "00:00:5e:00:53:02"), device_id=other)
    heard = clock() - timedelta(seconds=90)
    svc.record_ble_minutes(
        [
            MinuteRow(
                device_id=dev,
                source="00:00:5e:00:53:aa",
                minute=heard.strftime(BLE_MINUTE_FMT),
                max_rssi=-61,
                adverts=2,
                last_at=heard,
            )
        ]
    )
    async with _session(app) as c:
        rows = _data(await _call(c, "where_is", query=""))
        by_id = {r["device_id"]: r for r in rows}
        assert by_id[dev]["age_s"] == 90 and by_id[dev]["last"]["rssi"] == -61
        assert by_id[dev]["last"]["at"] == heard.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert by_id[other]["age_s"] is None and by_id[other]["last"] is None
        name = by_id[dev]["name"]
        if name:
            hit = _data(await _call(c, "where_is", query=name[:3].upper()))
            assert dev in {r["device_id"] for r in hit}
        assert _data(await _call(c, "where_is", query="no-such-device-zzz")) == []


async def test_ble_candidates(app: FastAPI, clock: FixedClock) -> None:
    async with _session(app) as c:
        assert _data(await _call(c, "ble_candidates")) == {
            "connected": None,
            "min_rssi": -80,
            "weak_hidden": 0,
            "candidates": [],
        }
        _with_candidate(app, clock)
        data = _data(await _call(c, "ble_candidates"))
        assert data["connected"] is True
        assert data["candidates"][0]["identity"] == BLE_KEY
        assert data["candidates"][0]["last_heard"] == "2026-01-01T00:00:00Z"


async def test_ble_candidates_weak_filter(app: FastAPI, clock: FixedClock) -> None:
    from test_ble_api import StubRuntime, candidate

    weak = "00:00:5e:00:53:02"
    app.state.bluetooth = StubRuntime([candidate(clock, weak, -90), candidate(clock)])
    async with _session(app) as c:
        data = _data(await _call(c, "ble_candidates"))
        assert [x["identity"] for x in data["candidates"]] == [BLE_KEY]
        assert data["weak_hidden"] == 1
        data = _data(await _call(c, "ble_candidates", include_weak=True))
        assert [x["identity"] for x in data["candidates"]] == [BLE_KEY, f"address:{weak}"]
        assert data["weak_hidden"] == 0


async def test_claim_ble_actor_is_claude(app: FastAPI, clock: FixedClock) -> None:
    _with_candidate(app, clock)
    async with _session(app) as c:
        r = _data(await _call(c, "claim_ble", candidate=BLE_KEY, new_name="Keys"))
        db = app.state.service.conn
        name, typ = db.execute(
            "select name, type from device where id = ?", (r["device_id"],)
        ).fetchone()
        assert (name, typ) == ("Keys", "other")
        actor = db.execute("select actor from batch where id = ?", (r["batch_id"],))
        assert actor.fetchone()[0] == "claude"
        # Already claimed -> no longer valid? Candidate list is stubbed, so the service says so.
        again = await _call(c, "claim_ble", candidate=BLE_KEY, new_name="Keys 2")
        assert again.is_error and "already claimed" in _text(again)


async def test_claim_ble_errors(app: FastAPI, clock: FixedClock) -> None:
    _with_candidate(app, clock)
    async with _session(app) as c:
        bad = await _call(c, "claim_ble", candidate="address:00:00:5e:00:53:09", new_name="x")
        assert bad.is_error and _text(bad) == "no such candidate"
        junk = await _call(c, "claim_ble", candidate="nonsense", new_name="x")
        assert junk.is_error
        both = await _call(c, "claim_ble", candidate=BLE_KEY)
        assert both.is_error and "exactly one" in _text(both)
        app.state.bluetooth = None
        gone = await _call(c, "claim_ble", candidate=BLE_KEY, new_name="x")
        assert gone.is_error and _text(gone) == "no such candidate"
