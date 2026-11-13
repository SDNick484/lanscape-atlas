"""Connection setup and schema migration."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 6


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open the database with WAL, foreign keys and a busy timeout."""
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _load_migration(n: int) -> str:
    """SQL text of migration ``n`` (``migrations/000n_*.sql``)."""
    folder = resources.files("lanscape_atlas.store").joinpath("migrations")
    for item in folder.iterdir():
        if item.name.startswith(f"{n:04d}_") and item.name.endswith(".sql"):
            return item.read_text("utf-8")
    raise RuntimeError(f"missing migration {n}")


def _apply(conn: sqlite3.Connection, script: str, version: int) -> None:
    """Run ``script`` and set user_version in one transaction; roll back on failure."""
    # executescript commits first and runs outside a transaction; wrap it so a
    # failure leaves no partial schema and user_version is set atomically.
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        raise


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the schema up to SCHEMA_VERSION; safe to call on every startup."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
        )
    if current == 0:
        script = resources.files("lanscape_atlas.store").joinpath("schema.sql").read_text("utf-8")
        _apply(conn, script, 1)
        current = 1
    for n in range(current + 1, SCHEMA_VERSION + 1):
        _apply(conn, _load_migration(n), n)
        current = n
    return current
