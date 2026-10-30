from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lanscape_atlas.collectors import asusrouter as mod
from lanscape_atlas.collectors.asusrouter import AsusRouterCollector, client_to_observation
from lanscape_atlas.collectors.base import CollectorError
from lanscape_atlas.config import RouterSettings

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
FIX = json.loads((Path(__file__).parent / "fixtures" / "asus_clients.json").read_text())


def test_client_to_observation_wifi() -> None:
    o = client_to_observation(FIX["wifi_5g"], NOW)
    assert o is not None
    assert (o.source, o.iface_kind, o.mac, o.ipv4, o.hostname) == (
        "asusrouter",
        "wifi",
        "02:00:5e:00:53:10",
        "192.0.2.50",
        "Pixel-Phone",
    )
    assert o.seen_at == NOW
    assert o.attrs == {"ap_node": "00:00:5e:00:53:02", "band": "5", "rssi": -61}


def test_client_to_observation_wired() -> None:
    o = client_to_observation(FIX["wired"], NOW)
    assert o is not None
    assert o.iface_kind == "ethernet" and o.mac == "00:00:5e:00:53:12"
    assert o.attrs == {"ap_node": "00:00:5e:00:53:01", "band": None, "rssi": None}


def test_wifi_without_node_and_2g_band() -> None:
    o = client_to_observation(FIX["wifi_2g_no_node"], NOW)
    assert o is not None and o.attrs == {"ap_node": None, "band": "2.4", "rssi": -72}


def test_bad_ip_and_node_keep_mac() -> None:
    o = client_to_observation(FIX["bad_ip"], NOW)
    assert o is not None and o.ipv4 is None and o.mac == "00:00:5e:00:53:13"
    assert o.attrs == {"ap_node": None, "band": "6", "rssi": -50}


@pytest.mark.parametrize("key", ["no_mac", "bad_mac"])
def test_client_without_mac_skipped(key: str) -> None:
    assert client_to_observation(FIX[key], NOW) is None


def test_missing_sections_tolerated() -> None:
    o = client_to_observation({"description": {"mac": "00:00:5e:00:53:20"}}, NOW)
    assert o is not None and o.iface_kind is None and o.ipv4 is None and o.hostname is None


# --- collect() with a fake library ---------------------------------------------------------

SETTINGS = RouterSettings(host="192.0.2.1", username="admin", password="s3cret-pw")


class FakeSession:
    closed = False

    async def close(self) -> None:
        self.closed = True


def install_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: Exception | None = None,
    clients: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"disconnected": False, "session": None}

    class FakeRouter:
        def __init__(self, hostname: str, username: str, password: str, **kw: Any) -> None:
            state["args"] = (hostname, username, password, kw)
            state["session"] = kw.get("session")

        async def async_connect(self) -> bool:
            if fail:
                raise fail
            return True

        async def async_get_data(self, datatype: Any, **kw: Any) -> dict[str, Any]:
            return clients or {}

        async def async_disconnect(self) -> bool:
            state["disconnected"] = True
            return True

    monkeypatch.setattr(mod, "AsusRouter", FakeRouter)
    monkeypatch.setattr(mod, "_new_session", lambda timeout_s: _mk(state))
    return state


def _mk(state: dict[str, Any]) -> FakeSession:
    s = FakeSession()
    state["fs"] = s
    return s


def to_clients() -> dict[str, Any]:
    from asusrouter.modules.client import (
        AsusClient,
        AsusClientConnectionWlan,
        AsusClientDescription,
    )
    from asusrouter.modules.connection import ConnectionState, ConnectionType

    good = AsusClient(
        state=ConnectionState.CONNECTED,
        description=AsusClientDescription(name="Pixel-Phone", mac="02:00:5E:00:53:10"),
        connection=AsusClientConnectionWlan(
            type=ConnectionType.WLAN_5G,
            ip_address="192.0.2.50",
            node="00:00:5E:00:53:02",
            online=True,
            rssi=-61,
        ),
    )
    off = AsusClient(
        state=ConnectionState.DISCONNECTED,
        description=AsusClientDescription(name="old", mac="00:00:5e:00:53:30"),
    )
    return {"02:00:5E:00:53:10": good, "00:00:5e:00:53:30": off}


async def test_collect_maps_real_dataclasses_and_skips_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    st = install_fake(monkeypatch, clients=to_clients())
    c = AsusRouterCollector(SETTINGS, timeout_s=5)
    obs = await c.collect()
    assert len(obs) == 1
    assert obs[0].mac == "02:00:5e:00:53:10" and obs[0].attrs["band"] == "5"
    assert obs[0].attrs["ap_node"] == "00:00:5e:00:53:02"
    assert st["disconnected"] and st["fs"].closed
    assert st["args"][3]["use_ssl"] is True and c.name == "asusrouter" and c.timeout_s == 5


async def test_collect_failure_wrapped_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    st = install_fake(monkeypatch, fail=RuntimeError("login failed for pw s3cret-pw"))
    with pytest.raises(CollectorError) as ei:
        await AsusRouterCollector(SETTINGS).collect()
    assert "s3cret-pw" not in str(ei.value)
    assert ei.value.__cause__ is None or "s3cret-pw" not in repr(ei.value)
    assert st["disconnected"] and st["fs"].closed


async def test_connect_false_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    st = install_fake(monkeypatch)

    async def no(self: Any) -> bool:
        return False

    monkeypatch.setattr(mod.AsusRouter, "async_connect", no)
    with pytest.raises(CollectorError):
        await AsusRouterCollector(SETTINGS).collect()
    assert st["fs"].closed
