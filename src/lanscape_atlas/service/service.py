"""The Service: the only path for user/Claude writes.

Every write records per-field provenance in ``field_source`` and an undoable
``change`` row (grouped into a ``batch``). ``old_actor``/``old_set_at`` on each
change capture the ``field_source`` row as it was *before* the write so undo
(Task 6) can restore provenance as well as values. Values are stored as JSON
text so ``None`` and numbers round-trip.

Transactions: the public user-facing writes (``update_device``, ``set_place``,
``add_link``, ``remove_link``, ``create_placeholder_device``, ``claim_interface``,
``claim_ble``, ``unclaim_ble``, ``link_scanner``, ``undo``) each own one
``BEGIN IMMEDIATE`` transaction, hold ``self.lock`` for its duration, and
publish ``change.applied`` after commit. The Bluetooth collector writes
(``record_ble_minutes``, ``upsert_scanners``) and the prunes
(``prune_observations``, ``prune_ble``) also own one transaction each but are
not change-logged and publish nothing. ``begin_batch`` and
``record_collector_field`` do NOT open or commit a transaction: the caller
(the Ingestor) owns the transaction, should hold ``self.lock`` around it, and
publishes any events itself.

Row-level change shapes (``field='*'``): ``old_value`` null = creation,
``new_value`` null = deletion, the other side holding the full row as JSON. A
deleted device's JSON also carries its ``field_source`` rows under
``"_field_source"`` (and its ``topology_layout`` position under ``"_layout"``,
if any) so undo restores provenance and layout with the row. A claim logs
``interface.device_id`` and ``service.device_id`` moves as plain field changes
(``old_actor``/``old_set_at`` NULL: those columns have no ``field_source``).

Bluetooth: a BLE claim logs the ``ble_identity`` row as a row-level creation
(after the new device's creation and owned ``name``/``type`` when it claims as a
new device); an unclaim logs it as a row-level deletion, so undo re-inserts it
with the same id. ``link_scanner`` is a plain owned field change on
``ble_scanner.device_id``. ``ble_sighting`` rows are collector data: deleting a
device (e.g. undoing a claim that created it) deletes its sightings, and undo
never brings them back.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from lanscape_atlas.bluetooth.aggregator import MinuteRow
from lanscape_atlas.bluetooth.match import Identity, check_canonical
from lanscape_atlas.clock import Clock, iso
from lanscape_atlas.events import EVENT_CHANGE_APPLIED, Event, EventBus
from lanscape_atlas.floorplan.geometry import check_aspect, parse_polygon, point_in_polygon
from lanscape_atlas.service import queries

USER_FIELDS = frozenset(
    {"name", "type", "icon", "place_id", "pos_x", "pos_y", "owner", "notes", "trust", "scan_policy"}
)
PROVENANCE_FIELDS = frozenset({"name", "type", "icon"})
# Curated fields a claim carries from a merged-away source onto its target (Ruling 42).
_MERGE_FIELDS = ("name", "type", "icon", "place_id", "owner", "notes", "pos_x", "pos_y")
_POINT_FIELDS = ("pos_x", "pos_y")
# Device fields that make up a placement (as counted by queries.last_placement_batch).
_PLACEMENT_FIELDS = ("place_id", "pos_x", "pos_y", "roaming")

TRUST_VALUES = frozenset({"known", "new", "unwanted"})
SCAN_POLICY_VALUES = frozenset({"normal", "never"})
LINK_KINDS = frozenset({"wired", "wifi", "mesh_backhaul"})
WRITE_ACTORS = frozenset({"user", "claude", "system"})

_PLACE_COLUMNS = ("id", "name", "floor", "polygon")
_TEXT_FIELDS = frozenset({"name", "type", "icon", "owner", "notes"})
_LINK_COLUMNS = ("id", "a_device_id", "b_device_id", "kind", "detail", "source", "layout")
_SERVICE_COLUMNS = (
    "id",
    "device_id",
    "port",
    "proto",
    "name",
    "product",
    "version",
    "source",
    "first_seen",
    "last_seen",
)
_FLOOR_PLAN_COLUMNS = ("id", "width", "height", "sha256", "file_name", "updated_at")
_BLE_IDENTITY_COLUMNS = ("id", "device_id", "kind", "value", "created_at")
# ble_sighting.minute format; strings in it compare chronologically.
_BLE_MINUTE_FMT = "%Y-%m-%dT%H:%MZ"
_FIELD_SOURCE_KEY = "_field_source"
# A deleted device's saved topology position ({"x", "y"}), present only if it had one.
_LAYOUT_KEY = "_layout"
_DEVICE_COLUMNS = (
    "id",
    "name",
    "type",
    "icon",
    "place_id",
    "pos_x",
    "pos_y",
    "owner",
    "notes",
    "trust",
    "scan_policy",
    "roaming",
    "online",
    "first_seen",
    "last_seen",
)


class UndoConflict(Exception):
    """Raised when a later, still-live batch changed something the undone batch touched."""

    def __init__(self, conflicting: list[int]) -> None:
        super().__init__(f"undo conflicts with later batches {conflicting}")
        self.conflicting = conflicting


class StaleWrite(Exception):
    """Raised when a device's placement changed after the batch the client last saw."""

    def __init__(self, conflicting: list[int]) -> None:
        super().__init__(f"placement changed since it was loaded for devices {conflicting}")
        self.conflicting = conflicting


@dataclass(frozen=True)
class Placement:
    """One device's requested placement for ``Service.place_devices``.

    ``roaming=True``: ``place_id`` set, ``x``/``y`` None. ``roaming=False``: a point
    (``x`` and ``y``) inside the room's polygon, a room with no point, or no
    ``place_id`` and no point (unplaced).
    """

    device_id: int
    place_id: int | None
    x: float | None
    y: float | None
    roaming: bool = False


_ENTITY_COLUMNS: dict[str, frozenset[str]] = {
    "device": frozenset(_DEVICE_COLUMNS) - {"id"},
    "link": frozenset(_LINK_COLUMNS) - {"id"},
    "place": frozenset(_PLACE_COLUMNS) - {"id"},
    # Only claim_interface writes these; device_id is their one undoable field.
    "interface": frozenset({"device_id"}),
    "service": frozenset({"device_id"}),
    "floor_plan": frozenset(_FLOOR_PLAN_COLUMNS) - {"id"},
    # claim_ble/unclaim_ble write these rows whole (field='*').
    "ble_identity": frozenset({"device_id", "kind", "value", "created_at"}),
    # Only link_scanner writes this; name/last_seen are collector data, not logged.
    "ble_scanner": frozenset({"device_id"}),
}
_ROW_COLUMNS: dict[str, tuple[str, ...]] = {
    "device": _DEVICE_COLUMNS,
    "link": _LINK_COLUMNS,
    "service": _SERVICE_COLUMNS,
    "floor_plan": _FLOOR_PLAN_COLUMNS,
    "ble_identity": _BLE_IDENTITY_COLUMNS,
}


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class Service:
    def __init__(
        self,
        conn: sqlite3.Connection,
        events: EventBus,
        clock: Clock,
        *,
        ip_match_window_min: int = 10,
    ) -> None:
        self.conn = conn
        self.events = events
        self.clock = clock
        # Settings.ip_match_window_min; readers pass it to the queries that decide which
        # open lease is current (Ruling 44).
        self.ip_match_window_min = ip_match_window_min
        # Process-wide write lock; the Ingestor acquires the same lock.
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ helpers

    def _now(self) -> str:
        return iso(self.clock())

    @contextmanager
    def _write_txn(self) -> Iterator[None]:
        """Hold the write lock and one BEGIN IMMEDIATE transaction.

        IMMEDIATE takes the write lock up front so a concurrent writer makes us
        wait on ``busy_timeout`` instead of failing on a read->write upgrade.
        """
        with self.lock:
            if self.conn.in_transaction:
                raise RuntimeError("Service write called inside an open transaction")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                # COMMIT inside the try: if it fails we roll back rather than
                # leaving the connection stuck in an open transaction.
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _publish(self, batch_id: int, actor: str) -> None:
        if batch_id:
            self.events.publish(Event(EVENT_CHANGE_APPLIED, {"batch_id": batch_id, "actor": actor}))

    @staticmethod
    def _check_actor(actor: str) -> None:
        if actor not in WRITE_ACTORS:
            raise ValueError(f"actor must be one of {sorted(WRITE_ACTORS)}, got {actor!r}")

    def _device_row(self, device_id: int) -> sqlite3.Row:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM device WHERE id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no device with id {device_id}")
        return row

    def _source(self, entity: str, entity_id: int, field: str) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            "SELECT actor, set_at FROM field_source WHERE entity = ? AND entity_id = ?"
            " AND field = ?",
            (entity, entity_id, field),
        ).fetchone()
        if row is None:
            return None, None
        return str(row[0]), str(row[1])

    def _log_change(
        self,
        batch_id: int,
        entity: str,
        entity_id: int,
        field: str,
        old: Any,
        new: Any,
        old_actor: str | None = None,
        old_set_at: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO change(batch_id, entity, entity_id, field, old_value, new_value,"
            " old_actor, old_set_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, entity, entity_id, field, _dump(old), _dump(new), old_actor, old_set_at),
        )

    def _write_field(
        self,
        batch_id: int,
        entity: str,
        entity_id: int,
        field: str,
        old: Any,
        new: Any,
        actor: str,
    ) -> None:
        """Log the change (with prior provenance), write the value, upsert field_source.

        ``entity`` and ``field`` are always internal whitelisted identifiers.
        """
        old_actor, old_set_at = self._source(entity, entity_id, field)
        self._log_change(batch_id, entity, entity_id, field, old, new, old_actor, old_set_at)
        self.conn.execute(f"UPDATE {entity} SET {field} = ? WHERE id = ?", (new, entity_id))
        self.conn.execute(
            "INSERT INTO field_source(entity, entity_id, field, actor, set_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(entity, entity_id, field)"
            " DO UPDATE SET actor = excluded.actor, set_at = excluded.set_at",
            (entity, entity_id, field, actor, self._now()),
        )

    def _validate_device_fields(self, fields: Mapping[str, Any]) -> None:
        unknown = set(fields) - USER_FIELDS
        if unknown:
            raise ValueError(f"not user-editable device fields: {sorted(unknown)}")
        for field, value in fields.items():
            if field == "trust" and value not in TRUST_VALUES:
                raise ValueError(f"trust must be one of {sorted(TRUST_VALUES)}, got {value!r}")
            if field == "scan_policy" and value not in SCAN_POLICY_VALUES:
                raise ValueError(
                    f"scan_policy must be one of {sorted(SCAN_POLICY_VALUES)}, got {value!r}"
                )
            if field in ("pos_x", "pos_y") and value is not None and not _is_number(value):
                raise ValueError(f"{field} must be a number or None, got {value!r}")
            if (
                field == "place_id"
                and value is not None
                and (not isinstance(value, int) or isinstance(value, bool))
            ):
                raise ValueError(f"place_id must be an int or None, got {value!r}")
            if field in _TEXT_FIELDS and value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string or None, got {value!r}")

    @staticmethod
    def _leaving_room(row: sqlite3.Row, new_place_id: int | None) -> dict[str, Any]:
        """Placement fields to clear when a device's ``place_id`` changes.

        The point belongs to the old room, so it is cleared. ``roaming`` is kept
        for another room and cleared when the device leaves every room.
        """
        clear: dict[str, Any] = {f: None for f in ("pos_x", "pos_y") if row[f] is not None}
        if new_place_id is None and row["roaming"]:
            clear["roaming"] = 0
        return clear

    # ------------------------------------------------------------ batch / collector

    def begin_batch(self, actor: str) -> int:
        """Insert a ``batch`` row and return its id. Caller owns the transaction."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO batch(actor, at) VALUES (?, ?)", (actor, self._now())
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def collector_may_set(self, device_id: int, field: str) -> bool:
        """True if the field has no owner yet or is owned by a collector."""
        actor, _ = self._source("device", device_id, field)
        return actor is None or actor.startswith("collector:")

    def record_collector_field(
        self, batch_id: int, device_id: int, field: str, value: Any, collector: str
    ) -> bool:
        """Write a collector-discovered ``name``/``type``/``icon`` into ``batch_id``.

        Returns True if it wrote; False if a user/claude owns the field or the
        value is unchanged. Does not open/commit a transaction or publish events:
        the caller (Ingestor) owns both and should hold ``self.lock``.
        """
        if field not in PROVENANCE_FIELDS:
            raise ValueError(f"collectors may only set {sorted(PROVENANCE_FIELDS)}, not {field!r}")
        with self.lock:
            if not self.collector_may_set(device_id, field):
                return False
            old = self._device_row(device_id)[field]
            if old == value:
                return False
            self._write_field(
                batch_id, "device", device_id, field, old, value, f"collector:{collector}"
            )
            return True

    # --------------------------------------------------------------- public writes

    def update_device(self, actor: str, device_id: int, fields: dict[str, Any]) -> int:
        """Set user-editable fields; returns the batch id, or 0 if nothing changed.

        ``pos_x``/``pos_y`` are refused (``ValueError``): points go through
        ``place_devices``, which checks containment. A ``place_id`` change
        clears the device's point in the same batch, and a change to ``None``
        also clears ``roaming`` (roaming needs a room).
        """
        self._check_actor(actor)
        if "pos_x" in fields or "pos_y" in fields:
            raise ValueError("pos_x and pos_y cannot be set here; set positions with place_devices")
        self._validate_device_fields(fields)
        batch_id = 0
        with self._write_txn():
            row = self._device_row(device_id)
            place_id = fields.get("place_id")
            if (
                place_id is not None
                and self.conn.execute("SELECT 1 FROM place WHERE id = ?", (place_id,)).fetchone()
                is None
            ):
                raise ValueError(f"no place with id {place_id}")
            changed = {f: v for f, v in fields.items() if row[f] != v}
            if "place_id" in changed:
                changed.update(self._leaving_room(row, changed["place_id"]))
            if changed:
                batch_id = self.begin_batch(actor)
                for field, value in changed.items():
                    self._write_field(
                        batch_id, "device", device_id, field, row[field], value, actor
                    )
        self._publish(batch_id, actor)
        return batch_id

    def set_place(self, actor: str, device_id: int, place_name: str) -> int:
        """Assign a device to the named place, creating the place if needed.

        Moving to another place clears the device's point in the same batch;
        ``roaming`` is kept.
        """
        self._check_actor(actor)
        if not isinstance(place_name, str) or not place_name.strip():
            raise ValueError("place_name must be a non-empty string")
        batch_id = 0
        with self._write_txn():
            row = self._device_row(device_id)
            prow = self.conn.execute(
                "SELECT id FROM place WHERE name = ?", (place_name,)
            ).fetchone()
            if prow is None:
                batch_id = self.begin_batch(actor)
                cur = self.conn.execute("INSERT INTO place(name) VALUES (?)", (place_name,))
                assert cur.lastrowid is not None
                place_id = cur.lastrowid
                self._write_field(batch_id, "place", place_id, "name", None, place_name, actor)
            else:
                place_id = int(prow[0])
            if row["place_id"] != place_id:
                if not batch_id:
                    batch_id = self.begin_batch(actor)
                self._write_field(
                    batch_id, "device", device_id, "place_id", row["place_id"], place_id, actor
                )
                # The point belonged to the old room; roaming stays (it has a room).
                for field, value in self._leaving_room(row, place_id).items():
                    self._write_field(
                        batch_id, "device", device_id, field, row[field], value, actor
                    )
        self._publish(batch_id, actor)
        return batch_id

    def save_room(self, actor: str, name: str, polygon: object) -> int:
        """Create the named room (place) if new, and store its polygon; returns the batch id.

        The name is stripped of leading and trailing whitespace first.

        ``polygon`` is validated by ``parse_polygon`` (``ValueError``, nothing
        written) and stored as ``json.dumps`` of the point lists so the text is
        stable. Creation follows the ``set_place`` pattern, so undo deletes the
        place. Redrawing a room clears, in the same batch, the point of every
        device in it whose point falls outside the new polygon, so undo restores
        the old polygon and those points. Returns 0 if nothing changed.
        """
        self._check_actor(actor)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        name = name.strip()
        text = json.dumps([list(p) for p in parse_polygon(polygon)])
        batch_id = 0
        with self._write_txn():
            prow = self.conn.execute(
                "SELECT id, polygon FROM place WHERE name = ?", (name,)
            ).fetchone()
            if prow is None:
                batch_id = self.begin_batch(actor)
                cur = self.conn.execute("INSERT INTO place(name) VALUES (?)", (name,))
                assert cur.lastrowid is not None
                place_id = cur.lastrowid
                self._write_field(batch_id, "place", place_id, "name", None, name, actor)
                old = None
            else:
                place_id = int(prow[0])
                old = prow[1]
            if old != text:
                if not batch_id:
                    batch_id = self.begin_batch(actor)
                self._write_field(batch_id, "place", place_id, "polygon", old, text, actor)
                self._clear_stranded(batch_id, place_id, json.loads(text), actor)
        self._publish(batch_id, actor)
        return batch_id

    def _clear_stranded(
        self, batch_id: int, place_id: int, polygon_points: object, actor: str
    ) -> None:
        """Clear (logged) the point of each device in ``place_id`` now outside its polygon."""
        polygon = parse_polygon(polygon_points)
        for row in self.conn.execute(
            "SELECT id, pos_x, pos_y FROM device WHERE place_id = ?"
            " AND (pos_x IS NOT NULL OR pos_y IS NOT NULL) ORDER BY id",
            (place_id,),
        ).fetchall():
            x, y = row["pos_x"], row["pos_y"]
            if x is not None and y is not None and point_in_polygon(x, y, polygon):
                continue
            for field in ("pos_x", "pos_y"):
                if row[field] is not None:
                    self._write_field(
                        batch_id, "device", int(row["id"]), field, row[field], None, actor
                    )

    def rename_room(self, actor: str, place_id: int, new_name: str) -> int:
        """Rename a place, keeping its id (and so its devices); returns the batch id.

        ``KeyError`` for an unknown place; ``ValueError`` for a blank or taken name.
        Returns 0 if the name is unchanged.
        """
        self._check_actor(actor)
        if not isinstance(new_name, str) or not new_name.strip():
            raise ValueError("new_name must be a non-empty string")
        new_name = new_name.strip()
        batch_id = 0
        with self._write_txn():
            row = self.conn.execute("SELECT name FROM place WHERE id = ?", (place_id,)).fetchone()
            if row is None:
                raise KeyError(f"no place with id {place_id}")
            if row[0] != new_name:
                if self.conn.execute("SELECT 1 FROM place WHERE name = ?", (new_name,)).fetchone():
                    raise ValueError(f"a place named {new_name!r} already exists")
                batch_id = self.begin_batch(actor)
                self._write_field(batch_id, "place", place_id, "name", row[0], new_name, actor)
        self._publish(batch_id, actor)
        return batch_id

    def set_floor_plan(
        self, actor: str, *, width: int, height: int, sha256: str, file_name: str
    ) -> int:
        """Create or replace the single floor plan image row (id 1); returns the batch id.

        A replacement must keep the existing aspect ratio (``ValueError``
        otherwise, nothing written). The first upload logs ``sha256`` with a
        NULL old value, which undo reads as a creation and answers by deleting
        the row.
        """
        self._check_actor(actor)
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ValueError("width and height must be positive integers")
        if not isinstance(sha256, str) or not sha256:
            raise ValueError("sha256 must be a non-empty string")
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("file_name must be a non-empty string")
        with self._write_txn():
            cur = self.conn.execute("SELECT * FROM floor_plan WHERE id = 1").fetchone()
            if cur is not None:
                check_aspect(int(cur["width"]), int(cur["height"]), width, height)
            batch_id = self.begin_batch(actor)
            now = self._now()
            if cur is None:
                self.conn.execute(
                    "INSERT INTO floor_plan(id, width, height, sha256, file_name, updated_at)"
                    " VALUES (1, ?, ?, ?, ?, ?)",
                    (width, height, sha256, file_name, now),
                )
                # sha256 first with old=None is the creation marker undo looks for.
                for field, value in (
                    ("sha256", sha256),
                    ("width", width),
                    ("height", height),
                    ("file_name", file_name),
                    ("updated_at", now),
                ):
                    self._log_change(batch_id, "floor_plan", 1, field, None, value)
                    self._restore_source("floor_plan", 1, field, actor, now)
            else:
                for field, value in (
                    ("width", width),
                    ("height", height),
                    ("sha256", sha256),
                    ("file_name", file_name),
                    ("updated_at", now),
                ):
                    self._write_field(batch_id, "floor_plan", 1, field, cur[field], value, actor)
        self._publish(batch_id, actor)
        return batch_id

    def place_devices(
        self, actor: str, placements: Sequence[Placement], based_on: Mapping[int, int]
    ) -> int:
        """Set ``place_id``/``pos_x``/``pos_y``/``roaming`` for several devices in one batch.

        ``based_on`` maps each device id to the last placement batch the client
        saw for it (0 if none; a missing entry counts as 0). In one transaction,
        and before anything is written: every placement is validated (shape,
        known device, known place, point inside the room's polygon:
        ``ValueError``, ``KeyError`` for an unknown device), then any device
        whose ``queries.last_placement_batch`` is newer than ``based_on`` raises
        ``StaleWrite`` listing all such ids. Only changed fields are logged.
        Returns the batch id, or 0 if nothing changed.
        """
        self._check_actor(actor)
        for p in placements:
            self._validate_placement(p)
        ids = [p.device_id for p in placements]
        dupes = sorted({d for d in ids if ids.count(d) > 1})
        if dupes:
            raise ValueError(f"devices listed more than once: {dupes}")
        for device_id, seen in based_on.items():
            if isinstance(seen, bool) or not isinstance(seen, int):
                raise ValueError(f"based_on[{device_id}] must be an int, got {seen!r}")
        batch_id = 0
        with self._write_txn():
            rows = {p.device_id: self._device_row(p.device_id) for p in placements}
            polygons: dict[int, list[tuple[float, float]] | None] = {}
            for p in placements:
                if p.place_id is None:
                    continue
                if p.place_id not in polygons:
                    prow = self.conn.execute(
                        "SELECT polygon FROM place WHERE id = ?", (p.place_id,)
                    ).fetchone()
                    if prow is None:
                        raise ValueError(f"no place with id {p.place_id}")
                    polygons[p.place_id] = (
                        None if prow[0] is None else parse_polygon(json.loads(prow[0]))
                    )
                if p.x is None or p.y is None:
                    continue
                polygon = polygons[p.place_id]
                if polygon is None:
                    raise ValueError(f"place {p.place_id} has no polygon; draw the room first")
                if not point_in_polygon(p.x, p.y, polygon):
                    raise ValueError(
                        f"point ({p.x}, {p.y}) for device {p.device_id} is outside"
                        f" place {p.place_id}"
                    )
            stale = sorted(
                d for d in rows if queries.last_placement_batch(self.conn, d) > based_on.get(d, 0)
            )
            if stale:
                raise StaleWrite(stale)
            for p in placements:
                row = rows[p.device_id]
                target = {
                    "place_id": p.place_id,
                    "pos_x": p.x,
                    "pos_y": p.y,
                    "roaming": 1 if p.roaming else 0,
                }
                for field, value in target.items():
                    if row[field] == value:
                        continue
                    if not batch_id:
                        batch_id = self.begin_batch(actor)
                    self._write_field(
                        batch_id, "device", p.device_id, field, row[field], value, actor
                    )
        self._publish(batch_id, actor)
        return batch_id

    @staticmethod
    def _validate_placement(p: Placement) -> None:
        """Shape checks for one Placement (no database access); ``ValueError`` on failure."""
        if not isinstance(p, Placement):
            raise ValueError(f"expected a Placement, got {p!r}")
        if isinstance(p.device_id, bool) or not isinstance(p.device_id, int):
            raise ValueError(f"device_id must be an int, got {p.device_id!r}")
        if p.place_id is not None and (
            isinstance(p.place_id, bool) or not isinstance(p.place_id, int)
        ):
            raise ValueError(f"place_id must be an int or None, got {p.place_id!r}")
        for label, value in (("x", p.x), ("y", p.y)):
            if value is not None and not _is_number(value):
                raise ValueError(f"{label} must be a number or None, got {value!r}")
        if not isinstance(p.roaming, bool):
            raise ValueError(f"roaming must be a bool, got {p.roaming!r}")
        has_x, has_y = p.x is not None, p.y is not None
        if p.roaming:
            if p.place_id is None or has_x or has_y:
                raise ValueError(f"device {p.device_id}: roaming needs a place_id and no point")
            return
        if has_x != has_y:
            raise ValueError(f"device {p.device_id}: x and y must both be set or both be None")
        if has_x and p.place_id is None:
            raise ValueError(f"device {p.device_id}: a point needs a place_id")

    def add_link(
        self,
        actor: str,
        a_device_id: int,
        b_device_id: int,
        kind: str,
        detail: str | None = None,
    ) -> int:
        """Add a manual link between two devices; logged as ``entity='link', field='*'``."""
        self._check_actor(actor)
        if kind not in LINK_KINDS:
            raise ValueError(f"link kind must be one of {sorted(LINK_KINDS)}, got {kind!r}")
        if a_device_id == b_device_id:
            raise ValueError("a link needs two different devices")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("detail must be a string or None")
        with self._write_txn():
            self._device_row(a_device_id)
            self._device_row(b_device_id)
            batch_id = self.begin_batch(actor)
            cur = self.conn.execute(
                "INSERT INTO link(a_device_id, b_device_id, kind, detail, source)"
                " VALUES (?, ?, ?, ?, 'manual')",
                (a_device_id, b_device_id, kind, detail),
            )
            assert cur.lastrowid is not None
            link_id = cur.lastrowid
            self._log_change(batch_id, "link", link_id, "*", None, self._link_dict(link_id))
        self._publish(batch_id, actor)
        return batch_id

    def remove_link(self, actor: str, link_id: int) -> int:
        """Delete a link; the change row keeps the full row as ``old_value``."""
        self._check_actor(actor)
        with self._write_txn():
            old = self._link_dict(link_id)
            batch_id = self.begin_batch(actor)
            self._log_change(batch_id, "link", link_id, "*", old, None)
            self._delete_entity("link", link_id)
        self._publish(batch_id, actor)
        return batch_id

    def _link_dict(self, link_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT {', '.join(_LINK_COLUMNS)} FROM link WHERE id = ?", (link_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no link with id {link_id}")
        return {c: row[c] for c in _LINK_COLUMNS}

    def create_placeholder_device(self, actor: str, name: str, type: str) -> tuple[int, int]:
        """Create a ``trust='known'`` device with no interfaces (e.g. an unmanaged switch).

        Logged as a row-level ``field='*'`` creation change followed by owned
        ``name``/``type`` field changes, so collectors never rename it.
        """
        self._check_actor(actor)
        self._check_new_device(name, type)
        with self._write_txn():
            batch_id = self.begin_batch(actor)
            device_id = self._create_device(batch_id, actor, name, type)
        self._publish(batch_id, actor)
        return device_id, batch_id

    @staticmethod
    def _check_new_device(name: object, type: object) -> None:
        for label, value in (("name", name), ("type", type)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string")

    def _create_device(self, batch_id: int, actor: str, name: str, type: str) -> int:
        """Insert a ``trust='known'`` device into ``batch_id``; returns its id.

        Logs a row-level ``field='*'`` creation then owned ``name``/``type``
        field changes. Caller owns the transaction and validated the inputs.
        """
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO device(trust, first_seen, last_seen) VALUES ('known', ?, ?)",
            (now, now),
        )
        assert cur.lastrowid is not None
        device_id = cur.lastrowid
        created = self._device_row(device_id)
        self._log_change(
            batch_id, "device", device_id, "*", None, {c: created[c] for c in _DEVICE_COLUMNS}
        )
        self._write_field(batch_id, "device", device_id, "name", None, name, actor)
        self._write_field(batch_id, "device", device_id, "type", None, type, actor)
        return device_id

    def claim_interface(self, actor: str, interface_id: int, device_id: int) -> int:
        """Move an interface (e.g. a phone's random MAC) onto ``device_id``; returns the batch id.

        Leases and observations key on ``interface_id`` and so follow it. If
        the source device is left with no interfaces it is merged away in the
        same batch: its services move to the target (on a ``(port, proto)``
        collision the target's row is kept and the source's row is logged as
        deleted), then the source row is deleted with its full JSON (and
        provenance and any saved topology layout, Ruling 32) logged so undo
        re-inserts it with its original id. A saved layout never blocks a claim.

        Raises ``KeyError`` for an unknown interface or target, and
        ``ValueError`` if the target already owns the interface, or if the
        source would be deleted but still has links or a non-null curated field
        (name, type, icon, place_id, owner, notes, pos_x, pos_y) whose value
        differs from the target's non-null value (Ruling 42). Curated source
        fields the target lacks are copied onto it, with their provenance, in
        the same batch so undo restores both devices exactly. ``trust`` and
        ``scan_policy`` never block. The source's point (``pos_x``/``pos_y``) is
        copied only as a pair together with its room, and never onto a roaming
        target (Ruling 23a). Nothing is written on refusal.
        """
        self._check_actor(actor)
        with self._write_txn():
            irow = self.conn.execute(
                "SELECT device_id FROM interface WHERE id = ?", (interface_id,)
            ).fetchone()
            if irow is None:
                raise KeyError(f"no interface with id {interface_id}")
            self._device_row(device_id)
            source = int(irow[0])
            if source == device_id:
                raise ValueError(f"interface {interface_id} already belongs to device {device_id}")
            remaining = self.conn.execute(
                "SELECT count(*) FROM interface WHERE device_id = ? AND id != ?",
                (source, interface_id),
            ).fetchone()[0]
            delete_source = remaining == 0
            to_copy = self._plan_merge(source, device_id) if delete_source else []
            batch_id = self.begin_batch(actor)
            self._log_change(batch_id, "interface", interface_id, "device_id", source, device_id)
            self.conn.execute(
                "UPDATE interface SET device_id = ? WHERE id = ?", (device_id, interface_id)
            )
            if delete_source:
                self._copy_fields(batch_id, source, device_id, to_copy)
                self._merge_services(batch_id, source, device_id)
                self._log_change(
                    batch_id, "device", source, "*", self._snapshot("device", source), None
                )
                self._delete_entity("device", source)
        self._publish(batch_id, actor)
        return batch_id

    def _plan_merge(self, source: int, target: int) -> list[str]:
        """Return the curated source fields to copy onto ``target`` (Ruling 42), or refuse.

        ``trust`` and ``scan_policy`` never block, nor does a curated field whose
        value is NULL. A remaining non-null curated field (user/claude-owned, or a
        ``place_id`` whatever its provenance) is copied when the target's value is
        NULL, skipped when equal, and refuses the claim when it differs. Links
        still refuse.
        """
        if self.conn.execute(
            "SELECT 1 FROM link WHERE a_device_id = ? OR b_device_id = ?", (source, source)
        ).fetchone():
            raise ValueError(f"device {source} has links; remove them before claiming")
        src = self._device_row(source)
        tgt = self._device_row(target)
        owned = {
            str(r[0])
            for r in self.conn.execute(
                "SELECT field FROM field_source WHERE entity = 'device' AND entity_id = ?"
                " AND actor NOT LIKE 'collector:%'",
                (source,),
            )
        }
        copy: list[str] = []
        conflicts: list[str] = []
        for field in _MERGE_FIELDS:
            if src[field] is None or (field not in owned and field != "place_id"):
                continue
            if field in _POINT_FIELDS and tgt["roaming"]:
                continue  # a roaming device has no point; the source's point is dropped
            if tgt[field] is None:
                copy.append(field)
            elif tgt[field] != src[field]:
                conflicts.append(field)
        if conflicts:
            raise ValueError(
                f"claim would discard curated fields on device {source}: {', '.join(conflicts)}"
                " \u2014 rename or clear them first"
            )
        # A point travels only as a pair and only with its room: the copied
        # place_id, or a target already in the source's room.
        same_room = tgt["place_id"] is not None and tgt["place_id"] == src["place_id"]
        point = [f for f in copy if f in _POINT_FIELDS]
        if point and (len(point) != 2 or not ("place_id" in copy or same_room)):
            copy = [f for f in copy if f not in _POINT_FIELDS]
        return copy

    def _copy_fields(self, batch_id: int, source: int, target: int, fields: list[str]) -> None:
        """Copy values and their provenance from ``source`` onto ``target`` (logged)."""
        src = self._device_row(source)
        for field in fields:
            old_actor, old_set_at = self._source("device", target, field)
            self._log_change(
                batch_id, "device", target, field, None, src[field], old_actor, old_set_at
            )
            self.conn.execute(f"UPDATE device SET {field} = ? WHERE id = ?", (src[field], target))
            actor, set_at = self._source("device", source, field)
            self._restore_source("device", target, field, actor, set_at)

    def _merge_services(self, batch_id: int, source: int, target: int) -> None:
        for row in self.conn.execute(
            "SELECT id, port, proto FROM service WHERE device_id = ? ORDER BY id", (source,)
        ).fetchall():
            service_id = int(row["id"])
            if self.conn.execute(
                "SELECT 1 FROM service WHERE device_id = ? AND port = ? AND proto = ?",
                (target, row["port"], row["proto"]),
            ).fetchone():
                self._log_change(
                    batch_id,
                    "service",
                    service_id,
                    "*",
                    self._snapshot("service", service_id),
                    None,
                )
                self._delete_entity("service", service_id)
            else:
                self._log_change(batch_id, "service", service_id, "device_id", source, target)
                self.conn.execute(
                    "UPDATE service SET device_id = ? WHERE id = ?", (target, service_id)
                )

    # ------------------------------------------------------------------ Bluetooth

    def claim_ble(
        self,
        actor: str,
        identity: Identity,
        *,
        device_id: int | None = None,
        new_name: str | None = None,
        new_type: str | None = None,
    ) -> tuple[int, int]:
        """Attach a BLE identity to a device; returns ``(device_id, batch_id)``.

        Exactly one of ``device_id`` (an existing device) or ``new_name`` (with a
        non-empty ``new_type``: create the device, logged as
        ``create_placeholder_device`` logs it, in the same batch). The identity
        row is logged as a row-level ``field='*'`` creation after the device, so
        undo deletes the identity before the device. Raises ``ValueError`` for a
        bad actor, a non-canonical identity, a bad target combination or an
        identity that is already claimed (nothing is written), and ``KeyError``
        for an unknown ``device_id``.
        """
        self._check_actor(actor)
        check_canonical(identity)
        if (device_id is None) == (new_name is None):
            raise ValueError("claim needs exactly one of device_id or new_name")
        if device_id is None:
            self._check_new_device(new_name, new_type)
        elif new_type is not None:
            raise ValueError("new_type is only valid with new_name")
        elif not isinstance(device_id, int) or isinstance(device_id, bool):
            raise ValueError(f"device_id must be an int, got {device_id!r}")
        with self._write_txn():
            claimed = self.conn.execute(
                "SELECT id, device_id FROM ble_identity WHERE kind = ? AND value = ?",
                (identity.kind, identity.value),
            ).fetchone()
            if claimed is not None:
                raise ValueError(
                    f"{identity.key()} is already claimed by device {claimed['device_id']}"
                )
            if device_id is not None:
                self._device_row(device_id)
            batch_id = self.begin_batch(actor)
            if device_id is None:
                assert new_name is not None and new_type is not None
                device_id = self._create_device(batch_id, actor, new_name, new_type)
            cur = self.conn.execute(
                "INSERT INTO ble_identity(device_id, kind, value, created_at) VALUES (?, ?, ?, ?)",
                (device_id, identity.kind, identity.value, self._now()),
            )
            assert cur.lastrowid is not None
            identity_id = cur.lastrowid
            self._log_change(
                batch_id,
                "ble_identity",
                identity_id,
                "*",
                None,
                self._row_dict("ble_identity", identity_id),
            )
        self._publish(batch_id, actor)
        return device_id, batch_id

    def unclaim_ble(self, actor: str, identity_id: int) -> int:
        """Delete a BLE identity; returns the batch id. The device and its sightings stay.

        Logged as a row-level deletion holding the full row, so undo re-inserts
        it with the same id. Raises ``KeyError`` for an unknown identity.
        """
        self._check_actor(actor)
        with self._write_txn():
            old = self._row_dict("ble_identity", identity_id)
            batch_id = self.begin_batch(actor)
            self._log_change(batch_id, "ble_identity", identity_id, "*", old, None)
            self._delete_entity("ble_identity", identity_id)
        self._publish(batch_id, actor)
        return batch_id

    def link_scanner(self, actor: str, scanner_id: int, device_id: int | None) -> int:
        """Link a BLE scanner to the atlas device it runs on (None unlinks).

        A plain owned field change on ``ble_scanner.device_id``; returns the
        batch id, or 0 if unchanged. Raises ``KeyError`` for an unknown scanner
        or device.
        """
        self._check_actor(actor)
        if device_id is not None and (
            not isinstance(device_id, int) or isinstance(device_id, bool)
        ):
            raise ValueError(f"device_id must be an int or None, got {device_id!r}")
        with self._write_txn():
            row = self.conn.execute(
                "SELECT device_id FROM ble_scanner WHERE id = ?", (scanner_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no ble_scanner with id {scanner_id}")
            if device_id is not None:
                self._device_row(device_id)
            old = row["device_id"]
            if old == device_id:
                return 0
            batch_id = self.begin_batch(actor)
            self._write_field(
                batch_id, "ble_scanner", scanner_id, "device_id", old, device_id, actor
            )
        self._publish(batch_id, actor)
        return batch_id

    def upsert_scanners(self, scanners: Mapping[str, str | None], seen_at: datetime) -> None:
        """Record scanners HA reports (source -> name). Not change-logged.

        Creates unknown sources; for known ones updates ``name`` (a None name
        keeps the stored one) and moves ``last_seen`` forward.
        """
        if not scanners:
            return
        at = iso(seen_at)
        with self._write_txn():
            for source, name in scanners.items():
                self.conn.execute(
                    "INSERT INTO ble_scanner(source, name, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(source) DO UPDATE SET"
                    " name = coalesce(excluded.name, name),"
                    " last_seen = max(last_seen, excluded.last_seen)",
                    (source, name, at, at),
                )

    def record_ble_minutes(self, rows: Sequence[MinuteRow]) -> int:
        """Upsert per-minute sightings in one transaction; returns rows written.

        Not change-logged and publishes nothing. Each row's ``source`` resolves
        to a ``ble_scanner`` (created with ``name`` NULL if unknown; ``last_seen``
        moves forward). On the ``(device, scanner, minute)`` key: ``max_rssi`` is
        the max, ``adverts`` the sum, ``last_at`` the max, and ``place_id`` keeps
        the first snapshot of the scanner's linked device's room. Rows whose
        device no longer exists (a claim undone since the minute was drained)
        are skipped.
        """
        if not rows:
            return 0
        written = 0
        with self._write_txn():
            for r in rows:
                if (
                    self.conn.execute(
                        "SELECT 1 FROM device WHERE id = ?", (r.device_id,)
                    ).fetchone()
                    is None
                ):
                    continue
                last_at = iso(r.last_at)
                self.conn.execute(
                    "INSERT INTO ble_scanner(source, first_seen, last_seen) VALUES (?, ?, ?)"
                    " ON CONFLICT(source) DO UPDATE SET"
                    " last_seen = max(last_seen, excluded.last_seen)",
                    (r.source, last_at, last_at),
                )
                scanner = self.conn.execute(
                    "SELECT s.id, d.place_id FROM ble_scanner s"
                    " LEFT JOIN device d ON d.id = s.device_id WHERE s.source = ?",
                    (r.source,),
                ).fetchone()
                self.conn.execute(
                    "INSERT INTO ble_sighting(device_id, scanner_id, minute, place_id,"
                    " max_rssi, adverts, last_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(device_id, scanner_id, minute) DO UPDATE SET"
                    " max_rssi = max(max_rssi, excluded.max_rssi),"
                    " adverts = adverts + excluded.adverts,"
                    " last_at = max(last_at, excluded.last_at)",
                    (
                        r.device_id,
                        scanner["id"],
                        r.minute,
                        scanner["place_id"],
                        r.max_rssi,
                        r.adverts,
                        last_at,
                    ),
                )
                written += 1
        return written

    def prune_ble(self, older_than_days: int) -> int:
        """Delete ``ble_sighting`` rows whose minute is older than the cutoff; returns the count.

        Not logged in ``change`` (sightings are collector data).
        """
        self._check_days(older_than_days)
        cutoff = (self.clock() - timedelta(days=older_than_days)).strftime(_BLE_MINUTE_FMT)
        with self._write_txn():
            cur = self.conn.execute("DELETE FROM ble_sighting WHERE minute < ?", (cutoff,))
        return cur.rowcount

    @staticmethod
    def _check_days(older_than_days: object) -> None:
        if (
            not isinstance(older_than_days, int)
            or isinstance(older_than_days, bool)
            or older_than_days < 0
        ):
            raise ValueError(f"older_than_days must be a non-negative int, got {older_than_days!r}")

    def prune_observations(self, older_than_days: int) -> int:
        """Delete observations with ``seen_at`` older than ``older_than_days``; returns the count.

        Not logged in ``change`` (observations are raw input, not curated state).
        """
        self._check_days(older_than_days)
        cutoff = iso(self.clock() - timedelta(days=older_than_days))
        with self._write_txn():
            cur = self.conn.execute("DELETE FROM observation WHERE seen_at < ?", (cutoff,))
        return cur.rowcount

    # -------------------------------------------------------------------- undo

    def undo(self, actor: str, batch_id: int) -> int:
        """Revert ``batch_id`` exactly; returns the new batch id.

        The new batch has ``undoes_batch_id = batch_id`` and logs every inverse
        write as an ordinary change row, so undoing it re-applies the original.
        Values *and* ``field_source`` provenance are restored to what they were
        before the batch. Raises ``KeyError`` (unknown batch), ``ValueError``
        ("already undone") or ``UndoConflict`` (a later, still-live batch
        touched something this batch changed, or now references an entity it
        created).
        """
        self._check_actor(actor)
        with self._write_txn():
            if (
                self.conn.execute("SELECT 1 FROM batch WHERE id = ?", (batch_id,)).fetchone()
                is None
            ):
                raise KeyError(f"no batch with id {batch_id}")
            if (
                self.conn.execute(
                    "SELECT 1 FROM batch WHERE undoes_batch_id = ?", (batch_id,)
                ).fetchone()
                is not None
            ):
                raise ValueError("already undone")
            rows = [
                _Change.from_row(r)
                for r in self.conn.execute(
                    "SELECT * FROM change WHERE batch_id = ? ORDER BY id", (batch_id,)
                )
            ]
            conflicting = self._undo_conflicts(batch_id, rows)
            if conflicting:
                raise UndoConflict(conflicting)
            cur = self.conn.execute(
                "INSERT INTO batch(actor, at, undoes_batch_id) VALUES (?, ?, ?)",
                (actor, self._now(), batch_id),
            )
            assert cur.lastrowid is not None
            new_batch = cur.lastrowid
            for ch in reversed(rows):
                self._apply_inverse(new_batch, ch)
            # Raising here rolls the whole undo back.
            self._check_undo_placements(batch_id, new_batch, rows)
        self._publish(new_batch, actor)
        return new_batch

    def _check_undo_placements(self, batch_id: int, new_batch: int, rows: list[_Change]) -> None:
        """After an undo's inverses are applied, refuse a placement that breaks spec section 3.

        Checked: devices whose placement the undone batch touched, and devices in
        a place whose polygon it touched. A point must have a place whose
        *current* polygon contains it, and a roaming device must have a room and
        no point. A violation raises ``UndoConflict`` naming the later live
        batches that redrew that room or changed that device's placement
        (Ruling 23c). With no such batch the undo restores exactly the state
        from before ``batch_id`` (e.g. pre-Phase 2 data), so it is allowed.
        """
        devices = {
            ch.entity_id
            for ch in rows
            if ch.entity == "device" and (ch.field == "*" or ch.field in _PLACEMENT_FIELDS)
        }
        places = {ch.entity_id for ch in rows if ch.entity == "place" and ch.field == "polygon"}
        for pid in places:
            devices.update(
                int(r[0])
                for r in self.conn.execute("SELECT id FROM device WHERE place_id = ?", (pid,))
            )
        live = self._live_filter(batch_id)
        conflicting: set[int] = set()
        for device_id in sorted(devices):
            row = self.conn.execute(
                "SELECT place_id, pos_x, pos_y, roaming FROM device WHERE id = ?", (device_id,)
            ).fetchone()
            if row is None or self._placement_ok(row):
                continue
            found = {
                int(r[0])
                for r in self.conn.execute(
                    "SELECT DISTINCT batch_id FROM change WHERE batch_id > ?"
                    " AND entity = 'device' AND entity_id = ?"
                    f" AND field IN ('*', {', '.join('?' for _ in _PLACEMENT_FIELDS)})",
                    (batch_id, device_id, *_PLACEMENT_FIELDS),
                )
            }
            if row["place_id"] is not None:
                found.update(
                    int(r[0])
                    for r in self.conn.execute(
                        "SELECT DISTINCT batch_id FROM change WHERE batch_id > ?"
                        " AND entity = 'place' AND entity_id = ? AND field = 'polygon'",
                        (batch_id, row["place_id"]),
                    )
                )
            conflicting |= {b for b in found if b != new_batch and live(b)}
        if conflicting:
            raise UndoConflict(sorted(conflicting))

    def _placement_ok(self, row: sqlite3.Row) -> bool:
        """Spec section 3 invariants for one device's (place_id, pos_x, pos_y, roaming)."""
        x, y, place_id = row["pos_x"], row["pos_y"], row["place_id"]
        if row["roaming"]:
            return place_id is not None and x is None and y is None
        if x is None and y is None:
            return True
        if x is None or y is None or place_id is None:
            return False
        prow = self.conn.execute("SELECT polygon FROM place WHERE id = ?", (place_id,)).fetchone()
        if prow is None or prow[0] is None:
            return False
        return point_in_polygon(x, y, parse_polygon(json.loads(prow[0])))

    # -- conflict detection

    def _live_filter(self, batch_id: int) -> Callable[[int], bool]:
        """Return a predicate: is later batch L a conflict source for undoing ``batch_id``?

        L is excluded only when its effect provably nets out entirely after
        ``batch_id``:

        (i) L has itself been undone (its undoer is later than L, hence later
        than ``batch_id``, so the pair cancels out after the target); or
        (ii) L is an odd-depth undo batch (it cancels its chain root R) and
        R > ``batch_id``, so the cancelled write and its cancellation both lie
        after the target.

        Everything else is live, in particular an undo whose chain root is at
        or before ``batch_id``: it rewrote state *on top of* the target and
        undoing the target would clobber it. Even-depth undo tails (re-applying
        R) are live too; undoing them is how the user clears the conflict.
        """
        parent: dict[int, int] = {}
        for bid, target in self.conn.execute(
            "SELECT id, undoes_batch_id FROM batch WHERE undoes_batch_id IS NOT NULL"
        ):
            parent[int(bid)] = int(target)
        undone = set(parent.values())

        def live(bid: int) -> bool:
            if bid in undone:
                return False
            depth, root = 0, bid
            while root in parent:
                root = parent[root]
                depth += 1
            return not (depth % 2 == 1 and root > batch_id)

        return live

    def _undo_conflicts(self, batch_id: int, rows: list[_Change]) -> list[int]:
        live = self._live_filter(batch_id)
        found: set[int] = set()

        def collect(sql: str, params: tuple[Any, ...]) -> None:
            for (bid,) in self.conn.execute(sql, params):
                if live(int(bid)):
                    found.add(int(bid))

        for ch in rows:
            whole = ch.field == "*" or ch.is_place_existence or ch.is_floor_plan_creation
            # Same entity: same field, or any field when either side is row-level.
            collect(
                "SELECT DISTINCT batch_id FROM change WHERE batch_id > ? AND entity = ?"
                " AND entity_id = ? AND (field = ? OR field = '*' OR ? OR"
                " (entity = 'place' AND field = 'name'"
                "  AND (old_value = 'null' OR new_value = 'null')))",
                (batch_id, ch.entity, ch.entity_id, ch.field, whole),
            )
            # References to an entity this batch created (undo would delete it).
            if ch.creates and ch.entity == "place":
                ref = _dump(ch.entity_id)
                collect(
                    "SELECT DISTINCT batch_id FROM change WHERE batch_id > ?"
                    " AND entity = 'device' AND field = 'place_id'"
                    " AND (old_value = ? OR new_value = ?)",
                    (batch_id, ref, ref),
                )
            if ch.creates and ch.entity == "device":
                ref = _dump(ch.entity_id)
                collect(
                    "SELECT DISTINCT batch_id FROM change WHERE batch_id > ?"
                    " AND entity IN ('interface', 'service', 'ble_scanner')"
                    " AND field = 'device_id' AND (old_value = ? OR new_value = ?)",
                    (batch_id, ref, ref),
                )
                for bid, row in self._later_rows(batch_id, "link"):
                    if ch.entity_id in (row["a_device_id"], row["b_device_id"]) and live(bid):
                        found.add(bid)
                for bid, row in self._later_rows(batch_id, "ble_identity"):
                    if row["device_id"] == ch.entity_id and live(bid):
                        found.add(bid)
            # A BLE identity is unique by (kind, value) across row ids: a later
            # claim of the same identity blocks re-inserting it.
            if ch.entity == "ble_identity" and ch.field == "*":
                mine = ch.old if ch.old is not None else ch.new
                for bid, row in self._later_rows(batch_id, "ble_identity"):
                    if (row["kind"], row["value"]) == (mine["kind"], mine["value"]) and live(bid):
                        found.add(bid)
        return sorted(found)

    def _later_rows(self, batch_id: int, entity: str) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(batch_id, row)`` for each side of later row-level changes on ``entity``."""
        for bid, old_v, new_v in self.conn.execute(
            "SELECT batch_id, old_value, new_value FROM change WHERE batch_id > ?"
            " AND entity = ? AND field = '*'",
            (batch_id, entity),
        ).fetchall():
            for raw in (old_v, new_v):
                row = json.loads(raw) if raw is not None else None
                if row:
                    yield int(bid), row

    # -- applying inverses

    def _apply_inverse(self, batch_id: int, ch: _Change) -> None:
        if ch.entity not in _ENTITY_COLUMNS:
            raise RuntimeError(f"cannot undo change on unknown entity {ch.entity!r}")
        if ch.field == "*":
            if ch.old is None:  # creation -> delete
                current = self._snapshot(ch.entity, ch.entity_id)
                self._log_change(batch_id, ch.entity, ch.entity_id, "*", current, None)
                self._delete_entity(ch.entity, ch.entity_id)
            else:  # removal -> re-insert (original id unless discovered data reused it)
                row = dict(ch.old)
                entity_id = ch.entity_id
                if (
                    ch.entity == "service"
                    and self.conn.execute(
                        "SELECT 1 FROM service WHERE id = ?", (entity_id,)
                    ).fetchone()
                ):
                    # service ids are not AUTOINCREMENT; a later discovery may
                    # have taken it. Re-insert under a fresh id and log that id.
                    row["id"] = None
                if ch.entity == "device":
                    # Snapshots logged before schema v2 have no roaming key.
                    row.setdefault("roaming", 0)
                entity_id = self._insert_row(ch.entity, row)
                row["id"] = entity_id
                for field, (src_actor, src_set_at) in sorted(
                    row.get(_FIELD_SOURCE_KEY, {}).items()
                ):
                    self._restore_source(ch.entity, entity_id, field, src_actor, src_set_at)
                layout = row.get(_LAYOUT_KEY)
                if ch.entity == "device" and isinstance(layout, dict):
                    self.conn.execute(
                        "INSERT INTO topology_layout(device_id, x, y) VALUES (?, ?, ?)",
                        (entity_id, layout.get("x"), layout.get("y")),
                    )
                self._log_change(batch_id, ch.entity, entity_id, "*", None, row)
            return
        if ch.field not in _ENTITY_COLUMNS[ch.entity]:
            raise RuntimeError(f"cannot undo change on unknown field {ch.entity}.{ch.field}")
        if ch.entity == "floor_plan" and ch.old is None:
            # First upload: the sha256 marker deletes the whole row; the sibling
            # per-field creation changes are covered by that deletion.
            if ch.field == "sha256":
                current = self._snapshot("floor_plan", ch.entity_id)
                self._log_change(batch_id, "floor_plan", ch.entity_id, "*", current, None)
                self._delete_entity("floor_plan", ch.entity_id)
            return
        if ch.is_place_existence:
            cur_actor, cur_set_at = self._source("place", ch.entity_id, "name")
            if ch.old is None:  # place creation -> delete
                self._log_change(
                    batch_id, "place", ch.entity_id, "name", ch.new, None, cur_actor, cur_set_at
                )
                self._delete_entity("place", ch.entity_id)
            else:  # place deletion (by an earlier undo) -> re-create with same id
                self.conn.execute(
                    "INSERT INTO place(id, name) VALUES (?, ?)", (ch.entity_id, ch.old)
                )
                self._log_change(
                    batch_id, "place", ch.entity_id, "name", None, ch.old, cur_actor, cur_set_at
                )
                self._restore_source("place", ch.entity_id, "name", ch.old_actor, ch.old_set_at)
            return
        cur_actor, cur_set_at = self._source(ch.entity, ch.entity_id, ch.field)
        row = self.conn.execute(
            f"SELECT {ch.field} FROM {ch.entity} WHERE id = ?", (ch.entity_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no {ch.entity} with id {ch.entity_id}")
        self._log_change(
            batch_id, ch.entity, ch.entity_id, ch.field, row[0], ch.old, cur_actor, cur_set_at
        )
        self.conn.execute(
            f"UPDATE {ch.entity} SET {ch.field} = ? WHERE id = ?", (ch.old, ch.entity_id)
        )
        self._restore_source(ch.entity, ch.entity_id, ch.field, ch.old_actor, ch.old_set_at)

    def _restore_source(
        self, entity: str, entity_id: int, field: str, actor: str | None, set_at: str | None
    ) -> None:
        if actor is None:
            self.conn.execute(
                "DELETE FROM field_source WHERE entity = ? AND entity_id = ? AND field = ?",
                (entity, entity_id, field),
            )
        else:
            self.conn.execute(
                "INSERT INTO field_source(entity, entity_id, field, actor, set_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(entity, entity_id, field)"
                " DO UPDATE SET actor = excluded.actor, set_at = excluded.set_at",
                (entity, entity_id, field, actor, set_at),
            )

    def _snapshot(self, entity: str, entity_id: int) -> dict[str, Any]:
        """Full row as a dict, plus its ``field_source`` rows (if any) for a device."""
        snap = self._row_dict(entity, entity_id)
        if entity in ("device", "floor_plan"):
            sources = {
                str(r[0]): [str(r[1]), str(r[2])]
                for r in self.conn.execute(
                    "SELECT field, actor, set_at FROM field_source"
                    " WHERE entity = ? AND entity_id = ? ORDER BY field",
                    (entity, entity_id),
                )
            }
            if sources:
                snap[_FIELD_SOURCE_KEY] = sources
            layout = (
                self.conn.execute(
                    "SELECT x, y FROM topology_layout WHERE device_id = ?", (entity_id,)
                ).fetchone()
                if entity == "device"
                else None
            )
            if layout is not None:
                snap[_LAYOUT_KEY] = {"x": layout["x"], "y": layout["y"]}
        return snap

    def _row_dict(self, entity: str, entity_id: int) -> dict[str, Any]:
        cols = _ROW_COLUMNS[entity]
        row = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM {entity} WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no {entity} with id {entity_id}")
        return {c: row[c] for c in cols}

    def _insert_row(self, entity: str, values: dict[str, Any]) -> int:
        """Insert ``values`` (``id`` None = assign a fresh one); returns the row id."""
        cols = _ROW_COLUMNS[entity]
        cur = self.conn.execute(
            f"INSERT INTO {entity}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(values[c] for c in cols),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _delete_entity(self, entity: str, entity_id: int) -> None:
        """Delete a device/link/place/service/ble_identity and its field_source rows.

        Same transaction as the caller. Refuses to silently cascade: a device
        still referenced by links, interfaces, services, BLE identities or a
        linked BLE scanner, or a place still holding devices or a floor/polygon,
        raises ``ValueError``. (References written by later batches are reported
        earlier as ``UndoConflict``; these are references the change log does
        not cover, e.g. discovered data.) A device's ``topology_layout`` and
        ``ble_sighting`` rows are deleted with it.
        """
        if entity == "device":
            for table, where in (
                ("link", "a_device_id = ? OR b_device_id = ?"),
                ("interface", "device_id = ? OR device_id = ?"),
                ("service", "device_id = ? OR device_id = ?"),
                ("ble_identity", "device_id = ? OR device_id = ?"),
                # ON DELETE SET NULL would drop the link silently and undo could not restore it.
                ("ble_scanner", "device_id = ? OR device_id = ?"),
            ):
                if self.conn.execute(
                    f"SELECT 1 FROM {table} WHERE {where}", (entity_id, entity_id)
                ).fetchone():
                    raise ValueError(f"cannot delete device {entity_id}: referenced by {table}")
            # A layout is a preference; callers snapshot it (``_layout``) before deleting.
            self.conn.execute("DELETE FROM topology_layout WHERE device_id = ?", (entity_id,))
            # Sightings are collector data and are not restored by undo (spec section 7).
            self.conn.execute("DELETE FROM ble_sighting WHERE device_id = ?", (entity_id,))
        elif entity == "place":
            if self.conn.execute(
                "SELECT 1 FROM device WHERE place_id = ?", (entity_id,)
            ).fetchone():
                raise ValueError(f"cannot delete place {entity_id}: devices are assigned to it")
            if self.conn.execute(
                "SELECT 1 FROM place WHERE id = ? AND (floor IS NOT NULL OR polygon IS NOT NULL)",
                (entity_id,),
            ).fetchone():
                raise ValueError(f"cannot delete place {entity_id}: it has a floor/polygon")
        self.conn.execute(f"DELETE FROM {entity} WHERE id = ?", (entity_id,))
        self.conn.execute(
            "DELETE FROM field_source WHERE entity = ? AND entity_id = ?", (entity, entity_id)
        )


@dataclass(frozen=True)
class _Change:
    entity: str
    entity_id: int
    field: str
    old: Any
    new: Any
    old_actor: str | None
    old_set_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> _Change:
        def load(raw: str | None) -> Any:
            return None if raw is None else json.loads(raw)

        return cls(
            entity=str(row["entity"]),
            entity_id=int(row["entity_id"]),
            field=str(row["field"]),
            old=load(row["old_value"]),
            new=load(row["new_value"]),
            old_actor=row["old_actor"],
            old_set_at=row["old_set_at"],
        )

    @property
    def is_place_existence(self) -> bool:
        """``place.name`` going to/from null encodes creation/deletion of the place."""
        return (
            self.entity == "place"
            and self.field == "name"
            and (self.old is None or self.new is None)
        )

    @property
    def is_floor_plan_creation(self) -> bool:
        """``floor_plan.sha256`` logged with a NULL old value marks the first upload."""
        return self.entity == "floor_plan" and self.field == "sha256" and self.old is None

    @property
    def creates(self) -> bool:
        return (
            self.field == "*" or self.is_place_existence or self.is_floor_plan_creation
        ) and self.old is None
