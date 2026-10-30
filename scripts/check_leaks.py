#!/usr/bin/env python3
"""Fail if tracked files contain personal network data.

Usage: python scripts/check_leaks.py [paths...]
With no paths, scans every file from ``git ls-files``. Exits 0 when clean,
1 after printing ``path:line: reason`` lines.

The optional denylist (one term per line, ``#`` comments) is read from
``$LANSCAPE_DENYLIST`` (must exist if set) or ``~/.config/lanscape-atlas/denylist.txt``.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

# Spec section 9: the path allow-list exempts a file from the private-IP check
# ONLY. MAC addresses and denylist terms are flagged everywhere.
IP_ALLOWED_PATH_GLOBS = (
    "docs/superpowers/**",
    "scripts/check_leaks.py",
    "tests/test_check_leaks.py",
)
# Documentation MAC blocks (RFC 7042 style) and synthetic random MACs,
# compared as 10 lowercase hex digits with separators removed.
ALLOWED_MAC_PREFIXES = ("00005e0053", "02005e0053")
# The canonical documentation placeholder, allowed as an exact match only.
PLACEHOLDER_MACS = ("aabbccddeeff",)
# Exactly RFC 1918; loopback, link-local, 0.0.0.0 etc. are not leaks.
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

# Boundaries reject only an adjacent hex digit, so "mac:<mac>" and "<mac>: up" match.
MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
    r"|(?<![0-9A-Fa-f.])(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}(?![0-9A-Fa-f])"
)
IPV6_CANDIDATE_RE = re.compile(r"[0-9A-Fa-f:.]*::[0-9A-Fa-f:.]*")
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]*\d)")


class DenylistError(Exception):
    """The denylist was explicitly requested but cannot be read."""


def load_denylist() -> set[str]:
    """Load lowercase denylist terms.

    An explicit ``$LANSCAPE_DENYLIST`` must be readable (fail closed); a missing
    file at the default location simply means no denylist.
    """
    override = os.environ.get("LANSCAPE_DENYLIST")
    if override:
        try:
            text = Path(override).read_text(encoding="utf-8")
        except OSError as exc:
            raise DenylistError(f"cannot read LANSCAPE_DENYLIST={override}: {exc}") from exc
    else:
        default = Path.home() / ".config" / "lanscape-atlas" / "denylist.txt"
        try:
            text = default.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        except OSError as exc:
            raise DenylistError(f"cannot read {default}: {exc}") from exc
    terms = (line.strip().lower() for line in text.splitlines())
    return {t for t in terms if t and not t.startswith("#")}


def is_ip_exempt(path: str) -> bool:
    return any(fnmatch(path, glob) for glob in IP_ALLOWED_PATH_GLOBS)


def _ip_is_leak(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return any(ip in net for net in RFC1918_NETWORKS)


def _mask_ipv6(line: str) -> str:
    """Blank out valid IPv6 addresses so their tails are not read as MACs."""

    def repl(m: re.Match[str]) -> str:
        token = m.group()
        for candidate in (token, token.rstrip(":."), token.rstrip(":.") + ":"):
            try:
                ipaddress.IPv6Address(candidate.split("/")[0])
            except ValueError:
                continue
            return " " * len(token)
        return token

    return IPV6_CANDIDATE_RE.sub(repl, line)


def find_leaks(text: str, path: str, denylist: set[str]) -> list[str]:
    """Return ``path:line: reason`` strings for each leak found in ``text``."""
    check_ips = not is_ip_exempt(path)
    found: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in MAC_RE.finditer(_mask_ipv6(line)):
            digits = re.sub(r"[^0-9a-f]", "", m.group().lower())
            if digits not in PLACEHOLDER_MACS and not digits.startswith(ALLOWED_MAC_PREFIXES):
                found.append(f"{path}:{lineno}: MAC address outside documentation range")
        if check_ips:
            for m in IPV4_RE.finditer(line):
                if _ip_is_leak(m.group()):
                    found.append(f"{path}:{lineno}: RFC 1918 IPv4 address")
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
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue  # binary
        leaks.extend(find_leaks(data.decode("utf-8", errors="replace"), path, denylist))
    for leak in leaks:
        print(leak)
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
