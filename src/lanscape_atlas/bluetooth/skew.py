"""Map Home Assistant advert timestamps onto atlas's clock (pure, no I/O).

HA's ``time`` is epoch seconds from HA's own clock, which may be skewed against atlas's.
Per connection, ``offset = min(receipt - time)`` over the adverts received on it; an advert's
effective time is ``min(receipt, time + offset)``. The freshest advert seen fixes the offset,
so a replayed (old) advert keeps its true age, shifted later by at most the transport delay
of that freshest advert, and nothing is ever placed after its receipt.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

# An HA time this far ahead of receipt is not skew but garbage; it must not set the offset.
MAX_AHEAD = timedelta(hours=24)


def effective_time(receipt: datetime, ha_time: float, offset: float) -> datetime:
    """``min(receipt, ha_time + offset)`` as a UTC datetime."""
    return datetime.fromtimestamp(min(receipt.timestamp(), ha_time + offset), UTC)


class SkewCorrector:
    """Tracks the HA-to-atlas clock offset for one connection; ``reset`` at each connect."""

    def __init__(self) -> None:
        self._offset: float | None = None

    def reset(self) -> None:
        self._offset = None

    def observe(self, receipt: datetime, ha_time: float) -> bool:
        """Fold one advert into the offset; False (ignored) if its time is implausible."""
        delta = receipt.timestamp() - ha_time
        if not math.isfinite(delta) or -delta > MAX_AHEAD.total_seconds():
            return False
        if self._offset is None or delta < self._offset:
            self._offset = delta
        return True

    def effective(self, receipt: datetime, ha_time: float) -> datetime:
        """The advert's time on atlas's clock; ``receipt`` until an offset is known."""
        if self._offset is None:
            return receipt
        return effective_time(receipt, ha_time, self._offset)
