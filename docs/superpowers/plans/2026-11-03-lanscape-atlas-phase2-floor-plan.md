# Floor Plan (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Place devices on an uploaded plan of the house: rooms are drawn polygons, fixed devices are dragged to a point inside their room, roaming devices keep a room only. Every write is undoable and visible over HTTP and MCP.

**Architecture:** A pure geometry module decides containment and validates polygons. Plan image bytes live in the data directory; the database holds metadata. A `Service` write method per operation (plan, room, placement) logs changes through the existing batch and change machinery, so undo works unchanged except for one extension (floor plan creation). Read model in `queries.py`, served by a new `/api/v1/floorplan` router and a `/floorplan` page with its own CSRF-protected writes, and two MCP tools.

**Tech Stack:** Python 3.12+, SQLite (schema migration 1 to 2), FastAPI, Jinja2 + HTMX, vanilla JS and inline SVG, Pillow (PNG/JPEG dimensions), defusedxml (SVG parsing), pytest, Playwright (one browser pass).

**Spec:** `docs/superpowers/specs/2026-11-03-lanscape-atlas-phase2-floor-plan-design.md`

## Global Constraints

- Schema: Phase 1 is deployed at `user_version = 1`. Phase 2 adds migration `0002`. `schema.sql` (v1) is never edited.
- Coordinates: polygon points and device `pos_x`/`pos_y` are normalized, each in `[0, 1]`.
- Roaming devices have `pos_x` and `pos_y` NULL. Fixed devices with a point have a `place_id`.
- Plan replacement: aspect ratio must match the existing plan within relative tolerance `1e-3`, else `ValueError` (HTTP 422).
- Upload limit: 10 MiB (`10 * 1024 * 1024` bytes). Accepted types: `png`, `jpg`, `svg`.
- Write actors: `user` or `claude` for user-facing writes; `collector:*` is not used by Phase 2.
- MCP exposes text only: no image bytes, no file paths.
- No Cytoscape on the floor plan page. The topology page is not changed.
- Test fixtures never commit an image file. Synthetic plans are generated in test code (`tests/helpers/plan.py`) and written to `tmp_path`.
- Pre-commit: `ruff check`, `ruff format --check`, `mypy src`, `pytest`, `python scripts/check_leaks.py` all clean.

## Review Focus

1. A room name with markup or quotes (`<script>`, `"`) renders as text on the page and does not break the SVG. Pinned in Task 9.
2. A malformed polygon (two points, NaN, a coordinate above 1, a non-list) returns 422, not 500, and writes nothing. Pinned in Task 2 and Task 5.
3. A point exactly on a polygon edge or vertex counts as inside, consistently between server and page. Pinned in Task 2.
4. An oversized or non-image upload returns 422 and leaves no file in the data directory. Pinned in Task 3.
5. Two tabs editing the same device: the second save returns 409 and overwrites nothing. Pinned in Task 6.

---

### Task 0: Shared test helpers

**Files:**
- Create: `tests/helpers/__init__.py` (empty), `tests/helpers/plan.py`, `tests/helpers/floorplan_fixtures.py`
- Modify: `tests/conftest.py` (import the fixtures from `helpers.floorplan_fixtures` so every test module sees them)

**Interfaces:**
- Produces in `tests/helpers/plan.py`:
  - `png_bytes(w: int, h: int) -> bytes` and `jpeg_bytes(w: int, h: int) -> bytes` (Pillow, in memory); `svg_bytes(w: int, h: int) -> bytes` (root element with `width`, `height` and `viewBox`).
  - `schema_sql_v1() -> str`: the text of `store/schema.sql`.
  - `SQ_POINTS = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]`.
  - `save_square_room(svc, name: str = "Room A") -> int`: calls `svc.save_room("user", name, SQ_POINTS)` and returns the place id. Requires Task 5.
  - `svc_last(db: sqlite3.Connection, device_id: int) -> int`: calls `queries.last_placement_batch`. Requires Task 6.
- Produces in `tests/helpers/floorplan_fixtures.py`, imported by `conftest.py`:
  - `client`, `client_with_token`, `client_csrf`: built the same way as the harnesses in `tests/test_web_security.py` and `tests/test_topology.py`.
  - `mcp_client`: the in-process harness from `tests/test_mcp.py`.
  - `seeded_room` (int place id of a `save_square_room` room), `seeded_device` (int from `make_device`).
  - `room_and_device`: namedtuple with `db`, `room`, `dev`.
  - `plan_seeded`: a `floor_plan` row exists (Task 4 `set_floor_plan` with a 1600x1200 plan).
  - `saved_room_named(name: str) -> Callable[[str], int]`.
  - `stale_setup`: namedtuple with `body`, a placement payload whose `based_on` is already stale.
  - `tmp_data_dir`: a temporary data directory with an empty `floorplan/` subdirectory.
  - `tmp_git_repo`, `git_add(repo, path)`, `run_check_leaks(repo) -> subprocess.CompletedProcess`.
  - `live_server`, `page` (Playwright; the test is skipped when Playwright is unavailable), `synthetic_plan_and_room`, `drag_to_room_centre(page, locator)`, `drag_outside_room(page, locator)`, `dev_position(page, device_id) -> tuple[float, float] | None`.

- [ ] **Step 1: Read the existing harnesses**

Read `tests/test_web_security.py`, `tests/test_topology.py` and `tests/test_mcp.py`. Copy their client, CSRF and in-process MCP setup into `floorplan_fixtures.py`. Do not write a new harness.

- [ ] **Step 2: Verify the helpers import**

Run: `pytest --collect-only -q`
Expected: collection succeeds with no import errors. The helpers are exercised by the tasks that use them: image helpers in Task 3, rooms in Task 5, placements in Task 6.

- [ ] **Step 3: Commit**

```bash
git add tests/helpers tests/conftest.py
git commit -m "test: shared floor plan helpers and fixtures"
```

---

### Task 1: Schema migration framework and v2 schema

**Files:**
- Modify: `src/lanscape_atlas/store/db.py`
- Create: `src/lanscape_atlas/store/migrations/0002_floor_plan.sql`
- Test: `tests/test_migrations.py`
- Modify: `tests/test_packaging.py` (assert the migration file is in the wheel)

**Interfaces:**
- Consumes: existing `schema.sql` (v1).
- Produces: `SCHEMA_VERSION = 2`; `migrate(conn: sqlite3.Connection) -> int`; `_load_migration(n: int) -> str` (returns the SQL for migration `n`, used by tests).

- [ ] **Step 1: Write the failing tests**

```python
def test_fresh_database_reaches_v2(tmp_path):
    conn = open_db(tmp_path / "a.db")
    assert migrate(conn) == 2
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='floor_plan'").fetchone()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(device)")}
    assert "roaming" in cols

def test_v1_database_upgrades_and_keeps_rows(tmp_path):
    conn = open_db(tmp_path / "b.db")
    conn.executescript(schema_sql_v1())          # schema.sql only
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO device(first_seen,last_seen) VALUES('2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')")
    assert migrate(conn) == 2
    row = conn.execute("SELECT roaming FROM device").fetchone()
    assert row[0] == 0

def test_newer_database_is_refused(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute("PRAGMA user_version = 3")
    with pytest.raises(RuntimeError, match="newer than supported"):
        migrate(conn)

def test_failed_migration_leaves_version_at_1(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "d.db")
    conn.executescript(schema_sql_v1())
    conn.execute("PRAGMA user_version = 1")
    monkeypatch.setattr(db, "_load_migration", lambda n: "ALTER TABLE nope ADD COLUMN x INT;")
    with pytest.raises(sqlite3.Error):
        migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_migrations.py -v`
Expected: FAIL (`SCHEMA_VERSION` is 1; no migration file).

- [ ] **Step 3: Implement the migration**

`0002_floor_plan.sql`:

```sql
CREATE TABLE floor_plan (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    width      INTEGER NOT NULL CHECK (width > 0),
    height     INTEGER NOT NULL CHECK (height > 0),
    sha256     TEXT NOT NULL,
    file_name  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE device ADD COLUMN roaming INTEGER NOT NULL DEFAULT 0 CHECK (roaming IN (0, 1));
```

`db.migrate`: when `user_version` is 0, run `schema.sql` (v1) and set `user_version = 1`. Then, for each migration `n` in `2..SCHEMA_VERSION` where `n > current`, run `_load_migration(n)` inside `BEGIN IMMEDIATE` ... `COMMIT` with `PRAGMA user_version = n` in the same transaction, rolling back on `sqlite3.Error`. Keep the newer-version refusal as it is. One approach: a list of `(version, sql)` steps, applied in order, each in its own transaction.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_migrations.py tests/test_store.py -v`
Expected: PASS.

- [ ] **Step 5: Add the wheel check**

In `tests/test_packaging.py`, assert that the built wheel contains `lanscape_atlas/store/migrations/0002_floor_plan.sql`. Run: `pytest tests/test_packaging.py -v`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lanscape_atlas/store/ tests/test_migrations.py tests/test_packaging.py
git commit -m "feat(store): migrate schema v1 to v2 for floor plan"
```

---

### Task 2: Geometry: polygon parsing, containment, aspect check

**Files:**
- Create: `src/lanscape_atlas/floorplan/__init__.py`, `src/lanscape_atlas/floorplan/geometry.py`
- Test: `tests/test_geometry.py`

**Interfaces:**
- Produces:
  - `parse_polygon(raw: object) -> list[tuple[float, float]]`: a list of at least 3 `[x, y]` pairs, every value finite and in `[0, 1]`. Raises `ValueError` otherwise.
  - `point_in_polygon(x: float, y: float, polygon: Sequence[tuple[float, float]]) -> bool`: ray casting; points on an edge or vertex return True.
  - `check_aspect(old_w: int, old_h: int, new_w: int, new_h: int) -> None`: raises `ValueError` unless `math.isclose(old_w/old_h, new_w/new_h, rel_tol=1e-3)`.

- [ ] **Step 1: Write the failing tests**

```python
SQUARE = [(0.1, 0.1), (0.5, 0.1), (0.5, 0.5), (0.1, 0.5)]
L_SHAPE = [(0, 0), (1, 0), (1, 0.4), (0.4, 0.4), (0.4, 1), (0, 1)]

def test_inside_and_outside_square():
    assert point_in_polygon(0.3, 0.3, SQUARE)
    assert not point_in_polygon(0.7, 0.3, SQUARE)

def test_edge_and_vertex_count_as_inside():
    assert point_in_polygon(0.3, 0.1, SQUARE)   # on bottom edge
    assert point_in_polygon(0.5, 0.5, SQUARE)   # on vertex

def test_concave_notch_is_outside():
    assert not point_in_polygon(0.7, 0.7, L_SHAPE)
    assert point_in_polygon(0.2, 0.7, L_SHAPE)

@pytest.mark.parametrize("raw", [
    [[0.1, 0.1], [0.2, 0.2]],                    # two points
    [[0.1, 0.1], [0.2, float("nan")], [0.3, 0.1]],
    [[0.1, 0.1], [1.5, 0.2], [0.3, 0.1]],        # above 1
    "not a list",
    [[0.1, 0.1], [0.2], [0.3, 0.1]],
])
def test_parse_polygon_rejects(raw):
    with pytest.raises(ValueError):
        parse_polygon(raw)

def test_aspect_same_ratio_different_size_ok():
    check_aspect(1600, 1200, 800, 600)

def test_aspect_different_ratio_rejected():
    with pytest.raises(ValueError):
        check_aspect(1600, 1200, 1920, 1080)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_geometry.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement the module**

Use a standard even-odd ray cast for interior points. For edge checks, test each segment for collinearity within `1e-12` and bounds before the ray test. `parse_polygon` must reject `bool` values and non-numeric values, and it must not return the input list (copy to tuples).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_geometry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/floorplan/ tests/test_geometry.py
git commit -m "feat(floorplan): polygon parsing, containment and aspect check"
```

---

### Task 3: Plan image reading and storage

**Files:**
- Create: `src/lanscape_atlas/floorplan/image.py`
- Modify: `pyproject.toml` (add `Pillow` and `defusedxml` to runtime dependencies)
- Test: `tests/test_floorplan_image.py`
- Create: `tests/helpers/plan.py` (generates synthetic PNG, JPEG and SVG bytes in code)

**Interfaces:**
- Produces:
  - `MAX_BYTES = 10 * 1024 * 1024`, `ALLOWED_EXT = frozenset({"png", "jpg", "svg"})`
  - `read_dimensions(data: bytes, ext: str) -> tuple[int, int]`: Pillow for `png` and `jpg`; for `svg`, the `width`/`height` attributes, or `viewBox` when they are absent. Raises `ValueError` for anything unreadable.
  - `store_plan_file(data_dir: Path, data: bytes, ext: str) -> tuple[str, str]`: returns `(sha256, file_name)`, writes `data_dir/floorplan/<sha256>.<ext>` via temp file and `os.replace`. Same bytes give the same file.
  - `validate_upload(data: bytes, ext: str) -> None`: raises `ValueError` for an unknown extension, a size over `MAX_BYTES`, or empty data.
- Tests call `tests/helpers/plan.py::png_bytes(w, h)`, `jpeg_bytes(w, h)`, `svg_bytes(w, h)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_png_dimensions():
    assert read_dimensions(png_bytes(640, 480), "png") == (640, 480)

def test_svg_viewbox_dimensions():
    assert read_dimensions(svg_bytes(800, 600), "svg") == (800, 600)

def test_svg_entity_expansion_is_refused():
    bomb = b'<!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><svg width="1" height="1">&b;</svg>'
    with pytest.raises(ValueError):
        read_dimensions(bomb, "svg")

def test_rejects_non_image_bytes():
    with pytest.raises(ValueError):
        read_dimensions(b"hello", "png")

def test_rejects_empty_upload():
    with pytest.raises(ValueError):
        validate_upload(b"", "jpg")

def test_rejects_unknown_extension():
    with pytest.raises(ValueError):
        validate_upload(png_bytes(10, 10), "gif")

def test_rejects_oversized(monkeypatch):
    with pytest.raises(ValueError, match="10 MiB"):
        validate_upload(b"x" * (MAX_BYTES + 1), "png")

def test_store_is_content_addressed(tmp_path):
    a = store_plan_file(tmp_path, png_bytes(10, 10), "png")
    b = store_plan_file(tmp_path, png_bytes(10, 10), "png")
    assert a == b
    assert (tmp_path / "floorplan" / f"{a[0]}.png").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_floorplan_image.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the module**

Use `defusedxml.ElementTree.fromstring` for SVG so entity expansion raises. Read PNG/JPEG with `PIL.Image.open` in a `with` block and verify it with `img.verify()` before reading size again. Create `floorplan/` with mode `0o750` if missing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_floorplan_image.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/floorplan/image.py pyproject.toml tests/test_floorplan_image.py tests/helpers/plan.py
git commit -m "feat(floorplan): read and store plan images safely"
```

---

### Task 4: Service.set_floor_plan and its undo

**Files:**
- Modify: `src/lanscape_atlas/service/service.py`, `src/lanscape_atlas/service/__init__.py`
- Test: `tests/test_floorplan_service.py`

**Interfaces:**
- Consumes: `check_aspect` (Task 2), `store_plan_file` (Task 3), `_write_field`, `begin_batch`, `_write_txn`.
- Produces: `Service.set_floor_plan(actor: str, *, width: int, height: int, sha256: str, file_name: str) -> int` (batch id).
- Undo extension: `_apply_inverse` (service.py) must treat a `floor_plan` change whose `old_value` is NULL on the `sha256` field as a creation, and undo by deleting the `floor_plan` row. Add the case next to the existing `place` creation handling and reuse `_Change.creates`.

- [ ] **Step 1: Write the failing tests**

```python
def test_first_upload_creates_row_and_logs(svc, db, events):
    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256="a"*64, file_name="a"*64+".png")
    assert bid > 0
    assert db.execute("SELECT width, height FROM floor_plan WHERE id=1").fetchone() == (1600, 1200)

def test_replacement_with_same_aspect_ok(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256="a"*64, file_name="x.png")
    svc.set_floor_plan("user", width=800, height=600, sha256="b"*64, file_name="y.png")
    assert db.execute("SELECT width FROM floor_plan").fetchone()[0] == 800

def test_different_aspect_refused_and_nothing_written(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256="a"*64, file_name="x.png")
    before = dump_state(db)
    with pytest.raises(ValueError, match="aspect"):
        svc.set_floor_plan("user", width=1920, height=1080, sha256="c"*64, file_name="z.png")
    assert dump_state(db) == before

def test_undo_restores_previous_plan(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256="a"*64, file_name="x.png")
    bid = svc.set_floor_plan("user", width=800, height=600, sha256="b"*64, file_name="y.png")
    svc.undo("user", bid)
    assert db.execute("SELECT sha256 FROM floor_plan").fetchone()[0] == "a"*64

def test_undo_of_first_upload_deletes_row(svc, db):
    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256="a"*64, file_name="x.png")
    svc.undo("user", bid)
    assert db.execute("SELECT COUNT(*) FROM floor_plan").fetchone()[0] == 0

def test_claude_actor_accepted_collector_refused(svc):
    svc.set_floor_plan("claude", width=10, height=10, sha256="a"*64, file_name="x.png")
    with pytest.raises(ValueError):
        svc.set_floor_plan("collector:arp", width=10, height=10, sha256="b"*64, file_name="y.png")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_floorplan_service.py -v`
Expected: FAIL (no method).

- [ ] **Step 3: Implement**

Inside `_write_txn`: read the current row (or None). If it exists, `check_aspect` against the new size. Begin a batch. If it does not exist, insert `floor_plan(id=1, ...)` and log one change per field with `old=None` for `sha256` (the marker used by undo). If it exists, `_write_field` for `width`, `height`, `sha256`, `file_name`, and `updated_at`. Publish the batch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_floorplan_service.py tests/test_service_undo.py -v`
Expected: PASS (the existing undo tests must stay green).

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/service/ tests/test_floorplan_service.py
git commit -m "feat(service): set_floor_plan with undo, refuses aspect change"
```

---

### Task 5: Service rooms: save_room and rename_room

**Files:**
- Modify: `src/lanscape_atlas/service/service.py`
- Test: `tests/test_floorplan_rooms.py`

**Interfaces:**
- Consumes: `parse_polygon` (Task 2), `_write_field`, `begin_batch`, `_write_txn`.
- Produces:
  - `Service.save_room(actor: str, name: str, polygon: object) -> int`: creates the room if the name is new, otherwise attaches to the existing place, and writes `polygon` as JSON. Returns the batch id.
  - `Service.rename_room(actor: str, place_id: int, new_name: str) -> int`: raises `KeyError` for an unknown place, `ValueError` if the new name is taken or blank.

- [ ] **Step 1: Write the failing tests**

```python
SQ = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]

def test_save_room_creates_and_stores_polygon(svc, db):
    svc.save_room("user", "Room A", SQ)
    row = db.execute("SELECT name, polygon FROM place").fetchone()
    assert row[0] == "Room A"
    assert json.loads(row[1]) == SQ

def test_save_room_existing_name_updates_polygon_same_place(svc, db):
    svc.save_room("user", "Room A", SQ)
    pid = db.execute("SELECT id FROM place").fetchone()[0]
    svc.save_room("user", "Room A", [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6]])
    assert db.execute("SELECT COUNT(*) FROM place").fetchone()[0] == 1
    assert db.execute("SELECT id FROM place").fetchone()[0] == pid

@pytest.mark.parametrize("bad", [[[0.1, 0.1]], [[0.1, 2.0], [0.2, 0.2], [0.3, 0.1]]])
def test_save_room_rejects_bad_polygon_and_writes_nothing(svc, db, bad):
    before = dump_state(db)
    with pytest.raises(ValueError):
        svc.save_room("user", "Room A", bad)
    assert dump_state(db) == before

def test_rename_keeps_place_id_and_devices(svc, db, make_device):
    svc.save_room("user", "Room A", SQ)
    pid = db.execute("SELECT id FROM place").fetchone()[0]
    dev = make_device()
    svc.update_device("user", dev, {"place_id": pid})
    svc.rename_room("user", pid, "Room B")
    assert db.execute("SELECT place_id FROM device WHERE id=?", (dev,)).fetchone()[0] == pid

def test_rename_to_existing_name_refused(svc):
    svc.save_room("user", "Room A", SQ)
    svc.save_room("user", "Room B", SQ)
    with pytest.raises(ValueError, match="exists"):
        svc.rename_room("user", 1, "Room B")

def test_undo_of_polygon_edit_restores_old(svc, db):
    svc.save_room("user", "Room A", SQ)
    bid = svc.save_room("user", "Room A", [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6]])
    svc.undo("user", bid)
    assert json.loads(db.execute("SELECT polygon FROM place").fetchone()[0]) == SQ
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_floorplan_rooms.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Store the normalized points as `json.dumps([list(p) for p in points])` so the stored text is stable. Creation of a new place reuses the `set_place` pattern: insert, then `_write_field(batch, "place", id, "name", None, name, actor)`, so that undo of creation deletes the place (existing Ruling 14 path). Use the `_write_txn` + `begin_batch` pattern from `set_place`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_floorplan_rooms.py tests/test_service_undo.py tests/test_service_writes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/service/service.py tests/test_floorplan_rooms.py
git commit -m "feat(service): save_room and rename_room with undo"
```

---

### Task 6: Service placements: place_devices, StaleWrite, last_placement_batch

**Files:**
- Modify: `src/lanscape_atlas/service/service.py`, `src/lanscape_atlas/service/queries.py`, `src/lanscape_atlas/service/__init__.py`
- Test: `tests/test_floorplan_placements.py`

**Interfaces:**
- Consumes: `point_in_polygon`, `parse_polygon`, `_write_field`, `_write_txn`, `begin_batch`.
- Produces:
  - `@dataclass(frozen=True) class Placement: device_id: int; place_id: int | None; x: float | None; y: float | None; roaming: bool = False`
  - `class StaleWrite(Exception)` with `conflicting: list[int]` (device ids).
  - `Service.place_devices(actor: str, placements: Sequence[Placement], based_on: Mapping[int, int]) -> int`: `based_on` maps device id to the batch id the client last saw for that device (0 if none).
  - `queries.last_placement_batch(conn: sqlite3.Connection, device_id: int) -> int`: `MAX(batch_id)` over `change` rows for `entity='device'` with field in `('place_id','pos_x','pos_y','roaming')`, or 0.

Rules, enforced inside one transaction:
- `roaming=True` requires `place_id` and both `x` and `y` `None`. Any other combination with `roaming=True` raises `ValueError`.
- `roaming=False` with `place_id` `None` and `x`/`y` `None` means unplaced. A point requires a `place_id` whose polygon exists and contains `(x, y)`, else `ValueError` (HTTP 422). A place with no polygon also raises `ValueError`.
- Any device whose `last_placement_batch` is greater than `based_on[device_id]` raises `StaleWrite` with all such device ids. Nothing is written.
- Only changed fields are logged, one batch per call. `roaming` true sets `pos_x` and `pos_y` to `None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_fixed_placement_inside_room_saves(svc, db, make_device):
    room = save_square_room(svc)         # Task 5 helper: Room A, square [0.1..0.5]
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    assert db.execute("SELECT place_id, pos_x, pos_y FROM device WHERE id=?", (dev,)).fetchone() == (room, 0.3, 0.3)

def test_point_outside_room_refused_nothing_written(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    before = dump_state(db)
    with pytest.raises(ValueError, match="outside"):
        svc.place_devices("user", [Placement(dev, room, 0.9, 0.9, False)], based_on={dev: 0})
    assert dump_state(db) == before

def test_roaming_clears_point(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    svc.place_devices("user", [Placement(dev, room, None, None, True)], based_on={dev: svc_last(db, dev)})
    assert db.execute("SELECT roaming, pos_x, pos_y FROM device WHERE id=?", (dev,)).fetchone() == (1, None, None)

def test_stale_write_refused_with_conflicting_ids(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    seen = svc_last(db, dev)                         # 0: nothing yet
    svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: seen})
    before = dump_state(db)
    with pytest.raises(StaleWrite) as exc:
        svc.place_devices("user", [Placement(dev, room, 0.2, 0.2, False)], based_on={dev: seen})
    assert exc.value.conflicting == [dev]
    assert dump_state(db) == before

def test_batch_of_moves_is_one_batch(svc, db, make_device):
    room = save_square_room(svc)
    a, b = make_device(), make_device()
    bid = svc.place_devices("user", [Placement(a, room, 0.2, 0.2, False), Placement(b, room, 0.3, 0.3, False)], based_on={a: 0, b: 0})
    assert db.execute("SELECT COUNT(*) FROM change WHERE batch_id=?", (bid,)).fetchone()[0] >= 4

def test_undo_restores_place_point_roaming_exactly(svc, db, make_device):
    room = save_square_room(svc)
    dev = make_device()
    before = dump_state(db)
    bid = svc.place_devices("user", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
    svc.undo("user", bid)
    assert dump_state(db) == before

def test_collector_actor_refused(svc, make_device):
    room = save_square_room(svc)
    dev = make_device()
    with pytest.raises(ValueError):
        svc.place_devices("collector:arp", [Placement(dev, room, 0.3, 0.3, False)], based_on={dev: 0})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_floorplan_placements.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Check every placement against its room's polygon before any write. Then compute the stale set using `last_placement_batch`. Only then begin the batch. Use `_write_field` for `place_id`, `pos_x`, `pos_y`, and `roaming` where the value differs. Use `_check_actor`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_floorplan_placements.py tests/test_service_undo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/service/ tests/test_floorplan_placements.py
git commit -m "feat(service): place_devices with containment and stale-write checks"
```

---

### Task 7: Read model for the floor plan

**Files:**
- Modify: `src/lanscape_atlas/service/queries.py`
- Test: `tests/test_floorplan_queries.py`

**Interfaces:**
- Produces: `floor_plan_view(conn: sqlite3.Connection, *, now: datetime) -> dict[str, Any]` with keys:
  - `plan`: `None` or `{"width", "height", "file_name", "updated_at"}`
  - `rooms`: `[{"id", "name", "polygon": [[x, y], ...] or None}]`
  - `placed`: `[{"device_id", "name", "place_id", "x", "y", "online", "last_batch"}]`
  - `roaming`: `[{"device_id", "name", "place_id", "set_by", "set_at", "online", "last_batch"}]`, where `set_by` and `set_at` come from the `field_source` row for `place_id`
  - `unplaced`: `[{"device_id", "name", "last_batch"}]`
- `last_batch` is `last_placement_batch` from Task 6, so the page can send `based_on`.

Placeholders (devices with no interfaces) are included as fixed devices.

- [ ] **Step 1: Write the failing tests**

```python
def test_view_splits_placed_roaming_unplaced(svc, db, make_device, clock):
    room = save_square_room(svc)
    fixed, phone, loose = make_device(name="Dev A"), make_device(name="Phone A"), make_device(name="Dev B")
    svc.place_devices("user", [Placement(fixed, room, 0.3, 0.3, False)], based_on={fixed: 0})
    svc.place_devices("user", [Placement(phone, room, None, None, True)], based_on={phone: 0})
    view = floor_plan_view(db, now=clock())
    assert [p["device_id"] for p in view["placed"]] == [fixed]
    assert view["roaming"][0]["device_id"] == phone
    assert view["roaming"][0]["set_by"] == "user"
    assert [u["device_id"] for u in view["unplaced"]] == [loose]

def test_rooms_include_polygon_or_none(svc, db):
    save_square_room(svc)
    view = floor_plan_view(db, now=T0)
    assert view["rooms"][0]["polygon"] == SQ_POINTS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_floorplan_queries.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Use single queries with `LEFT JOIN field_source` (entity `device`, field `place_id`). Decode `polygon` with `json.loads` when present. Do not expose file paths.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_floorplan_queries.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/service/queries.py tests/test_floorplan_queries.py
git commit -m "feat(queries): floor plan read model"
```

---

### Task 8: HTTP API under /api/v1/floorplan

**Files:**
- Create: `src/lanscape_atlas/web/floorplan_api.py`
- Modify: `src/lanscape_atlas/web/app.py` (mount the router under the existing `/api/v1` router, with the same bearer-token dependency)
- Test: `tests/test_floorplan_api.py`

**Interfaces:**
- Consumes: `floor_plan_view`, `Service.save_room`, `Service.rename_room`, `Service.place_devices`, `Placement`, `StaleWrite`.
- Produces:
  - `GET /api/v1/floorplan`: the `floor_plan_view` payload.
  - `PUT /api/v1/floorplan/rooms`: body `{"name": str, "polygon": list}` → `{"batch_id": int}`.
  - `POST /api/v1/floorplan/placements`: body `{"placements": [{"device_id", "place_id", "x", "y", "roaming"}], "based_on": {"<device_id>": int}}` → `{"batch_id": int}`.
  - Errors: `KeyError` → 404, `ValueError` → 422, `StaleWrite` → 409 with `{"detail": "conflict", "conflicting": [...], "message": "Some devices changed since this page loaded. Reload to see the latest values."}`.
- Actor: match the existing `/api/v1` convention. Read `web/api.py` and use the same actor value it uses for user writes.

- [ ] **Step 1: Write the failing tests**

```python
def test_get_returns_view_shape(client):
    r = client.get("/api/v1/floorplan")
    assert r.status_code == 200
    assert set(r.json()) >= {"plan", "rooms", "placed", "roaming", "unplaced"}

def test_placement_outside_room_is_422(client, seeded_room, seeded_device):
    r = client.post("/api/v1/floorplan/placements", json={"placements": [
        {"device_id": seeded_device, "place_id": seeded_room, "x": 0.9, "y": 0.9, "roaming": False}],
        "based_on": {str(seeded_device): 0}})
    assert r.status_code == 422

def test_stale_placement_is_409_with_ids(client, seeded_room, seeded_device):
    body = {"placements": [{"device_id": seeded_device, "place_id": seeded_room, "x": 0.3, "y": 0.3, "roaming": False}],
            "based_on": {str(seeded_device): 0}}
    assert client.post("/api/v1/floorplan/placements", json=body).status_code == 200
    r = client.post("/api/v1/floorplan/placements", json=body)
    assert r.status_code == 409
    assert r.json()["detail"] == "conflict"
    assert r.json()["conflicting"] == [seeded_device]

def test_token_required_when_configured(client_with_token):
    assert client_with_token.get("/api/v1/floorplan").status_code == 401   # reuse test_web_security pattern
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_floorplan_api.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Use `Pydantic` models for request bodies with `extra="forbid"`. Keep the `based_on` keys as strings in the body, and convert them to ints. Reject an unknown `actor` field, since the actor comes from the API convention. Reuse the existing `ValueError`/`KeyError` handlers and add `StaleWrite` next to `UndoConflict` in the same place.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_floorplan_api.py tests/test_web_security.py tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/web/floorplan_api.py src/lanscape_atlas/web/app.py tests/test_floorplan_api.py
git commit -m "feat(api): floor plan endpoints under /api/v1"
```

---

### Task 9: The /floorplan page: layout, placement, and CSRF writes

**Files:**
- Create: `src/lanscape_atlas/web/floorplan.py` (routes), `src/lanscape_atlas/web/templates/floorplan.html`, `src/lanscape_atlas/web/static/floorplan.js`
- Modify: `src/lanscape_atlas/web/app.py` (mount routes; nav item), the existing nav template
- Test: `tests/test_floorplan_pages.py`

**Interfaces:**
- Consumes: the CSRF dependency that `web/topology.py` already uses (match its name and cookie pattern), `floor_plan_view`, `Service` writes from Tasks 4–6.
- Produces:
  - `GET /floorplan`: HTML page. The plan image is served from `GET /floorplan/plan.png` (or `.jpg`/`.svg`) reading `data_dir/floorplan/<file_name>`, with `Content-Type` from the extension.
  - `POST /floorplan/plan`: multipart upload → `store_plan_file`, `read_dimensions`, `Service.set_floor_plan`. Returns 303 back to `/floorplan`, or 422 with a message.
  - `POST /floorplan/rooms`: form fields `name`, `polygon` (JSON text) → `save_room`.
  - `POST /floorplan/placements`: JSON body in the same shape as Task 8 → `place_devices`. Returns 409 with `{"detail": "conflict", "conflicting": [...], "message": "Some devices changed since this page loaded. Reload to see the latest values."}`.
- `floorplan.js`: draws rooms as SVG polygons in plan coordinates (0–1 scaled to the image box). Drag a fixed device: on drop, if the point is outside its room's polygon, revert to the last valid position. Arrow keys nudge the selected device by 0.005 in normalized units. Edits collect in memory; "Save changes" sends one POST and reloads on 409.

- [ ] **Step 1: Write the failing tests**

```python
def test_room_name_is_escaped(client_csrf, saved_room_named):
    saved_room_named('<script>alert(1)</script>')
    body = client_csrf.get("/floorplan").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body

def test_post_without_csrf_is_403(client):
    assert client.post("/floorplan/rooms", data={"name": "Room A", "polygon": "[]"}).status_code == 403

def test_placement_conflict_renders_message(client_csrf, stale_setup):
    r = client_csrf.post("/floorplan/placements", json=stale_setup.body)
    assert r.status_code == 409
    assert "reload" in r.json()["message"].lower()

def test_plan_upload_bad_type_is_422_and_no_file(client_csrf, tmp_data_dir):
    r = client_csrf.post("/floorplan/plan", files={"file": ("x.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 422
    assert list((tmp_data_dir / "floorplan").glob("*")) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_floorplan_pages.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Build the page with Jinja2 autoescape on. Put the room and device data in a `<script type="application/json">` block with `json.dumps` plus a `</` replacement to block script breakout (`.replace("</", "<\\/")`), and read it in JS with `JSON.parse`. Create SVG elements with `createElementNS` and set text with `textContent`, never `innerHTML`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_floorplan_pages.py tests/test_pages.py tests/test_topology.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/web/ tests/test_floorplan_pages.py
git commit -m "feat(web): floor plan page with CSRF-protected writes"
```

---

### Task 10: MCP tools: get_floor_plan and place_devices

**Files:**
- Modify: `src/lanscape_atlas/mcp_server.py`
- Test: `tests/test_mcp.py` (add cases)

**Interfaces:**
- Consumes: `floor_plan_view`, `Service.place_devices`, `Placement`, `StaleWrite`, the existing `tool` decorator and `_plain_errors`.
- Produces:
  - `get_floor_plan() -> dict[str, Any]`: `floor_plan_view` output, with the `plan` field reduced to `{"width", "height", "updated_at"}`. No `file_name`, no image bytes.
  - `place_devices(placements: list[dict[str, Any]], based_on: dict[str, int]) -> dict[str, int]`: each placement is `{"device_id", "place_id", "x", "y", "roaming"}`. Actor is `"claude"`. Returns `{"batch_id": int}`.
- Both tools have a `title` and explicit annotations, matching the other tools in the file.

- [ ] **Step 1: Write the failing tests**

```python
def test_get_floor_plan_has_no_file_names(mcp_client, plan_seeded):
    out = mcp_client.call("get_floor_plan")
    assert "file_name" not in json.dumps(out)
    assert "sha256" not in json.dumps(out)

def test_place_devices_actor_is_claude_and_undoable(mcp_client, room_and_device):
    out = mcp_client.call("place_devices", placements=[{"device_id": room_and_device.dev, "place_id": room_and_device.room, "x": 0.3, "y": 0.3, "roaming": False}], based_on={str(room_and_device.dev): 0})
    assert out["batch_id"] > 0
    assert last_batch_actor(room_and_device.db) == "claude"
    mcp_client.call("undo", batch_id=out["batch_id"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp.py -v -k floor`
Expected: FAIL.

- [ ] **Step 3: Implement**

Map `StaleWrite` to a tool error that names the conflicting device ids, through the existing `_tool_error` path. Coerce `based_on` keys with `int()`, and reject non-integer keys with a `ValueError`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lanscape_atlas/mcp_server.py tests/test_mcp.py
git commit -m "feat(mcp): get_floor_plan and place_devices tools"
```

---

### Task 11: Privacy tooling: denylist, image check, synthetic fixtures

**Files:**
- Modify: `scripts/check_leaks.py`
- Test: `tests/test_check_leaks.py` (add cases)

**Interfaces:**
- Produces: `check_leaks` reads the room-name denylist from the file named by `LANSCAPE_LEAK_DENYLIST` (one name per line, case-insensitive). If the variable is set but the file is missing, the script exits non-zero. It also fails when any git-tracked path ends in `.png`, `.jpg`, `.jpeg` or `.svg`.

- [ ] **Step 1: Write the failing tests**

```python
def test_tracked_image_fails(tmp_git_repo):
    (tmp_git_repo / "plan.png").write_bytes(b"\x89PNG")
    git_add(tmp_git_repo, "plan.png")
    assert run_check_leaks(tmp_git_repo).returncode != 0

def test_denylisted_room_name_fails(tmp_git_repo, monkeypatch, tmp_path):
    deny = tmp_path / "deny.txt"; deny.write_text("Room Zed\n")
    monkeypatch.setenv("LANSCAPE_LEAK_DENYLIST", str(deny))
    (tmp_git_repo / "note.md").write_text("see Room Zed\n")
    git_add(tmp_git_repo, "note.md")
    assert run_check_leaks(tmp_git_repo).returncode != 0

def test_missing_denylist_file_fails(tmp_git_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("LANSCAPE_LEAK_DENYLIST", str(tmp_path / "nope.txt"))
    assert run_check_leaks(tmp_git_repo).returncode != 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_check_leaks.py -v`
Expected: FAIL on the new cases.

- [ ] **Step 3: Implement**

Use `git ls-files` for the image check. Load the denylist once. Keep the existing MAC and IP rules unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_check_leaks.py -v && python scripts/check_leaks.py`
Expected: PASS. The repo itself must still pass the real check.

- [ ] **Step 5: Commit**

```bash
git add scripts/check_leaks.py tests/test_check_leaks.py
git commit -m "feat(leaks): room-name denylist and tracked-image check"
```

---

### Task 12: Browser pass, deploy notes, and final verification

**Files:**
- Create: `tests/browser/test_floorplan_browser.py` (follows the Playwright setup used by the Phase 1 page screenshots; skip if Playwright is not installed)
- Modify: `README.md` (floor plan section: upload, rooms, placement, roaming, undo, MCP text-only note), `deploy/setup-alpine.sh` (create `$DATA_DIR/floorplan` owned by the service user, mode `0750`), `tests/test_setup_alpine.py`

**Interfaces:**
- Consumes: the page from Task 9, `tests/helpers/plan.py::svg_bytes`.

- [ ] **Step 1: Write the failing tests**

```python
def test_drag_inside_room_saves(page, live_server, synthetic_plan_and_room):
    page.goto(f"{live_server}/floorplan")
    dev = page.locator("[data-device-id='1']")
    drag_to_room_centre(page, dev)
    page.click("#save-changes")
    assert page.locator(".flash").inner_text().startswith("Saved")

def test_drag_outside_room_reverts(page, live_server, synthetic_plan_and_room):
    page.goto(f"{live_server}/floorplan")
    before = dev_position(page, 1)
    drag_outside_room(page, page.locator("[data-device-id='1']"))
    assert dev_position(page, 1) == before

def test_deploy_script_creates_floorplan_dir():
    text = Path("deploy/setup-alpine.sh").read_text()
    assert "floorplan" in text and "0750" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/browser/test_floorplan_browser.py tests/test_setup_alpine.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add the browser test to the existing Playwright configuration. Add the README section. Add the directory creation to `setup-alpine.sh` next to the existing data directory step.

- [ ] **Step 4: Run the full verification**

Run, from the repo root:

```bash
ruff check . && ruff format --check . && mypy src && pytest && python scripts/check_leaks.py
```

Expected: every command exits 0. `pytest` reports all tests passing, including the browser test when Playwright is available.

- [ ] **Step 5: Commit**

```bash
git add tests/browser/ README.md deploy/setup-alpine.sh tests/test_setup_alpine.py
git commit -m "docs+deploy: floor plan README, data dir, browser pass"
```

---

## Self-Review Notes

- Spec coverage: §3 data model → Tasks 1, 4, 5, 6; §4 page → Task 9; §5 API and MCP → Tasks 8, 10; §6 undo → Tasks 4, 5, 6; §7 Bluetooth hook → intentionally no Phase 2 code (`heard_in_place_id` is Phase 3); §8 privacy → Task 11 and Task 3 (no committed images); §9 testing → covered per task and Task 12; §10 open items → resolved in Tasks 2, 3 and 6 (Pillow and defusedxml for image handling; 10 MiB limit; `field_source` columns confirmed as `entity, entity_id, field, actor, set_at`).
- New dependencies, decided here and to be confirmed by the owner: `Pillow` and `defusedxml`.
- Unresolved for the owner: none in the plan. Executor choice (subagent-driven or native) is pending.
