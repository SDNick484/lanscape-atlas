import pytest

from lanscape_atlas.floorplan.geometry import (
    check_aspect,
    parse_polygon,
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
