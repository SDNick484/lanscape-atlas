"""Read-only queries over the inventory.

The connection is shared (Ruling 30): callers hold ``service.lock`` around every call, as
the web handlers, MCP tools and collector readers do. The functions never take it
themselves and never write.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from lanscape_atlas.clock import iso
from lanscape_atlas.netutil import normalize_mac, valid_ipv4

# Interface kinds whose address is a MAC. "unknown" is a MAC seen without a link
# type (e.g. ARP only); bluetooth and zwave addresses are not Ethernet MACs.
MAC_KINDS = ("ethernet", "wifi", "unknown")
_MAC_KINDS_SQL = ", ".join(f"'{k}'" for k in MAC_KINDS)

# Settings.ip_match_window_min's default; callers pass the configured value.
DEFAULT_WINDOW_MIN = 10


def current_lease_sql(lease: str = "l", iface: str = "i") -> str:
    """SQL condition: ``lease`` (on interface ``iface``) is the interface's current lease.

    Ruling 44: an open lease is current only if it was seen within the IP match window
    of the interface's own ``last_seen``. When an interface moves IP1 -> IP2, IP1's lease
    stays open (nobody else took it) but stops being current. Takes one parameter:
    ``window_arg(window_min)``.
    """
    return (
        f"{lease}.closed = 0 AND {lease}.last_seen >="
        f" strftime('%Y-%m-%dT%H:%M:%SZ', {iface}.last_seen, ?)"
    )


def window_arg(window_min: int) -> str:
    """The SQLite date modifier ``current_lease_sql`` expects."""
    return f"-{int(window_min)} minutes"


@dataclass(frozen=True)
class DeviceSummary:
    id: int
    name: str | None
    type: str | None
    icon: str | None
    online: bool
    ipv4: str | None
    macs: list[str]
    vendor: str | None
    place: str | None
    ap_node: str | None
    band: str | None
    service_count: int
    last_seen: str
    trust: str


@dataclass(frozen=True)
class InterfaceView:
    id: int
    kind: str
    address: str
    is_random: bool
    vendor: str | None
    ap_node: str | None
    band: str | None
    hints: dict[str, Any]
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class LeaseView:
    id: int
    interface_id: int
    ipv4: str
    first_seen: str
    last_seen: str
    closed: bool


@dataclass(frozen=True)
class ServiceView:
    id: int
    device_id: int
    port: int
    proto: str
    name: str | None
    product: str | None
    version: str | None
    source: str
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class DeviceView(DeviceSummary):
    owner: str | None
    notes: str | None
    scan_policy: str
    interfaces: list[InterfaceView]
    leases: list[LeaseView]
    services: list[ServiceView]
    field_sources: dict[str, tuple[str, str]]


@dataclass(frozen=True)
class ChangeView:
    id: int
    batch_id: int
    actor: str
    at: str
    entity: str
    entity_id: int
    field: str
    old_value: Any
    new_value: Any


# One row per device with every derived summary column. "Most recent" ties are
# broken by the higher id so results are deterministic.
_SUMMARY_SQL = f"""
SELECT d.id, d.name, d.type, d.icon, d.online, d.last_seen, d.trust,
       p.name AS place,
       (SELECT l.ipv4 FROM ip_lease l JOIN interface i ON i.id = l.interface_id
         WHERE i.device_id = d.id AND {current_lease_sql()}
         ORDER BY l.last_seen DESC, l.id DESC LIMIT 1) AS ipv4,
       (SELECT i.id FROM interface i WHERE i.device_id = d.id
         ORDER BY i.last_seen DESC, i.id DESC LIMIT 1) AS latest_iface,
       (SELECT count(*) FROM service s WHERE s.device_id = d.id) AS service_count
  FROM device d LEFT JOIN place p ON p.id = d.place_id
"""


def _summaries(
    conn: sqlite3.Connection,
    where: str = "",
    params: tuple[Any, ...] = (),
    *,
    limit: int | None = None,
    offset: int = 0,
    window_min: int = DEFAULT_WINDOW_MIN,
) -> list[DeviceSummary]:
    sql = f"SELECT * FROM ({_SUMMARY_SQL}) AS d {where} ORDER BY d.id"
    # The summary's own placeholder (current-lease window) comes first in the text.
    args: tuple[Any, ...] = (window_arg(window_min), *params)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args = (*args, limit, offset)
    rows = conn.execute(sql, args).fetchall()
    return [_summary_from_row(conn, r) for r in rows]


def _summary_from_row(conn: sqlite3.Connection, r: sqlite3.Row) -> DeviceSummary:
    macs = [
        str(m[0])
        for m in conn.execute(
            f"SELECT address FROM interface WHERE device_id = ? AND kind IN ({_MAC_KINDS_SQL})"
            " ORDER BY address",
            (r["id"],),
        )
    ]
    vendor = ap_node = band = None
    if r["latest_iface"] is not None:
        i = conn.execute(
            "SELECT vendor, ap_node, band FROM interface WHERE id = ?", (r["latest_iface"],)
        ).fetchone()
        vendor, ap_node, band = i["vendor"], i["ap_node"], i["band"]
    return DeviceSummary(
        id=r["id"],
        name=r["name"],
        type=r["type"],
        icon=r["icon"],
        online=bool(r["online"]),
        ipv4=r["ipv4"],
        macs=macs,
        vendor=vendor,
        place=r["place"],
        ap_node=ap_node,
        band=band,
        service_count=r["service_count"],
        last_seen=r["last_seen"],
        trust=r["trust"],
    )


def _like(text: str) -> str:
    """Escape LIKE wildcards (escape char ``\\``) and wrap in ``%``."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# Case-insensitive substring on name, vendor (any interface) or any hostname hint.
_TEXT_MATCH = """(
    lower(coalesce(d.name, '')) LIKE ? ESCAPE '\\'
    OR EXISTS (SELECT 1 FROM interface i WHERE i.device_id = d.id AND (
        lower(coalesce(i.vendor, '')) LIKE ? ESCAPE '\\'
        OR EXISTS (SELECT 1 FROM json_each(i.hints) h
                    WHERE lower(CAST(h.value AS TEXT)) LIKE ? ESCAPE '\\')))
)"""


def list_devices(
    conn: sqlite3.Connection,
    *,
    online: bool | None = None,
    type: str | None = None,
    trust: str | None = None,
    place: str | None = None,
    ap_node: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    window_min: int = DEFAULT_WINDOW_MIN,
) -> list[DeviceSummary]:
    clauses: list[str] = []
    params: list[Any] = []
    if online is not None:
        clauses.append("d.online = ?")
        params.append(1 if online else 0)
    for col, val in (("type", type), ("trust", trust), ("place", place)):
        if val is not None:
            clauses.append(f"d.{col} = ?")
            params.append(val)
    if ap_node is not None:
        clauses.append("(SELECT ap_node FROM interface WHERE id = d.latest_iface) = ?")
        params.append(ap_node)
    if q is not None:
        match = _match_clause(q, window_min)
        if match is not None:
            clauses.append(match[0])
            params.extend(match[1])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return _summaries(conn, where, tuple(params), limit=limit, offset=offset, window_min=window_min)


def get_device(
    conn: sqlite3.Connection, device_id: int, *, window_min: int = DEFAULT_WINDOW_MIN
) -> DeviceView:
    found = _summaries(conn, "WHERE d.id = ?", (device_id,), window_min=window_min)
    if not found:
        raise KeyError(f"no device with id {device_id}")
    s = found[0]
    d = conn.execute(
        "SELECT owner, notes, scan_policy FROM device WHERE id = ?", (device_id,)
    ).fetchone()
    interfaces = device_interfaces(conn, device_id)
    leases = [
        LeaseView(
            id=r["id"],
            interface_id=r["interface_id"],
            ipv4=r["ipv4"],
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
            closed=bool(r["closed"]),
        )
        for r in conn.execute(
            "SELECT l.* FROM ip_lease l JOIN interface i ON i.id = l.interface_id"
            " WHERE i.device_id = ? ORDER BY l.last_seen DESC, l.id DESC",
            (device_id,),
        )
    ]
    sources = {
        str(r["field"]): (str(r["actor"]), str(r["set_at"]))
        for r in conn.execute(
            "SELECT field, actor, set_at FROM field_source WHERE entity = 'device'"
            " AND entity_id = ?",
            (device_id,),
        )
    }
    return DeviceView(
        **{f: getattr(s, f) for f in DeviceSummary.__dataclass_fields__},
        owner=d["owner"],
        notes=d["notes"],
        scan_policy=d["scan_policy"],
        interfaces=interfaces,
        leases=leases,
        services=list_services(conn, device_id),
        field_sources=sources,
    )


def device_interfaces(conn: sqlite3.Connection, device_id: int) -> list[InterfaceView]:
    """A device's interfaces ordered by address (empty for an unknown device)."""
    return [
        InterfaceView(
            id=r["id"],
            kind=r["kind"],
            address=r["address"],
            is_random=bool(r["is_random"]),
            vendor=r["vendor"],
            ap_node=r["ap_node"],
            band=r["band"],
            hints=json.loads(r["hints"] or "{}"),
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
        )
        for r in conn.execute(
            "SELECT * FROM interface WHERE device_id = ? ORDER BY address", (device_id,)
        )
    ]


_MAC_PARTIAL = re.compile(r"^[0-9a-f:\-.]+$")
_MAC_SEPARATORS = re.compile(r"[:\-.]")


def _match_clause(query: str, window_min: int) -> tuple[str, list[Any]] | None:
    """The shared device matcher behind ``find_devices`` and ``list_devices(q=)``.

    An exact IPv4 matches devices holding it on a current lease (``current_lease_sql``).
    Otherwise text matches name, vendor or any hostname hint, OR'd with a MAC match when
    the text looks like a full or partial MAC (``:``, ``-`` or ``.`` separated, or bare hex).
    Returns ``(sql, params)`` over the summary alias ``d``, or ``None`` for blank text.
    """
    text = query.strip()
    if not text:
        return None
    ip = valid_ipv4(text)
    if ip is not None:
        return (
            "d.id IN (SELECT i.device_id FROM ip_lease l JOIN interface i"
            f" ON i.id = l.interface_id WHERE l.ipv4 = ? AND {current_lease_sql()})",
            [ip, window_arg(window_min)],
        )
    lowered = text.lower()
    pat = _like(lowered)
    clauses = [_TEXT_MATCH]
    params: list[Any] = [pat, pat, pat]
    hex_needle: str | None = None
    try:
        hex_needle = normalize_mac(text).replace(":", "")
    except ValueError:
        if _MAC_PARTIAL.match(lowered):
            hex_needle = _MAC_SEPARATORS.sub("", lowered) or None
    if hex_needle is not None:
        # Compare separator-free hex so "53-1a", "53:1a" and "5e00.531a" all match.
        clauses.append(
            "EXISTS (SELECT 1 FROM interface i WHERE i.device_id = d.id"
            " AND replace(lower(i.address), ':', '') LIKE ? ESCAPE '\\')"
        )
        params.append(_like(hex_needle))
    return "(" + " OR ".join(clauses) + ")", params


def find_devices(
    conn: sqlite3.Connection, query: str, *, window_min: int = DEFAULT_WINDOW_MIN
) -> list[DeviceSummary]:
    """Exact IPv4 (current lease only), full/partial MAC, or name/hostname/vendor text."""
    match = _match_clause(query, window_min)
    if match is None:
        return []
    return _summaries(conn, "WHERE " + match[0], tuple(match[1]), window_min=window_min)


def list_services(
    conn: sqlite3.Connection, device_id: int | None = None, port: int | None = None
) -> list[ServiceView]:
    clauses: list[str] = []
    params: list[Any] = []
    if device_id is not None:
        clauses.append("device_id = ?")
        params.append(device_id)
    if port is not None:
        clauses.append("port = ?")
        params.append(port)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return [
        ServiceView(
            id=r["id"],
            device_id=r["device_id"],
            port=r["port"],
            proto=r["proto"],
            name=r["name"],
            product=r["product"],
            version=r["version"],
            source=r["source"],
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
        )
        for r in conn.execute(
            f"SELECT * FROM service {where} ORDER BY device_id, port, proto", tuple(params)
        )
    ]


def recent_changes(
    conn: sqlite3.Connection, limit: int = 50, actor: str | None = None
) -> list[ChangeView]:
    where = "WHERE b.actor = ?" if actor is not None else ""
    params: tuple[Any, ...] = (actor,) if actor is not None else ()
    rows = conn.execute(
        "SELECT c.id, c.batch_id, b.actor, b.at, c.entity, c.entity_id, c.field,"
        " c.old_value, c.new_value FROM change c JOIN batch b ON b.id = c.batch_id"
        f" {where} ORDER BY c.id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [
        ChangeView(
            id=r["id"],
            batch_id=r["batch_id"],
            actor=r["actor"],
            at=r["at"],
            entity=r["entity"],
            entity_id=r["entity_id"],
            field=r["field"],
            old_value=None if r["old_value"] is None else json.loads(r["old_value"]),
            new_value=None if r["new_value"] is None else json.loads(r["new_value"]),
        )
        for r in rows
    ]


def inbox(conn: sqlite3.Connection, *, window_min: int = DEFAULT_WINDOW_MIN) -> list[DeviceSummary]:
    """Devices with ``trust='new'`` plus devices whose interfaces are all random MACs."""
    return _summaries(
        conn,
        "WHERE d.trust = 'new' OR (d.latest_iface IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM interface i WHERE i.device_id = d.id AND i.is_random = 0))",
        window_min=window_min,
    )


def undo_links(conn: sqlite3.Connection) -> dict[int, int]:
    """``{undo batch id: the batch it undoes}`` for every undo batch."""
    return {
        int(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT id, undoes_batch_id FROM batch WHERE undoes_batch_id IS NOT NULL"
        )
    }


def collector_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """``collector_status`` rows by name; ``last_error`` is already redacted by the runner."""
    rows = conn.execute(
        "SELECT name, last_ok, last_error, last_error_at, consecutive_failures, next_run_at"
        " FROM collector_status ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


@dataclass(frozen=True)
class DeviceCounts:
    total: int
    online: int
    inbox: int


def device_counts(conn: sqlite3.Connection) -> DeviceCounts:
    total, online = conn.execute("SELECT count(*), coalesce(sum(online), 0) FROM device").fetchone()
    return DeviceCounts(total=int(total), online=int(online), inbox=len(inbox(conn)))


def ap_device_names(
    conn: sqlite3.Connection, macs: Iterable[str | None]
) -> dict[str, tuple[int, str | None]]:
    """``{mac: (device id, device name)}`` for the devices owning these MACs (e.g. AP nodes).

    MACs no device owns are absent. If two interfaces share an address (different
    kinds), the lower interface id wins.
    """
    wanted = sorted({m for m in macs if m})
    found: dict[str, tuple[int, str | None]] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(
            "SELECT i.address, d.id, d.name FROM interface i JOIN device d ON d.id = i.device_id"
            f" WHERE i.kind IN ({_MAC_KINDS_SQL}) AND i.address IN ({marks}) ORDER BY i.id DESC",
            chunk,
        ):
            found[str(r[0])] = (int(r[1]), r[2])
    return found


_PLACEMENT_FIELDS = ("place_id", "pos_x", "pos_y", "roaming")


def last_placement_batch(conn: sqlite3.Connection, device_id: int) -> int:
    """Latest batch id that changed a device's placement fields, or 0 if none.

    Covers ``place_id``, ``pos_x``, ``pos_y`` and ``roaming`` changes from any writer
    (``place_devices``, ``update_device``, ``set_place``, claims and undo batches).
    ``place_devices`` compares it with the client's ``based_on`` to detect stale writes.
    """
    marks = ", ".join("?" for _ in _PLACEMENT_FIELDS)
    row = conn.execute(
        "SELECT MAX(batch_id) FROM change WHERE entity = 'device' AND entity_id = ?"
        f" AND field IN ({marks})",
        (device_id, *_PLACEMENT_FIELDS),
    ).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def last_shapes_batch(conn: sqlite3.Connection) -> int:
    """Newest batch id that touched any drawn shape (``plan_shape``), or 0 if none.

    Covers ``save_shapes`` and undo batches. The page sends it back as ``based_on``;
    ``Service.save_shapes`` refuses (``StaleShapes``) when a newer one exists.
    """
    row = conn.execute("SELECT MAX(batch_id) FROM change WHERE entity = 'plan_shape'").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def plan_shapes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every drawn shape, by id: ``{id, kind, geometry (parsed JSON), label}``."""
    return [
        {"id": r[0], "kind": r[1], "geometry": json.loads(r[2]), "label": r[3]}
        for r in conn.execute("SELECT id, kind, geometry, label FROM plan_shape ORDER BY id")
    ]


def drawing_summary(shapes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What MCP may see of the drawing (spec §6.6): counts by kind and the names.

    ``{"counts": {kind: n}, "furniture": [names], "labels": [texts]}``. Never any
    geometry. Only named furniture is listed; ids order. Names are the owner's text:
    untrusted data, not instructions.
    """
    counts = {k: 0 for k in ("wall", "door", "furniture", "label")}
    furniture: list[str] = []
    labels: list[str] = []
    for s in shapes:
        counts[str(s["kind"])] = counts.get(str(s["kind"]), 0) + 1
        if s["label"]:
            if s["kind"] == "furniture":
                furniture.append(str(s["label"]))
            elif s["kind"] == "label":
                labels.append(str(s["label"]))
    return {"counts": counts, "furniture": furniture, "labels": labels}


def _heard_by_device(conn: sqlite3.Connection, now: datetime) -> dict[int, dict[str, Any]]:
    """Newest sighting per device if within 24 h of ``now``: ``{device_id: {place_id, at}}``."""
    rows = conn.execute(
        "SELECT device_id, place_id, last_at FROM ("
        " SELECT device_id, place_id, last_at, ROW_NUMBER() OVER ("
        "  PARTITION BY device_id ORDER BY last_at DESC, max_rssi DESC, scanner_id ASC) AS rn"
        " FROM ble_sighting) WHERE rn = 1 AND last_at >= ?",
        (iso(now - timedelta(hours=24)),),
    )
    return {r["device_id"]: {"place_id": r["place_id"], "at": r["last_at"]} for r in rows}


def floor_plan_view(conn: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    """Everything the floor plan page needs, read-only.

    ``placed`` are fixed devices in a room with coordinates; ``roaming`` are roaming
    devices assigned to a room (with who set it and when: the newer of the ``place_id``
    and ``roaming`` ``field_source`` rows, roaming on a tie); ``unplaced`` is everything
    else (no room, or a fixed device without coordinates). Each ``unplaced`` entry
    carries its ``place_id`` (the room id, or None) so the page can group the tray by room.
    Each device carries ``last_batch`` (see ``last_placement_batch``) so the page can
    send it back as ``based_on``. Exposes plan metadata only, never the file path or
    image bytes: ``plan`` is None (no plan) or ``{width, height, file_name, updated_at,
    blank, width_mm, units}``; a blank plan (``blank`` true, ``file_name`` None) has
    ``width``/``height`` in mm, an image plan in pixels; ``width_mm`` is the plan's
    real width (None for an uncalibrated image) and ``units`` the display units.
    Online state is the stored ``device.online`` flag. Every device entry
    whose newest Bluetooth sighting is at most 24 h old (``now`` is the reference) also
    carries ``heard: {"place_id", "at"}`` (``place_id`` is None for an unlinked scanner);
    the sighting is chosen like ``ble_queries.last_heard`` (newest, then stronger, then
    lower scanner id), but for all devices in one query.

    ``shapes`` are the drawn shapes (``plan_shapes``) and ``shapes_batch`` the
    ``last_shapes_batch`` the page sends back as ``based_on`` when it saves them.
    """
    heard = _heard_by_device(conn, now)
    plan_row = conn.execute(
        "SELECT width, height, file_name, updated_at, width_mm, units FROM floor_plan WHERE id = 1"
    ).fetchone()
    plan = (
        {
            "width": plan_row["width"],
            "height": plan_row["height"],
            "file_name": plan_row["file_name"],
            "updated_at": plan_row["updated_at"],
            "blank": plan_row["file_name"] is None,
            "width_mm": plan_row["width_mm"],
            "units": plan_row["units"],
        }
        if plan_row is not None
        else None
    )
    rooms = [
        {
            "id": r["id"],
            "name": r["name"],
            "polygon": json.loads(r["polygon"]) if r["polygon"] else None,
        }
        for r in conn.execute("SELECT id, name, polygon FROM place ORDER BY id")
    ]
    marks = ", ".join("?" for _ in _PLACEMENT_FIELDS)
    placed: list[dict[str, Any]] = []
    roaming: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT d.id, d.name, d.place_id, d.pos_x, d.pos_y, d.roaming, d.online,"
        " CASE WHEN fr.set_at IS NOT NULL AND (fs.set_at IS NULL OR fr.set_at >= fs.set_at)"
        " THEN fr.actor ELSE fs.actor END AS set_by,"
        " CASE WHEN fr.set_at IS NOT NULL AND (fs.set_at IS NULL OR fr.set_at >= fs.set_at)"
        " THEN fr.set_at ELSE fs.set_at END AS set_at,"
        " coalesce(lb.batch_id, 0) AS last_batch"
        " FROM device d"
        " LEFT JOIN field_source fs ON fs.entity = 'device' AND fs.entity_id = d.id"
        " AND fs.field = 'place_id'"
        " LEFT JOIN field_source fr ON fr.entity = 'device' AND fr.entity_id = d.id"
        " AND fr.field = 'roaming'"
        " LEFT JOIN (SELECT entity_id, MAX(batch_id) AS batch_id FROM change"
        f" WHERE entity = 'device' AND field IN ({marks}) GROUP BY entity_id) lb"
        " ON lb.entity_id = d.id"
        " ORDER BY d.id",
        _PLACEMENT_FIELDS,
    )
    for r in rows:
        base: dict[str, Any] = {"device_id": r["id"], "name": r["name"]}
        if r["id"] in heard:
            base["heard"] = heard[r["id"]]
        if r["place_id"] is not None and r["roaming"]:
            roaming.append(
                {
                    **base,
                    "place_id": r["place_id"],
                    "set_by": r["set_by"],
                    "set_at": r["set_at"],
                    "online": bool(r["online"]),
                    "last_batch": r["last_batch"],
                }
            )
        elif r["place_id"] is not None and r["pos_x"] is not None and r["pos_y"] is not None:
            placed.append(
                {
                    **base,
                    "place_id": r["place_id"],
                    "x": r["pos_x"],
                    "y": r["pos_y"],
                    "online": bool(r["online"]),
                    "last_batch": r["last_batch"],
                }
            )
        else:
            unplaced.append({**base, "place_id": r["place_id"], "last_batch": r["last_batch"]})
    return {
        "plan": plan,
        "rooms": rooms,
        "placed": placed,
        "roaming": roaming,
        "unplaced": unplaced,
        "shapes": plan_shapes(conn),
        "shapes_batch": last_shapes_batch(conn),
    }
