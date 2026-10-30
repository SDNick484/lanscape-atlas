"""MAC and IPv4 normalization helpers."""

from __future__ import annotations

import ipaddress
import re

_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_SEPARATED = re.compile(r"^[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}$")
_DOTTED = re.compile(r"^[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}$")


def normalize_mac(raw: str) -> str:
    """Return ``aa:bb:cc:dd:ee:ff`` form; raise ValueError if ``raw`` is not a MAC."""
    s = raw.strip().lower()
    if _SEPARATED.match(s):
        digits = s.replace(s[2], "")
    elif _DOTTED.match(s):
        digits = s.replace(".", "")
    elif _HEX12.match(s):
        digits = s
    else:
        raise ValueError(f"invalid MAC address: {raw!r}")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def is_random_mac(mac: str) -> bool:
    """True if the locally-administered bit is set (randomized/private MAC)."""
    return bool(int(normalize_mac(mac).split(":")[0], 16) & 0x02)


def valid_ipv4(raw: str) -> str | None:
    """Return the normalized dotted quad, or None if ``raw`` is not a valid IPv4 address."""
    try:
        return str(ipaddress.IPv4Address(raw.strip()))
    except ValueError:
        return None
