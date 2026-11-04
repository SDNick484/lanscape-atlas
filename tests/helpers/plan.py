"""Floor plan test helpers: synthetic images, schema text, and room/placement shortcuts.

Images are generated in memory; no image file is ever committed. Pillow is imported
lazily so modules importing this file collect even before the dependency is installed.
"""

from __future__ import annotations

import io
import sqlite3
from importlib.resources import files
from typing import Any

SQ_POINTS = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]


def _image_bytes(fmt: str, w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 220, 240)).save(buf, format=fmt)
    return buf.getvalue()


def png_bytes(w: int, h: int) -> bytes:
    return _image_bytes("PNG", w, h)


def jpeg_bytes(w: int, h: int) -> bytes:
    return _image_bytes("JPEG", w, h)


def svg_bytes(w: int, h: int) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" fill="#ccddee"/></svg>'
    ).encode()


def schema_sql_v1() -> str:
    """Text of ``store/schema.sql`` (the Phase 1, user_version 1 schema)."""
    return (files("lanscape_atlas") / "store" / "schema.sql").read_text()


def save_square_room(svc: Any, name: str = "Room A") -> int:
    """Save a square room through the service and return its place id.

    ``save_room`` returns a batch id, which only equals the place id on a fresh
    database, so the id is looked up by name.
    """
    svc.save_room("user", name, SQ_POINTS)
    row = svc.conn.execute("SELECT id FROM place WHERE name = ?", (name,)).fetchone()
    place_id: int = row[0]
    return place_id


def svc_last(db: sqlite3.Connection, device_id: int) -> int:
    """Latest placement batch id for a device."""
    from lanscape_atlas.service import queries

    batch: int = queries.last_placement_batch(db, device_id)
    return batch
