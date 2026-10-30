"""The Ingestor: turns collector observations into discovered facts (spec section 6, steps 1-7).

It is the only writer of discovered data. It shares the Service's connection,
holds ``service.lock`` for the whole of each :meth:`Ingestor.ingest` call and
runs all writes in one ``BEGIN IMMEDIATE`` transaction. Events are buffered
and published only after the commit.

New observations are processed in ``seen_at`` order (stable, so ties keep
input order) and the latest sighting of an IPv4 owns its open lease. Presence
(``device.online``) is a discovered fact: written directly, never logged in
``change``, refreshed at the end of every ``ingest``.

User-facing fields (``name``) go through ``Service.record_collector_field`` so
they get provenance and an undoable change row, and are never written over a
value owned by ``user`` or ``claude``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from lanscape_atlas.clock import Clock, iso, parse_iso
from lanscape_atlas.events import EVENT_FIRST_SEEN, EVENT_OFFLINE, EVENT_ONLINE, Event, EventBus
from lanscape_atlas.ingest.naming import HINT_SOURCE, clean_name, is_junk_hostname, pick_name
from lanscape_atlas.netutil import is_random_mac, normalize_mac, valid_ipv4
from lanscape_atlas.observation import Observation
from lanscape_atlas.oui import OuiDB
from lanscape_atlas.service import Service

log = logging.getLogger(__name__)

DEFINITE_KINDS = frozenset({"ethernet", "wifi"})
MOBILE_TYPES = frozenset({"phone", "tablet", "laptop"})
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class IngestResult:
    created: int
    updated: int
    unattached: int
    dropped: int


@dataclass
class _Run:
    """Mutable bookkeeping for one ``ingest()`` call."""

    created: set[int] = field(default_factory=set)
    touched: set[int] = field(default_factory=set)
    unattached: dict[int, Observation] = field(default_factory=dict)  # observation.id -> obs
    dropped: int = 0
    batches: dict[str, int] = field(default_factory=dict)  # collector name -> batch id
    written: set[int] = field(default_factory=set)  # batch ids that got a change
    events: list[Event] = field(default_factory=list)


class Ingestor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        service: Service,
        events: EventBus,
        oui: OuiDB,
        clock: Clock,
        *,
        ip_match_window: timedelta = timedelta(minutes=10),
        grace: timedelta = timedelta(minutes=10),
        mobile_grace: timedelta = timedelta(minutes=30),
    ) -> None:
        self.conn = conn
        self.service = service
        self.events = events
        self.oui = oui
        self.clock = clock
        self.ip_match_window = ip_match_window
        self.grace = grace
        self.mobile_grace = mobile_grace

    # ------------------------------------------------------------------ public

    def ingest(self, observations: list[Observation]) -> IngestResult:
        run = _Run()
        with self.service.lock:
            if self.conn.in_transaction:
                raise RuntimeError("Ingestor.ingest called inside an open transaction")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                # 1. Retry stored unattached observations still inside the window.
                for obs_id, obs in self._stored_unattached():
                    self._process(run, obs_id, obs)
                # 2. Persist and process new observations, oldest first (stable
                #    sort) so the latest sighting of an IPv4 owns its lease.
                for obs in sorted(observations, key=_seen_key):
                    self._ingest_new(run, obs)
                # 3. Earlier IP-only observations may match leases opened in step 2.
                for obs_id, obs in list(run.unattached.items()):
                    self._process(run, obs_id, obs)
                # 4. Name fill once per touched device, after every fact is in
                #    (Ruling 19), so the name cannot flip inside one call.
                for device_id in sorted(run.touched):
                    self._fill_name(run, device_id)
                # 5. Presence, inside this transaction and lock.
                run.events.extend(self._update_presence(None))
                for collector, batch_id in run.batches.items():
                    if batch_id not in run.written:
                        self.conn.execute("DELETE FROM batch WHERE id = ?", (batch_id,))
                        log.debug("removed empty collector batch for %s", collector)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
        for event in run.events:
            self.events.publish(event)
        return IngestResult(
            created=len(run.created),
            updated=len(run.touched - run.created),
            unattached=len(run.unattached),
            dropped=run.dropped,
        )

    def refresh_presence(self, now: datetime | None = None) -> None:
        """Recompute ``device.online`` in its own transaction; publish transitions after commit.

        ``ingest`` runs the same computation inside its own transaction.
        """
        with self.service.lock:
            if self.conn.in_transaction:
                raise RuntimeError("Ingestor.refresh_presence called inside an open transaction")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                events = self._update_presence(now)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
        for event in events:
            self.events.publish(event)

    # ---------------------------------------------------------------- pipeline

    def _stored_unattached(self) -> list[tuple[int, Observation]]:
        cutoff = iso(self.clock() - self.ip_match_window)
        rows = self.conn.execute(
            "SELECT id, payload FROM observation WHERE attached = 0 AND seen_at >= ?"
            " ORDER BY seen_at, id",
            (cutoff,),
        ).fetchall()
        return [(int(r[0]), Observation.from_json(str(r[1]))) for r in rows]

    def _ingest_new(self, run: _Run, obs: Observation) -> None:
        reason = self._drop_reason(obs)
        if reason is not None:
            run.dropped += 1
            log.debug("dropped %s observation: %s", obs.source, reason)
            return
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO observation(source, seen_at, payload) VALUES (?, ?, ?)",
            (obs.source, iso(obs.seen_at), obs.to_json()),
        )
        if cur.rowcount == 0:
            return  # already stored (this call or an earlier one)
        assert cur.lastrowid is not None
        self._process(run, cur.lastrowid, obs)

    @staticmethod
    def _drop_reason(obs: Observation) -> str | None:
        if obs.seen_at.tzinfo is None or obs.seen_at.utcoffset() is None:
            return "naive seen_at"
        if obs.mac is not None:
            try:
                normalize_mac(obs.mac)
            except ValueError:
                return f"invalid MAC {obs.mac!r}"
            return None
        if obs.ipv4 is None:
            return "neither MAC nor IPv4"
        if valid_ipv4(obs.ipv4) is None:
            return f"invalid IPv4 {obs.ipv4!r}"
        return None

    def _process(self, run: _Run, obs_id: int, obs: Observation) -> None:
        """Correlate one valid observation and apply its facts."""
        seen = iso(obs.seen_at)
        ipv4 = valid_ipv4(obs.ipv4) if obs.ipv4 is not None else None
        if obs.mac is not None:
            iface_id, device_id = self._upsert_interface(run, normalize_mac(obs.mac), obs, seen)
            if ipv4 is not None:
                self._touch_lease(iface_id, ipv4, seen)
        else:
            assert ipv4 is not None
            match = self._match_ip(ipv4, obs.seen_at)
            if match is None:
                run.unattached[obs_id] = obs
                return
            iface_id, device_id = match
            self._bump_last_seen("interface", iface_id, seen)
            self._bump_last_seen("device", device_id, seen)
        run.unattached.pop(obs_id, None)
        run.touched.add(device_id)
        self.conn.execute(
            "UPDATE observation SET interface_id = ?, attached = 1 WHERE id = ?",
            (iface_id, obs_id),
        )
        # Latest-value facts (ap_node, band, hints) are replaced only by the newest
        # sighting from that source; an out-of-order older one must not win.
        newer = self.conn.execute(
            "SELECT 1 FROM observation WHERE interface_id = ? AND source = ? AND seen_at > ?"
            " LIMIT 1",
            (iface_id, obs.source, seen),
        ).fetchone()
        if newer is None:
            self._apply_attrs(iface_id, obs)
            self._apply_hint(iface_id, obs, ipv4)
        services = obs.attrs.get("services")
        if services:
            self._apply_services(device_id, obs.source, services, seen)

    # ---------------------------------------------------------------- identity

    def _upsert_interface(
        self, run: _Run, mac: str, obs: Observation, seen: str
    ) -> tuple[int, int]:
        kind = obs.iface_kind if obs.iface_kind in DEFINITE_KINDS else None
        row = self.conn.execute(
            "SELECT id, device_id, kind, vendor FROM interface WHERE address = ?"
            " ORDER BY id LIMIT 1",
            (mac,),
        ).fetchone()
        if row is None:
            random = is_random_mac(mac)
            dev = self.conn.execute(
                "INSERT INTO device(trust, first_seen, last_seen) VALUES ('new', ?, ?)",
                (seen, seen),
            )
            assert dev.lastrowid is not None
            device_id = dev.lastrowid
            iface = self.conn.execute(
                "INSERT INTO interface(device_id, kind, address, is_random, vendor,"
                " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (device_id, kind or "unknown", mac, int(random), self.oui.lookup(mac), seen, seen),
            )
            assert iface.lastrowid is not None
            run.created.add(device_id)
            run.events.append(
                Event(EVENT_FIRST_SEEN, {"device_id": device_id, "mac": mac, "is_random": random})
            )
            return iface.lastrowid, device_id
        iface_id, device_id = int(row["id"]), int(row["device_id"])
        if kind is not None and row["kind"] == "unknown":
            self.conn.execute("UPDATE interface SET kind = ? WHERE id = ?", (kind, iface_id))
        if not row["vendor"]:
            vendor = self.oui.lookup(mac)
            if vendor:
                self.conn.execute(
                    "UPDATE interface SET vendor = ? WHERE id = ?", (vendor, iface_id)
                )
        self._bump_last_seen("interface", iface_id, seen)
        self._bump_last_seen("device", device_id, seen)
        return iface_id, device_id

    def _bump_last_seen(self, table: str, row_id: int, seen: str) -> None:
        # ``table`` is an internal identifier ("interface" | "device" | "ip_lease").
        self.conn.execute(
            f"UPDATE {table} SET last_seen = ? WHERE id = ? AND last_seen < ?",
            (seen, row_id, seen),
        )

    def _touch_lease(self, iface_id: int, ipv4: str, seen: str) -> None:
        """Record that ``iface_id`` held ``ipv4`` at ``seen`` (spec section 6 step 6).

        The latest sighting owns the open lease. If another interface's open
        lease on ``ipv4`` was seen *after* ``seen``, this (older) sighting does
        not take it back: it is kept as closed history only, coalesced into
        this pair's latest closed lease when within ``ip_match_window``.
        Otherwise every other open lease on ``ipv4`` is closed (its
        ``last_seen`` kept) and this pair's open lease is opened or bumped.
        """
        blocking = self.conn.execute(
            "SELECT 1 FROM ip_lease WHERE ipv4 = ? AND closed = 0 AND interface_id != ?"
            " AND last_seen > ? LIMIT 1",
            (ipv4, iface_id, seen),
        ).fetchone()
        if blocking is not None:
            self._record_closed_lease(iface_id, ipv4, seen)
            return
        self.conn.execute(
            "UPDATE ip_lease SET closed = 1 WHERE ipv4 = ? AND closed = 0 AND interface_id != ?",
            (ipv4, iface_id),
        )
        row = self.conn.execute(
            "SELECT id FROM ip_lease WHERE interface_id = ? AND ipv4 = ? AND closed = 0"
            " ORDER BY id DESC LIMIT 1",
            (iface_id, ipv4),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO ip_lease(interface_id, ipv4, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?)",
                (iface_id, ipv4, seen, seen),
            )
        else:
            self._bump_last_seen("ip_lease", int(row[0]), seen)

    def _record_closed_lease(self, iface_id: int, ipv4: str, seen: str) -> None:
        seen_dt = parse_iso(seen)
        row = self.conn.execute(
            "SELECT id, first_seen, last_seen FROM ip_lease WHERE interface_id = ? AND ipv4 = ?"
            " AND closed = 1 ORDER BY last_seen DESC, id DESC LIMIT 1",
            (iface_id, ipv4),
        ).fetchone()
        if row is not None:
            first, last = parse_iso(str(row["first_seen"])), parse_iso(str(row["last_seen"]))
            if first - self.ip_match_window <= seen_dt <= last + self.ip_match_window:
                self.conn.execute(
                    "UPDATE ip_lease SET first_seen = min(first_seen, ?),"
                    " last_seen = max(last_seen, ?) WHERE id = ?",
                    (seen, seen, int(row["id"])),
                )
                return
        self.conn.execute(
            "INSERT INTO ip_lease(interface_id, ipv4, first_seen, last_seen, closed)"
            " VALUES (?, ?, ?, ?, 1)",
            (iface_id, ipv4, seen, seen),
        )

    def _match_ip(self, ipv4: str, seen_at: datetime) -> tuple[int, int] | None:
        """Interface holding an open lease on ``ipv4`` seen within the window of ``seen_at``."""
        lo = iso(seen_at - self.ip_match_window)
        hi = iso(seen_at + self.ip_match_window)
        row = self.conn.execute(
            "SELECT i.id, i.device_id FROM ip_lease l JOIN interface i ON i.id = l.interface_id"
            " WHERE l.ipv4 = ? AND l.closed = 0 AND l.last_seen >= ? AND l.first_seen <= ?"
            " ORDER BY l.last_seen DESC, l.id DESC LIMIT 1",
            (ipv4, lo, hi),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1])

    # -------------------------------------------------------------- facts

    def _apply_attrs(self, iface_id: int, obs: Observation) -> None:
        for col in ("ap_node", "band"):
            value = obs.attrs.get(col)
            if isinstance(value, str) and value:
                self.conn.execute(f"UPDATE interface SET {col} = ? WHERE id = ?", (value, iface_id))

    def _apply_hint(self, iface_id: int, obs: Observation, ipv4: str | None) -> None:
        override = obs.attrs.get("name_source")
        hint_source = override if isinstance(override, str) else HINT_SOURCE.get(obs.source)
        if hint_source is None or obs.hostname is None:
            return
        row = self.conn.execute(
            "SELECT address, hints FROM interface WHERE id = ?", (iface_id,)
        ).fetchone()
        mac = str(row["address"])
        # Check both raw and cleaned forms: "<mac>.local" is junk once cleaned.
        if is_junk_hostname(obs.hostname, mac=mac, ipv4=ipv4) or is_junk_hostname(
            clean_name(hint_source, obs.hostname), mac=mac, ipv4=ipv4
        ):
            return
        hints = _load_hints(row["hints"])
        if hints.get(hint_source) == obs.hostname:
            return
        hints[hint_source] = obs.hostname
        self.conn.execute(
            "UPDATE interface SET hints = ? WHERE id = ?",
            (json.dumps(hints, sort_keys=True), iface_id),
        )

    def _apply_services(self, device_id: int, source: str, services: Any, seen: str) -> None:
        if not isinstance(services, list):
            log.debug("ignoring non-list services attr from %s", source)
            return
        for item in services:
            if not isinstance(item, dict):
                continue
            port, proto = item.get("port"), item.get("proto")
            if not isinstance(port, int) or isinstance(port, bool) or not isinstance(proto, str):
                log.debug("ignoring malformed service %r from %s", item, source)
                continue
            # Newer sightings refresh name/product/version (keeping known values
            # when the new one is empty); older ones only widen first_seen.
            self.conn.execute(
                "INSERT INTO service(device_id, port, proto, name, product, version, source,"
                " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(device_id, port, proto) DO UPDATE SET"
                "  name = CASE WHEN excluded.last_seen >= service.last_seen"
                "    THEN coalesce(excluded.name, service.name) ELSE service.name END,"
                "  product = CASE WHEN excluded.last_seen >= service.last_seen"
                "    THEN coalesce(excluded.product, service.product) ELSE service.product END,"
                "  version = CASE WHEN excluded.last_seen >= service.last_seen"
                "    THEN coalesce(excluded.version, service.version) ELSE service.version END,"
                "  source = CASE WHEN excluded.last_seen >= service.last_seen"
                "    THEN excluded.source ELSE service.source END,"
                "  first_seen = min(service.first_seen, excluded.first_seen),"
                "  last_seen = max(service.last_seen, excluded.last_seen)",
                (
                    device_id,
                    port,
                    proto,
                    _opt_str(item.get("name")),
                    _opt_str(item.get("product")),
                    _opt_str(item.get("version")),
                    source,
                    seen,
                    seen,
                ),
            )

    # -------------------------------------------------------------- presence

    def _update_presence(self, now: datetime | None) -> list[Event]:
        """Set ``device.online`` from max interface ``last_seen``; return transition events.

        Caller holds the lock and owns the transaction. A device with no
        interfaces (a placeholder) is never online and never emits events.
        """
        now = now if now is not None else self.clock()
        normal_cutoff = iso(now - self.grace)
        mobile_cutoff = iso(now - self.mobile_grace)
        rows = self.conn.execute(
            "SELECT d.id, d.type, d.online, max(i.last_seen) AS seen"
            " FROM device d LEFT JOIN interface i ON i.device_id = d.id"
            " GROUP BY d.id ORDER BY d.id"
        ).fetchall()
        events: list[Event] = []
        for row in rows:
            device_id, was = int(row["id"]), bool(row["online"])
            if row["seen"] is None:
                if was:
                    self.conn.execute("UPDATE device SET online = 0 WHERE id = ?", (device_id,))
                continue
            cutoff = mobile_cutoff if row["type"] in MOBILE_TYPES else normal_cutoff
            is_online = str(row["seen"]) >= cutoff
            if is_online == was:
                continue
            self.conn.execute(
                "UPDATE device SET online = ? WHERE id = ?", (int(is_online), device_id)
            )
            name = EVENT_ONLINE if is_online else EVENT_OFFLINE
            events.append(Event(name, {"device_id": device_id}))
        return events

    # -------------------------------------------------------------- naming

    def _fill_name(self, run: _Run, device_id: int) -> None:
        """Fill ``device.name`` from merged hints; actor is ``collector:<winning source>``."""
        if not self.service.collector_may_set(device_id, "name"):
            return
        rows = self.conn.execute(
            "SELECT id, address, vendor, hints FROM interface WHERE device_id = ?"
            " ORDER BY last_seen, id",
            (device_id,),
        ).fetchall()
        merged: dict[str, str | None] = {}
        vendor: str | None = None
        macs: list[str] = []
        for row in rows:  # oldest first, so the most recently seen interface wins ties
            macs.append(str(row["address"]))
            for key, value in _load_hints(row["hints"]).items():
                if value is not None:
                    merged[key] = value
            if row["vendor"]:
                vendor = str(row["vendor"])
        ipv4 = self._current_ipv4(device_id)
        name, source = pick_name(merged, vendor, ipv4, macs=macs)
        if name is None or source is None:
            return
        current = self.conn.execute(
            "SELECT name FROM device WHERE id = ?", (device_id,)
        ).fetchone()[0]
        if current == name:
            return
        batch_id = run.batches.get(source)
        if batch_id is None:
            batch_id = self.service.begin_batch(f"collector:{source}")
            run.batches[source] = batch_id
        if self.service.record_collector_field(batch_id, device_id, "name", name, source):
            run.written.add(batch_id)

    def _current_ipv4(self, device_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT l.ipv4 FROM ip_lease l JOIN interface i ON i.id = l.interface_id"
            " WHERE i.device_id = ? AND l.closed = 0 ORDER BY l.last_seen DESC, l.id DESC"
            " LIMIT 1",
            (device_id,),
        ).fetchone()
        return None if row is None else str(row[0])


def _seen_key(obs: Observation) -> datetime:
    """Sort key for ``seen_at``; naive (to-be-dropped) values sort first."""
    seen = obs.seen_at
    if not isinstance(seen, datetime) or seen.tzinfo is None or seen.utcoffset() is None:
        return _EPOCH
    return seen


def _load_hints(raw: Any) -> dict[str, str | None]:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v if isinstance(v, str) else None for k, v in data.items()}


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
