"""Bluetooth read queries: last heard, find, badge, hourly strip, scanners."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from lanscape_atlas.bluetooth.aggregator import BLE_MINUTE_FMT, MinuteRow
from lanscape_atlas.bluetooth.match import Identity
from lanscape_atlas.service import Service
from lanscape_atlas.service import ble_queries as q

SCAN_A = "00:00:5e:00:53:aa"
SCAN_B = "00:00:5e:00:53:bb"
NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _claim(svc: Service, device_id: int, n: int = 1) -> None:
    svc.claim_ble("user", Identity("address", f"00:00:5e:00:53:{n:02x}"), device_id=device_id)


def _hear(
    svc: Service,
    device_id: int,
    at: datetime,
    *,
    source: str = SCAN_A,
    rssi: int = -70,
    adverts: int = 3,
) -> None:
    svc.record_ble_minutes(
        [
            MinuteRow(
                device_id=device_id,
                source=source,
                minute=at.strftime(BLE_MINUTE_FMT),
                max_rssi=rssi,
                adverts=adverts,
                last_at=at,
            )
        ]
    )


def _scanner_id(db: sqlite3.Connection, source: str) -> int:
    return int(db.execute("select id from ble_scanner where source = ?", (source,)).fetchone()[0])


def _place(db: sqlite3.Connection, name: str) -> int:
    with db:
        cur = db.execute("insert into place(name) values (?)", (name,))
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_last_heard_newest_wins_over_stronger(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    _hear(svc, d, NOW - timedelta(minutes=30), source=SCAN_A, rssi=-40)
    _hear(svc, d, NOW - timedelta(minutes=5), source=SCAN_B, rssi=-90)
    lh = q.last_heard(db, d)
    assert lh is not None
    assert lh.at == NOW - timedelta(minutes=5)
    assert lh.rssi == -90
    assert lh.scanner_source == SCAN_B
    assert lh.place_id is None and lh.place_name is None


def test_last_heard_reports_place_snapshot(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    room = _place(db, "Kitchen")
    esp = make_device(name="Proxy", place_id=room)
    d = make_device(name="Keys")
    _claim(svc, d)
    svc.upsert_scanners({SCAN_A: "kitchen-proxy"}, NOW)
    svc.link_scanner("user", _scanner_id(db, SCAN_A), esp)
    _hear(svc, d, NOW)
    lh = q.last_heard(db, d)
    assert lh is not None
    assert (lh.place_id, lh.place_name, lh.scanner_name) == (room, "Kitchen", "kitchen-proxy")


def test_last_heard_none_when_never_heard(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    assert q.last_heard(db, d) is None


def test_find_only_claimed(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    claimed = make_device(name="Keys", type="tag")
    make_device(name="Printer")
    _claim(svc, claimed)
    rows = q.find(db, None, now=NOW)
    assert [r.device_id for r in rows] == [claimed]
    assert rows[0].last is None and rows[0].badge == "not_heard_24h"
    assert rows[0].type == "tag"


def test_find_filters_by_name_and_orders_by_recent(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    a = make_device(name="House Keys")
    b = make_device(name="Car keys")
    c = make_device(name="Wallet")
    for n, d in enumerate((a, b, c), start=1):
        _claim(svc, d, n)
    _hear(svc, a, NOW - timedelta(minutes=10))
    _hear(svc, b, NOW - timedelta(minutes=2))
    assert [r.device_id for r in q.find(db, "KEYS", now=NOW)] == [b, a]
    assert [r.device_id for r in q.find(db, None, now=NOW)] == [b, a, c]
    assert q.find(db, "nothing", now=NOW) == []


def _fill_minutes(svc: Service, d: int, count: int) -> None:
    """``count`` distinct minutes ending at NOW (inclusive)."""
    for i in range(count):
        _hear(svc, d, NOW - timedelta(minutes=i))


def test_badge_steady_at_48_minutes(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    _fill_minutes(svc, d, 48)
    assert q.badge(db, d, now=NOW) == "steady"


def test_badge_intermittent_at_47_minutes(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    _fill_minutes(svc, d, 47)
    assert q.badge(db, d, now=NOW) == "intermittent"


def test_badge_counts_distinct_minutes_across_scanners(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    _fill_minutes(svc, d, 47)
    for i in range(47):
        _hear(svc, d, NOW - timedelta(minutes=i), source=SCAN_B)
    assert q.badge(db, d, now=NOW) == "intermittent"


def test_badge_not_heard_after_24h(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    _hear(svc, d, NOW - timedelta(hours=24, minutes=1))
    assert q.badge(db, d, now=NOW) == "not_heard_24h"
    _hear(svc, d, NOW - timedelta(hours=24))
    assert q.badge(db, d, now=NOW) == "intermittent"


def test_hourly_strip_picks_most_adverts(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    kitchen = _place(db, "Kitchen")
    office = _place(db, "Office")
    esp_k = make_device(name="K", place_id=kitchen)
    esp_o = make_device(name="O", place_id=office)
    d = make_device(name="Keys")
    _claim(svc, d)
    svc.upsert_scanners({SCAN_A: "a", SCAN_B: "b"}, NOW)
    svc.link_scanner("user", _scanner_id(db, SCAN_A), esp_k)
    svc.link_scanner("user", _scanner_id(db, SCAN_B), esp_o)
    hour = NOW - timedelta(hours=2)  # 2026-12-31 22:00
    _hear(svc, d, hour + timedelta(minutes=1), source=SCAN_A, adverts=5)
    _hear(svc, d, hour + timedelta(minutes=2), source=SCAN_B, adverts=4)
    _hear(svc, d, hour + timedelta(minutes=3), source=SCAN_B, adverts=4)
    strip = q.hourly_strip(db, d, now=NOW)
    assert len(strip) == 24
    assert strip[21] == "Office"  # 8 adverts beats Kitchen's 5
    assert strip[23] is None and strip[0] is None


def test_hourly_strip_unlinked_scanner_says_near(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    svc.upsert_scanners({SCAN_A: "porch-proxy"}, NOW)
    _hear(svc, d, NOW - timedelta(minutes=3), source=SCAN_A)  # current hour (23:57 prev day)
    _hear(svc, d, NOW - timedelta(hours=5), source=SCAN_B)  # unnamed scanner
    strip = q.hourly_strip(db, d, now=NOW)
    assert strip[22] == "near porch-proxy"
    assert strip[18] == f"near {SCAN_B}"


def test_hourly_strip_current_hour_is_last(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    now = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    _hear(svc, d, datetime(2026, 1, 1, 10, 5, tzinfo=UTC))
    assert q.hourly_strip(db, d, now=now)[23] == f"near {SCAN_A}"


def _iface(db: sqlite3.Connection, device_id: int, kind: str, address: str) -> None:
    with db:
        db.execute(
            "insert into interface(device_id, kind, address, first_seen, last_seen)"
            " values (?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (device_id, kind, address),
        )


def _one_scanner(svc: Service, source: str, name: str | None) -> None:
    svc.upsert_scanners({source: name}, NOW)


def test_scanner_suggestion_by_name(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="Porch-Proxy")
    make_device(name="Other")
    _one_scanner(svc, SCAN_A, "porch-proxy")
    [view] = q.scanners(db)
    assert view.suggestions == [d]
    assert view.device_id is None and view.last_seen == NOW


def test_scanner_suggestion_by_mac_plus_two(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="ESP")
    _iface(db, d, "wifi", "00:00:5e:00:53:10")
    _one_scanner(svc, "00:00:5e:00:53:12", None)
    [view] = q.scanners(db)
    assert view.suggestions == [d]


def test_scanner_suggestion_mac_wraps(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(name="ESP")
    _iface(db, d, "wifi", "00:00:5e:00:53:ff")
    _one_scanner(svc, "00:00:5e:00:54:01", None)
    [view] = q.scanners(db)
    assert view.suggestions == [d]


def test_scanner_suggestions_dedupe_order_and_exclude_linked(
    svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]
) -> None:
    by_mac = make_device(name="Zed")
    both = make_device(name="Hall")
    linked = make_device(name="Hall")
    _iface(db, by_mac, "ethernet", "00:00:5e:00:53:10")
    _iface(db, both, "wifi", "00:00:5e:00:53:10")
    _iface(db, linked, "unknown", "00:00:5e:00:53:10")
    _one_scanner(svc, "00:00:5e:00:53:12", "hall")
    sid = _scanner_id(db, "00:00:5e:00:53:12")
    assert q.scanners(db)[0].suggestions == [both, linked, by_mac]
    room = _place(db, "Hall")
    with db:
        db.execute("update device set place_id = ? where id = ?", (room, linked))
    svc.link_scanner("user", sid, linked)
    [view] = q.scanners(db)
    assert view.suggestions == [both, by_mac]
    assert (view.device_id, view.device_name, view.place_name) == (linked, "Hall", "Hall")


def test_identities(svc: Service, db: sqlite3.Connection, make_device: Callable[..., int]) -> None:
    d = make_device(name="Keys")
    _claim(svc, d)
    [row] = q.identities(db, d)
    assert isinstance(row, dict)
    assert {k: row[k] for k in ("kind", "value")} == {
        "kind": "address",
        "value": "00:00:5e:00:53:01",
    }
    assert set(row) == {"id", "kind", "value", "created_at"}
