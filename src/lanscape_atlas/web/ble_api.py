"""Bluetooth JSON API under ``/api/v1``: find, candidates, claim, identities, scanners.

Writes go through ``Service`` with ``actor="user"``. Bodies are strict (unknown
fields rejected, ids not coerced). Errors are mapped by the app-level handlers in
``app.py`` (KeyError 404, ValueError 422, UndoConflict 409).

``GET /ble/candidates`` and ``POST /ble/claim`` are ``async def``: the aggregator
is not thread-safe and is only touched on the event loop; their DB work runs in a
worker thread so the loop never blocks on ``service.lock``.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from lanscape_atlas.bluetooth.aggregator import Candidate
from lanscape_atlas.bluetooth.match import Identity
from lanscape_atlas.bluetooth.runtime import BluetoothRuntime
from lanscape_atlas.clock import iso
from lanscape_atlas.service import Service
from lanscape_atlas.service import ble_queries as bq

ACTOR = "user"

router = APIRouter(prefix="/api/v1")


def _service(request: Request) -> Service:
    svc: Service = request.app.state.service
    return svc


def candidate_dict(c: Candidate) -> dict[str, Any]:
    return {
        "identity": c.identity.key(),
        "name": c.name,
        "best_rssi": c.best_rssi,
        "source": c.source,
        "first_heard": iso(c.first_heard),
        "last_heard": iso(c.last_heard),
        "adverts": c.adverts,
        "typical_gap_s": c.typical_gap_s,
    }


def find_row_dict(row: bq.FindRow) -> dict[str, Any]:
    last = None
    if row.last is not None:
        last = {**asdict(row.last), "at": iso(row.last.at)}
    return {
        "device_id": row.device_id,
        "name": row.name,
        "type": row.type,
        "last": last,
        "badge": row.badge,
    }


def scanner_dict(s: bq.ScannerView) -> dict[str, Any]:
    d = asdict(s)
    d["last_seen"] = iso(s.last_seen)
    return d


def age_seconds(row: bq.FindRow, now: datetime) -> int | None:
    if row.last is None:
        return None
    return max(0, int((now - row.last.at).total_seconds()))


def ago(seconds: int) -> str:
    """Human age: under a minute "just now", under an hour "N min ago", else "H h ago"."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def where(last: bq.LastHeard) -> str:
    """The room a sighting was in, else ``near <scanner>``."""
    return last.place_name or f"near {last.scanner_name or last.scanner_source}"


def require_candidate(runtime: BluetoothRuntime | None, key: str) -> Identity:
    """The parsed identity if it is a current candidate; else ``ValueError``."""
    identity = Identity.parse(key)
    if runtime is None or not any(c.identity == identity for c in runtime.aggregator.candidates()):
        raise ValueError("no such candidate")
    return identity


class NewDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(max_length=200)]
    type: Annotated[str, Field(max_length=100)]


class ClaimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: Annotated[str, Field(max_length=200)]
    device_id: StrictInt | None = None
    new: NewDevice | None = None

    @model_validator(mode="after")
    def _one_target(self) -> ClaimBody:
        if (self.device_id is None) == (self.new is None):
            raise ValueError("give exactly one of device_id or new")
        return self


class ScannerLinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: StrictInt | None


@router.get("/find")
def find(request: Request, q: str | None = None) -> list[dict[str, Any]]:
    svc = _service(request)
    with svc.lock:
        rows = bq.find(svc.conn, q, now=svc.clock())
    return [find_row_dict(r) for r in rows]


@router.get("/ble/candidates")
async def candidates(request: Request) -> dict[str, Any]:
    runtime: BluetoothRuntime | None = request.app.state.bluetooth
    if runtime is None:
        return {"connected": None, "candidates": []}
    return {
        "connected": runtime.status().connected,
        "candidates": [candidate_dict(c) for c in runtime.aggregator.candidates()],
    }


@router.post("/ble/claim")
async def claim(request: Request, body: ClaimBody) -> dict[str, int]:
    svc = _service(request)
    identity = require_candidate(request.app.state.bluetooth, body.identity)
    new = body.new
    device_id, batch_id = await asyncio.to_thread(
        lambda: svc.claim_ble(
            ACTOR,
            identity,
            device_id=body.device_id,
            new_name=None if new is None else new.name,
            new_type=None if new is None else new.type,
        )
    )
    return {"device_id": device_id, "batch_id": batch_id}


@router.delete("/ble/identities/{identity_id}")
def unclaim(request: Request, identity_id: int) -> dict[str, int]:
    return {"batch_id": _service(request).unclaim_ble(ACTOR, identity_id)}


@router.get("/ble/scanners")
def scanners(request: Request) -> list[dict[str, Any]]:
    svc = _service(request)
    with svc.lock:
        views = bq.scanners(svc.conn)
    return [scanner_dict(s) for s in views]


@router.put("/ble/scanners/{scanner_id}/device")
def link_scanner(request: Request, scanner_id: int, body: ScannerLinkBody) -> dict[str, int]:
    return {"batch_id": _service(request).link_scanner(ACTOR, scanner_id, body.device_id)}
