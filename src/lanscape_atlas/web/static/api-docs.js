// Renders the API description at /docs with the vendored Swagger UI bundle.
// Lives in a file, not an inline script, so a strict Content-Security-Policy
// can forbid inline scripts. The spec is /api/openapi.json, which needs the bearer
// token when one is configured: the page asks for it, keeps it in memory only, and
// sends it on the spec request and on "Try it out" calls to this origin.
(function () {
  "use strict";

  var SPEC_URL = "/api/openapi.json";
  var gate = document.getElementById("api-docs-gate");
  var form = document.getElementById("api-docs-form");
  var tokenInput = document.getElementById("api-docs-token");
  var gateError = document.getElementById("api-docs-error");
  var status = document.getElementById("api-docs-status");

  function fail(message) {
    status.textContent = message;
    status.hidden = false;
  }

  function render(spec, token) {
    gate.hidden = true;
    status.hidden = true;
    window.SwaggerUIBundle({
      spec: spec,
      // The default validator is an external service; keep the page self-contained.
      validatorUrl: null,
      docExpansion: "list",
      requestInterceptor: function (req) {
        if (token && req.url.indexOf(window.location.origin + "/") === 0) {
          req.headers.Authorization = "Bearer " + token;
        }
        return req;
      },
      dom_id: "#swagger-ui",
    });
  }

  // token is "" when none was given. A 401 without a token asks for one; a 401 with a
  // token means the token was refused.
  function load(token) {
    var headers = token ? { Authorization: "Bearer " + token } : {};
    fetch(SPEC_URL, { headers: headers, credentials: "same-origin" })
      .then(function (res) {
        if (res.status === 401) {
          gate.hidden = false;
          status.hidden = true;
          gateError.textContent = token ? "The API token was rejected." : "";
          tokenInput.focus();
          return;
        }
        if (!res.ok) {
          throw new Error("The API description could not be loaded (HTTP " + res.status + ").");
        }
        return res.json().then(function (spec) {
          render(spec, token);
        });
      })
      .catch(function (err) {
        fail(err && err.message ? err.message : "The API description could not be loaded.");
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var token = tokenInput.value.trim();
    tokenInput.value = "";
    gateError.textContent = "";
    if (token) {
      status.textContent = "Loading the API description...";
      status.hidden = false;
      gate.hidden = true;
      load(token);
    }
  });

  if (!window.SwaggerUIBundle) {
    fail("The Swagger UI bundle did not load.");
    return;
  }
  load("");
})();
