"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const num = (value) =>
    value == null
      ? "暂无"
      : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  const percent = (value) =>
    value == null ? "暂无" : Number(value).toFixed(1) + "%";
  const bytes = (value) => {
    if (value == null) return "暂无";
    let n = Number(value),
      unit = 0;
    while (n >= 1024 && unit < 4) {
      n /= 1024;
      unit++;
    }
    return (
      (unit ? n.toFixed(n < 10 ? 2 : 1) : n) +
      " " +
      ["B", "KB", "MB", "GB", "TB"][unit]
    );
  };
  const duration = (value) =>
    value == null
      ? "暂无"
      : value < 1000
        ? Math.round(value) + " ms"
        : (value / 1000).toFixed(1) + " s";
  const shortDay = (day) => (day ? day.slice(5).replace("-", "/") : "暂无记录");
  const clock = (stamp) =>
    stamp
      ? new Date(
          stamp.includes("T") ? stamp : stamp.replace(" ", "T") + "Z",
        ).toLocaleString("zh-CN", {
          timeZone: "Asia/Shanghai",
          hour12: false,
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "暂无记录";
  const views = {
    home: "首页",
    explorer: "课程浏览",
    classrooms: "空教室",
    eatwhat: "今天吃什么",
    "print-setup": "校园打印",
    optimizer: "排课优化",
    ddl: "DDL 备忘",
    toolbox: "工具箱",
    teachers: "教师评价",
    "media-dl": "无水印下载",
    settings: "设置",
    programme: "专业地图",
    career: "职业规划",
    files: "文件中心",
    "campus-map": "校园地图",
    pet: "校园宠物",
    stats: "数据看板",
  };
  const platforms = {
    youtube: "YouTube",
    bilibili: "哔哩哔哩",
    douyin: "抖音",
    xiaohongshu: "小红书",
    twitter: "X / Twitter",
    tiktok: "TikTok",
    instagram: "Instagram",
    weibo: "微博",
    kuaishou: "快手",
    unknown: "未识别平台",
  };
  const deviceNames = {
    mobile: "手机",
    desktop: "电脑",
    tablet: "平板",
    bot: "已识别机器人",
    unknown: "未识别设备",
  };
  const sourceNames = {
    direct: "直接访问",
    internal: "站内跳转",
    external: "外部来源",
    legacy: "历史来源未区分",
    unknown: "无法识别",
  };
  let data = null,
    requestId = 0,
    controller = null,
    timer = null,
    lastSuccess = 0;
  let trafficMetric = "views",
    mediaMetric = "resolves",
    activeTab = "traffic",
    lastQuery = null;
  const chartSelection = { traffic: null, media: null };
  const empty = (message) => `<div class="empty">${esc(message)}</div>`;

  function comparison(current, previous, type, complete, reverse = false) {
    if (!complete) return ["前一周期记录不足", "neutral"];
    if (current == null || previous == null) return ["暂无可比数据", "neutral"];
    if (!previous && !current) return ["与前一周期持平", "neutral"];
    if (!previous) return ["前一周期为 0", "neutral"];
    const diff =
      type === "rate"
        ? current - previous
        : ((current - previous) / previous) * 100;
    const text =
      Math.abs(diff) < 0.05
        ? "与前一周期持平"
        : `${diff > 0 ? "↑" : "↓"} ${Math.abs(diff).toFixed(1)}${type === "rate" ? " 个百分点" : "%"} · 较前一周期`;
    return [
      text,
      Math.abs(diff) < 0.05
        ? "neutral"
        : diff > 0 !== reverse
          ? "positive"
          : "negative",
    ];
  }
  function metric(label, value, current, previous, type, complete, reverse) {
    const [text, tone] = comparison(current, previous, type, complete, reverse);
    return `<article class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-compare ${tone}">${esc(text)}</div></article>`;
  }
  function completePrevious(source) {
    const first = data.coverage[source].firstEvent;
    return (
      !!first &&
      new Date(first.replace(" ", "T") + "Z") <=
        new Date(data.window.previousStart + "T00:00:00+08:00")
    );
  }
  function splitRows(rows, labels, field = "views", formatter = num) {
    const total = rows.reduce((sum, r) => sum + (r[field] || 0), 0);
    if (!total) return empty("所选周期暂无记录");
    return rows
      .map(
        (r) =>
          `<div class="split-row"><div class="split-top"><span>${esc(labels[r.name] || r.name)}</span><b>${esc(formatter(r[field]))} <span class="hint">${((r[field] / total) * 100).toFixed(1)}%</span></b></div><div class="track"><div class="fill" style="width:${(r[field] / total) * 100}%"></div></div></div>`,
      )
      .join("");
  }
  function table(headers, rows) {
    return `<table><thead><tr>${headers.map((h) => `<th scope="col">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }
  function renderTraffic() {
    const t = data.traffic,
      p = t.previous,
      complete = completePrevious("traffic");
    $("traffic-metrics").innerHTML = [
      metric("浏览次数", num(t.views), t.views, p.views, "count", complete),
      metric(
        "周期独立访客",
        num(t.visitors),
        t.visitors,
        p.visitors,
        "count",
        complete,
      ),
      metric(
        "新访客",
        num(t.newVisitors),
        t.newVisitors,
        p.newVisitors,
        "count",
        complete,
      ),
      metric(
        "人均浏览",
        num(t.viewsPerVisitor),
        t.viewsPerVisitor,
        p.viewsPerVisitor,
        "count",
        complete,
      ),
    ].join("");
    const max = Math.max(...t.pages.map((r) => r.views), 1);
    $("pages").innerHTML = t.pages.length
      ? t.pages
          .map(
            (r, i) =>
              `<div class="rank-row"><span class="rank-order">${String(i + 1).padStart(2, "0")}</span><div><div class="rank-name">${esc(views[r.name] || r.name)}</div><div class="track"><div class="fill" style="width:${(r.views / max) * 100}%"></div></div></div><div class="rank-amount">${num(r.views)}<small class="hint">${num(r.visitors)} 访客</small></div></div>`,
          )
          .join("")
      : empty("所选周期还没有访问记录");
    $("devices").innerHTML = splitRows(t.devices, deviceNames);
    $("sources").innerHTML = splitRows(t.sources, sourceNames);
    $("referrers").innerHTML = t.referrers.length
      ? t.referrers
          .map(
            (r) =>
              `<div class="split-top split-row"><span>${esc(r.host)}</span><b>${num(r.views)}</b></div>`,
          )
          .join("") +
        (t.otherReferrers
          ? `<p class="hint">其余来源共 ${num(t.otherReferrers)} 次</p>`
          : "")
      : empty("本周期没有外部来源记录");
    const peak = t.hourly.reduce((a, b) => (a.views >= b.views ? a : b)),
      high = Math.max(1, peak.views);
    $("hours").innerHTML =
      `<div class="hours" role="img" aria-label="24 小时访问分布，最高为 ${peak.hour} 时 ${peak.views} 次">${t.hourly.map((r) => `<div class="hour ${r.views && r.hour === peak.hour ? "peak" : ""}" style="height:${(r.views / high) * 100}%" title="${r.hour}:00 · ${r.views} 次"></div>`).join("")}</div><div class="hour-axis"><span>0 时</span><span>6 时</span><span>12 时</span><span>18 时</span><span>23 时</span></div><details><summary>查看时段明细</summary>${table(
        ["时段", "浏览次数"],
        t.hourly.map((r) => [`${r.hour}:00`, num(r.views)]),
      )}</details>`;
    $("peak").textContent = peak.views
      ? `访问高峰：${peak.hour}:00 至 ${peak.hour + 1}:00，共 ${num(peak.views)} 次。`
      : "暂无访问高峰";
    $("traffic-daily").innerHTML = table(
      ["日期", "浏览次数", "独立访客"],
      data.daily.map((r) => [
        r.day,
        r.views == null ? "未记录" : num(r.views),
        r.visitors == null ? "未记录" : num(r.visitors),
      ]),
    );
  }
  function renderPlatforms() {
    const field = $("platform-sort").value;
    const rows = [...data.media.platforms].sort(
      (a, b) => b[field] - a[field] || a.name.localeCompare(b.name),
    );
    $("platforms").innerHTML = rows.length
      ? table(
          [
            "平台",
            "解析次数",
            "解析成功率",
            "下载次数",
            "传输成功率",
            "已传输流量",
          ],
          rows.map((r) => [
            platforms[r.name] || r.name,
            num(r.resolves),
            percent(r.resolves ? (r.resolveOk / r.resolves) * 100 : null),
            num(r.downloads),
            percent(r.downloads ? (r.downloadOk / r.downloads) * 100 : null),
            bytes(r.bytes),
          ]),
        )
      : empty("所选周期没有平台请求");
  }
  function renderMedia() {
    const m = data.media,
      p = m.previous,
      complete = completePrevious("media");
    $("media-metrics").innerHTML = [
      metric(
        "解析请求",
        num(m.resolves),
        m.resolves,
        p.resolves,
        "count",
        complete,
      ),
      metric(
        "解析成功率",
        percent(m.resolveRate),
        m.resolveRate,
        p.resolveRate,
        "rate",
        complete,
      ),
      metric(
        "下载请求",
        num(m.downloads),
        m.downloads,
        p.downloads,
        "count",
        complete,
      ),
      metric(
        "传输成功率",
        percent(m.downloadRate),
        m.downloadRate,
        p.downloadRate,
        "rate",
        complete,
      ),
      metric("已传输流量", bytes(m.bytes), m.bytes, p.bytes, "count", complete),
      `<article class="metric"><div class="metric-label">P95 解析耗时</div><div class="metric-value">${duration(m.latencyP95)}</div><div class="metric-compare neutral">基于 ${num(m.latencySamples)} 条耗时记录</div></article>`,
    ].join("");
    $("modes").innerHTML = splitRows(
      [
        { name: "singles", views: m.singles },
        { name: "merges", views: m.merges },
        { name: "batches", views: m.batches },
      ],
      {
        singles: "单项及早期下载记录",
        merges: "音视频合并",
        batches: "ZIP 打包",
      },
    );
    $("latency-p50").textContent = duration(m.latencyP50);
    $("latency-samples").textContent =
      "中位数和 P95 均基于所选周期内的已完成解析请求。";
    $("failure-count").textContent =
      `解析失败 ${num(m.resolves - m.resolveOk)} 次 · 传输未完成 ${num(m.downloads - m.downloadOk)} 次`;
    $("errors").innerHTML = m.errors.length
      ? m.errors
          .map(
            (r) =>
              `<div class="error-row ${esc(r.name)}"><div>${esc(r.label)}<small>${esc(r.platforms.map((p) => platforms[p] || p).join(" / "))} · 最近 ${clock(r.lastSeen)}</small></div><b>${num(r.count)}</b></div>`,
          )
          .join("")
      : empty(
          m.resolves || m.downloads
            ? "本周期没有失败记录"
            : "暂无可分析的请求记录",
        );
    $("media-daily").innerHTML = table(
      ["日期", "解析", "解析成功", "下载", "传输成功", "流量"],
      data.daily.map((r) => [
        r.day,
        num(r.resolves),
        num(r.resolveOk),
        num(r.downloads),
        num(r.downloadOk),
        bytes(r.bytes),
      ]),
    );
    renderPlatforms();
  }
  function renderChart(kind) {
    if (!data || activeTab !== kind) return;
    const metric = kind === "traffic" ? trafficMetric : mediaMetric,
      target = $(kind + "-chart"),
      daily = data.daily;
    const width = Math.max(target.clientWidth, 280),
      height = 220,
      left = 48,
      right = 16,
      top = 18,
      bottom = 35;
    const plotWidth = width - left - right,
      plotHeight = height - top - bottom,
      values = daily.map((r) => r[metric]);
    const known = values.filter((v) => v != null),
      rawMax = Math.max(...known, 1),
      step = 10 ** Math.floor(Math.log10(rawMax)),
      max = Math.ceil(rawMax / step) * step;
    const x = (i) =>
        left +
        (daily.length === 1
          ? plotWidth / 2
          : (i / (daily.length - 1)) * plotWidth),
      y = (v) => top + plotHeight - (v / max) * plotHeight;
    const color = kind === "traffic" ? "#c9500a" : "#277c91",
      fmt = metric === "bytes" ? bytes : num;
    const paths = [];
    let current = [];
    values.forEach((v, i) => {
      if (v == null) {
        if (current.length) paths.push(current);
        current = [];
      } else current.push([x(i), y(v)]);
    });
    if (current.length) paths.push(current);
    const line = paths
      .map(
        (points) =>
          `<path d="${points.map((p, i) => (i ? "L" : "M") + p.join(",")).join(" ")}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linejoin="round"/>${points.length === 1 ? `<circle cx="${points[0][0]}" cy="${points[0][1]}" r="3" fill="${color}"/>` : ""}`,
      )
      .join("");
    const ticks = max <= 2 ? [0, max] : [0, Math.round(max / 2), max];
    const grid = ticks
      .map(
        (v) =>
          `<line x1="${left}" y1="${y(v)}" x2="${width - right}" y2="${y(v)}" stroke="#dde3d8" stroke-dasharray="3 5"/><text x="${left - 8}" y="${y(v) + 4}" text-anchor="end" fill="#66706f" font-size="11">${esc(fmt(v))}</text>`,
      )
      .join("");
    const indices = [
      ...new Set([0, Math.floor((daily.length - 1) / 2), daily.length - 1]),
    ];
    const labels = indices
      .map(
        (i) =>
          `<text x="${x(i)}" y="${height - 9}" text-anchor="${i === 0 && daily.length > 1 ? "start" : i === daily.length - 1 && daily.length > 1 ? "end" : "middle"}" fill="#66706f" font-size="11">${shortDay(daily[i].day)}</text>`,
      )
      .join("");
    target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" tabindex="0" role="img" aria-label="${kind === "traffic" ? "每日访问" : "每日下载"}趋势，使用左右方向键查看日期">${grid}${line}${labels}<line class="guide" y1="${top}" y2="${height - bottom}" stroke="#82937f" stroke-dasharray="3 3"/><circle class="marker" r="4" fill="${color}" stroke="white" stroke-width="2"/></svg>`;
    const svg = target.querySelector("svg");
    function select(index) {
      index = Math.max(0, Math.min(daily.length - 1, index));
      chartSelection[kind] = index;
      const r = daily[index],
        value = r[metric],
        guide = svg.querySelector(".guide"),
        marker = svg.querySelector(".marker");
      guide.setAttribute("x1", x(index));
      guide.setAttribute("x2", x(index));
      marker.setAttribute("cx", x(index));
      marker.setAttribute("cy", y(value || 0));
      marker.style.display = value == null ? "none" : "";
      $(kind + "-detail").innerHTML =
        kind === "traffic"
          ? `<span>${r.day}</span><span>浏览 <strong>${r.views == null ? "未记录" : num(r.views)}</strong></span><span>访客 <strong>${r.visitors == null ? "未记录" : num(r.visitors)}</strong></span>`
          : `<span>${r.day}</span><span>解析 <strong>${num(r.resolves)}</strong></span><span>下载 <strong>${num(r.downloads)}</strong></span><span>流量 <strong>${bytes(r.bytes)}</strong></span>`;
    }
    const point = (event) => {
      const rect = svg.getBoundingClientRect();
      select(
        Math.round(
          ((((event.clientX - rect.left) / rect.width) * width - left) /
            plotWidth) *
            (daily.length - 1),
        ),
      );
    };
    svg.addEventListener("pointermove", point);
    svg.addEventListener("pointerdown", point);
    svg.addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        select(
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? daily.length - 1
              : chartSelection[kind] + (event.key === "ArrowLeft" ? -1 : 1),
        );
      }
    });
    select(chartSelection[kind] ?? daily.length - 1);
  }
  function render() {
    $("range-label").textContent =
      `${data.window.start} 至 ${data.window.end}${data.window.partialToday ? " · 今日尚未结束" : ""}`;
    $("updated").textContent = "更新于 " + clock(data.generatedAt);
    $("start").max = $("end").max = data.window.today;
    $("quality-note").textContent =
      `${data.window.excludeBots ? "已排除" : "本周期识别到"} ${num(data.traffic.excludedBots)} 次机器人访问；已过滤 ${num(data.media.excludedTests)} 条可明确识别的历史测试下载。原始记录保留。`;
    const since = (value) =>
      value
        ? new Date(value.replace(" ", "T") + "Z").toLocaleDateString("zh-CN", {
            timeZone: "Asia/Shanghai",
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
          })
        : "暂无记录";
    $("history-note").textContent =
      `历史原始记录共 ${num(data.coverage.traffic.records)} 次浏览、${num(data.coverage.traffic.visitors)} 个访客标识。此累计值含机器人，不随日期筛选变化。`;
    $("runtime-note").textContent = data.runtime
      ? `当前服务进程累计拦截：识别的自动客户端 ${num(data.runtime.uaBlocked)} 次、触发限速 ${num(data.runtime.rateLimited)} 次。重启后重新计数。`
      : "";
    $("coverage-note").textContent =
      `访问记录始于 ${since(data.coverage.traffic.firstEvent)}，最近事件 ${clock(data.coverage.traffic.latestEvent)}；下载记录始于 ${since(data.coverage.media.firstEvent)}，最近事件 ${clock(data.coverage.media.latestEvent)}。`;
    renderTraffic();
    renderMedia();
    renderChart(activeTab);
  }
  function setTab(tab, focus = false) {
    activeTab = tab === "media" ? "media" : "traffic";
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === activeTab;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      $(button.getAttribute("aria-controls")).hidden = !active;
      if (active && focus) button.focus();
    });
    if (data) {
      renderChart(activeTab);
      saveUrl();
    }
  }
  function queryFromControls() {
    const q = new URLSearchParams({
      exclude_bots: $("exclude-bots").checked ? "1" : "0",
    });
    if ($("period").value === "custom") {
      q.set("start", $("start").value);
      q.set("end", $("end").value);
    } else q.set("days", $("period").value);
    return q;
  }
  function restoreControls(query) {
    const q = new URLSearchParams(query);
    $("period").value = q.has("start") ? "custom" : q.get("days") || "30";
    $("start").value = q.get("start") || data?.window.start || "";
    $("end").value = q.get("end") || data?.window.end || "";
    $("exclude-bots").checked = q.get("exclude_bots") !== "0";
    $("custom-range").hidden = $("period").value !== "custom";
  }
  function saveUrl() {
    if (!lastQuery) return;
    const q = new URLSearchParams(lastQuery);
    if (activeTab === "media") q.set("tab", "media");
    history.replaceState(null, "", location.pathname + "?" + q);
  }
  async function load() {
    const query = queryFromControls(),
      id = ++requestId;
    controller?.abort();
    const activeController = new AbortController();
    controller = activeController;
    const timeout = setTimeout(() => activeController.abort(), 20000);
    $("dashboard").setAttribute("aria-busy", "true");
    $("refresh").disabled = true;
    $("export").disabled = true;
    $("notice").className = "notice";
    $("notice").textContent = data
      ? "正在更新，当前仍显示上次成功获取的数据…"
      : "正在读取统计，请稍候…";
    try {
      const response = await fetch("/api/analytics/dashboard?" + query, {
        signal: activeController.signal,
        cache: "no-store",
      });
      if (
        !(response.headers.get("content-type") || "").includes(
          "application/json",
        )
      )
        throw new Error("统计服务暂时不可用，请稍后重试。");
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "统计读取失败。");
      if (
        !result.window ||
        !result.traffic?.previous ||
        !result.media?.previous ||
        !result.coverage?.traffic ||
        !result.coverage?.media ||
        !Array.isArray(result.daily) ||
        result.daily.length !== result.window.days ||
        !result.daily.length ||
        !["pages", "devices", "hourly", "sources", "referrers"].every((key) =>
          Array.isArray(result.traffic[key]),
        ) ||
        !["platforms", "errors"].every((key) =>
          Array.isArray(result.media[key]),
        )
      )
        throw new Error("统计数据格式不完整，请稍后重试。");
      if (id !== requestId) return;
      data = result;
      lastQuery = query.toString();
      lastSuccess = Date.now();
      chartSelection.traffic = chartSelection.media = null;
      restoreControls(lastQuery);
      render();
      saveUrl();
      $("notice").textContent =
        `共 ${data.window.days} 个自然日，所有指标使用同一时间范围。`;
    } catch (error) {
      if (id !== requestId) return;
      if (lastQuery) restoreControls(lastQuery);
      $("notice").className = "notice error";
      $("notice").textContent =
        (error.name === "AbortError" ? "请求超时，请重试。" : error.message) +
        (data ? " 已保留上次成功更新的数据。" : " 点击“刷新数据”重试。");
    } finally {
      clearTimeout(timeout);
      if (id === requestId) {
        $("dashboard").setAttribute("aria-busy", "false");
        $("refresh").disabled = false;
        $("export").disabled = !data;
        controller = null;
      }
    }
  }
  function exportCsv() {
    if (!data) return;
    const cell = (value) => {
      let s = value == null ? "" : String(value);
      if (/^\s*[=+\-@\t\r]/.test(s)) s = "'" + s;
      return '"' + s.replace(/"/g, '""') + '"';
    };
    const rows = [
      [
        "北京时间日期",
        "浏览次数",
        "日独立访客",
        "解析次数",
        "解析成功",
        "下载请求",
        "传输成功",
        "已传输字节",
        "访问机器人过滤",
        "统计截至北京时间",
      ],
      ...data.daily.map((r) => [
        r.day,
        r.views,
        r.visitors,
        r.resolves,
        r.resolveOk,
        r.downloads,
        r.downloadOk,
        r.bytes,
        data.window.excludeBots ? "排除已识别机器人" : "全部访问",
        data.window.until,
      ]),
    ];
    const url = URL.createObjectURL(
      new Blob(
        ["\uFEFF" + rows.map((r) => r.map(cell).join(",")).join("\r\n")],
        { type: "text/csv;charset=utf-8" },
      ),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `MAXCOURSE-每日统计-${data.window.start}-${data.window.end}-${data.window.excludeBots ? "排除机器人" : "全部访问"}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const params = new URLSearchParams(location.search);
  restoreControls(params);
  setTab(params.get("tab"));
  $("period").addEventListener("change", () => {
    $("custom-range").hidden = $("period").value !== "custom";
    if ($("period").value !== "custom") load();
  });
  $("custom-range").addEventListener("submit", (event) => {
    event.preventDefault();
    load();
  });
  $("exclude-bots").addEventListener("change", load);
  $("refresh").addEventListener("click", load);
  $("export").addEventListener("click", exportCsv);
  $("platform-sort").addEventListener("change", () => {
    if (data) renderPlatforms();
  });
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        setTab(activeTab === "traffic" ? "media" : "traffic", true);
      }
    });
  });
  document
    .querySelectorAll("[data-traffic-metric],[data-media-metric]")
    .forEach((button) =>
      button.addEventListener("click", () => {
        const kind = button.dataset.trafficMetric ? "traffic" : "media";
        if (kind === "traffic") trafficMetric = button.dataset.trafficMetric;
        else mediaMetric = button.dataset.mediaMetric;
        button.parentElement.querySelectorAll("button").forEach((b) => {
          b.classList.toggle("selected", b === button);
          b.setAttribute("aria-pressed", String(b === button));
        });
        renderChart(kind);
      }),
    );
  $("auto-refresh").addEventListener("change", () => {
    clearInterval(timer);
    timer = $("auto-refresh").checked
      ? setInterval(() => {
          if (!document.hidden && !controller) load();
        }, 60000)
      : null;
  });
  document.addEventListener("visibilitychange", () => {
    if (
      !document.hidden &&
      $("auto-refresh").checked &&
      !controller &&
      Date.now() - lastSuccess > 60000
    )
      load();
  });
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => renderChart(activeTab), 150);
  });
  window.addEventListener("beforeunload", () => {
    controller?.abort();
    clearInterval(timer);
  });
  $("traffic-metrics").innerHTML = Array(4)
    .fill(
      '<article class="metric" aria-hidden="true"><div class="metric-label">正在读取</div><div class="metric-value">…</div><div class="metric-compare">请稍候</div></article>',
    )
    .join("");
  $("media-metrics").innerHTML = Array(6)
    .fill(
      '<article class="metric" aria-hidden="true"><div class="metric-label">正在读取</div><div class="metric-value">…</div><div class="metric-compare">请稍候</div></article>',
    )
    .join("");
  load();
})();
