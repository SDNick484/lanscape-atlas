"""The Service: the only path for user/Claude writes.

Every write records per-field provenance in ``field_source`` and an undoable
``change`` row (grouped into a ``batch``). ``old_actor``/``old_set_at`` on each
change capture the ``field_source`` row as it was *before* the write so undo
(Task 6) can restore provenance as well as values. Values are stored as JSON
text so ``None`` and numbers round-trip.

Transactions: the public user-facing writes (``update_device``, ``set_place``,
``add_link``, ``remove_link``, ``create_placeholder_device``, ``claim_interface``,
``claim_ble``, ``unclaim_ble``, ``hide_ble``, ``unhide_ble``, ``link_scanner``,
``place_devices``, ``save_shapes``, ``undo``) each own one ``BEGIN IMMEDIATE``
transaction, hold ``self.lock`` for its duration, and publish ``change.applied``
after commit. The Bluetooth collector writes (``record_ble_minutes``,
``upsert_scanners``) and the prunes
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

Floor plan (schema v5): creating the plan (blank or first image) logs a row-level
creation; turning a blank plan into an image plan logs a row-level **replacement**
(``old_value`` and ``new_value`` both full rows with their ``field_source``), which only
``floor_plan`` uses, because sha256/file_name must leave NULL together (CHECK) and a
field-by-field undo would pass through a refused row. Scale, units and image
replacement are plain field changes. A pre-v5 first upload (per-field rows with NULL
old values, ``sha256`` the marker) still undoes by deleting the row.

Drawn shapes (schema v6): ``save_shapes`` logs a created ``plan_shape`` as a row-level
creation and a deleted one as a row-level deletion (each snapshot carries the row's
``field_source``, so undo re-inserts it with the same id and provenance); an edit logs
``geometry``/``label`` field changes. ``geometry`` is canonical JSON text and is logged
as that text. A ``plan_shape`` batch also blocks undoing the plan's creation (see
``_Change.reshapes_plan``).

Bluetooth: a BLE claim logs the ``ble_identity`` row as a row-level creation
(after the new device's creation and owned ``name``/``type`` when it claims as a
new device); an unclaim logs it as a row-level deletion, so undo re-inserts it
with the same id. ``link_scanner`` is a plain owned field change on
``ble_scanner.device_id``. ``ble_sighting`` rows are collector data: deleting a
device (e.g. undoing a claim that created it) deletes its sightings, and undo
never brings them back.

"Not mine": ``hide_ble`` logs a ``ble_hidden`` row (salted digest and label, never
the raw identity) as a row-level creation, ``unhide_ble`` as a row-level deletion.
Claiming a hidden identity deletes its ``ble_hidden`` row in the claim's batch, so
one undo restores both. A digest names one identity, so undo treats a later claim,
unclaim, hide or unhide of the same identity as a conflict: a hidden identity is
never claimed and never hidden twice.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from lanscape_atlas.bluetooth.aggregator import BLE_MINUTE_FMT, HIDDEN_LABEL_MAX, MinuteRow
from lanscape_atlas.bluetooth.match import Identity, check_canonical, hidden_digest
from lanscape_atlas.clock import Clock, iso
from lanscape_atlas.events import EVENT_CHANGE_APPLIED, Event, EventBus
from lanscape_atlas.floorplan.geometry import (
    check_aspect,
    parse_door,
    parse_polygon,
    parse_wall,
    point_in_polygon,
)
from lanscape_atlas.service import ble_queries, queries

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
_FLOOR_PLAN_COLUMNS = (
    "id",
    "width",
    "height",
    "sha256",
    "file_name",
    "updated_at",
    "width_mm",
    "units",
)
# Columns added by schema v5: a floor_plan snapshot logged before v5 lacks them, so
# re-inserting one (redo of an undone pre-v5 upload) fills these in (cf. ``roaming``).
_FLOOR_PLAN_V5_DEFAULTS: dict[str, Any] = {"width_mm": None, "units": "imperial"}
# The pre-v5 first upload logged these fields one by one with a NULL old value; the
# ``sha256`` row is the creation marker. Since v5 a plan is created with a row-level
# ``'*'`` change and none of these fields is ever logged with a NULL old value again
# (width/height/updated_at are NOT NULL, and sha256/file_name only leave NULL in a
# row-level blank -> image replacement), so such a row can only be a pre-v5 creation.
_FLOOR_PLAN_LEGACY_CREATION = frozenset({"sha256", "width", "height", "file_name", "updated_at"})
PLAN_UNITS = frozenset({"imperial", "metric"})
# Each side of a blank plan, in mm (spec §5.2: 1 m to 300 m).
BLANK_MIN_MM = 1_000
BLANK_MAX_MM = 300_000
# Largest real-world plan width a scale may set (1 km): a typo guard, not a physical limit.
SCALE_MAX_MM = 1_000_000.0
_PLAN_SHAPE_COLUMNS = ("id", "kind", "geometry", "label", "created_at")
# Every kind the schema allows (0006_plan_shape.sql); SHAPE_RULES says which are drawable.
SHAPE_KINDS = frozenset({"wall", "door", "furniture", "label"})
SHAPES_MAX = 5_000
SHAPE_LABEL_MAX = 80
_BLE_IDENTITY_COLUMNS = ("id", "device_id", "kind", "value", "created_at")
_BLE_HIDDEN_COLUMNS = ("id", "digest", "label", "hidden_at")
# Row-level entities that stand for one BLE identity (compared by salted digest in undo).
_BLE_ROW_ENTITIES = ("ble_identity", "ble_hidden")
# Entities whose ``device_id`` field is logged as a plain field change.
_DEVICE_REF_ENTITIES = frozenset({"interface", "service", "ble_scanner"})
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
    """Raised when a later, still-live batch changed something the undone batch touched.

    ``conflicting`` may be empty when the obstacle is not in the change log (e.g. a
    device the undo would reference no longer exists); ``reason`` then says why.
    """

    def __init__(self, conflicting: list[int], reason: str | None = None) -> None:
        super().__init__(reason or f"undo conflicts with later batches {conflicting}")
        self.conflicting = conflicting
        self.reason = reason


class StaleWrite(Exception):
    """Raised when a device's placement changed after the batch the client last saw."""

    def __init__(self, conflicting: list[int]) -> None:
        super().__init__(f"placement changed since it was loaded for devices {conflicting}")
        self.conflicting = conflicting


class StaleShapes(StaleWrite):
    """Raised by ``save_shapes`` when a batch newer than ``based_on`` touched any shape.

    ``conflicting`` holds that newest batch id (not device ids, unlike ``StaleWrite``).
    """

    def __init__(self, conflicting: list[int]) -> None:
        Exception.__init__(
            self, f"the drawing changed since it was loaded (batch {conflicting[-1]})"
        )
        self.conflicting = conflicting


@dataclass(frozen=True)
class NewShape:
    """A shape for ``Service.save_shapes`` to create. ``geometry`` is validated by kind."""

    kind: str
    geometry: Any
    label: str | None = None


@dataclass(frozen=True)
class ShapeEdit:
    """A shape's new ``geometry`` and ``label`` for ``Service.save_shapes`` (kind is kept)."""

    id: int
    geometry: Any
    label: str | None = None


@dataclass(frozen=True)
class ShapeRule:
    """How ``save_shapes`` validates one kind of shape.

    ``parse`` takes the raw geometry and returns it canonical (or raises ``ValueError``).
    ``label`` is ``"none"`` (must be None), ``"optional"`` or ``"required"``; a label
    is stripped and at most ``SHAPE_LABEL_MAX`` characters.
    """

    parse: Callable[[object], dict[str, Any]]
    label: str = "none"


# The drawable kinds. Issue #11 adds "furniture" and "label" here (the schema already
# allows them); a kind in SHAPE_KINDS but not here is refused as "not supported yet".
SHAPE_RULES: dict[str, ShapeRule] = {
    "wall": ShapeRule(parse_wall),
    "door": ShapeRule(parse_door),
}


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
    # hide_ble/unhide_ble (and claim_ble of a hidden identity) write these rows whole.
    "ble_hidden": frozenset(_BLE_HIDDEN_COLUMNS) - {"id"},
    # Only link_scanner writes this; name/last_seen are collector data, not logged.
    "ble_scanner": frozenset({"device_id"}),
    # save_shapes writes rows whole and edits geometry/label.
    "plan_shape": frozenset(_PLAN_SHAPE_COLUMNS) - {"id"},
}
_ROW_COLUMNS: dict[str, tuple[str, ...]] = {
    "device": _DEVICE_COLUMNS,
    "link": _LINK_COLUMNS,
    "service": _SERVICE_COLUMNS,
    "floor_plan": _FLOOR_PLAN_COLUMNS,
    "ble_identity": _BLE_IDENTITY_COLUMNS,
    "ble_hidden": _BLE_HIDDEN_COLUMNS,
    "plan_shape": _PLAN_SHAPE_COLUMNS,
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
        """Create or replace the single floor plan image (row id 1); returns the batch id.

        - No plan: creates an image plan (no scale, imperial units), logged as a
          row-level ``'*'`` creation that undo answers by deleting the row.
        - A blank plan: turns it into an image plan, keeping ``width_mm`` and
          ``units`` (spec §5.2), logged as one row-level ``'*'`` replacement so undo
          restores the blank plan exactly. The new image must keep the plan's aspect
          ratio while any room polygon or drawn shape exists; with neither, any aspect
          is accepted.
        - An image plan: replaces the image field by field; the aspect ratio must match.

        ``ValueError`` (nothing written) for bad arguments or a refused aspect.
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
            image = {"width": width, "height": height, "sha256": sha256, "file_name": file_name}
            if cur is None:
                batch_id = self.begin_batch(actor)
                self._create_plan(batch_id, actor, image)
            elif cur["sha256"] is None:
                if self._plan_has_drawing():
                    try:
                        check_aspect(int(cur["width"]), int(cur["height"]), width, height)
                    except ValueError:
                        raise ValueError(
                            "aspect ratio differs from the blank plan, and rooms are drawn on"
                            " it: crop the image to the plan's proportions first"
                        ) from None
                batch_id = self.begin_batch(actor)
                self._replace_plan(batch_id, actor, image)
            else:
                check_aspect(int(cur["width"]), int(cur["height"]), width, height)
                batch_id = self.begin_batch(actor)
                now = self._now()
                for field, value in (*image.items(), ("updated_at", now)):
                    self._write_field(batch_id, "floor_plan", 1, field, cur[field], value, actor)
        self._publish(batch_id, actor)
        return batch_id

    def create_blank_plan(
        self, actor: str, *, width_mm: float, height_mm: float, units: str
    ) -> int:
        """Create a blank plan (no image) of ``width_mm`` x ``height_mm``; returns the batch id.

        Only when no plan exists. Each side must be 1,000 to 300,000 mm; both are
        stored rounded to whole mm as ``width``/``height``, with ``width_mm = width``.
        Logged as a row-level ``'*'`` creation (undo deletes the row, redo re-creates
        it). ``ValueError`` otherwise, with nothing written.
        """
        self._check_actor(actor)
        sides = []
        for name, value in (("width", width_mm), ("depth", height_mm)):
            if not _is_number(value) or not math.isfinite(value):
                raise ValueError(f"{name} must be a number of millimetres")
            if not BLANK_MIN_MM <= value <= BLANK_MAX_MM:
                raise ValueError(f"{name} must be between 1 m and 300 m (3.3 ft and 984 ft)")
            sides.append(round(value))
        self._check_units(units)
        with self._write_txn():
            if self.conn.execute("SELECT 1 FROM floor_plan WHERE id = 1").fetchone():
                raise ValueError("a floor plan already exists")
            batch_id = self.begin_batch(actor)
            self._create_plan(
                batch_id,
                actor,
                {
                    "width": sides[0],
                    "height": sides[1],
                    "width_mm": float(sides[0]),
                    "units": units,
                },
            )
        self._publish(batch_id, actor)
        return batch_id

    def set_plan_scale(self, actor: str, *, width_mm: float) -> int:
        """Set an image plan's real-world width in mm; returns the batch id (0 if unchanged).

        A logged field write on ``floor_plan.width_mm``. Refused (``ValueError``) with
        no plan, on a blank plan (its scale is its size), or for a width that is not
        a finite number in (0, 1,000,000] mm. Rooms and devices keep their
        normalized positions, so only displayed lengths change.
        """
        self._check_actor(actor)
        if (
            not _is_number(width_mm)
            or not math.isfinite(width_mm)
            or not 0 < width_mm <= SCALE_MAX_MM
        ):
            raise ValueError("width_mm must be a number of millimetres above 0 and up to 1 km")
        batch_id = 0
        with self._write_txn():
            cur = self._plan_row()
            if cur["sha256"] is None:
                raise ValueError("a blank plan's scale is its size; it cannot be set")
            value = float(width_mm)
            if cur["width_mm"] != value:
                batch_id = self.begin_batch(actor)
                self._write_field(
                    batch_id, "floor_plan", 1, "width_mm", cur["width_mm"], value, actor
                )
        self._publish(batch_id, actor)
        return batch_id

    def set_plan_units(self, actor: str, units: str) -> int:
        """Set the plan's display units ('imperial' or 'metric'); returns the batch id.

        A logged field write on ``floor_plan.units``; 0 if unchanged. ``ValueError``
        for other values or when there is no plan.
        """
        self._check_actor(actor)
        self._check_units(units)
        batch_id = 0
        with self._write_txn():
            cur = self._plan_row()
            if cur["units"] != units:
                batch_id = self.begin_batch(actor)
                self._write_field(batch_id, "floor_plan", 1, "units", cur["units"], units, actor)
        self._publish(batch_id, actor)
        return batch_id

    @staticmethod
    def _check_units(units: object) -> None:
        if units not in PLAN_UNITS:
            raise ValueError(f"units must be one of {sorted(PLAN_UNITS)}, got {units!r}")

    def _plan_row(self) -> sqlite3.Row:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM floor_plan WHERE id = 1"
        ).fetchone()
        if row is None:
            raise ValueError("there is no floor plan yet")
        return row

    def _plan_has_drawing(self) -> bool:
        """True if any room polygon or drawn shape exists (the aspect rule then applies).

        Shapes are ``plan_shape`` rows (schema v6). The table check stays so this also
        works on a connection still at an older schema (tests build those).
        """
        if self.conn.execute("SELECT 1 FROM place WHERE polygon IS NOT NULL LIMIT 1").fetchone():
            return True
        if self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plan_shape'"
        ).fetchone():
            return self.conn.execute("SELECT 1 FROM plan_shape LIMIT 1").fetchone() is not None
        return False

    def _create_plan(self, batch_id: int, actor: str, values: Mapping[str, Any]) -> None:
        """Insert floor_plan row 1 from ``values`` and log it as a row-level creation.

        Every column written gets ``field_source`` (actor, now); the logged snapshot
        carries those rows so redo restores provenance with the row.
        """
        now = self._now()
        row: dict[str, Any] = {
            "id": 1,
            "sha256": None,
            "file_name": None,
            **_FLOOR_PLAN_V5_DEFAULTS,
            **values,
            "updated_at": now,
        }
        self._insert_row("floor_plan", row)
        for field in (*values, "updated_at"):
            self._restore_source("floor_plan", 1, field, actor, now)
        self._log_change(batch_id, "floor_plan", 1, "*", None, self._snapshot("floor_plan", 1))

    def _replace_plan(self, batch_id: int, actor: str, values: Mapping[str, Any]) -> None:
        """Overwrite fields of floor_plan row 1, logged as one row-level replacement.

        Used where a field-by-field write (and its field-by-field undo) would pass
        through a row the CHECK refuses, e.g. a blank plan getting its image (sha256
        and file_name leave NULL together). Changed fields get ``field_source``.
        """
        old = self._snapshot("floor_plan", 1)
        now = self._now()
        changed = {**values, "updated_at": now}
        sets = ", ".join(f"{field} = ?" for field in changed)
        self.conn.execute(f"UPDATE floor_plan SET {sets} WHERE id = 1", tuple(changed.values()))
        for field in changed:
            self._restore_source("floor_plan", 1, field, actor, now)
        self._log_change(batch_id, "floor_plan", 1, "*", old, self._snapshot("floor_plan", 1))

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

    def save_shapes(
        self,
        actor: str,
        *,
        creates: Sequence[NewShape],
        updates: Sequence[ShapeEdit],
        deletes: Sequence[int],
        based_on: int,
    ) -> int:
        """Create, edit and delete drawn shapes in one batch; returns the batch id (0 if none).

        Everything is checked before anything is written, in one transaction:
        - each shape against ``SHAPE_RULES`` for its kind (``ValueError``; kinds the
          schema allows but no rule covers yet are refused as "not supported yet");
          an edit keeps the shape's kind;
        - ``based_on``: the newest batch that touched any shape when the page loaded
          (``queries.last_shapes_batch``). A newer one raises ``StaleShapes`` before
          ids are looked up, so a shape deleted elsewhere reads as stale, not unknown;
        - ids: each listed once across ``updates`` and ``deletes``, and existing
          (``KeyError`` otherwise);
        - at most ``SHAPES_MAX`` shapes on the plan afterwards.
        Deletions, then edits (only changed fields are logged), then creations.
        """
        self._check_actor(actor)
        new = [
            (c.kind, self._check_shape(c.kind, c.geometry, c.label)) for c in self._news(creates)
        ]
        edits = self._edits(updates)
        gone = [self._shape_id(d) for d in deletes]
        if isinstance(based_on, bool) or not isinstance(based_on, int):
            raise ValueError(f"based_on must be an int, got {based_on!r}")
        ids = [e.id for e in edits] + gone
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"shapes listed more than once: {dupes}")
        batch_id = 0
        with self._write_txn():
            self._plan_row()
            newest = queries.last_shapes_batch(self.conn)
            if newest > based_on:
                raise StaleShapes([newest])
            current = {i: self._row_dict("plan_shape", i) for i in ids}
            changed: list[tuple[int, dict[str, Any]]] = []
            for e in edits:
                row = current[e.id]
                geometry, label = self._check_shape(str(row["kind"]), e.geometry, e.label)
                target = {"geometry": geometry, "label": label}
                diff = {f: v for f, v in target.items() if row[f] != v}
                if diff:
                    changed.append((e.id, diff))
            total = self.conn.execute("SELECT count(*) FROM plan_shape").fetchone()[0]
            if int(total) - len(gone) + len(new) > SHAPES_MAX:
                raise ValueError(f"a plan holds at most {SHAPES_MAX} shapes")
            if not (gone or changed or new):
                return 0
            batch_id = self.begin_batch(actor)
            for shape_id in gone:
                old = self._snapshot("plan_shape", shape_id)
                self._log_change(batch_id, "plan_shape", shape_id, "*", old, None)
                self._delete_entity("plan_shape", shape_id)
            for shape_id, diff in changed:
                for field, value in diff.items():
                    old_value = current[shape_id][field]
                    self._write_field(
                        batch_id, "plan_shape", shape_id, field, old_value, value, actor
                    )
            for kind, (geometry, label) in new:
                self._create_shape(batch_id, actor, kind, geometry, label)
        self._publish(batch_id, actor)
        return batch_id

    @staticmethod
    def _news(creates: Sequence[NewShape]) -> list[NewShape]:
        for c in creates:
            if not isinstance(c, NewShape):
                raise ValueError(f"expected a NewShape, got {c!r}")
        return list(creates)

    def _edits(self, updates: Sequence[ShapeEdit]) -> list[ShapeEdit]:
        for u in updates:
            if not isinstance(u, ShapeEdit):
                raise ValueError(f"expected a ShapeEdit, got {u!r}")
            self._shape_id(u.id)
        return list(updates)

    @staticmethod
    def _shape_id(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"a shape id must be an int, got {value!r}")
        return value

    @staticmethod
    def _check_shape(kind: object, geometry: object, label: object) -> tuple[str, str | None]:
        """Validate one shape; returns its canonical geometry JSON text and label."""
        if not isinstance(kind, str) or kind not in SHAPE_KINDS:
            raise ValueError(f"shape kind must be one of {sorted(SHAPE_KINDS)}, got {kind!r}")
        rule = SHAPE_RULES.get(kind)
        if rule is None:
            raise ValueError(f"{kind} shapes are not supported yet")
        canonical = json.dumps(rule.parse(geometry), sort_keys=True, separators=(",", ":"))
        if label is not None and not isinstance(label, str):
            raise ValueError(f"a {kind} label must be a string or None")
        text = label.strip() if label is not None else None
        if rule.label == "none":
            if label is not None:
                raise ValueError(f"a {kind} has no label")
        elif text == "" or text is None:
            if rule.label == "required":
                raise ValueError(f"a {kind} needs a label")
            text = None
        if text is not None and len(text) > SHAPE_LABEL_MAX:
            raise ValueError(f"a {kind} label is at most {SHAPE_LABEL_MAX} characters")
        return canonical, text

    def _create_shape(
        self, batch_id: int, actor: str, kind: str, geometry: str, label: str | None
    ) -> int:
        """Insert a shape and log it as a row-level creation (with its field_source)."""
        now = self._now()
        shape_id = self._insert_row(
            "plan_shape",
            {"id": None, "kind": kind, "geometry": geometry, "label": label, "created_at": now},
        )
        for field in ("kind", "geometry") if label is None else ("kind", "geometry", "label"):
            self._restore_source("plan_shape", shape_id, field, actor, now)
        self._log_change(
            batch_id, "plan_shape", shape_id, "*", None, self._snapshot("plan_shape", shape_id)
        )
        return shape_id

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
        source would be deleted but still has links, Bluetooth identities, a
        linked Bluetooth scanner, or a non-null curated field
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
        NULL, skipped when equal, and refuses the claim when it differs. Links,
        Bluetooth identities and a linked Bluetooth scanner still refuse.
        """
        if self.conn.execute(
            "SELECT 1 FROM link WHERE a_device_id = ? OR b_device_id = ?", (source, source)
        ).fetchone():
            raise ValueError(f"device {source} has links; remove them before claiming")
        if self.conn.execute(
            "SELECT 1 FROM ble_identity WHERE device_id = ?", (source,)
        ).fetchone():
            raise ValueError(
                f"device {source} has Bluetooth identities; unclaim them before claiming"
            )
        if self.conn.execute("SELECT 1 FROM ble_scanner WHERE device_id = ?", (source,)).fetchone():
            raise ValueError(
                f"device {source} is linked to a Bluetooth scanner; unlink it before claiming"
            )
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
        undo deletes the identity before the device. A hidden ("Not mine")
        identity is unhidden in the same batch (its ``ble_hidden`` row logged as a
        row-level deletion), so one undo restores both. Raises ``ValueError`` for a
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
            hidden = self.conn.execute(
                "SELECT id FROM ble_hidden WHERE digest = ?", (self._digest(identity),)
            ).fetchone()
            if hidden is not None:
                old = self._row_dict("ble_hidden", hidden["id"])
                self._log_change(batch_id, "ble_hidden", hidden["id"], "*", old, None)
                self._delete_entity("ble_hidden", hidden["id"])
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

    def hide_ble(self, actor: str, identity: Identity, label: str) -> tuple[int, int]:
        """Hide an unclaimed identity ("Not mine"); returns ``(hidden_id, batch_id)``.

        Stores the salted digest (``match.hidden_digest``) and ``label`` (trimmed, at
        most ``HIDDEN_LABEL_MAX`` characters), never the identity itself, logged as a
        row-level creation. Raises ``ValueError`` for a bad actor, a non-canonical
        identity, an empty label, a claimed identity or one already hidden.
        """
        self._check_actor(actor)
        check_canonical(identity)
        if not isinstance(label, str) or not label.strip():
            raise ValueError("label must be a non-empty string")
        label = label.strip()[:HIDDEN_LABEL_MAX]
        with self._write_txn():
            claimed = self.conn.execute(
                "SELECT device_id FROM ble_identity WHERE kind = ? AND value = ?",
                (identity.kind, identity.value),
            ).fetchone()
            if claimed is not None:
                raise ValueError(
                    f"claimed by device {claimed['device_id']}; unclaim it before hiding it"
                )
            digest = self._digest(identity)
            if self.conn.execute("SELECT 1 FROM ble_hidden WHERE digest = ?", (digest,)).fetchone():
                raise ValueError("already hidden")
            batch_id = self.begin_batch(actor)
            cur = self.conn.execute(
                "INSERT INTO ble_hidden(digest, label, hidden_at) VALUES (?, ?, ?)",
                (digest, label, self._now()),
            )
            assert cur.lastrowid is not None
            hidden_id = cur.lastrowid
            self._log_change(
                batch_id,
                "ble_hidden",
                hidden_id,
                "*",
                None,
                self._row_dict("ble_hidden", hidden_id),
            )
        self._publish(batch_id, actor)
        return hidden_id, batch_id

    def unhide_ble(self, actor: str, hidden_id: int) -> int:
        """Delete a hidden row so the identity is listed again; returns the batch id.

        Logged as a row-level deletion, so undo re-inserts it with the same id.
        Raises ``KeyError`` for an unknown id.
        """
        self._check_actor(actor)
        with self._write_txn():
            old = self._row_dict("ble_hidden", hidden_id)
            batch_id = self.begin_batch(actor)
            self._log_change(batch_id, "ble_hidden", hidden_id, "*", old, None)
            self._delete_entity("ble_hidden", hidden_id)
        self._publish(batch_id, actor)
        return batch_id

    def _digest(self, identity: Identity) -> str:
        return hidden_digest(ble_queries.ble_salt(self.conn), identity)

    def _row_digest(self, entity: str, row: Mapping[str, Any]) -> str:
        """The identity digest a logged ``ble_identity``/``ble_hidden`` row stands for."""
        if entity == "ble_hidden":
            return str(row["digest"])
        return self._digest(Identity(row["kind"], row["value"]))

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
        cutoff = (
            (self.clock() - timedelta(days=older_than_days))
            .astimezone(UTC)
            .strftime(BLE_MINUTE_FMT)
        )
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
            for device_id in sorted(self._referenced_devices(rows)):
                if (
                    self.conn.execute("SELECT 1 FROM device WHERE id = ?", (device_id,)).fetchone()
                    is None
                ):
                    # Gone without a live batch to blame (deleted outside the change log).
                    raise UndoConflict([], f"cannot undo: device {device_id} no longer exists")
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
            # Undo would delete the plan (or change its aspect) under room polygons or
            # drawn shapes written after it: refuse rather than strand or distort them.
            # plan_shape (schema v6, issue #10) is matched in the change log, which works
            # before that table exists.
            if ch.reshapes_plan:
                collect(
                    "SELECT DISTINCT batch_id FROM change WHERE batch_id > ?"
                    " AND ((entity = 'place' AND field = 'polygon') OR entity = 'plan_shape')",
                    (batch_id,),
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
            # A BLE identity is claimed or hidden at most once, across row ids: a later
            # claim, unclaim, hide or unhide of the same identity (same salted digest)
            # blocks undoing this row.
            if ch.entity in _BLE_ROW_ENTITIES and ch.field == "*":
                mine = self._row_digest(ch.entity, ch.old if ch.old is not None else ch.new)
                for entity in _BLE_ROW_ENTITIES:
                    for bid, row in self._later_rows(batch_id, entity):
                        if live(bid) and self._row_digest(entity, row) == mine:
                            found.add(bid)
        # Undo would re-point a row at a device a later live batch deleted.
        for device_id in sorted(self._referenced_devices(rows)):
            collect(
                "SELECT DISTINCT batch_id FROM change WHERE batch_id > ? AND entity = 'device'"
                " AND entity_id = ? AND field = '*' AND new_value = 'null'",
                (batch_id, device_id),
            )
        return sorted(found)

    @staticmethod
    def _referenced_devices(rows: list[_Change]) -> set[int]:
        """Device ids the inverses of ``rows`` would point rows at.

        A re-inserted ``link``/``service``/``ble_identity`` row references its
        devices; a restored ``device_id`` field references its old value. Devices
        the same batch deleted are excluded: its undo re-inserts them first.
        """
        restored = {
            ch.entity_id
            for ch in rows
            if ch.entity == "device" and ch.field == "*" and ch.old is not None
        }
        refs: set[int] = set()
        for ch in rows:
            if ch.field == "*" and isinstance(ch.old, dict):
                if ch.entity == "link":
                    refs.update((ch.old["a_device_id"], ch.old["b_device_id"]))
                elif ch.entity in ("service", "ble_identity"):
                    refs.add(ch.old["device_id"])
            elif (
                ch.field == "device_id"
                and ch.entity in _DEVICE_REF_ENTITIES
                and isinstance(ch.old, int)
            ):
                refs.add(ch.old)
        return {int(d) for d in refs if d is not None} - restored

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
            elif ch.new is not None:  # whole-row replacement (floor_plan) -> put old back
                self._restore_row(batch_id, ch)
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
                if ch.entity == "floor_plan":
                    # Snapshots logged before schema v5 have no width_mm/units keys.
                    for key, default in _FLOOR_PLAN_V5_DEFAULTS.items():
                        row.setdefault(key, default)
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
        if ch.is_legacy_floor_plan_creation:
            # Pre-v5 first upload: the sha256 marker deletes the whole row; the sibling
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

    def _restore_row(self, batch_id: int, ch: _Change) -> None:
        """Undo a row-level replacement: write ``ch.old``'s columns and provenance back.

        Logs the inverse as another replacement (current -> restored), so undoing the
        undo replaces it again. Only ``floor_plan`` is ever replaced whole.
        """
        if ch.entity != "floor_plan":
            raise RuntimeError(f"cannot undo a row replacement on {ch.entity!r}")
        current = self._snapshot(ch.entity, ch.entity_id)
        row = {**_FLOOR_PLAN_V5_DEFAULTS, **ch.old}
        cols = [c for c in _ROW_COLUMNS[ch.entity] if c != "id"]
        self.conn.execute(
            f"UPDATE {ch.entity} SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
            (*(row[c] for c in cols), ch.entity_id),
        )
        self.conn.execute(
            "DELETE FROM field_source WHERE entity = ? AND entity_id = ?",
            (ch.entity, ch.entity_id),
        )
        for field, (src_actor, src_set_at) in sorted(row.get(_FIELD_SOURCE_KEY, {}).items()):
            self._restore_source(ch.entity, ch.entity_id, field, src_actor, src_set_at)
        restored = self._snapshot(ch.entity, ch.entity_id)
        self._log_change(batch_id, ch.entity, ch.entity_id, "*", current, restored)

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
        """Full row as a dict, plus its ``field_source`` rows (if any) for a device,
        the floor plan or a drawn shape."""
        snap = self._row_dict(entity, entity_id)
        if entity in ("device", "floor_plan", "plan_shape"):
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
    def is_legacy_floor_plan_creation(self) -> bool:
        """One of the per-field rows of a pre-v5 first upload (NULL old value).

        See ``_FLOOR_PLAN_LEGACY_CREATION``: since v5 these fields are never logged
        one by one with a NULL old value, so this cannot match a later write.
        """
        return (
            self.entity == "floor_plan"
            and self.field in _FLOOR_PLAN_LEGACY_CREATION
            and self.old is None
        )

    @property
    def is_floor_plan_creation(self) -> bool:
        """The marker of a pre-v5 first upload: ``sha256`` logged with a NULL old value.

        A plan created since v5 logs a row-level ``'*'`` creation instead.
        """
        return self.is_legacy_floor_plan_creation and self.field == "sha256"

    @property
    def creates(self) -> bool:
        return (
            self.field == "*" or self.is_place_existence or self.is_floor_plan_creation
        ) and self.old is None

    @property
    def reshapes_plan(self) -> bool:
        """Undoing this would remove the floor plan or change its aspect ratio.

        True for a plan creation (row-level or the pre-v5 marker) and for a row-level
        replacement whose two sides differ in aspect (an image of any aspect over a
        blank plan with nothing drawn on it).
        """
        if self.entity != "floor_plan":
            return False
        if self.creates:
            return True
        if self.field != "*" or not isinstance(self.old, dict) or not isinstance(self.new, dict):
            return False
        try:
            check_aspect(
                self.old["width"], self.old["height"], self.new["width"], self.new["height"]
            )
        except ValueError:
            return True
        return False
