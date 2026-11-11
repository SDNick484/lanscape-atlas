"""Tests for deploy/setup-alpine.sh: env parsing, config rendering and dry-run safety.

The script itself targets Alpine + OpenRC as root, so these tests only drive the parts that
run anywhere: ``--render-config`` (parse setup.env, render config.toml from
config.example.toml) and ``--dry-run`` (inspect and report, change nothing).
"""

from __future__ import annotations

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
    )
    if shutil.which(cmd[0])
]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")


def write_env(tmp_path: Path, data: dict[str, str], mode: int = 0o600) -> Path:
    path = tmp_path / "setup.env"
    path.write_text("# test env\n" + "".join(f"{k}={v}\n" for k, v in data.items()))
    path.chmod(mode)
    return path


def run(shell: list[str], *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*shell, str(SCRIPT), *args], capture_output=True, text=True, check=check, timeout=60
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
