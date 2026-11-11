"""Server-rendered web UI (spec section 8.1): Jinja2 templates plus HTMX.

Pages are not behind the API token (the reverse proxy authenticates), so every
state-changing POST carries a CSRF token (Ruling 34): a random ``atlas_csrf``
cookie set on page GETs, echoed as a hidden ``csrf`` form field or an
``X-CSRF-Token`` header (HTMX ``hx-headers``), compared in constant time.

Reads go through ``service.queries`` under ``service.lock``; writes go through
``Service`` with ``actor="user"``. Service errors are rendered as HTML here
(ValueError 422 inline, UndoConflict and ScanRefused 409 inline); anything left
over falls through to the app-level handlers, which render HTML for page paths.

The topology page (Task 17) is the exception: its ``/topology/*`` endpoints take
and return JSON (CSRF token in ``X-CSRF-Token`` only) and their errors are JSON,
with the same 404/422/409 mapping as ``/api``.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from lanscape_atlas.collectors.nmap import ScanRefused
from lanscape_atlas.service import Placement, Service, StaleWrite, UndoConflict
from lanscape_atlas.service import queries as q
from lanscape_atlas.web.deep_scan import start_deep_scan
from lanscape_atlas.web.floorplan_api import STALE_MESSAGE
from lanscape_atlas.web.topology import (
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
CSRF_COOKIE = "atlas_csrf"
CSRF_FIELD = "csrf"
CSRF_HEADER = "x-csrf-token"
# token_urlsafe(32) is 43 chars; shorter cookies were not minted here and are refused.
CSRF_MIN_LEN = 32

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

MAX_ID = 2**63 - 1
PathId = Annotated[int, PathParam(ge=1, le=MAX_ID)]

# Device page form: free-text fields ("" clears) and the two enumerations.
TEXT_FIELDS = ("name", "type", "icon", "owner", "notes")
CHOICE_FIELDS = ("trust", "scan_policy")
TRUST_CHOICES = ("new", "known", "unwanted")
SCAN_POLICY_CHOICES = ("normal", "never")
DEVICE_LIMIT = 1000
HISTORY_LIMIT = 500

router = APIRouter(include_in_schema=False)


def _service(request: Request) -> Service:
    svc: Service = request.app.state.service
    return svc


# --------------------------------------------------------------- templating


def _fmt_value(value: Any) -> str:
    if value is None:
        return "∅"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _day(ts: str | None) -> str:
    return ts[:10] if ts else ""


def _short_ts(ts: str | None) -> str:
    """``2026-01-01T00:09:45Z`` -> ``2026-01-01 00:09``."""
    if not ts:
        return ""
    return ts[:16].replace("T", " ")


templates.env.filters["fmt_value"] = _fmt_value
templates.env.filters["day"] = _day
templates.env.filters["ts"] = _short_ts


def _csrf_token(request: Request) -> tuple[str, bool]:
    """The request's CSRF token and whether it is new (cookie must be set)."""
    cached: str | None = getattr(request.state, "csrf", None)
    existing = request.cookies.get(CSRF_COOKIE)
    if cached is not None:
        return cached, cached != existing
    if existing and len(existing) >= CSRF_MIN_LEN:
        token, new = existing, False
    else:
        token, new = secrets.token_urlsafe(32), True
    request.state.csrf = token
    return token, new


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    token, new = _csrf_token(request)
    ctx = {"csrf": token, "root": request.scope.get("root_path", ""), "flash": None}
    ctx.update(context or {})
    response: HTMLResponse = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if new:
        response.set_cookie(
            CSRF_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            path="/",
            secure=request.url.scheme == "https",
        )
    return response


def error_page(request: Request, status_code: int, message: str) -> HTMLResponse:
    """A full HTML error page (used by the app-level handlers for page paths)."""
    return render(request, "error.html", {"status": status_code, "message": message}, status_code)


def _redirect(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse(request.scope.get("root_path", "") + path, status_code=303)


def _message(exc: BaseException) -> str:
    return str(exc.args[0]) if exc.args else type(exc).__name__


# --------------------------------------------------------------------- CSRF


def _check_csrf(request: Request, sent: str) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if (
        len(cookie) < CSRF_MIN_LEN
        or not sent
        or not hmac.compare_digest(cookie.encode(), sent.encode())
    ):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")


async def csrf_form(request: Request) -> FormData:
    """Dependency for every form page POST: parse the form and enforce the CSRF token."""
    form = await request.form()
    sent = form.get(CSRF_FIELD)
    if not isinstance(sent, str) or not sent:
        sent = request.headers.get(CSRF_HEADER, "")
    _check_csrf(request, sent)
    return form


def csrf_header(request: Request) -> None:
    """Dependency for JSON page POSTs: the token travels only in ``X-CSRF-Token``."""
    _check_csrf(request, request.headers.get(CSRF_HEADER, ""))


Form = Annotated[FormData, Depends(csrf_form)]
CsrfHeader = Depends(csrf_header)


def _text(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) else None


def _or(value: str | None, fallback: str) -> str:
    return fallback if value is None else value


def _clean(value: str | None) -> str | None:
    """Form text -> stored value: trimmed; empty clears the field."""
    if value is None:
        return None
    value = value.strip()
    return value or None


# ------------------------------------------------------------------ devices


@dataclass(frozen=True)
class DeviceFilters:
    online: bool | None = None
    type: str | None = None
    trust: str | None = None
    place: str | None = None
    ap_node: str | None = None
    q: str | None = None
    sort: str = "id"
    desc: bool = False
    raw: dict[str, str] = field(default_factory=dict)

    def query(self, **overrides: str) -> str:
        params = {k: v for k, v in {**self.raw, **overrides}.items() if v}
        return urlencode(params)


SORT_KEYS: dict[str, Callable[[q.DeviceSummary], Any]] = {
    "id": lambda d: d.id,
    "online": lambda d: (not d.online, d.id),
    "name": lambda d: ((d.name or "").lower(), d.id),
    "ipv4": lambda d: (d.ipv4 is None, int(ipaddress.IPv4Address(d.ipv4)) if d.ipv4 else 0),
    "vendor": lambda d: ((d.vendor or "").lower(), d.id),
    "place": lambda d: ((d.place or "").lower(), d.id),
    "services": lambda d: (d.service_count, d.id),
    "last_seen": lambda d: (d.last_seen, d.id),
}


def device_filters(
    online: Annotated[str | None, Query()] = None,
    type: Annotated[str | None, Query()] = None,
    trust: Annotated[str | None, Query()] = None,
    place: Annotated[str | None, Query()] = None,
    ap_node: Annotated[str | None, Query()] = None,
    q_: Annotated[str | None, Query(alias="q")] = None,
    sort: Annotated[str | None, Query()] = None,
    dir: Annotated[str | None, Query()] = None,
) -> DeviceFilters:
    """Same filters as ``GET /api/v1/devices``; empty values (from the GET form) mean none."""
    flag = (online or "").strip().lower()
    if flag not in ("", "true", "false"):
        raise HTTPException(status_code=422, detail="online must be true or false")
    sort_key = sort if sort in SORT_KEYS else "id"
    raw = {
        "online": flag,
        "type": (type or "").strip(),
        "trust": (trust or "").strip(),
        "place": (place or "").strip(),
        "ap_node": (ap_node or "").strip(),
        "q": (q_ or "").strip(),
        "sort": "" if sort_key == "id" else sort_key,
        "dir": "desc" if dir == "desc" else "",
    }
    return DeviceFilters(
        online=None if not flag else flag == "true",
        type=raw["type"] or None,
        trust=raw["trust"] or None,
        place=raw["place"] or None,
        ap_node=raw["ap_node"] or None,
        q=raw["q"] or None,
        sort=sort_key,
        desc=dir == "desc",
        raw=raw,
    )


Filters = Annotated[DeviceFilters, Depends(device_filters)]


def _devices(
    svc: Service, f: DeviceFilters
) -> tuple[list[q.DeviceSummary], dict[str, tuple[int, str | None]]]:
    """Filtered, sorted rows plus ``{AP MAC: (device id, name)}`` for the AP column."""
    with svc.lock:
        rows = q.list_devices(
            svc.conn,
            online=f.online,
            type=f.type,
            trust=f.trust,
            place=f.place,
            ap_node=f.ap_node,
            q=f.q,
            limit=DEVICE_LIMIT,
            window_min=svc.ip_match_window_min,
        )
        aps = q.ap_device_names(svc.conn, (d.ap_node for d in rows))
    return sorted(rows, key=SORT_KEYS[f.sort], reverse=f.desc), aps


@router.get("/", response_class=HTMLResponse)
def devices_page(request: Request, filters: Filters) -> HTMLResponse:
    devices, aps = _devices(_service(request), filters)
    return render(
        request,
        "devices.html",
        {"nav": "devices", "devices": devices, "aps": aps, "f": filters, "sort_keys": SORT_KEYS},
    )


@router.get("/partials/devices", response_class=HTMLResponse)
def devices_partial(request: Request, filters: Filters) -> HTMLResponse:
    devices, aps = _devices(_service(request), filters)
    return render(request, "_device_rows.html", {"devices": devices, "aps": aps, "f": filters})


# -------------------------------------------------------------- device page


def _form_values(view: q.DeviceView) -> dict[str, str]:
    values = {f: getattr(view, f) or "" for f in TEXT_FIELDS}
    values.update(place=view.place or "", trust=view.trust, scan_policy=view.scan_policy)
    return values


@dataclass(frozen=True)
class DevicePage:
    view: q.DeviceView
    aps: dict[str, tuple[int, str | None]]
    roaming: bool = False


def _device_page(
    request: Request,
    page: DevicePage,
    *,
    values: dict[str, str] | None = None,
    origs: dict[str, str] | None = None,
    flash: tuple[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    current = _form_values(page.view)
    return render(
        request,
        "device.html",
        {
            "nav": "devices",
            "d": page.view,
            "aps": page.aps,
            "roaming": page.roaming,
            "values": values or current,
            "origs": origs or current,
            "trust_choices": TRUST_CHOICES,
            "scan_policy_choices": SCAN_POLICY_CHOICES,
            "flash": flash,
        },
        status_code,
    )


def _load_view(svc: Service, device_id: int) -> DevicePage:
    with svc.lock:
        view = q.get_device(svc.conn, device_id, window_min=svc.ip_match_window_min)
        aps = q.ap_device_names(svc.conn, (i.ap_node for i in view.interfaces))
        row = svc.conn.execute("SELECT roaming FROM device WHERE id = ?", (device_id,)).fetchone()
    return DevicePage(view, aps, bool(row[0]))


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_page(request: Request, device_id: PathId, scan: str | None = None) -> HTMLResponse:
    page = _load_view(_service(request), device_id)
    flash = ("ok", "Deep scan started.") if scan == "started" else None
    return _device_page(request, page, flash=flash)


@router.post("/devices/{device_id}", response_model=None)
def device_save(request: Request, device_id: PathId, form: Form) -> Response:
    """Write only fields the user changed (submitted value != the value shown)."""
    svc = _service(request)
    current = _form_values(_load_view(svc, device_id).view)
    submitted = {k: _text(form, k) for k in current}
    origs = {k: _text(form, f"orig_{k}") for k in current}
    # Fields absent from the form are left alone; a missing orig_ falls back to the DB value.
    base: dict[str, str] = {k: _or(origs[k], current[k]) for k in current}
    changes: dict[str, Any] = {}
    for key in (*TEXT_FIELDS, *CHOICE_FIELDS):
        new = submitted[key]
        if new is None:
            continue
        if key in TEXT_FIELDS:
            if _clean(new) != _clean(base[key]):
                changes[key] = _clean(new)
        elif new != base[key]:
            changes[key] = new
    place = submitted["place"]
    new_place = _clean(place) if place is not None else _clean(base["place"])
    place_changed = new_place != _clean(base["place"])
    if place_changed and new_place is None:
        changes["place_id"] = None
    try:
        if changes:
            svc.update_device(ACTOR, device_id, changes)
        if place_changed and new_place is not None:
            svc.set_place(ACTOR, device_id, new_place)
    except ValueError as e:
        values = {k: _or(submitted[k], current[k]) for k in current}
        return _device_page(
            request,
            _load_view(svc, device_id),
            values=values,
            origs=base,
            flash=("error", _message(e)),
            status_code=422,
        )
    return _redirect(request, f"/devices/{device_id}")


@router.post("/devices/{device_id}/deep-scan", response_model=None)
async def device_deep_scan(request: Request, device_id: PathId, form: Form) -> Response:
    try:
        start_deep_scan(request.app.state, device_id)
    except ScanRefused as e:
        page = await run_in_threadpool(_load_view, _service(request), device_id)
        return _device_page(request, page, flash=("error", _message(e)), status_code=409)
    return _redirect(request, f"/devices/{device_id}?scan=started")


@router.post("/devices/{device_id}/roaming", response_model=None)
def device_roaming(request: Request, device_id: PathId, form: Form) -> Response:
    """Roaming on (needs a room; clears the point) or off (keeps the room, no point)."""
    svc = _service(request)
    choice = _text(form, "roaming")
    try:
        if choice not in ("on", "off"):
            raise ValueError("roaming must be on or off")
        with svc.lock:
            row = svc.conn.execute(
                "SELECT place_id FROM device WHERE id = ?", (device_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no device with id {device_id}")
            seen = q.last_placement_batch(svc.conn, device_id)
        if row[0] is None:
            raise ValueError("assign a room before marking the device roaming")
        placement = Placement(device_id, int(row[0]), None, None, roaming=choice == "on")
        svc.place_devices(ACTOR, [placement], {device_id: seen})
    except StaleWrite:
        page = _load_view(svc, device_id)
        return _device_page(request, page, flash=("error", STALE_MESSAGE), status_code=409)
    except ValueError as e:
        page = _load_view(svc, device_id)
        return _device_page(request, page, flash=("error", _message(e)), status_code=422)
    return _redirect(request, f"/devices/{device_id}")


# -------------------------------------------------------------------- inbox


@dataclass(frozen=True)
class InboxItem:
    device: q.DeviceSummary
    interfaces: list[q.InterfaceView]


def _inbox_page(
    request: Request, flash: tuple[str, str] | None = None, status_code: int = 200
) -> HTMLResponse:
    svc = _service(request)
    with svc.lock:
        items = [
            InboxItem(d, q.device_interfaces(svc.conn, d.id))
            for d in q.inbox(svc.conn, window_min=svc.ip_match_window_min)
        ]
        known = q.list_devices(
            svc.conn, trust="known", limit=DEVICE_LIMIT, window_min=svc.ip_match_window_min
        )
    return render(
        request,
        "inbox.html",
        {"nav": "inbox", "items": items, "known": known, "flash": flash},
        status_code,
    )


@router.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request) -> HTMLResponse:
    return _inbox_page(request)


@router.post("/inbox/{device_id}/name", response_model=None)
def inbox_name(request: Request, device_id: PathId, form: Form) -> Response:
    fields: dict[str, Any] = {"name": _clean(_text(form, "name"))}
    kind = _clean(_text(form, "type"))
    if kind is not None:
        fields["type"] = kind
    try:
        if fields["name"] is None:
            raise ValueError("name must not be empty")
        _service(request).update_device(ACTOR, device_id, fields)
    except ValueError as e:
        return _inbox_page(request, ("error", _message(e)), 422)
    return _redirect(request, "/inbox")


@router.post("/inbox/{device_id}/{action}", response_model=None)
def inbox_action(request: Request, device_id: PathId, action: str, form: Form) -> Response:
    if action not in ("known", "unwanted"):
        raise HTTPException(status_code=404, detail=f"unknown inbox action {action!r}")
    _service(request).update_device(ACTOR, device_id, {"trust": action})
    return _redirect(request, "/inbox")


@router.post("/interfaces/{interface_id}/claim", response_model=None)
def claim(request: Request, interface_id: PathId, form: Form) -> Response:
    raw = (_text(form, "device_id") or "").strip()
    try:
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_ID:
            raise ValueError("choose a device to claim the interface onto")
        _service(request).claim_interface(ACTOR, interface_id, int(raw))
    except ValueError as e:
        return _inbox_page(request, ("error", _message(e)), 422)
    return _redirect(request, "/inbox")


# ------------------------------------------------------------------ history


@dataclass(frozen=True)
class BatchView:
    id: int
    actor: str
    at: str
    changes: list[q.ChangeView]
    undone: bool
    undoes: int | None
    can_undo: bool


def _history_page(
    request: Request,
    actor: str | None = None,
    flash: tuple[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    svc = _service(request)
    with svc.lock:
        changes = q.recent_changes(svc.conn, limit=HISTORY_LIMIT, actor=actor or None)
        links = q.undo_links(svc.conn)
    grouped: dict[int, list[q.ChangeView]] = {}
    for ch in changes:
        grouped.setdefault(ch.batch_id, []).append(ch)
    undone = set(links.values())
    batches = []
    for bid, rows in sorted(grouped.items(), reverse=True):
        undoes = links.get(bid)
        is_undone = bid in undone
        batches.append(
            BatchView(
                id=bid,
                actor=rows[0].actor,
                at=rows[0].at,
                changes=list(reversed(rows)),
                undone=is_undone,
                undoes=undoes,
                # Ruling 36a: same rule as the service; undoing an undo is a redo.
                can_undo=not is_undone,
            )
        )
    return render(
        request,
        "history.html",
        {"nav": "history", "batches": batches, "actor": actor or "", "flash": flash},
        status_code,
    )


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, actor: str | None = None) -> HTMLResponse:
    return _history_page(request, actor)


@router.post("/history/{batch_id}/undo", response_model=None)
def history_undo(request: Request, batch_id: PathId, form: Form) -> Response:
    # Keep the page's actor filter (hidden form field, else the query string).
    actor = _clean(_text(form, "actor")) or _clean(request.query_params.get("actor"))
    try:
        _service(request).undo(ACTOR, batch_id)
    except UndoConflict as e:
        if e.conflicting:
            ids = ", ".join(f"#{b}" for b in e.conflicting)
            msg = f"Cannot undo #{batch_id}: later changes touch the same data ({ids})."
            msg += " Undo those first."
        else:
            msg = f"#{batch_id}: {e}"
        return _history_page(request, actor, flash=("error", msg), status_code=409)
    except ValueError as e:
        flash = ("error", f"#{batch_id}: {_message(e)}")
        return _history_page(request, actor, flash=flash, status_code=422)
    suffix = "?" + urlencode({"actor": actor}) if actor else ""
    return _redirect(request, "/history" + suffix)


# ----------------------------------------------------------------- topology
# The page reads and writes through JSON endpoints here (not /api, which may need
# a bearer token the browser does not have). Errors on ``/topology/*`` are JSON
# (see ``app._wants_json``); writes share their logic with the API.


@router.get("/topology", response_class=HTMLResponse)
def topology_page(request: Request) -> HTMLResponse:
    return render(request, "topology.html", {"nav": "topology"})


@router.get("/topology/data")
def topology_data(request: Request) -> Graph:
    """Same JSON as ``GET /api/v1/topology``."""
    svc = _service(request)
    with svc.lock:
        return topology_graph(svc.conn)


@router.post("/topology/layout", dependencies=[CsrfHeader])
def topology_layout(request: Request, body: LayoutBody) -> dict[str, int]:
    return {"updated": save_layout(_service(request), body.positions)}


@router.post("/topology/links", dependencies=[CsrfHeader])
def topology_add_link(request: Request, body: LinkBody) -> dict[str, int]:
    return add_link(_service(request), ACTOR, body)


@router.post("/topology/links/{link_id}/delete", dependencies=[CsrfHeader])
def topology_remove_link(request: Request, link_id: PathId) -> dict[str, int]:
    return {"batch_id": _service(request).remove_link(ACTOR, link_id)}


@router.post("/topology/placeholder", dependencies=[CsrfHeader])
def topology_placeholder(request: Request, body: PlaceholderBody) -> dict[str, int]:
    return create_placeholder(_service(request), ACTOR, body)


# ------------------------------------------------------------------- status


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    svc = _service(request)
    with svc.lock:
        collectors = q.collector_status(svc.conn)
        counts = q.device_counts(svc.conn)
    return render(
        request, "status.html", {"nav": "status", "collectors": collectors, "counts": counts}
    )


def install(app: FastAPI) -> None:
    """Mount the static files and the page routes onto ``app``."""
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)
