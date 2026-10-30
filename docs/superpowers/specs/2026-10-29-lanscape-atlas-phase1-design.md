# lanscape-atlas — Phase 1 Design

**Date:** 2026-10-29
**Status:** Draft for review
**Scope:** Phase 1 of a multi-phase project (see §11)

## 1. Purpose

A self-hosted home network inventory and mapping tool. It discovers devices on a
home LAN, ties MAC ↔ IPv4 ↔ hostname together, records what services each device
runs, and lets the owner (or Claude, via MCP) give every device a durable
identity — name, type, icon, location — that survives DHCP churn and routers that
forget hostnames.

It serves four jobs, all in scope:

1. **Identify** — "what is 192.168.x.y / this MAC?" answered in seconds.
2. **Troubleshoot** — what is online, and how is it connected (which AP/mesh node, which switch).
3. **Security** — new, never-seen devices land in an Inbox (alerts come in Phase 4).
4. **Documentation** — a living record of the home network that stays current.

## 2. Constraints

- **Public code, private data.** The code is published on GitHub (MIT license). No
  personal network data (MACs, IPs, hostnames, room names, credentials, floor
  plans) may ever enter the repository. Enforced mechanically, not by convention (§9).
- **IPv4 only** in Phase 1. The schema must not block IPv6 later.
- **Large, busy network.** Hundreds of devices, frequent churn, many phones with
  randomized MACs, fragile IoT devices.
- **Reference environment** (used for design, never hard-coded): an ASUS AiMesh
  network (router + one wired-backhaul node), mostly unmanaged switches (no
  SNMP/LLDP), mixed Wi-Fi and Ethernet.
- **Deployment:** a single process in an Alpine Linux LXC on Proxmox, managed by
  OpenRC; the LXC must be bridged onto the LAN segment it scans (ARP and mDNS
  require layer-2 adjacency). Remote access via a reverse proxy/tunnel with its
  own auth (e.g. Cloudflare Access); LAN-only mode must also work.
- **Language:** Python 3.12+. The MCP layer reuses patterns from the owner's
  `mcp-server-shared` repo (`remote.py`, `logsafe.py`) where applicable.

## 3. Non-goals (Phase 1)

- IPv6 discovery or display.
- Floor plan view (Phase 2) — the `place` table exists; the drawing UI does not.
- Bluetooth discovery (Phase 3), MQTT/Home Assistant publishing and notifications
  (Phase 4), Z-Wave (Phase 5).
- Automatic physical topology behind unmanaged switches (impossible without
  SNMP/LLDP; links behind them are drawn manually).
- Multi-user accounts or RBAC inside the app (auth is delegated to the proxy).
- Controlling devices (rebooting, blocking clients on the router).

## 4. Architecture

```
collectors ─► ingest ─► core store ─► service layer ─┬─► HTTP API ─► web UI
(asusrouter,   merge &   (SQLite)     reads/writes,  ├─► MCP server
 arp, mdns,    identity               provenance,    └─► event bus ─► (sinks: Phase 4)
 ssdp, nmap)                          change log
                 ▲
             scheduler
```

One Python process: FastAPI app + in-process APScheduler (interval and cron
triggers; nightly scans need cron semantics). Units and their contracts:

| Unit | Responsibility | Depends on |
|---|---|---|
| `collectors/*` | Gather raw facts from one source; return `list[Observation]`. Never touch the DB. | Source libs/binaries only |
| `ingest` | Turn observations into devices/interfaces/leases/services; identity matching; fill rules; emit events. The *only* writer for discovered data. | store, service layer |
| `store` | SQLite schema, migrations, repository functions. | — |
| `service` | Every read and every user/Claude write. Enforces provenance, writes the change log, implements undo. | store, event bus |
| `api` | HTTP JSON API + HTMX views. Thin over `service`. | service |
| `mcp` | MCP tools, thin over `service`, actor=`claude`. | service |
| `scheduler` | Runs collectors on their schedules; respects scan policy. | collectors, ingest |
| `events` | In-process pub/sub (`device.first_seen`, `device.online`, `device.offline`, `change.applied`). | — |

### 4.1 Collector contract

```python
@dataclass(frozen=True)
class Observation:
    source: str                 # "asusrouter", "arp", "mdns", "ssdp", "nmap"
    seen_at: datetime           # UTC
    iface_kind: str | None      # "ethernet" | "wifi" | None (unknown)
    mac: str | None             # normalized lower-case colon form
    ipv4: str | None
    hostname: str | None
    attrs: dict                 # source-specific: ap_node, band, rssi, mdns services,
                                # ssdp server/model, nmap ports, vendor hints...

class Collector(Protocol):
    name: str
    async def collect(self) -> list[Observation]: ...
```

Collectors are registered via config; each has its own schedule and timeout.

### 4.2 Phase 1 collectors

| Collector | Source | Default schedule | Notes |
|---|---|---|---|
| `asusrouter` | `asusrouter` Python lib (router HTTP API) | 2 min | Clients, IP, hostname, wired/Wi-Fi, AiMesh node, band. Requires router credentials in config. |
| `arp` | `arp-scan` on the bridged interface | 5 min | Requires `CAP_NET_RAW`. |
| `mdns` | `zeroconf` browse of common service types | continuous listener, flush 1 min | Friendly names, service types. |
| `ssdp` | SSDP M-SEARCH | 10 min | Model/manufacturer for UPnP devices. |
| `nmap_light` | `nmap` top ~100 TCP ports, `-T3`, service detection off | nightly | Skips `scan_policy=never`. |
| `nmap_deep` | `nmap -sV` full TCP range for one device | on demand only | Skips `scan_policy=never`; refuses with a clear error. |

OUI vendor lookup uses a bundled, periodically refreshed IEEE OUI file (no network
call at lookup time).

## 5. Data model

Example values use documentation ranges (§9). All timestamps UTC.

- **device** — `id, name, type, icon, place_id?, pos_x?, pos_y?, owner?, notes?,
  trust (known|new|unwanted), scan_policy (normal|never), first_seen, last_seen`.
  Example: `name="Living Room Shield", type=media, icon=tv`.
- **interface** — `id, device_id, kind, address, is_random, vendor?, ap_node?, band?,
  first_seen, last_seen`.
  `kind` ∈ {`ethernet`, `wifi`} in Phase 1; extensible to `bluetooth`, `zwave`.
  `address` is the MAC for Ethernet/Wi-Fi (later: BT address, `homeid:node` for Z-Wave).
  `is_random` = locally-administered bit set (second-least-significant bit of the first octet).
  Unique on `(kind, address)`.
- **ip_lease** — `interface_id, ipv4, first_seen, last_seen`. History, not just current.
- **service** — `device_id, port, proto, name?, product?, version?, source, first_seen, last_seen`.
- **place** — `id, name, floor?, polygon?` (polygon unused until Phase 2).
- **link** — `id, a_device_id, b_device_id, kind (wired|wifi|mesh_backhaul), detail?,
  source (manual|discovered), layout?`. Discovered links come from asusrouter
  (client ↔ AP node); everything behind unmanaged switches is manual.
- **observation** — raw collector output (JSON), retained `observation_retention_days`
  (default 30).
- **field_source** — `entity, entity_id, field, actor, set_at`. One row per field
  currently set; records who owns that value.
- **change** — append-only: `id, batch_id, actor, entity, entity_id, field,
  old_value, new_value, at`. `actor` ∈ `user`, `claude`, `collector:<name>`, `system`.
- **topology_layout** — saved node positions for the topology view.

## 6. Ingest and identity

For each collector run:

1. Persist observations.
2. **Correlate.** Match by `(kind, mac)` first. For IP-only observations (mDNS,
   SSDP, nmap), match to the interface holding that IPv4 in an `ip_lease` whose
   `last_seen` is within `ip_match_window` (default 10 min). If none, keep the
   observation unattached; retry on the next run; drop after retention.
3. **Unknown MAC** → create `device(trust=new)` + `interface`; emit
   `device.first_seen` with `is_random` in the payload.
4. **Update discovered facts:** `last_seen`, `ip_lease`, `ap_node`, `band`,
   `vendor`, services (upsert by `(device, port, proto)`).
5. **Fill rules** for user-facing fields (`name`, `type`, `icon`):
   - Precedence: `user` > `claude` > collectors.
   - A collector may only set a field whose `field_source.actor` is a collector
     (or that is empty).
   - Name candidates, best first: mDNS instance name > router/DHCP hostname >
     SSDP friendly name > reverse DNS > `"<vendor> .<last octet>"`.
6. **IP reuse.** When an IPv4 appears on a different MAC, close the old lease
   (`last_seen` stays as is) and open a new one; no merge.
7. **Online/offline.** A device is online if any interface was seen within its
   grace period: default 10 min; `type ∈ {phone, tablet, laptop}` default 30 min.
   Transitions emit `device.online` / `device.offline`.
8. **Random MACs** are never auto-merged. The Inbox shows them with a
   "claim onto device…" action. A claim moves the interface (and its leases and
   observations) to the target device as one change batch; if the source device
   becomes empty it is deleted in the same batch. Undo restores both.

**Idempotency:** re-ingesting the same observations produces no new rows in
`change` and no state difference (tested, §10).

**Errors:** a collector exception or timeout is logged, recorded in
`collector_status (name, last_ok, last_error, consecutive_failures)`, and backed
off exponentially (cap 1 h). It never blocks other collectors. The UI shows a
red status chip per failing collector.

## 7. Service layer, change log, undo

- Every user or Claude write goes through `service.apply(actor, ops) -> batch_id`.
  One call = one batch = one transaction.
- Each field write records a `change` row and updates `field_source`.
- `service.undo(batch_id)` writes a new batch that restores `old_value`s and
  `field_source` rows. Undo of an older batch is refused if a later batch changed
  the same field (returns the conflicting batch ids) — no silent clobbering.
- Collector writes are recorded in `change` only for user-facing fields (name,
  type, icon) to keep the log readable; high-churn fields (`last_seen`, leases)
  are not logged.

## 8. Interfaces

### 8.1 Web UI

Server-rendered (Jinja2 + HTMX), one small JS graph library for topology
(Cytoscape.js), no SPA build step.

- **Devices** — searchable/sortable table: online dot, name, icon, IPv4, MAC(s),
  vendor, place, AP node/band, service count, last seen. Filters: online, type,
  trust, place, AP node.
- **Device page** — editable name/type/icon/place/owner/notes/trust/scan_policy,
  each showing "set by <actor> · <date>"; interfaces; IP history; services;
  **Deep scan** button (disabled when `scan_policy=never`).
- **Inbox** — `trust=new` devices and unclaimed random-MAC interfaces, with quick
  actions: name, set type, claim onto device, mark known/unwanted.
- **Topology** — router → mesh node(s) → switches → devices. Discovered links
  dashed, manual links solid. Drag to arrange (positions saved); add/remove
  manual links; add placeholder devices (e.g. an unmanaged switch with no IP).
- **History** — change log grouped by batch, filter by actor, **Undo** per batch.
- **Status** — collector health, last run, counts.

### 8.2 HTTP API

JSON under `/api/v1`, mirroring service-layer operations (devices, interfaces,
services, places, links, changes, undo, scans, collectors). OpenAPI generated by
FastAPI.

### 8.3 MCP server

Same process, streamable HTTP transport at `/mcp`; all writes use `actor=claude`.

| Tool | Purpose |
|---|---|
| `find_device(query)` | Match by IPv4, MAC (full or partial), name, vendor, hostname |
| `list_devices(filters)` | online/type/trust/place/ap_node filters, paginated |
| `get_device(id)` | Full record incl. interfaces, IP history, services, field sources |
| `list_services(device_id? \| port?)` | "What runs SSH?" / "What's on the NAS?" |
| `update_device(id, fields)` | name/type/icon/place/owner/notes/trust/scan_policy |
| `claim_interface(interface_id, device_id)` | Attach a random-MAC interface to a device |
| `set_place(device_id, place_name)` | Creates the place if missing |
| `run_deep_scan(device_id)` | Respects `scan_policy=never` |
| `recent_changes(limit, actor?)` | Audit trail |
| `undo(batch_id)` | Revert a batch |

Tool outputs never include router credentials or raw config.

### 8.4 Auth

Delegated to the fronting proxy (e.g. Cloudflare Access) for both UI and `/mcp`.
For LAN-only use, the app binds to a configured interface with no auth.
An optional static bearer token for `/mcp` and `/api` can be enabled in config.

## 9. Privacy and repository hygiene

- **Data directory** outside the repo: `$LANSCAPE_DATA_DIR` (default
  `/var/lib/lanscape-atlas`) holds `atlas.db`, `config.toml`, OUI file, and later
  floor-plan assets. `.gitignore` also excludes `data/`, `*.db`, `config.toml`.
- **Config** ships only as `config.example.toml` with placeholder values.
  Secrets (router password, MCP token) may also come from environment variables.
- **Fixtures and examples** use only:
  - IPv4: `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` (RFC 5737)
  - MACs: `00:00:5e:00:53:00`–`00:00:5e:00:53:ff` (RFC 7042 documentation range)
  - Hostnames/rooms: generic (`living-room-tv`, `office-switch`).
- **Leak checks** — `scripts/check_leaks.py`, run by a pre-commit hook and in CI,
  fails on:
  - any MAC-shaped string outside the RFC 7042 range;
  - any RFC 1918 IPv4 address (`10/8`, `172.16/12`, `192.168/16`) outside an
    explicit allow-list of files (e.g. docs stating the RFC 1918 ranges themselves);
  - any term from an optional, untracked `~/.config/lanscape-atlas/denylist.txt`
    (personal hostnames, room names, domain).
- **Anonymizer** — `scripts/anonymize.py` converts real captures into fixtures,
  mapping each real MAC/IP/hostname to a documentation-range value consistently
  within a capture set (same input → same output), so relationships survive.
- **License:** MIT.

## 10. Testing

- **Collectors:** unit tests parse recorded, anonymized fixtures (router JSON,
  `arp-scan` output, zeroconf records, SSDP responses, nmap XML). No network in CI.
- **Ingest:** table-driven tests for name precedence, user/Claude override
  protection, random-MAC flagging, claim + undo, IP reuse across MACs, IP-only
  correlation window, online/offline grace, and idempotent re-ingest.
- **Service layer:** provenance, batching, undo, undo-conflict refusal.
- **API/MCP:** thin contract tests over a temp SQLite DB.
- **Fake house dataset:** ~40 devices in documentation ranges, used for tests,
  local UI development and README screenshots.
- **CI:** GitHub Actions — ruff, mypy (strict on `service`, `ingest`, `store`),
  pytest, leak check.

## 11. Phasing

| Phase | Scope | Notes |
|---|---|---|
| **1 (this spec)** | Core, ingest, collectors (asusrouter, arp, mdns, ssdp, nmap light/deep), web UI (devices, inbox, device page, topology, history, status), HTTP API, MCP | |
| 2 | Floor plan: upload image/SVG, draw rooms, drag devices | Uses `place.polygon`, `device.pos_x/y` |
| 3 | Bluetooth via ESP32/ESPHome Bluetooth proxies | `interface.kind=bluetooth`; rough room location |
| 4 | MQTT/Home Assistant publishing (HA MQTT discovery) and new-device notifications | Subscribes to event bus; default alerts only for non-random new MACs |
| 5 | Z-Wave via Hubitat/Home Assistant | `interface.kind=zwave`, `address=homeid:node` |

Each later phase gets its own spec → plan → implementation cycle.

## 12. Open questions deferred to the plan

- Exact mDNS service-type list to browse (start with a curated ~30).
- Whether the ASUS router exposes per-client switch-port info on the router
  (if so, it becomes a discovered `link` detail).
