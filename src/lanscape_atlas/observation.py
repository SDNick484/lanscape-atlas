"""Immutable record returned by collectors and consumed by the ingestor."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lanscape_atlas.clock import iso, parse_iso


@dataclass(frozen=True)
class Observation:
    source: str
    seen_at: datetime  # UTC, tz-aware
    iface_kind: str | None
    mac: str | None
    ipv4: str | None
    hostname: str | None
    attrs: dict[str, Any]

    def to_json(self) -> str:
        """Deterministic JSON (sorted keys) so observations can be deduped."""
        return json.dumps(
            {
                "source": self.source,
                "seen_at": iso(self.seen_at),
                "iface_kind": self.iface_kind,
                "mac": self.mac,
                "ipv4": self.ipv4,
                "hostname": self.hostname,
                "attrs": self.attrs,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, s: str) -> Observation:
        d = json.loads(s)
        return cls(
            source=d["source"],
            seen_at=parse_iso(d["seen_at"]),
            iface_kind=d.get("iface_kind"),
            mac=d.get("mac"),
            ipv4=d.get("ipv4"),
            hostname=d.get("hostname"),
            attrs=d.get("attrs") or {},
        )
