from __future__ import annotations

import asyncio
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from sqlite3 import Connection

import pytest

from conftest import T0
from lanscape_atlas.collectors.base import CollectorError
from lanscape_atlas.collectors.nmap import (
    NmapLightCollector,
    ScanRefused,
    check_deep_scan,
    deep_scan,
    parse_nmap_xml,
    run_nmap,
    scan_targets,
)

FIX = Path(__file__).parent / "fixtures"
TS = "2026-01-01T00:00:00Z"  # interface and lease sightings in the DB helpers


def test_parse_nmap_light() -> None:
    obs = parse_nmap_xml((FIX / "nmap_light.xml").read_text(), T0)
    assert [o.ipv4 for o in obs] == ["192.0.2.1", "192.0.2.20"]
    first = obs[0]
    assert first.source == "nmap"
    assert first.mac == "00:00:5e:00:53:01"
    assert first.hostname is None and first.iface_kind is None
    assert first.seen_at == T0
    assert first.attrs == {
        "services": [
            {"port": 22, "proto": "tcp", "name": "ssh"},
            {"port": 80, "proto": "tcp", "name": "http", "product": "lighttpd", "version": "1.4"},
        ]
    }
    assert obs[1].attrs["services"] == [{"port": 53, "proto": "udp", "name": "domain"}]


def test_parse_nmap_skips_closed_and_down() -> None:
    obs = parse_nmap_xml((FIX / "nmap_light.xml").read_text(), T0)
    assert "192.0.2.10" not in [o.ipv4 for o in obs]
    ports = [s["port"] for s in obs[0].attrs["services"]]
    assert 23 not in ports and 25 not in ports


def test_parse_nmap_edge_cases() -> None:
    xml = (
        "<nmaprun>"
        '<host><status state="up"/><address addr="00:00:5e:00:53:02" addrtype="mac"/></host>'
        '<host><status state="up"/><address addr="999.1.1.1" addrtype="ipv4"/></host>'
        '<host><status state="up"/><address addr="192.0.2.5" addrtype="ipv4"/>'
        '<address addr="zz" addrtype="mac"/></host>'
        "</nmaprun>"
    )
    obs = parse_nmap_xml(xml, T0)
    assert [(o.ipv4, o.mac, o.attrs) for o in obs] == [("192.0.2.5", None, {"services": []})]


@pytest.mark.parametrize(
    "xml",
    [
        "",
        "not xml",
        '<!DOCTYPE x [<!ENTITY a "b">]><nmaprun/>',
        "<!entity a 'b'><nmaprun/>",
        "<!DOCTYPE nmaprun [<!ENTITY a 'b'>]><nmaprun/>",
        "<!DOCTYPE nmaprun []><nmaprun/>",
        '<!DOCTYPE nmaprun SYSTEM "file:///etc/passwd"><nmaprun/>',
        '<!DOCTYPE nmaprun PUBLIC "-//x//y" "http://example.com/x.dtd"><nmaprun/>',
        "<!DOCTYPE other><nmaprun/>",
        "<!DOCTYPE nmaprun><!DOCTYPE nmaprun><nmaprun/>",
    ],
)
def test_parse_nmap_rejects_bad_xml(xml: str) -> None:
    assert parse_nmap_xml(xml, T0) == []


def test_fixtures_carry_real_nmap_header() -> None:
    for name in ("nmap_light.xml", "nmap_deep.xml"):
        text = (FIX / name).read_text()
        assert "<!DOCTYPE nmaprun>" in text
        assert "<?xml-stylesheet" in text


@pytest.mark.parametrize("doctype", ["<!DOCTYPE nmaprun>", "<!DOCTYPE  nmaprun >"])
def test_parse_nmap_accepts_plain_nmaprun_doctype(doctype: str) -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + doctype
        + '\n<?xml-stylesheet href="file:///usr/share/nmap/nmap.xsl" type="text/xsl"?>\n'
        '<nmaprun><host><status state="up"/><address addr="192.0.2.5" addrtype="ipv4"/>'
        "</host></nmaprun>"
    )
    assert [o.ipv4 for o in parse_nmap_xml(xml, T0)] == ["192.0.2.5"]


def _iface(
    db: Connection, device_id: int, addr: str, ips: list[tuple[str, str, int]] | None = None
) -> None:
    with db:
        cur = db.execute(
            "insert into interface(device_id, kind, address, first_seen, last_seen)"
            " values (?, 'ethernet', ?, ?, ?)",
            (device_id, addr, TS, TS),
        )
        for ip, seen, closed in ips or []:
            db.execute(
                "insert into ip_lease(interface_id, ipv4, first_seen, last_seen, closed)"
                " values (?, ?, ?, ?, ?)",
                (cur.lastrowid, ip, seen, seen, closed),
            )


def test_scan_targets_excludes_never_and_offline(
    db: Connection, make_device: Callable[..., int]
) -> None:
    ok1 = make_device(online=1, scan_policy="normal")
    ok2 = make_device(online=1)
    never = make_device(online=1, scan_policy="never")
    off = make_device(online=0)
    _iface(db, ok1, "00:00:5e:00:53:10", [("192.0.2.20", TS, 0), ("192.0.2.9", TS, 1)])
    _iface(db, ok2, "00:00:5e:00:53:11", [("192.0.2.3", TS, 0)])
    _iface(db, ok1, "00:00:5e:00:53:12", [("192.0.2.3", TS, 0)])  # dup IP
    _iface(db, never, "00:00:5e:00:53:13", [("192.0.2.40", TS, 0)])
    _iface(db, off, "00:00:5e:00:53:14", [("192.0.2.41", TS, 0)])
    assert scan_targets(db) == ["192.0.2.20", "192.0.2.3"]  # string-sorted, deduped


class FakeRunner:
    def __init__(self, xml: str = "<nmaprun/>") -> None:
        self.xml = xml
        self.calls: list[tuple[list[str], float]] = []

    async def __call__(self, argv: list[str], timeout_s: float) -> str:
        self.calls.append((argv, timeout_s))
        return self.xml


async def test_light_collector_argv_and_parse() -> None:
    r = FakeRunner((FIX / "nmap_light.xml").read_text())
    c = NmapLightCollector(lambda: ["192.0.2.1", "192.0.2.20"], runner=r)
    assert c.name == "nmap_light" and c.timeout_s == 1800
    obs = await c.collect()
    assert len(obs) == 2
    assert r.calls == [
        (
            ["nmap", "-oX", "-", "-T3", "--top-ports", "100", "-Pn", "192.0.2.1", "192.0.2.20"],
            1800,
        )
    ]


async def test_light_collector_no_targets_skips_run() -> None:
    r = FakeRunner()
    assert await NmapLightCollector(lambda: [], runner=r).collect() == []
    assert r.calls == []


async def test_deep_scan_refuses_never(db: Connection, make_device: Callable[..., int]) -> None:
    d = make_device(online=1, scan_policy="never")
    _iface(db, d, "00:00:5e:00:53:20", [("192.0.2.50", TS, 0)])
    r = FakeRunner()
    with pytest.raises(ScanRefused, match="scan_policy=never"):
        await deep_scan(db, d, runner=r)
    assert r.calls == []


async def test_deep_scan_refuses_no_ip_and_unknown(
    db: Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(online=1)
    _iface(db, d, "00:00:5e:00:53:21", [("192.0.2.51", TS, 1)])
    with pytest.raises(ScanRefused, match="no current IPv4"):
        await deep_scan(db, d, runner=FakeRunner())
    with pytest.raises(KeyError):
        await deep_scan(db, 9999, runner=FakeRunner())


async def test_deep_scan_uses_current_ip(db: Connection, make_device: Callable[..., int]) -> None:
    d = make_device(online=0)
    _iface(db, d, "00:00:5e:00:53:22", [("192.0.2.60", "2026-01-01", 0)])
    _iface(
        db,
        d,
        "00:00:5e:00:53:23",
        [("192.0.2.61", "2026-01-02", 0), ("192.0.2.62", "2026-01-03", 1)],
    )
    r = FakeRunner((FIX / "nmap_deep.xml").read_text())
    obs = await deep_scan(db, d, runner=r)
    assert r.calls == [(["nmap", "-oX", "-", "-sV", "-p-", "-T3", "-Pn", "192.0.2.61"], 1800)]
    assert [o.ipv4 for o in obs] == ["192.0.2.30"]
    assert obs[0].attrs["services"][0]["product"] == "Example httpd"


def _fake_nmap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    exe = tmp_path / "nmap"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))


async def test_run_nmap_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_nmap(tmp_path, monkeypatch, 'echo "<args>$@</args>"\n')
    out = await run_nmap(["nmap", "-oX", "-"], 5)
    assert "<args>-oX -</args>" in out


async def test_run_nmap_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(CollectorError, match="nmap not found"):
        await run_nmap(["nmap"], 5)


async def test_run_nmap_nonzero_truncates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_nmap(tmp_path, monkeypatch, 'printf "%0500d" 0 >&2\nexit 2\n')
    with pytest.raises(CollectorError) as ei:
        await run_nmap(["nmap"], 5)
    assert len(str(ei.value)) < 400


async def test_run_nmap_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_nmap(tmp_path, monkeypatch, "exec " + str(shutil.which("sleep")) + " 5\n")
    with pytest.raises(CollectorError, match="timed out"):
        await run_nmap(["nmap"], 0.3)


async def test_run_nmap_cancel_kills_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pidfile = tmp_path / "pid"
    _fake_nmap(tmp_path, monkeypatch, f"echo $$ > {pidfile}\nexec {shutil.which('sleep')} 30\n")
    task = asyncio.create_task(run_nmap(["nmap"], 60))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text().strip())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_scan_targets_excludes_ip_shared_with_never(
    db: Connection, make_device: Callable[..., int]
) -> None:
    normal = make_device(online=1)
    never = make_device(online=1, scan_policy="never")
    _iface(db, normal, "00:00:5e:00:53:30", [("192.0.2.70", TS, 0), ("192.0.2.71", TS, 0)])
    _iface(db, never, "00:00:5e:00:53:31", [("192.0.2.70", TS, 0)])
    assert scan_targets(db) == ["192.0.2.71"]


def test_scan_targets_drops_non_ip_values(db: Connection, make_device: Callable[..., int]) -> None:
    d = make_device(online=1)
    _iface(db, d, "00:00:5e:00:53:32", [("-oN/tmp/x", TS, 0), ("192.0.2.72", TS, 0)])
    assert scan_targets(db) == ["192.0.2.72"]


async def test_deep_scan_refuses_ip_shared_with_never(
    db: Connection, make_device: Callable[..., int]
) -> None:
    normal = make_device(online=1)
    never = make_device(online=1, scan_policy="never")
    _iface(db, normal, "00:00:5e:00:53:33", [("192.0.2.73", TS, 0)])
    _iface(db, never, "00:00:5e:00:53:34", [("192.0.2.73", TS, 0)])
    r = FakeRunner()
    with pytest.raises(ScanRefused, match="shared with a never-scan device"):
        await deep_scan(db, normal, runner=r)
    assert r.calls == []


async def test_deep_scan_refuses_non_ip_lease(
    db: Connection, make_device: Callable[..., int]
) -> None:
    d = make_device(online=1)
    _iface(db, d, "00:00:5e:00:53:35", [("-oN/tmp/x", TS, 0)])
    r = FakeRunner()
    with pytest.raises(ScanRefused, match="no current IPv4"):
        await deep_scan(db, d, runner=r)
    assert r.calls == []


def test_scan_targets_excludes_stale_open_lease(
    db: Connection, make_device: Callable[..., int]
) -> None:
    """Ruling 44: an interface that moved IP1 -> IP2 is scanned only at IP2."""
    d = make_device(online=1)
    with db:
        cur = db.execute(
            "insert into interface(device_id, kind, address, first_seen, last_seen)"
            " values (?, 'ethernet', '00:00:5e:00:53:70', ?, ?)",
            (d, "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        )
        for ip, seen in (
            ("192.0.2.70", "2026-01-01T00:00:00Z"),
            ("192.0.2.71", "2026-01-01T01:00:00Z"),
        ):
            db.execute(
                "insert into ip_lease(interface_id, ipv4, first_seen, last_seen, closed)"
                " values (?, ?, ?, ?, 0)",
                (cur.lastrowid, ip, seen, seen),
            )
    assert scan_targets(db) == ["192.0.2.71"]
    assert scan_targets(db, window_min=120) == ["192.0.2.70", "192.0.2.71"]
    assert check_deep_scan(db, d) == "192.0.2.71"
