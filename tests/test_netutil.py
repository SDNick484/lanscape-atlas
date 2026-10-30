import pytest

from lanscape_atlas.netutil import is_random_mac, normalize_mac, valid_ipv4


@pytest.mark.parametrize(
    "raw",
    ["00:00:5E:00:53:1A", "00-00-5e-00-53-1a", "00005e00531a", "0000.5e00.531a"],
)
def test_normalize_mac_forms(raw):
    assert normalize_mac(raw) == "00:00:5e:00:53:1a"


@pytest.mark.parametrize("raw", ["<incomplete>", "", "00:00:5e:00:53", "zz:00:5e:00:53:1a"])
def test_normalize_mac_rejects(raw):
    with pytest.raises(ValueError):
        normalize_mac(raw)


def test_is_random_mac():
    assert is_random_mac("02:00:5e:00:53:01") is True
    assert is_random_mac("00:00:5e:00:53:01") is False


def test_valid_ipv4():
    assert valid_ipv4("192.0.2.10") == "192.0.2.10"
    assert valid_ipv4("192.0.2.010") is None  # leading zeros rejected (stdlib ipaddress)
    assert valid_ipv4("2001:db8::1") is None
    assert valid_ipv4("not-an-ip") is None
