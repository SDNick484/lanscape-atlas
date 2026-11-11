"""The /find page and the Bluetooth parts of the device page (Phase 3, Task 10)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
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
from lanscape_atlas.web.app import create_app
from lanscape_atlas.web.ble_api import ago

SCANNER = "00:00:5e:00:53:aa"
ADDR = "00:00:5e:00:53:01"
KEY = f"address:{ADDR}"


class StubAggregator:
    def __init__(self, items: list[Candidate]) -> None:
        self.items = items

    def candidates(self) -> list[Candidate]:
        return list(self.items)


class StubRuntime:
    def __init__(
        self,
        items: list[Candidate],
        status: ClientStatus | None = None,
    ) -> None:
        self.aggregator = StubAggregator(items)
        self._status = status or ClientStatus(True, None, None)

    def status(self) -> ClientStatus:
        return self._status


def cand(clock: FixedClock, name: str | None = "Tag", address: str = ADDR) -> Candidate:
    now = clock()
    return Candidate(
        Identity("address", address), name, -55, SCANNER, now - timedelta(minutes=5), now, 7, 2.5
    )


@pytest.fixture
def app(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> FastAPI:
    settings = Settings(data_dir=tmp_path, allowed_hosts=("testserver",))
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    app.state.bluetooth = StubRuntime([cand(clock)])
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _token(client: TestClient) -> str:
    if "atlas_csrf" not in client.cookies:
        assert client.get("/find").status_code == 200
    token = client.cookies.get("atlas_csrf")
    assert token
    return token


def _post(client: TestClient, url: str, data: dict[str, Any]) -> Any:
    return client.post(url, data={"csrf": _token(client), **data}, follow_redirects=False)


def _hear(
    app: FastAPI, device_id: int, at: datetime, source: str = SCANNER, rssi: int = -60
) -> None:
    app.state.service.record_ble_minutes(
        [
            MinuteRow(
                device_id=device_id,
                source=source,
                minute=at.strftime(BLE_MINUTE_FMT),
                max_rssi=rssi,
                adverts=3,
                last_at=at,
            )
        ]
    )


def _claimed(app: FastAPI, make_device: Callable[..., int], name: str = "Keys") -> int:
    d = make_device(name=name, type="tag")
    app.state.service.claim_ble("user", Identity("address", ADDR), device_id=d)
    return d


def _room_scanner(app: FastAPI, db: sqlite3.Connection, make_device: Callable[..., int]) -> None:
    svc = app.state.service
    esp = make_device(name="Proxy")
    svc.set_place("user", esp, "Kitchen")
    svc.upsert_scanners({SCANNER: "kitchen-proxy"}, app.state.service.clock())
    sid = db.execute("select id from ble_scanner where source = ?", (SCANNER,)).fetchone()[0]
    svc.link_scanner("user", sid, esp)


# ------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "just now"),
        (59, "just now"),
        (60, "1 min ago"),
        (59 * 60 + 59, "59 min ago"),
        (3600, "1 h ago"),
        (5 * 3600 + 1200, "5 h ago"),
    ],
)
def test_ago_text(seconds: int, text: str) -> None:
    assert ago(seconds) == text


# ---------------------------------------------------------------- find rows


def test_find_lists_claimed_with_room_and_age(
    client: TestClient,
    app: FastAPI,
    db: sqlite3.Connection,
    clock: FixedClock,
    make_device: Callable[..., int],
) -> None:
    _room_scanner(app, db, make_device)
    d = _claimed(app, make_device)
    _hear(app, d, clock() - timedelta(minutes=4))
    html = client.get("/find").text
    assert "Keys" in html and "Kitchen" in html and "4 min ago" in html
    assert f'href="/devices/{d}"' in html
    part = client.get("/partials/find", params={"q": "key"}).text
    assert "Keys" in part and "<html" not in part
    assert "Keys" not in client.get("/partials/find", params={"q": "zzz"}).text


def test_find_names_scanner_without_room(
    client: TestClient, app: FastAPI, clock: FixedClock, make_device: Callable[..., int]
) -> None:
    d = _claimed(app, make_device)
    _hear(app, d, clock())
    assert "near " + SCANNER in client.get("/find").text


def test_find_shows_badges(
    client: TestClient, app: FastAPI, clock: FixedClock, make_device: Callable[..., int]
) -> None:
    steady = _claimed(app, make_device, "Steady one")
    for i in range(50):
        _hear(app, steady, clock() - timedelta(minutes=i))
    sparse = make_device(name="Sparse one", type="tag")
    app.state.service.claim_ble("user", Identity("address", "00:00:5e:00:53:02"), device_id=sparse)
    _hear(app, sparse, clock() - timedelta(minutes=1))
    silent = make_device(name="Silent one", type="tag")
    app.state.service.claim_ble("user", Identity("address", "00:00:5e:00:53:03"), device_id=silent)
    html = client.get("/find").text
    assert "Steady</span>" in html
    assert "Intermittent</span>" in html
    assert "Heard only sometimes — pressed, idle, or throttled" in html
    assert "Not heard in 24 h</span>" in html


# --------------------------------------------------------------- candidates


def test_find_candidates_and_claim_flow(
    client: TestClient,
    app: FastAPI,
    db: sqlite3.Connection,
    clock: FixedClock,
    make_device: Callable[..., int],
) -> None:
    html = client.get("/find").text
    assert KEY in html and "Tag" in html
    existing = make_device(name="Existing", type="tag")
    r = _post(client, "/find/claim", {"identity": KEY, "device_id": str(existing)})
    assert r.status_code == 303 and r.headers["location"].startswith("/find")
    flashed = client.get(r.headers["location"]).text
    assert "Existing" in flashed and 'class="flash ok"' in flashed
    assert db.execute("select device_id from ble_identity").fetchone()[0] == existing
    # New device from a second candidate.
    other = "00:00:5e:00:53:09"
    app.state.bluetooth = StubRuntime([cand(clock, address=other)])
    r = _post(
        client,
        "/find/claim",
        {"identity": f"address:{other}", "new_name": "Remote", "new_type": "remote"},
    )
    assert r.status_code == 303
    row = db.execute("select name, type from device where name = 'Remote'").fetchone()
    assert tuple(row) == ("Remote", "remote")
    assert "Remote" in client.get("/find").text


def test_claim_stale_candidate_is_422_flash(
    client: TestClient, app: FastAPI, make_device: Callable[..., int], db: sqlite3.Connection
) -> None:
    d = make_device(name="Existing", type="tag")
    app.state.bluetooth = StubRuntime([])
    r = _post(client, "/find/claim", {"identity": KEY, "device_id": str(d)})
    assert r.status_code == 422
    assert "That device is no longer in range; nothing was claimed." in r.text
    assert db.execute("select count(*) from ble_identity").fetchone()[0] == 0
    # Junk identity is the same user-facing failure, not a 500.
    r = _post(client, "/find/claim", {"identity": "nope", "device_id": str(d)})
    assert r.status_code == 422


def test_claim_without_target_or_with_both_is_422(
    client: TestClient, make_device: Callable[..., int], db: sqlite3.Connection
) -> None:
    d = make_device(name="Existing", type="tag")
    assert _post(client, "/find/claim", {"identity": KEY}).status_code == 422
    both = {"identity": KEY, "device_id": str(d), "new_name": "X", "new_type": "tag"}
    assert _post(client, "/find/claim", both).status_code == 422
    assert db.execute("select count(*) from ble_identity").fetchone()[0] == 0


def test_find_claim_requires_csrf(client: TestClient, make_device: Callable[..., int]) -> None:
    d = make_device(name="Existing", type="tag")
    data = {"identity": KEY, "device_id": str(d)}
    token = _token(client)
    assert client.post("/find/claim", data=data).status_code == 403
    assert client.post("/find/claim", data={**data, "csrf": "wrong"}).status_code == 403
    client.cookies.clear()
    assert client.post("/find/claim", data={**data, "csrf": token}).status_code == 403
    client.cookies.set("atlas_csrf", "x")
    assert client.post("/find/claim", data={**data, "csrf": "x"}).status_code == 403
    client.cookies.clear()
    assert _post(client, "/find/claim", data).status_code == 303


def test_unclaim_requires_csrf(
    client: TestClient, app: FastAPI, make_device: Callable[..., int], db: sqlite3.Connection
) -> None:
    d = _claimed(app, make_device)
    iid = db.execute("select id from ble_identity").fetchone()[0]
    url = f"/devices/{d}/ble/{iid}/unclaim"
    assert client.post(url, data={}).status_code == 403
    assert db.execute("select count(*) from ble_identity").fetchone()[0] == 1


def test_find_escapes_advert_names(client: TestClient, app: FastAPI, clock: FixedClock) -> None:
    app.state.bluetooth = StubRuntime([cand(clock, name="<script>alert(1)</script>")])
    html = client.get("/find").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ------------------------------------------------------------------- banner


def _down(app: FastAPI, clock: FixedClock, minutes: float, err: str | None = "refused") -> None:
    since = clock() - timedelta(minutes=minutes)
    app.state.bluetooth = StubRuntime([], ClientStatus(False, since, err))


def test_banner_after_two_minutes_down(client: TestClient, app: FastAPI, clock: FixedClock) -> None:
    _down(app, clock, 3, "connection refused")
    for url in ("/find", "/partials/find"):
        html = client.get(url).text
        assert "Home Assistant is unreachable — Bluetooth sightings are stale." in html
        assert "connection refused" in html


def test_no_banner_within_two_minutes(client: TestClient, app: FastAPI, clock: FixedClock) -> None:
    _down(app, clock, 1)
    assert "unreachable" not in client.get("/find").text
    app.state.bluetooth = StubRuntime([], ClientStatus(False, None, "x"))  # not started
    assert "unreachable" not in client.get("/find").text
    app.state.bluetooth = StubRuntime([], ClientStatus(True, clock() - timedelta(hours=1), None))
    assert "unreachable" not in client.get("/find").text


def test_never_connected_banner_counts_from_start(
    client: TestClient, app: FastAPI, clock: FixedClock
) -> None:
    started = clock()  # what HaBluetoothClient.start() stamps as since
    app.state.bluetooth = StubRuntime([], ClientStatus(False, started, "auth_invalid: ***"))
    clock.advance(minutes=1)
    assert "unreachable" not in client.get("/find").text
    clock.advance(minutes=2)
    html = client.get("/find").text
    assert "Home Assistant is unreachable" in html and "auth_invalid: ***" in html


def test_claimed_param_out_of_range(client: TestClient) -> None:
    # Same convention as other page ids: out of range is a 422 HTML error, never a 500.
    for bad in (str(2**63), "0", "-1"):
        r = client.get("/find", params={"claimed": bad})
        assert r.status_code == 422 and "text/html" in r.headers["content-type"]


def test_find_says_not_configured(client: TestClient, app: FastAPI) -> None:
    app.state.bluetooth = None
    r = client.get("/find")
    assert r.status_code == 200
    assert (
        "Bluetooth Find needs Home Assistant. Add a [home_assistant] section to config.toml."
        in (r.text)
    )
    assert "unreachable" not in r.text and "Candidates" not in r.text


def test_nav_has_find(client: TestClient) -> None:
    assert re.search(r'<a href="/find"[^>]*>Find</a>', client.get("/").text)


# -------------------------------------------------------------- device page


def test_device_page_last_heard_and_strip(
    client: TestClient,
    app: FastAPI,
    db: sqlite3.Connection,
    clock: FixedClock,
    make_device: Callable[..., int],
) -> None:
    _room_scanner(app, db, make_device)
    d = _claimed(app, make_device)
    _hear(app, d, clock() - timedelta(minutes=4))
    html = client.get(f"/devices/{d}").text
    assert "Last heard near Kitchen, 4 min ago" in html
    cells = re.findall(r'<li class="hour[^"]*" title="([^"]*)"', html)
    assert len(cells) == 24
    # The clock is 00:00, so 4 minutes ago falls in the previous hour (index 22).
    assert cells[22] == "1 h ago: Kitchen"
    assert all(c.endswith("not heard") for i, c in enumerate(cells) if i != 22)
    assert KEY in html and "Unclaim" in html


def test_device_page_last_heard_without_room_and_never(
    client: TestClient, app: FastAPI, clock: FixedClock, make_device: Callable[..., int]
) -> None:
    d = _claimed(app, make_device)
    assert "Not heard yet" in client.get(f"/devices/{d}").text
    _hear(app, d, clock())
    assert f"Last heard near {SCANNER}, just now" in client.get(f"/devices/{d}").text


def test_device_page_without_ble_has_no_section(
    client: TestClient, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Plain")
    assert 'id="device-ble"' not in client.get(f"/devices/{d}").text


def test_device_page_unclaim(
    client: TestClient, app: FastAPI, make_device: Callable[..., int], db: sqlite3.Connection
) -> None:
    d = _claimed(app, make_device)
    iid = db.execute("select id from ble_identity").fetchone()[0]
    r = _post(client, f"/devices/{d}/ble/{iid}/unclaim", {})
    assert r.status_code == 303 and r.headers["location"] == f"/devices/{d}"
    assert db.execute("select count(*) from ble_identity").fetchone()[0] == 0
    # Identity belonging to another device: not found, nothing deleted.
    d2 = _claimed_other(app, make_device)
    iid2 = db.execute("select id from ble_identity").fetchone()[0]
    assert _post(client, f"/devices/{d}/ble/{iid2}/unclaim", {}).status_code == 404
    assert db.execute("select count(*) from ble_identity").fetchone()[0] == 1
    assert d2 != d


def _claimed_other(app: FastAPI, make_device: Callable[..., int]) -> int:
    d = make_device(name="Other", type="tag")
    app.state.service.claim_ble("user", Identity("address", ADDR), device_id=d)
    return d
