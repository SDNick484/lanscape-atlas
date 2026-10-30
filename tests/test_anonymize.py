"""Tests for scripts/anonymize.py (importable via the pytest pythonpath).

Real-looking MACs and private IPs are built at runtime so this file itself passes the
leak checker.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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
