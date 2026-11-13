# lanscape-atlas — Navigation and Drawing Design (Topology and Floor Plan)

**Status:** Approved by the owner (2026-11-13), including the written spec.
**Scope:** Improvements to the Topology page (Phase 1, Task 17) and the Floor Plan page
(Phase 2). Phases 1–3 are complete and deployed at schema version 4.
**Delivery:** five `Prompt:` issues worked by the issue queue (§9). This document is the
source of truth; each issue points at its sections.

## 1. Intent

The owner arranges and reads two maps: the network graph (Topology) and the house
(Floor Plan). Both need to be easier to move around and search. The Floor Plan also
needs to work without an uploaded image, and to show what an image lacks.

- **Topology.** Zoom and manual connections already work but aren't discoverable, and
  a device wired to an unmanaged switch still shows its dashed router edge, so it looks
  like it has two parents. Devices in the same room should read as a group.
- **Floor Plan.** No zoom or pan today; nothing works without an uploaded image. The
  owner wants to draw the floor (walls, doors, furniture, fixtures, labels) on a blank
  grid, or draw over an image to add doors and furniture, with real measurements.
- **Both.** A search box that finds a device on the map.

## 2. Goals, non-goals, anti-goals

**Goals**
- Topology: on-page help, + / − zoom buttons, dashed-edge hiding (§3.3), node colour by
  place, search.
- Floor Plan: zoom and pan in every mode; search; a blank plan with real dimensions;
  scale calibration for an image; drawing walls (segment and polyline), doors,
  furniture/fixtures (rectangle or outline), and labels; typed lengths; snapping;
  lengths and areas shown.

**Non-goals**
- Walls defining rooms. Drawing is picture only (decided: option A). Rooms stay the
  Phase 2 `place.polygon` polygons that placement and Bluetooth use. Room corners snap to
  wall endpoints so they line up.
- Multi-floor plans. Still one plan (`floor_plan.id = 1`), `place.floor` stays NULL.
- Removing the image from a plan (going from image back to blank).
- Drawing through MCP or the JSON API. MCP can read a summary (§6.6); writes are page-only.
- Client-side multi-step undo (Ctrl+Z) inside an unsaved edit session. "Discard" and
  server undo (History page) cover it.
- Third-party drawing, canvas or pan/zoom libraries (decided: hand-written SVG, §8).
- Icon libraries or furniture catalogues; furniture is a rectangle or outline with a label.

**Anti-goals (would be a failure even if it works)**
- Any change to how placement, rooms or Bluetooth room attribution behave.
- Breaking the Phase 2 browser tests or the existing undo guarantees.
- Personal data in the repo. The repo is **public**. Fixtures, screenshots and examples
  use generic room names ("Room A") and synthetic shapes; `check_leaks` must pass.
- A user edit that isn't undoable.

## 3. Topology (issue 1)

### 3.1 Help and zoom buttons
- Replace the one-line `topo-help` paragraph with a collapsible `<details>` "How to use
  this map" block. Open by default the first time; its open/closed state is remembered
  in `localStorage` (wrapped in try/catch; default open if storage fails). Content:
  - Zoom: scroll, pinch the trackpad, or + / −. Fit shows everything.
  - Pan: drag empty space.
  - Move: drag a device (saved automatically).
  - Search: type a name, IP or MAC and pick a result.
  - Connect: shift-click two devices (or drag a box around them), then "Connect selected".
  - Unmanaged switch or hub: "Add placeholder", connect it to its uplink, then connect each
    device behind it. That device's dashed router line then hides.
  - Delete: click a solid line, then "Delete link". Dashed lines come from the router and
    can't be deleted.
  - Colours: a device's fill is its room's colour (legend in the toolbar).
- `+` and `−` buttons beside Fit: zoom by ×1.25 / ÷1.25 about the centre of the view,
  clamped to the existing `minZoom` 0.1 / `maxZoom` 4. Keyboard: `+`/`=` and `-` when focus
  is on the graph container. Scroll and pinch keep Cytoscape's behaviour.

### 3.2 Colour by place
- `topology_graph` adds `place_id` (int or null) to each node.
- A fixed palette of 10 colours as CSS custom properties `--place-0` … `--place-9`, each
  defined for light and dark (`prefers-color-scheme`) and chosen so the node label (text
  outline) and the online/selection border stay readable. Colour index =
  `place_id % 10`, so a room keeps its colour across reloads and renames. More than 10
  rooms repeat colours (accepted).
- The node **fill** is the place colour; no place keeps today's `--node` fill. The
  **border** keeps its meaning (online, offline, selected). Offline dimming stays.
- Toolbar legend: one swatch + room name per place that has at least one node in the
  graph, plus "no place". Room names are untrusted: built with `textContent`.
- The node JSON carries `place_id` only; the page gets room names from a `places` list
  added to the graph response: `{"id", "name"}` for every place.

### 3.3 Hide a device's dashed edge once it is wired to a switch or hub
- Rule, in `dedupe_edges` / `topology_graph` (server side, so `/api/v1/topology`, the page
  and any MCP consumer agree): drop a derived (`ap-…`) edge when **either endpoint** is a
  device whose type is not `router`, `mesh_node` or `ap`, and that device has a **manual**
  link to a device whose `type` is `switch` or `hub` (both comparisons case-insensitive,
  trimmed). Infrastructure (router, mesh node, AP) never loses its derived edges, so a
  mesh backhaul edge survives even if a node is also wired to a switch. *(Builder-check
  call: `dedupe_edges` keeps the first edge's direction, so "source" is not reliably the
  client.)*
- The rule does not require the switch to have an uplink. A device wired to a switch that
  isn't itself connected shows as detached; the help text says to connect the uplink.
- The underlying `interface.ap_node` data is untouched; the device page still shows it.
- Deleting that manual link brings the dashed edge back (it's derived at query time).
- The existing rule (a manual link on the same pair drops the derived edge) stays.
- Help text explains it (§3.1).

### 3.4 Search
- A search input in the toolbar (`role="combobox"`, results listbox, keyboard: arrows,
  Enter, Escape).
- New page endpoint `GET /topology/search?q=` (same-origin page route like
  `/topology/data`; the bearer API already has `/api/v1/devices?q=`). It calls
  `queries.list_devices(conn, q=…)` (the shared matcher behind the device list, API and
  MCP: an exact IPv4 matches current leases only; otherwise MAC and name/hostname/vendor
  text are OR-ed) under `Service.lock`. The matcher doesn't rank (results come back in id
  order), so the route ranks them in Python: exact IP or full-MAC match, then name starts
  with `q`, then the rest, each group by name (case-insensitive). It returns at most 20
  `{device_id, name, type}`. *(Builder-check call.)* Empty or
  whitespace `q` → `[]`. Query length capped at 100 characters (422 above).
- Debounced 200 ms. Picking a result: select the node, open the side panel, animate
  `cy.animate({center, zoom: max(current, 1.5)})` over 300 ms, then pulse the node once
  (a CSS-class border flash for ~1 s). A result with no node on the graph can't happen
  today (every device is a node) but must not throw.

### 3.5 Tests and docs
- Unit: `dedupe_edges` / `topology_graph` hiding rule (switch, hub, other type, link
  deleted → edge returns, both directions of the manual link), `place_id` and `places` in
  the graph, search route (IP, MAC, name, empty, cap, lock).
- Browser (Playwright, fake house): +/− change the zoom level; search centres and selects
  a node; legend swatch count; dashed edge disappears after connecting a device to a
  placeholder switch.
- README Topology section and screenshot (fake house only).

## 4. Floor Plan: zoom, pan and search (issue 2)

### 4.1 View transform
- Today the `<img>` and `<svg>` overlay scale to the container; the SVG `viewBox` is the
  plan's pixel size. Zoom and pan become a **view transform** on a wrapper (CSS
  `transform: translate() scale()` on `.fp-stage` content, or an equivalent `viewBox`
  change applied to both layers). The builder picks the mechanism; the requirement is
  that the image and every SVG layer move together exactly and that every existing
  screen→plan coordinate conversion (placement drop, nudge, room drawing, hit tests) goes
  through one shared function that accounts for the transform.
- Zoom range 1× (fit) to 8×. Controls:
  - `+` / `−` / `Fit` buttons in the toolbar (×1.25 about the view centre).
  - Wheel and trackpad pinch (browsers send pinch as `wheel` with `ctrlKey`): zoom about
    the pointer. Plain wheel also zooms, matching Topology. `preventDefault` on the stage
    only, so the page doesn't zoom.
  - Touch: two-pointer pinch via Pointer Events.
  - Keyboard: `+`/`=`, `-`, `0` (fit) when the stage has focus.
- Pan: drag on empty plan space (no device, no handle under the pointer). A press that
  moves less than 4 px is a click, so Draw-room clicks still add corners; a press that
  moves more pans and adds no corner. Space+drag pans in every mode.
- Marker radius, stroke widths and label sizes stay constant on screen (divide by zoom),
  so devices don't become huge at 8×.
- The transform is per page view, not saved.

### 4.2 Search
- Same combobox as Topology. Endpoint: `GET /floorplan/search?q=` returning devices (same
  matcher, same caps as §3.4) **plus** rooms whose name contains `q` (case-insensitive),
  each tagged `kind: "device" | "room"`.
- Picking a placed device: zoom to it (at least 3×), centre it, select it, pulse it.
- Picking a roaming device or one waiting in a room's list: zoom to fit its room, highlight
  the room and scroll its tray entry into view and highlight it.
- Picking an unassigned device: scroll to and highlight its tray entry.
- Picking a room: zoom to fit the room and highlight it.

### 4.3 Tests and docs
- JS unit-level checks are not in the project today; cover behaviour in Playwright:
  zoom buttons change scale; a drop at 3× zoom lands at the right plan coordinates
  (compare the saved `pos_x/pos_y` to the expected point); room corners clicked at 2×
  are stored correctly; drag pans without adding a corner; search for a placed device
  centres it; search for a room highlights it.
- All existing Phase 2 browser tests pass unchanged.
- README Floor Plan section: navigation.

## 5. Floor Plan: blank plan and scale (issue 3)

### 5.1 Data model (schema v5, `store/migrations/0005_plan_scale.sql`)
- `floor_plan` changes. SQLite can't drop NOT NULL, so the migration rebuilds the table
  (create new, copy, drop, rename) inside the migration transaction:
  - `sha256` and `file_name` become nullable. A row with both NULL is a **blank plan**;
    both set is an image plan; one set and one NULL is invalid (CHECK).
  - `width_mm` REAL NULL, CHECK `width_mm > 0`: the real-world width of the whole plan.
    NULL means "no scale" (an uncalibrated image).
  - `units` TEXT NOT NULL DEFAULT `'imperial'` CHECK in (`'imperial'`, `'metric'`): display
    units only; everything is stored in mm or plan coordinates.
- For a blank plan, `width` and `height` are the plan size **in mm** (integers) and
  `width_mm = width`. For an image plan they stay the image's pixel size.
- Coordinates everywhere stay normalized `[0, 1]` (rooms, device points, shapes). A
  length on the plan converts to mm as
  `mm = sqrt((dx * width_mm)^2 + (dy * width_mm * height / width)^2)` for normalized
  `dx, dy`. Put this in `floorplan/geometry.py` with tests.
- Existing v4 rows: copied unchanged, `width_mm` NULL, `units` `'imperial'`.
- `SCHEMA_VERSION` = 5; the migration ships in the wheel (`test_packaging`).
- `setup-alpine.sh` already backs up `atlas.db` before an upgrade; nothing new needed.

### 5.2 Service and undo
- `Service.create_blank_plan(actor, *, width_mm, height_mm, units)`: only when no plan row
  exists (else `ValueError`). Width and height between 1 m and 300 m (else `ValueError`).
  Logged and undoable; undo deletes the row, redo re-creates it.
- `Service.set_plan_scale(actor, *, width_mm)` and `Service.set_plan_units(actor, units)`:
  logged field writes on `floor_plan`, undoable. `set_plan_scale` is refused on a blank
  plan (its scale is its size).
- `set_floor_plan` (image upload) on a **blank** plan turns it into an image plan:
  `sha256`, `file_name`, `width`, `height` become the image's; `width_mm` is kept (the
  drawing's real size carries over). The Phase 2 aspect rule applies against the blank
  plan's width/height, **except** when the plan has no rooms and no shapes, where any
  aspect is accepted (nothing would be distorted; the plan's real depth then follows the
  image's aspect from the kept `width_mm`). Undo restores the blank plan exactly.
  **Owner decision (2026-11-13): refuse a mismatched aspect when rooms or shapes exist**;
  the user crops the image first. Fitting an image under an existing drawing (image
  offset/scale columns) is out of scope and can be added later without moving data.
- Today the first upload logs `sha256` with a NULL old value as the creation marker undo
  reads. A blank plan has NULL `sha256`, so creation can no longer be detected that way.
  The builder must pick an unambiguous creation marker for `floor_plan` (for example log
  `width` first with old NULL, or a dedicated `'*'` row as `device` and `link` creations
  use) and keep undo/redo exact for: first image upload (existing data on CT 210 was
  created with the old marker and must still undo), blank creation, image over blank,
  image replacement, scale change, units change — each for **undo and redo**. Redo of a
  pre-v5 snapshot re-inserts a row whose stored values lack `width_mm` and `units`:
  default them (`setdefault`, as Phase 2 Ruling 20 did for `roaming`).
- Undoing plan creation (blank or first image) is **refused as a conflict (409)** while
  any room polygon or `plan_shape` row exists that was written after it, following the
  existing rule that undo refuses rather than cascade-delete (Phase 1 Ruling 16). The user
  deletes the drawing first, or undoes those later batches.

### 5.3 UI
- With no plan, the page offers two choices: "Upload an image" (today's form) and
  "Start a blank plan" (width × depth in feet or metres, with a units choice). Submitting
  creates the plan and shows the page in Layout mode.
- A blank plan renders a grid instead of the image: 1 ft squares (imperial) or 0.5 m
  (metric), with a heavier line every 10 ft / 5 m. When zoomed out so squares are smaller
  than 6 screen px, only the heavy lines show.
- Image plan: a "Set scale" button. The user draws a line over the image (two clicks), types
  its real length (`14'6"`, `14 ft`, `174 in`, `4.4 m`, `440 cm`), and atlas stores
  `width_mm`. Redoing it replaces the scale; shapes and rooms keep their normalized
  positions, so displayed lengths change.
- Units toggle (ft / m) in the toolbar when a scale exists.
- Length parser and formatter in JS with Python twins only if the server needs them
  (it doesn't for v1). Formats: imperial shows `14' 6"` (nearest inch), metric `4.42 m`
  (cm precision).

### 5.4 API and MCP
- `floor_plan_view` / `get_floor_plan` gain `blank` (bool), `width_mm` (or null) and `units`.
  The image route returns 404 for a blank plan.
- No new MCP write tools.

### 5.5 Tests
- Migration: v4 DB with a plan migrates to v5 with values intact; v4 DB with no plan;
  CHECK constraints refuse half-set image fields.
- Service: blank create/undo/redo; image over blank with and without rooms (aspect rule);
  scale and units writes with undo; undo of a pre-v5 first upload (old marker) still works.
- Geometry: mm conversion on non-square plans.
- Browser: create a blank plan, see the grid, set units; set scale on an image plan and see
  a measured line's length.

## 6. Floor Plan: drawing (issues 4 and 5)

### 6.1 Data model (schema v6, `store/migrations/0006_plan_shape.sql`)
- `plan_shape`:
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT
  - `kind` TEXT NOT NULL CHECK in (`'wall'`, `'door'`, `'furniture'`, `'label'`)
  - `geometry` TEXT NOT NULL — JSON, by kind (normalized coordinates, all finite, each in
    `[0, 1]`):
    - `wall`: `{"points": [[x, y], …], "closed": bool}`, 2–500 points. A two-point wall is a
      segment; more is a polyline.
    - `door`: `{"x", "y", "angle", "width", "flip"}`: hinge-side centre on the wall line,
      `angle` in degrees in **aspect-corrected** space (so 90° looks square on a
      non-square plan), `width` as a fraction of the plan width, `flip` 0–3 (which side it
      swings to and which side the hinge is on).
    - `furniture`: either `{"rect": {"cx", "cy", "w", "h", "angle"}}` (w, h as fractions of
      plan width, angle in aspect-corrected degrees) or `{"points": [[x, y], …]}` (3–500
      points, closed outline).
    - `label`: `{"x", "y", "size"}` (size as a fraction of plan width).
  - `label` TEXT NULL — furniture name or label text, stripped, ≤ 80 characters; required
    for `kind = 'label'`.
  - `created_at` TEXT NOT NULL
- Shapes don't reference rooms or each other (no FK). A door "on a wall" is a drawing
  convention the UI helps with by snapping; moving a wall doesn't move its doors (v1).
- Cap: 5,000 shapes per plan.

### 6.2 Service and undo
- `Service.save_shapes(actor, *, creates, updates, deletes, based_on)` — one transaction,
  one batch, everything validated before anything is written (`ValueError`,
  `KeyError` for an unknown id). `based_on` is the newest batch id that touched any
  `plan_shape` row when the page loaded; if a newer one exists → 409 with a reload
  message. Use a `StaleWrite` subclass (e.g. `StaleShapes`) with its own message, since
  `StaleWrite`'s text and MCP's `error_message` hint are about device placements.
- Creation logs a `'*'` row (snapshot) so undo deletes and redo re-inserts with the same
  id; update logs `geometry`/`label` field changes; delete logs a `'*'` snapshot. Undo and
  undo-of-undo follow the existing machinery and conflict rules.
- `shapes` added to `floor_plan_view`.
- Page route `POST /floorplan/shapes` (JSON, CSRF header, Origin check, like
  `/floorplan/placements`). Request models use `extra="forbid"`, strict types and caps
  (Phase 2 Ruling 42 pattern).

### 6.3 Drawing UI (issue 4: walls and doors; issue 5: furniture, labels, areas)
- A **Draw** mode beside Place and Draw room, with a tool picker: Select, Wall, Polyline,
  Door, Furniture (rectangle / outline), Label. Edits stay in memory until "Save
  changes" (the existing button) sends one `POST /floorplan/shapes`; "Discard" reverts.
  The `beforeunload` guard covers unsaved shapes.
- **Wall:** click start, click end → one segment. **Polyline:** each click adds a corner;
  double-click or Enter ends it; clicking the first point closes it; Escape cancels.
- **Typed length:** after the first point, typing a length (§5.3 formats) and Enter places
  the next point at exactly that length along the current pointer direction. Shown in a
  small input near the cursor. Only when the plan has a scale.
- **Angle lock:** Shift locks the segment to 0°/45°/90° (aspect-corrected).
- **Snapping** (toggle in the toolbar, on by default; Alt suppresses it while held), in
  order: existing wall endpoints and room corners within 8 screen px, then the grid (blank
  plans, or image plans with a scale). Room drawing (Draw room) also snaps to wall
  endpoints.
- **Door:** hover a wall to preview a door aligned to it at the default width (80 cm /
  32 in); click to place; in Select, a selected door has width handles and a flip
  control (cycles `flip`). Drawn as a gap in the wall stroke plus a quarter-circle swing.
- **Furniture:** rectangle by drag (corner to corner) with a rotate handle; outline by
  clicks like a closed polyline; optional name. Light fill, drawn **below** rooms' labels
  and device markers so it never hides a device or eats its clicks (pointer events off
  for shapes outside Draw mode).
- **Label:** click to place, type text.
- **Select:** click a shape to select; drag to move; drag a wall corner or rectangle handle
  to edit; Delete/Backspace removes; arrow keys nudge (same step as device nudge).
- **Measurements shown** (when the plan has a scale): live length while drawing; a length
  label on each wall segment (toolbar toggle "Show lengths", default on); width × depth on
  rectangles; each room's approximate area (shoelace in aspect-corrected units × scale²) in
  its label, as `≈ 180 sq ft` / `≈ 16.7 m²`.
- **Layer order** (bottom to top): image or grid, furniture, walls and doors, rooms,
  labels, device markers.
- Outside Draw mode, shapes are display only.

### 6.4 Split between issue 4 and issue 5
- Issue 4: migration v6, `save_shapes` + undo, route, Draw mode shell with Select, Wall,
  Polyline, Door, typed lengths, angle lock, snapping (including room-corner snapping to
  walls), wall length labels.
- Issue 5: Furniture (rectangle with rotation, outline), Label, room areas, furniture
  dimensions, MCP summary (§6.6), README drawing section and screenshot.

### 6.5 Tests
- Service: validation per kind (bounds, point counts, caps, label rules), stale write,
  create/update/delete undo and redo exact, conflict when a later batch touched the shape.
- Geometry: aspect-corrected angles and lengths, shoelace area.
- Browser: draw a two-point wall with a typed length on a blank plan and check the stored
  points; polyline close; Shift angle lock; snap to an endpoint; place a door on a wall;
  move and delete a shape; save, reload, shapes persist; undo from History removes them;
  a rectangle with rotation; a label; room area text.

### 6.6 MCP
- `get_floor_plan` adds `blank`, `width_mm`, `units`, and a `drawing` summary: counts by kind
  and the furniture/label names (no geometry). `INSTRUCTIONS` says the drawing is
  page-only and visual; rooms remain the polygons.

## 7. Privacy

- The repo is public. Drawn shapes, furniture names, labels and dimensions describe the
  owner's house and are personal data: they live in the database only.
- Fixtures and screenshots use the fake house with generic names and synthetic shapes.
- `check_leaks` must pass on every PR; the existing denylist mechanism
  (`LANSCAPE_LEAK_DENYLIST`) covers room names.

## 8. Approaches considered

- **Floor Plan editor technology.** (A) extend the hand-written SVG/JS, split into several
  plain script files with no build step — **chosen**: keeps Phase 2's code and browser
  tests, no new vendored code. (B) vendor svg.js/panzoom — saves a few hundred lines but
  adds a second paradigm beside the existing code. (C) move to a canvas library
  (Konva/Fabric) — free transform handles but rewrites rooms and placement.
- **Coordinates for measurements.** (a) keep normalized coordinates and add a plan scale
  (`width_mm`) — **chosen**: no Phase 2/3 data moves. (b) convert everything to mm —
  migrates every row and makes an uncalibrated image awkward.
- **Storing drawings.** (a) `plan_shape` rows through the change log — **chosen**: every
  user edit stays undoable, MCP can summarize. (b) one JSON blob, not logged — breaks the
  undo rule.
- **Drawing semantics.** (A) drawing is picture only, rooms stay polygons — **chosen**.
  (B) walls define rooms — needs closed-region detection and reworks rooms on every wall
  move; too much for the value.
- **Topology.** One sensible way: Cytoscape's own zoom API, server-side colour and edge
  rules so API and page agree.

## 9. Delivery

Five `Prompt:` issues. A docs-only PR adds this spec first; each issue links its sections.

| # | Issue | Runs after | Sections |
|---|---|---|---|
| 1 | Topology: help, + / −, dashed-edge hiding, place colours, search | none | §3 |
| 2 | Floor Plan: zoom, pan, search | none | §4 |
| 3 | Floor Plan: blank plan, scale, units (schema v5) | 2 | §5 |
| 4 | Floor Plan: drawing — walls, polylines, doors, typed lengths, snapping (schema v6) | 3 | §6.1–6.4 (issue 4 part) |
| 5 | Floor Plan: furniture, labels, areas, MCP summary | 4 | §6.3–6.6 (issue 5 part) |

Issues 1 and 2 touch different pages and can run in parallel; both edit `app.css`,
`README.md` and possibly `base.html`, so the queue run should expect small merge
conflicts there. Issues 2–5 all edit the floor plan JS and template and stack in order.

## 10. Left to the builder

- The exact zoom mechanism (CSS transform vs `viewBox`), as long as §4.1's shared
  coordinate conversion holds.
- How `floorplan.js` is split into files.
- The palette's exact colours (must pass contrast against label text and both borders in
  light and dark).
- The `floor_plan` creation marker (§5.2), under the stated undo requirements.
- Default door width rounding, handle sizes, pulse animation.
- Whether the search combobox is shared code between the two pages.
