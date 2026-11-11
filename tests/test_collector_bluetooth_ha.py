"""Home Assistant Bluetooth WebSocket client against a fake HA server."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conftest import FixedClock
from helpers.fake_ha import FakeHA, ManualTicker
from lanscape_atlas.bluetooth.match import Advert
from lanscape_atlas.collectors.bluetooth_ha import HaBluetoothClient
from lanscape_atlas.config import HomeAssistantSettings

TOKEN = "s3cr3t-token-value"
SRC = "00:00:5e:00:53:01"
SRC_B = "00:00:5e:00:53:02"
DEV1 = "00:00:5e:00:53:10"
DEV2 = "00:00:5e:00:53:11"
T0 = datetime(2026, 1, 1, tzinfo=UTC).timestamp()  # the clock fixture's start


def adv(
    address: str,
    rssi: int = -60,
    source: str = SRC,
    *,
    time: float = T0,
    name: str = "x",
) -> dict[str, Any]:
    return {
        "address": address,
        "rssi": rssi,
        "source": source,
        "name": name,
        "manufacturer_data": {},
        "service_data": {},
        "service_uuids": [],
        "time": time,
    }


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


class Sink:
    def __init__(self) -> None:
        self.adverts: list[Advert] = []
        self.at: list[datetime] = []
        self.scanners: list[dict[str, str | None]] = []

    def on_advert(self, a: Advert, at: datetime) -> None:
        self.adverts.append(a)
        self.at.append(at)

    def on_scanners(self, s: dict[str, str | None]) -> None:
        self.scanners.append(s)


def make_client(
    server: TestServer, sink: Sink, clock: FixedClock, token: str = TOKEN, **kw: Any
) -> HaBluetoothClient:
    settings = HomeAssistantSettings(
        url=str(server.make_url("")).rstrip("/"), token=token, verify_tls=True
    )
    kw.setdefault("backoff_s", (0.01,))
    return HaBluetoothClient(settings, sink.on_advert, sink.on_scanners, clock, **kw)


async def wait_for(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    async def _poll() -> None:
        while not pred():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


async def run(client: HaBluetoothClient, body: Callable[[], Awaitable[None]]) -> None:
    await client.start()
    try:
        await body()
    finally:
        await client.stop()


async def test_auth_and_subscriptions(ha: tuple[FakeHA, TestServer], clock: FixedClock) -> None:
    fake, server = ha
    client = make_client(server, Sink(), clock)

    async def body() -> None:
        await asyncio.wait_for(fake.subscribed.wait(), 3)
        await wait_for(lambda: client.status().connected)

    await run(client, body)
    assert fake.auth_messages[0] == {"type": "auth", "access_token": TOKEN}
    cmds = fake.received[0]
    assert [(c["id"], c["type"]) for c in cmds] == [
        (1, "bluetooth/subscribe_scanner_details"),
        (2, "bluetooth/subscribe_advertisements"),
    ]
    assert client.name == "bluetooth"


async def test_adverts_delivered(ha: tuple[FakeHA, TestServer], clock: FixedClock) -> None:
    """The subscribe replays HA's history; afterwards only payload changes are pushed."""
    fake, server = ha
    await fake.hear(adv(DEV1, -50))
    sink = Sink()
    client = make_client(server, sink, clock)

    async def body() -> None:
        await wait_for(lambda: len(sink.adverts) == 1)
        assert await fake.hear(adv(DEV2, -70))  # first advert of a new address: pushed
        await wait_for(lambda: len(sink.adverts) == 2)

    await run(client, body)
    assert [(a.address, a.rssi, a.source) for a in sink.adverts] == [
        (DEV1, -50, SRC),
        (DEV2, -70, SRC),
    ]
    assert (fake.replays, fake.live_events) == (1, 1)


async def test_advert_remove_ignored(ha: tuple[FakeHA, TestServer], clock: FixedClock) -> None:
    fake, server = ha
    fake.frames[0] = [
        {"id": 2, "type": "event", "event": {"remove": [{"address": DEV1}]}},
        {"id": 2, "type": "event", "event": {"add": [adv(DEV2)]}},
    ]
    sink = Sink()
    client = make_client(server, sink, clock)

    async def body() -> None:
        await wait_for(lambda: len(sink.adverts) == 1)

    await run(client, body)
    assert sink.adverts[0].address == DEV2


async def test_scanner_details_delivered(ha: tuple[FakeHA, TestServer], clock: FixedClock) -> None:
    fake, server = ha
    fake.frames[0] = [
        {
            "id": 1,
            "type": "event",
            "event": {
                "add": [
                    {"source": "00-00-5E-00-53-01", "name": "Hallway"},
                    {"source": "00:00:5e:00:53:02"},
                    {"source": "not-an-address", "name": "Bad"},
                ]
            },
        },
        {"id": 1, "type": "event", "event": {"remove": [{"source": SRC, "name": "Hallway"}]}},
    ]
    sink = Sink()
    client = make_client(server, sink, clock)

    async def body() -> None:
        await wait_for(lambda: len(sink.scanners) >= 1)
        await asyncio.sleep(0.05)  # let the remove frame be processed

    await run(client, body)
    assert sink.scanners == [{SRC: "Hallway", "00:00:5e:00:53:02": None}]


async def test_bad_event_is_skipped(ha: tuple[FakeHA, TestServer], clock: FixedClock) -> None:
    fake, server = ha
    bad = {"address": "zz", "rssi": "loud", "source": SRC}
    fake.frames[0] = [
        {"id": 2, "type": "event", "event": {"add": [adv(DEV1), bad, adv(DEV2)]}},
        "this is not json",
        {"id": 2, "type": "event", "event": "weird"},
        {"id": 99, "type": "event", "event": {"add": [adv(DEV1)]}},
        {"nonsense": True},
        [1, 2, 3],
    ]
    sink = Sink()
    client = make_client(server, sink, clock)

    async def body() -> None:
        await wait_for(lambda: len(sink.adverts) == 2)
        await asyncio.sleep(0.05)
        assert client.status().connected

    await run(client, body)
    assert [a.address for a in sink.adverts] == [DEV1, DEV2]


async def test_callback_exception_does_not_kill_connection(
    ha: tuple[FakeHA, TestServer], clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = ha
    fake.frames[0] = [
        {"id": 2, "type": "event", "event": {"add": [adv(DEV1)]}},
        {"id": 2, "type": "event", "event": {"add": [adv(DEV2)]}},
    ]
    sink = Sink()
    seen: list[str] = []

    def flaky(a: Advert, _at: datetime) -> None:
        seen.append(a.address)
        if a.address == DEV1:
            raise RuntimeError(f"oops {TOKEN}")

    settings = HomeAssistantSettings(url=str(server.make_url("")).rstrip("/"), token=TOKEN)
    client = HaBluetoothClient(settings, flaky, sink.on_scanners, clock, backoff_s=(0.01,))
    caplog.set_level(logging.DEBUG)

    async def body() -> None:
        await wait_for(lambda: seen == [DEV1, DEV2])

    await run(client, body)
    assert fake.connections == 1
    assert TOKEN not in caplog.text


async def test_auth_invalid_sets_error_and_retries(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    client = make_client(server, Sink(), clock, token="wrong-token")

    async def body() -> None:
        await wait_for(lambda: fake.connections >= 2)

    await run(client, body)
    st = client.status()
    assert not st.connected
    assert st.last_error is not None and "Invalid access token" in st.last_error
    assert "wrong-token" not in st.last_error


async def test_reconnects_after_server_drop(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    fake.frames[0] = [{"id": 2, "type": "event", "event": {"add": [adv(DEV1)]}}]
    fake.drop_after_frames.add(0)
    fake.frames[1] = [{"id": 2, "type": "event", "event": {"add": [adv(DEV2)]}}]
    sink = Sink()
    client = make_client(server, sink, clock, backoff_s=(0.2,))
    observed: list[bool] = []

    async def body() -> None:
        await wait_for(lambda: client.status().connected)
        t_up = client.status().since
        clock.advance(seconds=5)
        await wait_for(lambda: not client.status().connected)
        observed.append(False)
        assert client.status().since == clock() and client.status().since != t_up
        await wait_for(lambda: client.status().connected)
        observed.append(True)
        await wait_for(lambda: len(sink.adverts) == 2)

    await run(client, body)
    assert observed == [False, True]
    assert [a.address for a in sink.adverts] == [DEV1, DEV2]
    assert fake.connections >= 2


async def test_status_since_tracks_transitions(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    _fake, server = ha
    client = make_client(server, Sink(), clock)
    assert client.status().since is None
    assert not client.status().connected
    start = clock()

    async def body() -> None:
        await wait_for(lambda: client.status().connected)
        assert client.status().since == start

    await run(client, body)
    assert not client.status().connected


async def test_subscription_failure_is_error(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    fake.fail_ids[2] = "not allowed"
    client = make_client(server, Sink(), clock)

    async def body() -> None:
        await wait_for(lambda: fake.connections >= 2)

    await run(client, body)
    st = client.status()
    assert not st.connected
    assert st.last_error is not None and "not allowed" in st.last_error


async def test_token_not_in_error(
    ha: tuple[FakeHA, TestServer], clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = ha
    fake.fail_ids[1] = f"bad things with {TOKEN} inside"
    caplog.set_level(logging.DEBUG)
    client = make_client(server, Sink(), clock)

    async def body() -> None:
        await wait_for(lambda: client.status().last_error is not None)

    await run(client, body)
    err = client.status().last_error
    assert err is not None
    assert TOKEN not in err
    assert "***" in err
    assert TOKEN not in caplog.text


async def test_stop_cancels_cleanly(ha: tuple[FakeHA, TestServer], clock: FixedClock) -> None:
    fake, server = ha
    client = make_client(server, Sink(), clock)
    await client.start()
    await client.start()  # idempotent
    await wait_for(lambda: client.status().connected)
    await client.stop()
    assert not client.status().connected
    await client.stop()  # idempotent
    await asyncio.sleep(0.05)
    assert fake.connections == 1  # no reconnect after stop


async def test_stop_before_connect_and_while_backing_off(clock: FixedClock) -> None:
    settings = HomeAssistantSettings(url="http://127.0.0.1:1", token=TOKEN)
    sink = Sink()
    client = HaBluetoothClient(settings, sink.on_advert, sink.on_scanners, clock, backoff_s=(30,))
    await client.stop()  # never started
    await client.start()
    await wait_for(lambda: client.status().last_error is not None)
    await client.stop()  # cancels the backoff sleep
    assert TOKEN not in (client.status().last_error or "")


def test_ws_url() -> None:
    from lanscape_atlas.collectors.bluetooth_ha import ws_url

    assert ws_url("http://ha.example:8123") == "ws://ha.example:8123/api/websocket"
    assert ws_url("https://ha.example") == "wss://ha.example/api/websocket"


async def test_handshake_timeout_is_an_error_and_retried(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    fake.silent = True
    client = make_client(server, Sink(), clock, handshake_timeout_s=0.1)

    async def body() -> None:
        await wait_for(lambda: fake.connections >= 2)  # timed out, backed off, retried
        err = client.status().last_error
        assert err is not None and "handshake" in err
        assert not client.status().connected

    await run(client, body)


async def test_since_is_start_time_before_any_connection(clock: FixedClock) -> None:
    settings = HomeAssistantSettings(url="http://127.0.0.1:1", token=TOKEN)
    sink = Sink()
    client = HaBluetoothClient(settings, sink.on_advert, sink.on_scanners, clock, backoff_s=(30,))
    assert client.status().since is None  # not started
    await client.start()
    started = clock()
    try:
        await wait_for(lambda: client.status().last_error is not None)
        status = client.status()
        assert not status.connected and status.since == started
    finally:
        await client.stop()


# -- snapshot re-subscribe (HA's stream is change-driven) -----------------------------------


def cmd_log(fake: FakeHA, idx: int = 0) -> list[tuple[Any, ...]]:
    return [
        (c["id"], c["type"], c["subscription"]) if "subscription" in c else (c["id"], c["type"])
        for c in fake.received[idx]
    ]


async def test_resubscribes_each_snapshot_with_increasing_ids(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    ticker = ManualTicker()
    client = make_client(server, Sink(), clock, snapshot_sleep=ticker.wait)

    async def body() -> None:
        await wait_for(lambda: client.status().connected and ticker.waiting == 1)
        for n in range(3):
            ticker.tick()
            await wait_for(lambda n=n: len(fake.received[0]) == 4 + 2 * n)
        await wait_for(lambda: fake.subscriptions(0) == {8})

    await run(client, body)
    assert cmd_log(fake) == [
        (1, "bluetooth/subscribe_scanner_details"),
        (2, "bluetooth/subscribe_advertisements"),
        (3, "unsubscribe_events", 2),
        (4, "bluetooth/subscribe_advertisements"),
        (5, "unsubscribe_events", 4),
        (6, "bluetooth/subscribe_advertisements"),
        (7, "unsubscribe_events", 6),
        (8, "bluetooth/subscribe_advertisements"),
    ]
    assert fake.connections == 1


async def test_snapshot_interval_uses_real_timer(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    client = make_client(server, Sink(), clock, snapshot_s=0.02)

    async def body() -> None:
        await wait_for(lambda: bool(fake.received) and len(fake.received[0]) >= 2 + 2 * 3)

    await run(client, body)
    assert fake.connections == 1


async def test_unchanged_advert_arrives_only_with_the_next_snapshot(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    ticker = ManualTicker()
    sink = Sink()
    client = make_client(server, sink, clock, snapshot_sleep=ticker.wait)

    async def body() -> None:
        await wait_for(lambda: client.status().connected and ticker.waiting == 1)
        assert await fake.hear(adv(DEV1, time=T0 - 3))  # new address: pushed live
        await wait_for(lambda: len(sink.adverts) == 1)
        for k in range(5):  # a steady beacon re-broadcasting the same payload
            assert not await fake.hear(adv(DEV1, -50 - k, time=T0 - 2 + k * 0.1))
        await asyncio.sleep(0.05)
        assert len(sink.adverts) == 1
        ticker.tick()
        await wait_for(lambda: len(sink.adverts) == 2)

    await run(client, body)
    latest = sink.adverts[-1]
    assert (latest.rssi, latest.time) == (-54, T0 - 2 + 0.4)


async def test_snapshot_replays_history_with_ha_times(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    await fake.hear(adv(DEV1, time=T0 - 1))  # fresh: fixes the offset
    await fake.hear(adv(DEV2, time=T0 - 7 * 60))  # heard 7 minutes ago
    sink = Sink()
    client = make_client(server, sink, clock)

    async def body() -> None:
        await wait_for(lambda: len(sink.adverts) == 2)

    await run(client, body)
    at = dict(zip((a.address for a in sink.adverts), sink.at, strict=True))
    assert at[DEV1] == clock.now
    assert at[DEV2] == clock.now - timedelta(minutes=7) + timedelta(seconds=1)


@pytest.mark.parametrize("ha_ahead_s", [30.0, -30.0, 0.0])
async def test_ha_clock_skew_corrected(
    ha: tuple[FakeHA, TestServer], clock: FixedClock, ha_ahead_s: float
) -> None:
    fake, server = ha
    ticker = ManualTicker()
    sink = Sink()
    await fake.hear(adv(DEV1, time=T0 + ha_ahead_s))  # heard just now, by HA's clock
    client = make_client(server, sink, clock, snapshot_sleep=ticker.wait)

    async def body() -> None:
        await wait_for(lambda: len(sink.adverts) == 1 and ticker.waiting == 1)
        clock.advance(minutes=2)
        # Heard 90 s ago by atlas's clock (HA stamps it with its own skewed clock).
        await fake.hear(adv(DEV1, -40, time=clock.now.timestamp() - 90 + ha_ahead_s))
        ticker.tick()
        await wait_for(lambda: len(sink.adverts) == 2)

    await run(client, body)
    assert sink.at == [clock.now - timedelta(minutes=2), clock.now - timedelta(seconds=90)]


async def test_far_future_time_is_dropped(ha: tuple[FakeHA, TestServer], clock: FixedClock) -> None:
    fake, server = ha
    await fake.hear(adv(DEV1, time=T0 + 3 * 86400))  # garbage, not skew
    await fake.hear(adv(DEV2, time=T0 - 5))
    sink = Sink()
    client = make_client(server, sink, clock)

    async def body() -> None:
        await wait_for(lambda: len(sink.adverts) == 1)
        await asyncio.sleep(0.05)

    await run(client, body)
    assert [a.address for a in sink.adverts] == [DEV2]
    assert sink.at == [clock.now]


async def test_events_for_a_replaced_subscription_are_ignored(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    fake, server = ha
    ticker = ManualTicker()
    sink = Sink()
    client = make_client(server, sink, clock, snapshot_sleep=ticker.wait)

    async def body() -> None:
        await wait_for(lambda: client.status().connected and ticker.waiting == 1)
        ticker.tick()
        await wait_for(lambda: fake.subscriptions(0) == {4} and ticker.waiting == 1)
        await fake.send(0, {"id": 2, "type": "event", "event": {"add": [adv(DEV1)]}})
        await fake.send(0, {"id": 4, "type": "event", "event": {"add": [adv(DEV2)]}})
        await wait_for(lambda: len(sink.adverts) == 1)
        await asyncio.sleep(0.05)

    await run(client, body)
    assert [a.address for a in sink.adverts] == [DEV2]


@pytest.mark.parametrize(
    "fail",
    [("unsubscribe_events", 3), ("bluetooth/subscribe_advertisements", 4)],
    ids=["unsubscribe", "subscribe"],
)
async def test_failed_resubscribe_reconnects(
    ha: tuple[FakeHA, TestServer],
    clock: FixedClock,
    fail: tuple[str, int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake, server = ha
    kind, cid = fail
    caplog.set_level(logging.WARNING)
    ticker = ManualTicker()
    client = make_client(server, Sink(), clock, snapshot_sleep=ticker.wait)

    async def body() -> None:
        await wait_for(lambda: client.status().connected and ticker.waiting == 1)
        fake.fail_next[kind] = f"refused {TOKEN}"
        ticker.tick()
        await wait_for(lambda: fake.connections == 2)
        await wait_for(lambda: client.status().connected)

    await run(client, body)
    assert f"command {cid} failed: refused ***" in caplog.text
    assert TOKEN not in caplog.text
    # The new connection starts its ids again and re-subscribes from scratch.
    assert cmd_log(fake, 1)[:2] == [
        (1, "bluetooth/subscribe_scanner_details"),
        (2, "bluetooth/subscribe_advertisements"),
    ]


async def test_snapshot_frame_above_default_ws_limit(
    ha: tuple[FakeHA, TestServer], clock: FixedClock
) -> None:
    """One snapshot is one frame: aiohttp's 4 MiB default must not cut it off."""
    fake, server = ha
    big = "n" * (5 * 1024 * 1024)
    await fake.hear(adv(DEV1, name=big))
    sink = Sink()
    client = make_client(server, sink, clock)

    async def body() -> None:
        await wait_for(lambda: len(sink.adverts) == 1)

    await run(client, body)
    assert fake.connections == 1
    assert sink.adverts[0].name == big
