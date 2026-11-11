from __future__ import annotations

import asyncio
import os
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from tzlocal import get_localzone

from conftest import T0
from lanscape_atlas import collectors as collectors_pkg
from lanscape_atlas import scheduler as scheduler_mod
from lanscape_atlas.clock import iso
from lanscape_atlas.collectors import REGISTRY, build_collectors
from lanscape_atlas.collectors.arp import ArpCollector
from lanscape_atlas.config import BluetoothSettings, CollectorSettings, RouterSettings, Settings
from lanscape_atlas.observation import Observation
from lanscape_atlas.scheduler import (
    CollectorRunner,
    RunOutcome,
    next_delay,
    start_scheduler,
    stop_scheduler,
)

MAC = "00:00:5e:00:53:01"
IP = "192.0.2.10"
PASSWORD = "hunter2-router-secret"
TOKEN = "api-token-abcdef"


def obs(source: str, mac: str, ip: str) -> Observation:
    return Observation(
        source=source, seen_at=T0, iface_kind=None, mac=mac, ipv4=ip, hostname=None, attrs={}
    )


class FakeCollector:
    def __init__(
        self,
        name: str,
        observations: list[Observation] | None = None,
        exc: BaseException | None = None,
        delay: float = 0.0,
        timeout_s: float = 1.0,
    ) -> None:
        self.name = name
        self.timeout_s = timeout_s
        self.observations = observations or []
        self.exc = exc
        self.delay = delay
        self.calls = 0
        self.cancelled = False

    async def collect(self) -> list[Observation]:
        self.calls += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.exc is not None:
            raise self.exc
        return list(self.observations)


class LongLivedCollector(FakeCollector):
    def __init__(self, name: str = "mdns") -> None:
        super().__init__(name)
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


def status(db, name: str) -> dict | None:
    row = db.execute("select * from collector_status where name = ?", (name,)).fetchone()
    return dict(row) if row else None


@pytest.fixture
def runner(svc, ing, clock) -> CollectorRunner:
    return CollectorRunner(svc, ing, clock, secrets=[PASSWORD, TOKEN, ""])


@pytest.fixture
def short_grace(monkeypatch) -> None:
    """Shrink the runner's outer-timeout grace so hung fakes are cut off quickly."""
    monkeypatch.setattr(scheduler_mod, "TIMEOUT_GRACE_S", 0.05)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, scan_interface="eth9")


# ------------------------------------------------------------------ next_delay


def test_next_delay_caps_at_3600() -> None:
    assert next_delay(60, 0) == 60
    assert next_delay(60, 1) == 120
    assert next_delay(60, 3) == 480
    assert next_delay(60, 6) == 3600
    assert next_delay(60, 50) == 3600
    assert next_delay(3600, 1) == 3600


# ------------------------------------------------------------------ run_once


async def test_run_once_success_resets_failures(runner, db, clock) -> None:
    db.execute(
        "insert into collector_status(name, last_error, last_error_at, consecutive_failures,"
        " next_run_at) values ('arp', 'old error', '2025-12-31T23:00:00Z', 3,"
        " '2025-12-31T23:59:00Z')"
    )
    db.commit()
    c = FakeCollector("arp", [obs("arp", MAC, IP)])
    out = await runner.run_once(c)
    assert isinstance(out, RunOutcome)
    assert out.ok and out.error is None
    assert out.result is not None and out.result.created == 1
    st = status(db, "arp")
    assert st == {
        "name": "arp",
        "last_ok": iso(clock()),
        "last_error": "old error",
        "last_error_at": "2025-12-31T23:00:00Z",
        "consecutive_failures": 0,
        "next_run_at": None,
    }
    assert db.execute("select count(*) from device").fetchone()[0] == 1


async def test_run_once_timeout_recorded(runner, db, clock, settings, short_grace) -> None:
    """Review Focus 4: a hung collect() is cut off at timeout_s and recorded as a failure."""
    c = FakeCollector("arp", [obs("arp", MAC, IP)], delay=30, timeout_s=0.05)
    t = time.monotonic()
    out = await runner.run_once(c)
    assert time.monotonic() - t < 5
    assert not out.ok and out.result is None
    assert out.error is not None and out.error.startswith("TimeoutError")
    assert c.cancelled  # the hung coroutine was cancelled, not left running
    st = status(db, "arp")
    assert st is not None
    assert st["last_error"] == out.error
    assert st["last_error_at"] == iso(clock())
    assert st["consecutive_failures"] == 1
    assert st["last_ok"] is None
    # arp's default interval is 300 s; one failure doubles it.
    assert st["next_run_at"] == "2026-01-01T00:10:00Z"
    assert db.execute("select count(*) from device").fetchone()[0] == 0


async def test_run_once_exception_isolated(runner, db) -> None:
    bad = FakeCollector("ssdp", exc=RuntimeError("boom"))
    good = FakeCollector("arp", [obs("arp", MAC, IP)])
    outs = [await runner.run_once(c) for c in (bad, good)]
    assert [o.ok for o in outs] == [False, True]
    assert outs[0].error == "RuntimeError: boom"
    assert status(db, "ssdp")["consecutive_failures"] == 1
    assert status(db, "arp")["consecutive_failures"] == 0
    assert db.execute("select count(*) from device").fetchone()[0] == 1


async def test_run_once_failures_accumulate_and_back_off(runner, db, clock) -> None:
    c = FakeCollector("ssdp", exc=RuntimeError("down"))
    for _ in range(3):
        await runner.run_once(c)
    st = status(db, "ssdp")
    assert st["consecutive_failures"] == 3
    # ssdp interval 600 s * 2**3 = 4800 -> capped at 3600.
    assert st["next_run_at"] == "2026-01-01T01:00:00Z"


async def test_run_once_cron_collector_uses_3600_base(runner, db) -> None:
    c = FakeCollector("nmap_light", exc=RuntimeError("nmap missing"))
    await runner.run_once(c)
    assert status(db, "nmap_light")["next_run_at"] == "2026-01-01T01:00:00Z"


async def test_error_message_redacts_router_password(runner, db) -> None:
    c = FakeCollector(
        "asusrouter", exc=RuntimeError(f"login failed for admin:{PASSWORD} token={TOKEN}")
    )
    out = await runner.run_once(c)
    assert out.error == "RuntimeError: login failed for admin:*** token=***"
    stored = status(db, "asusrouter")["last_error"]
    assert PASSWORD not in stored and TOKEN not in stored


async def test_error_message_truncated_to_500(runner, db) -> None:
    out = await runner.run_once(FakeCollector("arp", exc=ValueError("x" * 2000)))
    assert out.error is not None and len(out.error) == 500
    assert len(status(db, "arp")["last_error"]) == 500


async def test_ingest_exception_counts_as_failure(runner, db, monkeypatch) -> None:
    def explode(_obs):
        raise RuntimeError("ingest broke")

    monkeypatch.setattr(runner.ingestor, "ingest", explode)
    out = await runner.run_once(FakeCollector("arp", [obs("arp", MAC, IP)]))
    assert not out.ok and out.error == "RuntimeError: ingest broke"
    assert status(db, "arp")["consecutive_failures"] == 1


async def test_cancelled_error_propagates(runner) -> None:
    with pytest.raises(asyncio.CancelledError):
        await runner.run_once(FakeCollector("arp", exc=asyncio.CancelledError()))

    c = FakeCollector("arp", delay=30, timeout_s=60)
    task = asyncio.ensure_future(runner.run_once(c))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.cancelled


async def test_status_write_takes_service_lock(runner, svc, db) -> None:
    """A thread holding Service.lock blocks the status write until it releases."""
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with svc.lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    task = asyncio.ensure_future(
        asyncio.to_thread(asyncio.run, runner.run_once(FakeCollector("ssdp", exc=OSError("x"))))
    )
    await asyncio.sleep(0.2)
    assert status(db, "ssdp") is None
    release.set()
    await task
    t.join()
    assert status(db, "ssdp")["consecutive_failures"] == 1


# ------------------------------------------------------------------ backoff skip


async def test_run_if_due_skips_during_backoff(runner, db, clock) -> None:
    c = FakeCollector("ssdp", exc=RuntimeError("down"))
    await runner.run_once(c)  # next_run_at = now + 1200 s
    assert c.calls == 1
    clock.advance(seconds=600)
    assert await runner.run_if_due(c) is None
    assert c.calls == 1
    clock.advance(seconds=600)  # exactly next_run_at -> due
    c.exc = None
    out = await runner.run_if_due(c)
    assert out is not None and out.ok
    assert c.calls == 2
    assert status(db, "ssdp")["next_run_at"] is None


async def test_run_if_due_runs_without_status_row(runner) -> None:
    c = FakeCollector("arp")
    out = await runner.run_if_due(c)
    assert out is not None and out.ok and c.calls == 1


# ------------------------------------------------------------------ build_collectors


def test_build_collectors_defaults_skip_router(settings, svc) -> None:
    cs = build_collectors(settings, svc)
    assert [c.name for c in cs] == ["arp", "mdns", "ssdp", "nmap_light"]
    by = {c.name: c for c in cs}
    assert by["arp"]._interface == "eth9"
    for name, c in by.items():
        assert c.timeout_s == settings.collectors[name].timeout_s


def test_build_collectors_with_router_and_disabled(tmp_path, svc) -> None:
    cols = dict(Settings(data_dir=tmp_path).collectors)
    cols["asusrouter"] = CollectorSettings(True, 120, None, 12.5)
    cols["ssdp"] = CollectorSettings(False, 600, None, 10)
    s = Settings(
        data_dir=tmp_path,
        router=RouterSettings(host="192.0.2.1", username="admin", password=PASSWORD),
        collectors=cols,
    )
    cs = build_collectors(s, svc)
    assert [c.name for c in cs] == ["asusrouter", "arp", "mdns", "nmap_light"]
    assert cs[0].timeout_s == 12.5


def test_build_collectors_router_enabled_but_missing(tmp_path, svc) -> None:
    cols = dict(Settings(data_dir=tmp_path).collectors)
    cols["asusrouter"] = CollectorSettings(True, 120, None, 30)
    s = Settings(data_dir=tmp_path, collectors=cols)
    assert "asusrouter" not in [c.name for c in build_collectors(s, svc)]


def test_registry_builders_swappable(settings, svc, monkeypatch) -> None:
    fake = FakeCollector("arp")
    monkeypatch.setitem(REGISTRY, "arp", lambda s, service, cs: fake)
    cs = build_collectors(settings, svc)
    assert cs[0] is fake
    assert collectors_pkg.REGISTRY is REGISTRY


async def test_nmap_reader_takes_service_lock(settings, svc) -> None:
    nmap = next(c for c in build_collectors(settings, svc) if c.name == "nmap_light")
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with svc.lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    result: list[list[str]] = []
    reader = threading.Thread(target=lambda: result.append(nmap._targets()))
    reader.start()
    reader.join(0.2)
    assert reader.is_alive()  # blocked on the lock
    release.set()
    reader.join(5)
    t.join()
    assert result == [[]]


# ------------------------------------------------------------------ scheduler


async def test_scheduler_registers_jobs(settings, runner, svc) -> None:
    cs = [
        FakeCollector("arp"),
        FakeCollector("ssdp"),
        LongLivedCollector("mdns"),
        FakeCollector("nmap_light"),
    ]
    sched = await start_scheduler(settings, runner, cs, svc)
    try:
        jobs = {j.id: j for j in sched.get_jobs()}
        assert set(jobs) == {
            "collector:arp",
            "collector:ssdp",
            "collector:mdns",
            "collector:nmap_light",
            "prune",
            "presence",
        }
        for name, secs in (("arp", 300), ("ssdp", 600), ("mdns", 60)):
            trig = jobs[f"collector:{name}"].trigger
            assert isinstance(trig, IntervalTrigger)
            assert trig.interval.total_seconds() == secs
        nm = jobs["collector:nmap_light"].trigger
        assert isinstance(nm, CronTrigger)
        fields = {f.name: str(f) for f in nm.fields}
        assert fields["minute"] == "17" and fields["hour"] == "3"
        assert str(nm.timezone) == str(get_localzone())
        prune = jobs["prune"].trigger
        assert isinstance(prune, CronTrigger)
        pf = {f.name: str(f) for f in prune.fields}
        assert pf["hour"] == "4" and pf["minute"] == "41"
        assert str(prune.timezone) == str(get_localzone())
        pres = jobs["presence"].trigger
        assert isinstance(pres, IntervalTrigger) and pres.interval.total_seconds() == 60
        for j in jobs.values():
            assert j.max_instances == 1
            assert j.coalesce is True
    finally:
        await stop_scheduler(sched, cs)


async def test_collector_job_function_honors_backoff(settings, runner, svc, db, clock) -> None:
    c = FakeCollector("arp", [obs("arp", MAC, IP)])
    sched = await start_scheduler(settings, runner, [c], svc)
    try:
        job = sched.get_job("collector:arp")
        db.execute(
            "insert into collector_status(name, consecutive_failures, next_run_at)"
            " values ('arp', 2, ?)",
            ("2026-01-01T00:05:00Z",),
        )
        db.commit()
        await job.func(*job.args)
        assert c.calls == 0
        clock.advance(minutes=5)
        await job.func(*job.args)
        assert c.calls == 1
        assert status(db, "arp")["consecutive_failures"] == 0
    finally:
        await stop_scheduler(sched, [c])


async def test_failing_collector_job_does_not_stop_others(
    settings, runner, svc, db, short_grace
) -> None:
    bad = FakeCollector("ssdp", exc=RuntimeError("boom"))
    hung = FakeCollector("mdns", delay=30, timeout_s=0.05)
    good = FakeCollector("arp", [obs("arp", MAC, IP)])
    sched = await start_scheduler(settings, runner, [bad, hung, good], svc)
    try:
        jobs = [sched.get_job(f"collector:{n}") for n in ("ssdp", "mdns", "arp")]
        await asyncio.gather(*(j.func(*j.args) for j in jobs))
        assert status(db, "arp")["last_ok"] is not None
        assert status(db, "ssdp")["consecutive_failures"] == 1
        assert status(db, "mdns")["last_error"].startswith("TimeoutError")
    finally:
        await stop_scheduler(sched, [bad, hung, good])


async def test_start_and_stop_await_long_lived(settings, runner, svc) -> None:
    md = LongLivedCollector("mdns")
    plain = FakeCollector("arp")
    sched = await start_scheduler(settings, runner, [md, plain], svc)
    assert md.started == 1 and md.stopped == 0
    assert sched.running
    await stop_scheduler(sched, [md, plain])
    assert md.stopped == 1
    assert not sched.running


class FlakyStart(LongLivedCollector):
    def __init__(self, name: str = "mdns", fail: int = 1) -> None:
        super().__init__(name)
        self.fail = fail
        self.observations = [obs(name, MAC, IP)]

    async def start(self) -> None:
        self.started += 1
        if self.fail > 0:
            self.fail -= 1
            raise OSError(f"no multicast for {PASSWORD}")


async def test_failing_start_recorded_and_retried(settings, runner, svc, db, clock, caplog) -> None:
    md = FlakyStart("mdns", fail=2)
    arp = FakeCollector("arp")
    sched = await start_scheduler(settings, runner, [md, arp], svc)
    try:
        assert sched.get_job("collector:arp") is not None
        assert md.started == 1
        st = status(db, "mdns")
        assert st["consecutive_failures"] == 1
        assert st["last_error"] == "OSError: no multicast for ***"
        assert st["last_ok"] is None
        # mdns interval 60 s, one failure -> 120 s backoff.
        assert st["next_run_at"] == "2026-01-01T00:02:00Z"
        assert PASSWORD not in caplog.text

        job = sched.get_job("collector:mdns")
        await job.func(*job.args)  # still in backoff: start() not retried
        assert md.started == 1 and md.calls == 0

        clock.advance(seconds=120)
        out = await job.func(*job.args)  # start() fails again
        assert out is not None and not out.ok
        assert md.started == 2 and md.calls == 0
        st = status(db, "mdns")
        assert st["consecutive_failures"] == 2
        assert st["next_run_at"] == "2026-01-01T00:06:00Z"  # 120 s + 60 * 2**2

        clock.advance(seconds=240)
        out = await job.func(*job.args)  # start() succeeds, then collect runs
        assert out is not None and out.ok
        assert md.started == 3 and md.calls == 1
        st = status(db, "mdns")
        assert st["consecutive_failures"] == 0 and st["last_ok"] == iso(clock())

        await job.func(*job.args)  # started now: start() not called again
        assert md.started == 3 and md.calls == 2
    finally:
        await stop_scheduler(sched, [md, arp])
    assert md.stopped == 1


async def test_hung_start_is_bounded(settings, runner, svc, db, short_grace) -> None:
    class HungStart(LongLivedCollector):
        async def start(self) -> None:
            await asyncio.sleep(30)

    md = HungStart("mdns")
    md.timeout_s = 0.05
    sched = await start_scheduler(settings, runner, [md], svc)
    try:
        assert status(db, "mdns")["last_error"].startswith("TimeoutError")
    finally:
        await stop_scheduler(sched, [md], runner)


async def test_failing_stop_logs_redacted(settings, runner, svc, caplog) -> None:
    class BadStop(LongLivedCollector):
        async def stop(self) -> None:
            raise OSError(f"close failed {TOKEN}")

    md = BadStop("mdns")
    sched = await start_scheduler(settings, runner, [md], svc)
    await stop_scheduler(sched, [md], runner)
    assert "close failed ***" in caplog.text
    assert TOKEN not in caplog.text


async def test_outer_timeout_grace_lets_collector_timeout_win(runner) -> None:
    class SelfTimed(FakeCollector):
        async def collect(self) -> list[Observation]:
            try:
                await asyncio.wait_for(asyncio.sleep(30), self.timeout_s)
            except TimeoutError:
                raise RuntimeError("own timeout fired") from None
            return []

    out = await runner.run_once(SelfTimed("arp", timeout_s=0.05))
    assert out.error == "RuntimeError: own timeout fired"


async def test_runner_timeout_kills_arp_child(runner, db, tmp_path, monkeypatch) -> None:
    pidfile = tmp_path / "pid"
    exe = tmp_path / "arp-scan"
    exe.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec {shutil.which('sleep')} 30\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    out = await runner.run_once(ArpCollector("eth0", timeout_s=0.3))
    assert not out.ok
    assert out.error == "CollectorError: arp-scan timed out after 0.3s"
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert status(db, "arp")["consecutive_failures"] == 1


async def test_runner_cancel_kills_arp_child(runner, tmp_path, monkeypatch) -> None:
    """The runner's outer wait_for cancelling collect() must still reap arp-scan."""
    monkeypatch.setattr(scheduler_mod, "TIMEOUT_GRACE_S", -0.2)  # outer 0.1 s < inner 0.3 s
    pidfile = tmp_path / "pid"
    exe = tmp_path / "arp-scan"
    exe.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec {shutil.which('sleep')} 30\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    # The outer wait_for fires first and cancels collect() mid-communicate().
    arp = ArpCollector("eth0", timeout_s=0.3)
    out = await runner.run_once(arp)
    assert not out.ok
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_prune_and_presence_jobs_call_through(settings, runner, svc, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(svc, "prune_observations", lambda d: calls.append(("prune", d)) or 0)
    monkeypatch.setattr(
        runner.ingestor, "refresh_presence", lambda now=None: calls.append(("presence", now))
    )
    sched = await start_scheduler(settings, runner, [], svc)
    try:
        for jid in ("prune", "presence"):
            j = sched.get_job(jid)
            await j.func(*j.args)
        assert calls == [("prune", 30), ("presence", None)]
    finally:
        await stop_scheduler(sched, [])


async def test_prune_job_prunes_ble(tmp_path, runner, svc, monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(svc, "prune_observations", lambda d: calls.append(("obs", d)) or 0)
    monkeypatch.setattr(svc, "prune_ble", lambda d: calls.append(("ble", d)) or 0)
    s = Settings(data_dir=tmp_path, bluetooth=BluetoothSettings(retention_days=9))
    sched = await start_scheduler(s, runner, [], svc)
    try:
        j = sched.get_job("prune")
        await j.func(*j.args)
        assert calls == [("obs", 30), ("ble", 9)]
    finally:
        await stop_scheduler(sched, [])


async def test_prune_job_ble_failure_does_not_block_observations(
    runner, svc, monkeypatch, caplog
) -> None:
    calls: list[tuple[str, int]] = []

    def boom(_d):
        raise RuntimeError("db gone")

    monkeypatch.setattr(svc, "prune_observations", lambda d: calls.append(("obs", d)) or 0)
    monkeypatch.setattr(svc, "prune_ble", boom)
    await scheduler_mod._prune_job(svc, 30, 14)  # must not raise
    assert calls == [("obs", 30)]
    assert "BLE sighting prune failed" in caplog.text


async def test_prune_job_failing_observation_prune_still_prunes_ble(
    runner, svc, monkeypatch, caplog
) -> None:
    calls: list[tuple[str, int]] = []

    def boom(_d):
        raise RuntimeError("db gone")

    monkeypatch.setattr(svc, "prune_observations", boom)
    monkeypatch.setattr(svc, "prune_ble", lambda d: calls.append(("ble", d)) or 0)
    await scheduler_mod._prune_job(svc, 30, 14)  # must not raise
    assert calls == [("ble", 14)]
    assert "observation prune failed" in caplog.text


async def test_prune_job_swallows_errors(settings, runner, svc, monkeypatch) -> None:
    def boom(_d):
        raise RuntimeError("db gone")

    monkeypatch.setattr(svc, "prune_observations", boom)
    sched = await start_scheduler(settings, runner, [], svc)
    try:
        j = sched.get_job("prune")
        await j.func(*j.args)  # must not raise
    finally:
        await stop_scheduler(sched, [])


async def test_runner_from_settings_redacts_and_uses_intervals(tmp_path, svc, ing, clock, db):
    cols = dict(Settings(data_dir=tmp_path).collectors)
    cols["arp"] = CollectorSettings(True, 100, None, 5)
    s = Settings(
        data_dir=tmp_path,
        api_token=TOKEN,
        router=RouterSettings(host="192.0.2.1", username="admin", password=PASSWORD),
        collectors=cols,
    )
    r = CollectorRunner.from_settings(s, svc, ing, clock)
    out = await r.run_once(FakeCollector("arp", exc=OSError(f"{PASSWORD}/{TOKEN}")))
    assert out.error == "OSError: ***/***"
    assert status(db, "arp")["next_run_at"] == "2026-01-01T00:03:20Z"  # 100 s * 2


async def test_runner_redacts_ha_token(tmp_path, svc, ing, clock, db):
    from lanscape_atlas.config import HomeAssistantSettings

    ha_token = "ha-secret-token"
    s = Settings(
        data_dir=tmp_path,
        home_assistant=HomeAssistantSettings(url="http://192.0.2.10:8123", token=ha_token),
    )
    r = CollectorRunner.from_settings(s, svc, ing, clock)
    out = await r.run_once(FakeCollector("arp", exc=OSError(f"auth failed {ha_token}")))
    assert out.error == "OSError: auth failed ***"
    assert ha_token not in status(db, "arp")["last_error"]


async def test_interval_jobs_fire_at_startup(settings, runner, svc) -> None:
    """M7: interval collectors run once right away, not only after the first interval."""
    from datetime import datetime, timedelta

    cs = [FakeCollector("arp"), LongLivedCollector("mdns"), FakeCollector("nmap_light")]
    before = datetime.now(get_localzone())
    sched = await start_scheduler(settings, runner, cs, svc)
    try:
        sched.pause()  # inspect the planned times without letting the jobs run
        jobs = {j.id: j for j in sched.get_jobs()}
        soon = before + timedelta(seconds=5)
        for job_id in ("collector:arp", "collector:mdns", "presence"):
            assert jobs[job_id].next_run_time <= soon, job_id
        # Cron jobs keep their schedule.
        nm = jobs["collector:nmap_light"].next_run_time
        assert (nm.hour, nm.minute) == (3, 17)
        pr = jobs["prune"].next_run_time
        assert (pr.hour, pr.minute) == (4, 41)
    finally:
        await stop_scheduler(sched, cs, runner=runner)


async def test_ble_flush_job_registered(tmp_path, runner, svc) -> None:
    from lanscape_atlas.config import HomeAssistantSettings

    s = Settings(
        data_dir=tmp_path,
        home_assistant=HomeAssistantSettings(url="http://192.0.2.10:8123", token="t"),
        bluetooth=BluetoothSettings(flush_s=42),
    )

    class FakeRuntime:
        def __init__(self) -> None:
            self.started = self.stopped = False
            self.flushes = 0

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

        async def flush(self) -> int:
            self.flushes += 1
            return 0

    rt = FakeRuntime()
    sched = await start_scheduler(s, runner, [], svc, bluetooth=rt)  # type: ignore[arg-type]
    try:
        assert rt.started
        job = {j.id: j for j in sched.get_jobs()}["ble_flush"]
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == 42
        assert job.max_instances == 1 and job.coalesce is True
        assert job.func == rt.flush
    finally:
        await stop_scheduler(sched, [], runner=runner, bluetooth=rt)  # type: ignore[arg-type]
    assert rt.stopped


async def test_no_ble_flush_job_without_runtime(settings, runner, svc) -> None:
    sched = await start_scheduler(settings, runner, [], svc)
    try:
        assert "ble_flush" not in {j.id for j in sched.get_jobs()}
    finally:
        await stop_scheduler(sched, [], runner=runner)


async def test_record_status_ok_and_error_redacted(runner, db, clock) -> None:
    runner.record_status("bluetooth", f"boom {TOKEN}")
    st = status(db, "bluetooth")
    assert st["last_error"] == "boom ***" and st["consecutive_failures"] == 1
    runner.record_status("bluetooth", None)
    st = status(db, "bluetooth")
    assert st["consecutive_failures"] == 0 and st["last_ok"] == iso(clock())
