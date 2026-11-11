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
    return Advert(address, rssi, source, name, manufacturer_data or {}, time=1.0)


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
    agg.add(adv(rssi=-70), clock())
    clock.advance(seconds=10)
    agg.add(adv(rssi=-55), clock())
    clock.advance(seconds=10)
    agg.add(adv(rssi=-80), clock())
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
    agg.add(adv(), clock())
    assert agg.drain_closed() == []
    clock.advance(minutes=1)
    assert len(agg.drain_closed()) == 1
    assert agg.drain_closed() == []
    agg.add(adv(), clock())
    assert len(agg.drain_all()) == 1
    assert agg.drain_all() == []


def test_two_sources_same_minute_two_rows(clock: FixedClock) -> None:
    agg = make(clock)
    claimed(agg)
    agg.add(adv(source=SRC_A), clock())
    clock.advance(seconds=1)
    agg.add(adv(source=SRC_B), clock())
    clock.advance(minutes=1)
    rows = agg.drain_closed()
    assert {r.source for r in rows} == {SRC_A, SRC_B}


def test_unclaimed_not_in_rows(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv(address=ADDR_2), clock())
    clock.advance(minutes=5)
    assert agg.drain_closed() == []
    assert agg.drain_all() == []


def test_ibeacon_is_candidate_immediately(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv(), clock())
    cands = agg.candidates()
    assert [c.identity.kind for c in cands] == ["ibeacon"]
    assert cands[0].identity.value == f"{BEACON_UUID}/1/1"


def test_static_random_is_candidate_immediately(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv(address=_static_random()), clock())
    assert len(agg.candidates()) == 1


def test_known_vendor_is_candidate(clock: FixedClock) -> None:
    agg = make(clock, known_vendor=lambda a: a == ADDR_2)
    agg.add(adv(address=ADDR_2), clock())
    agg.add(adv(address=ADDR_1), clock())
    assert [c.identity.value for c in agg.candidates()] == [ADDR_2]


def test_rotating_address_not_candidate_before_30_min(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv(), clock())
    clock.advance(minutes=29)
    agg.add(adv(), clock())
    assert agg.candidates() == []


def test_address_becomes_candidate_after_30_min_span(clock: FixedClock) -> None:
    agg = make(clock)
    first = clock.now
    for _ in range(4):
        agg.add(adv(name="Tag"), clock())
        clock.advance(minutes=10)
    assert STABLE_SPAN == timedelta(minutes=30)
    (cand,) = agg.candidates()
    assert cand.first_heard == first
    assert cand.last_heard == first + timedelta(minutes=30)
    assert cand.adverts == 4


def test_tracker_entry_expires_30_min_after_last_heard(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv(), clock())
    clock.advance(minutes=30)
    agg.add(
        adv(address=ADDR_2), clock()
    )  # lazy expiry; ADDR_1 is exactly 30 min old, still tracked
    clock.advance(minutes=1)
    agg.add(adv(address=ADDR_2), clock())  # ADDR_1 now expired
    clock.advance(minutes=29)
    agg.add(adv(), clock())  # fresh start: span restarts, not a candidate
    assert agg.candidates() == []


def test_candidate_expires_after_24h(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv(), clock())
    clock.advance(hours=24)
    assert len(agg.candidates()) == 1
    clock.advance(seconds=1)
    assert agg.candidates() == []
    assert CANDIDATE_TTL == timedelta(hours=24)


def test_candidate_cap_evicts_oldest(clock: FixedClock) -> None:
    agg = make(clock, candidate_cap=3)
    for minor in (1, 2, 3, 4):
        agg.add(beacon_adv(minor=minor), clock())
        clock.advance(seconds=1)
    values = {c.identity.value for c in agg.candidates()}
    assert values == {f"{BEACON_UUID}/1/{m}" for m in (2, 3, 4)}


def test_candidates_newest_first(clock: FixedClock) -> None:
    agg = make(clock)
    for minor in (1, 2, 3):
        agg.add(beacon_adv(minor=minor), clock())
        clock.advance(seconds=1)
    agg.add(beacon_adv(minor=1), clock())
    assert [c.identity.value.rsplit("/", 1)[1] for c in agg.candidates()] == ["1", "3", "2"]


def test_tracker_cap_evicts_oldest(clock: FixedClock) -> None:
    agg = make(clock, tracker_cap=3)
    for addr in (ADDR_1, ADDR_2, ADDR_3, ADDR_4):
        agg.add(adv(address=addr), clock())  # same instant: insertion order decides eviction
    # ADDR_1 was evicted, so hearing it 30 min after its first advert restarts the span.
    clock.advance(minutes=30)
    agg.add(adv(address=ADDR_1), clock())
    agg.add(adv(address=ADDR_4), clock())  # still tracked: span is 30 min
    assert [c.identity.value for c in agg.candidates()] == [ADDR_4]


def test_set_index_claims_future_adverts(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv(), clock())
    assert len(agg.candidates()) == 1
    clock.advance(seconds=20)
    agg.set_index({Identity("ibeacon", f"{BEACON_UUID}/1/1"): 9})
    assert agg.candidates() == []
    agg.add(beacon_adv(rssi=-40), clock())
    clock.advance(minutes=1)
    (row,) = agg.drain_closed()
    assert (row.device_id, row.adverts, row.max_rssi) == (9, 1, -40)


def test_set_index_removes_tracked_identity(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(adv(), clock())
    claimed(agg)
    clock.advance(minutes=31)
    agg.add(adv(), clock())
    assert agg.candidates() == []


def test_typical_gap_is_median_interval(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv(), clock())
    assert agg.candidates()[0].typical_gap_s is None
    for gap in (10, 20, 100):
        clock.advance(seconds=gap)
        agg.add(beacon_adv(), clock())
    (cand,) = agg.candidates()
    assert cand.typical_gap_s == 20.0


def test_candidate_fields_use_latest_source_best_rssi_latest_name(clock: FixedClock) -> None:
    agg = make(clock)
    agg.add(beacon_adv(rssi=-50), clock())
    clock.advance(seconds=1)
    agg.add(
        adv(
            address=ADDR_3,
            rssi=-80,
            source=SRC_B,
            name="Tag",
            manufacturer_data=beacon_adv().manufacturer_data,
        ),
        clock(),
    )
    clock.advance(seconds=1)
    agg.add(beacon_adv(rssi=-90), clock())
    (cand,) = agg.candidates()
    assert cand.best_rssi == -50
    assert cand.source == SRC_A
    assert cand.name == "Tag"
    assert cand.adverts == 3


def test_requeue_merges_rows_back_into_buckets(clock: FixedClock) -> None:
    agg = BleAggregator(clock, known_vendor=lambda _a: False)
    agg.set_index({Identity("address", ADDR_1): 7})
    agg.add(adv(rssi=-70), clock())
    clock.advance(seconds=1)
    agg.add(adv(rssi=-70), clock())
    clock.advance(minutes=1)
    rows = agg.drain_closed()
    assert len(rows) == 1 and agg.drain_all() == []
    # new data for the same bucket arrives while the rows are out, then they come back
    agg.requeue(rows)
    agg.requeue(rows)
    (merged,) = agg.drain_all()
    assert (merged.max_rssi, merged.adverts, merged.last_at) == (-70, 4, rows[0].last_at)


def test_requeue_takes_max_rssi_and_latest_last_at(clock: FixedClock) -> None:
    agg = BleAggregator(clock, known_vendor=lambda _a: False)
    agg.set_index({Identity("address", ADDR_1): 7})
    agg.add(adv(rssi=-80), clock())
    old = agg.drain_all()
    t_old = old[0].last_at
    clock.advance(seconds=5)
    agg.add(adv(rssi=-50), clock())
    agg.requeue(old)
    (merged,) = agg.drain_all()
    assert (merged.max_rssi, merged.adverts) == (-50, 2)
    assert merged.last_at == t_old + timedelta(seconds=5)  # the later one wins
    # and the other way round: an older requeue never moves last_at back
    clock.advance(seconds=1)
    agg.add(adv(rssi=-90), clock())
    agg.requeue([old[0]])
    (again,) = agg.drain_all()
    assert again.last_at == t_old + timedelta(seconds=6)


# -- effective time (HA's time corrected for skew), replay dedupe ----------------------------


def test_buckets_by_effective_time_not_receipt(clock: FixedClock) -> None:
    agg = make(clock)
    claimed(agg)
    clock.advance(minutes=10)
    heard = clock.now - timedelta(minutes=6, seconds=30)  # replayed from HA's history
    agg.add(adv(rssi=-66), heard)
    (row,) = agg.drain_closed()
    assert (row.minute, row.last_at, row.adverts, row.max_rssi) == (
        "2026-01-01T00:03Z",
        heard,
        1,
        -66,
    )


def test_replayed_advert_not_double_counted(clock: FixedClock) -> None:
    agg = make(clock)
    claimed(agg)
    t = clock.now
    agg.add(adv(rssi=-70), t)
    agg.add(adv(rssi=-40), t)  # the same advert replayed by the next snapshot
    agg.add(adv(rssi=-40), t - timedelta(seconds=1))  # older still: ignored
    clock.advance(seconds=30)
    agg.add(adv(rssi=-65), t + timedelta(seconds=20))  # strictly newer: counted
    clock.advance(minutes=1)
    (row,) = agg.drain_closed()
    assert (row.adverts, row.max_rssi, row.last_at) == (2, -65, t + timedelta(seconds=20))


def test_replayed_candidate_not_double_counted(clock: FixedClock) -> None:
    agg = make(clock)
    t = clock.now
    agg.add(beacon_adv(), t)
    clock.advance(seconds=60)
    agg.add(beacon_adv(), t)  # snapshot replay of the same advert
    (cand,) = agg.candidates()
    assert (cand.adverts, cand.last_heard, cand.typical_gap_s) == (1, t, None)


def test_candidate_times_are_effective_times(clock: FixedClock) -> None:
    agg = make(clock)
    clock.advance(minutes=10)
    first = clock.now - timedelta(minutes=5)
    agg.add(beacon_adv(), first)
    agg.add(beacon_adv(), first + timedelta(minutes=2))
    (cand,) = agg.candidates()
    assert (cand.first_heard, cand.last_heard) == (first, first + timedelta(minutes=2))
    assert cand.typical_gap_s == 120.0


def test_adverts_older_than_24h_ignored(clock: FixedClock) -> None:
    agg = make(clock)
    claimed(agg)
    clock.advance(hours=30)
    stale = clock.now - timedelta(hours=24, seconds=1)
    agg.add(adv(), stale)
    agg.add(beacon_adv(), stale)
    assert agg.drain_all() == []
    assert agg.candidates() == []
    agg.add(adv(), clock.now - timedelta(hours=24))  # exactly 24 h: still kept
    assert len(agg.drain_all()) == 1


def test_late_advert_for_flushed_minute_is_drained_again(clock: FixedClock) -> None:
    agg = make(clock)
    claimed(agg)
    t0 = clock.now
    agg.add(adv(rssi=-70), t0 + timedelta(seconds=10))
    clock.advance(minutes=1, seconds=5)
    (first,) = agg.drain_closed()
    agg.add(adv(rssi=-60), t0 + timedelta(seconds=50))  # snapshot shows a later advert
    (late,) = agg.drain_closed()
    assert first.minute == late.minute == "2026-01-01T00:00Z"
    assert (late.adverts, late.max_rssi) == (1, -60)


def test_stale_replay_does_not_extend_an_address_span(clock: FixedClock) -> None:
    agg = make(clock)
    clock.advance(hours=1)
    agg.add(adv(address=ADDR_2), clock.now)  # heard now, ahead in the tracker
    agg.add(adv(), clock.now - timedelta(minutes=40))  # replayed: last heard 40 min ago
    agg.add(adv(), clock.now)  # a 40-min-old sighting is expired, so the span restarts
    assert agg.candidates() == []


def test_candidate_list_drops_entries_older_than_24h_out_of_order(clock: FixedClock) -> None:
    agg = make(clock)
    clock.advance(hours=30)
    agg.add(beacon_adv(minor=1), clock.now)
    agg.add(beacon_adv(minor=2), clock.now - timedelta(hours=23, minutes=59))
    assert len(agg.candidates()) == 2
    clock.advance(minutes=2)
    assert [c.identity.value[-1] for c in agg.candidates()] == ["1"]
