# lanscape-atlas — Phase 3 Design: Bluetooth (Find)

**Status:** Approved by the owner (2026-11-10), including the three decisions flagged at
review: §11 supersedes Phase 2 §7, the static-random test address is built at runtime
(§9), and Bluetooth has its own retention (§8).
**Scope:** Phase 3 of §11 of the Phase 1 design. Phases 1 and 2 are complete.
**Supersedes:** Phase 2 §7 ("Phase 3 hook") where it differs; see §11.

## 1. Goal

Answer "where did I leave it?" for phones, remotes and headphones, at room level with
recency: "last heard near Room A, 4 min ago". Not a point on the plan.

The design says plainly what is findable. It does not promise every Bluetooth device.

## 2. Non-goals

- A position inside a room, trilateration, or fusing several scanners' RSSI. Room
  attribution comes from the one scanner Home Assistant (HA) picks per address (§4.2).
- iPhones, AirPods and other Apple devices. They rotate their address and need an IRK.
  No IRK handling ships in this phase.
- Talking to ESPHome proxies directly (native API). See §4.1 for why.
- Storing unclaimed devices (neighbours, passers-by). See §5.3.
- Changing a device's `place_id`, `pos_x`, `pos_y` or `roaming`. Bluetooth never writes
  them.
- Making a remote beep or a phone ring. Out of scope; the device's own app does that.

## 3. What is findable

| Kind | How it is matched | What "last heard" means |
|---|---|---|
| Android phone | HA Companion app "BLE Transmitter" iBeacon (UUID, major, minor) | Current, to about one snapshot interval (§4.2, default 60 s). Lags if Android battery optimisation throttles the app. |
| Beaconing remote (advertises every few seconds) | Static or public address | Current, to about one snapshot interval. |
| Press-to-advertise remote | Static or public address | When it was last pressed. |
| Headphones with a stable address | Static or public address | Last time they were idle and nearby. Many go silent while connected. |
| Anything with a rotating address | Not matched | Not findable. Never shown. |

The Find page shows a per-device badge from observed behaviour (§6.1) so this is
visible, not just documented.

## 4. Architecture

### 4.1 Source: Home Assistant, not the ESPHome native API

- HA already merges every scanner: the HA host's USB adapter, the Shelly plug's
  passive scanner, and future ESPHome proxies.
- An ESPHome `bluetooth_proxy` has one subscriber slot. In ESPHome 2026.10 the newest
  subscriber takes it and the previous client's GATT connections are dropped. atlas
  subscribing directly would knock HA off the proxy, and the two would flap. The native
  route would also never see the Shelly or the HA host adapter.
- Accepted costs:
  - HA is a dependency. When HA is unreachable, Find shows stale data with a banner
    (§6.1) and the status page shows the collector failing.
  - atlas holds an HA **admin** long-lived token, because the advertisement subscription
    is admin-only. It is a secret like the router password (§9).

### 4.2 The HA advertisement stream

atlas opens one WebSocket to HA (`/api/websocket`), authenticates with the token and
sends two subscriptions:

- `bluetooth/subscribe_advertisements` (no parameters). Each event is either
  `{"add": [adv, ...]}` or `{"remove": [{"address"}]}`. An `adv` carries `name`,
  `address`, `rssi`, `manufacturer_data` (company id → hex), `service_data`,
  `service_uuids`, `source` (scanner), `connectable`, `time` (epoch seconds, HA's clock),
  `tx_power`, `raw`.
- `bluetooth/subscribe_scanner_details`, to learn each scanner's `source` and name.

**The advert stream is change-driven, not a feed of every advert.** In habluetooth 7.1.2
(pinned by HA 2026.11.1) every advert updates HA's history (the latest advert per
address), but the subscription callbacks only run when `manufacturer_data`,
`service_data`, `service_uuids` or `name` differ from that address's previous advert. A
steady beacon, remote or phone iBeacon therefore produces about one event, ever.
Registering the subscription replays the current history as one `add` batch right after
the result. atlas builds on that:

- **Snapshot re-subscribe.** After the handshake, every `snapshot_s` seconds (default 60,
  §8) atlas sends `unsubscribe_events` for the current advert subscription and then a new
  `bluetooth/subscribe_advertisements`; the replay that follows is the snapshot. Command
  ids keep increasing per connection. Events for a replaced subscription id are ignored. A
  failed unsubscribe or subscribe result is an error and reconnects with the usual backoff.
  The scanner-details subscription is not repeated. One snapshot is one WebSocket frame,
  so the client accepts frames up to 16 MiB. The handshake timeout covers only the initial
  handshake.
- **HA's time, corrected for clock skew.** Each advert keeps HA's `time` (an advert with a
  missing or non-finite time is skipped). Per connection, atlas tracks
  `offset = min(receipt − time)` over the adverts received on it (fresh at every connect;
  one frame is received at one instant, so a whole batch is folded in before any of it is
  used). An advert's **effective time** is `min(receipt, time + offset)`. So a replayed
  advert keeps its true age instead of looking "heard now", HA clock skew cancels out, and
  nothing is ever placed after its receipt. An advert stamped more than 24 h ahead of
  receipt is garbage, not skew, and is dropped so it cannot set the offset.
- **Per-identity dedupe.** The aggregator remembers the effective time last counted per
  identity and ignores an advert that is not strictly newer, so each snapshot counts a
  steady device at most once and a replay of an unchanged advert is never double-counted.
  Adverts with an effective time older than 24 h are ignored.

"Last heard" for a steady device is therefore accurate to about one snapshot interval,
and a sighting's advert count is the number of snapshots (plus payload changes) that saw
it in that minute, not the number of adverts on the air.

HA picks one **owning** scanner per address, by smoothed RSSI with a switch threshold
and a reclaim deadband. Adverts heard by other scanners do not appear in the stream.
So the stream directly answers "which scanner is it nearest", with hysteresis. A
device near a room boundary can stay attributed to the neighbouring room. This is
accepted (§2).

`remove` events are ignored for storage. "Last heard" comes from the last advert.

Minutes are bucketed by an advert's effective time (HA's `time` corrected for skew, above),
not by receipt: most adverts arrive in a snapshot replay, up to a snapshot interval (or,
after a reconnect, much longer) after they were heard. Late rows for a minute that was
already flushed are fine: `record_ble_minutes` upserts and merges them.

### 4.3 Components

- **`collectors/bluetooth_ha.py` — `HaBluetoothClient`.** Long-lived, like the mDNS
  collector (`start()`/`stop()`). Owns the WebSocket, reconnects with the runner's
  existing exponential backoff, re-subscribes the advert stream every `snapshot_s`
  (§4.2), parses events into `Advert` values and hands each on with its effective time
  (`bluetooth/skew.py`). No DB access.
- **`bluetooth/match.py`.** Pure functions:
  - `ibeacon(manufacturer_data) -> (uuid, major, minor) | None`: company id 76, data
    prefix `02 15`, then 16 + 2 + 2 bytes.
  - `identity_of(advert) -> Identity | None`: the iBeacon if present, else the address.
  - `is_stable_address(address, span, oui)` (§5.3).
- **`bluetooth/aggregator.py` — `BleAggregator`.** In memory. Keyed by
  `(device_id, scanner_id, minute)`, the minute of the effective time. Keeps max RSSI,
  advert count, last time, and per identity the last effective time counted (replay
  dedupe, §4.2). Also holds the candidate list (§5.3), whose first/last heard and gaps
  use effective times. Bounded (§8).
- **Flush job (`ble_flush`, every 60 s).** Drains closed minutes from the aggregator
  and calls `Service.record_ble_minutes(rows)` under `Service.lock`, in one
  transaction. Records health in `collector_status` under the name `bluetooth`.
- **Identity index.** An in-memory map from identity to `device_id`, loaded at start
  and refreshed on claim, unclaim and undo (EventBus `change.applied`). Adverts with no
  claimed identity go to the candidate list only.

Collectors still never touch the DB. BLE data does not go through `Observation` or the
ingestor: an advert has no MAC/IP identity in the Phase 1 sense, and storing raw adverts
would cost far more than the per-minute rows.

## 5. Data model

Schema version 3, migration `store/migrations/0003_bluetooth.sql`. Schema v1 and the
v2 migration are not edited.

### 5.1 New tables

- **`ble_identity`** — what a claimed device is recognised by.
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT
  - `device_id` INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE
  - `kind` TEXT NOT NULL CHECK (kind IN ('address','ibeacon'))
  - `value` TEXT NOT NULL — normalized address (`aa:bb:…`, lower case), or
    `uuid/major/minor` with the UUID lower case and major/minor in decimal
  - `created_at` TEXT NOT NULL
  - UNIQUE (kind, value). A device can have several identities.
- **`ble_scanner`** — every scanner HA reports.
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT (the change log keys rows by integer id)
  - `source` TEXT NOT NULL UNIQUE (the scanner's Bluetooth MAC as HA gives it)
  - `name` TEXT (from HA; collector-written)
  - `device_id` INTEGER REFERENCES device(id) ON DELETE SET NULL — the atlas device the
    scanner is linked to; NULL until linked
  - `first_seen`, `last_seen` TEXT NOT NULL
- **`ble_sighting`** — per-minute aggregate, claimed devices only.
  - `device_id` INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE
  - `scanner_id` INTEGER NOT NULL REFERENCES ble_scanner(id) ON DELETE CASCADE
  - `minute` TEXT NOT NULL (UTC, `YYYY-MM-DDTHH:MMZ`)
  - `place_id` INTEGER REFERENCES place(id) ON DELETE SET NULL — the scanner's room at
    write time (§5.2)
  - `max_rssi` INTEGER NOT NULL, `adverts` INTEGER NOT NULL, `last_at` TEXT NOT NULL
  - PRIMARY KEY (device_id, scanner_id, minute), WITHOUT ROWID
  - Index on (device_id, last_at) for "last heard", and on (minute) for the prune job.

### 5.2 Rooms

- A scanner's room is the Phase 2 `place_id` of the atlas device it is linked to. The
  proxies, the Shelly and the HA host are all atlas devices already.
- Linking is a one-time manual pick on the scanner list (§6.4). The UI suggests a
  device by name, and by the ESP32 rule "Bluetooth MAC = Wi-Fi MAC + 2" when an
  interface matches. Suggestions are never applied automatically.
- `ble_sighting.place_id` is a snapshot. Moving a proxy later does not rewrite history.
- A sighting with no room (scanner unlinked, or its device has no room) shows as
  "near <scanner name>".

### 5.3 Claimed devices and candidates

- **Claimed.** A claimed item is an atlas device with one or more `ble_identity` rows.
  - Phone: the identity is attached to the phone's existing device (known from Wi-Fi).
  - Remote or headphones: a new device with no interfaces, like a Phase 1 placeholder.
- **Candidates.** Unclaimed sources live in memory only. Nothing is persisted.
  - A source is a candidate if it is an iBeacon, or a **stable address**: the address
    is static-random (top two bits `11`), or its OUI is in the vendor database, or it
    has been heard over a span of at least 30 minutes (longer than typical rotation).
    The HA payload carries no address type, so this is a heuristic (§12).
  - Kept for 24 hours after last heard. Each entry holds identity, advert name, best
    RSSI, last scanner, last heard, advert count, and the typical gap between adverts.
- There is no history from before a claim.

### 5.4 Device presence

- Device `online` stays network-only. Bluetooth never changes it.
- "Last heard" for a device is its newest `ble_sighting` row: room (or scanner), time,
  RSSI. It is read at query time; there is no `heard_in_place_id` column (§11).

## 6. Interfaces

### 6.1 Find page (`/find`, new nav item)

- Claimed devices: name, room or "near <scanner>", "N min ago", an RSSI bar, and a
  badge:
  - **steady** — heard in at least 80% of the minutes in its last active hour;
  - **intermittent** — heard, but less often (pressed, idle-only, or throttled);
  - **not heard in 24 h**.
- A search box (same matcher as the device list, by name).
- **Candidates** section: identity, advert name, nearest scanner, last heard, gap.
  "Claim" opens a small form: attach to an existing device, or create one with a name
  and type.
- A banner when the HA connection has been down for more than 2 minutes, with the last
  error (redacted).
- Server-rendered with HTMX, polling every 30 s. No new JS library.

### 6.2 Device page

- "Last heard near Room A, 4 min ago" (or "near <scanner>").
- A 24-hour strip: one cell per hour, showing the room with the most adverts in that
  hour.
- The device's BLE identities, each with "Unclaim".

### 6.3 Floor plan

- The room a device was last heard in gets a soft highlight when that device is
  selected, and the room's tray shows a "heard" chip for it.
- This is separate from the placed position. A remote placed in Room A but heard in
  Room B shows both.

### 6.4 Scanners

- A "Bluetooth scanners" table on the status page: name, source, linked device, room,
  last seen, and a "Link" control with the suggestions from §5.2.

### 6.5 HTTP API (bearer-protected, under `/api/v1`)

- `GET /find?q=` — claimed devices with last heard and badge.
- `GET /ble/candidates`
- `POST /ble/claim` — `{identity, device_id}` or `{identity, new: {name, type}}`.
- `DELETE /ble/identities/{id}` — unclaim.
- `GET /ble/scanners`, `PUT /ble/scanners/{source}/device` — link or unlink.
- Errors follow the existing mapping: KeyError → 404, ValueError → 422,
  UndoConflict → 409.

### 6.6 MCP

- `where_is(query)` — room or scanner, last heard, RSSI, badge, and how stale the
  reading is.
- `ble_candidates()` — unclaimed sources, newest first.
- `claim_ble(candidate, device_id | new_name)` — actor `claude`, undoable.
- `INSTRUCTIONS` mentions the three tools and that rotating-address devices are not
  findable.

## 7. Undo and change log

- Claim, unclaim and scanner linking are batches with an actor (`user` or `claude`),
  undoable through the existing machinery.
- Claim with a new device logs the device creation and the identity creation in one
  batch. Undo deletes both; the device's sightings go with it (cascade).
- Unclaim deletes the identity row and logs it, so undo re-inserts it with the same id.
  Sightings stay with the device.
- Collector writes (`ble_sighting`, `ble_scanner.name`, `ble_scanner.last_seen`) are not
  change-logged, like observations.
- The existing rules hold: undo refuses (409) when a later batch changed the same
  rows; undo-of-undo is redo.

## 8. Configuration, storage and limits

- New config table, enabled only when present (same pattern as `[router]`):

  ```toml
  [home_assistant]
  url = "http://192.0.2.10:8123"
  token = ""          # or LANSCAPE_HA_TOKEN
  verify_tls = true

  [bluetooth]
  retention_days = 14
  snapshot_s = 60     # advert re-subscribe interval (§4.2)
  ```

- Storage: at most one row per claimed device per scanner per minute heard. Worst case,
  20 claimed devices heard nonstop for 14 days is about 400k rows, roughly 30–40 MB with
  the index. The nightly prune job also deletes `ble_sighting` rows older than
  `retention_days`.
- Memory bounds: the aggregator holds the open minute plus any late minutes a replay
  added since the last flush (each drained by the next flush). The candidate list is
  capped at 500 entries; the pre-candidate tracker (addresses not yet known to be
  stable) at 5,000, evicting least recently heard.
- Load: one snapshot frame per `snapshot_s` (HA's whole advert history, typically a few
  hundred adverts) plus payload-change events, parsed on the event loop. The flush is one
  short transaction per minute under `Service.lock`.

## 9. Privacy

- The HA token is a secret: never logged, redacted in errors and `repr`, like the
  router password.
- Real addresses, iBeacon UUIDs, advert names and room names are personal data. They
  live in the database or in memory on the server only.
- Fixtures use public addresses from `00:00:5e:00:53:xx` and iBeacon UUIDs
  `00000000-0000-4000-8000-0000000000xx`.
- The static-random rule (§5.3) needs a test address whose top two bits are `11`, which
  the check_leaks allow-list (`00:` / `02:00:5e:00:53:xx`) does not cover. The allow-list
  is not widened. Tests build that address at runtime by setting the top two bits of a
  documentation address (the Phase 1 Ruling 5 pattern), so no such literal appears in
  the repo.
- `scripts/anonymize.py` gains rewriting of Bluetooth addresses and iBeacon UUIDs in a
  captured advert stream, so a real capture can become a fixture.
- `deploy/esphome/ble-proxy.example.yaml`: one generic classic-ESP32 proxy config.
  Only `!secret` references and a placeholder name. A moderate scan duty cycle, since a
  Wi-Fi ESP32 shares one radio between Wi-Fi and Bluetooth. Real per-proxy YAML stays in
  HA.

## 10. Testing

- **Parsing:** iBeacon decode (valid, short, wrong prefix, other company ids); identity
  choice; stable-address rules at their boundaries.
- **Client:** a fake HA WebSocket server (aiohttp test server) modeled on the real stream
  (per-address history, an event only on a payload change, a history replay after every
  subscribe), covering auth ok, auth failure, both subscriptions, snapshot re-subscribes
  and their failures, skew correction, reconnect after a drop, and token redaction in
  errors.
- **Aggregator:** max RSSI, counts, minute boundaries, candidate expiry and caps.
- **Service:** `record_ble_minutes` upserts; room snapshot; prune; claim/unclaim/link
  with undo and redo, using the existing undo test pattern.
- **Queries:** last heard, badge thresholds, 24-hour strip.
- **Web/API/MCP:** each route and tool, including the actor and 404/409/422.
- **Fake house:** extend the fixture with a synthetic advert capture so the Find page
  and floor-plan highlight render in the existing Playwright pass.
- **Privacy:** `check_leaks` passes; the example YAML has no real values.

## 11. Changes to the Phase 2 design

Phase 2 §7 sketched the hook before this design was settled. Two parts change:

- **Bluetooth identities are not `interface` rows with `kind=bluetooth`.** Interfaces
  drive leases, presence, nmap targets and claim/merge. A BLE address there would leak
  into all of it, and an iBeacon is not a MAC. `ble_identity` is used instead. The
  `bluetooth` value stays in the `interface.kind` CHECK and is unused.
- **There is no `heard_in_place_id` column.** "Last heard" is read from `ble_sighting`,
  which also keeps the history the device page needs. The Phase 2 rule still holds:
  Bluetooth never changes `place_id`, `pos_x`, `pos_y` or `roaming`.

Phase 2 §7's first point stands: a proxy is a fixed device with a room.

## 12. Delivery and open items for the plan

- **First commit on the branch:** an Alpine CI job beside the Ubuntu job. It runs the
  full suite (ruff check, ruff format --check, mypy src, pytest, check_leaks) in an
  Alpine container with apk's python3, at CT 210's Alpine release, with arp-scan and
  nmap installed. Real failures it exposes are fixed test-first.
- Confirm the exact field names of `subscribe_scanner_details` against HA 2026.11.
- Measure the real advert rate on the owner's HA before choosing the flush and cap
  numbers finally.
- The stable-address heuristic can misclassify a public address whose first byte looks
  random. Check against the owner's real capture (anonymized) during the build.
- Before release, validate the advert rate, the badge thresholds and the stable-address
  rule against an anonymized real capture of the snapshot stream (§4.2): with snapshots,
  counts and gaps reflect snapshots and payload changes, not adverts on the air.
- Decide whether the 24-hour strip and the badge are computed in SQL or in Python.
