"use strict";

// ------------------------------------------------------------------ monitoring tools
// One tab, Triage card and resource tab per tool (New Relic, Datadog, Grafana), each showing the one connection
// picked in the Scope bar. Drawn by the same code for every tool: what differs comes from the provider.

const MON_SEVERITY_TONE = { critical: "critical", warning: "warning", ok: "healthy", unknown: "unknown" };
const MON_SEVERITY_LABEL = { critical: "Critical", warning: "Warning", ok: "Not alerting", unknown: "No alerts set" };
const MON_PRIORITY_RANK = { critical: 0, warning: 1, info: 2 };
const MON_CONFIDENCE = { ...SN_CONFIDENCE, exact: "exact match (Azure ID in a tag)", picked: "picked by name" };

const monPill = (severity) => pillHtml(MON_SEVERITY_TONE[severity] || "unknown", MON_SEVERITY_LABEL[severity] || severity);
const issueTone = (i) => (i.priority === "critical" ? "critical" : i.priority === "warning" ? "warning" : "info");
const issuePill = (i) => pillHtml(issueTone(i), i.priority_label || "Issue");
const monLink = (url, text) => (url ? `<a href="${safeHref(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text));
// One line per source when several entities share a name (e.g. the Azure and Kubernetes views of one cluster).
const entityType = (e) => ((e.sources || []).length > 1
  ? e.sources.map((s) => `<div class="entity-source">${monLink(s.url, s.type || s.type_class || "entity")} ${monPill(s.severity)}${
    s.reporting ? "" : ' <span class="muted">(not reporting)</span>'}</div>`).join("")
  : `${esc(e.type)}${e.type_class && e.type_class !== e.type ? `<div class="muted">${esc(e.type_class)}</div>` : ""}`);
const connTitle = (c) => [c.name, c.description].filter(Boolean).join(" · ");
const toolLabel = (kind) => MONITOR_LABELS[kind] || kind;
const toolKinds = () => Object.keys(state.connections || {}).filter((k) => k !== "servicenow");
const sortEntities = (rows) => [...rows].sort((a, b) => (({ critical: 0, warning: 1, ok: 2 }[a.severity] ?? 3)
  - ({ critical: 0, warning: 1, ok: 2 }[b.severity] ?? 3)) || a.name.localeCompare(b.name));
const sortIssues = (rows) => [...rows].sort((a, b) => (MON_PRIORITY_RANK[a.priority] ?? 3) - (MON_PRIORITY_RANK[b.priority] ?? 3)
  || String(b.activated_at).localeCompare(String(a.activated_at)));

function toolParams(kind, extra = {}) {
  return { ...extra, connection: (state.selected || {})[kind] || "" };
}

async function loadMonitors(refresh = false) {
  const kind = state.toolKind;
  const scope = currentScope();
  const token = (state.monToken = (state.monToken || 0) + 1);
  state.monLoaded = `${kind}|${state.selected[kind]}`;
  $("mon-title").textContent = toolLabel(kind);
  if (!scope.subscriptions.length) {
    $("mon-body").innerHTML = pickScopeHtml(`${toolLabel(kind)} shows what it reports for the resources in scope.`);
    return;
  }
  $("mon-meta").textContent = "";
  $("mon-body").innerHTML = loading(`Reading ${state.selected[kind] || toolLabel(kind)} and matching what it reports to Azure resources…`);
  try {
    const params = toolParams(kind, scopeParams(scope));
    if (refresh) params.refresh = "1";
    const data = await api("api/monitors", { params });
    if (token !== state.monToken) return;
    state.mon = data;
    renderMonitors();
  } catch (error) {
    if (token === state.monToken && !error.signedOut) $("mon-body").innerHTML = noticeHtml(error);
  }
}

function monVisible(rows, fields) {
  const text = $("mon-filter-text").value.trim().toLowerCase();
  const scopeOnly = $("mon-scope-only").checked;
  return rows.filter((r) => (!scopeOnly || namesResource(r))
    && (!text || [...fields(r), ...r.matches.map((m) => m.name)].some((v) => (v || "").toLowerCase().includes(text))));
}

function renderMonitors() {
  const c = state.mon;
  const kind = state.toolKind;
  renderQueryBar();
  if (!c) return;
  if (!c.available) {
    $("mon-meta").innerHTML = c.name ? esc(connTitle(c)) + toolLink(c.console, c.label) : "";
    $("mon-body").innerHTML = snUnavailable(c);
    return;
  }
  const criticalOnly = $("mon-severity").value === "critical";
  const scopeOnly = $("mon-scope-only").checked;
  $("mon-meta").innerHTML = `${esc(`${connTitle(c)} · read ${ago(c.fetched_at)}`)}${toolLink(c.console, c.label)}`;
  const open = c.issues.filter(namesResource).length + c.entities.filter(namesResource).length;
  $("mon-alert-count").textContent = String(open);
  $("mon-alert-count").classList.toggle("hidden", !open);
  const allIssues = sortIssues(c.issues.filter((i) => !criticalOnly || i.priority === "critical"));
  const allEntities = sortEntities(c.entities.filter((e) => !criticalOnly || e.severity === "critical"));
  const issues = monVisible(allIssues, (i) => [i.title, i.condition, i.policy, ...i.entities]);
  const entities = monVisible(allEntities, (e) => [e.name, e.type, e.account]);
  const hasLogs = (c.capabilities || []).includes("logs");
  const hidden = (all, shown, one, many) => {
    const n = all.length - shown.length;
    return scopeOnly && n > 0 ? `<p class="muted">${n} other ${n === 1 ? one : many} ${n === 1 ? "doesn't" : "don't"} match any Azure
      resource in scope. <button type="button" class="link" data-show-all>Show them</button></p>` : "";
  };
  $("mon-body").innerHTML = `
    ${noticesHtml(c.notes)}
    ${c.truncated ? `<p class="notice">Only part of what's alerting was read (the first entities and open issues ${esc(c.label)} returned).</p>` : ""}
    <h3>Open issues <span class="muted">(${issues.length})</span></h3>${hidden(allIssues, issues, "open issue", "open issues")}
    ${issues.length ? `<table class="data-table" data-list="mon-issues"><thead><tr><th>Priority</th><th>Issue</th><th>Condition</th><th>Entities</th>
      <th>Activated</th><th>Azure resources it matches</th>${hasLogs ? "<th></th>" : ""}</tr></thead><tbody>${issues.map((i) => `<tr>
      <td>${issuePill(i)}</td><td>${monLink(i.url, i.title || i.id)}<div>${(c.capabilities || []).includes("alert history")
        ? alertCheckButton({ source: "monitor", ...toolParams(kind), account: i.account_id ?? "", condition: i.condition || "", policy: i.policy || "", priority: i.priority_label || "" }) : ""}</div></td>
      <td>${esc(i.condition)}${i.policy ? `<div class="muted">${esc(i.policy)}</div>` : ""}</td>
      <td>${esc(i.entities.slice(0, 3).join(", "))}${i.entities.length > 3 ? ` <span class="muted">+${i.entities.length - 3}</span>` : ""}</td>
      <td>${agoHtml(i.activated_at)}</td><td>${resourceChips(i.matches)}</td>
      ${hasLogs ? `<td>${logsButton(i.entity_ids || [], i.account_id, i.entities.join(", ") || i.title, true, "", i.matches)}${queryLink(i.matches)}</td>` : ""}</tr>`).join("")}</tbody></table>`
      : `<p class="ok">✓ No open issues${scopeOnly ? " match an Azure resource in scope" : " match these filters"}.</p>`}
    <h3>Alerting entities <span class="muted">(${entities.length})</span></h3>${hidden(allEntities, entities, "alerting entity", "alerting entities")}
    ${entities.length ? `<table class="data-table" data-list="mon-entities"><thead><tr><th>Severity</th><th>Entity</th><th>Type</th><th>Account</th>
      <th>Azure resources it matches</th>${hasLogs ? "<th></th>" : ""}</tr></thead><tbody>${entities.map((e) => `<tr>
      <td>${monPill(e.severity)}</td><td>${monLink(e.url, e.name)}${e.reporting ? "" : ' <span class="muted">(not reporting)</span>'}</td>
      <td>${entityType(e)}</td><td>${esc(e.account)}</td><td>${resourceChips(e.matches)}</td>
      ${hasLogs ? `<td>${logsButton(e.ids || [e.id], e.account_id, e.name, true, e.name, e.matches)}${queryLink(e.matches)}</td>` : ""}</tr>`).join("")}</tbody></table>`
      : `<p class="ok">✓ Nothing alerting${scopeOnly ? " matches an Azure resource in scope" : " matches these filters"}.</p>`}
    <p class="muted">Matching: a tag whose value is an Azure resource ID is an exact match; an entity named like a resource, or a
      distinctive resource name in its name, is a name match; short or common names inside a longer name are only "possible".</p>`;
}

// The first Azure resource a row matches, opened in Query (errors, slow transactions, or logs by name).
function queryLink(matches) {
  const m = (matches || []).find((x) => x.id && x.display_type !== "Resource group");
  return m ? ` <button type="button" class="link" data-mon-query="${esc(JSON.stringify({ id: m.id, name: m.name }))}" title="Open ${esc(m.name)} in Query">Query</button>` : "";
}

$("mon-body").addEventListener("click", (event) => {
  const logs = event.target.closest("[data-logs]");
  if (logs) { toggleRowLogs(logs); return; }
  const query = event.target.closest("[data-mon-query]");
  if (query) {
    showMonSub("query");
    pickQueryResource(JSON.parse(query.dataset.monQuery));
    return;
  }
  if (event.target.dataset && event.target.dataset.showAll !== undefined) {
    $("mon-scope-only").checked = false;
    rememberMonScopeOnly();
    renderMonitors();
    return;
  }
  const chip = event.target.closest("[data-open]");
  if (chip) { startSnapshot(JSON.parse(chip.dataset.open)); return; }
  if (event.target.dataset && event.target.dataset.goto) showView(event.target.dataset.goto);
});
$("mon-refresh").addEventListener("click", () => loadMonitors(true));
for (const id of ["mon-filter-text", "mon-severity", "mon-scope-only"]) $(id).addEventListener("input", debounce(renderMonitors, 150));

function rememberMonScopeOnly() {
  stored.set("ahc.monScopeOnly", $("mon-scope-only").checked ? "1" : "0");
}
$("mon-scope-only").addEventListener("change", rememberMonScopeOnly);
$("mon-scope-only").checked = stored.get("ahc.monScopeOnly") !== "0";

// Browse → a resource → <tool>: what the selected connection says about it, then its logs on request.
async function tabTool(node, body, kind) {
  const c = await exploreGet("monitors", node, toolParams(kind));
  if (!c.available) { body.innerHTML = snUnavailable(c); return; }
  const how = (r) => { const m = r.matches.find((x) => x.id.toLowerCase() === node.id.toLowerCase()) || r.matches[0]; return esc(m ? MON_CONFIDENCE[m.confidence] : ""); };
  body.innerHTML = `<p class="muted">${esc(connTitle(c))} · read ${esc(ago(c.fetched_at))}.${toolLink(c.console, c.label)}</p>
    ${(c.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join("")}
    <h4>Entities matching this resource</h4>
    ${c.entities.length ? table(["State", "Entity", "Type", "Account", "How it matched"], sortEntities(c.entities).map((e) => [monPill(e.severity),
      monLink(e.url, e.name) + (e.reporting ? "" : ' <span class="muted">(not reporting)</span>'), entityType(e), esc(e.account), how(e)]))
      : `<p class="muted">No ${esc(c.label)} entity matches this resource by Azure ID or name.</p>`}
    <h4>Open issues</h4>
    ${c.issues.length ? table(["Priority", "Open issue", "Condition", "Activated", "How it matched"], sortIssues(c.issues).map((i) => [issuePill(i),
      monLink(i.url, i.title || i.id), esc(i.condition), agoHtml(i.activated_at), how(i)]))
      : '<p class="ok">✓ No open issues touch this resource.</p>'}
    <h4>Logs</h4>
    ${c.capabilities.includes("logs") ? `<p class="muted">Read from ${esc(c.name)} only when you click Load. The cost follows how many
        lines it scans (the window × the partitions), so a shorter window or one partition keeps it down.</p>
      <div data-tool-logs>${loading("Finding what can be read…")}</div>` : `<p class="muted">${esc(c.label)} doesn't provide logs here.</p>`}`;
  const box = body.querySelector("[data-tool-logs]");
  if (box) mountResourceLogs(box, kind, node);
}

// A resource's logs: the plan first, so the partitions and the query are there before the first Load.
async function mountResourceLogs(box, kind, target) {
  const request = (path, extra) => api(path, { params: toolParams(kind, { id: target.id, ...extra }) });
  let plan;
  try { plan = await request("api/monitors/plan", { minutes: 30 }); } catch (error) {
    if (!error.signedOut) box.innerHTML = noticeHtml(error);
    return null;
  }
  if (!plan.available) { box.innerHTML = snUnavailable(plan); return null; }
  return readPanel(box, { request: (p) => request("api/monitors/logs", p), partitions: plan.logs.partitions,
    search: plan.search, azureIds: plan.azure_ids || [], byId: plan.by_id !== false, preview: { ...plan, ...plan.logs },
    onOptions: async (o) => {
      const again = await request("api/monitors/plan", { minutes: 30, ...o });
      return again.available ? { ...again, ...again.logs } : null;
    } });
}

// ------------------------------------------------------------------ reading a monitoring tool
// One panel for every read of a connection (the Query tab, an Alerts row's Logs, a resource's tool tab): window,
// partition, text and the name search; the query on top of the answer, "Will send" before the first read; Load older.

const READ_MINUTES = [[15, "15 minutes"], [30, "30 minutes"], [60, "1 hour"], [360, "6 hours"], [1440, "24 hours"], [10080, "7 days"]];
const PARTITION_HELP = "Log stores: Log, plus Log_<name> for each data partition set up. All reads every one; one scans less.";
const Q_TEXT = { logs: "Message contains… (optional)", errors: "Error message contains… (optional)",
  slow: "Transaction name contains… (optional)" };
const Q_COLUMNS = { appName: "App", transactionName: "Transaction", name: "Transaction", "error.class": "Error class",
  errors: "Errors", last_message: "Last message", last_seen: "Last seen", p95_s: "p95", avg_s: "Average", max_s: "Max",
  calls: "Calls", account: "Account" };
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

// The query a read sends (`sent`) or would send, with Copy and the tool's own console: one per account (NRQL) or one
// per place it's read (KQL, with its time span).
// ------------------------------------------------------------------ Is this alert right?
// Any alert whose source gives its rule and history (Azure metric alerts, a monitoring tool's conditions) gets the same
// arithmetic and documented limits (core/alert_check.py; README → "Is this alert right?"). Only when clicked.
function alertCheckButton(ref) {
  return `<button type="button" class="link alert-check-btn" data-alert-check="${esc(JSON.stringify(ref))}"
    title="Read the alert's rule and the last days of what it watches: is it unusual, or does it fire on normal values?">Is this alert right?</button>`;
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest && event.target.closest("[data-alert-check]");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();  // never opens the row it sits in
  const row = button.closest("tr, .attn-row, li, .ov-row") || button.parentElement;
  const next = row.nextElementSibling;
  if (next && next.classList.contains("alert-check")) { next.remove(); return; }
  const panel = document.createElement(row.tagName === "TR" ? "tr" : "div");
  panel.className = "alert-check";
  if (row.tagName === "TR") panel.innerHTML = `<td colspan="${row.children.length}"></td>`;
  row.after(panel);
  const slot = row.tagName === "TR" ? panel.firstElementChild : panel;
  slot.innerHTML = loading("Reading the alert's rule and the last days of what it watches…");
  try {
    slot.innerHTML = alertCheckHtml(await api("api/alert-check", { params: JSON.parse(button.dataset.alertCheck) }));
  } catch (error) { slot.innerHTML = noticeHtml(error); }
}, true);

function alertCheckHtml(d) {
  if (!d.available) return `<p class="notice">${esc(d.reason || "This alert can't be checked.")}</p>${d.fix ? `<p class="muted">${esc(d.fix)}</p>` : ""}`;
  const n = d.numbers || {}, r = d.rule || {};
  const num = (v) => (v == null ? "–" : fmt(v));
  const rows = [
    ["Now", num(n.now), n.now_at ? dayTime(n.now_at) : ""],
    ["Same time last week", num(n.week_ago), ""],
    ["Usual (median)", num(n.usual), ""],
    r.threshold != null ? ["Threshold", `${r.operator || ""} ${num(r.threshold)}`, ""] : null,
    n.breaching != null ? ["Rule true", `${n.breaching} of ${n.values} buckets (${n.breaching_percent}%)`, ""] : null,
    ["Now ÷ last week", num(n.now_vs_week), ""],
    n.threshold_vs_usual != null ? ["Threshold ÷ usual", num(n.threshold_vs_usual), ""] : null,
  ].filter(Boolean);
  const verdicts = d.verdicts.map((v) => `<li>${pill(v.status === "ok" ? "healthy" : v.status)} ${esc(v.text)}
      <div class="muted">Rule: ${esc(v.rule)}${v.setting ? ` · <code>${esc(v.setting)}</code> in Settings` : ""}</div></li>`).join("");
  const text = [`Is this alert right? ${r.name} (${d.source})`,
    ...d.verdicts.map((v) => `- ${v.text} (rule: ${v.rule}${v.setting ? `, ${v.setting}` : ""})`),
    ...rows.map(([k, v, extra]) => `${k}: ${v}${extra ? ` (${extra})` : ""}`),
    `Rule as the source defines it: ${r.query ? r.query.text : ""}`].join("\n");
  return `<div class="alert-check-body">
    <ul class="ac-verdicts">${verdicts}</ul>
    ${(r.links || []).length ? `<p class="attn-links">${linksHtml(r.links)}</p>` : ""}
    ${alertCheckChart(d)}
    <table class="ac-numbers"><tbody>${rows.map(([k, v, extra]) => `<tr><th>${esc(k)}</th><td>${esc(v)}${extra ? ` <span class="muted">${esc(extra)}</span>` : ""}</td></tr>`).join("")}</tbody></table>
    <details class="mon-query"><summary>How it was read</summary>
      <p><strong>${esc(d.source)}</strong> · ${esc(r.name)}${r.duration_minutes ? ` · must hold ${esc(fmt(r.duration_minutes))} min` : ""}</p>
      ${r.query ? `<div class="muted">${esc(r.query.label)}</div><pre>${esc(r.query.text)}</pre>` : ""}
      <p class="muted">The last ${esc(String(d.days))} days, ${esc(d.scaled)}; the latest bucket is left out while it fills in.</p>
      ${(d.notes || []).map((x) => `<p class="muted">${esc(x)}</p>`).join("")}
      ${queryBlock({ queries: d.queries })}
    </details>
    <button type="button" class="link" data-copy-check="${esc(text)}">Copy</button></div>`;
}

// The last days as a line, with the threshold dashed: a picture of "how often was the rule true".
function alertCheckChart(d) {
  const points = (d.points || []).filter((p) => p[1] != null);
  if (points.length < 2) return "";
  const r = d.rule || {};
  const values = points.map((p) => p[1]).concat(r.threshold != null ? [r.threshold] : []);
  const lo = Math.min(...values, 0), hi = Math.max(...values) || 1;
  const w = 600, h = 90, y = (v) => h - 4 - ((v - lo) / (hi - lo || 1)) * (h - 8);
  const line = points.map((p, i) => `${((i / (points.length - 1)) * w).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  const limit = r.threshold != null ? `<line x1="0" x2="${w}" y1="${y(r.threshold).toFixed(1)}" y2="${y(r.threshold).toFixed(1)}" class="ac-threshold"/>` : "";
  return `<svg class="ac-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img"
      aria-label="The last ${esc(String(d.days))} days of what the rule watches${r.threshold != null ? ", with its threshold" : ""}">${limit}<polyline points="${line}" class="ac-line"/></svg>
    <div class="ac-axis muted"><span>${esc(dayTime(points[0][0]))}</span><span>${r.threshold != null ? `dashed: threshold ${esc(r.operator)} ${esc(fmt(r.threshold))}` : ""}</span><span>${esc(dayTime(points[points.length - 1][0]))}</span></div>`;
}

document.addEventListener("click", (event) => {
  const copy = event.target.closest && event.target.closest("[data-copy-check]");
  if (copy) copyText(copy.dataset.copyCheck, copy);
});

function queryBlock(data, sent = true) {
  const queries = (data && data.queries) || [];
  if (!queries.length) return "";
  const textOf = (q) => q.nrql || q.query;
  const nameOf = (q) => (q.account != null ? `account ${q.account}` : q.label || "");
  const text = queries.map(textOf).join("\n\n");
  const count = queries.length > 1 ? `${queries.length} ${queries[0].account != null ? "accounts" : "queries"}` : nameOf(queries[0]);
  return `<div class="q-query"><div class="q-query-head"><strong>${sent ? "Query sent" : "Will send"}</strong>
      <span class="muted">${esc([data.label, count].filter(Boolean).join(" · "))}</span>
      <span class="spacer"></span><button type="button" class="link" data-copy-query="${esc(text)}">Copy</button>
      ${data.console ? `<a href="${safeHref(data.console)}" target="_blank" rel="noopener">Open ${esc(data.label)}</a>` : ""}</div>
    ${queries.map((q) => `${queries.length > 1 || q.timespan ? `<div class="muted">${esc([queries.length > 1 ? nameOf(q) : "", q.timespan ? `over ${q.timespan}` : ""]
      .filter(Boolean).join(" · ").replace(/^account/, "Account"))}</div>` : ""}<pre>${esc(textOf(q))}</pre>`).join("")}</div>`;
}

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-goto-security]")) { state.secSub = "config"; showView("security"); return; }
  const open = event.target.closest("[data-investigate]");
  if (open) {
    const t = JSON.parse(open.dataset.investigate);
    investigate(t.component, t.operation ?? null, t.role || "", t.trace ? { trace: t.trace, at: t.at, instance: t.instance } : null);
    return;
  }
  const copy = event.target.closest("[data-copy-query]");
  if (copy) copyText(copy.dataset.copyQuery, copy);
});

function partitionOptions(found, keep = "") {
  return `<option value="">${found.length ? `All partitions (${found.length})` : "All partitions"}</option>${
    found.map((p) => `<option value="${esc(p)}" ${p === keep ? "selected" : ""}>${esc(p)}</option>`).join("")}`;
}

// The lines one or more reads returned, newest first, each expandable to every attribute; Load older when cut.
function logsHtml(data, lines = data.lines || []) {
  if (!data.available) return snUnavailable(data);
  const top = queryBlock(data) + noticesHtml(data.notes);
  if (data.note) return `${top}<p class="muted">${esc(data.note)}</p>`;
  const from = [data.entities.length ? `from ${esc(data.entities.join(", "))}` : "", data.search ? `mentioning ${esc(data.search)}` : ""]
    .filter(Boolean).join(" or ");
  const read = [...new Set((data.queries || []).flatMap((q) => q.partitions || []))];
  const w = data.window || {};
  const span = w.since ? ` · ${clock(w.since)}–${w.until ? clock(w.until) : "now"}` : "";
  const oldest = lines.length ? Math.min(...lines.map((l) => l.ms).filter(Boolean)) : 0;
  return `${top}<p class="muted">${lines.length} line(s)${from ? ` ${from}` : ""}${read.length ? ` · read ${esc(read.join(", "))}` : ""}${span}, newest first.</p>
    ${lines.length ? `<table class="data-table mon-logs"><thead><tr><th>Time</th><th>Level</th><th>Entity</th><th>Message (click for every attribute)</th></tr></thead>
      <tbody>${lines.map((l) => `<tr><td title="${esc(l.time)}">${esc(l.time ? new Date(l.time).toLocaleTimeString() : "")}</td>
        <td>${esc(l.level)}</td><td>${l.via === "mention" ? `<span class="muted" title="Found because it mentions the name; it may come from something else">mentions ${esc(data.search)}</span>`
          : `${esc(l.entity || "entity")}`}</td>
        <td><details><summary>${esc(l.message.slice(0, 300) || "(no message)")}</summary><pre>${esc(JSON.stringify(l.attributes, null, 2))}</pre>
          ${l.message.length > 300 ? `<pre>${esc(l.message)}</pre>` : ""}</details></td></tr>`).join("")}</tbody></table>`
      : '<p class="ok">✓ No log lines in this window.</p>'}
    ${data.truncated && oldest > (w.since || 0) ? `<p><button type="button" data-r-older>Load older</button>
      <span class="muted">Each read takes the newest 100 lines; this reads the 100 before ${esc(clock(oldest))}, in the same window.</span></p>` : ""}`;
}

function reportCell(column, value) {
  if (value == null || value === "") return "–";
  if (column === "last_seen") return seen(value);
  if (column.endsWith("_s") && typeof value === "number") return value < 1 ? `${fmt(Math.round(value * 1000))} ms` : `${fmt(Math.round(value * 100) / 100)} s`;
  if (column === "last_message") return `<span class="ai-msg">${esc(String(value).slice(0, 300))}</span>`;
  return esc(typeof value === "number" ? fmt(value) : String(value));
}

function reportHtml(data) {
  if (!data.available) return snUnavailable(data);
  return `${queryBlock(data)}${noticesHtml(data.notes)}
    <p class="muted">${esc(data.report_label)}: ${data.rows.length} row(s)${data.entities.length ? ` from ${esc(data.entities.join(", "))} or` : ""}
      for apps named like the resource · last ${esc(data.minutes)} min${data.truncated ? " · <strong>only the first rows were read</strong>" : ""}.</p>
    ${data.rows.length ? table([...data.columns.map((c) => Q_COLUMNS[c] || c)], data.rows.map((r) => data.columns.map((c) => reportCell(c, r[c]))))
      : `<p class="ok">✓ No ${esc(data.report_label.toLowerCase())} in this window. Only apps with a ${esc(data.label)} APM agent report them.</p>`}`;
}

// `request(params)` makes the call; `preview` (a plan) is shown as "Will send" until the first read; `onOptions(o)`
// returns the plan with the name search or Azure ID match ticked off (`o`), to update that preview. Returns { run }.
function readPanel(box, { kind = "logs", request, partitions = [], search = "", azureIds = [], byId = true, preview = null, onOptions = null }) {
  const isLogs = kind === "logs";
  const root = document.createElement("div");
  root.innerHTML = `<div class="table-tools read-tools">
      <label>Last <select data-r-minutes>${READ_MINUTES.map(([v, l]) => `<option value="${v}" ${v === 30 ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      ${isLogs ? `<label title="${esc(PARTITION_HELP)}">Partition <select data-r-part>${partitionOptions(partitions)}</select></label>` : ""}
      <input type="search" data-r-text placeholder="${esc(Q_TEXT[kind] || "Contains… (optional)")}" aria-label="Contains">
      ${isLogs && search ? `<label class="inline" title="Lines that mention ${esc(search)} anywhere, even with no entity linked; they may come from other apps.">
        <input type="checkbox" data-r-mentions checked> also lines mentioning <code>${esc(search)}</code></label>` : ""}
      ${isLogs && azureIds.length ? `<label class="inline" title="Lines whose resourceId is ${azureIds.length === 1 ? "this resource's" : "these resources'"} Azure ID, as Azure's diagnostic logs carry it when forwarded, with no entity linked.">
        <input type="checkbox" data-r-byid ${byId ? "checked" : ""}> also lines tagged with ${azureIds.length === 1 ? "its" : "their"} Azure ID</label>` : ""}
      <button type="button" class="primary" data-r-run>${isLogs ? "Load logs" : "Run"}</button></div>
    <div data-r-preview>${preview ? queryBlock(preview, false) : ""}</div><div data-r-out></div>`;
  box.replaceChildren(root);
  const q = (selector) => root.querySelector(selector);
  const out = q("[data-r-out]");
  let last = null, lines = [];
  const options = () => {  // the ticked-off matches, as the routes read them
    const o = {};
    if (q("[data-r-mentions]") && !q("[data-r-mentions]").checked) o.mentions = "0";
    if (q("[data-r-byid]") && !q("[data-r-byid]").checked) o.by_id = "0";
    return o;
  };
  const params = (older) => {
    const p = { minutes: q("[data-r-minutes]").value, text: q("[data-r-text]").value };
    if (isLogs) Object.assign(p, { partition: q("[data-r-part]").value ? [q("[data-r-part]").value] : [] }, options());
    if (older) Object.assign(p, { since: last.window.since, until: Math.min(...lines.map((l) => l.ms).filter(Boolean)) });
    return p;
  };
  const run = async (older = false) => {
    const button = older && q("[data-r-older]");
    if (button) { button.disabled = true; button.textContent = "Loading older…"; } else out.innerHTML = loading("Reading…");
    try {
      const data = await request(params(older));
      if (isLogs && data.partitions && data.partitions.length) {  // every answer refreshes them, keeping the pick
        const select = q("[data-r-part]");
        select.innerHTML = partitionOptions(data.partitions, select.value);
      }
      lines = older && data.available ? [...lines, ...data.lines] : data.lines || [];
      last = data;
      q("[data-r-preview]").innerHTML = "";
      out.innerHTML = isLogs ? logsHtml(data, lines) : reportHtml(data);
    } catch (error) {
      if (!error.signedOut) out.innerHTML = noticeHtml(error);
    }
  };
  q("[data-r-run]").addEventListener("click", () => run());
  q("[data-r-text]").addEventListener("keydown", (event) => { if (event.key === "Enter") run(); });
  root.addEventListener("click", (event) => { if (event.target.closest("[data-r-older]")) run(true); });
  for (const box of [q("[data-r-mentions]"), q("[data-r-byid]")]) {
    if (box && onOptions) box.addEventListener("change", async () => {
      const plan = await onOptions(options()).catch(() => null);
      if (plan && !last) q("[data-r-preview]").innerHTML = queryBlock(plan, false);
    });
  }
  return { run };
}

// ------------------------------------------------------------------ the tool tab's Query
// 1 a resource in scope, 2 what the tool has for it (the plan: entities and how they matched, the name search, the
// partitions, the queries; nothing is read yet), 3 what to read, in the shared panel.

function showMonSub(sub) {
  const usable = state.mon && state.mon.available;
  state.monSub = usable ? sub : "alerts";
  $("mon-subtabs").classList.toggle("hidden", !usable);
  $("mon-q").classList.toggle("hidden", state.monSub !== "query");
  $("mon-alerts").classList.toggle("hidden", state.monSub !== "alerts");
  for (const b of $("mon-subtabs").querySelectorAll("[data-mon-sub]")) b.classList.toggle("active", b.dataset.monSub === state.monSub);
  if (state.monSub === "query") renderQueryList();
}

$("mon-subtabs").addEventListener("click", (event) => {
  const b = event.target.closest("[data-mon-sub]");
  if (b) showMonSub(b.dataset.monSub);
});

// Resources this connection already matched (from its alerting entities and open issues): suggested before typing.
function monMatched() {
  const out = new Map();
  for (const r of [...((state.mon || {}).issues || []), ...((state.mon || {}).entities || [])]) {
    for (const m of r.matches || []) if (m.id && m.display_type !== "Resource group" && !out.has(m.id.toLowerCase())) out.set(m.id.toLowerCase(), m);
  }
  return out;
}

function queryItem(r, matched) {
  const picked = state.monQuery && state.monQuery.id.toLowerCase() === r.id.toLowerCase();
  return `<li role="option" aria-selected="${picked}" class="${picked ? "picked" : ""}" data-pick="${esc(JSON.stringify({ id: r.id, name: r.name }))}">
    <span><strong>${esc(r.name)}</strong> <span class="muted">${esc([r.display_type, r.resource_group].filter(Boolean).join(" · "))}</span></span>
    ${matched.has(r.id.toLowerCase()) ? `<span class="chip-ok">alerting in ${esc(toolLabel(state.toolKind))}</span>` : ""}</li>`;
}

// An entity found by name in the tool itself, for apps that match no Azure resource.
function entityItem(e) {
  const pick = { entity: true, id: `entity:${e.ids[0]}`, ids: e.ids, account: e.account_id, name: e.name, apm: e.apm };
  const picked = state.monQuery && state.monQuery.id === pick.id;
  return `<li role="option" aria-selected="${picked}" class="${picked ? "picked" : ""}" data-pick="${esc(JSON.stringify(pick))}">
    <span><strong>${esc(e.name)}</strong> <span class="muted">${esc([e.type, e.account].filter(Boolean).join(" · "))}</span></span>
    ${e.apm ? '<span class="chip-ok">APM</span>' : ""}</li>`;
}

function renderQueryList(found = null, entities = state.monEntities || []) {
  const list = $("mon-q-list");
  const matched = monMatched();
  const text = $("mon-q-res").value.trim();
  if (found) {
    const inTool = entities.length ? `<li class="q-list-head muted">In ${esc(toolLabel(state.toolKind))}, by name</li>${entities.slice(0, 20).map(entityItem).join("")}` : "";
    list.innerHTML = (found.length ? `${entities.length ? '<li class="q-list-head muted">Azure resources in scope</li>' : ""}${found.slice(0, 50).map((r) => queryItem(r, matched)).join("")}`
      : `<li class="muted">Nothing in scope has that in its name${entities.length ? "" : `, nor does any ${esc(toolLabel(state.toolKind))} entity`}.</li>`) + inTool;
  } else if (text.length < 2) {
    const picks = [...matched.values()];
    list.innerHTML = picks.length ? `<li class="q-list-head muted">Alerting in ${esc(toolLabel(state.toolKind))}</li>${picks.slice(0, 20).map((r) => queryItem(r, matched)).join("")}`
      : '<li class="muted">Type at least 2 letters of a resource name.</li>';
  }
}

const Q_PICK = '<p class="muted">Pick a resource: this shows what will be read, before anything is.</p>';

// A new connection starts the Query over: its plan, partitions and results belong to it.
function renderQueryBar() {
  const c = state.mon;
  showMonSub(state.monSub || "alerts");
  if (!c || !c.available) return;
  for (const el of $("mon-q").querySelectorAll("[data-tool-label]")) el.textContent = toolLabel(state.toolKind);
  const conn = `${state.toolKind}|${state.selected[state.toolKind]}`;
  if (state.monQueryConn === conn) return;
  state.monQueryConn = conn;
  state.monQuery = null;
  state.monPlan = null;
  $("mon-q-res").value = "";
  $("mon-q-plan").innerHTML = Q_PICK;
  $("mon-q-kinds").innerHTML = "";
  $("mon-q-kind-note").textContent = "";
  $("mon-q-out").innerHTML = "";
}

const findResources = debounce(async () => {
  const text = $("mon-q-res").value.trim();
  if (text.length < 2) { renderQueryList(); return; }
  if (state.monQuery && state.monQuery.name === text) return;
  let found = [], entities = [];
  try {
    [found, entities] = await Promise.all([api("api/monitors/find", { params: { ...scopeParams(currentScope()), q: text } }),
      api("api/monitors/entities", { params: toolParams(state.toolKind, { q: text }) }).catch(() => [])]);
  } catch { return; }
  if ($("mon-q-res").value.trim() !== text) return;
  state.monFound = found;
  state.monEntities = entities;
  renderQueryList(found, entities);
}, 250);

function pickQueryResource(target) {
  state.monQuery = target;
  $("mon-q-res").value = target.name;
  renderQueryList(state.monFound && (target.entity || state.monFound.some((r) => r.id === target.id)) ? state.monFound : null);
  loadQueryPlan();
}

// A resource is asked by its Azure ID; an entity picked by name by its IDs and account, on the entity routes.
const ENTITY_ROUTES = { "api/monitors/plan": "api/monitors/entity-plan", "api/monitors/logs": "api/monitors/entity-logs",
  "api/monitors/report": "api/monitors/entity-report" };

function queryRequest(path, extra) {
  const t = state.monQuery;
  const target = t.entity ? { entity: t.ids, account: t.account, name: t.name, apm: t.apm ? "1" : "0" } : { id: t.id };
  return api(t.entity ? ENTITY_ROUTES[path] : path, { params: toolParams(state.toolKind, { ...target, ...extra }) });
}

async function loadQueryPlan() {
  const target = state.monQuery;
  $("mon-q-plan").innerHTML = loading(`Asking ${state.selected[state.toolKind] || toolLabel(state.toolKind)} what it has for ${target.name}…`);
  $("mon-q-kinds").innerHTML = "";
  $("mon-q-out").innerHTML = "";
  let plan;
  try { plan = await queryRequest("api/monitors/plan", { minutes: 30 }); } catch (error) {
    if (!error.signedOut) $("mon-q-plan").innerHTML = noticeHtml(error);
    return;
  }
  if (state.monQuery !== target) return;
  state.monPlan = plan;
  $("mon-q-plan").innerHTML = planHtml(plan, target);
  if (!plan.available) return;
  const kinds = queryKinds(plan);
  if (!kinds.some((k) => k.key === state.monKind && k.runnable)) state.monKind = (kinds.find((k) => k.runnable) || {}).key;
  renderQueryKinds();
}

function planHtml(plan, target) {
  if (!plan.available) return snUnavailable(plan);
  const entities = plan.entities.map((e) => `<li>${monLink(e.url, e.name)} <span class="muted">${esc([e.type, e.account].filter(Boolean).join(" · "))}
    · ${esc(MON_CONFIDENCE[e.how] || e.how)}</span>${e.apm ? ' <span class="chip-ok">APM</span>' : ""}</li>`).join("");
  const sources = [
    entities ? `<li>Lines from ${plan.entities.length === 1 ? "this entity" : "these entities"}:<ul>${entities}</ul></li>` : "",
    plan.attribute ? `<li>Lines whose <code>${esc(plan.attribute)}</code> is <code>${esc(target.name)}</code> (saved for this resource type in Settings).</li>` : "",
    plan.search ? `<li>Lines that mention <code>${esc(plan.search)}</code> anywhere: finds logs with no entity linked, and may include other apps'.
      Untick it below to leave it out.</li>` : ""].filter(Boolean).join("");
  const parts = plan.logs ? plan.logs.partitions : [];
  return `${noticesHtml(plan.notes)}
    ${sources ? `<ul class="q-sources">${sources}</ul>` : ""}
    ${!entities ? `<p class="muted">No ${esc(plan.label)} entity matches ${esc(target.name)} by Azure ID or name. Type its
      ${esc(plan.label)} app or entity name instead to pick it there.</p>` : ""}
    ${plan.search_note ? `<p class="muted">${esc(plan.search_note)}</p>` : ""}
    ${plan.logs && plan.logs.note ? noticeHtml(plan.logs.note) : ""}
    ${plan.logs ? `<p class="muted" title="${esc(PARTITION_HELP)}">Log partitions: ${parts.length ? esc(parts.join(", ")) : "none found"}.</p>` : ""}
    ${noticesHtml(plan.logs ? plan.logs.notes : [])}`;
}

function queryKinds(plan) {
  return [...(plan.logs ? [{ key: "logs", label: "Logs", runnable: !plan.logs.note, note: plan.logs.note || "" }] : []), ...(plan.reports || [])];
}

function renderQueryKinds() {
  const kinds = queryKinds(state.monPlan);
  $("mon-q-kinds").innerHTML = kinds.map((k) => `<button type="button" data-q-kind="${esc(k.key)}" class="${k.key === state.monKind ? "active" : ""}"
    aria-pressed="${k.key === state.monKind}" ${k.runnable ? "" : "disabled"} title="${esc(k.note)}">${esc(k.label)}</button>`).join("");
  const chosen = kinds.find((k) => k.key === state.monKind);
  $("mon-q-kind-note").textContent = chosen ? chosen.note : kinds.length ? "Nothing here can be read for this resource." : "";
  if (chosen) mountQueryPanel(chosen);
}

function mountQueryPanel(chosen) {
  const plan = state.monPlan;
  const isLogs = chosen.key === "logs";
  readPanel($("mon-q-out"), {
    kind: chosen.key,
    request: (p) => queryRequest(isLogs ? "api/monitors/logs" : "api/monitors/report", isLogs ? p : { ...p, kind: chosen.key }),
    partitions: isLogs ? plan.logs.partitions : [], search: isLogs ? plan.search : "",
    azureIds: isLogs ? plan.azure_ids || [] : [], byId: plan.by_id !== false,
    preview: { ...plan, queries: isLogs ? plan.logs.queries : chosen.queries },
    onOptions: isLogs ? async (o) => {
      const again = await queryRequest("api/monitors/plan", { minutes: 30, ...o });
      return again.available ? { ...again, ...again.logs } : null;
    } : null,
  });
}

$("mon-q-kinds").addEventListener("click", (event) => {
  const b = event.target.closest("[data-q-kind]");
  if (!b || b.disabled) return;
  state.monKind = b.dataset.qKind;
  renderQueryKinds();
});
$("mon-q-res").addEventListener("input", () => {
  if (state.monQuery && state.monQuery.name !== $("mon-q-res").value.trim()) state.monQuery = null;
  findResources();
});
$("mon-q-list").addEventListener("click", (event) => {
  const item = event.target.closest("[data-pick]");
  if (item) pickQueryResource(JSON.parse(item.dataset.pick));
});

// A Logs button on a tool-tab row: that entity's (or that issue's entities') logs, in the shared panel under the row.
// `matches` are the Azure resources the row matched: their IDs also find forwarded Azure logs with no entity.
function logsButton(ids, account, label, hasLogs, name = "", matches = []) {
  if (!hasLogs || !ids.length || !account) return "";
  const resources = matches.map((m) => m.id).filter((id) => /\/providers\//i.test(id || "")).slice(0, 20);
  return `<button type="button" class="link" data-logs="${esc(JSON.stringify({ ids, account, label, name, resources }))}">Logs</button>`;
}

function toggleRowLogs(button) {
  const target = JSON.parse(button.dataset.logs);
  toggleLogsRow(button, `Logs: ${esc(target.label)}`, (box) => {
    box.classList.add("mon-logs-panel");
    readPanel(box, { search: target.name, azureIds: target.resources || [],
      request: (p) => api("api/monitors/entity-logs", { params: toolParams(state.toolKind, {
        entity: target.ids, account: target.account, name: target.name || "", resource: target.resources || [], ...p }) }) }).run();
  }, ["Logs", "Hide logs"]);
}

// Triage: one "alerting" card per tool, from its selected connection.
// Triage: App Insights failure spikes from metrics (one GET per component, every one in scope), after the Azure cards.
async function loadAppInsightsCard(scope) {
  const token = state.triageToken;
  const holder = document.createElement("div");
  holder.className = "card";
  holder.innerHTML = `<h3>App Insights: failure spikes</h3>${loading("Comparing every component's last hour with last week (about 5 s per 100 components)…")}`;
  $("triage-cards").append(holder);
  let card;
  try {
    card = await api("api/triage/appinsights", { params: scopeParams(scope) });
  } catch (error) {
    if (!error.signedOut && token === state.triageToken) holder.innerHTML = `<h3>App Insights: failure spikes</h3>${noticeHtml(error)}`;
    return;
  }
  if (token !== state.triageToken) return;
  if (!card.total) { holder.remove(); return; }  // no components in scope: nothing to say
  holder.outerHTML = renderCard({ ...card, description: `${card.description} (${card.checked} component${card.checked === 1 ? "" : "s"} checked)` });
}

async function loadMonitorCards(scope) {
  for (const kind of toolKinds()) loadToolCard(kind, scope);
}

async function loadToolCard(kind, scope) {
  const token = state.triageToken;
  const label = toolLabel(kind);
  const holder = document.createElement("div");
  holder.className = "card";
  holder.innerHTML = `<h3>${esc(label)}</h3>${loading("Reading what's alerting…")}`;
  $("triage-cards").prepend(holder);
  let c;
  try {
    c = await api("api/monitors", { params: toolParams(kind, scopeParams(scope)) });
  } catch (error) {
    if (!error.signedOut) holder.innerHTML = `<h3>${esc(label)}</h3>${noticeHtml(error)}`;
    return;
  }
  if (token !== state.triageToken) return;
  const title = `${esc(label)}: alerting <span class="muted">· ${esc(state.selected[kind] || "")}</span>`;
  if (!c.available) { holder.outerHTML = `<div class="card warning" id="card-tool-${kind}"><h3>${title}</h3>${snUnavailable(c)}</div>`; return; }
  const all = sortEntities(c.entities.filter((e) => e.severity === "critical" || e.severity === "warning"));
  const rows = all.filter(namesResource);
  const others = all.length - rows.length;
  holder.outerHTML = `<div class="card ${rows.length ? (rows.some((e) => e.severity === "critical") ? "critical" : "warning") : "clear"}" id="card-tool-${kind}">
    <h3>${title}<span class="count">${rows.length}</span></h3>
    <p class="desc">Entities in critical or warning that match Azure resources in scope (${esc(connTitle(c))})</p>
    ${rows.length ? `<ul>${rows.slice(0, 15).map((e) => `<li class="static">${monPill(e.severity)} ${monLink(e.url, e.name)}
      <div class="where">${esc(e.type)} · ${resourceChips(e.matches, 3)}</div></li>`).join("")}</ul>`
      : '<p class="ok">✓ Nothing alerting matches a resource in scope</p>'}
    ${rows.length > 15 ? `<p class="muted">+${rows.length - 15} more in the ${esc(label)} tab.</p>` : ""}
    ${others ? `<p class="muted">+${others} other alerting entit${others === 1 ? "y doesn't" : "ies don't"} match anything in scope (${esc(label)} tab).</p>` : ""}
  </div>`;
}

