"""IEEE OUI (24-bit prefix) vendor lookup."""

from __future__ import annotations

import csv
from pathlib import Path

from .netutil import is_random_mac, normalize_mac


class OuiDB:
    def __init__(self, table: dict[str, str]) -> None:
        self._table = table

    @classmethod
    def load(cls, path: Path) -> OuiDB:
        """Parse IEEE ``oui.csv`` (col 2 = hex prefix, col 3 = organization)."""
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
        return cls(table)

    def lookup(self, mac: str) -> str | None:
        """Vendor for ``mac``; None if unknown or the MAC is randomized."""
        try:
            norm = normalize_mac(mac)
        except ValueError:
            return None
        if is_random_mac(norm):
            return None
        return self._table.get(norm.replace(":", "")[:6])
