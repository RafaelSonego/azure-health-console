"use strict";

// ------------------------------------------------------------------ metrics

const RANGES = ["1h", "6h", "24h", "7d", "30d", "90d"];  // Azure keeps platform metrics 93 days

function watchedMetrics(node) {
  const names = new Set();
  for (const sig of node.signals) {
    const match = /^(.+) \((Average|Maximum|Minimum|Total|Count)\)$/.exec(sig.name);
    if (match) names.add(match[1]);
  }
  return names;
}

async function tabMetrics(node, body) {
  const defs = await exploreGet("metric-definitions", node);
  if (!defs.length) {
    body.innerHTML = '<p class="muted">This resource type has no platform metrics.</p>';
    return;
  }
  const watched = watchedMetrics(node);
  const option = (m) => `<option value="${esc(m.name)}">${esc(m.label)}${m.unit ? ` (${esc(m.unit)})` : ""}</option>`;
  const pinned = defs.filter((m) => watched.has(m.name));
  const others = defs.filter((m) => !watched.has(m.name));
  body.innerHTML = `
    <div class="metric-controls">
      <select id="m-name">${pinned.length ? `<optgroup label="Watched">${pinned.map(option).join("")}</optgroup>` : ""}
        <optgroup label="All metrics">${others.map(option).join("")}</optgroup></select>
      <select id="m-agg"></select>
      <select id="m-split"></select>
      <span class="range">${RANGES.map((r) => `<button type="button" data-range="${r}" class="${r === "6h" ? "active" : ""}">${r}</button>`).join("")}</span>
    </div>
    <div class="chart-wrap"><canvas id="m-chart" aria-label="Metric chart" role="img"></canvas></div>
    <div id="m-table"></div>`;
  const pick = { range: "6h" };
  const sync = () => {
    const def = defs.find((m) => m.name === $("m-name").value);
    const aggs = def.aggregations.length ? def.aggregations : [def.primary];
    $("m-agg").innerHTML = aggs.map((a) => `<option ${a === def.primary ? "selected" : ""}>${esc(a)}</option>`).join("");
    $("m-split").innerHTML = `<option value="">No split</option>${def.dimensions.map((d) => `<option value="${esc(d)}">Split by ${esc(d)}</option>`).join("")}`;
  };
  const draw = async () => {
    const def = defs.find((m) => m.name === $("m-name").value);
    $("m-table").innerHTML = loading("Loading metric…");
    const data = await exploreGet("metric-series", node, {
      metric: def.name, aggregation: $("m-agg").value, range: pick.range, split: $("m-split").value });
    drawChart(node, def, data);
  };
  $("m-name").addEventListener("change", () => { sync(); draw(); });
  $("m-agg").addEventListener("change", draw);
  $("m-split").addEventListener("change", draw);
  body.querySelector(".range").addEventListener("click", (event) => {
    if (!event.target.dataset.range) return;
    pick.range = event.target.dataset.range;
    body.querySelectorAll(".range button").forEach((b) => b.classList.toggle("active", b.dataset.range === pick.range));
    draw();
  });
  sync();
  await draw();
}

function thresholdsFor(node, metric) {
  const out = [];
  for (const sig of node.signals) {
    if (!sig.name.startsWith(`${metric} (`) || !sig.threshold) continue;
    for (const m of sig.threshold.matchAll(/(warn|crit)?\s*[<>≤≥]\s*([\d.]+)/g)) {
      out.push({ label: m[1] === "warn" ? `warning ${m[2]}` : `critical ${m[2]}`, value: Number(m[2]), warn: m[1] === "warn" });
    }
  }
  return out;
}

function drawChart(node, def, data) {
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  state.chartArgs = [node, def, data];  // redrawn as is when the theme changes
  const series = data.series || [];
  const hasData = series.some((s) => s.points.some((p) => p[1] != null));
  if (data.error || !hasData) {
    $("m-table").innerHTML = `<p class="muted">${esc(data.error || "No data in this range.")}</p>`;
    return;
  }
  const longRange = series[0].points.length && (Date.parse(series[0].points.at(-1)[0]) - Date.parse(series[0].points[0][0])) > 864e5;
  const labels = series[0].points.map((p) => new Date(p[0]).toLocaleString([], longRange
    ? { month: "short", day: "numeric", hour: "2-digit" } : { hour: "2-digit", minute: "2-digit" }));
  const datasets = series.map((s, i) => ({
    label: s.label, data: s.points.map((p) => p[1]), borderColor: cssVar(`--series-${(i % 8) + 1}`),
    backgroundColor: cssVar(`--series-${(i % 8) + 1}`), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
    spanGaps: true, tension: 0,
  }));
  for (const t of thresholdsFor(node, def.name)) {
    datasets.push({ label: t.label, data: labels.map(() => t.value), borderColor: cssVar(t.warn ? "--warning" : "--critical"),
      borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 0 });
  }
  state.chart = new Chart($("m-chart"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: datasets.length > 1, labels: { color: cssVar("--text"), boxWidth: 12, boxHeight: 2 } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.y)} ${data.unit || ""}` } },
      },
      scales: {
        x: { ticks: { color: cssVar("--muted"), maxTicksLimit: 8 }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { color: cssVar("--muted") }, grid: { color: cssVar("--border") },
          title: { display: !!data.unit, text: data.unit, color: cssVar("--muted") } },
      },
    },
  });
  const stats = series.map((s) => {
    const values = s.points.map((p) => p[1]).filter((v) => v != null);
    const last = values.at(-1);
    const avg = values.reduce((a, b) => a + b, 0) / (values.length || 1);
    return [esc(s.label), fmt(last), fmt(Math.max(...values)), fmt(avg)];
  });
  $("m-table").innerHTML = table(["Series", "Latest", "Max", "Average"], stats)
    + (data.truncated ? '<p class="muted">Showing the top 8 series.</p>' : "");
}

