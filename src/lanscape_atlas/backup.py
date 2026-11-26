"""Consistent backup and restore of the database and the floor-plan images.

An archive is a gzip tar with exactly these members: ``manifest.json``, ``atlas.db`` (made
with the SQLite online-backup API, so it is consistent while the server runs) and
``floorplan/<file>`` for every stored plan image. Restore never uses ``tarfile.extract``: it
validates every member name and type, then copies the bytes into a staging directory itself.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path

from lanscape_atlas.clock import utcnow
from lanscape_atlas.config import Settings
from lanscape_atlas.store.db import SCHEMA_VERSION

FORMAT = 1
DB_NAME = "atlas.db"
PLAN_DIR = "floorplan"
MANIFEST = "manifest.json"
ARCHIVE_PREFIX = "atlas-backup-"
ARCHIVE_SUFFIX = ".tar.gz"
DEFAULT_KEEP = 7
# Plan files are <sha256>.<ext>; anything else (stray temp files) is not part of a backup.
_PLAN_NAME = re.compile(r"^[0-9a-f]{64}\.(png|jpg|svg)$")
_MAX_MEMBER = 1 << 30  # 1 GiB per member: far above any real database or plan image


class BackupError(Exception):
    """A backup or restore was refused or failed; the message says why."""


def default_backup_dir(data_dir: Path) -> Path:
    return data_dir / "backups"


def _stamp() -> str:
    return utcnow().strftime("%Y%m%d-%H%M%S")


def _archive_name() -> str:
    return f"{ARCHIVE_PREFIX}{_stamp()}{ARCHIVE_SUFFIX}"


def _is_archive_name(name: str) -> bool:
    return name.startswith(ARCHIVE_PREFIX) and name.endswith(ARCHIVE_SUFFIX)


def create_backup(settings: Settings, out: Path | None = None, keep: int = DEFAULT_KEEP) -> Path:
    """Write a backup archive (mode 0600) and prune old ones; return its path.

    ``out`` is a file path, or an existing/``/``-terminated directory in which a timestamped
    name is used; omitted, ``<data_dir>/backups/`` is used. Rotation keeps the newest ``keep``
    ``atlas-backup-*.tar.gz`` files in the destination directory (0 keeps all).
    """
    db_path = settings.data_dir / DB_NAME
    if not db_path.is_file():
        raise BackupError(f"no database at {db_path}")
    if keep < 0:
        raise BackupError("--keep must be 0 or more")
    if out is None:
        dest = default_backup_dir(settings.data_dir) / _archive_name()
    elif out.is_dir() or str(out).endswith(os.sep):
        dest = out / _archive_name()
    else:
        dest = out
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".backup.", suffix=".tmp")
    tmp = Path(tmp_name)  # mkstemp creates it 0600
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-backup-") as work:
            snap = Path(work) / DB_NAME
            _snapshot_db(db_path, snap)
            version = _user_version(snap)
            manifest = json.dumps(
                {"format": FORMAT, "schema_version": version, "created": utcnow().isoformat()}
            ).encode()
            with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
                _add_bytes(tar, MANIFEST, manifest)
                tar.add(snap, arcname=DB_NAME, filter=_plain)
                plans = settings.data_dir / PLAN_DIR
                if plans.is_dir():
                    for plan in sorted(plans.iterdir()):
                        if _PLAN_NAME.match(plan.name) and plan.is_file() and not plan.is_symlink():
                            tar.add(plan, arcname=f"{PLAN_DIR}/{plan.name}", filter=_plain)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if keep:
        _rotate(dest.parent, keep, dest)
    return dest


def _plain(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o600
    return info


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(utcnow().timestamp())
    tar.addfile(_plain(info), io.BytesIO(data))


def _snapshot_db(src: Path, dst: Path) -> None:
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise BackupError(f"could not snapshot the database: {exc}") from exc
    finally:
        source.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _rotate(folder: Path, keep: int, current: Path) -> None:
    archives = sorted(
        (p for p in folder.iterdir() if _is_archive_name(p.name) and p.is_file()),
        key=lambda p: p.name,  # the name embeds a UTC timestamp, so it sorts oldest first
    )
    for old in archives[:-keep]:
        if old != current:
            old.unlink(missing_ok=True)


def service_running(settings: Settings) -> bool:
    """True when something accepts connections on the configured bind address and port."""
    host = settings.bind_host
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host in ("::", "[::]"):
        host = "::1"
    try:
        with socket.create_connection((host, settings.bind_port), timeout=1.0):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def _open_archive(path: Path) -> Iterator[tarfile.TarFile]:
    try:
        tar = tarfile.open(path, mode="r:gz")
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise BackupError(f"{path} is not a readable backup archive: {exc}") from exc
    try:
        yield tar
    finally:
        tar.close()


def _check_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Return the members, or raise if any name or type is not one a backup contains."""
    members = tar.getmembers()
    seen: set[str] = set()
    for m in members:
        name = m.name
        if not m.isreg():
            raise BackupError(f"unsafe archive member {name!r}: not a regular file")
        if name in seen:
            raise BackupError(f"archive member {name!r} appears twice")
        seen.add(name)
        if m.size > _MAX_MEMBER:
            raise BackupError(f"archive member {name!r} is implausibly large")
        if name in (MANIFEST, DB_NAME):
            continue
        head, _, tail = name.partition("/")
        if head != PLAN_DIR or not _PLAN_NAME.match(tail):
            raise BackupError(f"unexpected archive member {name!r}")
    if MANIFEST not in seen or DB_NAME not in seen:
        raise BackupError("not a lanscape-atlas backup: manifest.json or atlas.db is missing")
    return members


def verify_archive(path: Path) -> int:
    """Check an archive end to end without touching the data; return its schema version."""
    with tempfile.TemporaryDirectory(prefix="atlas-verify-") as work:
        return _stage(path, Path(work))


def _stage(path: Path, stage: Path) -> int:
    """Validate ``path`` and copy its contents into ``stage``; return the schema version."""
    try:
        with _open_archive(path) as tar:
            members = _check_members(tar)
            (stage / PLAN_DIR).mkdir(mode=0o700)
            for m in members:
                target = stage / m.name  # names are validated: no separators beyond floorplan/
                fh = tar.extractfile(m)
                if fh is None:
                    raise BackupError(f"cannot read archive member {m.name!r}")
                with fh, open(target, "wb") as out:
                    shutil.copyfileobj(fh, out)
    except (tarfile.TarError, EOFError, OSError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"{path} is damaged: {exc}") from exc

    try:
        manifest = json.loads((stage / MANIFEST).read_text("utf-8"))
        fmt = manifest["format"]
        manifest_version = int(manifest["schema_version"])
    except (ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"unreadable manifest.json: {exc}") from exc
    if fmt != FORMAT:
        raise BackupError(f"backup format {fmt!r} is not supported by this version")
    try:
        conn = sqlite3.connect(str(stage / DB_NAME))
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"the database in the archive is not usable: {exc}") from exc
    if check != "ok":
        raise BackupError(f"the database in the archive failed its integrity check: {check}")
    if version != manifest_version:
        raise BackupError("the archive manifest does not match its database")
    if version > SCHEMA_VERSION:
        raise BackupError(
            f"the backup has schema version {version}, newer than the {SCHEMA_VERSION} this "
            "installed code supports; upgrade lanscape-atlas first"
        )
    return version


def restore_backup(settings: Settings, archive: Path) -> Path:
    """Replace the data with the archive's; return the directory holding the replaced data.

    The caller must have checked that the service is stopped. The old ``atlas.db`` (with its
    ``-wal``/``-shm``) and ``floorplan/`` are moved, not deleted, into
    ``<data_dir>/replaced-<timestamp>/``. Schema upgrades happen on the next start.
    """
    data_dir = settings.data_dir
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=data_dir, prefix=".restore-"))  # same filesystem: rename
    try:
        _stage(archive, stage)
        _match_owner(stage, data_dir)
        replaced = data_dir / f"replaced-{_stamp()}"
        replaced.mkdir(mode=0o700)
        for name in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", PLAN_DIR):
            if (data_dir / name).exists():
                os.replace(data_dir / name, replaced / name)
        os.replace(stage / DB_NAME, data_dir / DB_NAME)
        os.replace(stage / PLAN_DIR, data_dir / PLAN_DIR)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return replaced


def _match_owner(stage: Path, data_dir: Path) -> None:
    """When run as root, give the restored files to the data directory's owner."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    st = data_dir.stat()
    for p in [stage, *stage.rglob("*")]:
        os.chown(p, st.st_uid, st.st_gid)
