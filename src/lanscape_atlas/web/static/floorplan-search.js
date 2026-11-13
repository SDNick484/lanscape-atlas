/* lanscape-atlas floor plan page: the search box (spec §4.2). Plain ES2017, no
 * libraries, no build step. Loaded with `defer` after floorplan-view.js and
 * floorplan.js; it uses LanscapeFP.page (revealDevice / revealRoom / clearFound).
 *
 * The same combobox as Topology's: typing queries GET /floorplan/search?q= after
 * 200 ms; ArrowDown / ArrowUp move through the results, Enter (or a click) picks one,
 * Escape closes the list. Results are ranked devices then rooms, each tagged
 * `kind: "device" | "room"`; names are untrusted and only ever set as textContent.
 * Emptying the box clears the highlight a pick left behind.
 */
(function () {
  "use strict";

  var FP = window.LanscapeFP || {};
  var app = document.getElementById("fp-app");
  var input = document.getElementById("fp-search");
  var list = document.getElementById("fp-search-list");
  if (!app || !input || !list || !FP.page) {
    return;
  }
  var ROOT = app.getAttribute("data-root") || "";
  var DELAY_MS = 200;
  var timer = null;
  var seq = 0;
  var active = -1;

  function isId(v) { return typeof v === "number" && Math.floor(v) === v && v > 0; }

  function options() { return list.querySelectorAll("[role=option]"); }

  function close() {
    list.hidden = true;
    active = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function setActive(i) {
    var opts = options();
    if (!opts.length) {
      return;
    }
    active = (i + opts.length) % opts.length;
    for (var k = 0; k < opts.length; k += 1) {
      opts[k].setAttribute("aria-selected", k === active ? "true" : "false");
    }
    input.setAttribute("aria-activedescendant", opts[active].id);
    opts[active].scrollIntoView({ block: "nearest" });
  }

  function pick(kind, id, name) {
    close();
    input.value = name;
    if (kind === "room") {
      FP.page.revealRoom(id);
    } else {
      FP.page.revealDevice(id);
    }
  }

  function option(r, n) {
    var kind = r.kind === "room" ? "room" : "device";
    var id = kind === "room" ? r.place_id : r.device_id;
    if (!isId(id)) {
      return null;
    }
    var name = typeof r.name === "string" && r.name.trim() ? r.name : (kind === "room" ? "Room" : "Device") + " #" + id;
    var li = document.createElement("li");
    li.id = "fp-search-opt-" + n;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", "false");
    li.setAttribute("data-kind", kind);
    li.setAttribute("data-id", String(id));
    var nameEl = document.createElement("span");
    nameEl.textContent = name;
    li.appendChild(nameEl);
    var what = kind === "room" ? "room" : typeof r.type === "string" && r.type ? r.type : "";
    if (what) {
      var tag = document.createElement("span");
      tag.className = "muted";
      tag.textContent = " " + what;
      li.appendChild(tag);
    }
    // mousedown, not click: runs before the input's blur can close the list.
    li.addEventListener("mousedown", function (ev) {
      ev.preventDefault();
      pick(kind, id, name);
    });
    return li;
  }

  function show(results) {
    while (list.firstChild) {
      list.removeChild(list.firstChild);
    }
    active = -1;
    input.removeAttribute("aria-activedescendant");
    var n = 0;
    results.forEach(function (r) {
      var li = r && typeof r === "object" ? option(r, n) : null;
      if (li) {
        list.appendChild(li);
        n += 1;
      }
    });
    if (!n) {
      var none = document.createElement("li");
      none.className = "muted";
      none.setAttribute("role", "presentation");
      none.textContent = "No matches";
      list.appendChild(none);
    }
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function run() {
    timer = null;
    var q = input.value.trim();
    var mine = ++seq;
    if (!q) {
      close();
      FP.page.clearFound();
      return;
    }
    fetch(ROOT + "/floorplan/search?q=" + encodeURIComponent(q), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).then(function (r) {
      if (!r.ok) {
        throw new Error("HTTP " + r.status);
      }
      return r.json();
    }).then(function (data) {
      if (mine === seq) { // ignore answers to older queries
        show(Array.isArray(data) ? data : []);
      }
    }).catch(function () {
      if (mine === seq) {
        close();
      }
    });
  }

  input.addEventListener("input", function () {
    if (timer !== null) {
      clearTimeout(timer);
    }
    timer = setTimeout(run, DELAY_MS);
  });

  input.addEventListener("keydown", function (ev) {
    var open = !list.hidden;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      if (!open && input.value.trim()) {
        run();
        return;
      }
      setActive(active + 1);
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      setActive(active < 0 ? -1 : active - 1);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      var opts = options();
      var opt = open && opts.length ? opts[active >= 0 ? active : 0] : null;
      if (opt) {
        var id = Number(opt.getAttribute("data-id"));
        if (isId(id)) {
          pick(opt.getAttribute("data-kind"), id, opt.firstChild.textContent);
        }
      }
    } else if (ev.key === "Escape") {
      if (open) {
        ev.preventDefault();
        close();
      }
    }
  });

  input.addEventListener("blur", close);
})();
