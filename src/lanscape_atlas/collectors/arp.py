"""ARP sweep collector built on the ``arp-scan`` binary.

Runs ``arp-scan --interface=<if> --localnet --plain --quiet`` (needs CAP_NET_RAW) and parses
``<ip>\\t<mac>[\\t<vendor>...]`` lines. Header/footer text and malformed lines are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

from lanscape_atlas.clock import utcnow
from lanscape_atlas.collectors.base import CollectorError
from lanscape_atlas.netutil import normalize_mac, valid_ipv4
from lanscape_atlas.observation import Observation

SOURCE = "arp"
_STDERR_MAX = 300


def parse_arp_scan(text: str, seen_at: datetime) -> list[Observation]:
    """Parse ``arp-scan --plain`` output; one Observation per distinct (ip, mac)."""
    out: list[Observation] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ip = valid_ipv4(parts[0])
        if ip is None:
            continue
        try:
            mac = normalize_mac(parts[1])
        except ValueError:
            continue
        if (ip, mac) in seen:
            continue
        seen.add((ip, mac))
        out.append(
            Observation(
                source=SOURCE,
                seen_at=seen_at,
                iface_kind=None,
                mac=mac,
                ipv4=ip,
                hostname=None,
                attrs={},
            )
        )
    return out


class ArpCollector:
    name = SOURCE

    def __init__(self, interface: str, timeout_s: float = 60) -> None:
        self._interface = interface
        self.timeout_s = timeout_s

    async def collect(self) -> list[Observation]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "arp-scan",
                f"--interface={self._interface}",
                "--localnet",
                "--plain",
                "--quiet",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise CollectorError("arp-scan not found") from None
        except OSError as ex:
            raise CollectorError(f"arp-scan failed to start: {ex}"[:_STDERR_MAX]) from None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except BaseException as ex:
            # Timeout, cancellation (e.g. the runner's outer timeout) or anything else:
            # never leave arp-scan running.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            if isinstance(ex, TimeoutError):
                raise CollectorError(f"arp-scan timed out after {self.timeout_s:g}s") from None
            raise
        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip()[:_STDERR_MAX]
            raise CollectorError(f"arp-scan exited {proc.returncode}: {err}")
        return parse_arp_scan(stdout.decode("utf-8", "replace"), utcnow())
