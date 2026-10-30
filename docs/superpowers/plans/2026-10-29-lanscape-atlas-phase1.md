# lanscape-atlas Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-hosted service that discovers devices on a home LAN, keeps a durable MAC-keyed inventory with provenance and undo, and exposes it through a web UI, a JSON API and an MCP server.

**Architecture:** Collectors return immutable `Observation`s; a single `Ingestor` turns them into devices/interfaces/leases/services; every user or Claude write goes through `Service`, which records provenance and an undoable change log. FastAPI serves HTMX pages, `/api/v1` and `/mcp` from one process; APScheduler runs collectors.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`, WAL), FastAPI + Jinja2 + HTMX, Cytoscape.js, APScheduler 3.x, `asusrouter`, `zeroconf`, `arp-scan` and `nmap` binaries, official `mcp` Python SDK (FastMCP), pytest + pytest-asyncio, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-29-lanscape-atlas-phase1-design.md`

## Global Constraints

- Python `>=3.12`; package `lanscape_atlas` under `src/`; console script `lanscape-atlas`.
- License MIT. No personal network data in the repo, ever (spec §9).
- Fixtures/examples use only IPv4 `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` and MACs `00:00:5e:00:53:00`–`00:00:5e:00:53:ff` (and `02:00:5e:00:53:xx` for synthetic random MACs).
- IPv4 only. Timestamps stored as UTC ISO-8601 strings (`YYYY-MM-DDTHH:MM:SSZ`).
- Data dir: `$LANSCAPE_DATA_DIR`, default `/var/lib/lanscape-atlas`; DB file `atlas.db`; config `config.toml`.
- Secrets may come from env: `LANSCAPE_ROUTER_PASSWORD`, `LANSCAPE_API_TOKEN`.
- Actors: `"user"`, `"claude"`, `"system"`, `"collector:<name>"`. Precedence for user-facing fields: user > claude > collector.
- Defaults: `observation_retention_days=30`, `ip_match_window_min=10`, grace 10 min, grace 30 min for `type ∈ {phone, tablet, laptop}`, collector backoff cap 3600 s.
- Collector schedules: asusrouter 2 min, arp 5 min, mdns flush 1 min, ssdp 10 min, nmap_light nightly 03:17 local, nmap_deep on demand.
- Collectors never touch the DB. Only `Ingestor` writes discovered data; only `Service` writes user/Claude data.
- Every task: `ruff check`, `mypy src` (strict on `store`, `service`, `ingest`), `pytest`, `python scripts/check_leaks.py` pass before commit.
- Commit messages end with the attribution trailer configured in the session.

## Review Focus

1. **MAC formats in the wild** — upper case, `-` separators, no separators, Cisco `aabb.ccdd.eeff`, and garbage (`"<incomplete>"`). Expected: all valid forms normalize to `aa:bb:cc:dd:ee:ff`; invalid ones drop that observation with a debug log, never crash a collector run. (Task 2, Task 7)
2. **Junk hostnames from the router** — `""`, `"*"`, `"unknown"`, a bare MAC, or the IP itself. Expected: never used as a device name; next-best candidate wins. (Task 7)
3. **Two sources disagree about an IP in one run** — ARP says .50 is MAC A, router says .50 is MAC B. Expected: no crash; the observation with the later `seen_at` owns the open lease, the other lease is closed. (Task 8)
4. **A collector hangs or the router rejects credentials** — Expected: timeout/exception recorded in `collector_status`, backoff applied, other collectors keep running, UI status chip goes red. (Task 13)
5. **User edits a device while ingest is running** — Expected: no `database is locked` error surfaced to the user (WAL + `busy_timeout=5000`), and the user's name is not overwritten by the in-flight collector fill. (Task 5, Task 7)

---

## File Structure

```
pyproject.toml                      # deps, ruff, mypy, pytest config, console script
LICENSE  README.md  config.example.toml  .gitignore  .pre-commit-config.yaml
.github/workflows/ci.yml
deploy/openrc/lanscape-atlas        # OpenRC init script
scripts/check_leaks.py              # repo leak checker (spec §9)
scripts/anonymize.py                # real capture → fixture
src/lanscape_atlas/
  __init__.py
  config.py                         # Settings dataclass, load_settings()
  clock.py                          # Clock type, utcnow(), iso()
  netutil.py                        # normalize_mac, is_random_mac, valid_ipv4
  oui.py                            # OuiDB (bundled IEEE file)
  observation.py                    # Observation dataclass
  events.py                         # EventBus
  store/
    __init__.py
    db.py                           # open_db(), migrate()
    schema.sql                      # all tables (spec §5)
  service/
    __init__.py                     # re-exports Service
    service.py                      # writes, provenance, change log, undo
    queries.py                      # read models (DeviceSummary, DeviceView, ...)
  ingest/
    __init__.py
    ingestor.py                     # Ingestor.ingest(), refresh_presence()
    naming.py                       # name candidate selection
  collectors/
    __init__.py                     # registry: build_collectors(settings)
    base.py                         # Collector protocol
    asusrouter.py  arp.py  mdns.py  ssdp.py  nmap.py
  scheduler.py                      # CollectorRunner, backoff, APScheduler wiring
  web/
    app.py                          # create_app()
    api.py                          # /api/v1 routes
    pages.py                        # HTMX page routes
    templates/*.html
    static/  (htmx.min.js, cytoscape.min.js, app.css, topology.js)
  mcp_server.py                     # build_mcp()
  cli.py                            # `lanscape-atlas serve|migrate|ingest-file`
tests/
  conftest.py                       # tmp db, fixed clock, service/ingestor fixtures
  fixtures/  (fake_house.json, arp_scan.txt, nmap_light.xml, nmap_deep.xml,
              ssdp_responses.txt, asus_clients.json, mdns_records.json)
  test_*.py
```

---

### Task 1: Repository scaffold and leak checker

**Files:**
- Create: `pyproject.toml`, `LICENSE` (MIT), `.gitignore`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `src/lanscape_atlas/__init__.py`, `scripts/check_leaks.py`
- Test: `tests/test_check_leaks.py`

**Interfaces:**
- Produces: `scripts/check_leaks.py` CLI — `python scripts/check_leaks.py [paths...]` exits 0 clean / 1 with `path:line: reason` lines. Importable function `find_leaks(text: str, path: str, denylist: set[str]) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
from check_leaks import find_leaks  # tests add scripts/ to sys.path in conftest

def test_doc_range_mac_allowed():
    assert find_leaks("mac 00:00:5e:00:53:1a 02:00:5e:00:53:1b", "x.py", set()) == []

def test_real_mac_flagged():
    assert find_leaks("mac a4:83:e7:" + "12:34:56", "x.py", set())

def test_mac_with_dashes_flagged():
    assert find_leaks("A4-83-E7-" + "12-34-56", "x.py", set())

def test_rfc1918_ip_flagged():
    for ip in ("10.1.2.3", "172.20.0.5", "192.168.50.1"):
        assert find_leaks(f"ip {ip}", "x.py", set())

def test_doc_range_ip_allowed():
    assert find_leaks("192.0.2.10 198.51.100.7 203.0.113.9", "x.py", set()) == []

def test_allowlisted_file_may_mention_rfc1918():
    assert find_leaks("192.168.0.0/16", "docs/superpowers/specs/x.md", set()) == []

def test_denylist_term_case_insensitive():
    assert find_leaks("host: Garage-Cam", "x.py", {"garage-cam"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_check_leaks.py -v`
Expected: FAIL — `ModuleNotFoundError: check_leaks`

- [ ] **Step 3: Implement**

`pyproject.toml`: hatchling build, deps `fastapi uvicorn[standard] jinja2 python-multipart apscheduler>=3.10,<4 asusrouter zeroconf mcp aiohttp`, dev extras `pytest pytest-asyncio httpx ruff mypy pre-commit`; `[project.scripts] lanscape-atlas = "lanscape_atlas.cli:main"`; pytest `asyncio_mode = "auto"`, `pythonpath = ["src", "scripts"]`.
`.gitignore`: Python defaults plus `data/`, `*.db`, `*.db-*`, `config.toml`, `.env`.
`check_leaks.py`: MAC regex accepting `:`/`-` separators (case-insensitive) and allowing only prefixes `00:00:5e:00:53` and `02:00:5e:00:53` (synthetic random MACs, Task 14); IPv4 regex with `ipaddress.ip_address(...).is_private` minus the three RFC 5737 nets and `127.0.0.0/8`; allow-list of path globs `docs/superpowers/**`, `scripts/check_leaks.py`, `tests/test_check_leaks.py`; denylist from `~/.config/lanscape-atlas/denylist.txt` (one term per line, `#` comments) if present. With no args, scan `git ls-files` output; skip binary files.
`.pre-commit-config.yaml`: local hook running the script on staged files. CI workflow: Python 3.12, `pip install -e .[dev]`, ruff, mypy, pytest, leak check.

- [ ] **Step 4: Run tests and the checker on the repo**

Run: `pytest tests/test_check_leaks.py -v && python scripts/check_leaks.py`
Expected: all PASS; checker exits 0.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: scaffold project, CI and leak checker"
```

---

### Task 2: Network utilities and OUI lookup

**Files:**
- Create: `src/lanscape_atlas/netutil.py`, `src/lanscape_atlas/oui.py`, `src/lanscape_atlas/clock.py`, `src/lanscape_atlas/data/oui_sample.csv`
- Test: `tests/test_netutil.py`, `tests/test_oui.py`

**Interfaces:**
- Produces:
  - `normalize_mac(raw: str) -> str` — returns `aa:bb:cc:dd:ee:ff`; raises `ValueError` on invalid input.
  - `is_random_mac(mac: str) -> bool` — locally-administered bit (`int(first_octet,16) & 0x02`).
  - `valid_ipv4(raw: str) -> str | None` — normalized dotted quad, `None` for invalid or non-IPv4.
  - `class OuiDB: @classmethod load(path: Path) -> OuiDB; lookup(mac: str) -> str | None` — matches 24-bit prefix; random MACs return `None`.
  - `Clock = Callable[[], datetime]`; `utcnow() -> datetime` (tz-aware UTC); `iso(dt: datetime) -> str`; `parse_iso(s: str) -> datetime`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from lanscape_atlas.netutil import normalize_mac, is_random_mac, valid_ipv4

@pytest.mark.parametrize("raw", [
    "00:00:5E:00:53:1A", "00-00-5e-00-53-1a", "00005e00531a", "0000.5e00.531a"])
def test_normalize_mac_forms(raw):
    assert normalize_mac(raw) == "00:00:5e:00:53:1a"

@pytest.mark.parametrize("raw", ["<incomplete>", "", "00:00:5e:00:53", "zz:00:5e:00:53:1a"])
def test_normalize_mac_rejects(raw):
    with pytest.raises(ValueError):
        normalize_mac(raw)

def test_is_random_mac():
    assert is_random_mac("02:00:5e:00:53:01") is True
    assert is_random_mac("00:00:5e:00:53:01") is False

def test_valid_ipv4():
    assert valid_ipv4("192.0.2.10") == "192.0.2.10"
    assert valid_ipv4("192.0.2.010") is None   # leading zeros rejected (stdlib ipaddress)
    assert valid_ipv4("2001:db8::1") is None
    assert valid_ipv4("not-an-ip") is None
```

```python
def test_oui_lookup(tmp_path):
    p = tmp_path / "oui.csv"
    p.write_text("Registry,Assignment,Organization Name,Organization Address\nMA-L,00005E,ICANN IANA,x\n")
    db = OuiDB.load(p)
    assert db.lookup("00:00:5e:00:53:01") == "ICANN IANA"
    assert db.lookup("02:00:5e:00:53:01") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_netutil.py tests/test_oui.py -v` — Expected: FAIL (import errors)

- [ ] **Step 3: Implement** the signatures above. `OuiDB.load` parses the IEEE `oui.csv` format (column 2 = hex prefix, column 3 = org). Ship `oui_sample.csv` (a few documentation-safe rows) in the package; the real file is downloaded at deploy time to `$LANSCAPE_DATA_DIR/oui.csv` (Task 19).

- [ ] **Step 4: Run tests** — Expected: PASS

- [ ] **Step 5: Commit** — `git commit -m "feat: MAC/IPv4 normalization, OUI lookup, clock helpers"`

---

### Task 3: Store schema and migrations

**Files:**
- Create: `src/lanscape_atlas/store/__init__.py`, `store/db.py`, `store/schema.sql`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `open_db(path: Path | str) -> sqlite3.Connection` — `row_factory=sqlite3.Row`, `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`. `":memory:"` allowed for tests.
  - `migrate(conn) -> int` — applies `schema.sql` once, tracked via `PRAGMA user_version`; returns the resulting version (1).
  - Tables exactly as spec §5 plus `collector_status(name PK, last_ok, last_error, last_error_at, consecutive_failures INT DEFAULT 0, next_run_at)`. Key constraints: `interface UNIQUE(kind, address)`; `service UNIQUE(device_id, port, proto)`; `field_source PRIMARY KEY(entity, entity_id, field)`; `place UNIQUE(name)`; `ip_lease` index on `(ipv4, last_seen)`; `change` index on `(batch_id)` and `(entity, entity_id, field)`; `device.trust CHECK IN ('known','new','unwanted')`; `device.scan_policy CHECK IN ('normal','never')`; `interface.kind CHECK IN ('ethernet','wifi','bluetooth','zwave','unknown')`; `ip_lease.closed INT DEFAULT 0`; `observation(id, source, seen_at, payload JSON, interface_id NULL, attached INT DEFAULT 0)`; `change.batch_id` from a `batch(id, actor, at, undoes_batch_id NULL)` table; `change` also has `old_actor`, `old_set_at` (Task 6); `device.online INT DEFAULT 0` (Task 8); `interface.hints TEXT DEFAULT '{}'` (JSON, Task 7).

- [ ] **Step 1: Write the failing tests**

```python
def test_migrate_creates_tables(tmp_path):
    conn = open_db(tmp_path / "a.db")
    assert migrate(conn) == 1
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"device","interface","ip_lease","service","place","link","observation",
            "field_source","change","batch","topology_layout","collector_status"} <= names

def test_migrate_idempotent(tmp_path):
    conn = open_db(tmp_path / "a.db"); migrate(conn)
    assert migrate(conn) == 1

def test_wal_and_fk(tmp_path):
    conn = open_db(tmp_path / "a.db")
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1

def test_interface_unique(tmp_path):
    conn = open_db(tmp_path / "a.db"); migrate(conn)
    conn.execute("insert into device(name,trust,scan_policy,first_seen,last_seen) values('d','new','normal','t','t')")
    conn.execute("insert into interface(device_id,kind,address,is_random,first_seen,last_seen) values(1,'wifi','00:00:5e:00:53:01',0,'t','t')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into interface(device_id,kind,address,is_random,first_seen,last_seen) values(1,'wifi','00:00:5e:00:53:01',0,'t','t')")
```

- [ ] **Step 2: Run** `pytest tests/test_store.py -v` — Expected: FAIL
- [ ] **Step 3: Implement** `schema.sql` and `db.py` per the Interfaces block. Add `tests/conftest.py` fixtures: `db` (migrated tmp DB), `clock` (mutable fixed clock starting `2026-01-01T00:00:00Z`, with `.advance(minutes=...)`).
- [ ] **Step 4: Run** — Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: SQLite schema and migrations"`

---

### Task 4: Observation type and event bus

**Files:**
- Create: `src/lanscape_atlas/observation.py`, `src/lanscape_atlas/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Produces:
  - `Observation` frozen dataclass exactly as spec §4.1 (`source, seen_at, iface_kind, mac, ipv4, hostname, attrs`), plus `to_json() -> str` and `Observation.from_json(s) -> Observation`.
  - `Event = dataclass(name: str, payload: dict)`; names: `device.first_seen`, `device.online`, `device.offline`, `change.applied`.
  - `EventBus.subscribe(name: str, handler: Callable[[Event], None]) -> None`; `publish(event: Event) -> None` — synchronous; a handler exception is logged and does not stop other handlers or the publisher. `EventBus.recorded` list only when constructed with `record=True` (tests).

- [ ] **Step 1: Tests** — `test_publish_calls_subscribers_in_order`, `test_handler_exception_isolated` (first handler raises, second still called, publish does not raise), `test_observation_json_roundtrip` (attrs with nested list survive).
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: Observation type and in-process event bus"`

---

### Task 5: Service layer — writes, provenance, change log

**Files:**
- Create: `src/lanscape_atlas/service/__init__.py`, `service/service.py`, `service/queries.py`
- Test: `tests/test_service_writes.py`, `tests/test_service_queries.py`

**Interfaces:**
- Consumes: `open_db`, `migrate`, `EventBus`, `Clock`.
- Produces (`Service(conn, events, clock)`):
  - `USER_FIELDS = {"name","type","icon","place_id","pos_x","pos_y","owner","notes","trust","scan_policy"}`; `PROVENANCE_FIELDS = {"name","type","icon"}` (the fields collectors may also fill).
  - `update_device(actor: str, device_id: int, fields: dict[str, Any]) -> int` — returns `batch_id`; raises `KeyError` for unknown device, `ValueError` for fields outside `USER_FIELDS` or invalid enum values. Writes one `change` row per field whose value actually changes; a call with no effective change creates no batch and returns `0`. Upserts `field_source(entity='device', entity_id, field, actor, set_at)`. Publishes `change.applied {batch_id, actor}`.
  - `set_place(actor, device_id, place_name: str) -> int` — creates `place` if absent (creation logged in the same batch as `entity='place'`, `field='name'`, old `None`).
  - `add_link(actor, a_device_id, b_device_id, kind, detail=None) -> int` / `remove_link(actor, link_id) -> int` — logged as `entity='link'`, `field='*'`, value = JSON of the row.
  - `create_placeholder_device(actor, name, type) -> tuple[int, int]` → `(device_id, batch_id)`; `trust='known'`.
  - `collector_may_set(device_id, field) -> bool` — `True` if no `field_source` row or its actor starts with `collector:`.
  - `record_collector_field(conn, device_id, field, value, collector) -> None` — used by `Ingestor` only, for `PROVENANCE_FIELDS`: writes value, `field_source`, and a `change` row in a `batch` with actor `collector:<name>`; caller supplies/reuses the batch id via `begin_batch(actor) -> int`.
  - `begin_batch(actor: str) -> int`.
  - Queries (`queries.py`): `DeviceSummary(id, name, type, icon, online, ipv4, macs, vendor, place, ap_node, band, service_count, last_seen, trust)`; `DeviceView(summary fields + owner, notes, scan_policy, interfaces: list[InterfaceView], leases: list[LeaseView], services: list[ServiceView], field_sources: dict[str, tuple[str, str]])`; `list_devices(conn, *, online=None, type=None, trust=None, place=None, ap_node=None, q=None, limit=100, offset=0) -> list[DeviceSummary]`; `get_device(conn, device_id) -> DeviceView`; `find_devices(conn, query: str) -> list[DeviceSummary]` (exact IPv4, full or partial MAC, case-insensitive substring on name/hostname/vendor); `list_services(conn, device_id=None, port=None) -> list[ServiceView]`; `recent_changes(conn, limit=50, actor=None) -> list[ChangeView]`; `inbox(conn) -> list[DeviceSummary]` (`trust='new'` plus devices whose only interfaces are random).

- [ ] **Step 1: Write the failing tests**

```python
def test_update_device_logs_change_and_source(svc, make_device):
    d = make_device(name="Espressif .23")
    b = svc.update_device("user", d, {"name": "Garage Sensor"})
    rows = svc.conn.execute("select actor, field, old_value, new_value from change join batch on batch.id=change.batch_id where batch_id=?", (b,)).fetchall()
    assert [tuple(r) for r in rows] == [("user", "name", "Espressif .23", "Garage Sensor")]
    assert svc.conn.execute("select actor from field_source where entity='device' and entity_id=? and field='name'", (d,)).fetchone()[0] == "user"

def test_noop_update_creates_no_batch(svc, make_device):
    d = make_device(name="x")
    assert svc.update_device("user", d, {"name": "x"}) == 0

def test_rejects_unknown_field(svc, make_device):
    with pytest.raises(ValueError):
        svc.update_device("user", make_device(), {"first_seen": "x"})

def test_rejects_bad_enum(svc, make_device):
    with pytest.raises(ValueError):
        svc.update_device("claude", make_device(), {"trust": "maybe"})

def test_collector_may_not_override_user(svc, make_device):
    d = make_device(); svc.update_device("user", d, {"name": "Mine"})
    assert svc.collector_may_set(d, "name") is False

def test_set_place_creates_place_once(svc, make_device):
    d1, d2 = make_device(), make_device()
    svc.set_place("user", d1, "Office"); svc.set_place("user", d2, "Office")
    assert svc.conn.execute("select count(*) from place").fetchone()[0] == 1

def test_concurrent_writer_waits_not_fails(tmp_path, events, clock):
    # Review Focus 5: a second connection holds a write txn for 0.5 s (in a thread);
    # svc.update_device on the first connection blocks, then succeeds — no OperationalError.
```

Query tests: `test_find_by_ipv4`, `test_find_by_partial_mac` (`"53:1a"`), `test_find_by_name_case_insensitive`, `test_inbox_contains_new_and_random_only`.

- [ ] **Step 2: Run** `pytest tests/test_service_*.py -v` — FAIL
- [ ] **Step 3: Implement.** Each public write runs in one `with conn:` transaction. Store `old_value/new_value` as JSON text so `None` and numbers round-trip. `online` in `DeviceSummary` is the `device.online` column maintained by Task 8.
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: service layer writes, provenance and queries"`

---

### Task 6: Undo with conflict detection

**Files:**
- Modify: `src/lanscape_atlas/service/service.py`
- Test: `tests/test_service_undo.py`

**Interfaces:**
- Produces: `Service.undo(actor: str, batch_id: int) -> int` (new batch id, with `batch.undoes_batch_id=batch_id`); raises `UndoConflict(conflicting: list[int])` if any later, non-undone batch changed any `(entity, entity_id, field)` touched by `batch_id`; raises `KeyError` for unknown batch; raises `ValueError("already undone")` if a batch already undoes it. Restores `field_source` rows to their state before the batch (delete the row if none existed). Undo of `link` and `place` creations deletes them; undo of a removal re-inserts the saved row with its original id.
- `class UndoConflict(Exception): conflicting: list[int]` exported from `lanscape_atlas.service`.

- [ ] **Step 1: Tests** — `test_undo_restores_value_and_source`, `test_undo_conflict_when_later_batch_touched_field` (assert `exc.conflicting == [b2]`), `test_undo_ok_when_later_batch_touched_other_field`, `test_undo_twice_rejected`, `test_undo_link_add_removes_link`, `test_undo_link_remove_restores_same_id`, `test_undo_the_undo_reapplies`.
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement.** Restore `field_source` from `change.old_actor`/`old_set_at`; Task 5 must populate those columns on every field write.
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: batch undo with conflict detection"`

---

### Task 7: Ingest — correlation, creation, discovered facts, naming

**Files:**
- Create: `src/lanscape_atlas/ingest/__init__.py`, `ingest/ingestor.py`, `ingest/naming.py`
- Test: `tests/test_ingest_basic.py`, `tests/test_naming.py`

**Interfaces:**
- Consumes: `Service.begin_batch`, `Service.collector_may_set`, `Service.record_collector_field`, `OuiDB`, `EventBus`, `normalize_mac`, `is_random_mac`, `valid_ipv4`.
- Produces:
  - `Ingestor(conn, service, events, oui, clock, *, ip_match_window=timedelta(minutes=10))`
  - `Ingestor.ingest(obs: list[Observation]) -> IngestResult` where `IngestResult(created: int, updated: int, unattached: int, dropped: int)`.
  - `naming.is_junk_hostname(name: str | None, *, mac: str | None, ipv4: str | None) -> bool` — junk: `None`, empty/whitespace, `*`, `unknown`, `localhost`, equals the IPv4, equals the MAC in any separator form, or matches `^[0-9a-f]{12}$`.
  - `naming.best_name(candidates: dict[str, str | None], vendor: str | None, ipv4: str | None) -> str | None` — keys `mdns`, `dhcp`, `ssdp`, `rdns`; priority `mdns > dhcp > ssdp > rdns`, skipping junk; fallback `f"{vendor} .{last_octet}"` when vendor and ipv4 exist, else `None`. mDNS instance names have the service suffix and `.local` stripped.
- Behavior (spec §6 steps 1–5): persist each observation; observations with an invalid MAC are counted in `dropped` and logged at DEBUG; MAC observations create or update `interface` (+ `device(trust='new')` on first sight, emitting `device.first_seen {device_id, mac, is_random}`); IP-only observations attach to the interface whose open `ip_lease.ipv4` matches with `last_seen` within the window, else stay `attached=0` (counted `unattached`) and are retried on the next `ingest` call; `ap_node`, `band` from `attrs` update the interface; `attrs["services"]` (list of `{port, proto, name, product, version}`) upsert `service`; hostname candidates stored per interface in `interface.hints` (JSON) keyed by hint source; then `name` is filled via `best_name` when `collector_may_set`. Name/type/icon fills from one `ingest()` call share one collector batch per source.

- [ ] **Step 1: Write the failing tests**

```python
def obs(**kw):
    base = dict(source="arp", seen_at=T0, iface_kind=None, mac=None, ipv4=None, hostname=None, attrs={})
    return Observation(**{**base, **kw})

def test_new_mac_creates_device_and_event(ing, bus):
    r = ing.ingest([obs(mac="00:00:5E:00:53:01", ipv4="192.0.2.10")])
    assert r.created == 1
    assert bus.recorded[-1].name == "device.first_seen"
    assert bus.recorded[-1].payload["is_random"] is False

def test_invalid_mac_dropped_not_raised(ing):
    r = ing.ingest([obs(mac="<incomplete>", ipv4="192.0.2.10")])
    assert r.dropped == 1 and r.created == 0

def test_ip_only_attaches_within_window(ing, clock):
    ing.ingest([obs(mac="00:00:5e:00:53:01", ipv4="192.0.2.10")])
    clock.advance(minutes=5)
    r = ing.ingest([obs(source="mdns", ipv4="192.0.2.10", hostname="Living-Room-TV._googlecast._tcp.local")])
    assert r.unattached == 0

def test_ip_only_outside_window_unattached(ing, clock): ...  # advance 11 min → unattached == 1

def test_unattached_retried_next_run(ing, clock): ...  # mdns first, arp second → name filled after second ingest

def test_name_precedence_mdns_over_dhcp(ing, svc): ...  # result name == "Living-Room-TV"

def test_user_name_not_overwritten(ing, svc): ...

def test_reingest_same_batch_is_idempotent(ing, db):
    batch = [obs(source="asusrouter", mac="00:00:5e:00:53:01", ipv4="192.0.2.10", hostname="nas")]
    ing.ingest(batch)
    snap = dump_state(db)          # helper in conftest: all tables except observation
    ing.ingest(batch)
    assert dump_state(db) == snap
```

`test_naming.py`: parametrized junk cases (`""`, `"*"`, `"Unknown"`, `"192.0.2.10"`, `"00-00-5e-00-53-01"`, `"00005e005301"`) and `best_name` priority/fallback cases (`"ICANN IANA .10"`).

- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement.** Idempotency: `last_seen` only moves forward (`max`), leases/services upsert, `record_collector_field` is skipped when the value is unchanged, and observations are deduplicated on `(source, seen_at, payload)` before persisting.
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: ingest correlation, discovery facts and naming"`

---

### Task 8: Ingest — IP reuse, presence, random-MAC claims

**Files:**
- Modify: `src/lanscape_atlas/ingest/ingestor.py`, `src/lanscape_atlas/service/service.py`
- Test: `tests/test_ingest_presence.py`, `tests/test_claim.py`

**Interfaces:**
- Produces:
  - IP reuse in `Ingestor.ingest`: before opening a lease for `(interface, ipv4)`, close (`closed=1`) any other open lease for the same `ipv4`. Within one `ingest` call, observations are processed in `seen_at` order so the latest owner wins.
  - `Ingestor.refresh_presence(now: datetime | None = None) -> None` — sets `device.online` from max interface `last_seen` vs grace (`30 min` for `type ∈ {phone, tablet, laptop}`, else `10 min`); emits `device.online` / `device.offline` on transitions only. Called at the end of every `ingest`.
  - `Service.claim_interface(actor: str, interface_id: int, device_id: int) -> int` — moves the interface (its leases and observations follow via FK) to `device_id`; if the source device has no interfaces left, deletes it (row saved in the change log as JSON so undo can restore it with its original id). Raises `ValueError` if the target is the current owner. One batch; undo (Task 6) restores both.
  - `Service.prune_observations(older_than_days: int) -> int` (rows deleted).

- [ ] **Step 1: Tests** — `test_ip_moves_to_new_mac_closes_old_lease`, `test_same_run_conflict_latest_seen_wins` (Review Focus 3: ARP at T0 says A, router at T0+30 s says B → open lease belongs to B, no exception), `test_goes_offline_after_grace`, `test_phone_grace_is_30_min`, `test_online_event_only_on_transition`, `test_claim_moves_interface_and_deletes_empty_device`, `test_claim_undo_restores_both_devices`, `test_prune_observations`.
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: IP reuse, presence tracking and interface claims"`

---

### Task 9: Collector base and ASUS router collector

**Files:**
- Create: `src/lanscape_atlas/collectors/__init__.py`, `collectors/base.py`, `collectors/asusrouter.py`, `src/lanscape_atlas/config.py`, `config.example.toml`
- Test: `tests/test_config.py`, `tests/test_collector_asusrouter.py`, fixture `tests/fixtures/asus_clients.json`

**Interfaces:**
- Produces:
  - `class Collector(Protocol): name: str; timeout_s: float; async def collect(self) -> list[Observation]`.
  - `Settings` frozen dataclass: `data_dir: Path, bind_host: str ("127.0.0.1"), bind_port: int (8720), scan_interface: str ("eth0"), api_token: str | None, observation_retention_days: int (30), ip_match_window_min: int (10), router: RouterSettings | None, collectors: dict[str, CollectorSettings]`; `RouterSettings(host, username, password, use_ssl: bool = True)`; `CollectorSettings(enabled: bool, interval_s: int | None, cron: str | None, timeout_s: float)`. `load_settings(path: Path | None = None, env: Mapping[str,str] = os.environ) -> Settings` — path defaults to `$LANSCAPE_DATA_DIR/config.toml`; env `LANSCAPE_ROUTER_PASSWORD` / `LANSCAPE_API_TOKEN` override file values; missing file → defaults with router disabled.
  - `asusrouter.client_to_observation(client: Mapping[str, Any], seen_at: datetime) -> Observation | None` — pure mapper from the library's client record (as a dict) to `Observation(source="asusrouter", iface_kind="ethernet"|"wifi", mac, ipv4, hostname, attrs={"ap_node": <AiMesh node MAC, normalized>, "band": "2.4"|"5"|"6"|None, "rssi": int|None})`. Returns `None` for records without a MAC.
  - `AsusRouterCollector(settings: RouterSettings, timeout_s=30)` — opens an `aiohttp.ClientSession`, `AsusRouter(...)`, `async_connect()`, `async_get_data(AsusData.CLIENTS)` and, when available, `AsusData.AIMESH` to resolve node MAC → node name; converts each client via `dataclasses.asdict` (or the library's own dict form) then `client_to_observation`; always disconnects/closes in `finally`.

- [ ] **Step 1: Write the failing tests** — config: `test_defaults_without_file`, `test_env_overrides_password`, `test_example_config_parses` (loads `config.example.toml`). Collector: `test_client_to_observation_wifi` and `test_client_to_observation_wired` against `asus_clients.json` (documentation-range MACs/IPs; hand-built to mirror the installed `asusrouter` version's client fields — inspect `asusrouter`'s client dataclass before writing it), `test_client_without_mac_skipped`.
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement.** Verify field names against the installed `asusrouter` package (`pip show asusrouter`; read its `modules/client.py`) and pin the minimum version in `pyproject.toml` to the one verified.
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: settings and ASUS router collector"`

---

### Task 10: ARP and SSDP collectors

**Files:**
- Create: `collectors/arp.py`, `collectors/ssdp.py`
- Test: `tests/test_collector_arp.py`, `tests/test_collector_ssdp.py`, fixtures `arp_scan.txt`, `ssdp_responses.txt`

**Interfaces:**
- Produces:
  - `arp.parse_arp_scan(text: str, seen_at: datetime) -> list[Observation]` — parses `arp-scan --localnet --plain --quiet` lines `<ip>\t<mac>`; ignores lines that don't split into a valid IPv4 + MAC-shaped token; duplicates (`(DUP: 2)`) yield one observation per distinct MAC.
  - `ArpCollector(interface: str, timeout_s=60)` — runs `arp-scan --interface=<if> --localnet --plain --quiet` via `asyncio.create_subprocess_exec`; non-zero exit raises `CollectorError(stderr)`.
  - `ssdp.parse_ssdp_response(data: bytes, addr: str, seen_at) -> Observation | None` — HTTP-style headers; `attrs={"server": ..., "location": ..., "st": ...}`; `ipv4=addr`, no MAC.
  - `SsdpCollector(timeout_s=5)` — sends M-SEARCH `ssdp:all` to `239.255.255.250:1900`, collects for `timeout_s`, dedupes by `(addr, location)`. Friendly name/model from LOCATION XML is fetched only for unseen locations, with a 2 s per-request timeout; failures ignored. Puts `friendlyName` in `hostname` with `attrs["name_source"]="ssdp"`.
  - `class CollectorError(Exception)` in `collectors/base.py`.

- [ ] **Step 1: Tests** — `test_parse_arp_scan_basic`, `test_parse_arp_scan_skips_junk_lines`, `test_parse_arp_scan_dup_lines`, `test_parse_ssdp_response`, `test_parse_ssdp_garbage_returns_none`.
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: arp-scan and SSDP collectors"`

**Note:** `Ingestor` must treat `attrs["name_source"]` as the key into `hints` when present, else the observation's `source` mapped as `asusrouter→dhcp`, `mdns→mdns`, `ssdp→ssdp`, `rdns→rdns`. Add that mapping in `ingest/naming.py` as `HINT_SOURCE = {...}` and a test in `test_naming.py`.

---

### Task 11: mDNS collector

**Files:**
- Create: `collectors/mdns.py`
- Test: `tests/test_collector_mdns.py`, fixture `mdns_records.json`

**Interfaces:**
- Produces:
  - `MDNS_SERVICE_TYPES: tuple[str, ...]` — curated list (≈30): `_http._tcp, _https._tcp, _ssh._tcp, _smb._tcp, _afpovertcp._tcp, _nfs._tcp, _workstation._tcp, _device-info._tcp, _googlecast._tcp, _airplay._tcp, _raop._tcp, _spotify-connect._tcp, _sonos._tcp, _homekit._tcp, _hap._tcp, _matter._tcp, _matterc._udp, _esphomelib._tcp, _home-assistant._tcp, _hue._tcp, _ipp._tcp, _ipps._tcp, _printer._tcp, _pdl-datastream._tcp, _scanner._tcp, _uscan._tcp, _androidtvremote2._tcp, _nvstream._tcp, _companion-link._tcp, _sleep-proxy._udp`.
  - `record_to_observation(rec: Mapping[str, Any], seen_at) -> Observation | None` — `rec` keys `{name, type, addresses: list[str], port, server}`; picks the first IPv4; `hostname=name`; `attrs={"services":[{"port":port,"proto":"tcp"|"udp","name":type}], "server": server}`.
  - `MdnsCollector(flush_s=60)` — long-lived `AsyncZeroconf` + `AsyncServiceBrowser`; `collect()` returns and clears records gathered since the last call. `start()`/`stop()` lifecycle methods called by the scheduler.

- [ ] **Step 1: Tests** — `test_record_to_observation`, `test_record_ipv6_only_returns_none`, `test_collect_drains_buffer` (inject records via `_on_record(rec)` without a network).
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: mDNS collector"`

---

### Task 12: nmap light and deep scans with scan policy

**Files:**
- Create: `collectors/nmap.py`
- Test: `tests/test_collector_nmap.py`, fixtures `nmap_light.xml`, `nmap_deep.xml`

**Interfaces:**
- Produces:
  - `parse_nmap_xml(xml: str, seen_at) -> list[Observation]` — one observation per `<host>` with state `up`: IPv4, MAC (from `<address addrtype="mac">` if present), and `attrs["services"]` for `state=open` ports with `name`, `product`, `version`.
  - `NmapLightCollector(conn_reader: Callable[[], list[str]], timeout_s=1800)` — `conn_reader` returns target IPv4s (current leases of online devices whose `scan_policy='normal'`); command `nmap -oX - -T3 --top-ports 100 -Pn <targets>`; empty target list → `[]` without running nmap.
  - `deep_scan(conn, device_id: int, *, runner=run_nmap) -> list[Observation]` — refuses with `ScanRefused("scan_policy=never")` or `ScanRefused("no current IPv4")`; command `nmap -oX - -sV -p- -T3 -Pn <ip>`.
  - `scan_targets(conn) -> list[str]` (the default `conn_reader`).

- [ ] **Step 1: Tests** — `test_parse_nmap_light`, `test_parse_nmap_skips_closed_and_down`, `test_scan_targets_excludes_never_and_offline`, `test_deep_scan_refuses_never` , `test_deep_scan_uses_current_ip` (fake runner captures argv).
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: nmap light/deep scans honoring scan policy"`

---

### Task 13: Scheduler, collector status and backoff

**Files:**
- Create: `src/lanscape_atlas/scheduler.py`, `collectors/__init__.py` registry
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Collector`, `Ingestor`, `Settings`.
- Produces:
  - `build_collectors(settings, conn) -> list[Collector]` — enabled collectors only; asusrouter skipped when `settings.router is None`.
  - `next_delay(base_s: float, failures: int) -> float` — `min(base_s * 2**failures, 3600)`.
  - `CollectorRunner(conn, ingestor, clock).run_once(c: Collector) -> RunOutcome` — wraps `collect()` in `asyncio.wait_for(timeout=c.timeout_s)`; on success ingests and resets `consecutive_failures`, sets `last_ok`; on exception/timeout records `last_error` (exception type + message, truncated 500 chars, router password never included), increments failures, sets `next_run_at`. Never raises. `RunOutcome(ok: bool, error: str | None, result: IngestResult | None)`.
  - `start_scheduler(settings, runner, collectors) -> AsyncIOScheduler` — interval jobs for interval collectors, `CronTrigger.from_crontab` for `nmap_light` (default `"17 3 * * *"`), a daily prune job (`prune_observations`), `max_instances=1`, `coalesce=True`. A run whose `next_run_at` is in the future (backoff) is skipped.

- [ ] **Step 1: Tests** — `test_next_delay_caps_at_3600`, `test_run_once_success_resets_failures`, `test_run_once_timeout_recorded` (collector sleeps past `timeout_s=0.05`; Review Focus 4), `test_run_once_exception_isolated` (two collectors, first raises, second ingests), `test_error_message_redacts_router_password`.
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: scheduler with per-collector status and backoff"`

---

### Task 14: Anonymizer and fake-house dataset

**Files:**
- Create: `scripts/anonymize.py`, `tests/fixtures/fake_house.json`, `src/lanscape_atlas/cli.py` (`ingest-file` subcommand only for now)
- Test: `tests/test_anonymize.py`, `tests/test_fake_house.py`

**Interfaces:**
- Produces:
  - `Anonymizer(seed: str)` with `mac(real: str) -> str` (maps into `00:00:5e:00:53:xx`, preserving the locally-administered bit by using `02:00:5e:00:53:xx` for random MACs, both allowed by Task 1's checker), `ipv4(real) -> str` (into `192.0.2.0/24`, then `198.51.100.0/24`, then `203.0.113.0/24`; preserves the last octet when free), `hostname(real) -> str` (`host-<n>`), and `transform_text(text) -> str` applying all three to every match. Same input → same output within one instance. Raises if more than 762 distinct IPs.
  - CLI: `python scripts/anonymize.py --seed S in_file > out_file`.
  - `fake_house.json`: list of serialized `Observation`s for ~40 devices spanning all collectors: router, mesh node, 3 switches (placeholder names), NAS, Proxmox-style host, TVs, phones with random MACs, printers, ESPHome devices, cameras; at least one device with two interfaces.
  - `lanscape-atlas ingest-file <path>` — loads observations JSON and ingests (used for UI dev).

- [ ] **Step 1: Tests** — `test_mac_mapping_consistent`, `test_random_bit_preserved`, `test_transform_text_rewrites_all`, `test_output_passes_leak_check` (run `find_leaks` on transformed sample), `test_fake_house_ingests` (≥40 devices, ≥1 with 2 interfaces, ≥3 random-MAC interfaces, 0 dropped).
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS, and `python scripts/check_leaks.py` exits 0
- [ ] **Step 5: Commit** — `git commit -m "feat: fixture anonymizer and fake-house dataset"`

---

### Task 15: HTTP API

**Files:**
- Create: `src/lanscape_atlas/web/app.py`, `web/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `Service`, `queries`, `deep_scan`, `Settings`.
- Produces:
  - `create_app(settings: Settings, *, conn=None, clock=utcnow, start_background=True) -> FastAPI` — owns DB connection, `Service`, `Ingestor`, scheduler (skipped when `start_background=False`).
  - Optional bearer auth: when `settings.api_token` is set, `/api/*` and `/mcp*` require `Authorization: Bearer <token>`; pages are unaffected (proxy handles them).
  - Routes (all JSON; writes use `actor="user"`):
    - `GET /api/v1/devices?online&type&trust&place&ap_node&q&limit&offset` → `list[DeviceSummary]`
    - `GET /api/v1/devices/{id}` → `DeviceView` (404 unknown)
    - `PATCH /api/v1/devices/{id}` body `{fields}` → `{"batch_id": int}` (422 on `ValueError`)
    - `POST /api/v1/interfaces/{id}/claim` body `{"device_id"}` → `{"batch_id"}`
    - `PUT /api/v1/devices/{id}/place` body `{"name"}` → `{"batch_id"}`
    - `POST /api/v1/devices/{id}/deep-scan` → `202 {"status":"started"}`; `409 {"detail": "<ScanRefused message>"}`
    - `GET /api/v1/services?device_id&port`, `GET /api/v1/changes?limit&actor`
    - `POST /api/v1/changes/{batch_id}/undo` → `{"batch_id"}`; `409 {"detail":"conflict","conflicting":[...]}`
    - `POST /api/v1/links`, `DELETE /api/v1/links/{id}`, `GET /api/v1/topology` → `{"nodes":[...],"edges":[...]}` (nodes include saved positions)
    - `PUT /api/v1/topology/layout` body `{"positions": {device_id: [x, y]}}`
    - `GET /api/v1/collectors` → status rows

- [ ] **Step 1: Tests** (httpx `TestClient`, `start_background=False`, fake-house ingested) — `test_list_devices_filter_online`, `test_get_unknown_404`, `test_patch_bad_field_422`, `test_undo_conflict_409_payload`, `test_deep_scan_never_409`, `test_token_required_when_configured`, `test_topology_shape`.
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement.** Deep scans run as a FastAPI background task (async subprocess, no thread pool), then ingest via the same `Ingestor`.
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: JSON API"`

---

### Task 16: Web UI — devices, device page, inbox, history, status

**Files:**
- Create: `web/pages.py`, `web/templates/{base,devices,device,inbox,history,status}.html`, `web/templates/_device_rows.html`, `web/static/{htmx.min.js,app.css}`
- Test: `tests/test_pages.py`

**Interfaces:**
- Consumes: Task 15 app and `Service`/`queries`.
- Produces: routes `GET /` (devices), `GET /devices/{id}`, `POST /devices/{id}` (form save → redirect), `GET /inbox`, `POST /inbox/{device_id}/{action}` (`known|unwanted`), `POST /interfaces/{id}/claim`, `GET /history`, `POST /history/{batch_id}/undo`, `GET /status`, `GET /partials/devices` (HTMX table refresh with the same filters as the API, polled every 30 s). Each editable field on the device page shows `set by <actor> · <YYYY-MM-DD>` from `DeviceView.field_sources`. Deep-scan button disabled when `scan_policy == "never"`. Undo conflict renders an inline message listing conflicting batch ids. Vendor JS files are vendored (no CDN) with versions noted in `web/static/VENDORED.md`.

- [ ] **Step 1: Tests** — `test_devices_page_lists_fake_house`, `test_device_page_shows_set_by_user_after_edit`, `test_inbox_lists_new_devices`, `test_history_undo_button_works`, `test_deep_scan_button_disabled_for_never`, `test_status_shows_failing_collector`.
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement.** Keep templates plain; the `frontend-design` skill is not needed for Phase 1.
- [ ] **Step 4: Run** — PASS; then `lanscape-atlas serve` against a temp data dir with `ingest-file tests/fixtures/fake_house.json` and click through each page manually.
- [ ] **Step 5: Commit** — `git commit -m "feat: web UI pages"`

---

### Task 17: Topology view

**Files:**
- Create: `web/templates/topology.html`, `web/static/{cytoscape.min.js,topology.js}`
- Modify: `web/pages.py` (`GET /topology`)
- Test: `tests/test_topology.py`

**Interfaces:**
- Consumes: `GET /api/v1/topology`, `PUT /api/v1/topology/layout`, `POST/DELETE /api/v1/links`, `create_placeholder_device`.
- Produces: discovered edges derived at query time from `interface.ap_node`, which the asusrouter collector stores as the AiMesh node's MAC: edge from the client's device to the device owning an interface with that address, dashed; manual `link` rows solid. Unpositioned nodes are laid out with Cytoscape `breadthfirst` rooted at the router (`type='router'`); dragging saves positions (debounced 1 s). Toolbar: add placeholder device (name + type), connect two selected nodes (wired), delete selected manual link. Add `POST /api/v1/devices/placeholder` body `{name, type}` → `{"device_id","batch_id"}`.

- [ ] **Step 1: Tests** (server side only) — `test_topology_edges_from_ap_node`, `test_manual_link_is_solid_kind`, `test_layout_persisted`, `test_placeholder_device_endpoint`.
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS; manually verify drag-save and link creation in a browser against the fake house.
- [ ] **Step 5: Commit** — `git commit -m "feat: interactive topology view"`

---

### Task 18: MCP server

**Files:**
- Create: `src/lanscape_atlas/mcp_server.py`
- Modify: `web/app.py` (mount at `/mcp`)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `Service`, `queries`, `deep_scan`.
- Produces: `build_mcp(service: Service, scanner: Callable[[int], Awaitable[None]]) -> FastMCP` registering exactly the ten tools in spec §8.3 with those names; all writes use `actor="claude"`. Errors returned as tool errors with plain messages (`"device 42 not found"`, `"undo conflict with batches [17, 19]"`, `"scan refused: scan_policy=never"`). Mounted with streamable HTTP transport at `/mcp`. If `mcp-server-shared`'s `remote.py`/`logsafe.py` are vendored, place them under `src/lanscape_atlas/_shared/` with a `shared.lock` per that repo's sync convention; otherwise skip (the bearer-token check from Task 15 covers auth).

- [ ] **Step 1: Tests** — call tools in-process via the FastMCP test client: `test_tool_names_exact`, `test_find_device_by_ip`, `test_update_device_records_claude_actor`, `test_run_deep_scan_never_errors`, `test_undo_conflict_message`, `test_no_secrets_in_any_tool_output` (configure router password `"s3cret-test"`, call every read tool, assert substring absent).
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: MCP server"`

---

### Task 19: Serve command, OpenRC service, docs

**Files:**
- Modify: `src/lanscape_atlas/cli.py` (`serve`, `migrate`, `update-oui`)
- Create: `deploy/openrc/lanscape-atlas`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `lanscape-atlas migrate` — creates data dir and DB.
  - `lanscape-atlas serve [--host --port]` — runs uvicorn on `create_app(load_settings())`.
  - `lanscape-atlas update-oui` — downloads the IEEE `oui.csv` to `$LANSCAPE_DATA_DIR/oui.csv` (URL in a constant; atomic replace).
  - OpenRC script: `command=/opt/lanscape-atlas/venv/bin/lanscape-atlas`, `command_args="serve"`, `command_user=lanscape`, `supervisor=supervise-daemon`, `depend() { need net; }`, `export LANSCAPE_DATA_DIR`, `capabilities="^cap_net_raw,^cap_net_admin"` (for arp-scan/nmap).
  - README: what it is, screenshot placeholder from fake house, install on Alpine LXC (packages `py3-pip arp-scan nmap`), bridged-network requirement, config reference, Cloudflare Access / LAN-only notes, MCP connection example using a documentation hostname, privacy rules for contributors (leak checker, denylist path, anonymizer).

- [ ] **Step 1: Tests** — `test_migrate_creates_db(tmp_path, monkeypatch)`, `test_serve_builds_app_without_starting` (patch uvicorn.run, assert called with app and port 8720).
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run full suite and checks** — `ruff check . && mypy src && pytest && python scripts/check_leaks.py` — all pass.
- [ ] **Step 5: Commit** — `git commit -m "feat: CLI, OpenRC service and README"`
