"use strict";

// ------------------------------------------------------------------ timeline
// One list, newest first, of what happened to a resource: Azure's alerts, changes, operations and Resource Health,
// plus ServiceNow and each monitoring tool when connected. "What came first" is the question it answers.

const TIMELINE_WINDOWS = [["6", "6 h"], ["24", "24 h"], ["72", "3 days"], ["168", "7 days"], ["336", "14 days"], ["720", "30 days"]];
const HEALTH_STATUS = { Available: "healthy", Unavailable: "critical", Degraded: "warning" };

function timelineWindow() {
  return stored.choice("ahc.timelineWindow", TIMELINE_WINDOWS.map(([v]) => v), "24");
}

function azureEvents(data) {
  const events = [];
  for (const a of data.alerts) {
    const times = a.count ? ` ${a.count} times since ${new Date(a.first).toLocaleString()}` : "";
    events.push({ at: a.started, source: "Alert", name: a.name, status: a.status, title: `${a.name} fired${times}`, links: a.links,
      detail: [`Sev${fmt(a.severity)}`, a.what, a.service || "Azure Monitor", a.condition === "Fired" ? "still fired" : "", a.count ? "latest shown" : ""].filter(Boolean).join(" · ") });
    if (a.resolved && !a.count) events.push({ at: a.resolved, source: "Alert", status: "healthy", title: `${a.name} resolved`, detail: `Sev${fmt(a.severity)}` });
  }
  for (const c of data.changes) {
    const fields = Object.keys(c.changes || {});
    events.push({ at: c.at, source: "Change", status: c.type === "Delete" ? "warning" : "info", title: `${c.type || "Change"} by ${c.by || "unknown"}`,
      detail: [c.client, fields.slice(0, 6).join(", ") + (fields.length > 6 ? ` +${fields.length - 6}` : "")].filter(Boolean).join(" · ") });
  }
  for (const o of data.operations) {
    const failed = /fail/i.test(o.status || "");
    events.push({ at: o.at, source: "Operation", status: failed ? "critical" : "info",
      title: `${o.operation || "Operation"}${o.count ? ` ×${o.count}` : ""}`,
      detail: [o.status, o.caller, o.count && `first ${new Date(o.first).toLocaleString()}, latest shown`].filter(Boolean).join(" · ") });
  }
  for (const h of data.health_history) {
    events.push({ at: h.at, source: "Resource Health", status: HEALTH_STATUS[h.state] || "unknown", title: h.state || "Unknown",
      detail: [h.summary, h.reason].filter(Boolean).join(" · ") });
  }
  return events;
}

// A change's window as a pill colour: running now or overrunning stands out, closed ones are history.
const SN_CHANGE_TONE = { "in progress": "soon", "past its window": "warning", scheduled: "info", "not scheduled": "unknown", closed: "unknown" };
// What else names a change's resources: its affected CIs and its tasks (when those tables are read).
const snChangeParts = (r) => [(r.affected_cis || []).length ? `Affected CIs: ${r.affected_cis.join(", ")}` : "",
  (r.tasks || []).length ? `Tasks: ${r.tasks.map((t) => `${t.number}${t.state ? ` (${t.state})` : ""}`).join(", ")}` : ""].filter(Boolean).join(" · ");
const snChangeSpan = (d) => `the last ${d.change_history_days} days, now, and the next ${d.change_ahead_days} days`;

// One key per record, shared by the timeline's events and Overview's lists, so each is shown once.
const snKey = (r) => `servicenow:${r.sys_id || r.number}`;
const toolKey = (kind, i) => `${kind}:${i.id}`;

function serviceNowEvents(sn) {
  const priority = (p) => (p === 1 ? "critical" : p === 2 ? "warning" : "info");
  return [
    ...sn.incidents.map((r) => ({ key: snKey(r), at: r.opened_at, source: "ServiceNow", status: priority(r.priority), url: r.url,
      title: `${r.number} opened: ${r.title}`, detail: [r.priority_label, r.state, r.group].filter(Boolean).join(" · ") })),
    ...sn.changes.map((r) => ({ at: r.start, source: "ServiceNow", status: "info", url: r.url,
      title: `${r.number} change ${r.window}: ${r.title}`, detail: [r.type, r.risk && `risk ${r.risk}`, r.end && `ends ${new Date(r.end).toLocaleString()}`].filter(Boolean).join(" · ") })),
    ...[...sn.alerts, ...sn.events].map((r) => ({ at: r.updated_at, source: "ServiceNow", status: "warning", url: r.url,
      title: `${r.number || r.kind}: ${r.title}`, detail: [r.severity && `severity ${r.severity}`, r.source].filter(Boolean).join(" · ") })),
  ];
}

function monitorEvents(kind, data) {
  return data.issues.map((i) => ({ key: toolKey(kind, i), at: i.activated_at, source: toolLabel(kind), url: i.url,
    status: i.priority === "critical" ? "critical" : i.priority === "warning" ? "warning" : "info",
    title: `Issue: ${i.title || i.id}`, detail: [i.priority_label, i.condition, i.policy].filter(Boolean).join(" · ") }));
}

// Every source's events for one resource, newest first; shared by the Timeline tab and the Overview.
async function timelineData(node, hours) {
  const since = Date.now() - Number(hours) * 3600000;
  const sources = [exploreGet("activity", node, { hours }).then((d) => ({ name: "Azure", kind: "azure", events: azureEvents(d), errors: d.errors }))];
  if (state.snEnabled) {
    sources.push(exploreGet("servicenow", node, { connection: state.selected.servicenow || "" })
      .then((d) => (d.available ? { name: "ServiceNow", kind: "servicenow", events: serviceNowEvents(d), raw: d } : { name: "ServiceNow", unavailable: d.reason, fix: d.fix })));
  }
  for (const kind of toolKinds()) {
    sources.push(exploreGet("monitors", node, toolParams(kind))
      .then((d) => (d.available ? { name: toolLabel(kind), kind, events: monitorEvents(kind, d), errors: d.notes, raw: d } : { name: toolLabel(kind), unavailable: d.reason, fix: d.fix })));
  }
  const names = ["Azure", ...(state.snEnabled ? ["ServiceNow"] : []), ...toolKinds().map(toolLabel)];
  const results = await Promise.all(sources.map((p, i) => p.catch((error) => ({ name: names[i], unavailable: error.message }))));
  const all = results.flatMap((r) => r.events || []).filter((e) => e.at && new Date(e.at).getTime() >= since)
    .sort((a, b) => new Date(b.at) - new Date(a.at));
  return { all, results };
}

async function tabTimeline(node, body) {
  const hours = timelineWindow();
  const { all, results } = await timelineData(node, hours);
  const hidden = new Set(state.timelineHidden || []);
  const counts = {};
  for (const e of all) counts[e.source] = (counts[e.source] || 0) + 1;
  const shown = all.filter((e) => !hidden.has(e.source));
  const notes = [
    ...results.filter((r) => r.unavailable).map((r) => `${r.name}: ${r.unavailable}${r.fix ? ` ${r.fix}` : ""}`),
    ...results.flatMap((r) => (r.errors || []).map((e) => `${r.name}: ${e}`)),
    ...(!state.snEnabled && !toolKinds().length ? ["ServiceNow and monitoring tools aren't connected for this tenant, so only Azure's own events show. Settings → Integrations adds them."] : []),
  ];
  let day = "";
  const rows = shown.map((e) => {
    const when = new Date(e.at);
    const label = when.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    const head = label !== day ? `<li class="tl-day">${esc((day = label))}</li>` : "";
    return `${head}<li class="tl-event"><span class="tl-time" title="${esc(when.toLocaleString())}">${esc(when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</span>
      ${pill(e.status)} <span class="tl-source">${esc(e.source)}</span> <strong>${e.url ? monLink(e.url, e.title) : esc(e.title)}</strong>
      ${e.detail ? `<div class="muted tl-detail">${escTimes(e.detail)}</div>` : ""}${(e.links || []).length ? `<div class="attn-links">${linksHtml(e.links)}</div>` : ""}</li>`;
  }).join("");
  body.innerHTML = `<div class="table-tools">
      <label class="inline">Last <select id="tl-window">${TIMELINE_WINDOWS.map(([v, l]) => `<option value="${v}" ${v === hours ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      ${Object.keys(counts).sort().map((src) => `<label class="inline"><input type="checkbox" data-tl-source="${esc(src)}" ${hidden.has(src) ? "" : "checked"}> ${esc(src)} (${counts[src]})</label>`).join("")}
    </div>
    ${notes.length ? `<div class="notice partial"><strong>Not in the timeline</strong><ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
    ${shown.length ? `<ul class="timeline">${rows}</ul>` : `<p class="muted">Nothing happened to this resource in the last ${esc(TIMELINE_WINDOWS.find(([v]) => v === hours)[1])}${all.length ? " from the sources ticked" : ""}.</p>`}`;
  body.querySelector("#tl-window").addEventListener("change", (event) => {
    stored.set("ahc.timelineWindow", event.target.value);
    renderTab(node, "timeline");
  });
  for (const box of body.querySelectorAll("[data-tl-source]")) {
    box.addEventListener("change", () => {
      const off = new Set(state.timelineHidden || []);
      if (box.checked) off.delete(box.dataset.tlSource); else off.add(box.dataset.tlSource);
      state.timelineHidden = [...off];
      renderTab(node, "timeline");
    });
  }
}

async function tabRecommendations(node, body) {
  const data = await exploreGet("recommendations", node);
  const on = (r) => (!r.inside ? '<span class="muted">this resource</span>'
    : `${r.count} inside it<div class="muted">${r.on.map(esc).join(", ")}${r.count > r.on.length ? ", …" : ""}</div>`);
  const note = (rows) => (rows.some((r) => r.inside) ? `<p class="muted">Includes findings on resources inside this one (for
      example an API Management service's APIs or a SQL server's databases), one row per finding with how many it's on.</p>` : "");
  body.innerHTML = `
    <h4>Azure Advisor <span class="muted">${data.advisor.length}</span></h4>${note(data.advisor)}
    ${table(["Impact", "Category", "Recommendation", "On"], data.advisor.map((r) => [
      esc(r.impact), esc(r.category),
      `${esc(r.problem || "(Advisor gives no text for this one)")}${r.solution ? `<div class="muted">${esc(r.solution)}</div>` : ""}${
        r.link ? `<div class="rule"><a href="${safeHref(r.link)}" target="_blank" rel="noopener">Microsoft's page for it</a></div>`
          : r.guide ? `<div class="rule"><a href="${safeHref(r.guide)}" target="_blank" rel="noopener">Advisor's ${esc(r.category)} recommendations</a></div>` : ""}`,
      on(r)]))}
    <h4>Defender for Cloud, unhealthy <span class="muted">${data.security.length}</span></h4>${note(data.security)}
    ${table(["Severity", "Finding", "Category", "On"], data.security.map((r) => [
      esc(r.severity), `${esc(r.name)}${r.link ? ` <a href="${safeHref(tenantPortal(/^https?:/.test(r.link) ? r.link : `https://${r.link}`))}" target="_blank" rel="noopener">in the portal</a>` : ""}`, esc(r.category), on(r)]))}
    ${noticesHtml(data.errors)}`;
}

// ------------------------------------------------------------------ application insights
// One component: this window against the same window last week. Several apps often share one, so rows carry the role.

const AI_WINDOWS = ["1h", "6h", "24h", "3d", "7d", "14d", "30d"];
// How far back each source goes (measured): a window past it just finds nothing older.
const AI_KEEP_NOTE = "Application Insights keeps 30 days by default (some components 90 days or more); changes go back 14 days.";

function aiWindow() {
  return stored.choice("ahc.aiWindow", AI_WINDOWS, "1h");
}

// `part`: the count behind the share, so a few failures among many never read as 0%.
const pctText = (v, part) => (v == null ? "–" : part > 0 && v < 0.1 ? "under 0.1%" : `${fmt(v)}%`);
// Durations past a second in seconds, past a minute in minutes and seconds: 91,136 ms is 1 min 31 s. msText sorts by the
// milliseconds (data-v); msLabel is the bare text, for inside another cell.
const msLabel = (v) => (v == null || v === "" ? "–" : v < 999.5 ? `${fmt(Math.round(v))} ms`
  : v < 59950 ? `${(v / 1000).toFixed(1)} s` : `${Math.floor(Math.round(v / 1000) / 60)} min ${Math.round(v / 1000) % 60} s`);
const msText = (v) => (v == null || v === "" ? "–" : `<span data-v="${Number(v)}">${msLabel(v)}</span>`);
const seen = (iso) => agoHtml(iso);
const codes = (list) => esc((list || []).join(", ")) || "–";

// Worse: more than one point above last week's failure rate, or failing now with none last week.
const failingMore = (r) => r.failed_pct != null && r.failed > 0 && (!r.week || r.week.failed_pct == null || r.failed_pct > r.week.failed_pct + 1);

function roleCompare(r) {
  if (!r.week) return '<span class="muted">no requests last week</span>';
  return `${fmt(r.week.total)} · ${pctText(r.week.failed_pct, r.week.failed)} failed · p95 ${msLabel(r.week.p95)}`;
}

// An app's component, picked by hand when Azure doesn't say: remembered per app in this browser.
function aiPicks() {
  return stored.json("ahc.aiPick", {});
}

function setAiPick(appId, componentId) {
  const picks = aiPicks();
  if (componentId) picks[appId.toLowerCase()] = componentId; else delete picks[appId.toLowerCase()];
  stored.set("ahc.aiPick", picks);
}

const AI_HOW = { tag: "its hidden-link tag", name: "probable: a related name in this resource group", picked: "your pick",
  logger: "its App Insights logger" };

function aiLinkHtml(link) {
  const options = link.candidates.map((c) => `<option value="${esc(c.id)}" ${c.id.toLowerCase() === (link.component_id || "").toLowerCase() ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const picker = link.candidates.length ? `<select id="ai-pick"><option value="">${link.component_id ? "Pick another…" : "Pick its component…"}</option>${options}</select>
    ${link.how === "picked" ? '<button type="button" class="link" id="ai-unpick">forget my pick</button>' : ""}` : "";
  if (!link.component_id && link.gateway) {
    return `<p class="notice">This API Management service has no Application Insights logger, so its requests aren't in
      any component. Add one (API Management → Application Insights) to see its failures here and in Investigate.</p>`;
  }
  if (!link.component_id) {
    return `<p class="notice">No Application Insights component is linked to this app: it has no hidden-link tag and no
      component in its resource group has a related name.${link.candidates.length ? " If one of these is it, pick it:" :
      " Its resource group has no components either; search for the component and open it directly."}</p>${picker}`;
  }
  return `<p class="ai-link">Data from <strong>${esc(link.component_name)}</strong>
    <span class="muted">· found by ${esc(AI_HOW[link.how] || link.how)}</span> ${picker}</p>`;
}

// A button that opens Investigate on this component (and an operation, or one request's trace).
const aiInvestigate = (data, target, label, title) =>
  `<button type="button" class="link" title="${esc(title)}" data-investigate="${esc(JSON.stringify({ component: data.component_id, ...target }))}">${esc(label)}</button>`;

// Why a component is amber or red, in words under its pill, each reason with a link to the setting that decides it:
// a status alone left a first-time user guessing whether it was requests or exceptions, and against what.
const AI_CAUSE_SETTINGS = { failed: "appinsights.failed_rate_points", exceptions: "appinsights.exceptions_percent",
  steady: "appinsights.critical_rate_percent" };
function aiWhyHtml(c) {
  if (!c.status || !c.why) return "";
  const links = (c.causes || []).filter((cause) => AI_CAUSE_SETTINGS[cause]).map((cause) => settingLink(AI_CAUSE_SETTINGS[cause], "change"));
  return `<div class="ai-why">${esc(c.why)}${links.length ? ` ${links.join(" ")}` : ""}</div>`;
}

// The last hour from metrics, as Investigate's start list shows it.
function aiNowHtml(data) {
  const c = data.now;
  if (!c) return "";
  const row = { ...c, measured: true };
  return `<div class="ai-now">${c.status ? `${pillHtml(c.status, c.status, { title: c.why || "" })}${aiWhyHtml(c)}` : ""}
    <span><span class="muted">Last hour</span> ${invLastHour(row)}</span>
    <span><span class="muted">5xx</span> ${invImpact(row)}</span>
    <span title="Failed requests per hour, last 24 h">${sparkHtml(c.trend, "Failed requests per hour, last 24 h")}</span>
    <span><span class="muted">Same hour last week</span> ${invWeek(row)}</span>
    <span><span class="muted">Exceptions</span> <span class="${(c.causes || []).includes("exceptions") ? "worse" : ""}">${fmt(c.exceptions)}</span>
      <span class="muted">(last week ${fmt(c.week.exceptions)})</span></span>
    <span class="spacer"></span>${aiInvestigate(data, {}, "Investigate this app", "Investigate: its failing endpoints, exceptions, dependencies and recent changes")}</div>`;
}

async function tabAppInsights(node, body) {
  const win = aiWindow();
  body.innerHTML = loading("Querying Application Insights…");
  const params = { window: win };
  const pick = node.type === AI_TYPE ? "" : aiPicks()[node.id.toLowerCase()];
  if (pick) params.component = pick;
  const data = await exploreGet("appinsights", node, params);
  const relink = () => {
    const select = $("ai-pick"), unpick = $("ai-unpick");
    const again = () => tabAppInsights(node, body).catch((error) => { body.innerHTML = errorHtml(error); });
    if (select) select.addEventListener("change", () => { if (select.value) { setAiPick(node.id, select.value); again(); } });
    if (unpick) unpick.addEventListener("click", () => { setAiPick(node.id, ""); again(); });
  };
  if (!data.available) {
    body.innerHTML = aiLinkHtml(data.link);
    relink();
    return;
  }
  const span = `${new Date(data.from).toLocaleString()} → ${new Date(data.to).toLocaleTimeString()}`;
  body.innerHTML = `${data.link ? aiLinkHtml(data.link) : ""}
    <div class="ai-head">
      <label title="${esc(AI_KEEP_NOTE)}">Last <select id="ai-window">${AI_WINDOWS.map((w) => `<option ${w === win ? "selected" : ""}>${w}</option>`).join("")}</select></label>
      <span class="muted">${esc(span)}, compared with the same ${esc(win)} a week ago</span>
    </div>
    ${noticesHtml(data.errors)}
    ${aiNowHtml(data)}
    <h4>Requests by role</h4>${table(["Role", "Requests", "Failed", "p95", "Same window last week"], data.roles.map((r) => [
      esc(r.role || "(no role)"), fmt(r.total), `<span class="${failingMore(r) ? "worse" : ""}">${fmt(r.failed)} (${pctText(r.failed_pct, r.failed)})</span>`,
      msText(r.p95), roleCompare(r)]))}
    <h4>Failing operations</h4>${table(["Operation", "Role", "Failed", "Out of", "Codes", "Last", ""], data.operations.map((r) => [
      esc(r.operation || "–"), esc(r.role || ""), `${fmt(r.failed)} <span class="muted">(${pctText(r.failed_pct, r.failed)})</span>`, fmt(r.total),
      codes(r.codes), seen(r.last_seen), aiInvestigate(data, { operation: r.operation, role: r.role }, "Investigate",
        "Its sample requests, one end to end, and the pod that served it")]))}
    <h4>Exceptions</h4>${table(["Exception", "Operation", "Role", "Count", "Last", ""], data.exceptions.map((r) => [
      `<strong>${esc(r.type)}</strong><div class="ai-msg muted">${esc((r.message || "").slice(0, 300))}</div>`, esc(r.operation || ""),
      esc(r.role || ""), fmt(r.total), seen(r.last_seen),
      r.trace_id ? aiInvestigate(data, { trace: r.trace_id, at: r.last_seen, instance: r.instance }, "Follow", "The latest one's request end to end, in Investigate") : ""]))}
    <h4>Failing dependencies</h4>${table(["Type", "Target", "Role", "Failed", "Codes", "p95", "Last", ""], data.dependencies.map((r) => [
      esc(r.type), `<span class="ai-msg">${esc(r.target)}</span>`, esc(r.role || ""), `${fmt(r.failed)} of ${fmt(r.total)}`,
      codes(r.codes), msText(r.p95_ms), seen(r.last_seen),
      r.trace_id ? aiInvestigate(data, { trace: r.trace_id }, "Follow", "A request with one of these failed calls, in Investigate") : ""]))}
    <h4>Availability tests</h4>${data.availability.length ? table(["Test", "Location", "Runs", "Failed", "Last"], data.availability.map((r) => [
      esc(r.test), esc(r.location), fmt(r.runs), r.failed ? `<span class="worse">${fmt(r.failed)}</span>` : "0", seen(r.lastSeen)]))
      : '<p class="muted">No availability test ran in this window.</p>'}
    <details class="mon-query"><summary>Queries sent</summary>${data.queries.map((q) =>
      `<div class="muted">${esc(q.section)}</div><pre>${esc(q.query)}</pre>`).join("")}</details>`;
  relink();
  $("ai-window").addEventListener("change", (event) => {
    stored.set("ahc.aiWindow", event.target.value);
    tabAppInsights(node, body).catch((error) => { body.innerHTML = errorHtml(error); });
  });
}

