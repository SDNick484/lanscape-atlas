"""Tests for scripts/anonymize.py (importable via the pytest pythonpath).

Real-looking MACs and private IPs are built at runtime so this file itself passes the
leak checker.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from anonymize import Anonymizer
from check_leaks import find_leaks
from lanscape_atlas.netutil import is_random_mac, normalize_mac

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "anonymize.py"


def ip(*octets: int) -> str:
    return ".".join(str(o) for o in octets)


def mac(*octets: int, sep: str = ":") -> str:
    return sep.join(f"{o:02x}" for o in octets)


def cisco(*octets: int) -> str:
    h = "".join(f"{o:02x}" for o in octets)
    return f"{h[0:4]}.{h[4:8]}.{h[8:12]}"


UNI = (0xA8, 0xBB, 0xCC, 0x11, 0x22, 0x33)  # universally administered
RND = (0x3A, 0x11, 0x22, 0x33, 0x44, 0x55)  # locally administered


def sample() -> str:
    return (
        f"? ({ip(192, 168, 1, 10)}) at {mac(*UNI).upper()} [ether] on eth0\n"
        f"? ({ip(10, 0, 0, 7)}) at {mac(0xA8, 0xBB, 0xCC, 0x44, 0x55, 0x66, sep='-')} [ether]\n"
        f"cisco {cisco(0xA8, 0xBB, 0xCC, 0x77, 0x88, 0x99)} via {ip(172, 16, 5, 9)}"
        f" gw {ip(192, 168, 1, 1)}\n"
        f"random {mac(*RND)} gateway 127.0.0.1 any 0.0.0.0 bcast 255.255.255.255\n"
        "doc 192.0.2.5 stays\n"
    )


def test_mac_mapping_consistent() -> None:
    a = Anonymizer("s")
    first = a.mac(mac(*UNI).upper())
    assert a.mac(mac(*UNI, sep="-")) == first
    assert a.mac(cisco(*UNI)) == first
    assert first.startswith("00:00:5e:00:53:")
    assert a.mac(mac(0xA8, 0xBB, 0xCC, 0x11, 0x22, 0x34)) != first
    assert Anonymizer("s").mac(mac(*UNI)) == first
    assert Anonymizer("other").mac(mac(*UNI)) != first


def test_random_bit_preserved() -> None:
    a = Anonymizer("s")
    rnd = a.mac(mac(*RND))
    uni = a.mac(mac(0x38, 0x11, 0x22, 0x33, 0x44, 0x55))
    assert rnd.startswith("02:00:5e:00:53:") and is_random_mac(rnd)
    assert uni.startswith("00:00:5e:00:53:") and not is_random_mac(uni)
    assert normalize_mac(rnd) == rnd


def test_mac_capacity() -> None:
    a = Anonymizer("s")
    for i in range(256):
        a.mac(mac(0x38, 0, 0, 0, i // 256, i % 256))
    # random MACs have their own 256-slot pool
    assert a.mac(mac(0x3A, 0, 0, 0, 0, 0)).startswith("02:00:5e:00:53:")
    with pytest.raises(ValueError):
        a.mac(mac(0x38, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF))


def test_ipv4_preserves_last_octet_then_next_free() -> None:
    a = Anonymizer("s")
    assert a.ipv4(ip(192, 168, 1, 10)) == "192.0.2.10"
    assert a.ipv4(ip(192, 168, 1, 10)) == "192.0.2.10"
    assert a.ipv4(ip(10, 0, 0, 10)) == "192.0.2.11"  # .10 taken: next free slot
    assert a.ipv4(ip(10, 9, 9, 1)) == "192.0.2.1"


def test_ipv4_capacity() -> None:
    a = Anonymizer("s")
    seen = {a.ipv4(ip(10, i // 250, i % 250, (i % 250) + 1)) for i in range(762)}
    assert len(seen) == 762
    assert all(s.rsplit(".", 1)[0] in ("192.0.2", "198.51.100", "203.0.113") for s in seen)
    assert not any(s.endswith((".0", ".255")) for s in seen)
    with pytest.raises(ValueError):
        a.ipv4(ip(10, 200, 200, 200))


def test_ipv4_unchanged_ranges() -> None:
    a = Anonymizer("s")
    for addr in ("127.0.0.1", "0.0.0.0", "255.255.255.255", "192.0.2.77", "203.0.113.4"):
        assert a.ipv4(addr) == addr


def test_hostname_numbering() -> None:
    a = Anonymizer("s")
    assert a.hostname("Den-Laptop") == "host-1"
    assert a.hostname("hall-phone") == "host-2"
    assert a.hostname("den-laptop") == "host-1"


def test_transform_text_rewrites_all() -> None:
    out = Anonymizer("s").transform_text(sample())
    for real in ("a8:bb", "a8-bb", "a8bb.cc77", "3a:11:22", "192.168", ip(10, 0, 0, 7), "172.16"):
        assert real not in out.lower()
    assert "127.0.0.1" in out and "0.0.0.0" in out and "255.255.255.255" in out
    assert "192.0.2.5" in out
    lines = out.splitlines()
    assert "-" in lines[1].split("at ")[1].split()[0]  # dash style kept
    assert lines[2].split()[1].count(".") == 2  # cisco dotted style kept


def test_names_rewritten_case_insensitively() -> None:
    a = Anonymizer("s", names=["Den-Laptop", "hall"])
    out = a.transform_text("host DEN-LAPTOP and hall, not hallway or den-laptop2")
    assert out == "host host-1 and host-2, not hallway or den-laptop2"


def test_output_passes_leak_check() -> None:
    out = Anonymizer("s").transform_text(sample())
    assert find_leaks(out, "sample.txt", set()) == []


def test_cli_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "in.txt"
    src.write_text(sample() + "my Den-Laptop here\n")
    names = tmp_path / "names.txt"
    names.write_text("den-laptop\n\n")
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--seed", "x", "--names", str(names), str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert "host-1" in res.stdout
    assert find_leaks(res.stdout, "out.txt", set()) == []


def test_cli_overflow_exits_1(tmp_path: Path) -> None:
    src = tmp_path / "in.txt"
    src.write_text("\n".join(ip(10, i // 250, i % 250, 7) for i in range(763)))
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--seed", "x", str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "capacity" in res.stderr.lower()


# ---------------------------------------------------------------- BLE capture mode


def _ibeacon_hex(uid: str, major: int = 1, minor: int = 2) -> str:
    return (
        "0215" + uid.replace("-", "") + f"{major:04x}{minor:04x}" + "c5"
    )  # Apple iBeacon frame, tx power 0xc5


def _real_uuid(n: int) -> str:
    return str(uuid.UUID(int=0x1234_5678_9ABC_4DEF_8123_4567_89AB_0000 + n))


def _ble_capture() -> list[dict[str, Any]]:
    scanner = mac(0x24, 0x6F, 0x28, 0x01, 0x02, 0x03).upper()
    tag = mac(0xC1, 0x11, 0x22, 0x33, 0x44, 0x55)  # static random, built at runtime
    return [
        {
            "address": mac(0xA8, 0xBB, 0xCC, 0x11, 0x22, 0x33),
            "rssi": -60,
            "source": scanner,
            "name": "Den Remote",
            "manufacturer_data": {"76": _ibeacon_hex(_real_uuid(1))},
            "service_uuids": ["0000180f-0000-1000-8000-00805f9b34fb", _real_uuid(2)],
            "time": 100.5,
        },
        {
            "address": tag,
            "rssi": -71,
            "source": scanner,
            "name": None,
            "manufacturer_data": {"76": _ibeacon_hex(_real_uuid(1), 9, 9), "6": "0109"},
            "time": 101.5,
        },
        {
            "address": mac(0xA8, 0xBB, 0xCC, 0x11, 0x22, 0x33),
            "rssi": -64,
            "source": mac(0x24, 0x6F, 0x28, 0x0A, 0x0B, 0x0C),
            "manufacturer_data": {},
            "time": 102.5,
        },
    ]


def test_anonymize_rewrites_ble_addresses_and_uuids() -> None:
    cap = _ble_capture()
    a = Anonymizer("s", names=["Den Remote"])
    out = a.transform_adverts(cap)
    assert len(out) == 3
    # every address and source lands in the public documentation range, static random too
    for adv in out:
        assert adv["address"].startswith("00:00:5e:00:53:")
        assert adv["source"].startswith("00:00:5e:00:53:")
    # consistent: same input, same output, within and across the capture
    assert out[0]["address"] == out[2]["address"]
    assert out[0]["source"] == out[1]["source"] != out[2]["source"]
    assert out[0]["address"] != out[1]["address"]
    # the iBeacon UUID inside manufacturer hex is rewritten consistently, major/minor kept
    h0, h1 = out[0]["manufacturer_data"]["76"], out[1]["manufacturer_data"]["76"]
    assert h0.startswith("0215" + "00000000" + "0000" + "4000" + "8000" + "0000000000")
    assert h0[4:36] == h1[4:36]
    assert h0[36:] == "00010002c5" and h1[36:] == "00090009c5"
    assert len(h0) == len(_ibeacon_hex(_real_uuid(1)))
    # other manufacturer payloads and the Bluetooth base UUID are untouched
    assert out[1]["manufacturer_data"]["6"] == "0109"
    assert out[0]["service_uuids"][0] == "0000180f-0000-1000-8000-00805f9b34fb"
    assert re.fullmatch(
        r"00000000-0000-4000-8000-0000000000[0-9a-f]{2}", out[0]["service_uuids"][1]
    )
    assert out[0]["name"] == "host-1" and out[1]["name"] is None
    assert out[0]["rssi"] == -60 and out[0]["time"] == 100.5
    # the real values are gone from the serialised result
    text = json.dumps(out).lower()
    for real in ("a8:bb", "24:6f", "c1:11", "12345678", _real_uuid(1).replace("-", "")):
        assert real not in text
    assert Anonymizer("s", names=["Den Remote"]).transform_adverts(cap) == out
    assert find_leaks(text, "cap.json", set()) == []


def test_ble_uuid_capacity() -> None:
    a = Anonymizer("s")
    for i in range(256):
        a.ble_uuid(_real_uuid(i))
    with pytest.raises(ValueError, match="capacity"):
        a.ble_uuid(_real_uuid(256))


def test_ble_cli_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "adverts.json"
    src.write_text(json.dumps(_ble_capture()))
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--seed", "x", "--ble", str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out[0]["address"].startswith("00:00:5e:00:53:")
    assert find_leaks(res.stdout, "out.json", set()) == []
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a list"}')
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--seed", "x", "--ble", str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1 and "list" in res.stderr


# ------------------------------------------- BLE: raw frames, MACs in strings, UUID tokens


def _ad(ad_type: int, body: bytes) -> bytes:
    return bytes([len(body) + 1, ad_type]) + body


def _raw_frame(uid: str, name: str) -> str:
    beacon = bytes.fromhex("4c000215") + uuid.UUID(uid).bytes + bytes.fromhex("00010002c5")
    flags = _ad(0x01, b"\x06")
    return (flags + _ad(0xFF, beacon) + _ad(0x09, name.encode())).hex()


def _parse_ads(hex_text: str) -> list[tuple[int, bytes]]:
    data, out, i = bytes.fromhex(hex_text), [], 0
    while i < len(data):
        n = data[i]
        out.append((data[i + 1], data[i + 2 : i + 1 + n]))
        i += 1 + n
    return out


def test_raw_ibeacon_uuid_rewritten_like_manufacturer_data() -> None:
    real = _real_uuid(1)
    adv = {
        "address": mac(*UNI),
        "source": mac(0x24, 0x6F, 0x28, 0x01, 0x02, 0x03),
        "name": "Den Remote",
        "manufacturer_data": {"76": _ibeacon_hex(real)},
        "raw": _raw_frame(real, "Den Remote"),
        "time": 1.0,
    }
    (out,) = Anonymizer("s", names=["Den Remote"]).transform_adverts([adv])
    mapped = out["manufacturer_data"]["76"][4:36]
    ads = dict(_parse_ads(out["raw"]))  # still a valid AD sequence
    assert ads[0x01] == b"\x06"
    assert ads[0xFF][:4] == bytes.fromhex("4c000215")
    assert ads[0xFF][4:20].hex() == mapped  # same mapping as manufacturer_data
    assert ads[0xFF][20:] == bytes.fromhex("00010002c5")  # major, minor, tx kept
    assert ads[0x09] == b"host-1"  # the local name inside raw goes through --names too
    text = json.dumps(out).lower()
    assert real.replace("-", "") not in text and "den remote" not in text
    assert "44656e2052656d6f7465" not in text  # "Den Remote" as hex


@pytest.mark.parametrize(
    "raw",
    ["zz", "0201060", "1aff4c000215", "02010605ff4c0002", "020106000001", "07ff4c0002150102"],
    ids=["not-hex", "odd", "truncated-ad", "short-ad", "junk-after-pad", "short-ibeacon"],
)
def test_unparseable_raw_is_blanked(raw: str) -> None:
    (out,) = Anonymizer("s").transform_adverts(
        [{"address": mac(*UNI), "source": mac(*UNI), "raw": raw, "time": 1.0}]
    )
    assert out["raw"] == ""


def test_raw_without_ibeacon_and_none_raw() -> None:
    plain = (_ad(0x01, b"\x06") + _ad(0xFF, bytes.fromhex("0600010203"))).hex()
    padded = plain + "0000"
    out = Anonymizer("s").transform_adverts(
        [
            {"address": mac(*UNI), "source": mac(*UNI), "raw": plain},
            {"address": mac(*UNI), "source": mac(*UNI), "raw": padded},
            {"address": mac(*UNI), "source": mac(*UNI), "raw": None},
        ]
    )
    assert [o["raw"] for o in out] == [plain, padded, None]


def test_macs_in_any_string_value_rewritten_consistently() -> None:
    tag = mac(0xC1, 0x11, 0x22, 0x33, 0x44, 0x55)  # static random, built at runtime
    adv = {
        "address": tag,
        "source": mac(0x24, 0x6F, 0x28, 0x01, 0x02, 0x03),
        "name": f"Tag {tag.upper().replace(':', '-')}",
        "service_data": {"0000feaa-0000-1000-8000-00805f9b34fb": f"{mac(*UNI)}", "x": "0a0b"},
        "time": 1.0,
    }
    (out,) = Anonymizer("s").transform_adverts([adv])
    assert out["name"] == "Tag " + out["address"].upper().replace(":", "-")
    sd = out["service_data"]
    assert sd["0000feaa-0000-1000-8000-00805f9b34fb"].startswith("00:00:5e:00:53:")
    assert sd["x"] == "0a0b"  # service data hex is left alone
    assert find_leaks(json.dumps(out), "cap.json", set()) == []


def test_uuid_tokens_rewritten_dashless_and_in_keys() -> None:
    real_dashless = _real_uuid(3).replace("-", "")
    custom_service = _real_uuid(4)
    payload = "ab" * 16  # a 16-byte service data payload is not a UUID token
    adv = {
        "address": mac(*UNI),
        "source": mac(*UNI),
        "service_uuids": [real_dashless.upper(), "0000180f-0000-1000-8000-00805f9b34fb"],
        "service_data": {custom_service: payload},
        "time": 1.0,
    }
    a = Anonymizer("s")
    (out,) = a.transform_adverts([adv])
    token = out["service_uuids"][0]
    assert re.fullmatch(r"00000000000040008000000000000[0-9A-F]{3}", token)
    assert token.lower() == a.ble_uuid(_real_uuid(3)).replace("-", "")
    assert out["service_uuids"][1] == "0000180f-0000-1000-8000-00805f9b34fb"
    (key,) = out["service_data"]
    assert key == a.ble_uuid(custom_service)
    assert out["service_data"][key] == payload
    text = json.dumps(out).lower()
    assert real_dashless not in text and custom_service not in text
    assert find_leaks(json.dumps(out), "cap.json", set()) == []
