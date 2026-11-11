"""HA-time to atlas-time mapping for one connection (clock-skew correction)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lanscape_atlas.bluetooth.skew import MAX_AHEAD, SkewCorrector, effective_time

NOW = datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC)
T = NOW.timestamp()


def test_effective_time_is_ha_time_plus_offset_capped_at_receipt() -> None:
    assert effective_time(NOW, T - 90, 0.0) == NOW - timedelta(seconds=90)
    assert effective_time(NOW, T - 90, 30.0) == NOW - timedelta(seconds=60)
    assert effective_time(NOW, T + 10, 0.0) == NOW  # never later than receipt


@pytest.mark.parametrize("ha_ahead_s", [30.0, -30.0, 0.0])
def test_skew_corrected_for_fresh_and_replayed_adverts(ha_ahead_s: float) -> None:
    """HA's clock 30 s ahead, 30 s behind, or equal: true times come back."""
    skew = SkewCorrector()
    true_fresh = NOW - timedelta(seconds=1)  # heard 1 s before atlas received it
    true_old = NOW - timedelta(minutes=7)  # replayed from HA's history
    batch = [true_fresh.timestamp() + ha_ahead_s, true_old.timestamp() + ha_ahead_s]
    for t in batch:
        assert skew.observe(NOW, t)
    fresh, old = (skew.effective(NOW, t) for t in batch)
    # Only the transport delay of the freshest advert is unknowable: everything is
    # shifted later by at most that, never misfiled into another minute.
    assert fresh == NOW
    assert old == true_old + timedelta(seconds=1)
    assert old.strftime("%H:%M") == "00:03"


def test_offset_only_tightens_within_a_connection() -> None:
    skew = SkewCorrector()
    skew.observe(NOW, T - 5)  # first sample: 5 s transport delay (or 5 s skew)
    assert skew.effective(NOW, T - 5) == NOW
    later = NOW + timedelta(seconds=60)
    skew.observe(later, later.timestamp() - 1)  # a fresher sample: 1 s
    # The same advert replayed now maps 4 s earlier, so it is never "newer".
    assert skew.effective(later, T - 5) == NOW - timedelta(seconds=4)


def test_reset_starts_fresh() -> None:
    skew = SkewCorrector()
    skew.observe(NOW, T - 1)
    skew.reset()
    later = NOW + timedelta(seconds=60)
    skew.observe(later, later.timestamp() + 30)  # HA's clock was changed meanwhile
    assert skew.effective(later, later.timestamp() + 30) == later


def test_effective_before_observe_uses_receipt() -> None:
    assert SkewCorrector().effective(NOW, T - 600) == NOW


def test_far_future_time_is_rejected_and_never_poisons_the_offset() -> None:
    skew = SkewCorrector()
    assert not skew.observe(NOW, T + MAX_AHEAD.total_seconds() + 1)
    assert skew.observe(NOW, T - 2)
    assert skew.effective(NOW, T - 62) == NOW - timedelta(seconds=60)
