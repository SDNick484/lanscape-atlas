"""Bluetooth HTTP API: find, candidates, claim, unclaim, scanner links, auth."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import FixedClock
from lanscape_atlas.bluetooth.aggregator import BLE_MINUTE_FMT, Candidate, MinuteRow
from lanscape_atlas.bluetooth.match import Identity
from lanscape_atlas.collectors.bluetooth_ha import ClientStatus
from lanscape_atlas.config import Settings
from lanscape_atlas.service import UndoConflict
from lanscape_atlas.web.app import create_app

TOKEN = "test-api-token-123"
SCANNER = "00:00:5e:00:53:aa"
ADDR = "00:00:5e:00:53:01"
KEY = f"address:{ADDR}"


class StubAggregator:
    def __init__(self, items: list[Candidate]) -> None:
        self.items = items

    def candidates(self) -> list[Candidate]:
        return list(self.items)


class StubRuntime:
    def __init__(self, items: list[Candidate], connected: bool = True) -> None:
        self.aggregator = StubAggregator(items)
        self._connected = connected

    def status(self) -> ClientStatus:
        return ClientStatus(self._connected, None, None)


def candidate(clock: FixedClock, address: str = ADDR) -> Candidate:
    now = clock()
    return Candidate(
        Identity("address", address), "Tag", -55, SCANNER, now - timedelta(minutes=5), now, 7, 2.5
    )


@pytest.fixture
def app(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> FastAPI:
    settings = Settings(data_dir=tmp_path, allowed_hosts=("testserver",))
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    app.state.bluetooth = StubRuntime([candidate(clock)])
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _hear(app: FastAPI, clock: FixedClock, device_id: int) -> None:
    app.state.service.record_ble_minutes(
        [
            MinuteRow(
                device_id=device_id,
                source=SCANNER,
                minute=clock().strftime(BLE_MINUTE_FMT),
                max_rssi=-60,
                adverts=3,
                last_at=clock(),
            )
        ]
    )


def test_find_returns_claimed(
    client: TestClient, app: FastAPI, clock: FixedClock, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys", type="tag")
    other = make_device(name="Unclaimed")
    app.state.service.claim_ble("user", Identity("address", ADDR), device_id=d)
    _hear(app, clock, d)
    rows = client.get("/api/v1/find").json()
    assert [r["device_id"] for r in rows] == [d] and other not in {r["device_id"] for r in rows}
    row = rows[0]
    assert row["name"] == "Keys" and row["badge"] in {"steady", "intermittent", "not_heard_24h"}
    assert row["last"]["rssi"] == -60 and row["last"]["at"] == "2026-01-01T00:00:00Z"
    assert row["last"]["scanner_source"] == SCANNER
    assert client.get("/api/v1/find", params={"q": "zzz"}).json() == []
    assert len(client.get("/api/v1/find", params={"q": "key"}).json()) == 1


def test_find_row_without_sightings_has_null_last(
    client: TestClient, app: FastAPI, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    app.state.service.claim_ble("user", Identity("address", ADDR), device_id=d)
    assert client.get("/api/v1/find").json()[0]["last"] is None


def test_candidates_empty_without_ha(client: TestClient, app: FastAPI) -> None:
    app.state.bluetooth = None
    assert client.get("/api/v1/ble/candidates").json() == {"connected": None, "candidates": []}


def test_candidates_listed(client: TestClient) -> None:
    body = client.get("/api/v1/ble/candidates").json()
    assert body["connected"] is True
    (c,) = body["candidates"]
    assert c == {
        "identity": KEY,
        "name": "Tag",
        "best_rssi": -55,
        "source": SCANNER,
        "first_heard": "2025-12-31T23:55:00Z",
        "last_heard": "2026-01-01T00:00:00Z",
        "adverts": 7,
        "typical_gap_s": 2.5,
    }


def test_claim_existing_device(
    client: TestClient, make_device: Callable[..., int], db: sqlite3.Connection
) -> None:
    d = make_device(name="Keys")
    r = client.post("/api/v1/ble/claim", json={"identity": KEY, "device_id": d})
    assert r.status_code == 200, r.text
    assert r.json()["device_id"] == d and r.json()["batch_id"] > 0
    row = db.execute("select device_id, kind, value from ble_identity").fetchone()
    assert tuple(row) == (d, "address", ADDR)
    actor = db.execute("select actor from batch where id = ?", (r.json()["batch_id"],))
    assert actor.fetchone()[0] == "user"


def test_claim_new_device(client: TestClient, db: sqlite3.Connection) -> None:
    r = client.post(
        "/api/v1/ble/claim", json={"identity": KEY, "new": {"name": "Wallet", "type": "tag"}}
    )
    assert r.status_code == 200, r.text
    name, typ = db.execute(
        "select name, type from device where id = ?", (r.json()["device_id"],)
    ).fetchone()
    assert (name, typ) == ("Wallet", "tag")


def test_claim_unknown_candidate_422(client: TestClient, make_device: Callable[..., int]) -> None:
    d = make_device(name="Keys")
    other = Identity("address", "00:00:5e:00:53:02").key()
    r = client.post("/api/v1/ble/claim", json={"identity": other, "device_id": d})
    assert r.status_code == 422 and "no such candidate" in r.text


def test_claim_unparseable_identity_422(
    client: TestClient, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    r = client.post("/api/v1/ble/claim", json={"identity": "nonsense", "device_id": d})
    assert r.status_code == 422


def test_claim_without_runtime_422(
    client: TestClient, app: FastAPI, make_device: Callable[..., int]
) -> None:
    app.state.bluetooth = None
    d = make_device(name="Keys")
    r = client.post("/api/v1/ble/claim", json={"identity": KEY, "device_id": d})
    assert r.status_code == 422 and "no such candidate" in r.text


def test_claim_both_targets_422(client: TestClient, make_device: Callable[..., int]) -> None:
    d = make_device(name="Keys")
    body = {"identity": KEY, "device_id": d, "new": {"name": "x", "type": "tag"}}
    assert client.post("/api/v1/ble/claim", json=body).status_code == 422
    assert client.post("/api/v1/ble/claim", json={"identity": KEY}).status_code == 422


def test_claim_new_requires_type_and_strict_fields(client: TestClient) -> None:
    assert (
        client.post("/api/v1/ble/claim", json={"identity": KEY, "new": {"name": "x"}}).status_code
        == 422
    )
    bad = {"identity": KEY, "device_id": "5"}
    assert client.post("/api/v1/ble/claim", json=bad).status_code == 422
    extra = {"identity": KEY, "device_id": 1, "actor": "root"}
    assert client.post("/api/v1/ble/claim", json=extra).status_code == 422


def test_claim_unknown_device_404(client: TestClient) -> None:
    r = client.post("/api/v1/ble/claim", json={"identity": KEY, "device_id": 9999})
    assert r.status_code == 404


def test_unclaim_then_404_for_missing(
    client: TestClient, make_device: Callable[..., int], db: sqlite3.Connection
) -> None:
    d = make_device(name="Keys")
    client.post("/api/v1/ble/claim", json={"identity": KEY, "device_id": d})
    ident = db.execute("select id from ble_identity").fetchone()[0]
    r = client.delete(f"/api/v1/ble/identities/{ident}")
    assert r.status_code == 200 and r.json()["batch_id"] > 0
    assert db.execute("select count(*) from ble_identity").fetchone()[0] == 0
    assert client.delete(f"/api/v1/ble/identities/{ident}").status_code == 404
    assert client.delete("/api/v1/ble/identities/9999").status_code == 404


def test_link_scanner(
    client: TestClient,
    app: FastAPI,
    make_device: Callable[..., int],
    clock: FixedClock,
    db: sqlite3.Connection,
) -> None:
    d = make_device(name="Keys")
    app.state.service.claim_ble("user", Identity("address", ADDR), device_id=d)
    _hear(app, clock, d)
    (s,) = client.get("/api/v1/ble/scanners").json()
    assert s["source"] == SCANNER and s["device_id"] is None
    assert s["last_seen"].endswith("Z")
    host = make_device(name="ESP proxy")
    r = client.put(f"/api/v1/ble/scanners/{s['id']}/device", json={"device_id": host})
    assert r.status_code == 200 and r.json()["batch_id"] > 0
    assert client.get("/api/v1/ble/scanners").json()[0]["device_id"] == host
    r = client.put(f"/api/v1/ble/scanners/{s['id']}/device", json={"device_id": None})
    assert r.status_code == 200
    assert client.get("/api/v1/ble/scanners").json()[0]["device_id"] is None
    assert (
        client.put("/api/v1/ble/scanners/999/device", json={"device_id": None}).status_code == 404
    )
    assert (
        client.put(f"/api/v1/ble/scanners/{s['id']}/device", json={"device_id": 9999}).status_code
        == 404
    )
    assert client.put(f"/api/v1/ble/scanners/{s['id']}/device", json={}).status_code == 422


def test_ble_routes_require_token(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    settings = Settings(data_dir=tmp_path, api_token=TOKEN, allowed_hosts=("testserver",))
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    good = {"Authorization": f"Bearer {TOKEN}"}
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("get", "/api/v1/find", None),
        ("get", "/api/v1/ble/candidates", None),
        ("post", "/api/v1/ble/claim", {"identity": KEY, "device_id": 1}),
        ("delete", "/api/v1/ble/identities/1", None),
        ("get", "/api/v1/ble/scanners", None),
        ("put", "/api/v1/ble/scanners/1/device", {"device_id": None}),
    ]
    with TestClient(app) as c:
        for method, path, body in calls:
            kw: dict[str, Any] = {} if body is None else {"json": body}
            assert getattr(c, method)(path, **kw).status_code == 401, path
            assert getattr(c, method)(path, headers=good, **kw).status_code != 401, path


def test_undo_conflict_409_includes_reason(client: TestClient, app: FastAPI) -> None:
    @app.get("/api/v1/_boom")
    def boom() -> None:
        raise UndoConflict([], "device 5 no longer exists")

    @app.get("/api/v1/_boom2")
    def boom2() -> None:
        raise UndoConflict([3, 4])

    r = client.get("/api/v1/_boom")
    assert r.status_code == 409
    assert r.json() == {
        "detail": "conflict",
        "conflicting": [],
        "reason": "device 5 no longer exists",
    }
    r2 = client.get("/api/v1/_boom2")
    assert r2.status_code == 409 and r2.json() == {"detail": "conflict", "conflicting": [3, 4]}
