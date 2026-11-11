from __future__ import annotations

from pathlib import Path

import pytest

from lanscape_atlas.config import load_settings

ROOT = Path(__file__).resolve().parent.parent


def test_defaults_without_file(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "missing.toml", env={})
    assert s.data_dir == Path("/var/lib/lanscape-atlas")
    assert (s.bind_host, s.bind_port, s.scan_interface) == ("127.0.0.1", 8720, "eth0")
    assert s.api_token is None and s.router is None
    assert (s.observation_retention_days, s.ip_match_window_min) == (30, 10)
    assert s.collectors["asusrouter"].enabled is False
    assert s.collectors["arp"].interval_s == 300 and s.collectors["arp"].enabled
    assert s.collectors["nmap_light"].cron == "17 3 * * *"
    assert s.collectors["nmap_light"].interval_s is None
    assert s.collectors["nmap_light"].timeout_s == 1800


def test_data_dir_env_sets_default_and_config_path(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("bind_port = 9000\n")
    s = load_settings(env={"LANSCAPE_DATA_DIR": str(tmp_path)})
    assert s.data_dir == tmp_path and s.bind_port == 9000


def test_env_overrides_password_and_token(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(
        'api_token = "file"\n[router]\nhost = "192.0.2.1"\nusername = "u"\npassword = "x"\n'
    )
    s = load_settings(p, env={"LANSCAPE_ROUTER_PASSWORD": "envpw", "LANSCAPE_API_TOKEN": "tok"})
    assert s.router is not None and s.router.password == "envpw" and s.router.use_ssl is True
    assert s.api_token == "tok"
    assert s.collectors["asusrouter"].enabled is True


def test_empty_token_is_none_and_empty_router_host_disables(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('api_token = ""\n[router]\nhost = ""\n[collectors.asusrouter]\nenabled = true\n')
    s = load_settings(p, env={})
    assert s.api_token is None and s.router is None
    assert s.collectors["asusrouter"].enabled is False


def test_collector_override(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text("[collectors.arp]\nenabled = false\ninterval_s = 60\n")
    s = load_settings(p, env={})
    assert s.collectors["arp"].enabled is False and s.collectors["arp"].interval_s == 60
    assert s.collectors["arp"].timeout_s == 60


@pytest.mark.parametrize(
    "text,key",
    [
        ('bind_port = "80"\n', "bind_port"),
        ("bind_port = true\n", "bind_port"),
        ('[router]\nhost = "h"\nuse_ssl = "yes"\n', "use_ssl"),
        ("[collectors.arp]\ninterval_s = 1.5\n", "interval_s"),
        ("[collectors.bogus]\nenabled = true\n", "bogus"),
        ('[collectors.arp]\ntimeout_s = "x"\n', "timeout_s"),
    ],
)
def test_bad_types_name_key(tmp_path: Path, text: str, key: str) -> None:
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match=key):
        load_settings(p, env={})


def test_settings_frozen(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "x.toml", env={})
    with pytest.raises(AttributeError):
        s.bind_port = 1  # type: ignore[misc]


def test_example_config_parses() -> None:
    s = load_settings(ROOT / "config.example.toml", env={})
    assert s.router is not None and s.router.host == "192.0.2.1" and s.router.password == ""
    assert s.api_token is None
    assert s.collectors["asusrouter"].enabled is True
    assert s.collectors["mdns"].interval_s == 60 and s.collectors["ssdp"].timeout_s == 10


def test_secrets_not_in_repr(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('[router]\nhost = "192.0.2.1"\n')
    s = load_settings(
        p, env={"LANSCAPE_ROUTER_PASSWORD": "s3cret-test", "LANSCAPE_API_TOKEN": "s3cret-test"}
    )
    assert s.router is not None and s.api_token == "s3cret-test"
    assert "s3cret-test" not in repr(s)
    assert "s3cret-test" not in repr(s.router)


def test_allowed_hosts_setting(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.toml", env={}).allowed_hosts == ()
    p = tmp_path / "c.toml"
    p.write_text('allowed_hosts = ["atlas.example.com", "atlas.lan:*"]\n')
    s = load_settings(p, env={})
    assert s.allowed_hosts == ("atlas.example.com", "atlas.lan:*")
    for bad in ('allowed_hosts = "atlas.example.com"\n', "allowed_hosts = [1]\n"):
        p.write_text(bad)
        with pytest.raises(ValueError, match="allowed_hosts"):
            load_settings(p, env={})


def _ha(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(body)
    return p


def test_home_assistant_absent_is_none(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.toml", env={}).home_assistant is None
    p = _ha(tmp_path, '[home_assistant]\nurl = ""\n')
    assert load_settings(p, env={}).home_assistant is None


def test_home_assistant_loaded(tmp_path: Path) -> None:
    p = _ha(
        tmp_path,
        '[home_assistant]\nurl = "http://192.0.2.10:8123/"\ntoken = "tok"\nverify_tls = false\n',
    )
    ha = load_settings(p, env={}).home_assistant
    assert ha is not None
    assert (ha.url, ha.token, ha.verify_tls) == ("http://192.0.2.10:8123", "tok", False)


def test_ha_token_env_overrides_file(tmp_path: Path) -> None:
    p = _ha(tmp_path, '[home_assistant]\nurl = "https://192.0.2.10:8123"\ntoken = "file"\n')
    ha = load_settings(p, env={"LANSCAPE_HA_TOKEN": "envtok"}).home_assistant
    assert ha is not None and ha.token == "envtok"


def test_ha_url_must_be_http_or_https(tmp_path: Path) -> None:
    p = _ha(tmp_path, '[home_assistant]\nurl = "ftp://192.0.2.10"\ntoken = "t"\n')
    with pytest.raises(ValueError, match="home_assistant.url"):
        load_settings(p, env={})


def test_ha_url_without_token_is_error(tmp_path: Path) -> None:
    p = _ha(tmp_path, '[home_assistant]\nurl = "http://192.0.2.10:8123"\ntoken = ""\n')
    with pytest.raises(ValueError, match="home_assistant.token"):
        load_settings(p, env={"LANSCAPE_HA_TOKEN": ""})


def test_bluetooth_defaults(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "missing.toml", env={})
    assert (s.bluetooth.retention_days, s.bluetooth.flush_s) == (14, 60)
    assert s.bluetooth.snapshot_s == 30
    assert s.bluetooth.candidate_min_rssi == -80


def test_bluetooth_candidate_min_rssi(tmp_path: Path) -> None:
    s = load_settings(_ha(tmp_path, "[bluetooth]\ncandidate_min_rssi = -72\n"), env={})
    assert s.bluetooth.candidate_min_rssi == -72
    for bad in ("1", "-128", "-70.5", '"-70"'):
        with pytest.raises(ValueError, match="candidate_min_rssi"):
            load_settings(_ha(tmp_path, f"[bluetooth]\ncandidate_min_rssi = {bad}\n"), env={})


def test_bluetooth_retention_must_be_positive(tmp_path: Path) -> None:
    for body in (
        "[bluetooth]\nretention_days = 0\n",
        "[bluetooth]\nretention_days = -3\n",
        "[bluetooth]\nflush_s = 0\n",
        '[bluetooth]\nretention_days = "7"\n',
        "[bluetooth]\nsnapshot_s = 0\n",
        "[bluetooth]\nsnapshot_s = -60\n",
        "[bluetooth]\nsnapshot_s = 1.5\n",
    ):
        with pytest.raises(ValueError, match="bluetooth"):
            load_settings(_ha(tmp_path, body), env={})
    s = load_settings(
        _ha(tmp_path, "[bluetooth]\nretention_days = 7\nflush_s = 30\nsnapshot_s = 45\n"), env={}
    )
    assert (s.bluetooth.retention_days, s.bluetooth.flush_s) == (7, 30)
    assert s.bluetooth.snapshot_s == 45


def test_example_config_documents_snapshot_s() -> None:
    text = (Path(__file__).parent.parent / "config.example.toml").read_text()
    assert "snapshot_s = 30" in text
    assert "candidate_min_rssi = -80" in text


def test_settings_repr_hides_ha_token(tmp_path: Path) -> None:
    p = _ha(tmp_path, '[home_assistant]\nurl = "http://192.0.2.10:8123"\ntoken = "s3cret-ha"\n')
    s = load_settings(p, env={})
    assert s.home_assistant is not None and s.home_assistant.token == "s3cret-ha"
    assert "s3cret-ha" not in repr(s) and "s3cret-ha" not in repr(s.home_assistant)
