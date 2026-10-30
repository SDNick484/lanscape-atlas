"""nmap collectors: nightly light scan and on-demand deep scan.

Collectors never write the DB. ``scan_targets`` and ``deep_scan`` only *read* it (the accepted
exception) to honour ``device.scan_policy``: devices marked ``never`` are never scanned.

Security: nmap XML is treated as untrusted. Real ``nmap -oX`` output always starts with a bare
``<!DOCTYPE nmaprun>``, so exactly that declaration is accepted; any ENTITY declaration, internal
subset, SYSTEM/PUBLIC identifier, other doctype name or repeated DOCTYPE is rejected before
parsing.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from lanscape_atlas.clock import Clock, utcnow
from lanscape_atlas.collectors.base import CollectorError
from lanscape_atlas.netutil import normalize_mac, valid_ipv4
from lanscape_atlas.observation import Observation
from lanscape_atlas.service.queries import DEFAULT_WINDOW_MIN, current_lease_sql, window_arg

SOURCE = "nmap"
DEEP_TIMEOUT_S = 1800.0
_STDERR_MAX = 300

Runner = Callable[[list[str], float], Awaitable[str]]


_DOCTYPE_RE = re.compile(r"<!doctype\s+nmaprun\s*>")


def _safe_doctype(xml: str) -> bool:
    """True when ``xml`` has no DOCTYPE or only nmap's bare ``<!DOCTYPE nmaprun>``."""
    lowered = xml.lower()
    if "<!entity" in lowered:
        return False
    count = lowered.count("<!doctype")
    if count == 0:
        return True
    if count > 1:
        return False
    start = lowered.index("<!doctype")
    return _DOCTYPE_RE.match(lowered, start) is not None


class ScanRefused(Exception):
    """A scan was refused by policy or because the device has no reachable address."""


def parse_nmap_xml(xml: str, seen_at: datetime) -> list[Observation]:
    """One Observation per ``<host>`` that is up and has a valid IPv4; open ports only."""
    if not _safe_doctype(xml):
        return []
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError):
        return []
    out: list[Observation] = []
    for host in root.iter("host"):
        status = host.find("status")
        if status is None or status.get("state") != "up":
            continue
        ip: str | None = None
        mac: str | None = None
        for addr in host.findall("address"):
            kind, value = addr.get("addrtype"), addr.get("addr") or ""
            if kind == "ipv4" and ip is None:
                ip = valid_ipv4(value)
            elif kind == "mac" and mac is None:
                try:
                    mac = normalize_mac(value)
                except ValueError:
                    mac = None
        if ip is None:
            continue
        services: list[dict[str, Any]] = []
        for port in host.findall("./ports/port"):
            state = port.find("state")
            proto = port.get("protocol")
            try:
                portid = int(port.get("portid", ""))
            except ValueError:
                continue
            if state is None or state.get("state") != "open" or proto not in ("tcp", "udp"):
                continue
            entry: dict[str, Any] = {"port": portid, "proto": proto}
            svc = port.find("service")
            if svc is not None:
                for key in ("name", "product", "version"):
                    if svc.get(key):
                        entry[key] = svc.get(key)
            services.append(entry)
        out.append(
            Observation(
                source=SOURCE,
                seen_at=seen_at,
                iface_kind=None,
                mac=mac,
                ipv4=ip,
                hostname=None,
                attrs={"services": services},
            )
        )
    return out


async def run_nmap(argv: list[str], timeout_s: float) -> str:
    """Run nmap (never via a shell) and return stdout; kills the child on timeout/cancel."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise CollectorError("nmap not found") from None
    except OSError as ex:
        raise CollectorError(f"nmap failed to start: {ex}"[:_STDERR_MAX]) from None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except BaseException as ex:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        if isinstance(ex, TimeoutError):
            raise CollectorError(f"nmap timed out after {timeout_s:g}s") from None
        raise
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace").strip()[:_STDERR_MAX]
        raise CollectorError(f"nmap exited {proc.returncode}: {err}")
    return stdout.decode("utf-8", "replace")


# IPs with an open lease on any never-scan device (a shared IP must never be scanned).
_NEVER_IPS = (
    "SELECT l2.ipv4 FROM ip_lease l2 JOIN interface i2 ON i2.id = l2.interface_id"
    " JOIN device d2 ON d2.id = i2.device_id"
    " WHERE l2.closed = 0 AND d2.scan_policy = 'never'"
)


def scan_targets(conn: sqlite3.Connection, window_min: int = DEFAULT_WINDOW_MIN) -> list[str]:
    """Sorted, unique IPv4s of current leases of online devices with ``scan_policy='normal'``.

    A lease is current per ``current_lease_sql`` (Ruling 44). The never-scan exclusion
    stays conservative: any *open* lease of a never-scan device blocks that IP.
    """
    rows = conn.execute(
        "SELECT DISTINCT l.ipv4 FROM ip_lease l"
        " JOIN interface i ON i.id = l.interface_id"
        " JOIN device d ON d.id = i.device_id"
        f" WHERE {current_lease_sql()} AND d.online = 1 AND d.scan_policy = 'normal'"
        " AND l.ipv4 NOT IN (" + _NEVER_IPS + ")",
        (window_arg(window_min),),
    ).fetchall()
    return sorted({ip for (raw,) in rows if (ip := valid_ipv4(raw)) is not None})


class NmapLightCollector:
    name = "nmap_light"

    def __init__(
        self,
        conn_reader: Callable[[], list[str]],
        timeout_s: float = 1800,
        runner: Runner = run_nmap,
    ) -> None:
        self._targets = conn_reader
        self.timeout_s = timeout_s
        self._runner = runner

    async def collect(self) -> list[Observation]:
        targets = self._targets()
        if not targets:
            return []
        argv = ["nmap", "-oX", "-", "-T3", "--top-ports", "100", "-Pn", *targets]
        return parse_nmap_xml(await self._runner(argv, self.timeout_s), utcnow())


def check_deep_scan(
    conn: sqlite3.Connection, device_id: int, window_min: int = DEFAULT_WINDOW_MIN
) -> str:
    """Return the IPv4 a deep scan of ``device_id`` would target, or refuse.

    Raises ``KeyError`` for an unknown device and ``ScanRefused`` when policy forbids
    the scan or the device has no usable current IPv4. Reads only; the caller holds
    ``Service.lock`` when the connection is shared.
    """
    row = conn.execute("SELECT scan_policy FROM device WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        raise KeyError(f"no device with id {device_id}")
    if row[0] == "never":
        raise ScanRefused("scan_policy=never")
    lease = conn.execute(
        "SELECT l.ipv4 FROM ip_lease l JOIN interface i ON i.id = l.interface_id"
        f" WHERE i.device_id = ? AND {current_lease_sql()}"
        " ORDER BY l.last_seen DESC, l.id DESC LIMIT 1",
        (device_id, window_arg(window_min)),
    ).fetchone()
    ip = valid_ipv4(lease[0]) if lease is not None else None
    if ip is None:
        raise ScanRefused("no current IPv4")
    if conn.execute("SELECT 1 FROM (" + _NEVER_IPS + ") WHERE ipv4 = ?", (ip,)).fetchone():
        raise ScanRefused("ip shared with a never-scan device")
    return ip


async def scan_ip(
    ip: str, *, runner: Runner = run_nmap, clock: Clock = utcnow
) -> list[Observation]:
    """Full-port service scan of one already-validated IPv4 (see ``check_deep_scan``)."""
    argv = ["nmap", "-oX", "-", "-sV", "-p-", "-T3", "-Pn", ip]
    return parse_nmap_xml(await runner(argv, DEEP_TIMEOUT_S), clock())


async def deep_scan(
    conn: sqlite3.Connection,
    device_id: int,
    *,
    runner: Runner = run_nmap,
    clock: Clock = utcnow,
) -> list[Observation]:
    """Full-port service scan of one device's current IPv4, unless policy forbids it."""
    return await scan_ip(check_deep_scan(conn, device_id), runner=runner, clock=clock)
