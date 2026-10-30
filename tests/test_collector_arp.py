from __future__ import annotations

import asyncio
import os
import shutil
import stat
from pathlib import Path

import pytest

from conftest import T0
from lanscape_atlas.collectors.arp import ArpCollector, parse_arp_scan
from lanscape_atlas.collectors.base import CollectorError

FIXTURE = Path(__file__).parent / "fixtures" / "arp_scan.txt"


def pairs(text: str) -> list[tuple[str | None, str | None]]:
    return [(o.ipv4, o.mac) for o in parse_arp_scan(text, T0)]


def test_parse_arp_scan_basic() -> None:
    obs = parse_arp_scan(FIXTURE.read_text(), T0)
    first = obs[0]
    assert first.source == "arp"
    assert first.iface_kind is None
    assert first.mac == "00:00:5e:00:53:01"
    assert first.ipv4 == "192.0.2.1"
    assert first.hostname is None
    assert first.attrs == {}
    assert first.seen_at == T0
    assert ("192.0.2.10", "02:00:5e:00:53:0a") in pairs(FIXTURE.read_text())


def test_parse_arp_scan_skips_junk_lines() -> None:
    got = pairs(FIXTURE.read_text())
    assert all(ip != "192.0.2.11" for ip, _ in got)  # bad MAC
    assert all(mac != "00:00:5e:00:53:0e" for _, mac in got)  # bad IP, header lines
    assert pairs("") == []


def test_parse_arp_scan_dup_lines() -> None:
    got = pairs(FIXTURE.read_text())
    assert got.count(("192.0.2.12", "00:00:5e:00:53:0c")) == 1
    assert ("192.0.2.12", "00:00:5e:00:53:0d") in got


def _fake_arp_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    exe = tmp_path / "arp-scan"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))


async def test_collect_runs_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_arp_scan(
        tmp_path,
        monkeypatch,
        'echo "args: $@" >&2\nprintf "192.0.2.5\\t00:00:5e:00:53:05\\tV\\n"\n',
    )
    out = await ArpCollector("eth0").collect()
    assert [(o.ipv4, o.mac) for o in out] == [("192.0.2.5", "00:00:5e:00:53:05")]


async def test_collect_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_arp_scan(tmp_path, monkeypatch, 'printf "%0500d" 0 >&2\nexit 1\n')
    with pytest.raises(CollectorError) as ei:
        await ArpCollector("eth0").collect()
    assert len(str(ei.value)) < 400


async def test_collect_missing_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(CollectorError, match="arp-scan not found"):
        await ArpCollector("eth0").collect()


async def test_collect_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_arp_scan(tmp_path, monkeypatch, "exec " + shutil.which("sleep") + " 5\n")
    with pytest.raises(CollectorError, match="timed out"):
        await ArpCollector("eth0", timeout_s=0.3).collect()


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def _wait_for_pid(pidfile: Path) -> int:
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text())
        await asyncio.sleep(0.01)
    raise AssertionError("fake arp-scan never wrote its pid")


async def test_cancelled_collect_kills_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "pid"
    _fake_arp_scan(tmp_path, monkeypatch, f"echo $$ > {pidfile}\nexec {shutil.which('sleep')} 30\n")
    task = asyncio.ensure_future(ArpCollector("eth0", timeout_s=60).collect())
    pid = await _wait_for_pid(pidfile)
    assert not _pid_gone(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _pid_gone(pid)
