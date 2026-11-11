"""mDNS / DNS-SD discovery collector.

A long-lived ``AsyncServiceBrowser`` listens for a curated list of service types. Each resolved
service is buffered (latest record per ``(name, type)``, capped) and drained by ``collect()``.
Records carry no MAC, so observations are IP-only. IPv6-only records are ignored in Phase 1.

One device usually advertises several services under different instance names. So that its
name does not follow whichever record resolved last, the collector keeps the set of live
records (resolved, not yet removed) and, at each flush, gives every observation for an IPv4
the same hostname: the best cleaned name among that address's live records, by service type
(``TYPE_RANK``) then alphabetically. The raw instance name is kept in ``attrs["instance"]``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from lanscape_atlas.clock import Clock, utcnow
from lanscape_atlas.ingest.naming import clean_name, is_junk_hostname
from lanscape_atlas.netutil import valid_ipv4
from lanscape_atlas.observation import Observation

log = logging.getLogger(__name__)

SOURCE = "mdns"
MAX_HOSTNAME = 255
MAX_BUFFER = 2000
RESOLVE_TIMEOUT_MS = 3000  # zeroconf takes milliseconds

MDNS_SERVICE_TYPES: tuple[str, ...] = (
    "_http._tcp",
    "_https._tcp",
    "_ssh._tcp",
    "_smb._tcp",
    "_afpovertcp._tcp",
    "_nfs._tcp",
    "_workstation._tcp",
    "_device-info._tcp",
    "_googlecast._tcp",
    "_airplay._tcp",
    "_raop._tcp",
    "_spotify-connect._tcp",
    "_sonos._tcp",
    "_homekit._tcp",
    "_hap._tcp",
    "_matter._tcp",
    "_matterc._udp",
    "_esphomelib._tcp",
    "_home-assistant._tcp",
    "_hue._tcp",
    "_ipp._tcp",
    "_ipps._tcp",
    "_printer._tcp",
    "_pdl-datastream._tcp",
    "_scanner._tcp",
    "_uscan._tcp",
    "_androidtvremote2._tcp",
    "_nvstream._tcp",
    "_companion-link._tcp",
    "_sleep-proxy._udp",
)

# Whose instance name makes the best device name, best first. Unlisted types rank
# after the listed tiers but ahead of the MAC-prefixed and hex-ID ones at the end.
_RANK_TIERS: tuple[tuple[str, ...], ...] = (
    ("device-info",),
    ("airplay",),
    ("companion-link",),
    ("hap", "homekit"),
    ("esphomelib",),
    ("home-assistant",),
    ("googlecast", "androidtvremote2"),
    ("spotify-connect",),
    ("sonos",),
    ("smb", "afpovertcp", "nfs"),
    ("workstation",),
    ("ssh",),
    ("http", "https"),
    ("ipp", "ipps", "printer", "pdl-datastream", "scanner", "uscan"),
)
_LOW_TIERS: tuple[tuple[str, ...], ...] = (("raop", "sleep-proxy"), ("matter", "matterc"))
_UNLISTED_RANK = len(_RANK_TIERS)
TYPE_RANK: dict[str, int] = {
    t: i if i < _UNLISTED_RANK else i + 1
    for i, tier in enumerate(_RANK_TIERS + _LOW_TIERS)
    for t in tier
}


def canonical_name(records: Iterable[tuple[str, str]], ipv4: str) -> str | None:
    """Best cleaned, non-junk name among ``(service type, instance name)`` pairs, or None."""
    best: tuple[int, str, str] | None = None
    for type_, instance in records:
        name = clean_name(SOURCE, instance)
        if is_junk_hostname(name, mac=None, ipv4=ipv4):
            continue
        split = _split_type(type_)
        rank = TYPE_RANK.get(split[0], _UNLISTED_RANK) if split else _UNLISTED_RANK
        key = (rank, name.casefold(), name)
        if best is None or key < best:
            best = key
    return None if best is None else best[2]


def _split_type(type_: str) -> tuple[str, str] | None:
    """``_googlecast._tcp[.local.]`` -> ``("googlecast", "tcp")``; None if unrecognised."""
    parts = type_.strip().rstrip(".").split(".")
    if len(parts) >= 3 and parts[-1] == "local":
        parts = parts[:-1]
    if len(parts) < 2:
        return None
    proto = parts[-1].lstrip("_").lower()
    if proto not in ("tcp", "udp"):
        return None
    return parts[-2].lstrip("_"), proto


def record_to_observation(rec: Mapping[str, Any], seen_at: datetime) -> Observation | None:
    """Convert a resolved service record to an Observation; None if it has no IPv4 address."""
    ip = next((v for a in rec.get("addresses") or [] if (v := valid_ipv4(str(a)))), None)
    if ip is None:
        return None
    services: list[dict[str, Any]] = []
    port = rec.get("port")
    split = _split_type(str(rec.get("type") or ""))
    if isinstance(port, int) and port > 0 and split is not None:
        services.append({"port": port, "proto": split[1], "name": split[0]})
    name = rec.get("name")
    return Observation(
        source=SOURCE,
        seen_at=seen_at,
        iface_kind=None,
        mac=None,
        ipv4=ip,
        hostname=str(name)[:MAX_HOSTNAME] if name else None,
        attrs={"services": services, "server": rec.get("server") or None},
    )


class MdnsCollector:
    name = SOURCE

    def __init__(self, flush_s: float = 60, clock: Clock = utcnow, timeout_s: float = 5) -> None:
        self.flush_s = flush_s
        self.timeout_s = timeout_s
        self._clock = clock
        self._buffer: OrderedDict[tuple[str, str], Observation] = OrderedDict()
        # (name, type) -> IPv4 of every record resolved and not yet removed.
        self._live: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        azc = AsyncZeroconf()
        try:
            browser = AsyncServiceBrowser(
                azc.zeroconf,
                [t + ".local." for t in MDNS_SERVICE_TYPES],
                handlers=[self._handler],
            )
        except BaseException:
            # Close the sockets now; otherwise every scheduler retry leaks one instance.
            await azc.async_close()
            raise
        self._azc, self._browser = azc, browser
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        browser, azc = self._browser, self._azc
        self._browser = self._azc = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if browser is not None:
            await browser.async_cancel()
        if azc is not None:
            await azc.async_close()

    def _handler(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is ServiceStateChange.Removed:
            self._live.pop((name, service_type), None)
            return
        task = asyncio.ensure_future(self._resolve(zeroconf, service_type, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, zeroconf: Any, service_type: str, name: str) -> None:
        try:
            info = AsyncServiceInfo(service_type, name)
            if not await info.async_request(zeroconf, RESOLVE_TIMEOUT_MS):
                return
            self._on_record(
                {
                    "name": name,
                    "type": service_type,
                    "addresses": info.parsed_addresses(IPVersion.V4Only),
                    "port": info.port,
                    "server": info.server,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed resolve just skips this record
            log.debug("mDNS resolve failed for %s", name, exc_info=True)

    def _on_record(self, rec: Mapping[str, Any]) -> None:
        obs = record_to_observation(rec, self._clock())
        if obs is None or (ip := obs.ipv4) is None:
            return
        key = (str(rec.get("name")), str(rec.get("type")))
        self._buffer.pop(key, None)
        self._buffer[key] = obs
        while len(self._buffer) > MAX_BUFFER:
            self._buffer.popitem(last=False)
        self._live.pop(key, None)
        self._live[key] = ip
        while len(self._live) > MAX_BUFFER:
            self._live.popitem(last=False)

    async def collect(self) -> list[Observation]:
        if not self._started:
            return []
        by_ip: dict[str, list[tuple[str, str]]] = {}
        for (name, type_), ip in self._live.items():
            by_ip.setdefault(ip, []).append((type_, name[:MAX_HOSTNAME]))
        names: dict[str, str | None] = {}
        out = []
        for obs in self._buffer.values():
            assert obs.ipv4 is not None  # _on_record buffers only IPv4 observations
            if obs.ipv4 not in names:
                names[obs.ipv4] = canonical_name(by_ip.get(obs.ipv4, ()), obs.ipv4)
            attrs = {**obs.attrs, "instance": obs.hostname}
            out.append(replace(obs, hostname=names[obs.ipv4], attrs=attrs))
        self._buffer.clear()
        return out
