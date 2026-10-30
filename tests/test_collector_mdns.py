from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import T0
from lanscape_atlas.collectors import mdns
from lanscape_atlas.collectors.mdns import (
    MDNS_SERVICE_TYPES,
    MdnsCollector,
    record_to_observation,
)

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
    assert obs[0].hostname == "n100" and obs[-1].hostname == "n2099"


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
