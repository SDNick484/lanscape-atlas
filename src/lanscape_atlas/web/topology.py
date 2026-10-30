"""Topology graph and writes shared by the JSON API and the topology page (Task 17).

``GET /api/v1/topology`` and ``GET /topology/data`` both return ``topology_graph``;
the bearer-token API and the CSRF-protected page endpoints both write through
``save_layout``, ``add_link`` and the ``Service`` methods, so the two surfaces
cannot drift. Request bodies live here for the same reason.

Edges: manual/discovered ``link`` rows, plus edges derived at query time from
``interface.ap_node`` (the AiMesh node's MAC, stored by the asusrouter collector):
client device -> device owning an interface with that address. Derived edges are
collapsed to one per unordered device pair (first interface id, ``wifi`` if any
interface on the pair is wifi) and dropped where a manual link joins the pair.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, Field, FiniteFloat

from lanscape_atlas.service import Service

# sqlite INTEGER range; ids start at 1. Out of range -> 422, never an OverflowError 500.
MAX_ID = 2**63 - 1
BodyId = Annotated[int, Field(ge=1, le=MAX_ID)]

Graph = dict[str, list[dict[str, Any]]]


class PlaceholderBody(BaseModel):
    name: str
    type: str


class LinkBody(BaseModel):
    a_device_id: BodyId
    b_device_id: BodyId
    kind: str
    detail: str | None = None


class LayoutBody(BaseModel):
    positions: dict[BodyId, tuple[FiniteFloat, FiniteFloat]]


def dedupe_edges(
    links: list[dict[str, Any]], derived: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``links`` unchanged, then ``derived`` collapsed per unordered pair.

    The first derived edge on a pair keeps its id and direction; the pair's kind
    is ``wifi`` if any of its edges is. A pair joined by a manual link loses its
    derived edges entirely.
    """
    manual_pairs = {frozenset((e["source"], e["target"])) for e in links if e["manual"]}
    by_pair: dict[frozenset[int], dict[str, Any]] = {}
    for e in derived:
        pair = frozenset((e["source"], e["target"]))
        if pair in manual_pairs:
            continue
        kept = by_pair.get(pair)
        if kept is None:
            by_pair[pair] = dict(e)
        elif e["kind"] == "wifi":
            kept["kind"] = "wifi"
    return [*links, *by_pair.values()]


def topology_graph(conn: sqlite3.Connection) -> Graph:
    """Nodes (every device, with saved positions) and deduplicated edges.

    ``placeholder`` marks devices with no interfaces (drawn by hand, e.g. an
    unmanaged switch): they can never be seen online, so the page does not dim them.

    The caller holds ``service.lock``.
    """
    nodes = [
        {
            "id": r["id"],
            "name": r["name"],
            "type": r["type"],
            "icon": r["icon"],
            "online": bool(r["online"]),
            "placeholder": not r["has_iface"],
            "x": r["x"],
            "y": r["y"],
        }
        for r in conn.execute(
            "SELECT d.id, d.name, d.type, d.icon, d.online, t.x, t.y,"
            " EXISTS (SELECT 1 FROM interface i WHERE i.device_id = d.id) AS has_iface"
            " FROM device d"
            " LEFT JOIN topology_layout t ON t.device_id = d.id ORDER BY d.id"
        )
    ]
    links: list[dict[str, Any]] = [
        {
            "id": r["id"],
            "source": r["a_device_id"],
            "target": r["b_device_id"],
            "kind": r["kind"],
            "manual": r["source"] == "manual",
        }
        for r in conn.execute(
            "SELECT id, a_device_id, b_device_id, kind, source FROM link ORDER BY id"
        )
    ]
    derived: list[dict[str, Any]] = []
    seen: set[int] = set()
    for r in conn.execute(
        "SELECT i.id, i.device_id, i.kind, ap.device_id AS ap_device FROM interface i"
        " JOIN interface ap ON ap.address = i.ap_node"
        " WHERE i.ap_node IS NOT NULL ORDER BY i.id, ap.id"
    ):
        iface_id = int(r["id"])
        if iface_id in seen or r["ap_device"] == r["device_id"]:
            continue
        seen.add(iface_id)
        derived.append(
            {
                "id": f"ap-{iface_id}",
                "source": r["device_id"],
                "target": r["ap_device"],
                "kind": "wifi" if r["kind"] == "wifi" else "wired",
                "manual": False,
            }
        )
    return {"nodes": nodes, "edges": dedupe_edges(links, derived)}


def save_layout(svc: Service, positions: Mapping[int, tuple[float, float]]) -> int:
    """Upsert saved node positions in one transaction; returns how many were written.

    A layout preference, so not logged as a change. Non-finite coordinates raise
    ``ValueError`` (422) and an unknown device id raises ``KeyError`` (404); either
    way nothing is written.
    """
    for device_id, (x, y) in positions.items():
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"position for device {device_id} must be finite numbers")
    conn = svc.conn
    with svc.lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for device_id, (x, y) in positions.items():
                if (
                    conn.execute("SELECT 1 FROM device WHERE id = ?", (device_id,)).fetchone()
                    is None
                ):
                    raise KeyError(f"no device with id {device_id}")
                conn.execute(
                    "INSERT INTO topology_layout(device_id, x, y) VALUES (?, ?, ?)"
                    " ON CONFLICT(device_id) DO UPDATE SET x = excluded.x, y = excluded.y",
                    (device_id, float(x), float(y)),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return len(positions)


def add_link(svc: Service, actor: str, body: LinkBody) -> dict[str, int]:
    """``Service.add_link`` plus the new link's id: ``{"batch_id", "link_id"}``."""
    batch_id = svc.add_link(actor, body.a_device_id, body.b_device_id, body.kind, body.detail)
    with svc.lock:
        row = svc.conn.execute(
            "SELECT entity_id FROM change WHERE batch_id = ? AND entity = 'link' AND field = '*'",
            (batch_id,),
        ).fetchone()
    return {"batch_id": batch_id, "link_id": int(row[0])}


def create_placeholder(svc: Service, actor: str, body: PlaceholderBody) -> dict[str, int]:
    """``Service.create_placeholder_device``: ``{"device_id", "batch_id"}``."""
    device_id, batch_id = svc.create_placeholder_device(actor, body.name, body.type)
    return {"device_id": device_id, "batch_id": batch_id}
