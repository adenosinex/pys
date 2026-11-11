
/* Workbench channel (wbapi1) — injected by the panel platform layer.
   Bridges https://debi.su7.dpdns.org/site/wbtest/ (cross-origin) to the
   links platform APIs on this origin:
     wb-req save           -> POST /web/links/api/projects/html
     wb-req mark-encrypted -> POST /app/wb-mark (this file's companion
                              endpoint in links app; implemented in the
                              app via the scripts hook below)
   Replies postMessage wb-resp to the requesting origin only. */
(function () {
  var WB_ORIGIN = "https://debi.su7.dpdns.org";
  function reply(ev, payload) {
    payload.type = "wb-resp";
    ev.source.postMessage(payload, WB_ORIGIN);
  }
  window.addEventListener("message", function (ev) {
    if (ev.origin !== WB_ORIGIN) return;
    var d = ev.data || {};
    if (d.type === "wb-hello-ask") {
      ev.source.postMessage({ type: "wb-hello", api: "wbapi1", embed: window.__WB_EMBED__ || null }, WB_ORIGIN);
      return;
    }
    if (d.type !== "wb-req") return;
    var id = d.id;
    if (d.path === "save") {
      fetch("/web/links/api/projects/html", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(d.body || {})
      }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { s: r.status, j: j }; }); })
        .then(function (x) { reply(ev, { id: id, ok: x.s === 200 && x.j && x.j.ok, message: (x.j && x.j.message) || "" }); })
        .catch(function (e) { reply(ev, { id: id, ok: false, message: String(e) }); });
      return;
    }
    if (d.path === "mark-encrypted") {
      fetch("/web/links/api/wb-mark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(d.body || {})
      }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { s: r.status, j: j }; }); })
        .then(function (x) { reply(ev, { id: id, ok: x.s === 200 && x.j && x.j.ok, message: (x.j && x.j.message) || "" }); })
        .catch(function (e) { reply(ev, { id: id, ok: false, message: String(e) }); });
      return;
    }
    reply(ev, { id: id, ok: false, message: "unknown path" });
  });
  // Announce to any workbench frame already listening.
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: "wb-hello", api: "wbapi1", embed: window.__WB_EMBED__ || null }, WB_ORIGIN);
    }
  } catch (e) { }
})();
