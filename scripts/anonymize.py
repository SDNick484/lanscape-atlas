#!/usr/bin/env python3
"""Rewrite a real network capture into documentation-range values.

Usage:
    python scripts/anonymize.py --seed S [--names FILE] [--ble] in_file > out_file

Reads ``in_file`` and writes the rewritten text to stdout. Exits 1 with a message on
stderr if the capture has more distinct addresses than the documentation ranges hold.

What is rewritten
-----------------
* MACs (colon, dash and Cisco dotted forms; upper or lower case, style preserved) map to
  ``00:00:5e:00:53:xx``; MACs with the locally-administered bit set map to
  ``02:00:5e:00:53:xx``. 256 distinct MACs per prefix; more raises ``ValueError``.
  ``--seed`` salts a stable hash that picks the slot, so different seeds give different
  mappings.
* IPv4 addresses map into ``192.0.2.0/24``, then ``198.51.100.0/24``, then
  ``203.0.113.0/24`` (762 usable addresses; .0 and .255 are skipped). The last octet is kept
  when that slot is free in the current net, otherwise the next free slot is used. Loopback,
  ``0.0.0.0``, ``255.255.255.255`` and addresses already in a documentation range are left
  unchanged. Everything else, public addresses included, is rewritten.
* Hostnames cannot be detected reliably in free-form text, so they are NOT rewritten
  automatically. Pass ``--names FILE`` (real hostnames, one per line) to rewrite exactly
  those strings, case-insensitively and on word edges, to ``host-<n>`` (n counts from 1 in
  order of first sight in the names file).

Bluetooth advert captures (``--ble``)
-------------------------------------
``--ble`` reads a Home Assistant advert capture: ONE JSON LIST of advert dicts, the objects
``bluetooth/subscribe_advertisements`` delivers (``address``, ``source``, ``rssi``, ``name``,
``manufacturer_data`` as ``{"<company id>": "<hex>"}``, optionally ``service_uuids``, ``time``).
JSON lines are not accepted. The output is the same list, pretty-printed. Rewritten:

* ``address`` and ``source`` (the scanner) map to ``00:00:5e:00:53:xx`` (one shared pool of
  256, one mapping per capture), static-random addresses included. The random bit is NOT
  kept: the output carries no real address or address type.
* Every iBeacon UUID (the 16 bytes inside the Apple, company ``76``, ``0215...`` frame of
  ``manufacturer_data``) and every other full UUID in a string value, except those on the
  Bluetooth base (``-0000-1000-8000-00805f9b34fb``), maps to
  ``00000000-0000-4000-8000-0000000000xx`` (256 slots; more raises ``ValueError``). The
  iBeacon major, minor and TX power are kept.
* ``name`` goes through ``--names`` like free text. Other manufacturer payloads and
  ``service_data`` are copied as they are: review them by hand.

The mapping is deterministic per ``Anonymizer`` instance and never written to disk, so keep
the real input out of the repository. Run ``scripts/check_leaks.py`` on the output.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

_MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2})"
    r"(?![0-9A-Fa-f])"
    r"|(?<![0-9A-Fa-f.])(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}(?![0-9A-Fa-f])"
)
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]*\d)")

_UUID_RE = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)
_BT_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"
_APPLE_KEY = "76"
_IBEACON_HEX_PREFIX = "0215"
_BLE_UUID_FMT = "00000000-0000-4000-8000-0000000000{:02x}"
_DOC_NETS = ("192.0.2", "198.51.100", "203.0.113")
_UNCHANGED = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("255.255.255.255/32"),
    *(ipaddress.ip_network(f"{n}.0/24") for n in _DOC_NETS),
)
_SLOTS = range(1, 255)


def _plain_mac(raw: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if len(digits) != 12:
        raise ValueError(f"invalid MAC address: {raw!r}")
    return digits


class Anonymizer:
    def __init__(self, seed: str, names: list[str] | None = None) -> None:
        self.seed = seed
        self._macs: dict[str, str] = {}
        self._mac_used: dict[bool, set[int]] = {False: set(), True: set()}
        self._ble_addrs: dict[str, str] = {}
        self._ble_uuids: dict[str, str] = {}
        self._ips: dict[str, str] = {}
        self._ip_used: list[set[int]] = [set(), set(), set()]
        self._net = 0
        self._hosts: dict[str, str] = {}
        self._names: list[str] = []
        for name in names or []:
            self.hostname(name)
            self._names.append(name.strip())

    # ------------------------------------------------------------------ MAC
    def mac(self, real: str) -> str:
        digits = _plain_mac(real)
        if digits in self._macs:
            return self._macs[digits]
        random = bool(int(digits[:2], 16) & 0x02)
        used = self._mac_used[random]
        if len(used) >= 256:
            raise ValueError(
                f"MAC capacity exceeded: more than 256 distinct "
                f"{'locally administered' if random else 'universally administered'} MACs"
            )
        h = hashlib.sha256(f"{self.seed}\0{digits}".encode()).digest()
        slot = h[0]
        while slot in used:
            slot = (slot + 1) % 256
        used.add(slot)
        prefix = "02:00:5e:00:53" if random else "00:00:5e:00:53"
        out = f"{prefix}:{slot:02x}"
        self._macs[digits] = out
        return out

    # ------------------------------------------------------------ Bluetooth
    def ble_address(self, real: str) -> str:
        """Map a Bluetooth address (or scanner source) to ``00:00:5e:00:53:xx``."""
        digits = _plain_mac(real)
        if digits not in self._ble_addrs:
            slot = self._ble_slot("address", digits, len(self._ble_addrs), self._ble_addrs)
            self._ble_addrs[digits] = f"00:00:5e:00:53:{slot:02x}"
        return self._ble_addrs[digits]

    def ble_uuid(self, real: str) -> str:
        """Map a UUID to ``00000000-0000-4000-8000-0000000000xx``."""
        key = str(uuid.UUID(real.strip()))
        if key not in self._ble_uuids:
            slot = self._ble_slot("uuid", key, len(self._ble_uuids), self._ble_uuids)
            self._ble_uuids[key] = _BLE_UUID_FMT.format(slot)
        return self._ble_uuids[key]

    def _ble_slot(self, what: str, key: str, used_count: int, table: dict[str, str]) -> int:
        if used_count >= 256:
            raise ValueError(f"Bluetooth {what} capacity exceeded: more than 256 distinct values")
        taken = {int(v[-2:], 16) for v in table.values()}
        slot = hashlib.sha256(f"{self.seed}\0{what}\0{key}".encode()).digest()[0]
        while slot in taken:
            slot = (slot + 1) % 256
        return slot

    def _ble_string(self, text: str) -> str:
        def sub(m: re.Match[str]) -> str:
            if m.group().lower().endswith(_BT_BASE_SUFFIX):
                return m.group()
            return self.ble_uuid(m.group())

        return _UUID_RE.sub(sub, text)

    def _ble_manufacturer_hex(self, key: str, value: str) -> str:
        low = value.lower()
        if key == _APPLE_KEY and low.startswith(_IBEACON_HEX_PREFIX) and len(low) >= 44:
            raw = low[4:36]
            new = self.ble_uuid(str(uuid.UUID(hex=raw))).replace("-", "")
            return low[:4] + new + low[36:]
        return value

    def _ble_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._ble_string(self.transform_names(value))
        if isinstance(value, list):
            return [self._ble_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._ble_value(v) for k, v in value.items()}
        return value

    def transform_adverts(self, adverts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rewrite a list of Home Assistant advert dicts (see the module docstring)."""
        out: list[dict[str, Any]] = []
        for adv in adverts:
            new: dict[str, Any] = {}
            for k, v in adv.items():
                if k in ("address", "source") and isinstance(v, str):
                    new[k] = self.ble_address(v)
                elif k == "manufacturer_data" and isinstance(v, dict):
                    new[k] = {
                        mk: self._ble_manufacturer_hex(str(mk), mv) if isinstance(mv, str) else mv
                        for mk, mv in v.items()
                    }
                else:
                    new[k] = self._ble_value(v)
            out.append(new)
        return out

    # ----------------------------------------------------------------- IPv4
    def ipv4(self, real: str) -> str:
        addr = ipaddress.IPv4Address(real.strip())
        text = str(addr)
        if any(addr in net for net in _UNCHANGED):
            for i, prefix in enumerate(_DOC_NETS):
                if text.startswith(prefix + "."):
                    self._ip_used[i].add(int(text.rsplit(".", 1)[1]))
            return text
        if text in self._ips:
            return self._ips[text]
        last = addr.packed[3]
        while self._net < len(_DOC_NETS):
            used = self._ip_used[self._net]
            free = [s for s in _SLOTS if s not in used]
            if not free:
                self._net += 1
                continue
            slot = last if last in _SLOTS and last not in used else None
            if slot is None:
                slot = next((s for s in free if s > last), free[0])
            used.add(slot)
            out = f"{_DOC_NETS[self._net]}.{slot}"
            self._ips[text] = out
            return out
        raise ValueError("IPv4 capacity exceeded: more than 762 distinct addresses")

    # ------------------------------------------------------------- hostnames
    def hostname(self, real: str) -> str:
        key = real.strip().lower()
        if key not in self._hosts:
            self._hosts[key] = f"host-{len(self._hosts) + 1}"
        return self._hosts[key]

    # ----------------------------------------------------------------- text
    def _sub_mac(self, m: re.Match[str]) -> str:
        raw = m.group()
        new = self.mac(raw)
        if "." in raw:
            d = new.replace(":", "")
            new = f"{d[0:4]}.{d[4:8]}.{d[8:12]}"
        elif "-" in raw:
            new = new.replace(":", "-")
        return new.upper() if raw.upper() == raw and raw.lower() != raw else new

    def _sub_ip(self, m: re.Match[str]) -> str:
        try:
            return self.ipv4(m.group())
        except ipaddress.AddressValueError:
            return m.group()  # e.g. 999.1.1.1 or 01.2.3.4: not an address

    def transform_names(self, text: str) -> str:
        if self._names:
            alts = "|".join(re.escape(n) for n in sorted(set(self._names), key=len, reverse=True))
            pat = re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)
            text = pat.sub(lambda m: self.hostname(m.group()), text)
        return text

    def transform_text(self, text: str) -> str:
        text = self.transform_names(text)
        text = _MAC_RE.sub(self._sub_mac, text)
        return _IPV4_RE.sub(self._sub_ip, text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rewrite a capture into documentation-range values.")
    ap.add_argument("--seed", required=True, help="salt for the stable MAC mapping")
    ap.add_argument("--names", type=Path, help="file of real hostnames, one per line")
    ap.add_argument(
        "--ble",
        action="store_true",
        help="in_file is a JSON list of Home Assistant Bluetooth advert dicts",
    )
    ap.add_argument("in_file", type=Path)
    args = ap.parse_args(argv)
    try:
        names: list[str] = []
        if args.names is not None:
            names = [ln.strip() for ln in args.names.read_text().splitlines() if ln.strip()]
        text = args.in_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"anonymize: {exc}", file=sys.stderr)
        return 1
    try:
        anon = Anonymizer(args.seed, names)
        if args.ble:
            data = json.loads(text)
            if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
                raise ValueError("--ble expects a JSON list of advert objects")
            out = json.dumps(anon.transform_adverts(data), indent=1) + "\n"
        else:
            out = anon.transform_text(text)
    except ValueError as exc:  # includes json.JSONDecodeError
        print(f"anonymize: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
