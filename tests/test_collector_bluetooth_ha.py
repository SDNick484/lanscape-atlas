"""Home Assistant Bluetooth WebSocket client against a fake HA server."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conftest import FixedClock
from lanscape_atlas.bluetooth.match import Advert
from lanscape_atlas.collectors.bluetooth_ha import HaBluetoothClient
from lanscape_atlas.config import HomeAssistantSettings

TOKEN = "s3cr3t-token-value"
SRC = "00:00:5e:00:53:01"
DEV1 = "00:00:5e:00:53:10"
DEV2 = "00:00:5e:00:53:11"


def adv(address: str, rssi: int = -60, source: str = SRC) -> dict[str, Any]:
    return {
        "address": address,
        "rssi": rssi,
        "source": source,
        "name": "x",
        "manufacturer_data": {},
    }


class FakeHA:
    """Scriptable fake Home Assistant WebSocket endpoint."""

    def __init__(self) -> None:
        self.token = TOKEN
        self.connections = 0
        self.received: list[list[dict[str, Any]]] = []  # per connection, after auth
        self.auth_messages: list[dict[str, Any]] = []
        self.fail_ids: dict[int, str] = {}  # id -> error message
        # per connection index: raw text frames to send after both subscriptions
        self.frames: dict[int, list[Any]] = {}
        self.drop_after_frames: set[int] = set()
        self.subscribed = asyncio.Event()
        self.silent = False  # accept the socket but never send auth_required

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        idx = self.connections
        self.connections += 1
        self.received.append([])
        if self.silent:
            async for _ in ws:
                pass
            return ws
        await ws.send_json({"type": "auth_required", "ha_version": "2026.1.0"})
        auth = await ws.receive_json()
        self.auth_messages.append(auth)
        if auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.1.0"})
        seen = 0
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            cmd = msg.json()
            self.received[idx].append(cmd)
            cid = cmd["id"]
            if cid in self.fail_ids:
                await ws.send_json(
                    {
                        "id": cid,
                        "type": "result",
                        "success": False,
                        "result": None,
                        "error": {"code": "boom", "message": self.fail_ids[cid]},
                    }
                )
                continue
            await ws.send_json({"id": cid, "type": "result", "success": True, "result": None})
            seen += 1
            if seen == 2:
                self.subscribed.set()
                for frame in self.frames.get(idx, []):
                    if isinstance(frame, str):
                        await ws.send_str(frame)
                    else:
                        await ws.send_json(frame)
                if idx in self.drop_after_frames:
                    await asyncio.sleep(0.1)  # leave a window to observe connected=True
                    await ws.close()
                    return ws
        return ws


@pytest.fixture
async def ha() -> AsyncIterator[tuple[FakeHA, TestServer]]:
    fake = FakeHA()
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
        self.scanners: list[dict[str, str | None]] = []

    def on_advert(self, a: Advert) -> None:
        self.adverts.append(a)

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
    fake, server = ha
    fake.frames[0] = [
        {"id": 2, "type": "event", "event": {"add": [adv(DEV1, -50), adv(DEV2, -70)]}}
    ]
    sink = Sink()
    client = make_client(server, sink, clock)

    async def body() -> None:
        await wait_for(lambda: len(sink.adverts) == 2)

    await run(client, body)
    assert [(a.address, a.rssi, a.source) for a in sink.adverts] == [
        (DEV1, -50, SRC),
        (DEV2, -70, SRC),
    ]


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

    def flaky(a: Advert) -> None:
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
