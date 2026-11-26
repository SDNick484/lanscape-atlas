"""serve / migrate / update-oui subcommands. No network, no real server."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lanscape_atlas import cli
from lanscape_atlas.cli import main
from lanscape_atlas.oui import OuiDB


def _csv(rows: int) -> bytes:
    lines = ["Registry,Assignment,Organization Name,Organization Address"]
    for i in range(rows):
        lines.append(f"MA-L,{i:06X},Example Corp {i},Example Street")
    return ("\n".join(lines) + "\n").encode()


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


def _fake_urlopen(payload: bytes | Exception, seen: dict[str, Any]):  # type: ignore[no-untyped-def]
    def opener(req: Any, timeout: float | None = None) -> _Resp:
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        if isinstance(payload, Exception):
            raise payload
        return _Resp(payload)

    return opener


def test_migrate_creates_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    data = tmp_path / "data"
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(data))
    assert main(["migrate"]) == 0
    assert (data / "atlas.db").is_file()
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert str(data / "atlas.db") in capsys.readouterr().out


def test_serve_builds_app_without_starting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LANSCAPE_FORWARDED_ALLOW_IPS", raising=False)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append((a, k)))
    assert main(["serve"]) == 0
    ((args, kwargs),) = calls
    assert callable(args[0])
    assert kwargs["port"] == 8720
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "127.0.0.1"


def test_serve_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LANSCAPE_FORWARDED_ALLOW_IPS", "192.0.2.10")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append(k))
    assert main(["serve", "--host", "0.0.0.0", "--port", "9000"]) == 0  # noqa: S104
    assert calls[0]["host"] == "0.0.0.0"  # noqa: S104
    assert calls[0]["port"] == 9000
    assert calls[0]["forwarded_allow_ips"] == "192.0.2.10"


def test_update_oui_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "urlopen", _fake_urlopen(_csv(1500), seen))
    assert main(["update-oui"]) == 0
    assert seen["url"] == cli.OUI_URL
    assert seen["timeout"] == 60
    assert seen["ua"]
    # Built at runtime: the synthetic CSV's OUIs are not documentation MACs (leak check).
    mac = ":".join(["00", "00", "01"] + ["00"] * 3)
    assert OuiDB.load(tmp_path / "oui.csv").lookup(mac) == "Example Corp 1"
    assert [p.name for p in tmp_path.iterdir()] == ["oui.csv"]
    assert "oui.csv" in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    [_csv(10), b"<html>blocked</html>\n" * 2000, b"", OSError("offline")],
    ids=["too-few-rows", "bad-header", "empty", "network-error"],
)
def test_update_oui_failure_keeps_old(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,  # type: ignore[no-untyped-def]
    payload: bytes | Exception,
) -> None:
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    (tmp_path / "oui.csv").write_bytes(b"old")
    monkeypatch.setattr(cli, "urlopen", _fake_urlopen(payload, {}))
    assert main(["update-oui"]) == 1
    assert (tmp_path / "oui.csv").read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["oui.csv"]
    assert "lanscape-atlas" in capsys.readouterr().err
    assert os.path.isdir(tmp_path)


def test_serve_configures_info_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    assert main(["serve"]) == 0
    assert root.level == logging.INFO
    (handler,) = root.handlers
    assert handler.formatter is not None
    fmt = handler.formatter._fmt or ""
    for field in ("%(asctime)s", "%(levelname)s", "%(name)s", "%(message)s"):
        assert field in fmt


def test_serve_keeps_existing_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    root = logging.getLogger()
    mine = logging.NullHandler()
    monkeypatch.setattr(root, "handlers", [mine])
    monkeypatch.setattr(root, "level", logging.DEBUG)
    assert main(["serve"]) == 0
    assert root.handlers == [mine] and root.level == logging.DEBUG


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def fresh_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], None]]:
    """Call the yielded function in the test body (pytest adds its own root handlers when
    the call phase starts) to get an unconfigured root logger. Levels are restored with
    setLevel afterwards, which also clears the loggers' isEnabledFor caches."""
    root, aps = logging.getLogger(), logging.getLogger("apscheduler")
    saved = (root.level, aps.level)

    def unconfigure() -> None:
        monkeypatch.setattr(root, "handlers", [])
        root.setLevel(logging.WARNING)
        aps.setLevel(logging.NOTSET)

    yield unconfigure
    root.setLevel(saved[0])  # setLevel (not monkeypatch) also clears isEnabledFor caches
    aps.setLevel(saved[1])


def test_serve_sets_apscheduler_to_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_logging: Callable[[], None]
) -> None:
    fresh_logging()
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    assert main(["serve"]) == 0
    assert logging.getLogger().level == logging.INFO  # our own INFO lines are unchanged
    assert logging.getLogger("apscheduler").level == logging.WARNING
    executor = logging.getLogger("apscheduler.executors.default")
    assert executor.getEffectiveLevel() == logging.WARNING


async def test_apscheduler_logs_only_failures_and_misses(
    fresh_logging: Callable[[], None],
) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    fresh_logging()
    cli.configure_logging()
    assert logging.getLogger().level == logging.INFO
    seen = _Collect()
    aps = logging.getLogger("apscheduler")
    aps.addHandler(seen)
    ran: list[str] = []

    async def ok() -> None:
        ran.append("ok")

    async def boom() -> None:
        ran.append("boom")
        raise RuntimeError("job failed")

    sched = AsyncIOScheduler()
    now = datetime.now(UTC)
    sched.add_job(ok, "date", run_date=now, id="ok", name="ok")
    sched.add_job(boom, "date", run_date=now, id="boom", name="boom")
    # Due an hour ago with a 1 s grace: skipped as missed.
    sched.add_job(
        ok,
        IntervalTrigger(hours=2),
        next_run_time=now - timedelta(hours=1),
        misfire_grace_time=1,
        id="late",
        name="late",
    )
    try:
        sched.start()
        for _ in range(200):
            if len(ran) >= 2 and any("missed" in r.getMessage() for r in seen.records):
                break
            await asyncio.sleep(0.01)
    finally:
        sched.shutdown(wait=False)
        await asyncio.sleep(0)
        aps.removeHandler(seen)

    assert sorted(ran) == ["boom", "ok"]
    messages = [(r.levelno, r.getMessage()) for r in seen.records]
    assert all(level >= logging.WARNING for level, _ in messages), messages
    assert any(lvl == logging.ERROR and "raised an exception" in m for lvl, m in messages)
    assert any(lvl == logging.WARNING and "was missed by" in m for lvl, m in messages)
