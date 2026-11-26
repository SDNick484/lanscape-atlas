"""Static checks on the OpenRC service file and README install steps (I5)."""

from __future__ import annotations

import re
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


def test_init_script_limits_respawns() -> None:
    # A crash loop must end (and stop filling the log): supervise-daemon gives up after
    # respawn_max respawns within respawn_period seconds, respawn_delay apart.
    settings = dict(re.findall(r"^(respawn_\w+)=(\d+)$", INIT, re.M))
    assert set(settings) == {"respawn_delay", "respawn_max", "respawn_period"}
    delay, most, period = (
        int(settings[k]) for k in ("respawn_delay", "respawn_max", "respawn_period")
    )
    assert delay >= 1 and most >= 1
    assert period > delay * most  # otherwise the limit can never be reached


def test_logrotate_has_a_size_cap() -> None:
    conf = (ROOT / "deploy" / "logrotate" / "lanscape-atlas").read_text()
    assert f"{LOG} {{" in conf
    assert re.search(r"^\s+maxsize \d+M$", conf, re.M)
    assert re.search(r"^\s+copytruncate$", conf, re.M)


def test_readme_manual_install_rotates_the_log() -> None:
    install = README.split("## Install on an Alpine LXC", 1)[1].split("```", 2)[1]
    assert "apk add python3 py3-pip git arp-scan nmap logrotate" in install
    rule = "install -m 0644 deploy/logrotate/lanscape-atlas /etc/logrotate.d/lanscape-atlas"
    assert rule in install
