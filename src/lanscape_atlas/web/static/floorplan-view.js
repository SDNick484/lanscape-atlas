/* lanscape-atlas floor plan page: the view transform (zoom and pan). Plain ES2017, no
 * libraries, no build step. Loaded with `defer` before floorplan.js; both share the
 * window.LanscapeFP namespace, and this file sets LanscapeFP.view when the page has a
 * plan (#floorplan, the stage) and null otherwise.
 *
 * Mechanism: the <img> and the <svg> overlay sit together in one wrapper (#fp-view)
 * inside the clipping stage, and the wrapper gets a CSS transform
 * `translate(tx%, ty%) scale(s)` with transform-origin 0 0. The translation is a
 * fraction of the wrapper's own size, so the view stays put when the window is resized.
 * The image and every SVG layer move together because they are one transformed box.
 *
 * Coordinates: toPlan()/toClient() are the only conversions between client pixels and
 * normalized plan coordinates ([0, 1] across the plan); they read the wrapper's
 * on-screen box, which already includes the transform.
 *
 * showPoint() and fitBox() serve the search box (floorplan-search.js).
 *
 * Zoom is 1x (fit) to 8x: toolbar buttons and the + / = / - / 0 keys (x1.25 about the
 * view centre), wheel and trackpad pinch about the pointer, two-finger touch pinch.
 * Pan: drag on empty plan space (a press that moves less than 4 px stays a click), or
 * hold Space and drag anywhere on the stage, in every mode. The pan is clamped so the
 * plan always covers the stage. --fp-zoom on the stage lets CSS keep strokes constant
 * on screen; the page script keeps marker and label sizes constant through onChange.
 */
(function () {
  "use strict";

  var FP = (window.LanscapeFP = window.LanscapeFP || {});
  var MIN = 1;
  var MAX = 8;
  var STEP = 1.25;
  var CLICK_SLOP = 4; // px: a press that moves less than this is a click, not a pan
  var EDITABLE = "input, textarea, select, button, [contenteditable]";

  var stage = document.getElementById("floorplan");
  var viewEl = document.getElementById("fp-view");
  FP.view = null;
  if (!stage || !viewEl) {
    return;
  }

  var s = 1; // zoom
  var tx = 0; // translation, as a fraction of the wrapper's width
  var ty = 0; // ... and of its height
  var hooks = {
    noPan: function () { return false; }, // true when a press on this target must not pan
    interrupt: function () {}, // a gesture took over: cancel any drag in progress
    change: function () {}, // called with the zoom after every change
  };
  var spaceHeld = false;
  var hover = false;
  var pan = null; // {id, x0, y0, x, y, active}
  var suppressClick = false;
  var touches = {}; // pointerId -> {x, y}
  var pinch = null; // {d0, s0, p0}
  var shown = null; // the zoom last published to the page (data-zoom, --fp-zoom, change)

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  function apply() {
    tx = clamp(tx, 1 - s, 0);
    ty = clamp(ty, 1 - s, 0);
    viewEl.style.transform = "translate(" + tx * 100 + "%, " + ty * 100 + "%) scale(" + s + ")";
    if (s !== shown) {
      shown = s;
      stage.setAttribute("data-zoom", String(s));
      stage.style.setProperty("--fp-zoom", String(s));
      hooks.change(s);
    }
  }

  // ----------------------------------------------------- the one conversion

  // Client pixels -> normalized plan coordinates under the current transform.
  function toPlan(clientX, clientY) {
    var r = viewEl.getBoundingClientRect();
    return [(clientX - r.left) / r.width, (clientY - r.top) / r.height];
  }

  // Normalized plan coordinates -> client pixels.
  function toClient(x, y) {
    var r = viewEl.getBoundingClientRect();
    return [r.left + x * r.width, r.top + y * r.height];
  }

  // The stage's visible box (inside its border) in client pixels.
  function stageBox() {
    var r = stage.getBoundingClientRect();
    var left = r.left + stage.clientLeft, top = r.top + stage.clientTop;
    return { left: left, top: top, width: stage.clientWidth, height: stage.clientHeight };
  }

  // Is a client point inside the visible part of the plan?
  function inStage(clientX, clientY) {
    var b = stageBox();
    return clientX >= b.left && clientX <= b.left + b.width && clientY >= b.top && clientY <= b.top + b.height;
  }

  // The wrapper's untransformed box: where plan (0, 0) sits at zoom 1 with no pan.
  function base() {
    var r = viewEl.getBoundingClientRect();
    var w = viewEl.offsetWidth, h = viewEl.offsetHeight;
    return { left: r.left - tx * w, top: r.top - ty * h, w: w, h: h };
  }

  // ------------------------------------------------------------------ zoom

  // Zoom to `next`, keeping the plan point under (clientX, clientY) where it is.
  // Without a point, zoom about the centre of the view.
  function zoomTo(next, clientX, clientY) {
    if (clientX === undefined || clientY === undefined) {
      var b0 = stageBox();
      clientX = b0.left + b0.width / 2;
      clientY = b0.top + b0.height / 2;
    }
    var p = toPlan(clientX, clientY);
    var b = base();
    s = clamp(next, MIN, MAX);
    if (Math.abs(s - MIN) < 1e-9) { s = MIN; }
    if (Math.abs(s - MAX) < 1e-9) { s = MAX; }
    tx = (clientX - b.left) / b.w - p[0] * s;
    ty = (clientY - b.top) / b.h - p[1] * s;
    apply();
  }

  function zoomBy(factor, clientX, clientY) { zoomTo(s * factor, clientX, clientY); }

  function fit() {
    s = MIN;
    tx = 0;
    ty = 0;
    apply();
  }

  // Centre the plan point (x, y), zooming in to at least `minZoom` (as far as the
  // clamp allows near the plan's edges).
  function showPoint(x, y, minZoom) {
    s = clamp(Math.max(s, minZoom || MIN), MIN, MAX);
    tx = 0.5 - x * s;
    ty = 0.5 - y * s;
    apply();
  }

  // Zoom and pan so the plan box (x0, y0)-(x1, y1) fills the view with a margin.
  function fitBox(x0, y0, x1, y1) {
    var w = Math.max(x1 - x0, 1e-6), h = Math.max(y1 - y0, 1e-6);
    s = clamp(Math.min(1 / (w * 1.2), 1 / (h * 1.2)), MIN, MAX);
    tx = 0.5 - (x0 + x1) / 2 * s;
    ty = 0.5 - (y0 + y1) / 2 * s;
    apply();
  }

  function panBy(dx, dy) {
    var b = base();
    tx += dx / b.w;
    ty += dy / b.h;
    apply();
  }

  // ------------------------------------------------------------------- pan

  function endPan() {
    document.removeEventListener("pointermove", onPanMove);
    document.removeEventListener("pointerup", onPanUp);
    document.removeEventListener("pointercancel", onPanUp);
    stage.classList.remove("fp-panning");
    pan = null;
  }

  function onPanMove(e) {
    if (!pan || e.pointerId !== pan.id || pinch) {
      return;
    }
    if (!pan.active && Math.hypot(e.clientX - pan.x0, e.clientY - pan.y0) >= CLICK_SLOP) {
      pan.active = true;
      stage.classList.add("fp-panning");
    }
    if (pan.active) {
      panBy(e.clientX - pan.x, e.clientY - pan.y);
      pan.x = e.clientX;
      pan.y = e.clientY;
    }
  }

  function onPanUp(e) {
    if (!pan || e.pointerId !== pan.id) {
      return;
    }
    // A real pan swallows the click that follows it (no Layout-mode corner).
    suppressClick = pan.active;
    endPan();
  }

  function startPan(evt, immediate) {
    endPan();
    pan = { id: evt.pointerId, x0: evt.clientX, y0: evt.clientY, x: evt.clientX, y: evt.clientY, active: immediate };
    if (immediate) {
      stage.classList.add("fp-panning");
    }
    document.addEventListener("pointermove", onPanMove);
    document.addEventListener("pointerup", onPanUp);
    document.addEventListener("pointercancel", onPanUp);
  }

  // ----------------------------------------------------------------- pinch

  function touchCount() { return Object.keys(touches).length; }

  function pinchState() {
    var ids = Object.keys(touches);
    var a = touches[ids[0]], b = touches[ids[1]];
    return { d: Math.hypot(b.x - a.x, b.y - a.y), x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  }

  function onTouchMove(e) {
    if (!touches[e.pointerId]) {
      return;
    }
    touches[e.pointerId] = { x: e.clientX, y: e.clientY };
    if (!pinch || touchCount() < 2) {
      return;
    }
    var now = pinchState();
    if (pinch.d0 <= 0) {
      return;
    }
    // Keep the plan point first under the fingers' midpoint under it as they move.
    var b = base();
    s = clamp(pinch.s0 * (now.d / pinch.d0), MIN, MAX);
    tx = (now.x - b.left) / b.w - pinch.p0[0] * s;
    ty = (now.y - b.top) / b.h - pinch.p0[1] * s;
    apply();
  }

  function onTouchUp(e) {
    delete touches[e.pointerId];
    if (touchCount() < 2) {
      pinch = null;
    }
    if (!touchCount()) {
      document.removeEventListener("pointermove", onTouchMove);
      document.removeEventListener("pointerup", onTouchUp);
      document.removeEventListener("pointercancel", onTouchUp);
    }
  }

  function trackTouch(evt) {
    if (!touchCount()) {
      document.addEventListener("pointermove", onTouchMove);
      document.addEventListener("pointerup", onTouchUp);
      document.addEventListener("pointercancel", onTouchUp);
    }
    touches[evt.pointerId] = { x: evt.clientX, y: evt.clientY };
    if (touchCount() === 2) {
      endPan();
      hooks.interrupt();
      var st = pinchState();
      pinch = { d0: st.d, s0: s, p0: toPlan(st.x, st.y) };
      return true;
    }
    return touchCount() > 2;
  }

  // ---------------------------------------------------------------- events

  function isEditable(el) {
    return !!(el && el.closest && el.closest(EDITABLE));
  }

  // Capture phase: runs before the page script's own handlers on the SVG.
  stage.addEventListener("pointerdown", function (evt) {
    suppressClick = false;
    if (evt.pointerType === "touch" && trackTouch(evt)) {
      // A second finger: this is a pinch, not a drag or a click.
      evt.stopPropagation();
      evt.preventDefault();
      return;
    }
    if (evt.button !== 0) {
      return;
    }
    if (spaceHeld) {
      evt.stopPropagation();
      evt.preventDefault();
      hooks.interrupt();
      startPan(evt, true);
      return;
    }
    if (!hooks.noPan(evt.target)) {
      startPan(evt, false);
    }
  }, true);

  stage.addEventListener("click", function (evt) {
    if (suppressClick) {
      suppressClick = false;
      evt.stopPropagation();
      evt.preventDefault();
    }
  }, true);

  // Wheel and trackpad pinch (sent as wheel with ctrlKey). Only over the stage.
  stage.addEventListener("wheel", function (evt) {
    evt.preventDefault();
    var dy = evt.deltaY * (evt.deltaMode === 1 ? 16 : evt.deltaMode === 2 ? 400 : 1);
    zoomBy(Math.exp(-dy * (evt.ctrlKey ? 0.01 : 0.0015)), evt.clientX, evt.clientY);
  }, { passive: false });

  stage.addEventListener("keydown", function (evt) {
    if (evt.altKey || evt.ctrlKey || evt.metaKey || isEditable(evt.target)) {
      return;
    }
    if (evt.key === "+" || evt.key === "=") {
      zoomBy(STEP);
    } else if (evt.key === "-") {
      zoomBy(1 / STEP);
    } else if (evt.key === "0") {
      fit();
    } else {
      return;
    }
    evt.preventDefault();
  });

  stage.addEventListener("pointerenter", function () { hover = true; });
  stage.addEventListener("pointerleave", function () { hover = false; });

  document.addEventListener("keydown", function (evt) {
    if (evt.key !== " " || isEditable(evt.target)) {
      return;
    }
    var ours = hover || stage.contains(document.activeElement);
    if (ours) {
      evt.preventDefault(); // no page scroll while panning the plan
    }
    if (!evt.repeat && ours) {
      spaceHeld = true;
      stage.classList.add("fp-space");
    }
  });

  function releaseSpace() {
    spaceHeld = false;
    stage.classList.remove("fp-space");
  }

  document.addEventListener("keyup", function (evt) {
    if (evt.key === " ") {
      releaseSpace();
    }
  });
  window.addEventListener("blur", releaseSpace);

  function wireButton(id, fn) {
    var b = document.getElementById(id);
    if (b) {
      b.addEventListener("click", fn);
    }
  }
  wireButton("zoom-in", function () { zoomBy(STEP); });
  wireButton("zoom-out", function () { zoomBy(1 / STEP); });
  wireButton("zoom-fit", fit);

  FP.view = {
    MIN: MIN,
    MAX: MAX,
    toPlan: toPlan,
    toClient: toClient,
    inStage: inStage,
    zoom: function () { return s; },
    zoomTo: zoomTo,
    zoomBy: zoomBy,
    fit: fit,
    panBy: panBy,
    showPoint: showPoint,
    fitBox: fitBox,
    stage: stage,
    // The page script supplies noPan, interrupt and change.
    configure: function (h) {
      Object.keys(h).forEach(function (k) {
        if (typeof h[k] === "function" && hooks[k]) { hooks[k] = h[k]; }
      });
      hooks.change(s);
    },
  };
  apply();
})();
