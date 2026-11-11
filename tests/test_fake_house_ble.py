"""Replay the synthetic Home Assistant advert stream through the Bluetooth pipeline."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from conftest import FixedClock
from helpers.plan import save_square_room
from lanscape_atlas.bluetooth.aggregator import BleAggregator
from lanscape_atlas.bluetooth.match import Identity, parse_advert
from lanscape_atlas.bluetooth.skew import SkewCorrector
from lanscape_atlas.service import Placement, Service
from lanscape_atlas.service import ble_queries as bq

FIXTURE = Path(__file__).parent / "fixtures" / "ha_adverts.json"
SCANNER_A = "00:00:5e:00:53:a1"
SCANNER_B = "00:00:5e:00:53:a2"
PHONE = Identity("ibeacon", "00000000-0000-4000-8000-000000000001/1/1")
REMOTE = Identity("address", "00:00:5e:00:53:10")
KNOWN_VENDOR = {"00:00:5e:00:53:20", "00:00:5e:00:53:21"}


def adverts() -> list[dict[str, Any]]:
    raw = json.loads(FIXTURE.read_text())
    assert isinstance(raw, list)
    return raw


def test_fixture_shape() -> None:
    raw = adverts()
    parsed = [parse_advert(r) for r in raw]
    assert all(p is not None for p in parsed)
    assert {r["source"] for r in raw} == {SCANNER_A, SCANNER_B}
    assert all(r["address"].startswith("00:00:5e:00:53:") for r in raw)
    times = [r["time"] for r in raw]  # HA's epoch seconds, in order
    assert times == sorted(times) and times[0] == datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def test_fake_house_find(
    svc: Service, db: Any, clock: FixedClock, make_device: Callable[..., int]
) -> None:
    room_a = save_square_room(svc, "Room A")
    svc.save_room("user", "Room B", [[0.6, 0.6], [0.9, 0.6], [0.9, 0.9], [0.6, 0.9]])
    room_b = int(db.execute("SELECT id FROM place WHERE name = 'Room B'").fetchone()[0])

    # Scanners are reported by HA, then linked to the devices they run on (one per room).
    svc.upsert_scanners({SCANNER_A: "ble-proxy-a", SCANNER_B: "ble-proxy-b"}, clock())
    ids = {r["source"]: r["id"] for r in db.execute("SELECT id, source FROM ble_scanner")}
    for source, room, name, xy in (
        (SCANNER_A, room_a, "Proxy A", 0.3),
        (SCANNER_B, room_b, "Proxy B", 0.75),
    ):
        proxy = make_device(name=name)
        svc.place_devices("user", [Placement(proxy, room, xy, xy)], based_on={proxy: 0})
        svc.link_scanner("user", ids[source], proxy)

    phone, _ = svc.claim_ble("user", PHONE, new_name="Phone", new_type="phone")
    remote, _ = svc.claim_ble("user", REMOTE, new_name="Remote", new_type="remote")

    agg = BleAggregator(clock, known_vendor=lambda a: a in KNOWN_VENDOR)
    agg.set_index({PHONE: phone, REMOTE: remote})
    skew = SkewCorrector()  # what the client does per connection
    for raw in adverts():
        adv = parse_advert(raw)
        assert adv is not None
        receipt = clock()
        assert skew.observe(receipt, adv.time)
        agg.add(adv, skew.effective(receipt, adv.time))
        clock.advance(seconds=15)
    clock.advance(minutes=2)
    assert svc.record_ble_minutes(agg.drain_closed()) > 0
    assert agg.drain_all() == []

    now = clock()
    phone_heard = bq.last_heard(db, phone)
    remote_heard = bq.last_heard(db, remote)
    assert phone_heard is not None and phone_heard.place_name == "Room A"
    assert remote_heard is not None and remote_heard.place_name == "Room B"
    rows = {r.device_id: r for r in bq.find(db, None, now=now)}
    assert set(rows) == {phone, remote}
    assert rows[phone].last is not None and rows[phone].last.scanner_name == "ble-proxy-a"

    cands = agg.candidates()
    assert {c.identity.key() for c in cands} == {
        "ibeacon:00000000-0000-4000-8000-000000000002/5/6",
        "address:00:00:5e:00:53:20",
        "address:00:00:5e:00:53:21",
    }
    # Nothing Bluetooth did touched the placement fields of the claimed devices.
    for dev in (phone, remote):
        row = db.execute("SELECT place_id, pos_x, pos_y, roaming FROM device WHERE id=?", (dev,))
        assert tuple(row.fetchone()) == (None, None, None, 0)
    assert clock() - timedelta(days=1) < phone_heard.at
