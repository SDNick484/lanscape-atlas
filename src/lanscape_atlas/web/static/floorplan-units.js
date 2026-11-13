/* lanscape-atlas floor plan: real-world lengths. Plain ES2017, no libraries, no build
 * step. Loaded with `defer` before floorplan.js; sets window.LanscapeFP.units.
 *
 * parse(text) -> {mm: number} or {error: string}. Accepted (case-insensitive, spaces
 * optional, a decimal point allowed in each number):
 *   feet and inches  14'6"   14' 6"   14'6   14'   14 ft   14 ft 6 in   14.5 ft
 *   inches           174 in   174"   174 inches
 *   metric           4.4 m   440 cm   4400 mm
 * A bare number has no unit and is refused, as are zero, negative values, inches of 12
 * or more after feet (5'13"), empty input and anything else.
 *
 * format(mm, units): imperial to the nearest inch (14' 6"), metric to the nearest
 * centimetre (4.42 m).
 *
 * grid(units): the blank plan's grid in mm: {minor, major} = 1 ft / 10 ft or
 * 0.5 m / 5 m.
 */
(function () {
  "use strict";

  var FP = (window.LanscapeFP = window.LanscapeFP || {});
  var MM_PER_IN = 25.4;
  var MM_PER_FT = 304.8;
  var NUM = "(\\d+(?:\\.\\d*)?|\\.\\d+)";
  var FT = "(?:'|’|′|ft|foot|feet)";
  var IN = "(?:\"|''|”|″|in|inch|inches)";
  var FEET_INCHES = new RegExp("^" + NUM + "\\s*" + FT + "\\s*(?:" + NUM + "\\s*(?:" + IN + ")?)?$", "i");
  var INCHES = new RegExp("^" + NUM + "\\s*" + IN + "$", "i");
  var METRIC = new RegExp("^" + NUM + "\\s*(m|cm|mm)$", "i");
  var METRIC_MM = { m: 1000, cm: 10, mm: 1 };
  var EXAMPLES = "e.g. 14' 6\", 14 ft, 174 in, 4.4 m or 440 cm";

  function ok(mm) {
    if (!(mm > 0) || !isFinite(mm)) {
      return { error: "The length must be greater than zero." };
    }
    return { mm: mm };
  }

  function parse(text) {
    var s = typeof text === "string" ? text.trim() : "";
    var m;
    if (!s) {
      return { error: "Type a length, " + EXAMPLES + "." };
    }
    if (/^[-−]/.test(s)) {
      return { error: "The length must be greater than zero." };
    }
    m = FEET_INCHES.exec(s);
    if (m) {
      var inches = m[2] === undefined ? 0 : parseFloat(m[2]);
      if (inches >= 12) {
        return { error: "Inches after feet must be less than 12 (" + s + ")." };
      }
      return ok(parseFloat(m[1]) * MM_PER_FT + inches * MM_PER_IN);
    }
    m = INCHES.exec(s);
    if (m) {
      return ok(parseFloat(m[1]) * MM_PER_IN);
    }
    m = METRIC.exec(s);
    if (m) {
      return ok(parseFloat(m[1]) * METRIC_MM[m[2].toLowerCase()]);
    }
    if (/^[\d.]+$/.test(s)) {
      return { error: "Add a unit, " + EXAMPLES + "." };
    }
    return { error: "Not a length: use " + EXAMPLES + "." };
  }

  function format(mm, units) {
    if (typeof mm !== "number" || !isFinite(mm)) {
      return "";
    }
    if (units === "metric") {
      return (Math.round(mm / 10) / 100).toFixed(2) + " m";
    }
    var total = Math.round(mm / MM_PER_IN);
    var ft = Math.floor(total / 12);
    return ft + "' " + (total - ft * 12) + "\"";
  }

  function grid(units) {
    return units === "metric" ? { minor: 500, major: 5000 } : { minor: MM_PER_FT, major: 10 * MM_PER_FT };
  }

  FP.units = { parse: parse, format: format, grid: grid, MM_PER_FT: MM_PER_FT };
})();
