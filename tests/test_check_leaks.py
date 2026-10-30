from check_leaks import find_leaks, load_denylist, main

# Real-looking MACs are assembled at runtime so this file itself stays clean:
# the file allow-list only exempts the private-IP check (spec section 9).
REAL_OUI = "a4:83:e7:"
REAL_MAC = REAL_OUI + "12:34:56"


def test_doc_range_mac_allowed():
    assert find_leaks("mac 00:00:5e:00:53:1a 02:00:5e:00:53:1b", "x.py", set()) == []


def test_real_mac_flagged():
    assert find_leaks("mac " + REAL_MAC, "x.py", set())


def test_mac_with_dashes_flagged():
    assert find_leaks(REAL_MAC.upper().replace(":", "-"), "x.py", set())


def test_rfc1918_ip_flagged():
    for ip in ("10.1.2.3", "172.20.0.5", "192.168.50.1"):
        assert find_leaks(f"ip {ip}", "x.py", set())


def test_doc_range_ip_allowed():
    assert find_leaks("192.0.2.10 198.51.100.7 203.0.113.9", "x.py", set()) == []


def test_allowlisted_file_may_mention_rfc1918():
    assert find_leaks("192.168.0.0/16", "docs/superpowers/specs/x.md", set()) == []


def test_denylist_term_case_insensitive():
    assert find_leaks("host: Garage-Cam", "x.py", {"garage-cam"})


def test_allowlisted_path_still_flags_real_mac():
    assert find_leaks(f"mac {REAL_MAC}", "docs/superpowers/x.md", set())


def test_allowlisted_path_still_flags_denylist_term():
    assert find_leaks("Garage-Cam", "docs/superpowers/x.md", {"garage-cam"})


def test_mac_after_colon_prefix_flagged():
    assert find_leaks(f"mac:{REAL_MAC}", "x.py", set())


def test_mac_followed_by_colon_flagged():
    assert find_leaks(f"{REAL_MAC}: up", "x.py", set())


def test_cisco_dotted_mac_flagged():
    assert find_leaks("a483." + "e712.3456", "x.py", set())


def test_cisco_dotted_doc_mac_allowed():
    assert find_leaks("0000.5e00.531a", "x.py", set()) == []


def test_ipv6_not_flagged_as_mac():
    assert find_leaks("fe80::" + REAL_MAC, "x.py", set()) == []
    assert find_leaks("2001:db8::1", "x.py", set()) == []


def test_longer_digit_runs_not_ip():
    assert find_leaks("1192.168.1.10", "x.py", set()) == []


def test_cidr_private_ip_flagged():
    assert find_leaks("10.0.0.1/24", "x.py", set())


def test_non_rfc1918_clean():
    for ip in ("0.0.0.0", "255.255.255.0", "127.0.0.1", "169.254.1.1"):
        assert find_leaks(f"ip {ip}", "x.py", set()) == [], ip


def test_rfc1918_boundaries():
    assert find_leaks("172.16.0.1 172.31.255.1", "x.py", set())
    assert find_leaks("172.15.0.1 172.32.0.1", "x.py", set()) == []


def test_missing_explicit_denylist_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LANSCAPE_DENYLIST", str(tmp_path / "nope.txt"))
    target = tmp_path / "f.txt"
    target.write_text("clean\n")
    assert main([str(target)]) != 0
    assert "LANSCAPE_DENYLIST" in capsys.readouterr().err


def test_missing_default_denylist_means_none(tmp_path, monkeypatch):
    monkeypatch.delenv("LANSCAPE_DENYLIST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_denylist() == set()


def test_denylist_file_parsed(tmp_path, monkeypatch):
    f = tmp_path / "d.txt"
    f.write_text("# comment\n\nGarage-Cam\n")
    monkeypatch.setenv("LANSCAPE_DENYLIST", str(f))
    assert load_denylist() == {"garage-cam"}


def test_aa_bb_placeholder_mac_allowed():
    assert find_leaks("aa:bb:cc:dd:ee:ff", "x.py", set()) == []
