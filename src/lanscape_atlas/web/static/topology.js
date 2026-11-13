/* lanscape-atlas topology view (Task 17). Plain ES2017, no build step.
 *
 * Reads the graph from GET {root}/topology/data and writes through the
 * CSRF-protected JSON page endpoints (X-CSRF-Token header):
 *   POST /topology/layout            {"positions": {id: [x, y]}}
 *   POST /topology/links             {a_device_id, b_device_id, kind}
 *   POST /topology/links/{id}/delete
 *   POST /topology/placeholder       {name, type}
 * and searches with GET /topology/search?q= (ranked {device_id, name, type}).
 *
 * Everything from the server is untrusted: the DOM is built with textContent
 * only (graph labels are drawn on the canvas by Cytoscape, never as HTML).
 * The Cytoscape instance is exposed as window.atlasCy for debugging and tests.
 */
(function () {
  "use strict";

  var host = document.getElementById("topology");
  if (!host || typeof window.cytoscape !== "function") {
    return;
  }
  var ROOT = host.getAttribute("data-root") || "";
  var CSRF = host.getAttribute("data-csrf") || "";
  var SAVE_DELAY_MS = 1000;
  var PLACE_COLOURS = 10;
  var ZOOM_STEP = 1.25;
  var SEARCH_DELAY_MS = 200;
  var HOWTO_KEY = "atlas.topology.howto";

  var statusEl = document.getElementById("topo-status");
  var panel = document.getElementById("topo-panel");
  var connectBtn = document.getElementById("topo-connect");
  var deleteBtn = document.getElementById("topo-delete");
  var cy = null;

  // ------------------------------------------------------------ helpers

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.classList.toggle("error-text", !!isError);
  }

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function request(method, path, body, keepalive) {
    var opts = { method: method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (keepalive) {
      // Lets the final layout save outlive the page on unload.
      opts.keepalive = true;
    }
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-CSRF-Token"] = CSRF;
      opts.body = JSON.stringify(body || {});
    }
    return fetch(ROOT + path, opts).then(function (r) {
      return r.json().then(
        function (data) { return { r: r, data: data }; },
        function () { return { r: r, data: null }; }
      );
    }).then(function (res) {
      if (!res.r.ok) {
        var detail = res.data && res.data.detail;
        var msg = typeof detail === "string" ? detail : "request failed (HTTP " + res.r.status + ")";
        throw new Error(msg);
      }
      return res.data;
    });
  }

  function isId(v) {
    return typeof v === "number" && isFinite(v) && Math.floor(v) === v && v > 0;
  }

  function nodeKey(deviceId) { return "d" + deviceId; }

  function edgeKey(e) { return (e.manual ? "l" : "x") + String(e.id); }

  function nodeLabel(n) {
    var name = typeof n.name === "string" ? n.name.trim() : "";
    return name || "#" + n.id;
  }

  // ----------------------------------------------------------- elements

  function nodeElement(n) {
    var positioned = typeof n.x === "number" && typeof n.y === "number" && isFinite(n.x) && isFinite(n.y);
    var el = {
      group: "nodes",
      data: {
        id: nodeKey(n.id),
        deviceId: n.id,
        label: nodeLabel(n),
        name: typeof n.name === "string" ? n.name : "",
        type: typeof n.type === "string" ? n.type : "",
        online: n.online === true,
        placeholder: n.placeholder === true,
        placeId: isId(n.place_id) ? n.place_id : null,
        // Palette slot; -1 (no place, or a deleted one) keeps the neutral fill.
        placeIdx: isId(n.place_id) ? n.place_id % PLACE_COLOURS : -1,
        saved: positioned
      },
      // Placeholders (no interfaces) can never be seen online: not dimmed.
      classes: n.online === true || n.placeholder === true ? "" : "offline"
    };
    if (positioned) {
      el.position = { x: n.x, y: n.y };
    }
    return el;
  }

  function edgeElement(e) {
    var kind = e.kind === "wifi" || e.kind === "mesh_backhaul" ? e.kind : "wired";
    return {
      group: "edges",
      data: {
        id: edgeKey(e),
        source: nodeKey(e.source),
        target: nodeKey(e.target),
        kind: kind,
        manual: e.manual === true,
        linkId: e.manual === true ? e.id : null
      }
    };
  }

  function edgeElements(graph, nodeIds) {
    var out = [];
    (graph.edges || []).forEach(function (e) {
      if (!isId(e.source) || !isId(e.target)) { return; }
      if (!nodeIds[e.source] || !nodeIds[e.target]) { return; }
      out.push(edgeElement(e));
    });
    return out;
  }

  // -------------------------------------------------------------- style

  function buildStyle() {
    var c = {
      text: cssVar("--text", "#1d1d1f"),
      bg: cssVar("--bg", "#f7f7f5"),
      surface: cssVar("--surface", "#ffffff"),
      muted: cssVar("--muted", "#6b6b70"),
      accent: cssVar("--accent", "#2f6fdf"),
      online: cssVar("--online", "#2ea043"),
      offline: cssVar("--offline", "#a0a0a6"),
      wired: cssVar("--edge-wired", "#6b6b70"),
      wifi: cssVar("--edge-wifi", "#b5711d"),
      node: cssVar("--node", "#e8eefb")
    };
    var placeRules = [];
    for (var i = 0; i < PLACE_COLOURS; i++) {
      placeRules.push({
        selector: "node[placeIdx = " + i + "]",
        style: { "background-color": cssVar("--place-" + i, c.node) }
      });
    }
    return [
      {
        selector: "node",
        style: {
          "label": "data(label)",
          "width": 22,
          "height": 22,
          "background-color": c.node,
          "border-width": 2,
          "border-color": c.online,
          "color": c.text,
          "font-size": 10,
          "text-valign": "bottom",
          "text-margin-y": 4,
          "text-outline-color": c.bg,
          "text-outline-width": 2,
          "text-max-width": "100px",
          "text-wrap": "ellipsis"
        }
      },
      { selector: 'node[type = "router"]', style: { "shape": "diamond", "width": 34, "height": 34 } },
      { selector: 'node[type = "mesh_node"], node[type = "ap"]', style: { "shape": "hexagon", "width": 28, "height": 28 } },
      { selector: 'node[type = "switch"], node[type = "hub"]', style: { "shape": "round-rectangle", "width": 30, "height": 18 } },
    ].concat(placeRules, [
      { selector: "node.lbl-alt", style: { "text-valign": "top", "text-margin-y": -4 } },
      { selector: "node.offline", style: { "opacity": 0.45, "border-color": c.offline } },
      { selector: "node:selected", style: { "border-color": c.accent, "border-width": 4, "opacity": 1 } },
      {
        selector: "node.pulse",
        style: {
          "border-color": c.accent,
          "border-width": 7,
          "opacity": 1,
          "transition-property": "border-width, border-color",
          "transition-duration": "0.25s"
        }
      },
      {
        selector: "edge",
        style: {
          "width": 1.6,
          "curve-style": "bezier",
          "line-color": c.wired,
          "line-style": "solid"
        }
      },
      { selector: 'edge[kind = "wifi"]', style: { "line-color": c.wifi } },
      { selector: 'edge[kind = "mesh_backhaul"]', style: { "line-color": c.wifi, "width": 3 } },
      { selector: "edge[!manual]", style: { "line-style": "dashed", "line-dash-pattern": [6, 4] } },
      { selector: "edge:selected", style: { "line-color": c.accent, "width": 3.5 } }
    ]);
  }

  // ------------------------------------------------------------- layout

  // Derived (non-manual) edges per device: incoming = clients whose AP it is,
  // outgoing = its own uplink to another AP.
  function apStats(graph) {
    var incoming = {};
    var outgoing = {};
    (graph.edges || []).forEach(function (e) {
      if (e.manual === true || !isId(e.source) || !isId(e.target)) { return; }
      incoming[e.target] = (incoming[e.target] || 0) + 1;
      outgoing[e.source] = (outgoing[e.source] || 0) + 1;
    });
    return { incoming: incoming, outgoing: outgoing };
  }

  function bestBy(nodes, score) {
    var best = null;
    var bestScore = -Infinity;
    nodes.forEach(function (n) {
      var s = score(n);
      if (s > bestScore) { best = n; bestScore = s; }
    });
    return best;
  }

  // Root of each component (Ruling 38): the router; else the top of the AP
  // chain (an AP target with no derived uplink of its own; ties by incoming
  // derived edges, then degree); else the busiest AP target / best-connected node.
  function pickRoots(eles, stats) {
    var roots = cy.collection();
    eles.components().forEach(function (comp) {
      var nodes = comp.nodes();
      var router = nodes.filter(function (n) { return n.data("type") === "router"; });
      if (router.nonempty()) {
        roots = roots.add(router[0]);
        return;
      }
      function inc(n) { return stats.incoming[n.data("deviceId")] || 0; }
      var tops = nodes.filter(function (n) {
        return inc(n) > 0 && !stats.outgoing[n.data("deviceId")];
      });
      var best = tops.nonempty()
        ? bestBy(tops, function (n) { return inc(n) * 10000 + n.degree(false); })
        : bestBy(nodes, function (n) { return inc(n) * 10000 + n.degree(false); });
      if (best) { roots = roots.add(best); }
    });
    return roots;
  }

  function layoutUnpositioned(graph) {
    var fresh = cy.nodes().filter(function (n) { return !n.data("saved"); });
    if (fresh.empty()) { return; }
    var edges = fresh.connectedEdges().filter(function (e) {
      return fresh.has(e.source()) && fresh.has(e.target());
    });
    var eles = fresh.union(edges);
    var opts = {
      name: "breadthfirst",
      directed: false,
      roots: pickRoots(eles, apStats(graph)),
      spacingFactor: 1.15,
      avoidOverlap: true,
      animate: false,
      fit: false
    };
    var placed = cy.nodes().difference(fresh);
    var width = Math.max(cy.width(), 600);
    if (placed.nonempty()) {
      // Lay new devices out below the saved arrangement instead of on top of it.
      var bb = placed.boundingBox();
      opts.boundingBox = { x1: bb.x1, y1: bb.y2 + 80, w: Math.max(bb.w, width), h: Math.max(300, fresh.length * 8) };
    } else {
      opts.boundingBox = { x1: 0, y1: 0, w: width, h: Math.max(cy.height(), 400) };
    }
    eles.layout(opts).run();
  }

  // Devices sharing a row (same y) alternate their labels above and below,
  // so long names on a wide row do not run into each other.
  function staggerLabels() {
    var rows = {};
    cy.nodes().forEach(function (n) {
      var key = String(Math.round(n.position("y")));
      (rows[key] = rows[key] || []).push(n);
    });
    cy.batch(function () {
      Object.keys(rows).forEach(function (key) {
        var row = rows[key].sort(function (a, b) { return a.position("x") - b.position("x"); });
        row.forEach(function (n, i) { n.toggleClass("lbl-alt", row.length > 1 && i % 2 === 1); });
      });
    });
  }

  // --------------------------------------------------------------- save

  var pending = {};
  var saveTimer = null;

  function queueSave(nodes) {
    nodes.forEach(function (n) { pending[n.id()] = n; });
    // The first drag also pins the auto-laid-out devices, so the picture the
    // user arranged is the one that comes back on reload.
    cy.nodes().forEach(function (n) {
      if (!n.data("saved")) { pending[n.id()] = n; }
    });
    if (saveTimer !== null) { clearTimeout(saveTimer); }
    saveTimer = setTimeout(function () { flushSave(false); }, SAVE_DELAY_MS);
  }

  function flushSave(keepalive) {
    saveTimer = null;
    var positions = {};
    var batch = [];
    Object.keys(pending).forEach(function (key) {
      var n = pending[key];
      if (n.removed()) { return; }
      var p = n.position();
      if (!isFinite(p.x) || !isFinite(p.y)) { return; }
      positions[String(n.data("deviceId"))] = [p.x, p.y];
      batch.push(n);
    });
    pending = {};
    if (!batch.length) { return; }
    request("POST", "/topology/layout", { positions: positions }, keepalive).then(function () {
      batch.forEach(function (n) { n.data("saved", true); });
      setStatus("Layout saved.");
    }).catch(function (err) {
      // Keep the batch so the next save retries it (positions are read at flush time).
      batch.forEach(function (n) {
        if (!pending[n.id()]) { pending[n.id()] = n; }
      });
      setStatus("Could not save layout: " + err.message, true);
    });
  }

  // ------------------------------------------------------------ toolbar

  function selectedNodes() { return cy.nodes(":selected"); }
  function selectedEdges() { return cy.edges(":selected"); }

  function updateButtons() {
    var nodes = selectedNodes();
    var edges = selectedEdges();
    connectBtn.disabled = nodes.length !== 2;
    var one = edges.length === 1 ? edges[0] : null;
    deleteBtn.disabled = !(one && one.data("manual") === true);
    deleteBtn.title = one && one.data("manual") !== true
      ? "Discovered edges cannot be deleted"
      : "Select a manual link";
  }

  function syncEdges() {
    return request("GET", "/topology/data").then(function (graph) {
      var ids = {};
      cy.nodes().forEach(function (n) { ids[n.data("deviceId")] = true; });
      cy.batch(function () {
        cy.edges().remove();
        cy.add(edgeElements(graph, ids));
      });
      updateButtons();
    });
  }

  function onConnect() {
    var nodes = selectedNodes();
    if (nodes.length !== 2) { return; }
    var body = {
      a_device_id: nodes[0].data("deviceId"),
      b_device_id: nodes[1].data("deviceId"),
      kind: "wired"
    };
    connectBtn.disabled = true;
    request("POST", "/topology/links", body).then(function () {
      setStatus("Connected " + nodes[0].data("label") + " and " + nodes[1].data("label") + ".");
      return syncEdges();
    }).catch(function (err) {
      setStatus("Could not connect: " + err.message, true);
      updateButtons();
    });
  }

  function onDelete() {
    var edges = selectedEdges();
    if (edges.length !== 1 || edges[0].data("manual") !== true) { return; }
    var linkId = edges[0].data("linkId");
    if (!isId(linkId)) { return; }
    deleteBtn.disabled = true;
    request("POST", "/topology/links/" + linkId + "/delete").then(function () {
      setStatus("Link deleted.");
      return syncEdges();
    }).catch(function (err) {
      setStatus("Could not delete link: " + err.message, true);
      updateButtons();
    });
  }

  function onAdd(ev) {
    ev.preventDefault();
    var nameInput = document.getElementById("topo-name");
    var typeSelect = document.getElementById("topo-type");
    var name = nameInput.value.trim();
    var type = typeSelect.value;
    if (!name) {
      setStatus("Give the new device a name.", true);
      nameInput.focus();
      return;
    }
    request("POST", "/topology/placeholder", { name: name, type: type }).then(function (res) {
      if (!res || !isId(res.device_id)) { throw new Error("unexpected response"); }
      var ext = cy.extent();
      var el = nodeElement({
        id: res.device_id, name: name, type: type, online: false, placeholder: true, x: null, y: null
      });
      el.position = { x: (ext.x1 + ext.x2) / 2, y: ext.y1 + (ext.y2 - ext.y1) * 0.15 };
      var node = cy.add(el);
      cy.elements().unselect();
      node.select();
      nameInput.value = "";
      setStatus("Added " + node.data("label") + ".");
      queueSave(node);
      showPanel(node);
    }).catch(function (err) {
      setStatus("Could not add device: " + err.message, true);
    });
  }

  // -------------------------------------------------------------- panel

  function showPanel(node) {
    var id = node.data("deviceId");
    document.getElementById("topo-panel-name").textContent = node.data("label");
    document.getElementById("topo-panel-type").textContent = node.data("type") || "—";
    var state = document.getElementById("topo-panel-state");
    var online = node.data("online") === true;
    state.textContent = online ? "online" : node.data("placeholder") ? "placeholder (not monitored)" : "offline";
    state.className = online ? "state online" : "state offline";
    var link = document.getElementById("topo-panel-link");
    link.setAttribute("href", ROOT + "/devices/" + encodeURIComponent(String(id)));
    panel.hidden = false;
  }

  function hidePanel() { panel.hidden = true; }

  // ---------------------------------------------------- legend and zoom

  // One swatch per place that has at least one node, plus "no place". Place
  // names are untrusted: textContent only.
  function renderPlaceLegend(graph) {
    var box = document.getElementById("topo-places");
    if (!box) { return; }
    while (box.firstChild) { box.removeChild(box.firstChild); }
    var used = {};
    (graph.nodes || []).forEach(function (n) {
      if (isId(n.place_id)) { used[n.place_id] = true; }
    });
    function item(cls, label) {
      var wrap = document.createElement("span");
      wrap.className = "leg-item";
      var sw = document.createElement("span");
      sw.className = "sw-place " + cls;
      sw.setAttribute("aria-hidden", "true");
      var text = document.createElement("span");
      text.className = "leg-name";
      text.textContent = label;
      wrap.appendChild(sw);
      wrap.appendChild(text);
      box.appendChild(wrap);
    }
    (graph.places || []).forEach(function (p) {
      if (!p || !isId(p.id) || !used[p.id]) { return; }
      var name = typeof p.name === "string" && p.name.trim() ? p.name : "#" + p.id;
      item("p" + (p.id % PLACE_COLOURS), name);
    });
    item("none", "no place");
  }

  // Zoom about the centre of the view, clamped to the zoom limits. A no-op at a limit.
  function zoomBy(factor) {
    if (!cy) { return; }
    var next = Math.min(cy.maxZoom(), Math.max(cy.minZoom(), cy.zoom() * factor));
    if (next === cy.zoom()) { return; }
    cy.zoom({ level: next, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }

  function onZoomKey(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) { return; }
    if (ev.key === "+" || ev.key === "=") {
      ev.preventDefault();
      zoomBy(ZOOM_STEP);
    } else if (ev.key === "-" || ev.key === "_") {
      ev.preventDefault();
      zoomBy(1 / ZOOM_STEP);
    }
  }

  // ------------------------------------------------------------- how-to

  function initHowTo() {
    var box = document.getElementById("topo-howto");
    if (!box) { return; }
    try {
      // Open unless the visitor closed it before; storage may be unavailable.
      if (window.localStorage.getItem(HOWTO_KEY) === "closed") { box.open = false; }
    } catch (e) { /* default: open */ }
    box.addEventListener("toggle", function () {
      try {
        window.localStorage.setItem(HOWTO_KEY, box.open ? "open" : "closed");
      } catch (e) { /* ignore */ }
    });
  }

  // ------------------------------------------------------------- search

  var searchInput = document.getElementById("topo-search");
  var searchList = document.getElementById("topo-search-list");
  var searchTimer = null;
  var searchSeq = 0;
  var searchActive = -1;

  function searchOptions() { return searchList ? searchList.children : []; }

  function closeSearch() {
    if (!searchList) { return; }
    searchList.hidden = true;
    searchActive = -1;
    searchInput.setAttribute("aria-expanded", "false");
    searchInput.removeAttribute("aria-activedescendant");
  }

  function setActive(i) {
    var opts = searchOptions();
    if (!opts.length) { return; }
    searchActive = (i + opts.length) % opts.length;
    for (var k = 0; k < opts.length; k++) {
      opts[k].setAttribute("aria-selected", k === searchActive ? "true" : "false");
    }
    searchInput.setAttribute("aria-activedescendant", opts[searchActive].id);
    opts[searchActive].scrollIntoView({ block: "nearest" });
  }

  function renderResults(results) {
    while (searchList.firstChild) { searchList.removeChild(searchList.firstChild); }
    searchActive = -1;
    searchInput.removeAttribute("aria-activedescendant");
    var shown = 0;
    results.forEach(function (r) {
      if (!r || !isId(r.device_id)) { return; }
      var li = document.createElement("li");
      li.id = "topo-search-opt-" + shown;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      li.setAttribute("data-device-id", String(r.device_id));
      var name = typeof r.name === "string" && r.name.trim() ? r.name : "#" + r.device_id;
      var nameEl = document.createElement("span");
      nameEl.textContent = name;
      li.appendChild(nameEl);
      if (typeof r.type === "string" && r.type) {
        var typeEl = document.createElement("span");
        typeEl.className = "muted";
        typeEl.textContent = " " + r.type;
        li.appendChild(typeEl);
      }
      // mousedown, not click: runs before the input's blur can close the list.
      li.addEventListener("mousedown", function (ev) {
        ev.preventDefault();
        pickResult(r.device_id);
      });
      searchList.appendChild(li);
      shown += 1;
    });
    if (!shown) {
      var none = document.createElement("li");
      none.className = "muted";
      none.setAttribute("role", "presentation");
      none.textContent = "No matches";
      searchList.appendChild(none);
    }
    searchList.hidden = false;
    searchInput.setAttribute("aria-expanded", "true");
  }

  function runSearch() {
    searchTimer = null;
    var q = searchInput.value.trim();
    var seq = ++searchSeq;
    if (!q) { closeSearch(); return; }
    request("GET", "/topology/search?q=" + encodeURIComponent(q)).then(function (results) {
      if (seq !== searchSeq) { return; }  // a newer query is in flight
      renderResults(Array.isArray(results) ? results : []);
    }).catch(function (err) {
      if (seq !== searchSeq) { return; }
      closeSearch();
      setStatus("Search failed: " + err.message, true);
    });
  }

  function pickResult(deviceId) {
    closeSearch();
    if (!cy) { return; }
    var node = cy.getElementById(nodeKey(deviceId));
    if (node.empty()) {
      setStatus("That device is not on the graph.", true);
      return;
    }
    searchInput.value = node.data("label");
    cy.elements().unselect();
    node.select();
    showPanel(node);
    // Pan explicitly: `center: {eles}` would centre the node's box including its label.
    var zoom = Math.min(cy.maxZoom(), Math.max(cy.zoom(), 1.5));
    var pos = node.position();
    cy.animate(
      { zoom: zoom, pan: { x: cy.width() / 2 - pos.x * zoom, y: cy.height() / 2 - pos.y * zoom } },
      { duration: 300 }
    );
    node.addClass("pulse");
    setTimeout(function () { node.removeClass("pulse"); }, 1000);
  }

  function onSearchKey(ev) {
    var open = !searchList.hidden;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      if (!open && searchInput.value.trim()) { runSearch(); return; }
      setActive(searchActive + 1);
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      setActive(searchActive < 0 ? -1 : searchActive - 1);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      var opts = searchOptions();
      var opt = open && opts.length ? opts[searchActive >= 0 ? searchActive : 0] : null;
      var id = opt ? Number(opt.getAttribute("data-device-id")) : 0;
      if (isId(id)) { pickResult(id); }
    } else if (ev.key === "Escape") {
      if (open) { ev.preventDefault(); closeSearch(); }
    }
  }

  function initSearch() {
    if (!searchInput || !searchList) { return; }
    searchInput.addEventListener("input", function () {
      if (searchTimer !== null) { clearTimeout(searchTimer); }
      searchTimer = setTimeout(runSearch, SEARCH_DELAY_MS);
    });
    searchInput.addEventListener("keydown", onSearchKey);
    searchInput.addEventListener("blur", closeSearch);
  }

  // --------------------------------------------------------------- init

  function init(graph) {
    var nodeIds = {};
    var nodes = [];
    (graph.nodes || []).forEach(function (n) {
      if (!isId(n.id) || nodeIds[n.id]) { return; }
      nodeIds[n.id] = true;
      nodes.push(nodeElement(n));
    });
    cy = window.cytoscape({
      container: document.getElementById("cy"),
      elements: nodes.concat(edgeElements(graph, nodeIds)),
      style: buildStyle(),
      layout: { name: "preset", fit: false },
      boxSelectionEnabled: true,
      wheelSensitivity: 0.3,
      minZoom: 0.1,
      maxZoom: 4
    });
    window.atlasCy = cy;
    renderPlaceLegend(graph);
    layoutUnpositioned(graph);
    staggerLabels();
    cy.fit(undefined, 30);

    cy.on("dragfree", "node", function (ev) { queueSave(ev.target); });
    cy.on("select unselect add remove", updateButtons);
    cy.on("tap", "node", function (ev) { showPanel(ev.target); });
    cy.on("tap", function (ev) {
      if (ev.target === cy) {
        cy.elements().unselect();
        hidePanel();
      }
    });
    updateButtons();

    var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    if (mq && mq.addEventListener) {
      mq.addEventListener("change", function () { cy.style(buildStyle()); });
    }
    setStatus(cy.nodes().length + " devices, " + cy.edges().length + " links.");
  }

  connectBtn.addEventListener("click", onConnect);
  deleteBtn.addEventListener("click", onDelete);
  document.getElementById("topo-fit").addEventListener("click", function () {
    if (cy) { cy.fit(undefined, 30); }
  });
  document.getElementById("topo-zoom-in").addEventListener("click", function () { zoomBy(ZOOM_STEP); });
  document.getElementById("topo-zoom-out").addEventListener("click", function () { zoomBy(1 / ZOOM_STEP); });
  document.getElementById("cy").addEventListener("keydown", onZoomKey);
  document.getElementById("topo-add").addEventListener("submit", onAdd);
  initHowTo();
  initSearch();
  window.addEventListener("beforeunload", function () {
    if (saveTimer !== null) {
      clearTimeout(saveTimer);
      flushSave(true);
    }
  });

  request("GET", "/topology/data").then(init).catch(function (err) {
    setStatus("Could not load the topology: " + err.message, true);
  });
})();
