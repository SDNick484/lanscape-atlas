"""Per-minute aggregator and unclaimed-candidate list (pure, in memory)."""

from __future__ import annotations

from datetime import timedelta

from conftest import FixedClock
from lanscape_atlas.bluetooth.aggregator import (
    CANDIDATE_TTL,
    STABLE_SPAN,
    BleAggregator,
)
from lanscape_atlas.bluetooth.match import Advert, Identity, is_static_random

SRC_A = "00:00:5e:00:53:a1"
SRC_B = "00:00:5e:00:53:a2"
ADDR_1 = "02:00:5e:00:53:01"
ADDR_2 = "02:00:5e:00:53:02"
ADDR_3 = "02:00:5e:00:53:03"
ADDR_4 = "02:00:5e:00:53:04"
BEACON_UUID = "00000000-0000-4000-8000-000000000001"


def _static_random() -> str:
    addr = f"{0xC0 | 0x02:02x}:00:5e:00:53:05"  # top two bits set at runtime
    assert is_static_random(addr)
    return addr


def adv(
    address: str = ADDR_1,
    rssi: int = -60,
    source: str = SRC_A,
    name: str | None = None,
    manufacturer_data: dict[int, bytes] | None = None,
) -> Advert:
    return Advert(address, rssi, source, name, manufacturer_data or {})


def beacon_adv(minor: int = 1, rssi: int = -60) -> Advert:
    payload = b"\x02\x15" + bytes.fromhex(BEACON_UUID.replace("-", ""))
    payload += (1).to_bytes(2, "big") + minor.to_bytes(2, "big") + b"\xc5"
    return adv(address=ADDR_3, rssi=rssi, manufacturer_data={76: payload})


def make(clock: FixedClock, **kw: object) -> BleAggregator:
    kw.setdefault("known_vendor", lambda _a: False)
    return BleAggregator(clock, **kw)  # type: ignore[arg-type]


def claimed(agg: BleAggregator, address: str = ADDR_1, device_id: int = 7) -> None:
    agg.set_index({Identity("address", address): device_id})


def test_minute_bucket_max_rssi_and_count(clock: FixedClock) -> None:
    agg = make(clock)
    claimed(agg)
    agg.add(adv(rssi=-70))
    clock.advance(seconds=10)
    agg.add(adv(rssi=-55))
    clock.advance(seconds=10)
    agg.add(adv(rssi=-80))
    clock.advance(minutes=1)
    rows = agg.drain_closed()
    assert len(rows) == 1
    row = rows[0]
    assert (row.device_id, row.source, row.minute) == (7, SRC_A, "2026-01-01T00:00Z")
    assert (row.max_rssi, row.adverts) == (-55, 3)
    assert row.last_at == clock.now - timedelta(minutes=1)


def test_drain_closed_keeps_current_minute(clock: FixedClock) -> None:
    agg = make(clock)
    claimed(agg)
    agg.add(adv())
    assert agg.drain_closed() == []
    clock.advance(minutes=1)
    assert len(agg.drain_closed()) == 1
    assert agg.drain_closed() == []
    agg.add(adv())
    assert len(agg.drain_all()) == 1
    assert agg.drain_all() == []


def test_two_sources_same_minute_two_rows(clock: FixedClock) -> None:
    agg = make(clock)
    claimed(agg)
    agg.add(adv(source=SRC_A))
    agg.add(adv(source=SRC_B))
    clock.advance(minutes=1)
    rows = agg.drain_closed()
    assert {r.source for r in rows} == {SRC_A, SRC_B}


def test_unclaimed_not_in_rows(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv(address=ADDR_2))
    clock.advance(minutes=5)
    assert agg.drain_closed() == []
    assert agg.drain_all() == []


def test_ibeacon_is_candidate_immediately(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv())
    cands = agg.candidates()
    assert [c.identity.kind for c in cands] == ["ibeacon"]
    assert cands[0].identity.value == f"{BEACON_UUID}/1/1"


def test_static_random_is_candidate_immediately(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv(address=_static_random()))
    assert len(agg.candidates()) == 1


def test_known_vendor_is_candidate(clock: FixedClock) -> None:
    agg = make(clock, known_vendor=lambda a: a == ADDR_2)
    agg.add(adv(address=ADDR_2))
    agg.add(adv(address=ADDR_1))
    assert [c.identity.value for c in agg.candidates()] == [ADDR_2]


def test_rotating_address_not_candidate_before_30_min(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv())
    clock.advance(minutes=29)
    agg.add(adv())
    assert agg.candidates() == []


def test_address_becomes_candidate_after_30_min_span(clock: FixedClock) -> None:
    agg = make(clock)
    first = clock.now
    for _ in range(4):
        agg.add(adv(name="Tag"))
        clock.advance(minutes=10)
    assert STABLE_SPAN == timedelta(minutes=30)
    (cand,) = agg.candidates()
    assert cand.first_heard == first
    assert cand.last_heard == first + timedelta(minutes=30)
    assert cand.adverts == 4


def test_tracker_entry_expires_30_min_after_last_heard(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv())
    clock.advance(minutes=30)
    agg.add(adv(address=ADDR_2))  # lazy expiry; ADDR_1 is exactly 30 min old, still tracked
    clock.advance(minutes=1)
    agg.add(adv(address=ADDR_2))  # ADDR_1 now expired
    clock.advance(minutes=29)
    agg.add(adv())  # fresh start: span restarts, not a candidate
    assert agg.candidates() == []


def test_candidate_expires_after_24h(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv())
    clock.advance(hours=24)
    assert len(agg.candidates()) == 1
    clock.advance(seconds=1)
    assert agg.candidates() == []
    assert CANDIDATE_TTL == timedelta(hours=24)


def test_candidate_cap_evicts_oldest(clock: FixedClock) -> None:
    agg = make(clock, candidate_cap=3)
    for minor in (1, 2, 3, 4):
        agg.add(beacon_adv(minor=minor))
        clock.advance(seconds=1)
    values = {c.identity.value for c in agg.candidates()}
    assert values == {f"{BEACON_UUID}/1/{m}" for m in (2, 3, 4)}


def test_candidates_newest_first(clock: FixedClock) -> None:
    agg = make(clock)
    for minor in (1, 2, 3):
        agg.add(beacon_adv(minor=minor))
        clock.advance(seconds=1)
    agg.add(beacon_adv(minor=1))
    assert [c.identity.value.rsplit("/", 1)[1] for c in agg.candidates()] == ["1", "3", "2"]


def test_tracker_cap_evicts_oldest(clock: FixedClock) -> None:
    agg = make(clock, tracker_cap=3)
    for addr in (ADDR_1, ADDR_2, ADDR_3, ADDR_4):
        agg.add(adv(address=addr))  # same instant: insertion order decides eviction
    # ADDR_1 was evicted, so hearing it 30 min after its first advert restarts the span.
    clock.advance(minutes=30)
    agg.add(adv(address=ADDR_1))
    agg.add(adv(address=ADDR_4))  # still tracked: span is 30 min
    assert [c.identity.value for c in agg.candidates()] == [ADDR_4]


def test_set_index_claims_future_adverts(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv())
    assert len(agg.candidates()) == 1
    clock.advance(seconds=20)
    agg.set_index({Identity("ibeacon", f"{BEACON_UUID}/1/1"): 9})
    assert agg.candidates() == []
    agg.add(beacon_adv(rssi=-40))
    clock.advance(minutes=1)
    (row,) = agg.drain_closed()
    assert (row.device_id, row.adverts, row.max_rssi) == (9, 1, -40)


def test_set_index_removes_tracked_identity(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv())
    claimed(agg)
    clock.advance(minutes=31)
    agg.add(adv())
    assert agg.candidates() == []


def test_typical_gap_is_median_interval(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv())
    assert agg.candidates()[0].typical_gap_s is None
    for gap in (10, 20, 100):
        clock.advance(seconds=gap)
        agg.add(beacon_adv())
    (cand,) = agg.candidates()
    assert cand.typical_gap_s == 20.0


def test_candidate_fields_use_latest_source_best_rssi_latest_name(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv(rssi=-50))
    agg.add(
        adv(
            address=ADDR_3,
            rssi=-80,
            source=SRC_B,
            name="Tag",
            manufacturer_data=beacon_adv().manufacturer_data,
        )
    )
    agg.add(beacon_adv(rssi=-90))
    (cand,) = agg.candidates()
    assert cand.best_rssi == -50
    assert cand.source == SRC_A
    assert cand.name == "Tag"
    assert cand.adverts == 3
