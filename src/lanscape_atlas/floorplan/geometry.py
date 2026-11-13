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


# ------------------------------------------------- furniture and labels (issue #11)

# A rotated rectangle's corners may overshoot the plan by this much (float rounding of the
# 6-place stored values); anything further is refused, not clamped.
FIT_TOL = 2e-6
LABEL_MAX_SIZE = 1.0


def parse_furniture(raw: object) -> dict[str, Any]:
    """Validate furniture; returns it canonical. Two forms, each with exactly one key:

    ``{"rect": {"cx", "cy", "w", "h", "angle"}}``: centre in ``[0, 1]``; ``w`` and ``h``
    above 0, as fractions of the plan's width; ``angle`` in degrees in aspect-corrected
    space (so 90 looks square on a non-square plan), within one turn either way. Whether
    the rotated corners stay on the plan needs the plan's aspect: ``check_furniture_fits``.

    ``{"points": [[x, y], ...]}``: a closed outline of 3 to 500 points in ``[0, 1]``; no
    two consecutive points (including last and first) may be the same.
    """
    if not isinstance(raw, Mapping) or len(raw) != 1 or not set(raw) <= {"rect", "points"}:
        raise ValueError("furniture geometry must be an object with exactly one of rect or points")
    if "rect" in raw:
        rect = _keys(raw["rect"], "furniture rect", frozenset({"cx", "cy", "w", "h", "angle"}))
        angle = _finite(rect["angle"], "furniture angle")
        if abs(angle) > DOOR_MAX_ANGLE:
            raise ValueError("furniture angle must be between -360 and 360 degrees")
        size: dict[str, float] = {}
        for key in ("w", "h"):
            v = _finite(rect[key], f"furniture {key}")
            if v <= 0.0:
                raise ValueError(f"furniture {key} must be above 0")
            size[key] = v
        return {
            "rect": {
                "cx": _unit(rect["cx"], "furniture cx"),
                "cy": _unit(rect["cy"], "furniture cy"),
                "w": size["w"],
                "h": size["h"],
                "angle": angle,
            }
        }
    pts = raw["points"]
    if not isinstance(pts, list):
        raise ValueError("furniture points must be a list of [x, y] pairs")
    if not 3 <= len(pts) <= WALL_MAX_POINTS:
        raise ValueError(f"a furniture outline has 3 to {WALL_MAX_POINTS} points")
    points: list[list[float]] = []
    for item in pts:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("each furniture point must be an [x, y] pair")
        points.append(
            [_unit(item[0], "furniture coordinates"), _unit(item[1], "furniture coordinates")]
        )
    if any(a == b for a, b in zip(points, points[1:] + points[:1], strict=True)):
        raise ValueError("two consecutive furniture points are the same point")
    return {"points": points}


def parse_label(raw: object) -> dict[str, Any]:
    """Validate a text label's position: ``{"x", "y", "size"}``; returns it canonical.

    ``x``/``y`` in ``[0, 1]``; ``size`` (the text height as a fraction of the plan's
    width) above 0 and at most 1. The text itself is the shape's ``label``.
    """
    geo = _keys(raw, "label", frozenset({"x", "y", "size"}))
    size = _finite(geo["size"], "label size")
    if not 0.0 < size <= LABEL_MAX_SIZE:
        raise ValueError("label size must be above 0 and at most 1 (the plan's width)")
    return {"x": _unit(geo["x"], "label x"), "y": _unit(geo["y"], "label y"), "size": size}


def rect_corners(rect: Mapping[str, Any], *, aspect: float) -> list[tuple[float, float]]:
    """The four normalized corners of a furniture ``rect`` on a plan ``aspect`` deep.

    ``aspect`` is plan height / width. The rectangle is rotated in aspect-corrected
    space ``(x, y * aspect)``, where ``w`` and ``h`` (fractions of the plan width) are
    true lengths, then converted back to normalized coordinates.
    """
    a = math.radians(float(rect["angle"]))
    ux, uy = math.cos(a), math.sin(a)
    cx, cy = float(rect["cx"]), float(rect["cy"]) * aspect
    hw, hh = float(rect["w"]) / 2, float(rect["h"]) / 2
    corners: list[tuple[float, float]] = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        dx, dy = sx * hw, sy * hh
        corners.append((cx + dx * ux - dy * uy, (cy + dx * uy + dy * ux) / aspect))
    return corners


def check_furniture_fits(geometry: Mapping[str, Any], *, width: int, height: int) -> None:
    """Raise ValueError if a furniture rectangle's rotated corner is off the plan.

    A rotated rectangle near the edge can have a corner outside ``[0, 1]`` even when its
    centre is inside. The server refuses it (a clamp would change its shape or size).
    Outlines are already bounded point by point.
    """
    rect = geometry.get("rect")
    if rect is None:
        return
    if min(width, height) <= 0:
        raise ValueError("plan width and height must be positive")
    for x, y in rect_corners(rect, aspect=height / width):
        if not (-FIT_TOL <= x <= 1 + FIT_TOL and -FIT_TOL <= y <= 1 + FIT_TOL):
            raise ValueError("a furniture rectangle must stay on the plan: a corner is outside it")


def _segments_cross(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]
) -> bool:
    """Do segments ab and cd intersect (touching counts)?"""

    def orient(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2, d3, d4 = orient(c, d, a), orient(c, d, b), orient(a, b, c), orient(a, b, d)
    if ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and (
        (d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)
    ):
        return True
    return (
        (abs(d1) <= _EPS and _on_segment(a[0], a[1], c, d))
        or (abs(d2) <= _EPS and _on_segment(b[0], b[1], c, d))
        or (abs(d3) <= _EPS and _on_segment(c[0], c[1], a, b))
        or (abs(d4) <= _EPS and _on_segment(d[0], d[1], a, b))
    )


def polygon_is_simple(points: Sequence[tuple[float, float]]) -> bool:
    """True if no two non-adjacent edges of the polygon cross or touch."""
    n = len(points)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # adjacent edges share a corner
            if _segments_cross(points[i], points[(i + 1) % n], points[j], points[(j + 1) % n]):
                return False
    return True


def polygon_area_mm2(
    points: Sequence[tuple[float, float]], *, width: int, height: int, width_mm: float
) -> float | None:
    """Area in mm² of a normalized polygon on a plan ``width_mm`` wide (spec §6.3).

    Shoelace in aspect-corrected units (``y * height / width``) times ``width_mm``
    squared; always non-negative, whichever way the polygon winds. A polygon whose edges
    cross itself has no meaningful area, so it gets ``None`` (the page shows none).
    """
    if min(width, height) <= 0 or not math.isfinite(width_mm) or width_mm <= 0:
        raise ValueError("plan width, height and width_mm must be positive")
    if len(points) < 3 or not polygon_is_simple(points):
        return None
    aspect = height / width
    twice = 0.0
    for (x0, y0), (x1, y1) in zip(points, [*points[1:], points[0]], strict=True):
        twice += x0 * y1 * aspect - x1 * y0 * aspect
    return abs(twice) / 2 * width_mm * width_mm
