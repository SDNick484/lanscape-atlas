"""Read-only Bluetooth queries: last heard, find rows, badges, hourly strip, scanners, hidden.

Like ``queries``, the functions take the shared connection, never write, and rely on the
caller holding ``service.lock``. Sighting timestamps are ISO text (``clock.iso``), which
sorts chronologically, as do the ``BLE_MINUTE_FMT`` minute strings.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from lanscape_atlas.bluetooth.aggregator import BLE_MINUTE_FMT
from lanscape_atlas.clock import parse_iso
from lanscape_atlas.netutil import normalize_mac
from lanscape_atlas.service.queries import MAC_KINDS

Badge = Literal["steady", "intermittent", "not_heard_24h"]

STEADY_MINUTES = 48  # of the last 60 minutes heard => "steady"
_WINDOW_MIN = 60
_STRIP_HOURS = 24
_MAC_MASK = (1 << 48) - 1


@dataclass(frozen=True)
class LastHeard:
    device_id: int
    at: datetime
    rssi: int
    place_id: int | None
    place_name: str | None
    scanner_name: str | None
    scanner_source: str


@dataclass(frozen=True)
class FindRow:
    device_id: int
    name: str | None
    type: str | None
    last: LastHeard | None
    badge: Badge


@dataclass(frozen=True)
class ScannerView:
    id: int
    source: str
    name: str | None
    device_id: int | None
    device_name: str | None
    place_name: str | None
    last_seen: datetime
    suggestions: list[int]


@dataclass(frozen=True)
class HiddenView:
    id: int
    label: str
    hidden_at: str


def last_heard(conn: sqlite3.Connection, device_id: int) -> LastHeard | None:
    """The newest sighting of ``device_id`` (ties: stronger, then lower scanner id)."""
    row = conn.execute(
        "SELECT s.last_at, s.max_rssi, s.place_id, p.name AS place_name,"
        " sc.name AS scanner_name, sc.source AS scanner_source"
        " FROM ble_sighting s"
        " JOIN ble_scanner sc ON sc.id = s.scanner_id"
        " LEFT JOIN place p ON p.id = s.place_id"
        " WHERE s.device_id = ?"
        " ORDER BY s.last_at DESC, s.max_rssi DESC, s.scanner_id ASC LIMIT 1",
        (device_id,),
    ).fetchone()
    if row is None:
        return None
    return LastHeard(
        device_id=device_id,
        at=parse_iso(row["last_at"]),
        rssi=row["max_rssi"],
        place_id=row["place_id"],
        place_name=row["place_name"],
        scanner_name=row["scanner_name"],
        scanner_source=row["scanner_source"],
    )


def _badge(
    conn: sqlite3.Connection, device_id: int, last: LastHeard | None, now: datetime
) -> Badge:
    now = now.astimezone(UTC)
    if last is None or last.at < now - timedelta(hours=24):
        return "not_heard_24h"
    end = last.at.strftime(BLE_MINUTE_FMT)
    start = (last.at - timedelta(minutes=_WINDOW_MIN - 1)).strftime(BLE_MINUTE_FMT)
    n = conn.execute(
        "SELECT COUNT(DISTINCT minute) FROM ble_sighting"
        " WHERE device_id = ? AND minute >= ? AND minute <= ?",
        (device_id, start, end),
    ).fetchone()[0]
    return "steady" if n >= STEADY_MINUTES else "intermittent"


def badge(conn: sqlite3.Connection, device_id: int, *, now: datetime) -> Badge:
    """``not_heard_24h``, else ``steady`` (>= 48 of the 60 minutes up to last heard) or not."""
    return _badge(conn, device_id, last_heard(conn, device_id), now)


def find(conn: sqlite3.Connection, q: str | None, *, now: datetime) -> list[FindRow]:
    """Claimed devices (any BLE identity), optionally name-filtered, newest heard first."""
    rows = conn.execute(
        "SELECT d.id, d.name, d.type FROM device d"
        " WHERE EXISTS (SELECT 1 FROM ble_identity i WHERE i.device_id = d.id)"
        " ORDER BY d.id"
    ).fetchall()
    needle = q.casefold() if q else None
    out: list[FindRow] = []
    for r in rows:
        if needle and needle not in (r["name"] or "").casefold():
            continue
        last = last_heard(conn, r["id"])
        out.append(FindRow(r["id"], r["name"], r["type"], last, _badge(conn, r["id"], last, now)))
    heard = [(r.last.at, r) for r in out if r.last is not None]
    heard.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in heard] + [r for r in out if r.last is None]


def hourly_strip(conn: sqlite3.Connection, device_id: int, *, now: datetime) -> list[str | None]:
    """24 hourly labels, oldest first, ending with the hour containing ``now``.

    Each is the place (or ``near <scanner>``) with the most adverts that hour.
    """
    end_hour = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    start = end_hour - timedelta(hours=_STRIP_HOURS - 1)
    rows = conn.execute(
        "SELECT substr(s.minute, 1, 13) AS hour, s.adverts, p.name AS place_name,"
        " sc.name AS scanner_name, sc.source AS scanner_source"
        " FROM ble_sighting s"
        " JOIN ble_scanner sc ON sc.id = s.scanner_id"
        " LEFT JOIN place p ON p.id = s.place_id"
        " WHERE s.device_id = ? AND s.minute >= ?",
        (device_id, start.strftime(BLE_MINUTE_FMT)),
    ).fetchall()
    totals: dict[str, dict[str, int]] = {}
    for r in rows:
        label = r["place_name"] or f"near {r['scanner_name'] or r['scanner_source']}"
        bucket = totals.setdefault(r["hour"], {})
        bucket[label] = bucket.get(label, 0) + r["adverts"]
    strip: list[str | None] = []
    for i in range(_STRIP_HOURS):
        hour = (start + timedelta(hours=i)).strftime(BLE_MINUTE_FMT)[:13]
        counts = totals.get(hour)
        strip.append(min(counts, key=lambda k: (-counts[k], k)) if counts else None)
    return strip


def identities(conn: sqlite3.Connection, device_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, kind, value, created_at FROM ble_identity WHERE device_id = ? ORDER BY id",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _mac_plus_two(address: str) -> str | None:
    try:
        n = int(normalize_mac(address).replace(":", ""), 16)
    except ValueError:
        return None
    return ":".join(f"{((n + 2) & _MAC_MASK):012x}"[i : i + 2] for i in range(0, 12, 2))


def scanner_labels(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    """``source -> (scanner name, room of its linked device)``; either may be None."""
    rows = conn.execute(
        "SELECT sc.source, sc.name, p.name AS place_name FROM ble_scanner sc"
        " LEFT JOIN device d ON d.id = sc.device_id"
        " LEFT JOIN place p ON p.id = d.place_id"
    )
    return {r["source"]: (r["name"], r["place_name"]) for r in rows}


def scanners(conn: sqlite3.Connection) -> list[ScannerView]:
    """All scanners with their link and suggested devices to link.

    Suggestions: devices named like the scanner (case-insensitive), then devices with an
    interface whose MAC + 2 is the scanner's source; the linked device is excluded.
    """
    rows = conn.execute(
        "SELECT sc.id, sc.source, sc.name, sc.device_id, sc.last_seen,"
        " d.name AS device_name, p.name AS place_name"
        " FROM ble_scanner sc"
        " LEFT JOIN device d ON d.id = sc.device_id"
        " LEFT JOIN place p ON p.id = d.place_id"
        " ORDER BY sc.id"
    ).fetchall()
    by_plus_two: dict[str, set[int]] = {}
    marks = ",".join("?" for _ in MAC_KINDS)
    for r in conn.execute(
        f"SELECT device_id, address FROM interface WHERE kind IN ({marks})", MAC_KINDS
    ):
        target = _mac_plus_two(r["address"])
        if target:
            by_plus_two.setdefault(target, set()).add(r["device_id"])
    out: list[ScannerView] = []
    for r in rows:
        by_name = (
            [
                x["id"]
                for x in conn.execute(
                    "SELECT id FROM device WHERE name IS NOT NULL"
                    " AND lower(name) = lower(?) ORDER BY id",
                    (r["name"],),
                )
            ]
            if r["name"]
            else []
        )
        try:
            source = normalize_mac(r["source"])
        except ValueError:
            source = r["source"]
        suggestions = list(
            dict.fromkeys(
                i for i in by_name + sorted(by_plus_two.get(source, ())) if i != r["device_id"]
            )
        )
        out.append(
            ScannerView(
                id=r["id"],
                source=r["source"],
                name=r["name"],
                device_id=r["device_id"],
                device_name=r["device_name"],
                place_name=r["place_name"],
                last_seen=parse_iso(r["last_seen"]),
                suggestions=suggestions,
            )
        )
    return out


def ble_salt(conn: sqlite3.Connection) -> bytes:
    """The per-database salt for hidden-identity digests (``match.hidden_digest``)."""
    row = conn.execute("SELECT salt FROM ble_salt WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("ble_salt has no row; is the schema older than v4?")
    return bytes(row[0])


def hidden_digests(conn: sqlite3.Connection) -> set[str]:
    """Digests of every hidden ("Not mine") identity."""
    return {str(r[0]) for r in conn.execute("SELECT digest FROM ble_hidden")}


def hidden(conn: sqlite3.Connection) -> list[HiddenView]:
    """Hidden identities as the Status page and API list them, newest first."""
    rows = conn.execute(
        "SELECT id, label, hidden_at FROM ble_hidden ORDER BY hidden_at DESC, id DESC"
    )
    return [HiddenView(r["id"], r["label"], r["hidden_at"]) for r in rows]
