"""Starting an on-demand deep scan; shared by the JSON API and the web pages."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from lanscape_atlas.collectors.nmap import Runner, ScanRefused, check_deep_scan, scan_ip
from lanscape_atlas.ingest import Ingestor
from lanscape_atlas.scheduler import run_blocking
from lanscape_atlas.service import Service

log = logging.getLogger(__name__)


def _recheck(svc: Service, device_id: int) -> str:
    with svc.lock:
        return check_deep_scan(svc.conn, device_id, svc.ip_match_window_min)


async def _deep_scan_job(svc: Service, ingestor: Ingestor, runner: Runner, device_id: int) -> None:
    # The database work runs in a worker thread (run_blocking) so a held Service.lock or
    # another process's write lock never stalls the event loop; cancelling the task (app
    # shutdown) waits for a started ingest to commit or roll back.
    try:
        # Re-validate at run time: policy or leases may have changed since the request.
        ip = await run_blocking(_recheck, svc, device_id)
        observations = await scan_ip(ip, runner=runner, clock=svc.clock)
        result = await run_blocking(ingestor.ingest, observations)
        log.info("deep scan of device %d ingested: %s", device_id, result)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - a background scan must not crash the app
        log.warning("deep scan of device %d failed: %s: %s", device_id, type(e).__name__, e)


def start_deep_scan(state: Any, device_id: int) -> asyncio.Task[None]:
    """Validate and start a background deep scan of ``device_id`` on the running loop.

    ``state`` is ``app.state``. Raises ``KeyError`` (unknown device) or
    ``ScanRefused`` (policy, no usable IPv4, or a scan already running) before
    anything starts. Must be called on the event loop thread: check-then-insert
    into ``state.deep_scan_tasks`` relies on that to allow at most one in-flight
    scan per device (Ruling 33c).
    """
    svc: Service = state.service
    tasks: dict[int, asyncio.Task[None]] = state.deep_scan_tasks
    with svc.lock:
        check_deep_scan(svc.conn, device_id, svc.ip_match_window_min)
    running = tasks.get(device_id)
    if running is not None and not running.done():
        raise ScanRefused("deep scan already running")
    task = asyncio.create_task(
        _deep_scan_job(svc, state.ingestor, state.deep_scan_runner, device_id),
        name=f"deep-scan:{device_id}",
    )
    tasks[device_id] = task

    def _forget(t: asyncio.Task[None]) -> None:
        if tasks.get(device_id) is t:
            del tasks[device_id]

    task.add_done_callback(_forget)
    return task
