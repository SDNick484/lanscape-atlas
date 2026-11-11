"""A fake Home Assistant WebSocket endpoint that models HA's real Bluetooth advert stream.

Modeled on habluetooth 7.1.2 (HA 2026.11): HA keeps the latest advert per address in its
history and always updates it, but only feeds ``bluetooth/subscribe_advertisements`` when
``manufacturer_data``, ``service_data``, ``service_uuids`` or ``name`` differ from the
previous advert for that address. Registering the subscription replays the whole history as
one ``add`` batch right after the result. ``unsubscribe_events`` ends a subscription.

Tests drive the radio with ``hear()``; ``frames`` still lets a test inject raw frames once the
initial handshake is done, and ``send()`` sends a frame to a live connection at any time.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from aiohttp import web

PAYLOAD_KEYS = ("manufacturer_data", "service_data", "service_uuids", "name")


def payload(adv: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(adv.get(k) for k in PAYLOAD_KEYS)


class FakeHA:
    def __init__(self, token: str) -> None:
        self.token = token
        self.connections = 0
        self.received: list[list[dict[str, Any]]] = []  # per connection, after auth
        self.auth_messages: list[dict[str, Any]] = []
        self.fail_ids: dict[int, str] = {}  # command id -> error message (every connection)
        self.fail_next: dict[str, str] = {}  # command type -> error message (consumed once)
        # per connection index: raw frames to send once the initial two subscriptions are done
        self.frames: dict[int, list[Any]] = {}
        self.drop_after_frames: set[int] = set()
        self.subscribed = asyncio.Event()
        self.silent = False  # accept the socket but never send auth_required
        self.history: dict[str, dict[str, Any]] = {}  # address -> latest advert
        self.replays = 0  # snapshot batches sent (one per non-empty subscribe)
        self.live_events = 0  # change-driven events sent
        self._sockets: dict[int, web.WebSocketResponse] = {}
        self._subs: dict[int, set[int]] = {}  # connection -> live advert subscription ids

    # -- the "radio" ------------------------------------------------------------------------

    async def hear(self, adv: dict[str, Any]) -> bool:
        """HA hears ``adv``: history always updates; subscribers only see a payload change."""
        prev = self.history.get(adv["address"])
        self.history[adv["address"]] = dict(adv)
        if prev is not None and payload(prev) == payload(adv):
            return False
        for idx, subs in list(self._subs.items()):
            ws = self._sockets.get(idx)
            for sid in sorted(subs):
                if ws is not None and not ws.closed:
                    await ws.send_json({"id": sid, "type": "event", "event": {"add": [adv]}})
                    self.live_events += 1
        return True

    async def send(self, idx: int, frame: Any) -> None:
        ws = self._sockets[idx]
        if isinstance(frame, str):
            await ws.send_str(frame)
        else:
            await ws.send_json(frame)

    def subscriptions(self, idx: int) -> set[int]:
        return set(self._subs.get(idx, set()))

    # -- the WebSocket handler --------------------------------------------------------------

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
        await ws.send_json({"type": "auth_required", "ha_version": "2026.11.1"})
        auth = await ws.receive_json()
        self.auth_messages.append(auth)
        if auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.11.1"})
        self._sockets[idx] = ws
        self._subs[idx] = set()
        acked = 0
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                cmd = msg.json()
                self.received[idx].append(cmd)
                if not await self._command(ws, idx, cmd):
                    continue
                acked += 1
                if acked == 2:
                    self.subscribed.set()
                    for frame in self.frames.get(idx, []):
                        await self.send(idx, frame)
                    if idx in self.drop_after_frames:
                        await asyncio.sleep(0.1)  # leave a window to observe connected=True
                        await ws.close()
                        return ws
        finally:
            self._subs.pop(idx, None)
            self._sockets.pop(idx, None)
        return ws

    async def _command(self, ws: web.WebSocketResponse, idx: int, cmd: dict[str, Any]) -> bool:
        cid, kind = cmd["id"], cmd["type"]
        message = self.fail_ids.get(cid) or self.fail_next.pop(kind, None)
        if kind == "unsubscribe_events" and message is None:
            if cmd.get("subscription") not in self._subs[idx]:
                message = "Subscription not found."
        if message is not None:
            await ws.send_json(
                {
                    "id": cid,
                    "type": "result",
                    "success": False,
                    "result": None,
                    "error": {"code": "boom", "message": message},
                }
            )
            return False
        if kind == "unsubscribe_events":
            self._subs[idx].discard(cmd["subscription"])
        await ws.send_json({"id": cid, "type": "result", "success": True, "result": None})
        if kind == "bluetooth/subscribe_advertisements":
            self._subs[idx].add(cid)
            if self.history:  # the replay: HA's whole history, with the stored times
                batch = [dict(a) for a in self.history.values()]
                await ws.send_json({"id": cid, "type": "event", "event": {"add": batch}})
                self.replays += 1
        return True


class ManualTicker:
    """Stands in for ``asyncio.sleep`` between snapshots: each ``tick()`` releases one wait."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[None] = asyncio.Queue()
        self.waiting = 0

    async def wait(self, _seconds: float) -> None:
        self.waiting += 1
        try:
            await self._q.get()
        finally:
            self.waiting -= 1

    def tick(self) -> None:
        self._q.put_nowait(None)
