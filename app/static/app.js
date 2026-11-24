// Best-effort, foreground-only notifications + wall-clock countdowns.
// Countdowns are recomputed from Date.now() each tick, so backgrounding the tab
// and returning shows the correct remaining time (no dying interval drift).
(function () {
  "use strict";

  function fmt(ms) {
    if (ms <= 0) return "0:00";
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    return m + ":" + String(s % 60).padStart(2, "0");
  }

  function notify(title, body, tag) {
    if (!("Notification" in window)) return;
    if (Notification.permission !== "granted") return;
    if (document.visibilityState !== "visible") return; // honest: foreground only
    if (sessionStorage.getItem(tag)) return;
    try { new Notification(title, { body: body, tag: tag }); sessionStorage.setItem(tag, "1"); } catch (e) {}
  }

  function tickSlots() {
    const cards = document.querySelectorAll(".slot-card[data-started]");
    cards.forEach(function (card) {
      const started = new Date(card.getAttribute("data-started")).getTime();
      const dur = parseInt(card.getAttribute("data-duration") || "0", 10) * 60000;
      const end = started + dur;
      const now = Date.now();
      const el = card.querySelector(".countdown");
      if (!el) return;
      const ts = card.getAttribute("data-ts");
      if (now < end) {
        const left = end - now;
        el.textContent = "Ends in " + fmt(left);
        el.classList.toggle("amber", left <= 5 * 60000);
        el.classList.toggle("red", left <= 60000);
        notify("Session starting", "Head to your room.", "start-" + ts);
        if (left <= 5 * 60000) notify("5 minutes left", "This session is wrapping up.", "end-" + ts);
      } else {
        el.textContent = "Ended";
      }
    });
  }

  // highest proposal id currently shown — the rate list polls for anything newer
  window.maxRatePid = function () {
    var max = 0;
    document.querySelectorAll("#rate-list [data-pid]").forEach(function (el) {
      var v = parseInt(el.getAttribute("data-pid"), 10);
      if (v > max) max = v;
    });
    return max;
  };

  function urlB64ToUint8Array(b64) {
    var pad = "=".repeat((4 - (b64.length % 4)) % 4);
    var s = (b64 + pad).replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(s), arr = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    return arr;
  }

  async function reflectAlertState(btn) {
    // show "Alerts on" on reload if already enabled (so it's a remembered setting)
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    var vapid = document.querySelector('meta[name=vapid-key]');
    if (vapid && vapid.content && "serviceWorker" in navigator) {
      try {
        var reg = await navigator.serviceWorker.getRegistration();
        var sub = reg && (await reg.pushManager.getSubscription());
        if (sub) { btn.textContent = "Alerts on"; btn.disabled = true; }
      } catch (e) {}
      return;
    }
    btn.textContent = "Alerts on"; btn.disabled = true;  // foreground-only fallback
  }

  async function enableAlerts(btn) {
    try {
      var perm = await Notification.requestPermission();
      if (perm !== "granted") { btn.textContent = "Alerts blocked"; return; }
      var vapid = document.querySelector('meta[name=vapid-key]');
      if (vapid && vapid.content && "serviceWorker" in navigator && "PushManager" in window) {
        var reg = await navigator.serviceWorker.register("/sw.js");
        var sub = await reg.pushManager.getSubscription();
        if (!sub) {
          sub = await reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlB64ToUint8Array(vapid.content),
          });
        }
        var csrf = (document.querySelector('meta[name=csrf-token]') || {}).content || "";
        var resp = await fetch("/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify(sub),
        });
        // fetch only rejects on network error, not on a 4xx/5xx — check explicitly
        // so we never claim success the server didn't grant.
        if (!resp.ok) { btn.textContent = "Alerts could not be enabled"; return; }
      }
      btn.textContent = "Alerts on"; btn.disabled = true;
    } catch (e) { btn.textContent = "Alerts could not be enabled"; }
  }

  // Confirmation flow for regenerating a PUBLISHED schedule. Capture phase so it
  // runs BEFORE htmx. data-confirm1 is the abort gate; data-pin-ask (only when
  // sessions have already started) asks whether to keep them in place and sets the
  // pin_started field accordingly — a deliberate double-ask before disruption.
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-regen], [data-double-confirm]");
    if (!b) return;
    var msg1 = b.getAttribute("data-confirm1") || b.getAttribute("data-double-confirm");
    if (msg1 && !window.confirm(msg1)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      return;
    }
    var pinAsk = b.getAttribute("data-pin-ask");
    if (pinAsk) {
      var keep = window.confirm(pinAsk);
      var form = b.closest("form");
      var inp = form && form.querySelector('input[name=pin_started]');
      if (inp) inp.value = keep ? "1" : "0";
    }
  }, true);

  function init() {
    const btn = document.getElementById("notify-btn");
    if (btn) {
      var vapid = document.querySelector('meta[name=vapid-key]');
      var iOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
      var standalone = window.navigator.standalone === true ||
        (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
      if (vapid && vapid.content && iOS && !standalone) {
        // web push on iOS only works from a Home-Screen PWA — show how to install
        btn.hidden = true;
        var note = document.getElementById("notify-note"); if (note) note.hidden = true;
        var ins = document.getElementById("ios-install"); if (ins) ins.hidden = false;
      } else if ("Notification" in window) {
        btn.addEventListener("click", function () { enableAlerts(btn); });
        reflectAlertState(btn);   // remember if already enabled
      } else {
        btn.hidden = true;
      }
    }
    if (document.querySelector(".slot-card[data-started]")) {
      tickSlots();
      setInterval(tickSlots, 1000);
    }

    // copy-to-clipboard. Delegated on body so it keeps working after htmx swaps
    // re-render the console; reports real success/failure (never a false "copied ✓").
    function copyViaTextarea(text) {
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", "");
      ta.style.position = "fixed"; ta.style.top = "0"; ta.style.left = "0"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.focus(); ta.select();
      var ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
      return ok;
    }
    function copyText(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text).catch(function () {
          if (!copyViaTextarea(text)) throw new Error("copy failed");
        });
      }
      return copyViaTextarea(text) ? Promise.resolve() : Promise.reject(new Error("copy failed"));
    }
    document.body.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-copy]");
      if (!btn) return;
      var el = document.querySelector(btn.getAttribute("data-copy"));
      if (!el) return;
      var text = (el.value != null && el.value !== "") ? el.value : (el.textContent || "");
      if (!btn.getAttribute("data-label")) btn.setAttribute("data-label", btn.textContent);
      var orig = btn.getAttribute("data-label");
      copyText(text).then(
        function () { btn.textContent = "Copied"; },
        function () { btn.textContent = "Copy failed. Select the text and copy it."; }
      );
      setTimeout(function () { btn.textContent = orig; }, 1600);
    });

    // Cancel buttons inside a disclosure close it (delegated: works after htmx swaps)
    document.body.addEventListener("click", function (e) {
      var c = e.target.closest("[data-close]");
      var d = c && c.closest("details");
      if (d) d.open = false;
    });

    // toast on htmx failures (CSRF expiry, rate-limit, network) so nothing is silent
    function toast(msg) {
      var t = document.getElementById("toast");
      if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
      t.textContent = msg; t.hidden = false;
      clearTimeout(t._h); t._h = setTimeout(function () { t.hidden = true; }, 3500);
    }
    document.body.addEventListener("htmx:responseError", function (e) {
      var x = e.detail && e.detail.xhr, msg = "Something went wrong. Try again.";
      if (x && x.status === 429) msg = "Too many requests. Wait a moment, then try again.";
      else if (x && x.status === 403) msg = "That action is not allowed.";
      else if (x && x.status >= 400 && x.status < 500) {
        try { var d = JSON.parse(x.responseText); if (d && d.detail) msg = d.detail; } catch (_) {}
      }
      toast(msg);
    });
    document.body.addEventListener("htmx:sendError", function () {
      toast("Network error. Check your connection.");
    });

    // render QR codes (offline, via vendored qrcode-generator); re-run after htmx
    // swaps so the console's share panel keeps its QR when it re-renders.
    function renderQR(root) {
      if (typeof qrcode !== "function") return;
      (root || document).querySelectorAll("[data-qr]").forEach(function (box) {
        if (box.dataset.qrDone) return;
        try {
          var qr = qrcode(0, "M");
          qr.addData(box.getAttribute("data-qr"));
          qr.make();
          var cell = parseInt(box.getAttribute("data-qr-cell") || "4", 10);
          box.innerHTML = qr.createSvgTag({ cellSize: cell, margin: 1 });
          box.dataset.qrDone = "1";
        } catch (e) {}
      });
    }
    renderQR();
    document.body.addEventListener("htmx:afterSwap", function (e) { renderQR(e.target); });

    // Keep UI state across htmx swaps. The console re-renders whole on each action
    // and the grid re-renders on its 5s poll, which would close open panels and
    // reset scroll positions. Elements opt in with data-keep="<key>".
    var kept = {};
    document.body.addEventListener("htmx:beforeSwap", function () {
      kept = {};
      document.querySelectorAll("[data-keep]").forEach(function (el) {
        kept[el.getAttribute("data-keep")] = { open: el.open, left: el.scrollLeft, top: el.scrollTop };
      });
    });
    document.body.addEventListener("htmx:afterSwap", function () {
      document.querySelectorAll("[data-keep]").forEach(function (el) {
        var s = kept[el.getAttribute("data-keep")];
        if (!s) return;
        if (s.open) el.open = true;
        el.scrollLeft = s.left; el.scrollTop = s.top;
      });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
