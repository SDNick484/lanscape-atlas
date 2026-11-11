"""End to end: real client and runtime against a fake HA with a change-driven advert stream.

HA only pushes an advert when its payload changes and replays its history on every
subscribe (see helpers/fake_ha.py), so these tests check what atlas makes of that: one
count per snapshot for a steady device, consecutive minutes, room changes, and a restart
replay landing in its true minute.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conftest import FixedClock
from helpers.fake_ha import FakeHA, ManualTicker
from helpers.plan import save_square_room
from lanscape_atlas.bluetooth.match import Identity
from lanscape_atlas.bluetooth.runtime import BluetoothRuntime
from lanscape_atlas.collectors.bluetooth_ha import HaBluetoothClient
from lanscape_atlas.config import HomeAssistantSettings, Settings
from lanscape_atlas.scheduler import CollectorRunner
from lanscape_atlas.service import Placement
from lanscape_atlas.service import ble_queries as bq

TOKEN = "snapshot-test-token"
SRC_A = "00:00:5e:00:53:a1"
SRC_B = "00:00:5e:00:53:a2"
REMOTE = "00:00:5e:00:53:10"
NEIGHBOUR = "00:00:5e:00:53:30"
BEACON_ADDR = "00:00:5e:00:53:01"
UUID = "00000000-0000-4000-8000-000000000001"
BEACON = Identity("ibeacon", f"{UUID}/1/1")


def ibeacon_hex(minor: int) -> str:
    return "0215" + UUID.replace("-", "") + "0001" + f"{minor:04x}" + "c5"


def advert(
    address: str, time: float, *, source: str = SRC_A, rssi: int = -60, minor: int | None = None
) -> dict[str, Any]:
    return {
        "address": address,
        "rssi": rssi,
        "source": source,
        "name": None,
        "manufacturer_data": {} if minor is None else {"76": ibeacon_hex(minor)},
        "service_data": {},
        "service_uuids": [],
        "time": time,
        "connectable": False,
    }


async def wait_for(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    async def _poll() -> None:
        while not pred():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


@pytest.fixture
async def ha() -> AsyncIterator[tuple[FakeHA, TestServer]]:
    fake = FakeHA(TOKEN)
    app = web.Application()
    app.router.add_get("/api/websocket", fake.handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, server
    finally:
        await server.close()


class House:
    """A runtime wired to the fake HA, with snapshots released by hand."""

    def __init__(self, fake: FakeHA, svc: Any, ing: Any, oui: Any, clock: FixedClock, url: str):
        self.fake, self.svc, self.clock = fake, svc, clock
        self.ticker = ManualTicker()
        self.delivered = 0  # adverts the client handed to the runtime
        self._args = (svc, ing, oui, url)
        self.rt = self.new_runtime()

    def new_runtime(self) -> BluetoothRuntime:
        svc, ing, oui, url = self._args
        settings = Settings(
            data_dir=Path("."),
            home_assistant=HomeAssistantSettings(url=url, token=TOKEN),
        )

        def factory(
            ha: Any, on_advert: Callable[..., None], *a: Any, **kw: Any
        ) -> HaBluetoothClient:
            def counted(adv: Any, at: datetime) -> None:
                self.delivered += 1
                on_advert(adv, at)

            return HaBluetoothClient(
                ha, counted, *a, **kw, backoff_s=(0.01,), snapshot_sleep=self.ticker.wait
            )

        runner = CollectorRunner(svc, ing, self.clock, secrets=[TOKEN])
        return BluetoothRuntime(
            svc, settings, oui, self.clock, runner=runner, client_factory=factory
        )

    def now_ts(self, minus_s: float = 0) -> float:
        return self.clock.now.timestamp() - minus_s

    async def start(self) -> None:
        expect = self.delivered + len(self.fake.history)
        await self.rt.start()
        await wait_for(lambda: self.rt.status().connected and self.ticker.waiting == 1)
        await wait_for(lambda: self.delivered == expect)  # the initial replay is processed

    async def snapshot(self) -> None:
        """Release one snapshot and wait until atlas has processed its replay."""
        expect = self.delivered + len(self.fake.history)
        self.ticker.tick()
        await wait_for(lambda: self.delivered == expect and self.ticker.waiting == 1)

    async def hear_live(self, adv: dict[str, Any]) -> None:
        """HA hears a new payload and pushes it; wait until atlas has it."""
        expect = self.delivered + 1
        assert await self.fake.hear(adv)
        await wait_for(lambda: self.delivered == expect)


@pytest.fixture
async def house(
    ha: tuple[FakeHA, TestServer], svc: Any, ing: Any, oui: Any, clock: FixedClock
) -> AsyncIterator[House]:
    fake, server = ha
    h = House(fake, svc, ing, oui, clock, str(server.make_url("")).rstrip("/"))
    try:
        yield h
    finally:
        await h.rt.stop()


def counts(db: Any, device_id: int) -> list[tuple[str, str, int]]:
    rows = db.execute(
        "SELECT s.minute, sc.source, s.adverts FROM ble_sighting s"
        " JOIN ble_scanner sc ON sc.id = s.scanner_id WHERE s.device_id = ? ORDER BY s.minute",
        (device_id,),
    )
    return [(r[0], r[1], r[2]) for r in rows]


async def test_steady_beacon_counted_once_per_snapshot(
    house: House, svc: Any, db: Any, make_device: Callable[..., int]
) -> None:
    dev = make_device()
    svc.claim_ble("user", BEACON, device_id=dev)
    await house.start()
    await house.hear_live(advert(BEACON_ADDR, house.now_ts(), minor=1))  # new: pushed live
    await house.snapshot()  # replays the same advert: not counted again
    for _ in range(3):
        house.clock.advance(seconds=60)
        for k in range(5):  # re-broadcast, identical payload: HA pushes nothing
            sent = await house.fake.hear(advert(BEACON_ADDR, house.now_ts(4 - k), minor=1))
            assert not sent
        await house.snapshot()
        await house.snapshot()  # nothing new heard: the replay is not double-counted
    house.clock.advance(minutes=1)
    assert await house.rt.flush() == 4
    # Once for the live push, then once per snapshot that saw a newer advert: not once per
    # advert (16) and not once ever (1).
    assert counts(db, dev) == [
        ("2026-01-01T00:00Z", SRC_A, 1),
        ("2026-01-01T00:01Z", SRC_A, 1),
        ("2026-01-01T00:02Z", SRC_A, 1),
        ("2026-01-01T00:03Z", SRC_A, 1),
    ]
    assert house.fake.live_events == 1  # only the beacon's very first advert


async def test_device_that_keeps_advertising_fills_consecutive_minutes(
    house: House, svc: Any, db: Any, make_device: Callable[..., int]
) -> None:
    dev = make_device()
    svc.claim_ble("user", Identity("address", REMOTE), device_id=dev)
    await house.fake.hear(advert(REMOTE, house.now_ts()))
    await house.start()  # the initial subscribe replays it
    for _ in range(4):
        house.clock.advance(seconds=60)
        await house.fake.hear(advert(REMOTE, house.now_ts(), rssi=-61))  # not pushed
        await house.snapshot()
    house.clock.advance(minutes=1)
    await house.rt.flush()
    assert [m for m, _s, _n in counts(db, dev)] == [
        "2026-01-01T00:00Z",
        "2026-01-01T00:01Z",
        "2026-01-01T00:02Z",
        "2026-01-01T00:03Z",
        "2026-01-01T00:04Z",
    ]
    heard = bq.last_heard(db, dev)
    assert heard is not None
    assert heard.at == datetime(2026, 1, 1, 0, 4, tzinfo=UTC)
    assert house.fake.live_events == 0  # everything came from snapshots


async def test_moving_device_changes_room_on_next_snapshot(
    house: House, svc: Any, db: Any, make_device: Callable[..., int]
) -> None:
    room_a = save_square_room(svc, "Room A")
    room_b = save_square_room(svc, "Room B")
    svc.upsert_scanners({SRC_A: "proxy-a", SRC_B: "proxy-b"}, house.clock())
    ids = {r["source"]: r["id"] for r in db.execute("SELECT id, source FROM ble_scanner")}
    for source, room in ((SRC_A, room_a), (SRC_B, room_b)):
        proxy = make_device()
        svc.place_devices("user", [Placement(proxy, room, 0.3, 0.3)], based_on={proxy: 0})
        svc.link_scanner("user", ids[source], proxy)
    dev = make_device()
    svc.claim_ble("user", Identity("address", REMOTE), device_id=dev)
    await house.fake.hear(advert(REMOTE, house.now_ts(), source=SRC_A))
    await house.start()

    async def room() -> str | None:
        house.clock.advance(minutes=1)
        await house.rt.flush()
        heard = bq.last_heard(db, dev)
        return None if heard is None else heard.place_name

    assert await room() == "Room A"
    # HA moves the steady remote to scanner B in its history; the payload is unchanged.
    assert not await house.fake.hear(advert(REMOTE, house.now_ts(5), source=SRC_B))
    assert await room() == "Room A"  # nothing pushed yet
    await house.snapshot()
    assert await room() == "Room B"


async def test_restart_replays_old_history_into_its_true_minute(
    house: House, svc: Any, db: Any, make_device: Callable[..., int]
) -> None:
    dev = make_device()
    svc.claim_ble("user", Identity("address", REMOTE), device_id=dev)
    await house.start()
    house.clock.advance(seconds=30)
    await house.fake.hear(advert(REMOTE, house.now_ts()))  # heard at 00:00:30
    await house.snapshot()
    await house.rt.stop()  # writes the open minute
    assert [m for m, _s, _n in counts(db, dev)] == ["2026-01-01T00:00Z"]

    house.clock.advance(minutes=10)
    # An active house: something else is being heard right now (it fixes the offset).
    await house.fake.hear(advert(NEIGHBOUR, house.now_ts(1)))
    house.rt = house.new_runtime()
    # HA replays its whole history, the remote's 10-minute-old advert too.
    await house.start()
    await house.rt.stop()
    # The replay lands in 00:00 (upserted into the existing row), never in "now" (00:10).
    assert [m for m, _s, _n in counts(db, dev)] == ["2026-01-01T00:00Z"]
    heard = bq.last_heard(db, dev)
    assert heard is not None
    assert heard.at - datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC) <= timedelta(seconds=1)
