// Small progressive enhancements. Everything works without this file.
(function () {
  "use strict";

  // Interested / Not interested without a page reload.
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains("js-feedback") || !window.fetch) return;
    ev.preventDefault();
    var card = form.closest(".card");
    var buttons = card ? card.querySelectorAll("button") : [];
    buttons.forEach(function (b) { b.disabled = true; });
    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch", "Accept": "application/json" }
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (data) {
      if (!card) return;
      card.classList.toggle("is-hidden", data.interest === -1);
      var yes = card.querySelector(".fb-yes");
      if (yes) yes.setAttribute("aria-pressed", data.interest === 1 ? "true" : "false");
      buttons.forEach(function (b) { b.disabled = false; });
    }).catch(function () {
      form.submit(); // fall back to the plain form post
    });
  });

  // While an edition is building, check every few seconds and refresh when it's done.
  var banner = document.querySelector("[data-building]");
  if (banner && window.fetch) {
    var reload = document.body.classList.contains("page-edition") || document.body.classList.contains("page-empty");
    var poll = function () {
      fetch("/status", { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (s.building) { setTimeout(poll, 4000); return; }
          if (reload) {
            window.location.href = window.location.pathname === "/" ? "/" : window.location.pathname;
          } else {
            banner.textContent = "The new edition is ready.";
          }
        })
        .catch(function () { setTimeout(poll, 8000); });
    };
    setTimeout(poll, 3000);
  }

  // Offline reading. Browsers only allow service workers on https:// or localhost.
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
  }
})();
