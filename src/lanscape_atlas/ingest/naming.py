"""Hostname hygiene and device-name selection from per-source hints."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from lanscape_atlas.netutil import normalize_mac

# Observation source -> hint source. ``attrs["name_source"]`` overrides this;
# sources not listed here (arp, nmap) contribute no name hint.
HINT_SOURCE: dict[str, str] = {"asusrouter": "dhcp", "mdns": "mdns", "ssdp": "ssdp", "rdns": "rdns"}

# Best first (spec section 6, step 5).
PRIORITY: tuple[str, ...] = ("mdns", "dhcp", "ssdp", "rdns")

_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_JUNK_WORDS = frozenset({"*", "unknown", "localhost"})


def is_junk_hostname(name: str | None, *, mac: str | None, ipv4: str | None) -> bool:
    """True when ``name`` carries no information about the device."""
    if name is None:
        return True
    s = name.strip().lower()
    if not s or s in _JUNK_WORDS or _HEX12.match(s):
        return True
    if ipv4 is not None and s == ipv4.strip().lower():
        return True
    if mac is not None:
        try:
            if normalize_mac(s) == normalize_mac(mac):
                return True
        except ValueError:
            pass
    return False


def clean_name(source: str, name: str) -> str:
    """Strip mDNS service suffixes (from the first ``._``) and a trailing ``.local``."""
    s = name.strip()
    if source == "mdns":
        idx = s.find("._")
        if idx != -1:
            s = s[:idx]
    if s.lower().endswith(".local"):
        s = s[: -len(".local")]
    return s.strip()


VENDOR_SOURCE = "vendor"


def _is_any_mac(name: str, macs: Iterable[str]) -> bool:
    try:
        norm = normalize_mac(name)
    except ValueError:
        return False
    for mac in macs:
        try:
            if normalize_mac(mac) == norm:
                return True
        except ValueError:
            continue
    return False


def pick_name(
    candidates: Mapping[str, str | None],
    vendor: str | None,
    ipv4: str | None,
    *,
    macs: Iterable[str] = (),
) -> tuple[str | None, str | None]:
    """Like :func:`best_name`, also returning the winning source.

    The source is one of ``PRIORITY`` or ``"vendor"`` for the fallback; a cleaned
    candidate equal to any of ``macs`` (in any separator form) is junk.
    """
    mac_list = list(macs)
    for source in PRIORITY:
        raw = candidates.get(source)
        if raw is None:
            continue
        cleaned = clean_name(source, raw)
        if is_junk_hostname(cleaned, mac=None, ipv4=ipv4) or _is_any_mac(cleaned, mac_list):
            continue
        return cleaned, source
    if vendor and ipv4:
        return f"{vendor} .{ipv4.rsplit('.', 1)[-1]}", VENDOR_SOURCE
    return None, None


def best_name(
    candidates: Mapping[str, str | None], vendor: str | None, ipv4: str | None
) -> str | None:
    """Best name: mdns > dhcp > ssdp > rdns, skipping junk; else ``"<vendor> .<octet>"``."""
    return pick_name(candidates, vendor, ipv4)[0]
