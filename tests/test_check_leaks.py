import subprocess
import sys
from pathlib import Path

import pytest

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
