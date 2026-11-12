"""Settings loaded from ``config.toml`` plus environment overrides."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = "/var/lib/lanscape-atlas"


@dataclass(frozen=True)
class RouterSettings:
    host: str
    username: str
    password: str = field(repr=False)
    use_ssl: bool = True


@dataclass(frozen=True)
class HomeAssistantSettings:
    url: str
    token: str = field(repr=False)
    verify_tls: bool = True


@dataclass(frozen=True)
class BluetoothSettings:
    retention_days: int = 14
    flush_s: int = 60
    snapshot_s: int = 30  # re-subscribe interval: HA replays its advert history each time
    # Candidates whose best RSSI is below this (dBm) are listed only on request.
    candidate_min_rssi: int = -80


@dataclass(frozen=True)
class CollectorSettings:
    enabled: bool
    interval_s: int | None
    cron: str | None
    timeout_s: float


def _default_collectors() -> dict[str, CollectorSettings]:
    return {
        "asusrouter": CollectorSettings(False, 120, None, 30),
        "arp": CollectorSettings(True, 300, None, 60),
        "mdns": CollectorSettings(True, 60, None, 10),
        "ssdp": CollectorSettings(True, 600, None, 10),
        "nmap_light": CollectorSettings(True, None, "17 3 * * *", 1800),
    }


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    bind_host: str = "127.0.0.1"
    bind_port: int = 8720
    scan_interface: str = "eth0"
    api_token: str | None = field(default=None, repr=False)
    observation_retention_days: int = 30
    ip_match_window_min: int = 10
    router: RouterSettings | None = None
    home_assistant: HomeAssistantSettings | None = None
    bluetooth: BluetoothSettings = field(default_factory=BluetoothSettings)
    collectors: dict[str, CollectorSettings] = field(default_factory=_default_collectors)
    # Extra Host header values the whole app accepts when no api_token is set (DNS-rebinding
    # protection); "name:*" allows any port, "*" turns the check off.
    allowed_hosts: tuple[str, ...] = ()


def _get(
    d: Mapping[str, Any], key: str, typ: type | tuple[type, ...], default: Any, where: str = ""
) -> Any:
    if key not in d:
        return default
    v = d[key]
    types = typ if isinstance(typ, tuple) else (typ,)
    ok = isinstance(v, types) and not (isinstance(v, bool) and bool not in types)
    if not ok:
        names = "/".join(t.__name__ for t in types)
        raise ValueError(f"config key {where}{key!r} must be {names}, got {type(v).__name__}")
    return v


def _table(d: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    v = d.get(key, {})
    if not isinstance(v, dict):
        raise ValueError(f"config key {key!r} must be a table")
    return v


def load_settings(path: Path | None = None, env: Mapping[str, str] = os.environ) -> Settings:
    default_dir = Path(env.get("LANSCAPE_DATA_DIR") or DEFAULT_DATA_DIR)
    cfg_path = path if path is not None else default_dir / "config.toml"
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        with cfg_path.open("rb") as f:
            raw = tomllib.load(f)

    token = env.get("LANSCAPE_API_TOKEN", _get(raw, "api_token", str, ""))
    router: RouterSettings | None = None
    rt = _table(raw, "router")
    host = _get(rt, "host", str, "", "router.")
    if host:
        router = RouterSettings(
            host=host,
            username=_get(rt, "username", str, "admin", "router."),
            password=env.get("LANSCAPE_ROUTER_PASSWORD", _get(rt, "password", str, "", "router.")),
            use_ssl=_get(rt, "use_ssl", bool, True, "router."),
        )

    home_assistant: HomeAssistantSettings | None = None
    ha = _table(raw, "home_assistant")
    ha_url = _get(ha, "url", str, "", "home_assistant.").rstrip("/")
    if ha_url:
        if not ha_url.startswith(("http://", "https://")):
            raise ValueError("config key 'home_assistant.url' must start with http:// or https://")
        ha_token = env.get("LANSCAPE_HA_TOKEN", _get(ha, "token", str, "", "home_assistant."))
        if not ha_token:
            raise ValueError(
                "config key 'home_assistant.token' (or LANSCAPE_HA_TOKEN) is required "
                "when home_assistant.url is set"
            )
        home_assistant = HomeAssistantSettings(
            url=ha_url,
            token=ha_token,
            verify_tls=_get(ha, "verify_tls", bool, True, "home_assistant."),
        )

    bt = _table(raw, "bluetooth")
    bluetooth = BluetoothSettings(
        retention_days=_get(bt, "retention_days", int, 14, "bluetooth."),
        flush_s=_get(bt, "flush_s", int, 60, "bluetooth."),
        snapshot_s=_get(bt, "snapshot_s", int, 30, "bluetooth."),
        candidate_min_rssi=_get(bt, "candidate_min_rssi", int, -80, "bluetooth."),
    )
    if not -127 <= bluetooth.candidate_min_rssi <= 0:
        raise ValueError(
            "config key 'bluetooth.candidate_min_rssi' must be between -127 and 0 (dBm), "
            f"got {bluetooth.candidate_min_rssi}"
        )
    for key, val in (
        ("retention_days", bluetooth.retention_days),
        ("flush_s", bluetooth.flush_s),
        ("snapshot_s", bluetooth.snapshot_s),
    ):
        if val < 1:
            raise ValueError(f"config key 'bluetooth.{key}' must be a positive integer, got {val}")

    collectors = _default_collectors()
    for name, ct in _table(raw, "collectors").items():
        if name not in collectors:
            raise ValueError(f"unknown collector {name!r} in [collectors]")
        if not isinstance(ct, dict):
            raise ValueError(f"config key 'collectors.{name}' must be a table")
        base = collectors[name]
        w = f"collectors.{name}."
        interval = _get(ct, "interval_s", int, base.interval_s, w)
        cron = _get(ct, "cron", str, base.cron, w)
        collectors[name] = CollectorSettings(
            enabled=_get(ct, "enabled", bool, base.enabled, w),
            interval_s=interval,
            cron=cron,
            timeout_s=float(_get(ct, "timeout_s", (int, float), base.timeout_s, w)),
        )
    # asusrouter has nothing to talk to without a [router] section.
    if router is None:
        ar = collectors["asusrouter"]
        collectors["asusrouter"] = replace(ar, enabled=False)
    elif "asusrouter" not in _table(raw, "collectors"):
        collectors["asusrouter"] = replace(collectors["asusrouter"], enabled=True)

    hosts = _get(raw, "allowed_hosts", list, [])
    if not all(isinstance(h, str) and h for h in hosts):
        raise ValueError("config key 'allowed_hosts' must be a list of non-empty strings")

    return Settings(
        data_dir=Path(_get(raw, "data_dir", str, str(default_dir))),
        bind_host=_get(raw, "bind_host", str, "127.0.0.1"),
        bind_port=_get(raw, "bind_port", int, 8720),
        scan_interface=_get(raw, "scan_interface", str, "eth0"),
        api_token=token or None,
        observation_retention_days=_get(raw, "observation_retention_days", int, 30),
        ip_match_window_min=_get(raw, "ip_match_window_min", int, 10),
        router=router,
        home_assistant=home_assistant,
        bluetooth=bluetooth,
        collectors=collectors,
        allowed_hosts=tuple(hosts),
    )
