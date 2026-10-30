"""Connection setup and schema migration."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open the database with WAL, foreign keys and a busy timeout."""
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the schema if not yet applied; safe to call on every startup."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
        )
    if current == SCHEMA_VERSION:
        return current
    script = resources.files("lanscape_atlas.store").joinpath("schema.sql").read_text("utf-8")
    # executescript commits first and runs outside a transaction; wrap it so a
    # failure leaves no partial schema and user_version is set atomically.
    try:
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        raise
    return SCHEMA_VERSION
