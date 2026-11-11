"""Home Assistant Bluetooth WebSocket client.

Connects to ``/api/websocket``, authenticates with a long-lived token and subscribes to
``bluetooth/subscribe_scanner_details`` (id 1) and ``bluetooth/subscribe_advertisements``
(id 2). Parsed adverts and scanner names are handed to callbacks; this module never touches
the database. The connection is retried forever with a configurable backoff.

HA's advert stream is change-driven: an address is only pushed when its payload
(manufacturer data, service data, service UUIDs, name) changes, so a steady beacon is pushed
about once. Subscribing replays HA's history (the latest advert per address). So every
``snapshot_s`` seconds the advert subscription is replaced (``unsubscribe_events`` then a new
``bluetooth/subscribe_advertisements``, ids increasing per connection), and the replay that
follows is the snapshot. Each advert is passed on with its effective time: HA's ``time``
corrected for clock skew (``bluetooth.skew``), offset tracked per connection.

The token is a secret: it is replaced by ``***`` in every stored error and log message, and
the auth payload is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from lanscape_atlas.bluetooth.match import Advert, normalize_address, parse_advert
from lanscape_atlas.bluetooth.skew import SkewCorrector
from lanscape_atlas.clock import Clock
from lanscape_atlas.config import HomeAssistantSettings

log = logging.getLogger(__name__)

SCANNERS_ID = 1
ADVERTS_ID = 2
HEARTBEAT_S = 30
MAX_MSG_SIZE = 16 * 1024 * 1024  # one snapshot (HA's whole advert history) is one frame
_MAX_ERROR = 300


@dataclass
class ClientStatus:
    connected: bool
    since: datetime | None
    last_error: str | None


class _SessionError(Exception):
    """A failure that ends one connection attempt (message is already user-safe)."""


def ws_url(url: str) -> str:
    """Map an http(s) base URL to its ws(s) ``/api/websocket`` endpoint."""
    if url.startswith("https://"):
        base = "wss://" + url[len("https://") :]
    elif url.startswith("http://"):
        base = "ws://" + url[len("http://") :]
    else:
        base = url
    return base.rstrip("/") + "/api/websocket"


class HaBluetoothClient:
    name = "bluetooth"

    def __init__(
        self,
        settings: HomeAssistantSettings,
        on_advert: Callable[[Advert, datetime], None],
        on_scanners: Callable[[dict[str, str | None]], None],
        clock: Clock,
        *,
        backoff_s: tuple[float, ...] = (1, 2, 5, 15, 60),
        handshake_timeout_s: float = 30,
        snapshot_s: float = 60,
        snapshot_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._on_advert = on_advert
        self._on_scanners = on_scanners
        self._clock = clock
        self._backoff_s = backoff_s or (1.0,)
        self._handshake_timeout_s = handshake_timeout_s
        self._snapshot_s = snapshot_s
        self._snapshot_sleep = snapshot_sleep
        # Per-connection state, reset by ``_run_session``.
        self._skew = SkewCorrector()
        self._next_id = ADVERTS_ID + 1
        self._advert_sub = ADVERTS_ID
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._since: datetime | None = None
        self._last_error: str | None = None
        self._failures = 0

    # -- public API -------------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._since is None:
            self._since = self._clock()  # the disconnected state begins at start
        self._task = asyncio.create_task(self._run(), name="ha-bluetooth")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._set_connected(False)

    def status(self) -> ClientStatus:
        return ClientStatus(self._connected, self._since, self._last_error)

    # -- internals --------------------------------------------------------------------------

    def _redact(self, text: str) -> str:
        token = self._settings.token
        if token:
            text = text.replace(token, "***")
        return text[:_MAX_ERROR]

    def _set_connected(self, value: bool) -> None:
        if self._connected != value:
            self._connected = value
            self._since = self._clock()

    def _record_error(self, text: str) -> None:
        self._last_error = self._redact(text)
        log.warning("Home Assistant Bluetooth: %s", self._last_error)

    async def _run(self) -> None:
        while True:
            try:
                await self._connect_once()
                self._record_error("connection closed")
            except asyncio.CancelledError:
                raise
            except _SessionError as exc:
                self._record_error(str(exc))
            except Exception as exc:  # noqa: BLE001 - any transport failure means retry
                self._record_error(f"{type(exc).__name__}: {exc}")
            self._set_connected(False)
            delay = self._backoff_s[min(self._failures, len(self._backoff_s) - 1)]
            self._failures += 1
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        verify = self._settings.verify_tls
        async with aiohttp.ClientSession() as session:
            ws = await session.ws_connect(
                ws_url(self._settings.url),
                heartbeat=HEARTBEAT_S,
                ssl=verify,
                max_msg_size=MAX_MSG_SIZE,
            )
            try:
                await self._session(ws)
            finally:
                await ws.close()

    @staticmethod
    async def _recv(ws: aiohttp.ClientWebSocketResponse) -> Any:
        """Next JSON frame; non-JSON text frames come back as ``None``."""
        msg = await ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                return json.loads(msg.data)
            except ValueError:
                log.debug("Home Assistant Bluetooth: skipping non-JSON frame")
                return None
        if msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        ):
            raise _SessionError("connection closed")
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise _SessionError(f"websocket error: {ws.exception()!r}")
        return None  # binary etc.

    async def _session(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # The handshake (auth_required through both subscription results) is bounded; the
        # deadline is lifted once subscribed, since the advert stream may be quiet. Snapshot
        # re-subscribes are not covered by it.
        try:
            async with asyncio.timeout(self._handshake_timeout_s) as handshake:
                await self._run_session(ws, handshake)
        except TimeoutError:
            if handshake.expired():
                raise _SessionError(
                    f"handshake timed out after {self._handshake_timeout_s:g}s"
                ) from None
            raise

    async def _run_session(
        self, ws: aiohttp.ClientWebSocketResponse, handshake: asyncio.Timeout
    ) -> None:
        first = await self._recv(ws)
        if not isinstance(first, dict) or first.get("type") != "auth_required":
            raise _SessionError("unexpected greeting from Home Assistant")
        await ws.send_json({"type": "auth", "access_token": self._settings.token})
        while True:
            reply = await self._recv(ws)
            if not isinstance(reply, dict):
                continue
            kind = reply.get("type")
            if kind == "auth_ok":
                break
            if kind == "auth_invalid":
                raise _SessionError(f"auth_invalid: {reply.get('message', 'invalid token')}")
        self._skew.reset()
        self._next_id = ADVERTS_ID + 1
        self._advert_sub = ADVERTS_ID
        await ws.send_json({"id": SCANNERS_ID, "type": "bluetooth/subscribe_scanner_details"})
        await ws.send_json({"id": ADVERTS_ID, "type": "bluetooth/subscribe_advertisements"})
        pending = {SCANNERS_ID, ADVERTS_ID}
        snapshots: asyncio.Task[None] | None = None
        try:
            while True:
                frame = await self._recv(ws)
                if not isinstance(frame, dict):
                    if frame is not None:
                        log.debug("Home Assistant Bluetooth: skipping unexpected frame")
                    continue
                kind = frame.get("type")
                if kind == "result":
                    cid = frame.get("id")
                    if frame.get("success") is not True:
                        # Any failed command, the snapshot re-subscribes included: reconnect.
                        err = frame.get("error")
                        msg = err.get("message") if isinstance(err, dict) else None
                        raise _SessionError(f"command {cid} failed: {msg or 'unknown error'}")
                    if cid in pending:
                        pending.discard(cid)
                        if not pending:
                            self._failures = 0
                            self._last_error = None
                            self._set_connected(True)
                            handshake.reschedule(None)
                            snapshots = asyncio.create_task(
                                self._snapshots(ws), name="ha-bluetooth-snapshots"
                            )
                elif kind == "event":
                    self._handle_event(frame)
                else:
                    log.debug("Home Assistant Bluetooth: skipping unknown message type")
        finally:
            if snapshots is not None:
                snapshots.cancel()
                # The session's own error (or cancellation) is what matters here.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await snapshots

    def _take_id(self) -> int:
        cid = self._next_id
        self._next_id += 1
        return cid

    async def _snapshots(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Every ``snapshot_s``: replace the advert subscription; HA replays its history.

        Results are checked by the reader (a failure ends the session). If a send fails the
        socket is closed so the reader ends the session too.
        """
        try:
            while True:
                await self._snapshot_sleep(self._snapshot_s)
                old = self._advert_sub
                unsub_id, sub_id = self._take_id(), self._take_id()
                self._advert_sub = sub_id  # from now on, events for ``old`` are ignored
                await ws.send_json(
                    {"id": unsub_id, "type": "unsubscribe_events", "subscription": old}
                )
                await ws.send_json({"id": sub_id, "type": "bluetooth/subscribe_advertisements"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the reader reports the closed connection
            log.warning(
                "Home Assistant Bluetooth: snapshot failed: %s",
                self._redact(f"{type(exc).__name__}: {exc}"),
            )
            await ws.close()

    def _handle_event(self, frame: dict[str, Any]) -> None:
        cid = frame.get("id")
        event = frame.get("event")
        if not isinstance(event, dict):
            return
        add = event.get("add")
        if not isinstance(add, list):
            return  # "remove" and anything else is ignored (as are replaced subscriptions)
        if cid == self._advert_sub:
            # One frame is received at one instant: fold the whole batch into the offset
            # first, so a snapshot's fresh adverts fix the offset for its replayed old ones.
            receipt = self._clock()
            adverts = []
            for raw in add:
                advert = parse_advert(raw)
                if advert is not None and self._skew.observe(receipt, advert.time):
                    adverts.append(advert)
            for advert in adverts:
                self._safe(self._on_advert, advert, self._skew.effective(receipt, advert.time))
        elif cid == SCANNERS_ID:
            scanners: dict[str, str | None] = {}
            for details in add:
                if not isinstance(details, dict):
                    continue
                try:
                    source = normalize_address(details["source"])
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
                name = details.get("name")
                scanners[source] = name if isinstance(name, str) and name else None
            if scanners:
                self._safe(self._on_scanners, scanners)

    def _safe(self, fn: Callable[..., None], *args: Any) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - a bad callback must not kill the connection
            log.warning(
                "Home Assistant Bluetooth callback failed: %s",
                self._redact(f"{type(exc).__name__}: {exc}"),
            )
