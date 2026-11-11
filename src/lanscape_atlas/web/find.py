"""The Find page (Phase 3 spec 6.1) and BLE unclaim from the device page.

``GET /find`` and ``GET /partials/find`` and ``POST /find/claim`` are ``async def``:
the aggregator is not thread-safe, so candidates are copied on the event loop and
all DB work runs in a worker thread. The device-page unclaim is a plain DB write.

The banner shows only when Bluetooth is configured, the HA connection is down and has
been for more than ``BANNER_AFTER``. The client stamps ``since`` at start, so a
never-connected client counts from startup; ``since is None`` (not started) shows none.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from lanscape_atlas.service import Service
from lanscape_atlas.service import ble_queries as bq
from lanscape_atlas.service import queries as q
from lanscape_atlas.web.ble_api import age_seconds, ago, candidate_dict, require_candidate, where
from lanscape_atlas.web.pages import (
    ACTOR,
    DEVICE_LIMIT,
    MAX_ID,
    Form,
    PathId,
    _clean,
    _message,
    _redirect,
    _service,
    _text,
    render,
)

BANNER_AFTER = timedelta(minutes=2)
STALE_MESSAGE = "That device is no longer in range; nothing was claimed."
PICK_MESSAGE = "Choose a candidate to claim."
BADGE_LABELS = {
    "steady": "Steady",
    "intermittent": "Intermittent",
    "not_heard_24h": "Not heard in 24 h",
}
INTERMITTENT_HINT = "Heard only sometimes — pressed, idle, or throttled"

router = APIRouter(include_in_schema=False)


@dataclass(frozen=True)
class RowView:
    device_id: int
    name: str | None
    type: str | None
    where: str | None
    age: str | None
    rssi: int | None
    badge: str
    badge_label: str


def _row_view(row: bq.FindRow, now: Any) -> RowView:
    age = age_seconds(row, now)
    return RowView(
        device_id=row.device_id,
        name=row.name,
        type=row.type,
        where=where(row.last) if row.last else None,
        age=None if age is None else ago(age),
        rssi=row.last.rssi if row.last else None,
        badge=row.badge,
        badge_label=BADGE_LABELS[row.badge],
    )


def _banner(request: Request, now: Any) -> str | None:
    runtime = request.app.state.bluetooth
    if runtime is None:
        return None
    status = runtime.status()
    if status.connected or status.since is None or now - status.since <= BANNER_AFTER:
        return None
    text = "Home Assistant is unreachable — Bluetooth sightings are stale."
    return f"{text} {status.last_error}" if status.last_error else text


def _db_context(svc: Service, search: str | None, with_candidates: bool) -> dict[str, Any]:
    now = svc.clock()
    devices: list[Any] = []
    scanners: dict[str, tuple[str | None, str | None]] = {}
    with svc.lock:
        rows = bq.find(svc.conn, search, now=now)
        if with_candidates:
            devices = q.list_devices(
                svc.conn, limit=DEVICE_LIMIT, window_min=svc.ip_match_window_min
            )
            scanners = bq.scanner_labels(svc.conn)
    return {"rows": [_row_view(r, now) for r in rows], "devices": devices, "scanners": scanners}


async def _context(request: Request, search: str | None, *, full: bool) -> dict[str, Any]:
    svc = _service(request)
    runtime = request.app.state.bluetooth
    now = svc.clock()
    # Aggregator: loop only. Copy the candidates before leaving the loop.
    found = [] if runtime is None else list(runtime.aggregator.candidates())
    ctx = await run_in_threadpool(_db_context, svc, search, full and bool(found))
    scanners = ctx.pop("scanners")
    candidates = []
    for c in found:
        d = candidate_dict(c)
        d["age"] = ago(max(0, int((now - c.last_heard).total_seconds())))
        d["scanner_name"], d["scanner_room"] = scanners.get(c.source, (None, None))
        candidates.append(d)
    ctx.update(
        configured=runtime is not None,
        banner=_banner(request, now),
        candidates=candidates,
        q=search or "",
        intermittent_hint=INTERMITTENT_HINT,
    )
    return ctx


def _find_page(
    request: Request,
    ctx: dict[str, Any],
    flash: tuple[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return render(request, "find.html", {"nav": "find", "flash": flash, **ctx}, status_code)


@router.get("/find", response_class=HTMLResponse)
async def find_page(
    request: Request,
    q: str | None = None,
    claimed: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
) -> Response:
    ctx = await _context(request, q, full=True)
    flash = None
    if claimed is not None:
        name = await run_in_threadpool(_device_name, _service(request), claimed)
        flash = ("ok", f"Claimed as {name}.")
    return _find_page(request, ctx, flash)


@router.get("/partials/find", response_class=HTMLResponse)
async def find_partial(request: Request, q: str | None = None) -> Response:
    ctx = await _context(request, q, full=False)
    return render(request, "_find_rows.html", ctx)


def _device_name(svc: Service, device_id: int) -> str:
    with svc.lock:
        row = svc.conn.execute("SELECT name FROM device WHERE id = ?", (device_id,)).fetchone()
    return (row[0] if row and row[0] else f"device {device_id}") if row else "a device"


def _claim_args(form: FormData) -> tuple[int | None, str | None, str | None]:
    """``(device_id, new_name, new_type)`` from the claim form; ``ValueError`` if unusable."""
    raw = (_text(form, "device_id") or "").strip()
    name = _clean(_text(form, "new_name"))
    kind = _clean(_text(form, "new_type"))
    if raw and name:
        raise ValueError("Choose an existing device or give a new name, not both.")
    if raw:
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_ID:
            raise ValueError("Choose a device to attach to.")
        return int(raw), None, None
    if not name or not kind:
        raise ValueError("Choose an existing device, or give a name and a type for a new one.")
    return None, name, kind


@router.post("/find/claim", response_model=None)
async def find_claim(request: Request, form: Form) -> Response:
    svc = _service(request)
    runtime = request.app.state.bluetooth
    try:
        device_id, new_name, new_type = _claim_args(form)
        key = (_text(form, "identity") or "").strip()
        if not key:
            raise ValueError(PICK_MESSAGE)
        try:
            identity = require_candidate(runtime, key)
        except ValueError:
            raise ValueError(STALE_MESSAGE) from None
        claimed, _ = await run_in_threadpool(
            lambda: svc.claim_ble(
                ACTOR, identity, device_id=device_id, new_name=new_name, new_type=new_type
            )
        )
    except ValueError as e:
        ctx = await _context(request, None, full=True)
        return _find_page(request, ctx, ("error", _message(e)), 422)
    return _redirect(request, f"/find?claimed={claimed}")


@router.post("/devices/{device_id}/ble/{identity_id}/unclaim", response_model=None)
def unclaim(request: Request, device_id: PathId, identity_id: PathId, form: Form) -> Response:
    svc = _service(request)
    with svc.lock:
        owned = {i["id"] for i in bq.identities(svc.conn, device_id)}
    if identity_id not in owned:
        raise KeyError(f"device {device_id} has no Bluetooth identity {identity_id}")
    svc.unclaim_ble(ACTOR, identity_id)
    return _redirect(request, f"/devices/{device_id}")
