"""Command-line entry point (``lanscape-atlas``). Subcommands are added task by task."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import tempfile
from datetime import timedelta
from importlib.resources import as_file, files
from pathlib import Path
from urllib.request import Request, urlopen

from lanscape_atlas.clock import utcnow
from lanscape_atlas.config import load_settings
from lanscape_atlas.events import EventBus
from lanscape_atlas.ingest import Ingestor, IngestResult
from lanscape_atlas.observation import Observation
from lanscape_atlas.oui import OuiDB
from lanscape_atlas.service import Service
from lanscape_atlas.store.db import migrate, open_db

OUI_URL = "https://standards-oui.ieee.org/oui/oui.csv"
OUI_MIN_ROWS = 1000
_OUI_HEADER = ("registry", "assignment", "organization name")


def load_observations(path: Path) -> list[Observation]:
    """Read a JSON list of serialized observations (objects, as ``Observation.to_json`` makes)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(d, dict) for d in raw):
        raise ValueError(f"{path}: expected a JSON list of observation objects")
    return [Observation.from_json(json.dumps(d)) for d in raw]


def _load_oui(data_dir: Path) -> OuiDB:
    custom = data_dir / "oui.csv"
    if custom.is_file():
        return OuiDB.load(custom)
    with as_file(files("lanscape_atlas") / "data" / "oui_sample.csv") as sample:
        return OuiDB.load(sample)


def ingest_file(path: Path) -> IngestResult:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    observations = load_observations(path)
    conn = open_db(settings.data_dir / "atlas.db")
    try:
        migrate(conn)
        events = EventBus()
        service = Service(conn, events, utcnow, ip_match_window_min=settings.ip_match_window_min)
        ingestor = Ingestor(
            conn,
            service,
            events,
            _load_oui(settings.data_dir),
            utcnow,
            ip_match_window=timedelta(minutes=settings.ip_match_window_min),
        )
        return ingestor.ingest(observations)
    finally:
        conn.close()


def init_data_dir() -> Path:
    """Create the data dir (mode 0700) and the migrated database; return the DB path."""
    settings = load_settings()
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db_path = settings.data_dir / "atlas.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()
    return db_path


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """INFO-level logging with time, level and logger name, unless already configured."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def serve(host: str | None, port: int | None) -> None:
    import uvicorn

    from lanscape_atlas.web.app import create_app

    configure_logging()
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=host or settings.bind_host,
        port=port if port is not None else settings.bind_port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("LANSCAPE_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


def _validate_oui(path: Path) -> None:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header or tuple(c.strip().lower() for c in header[:3]) != _OUI_HEADER:
            raise ValueError("downloaded file does not look like the IEEE OUI CSV")
        rows = sum(1 for r in reader if len(r) >= 3)
    if rows < OUI_MIN_ROWS:
        raise ValueError(f"downloaded OUI file has only {rows} rows (need {OUI_MIN_ROWS})")
    OuiDB.load(path)


def update_oui() -> Path:
    """Download the IEEE MA-L CSV and atomically replace ``oui.csv`` once it validates."""
    settings = load_settings()
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = settings.data_dir / "oui.csv"
    req = Request(OUI_URL, headers={"User-Agent": "lanscape-atlas (oui updater)"})  # noqa: S310
    fd, tmp_name = tempfile.mkstemp(dir=settings.data_dir, prefix="oui.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, urlopen(req, timeout=60) as resp:  # noqa: S310
            while chunk := resp.read(65536):
                out.write(chunk)
        _validate_oui(tmp)
        tmp.chmod(0o644)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lanscape-atlas")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest-file", help="ingest a JSON file of serialized observations")
    p.add_argument("path", type=Path)
    sub.add_parser("migrate", help="create the data directory and database")
    p = sub.add_parser("serve", help="run the web app, API and MCP server")
    p.add_argument("--host", help="bind address (default: bind_host from config)")
    p.add_argument("--port", type=int, help="bind port (default: bind_port from config)")
    sub.add_parser("update-oui", help="download the IEEE OUI vendor list")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ingest-file":
        try:
            result = ingest_file(args.path)
        except (OSError, ValueError, KeyError) as exc:
            print(f"lanscape-atlas: {exc}", file=sys.stderr)
            return 1
        print(
            f"created={result.created} updated={result.updated} "
            f"unattached={result.unattached} dropped={result.dropped}"
        )
        return 0
    if args.command == "migrate":
        try:
            print(init_data_dir())
        except (OSError, ValueError) as exc:
            print(f"lanscape-atlas: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "update-oui":
        try:
            print(update_oui())
        except (OSError, ValueError) as exc:
            print(f"lanscape-atlas: update-oui failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "serve":
        try:
            serve(args.host, args.port)
        except (OSError, ValueError) as exc:
            print(f"lanscape-atlas: {exc}", file=sys.stderr)
            return 1
        return 0
    return 2  # pragma: no cover - argparse enforces a known subcommand


if __name__ == "__main__":
    sys.exit(main())
