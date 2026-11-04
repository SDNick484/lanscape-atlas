"""Floor plan JSON API under ``/api/v1/floorplan``.

Writes go through ``Service`` with ``actor="user"`` (the ``/api/v1`` convention).
Request bodies are strict: unknown fields are rejected (so a client cannot name
its own actor) and ids and flags are not coerced. Errors are mapped by the
app-level handlers in ``app.py`` (KeyError 404, ValueError 422, StaleWrite 409).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt

from lanscape_atlas.service import Placement, Service
from lanscape_atlas.service import queries as q

ACTOR = "user"

router = APIRouter(prefix="/api/v1/floorplan")

MAX_ITEMS = 1000
STALE_MESSAGE = "Some devices changed since this page loaded. Reload to see the latest values."
Number = StrictInt | StrictFloat


def _service(request: Request) -> Service:
    svc: Service = request.app.state.service
    return svc


class RoomBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(max_length=200)]
    polygon: Annotated[list[Any], Field(max_length=MAX_ITEMS)]


class PlacementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: StrictInt
    place_id: StrictInt | None
    x: Number | None = None
    y: Number | None = None
    roaming: StrictBool = False


class PlacementsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placements: Annotated[list[PlacementBody], Field(max_length=MAX_ITEMS)]
    based_on: dict[str, StrictInt] = Field(default_factory=dict)


@router.get("")
def get_floorplan(request: Request) -> dict[str, Any]:
    svc = _service(request)
    with svc.lock:
        return q.floor_plan_view(svc.conn, now=svc.clock())


@router.put("/rooms")
def put_room(request: Request, body: RoomBody) -> dict[str, int]:
    return {"batch_id": _service(request).save_room(ACTOR, body.name, body.polygon)}


def apply_placements(svc: Service, actor: str, body: PlacementsBody) -> int:
    """``Service.place_devices`` for a validated body; shared with the floor plan page."""
    try:
        based_on = {int(k): v for k, v in body.based_on.items()}
    except ValueError:
        raise ValueError("based_on keys must be device ids") from None
    placements = [
        Placement(
            p.device_id,
            p.place_id,
            None if p.x is None else float(p.x),
            None if p.y is None else float(p.y),
            p.roaming,
        )
        for p in body.placements
    ]
    return svc.place_devices(actor, placements, based_on)


@router.post("/placements")
def post_placements(request: Request, body: PlacementsBody) -> dict[str, int]:
    return {"batch_id": apply_placements(_service(request), ACTOR, body)}
