from __future__ import annotations

import pytest

from helpers.plan import jpeg_bytes, png_bytes, svg_bytes
from lanscape_atlas.floorplan.image import (
    MAX_BYTES,
    MAX_PIXELS,
    read_dimensions,
    store_plan_file,
    validate_upload,
)


def test_png_dimensions() -> None:
    assert read_dimensions(png_bytes(640, 480), "png") == (640, 480)


def test_jpeg_dimensions() -> None:
    assert read_dimensions(jpeg_bytes(320, 200), "jpg") == (320, 200)


def test_svg_viewbox_dimensions() -> None:
    assert read_dimensions(svg_bytes(800, 600), "svg") == (800, 600)


def test_svg_viewbox_only() -> None:
    data = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"/>'
    assert read_dimensions(data, "svg") == (400, 300)


def test_svg_width_height_with_px_unit() -> None:
    data = b'<svg xmlns="http://www.w3.org/2000/svg" width="120px" height="90px"/>'
    assert read_dimensions(data, "svg") == (120, 90)


def test_svg_without_size_is_refused() -> None:
    with pytest.raises(ValueError):
        read_dimensions(b'<svg xmlns="http://www.w3.org/2000/svg"/>', "svg")


def test_svg_entity_expansion_is_refused() -> None:
    bomb = (
        b'<!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        b'<svg width="1" height="1">&b;</svg>'
    )
    with pytest.raises(ValueError):
        read_dimensions(bomb, "svg")


def test_rejects_non_image_bytes() -> None:
    with pytest.raises(ValueError):
        read_dimensions(b"hello", "png")


def test_rejects_unknown_ext_in_read_dimensions() -> None:
    with pytest.raises(ValueError):
        read_dimensions(png_bytes(10, 10), "gif")


def test_rejects_empty_upload() -> None:
    with pytest.raises(ValueError):
        validate_upload(b"", "jpg")


def test_rejects_unknown_extension() -> None:
    with pytest.raises(ValueError):
        validate_upload(png_bytes(10, 10), "gif")


def test_rejects_oversized() -> None:
    with pytest.raises(ValueError, match="10 MiB"):
        validate_upload(b"x" * (MAX_BYTES + 1), "png")


def test_accepts_valid_upload() -> None:
    validate_upload(png_bytes(10, 10), "png")


def test_store_is_content_addressed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    a = store_plan_file(tmp_path, png_bytes(10, 10), "png")
    b = store_plan_file(tmp_path, png_bytes(10, 10), "png")
    assert a == b
    assert a[1] == f"{a[0]}.png"
    assert (tmp_path / "floorplan" / f"{a[0]}.png").exists()
    assert list((tmp_path / "floorplan").glob("*.tmp")) == []


@pytest.mark.filterwarnings("error::PIL.Image.DecompressionBombWarning")
def test_rejects_png_over_pixel_cap() -> None:
    # 100M pixels, a few KB on disk; must be refused by our cap, not only by Pillow's warning.
    data = png_bytes(10000, 10000)
    assert 10000 * 10000 > MAX_PIXELS
    with pytest.raises(ValueError, match="pixel limit"):
        read_dimensions(data, "png")


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_rejects_png_over_pixel_cap_without_warning_filter() -> None:
    with pytest.raises(ValueError, match="pixel limit"):
        read_dimensions(png_bytes(10000, 10000), "png")


def test_accepts_png_just_under_pixel_cap() -> None:
    assert 7000 * 7000 <= MAX_PIXELS
    assert read_dimensions(png_bytes(7000, 7000), "png") == (7000, 7000)
