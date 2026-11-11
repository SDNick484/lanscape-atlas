"""In-memory per-minute sighting aggregator and unclaimed-candidate list.

Claimed adverts (identity present in the index set via ``set_index``) are folded into
one row per (device, scanner, minute); a flush job drains closed minutes into the DB.
Unclaimed adverts feed a bounded candidate list for the Find page.

Every advert comes with its *effective time*: Home Assistant's ``time`` mapped onto atlas's
clock (``bluetooth.skew``). Minutes are bucketed by it (UTC), not by receipt, because HA's
stream is change-driven and most adverts arrive as a periodic snapshot replay of HA's
history. Per identity only an advert strictly newer than the last one counted is used, so a
replayed advert is never counted twice; adverts older than 24 h are ignored.
"""

from __future__ import annotations

import statistics
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from lanscape_atlas.bluetooth.match import Advert, Identity, identity_of, is_static_random
from lanscape_atlas.clock import Clock

CANDIDATE_TTL = timedelta(hours=24)
STABLE_SPAN = timedelta(minutes=30)
TRACKER_TTL = timedelta(minutes=30)
_GAP_WINDOW = 20  # inter-advert intervals kept for the median
# ble_sighting.minute format (UTC); strings in it compare chronologically.
BLE_MINUTE_FMT = "%Y-%m-%dT%H:%MZ"


@dataclass(frozen=True)
class MinuteRow:
    device_id: int
    source: str
    minute: str
    max_rssi: int
    adverts: int
    last_at: datetime


@dataclass(frozen=True)
class Candidate:
    identity: Identity
    name: str | None
    best_rssi: int
    source: str
    first_heard: datetime
    last_heard: datetime
    adverts: int
    typical_gap_s: float | None


@dataclass(frozen=True)
class CandidateView:
    shown: list[Candidate]
    weak: int  # candidates below the threshold, whether shown or not


def visible_candidates(
    candidates: Iterable[Candidate], *, min_rssi: int, include_weak: bool
) -> CandidateView:
    """Strongest best RSSI first (ties: most recently heard first); weak ones only on request."""
    ordered = sorted(candidates, key=lambda c: (-c.best_rssi, -c.last_heard.timestamp()))
    weak = sum(1 for c in ordered if c.best_rssi < min_rssi)
    if not include_weak:
        ordered = [c for c in ordered if c.best_rssi >= min_rssi]
    return CandidateView(ordered, weak)


@dataclass
class _Seen:
    first_heard: datetime
    last_heard: datetime
    best_rssi: int
    source: str
    name: str | None
    adverts: int = 1
    stamps: deque[datetime] = field(default_factory=lambda: deque(maxlen=_GAP_WINDOW + 1))

    def hear(self, adv: Advert, now: datetime) -> None:
        self.last_heard = now
        self.best_rssi = max(self.best_rssi, adv.rssi)
        self.source = adv.source
        if adv.name is not None:
            self.name = adv.name
        self.adverts += 1
        self.stamps.append(now)

    def gap(self) -> float | None:
        if len(self.stamps) < 2:
            return None
        s = list(self.stamps)
        return float(
            statistics.median((b - a).total_seconds() for a, b in zip(s, s[1:], strict=False))
        )


@dataclass
class _Bucket:
    max_rssi: int
    adverts: int
    last_at: datetime


class BleAggregator:
    """Aggregates adverts per minute and tracks unclaimed candidates.

    Not thread-safe by design: it is driven from a single asyncio loop.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        known_vendor: Callable[[str], bool],
        candidate_cap: int = 500,
        tracker_cap: int = 5000,
    ) -> None:
        self._clock = clock
        self._known_vendor = known_vendor
        self._candidate_cap = candidate_cap
        self._tracker_cap = tracker_cap
        self._index: Mapping[Identity, int] = {}
        # Effective time of the last advert counted per claimed identity (replay dedupe).
        self._last_counted: dict[Identity, datetime] = {}
        self._buckets: dict[tuple[int, str, str], _Bucket] = {}
        # Both maps are ordered least- to most-recently heard.
        self._tracker: OrderedDict[Identity, _Seen] = OrderedDict()
        self._candidates: OrderedDict[Identity, _Seen] = OrderedDict()

    def set_index(self, index: Mapping[Identity, int]) -> None:
        self._index = dict(index)
        self._last_counted = {i: t for i, t in self._last_counted.items() if i in self._index}
        for ident in self._index:
            self._tracker.pop(ident, None)
            self._candidates.pop(ident, None)

    def add(self, adv: Advert, at: datetime) -> None:
        """Count one advert heard at effective time ``at`` (see the module docstring)."""
        now = self._clock()
        self._expire(now)
        if at < now - CANDIDATE_TTL:
            return
        ident = identity_of(adv)
        device_id = self._index.get(ident)
        if device_id is not None:
            last = self._last_counted.get(ident)
            if last is not None and at <= last:
                return  # a replay of an advert already counted
            self._last_counted[ident] = at
            self._add_claimed(device_id, adv, at)
            return
        seen = self._candidates.get(ident)
        if seen is not None:
            if at > seen.last_heard:
                seen.hear(adv, at)
                self._candidates.move_to_end(ident)
            return
        seen = self._tracker.get(ident)
        if seen is None:
            seen = _Seen(at, at, adv.rssi, adv.source, adv.name)
            seen.stamps.append(at)
        elif at > seen.last_heard:
            seen.hear(adv, at)
        else:
            return
        if self._qualifies(ident, seen):
            self._tracker.pop(ident, None)
            self._candidates[ident] = seen
            self._evict(self._candidates, self._candidate_cap)
        elif seen.last_heard < now - TRACKER_TTL:
            self._tracker.pop(ident, None)  # a stale replay: already expired
        else:
            self._tracker[ident] = seen
            self._tracker.move_to_end(ident)
            self._evict(self._tracker, self._tracker_cap)

    def drain_closed(self) -> list[MinuteRow]:
        current = self._clock().strftime(BLE_MINUTE_FMT)
        return self._drain(lambda minute: minute < current)

    def drain_all(self) -> list[MinuteRow]:
        return self._drain(lambda _minute: True)

    def requeue(self, rows: Sequence[MinuteRow]) -> None:
        """Merge drained rows back into their buckets (a failed DB write loses nothing).

        Combines with any bucket that has since reappeared: max of ``max_rssi``,
        sum of ``adverts``, max of ``last_at``.
        """
        for r in rows:
            key = (r.device_id, r.source, r.minute)
            b = self._buckets.get(key)
            if b is None:
                self._buckets[key] = _Bucket(r.max_rssi, r.adverts, r.last_at)
            else:
                b.max_rssi = max(b.max_rssi, r.max_rssi)
                b.adverts += r.adverts
                b.last_at = max(b.last_at, r.last_at)

    def candidates(self) -> list[Candidate]:
        now = self._clock()
        self._expire(now)
        # Replays can arrive out of order, so expiry by map order is only a fast path.
        cutoff = now - CANDIDATE_TTL
        return [
            Candidate(
                identity=ident,
                name=s.name,
                best_rssi=s.best_rssi,
                source=s.source,
                first_heard=s.first_heard,
                last_heard=s.last_heard,
                adverts=s.adverts,
                typical_gap_s=s.gap(),
            )
            for ident, s in reversed(self._candidates.items())
            if s.last_heard >= cutoff
        ]

    def _add_claimed(self, device_id: int, adv: Advert, at: datetime) -> None:
        key = (device_id, adv.source, at.strftime(BLE_MINUTE_FMT))
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(adv.rssi, 1, at)
        else:
            bucket.max_rssi = max(bucket.max_rssi, adv.rssi)
            bucket.adverts += 1
            bucket.last_at = max(bucket.last_at, at)

    def _drain(self, take: Callable[[str], bool]) -> list[MinuteRow]:
        keys = sorted((k for k in self._buckets if take(k[2])), key=lambda k: (k[2], k[0], k[1]))
        rows = []
        for key in keys:
            b = self._buckets.pop(key)
            rows.append(MinuteRow(key[0], key[1], key[2], b.max_rssi, b.adverts, b.last_at))
        return rows

    def _qualifies(self, ident: Identity, seen: _Seen) -> bool:
        if ident.kind == "ibeacon":
            return True
        if is_static_random(ident.value) or self._known_vendor(ident.value):
            return True
        return seen.last_heard - seen.first_heard >= STABLE_SPAN

    def _expire(self, now: datetime) -> None:
        self._expire_map(self._tracker, now - TRACKER_TTL)
        self._expire_map(self._candidates, now - CANDIDATE_TTL)

    @staticmethod
    def _expire_map(m: OrderedDict[Identity, _Seen], cutoff: datetime) -> None:
        while m:
            ident, seen = next(iter(m.items()))
            if seen.last_heard >= cutoff:
                break
            del m[ident]

    @staticmethod
    def _evict(m: OrderedDict[Identity, _Seen], cap: int) -> None:
        while len(m) > cap:
            m.popitem(last=False)
