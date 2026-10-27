// Send to X3 admin: copy buttons, delete confirmation, and removing ?token= from the address bar.
(function () {
  "use strict";

  // The admin token is only needed once (it sets a session cookie); do not leave it in history.
  try {
    var url = new URL(window.location.href);
    if (url.searchParams.has("token")) {
      url.searchParams.delete("token");
      window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    }
  } catch (e) { /* old browser: nothing to do */ }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.className = "sr";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function flashButton(btn, label) {
    var old = btn.textContent;
    btn.textContent = label;
    window.setTimeout(function () { btn.textContent = old; }, 1500);
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest("button.copy") : null;
    if (!btn) return;
    var el = document.getElementById(btn.getAttribute("data-copy"));
    if (!el) return;
    var text = el.textContent.trim();
    // navigator.clipboard needs a secure context (https); plain-http LAN pages use the fallback.
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { flashButton(btn, "Copied"); },
        function () { flashButton(btn, fallbackCopy(text) ? "Copied" : "Select and copy"); }
      );
    } else {
      flashButton(btn, fallbackCopy(text) ? "Copied" : "Select and copy");
    }
  });

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var msg = form.getAttribute && form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) ev.preventDefault();
  });
})();
