// Small progressive enhancements. Everything works without this file.
(function () {
  "use strict";

  var READ_KEY = "paper-read-marks";   // recent read/unread changes, so Back shows them straight away

  function post(url, data, keepalive) {
    var body = new FormData();
    Object.keys(data).forEach(function (k) { body.append(k, data[k]); });
    return fetch(url, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      keepalive: !!keepalive,
      headers: { "X-Requested-With": "fetch", "Accept": "application/json" }
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // --- read marks ---------------------------------------------------------
  function loadMarks() {
    try { return JSON.parse(localStorage.getItem(READ_KEY) || "{}"); } catch (e) { return {}; }
  }
  function rememberMark(id, read) {
    try {
      var marks = loadMarks();
      delete marks[id];
      marks[id] = read ? 1 : 0;
      var keys = Object.keys(marks);
      while (keys.length > 300) { delete marks[keys.shift()]; }
      localStorage.setItem(READ_KEY, JSON.stringify(marks));
    } catch (e) { /* private mode etc.: the server still has it */ }
  }
  function showRead(card, read) {
    if (!card) return;
    card.classList.toggle("is-read", read);
    var mark = card.querySelector(".read-mark");
    if (mark) mark.hidden = !read;
    var form = card.querySelector("form.js-read");
    if (form) {
      form.querySelector("input[name=value]").value = read ? "unread" : "read";
      form.querySelector("button").textContent = read ? "Mark as unread" : "Mark as read";
    }
  }
  function setRead(id, read, keepalive) {
    rememberMark(id, read);
    showRead(document.getElementById("story-" + id), read);
    return post("/story/" + id + "/read", { value: read ? "read" : "unread" }, keepalive).catch(function () {});
  }

  // Coming back with the browser's Back button can show a cached copy of the paper: apply recent marks.
  window.addEventListener("pageshow", function (ev) {
    if (!ev.persisted) return;   // a fresh page already has the server's marks
    var marks = loadMarks();
    Object.keys(marks).forEach(function (id) {
      var card = document.getElementById("story-" + id);
      if (card && !card.classList.contains("reader")) showRead(card, marks[id] === 1);
    });
  });

  // On a story page: count it as read after it has been on screen for a while.
  var reader = document.querySelector("[data-autoread]");
  if (reader && window.fetch) {
    var needed = parseInt(reader.getAttribute("data-autoread"), 10) || 12;
    var seen = 0;
    var timer = setInterval(function () {
      if (document.visibilityState !== "visible") return;
      seen += 1;
      if (seen >= needed) {
        clearInterval(timer);
        setRead(reader.getAttribute("data-story"), true);
      }
    }, 1000);
  }

  // Stories that open the publisher's website count as read when tapped (we can't time them).
  document.addEventListener("click", function (ev) {
    var link = ev.target.closest ? ev.target.closest("a[data-mark-read]") : null;
    if (!link || !window.fetch) return;
    var card = link.closest(".card");
    if (card && !card.classList.contains("is-read")) setRead(card.getAttribute("data-story"), true, true);
  });

  // --- forms: feedback and read toggles without a page reload ---------------
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.classList || !window.fetch) return;
    var isFeedback = form.classList.contains("js-feedback");
    var isRead = form.classList.contains("js-read");
    if (!isFeedback && !isRead) return;
    ev.preventDefault();
    var card = form.closest(".card");
    var more = card ? card.querySelector("details.more") : null;

    if (isRead) {
      var value = form.querySelector("input[name=value]").value;
      setRead(card.getAttribute("data-story"), value === "read");
      if (more) more.open = false;
      return;
    }

    var buttons = card ? card.querySelectorAll("button") : [];
    buttons.forEach(function (b) { b.disabled = true; });
    post(form.action, { value: form.querySelector("input[name=value]").value }).then(function (data) {
      if (!card) return;
      card.classList.toggle("is-hidden", data.interest === -1);
      var yes = card.querySelector(".fb-yes");
      if (yes) yes.setAttribute("aria-pressed", data.interest === 1 ? "true" : "false");
      var liked = card.querySelector(".liked");
      if (liked) liked.hidden = data.interest !== 1;
      if (more) more.open = false;
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
