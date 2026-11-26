import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import check_leaks
from check_leaks import find_leaks, load_denylist, main

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_leaks.py"

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


def test_loopback_unspecified_multicast_broadcast_ipv4_clean():
    for ip in ("0.0.0.0", "127.0.0.1", "127.8.9.10", "224.0.0.251", "239.255.255.250"):
        assert find_leaks(f"ip {ip}", "x.py", set()) == [], ip
    assert find_leaks("to 255.255.255.255", "x.py", set()) == []


def test_rfc1918_boundaries():
    assert find_leaks("172.16.0.1 172.31.255.1", "x.py", set())
    assert find_leaks("172.15.0.1 172.32.0.1", "x.py", set())


@pytest.mark.parametrize(
    "ip",
    [
        "100.64.0.1",  # CGNAT / Tailscale, the case issue #48 names
        "100.127.255.254",
        "100.63.255.255",  # just below CGNAT, still not documentation
        "169.254.1.1",  # link-local
        "8.8.8.8",  # public
        "198.51.99.1",  # next to a documentation /24
        "192.0.3.1",
        "255.255.255.0",  # a netmask looks like an address; allow-list it if it ever appears
    ],
)
def test_non_documentation_ipv4_flagged(ip):
    assert find_leaks(f"ip {ip}", "x.py", set())


def test_documentation_ipv4_boundaries():
    assert find_leaks("192.0.2.0 192.0.2.255 198.51.100.1 203.0.113.254", "x.py", set()) == []
    assert find_leaks("203.0.112.255", "x.py", set())


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


# ---- Task 11: LANSCAPE_LEAK_DENYLIST and the tracked-image check ----------------------


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Keep the developer's real ~/.config denylist and env vars out of subprocess runs."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LANSCAPE_DENYLIST", raising=False)
    monkeypatch.delenv("LANSCAPE_LEAK_DENYLIST", raising=False)


def test_tracked_image_fails(tmp_git_repo, git_add, run_check_leaks, isolated_env):
    (tmp_git_repo / "plan.png").write_bytes(b"\x89PNG")
    git_add(tmp_git_repo, "plan.png")
    result = run_check_leaks(tmp_git_repo)
    assert result.returncode != 0
    assert "plan.png: image file must not be committed" in result.stdout


def test_uppercase_image_extension_fails(tmp_git_repo, git_add, run_check_leaks, isolated_env):
    (tmp_git_repo / "PLAN.PNG").write_bytes(b"\x89PNG")
    git_add(tmp_git_repo, "PLAN.PNG")
    assert run_check_leaks(tmp_git_repo).returncode != 0


def test_all_image_extensions_fail(tmp_git_repo, git_add, run_check_leaks, isolated_env):
    for ext in ("jpg", "jpeg", "svg", "gif", "webp"):
        (tmp_git_repo / f"p.{ext}").write_bytes(b"x")
        git_add(tmp_git_repo, f"p.{ext}")
    out = run_check_leaks(tmp_git_repo).stdout
    for ext in ("jpg", "jpeg", "svg", "gif", "webp"):
        assert f"p.{ext}: image file" in out


def test_docs_screenshots_exempt(tmp_git_repo, git_add, run_check_leaks, isolated_env):
    (tmp_git_repo / "docs" / "screenshots").mkdir(parents=True)
    (tmp_git_repo / "docs" / "screenshots" / "x.png").write_bytes(b"\x89PNG")
    git_add(tmp_git_repo, "docs/screenshots/x.png")
    assert run_check_leaks(tmp_git_repo).returncode == 0


def test_screenshots_exemption_is_exact_prefix(
    tmp_git_repo, git_add, run_check_leaks, isolated_env
):
    (tmp_git_repo / "docs" / "screenshots-extra").mkdir(parents=True)
    (tmp_git_repo / "docs" / "screenshots-extra" / "x.png").write_bytes(b"\x89PNG")
    git_add(tmp_git_repo, "docs/screenshots-extra/x.png")
    assert run_check_leaks(tmp_git_repo).returncode != 0


def test_explicit_path_image_fails(tmp_git_repo, isolated_env):
    (tmp_git_repo / "plan.png").write_bytes(b"\x89PNG")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "plan.png"],
        cwd=tmp_git_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "image file must not be committed" in result.stdout


def test_explicit_path_screenshot_exempt(tmp_git_repo, isolated_env):
    (tmp_git_repo / "docs" / "screenshots").mkdir(parents=True)
    (tmp_git_repo / "docs" / "screenshots" / "x.png").write_bytes(b"\x89PNG")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "docs/screenshots/x.png"],
        cwd=tmp_git_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_denylisted_room_name_fails(
    tmp_git_repo, git_add, run_check_leaks, isolated_env, monkeypatch, tmp_path
):
    deny = tmp_path / "deny.txt"
    deny.write_text("Room Zed\n")
    monkeypatch.setenv("LANSCAPE_LEAK_DENYLIST", str(deny))
    (tmp_git_repo / "note.md").write_text("see room ZED\n")
    git_add(tmp_git_repo, "note.md")
    assert run_check_leaks(tmp_git_repo).returncode != 0


def test_missing_leak_denylist_file_fails(
    tmp_git_repo, run_check_leaks, isolated_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("LANSCAPE_LEAK_DENYLIST", str(tmp_path / "nope.txt"))
    result = run_check_leaks(tmp_git_repo)
    assert result.returncode != 0
    assert "LANSCAPE_LEAK_DENYLIST" in result.stderr


def test_legacy_denylist_env_still_works(
    tmp_git_repo, git_add, run_check_leaks, isolated_env, monkeypatch, tmp_path
):
    deny = tmp_path / "legacy.txt"
    deny.write_text("garage-cam\n")
    monkeypatch.setenv("LANSCAPE_DENYLIST", str(deny))
    (tmp_git_repo / "note.md").write_text("host Garage-Cam\n")
    git_add(tmp_git_repo, "note.md")
    assert run_check_leaks(tmp_git_repo).returncode != 0


def test_denylist_sources_merge(tmp_path, monkeypatch):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("alpha\n")
    b.write_text("# c\nBeta\n")
    monkeypatch.setenv("LANSCAPE_DENYLIST", str(a))
    monkeypatch.setenv("LANSCAPE_LEAK_DENYLIST", str(b))
    assert load_denylist() == {"alpha", "beta"}


def test_clean_repo_passes(tmp_git_repo, git_add, run_check_leaks, isolated_env):
    (tmp_git_repo / "ok.md").write_text("nothing here\n")
    git_add(tmp_git_repo, "ok.md")
    assert run_check_leaks(tmp_git_repo).returncode == 0


def _check_paths(repo, *paths):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_screenshots_dotdot_escape_not_exempt(tmp_git_repo, isolated_env):
    (tmp_git_repo / "docs" / "screenshots").mkdir(parents=True)
    (tmp_git_repo / "plan.png").write_bytes(b"\x89PNG")
    assert _check_paths(tmp_git_repo, "docs/screenshots/../plan.png").returncode != 0
    assert _check_paths(tmp_git_repo, "docs\\screenshots\\..\\plan.png").returncode != 0


def test_screenshots_nested_exempt(tmp_git_repo, isolated_env):
    (tmp_git_repo / "docs" / "screenshots" / "sub").mkdir(parents=True)
    (tmp_git_repo / "docs" / "screenshots" / "sub" / "a.png").write_bytes(b"\x89PNG")
    assert _check_paths(tmp_git_repo, "docs/screenshots/sub/a.png").returncode == 0
    assert _check_paths(tmp_git_repo, "./docs/screenshots/sub/a.png").returncode == 0


def test_screenshots_prefix_case_sensitive(tmp_git_repo, isolated_env):
    assert _check_paths(tmp_git_repo, "Docs/Screenshots/a.png").returncode != 0


def test_empty_leak_denylist_env_fails(tmp_git_repo, run_check_leaks, isolated_env, monkeypatch):
    monkeypatch.setenv("LANSCAPE_LEAK_DENYLIST", "")
    result = run_check_leaks(tmp_git_repo)
    assert result.returncode != 0
    assert "LANSCAPE_LEAK_DENYLIST" in result.stderr


# 128-bit UUIDs: only the documentation UUIDs and the Bluetooth base are allowed. Real-looking
# ones are built at runtime so this file stays clean.
REAL_UUID = str(uuid.UUID(int=0x1234_5678_9ABC_4DEF_8123_4567_89AB_CDEF))


@pytest.mark.parametrize("text", [REAL_UUID, REAL_UUID.upper(), f"ibeacon:{REAL_UUID}/1/2"])
def test_real_uuid_flagged(text):
    (leak,) = find_leaks(f"x = {text!r}", "x.py", set())
    assert "UUID" in leak


@pytest.mark.parametrize(
    "text",
    [
        "00000000-0000-4000-8000-0000000000ab",
        "00000000-0000-4000-8000-0000000000AB",
        "0000180f-0000-1000-8000-00805f9b34fb",
        "0000FEAA-0000-1000-8000-00805F9B34FB",
        "00000000-0000-4000-8000-000000000001/1/2",
    ],
)
def test_doc_and_bluetooth_base_uuids_allowed(text):
    assert find_leaks(f"x = {text!r}", "x.py", set()) == []


def test_near_doc_uuid_flagged():
    doc = "00000000-0000-4000-8000-"
    assert find_leaks(doc + "0000000001ab", "x.py", set())  # built at runtime: keeps
    assert find_leaks(doc + "1000000000ab", "x.py", set())  # this file clean


def test_uuid_flagged_in_ip_exempt_path():
    assert find_leaks(REAL_UUID, "docs/superpowers/specs/x.md", set())


# ---- Issue #48: IPv6 is checked, and an unreadable tracked file fails ---------------------


@pytest.mark.parametrize(
    "text", ["2001:db8::1", "2001:DB8:ffff::1", "2001:db8:0:0:0:0:0:1", "[2001:db8::1]:443"]
)
def test_ipv6_documentation_range_allowed(text):
    assert find_leaks(f"addr {text}", "x.py", set()) == []


@pytest.mark.parametrize(
    "text",
    [
        "2001:db9::1",  # just outside 2001:db8::/32
        "2001:4860:4860::8888",  # public
        "2600:1:2:3:4:5:6:7",  # public, full form with no ::
        "fd00::1",  # unique local
        "ip6:2001:4860:4860::8888",
        "addr 2001:4860:4860::8888.",  # sentence full stop
        "fe80::a2b3:c4ff:fed5:e6f7",  # link-local with an EUI-64 interface ID: a MAC
    ],
)
def test_non_documentation_ipv6_flagged(text):
    assert find_leaks(text, "x.py", set()), text


@pytest.mark.parametrize("text", ["::1", "::", "[::1]:8080", "fe80::1"])
def test_loopback_unspecified_and_plain_link_local_ipv6_clean(text):
    assert find_leaks(f"addr {text}", "x.py", set()) == [], text


@pytest.mark.parametrize(
    "text",
    [
        ".fp-item .fp-name::after",  # CSS pseudo-element
        "ancestor::div[contains(@class,'x')]",  # XPath axis
        "filterwarnings('ignore::PIL.Image.Warning')",
        "uuid:abc::upnp:rootdevice",
    ],
)
def test_word_adjacent_double_colon_is_not_ipv6(text):
    assert find_leaks(text, "x.py", set()) == [], text


def test_ipv6_findings_name_the_class():
    (leak,) = find_leaks("addr 2001:4860:4860::8888", "x.py", set())
    assert "IPv6" in leak and leak.startswith("x.py:1:")


def test_ip_false_positive_allow_list_is_path_scoped(monkeypatch):
    monkeypatch.setattr(
        check_leaks,
        "IP_FALSE_POSITIVES",
        (("docs/*.md", "8.8.8.8", "example in a doc"), ("x.py", "2001:4860:4860::8888", "t")),
    )
    assert find_leaks("v 8.8.8.8", "docs/a.md", set()) == []
    assert find_leaks("v 8.8.8.8", "src/a.py", set())
    assert find_leaks("v 8.8.4.4", "docs/a.md", set())
    assert find_leaks("addr 2001:4860:4860::8888", "x.py", set()) == []
    assert find_leaks("addr 2001:4860:4860::8888", "y.py", set())


def test_unreadable_explicit_path_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LANSCAPE_DENYLIST", raising=False)
    monkeypatch.delenv("LANSCAPE_LEAK_DENYLIST", raising=False)
    assert main([str(tmp_path / "nope.txt")]) == 1
    assert "cannot read file" in capsys.readouterr().out


def test_tracked_but_deleted_file_fails(tmp_git_repo, git_add, run_check_leaks, isolated_env):
    (tmp_git_repo / "gone.txt").write_text("clean\n")
    git_add(tmp_git_repo, "gone.txt")
    (tmp_git_repo / "gone.txt").unlink()
    result = run_check_leaks(tmp_git_repo)
    assert result.returncode != 0
    assert "gone.txt: cannot read file" in result.stdout
