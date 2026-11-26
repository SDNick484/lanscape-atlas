"""Browser tests are skipped without Playwright, except in CI where that is a failure."""

from __future__ import annotations

import importlib
import os

if os.environ.get("CI"):
    # Each module's importorskip would otherwise turn a missing install into a skip.
    importlib.import_module("playwright.sync_api")
