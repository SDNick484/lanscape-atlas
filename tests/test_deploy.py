"""Static checks on the OpenRC service file and README install steps (I5)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = (ROOT / "deploy" / "openrc" / "lanscape-atlas").read_text()
README = (ROOT / "README.md").read_text()
LOG = "/var/log/lanscape-atlas.log"


def test_init_script_runs_nmap_privileged_with_caps() -> None:
    assert 'capabilities="^cap_net_raw,^cap_net_admin"' in INIT
    assert "export NMAP_PRIVILEGED=1" in INIT
    assert "0.45" in INIT


def test_init_script_logs_to_file_owned_by_service_user() -> None:
    assert f'output_log="{LOG}"' in INIT
    assert f'error_log="{LOG}"' in INIT
    assert f"checkpath -f -m 0640 -o lanscape:lanscape {LOG}" in INIT


def test_readme_creates_group_and_user() -> None:
    assert "addgroup -S lanscape" in README
    assert "adduser -S -D -H -G lanscape -h /var/lib/lanscape-atlas lanscape" in README


def test_readme_install_steps() -> None:
    assert "cd lanscape-atlas" in README
    assert "bind_host" in README.split("## Configuration")[0]
    assert "rc-service lanscape-atlas restart" in README
    cron = "su -s /bin/sh lanscape -c '/opt/lanscape-atlas/venv/bin/lanscape-atlas update-oui'"
    assert f"17 4 * * 0 {cron}" in README
    assert "/etc/crontabs/root" in README
