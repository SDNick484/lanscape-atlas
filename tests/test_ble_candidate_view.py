"""Candidate list ordering and the weak-signal filter (find page, API, MCP)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lanscape_atlas.bluetooth.aggregator import Candidate, CandidateView, visible_candidates
from lanscape_atlas.bluetooth.match import Identity

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SCANNER = "00:00:5e:00:53:aa"


def cand(n: int, rssi: int, minutes_ago: int = 0) -> Candidate:
    at = NOW - timedelta(minutes=minutes_ago)
    addr = f"00:00:5e:00:53:{n:02x}"
    return Candidate(Identity("address", addr), None, rssi, SCANNER, at, at, 1, None)


def keys(view: CandidateView) -> list[str]:
    return [c.identity.value[-2:] for c in view.shown]


def test_strongest_first_then_newest() -> None:
    items = [cand(1, -70, 5), cand(2, -50, 9), cand(3, -70, 1), cand(4, -60, 0)]
    view = visible_candidates(items, min_rssi=-80, include_weak=False)
    assert keys(view) == ["02", "04", "03", "01"]
    assert view.weak == 0


def test_weak_hidden_and_counted() -> None:
    items = [cand(1, -81), cand(2, -80), cand(3, -95), cand(4, -40)]
    view = visible_candidates(items, min_rssi=-80, include_weak=False)
    assert keys(view) == ["04", "02"]  # exactly the threshold is shown
    assert view.weak == 2


def test_include_weak_shows_all_sorted() -> None:
    items = [cand(1, -81), cand(2, -80), cand(3, -95), cand(4, -40)]
    view = visible_candidates(items, min_rssi=-80, include_weak=True)
    assert keys(view) == ["04", "02", "01", "03"]
    assert view.weak == 2  # still reported, so the page can offer "hide weak"


def test_excluded_candidates_are_dropped_and_not_counted_weak() -> None:
    items = [cand(1, -50), cand(2, -60), cand(3, -95), cand(4, -90)]
    hidden = {items[1].identity, items[2].identity}
    view = visible_candidates(
        items, min_rssi=-80, include_weak=False, exclude=lambda i: i in hidden
    )
    assert keys(view) == ["01"]
    assert view.weak == 1  # only 04; the hidden weak one (03) is not counted
    view = visible_candidates(items, min_rssi=-80, include_weak=True, exclude=hidden.__contains__)
    assert keys(view) == ["01", "04"]
