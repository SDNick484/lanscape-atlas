"""MCP server (spec section 8.3): look up, rename and place devices from Claude.

``build_mcp`` registers exactly the ten tools in ``TOOL_NAMES`` on an SDK
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

import functools
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from lanscape_atlas.collectors.nmap import ScanRefused
from lanscape_atlas.service import Service, UndoConflict, queries

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
)

INSTRUCTIONS = (
    "lanscape-atlas is an inventory of the devices on a home network. Use find_device or "
    "list_devices to locate a device, get_device for its full record, and update_device / "
    "set_place / claim_interface to name, place or merge devices. Every change you make is "
    "recorded as actor 'claude' and can be reverted with undo(batch_id)."
)

Scanner = Callable[[int], Awaitable[None]]

_NO_ENTITY = re.compile(r"^no (\w+) with id (-?\d+)$")


def error_message(exc: BaseException) -> str | None:
    """The plain tool-error text for an expected failure, or ``None`` if unexpected."""
    if isinstance(exc, UndoConflict):
        return f"undo conflict with batches {list(exc.conflicting)}"
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


def build_mcp(service: Service, scanner: Scanner) -> MCPServer[Any]:
    """An ``MCPServer`` exposing the inventory through exactly the tools in ``TOOL_NAMES``.

    ``scanner(device_id)`` starts a background deep scan or raises ``KeyError`` /
    ``ScanRefused``; ``create_app`` passes one built on ``web.deep_scan.start_deep_scan``.
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

    def tool[F: Callable[..., Any]](fn: F) -> F:
        mcp.add_tool(_plain_errors(fn))
        return fn

    # ------------------------------------------------------------------ reads

    @tool
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

    @tool
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

    @tool
    def get_device(device_id: int) -> dict[str, Any]:
        """Full record of one device: summary fields plus owner, notes, scan_policy,
        interfaces (MACs, Wi-Fi node, band, hostname hints), IP lease history, open
        services, and field_sources ({field: [actor, set_at]}, who last set each field)."""
        with service.lock:
            return asdict(
                queries.get_device(service.conn, device_id, window_min=service.ip_match_window_min)
            )

    @tool
    def list_services(
        device_id: int | None = None, port: int | None = None
    ) -> list[dict[str, Any]]:
        """List network services (open ports found by scans or announced via mDNS/SSDP).
        Filter by device_id ("what's on the NAS?") and/or port ("what runs SSH?" -> 22);
        with neither, lists every service."""
        with service.lock:
            return [asdict(s) for s in queries.list_services(service.conn, device_id, port)]

    @tool
    def recent_changes(limit: int = 20, actor: str | None = None) -> list[dict[str, Any]]:
        """The audit trail, newest first: one row per changed field with batch_id, actor
        ("user", "claude", "system" or "collector:<name>"), time, entity, field, and old
        and new values. Filter by actor; limit defaults to 20, max 200. Pass a row's
        batch_id to undo to revert that whole batch."""
        with service.lock:
            rows = queries.recent_changes(service.conn, limit=_cap(limit), actor=actor)
        return [asdict(r) for r in rows]

    # ----------------------------------------------------------------- writes

    @tool
    def update_device(device_id: int, fields: dict[str, Any]) -> dict[str, int]:
        """Set user-facing fields on a device. Allowed keys: name, type, icon, owner,
        notes, trust, scan_policy (and place_id; prefer set_place). Use null to clear a
        field. Recorded as actor "claude"; returns {"batch_id": n} for undo."""
        return {"batch_id": service.update_device(ACTOR, device_id, fields)}

    @tool
    def claim_interface(interface_id: int, device_id: int) -> dict[str, int]:
        """Attach an interface (typically a random/private Wi-Fi MAC that showed up as its
        own device) to device_id. The interface's old device is merged into device_id and
        removed if it has nothing left. Returns {"batch_id": n}; undo reverses it."""
        return {"batch_id": service.claim_interface(ACTOR, interface_id, device_id)}

    @tool
    def set_place(device_id: int, place_name: str) -> dict[str, int]:
        """Put a device in a place (room or area such as "Office" or "Living room"),
        creating the place if it does not exist yet. Returns {"batch_id": n}."""
        return {"batch_id": service.set_place(ACTOR, device_id, place_name)}

    @tool
    async def run_deep_scan(device_id: int) -> dict[str, str]:
        """Start an on-demand nmap deep scan of a device's current IPv4 in the background.
        Refused when the device's scan_policy is "never", it has no current IPv4, or a
        scan of it is already running. Returns {"status": "started"}; results appear
        later in get_device / list_services."""
        await scanner(device_id)
        return {"status": "started"}

    @tool
    def undo(batch_id: int) -> dict[str, int]:
        """Revert every change in a batch (batch ids come from recent_changes or a write
        tool's result). Fails with "undo conflict with batches [...]" if later changes
        touched the same fields; undo those first. Returns the new undo batch's id."""
        return {"batch_id": service.undo(ACTOR, batch_id)}

    return mcp
