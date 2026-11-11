from __future__ import annotations

import pytest

from lanscape_atlas.ingest.naming import HINT_SOURCE, best_name, is_junk_hostname

MAC = "00:00:5e:00:53:01"
IP = "192.0.2.10"


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "   ",
        "*",
        "Unknown",
        "unknown",
        "localhost",
        IP,
        "00-00-5e-00-53-01",
        "00:00:5E:00:53:01",
        "0000.5e00.5301",
        "00005e005301",
        "00005E005301",
    ],
)
def test_junk_hostnames(name):
    assert is_junk_hostname(name, mac=MAC, ipv4=IP) is True


@pytest.mark.parametrize("name", ["nas", "Living-Room-TV", "192.0.2.11", "printer.example"])
def test_real_hostnames(name):
    assert is_junk_hostname(name, mac=MAC, ipv4=IP) is False


def test_twelve_hex_junk_even_without_mac():
    assert is_junk_hostname("0123456789ab", mac=None, ipv4=None) is True


def test_hint_source_map():
    assert HINT_SOURCE == {"asusrouter": "dhcp", "mdns": "mdns", "ssdp": "ssdp", "rdns": "rdns"}


def test_priority_mdns_first():
    c = {"mdns": "Living-Room-TV", "dhcp": "android-1", "ssdp": "TV", "rdns": "tv.example"}
    assert best_name(c, "ICANN IANA", IP) == "Living-Room-TV"


def test_priority_order_skips_missing_and_junk():
    assert (
        best_name({"mdns": None, "dhcp": "*", "ssdp": "Bravia", "rdns": "x"}, None, IP) == "Bravia"
    )
    assert best_name({"dhcp": "nas", "rdns": "nas.example"}, None, IP) == "nas"
    assert best_name({"rdns": "host.example"}, None, None) == "host.example"


def test_mdns_suffix_stripped():
    assert (
        best_name({"mdns": "Living-Room-TV._googlecast._tcp.local"}, None, IP) == "Living-Room-TV"
    )
    assert best_name({"mdns": "office-printer.local"}, None, IP) == "office-printer"


def test_fallback_vendor_last_octet():
    assert best_name({}, "ICANN IANA", IP) == "ICANN IANA .10"
    assert best_name({"dhcp": "unknown"}, "ICANN IANA", IP) == "ICANN IANA .10"


def test_no_fallback_without_vendor_or_ip():
    assert best_name({}, None, IP) is None
    assert best_name({}, "ICANN IANA", None) is None


@pytest.mark.parametrize(
    "name", ["00-00-5e-00-53-01.local", "0000.5e00.5301.local", "00005e005301.local"]
)
def test_pick_name_rejects_device_mac_after_cleaning(name):
    from lanscape_atlas.ingest.naming import pick_name

    assert pick_name({"mdns": name}, None, IP, macs={MAC}) == (None, None)
    assert pick_name({"dhcp": name}, "ICANN IANA", IP, macs={MAC}) == ("ICANN IANA .10", "vendor")


def test_pick_name_returns_source():
    from lanscape_atlas.ingest.naming import pick_name

    assert pick_name({"dhcp": "nas", "rdns": "x"}, None, IP, macs=set()) == ("nas", "dhcp")


# Matter instance names are hex node IDs: commissionable (_matterc) rotate on
# every advertisement, operational (_matter) are <fabric>-<node>.
@pytest.mark.parametrize("name", ["D746633F41E690D5", "9FDDDC93C0D0B40A-000000000001B669"])
def test_matter_ids_are_junk(name):
    assert is_junk_hostname(name, mac=None, ipv4=None) is True


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("00005E005301@Den Receiver 005301._raop._tcp.local.", "Den Receiver 005301"),
        ("00005e005301@media.linux._raop._tcp.local.", "media.linux"),
        ("pc-0001ABCD [00:00:5e:00:53:01]._workstation._tcp.local.", "pc-0001ABCD"),
        ("Den-Receiver-0123456789abcdef0123456789abcdef._googlecast._tcp.local.", "Den-Receiver"),
        ("plug-00005e005301._http._tcp.local.", "plug"),
        ("pc-0001ABCD._ssh._tcp.local.", "pc-0001ABCD"),
        ("Living-Room-TV._googlecast._tcp.local", "Living-Room-TV"),
    ],
)
def test_mdns_clean_strips_instance_noise(raw, clean):
    from lanscape_atlas.ingest.naming import clean_name

    assert clean_name("mdns", raw) == clean


def test_hex_suffix_kept_for_other_sources():
    from lanscape_atlas.ingest.naming import clean_name

    assert clean_name("dhcp", "plug-00005e005301") == "plug-00005e005301"
