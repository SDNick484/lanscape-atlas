# lanscape-atlas Phase 3 (Bluetooth Find) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer "where did I leave it?" for claimed Bluetooth devices: "last heard near Room A, 4 min ago".

**Architecture:** A long-lived Home Assistant WebSocket client feeds adverts into an in-memory
aggregator (per device × scanner × minute, plus an unclaimed-candidate list). A 60 s flush job
writes closed minutes to `ble_sighting` under `Service.lock`. Claims, unclaims and scanner links
are change-logged Service writes. Pages, API and MCP read through a new `service/ble_queries.py`.

**Tech Stack:** Python ≥3.12 (CI also 3.14 on Alpine), SQLite, aiohttp (WebSocket client and test
server), FastAPI + Jinja2 + HTMX, mcp 2.x, APScheduler 3.x. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-10-lanscape-atlas-phase3-bluetooth-design.md`

## Global Constraints

- No personal data in the repo. Fixtures use addresses `00:00:5e:00:53:xx` / `02:00:5e:00:53:xx`
  and iBeacon UUIDs `00000000-0000-4000-8000-0000000000xx`. The static-random test address is
  built at runtime (spec §9); no `c2:…` literal anywhere.
- Every commit passes: `ruff check .`, `ruff format --check .`, `mypy src`, `pytest`,
  `python scripts/check_leaks.py`. Both CI jobs (Ubuntu 3.12, Alpine 3.24.1 / 3.14) must pass.
- Commits authored as `Nicholas E. Andrade <SDNick484@users.noreply.github.com>`; end each message
  with the `Co-Authored-By` / `Claude-Session` trailers. No amend, no stash, no history rewrite,
  no push without the owner's OK.
- Schema v1 and migration 0002 are never edited. Phase 3 is migration 0003, `SCHEMA_VERSION = 3`.
- Collectors never touch the DB. Bluetooth never writes `device.place_id`, `pos_x`, `pos_y`,
  `roaming` or `online`.
- The HA token is a secret: `repr=False`, added to `CollectorRunner` secrets, never logged.
- Minutes are bucketed (`YYYY-MM-DDTHH:MMZ`, UTC) by an advert's effective time: HA's
  `time` corrected for clock skew per connection (`min(receipt, time + offset)`,
  `offset = min(receipt − time)`), because HA's stream is change-driven and most adverts
  arrive in a periodic snapshot replay (spec §4.2, amended in the final fix wave). Replays
  are deduplicated per identity; adverts older than 24 h are ignored.
- Write actors stay `user` / `claude` / `system`. Error mapping stays KeyError→404,
  ValueError→422, UndoConflict→409.

## Review Focus

1. **HA drops the connection or restarts mid-stream.** The client reconnects with backoff, the
   aggregator keeps its open minutes, nothing is double-counted, and the Find banner appears only
   after 2 minutes down (Task 7 test `test_reconnects_after_server_drop`, Task 10 banner test).
2. **Malformed events** (missing `address`, `rssi` null, odd-length hex in `manufacturer_data`,
   unknown event shape) are skipped without killing the stream (Task 3
   `test_parse_advert_rejects_malformed`, Task 7 `test_bad_event_is_skipped`).
3. **One device heard by two scanners in the same minute** (HA ownership switch) yields two rows;
   "last heard" picks the newest `last_at`, not the higher RSSI (Task 6
   `test_last_heard_newest_wins_over_stronger`).
4. **Claiming something already claimed, or a candidate that expired between page load and
   submit**: 422 with a clear message, nothing written (Task 5 `test_claim_duplicate_identity`,
   Task 9 `test_claim_unknown_candidate_422`).
5. **Undo of "claim as new device" after sightings exist** deletes the device and its sightings
   instead of refusing (Task 5 `test_undo_claim_new_device_with_sightings`).

---

### Task 1: Schema v3 migration

**Files:**
- Create: `src/lanscape_atlas/store/migrations/0003_bluetooth.sql`
- Modify: `src/lanscape_atlas/store/db.py` (`SCHEMA_VERSION = 3`)
- Modify: `docs/superpowers/specs/2026-11-10-lanscape-atlas-phase3-bluetooth-design.md` §5.1
- Test: `tests/test_migrations.py`, `tests/test_packaging.py`

**Interfaces:**
- Produces tables (exact columns):
  - `ble_identity(id INTEGER PRIMARY KEY AUTOINCREMENT, device_id INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE, kind TEXT NOT NULL CHECK (kind IN ('address','ibeacon')), value TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (kind, value))`, index on `device_id`.
  - `ble_scanner(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL UNIQUE, name TEXT, device_id INTEGER REFERENCES device(id) ON DELETE SET NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)`.
  - `ble_sighting(device_id INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE, scanner_id INTEGER NOT NULL REFERENCES ble_scanner(id) ON DELETE CASCADE, minute TEXT NOT NULL, place_id INTEGER REFERENCES place(id) ON DELETE SET NULL, max_rssi INTEGER NOT NULL, adverts INTEGER NOT NULL, last_at TEXT NOT NULL, PRIMARY KEY (device_id, scanner_id, minute)) WITHOUT ROWID`, index `(device_id, last_at)`, index `(minute)` for prune.

- [ ] **Step 1: Write failing tests** in `tests/test_migrations.py`:
  `test_v2_to_v3_keeps_data` (build a v2 DB with one device and one place, migrate, assert
  `user_version == 3`, rows intact, three new tables exist); `test_fresh_equals_upgraded_v3`
  (compare `sqlite_master` SQL of a fresh DB and a v2→v3 DB for the three new tables);
  `test_sighting_cascades_with_device`. In `tests/test_packaging.py`, assert
  `0003_bluetooth.sql` is listed beside `0002_floor_plan.sql`.
- [ ] **Step 2: Run** `pytest tests/test_migrations.py tests/test_packaging.py -v`. Expected: FAIL (`user_version` 2).
- [ ] **Step 3: Implement** the migration file and bump `SCHEMA_VERSION`. Amend spec §5.1:
  `ble_scanner` gets an integer `id` (the change log keys rows by integer id) with `source`
  UNIQUE, and `ble_sighting` references `scanner_id` instead of `scanner_source`. Add one line to
  spec §4 stating minutes are bucketed by atlas's clock at receipt.
- [ ] **Step 4: Run** the full gate. Expected: all clean.
- [ ] **Step 5: Commit** `feat(store): schema v3 for Bluetooth identities, scanners and sightings`.

### Task 2: Configuration

**Files:**
- Modify: `src/lanscape_atlas/config.py`, `config.example.toml`, `src/lanscape_atlas/scheduler.py` (`CollectorRunner.from_settings` secrets)
- Test: `tests/test_config.py`, `tests/test_scheduler.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class HomeAssistantSettings: url: str; token: str = field(repr=False); verify_tls: bool = True`
  - `@dataclass(frozen=True) class BluetoothSettings: retention_days: int = 14; flush_s: int = 60`
  - `Settings.home_assistant: HomeAssistantSettings | None = None` (None when `[home_assistant]` is absent or `url` empty)
  - `Settings.bluetooth: BluetoothSettings = field(default_factory=BluetoothSettings)`
  - Env override `LANSCAPE_HA_TOKEN` (same precedence as `LANSCAPE_ROUTER_PASSWORD`).

- [ ] **Step 1: Write failing tests:** `test_home_assistant_absent_is_none`;
  `test_home_assistant_loaded` (url `http://192.0.2.10:8123`, token, verify_tls false);
  `test_ha_token_env_overrides_file`; `test_ha_url_must_be_http_or_https` (`ftp://…` → ValueError);
  `test_ha_url_without_token_is_error` (ValueError naming `home_assistant.token`);
  `test_bluetooth_defaults` (14, 60); `test_bluetooth_retention_must_be_positive`;
  `test_settings_repr_hides_ha_token`; in `test_scheduler.py`
  `test_runner_redacts_ha_token` (an error containing the token is stored redacted).
- [ ] **Step 2: Run** `pytest tests/test_config.py tests/test_scheduler.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** parsing with the existing `_get`/`_table` helpers; strip a trailing
  `/` from `url`. Add both tables to `config.example.toml` exactly as in spec §8, commented as
  optional.
- [ ] **Step 4: Run** the full gate. Expected: clean.
- [ ] **Step 5: Commit** `feat(config): [home_assistant] and [bluetooth] settings`.

### Task 3: Advert parsing and identity matching (pure)

**Files:**
- Create: `src/lanscape_atlas/bluetooth/__init__.py`, `src/lanscape_atlas/bluetooth/match.py`
- Test: `tests/test_ble_match.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Advert: address: str; rssi: int; source: str; name: str | None; manufacturer_data: dict[int, bytes]`
  - `@dataclass(frozen=True) class Identity: kind: Literal["address", "ibeacon"]; value: str` with `def key(self) -> str` returning `f"{kind}:{value}"` and `@classmethod def parse(cls, key: str) -> Identity` (ValueError on anything else).
  - `def parse_advert(raw: Mapping[str, Any]) -> Advert | None` — None for malformed input.
  - `def normalize_address(addr: str) -> str` — lower-case colon form; ValueError if not 6 octets.
  - `def parse_ibeacon(manufacturer_data: Mapping[int, bytes]) -> tuple[str, int, int] | None` — company id 76, prefix `02 15`, then UUID (16 bytes, lower-case canonical form), major, minor (big-endian u16).
  - `def identity_of(adv: Advert) -> Identity` — `ibeacon` value `f"{uuid}/{major}/{minor}"` if present, else `address`.
  - `def is_static_random(addr: str) -> bool` — top two bits of the first octet are `11`.

- [ ] **Step 1: Write failing tests:** `test_parse_ibeacon_valid` (fixture bytes for UUID
  `00000000-0000-4000-8000-000000000001`, major 100, minor 1 → that tuple);
  `test_parse_ibeacon_short`, `test_parse_ibeacon_wrong_prefix`, `test_parse_ibeacon_other_company`
  (all None); `test_identity_prefers_ibeacon`; `test_identity_falls_back_to_address`;
  `test_identity_key_roundtrip`; `test_identity_parse_rejects_garbage`;
  `test_static_random_built_at_runtime` (build the address as
  `f"{0x00 | 0xC0:02x}" + ":00:5e:00:53:01"`, assert True; `00:00:5e:00:53:01` False);
  `test_parse_advert_rejects_malformed` (parametrized: missing `address`, `rssi` None, odd-length
  hex, non-dict input → None); `test_parse_advert_hex_keys` (HA sends company ids as strings and
  data as hex).
- [ ] **Step 2: Run** `pytest tests/test_ble_match.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** in `match.py`. No I/O, no DB.
- [ ] **Step 4: Run** the test file, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(bluetooth): advert parsing and identity matching`.

### Task 4: Aggregator and candidate list (pure, in memory)

**Files:**
- Create: `src/lanscape_atlas/bluetooth/aggregator.py`
- Test: `tests/test_ble_aggregator.py`

**Interfaces:**
- Consumes: `Advert`, `Identity`, `identity_of`, `is_static_random` (Task 3); `Clock` (`lanscape_atlas.clock`).
- Produces:
  - `@dataclass(frozen=True) class MinuteRow: device_id: int; source: str; minute: str; max_rssi: int; adverts: int; last_at: datetime`
  - `@dataclass(frozen=True) class Candidate: identity: Identity; name: str | None; best_rssi: int; source: str; first_heard: datetime; last_heard: datetime; adverts: int; typical_gap_s: float | None`
  - `class BleAggregator(clock: Clock, *, known_vendor: Callable[[str], bool], candidate_cap: int = 500, tracker_cap: int = 5000)` with:
    - `set_index(index: Mapping[Identity, int]) -> None` (identity → device_id; replaces the whole map)
    - `add(adv: Advert) -> None` (claimed → minute bucket; unclaimed → tracker/candidates)
    - `drain_closed() -> list[MinuteRow]` (minutes strictly before the current minute; removes them)
    - `drain_all() -> list[MinuteRow]` (used on shutdown)
    - `candidates() -> list[Candidate]` (newest first; drops entries older than 24 h)
  - Constants: `CANDIDATE_TTL = timedelta(hours=24)`, `STABLE_SPAN = timedelta(minutes=30)`.
  - A source becomes a candidate if its identity is `ibeacon`, or `is_static_random`, or
    `known_vendor(address)`, or it has been heard over at least `STABLE_SPAN`. Tracker entries not
    yet candidates expire 30 minutes after last heard; both maps evict least-recently-heard at cap.

- [ ] **Step 1: Write failing tests** (all driven by the `FixedClock` fixture):
  `test_minute_bucket_max_rssi_and_count`; `test_drain_closed_keeps_current_minute`;
  `test_two_sources_same_minute_two_rows`; `test_unclaimed_not_in_rows`;
  `test_ibeacon_is_candidate_immediately`; `test_known_vendor_is_candidate`;
  `test_rotating_address_not_candidate_before_30_min`;
  `test_address_becomes_candidate_after_30_min_span`; `test_candidate_expires_after_24h`;
  `test_candidate_cap_evicts_oldest` (cap 3); `test_tracker_cap_evicts_oldest` (cap 3);
  `test_set_index_claims_future_adverts` (an identity claimed mid-minute counts from then on and
  leaves the candidate list); `test_typical_gap_is_median_interval`.
- [ ] **Step 2: Run** `pytest tests/test_ble_aggregator.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use `OrderedDict` for LRU eviction; `typical_gap_s` is the median
  of the last 20 inter-advert intervals (None with fewer than two adverts).
- [ ] **Step 4: Run** the test file, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(bluetooth): per-minute aggregator and candidate list`.

### Task 5: Service writes, undo and prune

**Files:**
- Modify: `src/lanscape_atlas/service/service.py`, `src/lanscape_atlas/scheduler.py` (`_prune_job`)
- Test: `tests/test_ble_service.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `MinuteRow`, `Identity` (Tasks 3–4).
- Produces (all on `Service`):
  - `record_ble_minutes(rows: Sequence[MinuteRow]) -> int` — upserts sightings (`max_rssi = MAX`, `adverts` summed, `last_at = MAX`), resolves `source → ble_scanner.id` (creating unknown scanners with `name` NULL), snapshots `place_id` from the scanner's linked device. Not change-logged. Returns rows written. Owns one transaction.
  - `upsert_scanners(scanners: Mapping[str, str | None], seen_at: datetime) -> None` — source → name; updates `name` and `last_seen`. Not change-logged.
  - `claim_ble(actor: str, identity: Identity, *, device_id: int | None = None, new_name: str | None = None, new_type: str | None = None) -> tuple[int, int]` — exactly one of `device_id` / `new_name`; returns `(device_id, batch_id)`. New device: same logging as `create_placeholder_device`, in the same batch. Identity row logged as a row-level `field='*'` creation on entity `ble_identity`.
  - `unclaim_ble(actor: str, identity_id: int) -> int` — row-level deletion change; returns batch id.
  - `link_scanner(actor: str, scanner_id: int, device_id: int | None) -> int` — plain field change on entity `ble_scanner`, field `device_id`; returns batch id (0 if unchanged).
  - `prune_ble(older_than_days: int) -> int` — deletes `ble_sighting` rows with `minute` older than the cutoff.
  - Map updates: `_ENTITY_COLUMNS["ble_identity"] = frozenset({"device_id", "kind", "value", "created_at"})`, `_ENTITY_COLUMNS["ble_scanner"] = frozenset({"device_id"})`, `_ROW_COLUMNS["ble_identity"] = ("id", "device_id", "kind", "value", "created_at")`.
  - `_delete_entity("device", …)` deletes the device's `ble_sighting` rows first (collector data, like `topology_layout`). A device with a remaining `ble_identity` row still refuses (ValueError), as for interfaces.
  - `_prune_job(service, days, ble_days)` also calls `prune_ble(ble_days)`; `start_scheduler` passes `settings.bluetooth.retention_days`.

- [ ] **Step 1: Write failing tests** in `tests/test_ble_service.py` (use `svc`, `clock`,
  `make_device`): `test_record_minutes_upserts_max_and_sum`;
  `test_record_minutes_snapshots_scanner_room` (link scanner to a device in place A, record, move
  the device to place B, record a later minute; first row keeps A);
  `test_record_minutes_creates_unknown_scanner`; `test_upsert_scanners_updates_name`;
  `test_claim_onto_existing_device`; `test_claim_as_new_device`;
  `test_claim_requires_exactly_one_target` (ValueError); `test_claim_unknown_device` (KeyError);
  `test_claim_duplicate_identity` (ValueError naming the identity, nothing written);
  `test_unclaim_then_undo_restores_same_id`; `test_undo_claim_onto_existing`;
  `test_undo_claim_new_device_with_sightings` (device, identity and sightings gone);
  `test_redo_claim_new_device`; `test_link_scanner_and_undo`; `test_link_scanner_unchanged_returns_0`;
  `test_prune_ble_respects_cutoff`; `test_bluetooth_never_touches_placement` (after record and claim,
  the device's `place_id`, `pos_x`, `pos_y`, `roaming`, `online` are unchanged). In
  `tests/test_scheduler.py`: `test_prune_job_prunes_ble`.
- [ ] **Step 2: Run** `pytest tests/test_ble_service.py tests/test_scheduler.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Publish `change.applied` after each logged write as the other
  public writes do. Undo of `ble_identity` creation/removal goes through the existing row-level
  `field='*'` path; confirm `_undo_conflicts` sees later changes to the same identity row.
- [ ] **Step 4: Run** the test file, `tests/test_service_undo.py`, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(service): claim, unclaim, scanner links and BLE sightings`.

### Task 6: Read queries

**Files:**
- Create: `src/lanscape_atlas/service/ble_queries.py`
- Test: `tests/test_ble_queries.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class LastHeard: device_id: int; at: datetime; rssi: int; place_id: int | None; place_name: str | None; scanner_name: str | None; scanner_source: str`
  - `@dataclass(frozen=True) class FindRow: device_id: int; name: str | None; type: str | None; last: LastHeard | None; badge: Literal["steady", "intermittent", "not_heard_24h"]`
  - `def last_heard(conn, device_id: int) -> LastHeard | None` — newest `last_at`.
  - `def find(conn, q: str | None, *, now: datetime) -> list[FindRow]` — claimed devices only (those with any `ble_identity`), name filter case-insensitive substring, ordered by most recently heard.
  - `def badge(conn, device_id: int, *, now: datetime) -> str` — `not_heard_24h` if no sighting in the last 24 h; else count distinct minutes with a sighting in the 60 minutes ending at the newest `last_at`: ≥ 48 → `steady`, otherwise `intermittent`.
  - `def hourly_strip(conn, device_id: int, *, now: datetime) -> list[str | None]` — 24 entries, oldest first; each is the place name (or `"near <scanner name>"`) with the most adverts in that hour, None if not heard.
  - `def identities(conn, device_id: int) -> list[dict[str, Any]]` — id, kind, value, created_at.
  - `@dataclass(frozen=True) class ScannerView: id: int; source: str; name: str | None; device_id: int | None; device_name: str | None; place_name: str | None; last_seen: datetime; suggestions: list[int]`
  - `def scanners(conn) -> list[ScannerView]` — suggestions: devices whose name equals the scanner name (case-insensitive), and devices with an interface whose MAC + 2 equals `source` (48-bit addition, wraps), excluding the already-linked device.

- [ ] **Step 1: Write failing tests:** `test_last_heard_newest_wins_over_stronger`;
  `test_last_heard_none_when_never_heard`; `test_find_only_claimed`; `test_find_filters_by_name`;
  `test_badge_steady_at_48_minutes`; `test_badge_intermittent_at_47_minutes`;
  `test_badge_not_heard_after_24h`; `test_hourly_strip_picks_most_adverts`;
  `test_hourly_strip_unlinked_scanner_says_near`; `test_scanner_suggestion_by_name`;
  `test_scanner_suggestion_by_mac_plus_two` (Wi-Fi `00:00:5e:00:53:10`, source `00:00:5e:00:53:12`);
  `test_scanner_suggestion_mac_wraps` (`…:53:ff` + 2 → `…:54:01`).
- [ ] **Step 2: Run** `pytest tests/test_ble_queries.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** in SQL where it is one query (last heard, find); Python for badge and strip.
- [ ] **Step 4: Run** the test file, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(service): Bluetooth read queries`.

### Task 7: Home Assistant WebSocket client

**Files:**
- Create: `src/lanscape_atlas/collectors/bluetooth_ha.py`
- Test: `tests/test_collector_bluetooth_ha.py`

**Interfaces:**
- Consumes: `HomeAssistantSettings` (Task 2), `parse_advert`, `Advert` (Task 3).
- Produces:
  - `@dataclass class ClientStatus: connected: bool; since: datetime | None; last_error: str | None`
  - `class HaBluetoothClient(settings: HomeAssistantSettings, on_advert: Callable[[Advert], None], on_scanners: Callable[[dict[str, str | None]], None], clock: Clock, *, backoff_s: tuple[float, ...] = (1, 2, 5, 15, 60))` with `name = "bluetooth"`, `async start() -> None` (spawns the connection task; returns immediately), `async stop() -> None` (cancels and closes), `status() -> ClientStatus`.
  - Protocol: `GET {url}/api/websocket` (ws/wss from http/https); expect `auth_required`; send `{"type": "auth", "access_token": token}`; on `auth_invalid` record the error and back off; on `auth_ok` send id 1 `bluetooth/subscribe_scanner_details` and id 2 `bluetooth/subscribe_advertisements`. Events for id 1: `add`/`remove` lists of scanner details (use `source` and `name`). Events for id 2: `add` lists → `on_advert` per parsed advert; `remove` ignored. A `result` with `success: false` is an error (reconnect with backoff).
  - The token never appears in `last_error` or logs.

- [ ] **Step 1: Write failing tests** with an `aiohttp.test_utils.TestServer` fake HA:
  `test_auth_and_subscriptions` (server asserts the auth message and both subscribe types);
  `test_adverts_delivered` (two `add` adverts → two `on_advert` calls with parsed values);
  `test_scanner_details_delivered`; `test_bad_event_is_skipped` (a malformed advert between two
  good ones; both good ones arrive); `test_auth_invalid_sets_error_and_retries`;
  `test_reconnects_after_server_drop` (server closes after one advert; a second connection
  delivers the next; `status().connected` goes False then True);
  `test_subscription_failure_is_error`; `test_token_not_in_error` (server sends an error echoing
  the token; `status().last_error` does not contain it); `test_stop_cancels_cleanly`.
- [ ] **Step 2: Run** `pytest tests/test_collector_bluetooth_ha.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with one `aiohttp.ClientSession` per connection attempt;
  `ssl=False` only when `verify_tls` is false; heartbeat 30 s.
- [ ] **Step 4: Run** the test file, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(collectors): Home Assistant Bluetooth WebSocket client`.

### Task 8: Runtime wiring (index, flush, health, lifespan)

**Files:**
- Create: `src/lanscape_atlas/bluetooth/runtime.py`
- Modify: `src/lanscape_atlas/scheduler.py`, `src/lanscape_atlas/web/app.py`
- Test: `tests/test_ble_runtime.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: Tasks 2–7; `EventBus`, `EVENT_CHANGE_APPLIED`; `OuiDB`.
- Produces:
  - `class BluetoothRuntime(service: Service, settings: Settings, oui: OuiDB, clock: Clock, *, client_factory: Callable[..., HaBluetoothClient] = HaBluetoothClient)` with:
    - `aggregator: BleAggregator`
    - `async start() -> None` (loads the identity index, subscribes to `change.applied` to reload it, starts the client), `async stop() -> None` (stops the client, flushes `drain_all()`)
    - `flush() -> int` (drains closed minutes, `service.record_ble_minutes`, then upserts `collector_status` row `bluetooth`: `last_ok` on success when connected, `last_error` from `client.status()` otherwise)
    - `status() -> ClientStatus`
  - `create_app` builds a `BluetoothRuntime` only when `settings.home_assistant` is set and stores it on `app.state.bluetooth` (else `None`). `start_scheduler(..., bluetooth=runtime)` starts it and adds job `ble_flush` (`IntervalTrigger(seconds=settings.bluetooth.flush_s)`); `stop_scheduler(..., bluetooth=runtime)` stops it after the scheduler.
  - `known_vendor(addr)` is `oui.lookup(addr) is not None` (use the existing `OuiDB` lookup name).

- [ ] **Step 1: Write failing tests** (fake client via `client_factory`): `test_index_loaded_at_start`;
  `test_index_reloaded_after_claim` (claim through `svc`, publish happens, next advert counts for
  the device); `test_flush_writes_closed_minutes`; `test_flush_records_ok_status`;
  `test_flush_records_client_error_status`; `test_stop_flushes_open_minutes`;
  `test_no_runtime_without_home_assistant` (`create_app` with no `[home_assistant]` →
  `app.state.bluetooth is None`); in `test_scheduler.py` `test_ble_flush_job_registered`.
- [ ] **Step 2: Run** `pytest tests/test_ble_runtime.py tests/test_scheduler.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** The flush runs `record_ble_minutes` in a thread
  (`asyncio.to_thread`) so a slow write does not stall the WebSocket reader.
- [ ] **Step 4: Run** the test files, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(bluetooth): runtime, flush job and app wiring`.

### Task 9: HTTP API and MCP tools

**Files:**
- Create: `src/lanscape_atlas/web/ble_api.py`
- Modify: `src/lanscape_atlas/web/app.py` (include router), `src/lanscape_atlas/mcp_server.py`
- Test: `tests/test_ble_api.py`, `tests/test_mcp.py`

**Interfaces:**
- Consumes: Tasks 5, 6, 8 (`app.state.bluetooth` may be None → candidates `[]`).
- Produces (bearer-protected, prefix `/api/v1`):
  - `GET /find?q=` → list of `FindRow` as JSON (`last` nested; datetimes ISO).
  - `GET /ble/candidates` → `{"connected": bool | null, "candidates": [...]}` (`identity` as `Identity.key()`).
  - `POST /ble/claim` body `{"identity": str, "device_id": int}` or `{"identity": str, "new": {"name": str, "type": str}}` (Pydantic, `extra="forbid"`, `StrictInt`) → `{"device_id", "batch_id"}`. The identity must be a current candidate, else 422 `"no such candidate"`.
  - `DELETE /ble/identities/{id}` → `{"batch_id"}`.
  - `GET /ble/scanners` → list of `ScannerView`; `PUT /ble/scanners/{id}/device` body `{"device_id": int | null}` → `{"batch_id"}`.
  - MCP tools (via `tool(title=…, annotations=…)`): `where_is(query: str) -> list[dict]` (read-only: `find` rows plus `age_s`), `ble_candidates() -> dict` (read-only), `claim_ble(candidate: str, device_id: int | None = None, new_name: str | None = None, new_type: str = "other") -> dict` (actor `claude`). Add all three to `TOOL_NAMES`; extend `INSTRUCTIONS` with one sentence naming them and saying rotating-address devices are not findable.

- [ ] **Step 1: Write failing tests:** API — `test_find_returns_claimed`, `test_candidates_empty_without_ha`,
  `test_claim_existing_device`, `test_claim_new_device`, `test_claim_unknown_candidate_422`,
  `test_claim_both_targets_422`, `test_unclaim_404_for_missing`, `test_link_scanner`,
  `test_ble_routes_require_token`. MCP — `test_where_is`, `test_ble_candidates`,
  `test_claim_ble_actor_is_claude`, `test_tool_names_include_ble`.
- [ ] **Step 2: Run** `pytest tests/test_ble_api.py tests/test_mcp.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Reads take `service.lock` (Phase 1 Ruling 30).
- [ ] **Step 4: Run** the test files, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(api,mcp): find, candidates, claim and scanner links`.

### Task 10: Find page and device page

**Files:**
- Create: `src/lanscape_atlas/web/find.py`, `src/lanscape_atlas/web/templates/find.html`, `src/lanscape_atlas/web/templates/_find_rows.html`
- Modify: `src/lanscape_atlas/web/templates/base.html` (nav), `src/lanscape_atlas/web/pages.py` + the device template
- Test: `tests/test_find_pages.py`

**Interfaces:**
- Consumes: Tasks 5, 6, 8.
- Produces pages (no bearer token; same-origin CSRF as other page POSTs):
  - `GET /find` (full page) and `GET /partials/find?q=` (rows; `hx-trigger="every 30s"`).
  - `POST /find/claim` form fields `identity`, `device_id` or `new_name` + `new_type`; redirects to `/find` with a flash.
  - Banner when `status().connected` is False and `since` is more than 2 minutes ago, showing redacted `last_error`. No banner when Bluetooth is not configured; the page then says Home Assistant is not configured.
  - Device page: "Last heard near X, N min ago" (or "near <scanner>"), the 24-cell strip, identities each with an Unclaim button (`POST /devices/{id}/ble/{identity_id}/unclaim`).

- [ ] **Step 1: Write failing tests:** `test_find_lists_claimed_with_room_and_age`,
  `test_find_shows_badges`, `test_find_candidates_and_claim_flow`,
  `test_find_claim_requires_csrf`, `test_banner_after_two_minutes_down`,
  `test_no_banner_within_two_minutes`, `test_find_says_not_configured`,
  `test_device_page_last_heard_and_strip`, `test_device_page_unclaim`,
  `test_find_escapes_advert_names` (an advert name `<script>` renders escaped).
- [ ] **Step 2: Run** `pytest tests/test_find_pages.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with the existing Jinja2/HTMX patterns; no new JS library.
- [ ] **Step 4: Run** the test file, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(web): Find page and last-heard on the device page`.

### Task 11: Status page scanners and floor-plan highlight

**Files:**
- Modify: `src/lanscape_atlas/web/pages.py` (status), the status template, `src/lanscape_atlas/service/queries.py` (`floor_plan_view`), `src/lanscape_atlas/web/floorplan.py` + its template/JS
- Test: `tests/test_find_pages.py`, `tests/test_floorplan_queries.py`, `tests/test_floorplan_pages.py`

**Interfaces:**
- Consumes: `scanners`, `last_heard` (Task 6), `link_scanner` (Task 5).
- Produces:
  - Status page table "Bluetooth scanners": name, source, linked device, room, last seen, a Link `<select>` with suggestions first; `POST /status/scanners/{id}/link` (CSRF).
  - `floor_plan_view` entries for every device with a sighting in the last 24 h gain `heard: {"place_id", "at"}` (absent otherwise). The JS highlights `heard.place_id`'s polygon when the device is selected; the room tray shows a "heard" chip. Placement data is unchanged.

- [ ] **Step 1: Write failing tests:** `test_status_lists_scanners_with_suggestions`,
  `test_status_link_scanner`, `test_floor_plan_view_includes_heard`,
  `test_floor_plan_view_heard_absent_after_24h`, `test_floorplan_page_renders_heard_chip`.
- [ ] **Step 2: Run** those files. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the files, then the full gate. Expected: PASS / clean.
- [ ] **Step 5: Commit** `feat(web): scanner links on the status page and heard room on the plan`.

### Task 12: Anonymizer, fake house, ESPHome example, README

**Files:**
- Modify: `scripts/anonymize.py`, `tests/test_anonymize.py`, `tests/fixtures/fake_house.json` (or a new `tests/fixtures/ha_adverts.json`), `tests/test_fake_house.py`, `tests/browser/` (existing pass), `README.md`
- Create: `deploy/esphome/ble-proxy.example.yaml`

**Interfaces:**
- Produces:
  - `anonymize.py` rewrites every Bluetooth address to the `00:00:5e:00:53:xx` range and every iBeacon UUID to `00000000-0000-4000-8000-0000000000xx`, consistently within one capture (the same input maps to the same output), including UUIDs inside `manufacturer_data` hex. Static-random addresses are rewritten to the public range too; only tests build a static-random address, at runtime.
  - `ha_adverts.json`: a synthetic stream (two scanners, one claimed phone iBeacon, one claimed remote, three candidates) the fake-house test replays through `BleAggregator` and `record_ble_minutes`.
  - ESPHome example: classic ESP32 (`board: esp32dev`), `esp32_ble_tracker` with `interval: 320ms`, `window: 120ms`, `active: false`, `bluetooth_proxy:` with `active: false`, `api: encryption: key: !secret api_key`, `wifi: ssid: !secret wifi_ssid`, `password: !secret wifi_password`, `name: ble-proxy-example`.
  - README: "Bluetooth (Find)" section — HA token creation (admin), `[home_assistant]` config, Companion app BLE Transmitter setup (battery optimisation off), linking scanners, what is and is not findable (spec §3 table), the ESPHome example.

- [ ] **Step 1: Write failing tests:** `test_anonymize_rewrites_ble_addresses_and_uuids`
  (including a UUID inside manufacturer hex); `test_fake_house_find` (after replay, the phone and
  remote are last heard in their rooms, three candidates listed); extend the browser pass to load
  `/find` and see both claimed devices; `test_esphome_example_has_only_secrets` (no `ssid:` value
  other than `!secret …`).
- [ ] **Step 2: Run** those tests. Expected: FAIL.
- [ ] **Step 3: Implement** and write the README section.
- [ ] **Step 4: Run** the full gate and `python scripts/check_leaks.py`. Expected: clean.
- [ ] **Step 5: Commit** `feat: Bluetooth fixtures, anonymizer support, ESPHome example and docs`.

---

## Finish

- [ ] Whole-branch review (fresh reviewer, most capable model) against the spec.
- [ ] `superpowers:verification-before-completion`: run the full gate locally.
- [ ] Ask the owner before pushing; then open the PR (body ends with the Claude Code attribution),
  `gh pr checks <n> --watch` until both jobs finish, notify the owner with each job's result.
  Fix and repeat on failure; after 2 failed attempts, notify instead.
- [ ] Log to Workbench (`lanscape-atlas`).
