"""ASUS router (AsusWRT / AiMesh) collector built on the ``asusrouter`` library.

Field mapping (verified against asusrouter 1.21.3, ``AsusData.CLIENTS`` -> ``{mac: AsusClient}``;
convert each with ``dataclasses.asdict``, so nested dataclasses become nested dicts):

* ``description.mac``        -> ``mac`` (normalized; invalid/missing -> record skipped)
* ``description.name``       -> ``hostname`` (raw)
* ``connection.ip_address``  -> ``ipv4`` (invalid -> None)
* ``connection.type``        -> ``iface_kind`` / ``attrs.band`` (ConnectionType values:
  ``wired``, ``2ghz``, ``5ghz``, ``5ghz2``, ``6ghz``, ``disconnected``)
* ``connection.node``        -> ``attrs.ap_node``: the library takes it from the key of the router's
  ``get_allclientlist`` (the AiMesh node's MAC), so no separate AiMesh query is needed.
* ``connection.rssi``        -> ``attrs.rssi`` (wireless only)
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import aiohttp
from asusrouter import AsusRouter
from asusrouter.modules.connection import ConnectionState
from asusrouter.modules.data import AsusData

from lanscape_atlas.clock import utcnow
from lanscape_atlas.collectors.base import CollectorError
from lanscape_atlas.config import RouterSettings
from lanscape_atlas.netutil import normalize_mac, valid_ipv4
from lanscape_atlas.observation import Observation

SOURCE = "asusrouter"

_BANDS = {"2ghz": "2.4", "5ghz": "5", "5ghz2": "5", "6ghz": "6"}


def _section(d: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    v = d.get(key)
    return v if isinstance(v, Mapping) else {}


def _norm_mac(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        return normalize_mac(raw)
    except ValueError:
        return None


def _rssi(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def client_to_observation(client: Mapping[str, Any], seen_at: datetime) -> Observation | None:
    """Map one ``dataclasses.asdict(AsusClient)`` record to an Observation (None if no MAC)."""
    desc = _section(client, "description")
    conn = _section(client, "connection")
    mac = _norm_mac(desc.get("mac"))
    if mac is None:
        return None

    ctype = conn.get("type")
    ctype = getattr(ctype, "value", ctype)  # Enum (asdict keeps it) or plain string
    ip = conn.get("ip_address")
    name = desc.get("name")

    iface_kind: str | None
    if ctype == "wired":
        iface_kind = "ethernet"
    elif ctype in _BANDS:
        iface_kind = "wifi"
    else:
        iface_kind = None

    return Observation(
        source=SOURCE,
        seen_at=seen_at,
        iface_kind=iface_kind,
        mac=mac,
        ipv4=valid_ipv4(ip) if isinstance(ip, str) else None,
        hostname=name if isinstance(name, str) and name else None,
        attrs={
            "ap_node": _norm_mac(conn.get("node")),
            "band": _BANDS.get(ctype) if isinstance(ctype, str) else None,
            "rssi": _rssi(conn.get("rssi")) if iface_kind == "wifi" else None,
        },
    )


def _new_session(timeout_s: float) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s))


class AsusRouterCollector:
    name = SOURCE

    def __init__(self, settings: RouterSettings, timeout_s: float = 30) -> None:
        self._settings = settings
        self.timeout_s = timeout_s

    def _redact(self, text: str) -> str:
        pw = self._settings.password
        return text.replace(pw, "***") if pw else text

    async def collect(self) -> list[Observation]:
        s = self._settings
        session = _new_session(self.timeout_s)
        router = AsusRouter(
            hostname=s.host,
            username=s.username,
            password=s.password,
            use_ssl=s.use_ssl,
            session=session,
        )
        try:
            if not await router.async_connect():
                raise CollectorError(f"asusrouter: could not connect to {s.host}")
            data = await router.async_get_data(AsusData.CLIENTS)
            now = utcnow()
            out: list[Observation] = []
            for client in data.values():
                if getattr(client, "state", None) != ConnectionState.CONNECTED:
                    continue  # router also lists known-but-offline clients
                obs = client_to_observation(dataclasses.asdict(client), now)
                if obs is not None:
                    out.append(obs)
            return out
        except CollectorError:
            raise
        except Exception as ex:
            msg = self._redact(str(ex))
            raise CollectorError(f"asusrouter: {type(ex).__name__}: {msg}"[:300]) from None
        finally:
            try:
                await router.async_disconnect()
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass
            await session.close()
