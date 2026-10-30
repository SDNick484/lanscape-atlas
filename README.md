# lanscape-atlas

A self-hosted home network inventory and mapping tool. It discovers the devices on
your LAN, ties MAC, IPv4 and hostname together, records which services each device
runs, and lets you (or Claude, over MCP) give every device a durable identity: a
name, type, icon and place that survives DHCP churn and routers that forget
hostnames.

![Devices list](docs/screenshots/devices.png)
![Topology view](docs/screenshots/topology.png)

The screenshots show a generated "fake house" dataset in documentation address
ranges, not a real network.

## Features (Phase 1)

- **Discovery** with five collectors: ASUS router client list (`asusrouter`),
  `arp-scan`, mDNS, SSDP, and a nightly light `nmap` scan. A deep `nmap -sV` scan
  runs on demand for a single device. Devices with `scan_policy=never` are skipped.
- **Stable identity**: names, types, icons, places, owners, notes and trust level,
  each with provenance ("set by user / claude / collector") and precedence
  user > claude > collector.
- **Inbox** for never-seen devices and unclaimed random-MAC interfaces, with quick
  actions to name, type, claim, or mark known/unwanted.
- **Topology** view: router, mesh nodes, switches, devices. Discovered links are
  dashed, manual links solid; drag to arrange, add placeholders for unmanaged switches.
- **History** of every change, grouped by batch, with per-batch **Undo**.
- **Status** page with collector health and counts.
- **HTTP API** under `/api/v1` (OpenAPI via FastAPI) and an **MCP server** at `/mcp`
  in the same process.
- **Vendor lookup** from a local IEEE OUI file; no network call at lookup time.

## Requirements

- Python 3.12 or newer, `arp-scan` and `nmap`.
- The host or container must be **bridged onto the LAN segment it scans**. ARP and
  mDNS only work with layer-2 adjacency; a NATed or routed container will see
  almost nothing.
- IPv4 only.

## Install on an Alpine LXC

Run as root inside a container that is bridged to your LAN. Alpine's `python3`
package normally includes the `venv` module; if `python3 -m venv` reports it
missing on your release, install `py3-virtualenv` and use `virtualenv` instead.

```sh
apk add python3 py3-pip git arp-scan nmap

# Get the code; every later step runs from inside the checkout
git clone https://github.com/SDNick484/lanscape-atlas.git
cd lanscape-atlas

# Application and virtualenv
python3 -m venv /opt/lanscape-atlas/venv
/opt/lanscape-atlas/venv/bin/pip install .

# Service user, group and data directory
addgroup -S lanscape
adduser -S -D -H -G lanscape -h /var/lib/lanscape-atlas lanscape
install -d -m 0700 -o lanscape -g lanscape /var/lib/lanscape-atlas

# Configuration
cp config.example.toml /var/lib/lanscape-atlas/config.toml
chown lanscape:lanscape /var/lib/lanscape-atlas/config.toml
chmod 600 /var/lib/lanscape-atlas/config.toml
$EDITOR /var/lib/lanscape-atlas/config.toml

# Create the database and fetch the vendor list
su -s /bin/sh lanscape -c '/opt/lanscape-atlas/venv/bin/lanscape-atlas migrate'
su -s /bin/sh lanscape -c '/opt/lanscape-atlas/venv/bin/lanscape-atlas update-oui'

# Service
install -m 0755 deploy/openrc/lanscape-atlas /etc/init.d/lanscape-atlas
rc-update add lanscape-atlas default
rc-service lanscape-atlas start
```

**Make it reachable from the LAN.** By default `serve` listens on `127.0.0.1`
only, so other machines cannot open the UI. Set `bind_host` in
`/var/lib/lanscape-atlas/config.toml` to the container's LAN address (for example
`bind_host = "192.0.2.50"`) or to `"0.0.0.0"`, then restart:

```sh
rc-service lanscape-atlas restart
```

The UI is then at `http://<bind_host>:8720`. Without an `api_token`, the app only
answers to `localhost`, `bind_host` and the names in `allowed_hosts` (see below):
with `0.0.0.0`, or if you browse by a DNS name, add the address or name you type in
the browser to `allowed_hosts` **with a port pattern**, e.g.
`allowed_hosts = ["192.0.2.50:*", "atlas.example.com"]` (a bare entry matches only a
Host header without a port, which is what a reverse proxy on 80/443 sends).

Token mode (`api_token` set) turns the Host check off and assumes an authenticating
proxy (e.g. Cloudflare Access) protects the pages and `/topology`; don't expose
token mode without one.

`arp-scan` and `nmap` need raw sockets. The init script grants
`cap_net_raw` and `cap_net_admin` to the unprivileged `lanscape` user through
`capabilities="^cap_net_raw,^cap_net_admin"` and exports `NMAP_PRIVILEGED=1` so
nmap uses them. Ambient capabilities under `supervise-daemon` require
**OpenRC 0.45 or newer**; on older versions run the service as root, or set file
capabilities on the binaries and remove the `NMAP_PRIVILEGED` line.

The service logs (INFO level, with time, level and logger name) to
`/var/log/lanscape-atlas.log`.

`lanscape-atlas update-oui` re-downloads the IEEE MA-L list. It validates the file
before replacing the old one and leaves the old file in place on any failure, so it
is safe to run from cron. To refresh it weekly as the `lanscape` user, add this line
to `/etc/crontabs/root` and make sure `crond` runs (`rc-update add crond default`,
`rc-service crond start`):

```sh
17 4 * * 0 su -s /bin/sh lanscape -c '/opt/lanscape-atlas/venv/bin/lanscape-atlas update-oui'
```

### Commands

| Command | Purpose |
|---|---|
| `lanscape-atlas migrate` | Create the data directory (mode 0700) and database; prints the DB path |
| `lanscape-atlas serve [--host H] [--port P]` | Run the web app, API and MCP server |
| `lanscape-atlas update-oui` | Download and validate the IEEE OUI vendor list |
| `lanscape-atlas ingest-file FILE.json` | Ingest serialized observations (useful for demos and tests) |

`serve` honors proxy headers from the addresses in `LANSCAPE_FORWARDED_ALLOW_IPS`
(comma-separated, default `127.0.0.1`). Set it to your reverse proxy's address if
the proxy is not on the same host.

## Configuration

Settings live in `$LANSCAPE_DATA_DIR/config.toml` (default data directory
`/var/lib/lanscape-atlas`). `config.example.toml` is a complete, commented starting
point.

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | `/var/lib/lanscape-atlas` | Holds `atlas.db`, `config.toml`, `oui.csv` |
| `bind_host` | `127.0.0.1` | Address `serve` listens on |
| `bind_port` | `8720` | Port `serve` listens on |
| `scan_interface` | `eth0` | Interface `arp-scan` uses |
| `observation_retention_days` | `30` | How long raw observations are kept |
| `ip_match_window_min` | `10` | Window for correlating an IP-only sighting to a known device |
| `api_token` | unset | Static bearer token required for `/api` and `/mcp` |
| `allowed_hosts` | `[]` | Extra `Host` headers the app accepts when no `api_token` is set |

`[router]` configures the ASUS collector; remove the table or leave `host` empty to
disable it.

| Key | Default | Meaning |
|---|---|---|
| `host` | none | Router address, e.g. `192.0.2.1` |
| `username` | `admin` | Router login |
| `password` | empty | Router password |
| `use_ssl` | `true` | Use HTTPS to the router |

Each collector has its own table, `[collectors.<name>]`, with `enabled`,
`timeout_s`, and either `interval_s` or (for `nmap_light`) a `cron` expression.

| Collector | Default schedule |
|---|---|
| `asusrouter` | every 120 s (enabled when `[router]` is set) |
| `arp` | every 300 s |
| `mdns` | flushed every 60 s |
| `ssdp` | every 600 s |
| `nmap_light` | cron `17 3 * * *` (nightly) |

Secrets can come from the environment instead of the file:
`LANSCAPE_ROUTER_PASSWORD`, `LANSCAPE_API_TOKEN`. `LANSCAPE_DATA_DIR` moves the data
directory.

### `allowed_hosts`

When no `api_token` is set, the whole app (pages, `/api`, `/topology`, `/mcp`) only
accepts requests whose `Host` header is `localhost`, `127.0.0.1`, the configured
`bind_host` (each with or without a port), or an entry in `allowed_hosts`; anything
else gets `421`. This blocks DNS-rebinding attacks. If you bind to `0.0.0.0` and
browse to the container by its LAN address or name, list that address or name here.
Matching is exact:

- A bare entry such as `"atlas.example.com"` matches only a `Host` header **without**
  a port.
- `"atlas.example.com:*"` matches that name on **any** port.
- `["*"]` disables the check; avoid it unless something else protects the app.

In every mode, a `POST`, `PUT`, `PATCH` or `DELETE` to `/api/*` or `/topology/*` that
carries an `Origin` header for a different host than the request's `Host` is refused
with `403`, so other web pages cannot trigger undo or scans from your browser. A
reverse proxy must therefore pass the original `Host` header through. Clients that
send no `Origin` (curl, MCP clients) are unaffected.

## Remote access and security

lanscape-atlas has no accounts of its own. Choose one of these setups:

1. **Behind an authenticating proxy (recommended for remote access).** Publish the
   app through Cloudflare Access or another auth proxy for the web pages, for
   example at `https://atlas.example.com`. Set `api_token` so `/api` and `/mcp`
   additionally require `Authorization: Bearer <token>`; automation and MCP clients
   usually cannot complete an interactive proxy login. Add the public hostname to
   `allowed_hosts` if you run without a token.
2. **LAN-only, no auth.** Bind to a LAN interface and set nothing else. This works,
   but **anyone who can reach the port can read and write your inventory**, including
   renaming devices, changing trust, deleting links and triggering scans. Use it only
   on a network you fully trust, and never expose the port to the internet.

## MCP

The MCP server uses streamable HTTP at `/mcp`. With a token configured:

```sh
claude mcp add --transport http atlas https://atlas.example.com/mcp \
  --header "Authorization: Bearer YOUR_API_TOKEN"
```

Tools: `find_device`, `list_devices`, `get_device`, `list_services`, `update_device`,
`claim_interface`, `set_place`, `run_deep_scan`, `recent_changes`, `undo`.

Everything Claude writes is recorded with actor `claude`. It appears in History as
its own batch and can be undone like any other change. Tool output never includes
router credentials or raw configuration.

The server does not yet use the `mcp-server-shared` helpers and could adopt them
through that repository's sync tooling.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python scripts/check_leaks.py
.venv/bin/pre-commit install      # runs the leak check on every commit
```

CI runs ruff, mypy, pytest and the leak check. Tests never touch the network.
`lanscape-atlas ingest-file tests/fixtures/fake_house.json` loads the fake-house
dataset for local UI work.

## Privacy rules for contributors

This repository is public; real network data must never enter it. Fixtures, docs and
examples use only:

- IPv4 `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`
- MACs `00:00:5e:00:53:00` to `00:00:5e:00:53:ff` (and `02:00:5e:00:53:xx` for
  synthetic random MACs)
- generic hostnames and rooms, and `atlas.example.com` for the app itself

Tools:

- `scripts/check_leaks.py` fails on MAC addresses outside the documentation range,
  private (RFC 1918) IPv4 addresses, and any term from your personal denylist. The
  pre-commit hook runs it on staged files; CI runs it on the whole repository.
- The optional denylist lives **outside the repository** at
  `~/.config/lanscape-atlas/denylist.txt` (one term per line, `#` comments). Put your
  hostnames, room names and domain there. Set `LANSCAPE_DENYLIST` to use another
  file; if set, the file must exist and be readable or the check fails.
- `scripts/anonymize.py` rewrites a real capture into documentation values with a
  stable mapping, so relationships between devices survive:

  ```sh
  python scripts/anonymize.py --seed SOME_SEED --names real-hostnames.txt capture.json > fixture.json
  python scripts/check_leaks.py fixture.json
  ```

  `--names` takes a file of real hostnames, one per line; they become `host-1`,
  `host-2`, and so on. Hostnames are not detected automatically, so list them.
  Keep the real capture and names file out of the repository.

Neither tool detects bare 12-hex-digit MACs without separators (for example
`aabbccddeeff`). Review such values by hand before committing.

## Known limitations

- IPv4 only.
- Links behind unmanaged switches cannot be discovered; draw them manually in the
  Topology view.
- Phones and other devices with randomized MACs appear as new interfaces until you
  claim them onto a device in the Inbox.
- The ASUS router collector has only been tested against a simulated router.

## Roadmap

| Phase | Scope |
|---|---|
| 2 | Floor plan: upload an image or SVG, draw rooms, drag devices onto them |
| 3 | Bluetooth presence via ESP32/ESPHome Bluetooth proxies, with rough room location |
| 4 | MQTT / Home Assistant publishing and new-device notifications |
| 5 | Z-Wave devices via Hubitat or Home Assistant |

## License

MIT. See [LICENSE](LICENSE).
