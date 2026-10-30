"""Ingest: the only writer of discovered data (devices, interfaces, leases, services)."""

from lanscape_atlas.ingest.ingestor import Ingestor, IngestResult

__all__ = ["IngestResult", "Ingestor"]
