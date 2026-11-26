"""IEEE OUI (24-bit prefix) vendor lookup.

The table reloads itself when ``oui.csv`` changes on disk (``lanscape-atlas update-oui``
replaces the file), so a running service picks up a new list without a restart. The
file's mtime is checked at most once per ``RELOAD_INTERVAL_S`` seconds, from inside
``lookup``. A file that fails to parse keeps the table already loaded and logs one
warning for that version of the file.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from collections.abc import Callable
from importlib.resources import as_file, files
from pathlib import Path

from .netutil import is_random_mac, normalize_mac

log = logging.getLogger(__name__)

RELOAD_INTERVAL_S = 60.0
CUSTOM_NAME = "oui.csv"

Clock = Callable[[], float]


def _parse(path: Path) -> dict[str, str]:
    """IEEE ``oui.csv`` (col 2 = hex prefix, col 3 = organization) to a prefix table."""
    table: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for row in reader:
            if len(row) < 3:
                continue
            prefix = row[1].strip().lower()
            if len(prefix) == 6:
                table[prefix] = row[2].strip()
    return table


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


class OuiDB:
    def __init__(
        self,
        table: dict[str, str],
        *,
        watch: Path,
        seen_mtime: int | None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._table = table
        self._watch = watch
        self._seen_mtime = seen_mtime  # mtime of the file behind the table (None: none yet)
        self._clock = clock
        self._checked_at: float | None = None
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: Path, clock: Clock = time.monotonic) -> OuiDB:
        """Load ``path`` and watch it for changes."""
        return cls(_parse(path), watch=path, seen_mtime=_mtime(path), clock=clock)

    @classmethod
    def load_data_dir(cls, data_dir: Path, clock: Clock = time.monotonic) -> OuiDB:
        """``data_dir/oui.csv`` if present, else the packaged sample.

        The custom file is watched even when the sample is in use, so the first
        ``update-oui`` takes effect without a restart.
        """
        custom = data_dir / CUSTOM_NAME
        if custom.is_file():
            return cls.load(custom, clock)
        with as_file(files("lanscape_atlas") / "data" / "oui_sample.csv") as sample:
            table = _parse(sample)
        return cls(table, watch=custom, seen_mtime=None, clock=clock)

    def _reload_if_changed(self) -> None:
        mtime = _mtime(self._watch)
        if mtime is None or mtime == self._seen_mtime:
            # Missing file: keep the table we have. Unchanged: nothing to do.
            return
        self._seen_mtime = mtime
        try:
            table = _parse(self._watch)
        except (OSError, ValueError, csv.Error) as exc:
            log.warning("keeping previous OUI table; cannot reload %s: %s", self._watch, exc)
            return
        self._table = table
        log.info("reloaded OUI table from %s (%d prefixes)", self._watch, len(table))

    def _maybe_reload(self) -> None:
        now = self._clock()
        with self._lock:
            if self._checked_at is not None and now - self._checked_at < RELOAD_INTERVAL_S:
                return
            self._checked_at = now
            self._reload_if_changed()

    def lookup(self, mac: str) -> str | None:
        """Vendor for ``mac``; None if unknown or the MAC is randomized."""
        self._maybe_reload()
        try:
            norm = normalize_mac(mac)
        except ValueError:
            return None
        if is_random_mac(norm):
            return None
        return self._table.get(norm.replace(":", "")[:6])
