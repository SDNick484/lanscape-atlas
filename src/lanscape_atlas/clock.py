"""UTC clock helpers; timestamps are stored as ``YYYY-MM-DDTHH:MM:SSZ``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_FMT)


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=UTC)
