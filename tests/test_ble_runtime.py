"""BluetoothRuntime: identity index, flush job, health row, app wiring (fake client)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lanscape_atlas.bluetooth.match import Advert, Identity
from lanscape_atlas.bluetooth.runtime import BluetoothRuntime
from lanscape_atlas.collectors.bluetooth_ha import ClientStatus
from lanscape_atlas.config import BluetoothSettings, HomeAssistantSettings, Settings
from lanscape_atlas.events import EVENT_CHANGE_APPLIED, Event
from lanscape_atlas.scheduler import CollectorRunner
from lanscape_atlas.web.app import create_app

TOKEN = "ha-secret-token"
ADDR = "00:00:5e:00:53:01"
SCANNER = "00:00:5e:00:53:aa"
IDENT = Identity("address", ADDR)


class FakeClient:
    name = "bluetooth"

    def __init__(self, settings: Any, on_advert: Any, on_scanners: Any, clock: Any, **kw: Any):
        self.on_advert = on_advert
        self.on_scanners = on_scanners
        self.clock = clock
        self.kwargs = kw
        self.connected = True
        self.last_error: str | None = None
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def status(self) -> ClientStatus:
        return ClientStatus(self.connected, None, self.last_error)

    def hear(self, advert: Advert, at: datetime | None = None) -> None:
        """What the real client does: pass the advert with its effective time."""
        self.on_advert(advert, at if at is not None else self.clock())


def advert(rssi: int = -60, address: str = ADDR, source: str = SCANNER) -> Advert:
    return Advert(address, rssi, source, None, {}, time=1.0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        home_assistant=HomeAssistantSettings(url="http://192.0.2.10:8123", token=TOKEN),
        bluetooth=BluetoothSettings(snapshot_s=45),
    )


@pytest.fixture
def make_runtime(
    svc, ing, clock, oui, settings
) -> Callable[[], tuple[BluetoothRuntime, FakeClient]]:
    clients: list[FakeClient] = []

    def factory(*a: Any, **kw: Any) -> FakeClient:
        c = FakeClient(*a, **kw)
        clients.append(c)
        return c

    def _make() -> tuple[BluetoothRuntime, FakeClient]:
        runner = CollectorRunner(svc, ing, clock, secrets=[TOKEN])
        rt = BluetoothRuntime(svc, settings, oui, clock, runner=runner, client_factory=factory)
        return rt, clients[-1]

    return _make


def collector_status(db, name: str = "bluetooth") -> dict | None:
    row = db.execute("select * from collector_status where name = ?", (name,)).fetchone()
    return dict(row) if row else None


def sightings(db) -> list[dict]:
    return [dict(r) for r in db.execute("select * from ble_sighting order by minute")]


async def test_index_loaded_at_start(make_runtime, svc, make_device) -> None:
    dev = make_device()
    svc.claim_ble("user", IDENT, device_id=dev)
    rt, client = make_runtime()
    await rt.start()
    try:
        assert client.started
        client.hear(advert())
        rows = rt.aggregator.drain_all()
        assert [(r.device_id, r.adverts) for r in rows] == [(dev, 1)]
    finally:
        await rt.stop()


async def test_client_gets_snapshot_interval(make_runtime) -> None:
    _rt, client = make_runtime()
    assert client.kwargs == {"snapshot_s": 45}


async def test_adverts_bucketed_by_effective_time(make_runtime, svc, make_device, clock) -> None:
    dev = make_device()
    svc.claim_ble("user", IDENT, device_id=dev)
    rt, client = make_runtime()
    await rt.start()
    try:
        clock.advance(minutes=5)
        client.hear(advert(), clock() - timedelta(minutes=4))  # replayed from HA's history
        client.hear(advert(), clock() - timedelta(minutes=4))  # replayed again: not counted
        rows = rt.aggregator.drain_all()
        assert [(r.minute, r.adverts) for r in rows] == [("2026-01-01T00:01Z", 1)]
    finally:
        await rt.stop()


async def test_index_reloaded_after_claim(make_runtime, svc, make_device) -> None:
    dev = make_device()
    rt, client = make_runtime()
    await rt.start()
    try:
        client.hear(advert())
        assert rt.aggregator.drain_all() == []
        svc.claim_ble("user", IDENT, device_id=dev)
        await asyncio.sleep(0)  # the reload is scheduled onto the loop
        client.hear(advert())
        assert [r.device_id for r in rt.aggregator.drain_all()] == [dev]
    finally:
        await rt.stop()


async def test_reload_from_other_thread_runs_on_loop(make_runtime, svc, make_device) -> None:
    dev = make_device()
    rt, client = make_runtime()
    await rt.start()
    loop_thread = threading.get_ident()
    reloaded_on: list[int] = []
    real = rt._reload_index

    def spy() -> None:
        reloaded_on.append(threading.get_ident())
        real()

    rt._reload_index = spy  # type: ignore[method-assign]
    try:
        t = threading.Thread(target=svc.claim_ble, args=("user", IDENT), kwargs={"device_id": dev})
        t.start()
        await asyncio.to_thread(t.join)
        for _ in range(100):
            if reloaded_on:
                break
            await asyncio.sleep(0.01)
        assert reloaded_on == [loop_thread]
        client.hear(advert())
        assert [r.device_id for r in rt.aggregator.drain_all()] == [dev]
    finally:
        await rt.stop()


async def test_publish_after_stop_is_harmless(make_runtime, svc, make_device, monkeypatch) -> None:
    dev = make_device()
    rt, _ = make_runtime()
    await rt.start()
    await rt.stop()
    reloads: list[Any] = []
    monkeypatch.setattr(rt.aggregator, "set_index", lambda idx: reloads.append(idx))
    svc.claim_ble("user", IDENT, device_id=dev)  # publishes change.applied
    svc.events.publish(Event(EVENT_CHANGE_APPLIED, {"batch_id": 1, "actor": "user"}))
    await asyncio.sleep(0.01)
    assert reloads == []


async def test_flush_writes_closed_minutes(make_runtime, svc, make_device, db, clock) -> None:
    dev = make_device()
    svc.claim_ble("user", IDENT, device_id=dev)
    rt, client = make_runtime()
    await rt.start()
    try:
        client.hear(advert(-70))
        client.hear(advert(-50), clock() + timedelta(seconds=1))  # a later advert
        assert await rt.flush() == 0  # the current minute is still open
        clock.advance(minutes=1)
        assert await rt.flush() == 1
        (row,) = sightings(db)
        assert (row["device_id"], row["max_rssi"], row["adverts"]) == (dev, -50, 2)
    finally:
        await rt.stop()


async def test_flush_records_ok_status(make_runtime, db) -> None:
    rt, _ = make_runtime()
    await rt.start()
    try:
        await rt.flush()
        st = collector_status(db)
        assert st is not None and st["last_ok"] is not None and st["last_error"] is None
    finally:
        await rt.stop()


async def test_flush_records_client_error_status(make_runtime, db) -> None:
    rt, client = make_runtime()
    await rt.start()
    try:
        client.connected = False
        client.last_error = f"auth_invalid: {TOKEN}"
        await rt.flush()
        st = collector_status(db)
        assert st is not None
        assert st["last_error"] == "auth_invalid: ***"
        assert st["consecutive_failures"] == 1
        client.connected = True
        client.last_error = None
        await rt.flush()
        st = collector_status(db)
        assert st is not None and st["consecutive_failures"] == 0 and st["last_ok"]
    finally:
        await rt.stop()


async def test_flush_buffers_scanner_names(make_runtime, db, clock) -> None:
    rt, client = make_runtime()
    await rt.start()
    try:
        client.on_scanners({SCANNER: None})
        client.on_scanners({SCANNER: "Hallway"})
        assert db.execute("select count(*) from ble_scanner").fetchone()[0] == 0
        await rt.flush()
        row = db.execute("select name from ble_scanner where source = ?", (SCANNER,)).fetchone()
        assert row["name"] == "Hallway"
    finally:
        await rt.stop()


async def test_later_none_name_never_erases_known_name(make_runtime, db) -> None:
    rt, client = make_runtime()
    await rt.start()
    try:
        client.on_scanners({SCANNER: "Hallway"})
        client.on_scanners({SCANNER: None})
        await rt.flush()
        row = db.execute("select name from ble_scanner where source = ?", (SCANNER,)).fetchone()
        assert row["name"] == "Hallway"
    finally:
        await rt.stop()


async def test_failed_write_keeps_known_scanner_name(make_runtime, db, svc, monkeypatch) -> None:
    rt, client = make_runtime()
    await rt.start()
    try:
        client.on_scanners({SCANNER: "Hallway"})
        real = svc.upsert_scanners
        calls = {"n": 0}

        def flaky(scanners, seen_at):
            calls["n"] += 1
            if calls["n"] == 1:
                client.on_scanners({SCANNER: None})  # arrives while the write is running
                raise RuntimeError("boom")
            return real(scanners, seen_at=seen_at)

        monkeypatch.setattr(svc, "upsert_scanners", flaky)
        await rt.flush()
        await rt.flush()
        row = db.execute("select name from ble_scanner where source = ?", (SCANNER,)).fetchone()
        assert row["name"] == "Hallway"
    finally:
        await rt.stop()


async def test_cancelled_flush_and_stop_lose_nothing_and_never_overlap(
    make_runtime, svc, make_device, db, clock, monkeypatch
) -> None:
    dev = make_device()
    svc.claim_ble("user", IDENT, device_id=dev)
    rt, client = make_runtime()
    await rt.start()
    entered, release = threading.Event(), threading.Event()
    state = {"active": 0, "max_active": 0}
    real = svc.record_ble_minutes

    def slow(rows):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        try:
            entered.set()
            release.wait(5)
            return real(rows)
        finally:
            state["active"] -= 1

    monkeypatch.setattr(svc, "record_ble_minutes", slow)
    client.hear(advert(-70))
    clock.advance(minutes=1)
    flush_task = asyncio.create_task(rt.flush())
    while not entered.is_set():
        await asyncio.sleep(0.005)
    client.hear(advert(-60))  # lands in the new, still-open minute
    # What scheduler shutdown does: cancel the running job, then stop the runtime.
    flush_task.cancel()
    stop_task = asyncio.create_task(rt.stop())
    await asyncio.sleep(0.05)
    assert not stop_task.done() and not flush_task.done()  # both wait for the write
    release.set()
    await stop_task
    with pytest.raises(asyncio.CancelledError):
        await flush_task
    assert state["max_active"] == 1
    got = [(r["minute"], r["adverts"], r["max_rssi"]) for r in sightings(db)]
    assert got == [("2026-01-01T00:00Z", 1, -70), ("2026-01-01T00:01Z", 1, -60)]


async def test_cancelled_flush_with_failing_write_requeues(
    make_runtime, svc, make_device, db, clock, monkeypatch
) -> None:
    dev = make_device()
    svc.claim_ble("user", IDENT, device_id=dev)
    rt, client = make_runtime()
    await rt.start()
    entered, release = threading.Event(), threading.Event()

    def failing(rows):
        entered.set()
        release.wait(5)
        raise RuntimeError("disk full")

    real = svc.record_ble_minutes
    monkeypatch.setattr(svc, "record_ble_minutes", failing)
    client.hear(advert(-70))
    clock.advance(minutes=1)
    flush_task = asyncio.create_task(rt.flush())
    while not entered.is_set():
        await asyncio.sleep(0.005)
    flush_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await flush_task
    monkeypatch.setattr(svc, "record_ble_minutes", real)
    await rt.stop()
    assert [r["adverts"] for r in sightings(db)] == [1]


async def test_failed_write_requeues_and_next_flush_writes_all(
    make_runtime, svc, make_device, db, clock, monkeypatch, caplog
) -> None:
    dev = make_device()
    svc.claim_ble("user", IDENT, device_id=dev)
    rt, client = make_runtime()
    await rt.start()
    try:
        client.hear(advert(-70))
        client.hear(advert(-50), clock() + timedelta(seconds=1))  # a later advert
        clock.advance(minutes=1)
        real = svc.record_ble_minutes
        calls = {"n": 0}

        def flaky(rows):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(f"disk full {TOKEN}")
            return real(rows)

        monkeypatch.setattr(svc, "record_ble_minutes", flaky)
        assert await rt.flush() == 0
        assert TOKEN not in caplog.text and "disk full ***" in caplog.text
        st = collector_status(db)
        assert st is not None and st["last_error"] == "RuntimeError: disk full ***"
        assert sightings(db) == []
        assert await rt.flush() == 1
        (row,) = sightings(db)
        assert (row["max_rssi"], row["adverts"]) == (-50, 2)
        st = collector_status(db)
        assert st is not None and st["consecutive_failures"] == 0
    finally:
        await rt.stop()


async def test_stop_flushes_open_minutes(make_runtime, svc, make_device, db) -> None:
    dev = make_device()
    svc.claim_ble("user", IDENT, device_id=dev)
    rt, client = make_runtime()
    await rt.start()
    client.hear(advert())
    await rt.stop()
    assert client.stopped
    assert [r["device_id"] for r in sightings(db)] == [dev]


async def test_status_delegates_to_client(make_runtime) -> None:
    rt, client = make_runtime()
    client.last_error = "x"
    assert rt.status().last_error == "x"


def test_no_runtime_without_home_assistant(tmp_path: Path, db, clock) -> None:
    app = create_app(Settings(data_dir=tmp_path), conn=db, clock=clock, start_background=False)
    assert app.state.bluetooth is None


def test_runtime_built_with_home_assistant(settings: Settings, db, clock) -> None:
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    assert isinstance(app.state.bluetooth, BluetoothRuntime)
