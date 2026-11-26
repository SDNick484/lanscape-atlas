"""Browser tests are skipped without Playwright, except in the CI browser job."""

from __future__ import annotations

import importlib
import os

# The browser job sets LANSCAPE_BROWSER_TESTS=1. The other CI jobs run the whole suite
# without Playwright installed, so this must not hang off CI alone.
if os.environ.get("LANSCAPE_BROWSER_TESTS"):
    # Each module's importorskip would otherwise turn a missing install into a skip.
    importlib.import_module("playwright.sync_api")
