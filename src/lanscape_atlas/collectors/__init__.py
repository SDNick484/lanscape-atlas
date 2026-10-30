"""Collector plugins: return Observations, never touch the database.

``build_collectors`` instantiates the enabled collectors from ``Settings``.
``REGISTRY`` maps a collector name to its builder so tests can swap builders.
Builders import their collector module lazily, so importing this package does
not pull in aiohttp/asusrouter/zeroconf.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from lanscape_atlas.collectors.base import Collector, CollectorError
from lanscape_atlas.config import CollectorSettings, Settings

if TYPE_CHECKING:
    from lanscape_atlas.service import Service

Builder = Callable[[Settings, "Service", CollectorSettings], Collector | None]


def _asusrouter(settings: Settings, service: Service, cs: CollectorSettings) -> Collector | None:
    if settings.router is None:
        return None
    from lanscape_atlas.collectors.asusrouter import AsusRouterCollector

    return AsusRouterCollector(settings.router, timeout_s=cs.timeout_s)


def _arp(settings: Settings, service: Service, cs: CollectorSettings) -> Collector | None:
    from lanscape_atlas.collectors.arp import ArpCollector

    return ArpCollector(settings.scan_interface, timeout_s=cs.timeout_s)


def _mdns(settings: Settings, service: Service, cs: CollectorSettings) -> Collector | None:
    from lanscape_atlas.collectors.mdns import MdnsCollector

    return MdnsCollector(flush_s=cs.interval_s or 60, timeout_s=cs.timeout_s)


def _ssdp(settings: Settings, service: Service, cs: CollectorSettings) -> Collector | None:
    from lanscape_atlas.collectors.ssdp import SsdpCollector

    return SsdpCollector(timeout_s=cs.timeout_s)


def _nmap_light(settings: Settings, service: Service, cs: CollectorSettings) -> Collector | None:
    from lanscape_atlas.collectors.nmap import NmapLightCollector, scan_targets

    def reader() -> list[str]:
        # The connection is shared; every DB access goes through Service.lock.
        with service.lock:
            return scan_targets(service.conn, service.ip_match_window_min)

    return NmapLightCollector(reader, timeout_s=cs.timeout_s)


REGISTRY: dict[str, Builder] = {
    "asusrouter": _asusrouter,
    "arp": _arp,
    "mdns": _mdns,
    "ssdp": _ssdp,
    "nmap_light": _nmap_light,
}


def build_collectors(settings: Settings, service: Service) -> list[Collector]:
    """Instantiate enabled collectors in ``REGISTRY`` order (asusrouter needs ``[router]``)."""
    out: list[Collector] = []
    for name, builder in REGISTRY.items():
        cs = settings.collectors.get(name)
        if cs is None or not cs.enabled:
            continue
        c = builder(settings, service, cs)
        if c is not None:
            out.append(c)
    return out


__all__ = ["REGISTRY", "Builder", "Collector", "CollectorError", "build_collectors"]
