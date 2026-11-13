"""Pure geometry helpers for floor plan rooms (normalized 0..1 coordinates)."""

from __future__ import annotations

import math
from collections.abc import Sequence

_EPS = 1e-12


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
