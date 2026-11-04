"""Safe reading and storage of floor plan images (png, jpg, svg)."""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from pathlib import Path

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET
from PIL import Image

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000
ALLOWED_EXT = frozenset({"png", "jpg", "svg"})

_LENGTH_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(px)?\s*$")


def validate_upload(data: bytes, ext: str) -> None:
    """Raise ValueError for an unknown extension, empty data or an oversized upload."""
    if ext not in ALLOWED_EXT:
        raise ValueError(f"unsupported image type: {ext!r}")
    if not data:
        raise ValueError("empty upload")
    if len(data) > MAX_BYTES:
        raise ValueError("image is larger than 10 MiB")


def _length(value: str | None) -> int | None:
    if value is None:
        return None
    m = _LENGTH_RE.match(value)
    if not m:
        return None
    n = round(float(m.group(1)))
    return n if n > 0 else None


def _svg_dimensions(data: bytes) -> tuple[int, int]:
    try:
        root = SafeET.fromstring(data)
    except (DefusedXmlException, SafeET.ParseError, ValueError) as exc:
        raise ValueError(f"unreadable SVG: {exc}") from exc
    if root.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
        raise ValueError("not an SVG document")
    w = _length(root.get("width"))
    h = _length(root.get("height"))
    if w and h:
        return w, h
    box = root.get("viewBox")
    if box:
        parts = box.replace(",", " ").split()
        if len(parts) == 4:
            try:
                vw, vh = round(float(parts[2])), round(float(parts[3]))
            except ValueError as exc:
                raise ValueError("bad SVG viewBox") from exc
            if vw > 0 and vh > 0:
                return vw, vh
    raise ValueError("SVG has no usable width/height or viewBox")


def _pixel_limit_error(w: int, h: int) -> ValueError:
    return ValueError(f"image exceeds the pixel limit ({w}x{h} > {MAX_PIXELS} pixels)")


def _raster_dimensions(data: bytes, ext: str) -> tuple[int, int]:
    expected = "PNG" if ext == "png" else "JPEG"
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format != expected:
                raise ValueError(f"data is not a {expected} image")
            # Image.open is lazy: img.size comes from the header, nothing is decoded yet.
            # Check the declared size before verify() decompresses the pixel data.
            w, h = img.size
            if w * h > MAX_PIXELS:
                raise _pixel_limit_error(w, h)
            img.verify()
        # verify() invalidates the image; reopen to read the size.
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
    except ValueError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        # Pillow's own guard fired inside open() (e.g. warnings promoted to errors).
        raise ValueError(f"image exceeds the pixel limit: {exc}") from exc
    except Exception as exc:  # Pillow raises many types (OSError, ...)
        raise ValueError(f"unreadable image: {exc}") from exc
    if w <= 0 or h <= 0:
        raise ValueError("image has no size")
    return w, h


def read_dimensions(data: bytes, ext: str) -> tuple[int, int]:
    """Return (width, height) in pixels, or raise ValueError for anything unreadable."""
    if ext == "svg":
        return _svg_dimensions(data)
    if ext in ("png", "jpg"):
        return _raster_dimensions(data, ext)
    raise ValueError(f"unsupported image type: {ext!r}")


def store_plan_file(data_dir: Path, data: bytes, ext: str) -> tuple[str, str]:
    """Write data to data_dir/floorplan/<sha256>.<ext>; return (sha256, file_name)."""
    if ext not in ALLOWED_EXT:
        raise ValueError(f"unsupported image type: {ext!r}")
    digest = hashlib.sha256(data).hexdigest()
    name = f"{digest}.{ext}"
    target_dir = data_dir / "floorplan"
    target_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    target = target_dir / name
    if target.exists():
        return digest, name
    fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return digest, name
