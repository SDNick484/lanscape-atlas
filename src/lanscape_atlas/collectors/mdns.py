"""mDNS / DNS-SD discovery collector.

A long-lived ``AsyncServiceBrowser`` listens for a curated list of service types. Each resolved
service is buffered (latest record per ``(name, type)``, capped) and drained by ``collect()``.
Records carry no MAC, so observations are IP-only; the instance name is the hostname and is
cleaned by the ingestor. IPv6-only records are ignored in Phase 1.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from lanscape_atlas.clock import Clock, utcnow
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
        if obs is None:
            return
        key = (str(rec.get("name")), str(rec.get("type")))
        self._buffer.pop(key, None)
        self._buffer[key] = obs
        while len(self._buffer) > MAX_BUFFER:
            self._buffer.popitem(last=False)

    async def collect(self) -> list[Observation]:
        if not self._started:
            return []
        out = list(self._buffer.values())
        self._buffer.clear()
        return out
