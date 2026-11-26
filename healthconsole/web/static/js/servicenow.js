"use strict";

// ------------------------------------------------------------------ ServiceNow
// Active incidents, changes now or in the next 24 hours, and Event Management alerts and events, each with the
// Azure resources it names. Shown only when ServiceNow is turned on; when it can't be read, the page says why.

const SN_PRIORITY_TONE = { 1: "critical", 2: "soon", 3: "warning" };
const SN_CONFIDENCE = { exact: "exact match (Azure ID)", name: "matched by name", possible: "possible match (short or common name)" };

function snPill(record) {
  const tone = SN_PRIORITY_TONE[record.priority] || "info";
  return pillHtml(tone, `P${record.priority}`, { rank: record.priority, title: record.priority_label });
}

// "3 h 2 min ago" with the exact time on hover; a list sorts it by the time itself.
function agoHtml(when) {
  if (when == null || when === "") return "–";
  const epoch = typeof when === "number" ? when : new Date(when).getTime() / 1000;
  if (Number.isNaN(epoch)) return esc(when);
  return `<span data-v="${epoch}" title="${esc(new Date(epoch * 1000).toLocaleString())}">${esc(ago(when))}</span>`;
}

// `when` is an ISO string (Azure, ServiceNow) or epoch seconds (the alert runner, on the server's clock).
function ago(when) {
  if (when == null || when === "") return "";
  const epoch = typeof when === "number" ? when : new Date(when).getTime() / 1000;
  if (Number.isNaN(epoch)) return String(when);
  const seconds = serverNow() - epoch;
  if (seconds < 0) return `in ${duration(-seconds)}`;
  if (seconds < 45) return "just now";
  if (seconds < 48 * 3600) return `${duration(seconds)} ago`;
  return new Date(epoch * 1000).toLocaleDateString();
}

const MATCH_VIA = { "configuration item": "from its CI", "affected CI": "from an affected CI", "change task": "from a change task" };

function resourceChips(matches, limit = 4) {
  if (!matches.length) return '<span class="muted">none found</span>';
  const chips = matches.slice(0, limit).map((m) => {
    const isGroup = m.display_type === "Resource group";
    const open = isGroup ? { kind: "group", group: `${m.subscription_id}/${m.name}`, name: m.name }
      : { kind: "resource", id: m.id, name: m.name };
    return `<button type="button" class="sn-res ${esc(m.confidence)}" data-open="${esc(JSON.stringify(open))}"
      title="${esc(`${m.display_type} in ${m.resource_group} · ${SN_CONFIDENCE[m.confidence]}${MATCH_VIA[m.via] ? `, ${MATCH_VIA[m.via]}` : ""}`)}">${esc(m.name)}</button>`;
  }).join("");
  return chips + (matches.length > limit ? ` <span class="muted">+${matches.length - limit}</span>` : "");
}

function snUnavailable(info) {
  return `<div class="notice partial"><strong>${esc(info.reason)}</strong>
    ${(info.problems || []).length ? `<ul>${info.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    ${info.fix ? `<p>${esc(info.fix)} <button type="button" class="link" data-goto="settings">Open Settings</button></p>` : ""}</div>`;
}

async function loadServiceNow(refresh = false) {
  const scope = currentScope();
  const token = (state.snToken = (state.snToken || 0) + 1);
  state.snLoaded = true;
  if (!scope.subscriptions.length) {
    $("sn-body").innerHTML = pickScopeHtml("ServiceNow shows the incidents, changes and alerts for the resources in scope.");
    return;
  }
  $("sn-body").innerHTML = loading("Reading ServiceNow and matching records to Azure resources…");
  try {
    const params = { ...scopeParams(scope), connection: state.selected.servicenow || "" };
    if (refresh) params.refresh = "1";
    const data = await api("api/servicenow", { params });
    if (token !== state.snToken) return;
    state.sn = data;
    renderServiceNow();
  } catch (error) {
    if (token === state.snToken && !error.signedOut) $("sn-body").innerHTML = noticeHtml(error);
  }
}

// "Names a resource in scope" means an exact or name match; a possible match (a short or plain name) can be chance.
const namesResource = (r) => r.matches.some((m) => m.confidence !== "possible");

function snVisible(records) {
  const text = $("sn-filter-text").value.trim().toLowerCase();
  const scopeOnly = $("sn-scope-only").checked;
  return records.filter((r) => (!scopeOnly || namesResource(r))
    && (!text || [r.number, r.title, r.description, r.group, r.assigned_to, r.ci, ...r.matches.map((m) => m.name)]
      .some((v) => (v || "").toLowerCase().includes(text))));
}

// A header click sorts (the shared list helper); its own order is ServiceNow's: priority, then most recently updated.
function snHead(columns) {
  return `<thead><tr>${columns.map(([, label]) => `<th>${label}</th>`).join("")}</tr></thead>`;
}

// Why a table wasn't read: a warning where its list would be (a ✓ there would claim it was read and empty).
function snNote(key, label) {
  const note = (state.sn.notes || {})[key];
  return note ? noticeHtml(label ? `${label}: ${note}` : note) : "";
}

function renderServiceNow() {
  const data = state.sn;
  if (!data) return;
  if (!data.available) {
    $("sn-meta").textContent = "";
    $("sn-body").innerHTML = snUnavailable(data);
    return;
  }
  const maxPriority = Number($("sn-priority").value || 9);
  const incidents = snVisible(data.incidents).filter((r) => r.priority <= maxPriority);
  const changes = snVisible(data.changes);
  const alerts = snVisible(data.alerts);
  const events = snVisible(data.events);
  const host = data.instance.replace(/^https:\/\//, "");
  const matched = data.incidents.filter(namesResource).length;
  $("sn-meta").innerHTML = `${esc(`${data.name} · ${host} · read ${ago(data.fetched_at)} · ${data.incidents.length} active incident(s), ${matched} naming Azure resources in scope`)}${toolLink(data.instance, "ServiceNow")}`;
  const scopeOnly = $("sn-scope-only").checked;
  const hidden = (all, shown, what) => (scopeOnly && all.length > shown.length
    ? `<p class="muted">${all.length - shown.length} other active ${what} don't name any Azure resource in scope.
        <button type="button" class="link" data-show-all>Show them</button></p>` : "");
  // What a list leaves out: the cap (a setting), and what ServiceNow's read rules hid of what was asked for.
  const truncated = (key, label, setting = "Most records per query") => {
    const total = (data.totals || {})[key], cap = (data.caps || {})[key] || data.limit, hidden = (data.hidden || {})[key];
    const capped = data.truncated[key] ? `Only the first ${cap.toLocaleString()}${total ? ` of ${total.toLocaleString()}` : ""} ${label} were read: `
      + `narrow the filter or raise "${setting}" in Settings → Integrations → ServiceNow → What to read.` : "";
    const hid = hidden ? `ServiceNow's read rules hid ${hidden.toLocaleString()} of ${data.truncated[key] ? `those ${cap.toLocaleString()}`
      : total.toLocaleString()} ${label} from ${data.who}: the list shows only what it can read.` : "";
    return [capped, hid].filter(Boolean).map((t) => noticeHtml(t)).join("");
  };
  const cmdb = (r) => (r.cmdb_note ? `<div class="sn-cmdb" title="The CMDB doesn't map this Azure resource">⚠ CMDB: ${esc(r.cmdb_note)}</div>` : "");
  const link = (r) => `<a href="${safeHref(r.url)}" target="_blank" rel="noopener">${esc(r.number || "open")}</a>`;
  // The same table and query as a ServiceNow list: it runs as you there, so it shows what your own access can read.
  const inSn = (...lists) => {
    const found = lists.filter(([key]) => (data.lists || {})[key]).map(([key, label]) => ({ url: data.lists[key], label }));
    return found.length ? ` <span class="h-links" title="The same query in ServiceNow, read with your own access">${linksHtml(found)}</span>` : "";
  };
  $("sn-body").innerHTML = `
    <h3>Incidents <span class="muted">(${incidents.length})</span>${inSn(["incident", "Open this list in ServiceNow"])}</h3>
    ${snNote("incident", "Incidents")}${truncated("incident", "incidents", "Incidents: most records")}${hidden(data.incidents.filter((r) => r.priority <= maxPriority), incidents, "incident(s)")}
    ${incidents.length ? `<table class="data-table" data-list="sn-incident">${snHead([["priority", "Priority"], ["number", "Number"],
      ["title", "Short description"], ["state", "State"], ["group", "Assignment group"], ["opened_at", "Opened"],
      ["matches", "Azure resources it names"]])}<tbody>${incidents.map((r) => `<tr>
      <td>${snPill(r)}</td><td>${link(r)}</td><td>${esc(r.title)}${cmdb(r)}</td><td>${esc(r.state)}</td>
      <td>${esc(r.group)}${r.assigned_to ? `<div class="muted">${esc(r.assigned_to)}</div>` : ""}</td>
      <td>${agoHtml(r.opened_at)}</td><td>${resourceChips(r.matches)}</td></tr>`).join("")}</tbody></table>`
      : snNote("incident", "") ? "" : `<p class="ok">✓ No active incidents${scopeOnly ? " name an Azure resource in scope" : " match these filters"}.</p>`}
    <h3>Changes: ${esc(snChangeSpan(data))} <span class="muted">(${changes.length})</span>${inSn(["change_request", "Open changes in ServiceNow"], ["change_closed", "Closed ones"])}</h3>
    ${snNote("change_request", "Change requests")}${truncated("change_request", "open changes", "Changes: most records")}
    ${truncated("change_closed", "closed changes", "Changes: most records")}${hidden(data.changes, changes, "change(s)")}
    ${changes.length ? `<table class="data-table" data-list="sn-change">${snHead([["window", "When"], ["number", "Number"],
      ["title", "Short description"], ["state", "State"], ["outcome", "Outcome"], ["start", "Window"], ["group", "Assignment group"],
      ["matches", "Azure resources it names"]])}<tbody>${changes.map((r) => `<tr>
      <td>${pillHtml(SN_CHANGE_TONE[r.window] || "info", r.window)}</td><td>${link(r)}</td>
      <td>${esc(r.title)}${snChangeParts(r) ? `<div class="muted">${esc(snChangeParts(r))}</div>` : ""}${cmdb(r)}</td><td>${esc(r.state)}</td>
      <td>${r.outcome ? `<span class="${/unsuccess/i.test(r.outcome) ? "worse" : ""}">${esc(r.outcome)}</span>` : '<span class="muted">–</span>'}</td>
      <td>${r.start ? `${esc(new Date(r.start).toLocaleString())} → ${esc(r.end ? new Date(r.end).toLocaleString() : "?")}` : '<span class="muted">no dates</span>'}</td>
      <td>${esc(r.group)}</td><td>${resourceChips(r.matches)}</td></tr>`).join("")}</tbody></table>`
      : snNote("change_request", "") ? "" : `<p class="ok">✓ No changes in ${esc(snChangeSpan(data))}.</p>`}
    ${snNote("change_closed", "Closed changes")}${snNote("task_ci", "Affected CIs")}${snNote("change_task", "Change tasks")}
    <h3>Event Management alerts <span class="muted">(${alerts.length})</span>${inSn(["em_alert", "Open this list in ServiceNow"])}</h3>
    ${snNote("em_alert", "Alerts")}
    ${alerts.length ? snEventTable("alert", alerts) : snNote("em_alert", "") ? "" : '<p class="ok">✓ No open alerts.</p>'}
    <h3>Event Management events, last 24 hours <span class="muted">(${events.length})</span>${inSn(["em_event", "Open this list in ServiceNow"])}</h3>
    ${snNote("em_event", "Events")}
    ${events.length ? snEventTable("event", events) : snNote("em_event", "") ? "" : '<p class="ok">✓ No events.</p>'}
    <p class="muted">Matching: an Azure resource ID on the record's configuration item is an exact match; a distinctive resource
      name in ${esc("the record's text")} is a name match; short or common names are only "possible". ⚠ CMDB marks records
      whose configuration item doesn't map the resource they name.</p>`;
}

function snEventTable(table, records) {
  return `<table class="data-table" data-list="sn-${table}">${snHead([["severity", "Severity"], ["number", "Number"], ["title", "Description"],
    ["source", "Source"], ["state", "State"], ["updated_at", "Updated"], ["matches", "Azure resources it names"]])}<tbody>${
    records.map((r) => `<tr>
    <td>${esc(r.severity)}</td><td><a href="${safeHref(r.url)}" target="_blank" rel="noopener">${esc(r.number || "open")}</a></td>
    <td>${esc(r.title)}</td><td>${esc(r.source)}</td><td>${esc(r.state)}</td><td>${agoHtml(r.updated_at)}</td>
    <td>${resourceChips(r.matches)}</td></tr>`).join("")}</tbody></table>`;
}

$("sn-body").addEventListener("click", (event) => {
  if (event.target.dataset && event.target.dataset.showAll !== undefined) {
    $("sn-scope-only").checked = false;
    rememberScopeOnly();
    renderServiceNow();
    return;
  }
  const chip = event.target.closest("[data-open]");
  if (chip) { startSnapshot(JSON.parse(chip.dataset.open)); return; }
  if (event.target.dataset && event.target.dataset.goto) showView(event.target.dataset.goto);
});
$("sn-refresh").addEventListener("click", () => loadServiceNow(true));
for (const id of ["sn-filter-text", "sn-priority", "sn-scope-only"]) $(id).addEventListener("input", debounce(renderServiceNow, 150));

// Scope-only is the default (a real instance holds every team's incidents) and the choice is remembered.
// Collapsed to one line by default; opening or closing it is remembered for every snapshot.
function serviceHealthHtml(events) {
  const open = stored.get("ahc.serviceHealthOpen") === "1";
  const issues = events.filter((e) => e.type === "ServiceIssue").length;
  const parts = [issues && `${issues} service issue${issues === 1 ? "" : "s"}`,
    events.length - issues && `${events.length - issues} planned maintenance`].filter(Boolean).join(", ");
  const ids = (e) => (e.tracking_ids || []).length > 1 ? ` <span class="muted">${e.tracking_ids.length} events: ${esc(e.tracking_ids.join(", "))}</span>`
    : (e.tracking_ids || []).length ? ` <span class="muted">${esc(e.tracking_ids[0])}</span>` : "";
  return `<details ${open ? "open" : ""}>
    <summary><strong>Active Service Health in these regions:</strong> ${esc(parts)}</summary><ul>${
    events.map((e) => `<li><b>${esc(e.type)}</b>: ${esc(e.title)} (${esc(e.services.join(", "))})${ids(e)}</li>`).join("")}</ul></details>`;
}

$("service-health").addEventListener("toggle", (event) => {
  stored.set("ahc.serviceHealthOpen", event.target.open ? "1" : "0");
}, true);

function rememberScopeOnly() {
  stored.set("ahc.snScopeOnly", $("sn-scope-only").checked ? "1" : "0");
}
$("sn-scope-only").addEventListener("change", rememberScopeOnly);
$("sn-scope-only").checked = stored.get("ahc.snScopeOnly") !== "0";

// Browse → a resource → ServiceNow: the records that name it (or, for a resource group, anything in it).
async function tabServiceNow(node, body) {
  const data = await exploreGet("servicenow", node, { connection: state.selected.servicenow || "" });
  if (!data.available) { body.innerHTML = snUnavailable(data); return; }
  const rows = [...data.incidents.map((r) => ({ ...r, what: "Incident" })), ...data.changes.map((r) => ({ ...r, what: `Change (${r.window})` })),
    ...data.alerts.map((r) => ({ ...r, what: "Alert" })), ...data.events.map((r) => ({ ...r, what: "Event" }))];
  const unread = Object.entries(data.notes || {});
  const notes = noticesHtml(unread.map(([tbl, note]) => `${tbl}: ${note}`));
  body.innerHTML = `<p class="muted">Active records in ${esc(data.name)} (${esc(data.instance.replace(/^https:\/\//, ""))}) that name this resource, read
      ${esc(ago(data.fetched_at))}.${toolLink(data.instance, "ServiceNow")}</p>${notes}
    ${rows.length ? table(["Type", "Record", "Description", "State", "How it matched"], rows.map((r) => {
      const m = r.matches.find((x) => x.id.toLowerCase() === node.id.toLowerCase()) || r.matches[0];
      return [esc(r.what) + (r.priority ? ` ${snPill(r)}` : ""), `<a href="${safeHref(r.url)}" target="_blank" rel="noopener">${esc(r.number || "open")}</a>`,
        `${esc(r.title)}${r.ci ? `<div class="muted">CI: ${r.ci_url ? monLink(r.ci_url, r.ci) : esc(r.ci)}</div>` : ""}${
          r.cmdb_note ? `<div class="sn-cmdb">⚠ CMDB: ${esc(r.cmdb_note)}</div>` : ""}`, esc(r.state),
        esc(m ? SN_CONFIDENCE[m.confidence] : "")];
    })) : unread.length ? "" : '<p class="ok">✓ No active incidents, changes, alerts or events name this resource.</p>'}`;
}

// Triage: two cards from ServiceNow, added after the Azure cards (seconds, cached for a minute).
async function loadServiceNowCards(scope) {
  if (!state.snEnabled) return;
  const token = state.triageToken;
  const holder = document.createElement("div");
  holder.className = "card";
  holder.id = "card-servicenow";
  holder.innerHTML = `<h3>ServiceNow</h3>${loading("Reading incidents and changes…")}`;
  $("triage-cards").prepend(holder);
  let data;
  try {
    data = await api("api/servicenow", { params: { ...scopeParams(scope), connection: state.selected.servicenow || "" } });
  } catch (error) {
    if (!error.signedOut) holder.innerHTML = `<h3>ServiceNow</h3>${noticeHtml(error)}`;
    return;
  }
  if (token !== state.triageToken) return;
  if (!data.available) { holder.innerHTML = `<h3>ServiceNow</h3>${snUnavailable(data)}`; return; }
  const allUrgent = data.incidents.filter((r) => r.priority <= 3);
  const allChanges = data.changes;  // in the connection's span: recent, in progress, scheduled
  const urgent = allUrgent.filter(namesResource);
  const changes = allChanges.filter(namesResource);
  const others = (all, shown, what) => (all.length > shown.length ? `<p class="muted">+${all.length - shown.length} other ${what}
    ${all.length - shown.length === 1 ? "doesn't" : "don't"} name anything in scope (ServiceNow tab).</p>` : "");
  const item = (r, extra) => `<li class="static">${extra} <a href="${safeHref(r.url)}" target="_blank" rel="noopener"><strong>${esc(r.number)}</strong></a>
    ${esc(r.title)}<div class="where">${esc(r.group || "")}${r.matches.length ? " · " : ""}${resourceChips(r.matches, 3)}</div></li>`;
  const card = (key, title, desc, rows, tone, render, extra, note = "") => `<div class="card ${rows.length ? tone : note ? "unknown" : "clear"}" id="card-${key}">
    <h3>${title}<span class="count">${rows.length}</span></h3><p class="desc">${desc}</p>${note ? noticeHtml(note) : ""}
    ${rows.length ? `<ul>${rows.map(render).join("")}</ul>` : note ? "" : '<p class="ok">✓ Nothing naming a resource in scope</p>'}${extra}</div>`;
  const via = ` <span class="muted">· ${esc(data.name)}</span>`;
  holder.outerHTML = card("sn-incidents", `ServiceNow: P1–P3 incidents${via}`, "Active priority 1 to 3 incidents naming Azure resources in scope",
    urgent, urgent.some((r) => r.priority === 1) ? "critical" : "warning", (r) => item(r, snPill(r)),
    others(allUrgent, urgent, "P1–P3 incident" + (allUrgent.length - urgent.length === 1 ? "" : "s")), (data.notes || {}).incident)
    + card("sn-changes", `ServiceNow: changes${via}`, `Change requests in ${snChangeSpan(data)} that name Azure resources in scope (often the cause)`,
      changes, changes.some((r) => ["in progress", "past its window"].includes(r.window)) ? "warning" : "info",
      (r) => item(r, `${pillHtml(SN_CHANGE_TONE[r.window] || "info", r.window)}${r.start ? ` <span class="muted">${esc(dayTime(r.start))}</span>` : ""}`),
      others(allChanges, changes, "change" + (allChanges.length - changes.length === 1 ? "" : "s")),
      (data.notes || {}).change_request);
}

$("triage-cards").addEventListener("click", (event) => {
  const chip = event.target.closest(".sn-res[data-open]");
  if (chip) { event.stopPropagation(); startSnapshot(JSON.parse(chip.dataset.open)); }
}, true);

