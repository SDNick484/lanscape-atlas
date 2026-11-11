"""MCP server (spec section 8.3): look up, rename and place devices from Claude.

``build_mcp`` registers exactly the tools in ``TOOL_NAMES`` on an SDK
``MCPServer`` (the mcp 2.x name for FastMCP). ``create_app`` mounts it at ``/mcp``
with the streamable HTTP transport; auth is the app's bearer-token middleware.

- Reads take ``service.lock`` (one shared sqlite connection, Ruling 30).
- Writes go through ``Service`` with ``actor="claude"``, so they show in history
  as Claude's changes and can be undone.
- Expected failures come back as tool errors (``isError``) with a plain message:
  ``"device 42 not found"``, ``"undo conflict with batches [17, 19]"``,
  ``"scan refused: scan_policy=never"`` or a ``ValueError``'s own text. Anything
  else is left to the SDK, which reports only ``Error executing tool <name>``.
- Outputs are built from ``service.queries`` dataclasses only; settings (router
  password, API token) are never read here.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError

from lanscape_atlas.collectors.nmap import ScanRefused
from lanscape_atlas.service import Service, StaleWrite, UndoConflict, queries
from lanscape_atlas.service import ble_queries as bq
from lanscape_atlas.web.ble_api import (
    age_seconds,
    candidates_payload,
    find_row_dict,
    require_candidate,
)
from lanscape_atlas.web.floorplan_api import PlacementsBody, apply_placements

ACTOR = "claude"
MAX_LIMIT = 200

TOOL_NAMES = (
    "find_device",
    "list_devices",
    "get_device",
    "list_services",
    "update_device",
    "claim_interface",
    "set_place",
    "run_deep_scan",
    "recent_changes",
    "undo",
    "get_floor_plan",
    "place_devices",
    "where_is",
    "ble_candidates",
    "claim_ble",
)

INSTRUCTIONS = (
    "lanscape-atlas is an inventory of the devices on a home network. Use find_device or "
    "list_devices to locate a device, get_device for its full record, and update_device / "
    "set_place / claim_interface to name, place or merge devices. Every change you make is "
    "recorded as actor 'claude' and can be reverted with undo(batch_id). get_floor_plan lists "
    "rooms and where devices sit; place_devices puts devices in rooms or at points on the plan. "
    "where_is finds where a Bluetooth tag or device was last heard; ble_candidates lists unclaimed "
    "Bluetooth devices and claim_ble attaches one to a device. Devices that rotate their "
    "Bluetooth address (most phones and watches) are not findable."
)

Scanner = Callable[[int], Awaitable[None]]
BluetoothGetter = Callable[[], Any]

_NO_ENTITY = re.compile(r"^no (\w+) with id (-?\d+)$")


def error_message(exc: BaseException) -> str | None:
    """The plain tool-error text for an expected failure, or ``None`` if unexpected."""
    if isinstance(exc, UndoConflict):
        if not exc.conflicting and exc.reason:
            return f"undo conflict: {exc.reason}"
        return f"undo conflict with batches {list(exc.conflicting)}"
    if isinstance(exc, StaleWrite):
        return (
            f"placement changed since it was read for devices {list(exc.conflicting)}; "
            "call get_floor_plan and retry with the new last_batch values"
        )
    if isinstance(exc, ScanRefused):
        return f"scan refused: {_arg(exc, 'refused')}"
    if isinstance(exc, KeyError):
        msg = _arg(exc, "not found")
        m = _NO_ENTITY.match(msg)
        return f"{m[1]} {m[2]} not found" if m else msg
    if isinstance(exc, OverflowError):
        # e.g. an id beyond sqlite's INTEGER range.
        return "integer out of range"
    if isinstance(exc, ValueError):
        return _arg(exc, "invalid request")
    return None


def _arg(exc: BaseException, default: str) -> str:
    # str(KeyError("x")) is "'x'"; use the bare argument instead.
    return str(exc.args[0]) if exc.args else default


def _tool_error(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], is_error=True)


def _plain_errors[F: Callable[..., Any]](fn: F) -> F:
    """Turn expected exceptions into an ``isError`` result carrying only the message.

    Raising the SDK's ``ToolError`` would prefix ``Error executing tool <name>:``;
    returning the result directly keeps the message exactly as documented. The
    wrapper keeps ``fn``'s signature (``functools.wraps``), which the SDK reads to
    build the input schema.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                msg = error_message(exc)
                if msg is None:
                    raise
                return _tool_error(msg)

        return run_async  # type: ignore[return-value]

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = error_message(exc)
            if msg is None:
                raise
            return _tool_error(msg)

    return run  # type: ignore[return-value]


def _cap(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def build_mcp(
    service: Service,
    scanner: Scanner,
    bluetooth: BluetoothGetter | None = None,
    *,
    candidate_min_rssi: int = -80,
) -> MCPServer[Any]:
    """An ``MCPServer`` exposing the inventory through exactly the tools in ``TOOL_NAMES``.

    ``scanner(device_id)`` starts a background deep scan or raises ``KeyError`` /
    ``ScanRefused``; ``create_app`` passes one built on ``web.deep_scan.start_deep_scan``.
    ``bluetooth()`` returns the ``BluetoothRuntime`` or ``None`` (no Home Assistant).
    ``candidate_min_rssi`` is ``[bluetooth] candidate_min_rssi``: weaker candidates are
    listed by ``ble_candidates`` only with ``include_weak``.
    """
    # MCPServer() calls logging.basicConfig(level=INFO) on the root logger; logging
    # setup belongs to the application, so undo that side effect.
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        mcp: MCPServer[Any] = MCPServer(
            "lanscape-atlas",
            title="lanscape-atlas",
            instructions=INSTRUCTIONS,
            version="0.1.0",
        )
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)

    def tool[F: Callable[..., Any]](
        title: str | None = None, annotations: ToolAnnotations | None = None
    ) -> Callable[[F], F]:
        def register(fn: F) -> F:
            mcp.add_tool(_plain_errors(fn), title=title, annotations=annotations)
            return fn

        return register

    # ------------------------------------------------------------------ reads

    @tool()
    def find_device(query: str) -> list[dict[str, Any]]:
        """Find devices matching an IPv4 address (current lease), a MAC address (full or
        partial, e.g. "00:00:5e:00:53" or "5e0053"), or text in the device name, vendor
        or a hostname. Returns device summaries (id, name, type, ipv4, macs, vendor,
        place, online, trust, ...); an empty list if nothing matches."""
        with service.lock:
            found = queries.find_devices(
                service.conn, query, window_min=service.ip_match_window_min
            )
            return [asdict(d) for d in found]

    @tool()
    def list_devices(
        online: bool | None = None,
        type: str | None = None,
        trust: str | None = None,
        place: str | None = None,
        ap_node: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List device summaries ordered by id. Optional exact-match filters: online
        (true/false), type (e.g. "phone", "nas"), trust ("new", "known", ...), place
        name, ap_node (the Wi-Fi node's MAC); q is a case-insensitive substring of the
        name, vendor or hostname, an exact current IPv4, or a full or partial MAC.
        Paginate with limit (default 50, max 200) and offset."""
        with service.lock:
            rows = queries.list_devices(
                service.conn,
                online=online,
                type=type,
                trust=trust,
                place=place,
                ap_node=ap_node,
                q=q,
                limit=_cap(limit),
                offset=max(0, offset),
                window_min=service.ip_match_window_min,
            )
        return [asdict(r) for r in rows]

    @tool()
    def get_device(device_id: int) -> dict[str, Any]:
        """Full record of one device: summary fields plus owner, notes, scan_policy,
        interfaces (MACs, Wi-Fi node, band, hostname hints), IP lease history, open
        services, and field_sources ({field: [actor, set_at]}, who last set each field)."""
        with service.lock:
            return asdict(
                queries.get_device(service.conn, device_id, window_min=service.ip_match_window_min)
            )

    @tool()
    def list_services(
        device_id: int | None = None, port: int | None = None
    ) -> list[dict[str, Any]]:
        """List network services (open ports found by scans or announced via mDNS/SSDP).
        Filter by device_id ("what's on the NAS?") and/or port ("what runs SSH?" -> 22);
        with neither, lists every service."""
        with service.lock:
            return [asdict(s) for s in queries.list_services(service.conn, device_id, port)]

    @tool()
    def recent_changes(limit: int = 20, actor: str | None = None) -> list[dict[str, Any]]:
        """The audit trail, newest first: one row per changed field with batch_id, actor
        ("user", "claude", "system" or "collector:<name>"), time, entity, field, and old
        and new values. Filter by actor; limit defaults to 20, max 200. Pass a row's
        batch_id to undo to revert that whole batch."""
        with service.lock:
            rows = queries.recent_changes(service.conn, limit=_cap(limit), actor=actor)
        return [asdict(r) for r in rows]

    # ----------------------------------------------------------------- writes

    @tool()
    def update_device(device_id: int, fields: dict[str, Any]) -> dict[str, int]:
        """Set user-facing fields on a device. Allowed keys: name, type, icon, owner,
        notes, trust, scan_policy (and place_id; prefer set_place). Use null to clear a
        field. Recorded as actor "claude"; returns {"batch_id": n} for undo."""
        return {"batch_id": service.update_device(ACTOR, device_id, fields)}

    @tool()
    def claim_interface(interface_id: int, device_id: int) -> dict[str, int]:
        """Attach an interface (typically a random/private Wi-Fi MAC that showed up as its
        own device) to device_id. The interface's old device is merged into device_id and
        removed if it has nothing left. Returns {"batch_id": n}; undo reverses it."""
        return {"batch_id": service.claim_interface(ACTOR, interface_id, device_id)}

    @tool()
    def set_place(device_id: int, place_name: str) -> dict[str, int]:
        """Put a device in a place (room or area such as "Office" or "Living room"),
        creating the place if it does not exist yet. Returns {"batch_id": n}."""
        return {"batch_id": service.set_place(ACTOR, device_id, place_name)}

    @tool()
    async def run_deep_scan(device_id: int) -> dict[str, str]:
        """Start an on-demand nmap deep scan of a device's current IPv4 in the background.
        Refused when the device's scan_policy is "never", it has no current IPv4, or a
        scan of it is already running. Returns {"status": "started"}; results appear
        later in get_device / list_services."""
        await scanner(device_id)
        return {"status": "started"}

    @tool()
    def undo(batch_id: int) -> dict[str, int]:
        """Revert every change in a batch (batch ids come from recent_changes or a write
        tool's result). Fails with "undo conflict with batches [...]" if later changes
        touched the same fields; undo those first. Returns the new undo batch's id."""
        return {"batch_id": service.undo(ACTOR, batch_id)}

    # ------------------------------------------------------------ floor plan

    @tool(
        title="Get floor plan",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def get_floor_plan() -> dict[str, Any]:
        """The floor plan: plan (width and height in pixels and updated_at, or null when no
        plan is uploaded; no image or file name), rooms (id, name, polygon: a list of
        [x, y] points), placed (fixed devices with a point), roaming (devices assigned to
        a room only, no point) and unplaced (no room, or a room but no point; each has
        place_id, null if none). Coordinates are normalized 0..1 across the plan image
        (x left to right, y top to bottom). Every device carries last_batch: pass it back
        in place_devices' based_on."""
        with service.lock:
            view = queries.floor_plan_view(service.conn, now=service.clock())
        plan = view["plan"]
        if plan is not None:
            view["plan"] = {k: plan[k] for k in ("width", "height", "updated_at")}
        return view

    @tool(
        title="Place devices on the floor plan",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        ),
    )
    def place_devices(
        placements: list[dict[str, Any]], based_on: dict[str, Any] | None = None
    ) -> dict[str, int]:
        """Place devices on the floor plan in one batch. Each placement is {device_id,
        place_id, x, y, roaming}: place_id is a room id from get_floor_plan (null to
        unplace); x and y are normalized 0..1 coordinates and must fall inside that
        room's polygon; roaming=true puts the device in the room only (x and y null);
        roaming=false with a place_id and no x/y assigns the room without a point.
        Ids must be integers and roaming a boolean. based_on maps "<device_id>" to that
        device's last_batch from get_floor_plan (0 if it has never been placed). It may be omitted,
        but a missing entry counts as 0, so any device that was ever placed is then stale. If a
        device changed since, nothing is written and the error names the stale ids:
        call get_floor_plan and retry. Recorded as actor "claude"; returns {"batch_id":
        n} (0 if nothing changed); undo(batch_id) reverts it."""
        try:
            body = PlacementsBody.model_validate(
                {"placements": placements, "based_on": based_on or {}}
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise ValueError(f"invalid placements: {where}: {first['msg']}") from None
        # Service.place_devices takes service.lock itself.
        return {"batch_id": apply_placements(service, ACTOR, body)}

    # ------------------------------------------------------------- bluetooth

    @tool(
        title="Where is a device",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def where_is(query: str) -> list[dict[str, Any]]:
        """Where a Bluetooth-tracked device was last heard. query is a case-insensitive
        substring of the device name ("" lists every claimed device). Each row: device_id,
        name, type, badge ("steady", "intermittent" or "not_heard_24h"), age_s (seconds
        since last heard, null if never) and last (null, or at, rssi, place_name, scanner_name,
        scanner_source). Only devices with a claimed Bluetooth identity are listed; devices
        that rotate their address are not findable."""
        with service.lock:
            now = service.clock()
            rows = bq.find(service.conn, query or None, now=now)
        return [{**find_row_dict(r), "age_s": age_seconds(r, now)} for r in rows]

    @tool(
        title="List unclaimed Bluetooth devices",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def ble_candidates(include_weak: bool = False) -> dict[str, Any]:
        """Unclaimed Bluetooth devices heard recently, strongest signal first: connected
        (Home Assistant link up, null when Home Assistant is not configured), min_rssi,
        weak_hidden (how many weaker than min_rssi were left out; pass include_weak=true to
        list them, as far-away ones are usually a neighbour's) and candidates, each with
        identity (pass to claim_ble), name, best_rssi, source, first_heard, last_heard,
        adverts and typical_gap_s. Holding a tag next to a scanner makes its best_rssi
        stand out."""
        runtime = bluetooth() if bluetooth else None
        return candidates_payload(runtime, min_rssi=candidate_min_rssi, include_weak=include_weak)

    @tool(
        title="Claim a Bluetooth device",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        ),
    )
    async def claim_ble(
        candidate: str,
        device_id: int | None = None,
        new_name: str | None = None,
        new_type: str = "other",
    ) -> dict[str, int]:
        """Attach a candidate from ble_candidates to a device so where_is can find it.
        Give either device_id (an existing device) or new_name (creates a device of type
        new_type, default "other"). candidate is the identity string from ble_candidates.
        Recorded as actor "claude"; returns {"device_id", "batch_id"}; undo(batch_id)
        reverts it."""
        runtime = bluetooth() if bluetooth else None
        identity = require_candidate(runtime, candidate)
        did, batch_id = await asyncio.to_thread(
            lambda: service.claim_ble(
                ACTOR,
                identity,
                device_id=device_id,
                new_name=new_name,
                new_type=new_type if new_name is not None else None,
            )
        )
        return {"device_id": did, "batch_id": batch_id}

    return mcp
