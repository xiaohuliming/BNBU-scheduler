"use strict";
(() => {
  const view = document.currentScript?.dataset.page;
  if (!view || !/^[a-z0-9-]+$/.test(view) || !/^https?:$/.test(location.protocol)) return;
  // Count a document once, even if its shared script is included twice.
  const tracked = window.__maxcourseTrackedPages || (window.__maxcourseTrackedPages = new Set());
  if (tracked.has(view)) return;
  tracked.add(view);
  let referrer = "";
  if (document.referrer) {
    try {
      const source = new URL(document.referrer);
      if (/^https?:$/.test(source.protocol)) referrer = source.origin + source.pathname;
    } catch (_) { /* An unavailable referrer is treated as direct. */ }
  }
  // Do not send search terms, fragments, credentials, or page contents.
  fetch("/api/analytics/track", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ view, path: location.pathname, referrer }),
    keepalive: true,
  }).catch(() => {});
})();
