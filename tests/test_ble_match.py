"""Tests for BLE advert parsing and identity matching."""

from __future__ import annotations

from typing import Any

import pytest

from lanscape_atlas.bluetooth.match import (
    Advert,
    Identity,
    check_canonical,
    identity_of,
    is_static_random,
    normalize_address,
    parse_advert,
    parse_ibeacon,
)

UUID = "00000000-0000-4000-8000-000000000001"
UUID_HEX = "00000000-0000-4000-8000-0000000000ab"  # has hex letters, for case tests
UUID_BYTES = bytes.fromhex(UUID.replace("-", ""))
IBEACON = b"\x02\x15" + UUID_BYTES + (100).to_bytes(2, "big") + (1).to_bytes(2, "big") + b"\xc5"


def _advert(mfr: dict[int, bytes] | None = None) -> Advert:
    return Advert(
        address="00:00:5e:00:53:01",
        rssi=-60,
        source="00:00:5e:00:53:aa",
        name=None,
        manufacturer_data=mfr or {},
        time=1.0,
    )


def test_parse_ibeacon_valid() -> None:
    assert parse_ibeacon({76: IBEACON}) == (UUID, 100, 1)


def test_parse_ibeacon_ignores_missing_tx_power() -> None:
    assert parse_ibeacon({76: IBEACON[:22]}) == (UUID, 100, 1)


def test_parse_ibeacon_short() -> None:
    assert parse_ibeacon({76: IBEACON[:21]}) is None


def test_parse_ibeacon_wrong_prefix() -> None:
    assert parse_ibeacon({76: b"\x02\x16" + IBEACON[2:]}) is None


def test_parse_ibeacon_other_company() -> None:
    assert parse_ibeacon({6: IBEACON}) is None


def test_identity_prefers_ibeacon() -> None:
    ident = identity_of(_advert({76: IBEACON}))
    assert ident == Identity("ibeacon", f"{UUID}/100/1")


def test_identity_falls_back_to_address() -> None:
    assert identity_of(_advert()) == Identity("address", "00:00:5e:00:53:01")


@pytest.mark.parametrize(
    "ident",
    [Identity("address", "00:00:5e:00:53:01"), Identity("ibeacon", f"{UUID}/100/1")],
)
def test_identity_key_roundtrip(ident: Identity) -> None:
    assert Identity.parse(ident.key()) == ident


@pytest.mark.parametrize(
    "key",
    [
        "",
        "address",
        "foo:bar",
        "address:",
        ":x",
        "ibeacon:",
        "address:ZZZ",
        "address:00:00:5e:00:53",
        "ibeacon:garbage",
        f"ibeacon:{UUID}/1",
        f"ibeacon:{UUID}/1/2/3",
        "ibeacon:not-a-uuid/1/1",
        f"ibeacon:{UUID}/65536/1",
        f"ibeacon:{UUID}/1/65536",
        f"ibeacon:{UUID}/+1/1",
        f"ibeacon:{UUID}/ 1/1",
        f"ibeacon:{UUID}/1/-1",
        f"ibeacon:{UUID}/1/1\n",
        f"ibeacon:{{{UUID}}}/1/1",
    ],
)
def test_identity_parse_rejects_garbage(key: str) -> None:
    with pytest.raises(ValueError):
        Identity.parse(key)


def test_identity_parse_normalizes_address() -> None:
    assert Identity.parse("address:00:00:5E:00:53:01") == Identity("address", "00:00:5e:00:53:01")


def test_identity_parse_normalizes_ibeacon() -> None:
    assert Identity.parse(f"ibeacon:{UUID_HEX.upper()}/0/65535") == Identity(
        "ibeacon", f"{UUID_HEX}/0/65535"
    )


def test_identity_parse_rejects_major_65536() -> None:
    with pytest.raises(ValueError, match="65536"):
        Identity.parse(f"ibeacon:{UUID}/65536/0")


@pytest.mark.parametrize(
    "ident",
    [
        Identity("address", "00:00:5E:00:53:01"),
        Identity("address", "garbage"),
        Identity("ibeacon", f"{UUID_HEX.upper()}/1/1"),
        Identity("ibeacon", f"{UUID}/01/1"),
        Identity("bogus", "x"),  # type: ignore[arg-type]
    ],
)
def test_check_canonical_rejects_non_canonical(ident: Identity) -> None:
    with pytest.raises(ValueError):
        check_canonical(ident)


@pytest.mark.parametrize(
    "ident",
    [Identity("address", "00:00:5e:00:53:01"), Identity("ibeacon", f"{UUID}/100/1")],
)
def test_check_canonical_accepts_canonical(ident: Identity) -> None:
    check_canonical(ident)


def test_static_random_built_at_runtime() -> None:
    addr = f"{0x00 | 0xC0:02x}" + ":00:5e:00:53:01"
    assert is_static_random(addr) is True
    assert is_static_random("00:00:5e:00:53:01") is False
    assert is_static_random("02:00:5e:00:53:01") is False


@pytest.mark.parametrize(
    "raw",
    ["00:00:5E:00:53:01", "00-00-5e-00-53-01", " 00:00:5e:00:53:01 "],
)
def test_normalize_address(raw: str) -> None:
    assert normalize_address(raw) == "00:00:5e:00:53:01"


@pytest.mark.parametrize("raw", ["", "00:00:5e:00:53", "00:00:5e:00:53:01:02", "zz:00:5e:00:53:01"])
def test_normalize_address_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_address(raw)


def _raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Tag",
        "address": "00:00:5E:00:53:01",
        "rssi": -70,
        "manufacturer_data": {"76": IBEACON.hex()},
        "service_data": {},
        "source": "00:00:5E:00:53:AA",
        "connectable": True,
        "time": 1.0,
    }
    base.update(over)
    return base


def test_parse_advert_hex_keys() -> None:
    adv = parse_advert(_raw())
    assert adv == Advert(
        address="00:00:5e:00:53:01",
        rssi=-70,
        source="00:00:5e:00:53:aa",
        name="Tag",
        manufacturer_data={76: IBEACON},
        time=1.0,
    )


def test_parse_advert_no_name_no_mfr() -> None:
    adv = parse_advert(_raw(name=None, manufacturer_data=None))
    assert adv is not None
    assert adv.name is None
    assert adv.manufacturer_data == {}


@pytest.mark.parametrize(
    "raw",
    [
        "not a dict",
        None,
        {k: v for k, v in _raw().items() if k != "address"},
        _raw(address="nope"),
        _raw(address=None),
        _raw(rssi=None),
        {k: v for k, v in _raw().items() if k != "rssi"},
        _raw(rssi=True),
        _raw(rssi="-70"),
        _raw(manufacturer_data={"76": "abc"}),
        _raw(manufacturer_data={"76": "zz"}),
        _raw(manufacturer_data={"apple": "00"}),
        _raw(manufacturer_data={"7.5": "00"}),
        _raw(source="bad"),
        {k: v for k, v in _raw().items() if k != "time"},
        _raw(time=None),
        _raw(time="1.0"),
        _raw(time=True),
        _raw(time=float("nan")),
        _raw(time=float("inf")),
        _raw(time=float("-inf")),
        _raw(time=0),
        _raw(time=-5.0),
    ],
)
def test_parse_advert_rejects_malformed(raw: Any) -> None:
    assert parse_advert(raw) is None


def test_parse_advert_keeps_ha_time() -> None:
    adv = parse_advert(_raw(time=1767225600.25))
    assert adv is not None and adv.time == 1767225600.25
    adv = parse_advert(_raw(time=1767225600))  # an int is fine too
    assert adv is not None and adv.time == 1767225600.0 and isinstance(adv.time, float)
