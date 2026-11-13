"""Device search shared by the topology page (and the floor plan search that reuses it).

``search_devices`` wraps the shared matcher (``queries.list_devices(q=)``) and then ranks
the matches with ``rank_device_matches``, which is pure so any page can reuse it on rows
it already has. ``SearchQuery`` is the FastAPI parameter type that enforces the length cap
(422 above ``MAX_QUERY_LEN``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Annotated, Any

from fastapi import Query

from lanscape_atlas.netutil import normalize_mac, valid_ipv4
from lanscape_atlas.service import queries as q_

MAX_QUERY_LEN = 100
SEARCH_LIMIT = 20
# The matcher is asked for more than SEARCH_LIMIT so ranking sees every plausible match,
# not just the first ones by id.
MATCH_FETCH_LIMIT = 1000

SearchQuery = Annotated[str, Query(max_length=MAX_QUERY_LEN)]


def _exact_key(text: str) -> tuple[str | None, str | None]:
    """The IPv4 and the canonical MAC that ``text`` spells in full, if any."""
    ip = valid_ipv4(text)
    try:
        mac: str | None = normalize_mac(text)
    except ValueError:
        mac = None
    return ip, mac


def rank_device_matches(
    rows: Iterable[q_.DeviceSummary], q: str, *, limit: int = SEARCH_LIMIT
) -> list[dict[str, Any]]:
    """Rank matcher results for ``q`` and return at most ``limit`` ``{device_id, name, type}``.

    ``rows`` are ``DeviceSummary`` objects already matched by ``list_devices(q=)``.
    Order: (0) the device holding exactly the IPv4 ``q`` or having exactly the full MAC
    ``q`` (any common MAC spelling), (1) names starting with ``q`` (case-insensitive),
    (2) the rest; each group sorted by name ignoring case, then id. ``q`` is stripped; a
    blank ``q`` gives ``[]``. ``name`` is ``""`` for an unnamed device and ``type`` is
    ``None`` when unset.
    """
    text = q.strip()
    if not text or limit <= 0:
        return []
    lowered = text.lower()
    ip, mac = _exact_key(text)

    def key(r: q_.DeviceSummary) -> tuple[int, str, int]:
        name = (r.name or "").strip()
        if (ip is not None and r.ipv4 == ip) or (mac is not None and mac in r.macs):
            group = 0
        elif name.lower().startswith(lowered):
            group = 1
        else:
            group = 2
        return group, name.lower(), r.id

    return [
        {"device_id": r.id, "name": (r.name or "").strip(), "type": r.type}
        for r in sorted(rows, key=key)[:limit]
    ]


def search_devices(
    conn: sqlite3.Connection, q: str, *, window_min: int, limit: int = SEARCH_LIMIT
) -> list[dict[str, Any]]:
    """Match then rank. The caller holds ``service.lock``; blank ``q`` gives ``[]``."""
    if not q.strip():
        return []
    rows = q_.list_devices(conn, q=q, limit=MATCH_FETCH_LIMIT, window_min=window_min)
    return rank_device_matches(rows, q, limit=limit)
