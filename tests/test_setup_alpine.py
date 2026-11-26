"""Tests for deploy/setup-alpine.sh: env parsing, config rendering and dry-run safety.

The script itself targets Alpine + OpenRC as root, so these tests only drive the parts that
run anywhere: ``--render-config`` (parse setup.env, render config.toml from
config.example.toml) and ``--dry-run`` (inspect and report, change nothing).
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import tomllib
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lanscape_atlas.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "setup-alpine.sh"

# A password that exercises TOML escaping: quote, backslash, dollar, hash, spaces, non-ASCII.
PASSWORD = 'p"a\\ss $word #1 é'
TOKEN = "t" * 40
HA_TOKEN = 'eyJ-ha"long\\lived$token #2'  # quote, backslash, dollar, hash
HA_ENV = {"HA_URL": "http://192.0.2.10:8123", "HA_TOKEN": HA_TOKEN}

BASE_ENV = {
    "REPO_URL": "git@github.com:example/lanscape-atlas.git",
    "ROUTER_HOST": "192.0.2.1",
    "ROUTER_USER": "admin",
    "ROUTER_PASSWORD": PASSWORD,
    "SCAN_INTERFACE": "eth0",
    "BIND_HOST": "192.0.2.50",
    "ALLOWED_HOSTS": "192.0.2.50:*, atlas.example.com",
}

SHELLS = [
    pytest.param(cmd, id=name)
    for name, cmd in (
        ("sh", ["sh"]),
        ("dash", ["dash"]),
        ("bash-posix", ["bash", "--posix"]),
        ("busybox-sh", ["busybox", "sh"]),  # Alpine's shell
    )
    if shutil.which(cmd[0])
]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")


def write_env(tmp_path: Path, data: dict[str, str], mode: int = 0o600) -> Path:
    path = tmp_path / "setup.env"
    path.write_text("# test env\n" + "".join(f"{k}={v}\n" for k, v in data.items()))
    path.chmod(mode)
    return path


def run(
    shell: list[str],
    *args: str,
    check: bool = False,
    env: dict[str, str] | None = None,
    script: Path = SCRIPT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*shell, str(script), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
        env=env,
    )


def render(
    shell: list[str],
    tmp_path: Path,
    data: dict[str, str],
    existing: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    out = tmp_path / "config.toml"
    args = ["--env", str(write_env(tmp_path, data)), "--render-config", str(out)]
    if existing is not None:
        args += ["--existing", str(existing)]
    return run(shell, *args), out


@pytest.mark.parametrize("shell", SHELLS)
def test_render_basic(shell: list[str], tmp_path: Path) -> None:
    proc, out = render(shell, tmp_path, BASE_ENV)
    assert proc.returncode == 0, proc.stderr
    cfg = tomllib.loads(out.read_text(encoding="utf-8"))
    assert cfg["data_dir"] == "/var/lib/lanscape-atlas"  # must match the init script
    assert cfg["bind_host"] == "192.0.2.50"
    assert cfg["bind_port"] == 8720
    assert cfg["scan_interface"] == "eth0"
    assert cfg["observation_retention_days"] == 3
    assert cfg["allowed_hosts"] == ["192.0.2.50:*", "atlas.example.com"]
    assert cfg["router"]["host"] == "192.0.2.1"
    assert cfg["router"]["username"] == "admin"
    assert cfg["router"]["password"] == PASSWORD
    assert cfg["router"]["use_ssl"] is True
    assert cfg["collectors"]["asusrouter"]["interval_s"] == 300
    assert re.fullmatch(r"[0-9a-f]{64}", cfg["api_token"])
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_render_loads_through_real_settings(tmp_path: Path) -> None:
    proc, out = render(["sh"], tmp_path, BASE_ENV)
    assert proc.returncode == 0, proc.stderr
    settings = load_settings(out, env={})
    assert settings.router is not None
    assert settings.router.password == PASSWORD
    assert settings.bind_host == "192.0.2.50"
    assert settings.api_token and len(settings.api_token) == 64
    assert settings.allowed_hosts == ("192.0.2.50:*", "atlas.example.com")
    assert settings.collectors["asusrouter"].interval_s == 300


def test_render_keeps_existing_token_and_env_overrides(tmp_path: Path) -> None:
    existing = tmp_path / "old.toml"
    existing.write_text('api_token = "' + "e" * 48 + '"\n')
    proc, out = render(["sh"], tmp_path, BASE_ENV, existing=existing)
    assert proc.returncode == 0, proc.stderr
    assert tomllib.loads(out.read_text())["api_token"] == "e" * 48

    proc, out = render(["sh"], tmp_path, {**BASE_ENV, "API_TOKEN": TOKEN}, existing=existing)
    assert proc.returncode == 0, proc.stderr
    assert tomllib.loads(out.read_text())["api_token"] == TOKEN


def test_render_without_router_disables_collector(tmp_path: Path) -> None:
    env = {k: v for k, v in BASE_ENV.items() if not k.startswith("ROUTER_")}
    proc, out = render(["sh"], tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert load_settings(out, env={}).router is None


def test_render_quoted_values_and_overrides(tmp_path: Path) -> None:
    env = {
        **BASE_ENV,
        "ROUTER_PASSWORD": "'  spaced  '",
        "BIND_PORT": "8721",
        "RETENTION_DAYS": "7",
        "ASUSROUTER_INTERVAL_S": "600",
        "ROUTER_USE_SSL": "false",
    }
    proc, out = render(["sh"], tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    cfg = tomllib.loads(out.read_text())
    assert cfg["router"]["password"] == "  spaced  "
    assert cfg["bind_port"] == 8721
    assert cfg["observation_retention_days"] == 7
    assert cfg["collectors"]["asusrouter"]["interval_s"] == 600
    assert cfg["router"]["use_ssl"] is False


@pytest.mark.parametrize("shell", SHELLS)
def test_render_with_home_assistant(shell: list[str], tmp_path: Path) -> None:
    proc, out = render(shell, tmp_path, {**BASE_ENV, **HA_ENV})
    assert proc.returncode == 0, proc.stderr
    cfg = tomllib.loads(out.read_text(encoding="utf-8"))
    assert cfg["home_assistant"] == {
        "url": "http://192.0.2.10:8123",
        "token": HA_TOKEN,
        "verify_tls": True,
    }
    assert cfg["bluetooth"]["retention_days"] == 14  # the rest of the example is intact
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    settings = load_settings(out, env={})  # the service has no LANSCAPE_HA_TOKEN
    assert settings.home_assistant is not None
    assert settings.home_assistant.url == "http://192.0.2.10:8123"
    assert settings.home_assistant.token == HA_TOKEN


def test_render_home_assistant_verify_tls_false(tmp_path: Path) -> None:
    env = {**BASE_ENV, **HA_ENV, "HA_URL": "https://ha.example:8123", "HA_VERIFY_TLS": "false"}
    proc, out = render(["sh"], tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    ha = load_settings(out, env={}).home_assistant
    assert ha is not None and ha.url == "https://ha.example:8123" and ha.verify_tls is False


def test_render_without_home_assistant_leaves_it_off(tmp_path: Path) -> None:
    proc, out = render(["sh"], tmp_path, BASE_ENV)
    assert proc.returncode == 0, proc.stderr
    assert "home_assistant" not in tomllib.loads(out.read_text())
    assert load_settings(out, env={}).home_assistant is None


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("value", ["-70", "-127", "0", "-0", "-090"])
def test_render_ble_candidate_min_rssi(shell: list[str], value: str, tmp_path: Path) -> None:
    env = {**BASE_ENV, **HA_ENV, "BLE_CANDIDATE_MIN_RSSI": value}
    proc, out = render(shell, tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    cfg = tomllib.loads(out.read_text(encoding="utf-8"))
    assert cfg["bluetooth"]["candidate_min_rssi"] == int(value)
    assert cfg["bluetooth"]["retention_days"] == 14  # the rest of [bluetooth] is intact
    assert load_settings(out, env={}).bluetooth.candidate_min_rssi == int(value)


def test_render_without_ble_candidate_min_rssi_keeps_the_default(tmp_path: Path) -> None:
    proc, out = render(["sh"], tmp_path, BASE_ENV)
    assert proc.returncode == 0, proc.stderr
    assert tomllib.loads(out.read_text())["bluetooth"]["candidate_min_rssi"] == -80
    proc, out = render(["sh"], tmp_path, {**BASE_ENV, "BLE_CANDIDATE_MIN_RSSI": ""})
    assert proc.returncode == 0, proc.stderr
    assert tomllib.loads(out.read_text())["bluetooth"]["candidate_min_rssi"] == -80


def test_setup_env_example_documents_ble_candidate_min_rssi() -> None:
    text = (ROOT / "deploy" / "setup.env.example").read_text()
    assert re.search(r"^# BLE_CANDIDATE_MIN_RSSI=-80$", text, re.M)
    block = text[: text.index("# BLE_CANDIDATE_MIN_RSSI=")].rsplit("\n\n", 1)[-1]
    assert "-80" in block and "-127" in block and "0" in block


def test_setup_env_example_documents_home_assistant() -> None:
    text = (ROOT / "deploy" / "setup.env.example").read_text()
    for key in ("HA_URL", "HA_TOKEN", "HA_VERIFY_TLS"):
        assert re.search(rf"^#?\s*{key}=", text, re.M), key


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"NOT_A_KEY": "x"}, "unknown key"),
        ({**HA_ENV, "HA_TOKEN": ""}, "HA_TOKEN"),
        ({"HA_TOKEN": HA_TOKEN}, "HA_URL"),
        ({**HA_ENV, "HA_URL": "192.0.2.10:8123"}, "HA_URL"),
        ({**HA_ENV, "HA_URL": "http://192.0.2.10 :8123"}, "HA_URL"),
        ({**HA_ENV, "HA_VERIFY_TLS": "yes"}, "HA_VERIFY_TLS"),
        ({"BLE_CANDIDATE_MIN_RSSI": "1"}, "BLE_CANDIDATE_MIN_RSSI"),
        ({"BLE_CANDIDATE_MIN_RSSI": "-128"}, "BLE_CANDIDATE_MIN_RSSI"),
        ({"BLE_CANDIDATE_MIN_RSSI": "80"}, "BLE_CANDIDATE_MIN_RSSI"),
        ({"BLE_CANDIDATE_MIN_RSSI": "-8O"}, "BLE_CANDIDATE_MIN_RSSI"),
        ({"BLE_CANDIDATE_MIN_RSSI": "--80"}, "BLE_CANDIDATE_MIN_RSSI"),
        ({"BLE_CANDIDATE_MIN_RSSI": "-"}, "BLE_CANDIDATE_MIN_RSSI"),
        ({"BLE_CANDIDATE_MIN_RSSI": "-80.5"}, "BLE_CANDIDATE_MIN_RSSI"),
        ({"BIND_PORT": "80x"}, "BIND_PORT"),
        ({"ASUSROUTER_INTERVAL_S": "0"}, "ASUSROUTER_INTERVAL_S"),
        ({"API_TOKEN": "short"}, "API_TOKEN"),
        ({"REF": "--upload-pack=x"}, "REF"),
        ({"REPO_URL": "file:///tmp/x"}, "REPO_URL"),
        ({"ROUTER_USE_SSL": "maybe"}, "ROUTER_USE_SSL"),
        ({"ROUTER_PASSWORD": ""}, "ROUTER_PASSWORD"),
        ({"UPDATE_OUI_WEEKLY": "no"}, "UPDATE_OUI_WEEKLY"),
    ],
)
def test_env_validation_rejects(change: dict[str, str], needle: str, tmp_path: Path) -> None:
    proc, out = render(["sh"], tmp_path, {**BASE_ENV, **change})
    assert proc.returncode != 0
    assert needle in proc.stderr
    assert not out.exists()


@pytest.mark.parametrize("missing", ["REPO_URL", "SCAN_INTERFACE", "BIND_HOST"])
def test_env_requires_keys(missing: str, tmp_path: Path) -> None:
    env = {k: v for k, v in BASE_ENV.items() if k != missing}
    proc, _ = render(["sh"], tmp_path, env)
    assert proc.returncode != 0
    assert missing in proc.stderr


def test_env_file_must_be_0600(tmp_path: Path) -> None:
    out = tmp_path / "config.toml"
    env_file = write_env(tmp_path, BASE_ENV, mode=0o644)
    proc = run(["sh"], "--env", str(env_file), "--render-config", str(out))
    assert proc.returncode != 0
    assert "0600" in proc.stderr
    assert not out.exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_secrets_never_printed(shell: list[str], tmp_path: Path) -> None:
    env = {**BASE_ENV, **HA_ENV, "API_TOKEN": TOKEN}
    env_file = write_env(tmp_path, env)
    out = tmp_path / "config.toml"
    results = [
        run(shell, "--env", str(env_file), "--render-config", str(out)),
        run(shell, "--env", str(env_file), "--dry-run"),
        # A broken file must not echo the offending line either.
        run(shell, "--env", str(write_env(tmp_path, {**env, "BIND_PORT": PASSWORD})), "--dry-run"),
    ]
    for proc in results:
        combined = proc.stdout + proc.stderr
        assert PASSWORD not in combined
        assert TOKEN not in combined
        assert HA_TOKEN not in combined


@pytest.mark.parametrize("shell", SHELLS)
def test_dry_run_changes_nothing(shell: list[str], tmp_path: Path) -> None:
    env_file = write_env(tmp_path, BASE_ENV)
    before = sorted(os.listdir(tmp_path))
    proc = run(shell, "--env", str(env_file), "--dry-run")
    # Exit 1 is expected off-Alpine or off-root: problems are reported, not fatal mid-run.
    assert proc.returncode in (0, 1), proc.stderr
    assert "dry-run" in proc.stdout
    assert "Traceback" not in proc.stdout + proc.stderr
    assert sorted(os.listdir(tmp_path)) == before


def test_dry_run_reports_missing_env_file(tmp_path: Path) -> None:
    proc = run(["sh"], "--env", str(tmp_path / "nope.env"), "--dry-run")
    assert proc.returncode == 1
    assert "env file not found" in proc.stderr


def test_help_and_unknown_flag() -> None:
    assert run(["sh"], "--help").returncode == 0
    bad = run(["sh"], "--frobnicate")
    assert bad.returncode != 0
    assert "unknown argument" in bad.stderr


def test_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_shellcheck_clean() -> None:
    if not shutil.which("shellcheck"):
        # CI installs shellcheck; there a missing binary is a failure, not a skip.
        if os.environ.get("CI"):
            pytest.fail("shellcheck is not installed but CI is set")
        pytest.skip("shellcheck not installed")
    proc = subprocess.run(
        ["shellcheck", "--shell=sh", str(SCRIPT)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stdout


def test_deploy_script_creates_floorplan_dir() -> None:
    text = SCRIPT.read_text()
    pattern = r'service_dir "\$DATA_DIR/floorplan" 0750'
    assert re.search(pattern, text)


@pytest.mark.parametrize("shell", SHELLS)
def test_dry_run_lists_floorplan_dir(shell: list[str], tmp_path: Path) -> None:
    proc = run(shell, "--env", str(write_env(tmp_path, BASE_ENV)), "--dry-run")
    assert "floorplan" in proc.stdout + proc.stderr
    assert "0750" in proc.stdout + proc.stderr


# ------------------------------------------------- hand edits, checksum and backups
#
# ``--write-config FILE`` (internal) runs the real-run config step against FILE: render,
# hand-edit check, timestamped backup and replace (no chown, no app load check).

CONF_HEADER = (
    "# Generated by deploy/setup-alpine.sh from setup.env;"
    " hand edits stop the installer (see README)."
)
OLD_HEADER = "# Generated by deploy/setup-alpine.sh from setup.env; edits are overwritten."
COVERED = "config.toml had hand edits that setup.env now covers; replaced"
BACKUP_RE = re.compile(r"config\.toml\.\d{8}-\d{6}(-\d+)?")
HAND_SECRET = "hand-typed-secret-0123456789"
HAND_HA = (
    '\n[home_assistant]\nurl = "https://ha.example:8123"\ntoken = "'
    + HAND_SECRET
    + '"\nverify_tls = true\n'
)


def conf_path(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return data / "config.toml"


def write_conf(
    shell: list[str], tmp_path: Path, data: dict[str, str], conf: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    env_file = write_env(tmp_path, data)
    return run(shell, "--env", str(env_file), "--write-config", str(conf), *extra)


def backups(conf: Path) -> list[Path]:
    return sorted(p for p in conf.parent.iterdir() if BACKUP_RE.fullmatch(p.name))


def strip_checksum(text: str) -> str:
    lines = text.split("\n")
    assert lines[1].startswith("# sha256: ")
    return "\n".join([lines[0], *lines[2:]])


def assert_no_secrets(proc: subprocess.CompletedProcess[str], *extra: str) -> None:
    combined = proc.stdout + proc.stderr
    for secret in (PASSWORD, TOKEN, HA_TOKEN, HAND_SECRET, *extra):
        assert secret not in combined


def listed(output: str, key: str, env_key: str | None) -> bool:
    hint = f"({env_key})" if env_key else "(no setup.env key)"
    return re.search(rf"^\s*{re.escape(key)}\s+{re.escape(hint)}$", output, re.M) is not None


@pytest.mark.parametrize("shell", SHELLS)
def test_render_writes_checksum_line(shell: list[str], tmp_path: Path) -> None:
    proc, out = render(shell, tmp_path, {**BASE_ENV, **HA_ENV})
    assert proc.returncode == 0, proc.stderr
    raw = out.read_bytes()
    header, sum_line, body = raw.split(b"\n", 2)
    assert header.decode() == CONF_HEADER
    match = re.fullmatch(rb"# sha256: ([0-9a-f]{64})", sum_line)
    assert match is not None
    assert match.group(1).decode() == hashlib.sha256(body).hexdigest()
    assert tomllib.loads(raw.decode())["home_assistant"]["token"] == HA_TOKEN


@pytest.mark.parametrize("shell", SHELLS)
def test_write_config_same_inputs_is_byte_identical_without_backup(
    shell: list[str], tmp_path: Path
) -> None:
    conf = conf_path(tmp_path)
    first = write_conf(shell, tmp_path, BASE_ENV, conf)
    assert first.returncode == 0, first.stderr
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600
    before = conf.read_bytes()
    second = write_conf(shell, tmp_path, BASE_ENV, conf)  # the generated token is kept
    assert second.returncode == 0, second.stderr
    assert "unchanged" in second.stdout
    assert conf.read_bytes() == before
    assert os.listdir(conf.parent) == ["config.toml"]  # no backup, no temp file left


@pytest.mark.parametrize("shell", SHELLS)
def test_write_config_follows_setup_env_changes(shell: list[str], tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    old = conf.read_bytes()
    env = {**BASE_ENV, **HA_ENV, "ROUTER_PASSWORD": "new-router-secret-42"}
    proc = write_conf(shell, tmp_path, env, conf)
    assert proc.returncode == 0, proc.stderr
    cfg = tomllib.loads(conf.read_text())
    assert cfg["home_assistant"]["token"] == HA_TOKEN
    assert cfg["router"]["password"] == "new-router-secret-42"
    assert cfg["api_token"] == tomllib.loads(old.decode())["api_token"]
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600
    (backup,) = backups(conf)
    assert backup.read_bytes() == old
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert_no_secrets(proc, "new-router-secret-42")


HAND_EDITS = {
    "add_home_assistant": (
        lambda t: t + HAND_HA,
        [
            ("home_assistant.url", "HA_URL"),
            ("home_assistant.token", "HA_TOKEN"),
            ("home_assistant.verify_tls", "HA_VERIFY_TLS"),
        ],
    ),
    "change_unmanaged_value": (
        lambda t: re.sub(r"(?m)^retention_days = 14$", "retention_days = 30", t),
        [("bluetooth.retention_days", None)],
    ),
    "change_router_password": (
        lambda t: re.sub(r"(?m)^password = .*$", f'password = "{HAND_SECRET}"', t),
        [("router.password", "ROUTER_PASSWORD")],
    ),
}


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("edit", sorted(HAND_EDITS))
def test_write_config_stops_on_hand_edit(shell: list[str], edit: str, tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    change, expected = HAND_EDITS[edit]
    text = conf.read_text()
    conf.write_text(change(text))
    edited = conf.read_bytes()
    assert edited != text.encode()
    proc = write_conf(shell, tmp_path, BASE_ENV, conf)
    assert proc.returncode != 0
    assert "config.toml was edited by hand" in proc.stderr
    assert "--discard-config-edits" in proc.stderr
    for key, env_key in expected:
        assert listed(proc.stderr, key, env_key), proc.stderr
    assert "bind_host" not in proc.stderr  # only the keys that differ
    assert conf.read_bytes() == edited
    assert os.listdir(conf.parent) == ["config.toml"]
    assert_no_secrets(proc)


def test_comment_only_edit_is_replaced_with_a_backup(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    rendered = conf.read_bytes()
    conf.write_text(conf.read_text() + "# a note of mine\n")
    edited = conf.read_bytes()
    proc = write_conf(["sh"], tmp_path, BASE_ENV, conf)
    assert proc.returncode == 0, proc.stderr
    assert COVERED in proc.stdout
    assert conf.read_bytes() == rendered
    (backup,) = backups(conf)
    assert backup.read_bytes() == edited


@pytest.mark.parametrize("shell", SHELLS)
def test_hand_edits_that_setup_env_now_covers_are_replaced(
    shell: list[str], tmp_path: Path
) -> None:
    # The hand-added table, then the same values put in setup.env: no stop loop.
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    ha = {"HA_URL": "https://ha.example:8123", "HA_TOKEN": HAND_SECRET, "HA_VERIFY_TLS": "true"}
    conf.write_text(conf.read_text() + HAND_HA)
    edited = conf.read_bytes()
    env = {**BASE_ENV, **ha}
    early = check_conf(shell, tmp_path, env, conf)
    assert early.returncode == 0, early.stderr
    proc = write_conf(shell, tmp_path, env, conf)
    assert proc.returncode == 0, proc.stderr
    assert COVERED in proc.stdout
    cfg = tomllib.loads(conf.read_text())
    assert cfg["home_assistant"]["token"] == HAND_SECRET
    assert conf.read_text().split("\n")[1].startswith("# sha256: ")
    (backup,) = backups(conf)
    assert backup.read_bytes() == edited
    again = write_conf(shell, tmp_path, env, conf)
    assert again.returncode == 0 and "unchanged" in again.stdout
    for p in (early, proc, again):
        assert_no_secrets(p)


def test_hand_edit_that_adds_nothing_setup_env_sets_still_stops(tmp_path: Path) -> None:
    # Equal in one direction only (the render adds a key the file lacks): still a stop.
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    conf.write_text(re.sub(r"(?m)^snapshot_s = 30\n", "", conf.read_text()))
    proc = write_conf(["sh"], tmp_path, BASE_ENV, conf)
    assert proc.returncode != 0
    assert "config.toml was edited by hand" in proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_reworded_header_is_not_a_hand_edit(shell: list[str], tmp_path: Path) -> None:
    # A file from a script with other line-1 wording keeps a valid checksum.
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    text = conf.read_text()
    old = text.replace(CONF_HEADER, OLD_HEADER, 1)
    assert old != text
    conf.write_text(old)
    early = check_conf(shell, tmp_path, BASE_ENV, conf)
    assert early.returncode == 0 and "no hand edits" in early.stdout, early.stderr
    proc = write_conf(shell, tmp_path, BASE_ENV, conf)
    assert proc.returncode == 0, proc.stderr
    assert COVERED not in proc.stdout
    assert conf.read_text() == text


@pytest.mark.parametrize("shell", SHELLS)
def test_legacy_config_with_hand_added_home_assistant_stops(
    shell: list[str], tmp_path: Path
) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    conf.write_text(strip_checksum(conf.read_text()) + HAND_HA)
    edited = conf.read_bytes()
    proc = write_conf(shell, tmp_path, BASE_ENV, conf)  # HA_URL empty
    assert proc.returncode != 0
    assert "config.toml was edited by hand" in proc.stderr
    assert listed(proc.stderr, "home_assistant.url", "HA_URL"), proc.stderr
    assert listed(proc.stderr, "home_assistant.token", "HA_TOKEN"), proc.stderr
    assert conf.read_bytes() == edited
    assert os.listdir(conf.parent) == ["config.toml"]
    assert_no_secrets(proc)


def test_legacy_check_through_render_hook(tmp_path: Path) -> None:
    proc, out = render(["sh"], tmp_path, BASE_ENV)
    assert proc.returncode == 0, proc.stderr
    existing = tmp_path / "old.toml"
    existing.write_text(strip_checksum(out.read_text()) + HAND_HA)
    out.unlink()
    proc, out = render(["sh"], tmp_path, BASE_ENV, existing=existing)
    assert proc.returncode != 0
    assert "config.toml was edited by hand" in proc.stderr
    assert listed(proc.stderr, "home_assistant.url", "HA_URL"), proc.stderr
    assert not out.exists()
    assert_no_secrets(proc)


@pytest.mark.parametrize("shell", SHELLS)
def test_legacy_config_matching_the_render_is_replaced(shell: list[str], tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    rendered = conf.read_bytes()
    conf.write_text(strip_checksum(rendered.decode()))
    legacy = conf.read_bytes()
    proc = write_conf(shell, tmp_path, BASE_ENV, conf)
    assert proc.returncode == 0, proc.stderr
    assert conf.read_bytes() == rendered
    (backup,) = backups(conf)
    assert backup.read_bytes() == legacy


@pytest.mark.parametrize(
    ("first", "edit", "second", "stops", "key"),
    [
        # A setup.env key changed since the legacy file was rendered: not a hand edit.
        (BASE_ENV, None, {**BASE_ENV, "RETENTION_DAYS": "7"}, False, None),
        (BASE_ENV, None, {**BASE_ENV, **HA_ENV}, False, None),
        # A value setup.env does not set differs: a hand edit...
        (
            BASE_ENV,
            ("candidate_min_rssi = -80", "candidate_min_rssi = -70"),
            BASE_ENV,
            True,
            "bluetooth.candidate_min_rssi",
        ),
        # ...unless setup.env now sets that value.
        (
            BASE_ENV,
            ("candidate_min_rssi = -80", "candidate_min_rssi = -70"),
            {**BASE_ENV, "BLE_CANDIDATE_MIN_RSSI": "-70"},
            False,
            None,
        ),
        # setup.env "sets" a key only with a non-empty value: the script's default does not.
        (BASE_ENV, ("bind_port = 8720", "bind_port = 8721"), BASE_ENV, True, "bind_port"),
        (
            BASE_ENV,
            ("bind_port = 8720", "bind_port = 8721"),
            {**BASE_ENV, "BIND_PORT": "8721"},
            False,
            None,
        ),
        (
            BASE_ENV,
            ("bind_port = 8720", "bind_port = 8721"),
            {**BASE_ENV, "BIND_PORT": "8722"},
            False,
            None,
        ),
        # A key the new render would drop: a hand edit, even though setup.env controls it.
        (
            BASE_ENV,
            None,
            {k: v for k, v in BASE_ENV.items() if k != "ALLOWED_HOSTS"},
            True,
            "allowed_hosts",
        ),
    ],
)
def test_legacy_config_rule(
    first: dict[str, str],
    edit: tuple[str, str] | None,
    second: dict[str, str],
    stops: bool,
    key: str | None,
    tmp_path: Path,
) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, first, conf).returncode == 0
    legacy = strip_checksum(conf.read_text())
    if edit is not None:
        assert edit[0] in legacy
        legacy = legacy.replace(edit[0], edit[1])
    conf.write_text(legacy)
    proc = write_conf(["sh"], tmp_path, second, conf)
    if stops:
        assert proc.returncode != 0
        assert "config.toml was edited by hand" in proc.stderr
        assert key is not None and key in proc.stderr
        assert conf.read_text() == legacy
    else:
        assert proc.returncode == 0, proc.stderr
        assert conf.read_text().split("\n")[1].startswith("# sha256: ")
    assert_no_secrets(proc)


@pytest.mark.parametrize("shell", SHELLS)
def test_discard_config_edits_replaces_and_backs_up(shell: list[str], tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    conf.write_text(conf.read_text() + HAND_HA)
    edited = conf.read_bytes()
    proc = write_conf(shell, tmp_path, BASE_ENV, conf, "--discard-config-edits")
    assert proc.returncode == 0, proc.stderr
    assert "home_assistant" not in tomllib.loads(conf.read_text())
    assert "home_assistant.url" in proc.stdout  # what was discarded, by name
    (backup,) = backups(conf)
    assert backup.read_bytes() == edited
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert_no_secrets(proc)


def test_config_backups_keep_newest_five_and_leave_prev_alone(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    old = [f"config.toml.2020010{day}-000000" for day in range(1, 7)]
    for name in old:
        (conf.parent / name).write_text("old\n")
    prev = conf.parent / "config.toml.prev"
    prev.write_text("prev\n")
    proc = write_conf(["sh"], tmp_path, {**BASE_ENV, "RETENTION_DAYS": "7"}, conf)
    assert proc.returncode == 0, proc.stderr
    names = [p.name for p in backups(conf)]
    assert len(names) == 5
    assert names[:4] == old[2:]
    assert not names[4].startswith("config.toml.2020")
    assert prev.read_text() == "prev\n"


def test_config_backup_rotation_orders_suffixes_and_ignores_other_names(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    ours = [
        "config.toml.20200101-000000",
        "config.toml.20200101-000000-1",
        "config.toml.20200101-000000-2",
        "config.toml.20200101-000000-10",
        "config.toml.20200102-000000",
        "config.toml.20200103-000000",
        "config.toml.20200104-000000",
    ]
    others = [
        "config.toml.20261111-120000.orig",
        "config.toml.20200101-000000-x",
        "config.toml.20200101-000000-1-2",
        "config.toml.2020010-000000",
        "config.toml.prev",
    ]
    for name in ours + others:
        (conf.parent / name).write_text(name + "\n")
    proc = write_conf(["sh"], tmp_path, {**BASE_ENV, "RETENTION_DAYS": "7"}, conf)
    assert proc.returncode == 0, proc.stderr
    left = {p.name for p in conf.parent.iterdir()}
    # 7 old + 1 new = 8; the 3 oldest go, and "-10" is newer than "-2".
    assert left >= {
        "config.toml.20200101-000000-10",
        "config.toml.20200102-000000",
        "config.toml.20200103-000000",
        "config.toml.20200104-000000",
    }
    assert not left & set(ours[:3])
    for name in others:
        assert (conf.parent / name).read_text() == name + "\n"
    assert len([n for n in left if BACKUP_RE.fullmatch(n)]) == 5


def test_config_backup_never_writes_through_a_symlink(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    old = conf.read_bytes()
    target = tmp_path / "outside" / "stolen.toml"
    target.parent.mkdir()
    now = datetime.now(UTC)
    planted = []
    for delta in range(-1, 4):  # the second the backup is taken in, whatever it is
        stamp = (now + timedelta(seconds=delta)).strftime("%Y%m%d-%H%M%S")
        link = conf.parent / f"config.toml.{stamp}"
        link.symlink_to(target)  # dangling
        planted.append(link)
    proc = write_conf(["sh"], tmp_path, {**BASE_ENV, "RETENTION_DAYS": "7"}, conf)
    assert proc.returncode == 0, proc.stderr
    assert not target.exists()
    real = [p for p in backups(conf) if not p.is_symlink()]
    assert len(real) == 1
    assert real[0].read_bytes() == old
    assert stat.S_IMODE(real[0].stat().st_mode) == 0o600
    assert not [p for p in conf.parent.iterdir() if p.name.startswith(".config")]


def test_config_backups_in_the_same_second_are_all_kept(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    for days in ("7", "8", "9"):
        env = {**BASE_ENV, "RETENTION_DAYS": days}
        assert write_conf(["sh"], tmp_path, env, conf).returncode == 0
    contents = {p.read_text() for p in backups(conf)}
    assert len(contents) == 3


@pytest.mark.parametrize("shell", SHELLS)
def test_dry_run_reports_hand_edits_without_writing(shell: list[str], tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    conf.write_text(conf.read_text() + HAND_HA)
    edited = conf.read_bytes()
    proc = write_conf(shell, tmp_path, {**BASE_ENV, **HA_ENV}, conf, "--dry-run")
    assert proc.returncode == 1
    assert "PROBLEM" in proc.stderr
    assert "config.toml was edited by hand" in proc.stderr
    assert listed(proc.stderr, "home_assistant.token", "HA_TOKEN"), proc.stderr
    assert conf.read_bytes() == edited
    assert os.listdir(conf.parent) == ["config.toml"]
    assert_no_secrets(proc)


def test_dry_run_reports_a_pending_replace_without_writing(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    before = conf.read_bytes()
    proc = write_conf(["sh"], tmp_path, {**BASE_ENV, **HA_ENV}, conf, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would back up" in proc.stdout
    assert conf.read_bytes() == before
    assert os.listdir(conf.parent) == ["config.toml"]
    assert_no_secrets(proc)


def test_help_mentions_discard_config_edits() -> None:
    assert "--discard-config-edits" in run(["sh"], "--help").stdout


def test_setup_env_example_explains_home_assistant_tls() -> None:
    text = (ROOT / "deploy" / "setup.env.example").read_text()
    block = text[: text.index("# HA_URL=")].rsplit("\n\n", 1)[-1]
    assert "https://" in block
    assert "certificate" in block
    assert "Server disconnected" in block


def test_readme_documents_config_hand_edits_and_backups() -> None:
    text = (ROOT / "README.md").read_text()
    assert "--discard-config-edits" in text
    assert "config.toml.YYYYmmdd-HHMMSS" in text


# ------------------------------------- early check and restart after a hand-edit stop
#
# ``--check-config FILE`` (internal) runs the early check (before the service is stopped)
# against FILE. rc-service is faked by a script first on PATH; LANSCAPE_SETUP_STOPPED=1 is
# how the script before a re-exec hands over "I stopped the service".

RESTARTED = "service restarted with your existing config.toml; nothing was overwritten"


def check_conf(
    shell: list[str],
    tmp_path: Path,
    data: dict[str, str],
    conf: Path,
    *extra: str,
    env: dict[str, str] | None = None,
    script: Path = SCRIPT,
) -> subprocess.CompletedProcess[str]:
    env_file = write_env(tmp_path, data)
    args = ["--env", str(env_file), "--check-config", str(conf), *extra]
    return run(shell, *args, env=env, script=script)


def fake_rc_service(tmp_path: Path, status: int = 0, stopped: bool = True) -> dict[str, str]:
    """Environment with a fake rc-service first on PATH that logs its arguments."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    fake = fakebin / "rc-service"
    fake.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$FAKE_RC_LOG"\nexit {status}\n')
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RC_LOG": str(tmp_path / "rc-service.log"),
    }
    if stopped:
        env["LANSCAPE_SETUP_STOPPED"] = "1"
    return env


def rc_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "rc-service.log"
    return log.read_text().splitlines() if log.exists() else []


def hand_edited_conf(shell: list[str], tmp_path: Path, legacy: bool = False) -> Path:
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    text = conf.read_text()
    conf.write_text((strip_checksum(text) if legacy else text) + HAND_HA)
    return conf


def test_main_flow_checks_config_before_stopping_the_service() -> None:
    main = SCRIPT.read_text().split(
        "# --------------------------------------------------------------------- main", 1
    )[1]
    calls = [line.strip() for line in main.splitlines() if re.fullmatch(r"[a-z_]+", line.strip())]
    assert (
        calls.index("preflight") < calls.index("check_config_early") < calls.index("stop_service")
    )
    assert (
        calls.index("reexec_if_updated")
        < calls.index("check_config_after_checkout")
        < calls.index("install_app")
        < calls.index("backup_db")
        < calls.index("write_config")
    )


def test_main_flow_does_network_and_build_steps_before_stopping_the_service() -> None:
    main = SCRIPT.read_text().split(MAIN_MARKER, 1)[1]
    words = [line.strip() for line in main.splitlines() if re.fullmatch(r"[a-z_]+", line.strip())]
    calls = [word for word in words if word not in ("fi", "else")]
    assert calls == STEPS  # the stubbed main sequence below covers every step
    order = [
        "acquire_lock",  # before anything is installed or changed
        "install_packages",
        "ensure_repo_access",
        "checkout_code",  # git fetch
        "reexec_if_updated",
        "check_config_after_checkout",
        "build_wheels",  # pip downloads and builds
        "stop_service",
        "install_app",
        "backup_db",
        "write_config",
        "migrate_db",
        "install_service_files",
        "start_service",
        "update_oui",  # IEEE download
        "smoke_test",
    ]
    assert [name for name in calls if name in order] == order


def function_body(name: str) -> str:
    text = SCRIPT.read_text()
    body = text[text.index(f"\n{name}() {{\n") :]
    return body[: body.index("\n}\n")]


def test_pip_touches_the_network_only_before_the_stop() -> None:
    wheels = function_body("build_wheels")
    assert '/bin/pip" wheel ' in wheels
    assert "--upgrade" not in wheels
    install = function_body("install_app")
    commands = install.split('"$VENV/bin/pip" install')[1:]
    assert len(commands) == 2
    for command in commands:
        assert "--no-index" in command
        assert "--upgrade" not in command
        assert '"$SRC"' not in command  # a source tree would need build tools from PyPI
    assert "--find-links" in commands[0]
    assert "--force-reinstall --no-deps" in commands[1]


def test_update_oui_runs_after_start_and_is_never_fatal() -> None:
    migrate = function_body("migrate_db")
    assert "update-oui" not in migrate
    # Before the start only when there is no vendor list at all (first install).
    first_install = r'if \[ ! -f "\$DATA_DIR/oui\.csv" \]; then\n(\t\t#.*\n)*\t\tfetch_oui'
    assert re.search(first_install, migrate)
    assert migrate.count("fetch_oui") == 1
    fetch = function_body("fetch_oui")
    assert "if run_as_service_user update-oui >/dev/null 2>&1; then" in fetch
    assert "die" not in fetch and "exit" not in fetch
    assert "fetch_oui" in function_body("update_oui")


@pytest.mark.parametrize("shell", SHELLS)
def test_early_check_stops_on_hand_edited_checksummed_config(
    shell: list[str], tmp_path: Path
) -> None:
    conf = hand_edited_conf(shell, tmp_path)
    edited = conf.read_bytes()
    proc = check_conf(shell, tmp_path, BASE_ENV, conf)
    assert proc.returncode != 0
    assert "config.toml was edited by hand" in proc.stderr
    assert listed(proc.stderr, "home_assistant.url", "HA_URL"), proc.stderr
    assert conf.read_bytes() == edited
    assert os.listdir(conf.parent) == ["config.toml"]
    assert_no_secrets(proc)


@pytest.mark.parametrize("kind", ["clean", "legacy_hand_edited", "absent"])
def test_early_check_passes_what_it_cannot_or_need_not_stop(kind: str, tmp_path: Path) -> None:
    if kind == "legacy_hand_edited":  # needs the new render: write_config checks it later
        conf = hand_edited_conf(["sh"], tmp_path, legacy=True)
    else:
        conf = conf_path(tmp_path)
        if kind == "clean":
            assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    before = conf.read_bytes() if conf.exists() else None
    proc = check_conf(["sh"], tmp_path, BASE_ENV, conf)
    assert proc.returncode == 0, proc.stderr
    assert (conf.read_bytes() if conf.exists() else None) == before


def test_early_check_with_discard_does_not_stop(tmp_path: Path) -> None:
    conf = hand_edited_conf(["sh"], tmp_path)
    proc = check_conf(["sh"], tmp_path, BASE_ENV, conf, "--discard-config-edits")
    assert proc.returncode == 0, proc.stderr
    assert_no_secrets(proc)


def test_early_check_without_an_example_stops_without_names(tmp_path: Path) -> None:
    conf = hand_edited_conf(["sh"], tmp_path)
    bare = tmp_path / "bare" / "deploy"
    bare.mkdir(parents=True)
    script = bare / "setup-alpine.sh"
    shutil.copy(SCRIPT, script)  # no ../config.example.toml next to this copy
    proc = check_conf(["sh"], tmp_path, BASE_ENV, conf, script=script)
    assert proc.returncode != 0
    assert "settings changed by hand" in proc.stderr
    assert "config.toml was edited by hand" in proc.stderr
    assert "home_assistant" not in proc.stderr
    assert_no_secrets(proc)


def test_early_check_dry_run_reports_without_writing(tmp_path: Path) -> None:
    conf = hand_edited_conf(["sh"], tmp_path)
    edited = conf.read_bytes()
    proc = check_conf(["sh"], tmp_path, BASE_ENV, conf, "--dry-run")
    assert proc.returncode == 1
    assert "PROBLEM" in proc.stderr and "config.toml was edited by hand" in proc.stderr
    assert conf.read_bytes() == edited


TAMPERINGS = {
    "clean": lambda t: t,
    "comment_added": lambda t: t + "# mine\n",
    "value_changed": lambda t: t.replace("bind_port = 8720", "bind_port = 8721"),
    "sum_line_tampered": lambda t: t.replace("# sha256: ", "# sha256: 0", 1),
    "header_reworded": lambda t: t.replace(CONF_HEADER, OLD_HEADER, 1),
    "header_foreign": lambda t: t.replace(CONF_HEADER, "# written by me", 1),
    "no_final_newline": lambda t: t.rstrip("\n"),
    "crlf": lambda t: t.replace("\n", "\r\n"),
}


@pytest.mark.parametrize("tamper", sorted(TAMPERINGS))
def test_early_and_late_checks_agree(tamper: str, tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    conf.write_bytes(TAMPERINGS[tamper](conf.read_text()).encode())
    early = check_conf(["sh"], tmp_path, BASE_ENV, conf)
    late, _ = render(["sh"], tmp_path, BASE_ENV, existing=conf)
    # Same checksum verdict (shell vs python)...
    early_ok = "config.toml has no hand edits" in early.stdout
    late_ok = early.returncode == 0 and COVERED not in late.stdout
    assert early_ok == late_ok == (tamper in ("clean", "header_reworded")), (early, late)
    # ...and the same stop verdict: only a setting that changes stops the run.
    assert (early.returncode == 0) == (late.returncode == 0), (early.stderr, late.stderr)
    assert (early.returncode != 0) == (tamper == "value_changed")


@pytest.mark.parametrize("shell", SHELLS)
def test_early_stop_restarts_a_service_stopped_before_the_reexec(
    shell: list[str], tmp_path: Path
) -> None:
    conf = hand_edited_conf(shell, tmp_path)
    edited = conf.read_bytes()
    proc = check_conf(shell, tmp_path, BASE_ENV, conf, env=fake_rc_service(tmp_path))
    assert proc.returncode != 0
    assert rc_calls(tmp_path) == ["lanscape-atlas start"]
    assert RESTARTED in proc.stdout + proc.stderr
    assert "config.toml was edited by hand" in proc.stderr
    assert "NOT restarted" not in proc.stderr
    assert conf.read_bytes() == edited
    assert_no_secrets(proc)


@pytest.mark.parametrize("shell", SHELLS)
def test_late_stop_restarts_the_stopped_service(shell: list[str], tmp_path: Path) -> None:
    conf = hand_edited_conf(shell, tmp_path, legacy=True)
    edited = conf.read_bytes()
    proc = write_conf(shell, tmp_path, BASE_ENV, conf)
    assert proc.returncode != 0  # without the fake: nothing to restart
    proc = run(
        shell,
        "--env",
        str(tmp_path / "setup.env"),
        "--write-config",
        str(conf),
        env=fake_rc_service(tmp_path),
    )
    assert proc.returncode != 0
    assert rc_calls(tmp_path) == ["lanscape-atlas start"]
    assert RESTARTED in proc.stdout + proc.stderr
    assert conf.read_bytes() == edited
    assert os.listdir(conf.parent) == ["config.toml"]
    assert_no_secrets(proc)


def test_failed_restart_says_how_to_start_it(tmp_path: Path) -> None:
    conf = hand_edited_conf(["sh"], tmp_path)
    proc = check_conf(["sh"], tmp_path, BASE_ENV, conf, env=fake_rc_service(tmp_path, status=1))
    assert proc.returncode != 0
    assert rc_calls(tmp_path) == ["lanscape-atlas start"]
    combined = proc.stdout + proc.stderr
    assert RESTARTED not in combined
    assert "could not restart" in combined
    assert "rc-service lanscape-atlas start" in combined
    assert "config.toml was edited by hand" in proc.stderr


@pytest.mark.parametrize(
    ("extra", "stopped"),
    [(("--dry-run",), True), ((), False), (("--discard-config-edits",), True)],
    ids=["dry-run", "not-stopped", "discard"],
)
def test_no_restart_when_not_needed(extra: tuple[str, ...], stopped: bool, tmp_path: Path) -> None:
    conf = hand_edited_conf(["sh"], tmp_path)
    env = fake_rc_service(tmp_path, stopped=stopped)
    early = check_conf(["sh"], tmp_path, BASE_ENV, conf, *extra, env=env)
    late = run(
        ["sh"], "--env", str(tmp_path / "setup.env"), "--write-config", str(conf), *extra, env=env
    )
    assert rc_calls(tmp_path) == []
    for proc in (early, late):
        assert RESTARTED not in proc.stdout + proc.stderr


def test_reexec_keeps_discard_config_edits() -> None:
    text = SCRIPT.read_text()
    body = text[text.index("reexec_if_updated() {") :]
    body = body[: body.index("\n}\n")]
    assert "--discard-config-edits" in body
    assert re.search(r'exec sh "\$new_script" "\$@"', body)


# ------------------------------------------------------ the main sequence, stubbed
#
# The real main sequence (the script's own text after the "main" marker) with every step
# replaced by a stub that prints "STEP <name>", except the steps under test. The stubbed
# stop_service records a stop as the real one does; install_app marks the code installed.

MAIN_MARKER = "# --------------------------------------------------------------------- main"
STEPS = [
    "preflight",
    "check_config_early",
    "acquire_lock",
    "install_packages",
    "ensure_user",
    "ensure_repo_access",
    "checkout_code",
    "reexec_if_updated",
    "check_config_after_checkout",
    "build_wheels",
    "stop_service",
    "install_app",
    "backup_db",
    "write_config",
    "migrate_db",
    "install_service_files",
    "start_service",
    "update_oui",
    "smoke_test",
]
STUB_EXTRA = {
    "stop_service": "STOPPED=1",
    "install_app": "CODE_INSTALLED=1; NEW_COMMIT=abc1234",
}


def run_main(
    tmp_path: Path,
    data: dict[str, str],
    conf: Path,
    real: set[str],
    *args: str,
    stub_extra: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    overrides: tuple[str, ...] = (),
    shell: tuple[str, ...] = ("sh",),
) -> subprocess.CompletedProcess[str]:
    text = SCRIPT.read_text()
    head, main = text.split(MAIN_MARKER, 1)
    extras = {**STUB_EXTRA, **(stub_extra or {})}
    stubs = []
    for name in STEPS:
        if name not in real:
            extra = extras.get(name, ":")
            stubs.append(f'{name}() {{ echo "STEP {name}"; {extra}; }}')
    settings = [f"CONF='{conf}'", "CONF_CHOWN=0", f"SRC='{tmp_path / 'no-src'}'", *overrides]
    harness_dir = tmp_path / "harness" / "deploy"
    harness_dir.mkdir(parents=True)
    shutil.copy(ROOT / "config.example.toml", harness_dir.parent / "config.example.toml")
    harness = harness_dir / "setup-alpine.sh"
    harness.write_text(head + "\n".join(stubs + settings) + "\n" + MAIN_MARKER + main)
    env_file = write_env(tmp_path, data)
    if env is None:
        env = fake_rc_service(tmp_path, stopped=False)
    return run(list(shell), "--env", str(env_file), *args, env=env, script=harness)


def test_main_sequence_runs_every_step_on_a_clean_config(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    proc = run_main(tmp_path, BASE_ENV, conf, {"check_config_early", "check_config_after_checkout"})
    assert proc.returncode == 0, proc.stderr
    ran = re.findall(r"^STEP (\w+)$", proc.stdout, re.M)
    assert ran == [s for s in STEPS if not s.startswith("check_config")]
    assert rc_calls(tmp_path) == []


def test_legacy_hand_edit_stops_before_the_service_is_stopped(tmp_path: Path) -> None:
    # The check against the new config.example.toml runs after the checkout, and the
    # checkout runs before the stop: the old install is never stopped.
    conf = hand_edited_conf(["sh"], tmp_path, legacy=True)
    edited = conf.read_bytes()
    proc = run_main(tmp_path, BASE_ENV, conf, {"check_config_early", "check_config_after_checkout"})
    assert proc.returncode != 0
    ran = re.findall(r"^STEP (\w+)$", proc.stdout, re.M)
    assert ran[-3:] == ["ensure_repo_access", "checkout_code", "reexec_if_updated"]
    for name in ("build_wheels", "stop_service", "install_app", "backup_db", "write_config"):
        assert name not in ran
    assert rc_calls(tmp_path) == []
    assert RESTARTED not in proc.stderr
    assert "config.toml was edited by hand" in proc.stderr
    assert conf.read_bytes() == edited
    assert_no_secrets(proc)


def test_legacy_hand_edit_restarts_a_service_an_older_script_stopped(tmp_path: Path) -> None:
    # Scripts before this one stopped the service, then re-executed the new script.
    conf = hand_edited_conf(["sh"], tmp_path, legacy=True)
    edited = conf.read_bytes()
    proc = run_main(
        tmp_path,
        BASE_ENV,
        conf,
        {"check_config_early", "check_config_after_checkout"},
        env=fake_rc_service(tmp_path, stopped=True),
    )
    assert proc.returncode != 0
    assert rc_calls(tmp_path) == ["lanscape-atlas start"]  # once: the EXIT trap adds none
    assert RESTARTED in proc.stderr
    assert FAILED not in proc.stderr
    assert conf.read_bytes() == edited


def test_checksummed_hand_edit_stops_before_the_service_is_stopped(tmp_path: Path) -> None:
    conf = hand_edited_conf(["sh"], tmp_path)
    proc = run_main(tmp_path, BASE_ENV, conf, {"check_config_early", "check_config_after_checkout"})
    assert proc.returncode != 0
    assert re.findall(r"^STEP (\w+)$", proc.stdout, re.M) == ["preflight"]
    assert rc_calls(tmp_path) == []
    assert "config.toml was edited by hand" in proc.stderr


def test_defensive_late_stop_says_the_new_code_is_running(tmp_path: Path) -> None:
    # If the late check in write_config ever fires, the restart message must not claim
    # the install is untouched.
    conf = hand_edited_conf(["sh"], tmp_path, legacy=True)
    edited = conf.read_bytes()
    proc = run_main(tmp_path, BASE_ENV, conf, {"write_config"})
    assert proc.returncode != 0
    assert rc_calls(tmp_path) == ["lanscape-atlas start"]
    assert RESTARTED not in proc.stderr
    assert "newly installed code" in proc.stderr
    assert "config.toml was not overwritten" in proc.stderr
    assert FAILED not in proc.stderr  # reported by the hand-edit stop, not again by the trap
    assert conf.read_bytes() == edited


# ------------------------------------------------ a run that fails after the stop
#
# The real stop_service and EXIT trap against an rc-service fake that keeps state: "status"
# succeeds while the state file exists, "stop" removes it, "start" creates it.

RC = "lanscape-atlas"
FAILED = "the run failed during step"
STARTED_AGAIN = "was started again and rc-service reports it running"
NOT_STARTED = "could NOT be started again"
FAIL_INSTALL = {"install_app": 'step "Virtualenv and package"; die "boom"'}


def stateful_rc_service(
    tmp_path: Path, running: bool = True, start_fails: bool = False, crashed: bool = False
) -> dict[str, str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    fake = fakebin / "rc-service"
    fake.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >>"$FAKE_RC_LOG"\n'
        "case $2 in\n"
        "status)\n"
        '\t[ -f "$FAKE_RC_STATE" ] || exit 3\n'
        '\tif grep -q crashed "$FAKE_RC_STATE"; then\n'
        "\t\techo ' * status: crashed'\n"
        "\t\texit 32\n"
        "\tfi\n"
        "\t;;\n"
        'stop | zap) rm -f "$FAKE_RC_STATE" ;;\n'
        "start)\n"
        '\t[ -z "${FAKE_RC_START_FAILS:-}" ] || exit 1\n'
        "\t# OpenRC: a crashed service counts as started, so start does nothing.\n"
        '\t! grep -qs crashed "$FAKE_RC_STATE" || exit 0\n'
        '\t: >"$FAKE_RC_STATE"\n'
        "\t;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    state = tmp_path / "rc-service.running"
    if crashed:
        state.write_text("crashed\n")
    elif running:
        state.touch()
    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RC_LOG": str(tmp_path / "rc-service.log"),
        "FAKE_RC_STATE": str(state),
    }
    if start_fails:
        env["FAKE_RC_START_FAILS"] = "1"
    return env


def run_upgrade(
    tmp_path: Path,
    *args: str,
    env: dict[str, str],
    stub_extra: dict[str, str] | None = None,
    real: tuple[str, ...] = (),
    overrides: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    init = tmp_path / "init.d-lanscape-atlas"
    init.write_text("#!/bin/sh\n")
    init.chmod(0o755)
    return run_main(
        tmp_path,
        BASE_ENV,
        conf_path(tmp_path),
        {"stop_service", *real},
        *args,
        stub_extra={"start_service": "STOPPED=0", **(stub_extra or {})},
        env=env,
        overrides=(f"INIT_FILE='{init}'", *overrides),
    )


def is_running(tmp_path: Path) -> bool:
    state = tmp_path / "rc-service.running"
    return state.exists() and "crashed" not in state.read_text()


def test_successful_run_stops_once_and_the_trap_starts_nothing(tmp_path: Path) -> None:
    proc = run_upgrade(tmp_path, env=stateful_rc_service(tmp_path))
    assert proc.returncode == 0, proc.stderr
    ran = re.findall(r"^STEP (\w+)$", proc.stdout, re.M)
    assert ran == [s for s in STEPS if s != "stop_service"]
    assert rc_calls(tmp_path) == [f"{RC} status", f"{RC} stop"]  # start_service is a stub
    assert FAILED not in proc.stderr


@pytest.mark.parametrize("failing", ["install_app", "backup_db", "write_config"])
def test_failure_after_the_stop_starts_the_service_again(failing: str, tmp_path: Path) -> None:
    proc = run_upgrade(
        tmp_path,
        env=stateful_rc_service(tmp_path),
        stub_extra={failing: 'step "Some step"; die "boom"'},
    )
    assert proc.returncode == 1
    assert rc_calls(tmp_path) == [f"{RC} status", f"{RC} stop", f"{RC} start", f"{RC} status"]
    assert is_running(tmp_path)
    assert f"{FAILED} 'Some step' (exit status 1)" in proc.stderr
    assert STARTED_AGAIN in proc.stderr
    assert NOT_STARTED not in proc.stderr
    assert "STEP start_service" not in proc.stdout


def test_failure_before_install_says_the_previous_version_was_restarted(tmp_path: Path) -> None:
    proc = run_upgrade(tmp_path, env=stateful_rc_service(tmp_path), stub_extra=FAIL_INSTALL)
    assert proc.returncode == 1
    assert f"{FAILED} 'Virtualenv and package'" in proc.stderr
    assert "the previously installed version (the virtualenv was not changed)" in proc.stderr
    assert "database migration had started" not in proc.stderr


def test_failure_during_install_says_the_virtualenv_may_be_incomplete(tmp_path: Path) -> None:
    proc = run_upgrade(
        tmp_path,
        env=stateful_rc_service(tmp_path),
        stub_extra={"install_app": 'CODE_INSTALLED=1; die "boom"'},
    )
    assert proc.returncode == 1
    assert "may be incomplete" in proc.stderr
    assert "previously installed version" not in proc.stderr
    assert STARTED_AGAIN in proc.stderr


def test_failure_after_migrate_warns_about_the_database(tmp_path: Path) -> None:
    proc = run_upgrade(
        tmp_path,
        env=stateful_rc_service(tmp_path),
        stub_extra={
            "install_app": "CODE_INSTALLED=1; INSTALL_DONE=1; NEW_COMMIT=abc1234",
            "migrate_db": 'step "Database migration"; MIGRATE_STARTED=1; die "boom"',
        },
    )
    assert proc.returncode == 1
    assert rc_calls(tmp_path)[-2:] == [f"{RC} start", f"{RC} status"]
    assert f"{FAILED} 'Database migration'" in proc.stderr
    assert "the database migration had started" in proc.stderr
    assert "the NEWLY installed code (abc1234)" in proc.stderr
    assert "previously installed version" not in proc.stderr
    assert STARTED_AGAIN in proc.stderr


def test_real_steps_record_how_far_the_install_and_migration_got() -> None:
    install = function_body("install_app")
    assert install.index("CODE_INSTALLED=1") < install.index("python3 -m venv")
    assert install.rstrip().endswith("INSTALL_DONE=1")
    migrate = function_body("migrate_db")
    assert migrate.index("MIGRATE_STARTED=1") < migrate.index("run_as_service_user migrate")


def test_failed_restart_after_a_failure_is_reported_as_down(tmp_path: Path) -> None:
    proc = run_upgrade(
        tmp_path, env=stateful_rc_service(tmp_path, start_fails=True), stub_extra=FAIL_INSTALL
    )
    assert proc.returncode == 1
    assert f"{RC} start" in rc_calls(tmp_path)
    assert not is_running(tmp_path)
    assert NOT_STARTED in proc.stderr and "is DOWN" in proc.stderr
    assert STARTED_AGAIN not in proc.stderr
    assert f"rc-service {RC} start" in proc.stderr


def test_restart_is_not_claimed_when_status_disagrees(tmp_path: Path) -> None:
    # "start" exits 0 but the service is not running afterwards: never reported as started.
    env = stateful_rc_service(tmp_path)
    fake = tmp_path / "fakebin" / "rc-service"
    fake.write_text(fake.read_text().replace(': >"$FAKE_RC_STATE"', ":"))
    proc = run_upgrade(tmp_path, env=env, stub_extra=FAIL_INSTALL)
    assert proc.returncode == 1
    assert rc_calls(tmp_path)[-2:] == [f"{RC} start", f"{RC} status"]
    assert NOT_STARTED in proc.stderr
    assert STARTED_AGAIN not in proc.stderr


@pytest.mark.parametrize("failing", ["checkout_code", "reexec_if_updated", "build_wheels"])
def test_failure_before_the_stop_never_stops_the_service(failing: str, tmp_path: Path) -> None:
    # A GitHub or PyPI outage: the installed version keeps running.
    proc = run_upgrade(
        tmp_path, env=stateful_rc_service(tmp_path), stub_extra={failing: 'die "no network"'}
    )
    assert proc.returncode == 1
    assert rc_calls(tmp_path) == []
    assert is_running(tmp_path)
    assert FAILED not in proc.stderr
    assert "Stop service" not in proc.stdout


def test_service_that_was_not_running_is_not_started_by_the_trap(tmp_path: Path) -> None:
    proc = run_upgrade(
        tmp_path, env=stateful_rc_service(tmp_path, running=False), stub_extra=FAIL_INSTALL
    )
    assert proc.returncode == 1
    assert rc_calls(tmp_path) == [f"{RC} status"]
    assert not is_running(tmp_path)
    assert FAILED not in proc.stderr


def test_dry_run_failure_stops_and_starts_nothing(tmp_path: Path) -> None:
    env = stateful_rc_service(tmp_path)
    env["LANSCAPE_SETUP_STOPPED"] = "1"  # even if handed over as stopped
    proc = run_upgrade(tmp_path, "--dry-run", env=env, stub_extra=FAIL_INSTALL)
    assert proc.returncode == 1
    assert rc_calls(tmp_path) == [f"{RC} status"]
    assert is_running(tmp_path)
    assert "(dry-run) would: rc-service lanscape-atlas stop" in proc.stdout
    assert FAILED not in proc.stderr


def test_single_step_mode_never_restarts_from_the_trap(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    bare = tmp_path / "bare" / "deploy"
    bare.mkdir(parents=True)
    script = bare / "setup-alpine.sh"
    shutil.copy(SCRIPT, script)
    (bare.parent / "config.example.toml").write_text("not = 'the example'\n")  # cannot render
    env = stateful_rc_service(tmp_path, running=False)
    env["LANSCAPE_SETUP_STOPPED"] = "1"
    env_file = write_env(tmp_path, BASE_ENV)
    proc = run(["sh"], "--env", str(env_file), "--write-config", str(conf), env=env, script=script)
    assert proc.returncode == 1
    assert "could not be rendered" in proc.stderr
    assert rc_calls(tmp_path) == []
    assert FAILED not in proc.stderr


def test_service_stopped_before_a_reexec_is_started_again_on_a_later_failure(
    tmp_path: Path,
) -> None:
    # Handed over by a script that stopped the service and then re-executed this one.
    env = stateful_rc_service(tmp_path, running=False)
    env["LANSCAPE_SETUP_STOPPED"] = "1"
    proc = run_upgrade(tmp_path, env=env, stub_extra={"build_wheels": 'die "no network"'})
    assert proc.returncode == 1
    assert rc_calls(tmp_path) == [f"{RC} start", f"{RC} status"]
    assert is_running(tmp_path)
    assert STARTED_AGAIN in proc.stderr


@pytest.mark.parametrize("stopped", ["1", "0"])
def test_reexec_hands_over_whether_the_service_was_stopped(stopped: str, tmp_path: Path) -> None:
    new_script = tmp_path / "no-src" / "deploy" / "setup-alpine.sh"
    new_script.parent.mkdir(parents=True)
    new_script.write_text(
        "#!/bin/sh\n"
        'echo "NEW SCRIPT stopped=$LANSCAPE_SETUP_STOPPED reexec=$LANSCAPE_SETUP_REEXEC args=$*"\n'
    )
    env = stateful_rc_service(tmp_path, running=False)
    if stopped == "1":
        env["LANSCAPE_SETUP_STOPPED"] = "1"
    proc = run_upgrade(tmp_path, env=env, real=("reexec_if_updated",))
    assert proc.returncode == 0, proc.stderr
    assert f"NEW SCRIPT stopped={stopped} reexec=1 args=--env " in proc.stdout
    assert "STEP check_config_after_checkout" not in proc.stdout  # the old script is gone
    assert rc_calls(tmp_path) == []  # exec runs no EXIT trap: the new script owns the restart


def test_signal_after_the_stop_starts_the_service_again(tmp_path: Path) -> None:
    proc = run_upgrade(
        tmp_path,
        env=stateful_rc_service(tmp_path),
        stub_extra={"install_app": 'step "Virtualenv and package"; kill -TERM $$; sleep 5'},
    )
    assert proc.returncode == 143
    assert rc_calls(tmp_path)[-2:] == [f"{RC} start", f"{RC} status"]
    assert is_running(tmp_path)
    assert STARTED_AGAIN in proc.stderr


def test_script_traps_the_signals_that_would_skip_the_exit_trap() -> None:
    text = SCRIPT.read_text()
    for sig, status in (("HUP", 129), ("INT", 130), ("TERM", 143)):
        assert re.search(rf"^trap 'exit {status}' {sig}$", text, re.M)


# ------------------------------------------- wheels before the stop, install without network
#
# The real build_wheels, stop_service and install_app with "python3 -m venv" and pip faked.
# The fakes log to the rc-service log, so one file gives the order of everything.


def fake_python_and_pip(tmp_path: Path, env: dict[str, str], wheel_fails: bool = False) -> None:
    pip = tmp_path / "fake-pip"
    pip.write_text(
        "#!/bin/sh\n"
        'printf \'pip %s\\n\' "$*" >>"$FAKE_RC_LOG"\n'
        '[ "$1" = wheel ] || exit 0\n'
        'if [ -n "${FAKE_PIP_WHEEL_FAILS:-}" ]; then\n'
        "\techo 'ERROR: No matching distribution found for example'\n"
        "\texit 1\n"
        "fi\n"
        "while [ $# -gt 1 ]; do\n"
        '\t[ "$1" != --wheel-dir ] || dir=$2\n'
        "\tshift\n"
        "done\n"
        'mkdir -p "$dir"\n'
        ': >"$dir/lanscape_atlas-0.1.0-py3-none-any.whl"\n'
        ': >"$dir/example_dep-1.0-py3-none-any.whl"\n'
    )
    pip.chmod(0o755)
    python = tmp_path / "fakebin" / "python3"
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -m ] && [ "$2" = venv ]; then\n'
        '\tprintf \'venv %s\\n\' "$3" >>"$FAKE_RC_LOG"\n'
        '\tmkdir -p "$3/bin"\n'
        '\tcp "$FAKE_PIP" "$3/bin/pip"\n'
        "\tprintf '#!/bin/sh\\nexit 0\\n' >\"$3/bin/python\"\n"
        '\tchmod 0755 "$3/bin/python"\n'
        "\texit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n'
    )
    python.chmod(0o755)
    env["FAKE_PIP"] = str(pip)
    if wheel_fails:
        env["FAKE_PIP_WHEEL_FAILS"] = "1"


def run_install(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    prefix = tmp_path / "opt"
    prefix.mkdir()
    return run_upgrade(
        tmp_path,
        env=env,
        real=("build_wheels", "install_app"),
        stub_extra={"checkout_code": "NEW_FULL=abc1234abc1234; NEW_COMMIT=abc1234"},
        overrides=(f"PREFIX='{prefix}'", f"VENV='{prefix}/venv'", f"STAMP='{prefix}/.stamp'"),
    )


def test_wheels_are_built_before_the_stop_and_installed_without_network(tmp_path: Path) -> None:
    env = stateful_rc_service(tmp_path)
    fake_python_and_pip(tmp_path, env)
    stale = tmp_path / "opt" / ".build.stale" / "wheels"  # left by a killed run
    stale.mkdir(parents=True)
    prefix = stale.parents[1]
    proc = run_upgrade(
        tmp_path,
        env=env,
        real=("build_wheels", "install_app"),
        stub_extra={"checkout_code": "NEW_FULL=abc1234abc1234; NEW_COMMIT=abc1234"},
        overrides=(f"PREFIX='{prefix}'", f"VENV='{prefix}/venv'", f"STAMP='{prefix}/.stamp'"),
    )
    assert proc.returncode == 0, proc.stderr
    log = rc_calls(tmp_path)
    kinds = [" ".join(c.split()[:2]) for c in log if not c.startswith("venv ")]
    assert kinds == ["pip wheel", f"{RC} status", f"{RC} stop", "pip install", "pip install"]
    wheel = next(c for c in log if c.startswith("pip wheel"))
    assert wheel.endswith(f" {tmp_path / 'no-src'}") and "--upgrade" not in wheel
    installs = [c for c in log if c.startswith("pip install")]
    for command in installs:
        assert "--no-index" in command and "--upgrade" not in command
        assert re.search(r"/\.build\.\w+/wheels/lanscape_atlas-0\.1\.0-py3-none-any\.whl$", command)
    assert re.search(r"--find-links \S+/\.build\.\w+/wheels ", installs[0])
    assert "--force-reinstall --no-deps" in installs[1]
    # The wheels are made by a throwaway virtualenv, before the real one is touched.
    venvs = [c.split(" ", 1)[1] for c in log if c.startswith("venv ")]
    assert len(venvs) == 2 and re.fullmatch(rf"{prefix}/\.build\.\w+/venv", venvs[0])
    assert venvs[1] == f"{prefix}/venv"
    assert log.index(f"venv {venvs[1]}") > log.index(f"{RC} stop")
    assert sorted(p.name for p in prefix.iterdir()) == ["venv"]  # build dirs are removed


def test_pip_wheel_failure_leaves_the_service_running_and_the_venv_alone(tmp_path: Path) -> None:
    env = stateful_rc_service(tmp_path)
    fake_python_and_pip(tmp_path, env, wheel_fails=True)
    proc = run_install(tmp_path, env)
    assert proc.returncode == 1
    assert "pip wheel failed" in proc.stderr
    assert "No matching distribution found" in proc.stderr  # pip's own output is shown
    log = rc_calls(tmp_path)
    assert not [c for c in log if c.startswith((RC, "pip install"))]
    assert is_running(tmp_path)
    assert list((tmp_path / "opt").iterdir()) == []  # no venv, and the build dir is removed
    assert FAILED not in proc.stderr


def test_readme_documents_the_upgrade_order_and_the_restart_after_a_failure() -> None:
    text = " ".join((ROOT / "README.md").read_text().split())
    section = text[text.index("### Scripted install and upgrade") :]
    assert section.index("pip wheel") < section.index("Only then does it stop the service")
    assert "starts the service again" in section
    assert "rc-service lanscape-atlas status" in section


# ------------------------------------------- a virtualenv made by another Python version
#
# The real install_app against a virtualenv left by an earlier Python minor version: its
# python still runs, but its pip and packages belong to the old version.

PY_MINOR = f"{sys.version_info.major}.{sys.version_info.minor}"


def old_venv(tmp_path: Path, version: str, pip_works: bool = True) -> Path:
    venv = tmp_path / "opt" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = /usr/bin\ninclude-system-site-packages = false\nversion = {version}\n"
    )
    python = venv / "bin" / "python"
    pip_check = "" if pip_works else '[ "$1 $2" != "-m pip" ] || exit 1\n'
    python.write_text(f"#!/bin/sh\n{pip_check}exit 0\n")
    python.chmod(0o755)
    (venv / "OLD").touch()
    return venv


@pytest.mark.parametrize(
    ("version", "pip_works", "rebuilt"),
    [
        ("3.1.0", True, True),  # another minor version: site-packages orphaned
        (f"{PY_MINOR}.0", False, True),  # same version, but pip is gone
        (f"{PY_MINOR}.99", True, False),  # a patch release: kept
    ],
    ids=["other-minor", "no-pip", "same-minor"],
)
def test_install_rebuilds_a_virtualenv_from_another_python(
    version: str, pip_works: bool, rebuilt: bool, tmp_path: Path
) -> None:
    env = stateful_rc_service(tmp_path)
    fake_python_and_pip(tmp_path, env)
    venv = old_venv(tmp_path, version, pip_works)
    shutil.copy(env["FAKE_PIP"], venv / "bin" / "pip")
    proc = install_into_existing_venv(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    made = [c for c in rc_calls(tmp_path) if c == f"venv {venv}"]
    assert bool(made) == rebuilt
    assert (venv / "OLD").exists() != rebuilt
    assert ("rebuilding virtualenv" in proc.stdout) == rebuilt
    assert [c for c in rc_calls(tmp_path) if c.startswith("pip install")]


def install_into_existing_venv(
    tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    prefix = tmp_path / "opt"
    return run_upgrade(
        tmp_path,
        env=env,
        real=("build_wheels", "install_app"),
        stub_extra={"checkout_code": "NEW_FULL=abc1234abc1234; NEW_COMMIT=abc1234"},
        overrides=(f"PREFIX='{prefix}'", f"VENV='{prefix}/venv'", f"STAMP='{prefix}/.stamp'"),
    )


def test_venv_check_compares_pyvenv_cfg_and_runs_pip() -> None:
    check = function_body("venv_matches_python")
    assert "pyvenv.cfg" in check
    assert '"$VENV/bin/python" -m pip --version' in check
    assert "venv_matches_python" in function_body("install_app")
    assert "venv_matches_python" in function_body("venv_ok")  # build_wheels' skip, too


# ------------------------------------------------------------------- a crashed service


def test_crashed_service_is_zapped_and_then_really_started(tmp_path: Path) -> None:
    env = stateful_rc_service(tmp_path, crashed=True)
    port = free_port()
    proc = run_upgrade(
        tmp_path,
        env=env,
        real=("start_service",),
        overrides=("BIND_HOST=127.0.0.1", f"BIND_PORT={port}"),
    )
    assert proc.returncode == 0, proc.stderr
    assert rc_calls(tmp_path) == [f"{RC} status", f"{RC} zap", f"{RC} status", f"{RC} start"]
    assert is_running(tmp_path)
    assert "has crashed" in proc.stderr


def test_start_without_the_zap_would_do_nothing(tmp_path: Path) -> None:
    # The fake models OpenRC: start on a crashed service is a no-op.
    env = stateful_rc_service(tmp_path, crashed=True)
    subprocess.run(["rc-service", RC, "start"], env=env, check=True)
    assert not is_running(tmp_path)


def test_crashed_service_is_started_again_after_a_failure(tmp_path: Path) -> None:
    proc = run_upgrade(
        tmp_path, env=stateful_rc_service(tmp_path, crashed=True), stub_extra=FAIL_INSTALL
    )
    assert proc.returncode == 1
    assert rc_calls(tmp_path) == [f"{RC} status", f"{RC} zap", f"{RC} start", f"{RC} status"]
    assert is_running(tmp_path)
    assert STARTED_AGAIN in proc.stderr


def test_dry_run_reports_a_crashed_service_without_zapping_it(tmp_path: Path) -> None:
    proc = run_upgrade(tmp_path, "--dry-run", env=stateful_rc_service(tmp_path, crashed=True))
    assert "(dry-run) would: rc-service lanscape-atlas zap" in proc.stdout
    assert rc_calls(tmp_path) == [f"{RC} status"]


# ------------------------------------------------------------------- REPO_URL changes

OLD_URL = "https://old.example/lanscape-atlas.git"


def git_checkout(tmp_path: Path, origin: str | None) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (src / "README").write_text("x\n")
    subprocess.run([*git, "add", "README"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    if origin is not None:
        subprocess.run([*git, "remote", "add", "origin", origin], check=True)
    return src


def origin_of(src: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(src), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "origin", [OLD_URL, BASE_ENV["REPO_URL"], None], ids=["old", "same", "none"]
)
def test_checkout_points_origin_at_repo_url(origin: str | None, tmp_path: Path) -> None:
    src = git_checkout(tmp_path, origin)
    proc = run_upgrade(
        tmp_path,
        env=stateful_rc_service(tmp_path),
        real=("checkout_code",),
        overrides=(f"SRC='{src}'", 'git_run() { echo "git_run $*"; }'),
    )
    assert proc.returncode == 0, proc.stderr
    assert origin_of(src) == BASE_ENV["REPO_URL"]
    assert "git_run -C" in proc.stdout and "fetch" in proc.stdout
    changed = "pointing origin at REPO_URL" in proc.stdout
    assert changed == (origin != BASE_ENV["REPO_URL"])
    assert OLD_URL not in proc.stdout + proc.stderr  # URLs can carry tokens


def test_dry_run_reports_a_changed_repo_url_without_changing_it(tmp_path: Path) -> None:
    src = git_checkout(tmp_path, OLD_URL)
    proc = run_upgrade(
        tmp_path,
        "--dry-run",
        env=stateful_rc_service(tmp_path),
        real=("checkout_code",),
        overrides=(f"SRC='{src}'",),
    )
    assert "(dry-run) would: point the checkout's origin at REPO_URL" in proc.stdout
    assert origin_of(src) == OLD_URL


# ------------------------------------------------------------------------- the lock


def run_locked(
    tmp_path: Path, *args: str, stub_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    held = '[ ! -d "$LOCK_DIR" ] || echo "LOCK HELD $(cat "$LOCK_DIR/pid") self=$$"'
    return run_upgrade(
        tmp_path,
        *args,
        env=stateful_rc_service(tmp_path),
        real=("acquire_lock",),
        stub_extra={"install_packages": held, **(stub_extra or {})},
        overrides=(f"LOCK_DIR='{tmp_path / 'setup.lock'}'",),
    )


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_lock_is_held_during_the_run_and_removed_after(tmp_path: Path) -> None:
    proc = run_locked(tmp_path)
    assert proc.returncode == 0, proc.stderr
    held = re.search(r"^LOCK HELD (\d+) self=(\d+)$", proc.stdout, re.M)
    assert held and held.group(1) == held.group(2)
    assert not (tmp_path / "setup.lock").exists()


def test_a_second_run_stops_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / "setup.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")  # a live process
    proc = run_locked(tmp_path)
    assert proc.returncode == 1
    assert f"another setup run (PID {os.getpid()}) is in progress" in proc.stderr
    assert "STEP install_packages" not in proc.stdout
    assert rc_calls(tmp_path) == []
    assert (lock / "pid").read_text() == f"{os.getpid()}\n"  # never removed by the loser


def test_a_stale_lock_is_taken_over(tmp_path: Path) -> None:
    lock = tmp_path / "setup.lock"
    lock.mkdir()
    pid = dead_pid()
    (lock / "pid").write_text(f"{pid}\n")
    proc = run_locked(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert f"removing the stale lock {lock} (PID {pid} is not running)" in proc.stderr
    assert "LOCK HELD" in proc.stdout
    assert not lock.exists()


@pytest.mark.parametrize("age_s", [0, 300])
def test_a_lock_without_a_pid_is_stale_only_when_old(age_s: int, tmp_path: Path) -> None:
    lock = tmp_path / "setup.lock"
    lock.mkdir()
    then = datetime.now().timestamp() - age_s
    os.utime(lock, (then, then))
    proc = run_locked(tmp_path)
    if age_s:
        assert proc.returncode == 0, proc.stderr
        assert "removing the stale lock" in proc.stderr
    else:
        assert proc.returncode == 1
        assert "another setup run is starting" in proc.stderr
        assert lock.exists()


def test_the_reexecuted_script_keeps_its_own_lock(tmp_path: Path) -> None:
    # exec keeps the PID: the lock the old script took is this run's own.
    take = 'mkdir "$LOCK_DIR"; echo $$ >"$LOCK_DIR/pid"'
    proc = run_locked(tmp_path, stub_extra={"check_config_early": take})
    assert proc.returncode == 0, proc.stderr
    assert "stale" not in proc.stderr and "LOCK HELD" in proc.stdout
    assert not (tmp_path / "setup.lock").exists()


def test_a_failed_run_restarts_the_service_and_releases_the_lock(tmp_path: Path) -> None:
    proc = run_locked(tmp_path, stub_extra=FAIL_INSTALL)
    assert proc.returncode == 1
    assert STARTED_AGAIN in proc.stderr
    assert not (tmp_path / "setup.lock").exists()


def test_dry_run_takes_no_lock_and_names_a_holder(tmp_path: Path) -> None:
    proc = run_locked(tmp_path, "--dry-run")
    assert "LOCK HELD" not in proc.stdout
    shutil.rmtree(tmp_path / "harness")
    lock = tmp_path / "setup.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")
    proc = run_locked(tmp_path, "--dry-run")
    assert f"another setup run (PID {os.getpid()}) is in progress" in proc.stderr
    assert (lock / "pid").exists()


def test_the_lock_is_taken_before_anything_is_installed() -> None:
    main = SCRIPT.read_text().split(MAIN_MARKER, 1)[1]
    assert main.index("acquire_lock") < main.index("install_packages")
    assert "LOCK_DIR=/run/$APP-setup.lock" in SCRIPT.read_text()
    assert "release_lock" in function_body("on_exit")


# ------------------------------------------------------------------- temporary files


def logging_mktemp(tmp_path: Path, env: dict[str, str], stub: bool = True) -> Path:
    """A mktemp first on PATH that logs its arguments; TMPDIR is a fresh directory."""
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    env["TMPDIR"] = str(tmpdir)
    env["FAKE_MKTEMP_LOG"] = str(tmp_path / "mktemp.log")
    if not stub:
        return tmpdir
    real = shutil.which("mktemp")
    assert real
    fake = tmp_path / "fakebin" / "mktemp"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$FAKE_MKTEMP_LOG"\nexec "{real}" "$@"\n')
    fake.chmod(0o755)
    if not env["PATH"].startswith(str(fake.parent)):
        env["PATH"] = f"{fake.parent}{os.pathsep}{env['PATH']}"
    return tmpdir


def mktemp_logging_script(tmp_path: Path) -> Path:
    """The script with a logging mktemp *function* defined first.

    A stub first on PATH is not enough: busybox ash (Alpine) runs its own mktemp applet
    without a PATH lookup. A shell function comes before applets and PATH in every shell.
    """
    harness = tmp_path / "logging" / "deploy" / "setup-alpine.sh"
    harness.parent.mkdir(parents=True)
    shutil.copy(ROOT / "config.example.toml", harness.parent.parent / "config.example.toml")
    shebang, rest = SCRIPT.read_text().split("\n", 1)
    log = 'mktemp() {\n\tprintf \'%s\\n\' "$*" >>"$FAKE_MKTEMP_LOG"\n\tcommand mktemp "$@"\n}\n'
    harness.write_text(f"{shebang}\n{log}{rest}")
    return harness


def mktemp_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "mktemp.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize("shell", SHELLS)
def test_checks_never_write_the_rendered_config_to_a_temp_file(
    shell: list[str], tmp_path: Path
) -> None:
    conf = hand_edited_conf(shell, tmp_path)
    env = fake_rc_service(tmp_path, stopped=False)
    tmpdir = logging_mktemp(tmp_path, env, stub=False)
    script = mktemp_logging_script(tmp_path)
    early = check_conf(shell, tmp_path, BASE_ENV, conf, env=env, script=script)
    assert early.returncode != 0 and "config.toml was edited by hand" in early.stderr
    env_file = str(write_env(tmp_path, BASE_ENV))
    dry = run(shell, "--env", env_file, "--dry-run", env=env, script=script)
    assert "dry-run" in dry.stdout
    # Only this run's own directory; the renders stayed in python's memory.
    assert mktemp_calls(tmp_path) == ["-d", "-d"]
    assert list(tmpdir.iterdir()) == []


def test_dry_run_still_says_whether_config_toml_would_change(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    same = write_conf(["sh"], tmp_path, BASE_ENV, conf, "--dry-run")
    assert "is unchanged" in same.stdout, same.stderr
    other = write_conf(["sh"], tmp_path, {**BASE_ENV, "BIND_PORT": "8721"}, conf, "--dry-run")
    assert "would back up and replace" in other.stdout, other.stderr
    assert os.listdir(conf.parent) == ["config.toml"]


@pytest.mark.parametrize("failing", [None, "install_app", "smoke_test"])
def test_every_temp_file_is_removed_on_exit(failing: str | None, tmp_path: Path) -> None:
    env = stateful_rc_service(tmp_path)
    tmpdir = logging_mktemp(tmp_path, env)
    extra = {failing: 'step "x"; : >"$RUN_TMP/scratch"; die "boom"'} if failing else {}
    proc = run_upgrade(tmp_path, env=env, stub_extra=extra)
    assert proc.returncode == (1 if failing else 0), proc.stderr
    assert list(tmpdir.iterdir()) == []


def test_write_config_failure_leaves_no_temp_file_next_to_config(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    proc = run_main(
        tmp_path,
        {**BASE_ENV, "BIND_PORT": "8721"},
        conf,
        {"write_config"},
        # Fail once the temp file is written, before it is renamed over config.toml.
        overrides=('mv() { case " $* " in *" $CONF_TMP "*) exit 1 ;; esac; command mv "$@"; }',),
    )
    assert proc.returncode != 0
    assert sorted(p.name for p in conf.parent.iterdir() if not BACKUP_RE.fullmatch(p.name)) == [
        "config.toml"
    ]


def test_scripts_temp_files_all_live_in_the_run_directory() -> None:
    text = SCRIPT.read_text()
    assert text.count("$(mktemp") == 4
    made = re.findall(r"^\s*(\w+)=\$\(mktemp\b", text, re.M)
    assert sorted(made) == ["BACKUP_TMP", "CONF_TMP", "RUN_TMP", "WORK_DIR"]
    exit_body = function_body("on_exit")
    for var in ("WORK_DIR", "RUN_TMP", "CONF_TMP", "BACKUP_TMP"):
        assert f'"${var}"' in exit_body


def test_reexec_removes_its_temp_directory_before_exec() -> None:
    body = function_body("reexec_if_updated")
    assert body.index('rm -rf "$RUN_TMP"') < body.index("exec sh")


# ----------------------------------------------------- smoke test: a run after this start

STARTED_AT = "2026-11-26T10:00:00Z"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fake_api(last_ok: str | None, token: str) -> tuple[ThreadingHTTPServer, int]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return
            if self.path.startswith("/api/v1/collectors"):
                body: object = [
                    {
                        "name": "arp_scan",
                        "last_ok": last_ok,
                        "last_error": None,
                        "consecutive_failures": 0,
                    }
                ]
            else:
                body = []
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


@pytest.mark.parametrize(
    ("last_ok", "running", "rc"),
    [
        ("2026-11-26T10:00:07Z", True, 0),  # a run after this start
        ("2026-11-26T09:59:59Z", True, 2),  # persisted from before the start
        (None, True, 2),
        ("2026-11-26T09:00:00Z", False, 2),
    ],
    ids=["fresh", "stale", "never", "stale-and-down"],
)
def test_smoke_test_needs_a_run_newer_than_this_start(
    last_ok: str | None, running: bool, rc: int, tmp_path: Path
) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, {**BASE_ENV, "API_TOKEN": TOKEN}, conf).returncode == 0
    server, port = fake_api(last_ok, TOKEN)
    try:
        proc = run_upgrade(
            tmp_path,
            env=stateful_rc_service(tmp_path, running=running),
            real=("smoke_test",),
            # The real stop_service stops it; the stubbed start starts it again, or not.
            stub_extra={
                "start_service": 'STOPPED=0; rc-service "$APP" start' if running else "STOPPED=0"
            },
            overrides=(
                "BIND_HOST=127.0.0.1",
                f"BIND_PORT={port}",
                f"SVC_STARTED_AT={STARTED_AT}",
                "SMOKE_RUN_WAIT_S=0",
                f"LOG_FILE='{tmp_path / 'service.log'}'",
            ),
        )
    finally:
        server.shutdown()
        server.server_close()
    assert proc.returncode == rc, proc.stdout + proc.stderr
    assert "API is up" in proc.stdout
    if rc:
        assert f"no collector completed a run since the start ({STARTED_AT})" in proc.stdout
        if running:
            assert "rc-service reports lanscape-atlas running" in proc.stderr
        else:
            assert "lanscape-atlas is NOT running (rc-service status: stopped)" in proc.stderr
    assert TOKEN not in proc.stdout + proc.stderr


def test_start_records_the_time_before_starting() -> None:
    start = function_body("start_service")
    assert start.index("SVC_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)") < start.index(
        'rc-service "$APP" start'
    )


# ----------------------------------------------------------- weekly update-oui job


def service_files_env(tmp_path: Path, crond: bool = True, crontab: str | None = None) -> dict:
    env = stateful_rc_service(tmp_path)
    rc_update = tmp_path / "fakebin" / "rc-update"
    listed_services = "lanscape-atlas" + (" crond" if crond else "")
    rc_update.write_text(
        "#!/bin/sh\n"
        f'[ "$1" != show ] || for s in {listed_services}; do echo " $s | default"; done\n'
    )
    rc_update.chmod(0o755)
    tab = tmp_path / "crontab"
    tab.write_text(
        crontab
        if crontab is not None
        else "0 3 * * 6 run-parts /etc/periodic/weekly\n0 2 * * * run-parts /etc/periodic/daily\n"
    )
    return env


def run_service_files(
    tmp_path: Path, env: dict, data: dict[str, str] = BASE_ENV, *args: str
) -> subprocess.CompletedProcess[str]:
    src = tmp_path / "src"
    for rel in ("deploy/openrc/lanscape-atlas", "deploy/logrotate/lanscape-atlas"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, src / rel)
    etc = tmp_path / "etc"
    return run_main(
        tmp_path,
        data,
        conf_path(tmp_path),
        {"install_service_files"},
        *args,
        env=env,
        overrides=(
            f"SRC='{src}'",
            f"INIT_FILE='{etc}/init.d/lanscape-atlas'",
            f"LOGROTATE_FILE='{etc}/logrotate.d/lanscape-atlas'",
            f"PERIODIC_FILE='{etc}/periodic/weekly/lanscape-atlas-update-oui'",
            f"CRONTAB='{tmp_path / 'crontab'}'",
        ),
    )


def periodic(tmp_path: Path) -> Path:
    return tmp_path / "etc" / "periodic" / "weekly" / "lanscape-atlas-update-oui"


def test_weekly_update_oui_job_is_installed(tmp_path: Path) -> None:
    (tmp_path / "etc" / "init.d").mkdir(parents=True)
    env = service_files_env(tmp_path)
    proc = run_service_files(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    job = periodic(tmp_path)
    assert stat.S_IMODE(job.stat().st_mode) == 0o755
    text = job.read_text()
    cmd = "su -s /bin/sh lanscape -c '/opt/lanscape-atlas/venv/bin/lanscape-atlas update-oui'"
    assert f"{cmd} >>/var/log/lanscape-atlas.log 2>&1" in text
    assert "UPDATE_OUI_WEEKLY=false" in text  # says how to opt out
    assert subprocess.run(["sh", "-n", str(job)]).returncode == 0
    assert "weekly update-oui will not run" not in proc.stderr


def test_weekly_job_rerun_is_up_to_date(tmp_path: Path) -> None:
    (tmp_path / "etc" / "init.d").mkdir(parents=True)
    env = service_files_env(tmp_path)
    assert run_service_files(tmp_path, env).returncode == 0
    shutil.rmtree(tmp_path / "harness")
    proc = run_service_files(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert f"{periodic(tmp_path)} up to date" in proc.stdout


def test_update_oui_weekly_false_removes_the_job(tmp_path: Path) -> None:
    (tmp_path / "etc" / "init.d").mkdir(parents=True)
    env = service_files_env(tmp_path)
    assert run_service_files(tmp_path, env).returncode == 0
    assert periodic(tmp_path).exists()
    shutil.rmtree(tmp_path / "harness")
    proc = run_service_files(tmp_path, env, {**BASE_ENV, "UPDATE_OUI_WEEKLY": "false"})
    assert proc.returncode == 0, proc.stderr
    assert not periodic(tmp_path).exists()
    assert "UPDATE_OUI_WEEKLY=false" in proc.stdout


def test_existing_crontab_line_is_not_doubled(tmp_path: Path) -> None:
    (tmp_path / "etc" / "init.d").mkdir(parents=True)
    cmd = "/opt/lanscape-atlas/venv/bin/lanscape-atlas update-oui"
    line = f"17 4 * * 0 su -s /bin/sh lanscape -c '{cmd}'\n"
    env = service_files_env(tmp_path, crontab=line)
    proc = run_service_files(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert not periodic(tmp_path).exists()
    assert "already in" in proc.stdout


def test_weekly_job_warns_when_crond_would_not_run_it(tmp_path: Path) -> None:
    (tmp_path / "etc" / "init.d").mkdir(parents=True)
    env = service_files_env(tmp_path, crond=False, crontab="")
    proc = run_service_files(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert "crond is not in the default runlevel" in proc.stderr
    assert "does not run /etc/periodic/weekly" in proc.stderr


def test_dry_run_reports_the_weekly_job_without_writing(tmp_path: Path) -> None:
    env = service_files_env(tmp_path)
    proc = run_service_files(tmp_path, env, BASE_ENV, "--dry-run")
    assert "(dry-run) would: install " in proc.stdout
    assert "lanscape-atlas-update-oui" in proc.stdout
    assert not (tmp_path / "etc").exists()


def test_setup_env_example_documents_update_oui_weekly() -> None:
    text = (ROOT / "deploy" / "setup.env.example").read_text()
    assert re.search(r"^# UPDATE_OUI_WEEKLY=true$", text, re.M)


def test_readme_documents_the_installer_robustness() -> None:
    text = " ".join((ROOT / "README.md").read_text().split())
    section = text[text.index("### Scripted install and upgrade") :]
    for needle in (
        "UPDATE_OUI_WEEKLY=false",
        "/etc/periodic/weekly/lanscape-atlas-update-oui",
        "/run/lanscape-atlas-setup.lock",
        "rc-service lanscape-atlas zap",
        "git remote set-url",
        "pyvenv.cfg",
    ):
        assert needle in section, needle


# ------------------------------------------- symbolic links in the service user's directory
#
# The service user owns the data directory and can put a link (or a hard link) anywhere
# in it. The script runs as root, so it must refuse to create, chown, chmod or write
# through one. The tests run as an ordinary user: SVC_USER and SVC_GROUP are the current
# user and group, so the chown the script does is one to the owner the file already has.

ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
LINK_REFUSED = "it is a symbolic link. The "


def data_dirs_run(
    shell: list[str], tmp_path: Path, data_dir: Path, *args: str, overrides: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """The real make_data_dirs (called by the stubbed ensure_user) against DATA_DIR."""
    return run_main(
        tmp_path,
        BASE_ENV,
        conf_path(tmp_path),
        set(),
        *args,
        stub_extra={"ensure_user": "make_data_dirs"},
        overrides=(
            f"DATA_DIR='{data_dir}'",
            f"SVC_USER='{ME}'",
            f"SVC_GROUP='{MY_GROUP}'",
            *overrides,
        ),
        shell=tuple(shell),
    )


def outside_dir(tmp_path: Path) -> Path:
    """A directory the script must not change (mode 0755, empty)."""
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(0o755)
    return target


@pytest.mark.parametrize("shell", SHELLS)
def test_data_dirs_are_created_with_their_modes(shell: list[str], tmp_path: Path) -> None:
    data = tmp_path / "var-lib"
    proc = data_dirs_run(shell, tmp_path, data)
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE((data / "floorplan").stat().st_mode) == 0o750
    data.chmod(0o755)
    (data / "floorplan").chmod(0o777)
    shutil.rmtree(tmp_path / "harness")
    assert data_dirs_run(shell, tmp_path, data).returncode == 0  # an existing one is fixed
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE((data / "floorplan").stat().st_mode) == 0o750


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("which", ["floorplan", "data_dir"])
def test_a_linked_data_dir_is_refused(shell: list[str], which: str, tmp_path: Path) -> None:
    target = outside_dir(tmp_path)
    data = tmp_path / "var-lib"
    if which == "floorplan":
        data.mkdir()
        (data / "floorplan").symlink_to(target)
        link = data / "floorplan"
    else:
        data.symlink_to(target)
        link = data
    proc = data_dirs_run(shell, tmp_path, data)
    assert proc.returncode == 1
    assert f"refusing to create or chown {link}: {LINK_REFUSED}" in proc.stderr
    assert link.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("shell", SHELLS)
def test_dry_run_reports_a_linked_floorplan_dir(shell: list[str], tmp_path: Path) -> None:
    target = outside_dir(tmp_path)
    data = tmp_path / "var-lib"
    data.mkdir()
    (data / "floorplan").symlink_to(target)
    proc = data_dirs_run(shell, tmp_path, data, "--dry-run")
    assert proc.returncode == 1
    assert f"PROBLEM: refusing to create or chown {data / 'floorplan'}" in proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_a_link_planted_after_the_check_is_not_followed(
    shell: list[str], kind: str, tmp_path: Path
) -> None:
    # refuse_link is switched off, as if the link appeared just after it looked.
    target = outside_dir(tmp_path)
    data = tmp_path / "var-lib"
    data.mkdir()
    if kind == "symlink":
        (data / "floorplan").symlink_to(target)
    else:
        (data / "floorplan").write_text("not a directory\n")
    proc = data_dirs_run(shell, tmp_path, data, overrides=("refuse_link() { :; }",))
    assert proc.returncode == 1
    assert f"ERROR: refusing to chown {data / 'floorplan'}" in proc.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("hook", ["--write-config", "--check-config"])
def test_a_linked_config_toml_is_refused(shell: list[str], hook: str, tmp_path: Path) -> None:
    target = tmp_path / "outside.toml"
    target.write_text("keep = true\n")
    target.chmod(0o644)
    conf = conf_path(tmp_path)
    conf.symlink_to(target)
    env_file = write_env(tmp_path, BASE_ENV)
    proc = run(shell, "--env", str(env_file), hook, str(conf))
    assert proc.returncode == 1
    assert f"refusing to read or replace {conf}: {LINK_REFUSED}" in proc.stderr
    assert conf.is_symlink()
    assert target.read_text() == "keep = true\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    dry = run(shell, "--env", str(env_file), hook, str(conf), "--dry-run")
    assert dry.returncode == 1
    assert f"PROBLEM: refusing to read or replace {conf}" in dry.stderr


def swapping_script(tmp_path: Path, pattern: str, how: str) -> Path:
    """The script with a mktemp *function* that replaces the temp file whose name matches
    PATTERN by a link to $SWAP_TARGET, as the service user could right after mktemp.

    A function, not a stub on PATH: busybox ash runs its mktemp and ln applets without
    a PATH lookup.
    """
    ln = "ln -s" if how == "symlink" else "ln"
    harness = tmp_path / "swapping" / "deploy" / "setup-alpine.sh"
    harness.parent.mkdir(parents=True)
    shutil.copy(ROOT / "config.example.toml", harness.parent.parent / "config.example.toml")
    shebang, rest = SCRIPT.read_text().split("\n", 1)
    swap = (
        "mktemp() {\n"
        '\t_made=$(command mktemp "$@") || return 1\n'
        f'\tcase $_made in */{pattern}*) rm -f "$_made"; {ln} "$SWAP_TARGET" "$_made" ;; esac\n'
        "\tprintf '%s\\n' \"$_made\"\n"
        "}\n"
    )
    harness.write_text(f"{shebang}\n{swap}{rest}")
    return harness


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
@pytest.mark.parametrize("which", [".config.", ".config-backup."])
def test_a_swapped_temp_file_is_not_written_through(
    shell: list[str], how: str, which: str, tmp_path: Path
) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(shell, tmp_path, BASE_ENV, conf).returncode == 0
    before = conf.read_bytes()
    target = tmp_path / "victim"
    target.write_text("victim\n")
    target.chmod(0o644)
    env = {**os.environ, "SWAP_TARGET": str(target)}
    script = swapping_script(tmp_path, which, how)
    env_file = write_env(tmp_path, {**BASE_ENV, "RETENTION_DAYS": "7"})  # a change: backup
    proc = run(shell, "--env", str(env_file), "--write-config", str(conf), env=env, script=script)
    assert proc.returncode == 1
    assert "ERROR: refusing to write" in proc.stderr or "ERROR: refusing to back up" in proc.stderr
    assert target.read_text() == "victim\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert conf.read_bytes() == before and not conf.is_symlink()
    assert backups(conf) == []
    left = [p.name for p in conf.parent.iterdir() if p.name.startswith(".config")]
    assert left == []  # the EXIT trap removed the swapped name (not its target)


BACKUP_STUBS = {"install_app": "CODE_INSTALLED=1; NEW_COMMIT=abc1234; OLD_COMMIT=0ld0000"}


def backup_run(
    shell: list[str], tmp_path: Path, data: Path, overrides: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """The real backup_db; as_service_user runs its command here, and says so."""
    as_user = 'as_service_user() { echo "AS $SVC_USER" >&2; sh -c "$1"; }'
    return run_main(
        tmp_path,
        BASE_ENV,
        conf_path(tmp_path),
        {"backup_db"},
        stub_extra=BACKUP_STUBS,
        overrides=(
            f"DATA_DIR='{data}'",
            f"BACKUP_DIR='{data}/backups'",
            f"SVC_USER='{ME}'",
            as_user,
            *overrides,
        ),
        shell=tuple(shell),
    )


def make_db(data: Path) -> None:
    data.mkdir(exist_ok=True)
    con = sqlite3.connect(data / "atlas.db")
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()
    con.close()


@pytest.mark.parametrize("shell", SHELLS)
def test_database_backup_is_made_by_the_service_user(shell: list[str], tmp_path: Path) -> None:
    data = tmp_path / "var-lib"
    make_db(data)
    old = data / "backups"
    old.mkdir()
    for i in range(4):  # older backups: two of them go (3 kept with the new one)
        f = old / f"atlas-2020010{i}-000000-aaaaaaa.db"
        f.write_text("")
        os.utime(f, (1_600_000_000 + i, 1_600_000_000 + i))
    proc = backup_run(shell, tmp_path, data)
    assert proc.returncode == 0, proc.stderr
    assert f"AS {ME}" in proc.stderr
    made = [p for p in old.iterdir() if p.name.endswith("-0ld0000.db")]
    assert len(made) == 1 and f"backed up to {made[0]}" in proc.stdout
    assert stat.S_IMODE(made[0].stat().st_mode) == 0o600
    assert sqlite3.connect(made[0]).execute("SELECT x FROM t").fetchone() == (42,)
    assert sorted(p.name for p in old.iterdir()) == sorted(
        [made[0].name, "atlas-20200102-000000-aaaaaaa.db", "atlas-20200103-000000-aaaaaaa.db"]
    )


@pytest.mark.parametrize("shell", SHELLS)
def test_database_backup_creates_its_directory_0700(shell: list[str], tmp_path: Path) -> None:
    data = tmp_path / "var-lib"
    make_db(data)
    proc = backup_run(shell, tmp_path, data)
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE((data / "backups").stat().st_mode) == 0o700


@pytest.mark.parametrize("shell", SHELLS)
def test_a_linked_backup_dir_is_refused(shell: list[str], tmp_path: Path) -> None:
    target = outside_dir(tmp_path)
    data = tmp_path / "var-lib"
    make_db(data)
    (data / "backups").symlink_to(target)
    proc = backup_run(shell, tmp_path, data)
    assert proc.returncode == 1
    link = data / "backups"
    assert f"refusing to write database backups into {link}: {LINK_REFUSED}" in proc.stderr
    assert "AS " not in proc.stderr
    assert list(target.iterdir()) == []
    assert STARTED_AGAIN in proc.stderr or NOT_STARTED in proc.stderr  # the trap restarts it


def test_root_never_changes_ownership_by_name_in_the_data_dir() -> None:
    text = SCRIPT.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\bchown\s+[\"'$-]", code)  # only os.fchown, on an O_NOFOLLOW descriptor
    assert not re.search(r"install -d[^\n]*-o ", code)
    assert "cp -p" not in code
    backup = function_body("backup_db")
    assert "as_service_user" in backup and "chmod 0600" not in backup


def test_readme_documents_the_symlink_rules_and_the_capabilities() -> None:
    text = " ".join((ROOT / "README.md").read_text().split())
    section = text[text.index("## Remote access and security") :]
    for needle in ("symbolic link", "file capabilities", "not tested on Alpine"):
        assert needle in section, needle
