"""The ESPHome example carries no credentials, only ``!secret`` references."""

from __future__ import annotations

import re
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parent.parent / "deploy" / "esphome" / "ble-proxy.example.yaml"
SECRET_KEYS = ("ssid", "password", "key")


def _lines() -> list[str]:
    return [ln for ln in EXAMPLE.read_text().splitlines() if not ln.lstrip().startswith("#")]


def test_esphome_example_has_only_secrets() -> None:
    seen = set()
    for ln in _lines():
        m = re.match(r"\s*(ssid|password|key):\s*(.*?)\s*$", ln)
        if m:
            assert re.fullmatch(r"!secret \w+", m.group(2)), ln
            seen.add(m.group(1))
    assert seen == set(SECRET_KEYS)


def test_esphome_example_values() -> None:
    text = "\n".join(_lines())
    assert "name: ble-proxy-example" in text
    assert "board: esp32dev" in text
    assert "interval: 320ms" in text and "window: 120ms" in text
    scan = text.split("esp32_ble_tracker:")[1].split("bluetooth_proxy:")[0]
    assert "active: false" in scan
    assert re.search(r"bluetooth_proxy:\s*\n\s+active: false", text)
