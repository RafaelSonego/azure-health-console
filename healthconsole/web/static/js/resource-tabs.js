"use strict";

function noticesHtml(texts) {
  return (texts || []).map(noticeHtml).join("");
}

async function exploreGet(view, node, params = {}) {
  const key = `${node.id}|${view}|${JSON.stringify(params)}`;
  if (!exploreCache.has(key)) {
    exploreCache.set(key, api(`api/explore/${view}`, { params: { id: node.id, ...params } })
      .catch((error) => { exploreCache.delete(key); throw error; }));
  }
  return exploreCache.get(key);
}

function showDetail(nodeId, tab = "overview", { push = false } = {}) {
  const node = state.snapshot.nodes.find((n) => n.id === nodeId);
  if (!node) return;
  state.exploreNode = node;
  const portal = `https://portal.azure.com/#@${state.snapshot.tenant_id}/resource${node.id}`;
  const tabs = personaTabs(allTabs());
  if (tab === "activity") tab = "timeline";  // the Activity tab became the Timeline
  if (!tabs.some(([key]) => key === tab)) tab = "overview";
  const visible = node.readable ? tabs.filter(([, , when]) => !when || when(node)) : [TABS[0]];
  if (!visible.some(([key]) => key === tab)) tab = "overview";
  $("detail").innerHTML = `
    <div class="explore-head">
      <div><h3>${esc(node.name)} ${pill(node.status, whyStatus(node))}</h3>
        <div class="muted">${esc(node.display_type)} · ${esc(node.resource_group)} · ${esc(node.location)}</div></div>
      <div class="explore-links"><button type="button" class="link" id="tab-refresh" title="Read this tab again from Azure">Refresh</button>
        · <a href="${safeHref(portal)}" target="_blank" rel="noopener">Open in portal</a>
        ${node.id !== state.snapshot.root_id && node.readable ? ' · <a href="#" id="focus-node">Check from here</a>' : ""}
        ${watchButton({ id: node.id, name: node.name, kind: "resource" })}</div>
    </div>
    <nav class="tabs">${tabGroupsHtml(visible, tab)}</nav>
    <div id="tab-body" class="tab-body"></div>`;
  $("detail").classList.remove("hidden");
  const focus = $("focus-node");
  if (focus) focus.addEventListener("click", (event) => {
    event.preventDefault();
    startSnapshot({ kind: "resource", id: node.id, name: node.name });
  });
  $("tab-refresh").addEventListener("click", () => {
    for (const key of [...exploreCache.keys()]) if (key.startsWith(`${node.id}|`)) exploreCache.delete(key);
    freshly(() => renderTab(node, state.detailTab));
  });
  $("detail").querySelector(".tabs").addEventListener("click", (event) => {
    const key = event.target.closest("[data-tab]")?.dataset.tab;
    if (key) showDetail(node.id, key, { push: true });
  });
  for (const [key, badge] of Object.entries((state.tabBadges || {})[node.id] || {})) {
    const button = $("detail").querySelector(`.tabs [data-tab="${key}"]`);
    if (button) button.insertAdjacentHTML("beforeend", tabBadgeHtml(badge));
  }
  if (state.detailNode !== node.id) Object.assign(state, { aksNsPick: null, aksSystem: false, aksCiHours: "" });  // a resource opens unfiltered
  state.detailNode = node.id;
  state.detailTab = tab;
  navRecord(push);
  renderTab(node, tab);
  checkDiagnostics(node);
}

async function renderTab(node, tab) {
  const body = $("tab-body");
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  const renderers = { overview: tabOverview, metrics: tabMetrics, details: tabDetails, scaling: tabScaling,
    diagnostics: tabDiagnostics, timeline: tabTimeline, recommendations: tabRecommendations, vulnerabilities: tabVulnerabilities, workloads: tabWorkloads, nodes: tabNodes,
    kubectl: async (n, b) => { b.innerHTML = await tabKubectl(n); wireAksParts(b); },
    servicenow: tabServiceNow, appinsights: tabAppInsights, cost: tabCost };
  body.innerHTML = loading("Loading…");
  try {
    await (tab.startsWith("tool-") ? tabTool(node, body, tab.slice(5)) : renderers[tab](node, body));
    fillPimHints(body);
  } catch (error) {
    const refused = error.payload && error.payload.kind === "access";
    if (refused) noteBlocked(node.id, node.name, error.payload.required);
    body.innerHTML = errorHtml(error) + (refused ? pimSlot(node.id, error.payload.required) : "");
    fillPimHints(body);
  }
}

// A signal's or event's links (alert_detail.links): the portal page of a resource or an alert, or a portal URL as given.
// A portal link without a directory opens in the browser's last one: with two tenants signed in, PIM could open in the other.
function tenantPortal(url) {
  const tenant = state.tenant || (state.snapshot && state.snapshot.tenant_id) || "";
  return tenant && /^https:\/\/portal\.azure\.com\/#(?!@)/.test(url || "") ? url.replace("https://portal.azure.com/#", `https://portal.azure.com/#@${tenant}/`) : url;
}

function portalUrl(link) {
  const tenant = state.tenant || (state.snapshot && state.snapshot.tenant_id) || "";
  if (link.resource) return `https://portal.azure.com/#@${tenant}/resource${link.resource}`;
  if (link.health) return `https://portal.azure.com/#@${tenant}/blade/Microsoft_Azure_Health/AzureHealthBrowseBlade/${encodeURIComponent(link.health)}`;
  if (link.alert) return `https://portal.azure.com/#@${tenant}/blade/Microsoft_Azure_Monitoring_Alerts/AlertDetails.ReactView/alertId/${encodeURIComponent(link.alert)}`;
  return link.url || "";
}

// Rows and cards open only from a control in them, never from a stray click (selecting text, misclicks).
const browseButtonHtml = (label = "Open in Browse") => `<button type="button" class="link open-browse" data-go>${esc(label)}</button>`;
document.addEventListener("click", (event) => {
  if (event.target.closest("tr[data-open], li[data-open], li[data-show-view]")
    && !event.target.closest("button, a, input, select, label, summary")) event.stopPropagation();
}, true);

// "↗" to a resource (or one of its pages) in the Azure portal, in a new tab; clicking it never opens the row it sits in.
function portalLinkHtml(id, { label = "↗", page = "", title = "Open in the Azure portal" } = {}) {
  if (!id || !id.startsWith("/subscriptions/")) return "";
  return `<a class="portal-link" href="${safeHref(portalUrl({ resource: id }) + (page ? `/${page}` : ""))}" target="_blank" rel="noopener"
    title="${esc(title)}">${esc(label)}</a>`;
}
document.addEventListener("click", (event) => { if (event.target.closest && event.target.closest(".portal-link")) event.stopPropagation(); }, true);

// " · Open <tool> ↗" after a tool tab's header line, to the connection's own home (instance or console).
const toolLink = (url, label) => (url ? ` · ${linksHtml([{ url, label: `Open ${label || "it"}` }])}` : "");

function linksHtml(links) {
  return (links || []).map((l) => `<a href="${safeHref(portalUrl(l))}" target="_blank" rel="noopener">${esc(l.label)} ↗</a>`).join(" · ");
}

// "Today 12:02", "Yesterday 18:30", "Mon 9 Nov 08:00": a time with its day, for lists that span days.
function dayTime(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return String(iso || "");
  const clockText = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(when).setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return `Today ${clockText}`;
  if (days === 1) return `Yesterday ${clockText}`;
  return `${when.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} ${clockText}`;
}

const SIGNAL_ORDER = { critical: 0, warning: 1, healthy: 2, info: 3, unknown: 4 };

function attentionRow(sig) {
  return `<div class="attn-row">${pill(sig.status)}<div class="attn-text"><strong>${esc(sig.name)}</strong>
      ${sig.value != null ? ` ${fmt(sig.value)} ${esc(sig.unit)}${sig.baseline != null ? ` <span class="muted">(last week ${fmt(sig.baseline)})</span>` : ""}` : ""}
      ${sig.at ? ` <span class="muted" title="${esc(new Date(sig.at).toLocaleString())}">· ${esc(dayTime(sig.at))}</span>` : ""}
      ${sig.detail ? `<div class="muted">${escTimes(sig.detail)}</div>` : ""}
      ${(sig.links || []).length ? `<div class="attn-links">${linksHtml(sig.links)}${alertCheckFor(sig)}</div>` : ""}</div></div>`;
}

// The check for an Azure alert signal: its fired alert's ID is among its links.
function alertCheckFor(sig) {
  const fired = (sig.links || []).find((l) => l.alert);
  return fired ? ` · ${alertCheckButton({ source: "azure", id: fired.alert })}` : "";
}

// The resource's Defender totals under its settings, with the tab that lists them (the same rows Summary counts).
function overviewVulnsHtml(v) {
  if (!v) return "";
  if (v.error) return `<p class="muted">Vulnerabilities weren't read: ${esc(v.error)}</p>`;
  if (!v.findings) return '<p class="muted">No open vulnerability findings from Defender.</p>';
  return `<p>${pill(v.high ? "warning" : "info")} <strong>${v.findings.toLocaleString()} open vulnerability finding(s)</strong>
    <span class="muted">${v.high.toLocaleString()} high, worst CVSS ${esc(String(v.maxCvss ?? "–"))}${v.running ? `, ${v.running.toLocaleString()} in running containers` : ""}${
    v.findings ? `, ${Math.round((100 * v.fixable) / v.findings)}% with a fix` : ""}.</span>
    <button type="button" class="link" data-goto-tab="vulnerabilities">See them on the Vulnerabilities tab</button></p>`;
}

// Overview's Security card: the same checks as Security → Exposure & configuration, for this one resource.
async function renderOverviewSecurity(node) {
  const slot = $("ov-security");
  try {
    const data = await exploreGet("security", node);
    if (!slot.isConnected) return;
    const problems = data.findings.filter((f) => f.severity !== "restricted");
    const fenced = data.findings.filter((f) => f.severity === "restricted");
    slot.innerHTML = `${noticesHtml(data.errors)}${problems.length ? `<ul class="sec-findings">${problems.map((f) =>
        `<li><span class="sec-dot ${esc(f.severity)}" title="${esc(f.severity)}"></span>${esc(f.what)}
          <span class="muted">· ${esc(data.categories[f.category] || f.category)}</span>${f.action ? `<div class="muted">${esc(f.action)}</div>` : ""}</li>`).join("")}</ul>`
      : `<p class="ok">✓ Nothing in its settings${data.errors.length ? " that could be read" : ""}.</p>`}
      ${overviewVulnsHtml(data.vulnerabilities)}
      ${fenced.map((f) => `<p class="muted">Public endpoint behind a firewall: ${esc(f.what)}.</p>`).join("")}
      <p class="muted">Checked: public access and firewall, keys and local auth, TLS and HTTPS, deletion protection${
        data.queries.length > 1 ? ", and its NSG rules or public IPs" : ""}. <button type="button" class="link" data-goto-security>All resources in Security</button></p>`;
  } catch (error) {
    if (slot.isConnected) slot.innerHTML = noticeHtml(error);
  }
}

// Needs attention's App Insights line: the component's last hour (an app's or gateway's, by the tab's own lookup).
async function renderOverviewAppInsights(node) {
  const slot = $("ov-ai-now");
  const pick = node.type === AI_TYPE ? "" : aiPicks()[node.id.toLowerCase()];
  try {
    const data = await exploreGet("appinsights-now", node, pick ? { component: pick } : {});
    if (!slot.isConnected) return;
    if (data.now && data.now.status && $("ov-ok")) $("ov-ok").remove();
    slot.innerHTML = !data.available ? '<p class="muted">Application Insights: no component found for it (see its Application Insights tab).</p>'
      : data.now ? `<div class="ov-ai-label muted">Application Insights${data.link ? ` · ${esc(data.link.component_name)}` : ""}</div>${aiNowHtml(data)}`
      : noticesHtml(data.errors);
  } catch (error) {
    if (slot.isConnected) slot.innerHTML = noticeHtml(`Application Insights: ${error.message}`);
  }
}

// Roles this resource holds, itself or through the identity only it signs in as: on resources in the picture (the
// dashed arrows) and on whole groups or subscriptions (core/discovery._add_role_links).
function rolesHeldHtml(node) {
  const s = state.snapshot;
  if (!s || !s.nodes) return "";
  const byId = new Map(s.nodes.map((n) => [n.id, n]));
  const via = (s.graph && s.graph.via) || {};
  const holders = [node.id, ...Object.entries(via).filter(([, user]) => user === node.id).map(([id]) => id)];
  const heldBy = (id) => (id === node.id ? "itself" : esc((byId.get(id) || {}).name || id));
  const rows = [
    ...s.edges.filter((e) => e.kind === "role" && holders.includes(e.source))
      .map((e) => [esc(e.label), `resource ${esc((byId.get(e.target) || {}).name || e.target)}`, heldBy(e.source)]),
    ...holders.flatMap((id) => ((byId.get(id) || {}).roles || [])
      .map((r) => [esc(r.role), `${esc(r.scope_kind)} ${esc(r.scope_name)}`, heldBy(id)])),
  ];
  return rows.length ? `<h4>Roles it holds</h4>${table(["Role", "On", "Held by"], rows)}` : "";
}

function tabOverview(node, body) {
  const signals = node.signals.map((sig) => [
    `${esc(sig.name)}<br><span class="muted">${esc(sig.source)}</span>`, pill(sig.status),
    `${sig.value != null ? `${fmt(sig.value)} ${esc(sig.unit)}` : "–"}${sig.baseline != null ? `<br><span class="muted">last wk ${fmt(sig.baseline)}</span>` : ""}`,
    `${esc(sig.threshold)}${sig.threshold ? "<br>" : ""}<span class="muted">${esc(sig.detail)}</span>${(sig.links || []).length ? `<div class="attn-links">${linksHtml(sig.links)}</div>` : ""}`]);
  const isVault = node.type === "microsoft.keyvault/vaults" && node.readable;
  const attention = node.signals.filter((sig) => PROBLEM.has(sig.status))
    .sort((a, b) => (SIGNAL_ORDER[a.status] ?? 5) - (SIGNAL_ORDER[b.status] ?? 5));
  body.innerHTML = `${node.readable ? "" : pimSlot(node.id, (node.signals[0] || {}).detail)}
    <div class="ov-cards">
      <div class="card ov-card ov-wide"><h4>Needs attention</h4>
        ${!node.readable ? `<p class="notice">This resource can't be read: it may no longer exist in Azure (Resource Graph can
            list a deleted resource for a while), or your access doesn't cover it. ${esc((node.signals[0] || {}).detail || "")}</p>`
          : attention.length ? attention.map(attentionRow).join("") + '<div id="ov-external"></div>'
          : `<div id="ov-external"></div><p class="ok" id="ov-ok">✓ Nothing needs attention${node.signals.length ? ` · ${node.signals.length} signal(s) checked, all below` : ": no signals for this type"}.</p>`}
        ${node.readable && [AI_TYPE, SITE_TYPE, APIM_TYPE].includes(node.type) ? `<div id="ov-ai-now">${loading("Reading the last hour from Application Insights…")}</div>` : ""}
        <h5 class="ov-sub">Recent events <span class="muted">last 24 hours, newest first</span></h5>
        <div id="ov-timeline">${loading("Reading the last 24 hours…")}</div></div>
      ${accessHereSlot(node.id)}
      ${node.readable ? `<div class="card ov-card"><h4>Security</h4><div id="ov-security">${loading("Checking its settings and vulnerabilities…")}</div></div>` : ""}
      <div class="card ov-card"><h4>Open elsewhere</h4><div id="ov-elsewhere">${state.snEnabled || toolKinds().length ? loading("Reading ServiceNow and monitoring tools…")
        : '<p class="muted">No ServiceNow or monitoring tool is connected for this tenant (Settings → Integrations).</p>'}</div></div>
    </div>
    ${isVault ? `<div id="vault-expiry">${loading("Reading secret and key expiry dates…")}</div>` : ""}
    ${node.type === AKS_TYPE && node.readable ? `<div class="card aks-one" id="aks-one" data-cluster="${esc(node.id)}">${aksOneHtml(node.id)}</div>` : ""}
    <h4 id="signals">Signals</h4>${table(["Signal", "Status", "Value", "Threshold / detail"], signals)}
    ${rolesHeldHtml(node)}
    <p class="muted ov-id"><code>${esc(node.id)}</code></p>`;
  if (isVault) renderVaultExpiry(node);
  if ($("ov-ai-now")) renderOverviewAppInsights(node);
  if ($("ov-security")) renderOverviewSecurity(node);
  fillAccessHere(body);
  if (node.readable) renderOverviewEvents(node);
  else {
    noteBlocked(node.id, node.name, (node.signals[0] || {}).detail);
    const box = $("ov-timeline");  // nothing reads events for a resource that can't be read: say so instead of waiting
    if (box) box.innerHTML = '<p class="muted">Not read, because the resource itself can\'t be read.</p>';
  }
}

const PROBLEM = new Set(["critical", "warning"]);

// A tab's count, kept per resource ({count, tone, title}); `nodeId` for a read that may end after you moved on.
const tabBadgeHtml = (b) => {
  const { count, tone = "critical", title = "" } = typeof b === "object" && b ? b : { count: b };
  return count ? ` <span class="tab-badge ${esc(tone)}"${title ? ` title="${esc(title)}"` : ""}>${fmt(count)}</span>` : "";
};
function setTabBadge(key, count, tone = "critical", { nodeId = (state.exploreNode || {}).id, title = "" } = {}) {
  if (nodeId) {
    state.tabBadges = state.tabBadges || {};
    (state.tabBadges[nodeId] = state.tabBadges[nodeId] || {})[key] = { count, tone, title };
  }
  if (nodeId && nodeId !== state.detailNode) return;
  const button = $("detail").querySelector(`.tabs [data-tab="${key}"]`);
  if (!button) return;
  const old = button.querySelector(".tab-badge");
  if (old) old.remove();
  button.insertAdjacentHTML("beforeend", tabBadgeHtml({ count, tone, title }));
}

async function renderOverviewEvents(node) {
  const { all, results } = await timelineData(node, "24");
  if (!state.exploreNode || state.exploreNode.id !== node.id) return;
  // What ServiceNow and the monitoring tools have open for it, each with the key its timeline event carries.
  const open = [];
  for (const r of results) {
    if (r.kind === "servicenow" && r.raw) {
      for (const i of r.raw.incidents) open.push({ key: snKey(i), status: i.priority === 1 ? "critical" : i.priority === 2 ? "warning" : "info",
        tool: "ServiceNow", what: "ServiceNow incident", text: `${i.number} ${i.title}`, url: i.url });
      setTabBadge("servicenow", r.raw.incidents.length);
    } else if (r.raw && r.kind !== "azure") {
      for (const i of r.raw.issues) open.push({ key: toolKey(r.kind, i), status: i.priority === "critical" ? "critical" : i.priority === "warning" ? "warning" : "info",
        tool: r.name, what: `${r.name} issue`, text: i.title || i.id, url: i.url,
        check: { source: "monitor", ...toolParams(r.kind), account: i.account_id ?? "", condition: i.condition || "", policy: i.policy || "", priority: i.priority_label || "" } });
      // An entity the tool holds in critical/warning with no matched open issue (Triage's card lists it too).
      // Skipped when an open issue covers it: by guid, or by name (New Relic also lists an issue as an entity).
      const covered = new Set(r.raw.issues.flatMap((i) => i.entity_ids || []));
      const titles = new Set(r.raw.issues.map((i) => (i.title || "").toLowerCase()));
      const alerting = (r.raw.entities || []).filter((e) => PROBLEM.has(e.severity) && !titles.has((e.name || "").toLowerCase())
        && !(e.ids || [e.id]).some((id) => covered.has(id)));
      for (const e of alerting) open.push({ key: `${r.kind}:entity:${e.id}`, status: e.severity, tool: r.name,
        what: `${r.name} entity in ${e.severity}${e.type ? ` · ${e.type}` : ""}`, text: e.name, url: e.url });
      setTabBadge(`tool-${r.kind}`, r.raw.issues.length + alerting.length);
    }
  }
  // Each item is shown once: what another tool has open and is urgent goes in Needs attention (the page never says
  // "nothing" while one is open), and the lists below leave out what's already there.
  const external = $("ov-external");
  const urgent = external ? open.filter((o) => PROBLEM.has(o.status)) : [];
  const shown = new Set(urgent.map((o) => o.key));
  if (urgent.length) {
    external.innerHTML = urgent.map((o) => `<div class="attn-row">${pill(o.status)}<div class="attn-text"><strong>${o.url ? monLink(o.url, o.text) : esc(o.text)}</strong>
        <div class="muted">${esc(o.what)}</div>${o.check ? `<div class="attn-links">${alertCheckButton(o.check)}</div>` : ""}</div></div>`).join("");
    const ok = $("ov-ok");
    if (ok) ok.outerHTML = '<p class="muted">Azure\'s own signals: nothing needs attention.</p>';
  }
  const box = $("ov-timeline");
  if (box) {
    const listed = new Set(node.signals.filter((sig) => sig.source === "alert" && PROBLEM.has(sig.status)).map((sig) => sig.name));
    const recent = all.filter((e) => !(e.source === "Alert" && listed.has(e.name)) && !(e.key && shown.has(e.key)));
    box.innerHTML = recent.length ? `${recent.slice(0, 6).map((e) => `<div class="ov-row"><span class="ov-time" title="${esc(new Date(e.at).toLocaleString())}">${esc(dayTime(e.at))}</span>
        ${pill(e.status)} <span class="ov-text"><span class="muted">${esc(e.source)}</span> ${e.url ? monLink(e.url, e.title) : esc(e.title)}</span></div>`).join("")}
        <button type="button" class="link ov-more" data-goto-tab="timeline">Open the timeline (${all.length})</button>`
      : `<p class="muted">${all.length ? "Nothing else in the last 24 hours." : "Nothing in the last 24 hours."}</p>${all.length ? `<button type="button" class="link ov-more" data-goto-tab="timeline">Open the timeline (${all.length})</button>` : ""}`;
  }
  const elsewhere = $("ov-elsewhere");
  if (elsewhere && (state.snEnabled || toolKinds().length)) {
    const unavailable = results.filter((r) => r.unavailable && r.name !== "Azure");
    const rest = open.filter((o) => !shown.has(o.key));
    const above = Object.entries(urgent.reduce((by, o) => ({ ...by, [o.tool]: [...(by[o.tool] || []), o] }), {})).map(([tool, items]) => {
      const counts = ["critical", "warning"].map((s) => [s, items.filter((o) => o.status === s).length]).filter(([, n]) => n);
      return `<p class="muted">${esc(tool)}: ${counts.map(([s, n]) => `${n} ${s}`).join(", ")}, shown in Needs attention.</p>`;
    }).join("");
    elsewhere.innerHTML = above + (rest.length ? rest.slice(0, 5).map((o) => `<div class="ov-row">${pill(o.status)} <span><span class="muted">${esc(o.tool)}</span>
        ${o.url ? monLink(o.url, o.text) : esc(o.text)}</span></div>`).join("")
      : above ? '<p class="ok">✓ Nothing else open that names this resource.</p>' : '<p class="ok">✓ Nothing open that names this resource.</p>')
      + unavailable.map((r) => `<p class="muted">${esc(r.name)}: ${esc(r.unavailable)}</p>`).join("");
  }
  setTabBadge("timeline", all.filter((e) => PROBLEM.has(e.status)).length);
}

$("detail").addEventListener("click", (event) => {
  const go = event.target.closest("[data-goto-tab]");
  if (go && state.exploreNode) { event.preventDefault(); showDetail(state.exploreNode.id, go.dataset.gotoTab); }
});

// Key Vaults lead with the list people open them for: every secret and key, expired first.
async function renderVaultExpiry(node) {
  const box = () => $("vault-expiry");
  try {
    const data = await exploreGet("details", node);
    if (!box()) return;
    const sections = data.sections.filter((s) => /^(Certificates|Secrets|Keys) \(/.test(s.title));
    box().innerHTML = sections.map((s) => {
      const rows = s.facts.filter((f) => f.status);
      return `<h4>${esc(s.title)}</h4>${s.note ? `<p class="muted">${esc(s.note)}</p>` : ""}
        ${rows.length ? table(["Status", "Name", "Expiry"], rows.map((f) => [pill(f.status), esc(f.label), esc(f.value)]))
          : `<p class="muted">${esc((s.facts[0] || {}).value || "None.")}</p>`}`;
    }).join("") + noticesHtml(data.errors);
  } catch (error) {
    if (box()) box().innerHTML = noticeHtml(error);
  }
}

function factsHtml(section) {
  if (section.table) {
    return table(section.table.columns, section.table.rows.map((r) => r.map((cell) => esc(fmt(cell)))));
  }
  return table(["", "", ""], section.facts.map((f) => [esc(f.label), esc(fmt(f.value)), f.status ? pill(f.status) : ""]))
    .replace("<tr><th></th><th></th><th></th></tr>", "");
}

async function tabDetails(node, body) {
  const data = await exploreGet("details", node);
  body.innerHTML = data.sections.map((s) => `<h4>${esc(s.title)}</h4>${s.note ? `<p class="muted">${esc(s.note)}</p>` : ""}${factsHtml(s)}`).join("")
    + noticesHtml(data.errors);
}

async function tabScaling(node, body) {
  const data = await exploreGet("scaling", node);
  body.innerHTML = (data.note ? `<p class="muted">${esc(data.note)}</p>` : "")
    + table(["Kind", "Name", "Current", "Min", "Max", "Status", "Detail"], data.items.map((i) => [
      esc(i.kind), esc(i.name), fmt(i.current), fmt(i.min), fmt(i.max), pill(i.status), esc(i.detail)]))
    + (data.items.length ? "" : '<p class="muted">No autoscale settings or node pools for this resource.</p>')
    + noticesHtml(data.errors);
}

// Azure's detector text is Markdown with some HTML (tables). It's rebuilt from plain text only:
// tables cell by cell, Markdown with everything escaped, links only when http(s). Azure's markup never reaches the page.
const MARK = "\uE000";  // placeholder for a rebuilt table; private-use, so HTML parsing leaves it alone

function azureText(raw) {
  const blocks = [];
  let text = String(raw || "").replace(/<\/?markdown>/gi, "");
  text = text.replace(/<table[\s\S]*?<\/table>/gi, (html) => {
    blocks.push(safeTable(html));
    return `\n${MARK}${blocks.length - 1}${MARK}\n`;
  });
  text = text.replace(/<br\s*\/?>/gi, "\n").replace(/<\/(p|div|li|h\d|tr)>/gi, "\n").replace(/<[^>]+>/g, "");
  return markdownToHtml(decodeEntities(text)).replace(new RegExp(`${MARK}(\\d+)${MARK}`, "g"), (_, i) => blocks[Number(i)]);
}

function decodeEntities(text) {
  const area = document.createElement("textarea");
  area.innerHTML = text;  // a textarea's content is plain text: nothing in it runs
  return area.value;
}

function safeTable(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");  // parsing only; scripts never run
  doc.querySelectorAll("br").forEach((br) => br.replaceWith("\n"));  // keep line breaks inside cells
  const rows = [...doc.querySelectorAll("tr")].map((tr) => [...tr.querySelectorAll("th,td")].map((c) => c.textContent.trim()));
  if (!rows.length) return "";
  const [head, ...body] = doc.querySelector("th") ? rows : [[], ...rows];
  return table(head, body.map((r) => r.map((cell) => esc(cell).replace(/\n+/g, "<br>"))));
}

function inlineMarkdown(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => `<a href="${url}" target="_blank" rel="noopener">${label}</a>`);
}

function markdownToHtml(text) {
  const out = [];
  const lines = text.split(/\r?\n/);
  let paragraph = [];
  let list = null;
  const flush = () => {
    if (paragraph.length) out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
    if (list) out.push(`<ul>${list.map((li) => `<li>${inlineMarkdown(li)}</li>`).join("")}</ul>`);
    paragraph = [];
    list = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.startsWith("```")) {
      flush();
      const code = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith("```"); i++) code.push(lines[i]);
      out.push(`<pre class="code">${esc(code.join("\n").trim())}</pre>`);
      continue;
    }
    if (new RegExp(`^${MARK}\\d+${MARK}$`).test(line)) { flush(); out.push(line); continue; }
    if (!line) { flush(); continue; }
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) { flush(); out.push(`<h5>${inlineMarkdown(heading[1])}</h5>`); continue; }
    const bullet = /^(?:[-*]|\d+\.)\s+(.*)$/.exec(line);
    if (bullet) {
      if (paragraph.length) { out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`); paragraph = []; }
      (list = list || []).push(bullet[1]);
      continue;
    }
    if (list) flush();
    paragraph.push(line);
  }
  flush();
  return out.join("");
}

// Microsoft's troubleshooting catalogue (all reads): service → problem type → its self-help guides, shown here, and
// its solutions and checks, which only the portal opens.
async function helpGuides(node, box, portalLink) {
  box.innerHTML = loading("Listing Microsoft's troubleshooting guides…");
  const { services } = await exploreGet("guides", node);
  if (!services.length) { box.innerHTML = `<p class="muted">Microsoft lists no troubleshooting guides for this type.</p>`; return; }
  const serviceSelect = services.length > 1
    ? `<label class="inline">Service <select id="hg-service"><option value="">Pick one…</option>
        ${services.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select></label>` : "";
  box.innerHTML = `<h4>Troubleshooting guides</h4>
    <p class="muted">Microsoft's own guides for ${esc(services.length > 1 ? "this type" : services[0].name)}, by problem type,
      as the portal's form lists them.</p>
    ${serviceSelect}<span id="hg-problems"></span><div id="hg-solutions"></div>`;
  const problemsBox = box.querySelector("#hg-problems");
  const solutionsBox = box.querySelector("#hg-solutions");
  const showProblems = async (serviceId) => {
    solutionsBox.innerHTML = "";
    if (!serviceId) { problemsBox.innerHTML = ""; return; }
    problemsBox.innerHTML = loading("Listing problem types…");
    const { problems } = await exploreGet("guides", node, { service: serviceId });
    const groups = [...new Set(problems.map((p) => p.group))];
    problemsBox.innerHTML = `<label class="inline">Problem type <select id="hg-problem"><option value="">Pick one…</option>
      ${groups.map((g) => `<optgroup label="${esc(g)}">${problems.filter((p) => p.group === g)
        .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}</optgroup>`).join("")}</select></label>`;
    problemsBox.querySelector("#hg-problem").addEventListener("change", (event) =>
      showSolutions(event.target.value).catch((error) => { solutionsBox.innerHTML = errorHtml(error); }));
  };
  const showSolutions = async (problemId) => {
    if (!problemId) { solutionsBox.innerHTML = ""; return; }
    solutionsBox.innerHTML = loading("Asking Microsoft what it offers…");
    const { solutions } = await exploreGet("guides", node, { problem: problemId });
    const guides = solutions.filter((s) => s.kind === "guide");
    const checks = solutions.filter((s) => s.kind === "portal");
    solutionsBox.innerHTML = (solutions.length ? "" : `<p class="muted">Microsoft offers nothing for this problem type.</p>`)
      + guides.map((g) => `<details class="more" data-guide="${esc(g.id)}"><summary>${esc(g.description || g.id)}</summary>
          <div class="md">${loading("Reading the guide…")}</div></details>`).join("")
      + (checks.length ? `<h5>Only in the portal</h5>
          <p class="muted">Microsoft opens these solutions and checks by creating a Help resource (a write), so the console
            lists them: ${portalLink}.</p>
          <ul>${checks.map((c) => `<li>${esc(c.description || c.id)}</li>`).join("")}</ul>` : "");
    solutionsBox.querySelectorAll("details[data-guide]").forEach((item) => item.addEventListener("toggle", async () => {
      if (!item.open || item.dataset.read) return;
      item.dataset.read = "1";
      const text = item.querySelector(".md");
      try {
        const guide = await exploreGet("guides", node, { guide: item.dataset.guide });
        text.innerHTML = guide.text ? azureText(guide.text) : `<p class="muted">This guide has no text; the portal shows it.</p>`;
      } catch (error) { text.innerHTML = errorHtml(error); delete item.dataset.read; }
    }));
  };
  const serviceBox = box.querySelector("#hg-service");
  if (serviceBox) serviceBox.addEventListener("change", (event) =>
    showProblems(event.target.value).catch((error) => { problemsBox.innerHTML = errorHtml(error); }));
  else await showProblems(services[0].id);
}

const DETECTORS_AT_ONCE = 6;
const FLAGGED = new Set(["critical", "warning"]);

// The Diagnostics tab's count: what Azure's detectors flag (critical or warning, last 24 h).
function diagnosticsBadge(nodeId, results) {
  const flagged = results.filter((r) => FLAGGED.has(r.status));
  setTabBadge("diagnostics", flagged.length, flagged.some((r) => r.status === "critical") ? "critical" : "warning",
    { nodeId, title: `${flagged.length} of Azure's detectors report a warning or critical finding in the last 24 hours` });
}

// Opening a resource runs its default detectors in the background (the tab's own reads, so the tab then opens at once)
// and counts what they flag on the Diagnostics tab. Once per resource per session; types without detectors stop at the list.
async function checkDiagnostics(node) {
  state.diagChecked = state.diagChecked || new Set();
  if (state.diagChecked.has(node.id) || !$("detail").querySelector('.tabs [data-tab="diagnostics"]')) return;
  state.diagChecked.add(node.id);
  try {
    const plan = await exploreGet("diagnostics", node, { plan: "1" });
    if (plan.error || !plan.supported) return;
    const queue = [...plan.detectors], results = [];
    const worker = async () => {
      while (queue.length) {
        const d = queue.shift();
        results.push(await exploreGet("diagnostics", node, { detector: d.name }).catch(() => ({ status: "unknown" })));
      }
    };
    await Promise.all(Array.from({ length: 3 }, worker));  // fewer at once than the tab: it runs behind what you're reading
    diagnosticsBadge(node.id, results);
  } catch (error) {
    state.diagChecked.delete(node.id);  // a failed list is tried again on the next visit
  }
}

// The list first (one read), then each detector on its own, six at a time, drawn as it answers: one can take 25 s.
async function tabDiagnostics(node, body, all = false) {
  body.innerHTML = loading("Listing Azure's detectors…");
  const plan = await exploreGet("diagnostics", node, { plan: "1", ...(all ? { all: "1" } : {}) });
  if (plan.error) { body.innerHTML = noticeHtml(plan.error); return; }
  const portal = `https://portal.azure.com/#@${state.snapshot.tenant_id}/resource${node.id}/troubleshoot`;
  const portalLink = `<a href="${safeHref(portal)}" target="_blank" rel="noopener">Diagnose and solve problems in the portal</a>`;
  if (!plan.supported) {
    body.innerHTML = (plan.unavailable
      ? `<p class="notice">Azure has a detectors API for this resource type, but ${esc(plan.unavailable)}. The console tries
          again in 15 minutes. Meanwhile, ${portalLink} shows them.</p>`
      : `<p class="muted">Azure's API has no working detectors for this resource type, so the console can't run them. The portal's
          page can still offer checks: for many types it runs them through Microsoft's Help service, which starts a diagnostic
          run (a write), and the console only reads. ${portalLink}.</p>`) + `<div id="help-guides"></div>`;
    helpGuides(node, body.querySelector("#help-guides"), portalLink)
      .catch((error) => { body.querySelector("#help-guides").innerHTML = errorHtml(error); });
    return;
  }
  const results = new Map();
  const ORDER = { critical: 0, warning: 1, info: 2, healthy: 3, unknown: 4 };
  const insight = (i) => `<div class="insight">${pill(i.status)} ${esc(i.text)}
    ${i.details.length ? `<details class="more"><summary>View details</summary>
      ${i.details.map((d) => `<h5>${esc(d.name)}</h5><div class="md">${azureText(d.value)}</div>`).join("")}</details>` : ""}</div>`;
  const dataTable = (t) => `<details class="more"><summary>${esc(t.title)} <span class="muted">(${t.total} row${t.total === 1 ? "" : "s"})</span></summary>
    ${table(t.columns, t.rows.map((r) => r.map((v) => esc(v ?? ""))))}
    ${t.total > t.rows.length ? `<p class="muted">First ${t.rows.length} of ${t.total}; the portal has the rest.</p>` : ""}</details>`;
  const item = (r, open) => `<details class="detector" data-detector="${esc(r.name)}" ${open ? "open" : ""}>
    <summary>${pill(r.status)} ${esc(r.title)} <span class="muted">${esc(r.category)}</span></summary>
    ${r.description ? `<p class="muted">${esc(r.description)}</p>` : ""}
    ${(r.insights || []).length ? r.insights.map(insight).join("") : '<p class="muted">No findings text.</p>'}
    ${(r.notes || []).map((n) => `${n.title ? `<h5>${esc(n.title)}</h5>` : ""}<div class="md">${azureText(n.value)}</div>`).join("")}
    ${(r.tables || []).map(dataTable).join("")}
    ${r.error ? `<p class="muted">${esc(r.error)}</p>` : ""}</details>`;
  const draw = () => {
    if (!body.isConnected) return;
    const opened = new Set([...body.querySelectorAll("details[data-detector][open]")].map((d) => d.dataset.detector));
    const done = [...results.values()].sort((a, b) => (ORDER[a.status] ?? 5) - (ORDER[b.status] ?? 5) || (a.title || "").localeCompare(b.title || ""));
    const flagged = done.filter((r) => FLAGGED.has(r.status));
    const quiet = done.filter((r) => !FLAGGED.has(r.status));
    const waiting = plan.detectors.length - results.size;
    if (!waiting) diagnosticsBadge(node.id, done);
    body.innerHTML = `<p class="muted">These are Azure's own checks, the same as the portal's ${portalLink.replace("Diagnose and solve problems in the portal", "Diagnose and solve problems")}
        (look under the category shown next to each one). They cover the <strong>last 24 hours</strong>, so a finding can be
        something that happened earlier today, not the current state, and the severity is Azure's own rating.</p>
      <p class="muted">${results.size} of ${plan.detectors.length} detectors answered${plan.total > plan.detectors.length ? ` (of ${plan.total} Azure offers${all ? "" : ": health, availability, performance and risk"})` : ""}.
        ${all ? "" : '<button type="button" id="run-all-detectors">Run all</button>'}</p>
      ${waiting ? loading(`${waiting} still answering (Azure's detectors take up to half a minute each)`) : ""}
      ${flagged.length ? flagged.map((r) => item(r, true)).join("") : results.size ? '<p>No detector reports a warning or critical finding so far.</p>' : ""}
      ${quiet.length ? `<details ${opened.has("__quiet") ? "open" : ""} data-detector="__quiet"><summary class="muted">${quiet.length} other detector(s)</summary>${quiet.map((r) => item(r, opened.has(r.name))).join("")}</details>` : ""}`;
    const runAll = $("run-all-detectors");
    if (runAll) runAll.addEventListener("click", () => tabDiagnostics(node, body, true).catch((e) => { body.innerHTML = errorHtml(e); }));
  };
  draw();
  const queue = [...plan.detectors];
  const worker = async () => {
    while (queue.length && body.isConnected) {
      const d = queue.shift();
      try { results.set(d.name, await exploreGet("diagnostics", node, { detector: d.name })); }
      catch (error) { results.set(d.name, { ...d, status: "unknown", insights: [], error: error.message }); }
      draw();
    }
  };
  await Promise.all(Array.from({ length: DETECTORS_AT_ONCE }, worker));
}

