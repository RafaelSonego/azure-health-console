"use strict";

// ---------------------------------------------------------------- workloads

// With `killedAt`, the logs of the instance that stopped then (`previous`), 5 minutes either side of it.
function podLogsButton(namespace, pod, container, killedAt) {
  const payload = esc(JSON.stringify({ namespace, pod: pod || "", container: container || "",
    ...(killedAt ? { at: killedAt, previous: "1", spread: 5 } : {}) }));
  return `<button type="button" class="link" data-logs="${payload}"${killedAt ? ' title="The killed instance\'s logs, 5 minutes either side of the kill"' : ""}>logs</button>`;
}

// ---------------------------------------------------------------- log viewer
// Log lines as time · level · message: a JSON line (structured logging) is unpacked and its other fields fold away;
// any other line keeps its text, with its level guessed from words like ERROR or WARN.
const LOG_KEYS = { level: ["level", "severity", "levelname", "lvl", "log.level", "@l"],
  message: ["message", "msg", "@m", "@mt", "log"], time: ["time", "timestamp", "ts", "@t", "asctime", "datetime"] };

function logTone(level) {
  const t = String(level || "").trim().toLowerCase();
  if (/^\d+$/.test(t)) return Number(t) >= 50 ? "error" : Number(t) >= 40 ? "warn" : "";
  return /^(fatal|crit|error|err\b|panic|alert|emerg)/.test(t) ? "error" : /^warn/.test(t) ? "warn" : "";
}

function parseLogLine(raw) {
  const m = /^(\d{4}-\d\d-\d\dT\S+\s+)?(\{[\s\S]*\})\s*$/.exec(raw);
  let o = null;
  if (m) try { o = JSON.parse(m[2]); } catch { o = null; }
  if (!o || typeof o !== "object" || Array.isArray(o)) {
    const tone = /\b(FATAL|CRITICAL|ERROR|Exception|Traceback|panic)\b/.test(raw) ? "error" : /\bWARN(ING)?\b/.test(raw) ? "warn" : "";
    return { time: "", level: "", tone, message: raw, rest: null };
  }
  const pick = (keys) => keys.find((k) => o[k] != null && typeof o[k] !== "object");
  const [lk, mk, tk] = [pick(LOG_KEYS.level), pick(LOG_KEYS.message), pick(LOG_KEYS.time)];
  const rest = Object.fromEntries(Object.entries(o).filter(([k]) => ![lk, mk, tk].includes(k)));
  return { time: tk ? String(o[tk]) : (m[1] || "").trim(), level: lk ? String(o[lk]) : "", tone: logTone(lk && o[lk]),
    message: mk ? String(o[mk]) : m[2], rest };
}

function logViewHtml(lines) {
  if (!lines.length) return '<p class="muted">No lines.</p>';
  const rows = lines.map((raw) => {
    const l = parseLogLine(String(raw));
    const parsed = l.time && !Number.isNaN(Date.parse(l.time)) ? new Date(l.time) : null;
    const when = l.time ? `<span class="ll-time" title="${esc(l.time)}">${esc(parsed ? parsed.toLocaleTimeString() : l.time)}</span>` : "";
    const more = l.rest && Object.keys(l.rest).length
      ? `<details class="ll-more"><summary>fields</summary><pre>${esc(JSON.stringify(l.rest, null, 2))}</pre></details>` : "";
    return `<div class="ll${l.tone ? ` ll-${l.tone}` : ""}" data-raw="${esc(raw)}">${when}${l.level ? `<span class="ll-level">${esc(l.level)}</span>` : ""}<span class="ll-msg">${esc(l.message)}</span>${more}</div>`;
  });
  const wrap = stored.get("ahc.logWrap") !== "0";
  return `<div class="logview${wrap ? " wrap" : ""}"><div class="logview-tools">
      <input type="search" class="logview-filter" placeholder="Filter these lines" aria-label="Filter these lines">
      <label class="inline"><input type="checkbox" class="logview-bad"> errors and warnings only</label>
      <label class="inline" title="Off: one line each, scroll sideways"><input type="checkbox" class="logview-wrap" ${wrap ? "checked" : ""}> wrap</label>
      <button type="button" class="link logview-copy" title="Copy the lines shown">copy</button>
      <span class="muted logview-count">${fmt(lines.length)} lines</span></div>
    <div class="logview-lines">${rows.join("")}</div></div>`;
}

function filterLogView(view) {
  const text = view.querySelector(".logview-filter").value.trim().toLowerCase();
  const bad = view.querySelector(".logview-bad").checked;
  const lines = [...view.querySelectorAll(".ll")];
  let shown = 0;
  for (const line of lines) {
    const hide = (bad && !line.matches(".ll-error, .ll-warn")) || (!!text && !line.dataset.raw.toLowerCase().includes(text));
    line.classList.toggle("hidden", hide);
    if (!hide) shown += 1;
  }
  view.querySelector(".logview-count").textContent = shown === lines.length ? `${fmt(lines.length)} lines` : `${fmt(shown)} of ${fmt(lines.length)} lines`;
}

document.addEventListener("input", (event) => {
  if (event.target.matches(".logview-filter")) filterLogView(event.target.closest(".logview"));
});
document.addEventListener("change", (event) => {
  const view = event.target.closest(".logview");
  if (!view) return;
  if (event.target.matches(".logview-bad")) filterLogView(view);
  if (event.target.matches(".logview-wrap")) { view.classList.toggle("wrap", event.target.checked); stored.set("ahc.logWrap", event.target.checked ? "1" : "0"); }
});
document.addEventListener("click", (event) => {
  const copy = event.target.closest(".logview-copy");
  if (copy) {
    const lines = [...copy.closest(".logview").querySelectorAll(".ll:not(.hidden)")].map((l) => l.dataset.raw);
    copyText(lines.join("\n"), copy, "copied");
    return;
  }
  const bar = event.target.closest(".open-panel");
  if (bar && bar.panel && event.target.closest("[data-open-back]")) {
    bar.panel.style.scrollMarginTop = `${toolbarHeight(bar) + 8}px`;
    bar.panel.scrollIntoView({ block: "start" });
    return;
  }
  const close = event.target.closest("[data-logs-close]") || (bar && event.target.closest("[data-open-close]"));
  if (close) {
    const panel = bar ? bar.panel : close.closest("tr.logs-row"), back = panel && panel.previousElementSibling;
    closeLogsRow(panel);
    if (back) { back.style.scrollMarginTop = `${toolbarHeight(back) + 8}px`; back.scrollIntoView({ block: "nearest" }); }
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !document.querySelector("tr.logs-row")) return;
  if (event.target.matches && event.target.matches("input, select, textarea") && event.target.value) return;
  document.querySelectorAll("tr.logs-row").forEach(closeLogsRow);
});

// A log panel in a row under the row clicked, one open at a time: ✕ Close, Esc or the same button again closes it.
// `fill(body, head)` loads it; `labels` are the button's text while closed and open.
function toggleLogsRow(button, title, fill, labels = ["logs", "hide logs"]) {
  const row = button.closest("tr");
  if (!row) return;
  const mine = row.nextElementSibling && row.nextElementSibling.classList.contains("logs-row");
  row.parentElement.querySelectorAll(":scope > tr.logs-row").forEach(closeLogsRow);  // a row opened inside one keeps it open
  if (mine) return;
  const panel = document.createElement("tr");
  panel.className = "logs-row";
  panel.dataset.attached = "";
  panel.logsButton = button;
  panel.logsLabels = labels;
  panel.openedAt = performance.now();
  const host = row.closest(".logs-panel, [data-jump]");
  const from = !host ? "" : host.classList.contains("logs-panel") ? (host.querySelector(".logs-panel-head strong") || {}).textContent || "" : host.dataset.jump;
  panel.innerHTML = `<td colspan="${row.children.length}"><div class="logs-panel"><div class="logs-panel-head">${from ? `<span class="panel-from muted">${esc(from)} ›</span>` : ""}<strong>${title}</strong>
    <span class="spacer"></span><button type="button" class="link" data-logs-close title="Close and go back to its row (Esc)">✕ Close</button></div><div class="logs-panel-body"></div></div></td>`;
  row.after(panel);
  button.textContent = labels[1];
  fill(panel.querySelector(".logs-panel-body"), panel.querySelector(".logs-panel-head strong"));
  panel.scrollIntoView({ block: "nearest" });
  openPanelBar(panel);
}

// The tab's toolbar (which stays at the top) names the last panel opened in the tab, with a way back to it and Close,
// so a long panel never leaves you wondering where you are.
function openPanelBar(changed) {
  const tools = (changed && changed.closest(".tab-body, #tab-body") || document).querySelector(".tab-tools");
  if (!tools) return;
  const open = [...tools.parentElement.querySelectorAll("tr.logs-row")].filter((p) => p.isConnected);
  const last = open.sort((a, b) => (a.openedAt || 0) - (b.openedAt || 0)).pop();
  let bar = tools.querySelector(".open-panel");
  if (!last) { if (bar) bar.remove(); return; }
  if (!bar) { bar = document.createElement("div"); bar.className = "open-panel"; tools.append(bar); }
  bar.panel = last;
  const head = last.querySelector(".logs-panel-head");
  const from = (head.querySelector(".panel-from") || {}).textContent || "";
  bar.innerHTML = `<span class="muted">Open:</span> ${esc(from)} <strong>${esc((head.querySelector("strong") || {}).textContent || "")}</strong>
    · <button type="button" class="link" data-open-back>Back to it</button> · <button type="button" class="link" data-open-close>✕ Close</button>`;
}

const toolbarHeight = (el) => ((el && el.closest(".tab-body, #tab-body") || document).querySelector(".tab-tools") || { offsetHeight: 0 }).offsetHeight;

function closeLogsRow(panel) {
  if (!panel) return;
  if (panel.logsButton) panel.logsButton.textContent = panel.logsLabels[0];
  const tools = (panel.closest(".tab-body, #tab-body") || { querySelector: () => null }).querySelector(".tab-tools");
  panel.remove();
  if (tools) openPanelBar(tools);
}

// A pod's logs from the API (`api/explore/logs`) into a logs row, titled with the pod and where they came from.
function podLogsRow(button, clusterId, target) {
  const name = `${esc(target.namespace)}/${esc(target.pod || "*")}${target.container ? "/" + esc(target.container) : ""}`;
  toggleLogsRow(button, `Logs: ${name}`, async (box, head) => {
    box.innerHTML = loading("Loading logs…");
    try {
      const logs = await api("api/explore/logs", { params: { id: clusterId, ...target, lines: 200 } });
      const around = target.at ? `, 5 minutes either side of the kill at ${esc(new Date(target.at).toLocaleString())}` : "";
      head.innerHTML = `Logs: ${name}${around} <span class="muted">via ${esc(logs.source || "")}</span>`;
      box.innerHTML = (logs.error ? noticeHtml(logs.error) : "") + logViewHtml(logs.lines || []);
    } catch (error) {
      box.innerHTML = errorHtml(error);
    }
  });
}

// ---------------------------------------------------------------- workload details
// Any line naming a Deployment, StatefulSet or DaemonSet opens its details under it (`api/workload/detail`, Kubernetes
// API, read on click), then its CPU and memory over 30 days and its runtime's memory (`api/workload/history`).
function workloadButton(namespace, label, text = label) {
  const at = String(label || "").indexOf("/");
  const owner = at > 0 ? podWorkload(label.slice(0, at), label.slice(at + 1)) : null;
  if (!owner || !namespace) return esc(text || "");
  const payload = esc(JSON.stringify({ namespace, kind: owner.kind, workload: owner.name }));
  return `<button type="button" class="link" data-workload="${payload}" title="Open its details: HPA, requests against use, pods, events">${esc(text)}</button>`;
}

const cpuText = (m) => (m == null ? "–" : m >= 1000 ? `${(m / 1000).toFixed(m >= 10000 ? 0 : 2)} cores` : `${Math.round(m)}m`);
const memText = (mib) => (mib == null ? "–" : mib >= 1024 ? `${(mib / 1024).toFixed(1)} GiB` : `${Math.round(mib)} MiB`);
const ofRequest = (p) => (p == null ? "" : ` <span class="muted">(${fmt(p)}%)</span>`);

function workloadDetailRow(button, clusterId, target) {
  const label = button.textContent;
  toggleLogsRow(button, `${esc(target.kind)} ${esc(target.namespace)}/${esc(target.workload)}`,
    (box, head) => openWorkload(box, head, clusterId, target), [label, label]);
}

async function loadWorkloadDetail(box, head, clusterId, target) {
  box.innerHTML = loading("Reading it from the Kubernetes API: the workload, its HPA, pods, metrics and events…");
  const params = { cluster: clusterId, namespace: target.namespace, kind: target.kind, workload: target.workload };
  const d = await api("api/workload/detail", { params }).catch((error) => ({ error: error.message }));
  if (d.error || !d.available) { box.innerHTML = noticeHtml(d.error || d.reason); return; }
  head.innerHTML = `${esc(target.kind)} ${esc(target.namespace)}/${esc(target.workload)} <span class="muted">via ${esc(d.source || "the Kubernetes API")}</span>`;
  box.innerHTML = workloadDetailHtml(d) + [(d.yaml || {}).workload, (d.yaml || {}).hpa].map((y) => yamlFoldHtml(y, clusterId)).join("")  /* JSON keys arrive sorted */ + `<h5>CPU and memory over time, runtime memory</h5><div class="wl-history">${loading("Reading 7 and 30 days of CPU and memory…")}</div>`;
  const targets = Object.fromEntries(((d.hpa || {}).metrics || []).filter((m) => m.type === "Utilization" && typeof m.target === "number")
    .map((m) => [`target_${m.resource}`, m.target]));
  const tool = pathTool();
  const h = await api("api/workload/history", { params: { ...params, ...targets, ...(tool ? toolParams(tool) : {}) } }).catch((error) => ({ error: error.message }));
  const out = box.querySelector(".wl-history");
  if (out) out.innerHTML = h.error ? noticeHtml(h.error) : workloadHistoryHtml(h, d);
  box.insertAdjacentHTML("beforeend", workloadFitFormHtml(d, h.error ? {} : h.needed || {}));
  wireWorkloadFit(box, params, d, h.error ? {} : h.needed || {});
}

// Would a change fit: new requests per container (today's, or what the HPA targets need), checked on Check only.
function workloadFitFormHtml(d, needed) {
  const rows = (d.containers || []).map((c) => [`<strong>${esc(c.name)}</strong>`,
    `<input type="number" min="0" step="50" data-fit-cpu="${esc(c.name)}" value="${Math.round(c.cpu.request || 0)}" aria-label="${esc(c.name)} CPU request in millicores"> m`,
    `<input type="number" min="0" step="256" data-fit-mem="${esc(c.name)}" value="${Math.round(c.memory.request || 0)}" aria-label="${esc(c.name)} memory request in MiB"> MiB`,
    `<span class="muted">now ${cpuText(c.cpu.request)} · ${memText(c.memory.request)}</span>`]);
  return `<h5>Would a change fit?</h5><div class="wl-fit">
    <p class="muted">New requests per pod, checked against the nodes its pods run on now and every pod on them (Kubernetes API, read when you
      press Check). The replicas follow its HPA's own arithmetic with the new request, unless you give a number.</p>
    <table class="wl-fit-form"><thead><tr><th>Container</th><th>CPU request</th><th>Memory request</th><th></th></tr></thead><tbody>${
      rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>
    <div class="table-tools"><label class="inline">Replicas <input type="number" min="1" max="10000" data-fit-replicas placeholder="from its HPA" style="width:9em"></label>
      ${Object.keys(needed).length ? `<button type="button" data-fit-suggest title="Each request raised or lowered to what the HPA's targets need at the peak (history above), rounded up">Use what its HPA targets need</button>` : ""}
      <button type="button" data-fit-check>Check</button></div><div class="wl-fit-out"></div></div>`;
}

function wireWorkloadFit(box, params, d, needed) {
  const wrap = box.querySelector(".wl-fit");
  if (!wrap) return;
  const cpuIn = [...wrap.querySelectorAll("[data-fit-cpu]")], memIn = [...wrap.querySelectorAll("[data-fit-mem]")];
  const suggest = () => {
    // The need is for the pod; each container gets its share of today's request, rounded up to 50m and 256 MiB.
    for (const [res, inputs, total, step] of [["cpu", cpuIn, (needed.cpu || {}).need * 1000, 50], ["memory", memIn, (needed.memory || {}).need / 2 ** 20, 256]]) {
      if (!total) continue;
      const now = (d.containers || []).map((c) => c[res].request || 0), sum = now.reduce((a, b) => a + b, 0);
      inputs.forEach((input, i) => { input.value = Math.ceil((sum ? total * now[i] / sum : total / inputs.length) / step) * step; });
    }
  };
  const check = async () => {
    const out = wrap.querySelector(".wl-fit-out");
    const containers = cpuIn.map((input, i) => `${input.dataset.fitCpu}:${Math.round(Number(input.value) || 0)}:${Math.round(Number(memIn[i].value) || 0)}`);
    const replicas = wrap.querySelector("[data-fit-replicas]").value.trim();
    out.innerHTML = loading("Reading the nodes and every pod on them…");
    const f = await api("api/workload/fit", { params: { ...params, container: containers, ...(replicas ? { replicas } : {}) } })
      .catch((error) => ({ error: error.message }));
    out.innerHTML = f.error || !f.available ? noticeHtml(f.error || f.reason) : workloadFitHtml(f);
  };
  wrap.addEventListener("click", (event) => {
    if (event.target.closest("[data-fit-suggest]")) suggest();
    if (event.target.closest("[data-fit-check]")) check();
  });
  wrap.addEventListener("keydown", (event) => { if (event.key === "Enter" && event.target.matches("input")) { event.preventDefault(); check(); } });
}

function workloadFitHtml(f) {
  const pod = `${cpuText(f.per_pod.after[0])} · ${memText(f.per_pod.after[1])}`;
  const m = f.missing, r = f.replicas, ro = f.rollout;
  const where = f.pools.map((p) => `${fmt(p.usable)} node(s) of ${esc(p.pool)}`).join(" and ");
  let head = f.fits ? `${pill("healthy")} <strong>Fits:</strong> ${fmt(r.after)} pod(s) of ${pod} on the ${where}.`
    : `${pill("warning")} <strong>Doesn't fit:</strong> ${fmt(f.short)} of ${fmt(r.after)} pod(s) of ${pod} have no room on the ${where}.`;
  if (m) {
    const auto = (f.pools.find((p) => p.pool === m.pool) || {}).autoscaler;
    head += m.nodes ? ` It needs <strong>${fmt(m.nodes)} more node(s)</strong> of ${esc(m.pool)} (${fmt(m.per_node)} such pod(s) per new node);`
      + (m.room === null ? (auto ? " its autoscaler is off, so someone adds them." : " the pool's autoscaler settings couldn't be read.")
        : m.room >= m.nodes ? ` its autoscaler can add them (${fmt(m.room)} below its maximum of ${fmt(auto.max)}).`
        : ` its autoscaler can only add ${fmt(m.room)} (maximum ${fmt(auto.max)}).`)
      : ` A pod this size doesn't fit on an empty node of ${esc(m.pool)}.`;
  }
  const pct = (a, b) => (b ? ` <span class="muted">(${fmt(Math.round(100 * a / b))}%)</span>` : "");
  const pools = table(["Pool", "Nodes", "CPU requested: before → after", "Memory requested: before → after", "Its pods", "Autoscaler"], f.pools.map((p) => [
    esc(p.pool), `${fmt(p.usable)}${p.usable < p.nodes ? ` <span class="muted">of ${fmt(p.nodes)}</span>` : ""}`,
    `${cpuText(p.before_cpu)}${pct(p.before_cpu, p.alloc_cpu)} → ${cpuText(p.after_cpu)}${pct(p.after_cpu, p.alloc_cpu)} <span class="muted">of ${cpuText(p.alloc_cpu)}</span>`,
    `${memText(p.before_mem)}${pct(p.before_mem, p.alloc_mem)} → ${memText(p.after_mem)}${pct(p.after_mem, p.alloc_mem)} <span class="muted">of ${memText(p.alloc_mem)}</span>`,
    `${fmt(p.pods_before)} → ${fmt(p.pods_after)}`,
    p.autoscaler ? (p.autoscaler.autoscale ? `${fmt(p.autoscaler.min)}–${fmt(p.autoscaler.max)}, now ${fmt(p.autoscaler.count)}` : "off")
      + (p.autoscaler.vm_size ? ` <span class="muted">${esc(p.autoscaler.vm_size)}</span>` : "") : "–"]), "wl-fit-pools");
  const metrics = r.metrics.length ? table(["Metric", "Now", "With the new request", "Target", "Asks for"], r.metrics.map((x) => [
    esc(x.resource), `${fmt(x.now)}%`, `${fmt(x.after)}%`, `${fmt(x.target)}%`, fmt(x.asks)]), "wl-fit-metrics") : "";
  const rollout = ro.surge || ro.unavailable ? `<p><strong>Rolling update:</strong> up to ${fmt(ro.surge)} new pod(s) at once (maxSurge) and ${fmt(ro.unavailable)}
    old one(s) down (maxUnavailable); room now for ${fmt(ro.room_now)} new pod(s) next to the old ones, on ${fmt(ro.nodes_with_room)} node(s).
    ${ro.note ? `<br>${pill("warning")} ${esc(ro.note)}` : ""}</p>` : (ro.note ? `<p class="muted">${esc(ro.note)}</p>` : "");
  return `<p>${head}</p><p>Replicas: ${fmt(r.now)} now → <strong>${fmt(r.after)}</strong> <span class="muted">(${esc(r.why)})</span></p>
    ${metrics}${pools}${rollout}<ul class="wl-notes muted">${f.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`;
}

function workloadDetailHtml(d) {
  const o = d.overview, h = d.hpa;
  const roll = (o.strategy || {}).rollingUpdate || {};
  const head = `<p><strong>${fmt(o.ready ?? 0)} of ${fmt(o.desired ?? 0)} ready</strong>${o.updated != null ? ` · ${fmt(o.updated)} on the latest version` : ""}
    ${o.revision ? ` · revision ${esc(o.revision)}` : ""}${o.created ? ` · created ${seen(o.created)}` : ""}
    · rollout ${esc((o.strategy || {}).type || "–")}${roll.maxSurge != null ? ` (max surge ${esc(roll.maxSurge)}, max unavailable ${esc(roll.maxUnavailable)})` : ""}
    · service account ${esc(o.service_account)}${Object.keys(o.node_selector || {}).length ? ` · nodes ${esc(Object.entries(o.node_selector).map(([k, v]) => `${k}=${v}`).join(", "))}` : ""}</p>
    ${o.rollout && o.rollout.reason ? `<p class="muted">Last rollout: ${esc(o.rollout.reason)}${o.rollout.lastUpdateTime ? `, ${seen(o.rollout.lastUpdateTime)}` : ""}. ${esc(o.rollout.message || "")}</p>` : ""}
    <p class="muted">Images: ${(o.images || []).map((i) => `<code>${esc(i)}</code>`).join(", ")}</p>`;
  const unread = Object.entries(d.unreadable || {}).map(([k, why]) => `<p class="muted">Couldn't read its ${esc(k.replace("_", " "))}: ${esc(why)}</p>`).join("");
  const findings = (d.findings || []).length ? `<h5>What the checks say</h5>${table(["", "Problem", "What to do"], d.findings.map((f) => [
    pill(SEVERITY_STATUS[f.severity]), `${esc(f.problem)}${f.container ? ` <span class="muted">(${esc(f.container)})</span>` : ""}<br><span class="muted">${esc(f.why)}</span>`, `${esc(f.action)}${ruleHtml(f.rule)}`]), "wl-findings")}` : "";
  return head + unread + findings + workloadHpaHtml(h, d) + workloadContainersHtml(d) + workloadPodsHtml(d) + workloadEventsHtml(d);
}

function workloadHpaHtml(h, d) {
  if (!h) return `<h5>Scaling</h5><p class="muted">No HPA: it runs a fixed ${fmt(d.overview.desired ?? 0)} replica(s).</p>`;
  const line = h.decides == null ? `Its HPA has no CPU or memory reading now, so it can't say which metric decides.`
    : h.limited ? `Held at its maximum of ${fmt(h.max)} by <strong>${esc(h.decides)}</strong>: that metric alone asks for ${fmt(h.wants)} pods.`
      : `<strong>${esc(h.decides)}</strong> decides: it asks for ${fmt(h.wants)} pods (min ${fmt(h.min)}, max ${fmt(h.max)}).`;
  const metrics = table(["Metric", "Target", "Now", "Pods it asks for"], h.metrics.map((m) => {
    const unit = m.type === "Utilization" ? "% of request" : "";
    return [`${esc(m.resource)}${m.resource === h.decides ? ` ${pill(h.limited ? "critical" : "info", "this metric sets the replica count")}` : ""}`,
      `${esc(m.target)}${unit ? `% <span class="muted">${unit.slice(2)}</span>` : ""}`, m.current == null ? "–" : `${esc(m.current)}${unit ? "%" : ""}`,
      m.asks == null ? "–" : fmt(m.asks) + (m.asks > h.max ? ` <span class="muted">(capped at ${fmt(h.max)})</span>` : m.asks < h.min ? ` <span class="muted">(min ${fmt(h.min)})</span>` : "")];
  }), "wl-hpa");
  const conditions = table(["Condition", "Status", "Reason", "Kubernetes says", "Since"], (h.conditions || []).map((c) => [
    esc(c.type), c.status === "True" ? (c.type === "ScalingLimited" ? pill("warning") + " True" : "True") : pill(c.type === "ScalingLimited" ? "healthy" : "critical") + " " + esc(c.status),
    esc(c.reason || ""), `<span class="muted ai-msg">${esc(c.message || "")}</span>`, c.lastTransitionTime ? seen(c.lastTransitionTime) : "–"]), "wl-hpa-conditions");
  const down = (h.behavior || {}).scaleDown, up = (h.behavior || {}).scaleUp;
  const behave = (name, b) => (b ? `${name}: wait ${fmt(b.stabilizationWindowSeconds ?? (name === "Scale down" ? 300 : 0))} s${(b.policies || []).map((p) => `, ${esc(p.type)} ${fmt(p.value)} per ${fmt(p.periodSeconds)} s`).join("")}` : "");
  return `<h5>Scaling: HPA ${esc(h.name)}</h5>
    <p class="big-line">${line}</p>
    <p class="muted">${fmt(h.current)} now, ${fmt(h.desired ?? h.current)} wanted, min ${fmt(h.min)}, max ${fmt(h.max)}${h.last_scale ? ` · last scaled ${seen(h.last_scale)}` : ""}.
      Each metric asks for <em>current pods × now ÷ target</em> (left alone within 10%), and the highest one wins.
      ${[behave("Scale up", up), behave("Scale down", down)].filter(Boolean).map(esc).join(" · ") || "Default behaviour: scale down after 5 minutes at a lower reading."}</p>
    ${metrics}${conditions}`;
}

function workloadContainersHtml(d) {
  const probe = (p) => (p ? `${esc(p.how)}${p.initialDelaySeconds ? `, after ${fmt(p.initialDelaySeconds)} s` : ""}${p.periodSeconds ? `, every ${fmt(p.periodSeconds)} s` : ""}` : '<span class="muted">none</span>');
  const rows = d.containers.map((c) => [
    `<strong>${esc(c.name)}</strong>${c.sidecar ? ' <span class="muted">sidecar</span>' : ""}`,
    `${cpuText(c.cpu.request)} / ${cpuText(c.cpu.limit)}`, `${cpuText(c.cpu.used)}${ofRequest(c.cpu.of_request)}`,
    `${memText(c.memory.request)} / ${memText(c.memory.limit)}`, `${memText(c.memory.used)}${ofRequest(c.memory.of_request)}${c.memory.of_limit != null ? ` <span class="muted">${fmt(c.memory.of_limit)}% of limit</span>` : ""}`,
    `<span class="muted">start: ${probe(c.probes.startup)}<br>ready: ${probe(c.probes.readiness)}<br>live: ${probe(c.probes.liveness)}</span>`,
    (c.memory_flags || []).length ? c.memory_flags.map((f) => f.flag ? `<code>${esc(f.flag)}</code> <span class="muted">${esc(f.source)}</span>`
      : `<span class="muted">${esc(f.source)}: ${esc(f.note)}</span>`).join("<br>") + (c.heap_max ? `<br><span class="muted">heap max ${memText(c.heap_max)}</span>` : "") : '<span class="muted">none set</span>']);
  const mounts = d.containers.flatMap((c) => (c.mounts || []).filter((m) => m.kind === "emptyDir").map((m) =>
    `${esc(c.name)}: <code>${esc(m.path)}</code> emptyDir${m.memory ? " in memory" : " on the node's disk"}${m.size_limit ? `, limit ${esc(m.size_limit)}` : ", no size limit"}`));
  const notes = d.containers.flatMap((c) => (c.notes || []).map((n) => `${esc(c.name)}: ${esc(n)}`));
  return `<h5>Containers: requests against use</h5>
    <p class="muted">Use is the average over its pods right now (metrics-server)${d.usage_live ? "" : ", not available on this cluster"}. The HPA compares use with the <em>request</em>;
      the limit is where a container is throttled (CPU) or killed (memory). Runtime memory settings are picked out of its env and arguments:
      only those flags are shown, never the rest of a value.</p>
    ${table(["Container", "CPU request / limit", "CPU used (of request)", "Memory request / limit", "Memory used (of request)", "Probes", "Runtime memory settings"], rows, "wl-containers")}
    ${notes.length ? `<ul class="wl-notes">${notes.map((n) => `<li>${n}</li>`).join("")}</ul>` : ""}
    ${mounts.length ? `<p class="muted">Scratch volumes (counted in the pod's memory when in memory, or as page cache when written often): ${mounts.join(" · ")}</p>` : ""}`;
}

function workloadPodsHtml(d) {
  const pods = d.pods || [];
  const spread = (d.spread || {});
  const zones = (spread.zones || []).filter(([z]) => z).map(([z, n]) => `zone ${esc(z)}: ${fmt(n)}`).join(", ");
  const crowded = (spread.nodes || []).filter(([, n]) => n > 1).slice(0, 3).map(([node, n]) => `${esc(node)}: ${fmt(n)}`).join(", ");
  return `<h5>Pods (${fmt(pods.length)})</h5>
    <p class="muted">On ${fmt((spread.nodes || []).length)} node(s)${zones ? `; ${zones}` : ""}${crowded ? `; most on one node: ${crowded}` : ""}.</p>
    ${table(["Pod", "Node (pool)", "Zone", "Ready", "Restarts", "Last exit", "Started", ""], pods.map((p) => [
      podButton(d.overview.namespace, p.pod), `${esc(p.node)} <span class="muted">${esc(p.pool)}</span>`, esc(p.zone || "–"),
      `${p.ready < p.containers ? pill("warning") + " " : ""}${fmt(p.ready)}/${fmt(p.containers)}${p.waiting ? ` <span class="muted">${esc(p.waiting)}</span>` : ""}`,
      fmt(p.restarts), p.last_exit ? `${esc(p.last_exit.reason || "")} ${p.last_exit.code != null ? `(exit ${esc(p.last_exit.code)})` : ""} ${p.last_exit.at ? seen(p.last_exit.at) : ""}` : "–",
      p.started ? seen(p.started) : "–", podLogsButton(d.overview.namespace, p.pod)]), "wl-pods")}`;
}

function workloadEventsHtml(d) {
  const events = d.events || [];
  return `<h5>Events</h5>${events.length ? table(["When", "Type", "Reason", "Object", "Count", "Message"], events.map((e) => [
    e.at ? seen(e.at) : "–", e.type === "Warning" ? pill("warning") + " Warning" : esc(e.type || ""), esc(e.reason || ""), esc(e.object), fmt(e.count),
    `<span class="muted ai-msg">${esc(e.message)}</span>`]), "wl-events")
    : '<p class="muted">No events about it, its pods or its HPA: Kubernetes keeps events about an hour.</p>'}`;
}

function workloadHistoryHtml(h, d) {
  const u = h.usage || {};
  const mib = (bytes) => (bytes == null ? null : bytes / 1048576);
  const rows = (u.containers || []).map((c) => [esc(c.container),
    `${cpuText(c.cpu.p95 * 1000)} / ${cpuText(c.cpu.p99 * 1000)} / ${cpuText(c.cpu.max_7d * 1000)}`, cpuText(c.cpu.max_30d * 1000),
    `${memText(mib(c.memory.p95))} / ${memText(mib(c.memory.p99))} / ${memText(mib(c.memory.max_7d))}`, memText(mib(c.memory.max_30d))]);
  const podCpu = d.containers.reduce((a, c) => a + (c.cpu.request || 0), 0), podMem = d.containers.reduce((a, c) => a + (c.memory.request || 0), 0);
  const need = Object.entries(h.needed || {}).map(([res, n]) => {
    const now = res === "cpu" ? podCpu : podMem;
    const needed = res === "cpu" ? n.need * 1000 : mib(n.need);
    const text = res === "cpu" ? cpuText : memText;
    const ok = now >= needed;
    return `<li>${ok ? pill("healthy") : pill("warning")} <strong>${esc(res)}</strong>: ${esc(n.basis)} ${text(res === "cpu" ? n.peak * 1000 : mib(n.peak))} per pod, so
      ${ok ? `the request of ${text(now)} keeps it under the ${fmt(n.target)}% target, and the HPA can scale down`
        : `the pod's request must be at least <strong>${text(needed)}</strong> (now ${text(now)}) for the HPA's ${fmt(n.target)}% target to be reachable;
           otherwise this metric keeps asking for more pods`}.</li>`;
  });
  const r = h.runtime || {};
  const live = d.containers.reduce((a, c) => a + (c.memory.used || 0), 0);
  const heapDays = (r.heap_daily || []).map(([, v]) => v).filter((v) => typeof v === "number");
  const runtime = r.found ? `<p>App <strong>${esc(r.app)}</strong> in ${esc(r.source)} <span class="muted">(${esc(r.why)})</span>:
      heap used ${memText(r.heap_used)} of ${memText(r.heap_max)} max (${memText(r.heap_committed)} committed), non-heap ${memText(r.non_heap_used)},
      process ${memText(r.process)}; the pod's working set is ${memText(live)} now.</p>
      ${heapDays.length ? `<p class="muted">Heap used per day over 30 days: ${memText(Math.min(...heapDays))} to ${memText(Math.max(...heapDays))}${Math.max(...heapDays) < 1.2 * Math.min(...heapDays) ? " (flat: no heap growth)" : ""}.</p>` : ""}
      ${r.process && live > 1.3 * r.process ? `<p class="notice partial">The pod holds ${memText(live - r.process)} more than the app's process. That memory is outside the runtime:
        usually files written in the container (logs, temp files, an emptyDir) kept as page cache, or another process. Kubernetes counts it, so the HPA does too.</p>` : ""}`
    : `<p class="muted">Runtime memory: ${esc(r.reason || "not read")}${r.app ? ` (app ${esc(r.app)})` : ""}</p>`;
  const queries = [...(u.queries || []), ...(r.queries || [])];
  return `${(h.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join("")}
    ${u.found ? `<p class="muted">From ${esc(u.source)}, per container and pod.</p>
      ${table(["Container", "CPU p95 / p99 / max, 7 days", "CPU max, 30 days", "Memory p95 / p99 / max, 7 days", "Memory max, 30 days"], rows, "wl-usage")}`
      : `<p class="muted">${esc(u.reason || "No CPU or memory history.")}</p>`}
    ${need.length ? `<p><strong>Requests for its HPA targets</strong> <span class="muted">(memory from its 30-day max, since memory seldom falls back;
      CPU from its 7-day p99, since its max is usually start-up, which the limit covers)</span></p><ul class="wl-notes">${need.join("")}</ul>` : ""}
    ${runtime}
    ${queries.length ? `<details class="mon-query"><summary>Queries sent</summary>${queryBlock({ queries })}</details>` : ""}`;
}

// What an AKS workload depends on in Azure (`explore/dependencies.py`): from its configuration and its identity.
const DEP_SOURCE = { "configuration and identity": "Config + identity", configuration: "Config", identity: "Identity" };
function dependenciesHtml(d) {
  const flag = `<p class="notice partial">${d.config_available
    ? `Read ${esc(d.read)}'s env and the ConfigMaps it uses${d.config_maps.length ? ` (${esc(d.config_maps.join(", "))})` : ""}: their values are
      scanned for host names on this machine, and only host names are shown.`
    : `Configuration not read: ${esc(d.config_reason || "the Kubernetes API isn't available")} So this list comes from Azure alone: what the
      workload's identity holds roles on. A dependency reached with a key or connection string isn't on it.`}
    ${d.secrets_not_read.length ? ` Secrets are never read: it also uses ${esc(d.secrets_not_read.join(", "))}.` : ""}</p>`;
  const who = d.identities.length ? `<p class="muted">Identity: ${d.identities.map((i) => `${esc(i.name)} <span class="muted">(${esc(i.subject)})</span>`).join(", ")}</p>`
    : `<p class="muted">No workload identity found for ${esc(d.service_account ? `service account ${d.service_account}` : `namespace ${d.namespace}`)}.</p>`;
  const rows = table(["Resource", "Type", "From", "Host names", "Found in", "Identity's roles"], d.resources.map((r) => [
    `${esc(r.name)} ${portalLinkHtml(r.id, { label: "↗", title: "Open it in the Azure portal" })}`, esc(r.type.split("/").slice(1).join("/")), esc(DEP_SOURCE[r.source] || r.source),
    esc(r.hosts.join(", ")) || '<span class="muted">–</span>', `<span class="muted">${esc(r.found_in.join("; "))}</span>`,
    esc([...r.roles, ...r.data_roles.map((x) => `${x} (data)`)].join(", ")) || '<span class="muted">none: uses a key or connection string</span>']), "dependencies");
  const list = (title, items) => (items.length ? `<h4>${esc(title)}</h4>${table(["Host", "Found in"], items.map((x) => [esc(x.host), `<span class="muted">${esc(x.found_in.join("; "))}</span>`]))}` : "");
  return `${flag}${who}${d.resources.length ? rows : '<p class="muted">No Azure resources found.</p>'}
    ${d.broad_roles.length ? `<p class="muted">Also holds ${d.broad_roles.map((b) => `${esc(b.role)} on ${esc(b.scope)}`).join("; ")}.</p>` : ""}
    ${list("Services in the cluster", d.in_cluster)}${list("Other hosts (not an Azure resource in scope)", d.other_hosts)}
    ${(d.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join("")}`;
}
async function loadDependencies(box, params) {
  box.innerHTML = loading("Reading its configuration and identity…");
  const d = await api("api/explore/dependencies", { params: { ...scopeParams(currentScope()), ...params } }).catch((error) => ({ error: error.message }));
  box.innerHTML = d.error ? noticeHtml(d.error) : dependenciesHtml(d);
}

// ------------------------------------------------------------------ workload path (decision 150)
// One workload and the hops in front of it, each from wherever its data is. Its front doors are suggested from data and
// confirmed once (kept per tenant); then every hop is read: last hour against the same hour last week, per minute.
const pathTool = () => toolKinds().find((k) => (state.selected || {})[k]) || "";
const PATH_ROWS = { agw: "App Gateway", apim: "API Management", requests: "Requests", pods: "Pods" };

function pathFigures(hop) {
  const n = hop.now || {}, w = hop.week || {};
  if (hop.hop === "pods") {
    return [`${fmt(n.restarts ?? 0)} restarts · ${fmt(n.pods ?? 0)} pods${n.not_ready ? ` · <strong class="worse">${fmt(n.not_ready)} not ready</strong>` : ""}${n.oom_containers ? ` · ${fmt(n.oom_containers)} OOM` : ""}`,
      `${fmt(w.restarts ?? 0)} restarts`];
  }
  const line = (r) => (r.total ? `${fmt(r.errors || 0)} of ${fmt(r.total)} failed (${pctText(r.total ? (100 * (r.errors || 0)) / r.total : 0, r.errors)})` : "no requests");
  const extra = [n.p95_ms != null ? `p95 ${msLabel(n.p95_ms)}` : "", n.gateway_errors ? `${fmt(n.gateway_errors)} the gateway's own (no backend answer)` : "",
    n.backend_errors ? `${fmt(n.backend_errors)} from the backend` : "", n.unreachable ? `${fmt(n.unreachable)}× 502` : "", n.timed_out ? `${fmt(n.timed_out)}× 504` : ""].filter(Boolean);
  return [`${line(n)}${extra.length ? `<div class="muted">${extra.join(" · ")}</div>` : ""}`, line(w)];
}

function pathHopName(hop) {
  const d = hop.details || {};
  const what = hop.hop === "apim" ? d.api : hop.hop === "agw" ? `${d.listener} → ${d.pool}` : hop.hop === "requests" ? d.component || "" : "";
  return `<strong>${esc(hop.label)}</strong>${what ? `<div class="muted">${esc(what)}</div>` : ""}`;
}

// Every hop's errors per minute over the last hour on one shared 60-minute axis (pods: restarts per 5 minutes).
function pathTimelineHtml(hops) {
  const end = Date.now(), w = 360, h = 26;
  const rows = hops.filter((x) => x.available && x.minutes && x.minutes.length).map((hop) => {
    const slots = new Array(60).fill(0);
    for (const [t, , errors] of hop.minutes) {
      const i = 59 - Math.floor((end - Date.parse(t)) / 60000);
      if (i >= 0 && i < 60) slots[i] += errors || 0;
    }
    const peak = Math.max(...slots, 1), bw = w / 60;
    const bars = slots.map((v, i) => (v ? `<rect x="${(i * bw).toFixed(1)}" y="${(h - (h * v) / peak).toFixed(1)}" width="${Math.max(bw - 1, 1).toFixed(1)}" height="${((h * v) / peak).toFixed(1)}"/>` : "")).join("");
    return `<div class="path-strip"><span>${esc(hop.label)}</span><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
      aria-label="${esc(`${hop.label}: ${hop.hop === "pods" ? "restarts" : "errors"} per minute, peak ${peak}`)}"><title>${esc(`${hop.hop === "pods" ? "Restarts" : "Errors"} per minute, peak ${fmt(Math.max(...slots))}`)}</title>${bars}</svg>
      <span class="muted">peak ${fmt(Math.max(...slots))}</span></div>`;
  });
  return rows.length ? `<div class="path-timeline"><p class="muted">Last 60 minutes, left to right, each row on its own scale:</p>${rows.join("")}</div>` : "";
}

function pathLinkSummary(link, tool) {
  const parts = [...(link.agw || []).map((g) => `App Gateway ${g.gateway_id.split("/").pop()}: ${g.listener} → ${g.pool}`),
    ...(link.apim || []).map((a) => `API ${a.api} on ${a.service_id.split("/").pop()}`),
    link.app ? `${tool || "monitoring tool"} app ${link.app}` : "", link.component ? `App Insights ${link.component.split("/").pop()}` : ""].filter(Boolean);
  return parts.length ? `Front doors: ${esc(parts.join(" · "))}` : "No front doors linked yet: only its pods are read, and its requests when a component or app is linked.";
}

async function loadPath(box, params) {
  const tool = pathTool();
  box.innerHTML = loading("Reading each hop: last hour against the same hour last week…");
  const data = await api("api/path/read", { params: { ...params, ...(tool ? toolParams(tool) : {}) } }).catch((error) => ({ error: error.message }));
  if (data.error) { box.innerHTML = noticeHtml(data.error); return; }
  const rows = data.hops.map((hop) => {
    const [now, week] = hop.available ? pathFigures(hop) : [`<span class="muted">${esc(hop.reason)}</span>`, ""];
    const detail = [...((hop.details || {}).reasons || []).map(([r, n]) => `${r} ×${fmt(n)}`),
      ...((hop.details || {}).backend_statuses || []).map(([r, n]) => `backend ${r} ×${fmt(n)}`),
      ...((hop.details || {}).events || []).map(([r, n]) => `${r} ×${fmt(n)}`)].join(" · ");
    return [hop.status ? pill(hop.status, hop.why) : '<span class="muted">–</span>', pathHopName(hop), `<span class="muted">${esc(hop.source)}</span>`,
      now, week, `<span class="muted ai-msg">${esc(detail)}</span>`];
  });
  const queries = data.hops.flatMap((h) => h.queries || []);
  const pr = data.rules || {};
  box.innerHTML = `<p class="big-line">${esc(data.verdict)}</p>
    ${data.rules ? `<p class="muted rule">Rising: ${fmt(pr.requests_failed_min)}+ failed requests and the rate ${fmt(pr.requests_failed_rate_points)} point(s) above last week
      (${settingLink("appinsights.failed_min", "the spike rule")}), or ${fmt(pr.restarts_min)}+ pod restarts; a drop: under ${fmt(pr.drop_percent)}% of last week's
      traffic when it had ${fmt(pr.drop_min)}+ · ${settingLink("path.drop_percent", "change")}</p>` : ""}
    <p class="muted">${pathLinkSummary(data.link || {}, data.tool)} <button type="button" class="link" data-path-edit>${(data.link.apim || []).length || (data.link.agw || []).length ? "Change" : "Link front doors"}</button></p>
    ${table(["", "Hop", "From", "Last hour", "Same hour last week", "Detail"], rows, "path-hops")}
    ${pathTimelineHtml(data.hops)}
    <div class="path-edit"></div>
    ${queries.length ? `<details class="mon-query"><summary>Queries sent</summary>${queryBlock({ queries })}</details>` : ""}`;
  box.querySelector("[data-path-edit]").addEventListener("click", () => editPathLinks(box, params, data.link || {}));
}

async function editPathLinks(box, params, link) {
  const out = box.querySelector(".path-edit");
  const tool = pathTool();
  out.innerHTML = loading("Looking for its front doors: API Management APIs, App Gateway routes, its app…");
  const c = await api("api/path/candidates", { params: { ...params, ...scopeParams(currentScope()), ...(tool ? toolParams(tool) : {}) } })
    .catch((error) => ({ error: error.message }));
  if (c.error) { out.innerHTML = noticeHtml(c.error); return; }
  const has = (list, test) => (list || []).some(test);
  const why = (r) => `<span class="muted">${esc(r.why.join("; "))}</span>`;
  const apis = c.apim.map((r, i) => `<label class="path-pick"><input type="checkbox" data-path-apim="${i}" ${has(link.apim, (a) => sameId(a.service_id, r.service_id) && a.api === r.api) ? "checked" : ""}>
    <strong>${esc(r.name)}</strong> <span class="muted">${esc(r.service)} · /${esc(r.path)} → ${esc(r.backend)}</span> ${why(r)}</label>`);
  const routes = c.agw.map((r, i) => `<label class="path-pick"><input type="checkbox" data-path-agw="${i}" ${has(link.agw, (g) => sameId(g.gateway_id, r.gateway_id) && g.listener === r.listener && g.pool === r.pool) ? "checked" : ""}>
    <strong>${esc(r.gateway)}</strong> <span class="muted">${esc(r.listener)}${r.hosts.length ? ` (${esc(r.hosts.join(", "))})` : ""} → ${esc(r.pool)}${r.paths.length ? ` ${esc(r.paths.join(", "))}` : ""}</span> ${why(r)}</label>`);
  const appOptions = [...new Set([link.app, ...c.apps.map((a) => a.app)].filter(Boolean))];
  out.innerHTML = `<h4>Front doors of ${esc(c.workload.name)}</h4>
    <p class="muted">Suggested from data, best first; tick the ones that send to this workload. Nothing is assumed: only what you tick is read.
      Kept on this machine for this tenant (config file, <code>workload_links</code>).</p>
    ${(c.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join("")}
    <h4>API Management APIs</h4>${apis.length ? apis.join("") : '<p class="muted">None suggested: no API shares a word with the workload\'s name or a path it serves.</p>'}
    <h4>App Gateway routes</h4>${routes.length ? routes.join("") : '<p class="muted">None found: no backend pool holds the cluster\'s load balancer or an address in its subnets (or the app\'s host names).</p>'}
    <h4>Its requests</h4>
    <label class="path-pick">${esc(c.tool || "Monitoring tool")} app <input list="path-apps" id="path-app" value="${esc(link.app || "")}" placeholder="${c.tool ? "app name" : "no tool picked in the Scope bar"}" ${c.tool ? "" : "disabled"}>
      <datalist id="path-apps">${appOptions.map((a) => `<option value="${esc(a)}">`).join("")}</datalist>
      ${c.apps.length ? `<span class="muted">suggested: ${esc(c.apps.slice(0, 3).map((a) => a.app).join(", "))}</span>` : ""}</label>
    <label class="path-pick">or App Insights component <select id="path-component"><option value="">none</option>${c.components.map((x) => `<option value="${esc(x.component)}" ${sameId(x.component, link.component) ? "selected" : ""}>${esc(x.name)} — ${esc(x.why.join("; "))}</option>`).join("")}</select></label>
    <div class="table-tools"><button type="button" class="primary" data-path-save>Save and read</button> <button type="button" data-path-cancel>Cancel</button></div>`;
  out.querySelector("[data-path-cancel]").addEventListener("click", () => { out.innerHTML = ""; });
  out.querySelector("[data-path-save]").addEventListener("click", async () => {
    const body = { ...params, apim: [...out.querySelectorAll("[data-path-apim]:checked")].map((x) => c.apim[x.dataset.pathApim]),
      agw: [...out.querySelectorAll("[data-path-agw]:checked")].map((x) => c.agw[x.dataset.pathAgw]),
      app: $("path-app").value.trim(), component: $("path-component").value };
    try { await api("api/path/link", { method: "POST", body }); } catch (error) { out.insertAdjacentHTML("beforeend", noticeHtml(error)); return; }
    loadPath(box, params);
  });
}

// The toolbar's namespace picker: namespaces with problems first, with their counts. System namespaces sit apart:
// picking one, or "All namespaces, including system", reads them in (`choice.reload`). Kept for this visit only.
const ALL_WITH_SYSTEM = "*system";
function wireNamespacePick(body, node, known, choice) {
  const pick = $("aks-ns");
  if (!pick) return;
  stored.set("ahc.aksNs", null);  // where 1.3 kept the pick across visits
  const counts = namespaceProblems(body), system = new Set(choice.system);
  const inTables = tabLists(body).flatMap((t) => {
    const col = namespaceColumn(t);
    return col < 0 ? [] : listGroups(t).map((rows) => (rows[0].cells[col] ? rows[0].cells[col].textContent.trim() : ""));
  });
  const all = [...new Set([...known, ...inTables].filter(Boolean))].sort();
  const own = all.filter((n) => !system.has(n)), theirs = all.filter((n) => system.has(n));
  const hot = all.filter((n) => counts[n]).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  const option = (n) => `<option value="${esc(n)}">${esc(n)}${counts[n] ? ` (${fmt(counts[n])})` : ""}</option>`;
  pick.innerHTML = `<option value="">All namespaces (${fmt(own.length)})</option>${theirs.length
    ? `<option value="${ALL_WITH_SYSTEM}">All namespaces, including system (${fmt(all.length)})</option>` : ""}${hot.length
    ? `<optgroup label="With problems">${hot.map(option).join("")}</optgroup>` : ""}${hot.length || theirs.length
    ? `<optgroup label="${hot.length ? "All" : "Namespaces"}">${own.map(option).join("")}</optgroup>` : own.map(option).join("")}${theirs.length
    ? `<optgroup label="System">${theirs.map(option).join("")}</optgroup>` : ""}`;
  pick.onchange = () => {  // a property, not a listener: this runs again when a later read adds rows
    state.aksNsPick = pick.value ? { id: node.id, ns: pick.value } : null;
    const wants = pick.value === ALL_WITH_SYSTEM || system.has(pick.value);
    if (wants !== choice.includeSystem) choice.reload(wants);
  };
  const kept = state.aksNsPick && state.aksNsPick.id === node.id ? state.aksNsPick.ns : "";
  if (kept && (kept === ALL_WITH_SYSTEM ? theirs.length : all.includes(kept))) { pick.value = kept; body.reapplyFilter(); }
}

// "Go to workload": the cluster's workloads matching what's typed (a name match first, then the picked namespace's), and
// the one picked opens its details under the toolbar; the Dependencies picker is set to it too.
function wireGoTo(body, node, list, tool) {
  const input = $("aks-go"), menu = $("aks-go-list"), out = $("aks-go-out");
  if (!input) return;
  let found = [], at = 0;
  const known = new Set(list.map((w) => `${w.namespace}/${w.kind}/${w.name}`));
  const add = (w) => { const key = `${w.namespace}/${w.kind}/${w.name}`; if (w.name && !known.has(key)) { known.add(key); list.push(w); } };
  const asked = new Set();
  const readNamespace = async (ns) => {  // no cluster-wide list: that namespace's own (API, Container Insights, the tool)
    if (!ns || asked.has(ns) || list.some((w) => w.namespace === ns)) return;
    asked.add(ns);
    const data = await api("api/explore/workload-names", { params: { id: node.id, namespace: ns, ...(tool ? toolParams(tool) : {}) } }).catch(() => ({}));
    (data.workloads || []).forEach((w) => add({ ...w, namespace: ns }));
    if (document.activeElement === input) show();
  };
  const nsPick = $("aks-ns");
  const pickedNs = () => nsPick.value.replace(ALL_WITH_SYSTEM, "");
  if (nsPick) { nsPick.addEventListener("change", () => readNamespace(pickedNs())); readNamespace(pickedNs()); }
  const hide = () => { menu.hidden = true; input.setAttribute("aria-expanded", "false"); };
  const mark = () => menu.querySelectorAll("[data-go]").forEach((b, i) => { b.classList.toggle("on", i === at); if (i === at) b.scrollIntoView({ block: "nearest" }); });
  const show = () => {
    const q = input.value.trim().toLowerCase(), ns = (($("aks-ns") || {}).value || "").replace(ALL_WITH_SYSTEM, "");
    if (!q) { hide(); return; }
    const rank = (w) => (w.name.toLowerCase() === q ? 0 : w.name.toLowerCase().startsWith(q) ? 2 : 4) - (w.namespace === ns ? 1 : 0);
    found = list.filter((w) => `${w.namespace}/${w.name}`.toLowerCase().includes(q))
      .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name) || a.namespace.localeCompare(b.namespace)).slice(0, 15);
    const typed = /^([a-z0-9][-a-z0-9]*)\/([a-z0-9][-a-z0-9.]*)$/.exec(q);
    if (typed && !found.some((w) => w.namespace === typed[1] && w.name === typed[2])) {
      found.push(...DEP_KINDS.map((kind) => ({ namespace: typed[1], name: typed[2], kind, typed: true })));
    }
    at = 0;
    menu.innerHTML = found.length ? found.map((w, i) => `<button type="button" role="option" data-go="${i}">${w.typed ? "Open as " : ""}<strong>${esc(w.typed ? w.kind : w.name)}</strong>
      <span class="muted">${esc(w.namespace)}${w.typed ? `/${esc(w.name)}` : ` · ${esc(w.kind)}`}</span></button>`).join("")
      : `<span class="muted">No workload matches${list.length ? "" : ": none could be listed, so type namespace/name"}.</span>`;
    menu.hidden = false;
    input.setAttribute("aria-expanded", "true");
    mark();
  };
  const go = (w) => {
    hide();
    input.value = `${w.namespace}/${w.name}`;
    out.innerHTML = `<div class="logs-panel"><div class="logs-panel-head"><strong></strong><span class="spacer"></span>
      <button type="button" class="link" data-go-close title="Close">✕ Close</button></div><div class="logs-panel-body"></div></div>`;
    openWorkload(out.querySelector(".logs-panel-body"), out.querySelector(".logs-panel-head strong"), node.id,
      { namespace: w.namespace, kind: w.kind, workload: w.name });
  };
  input.addEventListener("input", show);
  input.addEventListener("focus", () => { if (input.value.trim() && !out.innerHTML) show(); });
  input.addEventListener("blur", hide);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { hide(); return; }
    if (menu.hidden || !found.length) { if (event.key === "ArrowDown") show(); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      at = (at + (event.key === "ArrowDown" ? 1 : found.length - 1)) % found.length;
      mark();
    } else if (event.key === "Enter") { event.preventDefault(); go(found[at]); }
  });
  menu.addEventListener("mousedown", (event) => event.preventDefault());  // keeps the input focused until the click lands
  menu.addEventListener("click", (event) => { const b = event.target.closest("[data-go]"); if (b) go(found[Number(b.dataset.go)]); });
  out.addEventListener("click", (event) => { if (event.target.closest("[data-go-close]")) { out.innerHTML = ""; input.value = ""; } });
}

// One workload's Details, Dependencies and Path in one panel (Go to workload, or a workload's name on any line):
// Details first, the others read when picked.
const DEP_KINDS = ["Deployment", "StatefulSet", "DaemonSet"];
const WORKLOAD_VIEWS = [["detail", "Details"], ["deps", "Dependencies"], ["path", "Path"]];
function openWorkload(box, head, clusterId, target, view = "detail") {
  box.innerHTML = `<nav class="wl-views" aria-label="What to show">${WORKLOAD_VIEWS.map(([key, label]) =>
    `<button type="button" class="link${key === view ? " on" : ""}" data-wl-view="${key}" ${key === view ? 'aria-current="true"' : ""}>${label}</button>`).join(" · ")}</nav><div class="wl-view"></div>`;
  box.querySelector(".wl-views").addEventListener("click", (event) => {
    const pick = event.target.closest("[data-wl-view]");
    if (pick && pick.dataset.wlView !== view) openWorkload(box, head, clusterId, target, pick.dataset.wlView);
  });
  const inner = box.querySelector(".wl-view");
  head.innerHTML = `${esc(target.kind)} ${esc(target.namespace)}/${esc(target.workload)}`;
  const params = { namespace: target.namespace, kind: target.kind, workload: target.workload };
  if (view === "deps") loadDependencies(inner, { id: clusterId, ...params });
  else if (view === "path") loadPath(inner, { cluster: clusterId, ...params });
  else loadWorkloadDetail(inner, head, clusterId, target);
}

// ---------------------------------------------------------------- pod details (Workloads)
// "Change it in Settings": where a setting decides what the page concludes (a colour, a verdict, a window, which items
// count), its name is a link that opens Settings on its pane, scrolled to it and highlighted.
function settingLink(name, label = "") {
  return `<button type="button" class="link setting-link" data-open-setting="${esc(name)}" title="Open Settings at ${esc(SETTING_LABELS[name] || name)}">${esc(label || name)}</button>`;
}
async function openSetting(name) {
  showView("settings");
  const find = () => document.querySelector(`.set-row[data-name="${CSS.escape(name)}"]`);
  for (let i = 0; i < 50 && !find(); i++) await new Promise((r) => setTimeout(r, 100));  // Settings reads its file first
  const row = find();
  if (!row) return;
  showSettingsPane(PANE_OF[name.includes(".") ? name.split(".")[0] : "files"] || "general");
  row.scrollIntoView({ block: "center" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 2500);
}
document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-open-setting]");
  if (link) { event.preventDefault(); openSetting(link.dataset.openSetting); }
});
// Triage cards whose result a setting decides.
const CARD_SETTINGS = { autoscale: "triage.capacity_near_max_percent", service_health: "triage.resolved_hours",
  public_access: "security.risky_ports", subnets: "network.subnet_warning_percent", appinsights: "appinsights.failed_min" };

// What a check rests on (`rule` from the server): Kubernetes' own behaviour, a recommended practice, or the console's
// threshold, named with its setting so it can be changed; and the official page that explains it.
const RULE_BASIS = { kubernetes: "Kubernetes rule", practice: "Recommended practice", threshold: "Console's threshold" };
function ruleHtml(r) {
  if (!r) return "";
  const what = r.basis === "threshold" ? `${RULE_BASIS.threshold}: ${esc(r.value)} · ${settingLink(r.setting, "change")}` : RULE_BASIS[r.basis] || "";
  const doc = r.doc ? ` · <a href="${safeHref(r.doc)}" target="_blank" rel="noopener">docs</a>` : "";
  return `<div class="rule rule-${esc(r.basis)}">${what}${doc}</div>`;
}

// A pod's name opens what `kubectl describe pod` tells, under its row (`api/pod/detail`: the Kubernetes API, else
// Container Insights), and its real YAML.
function podButton(namespace, pod) {
  if (!namespace || !pod) return esc(pod || "");
  return `<button type="button" class="link" data-pod="${esc(JSON.stringify({ namespace, pod }))}" title="Open the pod: state, conditions, containers, events, YAML">${esc(pod)}</button>`;
}

function podDetailRow(button, clusterId, target) {
  const label = button.textContent;
  toggleLogsRow(button, `Pod ${esc(target.namespace)}/${esc(target.pod)}`, (box, head) => loadPodDetail(box, head, clusterId, target), [label, label]);
}

async function loadPodDetail(box, head, clusterId, target) {
  box.innerHTML = loading("Reading the pod, its events and its use…");
  const d = await api("api/pod/detail", { params: { cluster: clusterId, namespace: target.namespace, pod: target.pod } })
    .catch((error) => ({ available: false, reason: error.message }));
  if (!d.available) { box.innerHTML = noticeHtml(d.reason); return; }
  head.innerHTML = `Pod ${esc(target.namespace)}/${esc(target.pod)} <span class="muted">via ${esc(d.source)}${d.seen_at ? `, ${seen(d.seen_at)}` : ""}</span>`;
  box.innerHTML = podDetailHtml(d, clusterId);
}

const CONTAINER_STATE = { running: "healthy", waiting: "warning", terminated: "unknown" };
function podDetailHtml(d, clusterId) {
  const s = d.summary;
  const where = [s.node && `node ${esc(s.node)}${s.pool ? ` <span class="muted">(${esc(s.pool)})</span>` : ""}`, s.zone && `zone ${esc(s.zone)}`,
    s.pod_ip && `IP ${esc(s.pod_ip)}`, s.qos && `QoS ${esc(s.qos)}`, s.started && `started ${seen(s.started)}`].filter(Boolean).join(" · ");
  const owner = s.owner && !s.owner.startsWith("Pod/") ? ` · owned by ${workloadButton(s.namespace, s.owner)}` : "";
  const gaps = d.missing.length ? noticeHtml(`The Kubernetes API couldn't be read (${d.why_not_api || "no reason given"}), so this comes from Container Insights: ${d.missing.join(", ")} aren't available from it.`) : "";
  const conditions = d.conditions.length ? `<h5>Conditions</h5>${table(["Condition", "Status", "Reason", "Since"], d.conditions.map((c) => [
    esc(c.type), c.status === "True" ? pillHtml("healthy", "True") : pillHtml("warning", c.status || "?"),
    `${esc(c.reason || "–")}${c.message ? `<div class="muted ai-msg">${esc(c.message)}</div>` : ""}`, c.at ? seen(c.at) : "–"]), "pod-conditions")}` : "";
  const exit = (e) => (e ? `${esc(e.reason || "?")}${e.code != null ? ` <span class="muted">(exit ${esc(e.code)})</span>` : ""}${e.at ? `<div class="muted">${seen(e.at)}</div>` : ""}` : "–");
  const probes = (c) => Object.entries(c.probes || {}).filter(([, p]) => p).map(([k, p]) => `${k}: ${esc(p.how)}${p.periodSeconds ? ` every ${p.periodSeconds}s` : ""}`).join("<br>") || '<span class="muted">none</span>';
  const res = (c) => (c.memory ? `CPU ${cpuText(c.cpu.request)} / ${cpuText(c.cpu.limit)}${c.cpu.used != null ? ` <span class="muted">use ${cpuText(c.cpu.used)}</span>` : ""}<br>
    Mem ${memText(c.memory.request)} / ${memText(c.memory.limit)}${c.memory.used != null ? ` <span class="muted">use ${memText(c.memory.used)}</span>` : ""}` : '<span class="muted">–</span>');
  const containers = `<h5>Containers</h5>${table(["Container", "State now", "Ready", "Restarts", "Last exit", "Request / limit", "Probes", ""], d.containers.map((c) => [
    `<strong>${esc(c.name)}</strong>${c.role !== "app" ? ` <span class="muted">${esc(c.role)}</span>` : ""}${c.image ? `<div class="muted ai-msg">${esc(c.image)}</div>` : ""}`,
    `${pillHtml(CONTAINER_STATE[c.state] || "unknown", c.state || "?")} ${esc(c.reason)}${c.detail ? `<div class="muted ai-msg">${esc(c.detail)}</div>` : ""}`,
    c.ready == null ? "–" : c.ready ? "yes" : pillHtml("warning", "no"), fmt(c.restarts || 0), exit(c.last_exit), res(c), d.missing.includes("probes") ? "–" : probes(c),
    `${podLogsButton(s.namespace, s.name, c.name)}${c.last_exit ? ` · <button type="button" class="link" data-logs="${esc(JSON.stringify({ namespace: s.namespace, pod: s.name, container: c.name, previous: "1" }))}" title="The logs of the instance that stopped last">previous</button>` : ""}`]), "pod-containers")}`;
  const events = `<h5>Events</h5>${d.events.length ? table(["When", "Type", "Reason", "Count", "Message"], d.events.map((e) => [
    e.at ? seen(e.at) : "–", e.type === "Warning" ? pillHtml("warning", "Warning") : esc(e.type || ""), esc(e.reason || ""), fmt(e.count), `<span class="ai-msg">${esc(e.message)}</span>`]), "pod-events")
    : '<p class="muted">No events about this pod: Kubernetes keeps them about an hour.</p>'}`;
  const sc = d.scheduling;
  const scheduling = sc ? `<h5>Scheduling and identity</h5><p>${[`service account ${esc(sc.service_account)}`,
    Object.keys(sc.node_selector).length ? `node selector ${esc(Object.entries(sc.node_selector).map(([k, v]) => `${k}=${v}`).join(", "))}` : "",
    sc.tolerations.length ? `tolerations ${esc(sc.tolerations.join("; "))}` : "", sc.affinity.length ? `affinity: ${esc(sc.affinity.join(", "))}` : "",
    sc.spread ? `${fmt(sc.spread)} topology spread rule(s)` : "", sc.priority_class ? `priority ${esc(sc.priority_class)}` : ""].filter(Boolean).join(" · ")}</p>` : "";
  const volumes = d.volumes.length ? `<h5>Volumes</h5><p>${d.volumes.map((v) => `${esc(v.name)} <span class="muted">${esc(v.kind)}${v.ref ? ` ${esc(v.ref)}` : ""}</span>`).join(" · ")}</p>
    <p class="muted">Names and types only: a Secret's or ConfigMap's content is never read.</p>` : "";
  return `${gaps}<p><strong>${esc(s.phase || "?")}</strong>${s.reason ? ` · ${esc(s.reason)}` : ""}${s.deleting ? ` · ${pillHtml("warning", "terminating")}` : ""}${owner}
    ${s.message ? `<div class="muted ai-msg">${esc(s.message)}</div>` : ""}<div class="muted">${where}</div></p>
    ${conditions}${containers}${events}${scheduling}${volumes}${yamlFoldHtml(d.yaml, clusterId)}`;
}

// The real object as `kubectl get -o yaml` shows it, folded. Env values come hidden; Show values reads it again with
// them (`api/kube/yaml`, values=1), for this panel only.
function yamlFoldHtml(y, clusterId) {
  if (!y) return "";
  const target = esc(JSON.stringify({ cluster: clusterId, namespace: y.namespace, kind: y.kind, name: y.name }));
  return `<details class="yaml-fold" data-yaml="${target}"><summary>YAML: ${esc(y.kind)} ${esc(y.name)}</summary>
    <div class="yaml-tools">${y.hides ? '<label class="inline"><input type="checkbox" data-yaml-values> Show env values</label> · ' : ""}<button type="button" class="link" data-yaml-copy>Copy</button>
      · <code class="muted">${esc(y.command)}</code></div>
    <p class="muted yaml-note">${y.hides ? "Env values are hidden (‹hidden›); references to Secrets and ConfigMaps show their names." : "As the API returned it, less managedFields."}</p>
    <pre class="yaml">${esc(y.yaml)}</pre></details>`;
}

document.addEventListener("change", async (event) => {
  const box = event.target.closest("[data-yaml-values]");
  if (!box) return;
  const fold = box.closest("[data-yaml]"), pre = fold.querySelector("pre.yaml"), note = fold.querySelector(".yaml-note");
  if (pre.masked == null) pre.masked = pre.textContent;
  if (!box.checked) { pre.textContent = pre.masked; note.textContent = "Env values are hidden (‹hidden›); references to Secrets and ConfigMaps show their names."; return; }
  pre.textContent = "Reading it again with its values…";
  const y = await api("api/kube/yaml", { params: { ...JSON.parse(fold.dataset.yaml), values: "1" } }).catch((error) => ({ available: false, reason: error.message }));
  if (!box.checked) return;
  if (!y.available) { box.checked = false; pre.textContent = pre.masked; note.textContent = y.reason; return; }
  pre.textContent = y.yaml;
  note.innerHTML = `${pillHtml("warning", "values shown")} Anyone who can see this screen sees them: mind screen sharing. Unticking hides them again.`;
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-yaml-copy]");
  if (button) copyText(button.closest("[data-yaml]").querySelector("pre.yaml").textContent, button);
});

// ---------------------------------------------------------------- what's wrong now (Workloads)
// One row per workload over every source (`problems`, built by the server), worst first; the per-source sections
// below stay as the evidence, closed. New Relic's OOM kills join it when they arrive (`mergeMonitorOom`).
const SOURCE_SHORT = { "Kubernetes API": "K8s", "Container Insights": "CI", "platform metrics": "metrics" };
const sourceBadges = (sources) => sources.map((s) => `<span class="src-badge" title="${esc(s)}">${esc(SOURCE_SHORT[s] || s)}</span>`).join(" ");
const SEVERITY_RANK = { HIGH: 0, MEDIUM: 1 };
const EVIDENCE_TYPES = new Set(["not_running", "oom", "restarts", "events", "near_limit"]);  // lines with pods or events behind them

function rolloutHtml(c) {
  if (!c) return '<span class="muted">–</span>';
  const images = (c.images || []).map((i) => `${i.container}: ${i.from} → ${i.to}`).join("\n");
  const tip = images || (c.at ? "A new ReplicaSet with the same images (an env, config or annotation change)" : "Moving to a new revision");
  return `<span title="${esc(tip)}">${c.at ? seen(c.at) : ""}${c.revision ? ` <span class="muted">rev ${fmt(c.revision)}</span>` : ""}${
    c.rolling ? ` ${pillHtml("soon", "rolling out")}` : ""}${(c.images || []).length
    ? `<div class="muted ai-msg">${esc(c.images.map((i) => i.to.split("/").pop()).join(", "))}</div>` : ""}</span>`;
}

function problemWorkloadHtml(g) {
  if (!g.workload) return g.namespace ? '<span class="muted">pods counted per namespace only</span>' : "<strong>Cluster and nodes</strong>";
  return g.workload.startsWith("Pod/") ? `${esc(g.workload)} <span class="muted">owner not known</span>` : workloadButton(g.namespace, g.workload);
}

function problemsTable(groups) {
  return table(["Severity", "Namespace", "Workload", "Ready", "What's wrong", "Seen by", "Rolled out", "Pods"], groups.map((g) => [
    pill(SEVERITY_STATUS[g.severity]),
    esc(g.namespace),  // empty for the cluster's own row, so the namespace picker doesn't list it
    problemWorkloadHtml(g),
    g.desired != null ? `<span data-v="${g.desired ? g.ready / g.desired : 1}">${fmt(g.ready || 0)}/${fmt(g.desired)}</span>` : '<span class="muted">–</span>',
    g.signals.map((s) => `<div class="signal sig-${s.severity === "HIGH" ? "critical" : "warning"}" title="${esc(`Seen by ${s.sources.join(", ")}${s.action ? `\n${s.action}` : ""}`)}">${
      esc(s.text)}${s.detail ? ` <span class="muted">· ${esc(s.detail)}</span>` : ""}${EVIDENCE_TYPES.has(s.type) && g.workload
        ? ` <button type="button" class="link sig-more" data-evidence="${esc(g.workload.slice(g.workload.indexOf("/") + 1))}" title="Its pods and events, under Pods & events">details ›</button>` : ""}${ruleHtml(s.rule)}</div>`).join(""),
    sourceBadges(g.sources),
    rolloutHtml(g.changed),
    g.pods.length ? `<details class="pod-list"><summary>${fmt(g.pods.length)} pod${g.pods.length === 1 ? "" : "s"}</summary>${
      g.pods.map((p) => `<div>${podButton(g.namespace, p)} ${podLogsButton(g.namespace, p)}</div>`).join("")}</details>` : '<span class="muted">–</span>']), "aks-problems");
}

function verdictHtml(v, timings, count) {
  const head = v.only_counts
    ? `${pill("unknown")} <strong>Only pod counts could be read, so workloads can't be checked</strong>${count
      ? ` <span class="muted">· pods not running in ${fmt(count)} namespace${count === 1 ? "" : "s"}, below</span>` : ""}`
    : count ? `${pill(v.critical ? "critical" : "warning")} <strong>${fmt(count)} ${count === 1 ? "workload has" : "workloads have"} problems</strong>${
      v.critical ? ` <span class="muted">(${fmt(v.critical)} critical)</span>` : ""}`
      : v.cluster.length ? `${pill("warning")} <strong>No workload has problems, but the cluster does</strong>`
        : '<span class="ok">✓ <strong>Nothing wrong with any workload right now</strong></span>';
  const more = [
    v.cluster.length ? `cluster: ${esc(v.cluster.join("; "))}` : "",
    v.leftover ? `${fmt(v.leftover)} Failed pod${v.leftover === 1 ? "" : "s"} over a day old, kept apart under <em>Pods &amp; events</em>` : "",
    v.rolled_out.length ? `rolled out in the last 24 h: ${esc(v.rolled_out.join(", "))}` : "",
    v.rolling.length ? `rolling out now: ${esc(v.rolling.join(", "))}` : ""].filter(Boolean);
  const took = Object.entries(timings || {}).map(([name, s]) => `${esc(name)} ${s} s`).join(" · ");
  const missing = v.missing.map((m) => `<li><strong>${esc(m.source)}</strong> not read${m.reason ? `: ${esc(m.reason)}` : ""}</li>`).join("");
  return `<p class="verdict">${head}${more.length ? `<br>${more.join(" · ")}` : ""}</p>
    ${missing ? `<ul class="verdict-missing">${missing}</ul>` : ""}
    <p class="muted">From ${esc(v.read.join(", ") || "nothing")}${took ? ` · read in ${took}` : ""}</p>`;
}

// New Relic's kills that Container Insights didn't already list, added to their workload's row (or a new one).
function mergeMonitorOom(problems, rows, label, byWorkload = null) {
  const byKey = new Map(problems.groups.map((g) => [`${g.namespace}|${g.workload}`, g]));
  const kills = new Map();
  // Every kill the tool counted (`byWorkload`), not only the rows listed; the listed rows when a tool gives no counts.
  for (const r of byWorkload || rows) {
    const workload = r.workload || (r.pod ? `Pod/${r.pod}` : "");
    const key = `${r.namespace}|${workload}`;
    const k = kills.get(key) || { n: 0, pods: new Set(), namespace: r.namespace, workload };
    k.n += byWorkload ? r.kills : 1;
    for (const pod of byWorkload ? r.pods : [r.pod]) if (pod) k.pods.add(pod);
    kills.set(key, k);
  }
  const severity = (problems.severity || {}).oom || "HIGH";
  for (const [key, k] of kills) {
    let g = byKey.get(key);
    if (!g) {
      g = { namespace: k.namespace, workload: k.workload, signals: [], pods: [], sources: [], severity, ready: null, desired: null, changed: null };
      problems.groups.push(g);
      byKey.set(key, g);
    }
    g.signals.push({ type: "oom", severity, text: `${fmt(k.n)} OOM kill(s)`, sources: [label], detail: "", action: "" });
    g.pods = [...new Set([...g.pods, ...k.pods])].sort();
    if (!g.sources.includes(label)) g.sources.push(label);
    if (SEVERITY_RANK[severity] < SEVERITY_RANK[g.severity]) g.severity = severity;
  }
  problems.groups.sort((a, b) => (a.namespace || a.workload ? 1 : 0) - (b.namespace || b.workload ? 1 : 0)
    || SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] || b.signals.length - a.signals.length);
  return kills.size;
}

// A tool's warning events in What's wrong now: one line per workload (a pod's own group when another source named it,
// else its Deployment from the pod's name), node events per cluster, like the server does for its own sources.
function mergeMonitorEvents(problems, rows, label, hours) {
  const byKey = new Map(problems.groups.map((g) => [`${g.namespace}|${g.workload}`, g]));
  const ofPod = (ns, pod) => problems.groups.find((g) => g.namespace === ns && g.pods.includes(pod))?.workload
    || (/^(.+)-[a-z0-9]{6,10}-[a-z0-9]{5}$/.test(pod) ? `Deployment/${pod.replace(/-[a-z0-9]{6,10}-[a-z0-9]{5}$/, "")}` : `Pod/${pod}`);
  const merged = new Map();
  for (const r of rows) {
    const kind = r.ObjectKind || "Pod";
    const [ns, workload, pod] = kind === "Node" ? ["", "", ""] : kind === "Pod" ? [r.Namespace, ofPod(r.Namespace, r.Object), r.Object]
      : [r.Namespace, `${kind}/${r.Object}`, ""];
    const m = merged.get(`${ns}|${workload}`) || { ns, workload, reasons: new Map(), pods: new Set() };
    m.reasons.set(r.Reason, (m.reasons.get(r.Reason) || 0) + (r.Count || 1));
    if (pod) m.pods.add(pod);
    merged.set(`${ns}|${workload}`, m);
  }
  const severity = (problems.severity || {}).events || "MEDIUM";
  for (const m of merged.values()) {
    let g = byKey.get(`${m.ns}|${m.workload}`);
    if (!g) {
      g = { namespace: m.ns, workload: m.workload, signals: [], pods: [], sources: [], severity, ready: null, desired: null, changed: null };
      problems.groups.push(g);
      byKey.set(`${m.ns}|${m.workload}`, g);
    }
    const text = `warning events, last ${ciSpan(hours)}: ${[...m.reasons].sort((a, b) => b[1] - a[1]).map(([reason, n]) => `${reason} ×${fmt(n)}`).join(", ")}`;
    g.signals.push({ type: "events", severity, text, sources: [label], detail: "", action: "" });
    g.pods = [...new Set([...g.pods, ...m.pods])].sort();
    if (!g.sources.includes(label)) g.sources.push(label);
    if (SEVERITY_RANK[severity] < SEVERITY_RANK[g.severity]) g.severity = severity;
  }
  problems.groups.sort((a, b) => (a.namespace || a.workload ? 1 : 0) - (b.namespace || b.workload ? 1 : 0)
    || SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] || b.signals.length - a.signals.length);
  return merged.size;
}

function renderProblems(body, problems, timings) {
  const box = body.querySelector("#aks-problems");
  if (!box) return;
  const count = problems.groups.filter((g) => g.namespace || g.workload).length;
  box.querySelector(".aks-verdict").innerHTML = verdictHtml(problems.verdict, timings, count);
  box.querySelector(".aks-problem-list").innerHTML = problems.groups.length
    ? aksPart("problems", "One row per workload", problems.groups.length, true, problemsTable(problems.groups), "What's wrong now") : "";
}

// The monitoring tool's OOM kills for this cluster, less the ones Container Insights already listed (same container,
// killed within 3 minutes). `extra` is every kill the tool counted that isn't one of those, listed or not.
async function loadOomMonitor(node, kind, ciRows) {
  const params = { id: node.id, ...(state.aksCiHours ? { ci_hours: state.aksCiHours } : {}), ...toolParams(kind) };
  const data = await api("api/explore/oom-kills-monitor", { params }).catch((error) => ({ available: false, reason: error.message }));
  const box = $("oom-monitor");
  if (!box) return;
  if (!data.available) { box.innerHTML = noticeHtml([data.reason, data.fix].filter(Boolean).join(" ")); return null; }
  const same = (a, b) => a.pod && a.pod === b.pod && a.container === b.container
    && Math.abs(new Date(a.killed_at) - new Date(b.killed_at)) < 180000;
  const rows = data.rows.filter((r) => !ciRows.some((c) => same(r, c)));
  const shared = data.rows.length - rows.length;
  const total = data.total ?? data.rows.length;
  const unlisted = total - data.rows.length;
  const sent = `<details class="mon-query"><summary>Queries sent</summary>${queryBlock({ label: data.label, queries: data.queries })}</details>`;
  box.innerHTML = `${(data.notes || []).map((n) => noticeHtml(n)).join("")}
    ${total ? `<p><strong>${fmt(total)} OOM kill${total === 1 ? "" : "s"}</strong> <span class="muted">in ${esc(ciSpan(data.hours))}${unlisted > 0
      ? ` · the newest ${fmt(data.rows.length)} are listed; every one is counted in What's wrong now` : ""}${(data.detailed ?? total) < data.rows.length
      ? ` · memory read for the newest ${fmt(data.detailed)} (the kernel's message says the cause for the rest)` : ""}</span></p>` : ""}
    ${rows.length ? oomTable(rows, podLogsButton, "monitor-oom", podButton) : data.found ? '<p class="muted">None.</p>' : ""}
    ${shared ? `<p class="muted">${shared} more ${shared === 1 ? "is" : "are"} listed above, from Container Insights.</p>` : ""}${sent}`;
  return { rows, extra: total - shared, byWorkload: data.by_workload || null, label: data.label || toolLabel(kind) };
}

// The monitoring tool's warning events (any tool with the `warning events` capability), less those already listed.
async function loadEventsMonitor(node, kind, shownRows) {
  const params = { id: node.id, ...(state.aksCiHours ? { ci_hours: state.aksCiHours } : {}), ...toolParams(kind) };
  const data = await api("api/explore/warning-events-monitor", { params }).catch((error) => ({ available: false, reason: error.message }));
  const box = $("events-monitor");
  if (!box) return null;
  if (!data.available) { box.innerHTML = noticeHtml([data.reason, data.fix].filter(Boolean).join(" ")); return null; }
  const shown = new Set(shownRows.map(eventKey));
  const rows = data.rows.filter((r) => !shown.has(eventKey(r)));
  const shared = data.rows.length - rows.length;
  const total = data.total ?? data.rows.length;
  const sent = `<details class="mon-query"><summary>Queries sent</summary>${queryBlock({ label: data.label, queries: data.queries })}</details>`;
  box.innerHTML = `${(data.notes || []).map((n) => noticeHtml(n)).join("")}
    ${total > data.rows.length ? `<p class="muted">The ${fmt(data.rows.length)} seen most often of ${fmt(total)}, in ${esc(ciSpan(data.hours))}.</p>` : ""}
    ${rows.length ? warningEventsTable(rows, "monitor-events") : data.found ? '<p class="muted">None.</p>' : ""}
    ${shared ? `<p class="muted">${shared} more ${shared === 1 ? "is" : "are"} listed above.</p>` : ""}${sent}`;
  return { rows, extra: total - shared, hours: data.hours, label: data.label || toolLabel(kind) };
}

// One warning event per reason and object, whichever source: Container Insights, the Kubernetes API or a tool.
const eventKey = (r) => [r.Reason, r.Namespace, r.Object, r.ObjectKind || "Pod"].join("|");
function warningEventsTable(rows, key) {
  return table(["Reason", "Namespace", "Object", "Count", "Last seen", "Message"], rows.map((r) => [
    esc(r.Reason), esc(r.Namespace), (r.ObjectKind || "Pod") === "Pod" ? podButton(r.Namespace, r.Object)
      : `${esc(r.Object)} <span class="muted">${esc(r.ObjectKind)}</span>`,
    fmt(r.Count), r.Last ? invTime(r.Last) : "–", esc((r.Message || "").slice(0, 160))]), key);
}

// A section's count once a later read adds to it: the number, its warning pill, and open unless the user closed it.
function setPartCount(body, key, n) {
  const part = body.querySelector(`details[data-part="${key}"]`);
  if (!part) return;
  const summary = part.querySelector(":scope > summary");
  part.dataset.count = n;
  part.dataset.problem = n ? 1 : 0;
  part.classList.toggle("empty", !n);
  summary.querySelector(":scope > .muted").textContent = n ? fmt(n) : "none";
  if (n && !summary.querySelector(":scope > .pill")) summary.querySelector(":scope > .muted").insertAdjacentHTML("afterend", ` ${pill("warning")}`);
  if (n && stored.json("ahc.aksParts", {})[key] === undefined) part.open = true;
}

// One row per OOM kill: whose it was, where it ran, and whether its own limit or the node ran out (`oom_rows`).
const OOM_CAUSE = { container: ["warning", "Container limit"], node: ["critical", "Node memory"], unknown: ["unknown", "Can't tell"] };
function oomCauseHtml(r) {
  const [tone, label] = OOM_CAUSE[r.cause] || OOM_CAUSE.unknown;
  return `${pillHtml(tone, label, { title: r.why })}${r.sure || r.cause === "unknown" ? ""
    : ` <span class="muted">probable · ${settingLink(r.cause === "node" ? "aks.oom_node_full_percent" : "aks.oom_near_limit_percent", "threshold")}</span>`}
    <div class="muted">${esc(r.why)}</div>${r.kernel ? `<details><summary>Kernel's message</summary><code class="ai-msg">${esc(r.kernel)}</code></details>` : ""}`;
}
function oomMemoryHtml(r) {
  const used = r.memory_mib != null ? `${fmt(r.memory_mib)} MiB` : "–";
  const limit = r.no_limit ? "no limit" : r.limit_mib != null ? `of ${fmt(r.limit_mib)} MiB` : "";
  return `${used} ${esc(limit)}${r.node_pct != null ? `<div class="muted">node ${r.node_pct}%</div>` : ""}`;
}
function oomTable(rows, logs = podLogsButton, key = "ci-oom", name = (ns, pod) => esc(pod)) {
  return table(["Killed", "Namespace", "Pod", "Container", "Node", "Cause", "Memory", ""], rows.map((r) => [
    invTime(r.killed_at), esc(r.namespace), r.pod ? `${name(r.namespace, r.pod)}${r.owner ? `<div class="muted">${esc(r.owner)}</div>` : ""}`
      : '<span class="muted" title="Usually a child process, not the container\'s main one, so the container kept running">No container restarted on this node within 3 minutes</span>',
    esc(r.container), esc(r.node), oomCauseHtml(r), oomMemoryHtml(r), r.pod ? logs(r.namespace, r.pod, r.container, r.killed_at) : ""]), key);
}

// The lists a Kubernetes read couldn't get, and what that leaves out, so a gap never reads as "healthy".
