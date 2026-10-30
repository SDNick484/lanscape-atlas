"""SSDP / UPnP discovery collector.

Multicasts an ``M-SEARCH ssdp:all`` and collects unicast replies. Replies carry no MAC, so
observations are IP-only and the ingestor attaches them to an interface by lease. The optional
LOCATION document is fetched once per unseen ``(addr, location)`` for friendlyName / model.

Security: device XML is untrusted. Stdlib ``xml.etree`` (expat) never fetches external entities
or DTDs, but it can still expand internal entities ("billion laughs"), so documents over 64 KiB
and any document declaring a DOCTYPE/ENTITY are rejected before parsing. LOCATION fetches are
restricted to http(s) URLs whose host equals the replying device's IP, without redirects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp

from lanscape_atlas.clock import utcnow
from lanscape_atlas.collectors.base import CollectorError
from lanscape_atlas.netutil import valid_ipv4
from lanscape_atlas.observation import Observation

log = logging.getLogger(__name__)

SOURCE = "ssdp"
MCAST = ("239.255.255.250", 1900)
MAX_XML_BYTES = 64 * 1024
FETCH_TIMEOUT_S = 2.0
_WANTED = ("friendlyName", "modelName", "manufacturer")
# Header values and description fields are untrusted; cap them before they reach an
# Observation (and the DB). A LOCATION longer than this is stored truncated, never fetched.
MAX_FIELD_LEN = 255

_M_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
).encode("ascii")


def parse_ssdp_response(data: bytes, addr: str, seen_at: datetime) -> Observation | None:
    """Parse one SSDP reply datagram; None if not a valid reply or ``addr`` is not IPv4."""
    ip = valid_ipv4(addr)
    if ip is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines or not lines[0].upper().startswith("HTTP/1."):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers.setdefault(key.strip().lower(), value.strip())
    if not any(k in headers for k in ("st", "usn", "location")):
        return None
    attrs: dict[str, object] = {"name_source": SOURCE}
    for key, header in (("server", "server"), ("location", "location"), ("st", "st")):
        if headers.get(header):
            attrs[key] = headers[header][:MAX_FIELD_LEN]
    return Observation(
        source=SOURCE,
        seen_at=seen_at,
        iface_kind=None,
        mac=None,
        ipv4=ip,
        hostname=None,
        attrs=attrs,
    )


def parse_device_description(xml: str) -> dict[str, str]:
    """Extract friendlyName / modelName / manufacturer from a UPnP device description."""
    if len(xml.encode("utf-8", "replace")) > MAX_XML_BYTES:
        return {}
    lowered = xml.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        return {}
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError):
        return {}
    found: dict[str, str] = {}
    for el in root.iter():
        tag = el.tag.rpartition("}")[2] if isinstance(el.tag, str) else ""
        if tag in _WANTED and tag not in found:
            value = (el.text or "").strip()[:MAX_FIELD_LEN]
            if value:
                found[tag] = value
    return found


def location_allowed(location: str, addr: str) -> bool:
    """True only for http(s) URLs whose host is exactly the replying device's IPv4."""
    try:
        parts = urlsplit(location)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and host is not None and host == valid_ipv4(addr)


async def _fetch_description(location: str) -> dict[str, str]:
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(location, allow_redirects=False) as resp,
        ):
            if resp.status != 200:
                return {}
            body = await resp.content.read(MAX_XML_BYTES + 1)
        return parse_device_description(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - failures are ignored by design
        return {}


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: list[tuple[bytes, str]] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.replies.append((data, addr[0]))


class SsdpCollector:
    name = SOURCE

    def __init__(self, timeout_s: float = 5) -> None:
        self.timeout_s = timeout_s

    async def _search(self) -> list[tuple[bytes, str]]:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        sock.bind(("", 0))
        transport, proto = await loop.create_datagram_endpoint(_Protocol, sock=sock)
        try:
            transport.sendto(_M_SEARCH, MCAST)
            await asyncio.sleep(self.timeout_s)
        finally:
            transport.close()
        return proto.replies

    async def collect(self) -> list[Observation]:
        now = utcnow()
        try:
            replies = await self._search()
        except OSError as ex:
            raise CollectorError(f"ssdp: {type(ex).__name__}: {ex}"[:300]) from None
        seen: set[tuple[str, str]] = set()
        unique: list[Observation] = []
        for data, addr in replies:
            obs = parse_ssdp_response(data, addr, now)
            if obs is None:
                continue
            key = (addr, str(obs.attrs.get("location", "")))
            if key in seen:
                continue
            seen.add(key)
            unique.append(obs)
        fetch: dict[int, asyncio.Task[dict[str, str]]] = {}
        for i, obs in enumerate(unique):
            loc = obs.attrs.get("location")
            if (
                isinstance(loc, str)
                and len(loc) < MAX_FIELD_LEN  # possibly truncated: would fetch another URL
                and obs.ipv4
                and location_allowed(loc, obs.ipv4)
            ):
                fetch[i] = asyncio.ensure_future(_fetch_description(loc))
        out: list[Observation] = []
        for i, obs in enumerate(unique):
            info: dict[str, str] = {}
            if i in fetch:
                with contextlib.suppress(Exception):
                    info = await fetch[i]
            attrs = dict(obs.attrs)
            for src, dst in (("modelName", "model"), ("manufacturer", "manufacturer")):
                if src in info:
                    attrs[dst] = info[src]
            out.append(
                Observation(
                    source=obs.source,
                    seen_at=obs.seen_at,
                    iface_kind=None,
                    mac=None,
                    ipv4=obs.ipv4,
                    hostname=info.get("friendlyName"),
                    attrs=attrs,
                )
            )
        return out
