/* lanscape-atlas floor plan page: Draw mode (walls and doors). Plain ES2017, no
 * libraries, no build step. Loaded with `defer` after floorplan-view.js and before
 * floorplan.js, which calls LanscapeFP.drawing.create(ctx) once it has a plan.
 *
 * Drawing is picture only (spec §6): rooms stay the Phase 2 polygons. Shapes are
 * plan_shape rows ({id, kind, geometry, label}); edits stay in memory until the page's
 * "Save changes" posts payload() to /floorplan/shapes.
 *
 * Coordinates are normalized [0, 1] like everything else on the plan, and every client
 * <-> plan conversion goes through LanscapeFP.view (toPlan / toClient). Angles and
 * lengths are measured in aspect-corrected space, (x, y * aspect) with aspect = plan
 * height / width, where the plan is drawn true to shape: plan pixel space (x * width,
 * y * height) is that space scaled by the width, so angles there are the same.
 *
 * Tools (TOOLS below): Select, Wall (two clicks), Polyline (clicks add corners;
 * double-click or Enter ends it, a click on the first corner closes it, Escape
 * cancels) and Door (hover a wall to preview, click to place). After a wall's first
 * point, typing a length (LanscapeFP.units formats) and Enter puts the next point at
 * exactly that length toward the pointer; only on a plan with a scale. Shift locks the
 * segment to 0/45/90 degrees (aspect-corrected). Snapping (toolbar toggle, on by
 * default; Alt held suppresses it): wall corners and room corners within SNAP_PX
 * screen pixels, else the grid (blank plans, and image plans with a scale).
 *
 * Shape kinds (KINDS below) say how each kind draws, is hit, moves and shows handles;
 * tools (TOOLS below) say what a press, click, double-click and hover do. Issue #11
 * adds furniture and labels by adding KINDS and TOOLS entries and a tool button
 * (data-tool) in the template; the rest of this file is kind-agnostic.
 *
 * Shapes never take pointer events (their SVG groups are pointer-events: none): the
 * Select tool hit-tests geometry in screen pixels instead, so outside Draw mode they
 * are display only and never hide a device or a room.
 */
(function () {
  "use strict";

  var FP = (window.LanscapeFP = window.LanscapeFP || {});
  var SVGNS = "http://www.w3.org/2000/svg";
  var SNAP_PX = 8; // wall and room corners this close (screen px) snap
  var HIT_PX = 8; // Select: a shape or handle this close is under the pointer
  var DOOR_PX = 12; // Door: a wall this close takes the door
  var SAME_PX = 3; // a click this close to the previous point is the same point
  var DRAG_PX = 3; // a Select press that moves less than this is a click
  var NUDGE = 0.005; // arrow-key step, as for devices
  var MAX_POINTS = 500; // per wall (server: WALL_MAX_POINTS)
  var DOOR_MM = { metric: 800, imperial: 812.8 }; // 80 cm / 32 in
  var DOOR_NO_SCALE = 0.04; // default door width without a scale: 4% of the plan width
  var DOOR_MIN = 0.002;
  var EDITABLE = "input, textarea, select, button, [contenteditable]";

  function round6(v) { return Math.round(v * 1e6) / 1e6; }
  function round4(v) { return Math.round(v * 1e4) / 1e4; }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function clamp01(v) { return clamp(v, 0, 1); }
  function roundPt(p) { return [round6(clamp01(p[0])), round6(clamp01(p[1]))]; }
  function copy(o) { return JSON.parse(JSON.stringify(o)); }
  function dist(a, b) { return Math.hypot(a[0] - b[0], a[1] - b[1]); }

  // Distance from point p to segment a-b (any one 2-D space).
  function segDist(p, a, b) {
    var dx = b[0] - a[0], dy = b[1] - a[1];
    var len2 = dx * dx + dy * dy;
    var t = len2 > 0 ? clamp(((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2, 0, 1) : 0;
    return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
  }

  // ------------------------------------------------------------- pure geometry

  // Direction of (dx, dy) in degrees, aspect-corrected (90 looks square on any plan).
  function angleDeg(dx, dy, aspect) {
    return Math.atan2(dy * aspect, dx) * 180 / Math.PI;
  }

  var D = Math.SQRT1_2;
  var DIRS = [[1, 0], [D, D], [0, 1], [-D, D], [-1, 0], [-D, -D], [0, -1], [D, -D]];

  // Shift lock: `to` moved onto the nearest 0/45/90-degree line through `from`
  // (aspect-corrected), as the projection onto that line, shortened if needed to stay
  // on the plan. Horizontal and vertical results copy from's y or x exactly.
  function lockAngle(from, to, aspect) {
    var dx = to[0] - from[0], dy = (to[1] - from[1]) * aspect;
    if (dx === 0 && dy === 0) {
      return [from[0], from[1]];
    }
    var k = Math.round(Math.atan2(dy, dx) / (Math.PI / 4));
    var u = DIRS[((k % 8) + 8) % 8];
    var len = dx * u[0] + dy * u[1];
    var lim = Infinity;
    if (u[0] > 0) { lim = Math.min(lim, (1 - from[0]) / u[0]); }
    if (u[0] < 0) { lim = Math.min(lim, -from[0] / u[0]); }
    if (u[1] > 0) { lim = Math.min(lim, (1 - from[1]) * aspect / u[1]); }
    if (u[1] < 0) { lim = Math.min(lim, -from[1] * aspect / u[1]); }
    len = Math.max(0, Math.min(len, lim));
    return [
      u[0] === 0 ? from[0] : from[0] + u[0] * len,
      u[1] === 0 ? from[1] : from[1] + u[1] * len / aspect,
    ];
  }

  // Real length in mm between normalized points (cf. floorplan/geometry.plan_length_mm).
  function lengthMm(a, b, widthMm, aspect) {
    return Math.hypot((b[0] - a[0]) * widthMm, (b[1] - a[1]) * widthMm * aspect);
  }

  // The point exactly `mm` from `from` toward `toward`, or null without a direction.
  function pointAtLength(from, toward, mm, widthMm, aspect) {
    var dx = toward[0] - from[0], dy = (toward[1] - from[1]) * aspect;
    var n = Math.hypot(dx, dy);
    if (!(n > 1e-12)) {
      return null;
    }
    var l = mm / widthMm;
    return [from[0] + dx / n * l, from[1] + dy / n * l / aspect];
  }

  // Wall rules the server enforces (floorplan/geometry.parse_wall), for edits in place.
  function wallOk(g) {
    var pts = g.points, n = pts.length, i;
    if (n < 2 || n > MAX_POINTS || (g.closed && n < 3)) {
      return false;
    }
    for (i = 0; i + 1 < n; i += 1) {
      if (pts[i][0] === pts[i + 1][0] && pts[i][1] === pts[i + 1][1]) { return false; }
    }
    return !(g.closed && pts[n - 1][0] === pts[0][0] && pts[n - 1][1] === pts[0][1]);
  }

  function segments(g) {
    var out = [], pts = g.points, i;
    for (i = 0; i + 1 < pts.length; i += 1) { out.push([pts[i], pts[i + 1]]); }
    if (g.closed && pts.length > 2) { out.push([pts[pts.length - 1], pts[0]]); }
    return out;
  }

  FP.drawing = {
    geom: {
      angleDeg: angleDeg,
      lockAngle: lockAngle,
      lengthMm: lengthMm,
      pointAtLength: pointAtLength,
      wallOk: wallOk,
    },
    create: create,
  };

  // ------------------------------------------------------------------ instance

  function create(ctx) {
    var plan = ctx.plan, view = ctx.view, UNITS = ctx.units;
    var W = plan.width, H = plan.height, A = H / W;
    var widthMm = plan.width_mm > 0 ? plan.width_mm : 0; // 0: no scale
    var stage = view.stage;

    var shapes = {}; // id -> {id, kind, geometry, label}; new shapes have ids < 0
    var order = [];
    var created = {}, updated = {}, deleted = {};
    var nextTemp = -1;
    var active = false;
    var tool = "select";
    var draft = []; // wall points so far
    var hover = null; // {p: plan point, client: [x, y]} of the last pointer move
    var mods = { shift: false, alt: false };
    var selectedId = null;
    var drag = null;
    var discarded = false;

    var gShapes = svgEl("g", { "class": "fp-shapes" });
    var gLengths = svgEl("g", { "class": "fp-lengths" });
    var gOverlay = svgEl("g", { "class": "fp-draw-overlay" });

    var panel = document.getElementById("shape-draw");
    var hintEl = document.getElementById("draw-hint");
    var flipBtn = document.getElementById("door-flip");
    var deleteBtn = document.getElementById("shape-delete");
    var lengthIn = document.getElementById("draw-length");
    var snapBox = document.getElementById("snap-toggle");
    var lengthsBox = document.getElementById("lengths-toggle");

    function svgEl(tag, attrs) {
      var el = document.createElementNS(SVGNS, tag);
      Object.keys(attrs).forEach(function (k) { el.setAttribute(k, String(attrs[k])); });
      return el;
    }

    function clear(el) {
      while (el.firstChild) { el.removeChild(el.firstChild); }
    }

    function px(p) { return [p[0] * W, p[1] * H]; }
    function fromPx(q) { return [q[0] / W, q[1] / H]; }
    function client(p) { return view.toClient(p[0], p[1]); }
    function f2(v) { return v.toFixed(2); }
    function snapOn() { return !snapBox || snapBox.checked; }
    function lengthsOn() { return widthMm > 0 && (!lengthsBox || lengthsBox.checked); }

    function live() {
      return order.filter(function (id) { return shapes[id] && !deleted[id]; }).map(function (id) { return shapes[id]; });
    }

    function fmt(mm) { return UNITS ? UNITS.format(mm, plan.units) : ""; }

    // ------------------------------------------------------------- snapping

    // Every live wall corner, except corner `skip.index` of shape `skip.id`.
    function wallCorners(skip, fn) {
      live().forEach(function (s) {
        var k = KINDS[s.kind];
        if (!k || !k.corners) {
          return;
        }
        k.corners(s.geometry).forEach(function (q, i) {
          if (!skip || skip.id !== s.id || skip.index !== i) { fn(q); }
        });
      });
    }

    function gridStep() {
      if (!UNITS || !(widthMm > 0)) {
        return null;
      }
      var mm = UNITS.grid(plan.units).minor;
      return [mm / widthMm, mm / (widthMm * A)];
    }

    // Snap plan point p: the nearest wall corner (or room corner, opts.rooms) within
    // SNAP_PX screen px, else the grid (opts.grid). {p, kind: "point"|"grid"|null}.
    function snap(p, opts) {
      if (!snapOn() || mods.alt) {
        return { p: p, kind: null };
      }
      var c = client(p);
      var best = null, bestD = SNAP_PX + 1e-9;
      function consider(q) {
        var d = dist(c, client(q));
        if (d < bestD) {
          best = q;
          bestD = d;
        }
      }
      wallCorners(opts.skip, consider);
      if (opts.rooms) {
        ctx.roomCorners().forEach(consider);
      }
      if (best) {
        return { p: [best[0], best[1]], kind: "point" };
      }
      var g = opts.grid ? gridStep() : null;
      if (g) {
        return { p: [clamp01(Math.round(p[0] / g[0]) * g[0]), clamp01(Math.round(p[1] / g[1]) * g[1])], kind: "grid" };
      }
      return { p: p, kind: null };
    }

    // Where the next wall point goes for a pointer at plan point raw.
    function target(raw) {
      var p = [clamp01(raw[0]), clamp01(raw[1])];
      var last = draft.length ? draft[draft.length - 1] : null;
      if (last && mods.shift) {
        return { p: roundPt(lockAngle(last, p, A)), kind: "lock" };
      }
      var s = snap(p, { rooms: true, grid: true });
      return { p: roundPt(s.p), kind: s.kind };
    }

    // ---------------------------------------------------------------- kinds

    function wallPath(s) {
      // Gaps where doors sit on the wall's line: [t0, t1] per segment, along it.
      var doors = live().filter(function (d) { return d.kind === "door"; });
      var tol = Math.max(W, H) / 2000;
      var d = [], penOn = false;
      segments(s.geometry).forEach(function (seg) {
        var P = px(seg[0]), Q = px(seg[1]);
        var len = dist(P, Q);
        var ux = (Q[0] - P[0]) / len, uy = (Q[1] - P[1]) / len;
        var gaps = [];
        doors.forEach(function (door) {
          var g = door.geometry, a = g.angle * Math.PI / 180;
          var c = px([g.x, g.y]);
          var cross = Math.abs(Math.cos(a) * uy - Math.sin(a) * ux);
          var off = Math.abs((c[0] - P[0]) * uy - (c[1] - P[1]) * ux);
          if (cross < 1e-3 && off < tol) {
            var tc = ((c[0] - P[0]) * ux + (c[1] - P[1]) * uy) / len, half = g.width * W / 2 / len;
            if (tc + half > 0 && tc - half < 1) { gaps.push([Math.max(0, tc - half), Math.min(1, tc + half)]); }
          }
        });
        gaps.sort(function (x, y) { return x[0] - y[0]; });
        var pieces = [], t = 0;
        gaps.forEach(function (gp) {
          if (gp[0] > t) { pieces.push([t, gp[0]]); }
          t = Math.max(t, gp[1]);
        });
        if (t < 1) { pieces.push([t, 1]); }
        pieces.forEach(function (pc) {
          var a = [P[0] + (Q[0] - P[0]) * pc[0], P[1] + (Q[1] - P[1]) * pc[0]];
          var b = [P[0] + (Q[0] - P[0]) * pc[1], P[1] + (Q[1] - P[1]) * pc[1]];
          d.push((penOn && pc[0] === 0 ? "L" : "M" + f2(a[0]) + " " + f2(a[1]) + " L") + f2(b[0]) + " " + f2(b[1]));
          penOn = pc[1] === 1;
        });
        if (!pieces.length) { penOn = false; }
      });
      // An unbroken closed wall: close the path so its first corner is joined too.
      if (s.geometry.closed && d.length && penOn && d.join(" ").split("M").length === 2) {
        d.push("Z");
      }
      return d.join(" ");
    }

    // A door's points in plan pixels: centre c, direction u, opening ends a and b,
    // hinge, leaf end and the far end of the opening (where the swing arc lands).
    function doorParts(g) {
      var ang = g.angle * Math.PI / 180;
      var u = [Math.cos(ang), Math.sin(ang)];
      var c = px([g.x, g.y]);
      var w = g.width * W;
      var a = [c[0] - u[0] * w / 2, c[1] - u[1] * w / 2];
      var b = [c[0] + u[0] * w / 2, c[1] + u[1] * w / 2];
      var hinge = g.flip & 2 ? b : a, far = g.flip & 2 ? a : b;
      var side = g.flip & 1 ? -1 : 1;
      var leaf = [hinge[0] - u[1] * w * side, hinge[1] + u[0] * w * side];
      return { c: c, u: u, w: w, a: a, b: b, hinge: hinge, far: far, leaf: leaf };
    }

    function doorPath(g) {
      var p = doorParts(g);
      var v1 = [p.leaf[0] - p.hinge[0], p.leaf[1] - p.hinge[1]];
      var v2 = [p.far[0] - p.hinge[0], p.far[1] - p.hinge[1]];
      var sweep = v1[0] * v2[1] - v1[1] * v2[0] > 0 ? 1 : 0;
      return "M" + f2(p.hinge[0]) + " " + f2(p.hinge[1]) + " L" + f2(p.leaf[0]) + " " + f2(p.leaf[1]) +
        " A" + f2(p.w) + " " + f2(p.w) + " 0 0 " + sweep + " " + f2(p.far[0]) + " " + f2(p.far[1]);
    }

    function shapeClass(s, base) {
      return base + (s.id === selectedId ? " selected" : "") + (s.id < 0 || updated[s.id] ? " unsaved" : "");
    }

    // Translate geometry g by plan delta d, clamped so every point stays on the plan.
    function moveBy(pts, d) {
      var lo = [-Infinity, -Infinity], hi = [Infinity, Infinity];
      pts.forEach(function (q) {
        [0, 1].forEach(function (i) {
          lo[i] = Math.max(lo[i], -q[i]);
          hi[i] = Math.min(hi[i], 1 - q[i]);
        });
      });
      return [clamp(d[0], lo[0], hi[0]), clamp(d[1], lo[1], hi[1])];
    }

    // Per kind: z (drawn in ascending z; on a tie under the pointer the higher z is
    // picked), draw(shape) -> SVG element, hit(geometry, client) -> screen px distance,
    // handles(geometry) -> client points, dragHandle(shape, index, plan point),
    // move(geometry, original, plan delta), valid(geometry); optional corners(geometry)
    // (snap targets) and lengths(shape, group) (measurement labels).
    var KINDS = {
      wall: {
        z: 1,
        draw: function (s) {
          return svgEl("path", { "class": shapeClass(s, "fp-wall"), "data-shape-id": s.id, d: wallPath(s) });
        },
        hit: function (g, c) {
          var best = Infinity;
          segments(g).forEach(function (seg) { best = Math.min(best, segDist(c, client(seg[0]), client(seg[1]))); });
          return best;
        },
        corners: function (g) { return g.points; },
        handles: function (g) { return g.points.map(function (q) { return client(q); }); },
        dragHandle: function (s, i, raw) {
          var pts = s.geometry.points;
          var prev = i > 0 ? pts[i - 1] : s.geometry.closed ? pts[pts.length - 1] : pts[1];
          var p = [clamp01(raw[0]), clamp01(raw[1])];
          p = mods.shift ? lockAngle(prev, p, A) : snap(p, { rooms: true, grid: true, skip: { id: s.id, index: i } }).p;
          pts[i] = roundPt(p);
        },
        move: function (g, o, d) {
          d = moveBy(o.points, d);
          g.points = o.points.map(function (q) { return roundPt([q[0] + d[0], q[1] + d[1]]); });
        },
        valid: wallOk,
        lengths: function (s, g) {
          segments(s.geometry).forEach(function (seg, i) {
            var m = px([(seg[0][0] + seg[1][0]) / 2, (seg[0][1] + seg[1][1]) / 2]);
            var t = svgEl("text", {
              "class": "fp-length",
              "data-shape-id": s.id,
              "data-segment": i,
              x: f2(m[0]),
              y: f2(m[1]),
              "font-size": ctx.size(1.3),
            });
            t.textContent = fmt(lengthMm(seg[0], seg[1], widthMm, A));
            g.appendChild(t);
          });
        },
      },
      door: {
        z: 2,
        draw: function (s) {
          var g = svgEl("g", { "class": shapeClass(s, "fp-door"), "data-shape-id": s.id });
          g.appendChild(svgEl("path", { "class": "fp-door-swing", d: doorPath(s.geometry) }));
          return g;
        },
        hit: function (g, c) {
          var p = doorParts(g);
          function cl(q) { return client(fromPx(q)); }
          return Math.min(segDist(c, cl(p.a), cl(p.b)), segDist(c, cl(p.hinge), cl(p.leaf)));
        },
        handles: function (g) {
          var p = doorParts(g);
          return [client(fromPx(p.a)), client(fromPx(p.b))];
        },
        dragHandle: function (s, i, raw) {
          var p = doorParts(s.geometry), q = px(raw);
          var half = Math.abs((q[0] - p.c[0]) * p.u[0] + (q[1] - p.c[1]) * p.u[1]);
          s.geometry.width = round6(clamp(2 * half / W, DOOR_MIN, 1));
        },
        move: function (g, o, d) {
          d = moveBy([[o.x, o.y]], d);
          g.x = round6(clamp01(o.x + d[0]));
          g.y = round6(clamp01(o.y + d[1]));
        },
        valid: function () { return true; },
      },
    };

    // ---------------------------------------------------------------- render

    // Kind names by z, lowest first (walls under doors, so a swing shows over a wall).
    function byZ() {
      return Object.keys(KINDS).sort(function (a, b) { return KINDS[a].z - KINDS[b].z; });
    }

    function render() {
      clear(gShapes);
      clear(gLengths);
      var all = live();
      byZ().forEach(function (kind) {
        all.forEach(function (s) {
          if (s.kind === kind) { gShapes.appendChild(KINDS[kind].draw(s)); }
        });
      });
      if (lengthsOn()) {
        all.forEach(function (s) {
          var k = KINDS[s.kind];
          if (k && k.lengths) { k.lengths(s, gLengths); }
        });
      }
      renderOverlay();
    }

    function dot(p, cls, k) {
      var q = px(p);
      return svgEl("circle", { "class": cls, cx: f2(q[0]), cy: f2(q[1]), r: ctx.size(k) });
    }

    function renderOverlay() {
      clear(gOverlay);
      if (!active) {
        return;
      }
      var t = hover && draft.length ? target(hover.p) : null;
      if (draft.length) {
        var pts = draft.concat(t ? [t.p] : []).map(function (p) { var q = px(p); return f2(q[0]) + "," + f2(q[1]); });
        gOverlay.appendChild(svgEl("polyline", { "class": "fp-wall-draft", points: pts.join(" ") }));
        draft.forEach(function (p, i) {
          var start = i === 0 && tool === "polyline" && draft.length >= 3;
          gOverlay.appendChild(dot(p, start ? "fp-draft-start" : "fp-draft-pt", start ? 1.1 : 0.5));
        });
        if (t && widthMm > 0) {
          var last = draft[draft.length - 1], q = px(t.p);
          var label = svgEl("text", { "class": "fp-live-length", x: f2(q[0]), y: f2(q[1]), dx: ctx.size(1.2), dy: ctx.size(-1.2), "font-size": ctx.size(1.5) });
          label.textContent = fmt(lengthMm(last, t.p, widthMm, A));
          gOverlay.appendChild(label);
        }
      }
      var snapAt = null;
      if (t && t.kind === "point") {
        snapAt = t.p;
      } else if (hover && !draft.length && (tool === "wall" || tool === "polyline")) {
        var first = target(hover.p);
        if (first.kind === "point") { snapAt = first.p; }
      }
      if (snapAt) {
        gOverlay.appendChild(dot(snapAt, "fp-snap", 1.0));
      }
      if (hover && TOOLS[tool].preview) {
        var pv = TOOLS[tool].preview(hover);
        if (pv) { gOverlay.appendChild(pv); }
      }
      var sel = selectedId !== null ? shapes[selectedId] : null;
      if (sel && tool === "select" && KINDS[sel.kind] && KINDS[sel.kind].handles) {
        KINDS[sel.kind].handles(sel.geometry).forEach(function (c, i) {
          var p = view.toPlan(c[0], c[1]);
          var h = dot(p, "fp-handle", 0.7);
          h.setAttribute("data-handle", String(i));
          gOverlay.appendChild(h);
        });
      }
    }

    // ------------------------------------------------------------- edits

    function changed() {
      ctx.changed();
      render();
      updatePanel();
    }

    function add(kind, geometry) {
      var id = nextTemp;
      nextTemp -= 1;
      shapes[id] = { id: id, kind: kind, geometry: geometry, label: null };
      order.push(id);
      created[id] = true;
      changed();
      return id;
    }

    function touch(id) {
      if (id > 0) { updated[id] = true; }
      changed();
    }

    function remove(id) {
      if (id < 0) {
        delete shapes[id];
        delete created[id];
        order = order.filter(function (x) { return x !== id; });
      } else {
        deleted[id] = true;
        delete updated[id];
      }
      if (selectedId === id) { selectedId = null; }
      changed();
    }

    function select(id) {
      selectedId = id;
      render();
      updatePanel();
    }

    // ----------------------------------------------------------- drafting

    function setHint(text) {
      if (hintEl) { hintEl.textContent = text; }
    }

    function showLengthInput() {
      var want = active && widthMm > 0 && draft.length > 0 && (tool === "wall" || tool === "polyline");
      if (!lengthIn) {
        return;
      }
      if (!want) {
        lengthIn.value = "";
        lengthIn.classList.remove("error");
        if (document.activeElement === lengthIn) { stage.focus({ preventScroll: true }); }
      }
      lengthIn.hidden = !want;
    }

    function placeLengthInput(e) {
      if (!lengthIn || lengthIn.hidden) {
        return;
      }
      var r = stage.getBoundingClientRect();
      lengthIn.style.left = clamp(e.clientX - r.left + 16, 0, Math.max(0, r.width - 110)) + "px";
      lengthIn.style.top = clamp(e.clientY - r.top + 16, 0, Math.max(0, r.height - 30)) + "px";
    }

    function cancelDraft() {
      draft = [];
      showLengthInput();
      renderOverlay();
    }

    // Is p the same point as the last draft point (on screen)?
    function sameAsLast(p) {
      var last = draft[draft.length - 1];
      return (last[0] === p[0] && last[1] === p[1]) || dist(client(last), client(p)) < SAME_PX;
    }

    function finishWall(closed) {
      var pts = draft;
      draft = [];
      showLengthInput();
      if (pts.length >= (closed ? 3 : 2)) {
        add("wall", { points: pts, closed: closed });
      } else {
        renderOverlay();
      }
    }

    // Add point p to the draft; the Wall tool finishes on its second point.
    function addPoint(p) {
      if (draft.length && sameAsLast(p)) {
        return; // a double-click, or a click on the same spot: never a zero-length wall
      }
      draft.push(p);
      if (tool === "wall" && draft.length === 2) {
        finishWall(false);
        return;
      }
      if (draft.length >= MAX_POINTS) {
        finishWall(false);
        return;
      }
      showLengthInput();
      renderOverlay();
    }

    function typedLength() {
      var text = lengthIn.value;
      if (!text.trim()) {
        if (tool === "polyline" && draft.length >= 2) { finishWall(false); }
        return;
      }
      var got = UNITS.parse(text);
      if (got.error) {
        lengthIn.classList.add("error");
        ctx.status(got.error, true);
        return;
      }
      var from = draft[draft.length - 1];
      var toward = hover ? [clamp01(hover.p[0]), clamp01(hover.p[1])] : null;
      if (toward && mods.shift) { toward = lockAngle(from, toward, A); }
      var p = toward ? pointAtLength(from, toward, got.mm, widthMm, A) : null;
      if (!p) {
        ctx.status("Point at where the wall goes first, then type its length.", true);
        return;
      }
      if (p[0] < -1e-9 || p[0] > 1 + 1e-9 || p[1] < -1e-9 || p[1] > 1 + 1e-9) {
        lengthIn.classList.add("error");
        ctx.status("That length runs off the plan.", true);
        return;
      }
      lengthIn.value = "";
      lengthIn.classList.remove("error");
      addPoint(roundPt(p));
    }

    // ---------------------------------------------------------------- tools

    function doorWidth() {
      return widthMm > 0 ? DOOR_MM[plan.units === "metric" ? "metric" : "imperial"] / widthMm : DOOR_NO_SCALE;
    }

    // The door the Door tool would place for a pointer at client c: on the nearest
    // wall segment within DOOR_PX, centred under the pointer but kept on the segment.
    function doorAt(c) {
      var best = null, bestD = DOOR_PX + 1e-9;
      live().forEach(function (s) {
        if (s.kind !== "wall") {
          return;
        }
        segments(s.geometry).forEach(function (seg) {
          var d = segDist(c, client(seg[0]), client(seg[1]));
          if (d < bestD) {
            best = seg;
            bestD = d;
          }
        });
      });
      if (!best) {
        return null;
      }
      var P = px(best[0]), Q = px(best[1]);
      var len = dist(P, Q);
      var w = Math.min(doorWidth(), 1);
      var q = px(view.toPlan(c[0], c[1]));
      var t = ((q[0] - P[0]) * (Q[0] - P[0]) + (q[1] - P[1]) * (Q[1] - P[1])) / (len * len);
      var half = w * W / 2 / len;
      t = half >= 0.5 ? 0.5 : clamp(t, half, 1 - half);
      var centre = fromPx([P[0] + (Q[0] - P[0]) * t, P[1] + (Q[1] - P[1]) * t]);
      return {
        x: round6(clamp01(centre[0])),
        y: round6(clamp01(centre[1])),
        angle: round4(Math.atan2(Q[1] - P[1], Q[0] - P[0]) * 180 / Math.PI),
        width: round6(w),
        flip: 0,
      };
    }

    function drawClick(e) {
      var raw = view.toPlan(e.clientX, e.clientY);
      if (tool === "polyline" && draft.length >= 3 && dist([e.clientX, e.clientY], client(draft[0])) <= SNAP_PX) {
        finishWall(true);
        return;
      }
      if (!draft.length) {
        var first = target(raw);
        draft = [first.p];
        showLengthInput();
        placeLengthInput(e);
        renderOverlay();
        return;
      }
      addPoint(target(raw).p);
    }

    // Tools: click / dblclick / down (pointerdown) / preview (hover overlay) / hint.
    var TOOLS = {
      select: {
        hint: "Click a wall or door to select it. Drag to move it, drag a corner or a door's end to change it, " +
          "Delete removes it, arrow keys nudge it.",
        down: function (e) {
          var hit = hitAt(e.clientX, e.clientY);
          if (!hit) {
            select(null);
            return;
          }
          e.preventDefault(); // no text selection; it also stops the press focusing the stage
          stage.focus({ preventScroll: true }); // so Delete and the arrow keys reach us
          if (hit.id !== selectedId) { select(hit.id); }
          startDrag(e, hit);
        },
      },
      wall: {
        hint: "Click the wall's start, then its end. After the first click you can type a length and press Enter.",
        click: drawClick,
      },
      polyline: {
        hint: "Click to add corners. Double-click or Enter ends the wall, a click on the first corner closes it, " +
          "Escape cancels. Type a length and press Enter for an exact segment.",
        click: drawClick,
        dblclick: function () {
          if (draft.length >= 2) { finishWall(false); }
        },
      },
      door: {
        hint: "Point at a wall to see the door, then click to place it. Select it to change its width or flip it.",
        click: function (e) {
          var g = doorAt([e.clientX, e.clientY]);
          if (!g) {
            ctx.status("Point at a wall to place a door on it.", true);
            return;
          }
          add("door", g);
        },
        preview: function (h) {
          var g = doorAt(h.client);
          if (!g) {
            return null;
          }
          var el = KINDS.door.draw({ id: 0, kind: "door", geometry: g });
          el.setAttribute("class", "fp-door fp-door-preview");
          return el;
        },
      },
    };

    function setTool(next) {
      if (!TOOLS[next]) {
        return;
      }
      tool = next;
      cancelDraft();
      if (next !== "select") { selectedId = null; }
      ctx.app.setAttribute("data-tool", next);
      var btns = panel ? panel.querySelectorAll("[data-tool]") : [];
      for (var i = 0; i < btns.length; i += 1) {
        btns[i].setAttribute("aria-pressed", btns[i].getAttribute("data-tool") === next ? "true" : "false");
      }
      setHint(TOOLS[next].hint);
      render();
      updatePanel();
    }

    // --------------------------------------------------------- select / drag

    // The handle or shape under client (x, y): {id, handle (index or null)} or null.
    function hitAt(x, y) {
      var c = [x, y];
      var sel = selectedId !== null ? shapes[selectedId] : null;
      if (sel && !deleted[sel.id] && KINDS[sel.kind] && KINDS[sel.kind].handles) {
        var hs = KINDS[sel.kind].handles(sel.geometry);
        for (var i = 0; i < hs.length; i += 1) {
          if (dist(c, hs[i]) <= HIT_PX) { return { id: sel.id, handle: i }; }
        }
      }
      // Highest z first, and the first kind with anything in reach wins: a door sits on
      // its wall, so the door is picked.
      var kinds = byZ().reverse();
      for (var k = 0; k < kinds.length; k += 1) {
        var best = null, bestD = HIT_PX + 1e-9;
        live().forEach(function (s) {
          if (s.kind !== kinds[k]) {
            return;
          }
          var d = KINDS[s.kind].hit(s.geometry, c);
          if (d < bestD) {
            best = s;
            bestD = d;
          }
        });
        if (best) {
          return { id: best.id, handle: null };
        }
      }
      return null;
    }

    function endDrag() {
      document.removeEventListener("pointermove", onDragMove);
      document.removeEventListener("pointerup", onDragUp);
      document.removeEventListener("pointercancel", cancelDrag);
    }

    function startDrag(e, hit) {
      endDrag();
      var s = shapes[hit.id];
      drag = { id: hit.id, handle: hit.handle, orig: copy(s.geometry), start: view.toPlan(e.clientX, e.clientY), at: [e.clientX, e.clientY], moved: false };
      document.addEventListener("pointermove", onDragMove);
      document.addEventListener("pointerup", onDragUp);
      document.addEventListener("pointercancel", cancelDrag);
    }

    function onDragMove(e) {
      if (!drag) {
        return;
      }
      if (!drag.moved && dist(drag.at, [e.clientX, e.clientY]) < DRAG_PX) {
        return;
      }
      drag.moved = true;
      mods.shift = e.shiftKey;
      mods.alt = e.altKey;
      var s = shapes[drag.id], k = KINDS[s.kind];
      var raw = view.toPlan(e.clientX, e.clientY);
      s.geometry = copy(drag.orig);
      if (drag.handle !== null) {
        k.dragHandle(s, drag.handle, raw);
      } else {
        k.move(s.geometry, drag.orig, [raw[0] - drag.start[0], raw[1] - drag.start[1]]);
      }
      render();
    }

    function onDragUp() {
      endDrag();
      var d = drag;
      drag = null;
      if (!d || !d.moved) {
        return;
      }
      var s = shapes[d.id];
      if (!KINDS[s.kind].valid(s.geometry)) {
        s.geometry = d.orig;
        ctx.status("Two corners of a wall can't be on the same point: moved back.", true);
        render();
        return;
      }
      if (JSON.stringify(s.geometry) !== JSON.stringify(d.orig)) {
        touch(d.id);
      } else {
        render();
      }
    }

    function cancelDrag() {
      endDrag();
      if (drag) {
        shapes[drag.id].geometry = drag.orig;
        drag = null;
        render();
      }
    }

    function nudge(step) {
      var s = selectedId !== null ? shapes[selectedId] : null;
      if (!s || !KINDS[s.kind].move) {
        return;
      }
      var orig = copy(s.geometry);
      KINDS[s.kind].move(s.geometry, orig, [step[0] * NUDGE, step[1] * NUDGE]);
      if (JSON.stringify(s.geometry) !== JSON.stringify(orig)) { touch(s.id); }
    }

    // --------------------------------------------------------------- panel

    function updatePanel() {
      var s = selectedId !== null ? shapes[selectedId] : null;
      if (flipBtn) { flipBtn.disabled = !(s && s.kind === "door"); }
      if (deleteBtn) { deleteBtn.disabled = !s; }
    }

    // ---------------------------------------------------------------- events

    function isEditable(el) {
      return !!(el && el.closest && el.closest(EDITABLE));
    }

    var KEYS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

    function onKeyDown(e) {
      if (e.key === "Shift" || e.key === "Alt") {
        mods.shift = e.shiftKey || e.key === "Shift";
        mods.alt = e.altKey || e.key === "Alt";
        if (active) {
          if (e.key === "Alt") { e.preventDefault(); }
          renderOverlay();
        }
        return;
      }
      if (!active || e.target === lengthIn || isEditable(e.target)) {
        return;
      }
      if (draft.length && widthMm > 0 && lengthIn && !e.ctrlKey && !e.metaKey && !e.altKey && /^[0-9.]$/.test(e.key)) {
        lengthIn.hidden = false;
        lengthIn.focus({ preventScroll: true }); // the key's character lands in the input
        return;
      }
      if (e.key === "Escape") {
        if (draft.length) { cancelDraft(); } else { select(null); }
      } else if (e.key === "Enter") {
        if (tool === "polyline" && draft.length >= 2) { finishWall(false); } else { return; }
      } else if ((e.key === "Delete" || e.key === "Backspace") && selectedId !== null) {
        remove(selectedId);
      } else if (KEYS[e.key] && selectedId !== null && tool === "select") {
        nudge(KEYS[e.key]);
      } else {
        return;
      }
      e.preventDefault();
    }

    function onKeyUp(e) {
      if (e.key === "Shift" || e.key === "Alt") {
        mods.shift = e.key === "Shift" ? false : e.shiftKey;
        mods.alt = e.key === "Alt" ? false : e.altKey;
        if (active) { renderOverlay(); }
      }
    }

    function onMove(e) {
      if (!active || drag) {
        return;
      }
      mods.shift = e.shiftKey;
      mods.alt = e.altKey;
      hover = { p: view.toPlan(e.clientX, e.clientY), client: [e.clientX, e.clientY] };
      placeLengthInput(e);
      renderOverlay();
    }

    function wire() {
      var svg = ctx.svg;
      svg.addEventListener("pointermove", onMove);
      svg.addEventListener("pointerleave", function () {
        if (active && !drag) {
          hover = null;
          renderOverlay();
        }
      });
      svg.addEventListener("pointerdown", function (e) {
        if (active && e.button === 0 && TOOLS[tool].down) { TOOLS[tool].down(e); }
      });
      svg.addEventListener("click", function (e) {
        if (!active || !TOOLS[tool].click) {
          return;
        }
        mods.shift = e.shiftKey;
        mods.alt = e.altKey;
        TOOLS[tool].click(e);
      });
      svg.addEventListener("dblclick", function (e) {
        if (active && TOOLS[tool].dblclick) {
          e.preventDefault();
          TOOLS[tool].dblclick(e);
        }
      });
      document.addEventListener("keydown", onKeyDown);
      document.addEventListener("keyup", onKeyUp);
      window.addEventListener("blur", function () { mods.shift = false; mods.alt = false; });
      if (lengthIn) {
        lengthIn.addEventListener("keydown", function (e) {
          if (e.key === "Enter") {
            e.preventDefault();
            typedLength();
          } else if (e.key === "Escape") {
            e.preventDefault();
            cancelDraft();
          }
        });
        lengthIn.addEventListener("input", function () { lengthIn.classList.remove("error"); });
      }
      if (panel) {
        panel.addEventListener("click", function (e) {
          var b = e.target.closest && e.target.closest("[data-tool]");
          if (b) { setTool(b.getAttribute("data-tool")); }
        });
      }
      if (flipBtn) {
        flipBtn.addEventListener("click", function () {
          var s = selectedId !== null ? shapes[selectedId] : null;
          if (s && s.kind === "door") {
            s.geometry.flip = (s.geometry.flip + 1) % 4;
            touch(s.id);
          }
        });
      }
      if (deleteBtn) {
        deleteBtn.addEventListener("click", function () {
          if (selectedId !== null) { remove(selectedId); }
        });
      }
      if (snapBox) { snapBox.addEventListener("change", renderOverlay); }
      if (lengthsBox) { lengthsBox.addEventListener("change", render); }
    }

    // ------------------------------------------------------------------ load

    (ctx.shapes || []).forEach(function (s) {
      if (s && typeof s.id === "number" && s.geometry && typeof s.geometry === "object") {
        shapes[s.id] = { id: s.id, kind: String(s.kind), geometry: s.geometry, label: s.label === undefined ? null : s.label };
        order.push(s.id);
      }
    });
    wire();
    render();

    return {
      // The SVG groups floorplan.js stacks: shapes under rooms, lengths over room
      // labels, the overlay (drafts, previews, handles) on top.
      layers: { shapes: gShapes, lengths: gLengths, overlay: gOverlay },
      redraw: render,
      setActive: function (on) {
        active = !!on;
        if (panel) { panel.hidden = !active; }
        if (!active) {
          endDrag();
          drag = null;
          draft = [];
          hover = null;
          selectedId = null;
          showLengthInput();
        }
        setTool(active ? tool : "select");
      },
      // Select-tool presses on a shape or handle drag it instead of panning the view.
      noPan: function (e) {
        return active && tool === "select" && !!e && hitAt(e.clientX, e.clientY) !== null;
      },
      interrupt: cancelDrag,
      // Draw room: a corner within SNAP_PX of a wall corner takes it exactly.
      snapToWalls: function (p) {
        if (!snapOn() || mods.alt) {
          return null;
        }
        var s = snap(p, { rooms: false, grid: false });
        return s.kind === "point" ? s.p : null;
      },
      dirtyCount: function () {
        if (discarded) {
          return 0;
        }
        return Object.keys(created).length + Object.keys(updated).length + Object.keys(deleted).length;
      },
      discard: function () { discarded = true; },
      payload: function () {
        var creates = [], updates = [];
        order.forEach(function (id) {
          var s = shapes[id];
          if (!s || deleted[id]) {
            return;
          }
          if (id < 0) {
            creates.push({ kind: s.kind, geometry: s.geometry, label: s.label });
          } else if (updated[id]) {
            updates.push({ id: id, geometry: s.geometry, label: s.label });
          }
        });
        return {
          creates: creates,
          updates: updates,
          deletes: Object.keys(deleted).map(Number),
          based_on: ctx.batch || 0,
        };
      },
    };
  }
})();
