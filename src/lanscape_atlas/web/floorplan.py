"""The floor plan page (spec section 4): ``/floorplan`` and its CSRF-protected writes.

``GET /floorplan`` renders the plan image with an inline SVG overlay (rooms and placed
devices, drawn server-side and redrawn by ``static/floorplan.js``), the device trays
and the layout tools. The page data travels in a ``<script type="application/json">``
block with every ``<`` escaped, so no stored name can end the script element.

Writes:

- ``POST /floorplan/plan``: multipart upload (form field ``file``). The body is read
  with a size bound before it is parsed; the extension comes from the file name
  (lower-cased, ``jpeg`` -> ``jpg``). Order: ``validate_upload``, ``read_dimensions``,
  ``store_plan_file``, ``Service.set_floor_plan``; if the last step fails a file the
  upload created is removed again, so a rejected upload never leaves a new file.
- ``POST /floorplan/rooms`` (form ``name``, ``polygon`` JSON text) and
  ``POST /floorplan/rooms/{id}/name`` (form ``new_name``): 303 back to the page with
  a flash, or the page re-rendered with a 422 flash.
- ``POST /floorplan/placements``: JSON in the ``/api/v1/floorplan/placements`` shape,
  token in ``X-CSRF-Token``; errors are JSON (``app._wants_json``), and a stale
  ``based_on`` answers 409 with the reload message (``app._stale_write``).

``GET /floorplan/search?q=`` (spec §4.2, same-origin page route like
``/topology/search``): devices ranked by ``web.search`` (same matcher, caps and order as
Topology), then rooms whose name contains ``q`` ignoring case (a literal substring
test, so ``%`` and ``_`` are not wildcards), name-prefix first. Every result carries
``kind``: ``{"kind": "device", "device_id", "name", "type"}`` or
``{"kind": "room", "place_id", "name"}``.

``GET /floorplan/plan.<ext>`` serves only ``data_dir/floorplan/<file_name>`` with the
file name from the database, never a path from the request.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from lanscape_atlas.floorplan.image import (
    MAX_BYTES,
    read_dimensions,
    store_plan_file,
    validate_upload,
)
from lanscape_atlas.service import Service
from lanscape_atlas.service import queries as q
from lanscape_atlas.web.floorplan_api import (
    STALE_MESSAGE,
    PlacementsBody,
    RoomBody,
    apply_placements,
)
from lanscape_atlas.web.pages import (
    ACTOR,
    MAX_ID,
    CsrfHeader,
    Form,
    _redirect,
    csrf_form,
    render,
)
from lanscape_atlas.web.search import SEARCH_LIMIT, SearchQuery, search_devices

router = APIRouter(include_in_schema=False)

PathId = Annotated[int, PathParam(ge=1, le=MAX_ID)]

# Multipart framing around the file; anything beyond this is refused unparsed.
UPLOAD_LIMIT = MAX_BYTES + 64 * 1024
CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}
IMAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",
}
SAVED_MESSAGES = {
    "plan": "Floor plan saved.",
    "room": "Room saved",
    "rename": "Room renamed.",
    "placements": "Changes saved.",
}
UNNAMED = "(unnamed)"


def _service(request: Request) -> Service:
    svc: Service = request.app.state.service
    return svc


def _data_dir(request: Request) -> Path:
    data_dir: Path = request.app.state.settings.data_dir
    return data_dir


def _message(exc: BaseException) -> str:
    return str(exc.args[0]) if exc.args else type(exc).__name__


# ---------------------------------------------------------------- page model


def _label(name: str | None) -> str:
    return name.strip() if name and name.strip() else UNNAMED


def _plan_url(root: str, plan: dict[str, Any]) -> str:
    ext = str(plan["file_name"]).rsplit(".", 1)[-1]
    return f"{root}/floorplan/plan.{ext}?" + urlencode({"v": plan["updated_at"] or ""})


def page_data(view: dict[str, Any], root: str) -> dict[str, Any]:
    """The JSON the page script reads: ``floor_plan_view`` minus the file name."""
    plan = view["plan"]
    return {
        "plan": None
        if plan is None
        else {"width": plan["width"], "height": plan["height"], "url": _plan_url(root, plan)},
        "rooms": view["rooms"],
        "placed": view["placed"],
        "roaming": view["roaming"],
        "unplaced": view["unplaced"],
    }


def script_json(data: Any) -> str:
    """JSON safe inside ``<script type="application/json">``: no ``<`` survives raw.

    A superset of the ``"</" -> "<\\/"`` rule: it also covers ``<!--``, which can
    otherwise switch the HTML tokenizer into a state where ``</script>`` is ignored.
    """
    return json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")


def _svg_model(view: dict[str, Any]) -> dict[str, Any] | None:
    """Pixel-space shapes for the server-rendered overlay (same as the script draws)."""
    plan = view["plan"]
    if plan is None:
        return None
    w, h = int(plan["width"]), int(plan["height"])
    rooms = []
    for r in view["rooms"]:
        poly = r["polygon"]
        if not poly:
            continue
        points = " ".join(f"{x * w:.2f},{y * h:.2f}" for x, y in poly)
        cx = sum(x for x, _ in poly) / len(poly) * w
        cy = sum(y for _, y in poly) / len(poly) * h
        rooms.append({"id": r["id"], "name": r["name"], "points": points, "cx": cx, "cy": cy})
    markers = [
        {
            "id": d["device_id"],
            "label": _label(d["name"]),
            "cx": d["x"] * w,
            "cy": d["y"] * h,
            "online": d["online"],
        }
        for d in view["placed"]
    ]
    unit = max(w, h) / 100
    return {"w": w, "h": h, "unit": unit, "rooms": rooms, "markers": markers}


def _trays(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Tray groups: one per room (by id) with roaming or point-less devices, then Unassigned."""
    by_room: dict[int, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    for d in view["roaming"]:
        by_room.setdefault(d["place_id"], []).append({**d, "roaming": True})
    for d in view["unplaced"]:
        entry = {**d, "roaming": False}
        if d["place_id"] is None:
            unassigned.append(entry)
        else:
            by_room.setdefault(d["place_id"], []).append(entry)
    heard_in: dict[int, list[dict[str, Any]]] = {}
    for d in [*view["placed"], *view["roaming"], *view["unplaced"]]:
        h = d.get("heard")
        if h and h["place_id"] is not None:
            heard_in.setdefault(h["place_id"], []).append(d)
    groups = []
    for r in view["rooms"]:
        items = by_room.get(r["id"], [])
        items.sort(key=lambda e: e["device_id"])
        trayed = {e["device_id"] for e in items}
        extra = sorted(
            (
                {"device_id": d["device_id"], "name": d["name"], "heard_only": True}
                for d in heard_in.get(r["id"], [])
                if d["device_id"] not in trayed
            ),
            key=lambda e: e["device_id"],
        )
        if items or extra:
            groups.append(
                {"room_id": r["id"], "name": r["name"], "devices": items, "heard_only": extra}
            )
    if unassigned:
        groups.append({"room_id": None, "name": "Unassigned", "devices": unassigned})
    for g in groups:
        g.setdefault("heard_only", [])
        for e in [*g["devices"], *g["heard_only"]]:
            e["label"] = _label(e["name"])
    return groups


def _flash(request: Request) -> tuple[str, str] | None:
    params = request.query_params
    if params.get("stale"):
        return ("error", STALE_MESSAGE)
    msg = SAVED_MESSAGES.get(params.get("saved", ""))
    if msg is None:
        return None
    if params.get("saved") == "room":
        cleared = params.get("cleared", "")
        n = int(cleared) if cleared.isdigit() else 0
        if n:
            msg += f"; {n} device point{'s' if n != 1 else ''} cleared"
        msg += "."
    return ("ok", msg)


def _page(
    request: Request, flash: tuple[str, str] | None = None, status_code: int = 200
) -> HTMLResponse:
    svc = _service(request)
    with svc.lock:
        view = q.floor_plan_view(svc.conn, now=svc.clock())
    root = request.scope.get("root_path", "")
    data = page_data(view, root)
    return render(
        request,
        "floorplan.html",
        {
            "nav": "floorplan",
            "plan": data["plan"],
            "rooms": view["rooms"],
            "svg": _svg_model(view),
            "trays": _trays(view),
            "data_json": script_json(data),
            "flash": flash,
        },
        status_code,
    )


# --------------------------------------------------------------------- routes


@router.get("/floorplan", response_class=HTMLResponse)
def floorplan_page(request: Request) -> HTMLResponse:
    return _page(request, _flash(request))


def search_rooms(
    conn: sqlite3.Connection, q: str, *, limit: int = SEARCH_LIMIT
) -> list[dict[str, Any]]:
    """Rooms whose name contains ``q`` (case-insensitive, literal), prefix matches first.

    The caller holds ``service.lock``. A blank ``q`` gives ``[]``.
    """
    text = q.strip().casefold()
    if not text:
        return []
    hits = []
    for place_id, name in conn.execute("SELECT id, name FROM place"):
        folded = str(name).casefold()
        if text in folded:
            hits.append((0 if folded.startswith(text) else 1, folded, int(place_id), str(name)))
    hits.sort()
    return [{"kind": "room", "place_id": h[2], "name": h[3]} for h in hits[:limit]]


@router.get("/floorplan/search")
def floorplan_search(request: Request, q: SearchQuery = "") -> list[dict[str, Any]]:
    """Ranked devices (``web.search``) then matching rooms, for the page's search box."""
    svc = _service(request)
    with svc.lock:
        devices = search_devices(svc.conn, q, window_min=svc.ip_match_window_min)
        rooms = search_rooms(svc.conn, q)
    return [{"kind": "device", **d} for d in devices] + rooms


@router.get("/floorplan/plan.{ext}", response_model=None)
def plan_image(request: Request, ext: str) -> Response:
    svc = _service(request)
    with svc.lock:
        row = svc.conn.execute("SELECT file_name FROM floor_plan WHERE id = 1").fetchone()
    name = str(row[0]) if row is not None else ""
    stored_ext = name.rsplit(".", 1)[-1] if "." in name else ""
    path = _data_dir(request) / "floorplan" / name
    if (
        not name
        or Path(name).name != name
        or stored_ext != ext
        or stored_ext not in CONTENT_TYPES
        or not path.is_file()
    ):
        raise KeyError("no floor plan image")
    return FileResponse(path, media_type=CONTENT_TYPES[stored_ext], headers=IMAGE_HEADERS)


async def _read_bounded(request: Request) -> None:
    """Buffer the request body, refusing (ValueError) more than ``UPLOAD_LIMIT`` bytes.

    Starlette's ``request.form()`` then parses the buffered body.
    """
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > UPLOAD_LIMIT:
        raise ValueError("image is larger than 10 MiB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > UPLOAD_LIMIT:
            raise ValueError("image is larger than 10 MiB")
        chunks.append(chunk)
    request._body = b"".join(chunks)


def _upload_ext(filename: str | None) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return "jpg" if ext == "jpeg" else ext


def _save_plan(svc: Service, data_dir: Path, data: bytes, ext: str) -> None:
    validate_upload(data, ext)
    width, height = read_dimensions(data, ext)
    target = data_dir / "floorplan" / f"{hashlib.sha256(data).hexdigest()}.{ext}"
    existed = target.exists()
    digest, file_name = store_plan_file(data_dir, data, ext)
    try:
        svc.set_floor_plan(ACTOR, width=width, height=height, sha256=digest, file_name=file_name)
    except BaseException:
        if not existed:
            target.unlink(missing_ok=True)
        raise


@router.post("/floorplan/plan", response_model=None)
async def upload_plan(request: Request) -> Response:
    try:
        await _read_bounded(request)
    except ValueError as e:
        return await run_in_threadpool(_page, request, ("error", _message(e)), 422)
    form = await csrf_form(request)
    upload = form.get("file")
    try:
        if not isinstance(upload, UploadFile):
            raise ValueError("choose an image file to upload")
        data = await upload.read()
        ext = _upload_ext(upload.filename)
        await run_in_threadpool(_save_plan, _service(request), _data_dir(request), data, ext)
    except ValueError as e:
        return await run_in_threadpool(_page, request, ("error", _message(e)), 422)
    return _redirect(request, "/floorplan?saved=plan")


def _text(form: Any, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


@router.post("/floorplan/rooms", response_model=None)
def save_room(request: Request, form: Form) -> Response:
    svc = _service(request)
    try:
        try:
            polygon = json.loads(_text(form, "polygon"))
        except json.JSONDecodeError:
            raise ValueError("polygon must be JSON: a list of [x, y] pairs") from None
        try:
            body = RoomBody(name=_text(form, "name").strip(), polygon=polygon)
        except ValidationError:
            raise ValueError("room name (up to 200 characters) and polygon are required") from None
        batch_id = svc.save_room(ACTOR, body.name, body.polygon)
    except ValueError as e:
        return _page(request, ("error", _message(e)), 422)
    with svc.lock:
        cleared = svc.conn.execute(
            "SELECT count(*) FROM change WHERE batch_id = ? AND entity = 'device'"
            " AND field = 'pos_x'",
            (batch_id,),
        ).fetchone()[0]
    return _redirect(request, f"/floorplan?saved=room&cleared={int(cleared)}")


@router.post("/floorplan/rooms/{place_id}/name", response_model=None)
def rename_room(request: Request, place_id: PathId, form: Form) -> Response:
    try:
        _service(request).rename_room(ACTOR, place_id, _text(form, "new_name").strip())
    except ValueError as e:
        return _page(request, ("error", _message(e)), 422)
    return _redirect(request, "/floorplan?saved=rename")


@router.post("/floorplan/placements", dependencies=[CsrfHeader])
def save_placements(request: Request, body: PlacementsBody) -> dict[str, int]:
    return {"batch_id": apply_placements(_service(request), ACTOR, body)}
