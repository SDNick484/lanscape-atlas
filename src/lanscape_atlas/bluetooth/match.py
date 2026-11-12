"""Pure BLE advert parsing and identity matching (no I/O, no DB)."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

_APPLE_COMPANY_ID = 76
_IBEACON_PREFIX = b"\x02\x15"
_IBEACON_MIN_LEN = 22
_ADDRESS = re.compile(r"^[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}$")
_DECIMAL = re.compile(r"^[0-9]+$")
_IBEACON_VALUE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"/([0-9]{1,5})/([0-9]{1,5})"
)
_U16_MAX = 0xFFFF


@dataclass(frozen=True)
class Advert:
    address: str
    rssi: int
    source: str
    name: str | None
    manufacturer_data: dict[int, bytes]
    time: float  # Home Assistant's epoch seconds (HA's clock) for this advert


@dataclass(frozen=True)
class Identity:
    kind: Literal["address", "ibeacon"]
    value: str

    def key(self) -> str:
        return f"{self.kind}:{self.value}"

    @classmethod
    def parse(cls, key: str) -> Identity:
        """Parse ``<kind>:<value>``; the value is validated and stored canonical.

        ``address:`` takes anything ``normalize_address`` accepts. ``ibeacon:``
        takes ``<uuid>/<major>/<minor>``: a hyphenated UUID (any case) and two
        plain decimal integers 0-65535. Anything else raises ``ValueError``.
        """
        kind, sep, value = key.partition(":")
        if not sep or not value:
            raise ValueError(f"invalid identity key: {key!r}")
        if kind == "address":
            return cls("address", _canonical_value("address", value))
        if kind == "ibeacon":
            return cls("ibeacon", _canonical_value("ibeacon", value))
        raise ValueError(f"invalid identity key: {key!r}")


def hidden_digest(salt: bytes, identity: Identity) -> str:
    """The salted digest a hidden ("Not mine") identity is stored as: HMAC-SHA256 of its key."""
    return hmac.new(salt, identity.key().encode(), hashlib.sha256).hexdigest()


def _canonical_value(kind: str, value: str) -> str:
    """Return ``value`` in canonical form for ``kind``; raise ValueError if invalid."""
    if kind == "address":
        return normalize_address(value)
    if kind == "ibeacon":
        m = _IBEACON_VALUE.fullmatch(value)
        if m is None:
            raise ValueError(f"invalid iBeacon identity {value!r}: want <uuid>/<major>/<minor>")
        uid, major, minor = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        for label, n in (("major", major), ("minor", minor)):
            if n > _U16_MAX:
                raise ValueError(f"invalid iBeacon identity {value!r}: {label} {n} > {_U16_MAX}")
        return f"{uid}/{major}/{minor}"
    raise ValueError(f"invalid identity kind: {kind!r}")


def check_canonical(identity: Identity) -> None:
    """Raise ValueError unless ``identity`` is valid and already in canonical form.

    A hand-built ``Identity`` cannot bypass the validation ``Identity.parse`` does.
    """
    if not isinstance(identity.value, str):
        raise ValueError(f"invalid identity value: {identity.value!r}")
    canonical = _canonical_value(identity.kind, identity.value)
    if canonical != identity.value:
        raise ValueError(
            f"identity {identity.key()!r} is not canonical (expected {identity.kind}:{canonical})"
        )


def normalize_address(addr: str) -> str:
    """Return lower-case colon form; raise ValueError unless 6 two-hex-digit octets."""
    s = addr.strip().lower()
    if not _ADDRESS.match(s):
        raise ValueError(f"invalid Bluetooth address: {addr!r}")
    return s.replace("-", ":")


def parse_advert(raw: Mapping[str, Any]) -> Advert | None:
    """Parse one Home Assistant advert dict; None if malformed."""
    if not isinstance(raw, dict):
        return None
    try:
        address = normalize_address(raw["address"])
        source = normalize_address(raw["source"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    rssi = raw.get("rssi")
    if isinstance(rssi, bool) or not isinstance(rssi, int):
        return None
    mfr = _parse_manufacturer_data(raw.get("manufacturer_data"))
    if mfr is None:
        return None
    time = raw.get("time")
    if isinstance(time, bool) or not isinstance(time, (int, float)):
        return None
    if not math.isfinite(time) or time <= 0:
        return None
    name = raw.get("name")
    return Advert(
        address=address,
        rssi=rssi,
        source=source,
        name=name if isinstance(name, str) and name else None,
        manufacturer_data=mfr,
        time=float(time),
    )


def _parse_manufacturer_data(value: Any) -> dict[int, bytes] | None:
    if value is None:
        return {}
    if not isinstance(value, dict):
        return None
    out: dict[int, bytes] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not _DECIMAL.match(k) or not isinstance(v, str):
            return None
        try:
            out[int(k)] = bytes.fromhex(v)
        except ValueError:
            return None
        if len(v) % 2:
            return None
    return out


def parse_ibeacon(manufacturer_data: Mapping[int, bytes]) -> tuple[str, int, int] | None:
    """Return (uuid, major, minor) from an iBeacon frame, else None."""
    data = manufacturer_data.get(_APPLE_COMPANY_ID)
    if data is None or len(data) < _IBEACON_MIN_LEN or not data.startswith(_IBEACON_PREFIX):
        return None
    uid = str(uuid.UUID(bytes=bytes(data[2:18])))
    major = int.from_bytes(data[18:20], "big")
    minor = int.from_bytes(data[20:22], "big")
    return uid, major, minor


def identity_of(adv: Advert) -> Identity:
    beacon = parse_ibeacon(adv.manufacturer_data)
    if beacon is not None:
        uid, major, minor = beacon
        return Identity("ibeacon", f"{uid}/{major}/{minor}")
    return Identity("address", adv.address)


def is_static_random(addr: str) -> bool:
    """True if the top two bits of the first octet are 11 (static random address)."""
    return int(normalize_address(addr).split(":")[0], 16) & 0xC0 == 0xC0
