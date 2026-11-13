"""Pure geometry helpers for floor plan rooms and drawn shapes (normalized 0..1 coordinates)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

_EPS = 1e-12
WALL_MAX_POINTS = 500
# A door's angle is in degrees in aspect-corrected space; one full turn either way.
DOOR_MAX_ANGLE = 360.0
DOOR_FLIPS = range(4)


def parse_polygon(raw: object) -> list[tuple[float, float]]:
    """Validate ``raw`` as a polygon of >= 3 ``[x, y]`` pairs in ``[0, 1]``."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("polygon must be a list of [x, y] pairs")
    if len(raw) < 3:
        raise ValueError("polygon needs at least 3 points")
    points: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("each polygon point must be an [x, y] pair")
        coords: list[float] = []
        for v in item:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError("polygon coordinates must be numbers")
            try:
                f = float(v)
            except OverflowError:
                raise ValueError("polygon coordinates must be finite and in [0, 1]") from None
            if not math.isfinite(f) or not 0.0 <= f <= 1.0:
                raise ValueError("polygon coordinates must be finite and in [0, 1]")
            coords.append(f)
        points.append((coords[0], coords[1]))
    return points


def _on_segment(x: float, y: float, a: tuple[float, float], b: tuple[float, float]) -> bool:
    cross = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
    if abs(cross) > _EPS:
        return False
    return (
        min(a[0], b[0]) - _EPS <= x <= max(a[0], b[0]) + _EPS
        and min(a[1], b[1]) - _EPS <= y <= max(a[1], b[1]) + _EPS
    )


def point_in_polygon(x: float, y: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Even-odd ray cast; points on an edge or vertex count as inside."""
    n = len(polygon)
    for i in range(n):
        if _on_segment(x, y, polygon[i], polygon[(i + 1) % n]):
            return True
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def check_aspect(old_w: int, old_h: int, new_w: int, new_h: int) -> None:
    """Raise ValueError unless old and new image aspect ratios match."""
    if min(old_w, old_h, new_w, new_h) <= 0:
        raise ValueError("image dimensions must be positive")
    if not math.isclose(old_w / old_h, new_w / new_h, rel_tol=1e-3):
        raise ValueError("aspect ratio differs from the existing floor plan image")


def plan_length_mm(dx: float, dy: float, *, width: int, height: int, width_mm: float) -> float:
    """Real-world length in mm of the normalized plan delta ``(dx, dy)``.

    ``width``/``height`` are the plan's size in its own units (pixels for an image,
    mm for a blank plan) and ``width_mm`` its real width; the plan is ``width_mm *
    height / width`` deep, so ``dy`` is scaled by that depth (spec §5.1).
    """
    if min(width, height) <= 0 or not math.isfinite(width_mm) or width_mm <= 0:
        raise ValueError("plan width, height and width_mm must be positive")
    return math.hypot(dx * width_mm, dy * width_mm * height / width)


# ------------------------------------------------------------- drawn shapes (§6.1)


def _finite(value: object, what: str) -> float:
    """``value`` as a finite float; bools and non-numbers are refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number")
    try:
        f = float(value)
    except OverflowError:
        raise ValueError(f"{what} must be finite") from None
    if not math.isfinite(f):
        raise ValueError(f"{what} must be finite")
    return f


def _unit(value: object, what: str) -> float:
    f = _finite(value, what)
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"{what} must be in [0, 1]")
    return f


def _keys(raw: object, kind: str, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{kind} geometry must be an object")
    if set(raw) != keys:
        raise ValueError(f"{kind} geometry must have exactly the keys {sorted(keys)}")
    return raw


def parse_wall(raw: object) -> dict[str, Any]:
    """Validate a wall: ``{"points": [[x, y], ...], "closed": bool}``; returns it canonical.

    2 to 500 points, each finite and in ``[0, 1]``. Two consecutive points may not be
    the same (so a two-point wall is never zero-length); a closed wall needs at least 3
    points and its last point may not repeat its first (closing is implied).
    """
    geo = _keys(raw, "wall", frozenset({"points", "closed"}))
    closed = geo["closed"]
    if not isinstance(closed, bool):
        raise ValueError("wall closed must be true or false")
    pts = geo["points"]
    if not isinstance(pts, list):
        raise ValueError("wall points must be a list of [x, y] pairs")
    if not 2 <= len(pts) <= WALL_MAX_POINTS:
        raise ValueError(f"a wall has 2 to {WALL_MAX_POINTS} points")
    points: list[list[float]] = []
    for item in pts:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("each wall point must be an [x, y] pair")
        points.append([_unit(item[0], "wall coordinates"), _unit(item[1], "wall coordinates")])
    if closed and len(points) < 3:
        raise ValueError("a closed wall needs at least 3 points")
    pairs = list(zip(points[:-1], points[1:], strict=True))
    if closed:
        pairs.append((points[-1], points[0]))
    if any(a == b for a, b in pairs):
        raise ValueError("two consecutive wall points are the same point")
    return {"points": points, "closed": closed}


def parse_door(raw: object) -> dict[str, Any]:
    """Validate a door: ``{"x", "y", "angle", "width", "flip"}``; returns it canonical.

    ``x``/``y``: the centre of the opening on the wall line, in ``[0, 1]``. ``angle``:
    the wall's direction in degrees in aspect-corrected space (so 90 looks square on a
    non-square plan), within one turn either way. ``width``: the opening as a fraction
    of the plan's width, in ``(0, 1]``. ``flip``: 0 to 3; bit 0 swings the door to the
    other side of the wall, bit 1 moves the hinge to the other end of the opening.
    """
    geo = _keys(raw, "door", frozenset({"x", "y", "angle", "width", "flip"}))
    angle = _finite(geo["angle"], "door angle")
    if abs(angle) > DOOR_MAX_ANGLE:
        raise ValueError("door angle must be between -360 and 360 degrees")
    width = _finite(geo["width"], "door width")
    if not 0.0 < width <= 1.0:
        raise ValueError("door width must be above 0 and at most 1 (the plan's width)")
    flip = geo["flip"]
    if isinstance(flip, bool) or not isinstance(flip, int) or flip not in DOOR_FLIPS:
        raise ValueError("door flip must be 0, 1, 2 or 3")
    return {
        "x": _unit(geo["x"], "door x"),
        "y": _unit(geo["y"], "door y"),
        "angle": angle,
        "width": width,
        "flip": flip,
    }
