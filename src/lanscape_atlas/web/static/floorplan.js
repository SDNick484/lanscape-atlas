/* lanscape-atlas floor plan page (Phase 2). Plain ES2017, no libraries, no build step.
 *
 * Data comes from <script type="application/json" id="fp-data"> (floor_plan_view
 * without the file name). Everything in it is untrusted: the DOM is built with
 * createElement/createElementNS and textContent only, never innerHTML.
 *
 * Coordinates are normalized to [0, 1]; the SVG viewBox is the plan's pixel size,
 * so a point is drawn at (x * width, y * height). Zoom and pan live in
 * floorplan-view.js (loaded first); every client -> plan conversion goes through
 * its LanscapeFP.view.toPlan, and marker, corner and label sizes are divided by the
 * zoom so they stay the same size on screen.
 *
 * Place mode: drag a device from the list onto a room (the first room by id whose
 * polygon contains the drop point); drag a placed device within its room, or nudge
 * it with the arrow keys. A drop or nudge outside the room is reverted. Edits stay
 * in memory until "Save changes" sends one POST /floorplan/placements; a 409 (stale)
 * reloads the page with the server's message.
 *
 * Layout mode: click to add corners, close on the first corner or "Close room",
 * then name the room; the plain HTML form posts it to /floorplan/rooms. A new blank
 * plan opens in Layout mode (?layout=1).
 *
 * Blank plan (spec §5.3): #plan-grid stands in for the image, with 1 ft (or 0.5 m)
 * squares and a heavier line every 10 ft (or 5 m); the plan's width and height are mm.
 * When a square would be under 6 screen px only the heavy lines show
 * (data-grid="coarse" on the stage, "fine" otherwise).
 *
 * Scale mode (image plans): click two points, type the real distance between them
 * (LanscapeFP.units.parse), and the page posts the plan's real width to
 * /floorplan/scale. A length that does not parse shows a message and saves nothing.
 *
 * Draw mode (spec §6.3, floorplan-draw.js): walls and doors drawn over the plan. This
 * file owns the mode switch and the Save / Discard buttons; LanscapeFP.drawing owns the
 * shapes, their tools and their SVG groups, which drawPlan stacks as: shapes under the
 * rooms, wall lengths over the room labels, drafts and handles on top. Placement edits
 * and shape edits are never pending together: while either is unsaved the other mode's
 * button is disabled, so "Save changes" posts one or the other.
 *
 * LanscapeFP.page.revealDevice / revealRoom / clearFound are what the search box
 * (floorplan-search.js) calls when a result is picked: they move the view, select or
 * pulse a marker, and highlight the room and the tray entry.
 */
(function () {
  "use strict";

  var app = document.getElementById("fp-app");
  var dataEl = document.getElementById("fp-data");
  if (!app || !dataEl) {
    return;
  }
  var ROOT = app.getAttribute("data-root") || "";
  var CSRF = app.getAttribute("data-csrf") || "";
  var SVGNS = "http://www.w3.org/2000/svg";
  var EPS = 1e-12; // same as lanscape_atlas.floorplan.geometry._EPS
  var NUDGE = 0.005;
  var UNNAMED = "(unnamed)";

  var FP = (window.LanscapeFP = window.LanscapeFP || {});
  var view = FP.view || null; // null when there is no plan yet
  var DATA = JSON.parse(dataEl.textContent || "{}");
  var plan = DATA.plan || null;
  var svg = document.getElementById("plan-svg");
  var statusEl = document.getElementById("fp-status");
  var saveBtn = document.getElementById("save-changes");
  var discardBtn = document.getElementById("discard-changes");
  var layoutBtn = document.getElementById("mode-layout");
  var scaleBtn = document.getElementById("mode-scale");
  var drawBtn = document.getElementById("mode-draw");
  var gridEl = document.getElementById("plan-grid");
  var sizeEl = document.getElementById("fp-size");
  var UNITS = FP.units;
  var GRID_MIN_PX = 6; // below this many screen px per square only the heavy lines show
  var trayEl = document.getElementById("tray");
  var filterEl = document.getElementById("tray-filter");

  var rooms = []; // [{id, name, polygon}] in id order
  var roomById = {};
  var devices = {}; // id -> {id, name, online, place_id, x, y, roaming, last_batch}
  var order = []; // device ids, ascending
  var dirty = {}; // id -> true
  var selected = null;
  var mode = "place";
  var draft = [];
  var draftClosed = false;
  var foundRoom = null; // place id highlighted by a search pick
  var foundTray = null; // device id whose tray entry a search pick highlighted
  var draw = null; // the Draw mode instance (floorplan-draw.js) once there is a plan

  // ----------------------------------------------------------- geometry
  // Port of floorplan/geometry.py: points on an edge or vertex count as inside.

  function onSegment(x, y, a, b) {
    var cross = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]);
    if (Math.abs(cross) > EPS) {
      return false;
    }
    return (
      Math.min(a[0], b[0]) - EPS <= x && x <= Math.max(a[0], b[0]) + EPS &&
      Math.min(a[1], b[1]) - EPS <= y && y <= Math.max(a[1], b[1]) + EPS
    );
  }

  function pointInPolygon(x, y, poly) {
    var n = poly.length;
    var i;
    for (i = 0; i < n; i++) {
      if (onSegment(x, y, poly[i], poly[(i + 1) % n])) {
        return true;
      }
    }
    var inside = false;
    var j = n - 1;
    for (i = 0; i < n; i++) {
      var xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) {
        inside = !inside;
      }
      j = i;
    }
    return inside;
  }

  function round4(v) { return Math.round(v * 10000) / 10000; }

  function clamp01(v) { return Math.min(1, Math.max(0, v)); }

  function polygonOf(placeId) {
    var r = roomById[placeId];
    return r && Array.isArray(r.polygon) && r.polygon.length >= 3 ? r.polygon : null;
  }

  function roomAt(x, y) {
    for (var i = 0; i < rooms.length; i++) {
      var poly = polygonOf(rooms[i].id);
      if (poly && pointInPolygon(x, y, poly)) {
        return rooms[i];
      }
    }
    return null;
  }

  // ------------------------------------------------------------- state

  function label(name) {
    var s = typeof name === "string" ? name.trim() : "";
    return s || UNNAMED;
  }

  function addDevice(d, extra) {
    var dev = {
      id: d.device_id,
      name: d.name,
      online: d.online !== false,
      place_id: d.place_id === undefined ? null : d.place_id,
      x: null,
      y: null,
      roaming: false,
      set_by: d.set_by || null,
      set_at: d.set_at || null,
      last_batch: d.last_batch || 0,
      heard: d.heard && typeof d.heard === "object" ? d.heard : null,
    };
    Object.keys(extra).forEach(function (k) { dev[k] = extra[k]; });
    devices[dev.id] = dev;
    order.push(dev.id);
  }

  function load() {
    (DATA.rooms || []).forEach(function (r) {
      rooms.push(r);
      roomById[r.id] = r;
    });
    (DATA.placed || []).forEach(function (d) { addDevice(d, { x: d.x, y: d.y }); });
    (DATA.roaming || []).forEach(function (d) { addDevice(d, { roaming: true }); });
    (DATA.unplaced || []).forEach(function (d) { addDevice(d, {}); });
    order.sort(function (a, b) { return a - b; });
  }

  function onPlan(d) {
    return d.place_id !== null && !d.roaming && d.x !== null && d.y !== null;
  }

  function markDirty(d) {
    dirty[d.id] = true;
    refreshDirty();
  }

  function unsaved() {
    return Object.keys(dirty).length + (draw ? draw.dirtyCount() : 0);
  }

  // Save / Discard follow the pending edits (placements or shapes, never both); the
  // mode buttons are disabled while any are pending.
  function refreshDirty() {
    var n = unsaved();
    saveBtn.disabled = !n;
    discardBtn.disabled = !n;
    [layoutBtn, scaleBtn, drawBtn].forEach(function (b) {
      if (b) {
        b.disabled = !!n;
        if (n) {
          b.title = "Save or discard your changes first";
        } else {
          b.removeAttribute("title");
        }
      }
    });
    setStatus(n ? n + " unsaved change" + (n === 1 ? "" : "s") + "." : "");
  }

  function setPoint(d, placeId, x, y) {
    d.place_id = placeId;
    d.x = x;
    d.y = y;
    d.roaming = false;
    markDirty(d);
  }

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.classList.toggle("error-text", !!isError);
  }

  // ----------------------------------------------------------- drawing

  function svgEl(tag, attrs) {
    var el = document.createElementNS(SVGNS, tag);
    Object.keys(attrs).forEach(function (k) { el.setAttribute(k, String(attrs[k])); });
    return el;
  }

  function unit() {
    return Math.max(plan.width, plan.height) / 100;
  }

  // A size of k units in plan pixels that stays the same on screen at any zoom.
  function size(k) {
    return (unit() * k / (view ? view.zoom() : 1)).toFixed(3);
  }

  // ---------------------------------------------------- blank plan grid

  // Lines every `step` mm across a W x H mm plan, skipping multiples of `skip`.
  function gridLines(g, step, skip) {
    var W = plan.width, H = plan.height, k, v;
    for (k = 0; k * step <= W + 1e-6; k += 1) {
      v = k * step;
      if (!skip || Math.abs(v / skip - Math.round(v / skip)) > 1e-6) {
        g.appendChild(svgEl("line", { x1: v.toFixed(1), y1: 0, x2: v.toFixed(1), y2: H }));
      }
    }
    for (k = 0; k * step <= H + 1e-6; k += 1) {
      v = k * step;
      if (!skip || Math.abs(v / skip - Math.round(v / skip)) > 1e-6) {
        g.appendChild(svgEl("line", { x1: 0, y1: v.toFixed(1), x2: W, y2: v.toFixed(1) }));
      }
    }
  }

  function drawGrid() {
    if (!gridEl || !plan || !plan.blank || !UNITS) {
      return;
    }
    var step = UNITS.grid(plan.units);
    var minor = gridEl.querySelector(".fp-grid-minor");
    var major = gridEl.querySelector(".fp-grid-major");
    clear(minor);
    clear(major);
    gridLines(minor, step.minor, step.major);
    gridLines(major, step.major, 0);
    major.appendChild(svgEl("rect", { "class": "fp-grid-edge", x: 0, y: 0, width: plan.width, height: plan.height }));
    updateGrid();
  }

  // Hide the 1 ft / 0.5 m lines when a square is under GRID_MIN_PX on screen.
  function updateGrid() {
    if (!gridEl || !plan || !plan.blank || !UNITS || !view) {
      return;
    }
    var shown = gridEl.getBoundingClientRect().width; // includes the zoom
    var px = UNITS.grid(plan.units).minor / plan.width * shown;
    var fine = px >= GRID_MIN_PX;
    gridEl.querySelector(".fp-grid-minor").setAttribute("display", fine ? "inline" : "none");
    view.stage.setAttribute("data-grid", fine ? "fine" : "coarse");
  }

  // The plan's real size in the toolbar (when it has a scale).
  function showSize() {
    if (!sizeEl || !plan || !UNITS || !(plan.width_mm > 0)) {
      return;
    }
    var depth = plan.width_mm * plan.height / plan.width;
    sizeEl.textContent = UNITS.format(plan.width_mm, plan.units) + " \u00d7 " + UNITS.format(depth, plan.units);
  }

  // Re-size markers, draft corners and room labels after a zoom change.
  function applySizes() {
    updateGrid();
    if (!svg || !plan) {
      return;
    }
    if (draw) {
      draw.redraw();
    }
    var i, els = svg.querySelectorAll("circle.fp-dev");
    for (i = 0; i < els.length; i += 1) { els[i].setAttribute("r", size(0.9)); }
    els = svg.querySelectorAll(".fp-draft circle");
    for (i = 0; i < els.length; i += 1) {
      els[i].setAttribute("r", size(els[i].classList.contains("fp-draft-start") ? 1.1 : 0.5));
    }
    els = svg.querySelectorAll("text.fp-room-label");
    for (i = 0; i < els.length; i += 1) { els[i].setAttribute("font-size", size(1.8)); }
    els = svg.querySelectorAll("text.fp-room-area");
    for (i = 0; i < els.length; i += 1) {
      els[i].setAttribute("font-size", size(1.3));
      els[i].setAttribute("dy", size(2.1));
    }
  }

  function clear(el) {
    while (el.firstChild) {
      el.removeChild(el.firstChild);
    }
  }

  function markerClass(d) {
    var cls = "fp-dev";
    if (!d.online) { cls += " offline"; }
    if (selected === d.id) { cls += " selected"; }
    if (dirty[d.id]) { cls += " dirty"; }
    return cls;
  }

  // "≈ 180 sq ft" for a room polygon on a plan with a scale; "" without a scale, or for
  // an outline whose edges cross itself (no meaningful area).
  function roomArea(poly) {
    if (!UNITS || !FP.drawing || !(plan.width_mm > 0)) {
      return "";
    }
    var mm2 = FP.drawing.geom.polygonAreaMm2(poly, plan.height / plan.width, plan.width_mm);
    return mm2 === null ? "" : UNITS.formatArea(mm2, plan.units);
  }

  function drawPlan() {
    if (!svg || !plan) {
      return;
    }
    var W = plan.width, H = plan.height;
    clear(svg);
    if (draw) {
      svg.appendChild(draw.layers.shapes);
    }
    var gRooms = svgEl("g", { "class": "fp-rooms" });
    var gLabels = svgEl("g", { "class": "fp-labels" });
    rooms.forEach(function (r) {
      var poly = polygonOf(r.id);
      if (!poly) {
        return;
      }
      var pts = poly.map(function (p) { return (p[0] * W).toFixed(2) + "," + (p[1] * H).toFixed(2); });
      var sel = selected !== null ? devices[selected] : null;
      var heardHere = !!(sel && sel.heard && sel.heard.place_id === r.id);
      var cls = "fp-room" + (heardHere ? " room--heard" : "") + (foundRoom === r.id ? " fp-room--found" : "");
      gRooms.appendChild(svgEl("polygon", { "class": cls, "data-room-id": r.id, points: pts.join(" ") }));
      var cx = 0, cy = 0;
      poly.forEach(function (p) { cx += p[0]; cy += p[1]; });
      var text = svgEl("text", {
        "class": "fp-room-label",
        x: (cx / poly.length * W).toFixed(2),
        y: (cy / poly.length * H).toFixed(2),
        "font-size": size(1.8),
      });
      text.textContent = r.name;
      gLabels.appendChild(text);
      var area = roomArea(poly);
      if (area) {
        var at = svgEl("text", {
          "class": "fp-room-area",
          "data-room-id": r.id,
          x: (cx / poly.length * W).toFixed(2),
          y: (cy / poly.length * H).toFixed(2),
          dy: size(2.1),
          "font-size": size(1.3),
        });
        at.textContent = area;
        gLabels.appendChild(at);
      }
    });
    var gDevs = svgEl("g", { "class": "fp-devices" });
    order.forEach(function (id) {
      var d = devices[id];
      if (!onPlan(d)) {
        return;
      }
      var c = svgEl("circle", {
        "class": markerClass(d),
        "data-device-id": d.id,
        cx: (d.x * W).toFixed(2),
        cy: (d.y * H).toFixed(2),
        r: size(0.9),
        tabindex: 0,
        role: "button",
        "aria-label": label(d.name),
      });
      var title = svgEl("title", {});
      title.textContent = label(d.name);
      c.appendChild(title);
      gDevs.appendChild(c);
    });
    svg.appendChild(gRooms);
    svg.appendChild(gLabels);
    if (draw) {
      svg.appendChild(draw.layers.texts);
      svg.appendChild(draw.layers.lengths);
    }
    svg.appendChild(gDevs);
    svg.appendChild(drawDraft());
    if (draw) {
      svg.appendChild(draw.layers.overlay);
    }
  }

  function drawDraft() {
    var g = svgEl("g", { "class": "fp-draft" });
    if (!draft.length) {
      return g;
    }
    var W = plan.width, H = plan.height;
    var pts = draft.map(function (p) { return (p[0] * W).toFixed(2) + "," + (p[1] * H).toFixed(2); });
    g.appendChild(svgEl(draftClosed ? "polygon" : "polyline", { points: pts.join(" ") }));
    draft.forEach(function (p, i) {
      var start = i === 0 && draft.length >= 3 && !draftClosed;
      g.appendChild(svgEl("circle", {
        "class": start ? "fp-draft-start" : "fp-draft-pt",
        cx: (p[0] * W).toFixed(2),
        cy: (p[1] * H).toFixed(2),
        r: size(start ? 1.1 : 0.5),
      }));
    });
    return g;
  }

  function marker(id) {
    return svg ? svg.querySelector('[data-device-id="' + id + '"]') : null;
  }

  function updateMarker(d) {
    var c = marker(d.id);
    if (!c) {
      return;
    }
    c.setAttribute("cx", (d.x * plan.width).toFixed(2));
    c.setAttribute("cy", (d.y * plan.height).toFixed(2));
    c.setAttribute("class", markerClass(d));
  }

  function drawTrays() {
    if (!trayEl) {
      return;
    }
    clear(trayEl);
    var needle = filterEl ? filterEl.value.trim().toLowerCase() : "";
    var groups = rooms.map(function (r) { return { room: r, name: r.name, items: [], heardOnly: [] }; });
    var byRoom = {};
    groups.forEach(function (g) { byRoom[g.room.id] = g; });
    var unassigned = { room: null, name: "Unassigned", items: [], heardOnly: [] };
    var total = 0;
    order.forEach(function (id) {
      var d = devices[id];
      if (onPlan(d)) {
        return;
      }
      total += 1;
      if (needle && label(d.name).toLowerCase().indexOf(needle) === -1) {
        return;
      }
      var g = d.place_id !== null && byRoom[d.place_id] ? byRoom[d.place_id] : unassigned;
      g.items.push(d);
    });
    // Devices heard in a room that are not already in that room's tray (placed or
    // trayed elsewhere): a non-draggable "heard" entry in the room they were heard in.
    order.forEach(function (id) {
      var d = devices[id];
      var h = d.heard;
      var g = h && h.place_id !== null && h.place_id !== undefined ? byRoom[h.place_id] : null;
      if (!g || (!onPlan(d) && d.place_id === h.place_id)) {
        return;
      }
      if (needle && label(d.name).toLowerCase().indexOf(needle) === -1) {
        return;
      }
      g.heardOnly.push(d);
    });
    groups.push(unassigned);
    var shown = 0;
    groups.forEach(function (g) {
      if (!g.items.length && !g.heardOnly.length) {
        return;
      }
      shown += g.items.length + g.heardOnly.length;
      var box = document.createElement("div");
      box.className = "fp-group";
      var h = document.createElement("h3");
      h.textContent = g.name;
      var ul = document.createElement("ul");
      g.items.forEach(function (d) { ul.appendChild(trayItem(d, g.room !== null, g.room ? g.room.id : null)); });
      g.heardOnly.forEach(function (d) { ul.appendChild(heardItem(d)); });
      box.appendChild(h);
      box.appendChild(ul);
      trayEl.appendChild(box);
    });
    if (!shown) {
      var p = document.createElement("p");
      p.className = "empty";
      p.textContent = total ? "No devices match the filter." : "Every device is on the plan.";
      trayEl.appendChild(p);
    }
  }

  function heardItem(d) {
    var li = document.createElement("li");
    li.className = "fp-item fp-heard-only";
    li.setAttribute("data-heard-device", String(d.id));
    var name = document.createElement("span");
    name.className = "fp-name";
    name.textContent = label(d.name);
    var chip = document.createElement("span");
    chip.className = "chip heard";
    chip.title = "Heard here by a Bluetooth scanner";
    chip.textContent = "heard";
    li.appendChild(name);
    li.appendChild(chip);
    return li;
  }

  function trayItem(d, inRoom, roomId) {
    var li = document.createElement("li");
    li.className = "fp-item" + (dirty[d.id] ? " dirty" : "") + (d.online ? "" : " offline") +
      (foundTray === d.id ? " fp-found" : "");
    li.setAttribute("data-tray-device", String(d.id));
    var name = document.createElement("span");
    name.className = "fp-name";
    name.textContent = label(d.name);
    li.appendChild(name);
    if (roomId !== null && d.heard && d.heard.place_id === roomId) {
      var chip = document.createElement("span");
      chip.className = "chip heard";
      chip.title = "Heard here by a Bluetooth scanner";
      chip.textContent = "heard";
      li.appendChild(chip);
    }
    if (d.roaming && !dirty[d.id] && d.set_by) {
      var stamp = document.createElement("small");
      stamp.className = "set-by";
      var when = typeof d.set_at === "string" ? d.set_at.slice(0, 16).replace("T", " ") : "";
      stamp.textContent = "last set by " + d.set_by + (when ? " at " + when : "");
      li.appendChild(stamp);
    }
    if (inRoom) {
      var lab = document.createElement("label");
      lab.className = "fp-roam";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !!d.roaming;
      box.setAttribute("data-roaming-for", String(d.id));
      box.disabled = !saveBtn || mode !== "place"; // no plan yet, or not placing
      lab.appendChild(box);
      lab.appendChild(document.createTextNode(" roaming"));
      li.appendChild(lab);
    }
    return li;
  }

  function render() {
    drawPlan();
    drawTrays();
    showSelected();
  }

  // --------------------------------------------------------- selection

  function select(id) {
    var prev = selected;
    selected = id;
    [prev, id].forEach(function (x) {
      if (x !== null && devices[x]) { updateMarker(devices[x]); }
    });
    showSelected();
    updateHeardRoom();
  }

  // Highlight the room the selected device was last heard in (clear it otherwise).
  function updateHeardRoom() {
    if (!svg) {
      return;
    }
    var sel = selected !== null ? devices[selected] : null;
    var want = sel && sel.heard && sel.heard.place_id !== null ? String(sel.heard.place_id) : null;
    var polys = svg.querySelectorAll("polygon.fp-room");
    for (var i = 0; i < polys.length; i += 1) {
      polys[i].classList.toggle("room--heard", want !== null && polys[i].getAttribute("data-room-id") === want);
    }
  }

  function showSelected() {
    var box = document.getElementById("fp-selected");
    var d = selected !== null ? devices[selected] : null;
    if (!box) {
      return;
    }
    if (!d || !onPlan(d)) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    document.getElementById("fp-sel-name").textContent = label(d.name);
    document.getElementById("fp-sel-link").setAttribute("href", ROOT + "/devices/" + d.id);
  }

  // ------------------------------------------------- pointer: plan markers

  // Client point of a pointer event -> normalized plan point, under the view transform.
  function planPoint(evt) {
    return view.toPlan(evt.clientX, evt.clientY);
  }

  var stopTracking = null; // cancels the drag in progress, if any

  function trackPointer(onMove, onUp, onCancel) {
    function off() {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", cancel);
      stopTracking = null;
    }
    function up(e) {
      off();
      onUp(e);
    }
    function cancel() {
      off();
      if (onCancel) {
        onCancel();
      }
      render();
    }
    if (stopTracking) {
      stopTracking();
    }
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", cancel);
    stopTracking = cancel;
  }

  function dragMarker(evt, d) {
    var c = marker(d.id);
    var moved = false;
    evt.preventDefault();
    c.focus();
    select(d.id);
    trackPointer(function (e) {
      var p = planPoint(e);
      moved = true;
      c.setAttribute("cx", (clamp01(p[0]) * plan.width).toFixed(2));
      c.setAttribute("cy", (clamp01(p[1]) * plan.height).toFixed(2));
    }, function (e) {
      if (!moved) {
        return;
      }
      var p = planPoint(e);
      var x = round4(clamp01(p[0])), y = round4(clamp01(p[1]));
      var poly = polygonOf(d.place_id);
      if (poly && pointInPolygon(x, y, poly)) {
        setPoint(d, d.place_id, x, y);
      } else {
        setStatus("A device stays inside its room: moved back.", true);
      }
      updateMarker(d);
    });
  }

  // ------------------------------------------------- pointer: tray items

  function dragFromTray(evt, d) {
    var ghost = document.createElement("div");
    ghost.className = "fp-ghost";
    ghost.textContent = label(d.name);
    ghost.style.left = evt.clientX + "px";
    ghost.style.top = evt.clientY + "px";
    document.body.appendChild(ghost);
    evt.preventDefault();
    trackPointer(function (e) {
      ghost.style.left = e.clientX + "px";
      ghost.style.top = e.clientY + "px";
    }, function (e) {
      ghost.remove();
      dropOnPlan(d, e);
    }, function () {
      ghost.remove();
    });
  }

  function dropOnPlan(d, e) {
    if (!svg || !view || mode !== "place" || !view.inStage(e.clientX, e.clientY)) {
      return;
    }
    var p = planPoint(e);
    if (p[0] < 0 || p[0] > 1 || p[1] < 0 || p[1] > 1) {
      return;
    }
    var x = round4(p[0]), y = round4(p[1]);
    var room = roomAt(x, y);
    if (!room) {
      setStatus("Drop the device inside a room.", true);
      return;
    }
    setPoint(d, room.id, x, y);
    selected = d.id;
    render();
  }

  // ------------------------------------------------------------ keyboard

  var KEYS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

  function onKey(evt) {
    var d = selected !== null ? devices[selected] : null;
    if (!d || !onPlan(d) || mode !== "place") {
      return;
    }
    if (evt.key === "Escape") {
      select(null);
      return;
    }
    var step = KEYS[evt.key];
    if (!step) {
      return;
    }
    evt.preventDefault();
    var x = round4(clamp01(d.x + step[0] * NUDGE)), y = round4(clamp01(d.y + step[1] * NUDGE));
    var poly = polygonOf(d.place_id);
    if (poly && pointInPolygon(x, y, poly)) {
      setPoint(d, d.place_id, x, y);
      updateMarker(d);
    } else {
      setStatus("That would leave the room.", true);
    }
  }

  // -------------------------------------------------------------- layout

  // mode: "place", "layout" (draw a room), "scale" (set the plan's scale) or "draw"
  // (walls and doors, floorplan-draw.js).
  function setMode(next) {
    mode = next;
    // The drawing modes share the "layout" look: crosshair, devices dimmed and inert.
    app.classList.toggle("layout", mode !== "place");
    app.classList.toggle("drawing", mode === "draw");
    layoutBtn.setAttribute("aria-pressed", mode === "layout" ? "true" : "false");
    if (drawBtn) {
      drawBtn.setAttribute("aria-pressed", mode === "draw" ? "true" : "false");
    }
    if (draw) {
      draw.setActive(mode === "draw");
    }
    document.getElementById("room-draw").hidden = mode !== "layout";
    if (scaleBtn) {
      scaleBtn.setAttribute("aria-pressed", mode === "scale" ? "true" : "false");
      document.getElementById("scale-draw").hidden = mode !== "scale";
    }
    resetDraft();
    select(null);
    drawTrays(); // roaming checkboxes are only live while placing
    setStatus(
      mode === "layout" ? "Click on the plan to add the room's corners." :
      mode === "scale" ? "Click the two ends of something whose length you know." : ""
    );
  }

  function resetDraft() {
    draft = [];
    draftClosed = false;
    document.getElementById("room-form").hidden = true;
    document.getElementById("room-close").disabled = true;
    if (scaleBtn) {
      var input = document.getElementById("scale-length");
      input.value = "";
      input.disabled = true;
      document.getElementById("scale-save").disabled = true;
      document.getElementById("scale-error").textContent = "";
    }
    drawPlan();
  }

  function closeDraft() {
    if (draft.length < 3) {
      return;
    }
    draftClosed = true;
    document.getElementById("room-polygon").value = JSON.stringify(draft);
    document.getElementById("room-form").hidden = false;
    document.getElementById("room-close").disabled = true;
    drawPlan();
    document.getElementById("room-name").focus();
  }

  function onLayoutClick(evt) {
    if (mode !== "layout" || draftClosed) {
      return;
    }
    if (evt.target.classList && evt.target.classList.contains("fp-draft-start")) {
      closeDraft();
      return;
    }
    var p = planPoint(evt);
    // A corner near a drawn wall's corner takes it exactly, so rooms line up with walls.
    var onWall = draw ? draw.snapToWalls([clamp01(p[0]), clamp01(p[1])]) : null;
    draft.push(onWall || [round4(clamp01(p[0])), round4(clamp01(p[1]))]);
    document.getElementById("room-close").disabled = draft.length < 3;
    drawPlan();
  }

  // --------------------------------------------------------------- scale

  // Length of the drawn line as a fraction of the plan's width (aspect-corrected).
  function draftSpan() {
    var dx = draft[1][0] - draft[0][0], dy = draft[1][1] - draft[0][1];
    return Math.hypot(dx, dy * plan.height / plan.width);
  }

  function onScaleClick(evt) {
    if (mode !== "scale") {
      return;
    }
    var p = planPoint(evt);
    var pt = [clamp01(p[0]), clamp01(p[1])];
    draft = draft.length >= 2 ? [pt] : draft.concat([pt]);
    var input = document.getElementById("scale-length");
    var ready = draft.length === 2;
    input.disabled = !ready;
    document.getElementById("scale-save").disabled = !ready;
    document.getElementById("scale-error").textContent = "";
    drawPlan();
    if (ready) {
      var now = plan.width_mm > 0 && UNITS ? " It measures " + UNITS.format(draftSpan() * plan.width_mm, plan.units) + " at the current scale." : "";
      setStatus("Type the line's real length." + now);
      input.focus();
    } else {
      setStatus("Now click the other end.");
    }
  }

  function scaleError(text) {
    document.getElementById("scale-error").textContent = text;
    setStatus(text, true);
  }

  function saveScale() {
    if (mode !== "scale" || draft.length !== 2 || !UNITS) {
      return;
    }
    var got = UNITS.parse(document.getElementById("scale-length").value);
    if (got.error) {
      scaleError(got.error);
      return;
    }
    var span = draftSpan();
    if (!(span > 1e-4)) {
      scaleError("The two points are too close together: click two points further apart.");
      return;
    }
    var btn = document.getElementById("scale-save");
    btn.disabled = true;
    setStatus("Saving…");
    fetch(ROOT + "/floorplan/scale", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify({ width_mm: got.mm / span }),
    }).then(function (r) {
      return r.json().then(function (data) { return { r: r, data: data }; }, function () { return { r: r, data: null }; });
    }).then(function (res) {
      if (res.r.ok) {
        window.location.assign(ROOT + "/floorplan?saved=scale");
        return;
      }
      var detail = res.data && res.data.detail;
      scaleError(typeof detail === "string" ? detail : "Could not save the scale (HTTP " + res.r.status + ").");
      btn.disabled = false;
    }, function () {
      scaleError("Network error: the scale was not saved.");
      btn.disabled = false;
    });
  }

  // --------------------------------------------------------------- search

  function pulse(el) {
    el.classList.remove("fp-pulse");
    void el.getBoundingClientRect(); // restart the animation
    el.classList.add("fp-pulse");
    setTimeout(function () { el.classList.remove("fp-pulse"); }, 1000);
  }

  function showStage() {
    if (view) {
      view.stage.scrollIntoView({ block: "nearest" });
    }
  }

  function fitRoom(placeId) {
    var poly = polygonOf(placeId);
    if (!poly || !view) {
      return false;
    }
    var xs = poly.map(function (p) { return p[0]; }), ys = poly.map(function (p) { return p[1]; });
    view.fitBox(Math.min.apply(null, xs), Math.min.apply(null, ys), Math.max.apply(null, xs), Math.max.apply(null, ys));
    showStage();
    return true;
  }

  // Scroll the device's tray entry into view; clears the tray filter if it hides it.
  function showTrayEntry(id) {
    var sel = '[data-tray-device="' + id + '"]';
    var li = trayEl ? trayEl.querySelector(sel) : null;
    if (!li && filterEl && filterEl.value) {
      filterEl.value = "";
      drawTrays();
      li = trayEl.querySelector(sel);
    }
    if (li) {
      li.scrollIntoView({ block: "nearest" });
    }
  }

  function clearFound() {
    if (foundRoom === null && foundTray === null) {
      return;
    }
    foundRoom = null;
    foundTray = null;
    render();
  }

  function revealRoom(placeId) {
    var r = roomById[placeId];
    if (!r) {
      setStatus("That room is not on this page yet: reload to see it.", true);
      return;
    }
    foundRoom = r.id;
    foundTray = null;
    render();
    if (!fitRoom(r.id)) {
      setStatus("That room has no outline on the plan yet.", true);
    }
  }

  function revealDevice(id) {
    var d = devices[id];
    if (!d) {
      setStatus("That device is not on this page yet: reload to see it.", true);
      return;
    }
    foundRoom = null;
    foundTray = null;
    if (onPlan(d)) {
      render();
      if (view) {
        view.showPoint(d.x, d.y, 3);
        showStage();
      }
      if (mode === "place") {
        select(d.id);
      }
      var c = marker(d.id);
      if (c) {
        pulse(c);
      }
      return;
    }
    if (selected !== null) {
      select(null);
    }
    foundTray = d.id;
    var inRoom = d.place_id !== null && roomById[d.place_id];
    if (inRoom) {
      foundRoom = d.place_id;
    }
    render();
    if (inRoom) {
      fitRoom(d.place_id);
    }
    showTrayEntry(d.id);
  }

  FP.page = { revealDevice: revealDevice, revealRoom: revealRoom, clearFound: clearFound };

  // ---------------------------------------------------------------- save

  function post(path, body) {
    return fetch(ROOT + path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(function (data) { return { r: r, data: data }; }, function () { return { r: r, data: null }; });
    });
  }

  // One POST /floorplan/shapes. A 409 means another tab (or an undo) changed the
  // drawing: the server wrote nothing, and the page reloads with the message.
  function saveShapes() {
    saveBtn.disabled = true;
    setStatus("Saving…");
    post("/floorplan/shapes", draw.payload()).then(function (res) {
      if (res.r.ok || res.r.status === 409) {
        draw.discard();
        window.location.assign(ROOT + "/floorplan?" + (res.r.ok ? "saved=shapes&draw=1" : "stale=shapes"));
        return;
      }
      var detail = res.data && res.data.detail;
      setStatus(typeof detail === "string" ? detail : "Could not save the drawing (HTTP " + res.r.status + ").", true);
      saveBtn.disabled = false;
    }, function () {
      setStatus("Network error: your drawing is still here.", true);
      saveBtn.disabled = false;
    });
  }

  function save() {
    var ids = Object.keys(dirty);
    if (!ids.length) {
      if (draw && draw.dirtyCount()) {
        saveShapes();
      }
      return;
    }
    var placements = [], basedOn = {};
    ids.forEach(function (id) {
      var d = devices[id];
      placements.push({ device_id: d.id, place_id: d.place_id, x: d.x, y: d.y, roaming: !!d.roaming });
      basedOn[String(d.id)] = d.last_batch;
    });
    saveBtn.disabled = true;
    setStatus("Saving…");
    fetch(ROOT + "/floorplan/placements", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify({ placements: placements, based_on: basedOn }),
    }).then(function (r) {
      return r.json().then(function (data) { return { r: r, data: data }; }, function () { return { r: r, data: null }; });
    }).then(function (res) {
      if (res.r.ok || res.r.status === 409) {
        dirty = {};
        window.location.assign(ROOT + "/floorplan?" + (res.r.ok ? "saved=placements" : "stale=1"));
        return;
      }
      var detail = res.data && res.data.detail;
      setStatus(typeof detail === "string" ? detail : "Could not save (HTTP " + res.r.status + ").", true);
      saveBtn.disabled = false;
    }, function () {
      setStatus("Network error: your changes are still here.", true);
      saveBtn.disabled = false;
    });
  }

  // -------------------------------------------------------------- wiring

  function wireView() {
    if (!view) {
      return;
    }
    view.configure({
      // Devices and the draft's first corner are handles: a press on them never pans.
      // In Draw mode's Select tool, so are shapes and their handles.
      noPan: function (target, evt) {
        return !!(target && target.closest && target.closest("[data-device-id], .fp-draft-start")) ||
          !!(draw && draw.noPan(evt));
      },
      interrupt: function () {
        if (stopTracking) {
          stopTracking();
        }
        if (draw) {
          draw.interrupt();
        }
      },
      change: applySizes,
    });
  }

  function wire() {
    // Unsaved placements live only in this page: warn before it is left.
    window.addEventListener("beforeunload", function (evt) {
      if (unsaved()) {
        evt.preventDefault();
        evt.returnValue = "";
      }
    });
    if (svg && plan) {
      svg.addEventListener("pointerdown", function (evt) {
        if (mode !== "place" || evt.button !== 0) {
          return;
        }
        var id = evt.target.getAttribute && evt.target.getAttribute("data-device-id");
        if (id && devices[id]) {
          dragMarker(evt, devices[id]);
        } else {
          select(null);
        }
      });
      svg.addEventListener("focusin", function (evt) {
        var id = evt.target.getAttribute && evt.target.getAttribute("data-device-id");
        if (id && devices[id] && selected !== devices[id].id) {
          select(devices[id].id);
        }
      });
      svg.addEventListener("click", onLayoutClick);
      svg.addEventListener("click", onScaleClick);
      svg.addEventListener("keydown", onKey);
      document.getElementById("room-close").addEventListener("click", closeDraft);
      document.getElementById("room-cancel").addEventListener("click", function () { setMode("place"); });
      layoutBtn.addEventListener("click", function () { setMode(mode === "layout" ? "place" : "layout"); });
      if (scaleBtn) {
        scaleBtn.addEventListener("click", function () { setMode(mode === "scale" ? "place" : "scale"); });
        document.getElementById("scale-cancel").addEventListener("click", function () { setMode("place"); });
        document.getElementById("scale-save").addEventListener("click", saveScale);
        document.getElementById("scale-length").addEventListener("keydown", function (evt) {
          if (evt.key === "Enter") {
            evt.preventDefault();
            saveScale();
          } else if (evt.key === "Escape") {
            setMode("place");
          }
        });
      }
      saveBtn.addEventListener("click", save);
      discardBtn.addEventListener("click", function () {
        dirty = {};
        if (draw) {
          draw.discard();
        }
        window.location.reload();
      });
      if (drawBtn) {
        drawBtn.addEventListener("click", function () { setMode(mode === "draw" ? "place" : "draw"); });
      }
      document.getElementById("fp-sel-tray").addEventListener("click", function () {
        var d = selected !== null ? devices[selected] : null;
        if (d) {
          setPoint(d, d.place_id, null, null);
          selected = null;
          render();
        }
      });
    }
    if (trayEl) {
      trayEl.addEventListener("pointerdown", function (evt) {
        if (evt.button !== 0 || evt.target.closest("label, input")) {
          return;
        }
        var li = evt.target.closest("[data-tray-device]");
        var d = li ? devices[li.getAttribute("data-tray-device")] : null;
        if (d && svg && plan && mode === "place") {
          dragFromTray(evt, d);
        }
      });
      trayEl.addEventListener("change", function (evt) {
        var id = evt.target.getAttribute("data-roaming-for");
        var d = id ? devices[id] : null;
        if (!d || d.place_id === null || !saveBtn) {
          return;
        }
        d.roaming = evt.target.checked;
        d.x = null;
        d.y = null;
        markDirty(d);
        drawTrays();
      });
    }
    if (filterEl) {
      filterEl.addEventListener("input", drawTrays);
    }
  }

  // Draw mode needs the plan, the overlay and the view (zoom and pan).
  function makeDraw() {
    if (!FP.drawing || !svg || !plan || !view || !drawBtn) {
      return null;
    }
    return FP.drawing.create({
      app: app,
      plan: plan,
      svg: svg,
      view: view,
      units: UNITS,
      shapes: DATA.shapes || [],
      batch: DATA.shapes_batch || 0,
      size: size,
      status: setStatus,
      changed: refreshDirty,
      roomCorners: function () {
        var out = [];
        rooms.forEach(function (r) {
          (polygonOf(r.id) || []).forEach(function (p) { out.push(p); });
        });
        return out;
      },
    });
  }

  load();
  draw = makeDraw();
  wire();
  render();
  drawGrid();
  showSize();
  wireView();
  window.addEventListener("resize", updateGrid);
  if (layoutBtn && /(?:^|[?&])layout=1(?:&|$)/.test(window.location.search)) {
    setMode("layout"); // a new blank plan opens ready to draw its first room
  } else if (draw && /(?:^|[?&])draw=1(?:&|$)/.test(window.location.search)) {
    setMode("draw"); // back in Draw mode after saving a drawing
  }
})();
