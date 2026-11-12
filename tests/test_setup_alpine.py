"""Tests for deploy/setup-alpine.sh: env parsing, config rendering and dry-run safety.

The script itself targets Alpine + OpenRC as root, so these tests only drive the parts that
run anywhere: ``--render-config`` (parse setup.env, render config.toml from
config.example.toml) and ``--dry-run`` (inspect and report, change nothing).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
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
    assert cfg["observation_retention_days"] == 14
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


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    proc = subprocess.run(
        ["shellcheck", "--shell=sh", str(SCRIPT)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stdout


def test_deploy_script_creates_floorplan_dir() -> None:
    text = SCRIPT.read_text()
    pattern = r'install -d -m 0750 -o "\$SVC_USER" -g "\$SVC_USER" "\$DATA_DIR/floorplan"'
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

CONF_HEADER = "# Generated by deploy/setup-alpine.sh from setup.env; edits are overwritten."
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


def test_write_config_stops_on_comment_only_edit(tmp_path: Path) -> None:
    conf = conf_path(tmp_path)
    assert write_conf(["sh"], tmp_path, BASE_ENV, conf).returncode == 0
    conf.write_text(conf.read_text() + "# a note of mine\n")
    edited = conf.read_bytes()
    proc = write_conf(["sh"], tmp_path, BASE_ENV, conf)
    assert proc.returncode != 0
    assert "config.toml was edited by hand" in proc.stderr
    assert "no setting" in proc.stderr
    assert conf.read_bytes() == edited


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
        (BASE_ENV, ("-80", "-70"), BASE_ENV, True, "bluetooth.candidate_min_rssi"),
        # ...unless setup.env now sets that value.
        (BASE_ENV, ("-80", "-70"), {**BASE_ENV, "BLE_CANDIDATE_MIN_RSSI": "-70"}, False, None),
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
        legacy = legacy.replace(
            f"candidate_min_rssi = {edit[0]}", f"candidate_min_rssi = {edit[1]}"
        )
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
    "header_changed": lambda t: t.replace("edits are overwritten", "edits are kept", 1),
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
    assert (early.returncode == 0) == (late.returncode == 0), (early.stderr, late.stderr)
    assert (early.returncode == 0) == (tamper == "clean")


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
