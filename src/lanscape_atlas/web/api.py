"""JSON API under ``/api/v1``, mirroring the service layer (spec section 8.2).

Writes go through ``Service`` with ``actor="user"``. Reads take ``service.lock``
(one shared connection). Handlers are plain ``def`` so FastAPI runs them in its
threadpool; only deep scan is ``async`` because it starts an asyncio task.
Query dataclasses are returned via ``dataclasses.asdict``. Errors are mapped by
the app-level handlers in ``app.py`` (KeyError 404, ValueError 422, UndoConflict
and ScanRefused 409).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lanscape_atlas.service import Service
from lanscape_atlas.service import queries as q
from lanscape_atlas.web.deep_scan import start_deep_scan
from lanscape_atlas.web.topology import (
    MAX_ID,
    BodyId,
    Graph,
    LayoutBody,
    LinkBody,
    PlaceholderBody,
    add_link,
    create_placeholder,
    save_layout,
    topology_graph,
)

ACTOR = "user"

router = APIRouter(prefix="/api/v1")

# MAX_ID: sqlite INTEGER range. Out of range -> 422, never an OverflowError 500.
PathId = Annotated[int, Path(ge=1, le=MAX_ID)]
QueryId = Annotated[int | None, Query(ge=1, le=MAX_ID)]


def _service(request: Request) -> Service:
    svc: Service = request.app.state.service
    return svc


# ------------------------------------------------------------------ bodies


class PatchDeviceBody(BaseModel):
    fields: dict[str, Any]


class ClaimBody(BaseModel):
    device_id: BodyId


class PlaceBody(BaseModel):
    name: str


# ----------------------------------------------------------------- devices


@router.get("/devices")
def list_devices(
    request: Request,
    online: bool | None = None,
    type: str | None = None,
    trust: str | None = None,
    place: str | None = None,
    ap_node: str | None = None,
    q_: str | None = Query(None, alias="q"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    svc = _service(request)
    with svc.lock:
        rows = q.list_devices(
            svc.conn,
            online=online,
            type=type,
            trust=trust,
            place=place,
            ap_node=ap_node,
            q=q_,
            limit=limit,
            offset=offset,
            window_min=svc.ip_match_window_min,
        )
    return [asdict(r) for r in rows]


@router.post("/devices/placeholder")
def post_placeholder(request: Request, body: PlaceholderBody) -> dict[str, int]:
    return create_placeholder(_service(request), ACTOR, body)


@router.get("/devices/{device_id}")
def get_device(request: Request, device_id: PathId) -> dict[str, Any]:
    svc = _service(request)
    with svc.lock:
        view = q.get_device(svc.conn, device_id, window_min=svc.ip_match_window_min)
    return asdict(view)


@router.patch("/devices/{device_id}")
def patch_device(request: Request, device_id: PathId, body: PatchDeviceBody) -> dict[str, int]:
    return {"batch_id": _service(request).update_device(ACTOR, device_id, body.fields)}


@router.put("/devices/{device_id}/place")
def set_place(request: Request, device_id: PathId, body: PlaceBody) -> dict[str, int]:
    return {"batch_id": _service(request).set_place(ACTOR, device_id, body.name)}


@router.post("/interfaces/{interface_id}/claim")
def claim_interface(request: Request, interface_id: PathId, body: ClaimBody) -> dict[str, int]:
    return {"batch_id": _service(request).claim_interface(ACTOR, interface_id, body.device_id)}


# --------------------------------------------------------------- deep scan


@router.post("/devices/{device_id}/deep-scan", status_code=202)
async def deep_scan(request: Request, device_id: PathId) -> JSONResponse:
    # Refusals (409) and unknown devices (404) surface now, via the app handlers.
    start_deep_scan(request.app.state, device_id)
    return JSONResponse({"status": "started"}, status_code=202)


# ------------------------------------------------------- services / changes


@router.get("/services")
def list_services(
    request: Request,
    device_id: QueryId = None,
    port: Annotated[int | None, Query(ge=0, le=65535)] = None,
) -> list[dict[str, Any]]:
    svc = _service(request)
    with svc.lock:
        rows = q.list_services(svc.conn, device_id, port)
    return [asdict(r) for r in rows]


@router.get("/changes")
def list_changes(
    request: Request, limit: int = Query(50, ge=1, le=1000), actor: str | None = None
) -> list[dict[str, Any]]:
    svc = _service(request)
    with svc.lock:
        rows = q.recent_changes(svc.conn, limit=limit, actor=actor)
    return [asdict(r) for r in rows]


@router.post("/changes/{batch_id}/undo")
def undo(request: Request, batch_id: PathId) -> dict[str, int]:
    return {"batch_id": _service(request).undo(ACTOR, batch_id)}


# ------------------------------------------------------------------- links


@router.post("/links")
def post_link(request: Request, body: LinkBody) -> dict[str, int]:
    return add_link(_service(request), ACTOR, body)


@router.delete("/links/{link_id}")
def remove_link(request: Request, link_id: PathId) -> dict[str, int]:
    return {"batch_id": _service(request).remove_link(ACTOR, link_id)}


# ---------------------------------------------------------------- topology


@router.get("/topology")
def get_topology(request: Request) -> Graph:
    """Nodes and deduplicated edges; see ``web.topology.topology_graph``."""
    svc = _service(request)
    with svc.lock:
        return topology_graph(svc.conn)


@router.put("/topology/layout")
def put_layout(request: Request, body: LayoutBody) -> dict[str, int]:
    """Upsert saved node positions (``web.topology.save_layout``)."""
    return {"updated": save_layout(_service(request), body.positions)}


# -------------------------------------------------------------- collectors


@router.get("/collectors")
def list_collectors(request: Request) -> list[dict[str, Any]]:
    """``collector_status`` rows; ``last_error`` is already redacted by the runner."""
    svc = _service(request)
    with svc.lock:
        return q.collector_status(svc.conn)
