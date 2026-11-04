# lanscape-atlas — Phase 2 Design: Floor Plan

**Status:** Approved in conversation, section by section (2026-11-03). Pending owner review of this written spec.
**Scope:** Phase 2 of §11 of the Phase 1 design. Phase 1 is complete.

## 1. Goal

Answer "where is this device physically" on a plan of the house. Rooms are drawn on
an uploaded plan image. Fixed devices are placed by hand inside their room. Roaming
devices (phones and similar) keep a room but have no point on the plan.

## 2. Non-goals

- Automatic positions from mesh-node association. Node association is shown on the
  device page only; it never sets a room or a point.
- Multi-floor plans. The `floor` column stays NULL. The schema does not block a later
  multi-floor design.
- Bluetooth discovery and proxies (Phase 3). This spec only leaves the hook (§7).
- Auto-tracing rooms from the image. Rooms are drawn by hand.
- Third-party graph or drawing libraries for the floor plan view.

## 3. Data model

### 3.1 Migration

- `store/db.py` today applies `schema.sql` only on a fresh database, and only when
  `user_version` is not 1. It is extended to apply numbered migration files in order:
  `store/migrations/0002_floor_plan.sql` takes the database from 1 to 2.
- The existing v1 database on CT 210 keeps its data. The migration adds the new table
  and column, and nothing else changes.
- A database newer than the code's `SCHEMA_VERSION` still refuses to start.
- Migration files ship in the wheel (`test_packaging` checks this).

Existing tables are reused where possible. Phase 1 is deployed on CT 210 at schema
version 1, so Phase 2 ships as an incremental migration to schema version 2 (§3.1).
Schema v1 is never edited in place.

- **`place` (existing).** One row per room. `name` is the room name and is unique.
  `polygon` is a JSON list of `[x, y]` points. Each coordinate is between 0 and 1,
  relative to the plan image's width and height. Normalized coordinates survive the
  image being rescaled. `floor` stays NULL.
- **`floor_plan` (new singleton, `id = 1`).** Columns: `width`, `height`, `sha256`,
  `file_name`, `updated_at`. The image file lives in
  `<data_dir>/floorplan/`, outside git. Accepted types are PNG, JPEG and SVG. A
  replacement with a different aspect ratio (width/height) is rejected with a 422.
  Uploading or replacing a plan is a logged, undoable write. Undo restores the row.
  The old file is kept until retention removes it.
- **`device` (existing), changes:**
  - `roaming` INTEGER NOT NULL DEFAULT 0 (new).
  - `pos_x`, `pos_y` (existing REAL). For fixed devices, normalized coordinates on the
    plan. Each point must lie inside its room's polygon.
  - `place_id` (existing). NULL means unplaced.
  - Invariants: a roaming device has `pos_x` and `pos_y` NULL. A device with a
    position has a `place_id`.
  - The "last set by … at …" stamp for roaming devices comes from the existing
    `field_source` provenance (actor and time). Column names to be confirmed against
    schema.sql during planning.

## 4. Web UI

- **Page `/floorplan`.** A separate nav item. Server-rendered Jinja2 with HTMX, like
  the other pages. The plan image is the background. Rooms and devices are drawn in
  inline SVG, with small vanilla JS. Cytoscape is not used. The topology page is
  unchanged.
- **Layout mode (rare).**
  - Upload a plan (PNG, JPEG or SVG).
  - Draw a room by clicking points and closing the polygon, then name it. A name that
    matches an existing `place` attaches to it. A new name creates one.
  - Renaming a room is a logged write.
- **Placement mode (common).**
  - Side trays list unplaced and roaming devices, grouped by room.
  - Drag a fixed device onto the plan. The UI clamps the point to its room's polygon.
    The server enforces the same rule with a 422.
  - Arrow keys nudge the selected device.
- **Roaming devices.** They sit in their room's tray with "last set by … at …". Roaming
  can be toggled on the device page or in the tray. Turning roaming on clears the
  point, and turning it off keeps the room; the device then waits in that room's tray until it is dragged to a point.
- **Saving.** Edits accumulate in the browser and are sent in one "Save changes" POST.
  That POST is a CSRF-protected page endpoint, the same pattern as `/topology/*`. One
  save is one batch. Its actor is `user`.
- **Conflicts.** If a device changed after the page loaded, the server returns 409
  with the conflicting fields. The page reloads them.
- **Offline devices** stay on the plan, dimmed.

## 5. API and MCP

- **HTTP API (`/api/v1/floorplan`, bearer-protected).** Read: plan metadata, rooms and
  device positions. Write: room polygons, and placements as a batch. Image upload is
  UI-only.
- **MCP (two tools).**
  - `get_floor_plan`: returns rooms and device positions as text. The image is never
    served to MCP.
  - `place_devices`: a batch of `{device_id, room, x, y}` or
    `{device_id, room, roaming: true}`. Actor `claude`. Undoable as one batch.
- Errors follow the existing mapping: KeyError → 404, ValueError → 422,
  UndoConflict → 409.

## 6. Undo and change log

- Every write in §4–§5 is a batch with an actor (`user` or `claude`), and it is
  undoable through the existing undo machinery.
- Undo restores each device's previous `place_id`, `pos_x`, `pos_y` and `roaming`
  exactly. It also restores each room's previous polygon and name.
- Undo of a plan replacement restores the `floor_plan` row only. The file is not
  restored.
- The existing rules apply. Undo refuses (409) when a later batch has changed the
  same fields. Undo-of-undo is redo.

## 7. Phase 3 hook (Bluetooth)

- An ESPHome Bluetooth proxy is a fixed device with a room and a point on the plan.
- A Bluetooth sighting is an `interface` with `kind=bluetooth`. It gets its room from
  the proxy that heard it.
- **Decision:** a collector never overwrites a user or Claude value. Bluetooth writes
  a separate field, `heard_in_place_id`, and never changes a device's `place_id`,
  `pos_x` or `pos_y`. The device page and the map show both, for example "Room A (set),
  last heard in Room B, 3 min ago".
- The `heard_in_place_id` column is added in Phase 3, not in Phase 2.

## 8. Privacy

- Plan images, real room names and positions are personal data. They are never
  committed. They live in the database and in `<data_dir>/floorplan/`, both on the
  server only.
- `scripts/check_leaks.py` loads a denylist of real room names from a file outside the
  repo, with the path set by an environment variable. It also fails when an image file
  (PNG, JPEG, SVG) appears under a committed path.
- Test fixtures use a generated plan with rooms named "Room A" and "Room B". Plan
  images are never copied into fixtures.
- `scripts/anonymize.py --names` covers room names. The plan image is never included in
  an anonymized capture.
- `data/` stays gitignored.

## 9. Testing

- **Geometry:** polygon containment, including boundary points and concave shapes.
  Aspect-ratio rejection. The 422 for a point outside its room.
- **Conflicts:** 409 on a stale write.
- **Undo:** a batch of placements and room edits restores place, position, roaming and
  polygon exactly, using the existing undo test pattern.
- **MCP:** `get_floor_plan` and `place_devices`, including the actor and the batch.
- **Browser:** one Playwright pass on the synthetic plan, reusing the Task 17 setup.
- **Privacy:** `check_leaks` fails on a planted image file and on a planted denylisted
  room name.

## 10. Open items for the implementation plan

- Confirm the `field_source` column names and how roaming stamps are read.
- Choose the SVG polygon editing approach and write its tests.
- Decide on the upload size limit for the plan image.
- Confirm how `data_dir` is set in the config and deploy script.
