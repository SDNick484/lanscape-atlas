#!/usr/bin/env python3
"""Fail if tracked files contain personal network data.

Usage: python scripts/check_leaks.py [paths...]
With no paths, scans every file from ``git ls-files``. Exits 0 when clean,
1 after printing ``path:line: reason`` lines.

The optional denylist (one term per line, ``#`` comments, case-insensitive) is read from
``$LANSCAPE_DENYLIST`` (must exist if set) or, when that is unset,
``~/.config/lanscape-atlas/denylist.txt``. ``$LANSCAPE_LEAK_DENYLIST`` is an additional
source (e.g. room names) with the same rules and is also fail-closed; terms from all
sources are merged.

MAC addresses, 128-bit UUIDs (other than the documentation UUIDs and the Bluetooth base) and
denylist terms are flagged in every file. IPv4 and IPv6 addresses are flagged unless they are
in a documentation range, loopback, unspecified, multicast or (IPv4) broadcast; link-local
IPv6 is flagged only when it embeds an EUI-64 interface ID (a MAC). The IP checks skip the
paths in ``IP_ALLOWED_PATH_GLOBS`` and honour ``IP_FALSE_POSITIVES``. An unreadable tracked
file is an error.

Image files (.png .jpg .jpeg .svg .gif .webp) must never be committed, whether found by
the default ``git ls-files`` scan or passed as explicit paths (pre-commit). Only paths
under ``docs/screenshots/`` (synthetic renders) are exempt.
"""

from __future__ import annotations

import ipaddress
import os
import posixpath
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

# Spec section 9: the path allow-list exempts a file from the IP-address checks
# ONLY. MAC addresses, UUIDs and denylist terms are flagged everywhere.
IP_ALLOWED_PATH_GLOBS = (
    "docs/superpowers/**",
    "scripts/check_leaks.py",
    "tests/test_check_leaks.py",
)
# Exact address-shaped tokens that are not addresses (version strings, OIDs, ...), each
# (path glob, token, reason). A token is ignored only in files matching its glob.
IP_FALSE_POSITIVES: tuple[tuple[str, str, str], ...] = ()
# Documentation MAC blocks (RFC 7042 style) and synthetic random MACs,
# compared as 10 lowercase hex digits with separators removed.
ALLOWED_MAC_PREFIXES = ("00005e0053", "02005e0053")
# The canonical documentation placeholder, allowed as an exact match only.
PLACEHOLDER_MACS = ("aabbccddeeff",)
# Only these IPv4 networks are documentation addresses (RFC 5737). Any other IPv4 address is a
# leak, including RFC 1918, CGNAT (100.64.0.0/10), public WAN addresses and link-local.
IPV4_DOC_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
# The only IPv6 documentation range (RFC 3849).
IPV6_DOC_NETWORK = ipaddress.ip_network("2001:db8::/32")
IPV4_BROADCAST = ipaddress.IPv4Address("255.255.255.255")

# Boundaries reject only an adjacent hex digit, so "mac:<mac>" and "<mac>: up" match.
MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
    r"|(?<![0-9A-Fa-f.])(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}(?![0-9A-Fa-f])"
)
# Maximal runs of IPv6 characters; each is validated with ipaddress, so full and compressed
# forms are both caught and anything that is not an IPv6 address is ignored. A word character
# on either side rules a run out, so CSS "name::after", XPath "ancestor::div" and
# "ignore::Warning" are not read as addresses.
IPV6_CANDIDATE_RE = re.compile(r"(?<!\w)[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*(?!\w)")
# 128-bit UUIDs (iBeacon UUIDs, custom service UUIDs) identify real devices. Allowed: the
# documentation UUIDs 00000000-0000-4000-8000-0000000000xx and anything on the Bluetooth
# base UUID (xxxxxxxx-0000-1000-8000-00805f9b34fb, the 16/32-bit assigned numbers).
UUID_RE = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
    r"-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
)
ALLOWED_UUID_RE = re.compile(
    r"00000000-0000-4000-8000-0000000000[0-9a-f]{2}|[0-9a-f]{8}-0000-1000-8000-00805f9b34fb"
)
# Exact UUID values a vendored file carries as library constants, keyed by the repo-relative
# path they may appear in. Swagger UI 5.34.1 (swagger-ui-bundle.js) uses this one as the
# sample value for JSON Schema "uuid" and "string_uuid" formats; it is not a device UUID.
# Path-scoped and exact-value: nothing else in the tree may use it, and the file may not
# carry any other UUID.
# Split so this file does not itself match UUID_RE; the joined value is the UUID.
_SWAGGER_UI_SAMPLE_UUID = "3fa85f64" + "-5717-4562-b3fc-2c963f66afa6"
VENDORED_UUID_ALLOW: dict[str, frozenset[str]] = {
    "src/lanscape_atlas/web/static/swagger-ui-bundle.js": frozenset({_SWAGGER_UI_SAMPLE_UUID}),
}
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]*\d)")


class DenylistError(Exception):
    """The denylist was explicitly requested but cannot be read."""


def _read_terms(path: Path, label: str) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DenylistError(f"cannot read {label}={path}: {exc}") from exc
    terms = (line.strip().lower() for line in text.splitlines())
    return {t for t in terms if t and not t.startswith("#")}


def load_denylist() -> set[str]:
    """Load lowercase denylist terms from every configured source and merge them.

    ``$LANSCAPE_DENYLIST`` and ``$LANSCAPE_LEAK_DENYLIST`` must be readable when set
    (fail closed). With ``$LANSCAPE_DENYLIST`` unset, a missing file at the default
    location simply means no denylist from that source.
    """
    terms: set[str] = set()
    override = os.environ.get("LANSCAPE_DENYLIST")
    if override:
        terms |= _read_terms(Path(override), "LANSCAPE_DENYLIST")
    else:
        default = Path.home() / ".config" / "lanscape-atlas" / "denylist.txt"
        if default.exists():
            terms |= _read_terms(default, "default denylist")
    extra = os.environ.get("LANSCAPE_LEAK_DENYLIST")
    if extra is not None:
        if not extra:
            raise DenylistError("LANSCAPE_LEAK_DENYLIST is set but empty")
        terms |= _read_terms(Path(extra), "LANSCAPE_LEAK_DENYLIST")
    return terms


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")
IMAGE_EXEMPT_PREFIX = "docs/screenshots/"


def is_forbidden_image(path: str) -> bool:
    norm = posixpath.normpath(path.replace("\\", "/"))
    if not norm.lower().endswith(IMAGE_SUFFIXES):
        return False
    return not norm.startswith(IMAGE_EXEMPT_PREFIX)


def is_ip_exempt(path: str) -> bool:
    return any(fnmatch(path, glob) for glob in IP_ALLOWED_PATH_GLOBS)


def is_ip_false_positive(path: str, token: str) -> bool:
    return any(
        fnmatch(path, glob) and token == value for glob, value, _reason in IP_FALSE_POSITIVES
    )


def _ipv4_is_leak(raw: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(raw)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip == IPV4_BROADCAST:
        return False
    return not any(ip in net for net in IPV4_DOC_NETWORKS)


def _ipv6_address(token: str) -> ipaddress.IPv6Address | None:
    """Parse a candidate run as IPv6, tolerating a trailing ``:`` or ``.`` from prose."""
    for candidate in (token, token.rstrip(":."), token.rstrip(":.") + ":"):
        try:
            return ipaddress.IPv6Address(candidate)
        except ValueError:
            continue
    return None


def _ipv6_is_leak(ip: ipaddress.IPv6Address) -> bool:
    if ip in IPV6_DOC_NETWORK or ip.is_loopback or ip.is_unspecified:
        return False
    if ip.is_link_local:
        # A link-local address is only a leak when its interface ID is EUI-64 (the MAC is
        # visible as bytes 11-12 == ff:fe). Other link-local IDs identify nothing.
        return ip.packed[11:13] == b"\xff\xfe"
    return True


def _mask_ipv6(line: str) -> str:
    """Blank out valid IPv6 addresses so their tails are not read as MACs."""

    def repl(m: re.Match[str]) -> str:
        token = m.group()
        if _ipv6_address(token) is None:
            return token
        return " " * len(token)

    return IPV6_CANDIDATE_RE.sub(repl, line)


def find_leaks(text: str, path: str, denylist: set[str]) -> list[str]:
    """Return ``path:line: reason`` strings for each leak found in ``text``."""
    check_ips = not is_ip_exempt(path)
    vendored_uuids = VENDORED_UUID_ALLOW.get(path, frozenset())
    found: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in MAC_RE.finditer(_mask_ipv6(line)):
            digits = re.sub(r"[^0-9a-f]", "", m.group().lower())
            if digits not in PLACEHOLDER_MACS and not digits.startswith(ALLOWED_MAC_PREFIXES):
                found.append(f"{path}:{lineno}: MAC address outside documentation range")
        for m in UUID_RE.finditer(line):
            value = m.group().lower()
            if not ALLOWED_UUID_RE.fullmatch(value) and value not in vendored_uuids:
                found.append(f"{path}:{lineno}: UUID outside the documentation range")
        if check_ips:
            for m in IPV4_RE.finditer(line):
                if _ipv4_is_leak(m.group()) and not is_ip_false_positive(path, m.group()):
                    found.append(f"{path}:{lineno}: IPv4 address outside the documentation ranges")
            for m in IPV6_CANDIDATE_RE.finditer(line):
                ip6 = _ipv6_address(m.group())
                if ip6 is None or not _ipv6_is_leak(ip6):
                    continue
                if is_ip_false_positive(path, m.group()):
                    continue
                found.append(f"{path}:{lineno}: IPv6 address outside the documentation range")
        lowered = line.lower()
        for term in sorted(denylist):
            if term in lowered:
                found.append(f"{path}:{lineno}: denylisted term")
    return found


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True).stdout
    return out.splitlines()


def main(argv: list[str]) -> int:
    try:
        denylist = load_denylist()
    except DenylistError as exc:
        print(f"check_leaks: {exc}", file=sys.stderr)
        return 2
    paths = argv or tracked_files()
    leaks: list[str] = []
    for path in paths:
        if is_forbidden_image(path):
            leaks.append(
                f"{path}: image file must not be committed (plans and photos stay in the data dir)"
            )
            continue
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            # An unreadable file could hide anything, so it fails the check rather than passing.
            leaks.append(f"{path}: cannot read file, so it cannot be checked ({exc.strerror})")
            continue
        if b"\0" in data:
            continue  # binary
        leaks.extend(find_leaks(data.decode("utf-8", errors="replace"), path, denylist))
    for leak in leaks:
        print(leak)
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
