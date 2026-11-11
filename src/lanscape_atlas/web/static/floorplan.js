/* lanscape-atlas floor plan page (Phase 2). Plain ES2017, no libraries, no build step.
 *
 * Data comes from <script type="application/json" id="fp-data"> (floor_plan_view
 * without the file name). Everything in it is untrusted: the DOM is built with
 * createElement/createElementNS and textContent only, never innerHTML.
 *
 * Coordinates are normalized to [0, 1]; the SVG viewBox is the plan's pixel size,
 * so a point is drawn at (x * width, y * height).
 *
 * Place mode: drag a device from the list onto a room (the first room by id whose
 * polygon contains the drop point); drag a placed device within its room, or nudge
 * it with the arrow keys. A drop or nudge outside the room is reverted. Edits stay
 * in memory until "Save changes" sends one POST /floorplan/placements; a 409 (stale)
 * reloads the page with the server's message.
 *
 * Layout mode: click to add corners, close on the first corner or "Close room",
 * then name the room; the plain HTML form posts it to /floorplan/rooms.
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

  var DATA = JSON.parse(dataEl.textContent || "{}");
  var plan = DATA.plan || null;
  var svg = document.getElementById("plan-svg");
  var statusEl = document.getElementById("fp-status");
  var saveBtn = document.getElementById("save-changes");
  var discardBtn = document.getElementById("discard-changes");
  var layoutBtn = document.getElementById("mode-layout");
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
    var n = Object.keys(dirty).length;
    saveBtn.disabled = false;
    discardBtn.disabled = false;
    if (layoutBtn) {
      layoutBtn.disabled = true;
      layoutBtn.title = "Save or discard your changes first";
    }
    setStatus(n + " unsaved change" + (n === 1 ? "" : "s") + ".");
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

  function drawPlan() {
    if (!svg || !plan) {
      return;
    }
    var W = plan.width, H = plan.height, u = unit();
    clear(svg);
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
      gRooms.appendChild(svgEl("polygon", { "class": "fp-room" + (heardHere ? " room--heard" : ""), "data-room-id": r.id, points: pts.join(" ") }));
      var cx = 0, cy = 0;
      poly.forEach(function (p) { cx += p[0]; cy += p[1]; });
      var text = svgEl("text", {
        "class": "fp-room-label",
        x: (cx / poly.length * W).toFixed(2),
        y: (cy / poly.length * H).toFixed(2),
        "font-size": (u * 1.8).toFixed(2),
      });
      text.textContent = r.name;
      gLabels.appendChild(text);
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
        r: (u * 0.9).toFixed(2),
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
    svg.appendChild(gDevs);
    svg.appendChild(drawDraft());
  }

  function drawDraft() {
    var g = svgEl("g", { "class": "fp-draft" });
    if (!draft.length) {
      return g;
    }
    var W = plan.width, H = plan.height, u = unit();
    var pts = draft.map(function (p) { return (p[0] * W).toFixed(2) + "," + (p[1] * H).toFixed(2); });
    g.appendChild(svgEl(draftClosed ? "polygon" : "polyline", { points: pts.join(" ") }));
    draft.forEach(function (p, i) {
      var start = i === 0 && draft.length >= 3 && !draftClosed;
      g.appendChild(svgEl("circle", {
        "class": start ? "fp-draft-start" : "fp-draft-pt",
        cx: (p[0] * W).toFixed(2),
        cy: (p[1] * H).toFixed(2),
        r: (u * (start ? 1.1 : 0.5)).toFixed(2),
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
    li.className = "fp-item" + (dirty[d.id] ? " dirty" : "") + (d.online ? "" : " offline");
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

  function planPoint(evt) {
    var rect = svg.getBoundingClientRect();
    return [(evt.clientX - rect.left) / rect.width, (evt.clientY - rect.top) / rect.height];
  }

  function trackPointer(onMove, onUp) {
    function up(e) {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", cancel);
      onUp(e);
    }
    function cancel() {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", cancel);
      render();
    }
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", cancel);
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
    });
  }

  function dropOnPlan(d, e) {
    if (!svg || mode !== "place") {
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

  function setMode(next) {
    mode = next;
    app.classList.toggle("layout", mode === "layout");
    layoutBtn.setAttribute("aria-pressed", mode === "layout" ? "true" : "false");
    document.getElementById("room-draw").hidden = mode !== "layout";
    resetDraft();
    select(null);
    drawTrays(); // roaming checkboxes are only live while placing
    setStatus(mode === "layout" ? "Click on the plan to add the room's corners." : "");
  }

  function resetDraft() {
    draft = [];
    draftClosed = false;
    document.getElementById("room-form").hidden = true;
    document.getElementById("room-close").disabled = true;
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
    draft.push([round4(clamp01(p[0])), round4(clamp01(p[1]))]);
    document.getElementById("room-close").disabled = draft.length < 3;
    drawPlan();
  }

  // ---------------------------------------------------------------- save

  function save() {
    var ids = Object.keys(dirty);
    if (!ids.length) {
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

  function wire() {
    // Unsaved placements live only in this page: warn before it is left.
    window.addEventListener("beforeunload", function (evt) {
      if (Object.keys(dirty).length) {
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
      svg.addEventListener("keydown", onKey);
      document.getElementById("room-close").addEventListener("click", closeDraft);
      document.getElementById("room-cancel").addEventListener("click", function () { setMode("place"); });
      layoutBtn.addEventListener("click", function () { setMode(mode === "layout" ? "place" : "layout"); });
      saveBtn.addEventListener("click", save);
      discardBtn.addEventListener("click", function () {
        dirty = {};
        window.location.reload();
      });
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

  load();
  wire();
  render();
})();
