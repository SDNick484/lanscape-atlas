from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from zeroconf import ServiceStateChange

from conftest import T0
from lanscape_atlas.collectors import mdns
from lanscape_atlas.collectors.mdns import (
    MDNS_SERVICE_TYPES,
    MdnsCollector,
    record_to_observation,
)
from lanscape_atlas.observation import Observation

MAC = "00:00:5e:00:53:01"
RECS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "mdns_records.json").read_text()
)


def test_service_types() -> None:
    assert len(MDNS_SERVICE_TYPES) == 30
    assert all(t.startswith("_") and t.endswith(("._tcp", "._udp")) for t in MDNS_SERVICE_TYPES)


def test_record_to_observation() -> None:
    o = record_to_observation(RECS[0], T0)
    assert o is not None
    assert (o.source, o.mac, o.iface_kind, o.ipv4) == ("mdns", None, None, "192.0.2.30")
    assert o.hostname == "Living-Room-TV._googlecast._tcp.local."
    assert o.attrs == {
        "services": [{"port": 8009, "proto": "tcp", "name": "googlecast"}],
        "server": "tv-1234.local.",
    }


def test_record_udp_and_no_port() -> None:
    udp = record_to_observation(RECS[2], T0)
    assert udp is not None
    assert udp.attrs["services"] == [{"port": 5540, "proto": "udp", "name": "matterc"}]
    noport = record_to_observation(RECS[1], T0)
    assert noport is not None
    assert noport.attrs == {"services": [], "server": None}


def test_record_ipv6_only_returns_none() -> None:
    assert record_to_observation(RECS[3], T0) is None


def test_hostname_truncated() -> None:
    o = record_to_observation({**RECS[0], "name": "x" * 400}, T0)
    assert o is not None and o.hostname is not None and len(o.hostname) == 255


async def test_collect_drains_buffer() -> None:
    c = MdnsCollector(clock=lambda: T0)
    c._on_record(RECS[0])
    c._on_record(RECS[3])  # ignored
    assert await c.collect() == []  # not started
    c._started = True
    obs = await c.collect()
    assert [o.ipv4 for o in obs] == ["192.0.2.30"]
    assert obs[0].seen_at == T0
    assert await c.collect() == []


async def test_collect_before_start_keeps_nothing_started() -> None:
    assert await MdnsCollector().collect() == []


async def test_dedupe_and_cap() -> None:
    c = MdnsCollector(clock=lambda: T0)
    c._started = True
    c._on_record(RECS[0])
    c._on_record({**RECS[0], "addresses": ["192.0.2.99"]})
    obs = await c.collect()
    assert [o.ipv4 for o in obs] == ["192.0.2.99"]
    for i in range(2100):
        c._on_record({**RECS[0], "name": f"n{i}", "addresses": ["192.0.2.5"]})
    obs = await c.collect()
    assert len(obs) == 2000
    assert obs[0].attrs["instance"] == "n100" and obs[-1].attrs["instance"] == "n2099"


class FakeAZ:
    instances: list[FakeAZ] = []

    def __init__(self) -> None:
        self.closed = 0
        self.zeroconf = object()
        FakeAZ.instances.append(self)

    async def async_close(self) -> None:
        self.closed += 1


class FakeBrowser:
    instances: list[FakeBrowser] = []

    def __init__(self, zc: object, types: list[str], handlers: list[Any]) -> None:
        self.types, self.cancelled = types, 0
        FakeBrowser.instances.append(self)

    async def async_cancel(self) -> None:
        self.cancelled += 1


async def test_start_stop_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAZ.instances.clear()
    FakeBrowser.instances.clear()
    monkeypatch.setattr(mdns, "AsyncZeroconf", FakeAZ)
    monkeypatch.setattr(mdns, "AsyncServiceBrowser", FakeBrowser)
    c = MdnsCollector()
    await c.stop()  # before start: no-op
    await c.start()
    await c.start()
    assert len(FakeAZ.instances) == 1 and len(FakeBrowser.instances) == 1
    assert FakeBrowser.instances[0].types == [t + ".local." for t in MDNS_SERVICE_TYPES]
    await c.stop()
    await c.stop()
    assert FakeAZ.instances[0].closed == 1 and FakeBrowser.instances[0].cancelled == 1
    assert await c.collect() == []


async def test_start_closes_zeroconf_when_browser_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruling 46/M1: a failed browser start must not leak the AsyncZeroconf on retry."""
    FakeAZ.instances.clear()
    FakeBrowser.instances.clear()
    attempts = {"n": 0}

    def flaky_browser(zc: object, types: list[str], handlers: list[Any]) -> FakeBrowser:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("no multicast")
        return FakeBrowser(zc, types, handlers)

    monkeypatch.setattr(mdns, "AsyncZeroconf", FakeAZ)
    monkeypatch.setattr(mdns, "AsyncServiceBrowser", flaky_browser)
    c = MdnsCollector()
    with pytest.raises(OSError, match="no multicast"):
        await c.start()
    assert FakeAZ.instances[0].closed == 1
    assert c._azc is None and c._browser is None
    await c.start()  # retry succeeds with a fresh AsyncZeroconf
    assert len(FakeAZ.instances) == 2 and FakeAZ.instances[1].closed == 0
    await c.stop()
    assert FakeAZ.instances[1].closed == 1 and FakeBrowser.instances[0].cancelled == 1


# ------------------------------------------------- canonical name (live set)

PLUG_IP = "192.0.2.40"


def _rec(instance: str, type_: str, ip: str = PLUG_IP, port: int = 80) -> dict[str, Any]:
    return {
        "name": f"{instance}.{type_}.local.",
        "type": f"{type_}.local.",
        "addresses": [ip],
        "port": port,
        "server": None,
    }


def _started() -> MdnsCollector:
    c = MdnsCollector(clock=lambda: T0)
    c._started = True
    return c


async def test_hostname_does_not_depend_on_resolve_order() -> None:
    """Two concurrent _http names plus a rotating _matterc ID: every flush agrees."""
    recs = [
        _rec("plug-00005e005301", "_http._tcp"),
        _rec("plug-den", "_http._tcp"),
        _rec("D746633F41E690D5", "_matterc._udp", port=5540),
    ]
    hostnames = set()
    for order in (recs, recs[::-1]):
        c = _started()
        for r in order:
            c._on_record(r)
        hostnames |= {o.hostname for o in await c.collect()}
    assert hostnames == {"plug"}


async def test_raw_instance_kept_in_attrs() -> None:
    c = _started()
    c._on_record(_rec("plug-den", "_http._tcp"))
    (o,) = await c.collect()
    assert o.attrs["instance"] == "plug-den._http._tcp.local."
    assert o.hostname == "plug-den"


async def test_better_ranked_service_type_wins() -> None:
    c = _started()
    c._on_record(_rec("00005E005301@Den Receiver", "_raop._tcp"))
    c._on_record(_rec("RX-100", "_spotify-connect._tcp"))
    c._on_record(_rec("Den Receiver 005301", "_airplay._tcp"))
    assert {o.hostname for o in await c.collect()} == {"Den Receiver 005301"}


async def test_names_on_other_ips_do_not_compete() -> None:
    c = _started()
    c._on_record(_rec("Alpha", "_airplay._tcp", ip="192.0.2.41"))
    c._on_record(_rec("Zulu", "_airplay._tcp"))
    by_ip = {o.ipv4: o.hostname for o in await c.collect()}
    assert by_ip == {"192.0.2.41": "Alpha", PLUG_IP: "Zulu"}


async def test_removed_record_leaves_live_set() -> None:
    """A rename is a goodbye for the old record and an add for the new one."""
    c = _started()
    old = _rec("Alpha", "_airplay._tcp")
    c._on_record(old)
    await c.collect()
    c._handler(None, old["type"], old["name"], ServiceStateChange.Removed)
    c._on_record(_rec("Zulu", "_airplay._tcp"))
    assert [o.hostname for o in await c.collect()] == ["Zulu"]


async def test_canonical_hostname_truncated() -> None:
    c = _started()
    c._on_record(_rec("x" * 400, "_http._tcp"))
    (o,) = await c.collect()
    assert o.hostname is not None and len(o.hostname) == 255


async def test_only_junk_names_give_no_hostname() -> None:
    c = _started()
    c._on_record(_rec("9FDDDC93C0D0B40A-000000000001B669", "_matter._tcp", port=5540))
    (o,) = await c.collect()
    assert o.hostname is None
    assert o.attrs["instance"].startswith("9FDDDC93C0D0B40A")


async def test_flushes_with_varying_order_write_one_name(ing: Any, db: Any, clock: Any) -> None:
    """Regression: mDNS name churn (one collector:mdns batch per flush)."""
    ing.ingest([Observation("asusrouter", clock(), "wifi", MAC, PLUG_IP, "esp-plug", {})])
    recs = [_rec("plug-den", "_http._tcp"), _rec("Den Plug", "_hap._tcp", port=0)]
    c = MdnsCollector(clock=clock)
    c._started = True
    for i in range(6):
        clock.advance(minutes=1)
        for r in recs if i % 2 else recs[::-1]:
            c._on_record(r)
        ing.ingest(await c.collect())
    assert db.execute("select name from device").fetchone()[0] == "Den Plug"
    n = db.execute("select count(*) from batch where actor = 'collector:mdns'").fetchone()[0]
    assert n == 1
