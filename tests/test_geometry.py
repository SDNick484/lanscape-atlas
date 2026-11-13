import math

import pytest

from lanscape_atlas.floorplan.geometry import (
    check_aspect,
    check_furniture_fits,
    parse_polygon,
    plan_length_mm,
    point_in_polygon,
    polygon_area_mm2,
    polygon_is_simple,
    rect_corners,
)

SQUARE = [(0.1, 0.1), (0.5, 0.1), (0.5, 0.5), (0.1, 0.5)]
L_SHAPE = [(0, 0), (1, 0), (1, 0.4), (0.4, 0.4), (0.4, 1), (0, 1)]


def test_inside_and_outside_square():
    assert point_in_polygon(0.3, 0.3, SQUARE)
    assert not point_in_polygon(0.7, 0.3, SQUARE)


def test_edge_and_vertex_count_as_inside():
    assert point_in_polygon(0.3, 0.1, SQUARE)  # on bottom edge
    assert point_in_polygon(0.5, 0.5, SQUARE)  # on vertex


@pytest.mark.parametrize(
    "x, y",
    [
        (0.3, 0.1),  # bottom
        (0.3, 0.5),  # top
        (0.1, 0.3),  # left
        (0.5, 0.3),  # right
        (0.1, 0.1),  # each vertex
        (0.5, 0.1),
        (0.1, 0.5),
    ],
)
def test_every_square_edge_and_vertex_is_inside(x, y):
    assert point_in_polygon(x, y, SQUARE)


@pytest.mark.parametrize(
    "x, y",
    [
        (0.3, 0.1 - 1e-9),
        (0.3, 0.5 + 1e-9),
        (0.1 - 1e-9, 0.3),
        (0.5 + 1e-9, 0.3),
    ],
)
def test_just_outside_square_edge_is_outside(x, y):
    assert not point_in_polygon(x, y, SQUARE)


@pytest.mark.parametrize(
    "x, y",
    [
        (0.7, 0.4),  # notch horizontal edge
        (0.4, 0.7),  # notch vertical edge
        (0.4, 0.4),  # reflex vertex
    ],
)
def test_l_shape_notch_edges_and_reflex_vertex_are_inside(x, y):
    assert point_in_polygon(x, y, L_SHAPE)


@pytest.mark.parametrize("x, y", [(0.7, 0.4 + 1e-9), (0.4 + 1e-9, 0.7), (0.4 + 1e-9, 0.4 + 1e-9)])
def test_just_inside_the_notch_is_outside(x, y):
    assert not point_in_polygon(x, y, L_SHAPE)


def test_concave_notch_is_outside():
    assert not point_in_polygon(0.7, 0.7, L_SHAPE)
    assert point_in_polygon(0.2, 0.7, L_SHAPE)


@pytest.mark.parametrize(
    "raw",
    [
        [[0.1, 0.1], [0.2, 0.2]],  # two points
        [[0.1, 0.1], [0.2, float("nan")], [0.3, 0.1]],
        [[0.1, 0.1], [1.5, 0.2], [0.3, 0.1]],  # above 1
        "not a list",
        [[0.1, 0.1], [0.2], [0.3, 0.1]],
        [[0.1, 0.1], [True, 0.2], [0.3, 0.1]],
        [[0.1, 0.1], ["a", 0.2], [0.3, 0.1]],
        [[0.1, 0.1], [-0.1, 0.2], [0.3, 0.1]],
        [[0.1, 0.1], [float("inf"), 0.2], [0.3, 0.1]],
        [[0.1, 0.1], [10**400, 0.2], [0.3, 0.1]],  # float() would overflow
        [[0.1, 0.1], [-(10**400), 0.2], [0.3, 0.1]],
    ],
)
def test_parse_polygon_rejects(raw):
    with pytest.raises(ValueError):
        parse_polygon(raw)


def test_parse_polygon_copies_to_tuples():
    raw = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]
    out = parse_polygon(raw)
    assert out == [(0.1, 0.1), (0.5, 0.1), (0.5, 0.5)]
    assert out is not raw


def test_aspect_same_ratio_different_size_ok():
    check_aspect(1600, 1200, 800, 600)


def test_aspect_different_ratio_rejected():
    with pytest.raises(ValueError):
        check_aspect(1600, 1200, 1920, 1080)


def test_aspect_within_tolerance_ok():
    check_aspect(1000, 1000, 1000, 1001)  # 1e-3 relative: boundary-ish, accepted


def test_aspect_just_beyond_tolerance_rejected():
    with pytest.raises(ValueError):
        check_aspect(1000, 1000, 1000, 1003)


@pytest.mark.parametrize("dims", [(0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 0, 1), (1, 1, 1, -1)])
def test_aspect_non_positive_dimension_rejected(dims):
    with pytest.raises(ValueError):
        check_aspect(*dims)


# ------------------------------------------------------------ plan_length_mm


def test_plan_length_mm_horizontal_uses_width_mm():
    # A 2:1 plan 10 m wide: half its width is 5 m.
    assert plan_length_mm(0.5, 0, width=2000, height=1000, width_mm=10_000) == pytest.approx(5000)


def test_plan_length_mm_vertical_corrects_for_aspect():
    # Same plan: it is 5 m deep, so half its height is 2.5 m (not 5 m).
    assert plan_length_mm(0, 0.5, width=2000, height=1000, width_mm=10_000) == pytest.approx(2500)


def test_plan_length_mm_diagonal_on_tall_plan():
    # A 1:3 plan (pixels) 4 m wide is 12 m deep; the full diagonal is sqrt(4^2 + 12^2) m.
    got = plan_length_mm(1, 1, width=500, height=1500, width_mm=4000)
    assert got == pytest.approx(math.hypot(4000, 12_000))


def test_plan_length_mm_sign_does_not_matter():
    args = {"width": 1600, "height": 1200, "width_mm": 8000}
    assert plan_length_mm(-0.25, -0.5, **args) == pytest.approx(plan_length_mm(0.25, 0.5, **args))
    assert plan_length_mm(0, 0, **args) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 0, "height": 10, "width_mm": 1000},
        {"width": 10, "height": -1, "width_mm": 1000},
        {"width": 10, "height": 10, "width_mm": 0},
        {"width": 10, "height": 10, "width_mm": float("nan")},
    ],
)
def test_plan_length_mm_rejects_bad_plan(kwargs):
    with pytest.raises(ValueError):
        plan_length_mm(0.1, 0.1, **kwargs)


# ---------------------------------------------- areas and rectangles (issue #11)


def test_area_of_a_known_rectangle_on_a_non_square_plan() -> None:
    # A 4:3 plan 12 m wide is 9 m deep. A rectangle spanning x 0.25..0.75 and y 0.2..0.8
    # is 6 m x 5.4 m = 32.4 m².
    rect = [(0.25, 0.2), (0.75, 0.2), (0.75, 0.8), (0.25, 0.8)]
    got = polygon_area_mm2(rect, width=4000, height=3000, width_mm=12_000)
    assert got == pytest.approx(32.4e6)
    # Same plan as a blank plan (width/height are mm): the same area.
    assert polygon_area_mm2(rect, width=12_000, height=9_000, width_mm=12_000) == pytest.approx(
        32.4e6
    )
    # A square plan would give 6 m x 7.2 m: the aspect matters.
    assert polygon_area_mm2(rect, width=1000, height=1000, width_mm=12_000) == pytest.approx(43.2e6)


def test_area_does_not_depend_on_winding_or_start() -> None:
    tri = [(0.1, 0.1), (0.9, 0.1), (0.1, 0.5)]
    a = polygon_area_mm2(tri, width=100, height=50, width_mm=10_000)
    assert a == pytest.approx(0.5 * 0.8 * 10_000 * 0.4 * 5_000)
    assert polygon_area_mm2(tri[::-1], width=100, height=50, width_mm=10_000) == pytest.approx(a)
    assert polygon_area_mm2(tri[1:] + tri[:1], width=100, height=50, width_mm=10_000) == (
        pytest.approx(a)
    )


def test_area_of_an_l_shape() -> None:
    got = polygon_area_mm2(L_SHAPE, width=1, height=1, width_mm=10)
    assert got == pytest.approx((1 - 0.6 * 0.6) * 100)


def test_self_intersecting_polygon_has_no_area() -> None:
    bowtie = [(0.1, 0.1), (0.5, 0.5), (0.5, 0.1), (0.1, 0.5)]
    assert not polygon_is_simple(bowtie)
    assert polygon_area_mm2(bowtie, width=1, height=1, width_mm=10) is None
    assert polygon_is_simple(SQUARE) and polygon_is_simple(L_SHAPE)


def test_polygon_is_simple_touching_edges_count_as_crossing() -> None:
    # A corner touching a non-adjacent edge is not a simple polygon.
    pinched = [(0, 0), (1, 0), (1, 1), (0.5, 0), (0, 1)]
    assert not polygon_is_simple(pinched)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"width": 0, "height": 1, "width_mm": 1.0}, "positive"),
        ({"width": 1, "height": 1, "width_mm": 0.0}, "positive"),
        ({"width": 1, "height": 1, "width_mm": math.nan}, "positive"),
    ],
)
def test_area_needs_a_real_plan(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        polygon_area_mm2(SQUARE, **kwargs)


def test_rect_corners_rotate_in_aspect_corrected_space() -> None:
    rect = {"cx": 0.5, "cy": 0.5, "w": 0.4, "h": 0.2, "angle": 0.0}
    flat = rect_corners(rect, aspect=0.5)
    assert [v for c in flat for v in c] == pytest.approx([0.3, 0.3, 0.7, 0.3, 0.7, 0.7, 0.3, 0.7])
    # Turned 90 degrees the 0.4 side runs down: 0.4 / 0.5 = 0.8 of the height.
    turned = rect_corners({**rect, "angle": 90.0}, aspect=0.5)
    assert sorted({round(y, 9) for _, y in turned}) == [0.1, 0.9]
    assert sorted({round(x, 9) for x, _ in turned}) == [0.4, 0.6]
    # Each side is its true length in aspect-corrected space whatever the angle.
    a = rect_corners({**rect, "angle": 37.0}, aspect=0.5)
    side = math.hypot(a[1][0] - a[0][0], (a[1][1] - a[0][1]) * 0.5)
    assert side == pytest.approx(0.4)


def test_check_furniture_fits() -> None:
    inside = {"rect": {"cx": 0.5, "cy": 0.5, "w": 0.4, "h": 0.2, "angle": 45.0}}
    check_furniture_fits(inside, width=100, height=50)
    check_furniture_fits({"points": [[0, 0], [1, 0], [1, 1]]}, width=100, height=50)
    out = {"rect": {"cx": 0.1, "cy": 0.5, "w": 0.4, "h": 0.2, "angle": 0.0}}
    with pytest.raises(ValueError, match="outside"):
        check_furniture_fits(out, width=100, height=50)
    with pytest.raises(ValueError, match="positive"):
        check_furniture_fits(inside, width=0, height=50)
