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


# --- backup / restore --------------------------------------------------------------------


def _seed_data(data: Path) -> bytes:
    """A migrated database with one marker row plus one stored plan image; return the image."""
    import sqlite3

    from lanscape_atlas.floorplan.image import store_plan_file

    assert main(["migrate"]) == 0
    conn = sqlite3.connect(str(data / "atlas.db"))
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('before')")
    conn.commit()
    conn.close()
    image = b"\x89PNG\r\n\x1a\n" + b"plan-bytes"
    store_plan_file(data, image, "png")
    return image


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(data))
    monkeypatch.setattr("lanscape_atlas.backup.service_running", lambda s: False)
    return data


def _marker(data: Path) -> str:
    import sqlite3

    conn = sqlite3.connect(str(data / "atlas.db"))
    try:
        return str(conn.execute("SELECT v FROM marker").fetchone()[0])
    finally:
        conn.close()


def test_backup_restore_round_trip(data_dir: Path, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    image = _seed_data(data_dir)
    out = tmp_path / "out" / "b.tar.gz"
    assert main(["backup", "--out", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    # change the live data, then restore
    conn = sqlite3.connect(str(data_dir / "atlas.db"))
    conn.execute("UPDATE marker SET v='after'")
    conn.commit()
    conn.close()
    for f in (data_dir / "floorplan").iterdir():
        f.unlink()
    capsys.readouterr()
    assert main(["restore", str(out)]) == 0
    assert _marker(data_dir) == "before"
    plans = list((data_dir / "floorplan").iterdir())
    assert len(plans) == 1 and plans[0].read_bytes() == image
    kept = list(data_dir.glob("replaced-*"))
    assert len(kept) == 1
    old = sqlite3.connect(str(kept[0] / "atlas.db"))
    assert old.execute("SELECT v FROM marker").fetchone()[0] == "after"
    old.close()
    assert not list(data_dir.glob(".restore-*"))


def test_backup_default_dir_and_rotation(data_dir: Path) -> None:
    from lanscape_atlas import backup

    _seed_data(data_dir)
    folder = data_dir / "backups"
    folder.mkdir()
    for n in range(1, 5):
        (folder / f"atlas-backup-2020010{n}-000000.tar.gz").write_bytes(b"x")
    (folder / "unrelated.txt").write_text("keep me")
    assert main(["backup", "--keep", "2"]) == 0
    names = sorted(p.name for p in folder.iterdir())
    assert "unrelated.txt" in names
    archives = [n for n in names if n.startswith(backup.ARCHIVE_PREFIX)]
    assert len(archives) == 2
    assert archives[0] == "atlas-backup-20200104-000000.tar.gz"
    assert stat.S_IMODE((folder / archives[1]).stat().st_mode) == 0o600


def test_backup_without_database_fails(data_dir: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["backup"]) == 1
    assert "no database" in capsys.readouterr().err


def test_restore_refuses_while_running(
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    _seed_data(data_dir)
    out = tmp_path / "b.tar.gz"
    assert main(["backup", "--out", str(out)]) == 0
    monkeypatch.setattr("lanscape_atlas.backup.service_running", lambda s: True)
    assert main(["restore", str(out)]) == 1
    assert "running" in capsys.readouterr().err
    assert not list(data_dir.glob("replaced-*"))


def test_service_running_detects_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from lanscape_atlas import backup
    from lanscape_atlas.config import load_settings

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        monkeypatch.setenv("LANSCAPE_DATA_DIR", "/nonexistent")
        s = load_settings()
        from dataclasses import replace

        s = replace(s, bind_host="127.0.0.1", bind_port=srv.getsockname()[1])
        assert backup.service_running(s) is True
    finally:
        srv.close()
    assert backup.service_running(s) is False


def test_restore_refuses_newer_schema(data_dir: Path, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    from lanscape_atlas.store.db import SCHEMA_VERSION

    _seed_data(data_dir)
    conn = sqlite3.connect(str(data_dir / "atlas.db"))
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    out = tmp_path / "b.tar.gz"
    assert main(["backup", "--out", str(out)]) == 0
    capsys.readouterr()
    assert main(["restore", str(out)]) == 1
    assert "newer" in capsys.readouterr().err
    assert not list(data_dir.glob("replaced-*"))


def _tar(path: Path, members: list[tuple[str, bytes, bytes | None]]) -> None:
    """Write a tar.gz; (name, data, link target) - a link target makes it a symlink."""
    import tarfile

    with tarfile.open(path, "w:gz") as tar:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link.decode()
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize(
    "bad_name",
    [
        "../evil",
        "/etc/evil",
        "floorplan/../../evil",
        "floorplan/notahash.png",
        "extra.txt",
        "floorplan/" + "a" * 64 + ".exe",
    ],
)
def test_restore_rejects_bad_member_paths(
    data_dir: Path,
    tmp_path: Path,
    bad_name: str,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    _seed_data(data_dir)
    good = tmp_path / "good.tar.gz"
    assert main(["backup", "--out", str(good)]) == 0
    import tarfile

    with tarfile.open(good) as t:
        members = [(m.name, t.extractfile(m).read(), None) for m in t.getmembers()]  # type: ignore[union-attr]
    bad = tmp_path / "bad.tar.gz"
    _tar(bad, [*members, (bad_name, b"x", None)])
    capsys.readouterr()
    assert main(["restore", str(bad)]) == 1
    assert "restore refused" in capsys.readouterr().err
    assert _marker(data_dir) == "before"  # untouched
    assert not (tmp_path / "evil").exists() and not (data_dir.parent / "evil").exists()


def test_restore_rejects_symlink_member(data_dir: Path, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _seed_data(data_dir)
    bad = tmp_path / "bad.tar.gz"
    _tar(bad, [("manifest.json", b"{}", None), ("atlas.db", b"", b"/etc/passwd")])
    assert main(["restore", str(bad)]) == 1
    assert "regular file" in capsys.readouterr().err


def test_restore_rejects_non_archive_and_missing(data_dir: Path, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _seed_data(data_dir)
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a tarball")
    assert main(["restore", str(junk)]) == 1
    assert main(["restore", str(tmp_path / "missing.tar.gz")]) == 1
    empty = tmp_path / "empty.tar.gz"
    _tar(empty, [("manifest.json", b"{}", None)])
    assert main(["restore", str(empty)]) == 1
    assert _marker(data_dir) == "before"
