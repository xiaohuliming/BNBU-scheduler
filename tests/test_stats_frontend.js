"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const root = path.resolve(__dirname, "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function snapshot(pages) {
  const totals = { views: 12, visitors: 8, newVisitors: 8, viewsPerVisitor: 1.5 };
  const media = { resolves: 0, downloads: 0, bytes: 0, resolveRate: null, downloadRate: null, platforms: [], errors: [] };
  const coverage = { firstEvent: "2026-09-01 00:00:00", latestEvent: "2026-09-25 01:00:00", records: 12, visitors: 8 };
  return {
    window: { start: "2026-09-25", end: "2026-09-25", today: "2026-09-25", previousStart: "2026-09-24", days: 1, excludeBots: true },
    generatedAt: "2026-09-25T01:00:00Z",
    traffic: { ...totals, previous: totals, pages, devices: [], sources: [], referrers: [], hourly: Array.from({ length: 24 }, (_, hour) => ({ hour, views: 0 })) },
    media: { ...media, previous: media },
    daily: [{ day: "2026-09-25", views: 12, visitors: 8, resolves: 0, downloads: 0, bytes: 0 }],
    coverage: { traffic: coverage, media: coverage },
  };
}

async function dashboard(pages) {
  const dom = new JSDOM(read("stats/index.html"), { url: "https://example.test/stats/index.html", runScripts: "outside-only" });
  dom.window.fetch = async () => ({ ok: true, headers: { get: () => "application/json" }, json: async () => snapshot(pages) });
  dom.window.eval(read("stats/dashboard.js"));
  await tick();
  assert.equal(dom.window.document.getElementById("dashboard").getAttribute("aria-busy"), "false");
  return dom;
}

test("missing pages remain discoverable without fabricated zero counts", async () => {
  const dom = await dashboard([{ name: "home", views: 8, visitors: 2 }, { name: "sms-market", views: 4, visitors: 4 }]);
  try {
    const d = dom.window.document;
    const rows = [...d.querySelectorAll("#pages .rank-row")];
    for (const title of ["更新日志", "隐私说明", "MIS 助手", "数据看板"]) {
      const row = rows.find((r) => r.querySelector(".rank-name").textContent.includes(title));
      assert.match(row.textContent, /本周期无记录/);
      assert.equal(row.querySelector(".fill"), null);
      assert.equal(row.querySelector(".rank-order").textContent, "·");
    }
    assert.match(rows[1].textContent, /短信接码/);
    assert.match(d.getElementById("page-summary").textContent, /2 个在本周期有访问记录/);
    d.getElementById("page-sort").value = "visitors";
    d.getElementById("page-sort").dispatchEvent(new dom.window.Event("change"));
    assert.match(d.querySelector("#pages .rank-row").textContent, /短信接码/);
    d.getElementById("page-search").value = "PRIVACY";
    d.getElementById("page-search").dispatchEvent(new dom.window.Event("input"));
    assert.equal(d.querySelectorAll("#pages .rank-row").length, 1);
    assert.equal(d.querySelector("#pages a").getAttribute("href"), "/privacy/");
    d.getElementById("page-search").value = "没有这个页面";
    d.getElementById("page-search").dispatchEvent(new dom.window.Event("input"));
    assert.match(d.getElementById("pages").textContent, /没有匹配/);
  } finally { dom.window.close(); }
});

test("new page events, historical names and unknown names display safely without duplicates", async () => {
  const dom = await dashboard([
    { name: "privacy", views: 8, visitors: 3 },
    { name: "changelog", views: 3, visitors: 2 },
    { name: "sms-lab", views: 1, visitors: 1 },
    { name: '<img src=x onerror="alert(1)">', views: 1, visitors: 1 },
  ]);
  try {
    const d = dom.window.document;
    assert.equal(d.querySelectorAll('#pages a[href="/privacy/"]').length, 1);
    assert.match(d.querySelector("#pages .rank-row").textContent, /8次浏览 · 3 访客/);
    assert.match(d.getElementById("pages").textContent, /短信接码旧版/);
    assert.equal(d.querySelector("#pages img"), null);
  } finally { dom.window.close(); }
});

test("an empty period keeps the page inventory visible", async () => {
  const dom = await dashboard([]);
  try {
    assert.match(dom.window.document.getElementById("pages").textContent, /隐私说明/);
    assert.equal(dom.window.document.querySelectorAll("#pages .fill").length, 0);
  } finally { dom.window.close(); }
});

test("shared tracking sends one first-party event without query strings or fragments", async () => {
  for (const name of ["privacy", "changelog", "stats", "mis-helper", "eatwhat"]) {
    const dom = new JSDOM(read(`${name}/index.html`), {
      url: `https://example.test/${name}/?secret=private#private-fragment`,
      referrer: "https://source.test/article?token=private#private",
      runScripts: "outside-only",
    });
    try {
      const w = dom.window, calls = [];
      const scripts = w.document.querySelectorAll('script[src^="/site-info/analytics.js"]');
      assert.equal(scripts.length, 1);
      Object.defineProperty(w.document, "currentScript", { value: scripts[0] });
      w.fetch = (url, options) => { calls.push({ url, options }); return Promise.resolve({ ok: true }); };
      w.eval(read("site-info/analytics.js"));
      w.eval(read("site-info/analytics.js"));
      await tick();
      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, "/api/analytics/track");
      assert.deepEqual(JSON.parse(calls[0].options.body), { view: name, path: `/${name}/`, referrer: "https://source.test/article" });
      assert.equal(calls[0].options.keepalive, true);
      assert.equal(calls[0].options.credentials, "same-origin");
      if (name === "eatwhat") assert.doesNotMatch(read("eatwhat/index.html"), /analytics\/track|trackEatwhatVisit/);
    } finally { dom.window.close(); }
  }
});

test("tracking skips local files and failures do not break the page", async () => {
  for (const url of ["file:///tmp/privacy/index.html", "https://example.test/privacy/"]) {
    const dom = new JSDOM('<script data-page="privacy"></script>', { url, runScripts: "outside-only" });
    try {
      const w = dom.window;
      Object.defineProperty(w.document, "currentScript", { value: w.document.querySelector("script") });
      let count = 0;
      w.fetch = () => { count++; return Promise.reject(new Error("offline")); };
      w.eval(read("site-info/analytics.js"));
      await tick();
      assert.equal(count, url.startsWith("file:") ? 0 : 1);
    } finally { dom.window.close(); }
  }
});
