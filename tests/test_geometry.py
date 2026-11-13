import math

import pytest

from lanscape_atlas.floorplan.geometry import (
    check_aspect,
    parse_polygon,
    plan_length_mm,
    point_in_polygon,
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
