#!/bin/sh
# setup-alpine.sh - install or upgrade lanscape-atlas on Alpine Linux (OpenRC).
#
# Safe to re-run: every step checks the current state first, so the same command
# both installs and upgrades. Run as root inside a container that is bridged onto
# the LAN it scans. Nothing outside the paths listed below is touched, and no
# other service is stopped, restarted or upgraded.
#
#   sh setup-alpine.sh --dry-run     inspect and report; change nothing
#   sh setup-alpine.sh               install / upgrade
#   sh setup-alpine.sh --ref v0.2.0  deploy a tag, branch or commit instead of main
#
# Settings come from a root-owned, mode 0600 env file (default
# /etc/lanscape-atlas/setup.env); see deploy/setup.env.example. That file holds
# secrets and must never be committed. config.toml is re-rendered from it (and
# from config.example.toml) on every run, so edit the env file, not config.toml.
# A hand edit to config.toml stops the run before anything is overwritten (see
# render_config); --discard-config-edits overwrites it anyway. Each replaced
# config.toml is first copied to config.toml.YYYYmmdd-HHMMSS (UTC, newest 5 kept).
#
# Paths:
#   /opt/lanscape-atlas/{src,venv}          code (root-owned, read-only to the service)
#   /etc/lanscape-atlas/                    setup.env, deploy key, known_hosts (root, 0700)
#   /var/lib/lanscape-atlas/                data, config.toml (lanscape, 0700/0600), backups/,
#                                           config.toml.YYYYmmdd-HHMMSS (config backups)
#   /var/log/lanscape-atlas.log             service log (rotated weekly)
#   /etc/init.d/lanscape-atlas              OpenRC service
#   /etc/logrotate.d/lanscape-atlas         log rotation
#
# Exit status: 0 ok, 1 error or dry-run problems, 2 smoke test failed (service is
# running), 3 deploy key must be added to the repository first.

set -eu
umask 022

APP=lanscape-atlas
SVC_USER=lanscape
PREFIX=/opt/$APP
SRC=$PREFIX/src
VENV=$PREFIX/venv
STAMP=$PREFIX/.deployed-commit
ETC_DIR=/etc/$APP
DEFAULT_ENV_FILE=$ETC_DIR/setup.env
DEPLOY_KEY=$ETC_DIR/deploy_key
KNOWN_HOSTS=$ETC_DIR/known_hosts
DATA_DIR=/var/lib/$APP
CONF=$DATA_DIR/config.toml
BACKUP_DIR=$DATA_DIR/backups
BACKUPS_KEPT=3
CONFIG_BACKUPS_KEPT=5
# render_config exits with this status when the existing config.toml was edited by hand.
HAND_EDITED_RC=3
HAND_EDITED_MSG="config.toml was edited by hand; move these into setup.env, or re-run with --discard-config-edits to overwrite (the current file is left unchanged)"
LOG_FILE=/var/log/$APP.log
INIT_FILE=/etc/init.d/$APP
LOGROTATE_FILE=/etc/logrotate.d/$APP
MIN_OPENRC="0.45"

DRY_RUN=0
ENV_FILE=$DEFAULT_ENV_FILE
REF_ARG=""
RENDER_OUT=""
EXISTING_CONF=""
CONFIG_HOOK=""
DISCARD_CONFIG_EDITS=0
CONF_CHOWN=1
PROBLEMS=0
# Carried across the re-exec below so a failure can still say the service was left stopped.
STOPPED=${LANSCAPE_SETUP_STOPPED:-0}
SCRIPT_PATH=$0
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

# Settings (defaults; overridden by the env file).
REPO_URL=""
REF="main"
ROUTER_HOST=""
ROUTER_USER="admin"
ROUTER_PASSWORD=""
ROUTER_USE_SSL="true"
API_TOKEN=""
SCAN_INTERFACE=""
BIND_HOST=""
BIND_PORT="8720"
ALLOWED_HOSTS=""
ASUSROUTER_INTERVAL_S="300"
RETENTION_DAYS="14"
HA_URL=""
HA_TOKEN=""
HA_VERIFY_TLS="true"
BLE_CANDIDATE_MIN_RSSI=""

# ------------------------------------------------------------------ output

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

# Fatal on a real run; recorded and reported at the end on --dry-run.
problem() {
	if [ "$DRY_RUN" = 1 ]; then
		printf 'PROBLEM: %s\n' "$*" >&2
		PROBLEMS=$((PROBLEMS + 1))
	else
		die "$*"
	fi
}

# act "what this does" cmd args...   (runs cmd, or just reports it on --dry-run)
act() {
	desc=$1
	shift
	if [ "$DRY_RUN" = 1 ]; then
		note "(dry-run) would: $desc"
	else
		note "$desc"
		"$@"
	fi
}

on_exit() {
	rc=$?
	if [ "$rc" -ne 0 ] && [ "$STOPPED" = 1 ]; then
		warn "failed after stopping $APP; it was NOT restarted. Fix the error and re-run."
	fi
}
trap on_exit EXIT

usage() {
	cat <<EOF
Usage: sh setup-alpine.sh [--dry-run] [--env FILE] [--ref REF] [--discard-config-edits]

  --dry-run    inspect the system and report what would change; change nothing
  --env FILE   settings file (default $DEFAULT_ENV_FILE, root-owned, mode 0600)
  --ref REF    branch, tag or commit to deploy (default: REF from the env file, else main)
  --discard-config-edits
               overwrite config.toml even if it was edited by hand (a backup is kept)
  -h, --help   show this help
EOF
}

# --------------------------------------------------------------- arguments

while [ $# -gt 0 ]; do
	case $1 in
	--dry-run) DRY_RUN=1 ;;
	--env)
		[ $# -ge 2 ] || die "--env needs a file"
		ENV_FILE=$2
		shift
		;;
	--ref)
		[ $# -ge 2 ] || die "--ref needs a value"
		REF_ARG=$2
		shift
		;;
	--render-config) # internal (tests): render config.toml to FILE and exit
		[ $# -ge 2 ] || die "--render-config needs a file"
		RENDER_OUT=$2
		shift
		;;
	--existing) # internal (tests): existing config.toml (api_token kept, checked for hand edits)
		[ $# -ge 2 ] || die "--existing needs a file"
		EXISTING_CONF=$2
		shift
		;;
	--write-config) # internal (tests): run the config step against FILE (no chown or app check) and exit
		[ $# -ge 2 ] || die "--write-config needs a file"
		CONFIG_HOOK=$2
		shift
		;;
	--discard-config-edits) DISCARD_CONFIG_EDITS=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	*) die "unknown argument: $1 (try --help)" ;;
	esac
	shift
done

# ----------------------------------------------------------------- env file

# Parsed line by line, never sourced or eval'd: the file can hold any characters.
# Values are never printed; error messages name the key and line number only.
load_env() {
	if [ ! -f "$ENV_FILE" ]; then
		problem "env file not found: $ENV_FILE (see deploy/setup.env.example)"
		return 0
	fi
	mode=$(stat -c '%a' "$ENV_FILE")
	owner=$(stat -c '%u' "$ENV_FILE")
	[ "$mode" = 600 ] || problem "$ENV_FILE must be mode 0600 (is $mode): chmod 600 $ENV_FILE"
	[ "$owner" = "$(id -u)" ] || problem "$ENV_FILE must be owned by the user running this script"

	cr=$(printf '\r')
	n=0
	while IFS= read -r line || [ -n "$line" ]; do
		n=$((n + 1))
		line=${line%"$cr"}
		line=${line#"${line%%[![:space:]]*}"}
		line=${line%"${line##*[![:space:]]}"}
		case $line in
		'' | '#'*) continue ;;
		esac
		case $line in
		*=*) ;;
		*)
			problem "$ENV_FILE:$n: expected KEY=VALUE"
			continue
			;;
		esac
		key=${line%%=*}
		val=${line#*=}
		key=${key%"${key##*[![:space:]]}"}
		val=${val#"${val%%[![:space:]]*}"}
		case $val in
		\"*\")
			val=${val#\"}
			val=${val%\"}
			;;
		\'*\')
			val=${val#\'}
			val=${val%\'}
			;;
		esac
		case $key in
		REPO_URL) REPO_URL=$val ;;
		REF) REF=$val ;;
		ROUTER_HOST) ROUTER_HOST=$val ;;
		ROUTER_USER) ROUTER_USER=$val ;;
		ROUTER_PASSWORD) ROUTER_PASSWORD=$val ;;
		ROUTER_USE_SSL) ROUTER_USE_SSL=$val ;;
		API_TOKEN) API_TOKEN=$val ;;
		SCAN_INTERFACE) SCAN_INTERFACE=$val ;;
		BIND_HOST) BIND_HOST=$val ;;
		BIND_PORT) BIND_PORT=$val ;;
		ALLOWED_HOSTS) ALLOWED_HOSTS=$val ;;
		ASUSROUTER_INTERVAL_S) ASUSROUTER_INTERVAL_S=$val ;;
		RETENTION_DAYS) RETENTION_DAYS=$val ;;
		HA_URL) HA_URL=$val ;;
		HA_TOKEN) HA_TOKEN=$val ;;
		HA_VERIFY_TLS) HA_VERIFY_TLS=$val ;;
		BLE_CANDIDATE_MIN_RSSI) BLE_CANDIDATE_MIN_RSSI=$val ;;
		*) problem "$ENV_FILE:$n: unknown key '$key'" ;;
		esac
	done <"$ENV_FILE"
	[ -z "$REF_ARG" ] || REF=$REF_ARG
}

is_uint() {
	case $1 in
	'' | *[!0-9]*) return 1 ;;
	esac
	return 0
}

# is_dbm VALUE: a whole number from -127 to 0 (an optional minus, then 1-3 digits).
is_dbm() {
	dbm_digits=${1#-}
	if ! is_uint "$dbm_digits" || [ "${#dbm_digits}" -gt 3 ]; then
		return 1
	fi
	if [ "$dbm_digits" = "$1" ]; then
		[ "$dbm_digits" -eq 0 ]
	else
		[ "$dbm_digits" -le 127 ]
	fi
}

validate_env() {
	[ -n "$REPO_URL" ] || problem "REPO_URL is required"
	case $REPO_URL in
	git@* | ssh://* | https://*) ;;
	'') ;;
	*) problem "REPO_URL must start with git@, ssh:// or https://" ;;
	esac
	case $REPO_URL in
	*[[:space:]]*) problem "REPO_URL must not contain whitespace" ;;
	esac
	case $REF in
	'' | -* | *[!A-Za-z0-9._/-]*) problem "REF must be a branch, tag or commit name (letters, digits, . _ / -)" ;;
	esac
	[ -n "$SCAN_INTERFACE" ] || problem "SCAN_INTERFACE is required (for example eth0)"
	case $SCAN_INTERFACE in
	*[!A-Za-z0-9._-]*) problem "SCAN_INTERFACE has unexpected characters" ;;
	esac
	[ -n "$BIND_HOST" ] || problem "BIND_HOST is required (the address the UI listens on)"
	case $BIND_HOST in
	*[!0-9.]*) problem "BIND_HOST must be an IPv4 address (use 0.0.0.0 to listen on all)" ;;
	esac
	if ! is_uint "$BIND_PORT" || [ "$BIND_PORT" -lt 1 ] || [ "$BIND_PORT" -gt 65535 ]; then
		problem "BIND_PORT must be 1-65535"
	fi
	if ! is_uint "$ASUSROUTER_INTERVAL_S" || [ "$ASUSROUTER_INTERVAL_S" -lt 1 ]; then
		problem "ASUSROUTER_INTERVAL_S must be a positive integer"
	fi
	if ! is_uint "$RETENTION_DAYS" || [ "$RETENTION_DAYS" -lt 1 ]; then
		problem "RETENTION_DAYS must be a positive integer"
	fi
	case $ROUTER_USE_SSL in
	true | false) ;;
	*) problem "ROUTER_USE_SSL must be true or false" ;;
	esac
	if [ -n "$ROUTER_HOST" ]; then
		[ -n "$ROUTER_USER" ] || problem "ROUTER_USER is required when ROUTER_HOST is set"
		[ -n "$ROUTER_PASSWORD" ] || problem "ROUTER_PASSWORD is required when ROUTER_HOST is set"
	fi
	case $HA_VERIFY_TLS in
	true | false) ;;
	*) problem "HA_VERIFY_TLS must be true or false" ;;
	esac
	if [ -n "$HA_URL" ]; then
		case $HA_URL in
		http://* | https://*) ;;
		*) problem "HA_URL must start with http:// or https://" ;;
		esac
		case $HA_URL in
		*[[:space:]]*) problem "HA_URL must not contain whitespace" ;;
		esac
		[ -n "$HA_TOKEN" ] || problem "HA_TOKEN is required when HA_URL is set"
	elif [ -n "$HA_TOKEN" ]; then
		problem "HA_URL is required when HA_TOKEN is set"
	fi
	if [ -n "$BLE_CANDIDATE_MIN_RSSI" ] && ! is_dbm "$BLE_CANDIDATE_MIN_RSSI"; then
		problem "BLE_CANDIDATE_MIN_RSSI must be a whole number of dBm from -127 to 0 (e.g. -80)"
	fi
	if [ -n "$API_TOKEN" ] && [ "${#API_TOKEN}" -lt 16 ]; then
		problem "API_TOKEN must be at least 16 characters (or leave it unset to generate one)"
	fi
}

uses_ssh() {
	case $REPO_URL in
	git@* | ssh://*) return 0 ;;
	esac
	return 1
}

# ---------------------------------------------------------- config rendering

# render_config OUT EXAMPLE [EXISTING]
#
# Fills config.example.toml from the env settings. Every key it sets must exist in
# the example (so an example that drifts from this script fails loudly), and the
# result is parsed again and compared with what was asked for before it is written.
# Strings go through json.dumps, whose output is a valid TOML basic string, so
# passwords with quotes or backslashes cannot break the file. Secrets travel in
# the environment of this one python process, never on a command line.
#
# Line 2 of the result is "# sha256: <hex>", the SHA-256 of everything after it.
# Before OUT is written, EXISTING (when given) is checked for hand edits:
#   - it has a sha256 line: hand-edited unless the line is valid and matches;
#   - it has none (an older script wrote it, or a person did): hand-edited when one
#     of its settings would be dropped by the new render, or would get a different
#     value that setup.env does not set in this run (keys the render writes from
#     setup.env, including the script's defaults such as BIND_PORT, follow setup.env).
# A hand-edited EXISTING lists the settings it would lose or change, by name only
# (never values) with the setup.env key for each, and exits $HAND_EDITED_RC without
# writing OUT; with --discard-config-edits it lists them and carries on.
render_config() {
	CFG_OUT=$1 CFG_EXAMPLE=$2 CFG_EXISTING=${3:-} CFG_DISCARD=$DISCARD_CONFIG_EDITS \
		CFG_HAND_EDITED_RC=$HAND_EDITED_RC \
		CFG_BIND_HOST=$BIND_HOST CFG_BIND_PORT=$BIND_PORT CFG_SCAN_INTERFACE=$SCAN_INTERFACE \
		CFG_RETENTION_DAYS=$RETENTION_DAYS CFG_ALLOWED_HOSTS=$ALLOWED_HOSTS CFG_API_TOKEN=$API_TOKEN \
		CFG_ROUTER_HOST=$ROUTER_HOST CFG_ROUTER_USER=$ROUTER_USER CFG_ROUTER_PASSWORD=$ROUTER_PASSWORD \
		CFG_ROUTER_USE_SSL=$ROUTER_USE_SSL CFG_ASUS_INTERVAL=$ASUSROUTER_INTERVAL_S \
		CFG_HA_URL=$HA_URL CFG_HA_TOKEN=$HA_TOKEN CFG_HA_VERIFY_TLS=$HA_VERIFY_TLS \
		CFG_BLE_MIN_RSSI=$BLE_CANDIDATE_MIN_RSSI \
		CFG_DATA_DIR=$DATA_DIR python3 -I - <<'PY'
import hashlib
import json
import os
import re
import secrets
import sys
import tomllib

env = os.environ
lines = open(env["CFG_EXAMPLE"], encoding="utf-8").read().splitlines()
HEADER = "# Generated by deploy/setup-alpine.sh from setup.env; edits are overwritten."
# Every key set_key writes (except data_dir, fixed by this script) -> its setup.env key.
SETUP_ENV_KEYS = {
    "bind_host": "BIND_HOST",
    "bind_port": "BIND_PORT",
    "scan_interface": "SCAN_INTERFACE",
    "observation_retention_days": "RETENTION_DAYS",
    "api_token": "API_TOKEN",
    "allowed_hosts": "ALLOWED_HOSTS",
    "router.host": "ROUTER_HOST",
    "router.username": "ROUTER_USER",
    "router.password": "ROUTER_PASSWORD",
    "router.use_ssl": "ROUTER_USE_SSL",
    "collectors.asusrouter.interval_s": "ASUSROUTER_INTERVAL_S",
    "home_assistant.url": "HA_URL",
    "home_assistant.token": "HA_TOKEN",
    "home_assistant.verify_tls": "HA_VERIFY_TLS",
    "bluetooth.candidate_min_rssi": "BLE_CANDIDATE_MIN_RSSI",
}
written = set()  # dotted keys set_key wrote in this run


def lit(value):
    # JSON string syntax is valid TOML basic-string syntax, except raw DEL.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def set_key(section, key, value, commented=False):
    pattern = re.compile(r"^" + (r"(#\s*)?" if commented else "") + re.escape(key) + r"\s*=")
    current = ""
    for i, line in enumerate(lines):
        header = re.match(r"^\[([^\]]+)\]\s*$", line)
        if header:
            current = header.group(1)
        elif current == section and pattern.match(line):
            lines[i] = f"{key} = {value}"
            written.add(f"{section}.{key}" if section else key)
            return
    where = f"[{section}]" if section else "the top level"
    sys.exit(f"config.example.toml has no '{key}' in {where}; update deploy/setup-alpine.sh")


existing_raw = None  # bytes of the existing config.toml, if any
existing_cfg = None  # ...parsed, if it is valid TOML
if env.get("CFG_EXISTING") and os.path.isfile(env["CFG_EXISTING"]):
    with open(env["CFG_EXISTING"], "rb") as fh:
        existing_raw = fh.read()
    try:
        existing_cfg = tomllib.loads(existing_raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        existing_cfg = None

token = env["CFG_API_TOKEN"]
source = "from setup.env"
if not token and existing_cfg is not None:
    old = existing_cfg.get("api_token", "")
    if isinstance(old, str) and old:
        token, source = old, "kept from the existing config.toml"
if not token:
    token, source = secrets.token_hex(32), "generated"

allowed = [h.strip() for h in env["CFG_ALLOWED_HOSTS"].split(",") if h.strip()]
router_on = bool(env["CFG_ROUTER_HOST"])

# The init script and the app must agree on the data dir.
set_key("", "data_dir", lit(env["CFG_DATA_DIR"]))
set_key("", "bind_host", lit(env["CFG_BIND_HOST"]))
set_key("", "bind_port", str(int(env["CFG_BIND_PORT"])))
set_key("", "scan_interface", lit(env["CFG_SCAN_INTERFACE"]))
set_key("", "observation_retention_days", str(int(env["CFG_RETENTION_DAYS"])))
set_key("", "api_token", lit(token), commented=True)
if allowed:
    set_key("", "allowed_hosts", json.dumps(allowed), commented=True)
set_key("router", "host", lit(env["CFG_ROUTER_HOST"]))
set_key("router", "username", lit(env["CFG_ROUTER_USER"] if router_on else "admin"))
set_key("router", "password", lit(env["CFG_ROUTER_PASSWORD"] if router_on else ""))
set_key("router", "use_ssl", env["CFG_ROUTER_USE_SSL"])
set_key("collectors.asusrouter", "interval_s", str(int(env["CFG_ASUS_INTERVAL"])))

# The example ships [home_assistant] commented out (a URL without a token is an error).
ha_on = bool(env["CFG_HA_URL"])
if ha_on:
    header = re.compile(r"^#\s*\[home_assistant\]\s*$")
    for i, line in enumerate(lines):
        if header.match(line):
            lines[i] = "[home_assistant]"
            break
    else:
        sys.exit("config.example.toml has no '# [home_assistant]'; update deploy/setup-alpine.sh")
    set_key("home_assistant", "url", lit(env["CFG_HA_URL"]), commented=True)
    set_key("home_assistant", "token", lit(env["CFG_HA_TOKEN"]), commented=True)
    set_key("home_assistant", "verify_tls", env["CFG_HA_VERIFY_TLS"], commented=True)

# Unset: the example's default stays.
min_rssi = int(env["CFG_BLE_MIN_RSSI"]) if env["CFG_BLE_MIN_RSSI"] else None
if min_rssi is not None:
    set_key("bluetooth", "candidate_min_rssi", str(min_rssi))

unlisted = written - SETUP_ENV_KEYS.keys() - {"data_dir"}
if unlisted:
    sys.exit(f"SETUP_ENV_KEYS lacks {sorted(unlisted)}; update deploy/setup-alpine.sh")

body = "\n".join(lines) + "\n"
text = f"{HEADER}\n# sha256: {hashlib.sha256(body.encode('utf-8')).hexdigest()}\n{body}"

cfg = tomllib.loads(text)
expected = {
    "data_dir": env["CFG_DATA_DIR"],
    "bind_host": env["CFG_BIND_HOST"],
    "bind_port": int(env["CFG_BIND_PORT"]),
    "scan_interface": env["CFG_SCAN_INTERFACE"],
    "observation_retention_days": int(env["CFG_RETENTION_DAYS"]),
    "api_token": token,
}
for key, want in expected.items():
    if cfg.get(key) != want:
        sys.exit(f"rendered config.toml does not round-trip: {key}")
if bool(cfg.get("allowed_hosts")) != bool(allowed) or (allowed and cfg["allowed_hosts"] != allowed):
    sys.exit("rendered config.toml does not round-trip: allowed_hosts")
if cfg["router"]["host"] != env["CFG_ROUTER_HOST"] or (
    router_on and cfg["router"]["password"] != env["CFG_ROUTER_PASSWORD"]
):
    sys.exit("rendered config.toml does not round-trip: router")
if cfg["collectors"]["asusrouter"]["interval_s"] != int(env["CFG_ASUS_INTERVAL"]):
    sys.exit("rendered config.toml does not round-trip: collectors.asusrouter.interval_s")
want_ha = (
    {
        "url": env["CFG_HA_URL"],
        "token": env["CFG_HA_TOKEN"],
        "verify_tls": env["CFG_HA_VERIFY_TLS"] == "true",
    }
    if ha_on
    else None
)
if cfg.get("home_assistant") != want_ha:
    sys.exit("rendered config.toml does not round-trip: home_assistant")
if min_rssi is not None and cfg["bluetooth"]["candidate_min_rssi"] != min_rssi:
    sys.exit("rendered config.toml does not round-trip: bluetooth.candidate_min_rssi")


def flat(table, prefix=""):
    out = {}
    for key, value in table.items():
        if isinstance(value, dict):
            out.update(flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def same(a, b):
    return type(a) is type(b) and a == b


if existing_raw is not None:
    new_flat = flat(cfg)
    old_flat = flat(existing_cfg) if existing_cfg is not None else None
    # Settings of the existing file that the new render would drop or change.
    changed = (
        None
        if old_flat is None
        else [k for k, v in old_flat.items() if k not in new_flat or not same(v, new_flat[k])]
    )
    if re.search(rb"^#\s*sha256:", existing_raw, re.M):
        parts = existing_raw.split(b"\n", 2)
        sum_line = re.fullmatch(rb"# sha256: ([0-9a-f]{64})", parts[1]) if len(parts) == 3 else None
        hand_edited = not (
            sum_line
            and parts[0] == HEADER.encode()
            and sum_line.group(1).decode() == hashlib.sha256(parts[2]).hexdigest()
        )
    else:
        from_env = written - {"data_dir"}
        hand_edited = changed is None or any(k not in new_flat or k not in from_env for k in changed)
    if hand_edited:
        if changed is None:
            listing = ["(it is not valid TOML, so its settings cannot be listed)"]
        elif not changed:
            listing = ["(no setting would be lost or changed: only comments, layout or removed keys differ)"]
        else:
            names = [k if re.fullmatch(r"[A-Za-z0-9_.-]+", k) else json.dumps(k) for k in changed]
            width = max(map(len, names))
            listing = [
                f"{name:<{width}}  ({SETUP_ENV_KEYS[k]})" if k in SETUP_ENV_KEYS else f"{name:<{width}}  (no setup.env key)"
                for k, name in zip(changed, names)
            ]
        if env["CFG_DISCARD"] == "1":
            print("discarding hand edits to config.toml; these settings change (values not shown):")
            for row in listing:
                print("  " + row)
        else:
            print("config.toml has hand edits; these settings would be lost or changed (values not shown):", file=sys.stderr)
            for row in listing:
                print("    " + row, file=sys.stderr)
            sys.exit(int(env["CFG_HAND_EDITED_RC"]))

fd = os.open(env["CFG_OUT"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
print(f"api_token: {source} (it is stored only in config.toml)")
PY
}

have_python() { command -v python3 >/dev/null 2>&1; }

# Copy $CONF to $CONF.YYYYmmdd-HHMMSS (same owner, 0600) and keep the newest
# $CONFIG_BACKUPS_KEPT such copies. config.toml.prev (older scripts) is left alone.
backup_config() {
	stamp=$(date -u +%Y%m%d-%H%M%S)
	dest=$CONF.$stamp
	n=1
	while [ -e "$dest" ]; do # two runs in one second
		dest=$CONF.$stamp-$n
		n=$((n + 1))
	done
	(umask 077 && cp -p "$CONF" "$dest")
	chmod 0600 "$dest"
	note "previous config.toml backed up to $dest"
	printf '%s\n' "$CONF".[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]* |
		LC_ALL=C sort -r | tail -n +$((CONFIG_BACKUPS_KEPT + 1)) | while IFS= read -r old; do
		rm -f "$old"
	done
}

# update_config EXAMPLE: render config.toml into $CONF, stopping (a problem on
# --dry-run) if $CONF was edited by hand, unless --discard-config-edits. A changed
# file is backed up first; an unchanged one is left alone.
update_config() {
	example=$1
	if [ "$DRY_RUN" = 1 ]; then
		if [ ! -f "$example" ] || ! have_python; then
			note "(dry-run) config.example.toml or python3 not available yet; skipping the render check"
			return 0
		fi
		tmp=$(mktemp)
		# Validate the rendering for real, then discard it.
		rc=0
		render_config "$tmp" "$example" "$CONF" >/dev/null || rc=$?
		if [ "$rc" = 0 ]; then
			if [ ! -f "$CONF" ]; then
				note "(dry-run) config.toml renders and validates; would write $CONF (0600, $SVC_USER)"
			elif cmp -s "$tmp" "$CONF"; then
				note "(dry-run) config.toml renders and validates; $CONF is unchanged"
			else
				note "(dry-run) config.toml renders and validates; would back up and replace $CONF"
			fi
		elif [ "$rc" = "$HAND_EDITED_RC" ]; then
			problem "$HAND_EDITED_MSG"
		else
			problem "config.toml could not be rendered from $ENV_FILE"
		fi
		rm -f "$tmp"
		return 0
	fi
	tmp=$(mktemp "$(dirname "$CONF")/.config.XXXXXX")
	msg=$(mktemp)
	rc=0
	render_config "$tmp" "$example" "$CONF" >"$msg" || rc=$?
	if [ "$rc" != 0 ]; then
		rm -f "$tmp" "$msg"
		[ "$rc" != "$HAND_EDITED_RC" ] || die "$HAND_EDITED_MSG"
		die "config.toml could not be rendered from $ENV_FILE"
	fi
	sed 's/^/    /' "$msg"
	rm -f "$msg"
	if [ -f "$CONF" ] && cmp -s "$tmp" "$CONF"; then
		note "config.toml unchanged"
		rm -f "$tmp"
		return 0
	fi
	[ ! -f "$CONF" ] || backup_config
	[ "$CONF_CHOWN" = 0 ] || chown "$SVC_USER:$SVC_USER" "$tmp"
	chmod 0600 "$tmp"
	mv -f "$tmp" "$CONF"
	note "wrote $CONF (0600)"
}

# --------------------------------------------------------- render-only mode

load_env
validate_env
if [ "$PROBLEMS" -gt 0 ]; then
	# Dry run too: later checks assume sane values, and a bad value must not
	# be echoed back by some tool further down.
	say "fix the $PROBLEMS problem(s) above in $ENV_FILE first"
	exit 1
fi
if [ -n "$RENDER_OUT" ]; then
	rc=0
	render_config "$RENDER_OUT" "$SCRIPT_DIR/../config.example.toml" "$EXISTING_CONF" || rc=$?
	[ "$rc" != "$HAND_EDITED_RC" ] || die "$HAND_EDITED_MSG"
	exit "$rc"
fi
if [ -n "$CONFIG_HOOK" ]; then
	CONF=$CONFIG_HOOK
	CONF_CHOWN=0
	update_config "$SCRIPT_DIR/../config.example.toml"
	[ "$PROBLEMS" = 0 ] || exit 1
	exit 0
fi

# ---------------------------------------------------------------- preflight

openrc_version() {
	openrc --version 2>/dev/null |
		awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+\.[0-9]+(\.[0-9]+)?$/) { print $i; exit } }'
}

version_ge() { # version_ge A B: A >= B for dotted numeric versions
	awk -v a="$1" -v b="$2" 'BEGIN {
		na = split(a, x, "."); nb = split(b, y, ".")
		n = (na > nb) ? na : nb
		for (i = 1; i <= n; i++) {
			if ((x[i] + 0) > (y[i] + 0)) exit 0
			if ((x[i] + 0) < (y[i] + 0)) exit 1
		}
		exit 0
	}'
}

preflight() {
	step "Preflight"
	[ "$(id -u)" = 0 ] || problem "run as root"
	[ -f /etc/alpine-release ] || problem "this script targets Alpine Linux (no /etc/alpine-release)"
	command -v rc-service >/dev/null 2>&1 || problem "OpenRC not found (rc-service missing)"
	if command -v openrc >/dev/null 2>&1; then
		ver=$(openrc_version)
		if [ -z "$ver" ]; then
			warn "could not read the OpenRC version; ambient capabilities need >= $MIN_OPENRC"
		elif version_ge "$ver" "$MIN_OPENRC"; then
			note "OpenRC $ver (>= $MIN_OPENRC: ambient capabilities supported)"
		else
			problem "OpenRC $ver is older than $MIN_OPENRC; arp-scan and nmap need ambient capabilities"
		fi
	fi
	if have_python; then
		if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
			note "python3 $(python3 -c 'import platform; print(platform.python_version())') (>= 3.12)"
		else
			problem "python3 3.12 or newer is required"
		fi
	else
		note "python3 is not installed yet; it is installed below (version is checked afterwards)"
	fi
	if [ ! -d "/sys/class/net/$SCAN_INTERFACE" ]; then
		problem "SCAN_INTERFACE '$SCAN_INTERFACE' does not exist (see: ip -br addr)"
	fi
	case $BIND_HOST in
	0.0.0.0 | 127.*) ;;
	*)
		bound=$(ip -4 -o addr show 2>/dev/null | grep -c "inet $BIND_HOST/" || true)
		[ "$bound" -gt 0 ] || problem "BIND_HOST $BIND_HOST is not an address of this host"
		;;
	esac
	if [ "$BIND_HOST" = 0.0.0.0 ]; then
		warn "BIND_HOST=0.0.0.0 also listens on tailscale0 and docker0; prefer the LAN address"
	fi
}

# ------------------------------------------------------------------ steps

install_packages() {
	step "Packages"
	missing=""
	for p in python3 py3-pip arp-scan nmap git logrotate; do
		apk info -e "$p" >/dev/null 2>&1 || missing="$missing $p"
	done
	if uses_ssh; then
		command -v ssh >/dev/null 2>&1 || missing="$missing openssh-client-default"
		command -v ssh-keygen >/dev/null 2>&1 || missing="$missing openssh-keygen"
	fi
	if [ -z "$missing" ]; then
		note "all required packages are installed"
	else
		# apk add never upgrades packages that are already installed.
		# shellcheck disable=SC2086 # $missing is a list of package names
		act "apk add --no-cache$missing" apk add --no-cache $missing
	fi
	if [ "$DRY_RUN" = 0 ]; then
		python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' ||
			die "python3 3.12 or newer is required (this Alpine ships $(python3 --version 2>&1))"
		python3 -m venv --help >/dev/null 2>&1 || die "python3 has no venv module (install py3-virtualenv)"
	fi
}

ensure_user() {
	step "Service user and data directory"
	if grep -q "^$SVC_USER:" /etc/group; then
		note "group $SVC_USER exists"
	else
		act "addgroup -S $SVC_USER" addgroup -S "$SVC_USER"
	fi
	if id "$SVC_USER" >/dev/null 2>&1; then
		note "user $SVC_USER exists"
	else
		act "adduser -S -D -H -G $SVC_USER -h $DATA_DIR $SVC_USER" \
			adduser -S -D -H -G "$SVC_USER" -h "$DATA_DIR" "$SVC_USER"
	fi
	act "data dir $DATA_DIR (0700, $SVC_USER)" install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"
	act "floor plan dir $DATA_DIR/floorplan (0750, $SVC_USER)" install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR/floorplan"
	act "config dir $ETC_DIR (0700, root)" install -d -m 0700 "$ETC_DIR"
}

GIT_SSH="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS"

git_run() {
	GIT_SSH_COMMAND=$GIT_SSH GIT_TERMINAL_PROMPT=0 git "$@"
}

show_deploy_key_instructions() {
	say ""
	say "Add this key to the repository as a READ-ONLY deploy key:"
	say "  GitHub -> the repository -> Settings -> Deploy keys -> Add deploy key"
	say "  (leave 'Allow write access' unchecked), then re-run this script."
	say ""
	cat "$DEPLOY_KEY.pub"
	say ""
}

ensure_repo_access() {
	step "Repository access"
	if ! uses_ssh; then
		note "REPO_URL is https; no deploy key needed (a private repo needs an SSH REPO_URL)"
		return 0
	fi
	if [ ! -f "$DEPLOY_KEY" ]; then
		if [ "$DRY_RUN" = 1 ]; then
			note "(dry-run) would: generate an ed25519 deploy key at $DEPLOY_KEY and stop so you can add it"
			return 0
		fi
		note "generating a deploy key (ed25519, no passphrase) at $DEPLOY_KEY"
		(umask 077 && ssh-keygen -q -t ed25519 -N '' -C "$APP-deploy" -f "$DEPLOY_KEY")
		show_deploy_key_instructions
		exit 3
	fi
	if [ "$DRY_RUN" = 1 ]; then
		note "(dry-run) would: verify read access with git ls-remote"
		return 0
	fi
	fresh_hosts=0
	[ -f "$KNOWN_HOSTS" ] || fresh_hosts=1
	if git_run ls-remote --exit-code "$REPO_URL" HEAD >/dev/null 2>&1; then
		note "repository is readable with the deploy key"
		if [ "$fresh_hosts" = 1 ] && [ -f "$KNOWN_HOSTS" ]; then
			note "first connection: compare these host key fingerprints with the ones GitHub publishes:"
			ssh-keygen -lf "$KNOWN_HOSTS" | sed 's/^/        /'
		fi
	else
		warn "cannot read $REPO_URL with the deploy key"
		show_deploy_key_instructions
		exit 3
	fi
}

stop_service() {
	step "Stop service"
	if [ -x "$INIT_FILE" ] && rc-service "$APP" status >/dev/null 2>&1; then
		act "rc-service $APP stop" rc-service "$APP" stop
		[ "$DRY_RUN" = 1 ] || STOPPED=1
	else
		note "$APP is not running"
	fi
}

OLD_COMMIT=${LANSCAPE_SETUP_OLD:-}
NEW_COMMIT=""
NEW_FULL=""

checkout_code() {
	step "Code ($REF)"
	if [ "$DRY_RUN" = 1 ]; then
		if [ -d "$SRC/.git" ]; then
			note "currently at $(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
		fi
		note "(dry-run) would: fetch and check out $REF in $SRC"
		return 0
	fi
	if [ ! -d "$SRC/.git" ]; then
		note "cloning into $SRC"
		install -d -m 0755 "$PREFIX"
		git_run clone --quiet "$REPO_URL" "$SRC"
	fi
	[ -n "$OLD_COMMIT" ] || OLD_COMMIT=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo none)
	git_run -C "$SRC" fetch --quiet --prune --tags origin
	if git -C "$SRC" rev-parse --verify --quiet "origin/$REF^{commit}" >/dev/null; then
		target=origin/$REF
	else
		target=$REF
	fi
	git -C "$SRC" rev-parse --verify --quiet "$target^{commit}" >/dev/null ||
		die "ref '$REF' not found in the repository"
	# $SRC is deploy-only; local changes there are discarded.
	git -C "$SRC" checkout --quiet --force --detach "$target"
	NEW_COMMIT=$(git -C "$SRC" rev-parse --short HEAD)
	NEW_FULL=$(git -C "$SRC" rev-parse HEAD)
	if [ "$OLD_COMMIT" = "$NEW_COMMIT" ]; then
		note "already at $NEW_COMMIT"
	else
		note "$OLD_COMMIT -> $NEW_COMMIT"
	fi
}

# After the checkout, continue with the script that came with the new code.
reexec_if_updated() {
	[ "$DRY_RUN" = 0 ] || return 0
	[ -z "${LANSCAPE_SETUP_REEXEC:-}" ] || return 0
	new_script=$SRC/deploy/setup-alpine.sh
	[ -f "$new_script" ] || return 0
	if ! cmp -s "$SCRIPT_PATH" "$new_script"; then
		note "setup script updated in the checkout; continuing with the new version"
		LANSCAPE_SETUP_REEXEC=1
		LANSCAPE_SETUP_STOPPED=$STOPPED
		LANSCAPE_SETUP_OLD=$OLD_COMMIT
		export LANSCAPE_SETUP_REEXEC LANSCAPE_SETUP_STOPPED LANSCAPE_SETUP_OLD
		exec sh "$new_script" --env "$ENV_FILE" --ref "$REF"
	fi
}

venv_ok() {
	[ -x "$VENV/bin/python" ] && [ -x "$VENV/bin/$APP" ] &&
		"$VENV/bin/python" -c 'import lanscape_atlas' >/dev/null 2>&1
}

NEED_INSTALL=1

install_app() {
	step "Virtualenv and package"
	if [ "$DRY_RUN" = 1 ]; then
		note "(dry-run) would: create or refresh $VENV and pip install $SRC"
		return 0
	fi
	stamped=""
	[ ! -f "$STAMP" ] || stamped=$(cat "$STAMP")
	if [ "$stamped" = "$NEW_FULL" ] && venv_ok; then
		NEED_INSTALL=0
		note "$NEW_COMMIT already installed; skipping pip"
		return 0
	fi
	if ! { [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'pass' >/dev/null 2>&1; }; then
		note "creating virtualenv $VENV"
		rm -rf "$VENV"
		python3 -m venv "$VENV"
	fi
	pip_log=$(mktemp)
	note "pip install (dependencies, then the app itself)"
	if ! {
		"$VENV/bin/pip" install --quiet --disable-pip-version-check --no-input "$SRC" &&
			"$VENV/bin/pip" install --quiet --disable-pip-version-check --no-input \
				--force-reinstall --no-deps "$SRC"
	} >"$pip_log" 2>&1; then
		tail -n 40 "$pip_log" >&2
		rm -f "$pip_log"
		die "pip install failed (output above)"
	fi
	rm -f "$pip_log"
}

backup_db() {
	step "Database backup"
	db=$DATA_DIR/atlas.db
	if [ ! -f "$db" ]; then
		note "no database yet"
		return 0
	fi
	if [ "$DRY_RUN" = 1 ]; then
		note "(dry-run) would: back up $db to $BACKUP_DIR if the code changed (keep $BACKUPS_KEPT)"
		return 0
	fi
	if [ "$NEED_INSTALL" = 0 ]; then
		note "code unchanged; no backup needed"
		return 0
	fi
	(umask 077 && install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$BACKUP_DIR")
	dest=$BACKUP_DIR/atlas-$(date -u +%Y%m%d-%H%M%S)-${OLD_COMMIT:-none}.db
	python3 -I -c '
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
' "$db" "$dest"
	chown "$SVC_USER:$SVC_USER" "$dest"
	chmod 0600 "$dest"
	note "backed up to $dest"
	# shellcheck disable=SC2012 # names are ours: atlas-<timestamp>-<commit>.db
	ls -1t "$BACKUP_DIR"/atlas-*.db | tail -n +$((BACKUPS_KEPT + 1)) | while IFS= read -r old; do
		rm -f "$old"
	done
}

write_config() {
	step "Configuration"
	example=$SRC/config.example.toml
	[ -f "$example" ] || example=$SCRIPT_DIR/../config.example.toml
	update_config "$example"
	[ "$DRY_RUN" = 0 ] || return 0
	# Load it exactly as the service will: as the service user, through the app's own parser.
	check_py=$(mktemp)
	cat >"$check_py" <<'PY'
from lanscape_atlas.config import load_settings

s = load_settings()
print(
    "bind %s:%d, token %s, router %s, retention %dd"
    % (
        s.bind_host,
        s.bind_port,
        "set" if s.api_token else "unset",
        "on" if s.router else "off",
        s.observation_retention_days,
    )
)
PY
	chmod 0644 "$check_py"
	check=$(su -s /bin/sh "$SVC_USER" -c "LANSCAPE_DATA_DIR='$DATA_DIR' '$VENV/bin/python' -I '$check_py'") || {
		rm -f "$check_py"
		die "the app cannot load the rendered config.toml"
	}
	rm -f "$check_py"
	note "app loads it: $check"
}

run_as_service_user() {
	su -s /bin/sh "$SVC_USER" -c "LANSCAPE_DATA_DIR='$DATA_DIR' '$VENV/bin/$APP' $1"
}

migrate_and_oui() {
	step "Database migration and vendor list"
	if [ "$DRY_RUN" = 1 ]; then
		note "(dry-run) would: $APP migrate, then $APP update-oui (as $SVC_USER)"
		return 0
	fi
	db_path=$(run_as_service_user migrate) || die "$APP migrate failed"
	note "migrated: $db_path"
	if run_as_service_user update-oui >/dev/null 2>&1; then
		note "vendor list updated"
	else
		warn "update-oui failed (no network?); the previous vendor list stays in place"
	fi
	[ -z "$NEW_FULL" ] || printf '%s\n' "$NEW_FULL" >"$STAMP"
}

install_if_changed() { # install_if_changed SRC DEST MODE
	if cmp -s "$1" "$2" 2>/dev/null; then
		note "$2 up to date"
	else
		act "install $2" install -m "$3" "$1" "$2"
	fi
}

install_service_files() {
	step "OpenRC service and log rotation"
	if [ ! -f "$SRC/deploy/openrc/$APP" ]; then
		[ "$DRY_RUN" = 1 ] || die "$SRC/deploy/openrc/$APP is missing"
		note "(dry-run) would: install the init script and logrotate config from the checkout"
		return 0
	fi
	install_if_changed "$SRC/deploy/openrc/$APP" "$INIT_FILE" 0755
	act "mkdir /etc/logrotate.d" install -d -m 0755 /etc/logrotate.d
	if [ -f "$SRC/deploy/logrotate/$APP" ]; then
		install_if_changed "$SRC/deploy/logrotate/$APP" "$LOGROTATE_FILE" 0644
	else
		warn "this checkout has no deploy/logrotate/$APP; the log will not be rotated"
	fi
	if rc-update show default 2>/dev/null | awk '{print $1}' | grep -qx "$APP"; then
		note "$APP is in the default runlevel"
	else
		act "rc-update add $APP default" rc-update add "$APP" default
	fi
	[ -x /etc/periodic/daily/logrotate ] || warn "no /etc/periodic/daily/logrotate; is logrotate scheduled?"
}

port_free() {
	python3 -I -c '
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind((sys.argv[1], int(sys.argv[2])))
except OSError as exc:
    print(exc)
    sys.exit(1)
' "$BIND_HOST" "$BIND_PORT"
}

start_service() {
	step "Start service"
	if [ "$DRY_RUN" = 1 ]; then
		if have_python; then
			if reason=$(port_free); then
				note "port $BIND_HOST:$BIND_PORT is free"
			else
				note "port $BIND_HOST:$BIND_PORT: $reason (informational: $APP itself may hold it)"
			fi
		fi
		note "(dry-run) would: rc-service $APP start"
		return 0
	fi
	if ! reason=$(port_free); then
		die "cannot listen on $BIND_HOST:$BIND_PORT: $reason"
	fi
	note "rc-service $APP start"
	rc-service "$APP" start
	STOPPED=0
}

# The token is read from config.toml inside python and sent as a header; it is never
# on a command line or printed. Exit status 1 = something is wrong.
run_smoke() { # run_smoke OUTFILE
	python3 -I - "$BIND_HOST" "$BIND_PORT" "$CONF" >"$1" 2>&1 <<'PY'
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request

host, port, conf = sys.argv[1], sys.argv[2], sys.argv[3]
with open(conf, "rb") as fh:
    token = tomllib.load(fh).get("api_token", "") or ""
base = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"


def get(path, auth=True):
    req = urllib.request.Request(base + path)
    if auth and token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def wait(fn, seconds):
    deadline = time.time() + seconds
    while True:
        try:
            value = fn()
            if value:
                return value
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.time() > deadline:
            return None
        time.sleep(3)


if not wait(lambda: get("/api/v1/collectors") is not None, 60):
    print("API did not answer within 60 s")
    sys.exit(1)
print("API is up")

if token:
    try:
        get("/api/v1/collectors", auth=False)
        print("AUTH PROBLEM: /api answered without the token")
        sys.exit(1)
    except urllib.error.HTTPError as exc:
        print(f"auth enforced (no token -> HTTP {exc.code})")


def first_runs():
    rows = get("/api/v1/collectors")
    return rows if any(r["last_ok"] for r in rows) else None


rows = wait(first_runs, 150)
ran = rows is not None
rows = rows or get("/api/v1/collectors")
print(f"{'collector':<14} {'last_ok':<27} {'fails':<5} last_error")
for row in rows:
    err = (row["last_error"] or "").replace("\n", " ")[:90]
    print(f"{row['name']:<14} {row['last_ok'] or '-':<27} {row['consecutive_failures']:<5} {err}")

total = online = offset = 0
while True:
    page = get(f"/api/v1/devices?limit=1000&offset={offset}")
    total += len(page)
    online += sum(1 for d in page if d.get("online"))
    if len(page) < 1000:
        break
    offset += 1000
print(f"devices: {total} ({online} online)")
if not ran:
    print("no collector completed a run within 150 s")
    sys.exit(1)
PY
}

smoke_test() {
	step "Smoke test"
	if [ "$DRY_RUN" = 1 ]; then
		note "(dry-run) would: query /api/v1/collectors and /api/v1/devices with the token, then tail $LOG_FILE"
		return 0
	fi
	out=$(mktemp)
	status=0
	run_smoke "$out" || status=$?
	sed 's/^/    /' "$out"
	rm -f "$out"
	say ""
	say "    last lines of $LOG_FILE:"
	tail -n 15 "$LOG_FILE" 2>/dev/null | sed 's/^/      /' || true
	if [ "$status" -ne 0 ]; then
		warn "smoke test failed; the service is running. See the output above and: rc-service $APP status"
		exit 2
	fi
}

# --------------------------------------------------------------------- main

if [ "$DRY_RUN" = 1 ]; then
	say "lanscape-atlas setup: DRY RUN (nothing will be changed)"
fi
preflight
install_packages
ensure_user
ensure_repo_access
stop_service
checkout_code
reexec_if_updated
install_app
backup_db
write_config
migrate_and_oui
install_service_files
start_service
smoke_test

step "Done"
if [ "$DRY_RUN" = 1 ]; then
	if [ "$PROBLEMS" -gt 0 ]; then
		say "dry-run found $PROBLEMS problem(s) (see PROBLEM lines above)"
		exit 1
	fi
	say "dry-run found no problems; run again without --dry-run to apply"
else
	say "    UI: http://$BIND_HOST:$BIND_PORT"
	say "    API token: grep '^api_token' $CONF   (only root and $SVC_USER can read it)"
	say "    logs: tail -f $LOG_FILE"
fi
