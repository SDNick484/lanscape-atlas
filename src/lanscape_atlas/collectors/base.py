"""Collector protocol and shared error type."""

from __future__ import annotations

from typing import Protocol

from lanscape_atlas.observation import Observation


class CollectorError(Exception):
    """A collector failed to gather data (message must never contain secrets)."""


class Collector(Protocol):
    name: str
    timeout_s: float

    async def collect(self) -> list[Observation]: ...
