"""Bluetooth runtime: identity index, per-minute flush and health, around ``HaBluetoothClient``.

Everything here runs on the event loop except the database work, which ``flush`` pushes to a
worker thread so a slow write cannot stall the WebSocket reader. Adverts never go through the
Ingestor/Observation path: they feed the aggregator directly.

``change.applied`` can be published from FastAPI threadpool threads, so the subscriber only
schedules the index reload onto the loop (``call_soon_threadsafe``); the reload itself runs
there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from lanscape_atlas.bluetooth.aggregator import BleAggregator, MinuteRow
from lanscape_atlas.bluetooth.match import Advert, Identity
from lanscape_atlas.clock import Clock
from lanscape_atlas.collectors.bluetooth_ha import ClientStatus, HaBluetoothClient
from lanscape_atlas.config import Settings
from lanscape_atlas.events import EVENT_CHANGE_APPLIED, Event
from lanscape_atlas.oui import OuiDB
from lanscape_atlas.service import Service

if TYPE_CHECKING:
    from lanscape_atlas.scheduler import CollectorRunner

log = logging.getLogger(__name__)

STATUS_NAME = "bluetooth"


class BluetoothRuntime:
    def __init__(
        self,
        service: Service,
        settings: Settings,
        oui: OuiDB,
        clock: Clock,
        *,
        runner: CollectorRunner,
        client_factory: Callable[..., HaBluetoothClient] = HaBluetoothClient,
    ) -> None:
        if settings.home_assistant is None:
            raise ValueError("BluetoothRuntime needs [home_assistant] settings")
        self._service = service
        self._clock = clock
        self._runner = runner
        self.aggregator = BleAggregator(
            clock, known_vendor=lambda addr: oui.lookup(addr) is not None
        )
        self._scanners: dict[str, str | None] = {}
        self._write_lock = asyncio.Lock()
        self._client = client_factory(
            settings.home_assistant, self._on_advert, self._on_scanners, clock
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribed = False
        self._running = False

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._reload_index()
        if not self._subscribed:
            self._subscribed = True
            self._service.events.subscribe(EVENT_CHANGE_APPLIED, self._on_change)
        await self._client.start()

    async def stop(self) -> None:
        self._running = False
        await self._client.stop()
        # Best effort: a failed write requeues the rows, which die with the process.
        await self._write(self.aggregator.drain_all, record_status=False)

    # -- public -----------------------------------------------------------------------------

    async def flush(self) -> int:
        """Write closed minutes (and buffered scanner names); returns sighting rows written."""
        return await self._write(self.aggregator.drain_closed, record_status=True)

    def status(self) -> ClientStatus:
        return self._client.status()

    # -- internals --------------------------------------------------------------------------

    def _on_advert(self, advert: Advert) -> None:
        self.aggregator.add(advert)

    def _on_scanners(self, scanners: dict[str, str | None]) -> None:
        for source, name in scanners.items():
            # A later None (name not reported) never erases a known name.
            if name is not None or source not in self._scanners:
                self._scanners[source] = name

    def _on_change(self, _event: Event) -> None:
        # May run on any thread (FastAPI threadpool): only hop onto the loop here.
        loop = self._loop
        if loop is None or not self._running:
            return
        with contextlib.suppress(RuntimeError):  # loop already closed during shutdown
            loop.call_soon_threadsafe(self._reload_index)

    def _reload_index(self) -> None:
        if not self._running:
            return
        try:
            with self._service.lock:
                rows = self._service.conn.execute(
                    "SELECT device_id, kind, value FROM ble_identity"
                ).fetchall()
            self.aggregator.set_index(
                {Identity(r["kind"], r["value"]): int(r["device_id"]) for r in rows}
            )
        except Exception:  # noqa: BLE001 - keep the old index rather than kill the loop callback
            log.exception("could not reload the Bluetooth identity index")

    def _persist(self, rows: Sequence[MinuteRow], scanners: dict[str, str | None]) -> int:
        """Worker thread: scanner names first, then sightings."""
        self._service.upsert_scanners(scanners, seen_at=self._clock())
        return self._service.record_ble_minutes(rows)

    async def _write(self, drain: Callable[[], list[MinuteRow]], *, record_status: bool) -> int:
        """Drain and persist under the write lock (writes never overlap).

        Draining happens inside the lock so a flush cancelled while waiting for it loses
        nothing. If this coroutine is cancelled (scheduler shutdown) mid-write, the worker
        thread's write is awaited to completion first (requeued on failure) and only then
        is the cancellation re-raised.
        """
        async with self._write_lock:
            rows = drain()
            scanners, self._scanners = self._scanners, {}
            task = asyncio.ensure_future(asyncio.to_thread(self._persist, rows, scanners))
            cancelled = False
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                while not task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait({task})
            except Exception:  # noqa: BLE001 - reported below from the task
                pass
            written = 0
            error: str | None = None
            exc = task.exception() if not task.cancelled() else asyncio.CancelledError()
            if exc is None:
                written = task.result()
            else:
                # Nothing is lost: requeue the rows and put the scanner names back.
                self.aggregator.requeue(rows)
                for source, name in scanners.items():
                    # Names buffered meanwhile are newer; only fill gaps.
                    if source not in self._scanners or (
                        self._scanners[source] is None and name is not None
                    ):
                        self._scanners[source] = name
                error = self._runner.redact(f"{type(exc).__name__}: {exc}")
                log.warning("Bluetooth flush failed: %s", error)
            if cancelled:
                raise asyncio.CancelledError
            if record_status:
                await self._record_status(error)
            return written

    async def _record_status(self, flush_error: str | None) -> None:
        client = self._client.status()
        error = flush_error
        if error is None and not client.connected:
            error = client.last_error or "not connected"
        try:
            await asyncio.to_thread(self._runner.record_status, STATUS_NAME, error)
        except Exception as exc:  # noqa: BLE001
            log.error("could not record Bluetooth status: %s", type(exc).__name__)
