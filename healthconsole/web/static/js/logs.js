"use strict";

// ------------------------------------------------------------------ Settings → Logs
// This machine's log, read back: errors (with the ID the page showed), what was done in the console, and every
// request sent, grouped, so anyone can see that only reads left it. Nothing here is sent anywhere.

const LOG_KINDS = [["errors", "Errors"], ["audit", "What was done"], ["requests", "Requests sent"]];
const LOG_DAYS = [[1, "Today"], [7, "Last 7 days"], [30, "Last 30 days"]];
const LOG_SOURCES = { server: "The console (server)", page: "This page", job: "A check or read" };
const LOG_SERVICES = { ARM: "Azure Resource Manager", GRAPH: "Microsoft Graph", K8S: "Kubernetes API", ACR: "Container registry",
  SN: "ServiceNow", NR: "New Relic", PYPI: "PyPI (update check)", STATUS: "Azure status page" };
const LOG_ACTIONS = {
  settings_save: "Saved settings", settings_reload: "Reloaded settings from the file", integrations_servicenow: "Changed a ServiceNow connection",
  integrations_monitors: "Changed a monitoring tool connection", integrations_move_legacy: "Moved connections to this tenant",
  integrations_secret: "Saved or removed a secret in the keychain", integrations_test: "Tested a connection",
  servicenow_sign_in: "Signed in to ServiceNow", watchlist_add: "Added to the Watchlist", watchlist_remove: "Removed from the Watchlist",
  watchlist_check_now: "Checked the Watchlist", alerts_save: "Changed alert rules", alerts_run: "Ran the alert rules",
  alerts_acknowledge: "Acknowledged an alert", alerts_snooze: "Snoozed an alert", alerts_dismiss: "Dismissed an alert",
  saved_scopes_save: "Changed saved scopes", open_with_save: "Changed what the page opens with",
  saved_scopes_move_legacy: "Moved saved scopes to this tenant", cache_clear: "Cleared the cache", path_link_save: "Linked a path hop",
  access_user: "Checked someone's access", secrets_scan: "Read Key Vault expiry (names only)", access_audit_start: "Ran the access audit",
  signin_start: "Started an Azure sign-in", signin_cancel: "Cancelled an Azure sign-in", signout: "Signed out of Azure",
  no_launch_key: "Refused: a request without the console's key", start_snapshot: "Checked a resource or group",
  start_check: "Ran the AKS check", start_readiness: "Ran Pre-peak readiness", cost_overview: "Read costs",
  summary_changes: "Read what changed", cancel_job: "Cancelled a check", diagram_svg: "Drew a diagram", refresh: "Refreshed access",
};
const LOG_ACTION_KINDS = { change: ["info", "changed something"], sensitive: ["warning", "read about people or secrets"],
  read: ["healthy", "started a read"], refused: ["critical", "refused"] };
const logState = { kind: "errors", days: 1, rows: [], find: "" };
// "7:31 PM" today, "Nov 24, 7:31 PM" before, and how long ago; the full time on hover.
function logWhen(at) {
  const d = new Date(at), time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const label = d.toDateString() === new Date().toDateString() ? time : `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
  return `<span class="nowrap" data-v="${d.getTime() / 1000}" title="${esc(d.toLocaleString())}">${esc(label)}</span><div class="muted nowrap">${esc(ago(at))}</div>`;
}

function showLogs() {
  $("logs-tools").innerHTML = `<div class="seg" role="group" aria-label="What to show">${LOG_KINDS.map(([k, l]) =>
    `<button type="button" data-log-kind="${k}" class="${k === logState.kind ? "active" : ""}">${l}</button>`).join("")}</div>
    <label class="inline">From <select id="log-days">${LOG_DAYS.map(([d, l]) => `<option value="${d}" ${d === logState.days ? "selected" : ""}>${l}</option>`).join("")}</select></label>
    <input type="search" id="log-find" placeholder="Filter: an error ID, a name, an action…" aria-label="Filter the log" value="${esc(logState.find)}">
    <button type="button" id="log-refresh">Refresh</button>`;
  loadLogs();
}

async function loadLogs() {
  const out = $("logs-out");
  out.innerHTML = loading("Reading the log…");
  let data;
  try {
    data = await api("api/logs", { params: { kind: logState.kind, days: logState.days } });
  } catch (error) {
    out.innerHTML = errorHtml(error.message);
    return;
  }
  const where = `<p class="muted log-where">Kept ${fmt(data.keep_days)} days in <code>${esc(data.folder)}</code>, readable only by you
    (${settingLink("logs.keep_days", "change")}). Nothing here is sent anywhere; tokens, keys and secret values are never written.</p>`;
  if (!data.enabled) {
    out.innerHTML = `${where}${noticeHtml("The log is off for this console: it's kept only when the console runs with serve. Errors still show on the page.")}`;
    return;
  }
  logState.rows = data.rows;
  const span = (LOG_DAYS.find(([d]) => d === data.days) || [0, `the last ${data.days} days`])[1].toLowerCase();
  out.innerHTML = (data.kind === "errors" ? logErrorsHtml(data.rows, span)
    : data.kind === "audit" ? logAuditHtml(data.rows, span) : logRequestsHtml(data.rows, span)) + where;
  filterLogs();
}

// The box above filters whichever list is shown (every list, however short), by any text in a row.
function filterLogs() {
  for (const t of $("logs-out").querySelectorAll("table[data-list]")) {
    t.dataset.nofilter = "";
    t.dataset.filterText = logState.find;
    if (t.tBodies[0]) pageList(t);
  }
}

function logErrorsHtml(rows, span) {
  if (!rows.length) return `<p class="ok">✓ No errors ${esc(span)}.</p>`;
  return `<p class="muted">An error ID on the page (for example <em>error ID 7f3a2c1d</em>) is the same ID here: type it in the filter above.</p>`
    + table(["When", "Where", "What happened", "Error ID", ""], rows.map((e, i) => [logWhen(e.at), esc(LOG_SOURCES[e.source] || e.source),
      `${esc(e.message)}${e.type ? ` <span class="muted">${esc(e.type)}</span>` : ""}${e.stack ? `<details class="log-stack"><summary>Details</summary>
        <pre>${esc(e.stack)}</pre></details>` : ""}`, `<code class="nowrap">${esc(e.id)}</code>`,
      `<button type="button" class="link" data-log-copy="${i}" title="Copies this error, with subscription and object IDs hidden, to paste in a bug report">Copy for a bug report</button>`]), "log-errors");
}

function logAuditHtml(rows, span) {
  if (!rows.length) return `<p class="muted">Nothing was done in the console ${esc(span)}.</p>`;
  const detail = (d) => [d.changed && d.changed.length ? `changed ${d.changed.map(esc).join(", ")}` : "",
    d.looked_up ? `looked up ${esc(d.looked_up)}` : "", d.target ? `${esc(d.target)} · ${esc(d.field)}${d.removed ? " (removed)" : ""}` : "",
    d.path ? esc(d.path) : ""].filter(Boolean).join(" · ");
  return table(["When", "Who", "What", "Kind", "Result", "Details"], rows.map((e) => {
    const [tone, label] = LOG_ACTION_KINDS[e.kind] || ["unknown", e.kind];
    const ok = e.outcome >= 200 && e.outcome < 300, refused = e.kind === "refused";
    return [logWhen(e.at), `<span class="nowrap">${esc(e.user) || `<span class="muted">${refused ? "unknown (no key)" : "not signed in"}</span>`}</span>`,
      esc(LOG_ACTIONS[e.action] || e.action), `<span class="nowrap">${pillHtml(tone, label)}</span>`,
      `<span class="nowrap">${ok ? '<span class="ok">done</span>' : `<span class="worse">${refused ? "stopped" : `failed (${esc(e.outcome)})`}</span>`}</span>`, detail(e.detail || {})];
  }), "log-audit");
}

// The reads that services offer only as POST, named so a POST in the list says what it read.
const LOG_POST_READS = [[/\/providers\/microsoft\.resourcegraph\/resources$/i, "Resource Graph query"],
  [/\/providers\/microsoft\.insights\/logs$/i, "Log Analytics query"], [/\/listclusterusercredential$/i, "AKS user credential"],
  [/\/providers\/microsoft\.costmanagement\/(query|forecast)$/i, "Cost Management query"], [/^\/graphql$/i, "GraphQL query"],
  [/\/oauth_token\.do$/i, "ServiceNow sign-in token"]];
const LOG_NO_ANSWER = "Timed out or the connection dropped; the console retries, so one read can count more than once";

function logRequestsHtml(rows, span) {
  if (!rows.length) return `<p class="muted">No requests sent ${esc(span)}.</p>`;
  const services = new Map();
  for (const r of rows) {
    const svc = services.get(r.service) || services.set(r.service, { name: r.service, hosts: new Set(), count: 0, rows: new Map() }).get(r.service);
    svc.hosts.add(r.host);
    svc.count += r.count;
    const key = `${r.method} ${r.path.toLowerCase()}`;  // one row, however a path's case was written
    const g = svc.rows.get(key) || svc.rows.set(key, { method: r.method, path: r.path, count: 0, outcomes: {} }).get(key);
    g.count += r.count;
    g.outcomes[r.outcome] = (g.outcomes[r.outcome] || 0) + r.count;
  }
  const all = [...services.values()].flatMap((svc) => [...svc.rows.values()]);
  const sent = (test) => all.filter(test).reduce((n, g) => n + g.count - (g.outcomes.refused || 0), 0);
  const refused = all.reduce((n, g) => n + (g.outcomes.refused || 0), 0);
  const gets = sent((g) => g.method === "GET"), posts = sent((g) => g.method === "POST");
  const others = sent((g) => !["GET", "POST"].includes(g.method));
  const verdict = others ? `<p class="message error">${fmt(others)} requests that aren't GET or POST were sent ${esc(span)}. That shouldn't happen: please report it.</p>`
    : refused ? `<p class="notice">The console tried ${fmt(refused)} request${refused === 1 ? "" : "s"} that ${refused === 1 ? "isn't a read" : "aren't reads"} ${esc(span)}; its guard stopped ${refused === 1 ? "it" : "them"} before ${refused === 1 ? "it" : "they"} left this machine (marked <em>refused</em> below).</p>`
      : `<p class="ok big-line">✓ Only reads left this machine ${esc(span)}.</p>`;
  const counts = `<p><strong>${fmt(gets + posts + others)}</strong> requests: ${fmt(gets)} GET · ${fmt(posts)} reads sent as POST${
    others ? ` · <span class="worse">${fmt(others)} other</span>` : ""} · ${refused ? `<span class="worse">${fmt(refused)} refused</span>` : "none refused"}
    <details class="inline-help"><summary>Why are some reads a POST?</summary><p class="muted">A few services only take a read as POST,
    because the question is sent in the body: Resource Graph, Log Analytics and Cost Management queries and the AKS user credential
    (Azure), New Relic's GraphQL queries and the ServiceNow sign-in token. The console's guard lets those through and stops anything
    else before it leaves this machine. Names and IDs in a path show as {…}.</p></details></p>`;
  const answers = (o) => Object.entries(o).sort().map(([k, n]) => k === "refused" ? `<span class="worse">${fmt(n)} refused</span>`
    : k === "no answer" ? `<span title="${LOG_NO_ANSWER}">${fmt(n)} no answer</span>` : `${fmt(n)} ${k === "2xx" ? "OK" : k}`).join(" · ");
  const what = (g) => {
    const read = g.method === "POST" && (LOG_POST_READS.find(([re]) => re.test(g.path)) || [])[1];
    return `${read ? `<strong>${esc(read)}</strong><br>` : ""}<code class="log-path">${esc(g.path)}</code>`;
  };
  const blocks = [...services.values()].sort((a, b) => b.count - a.count).map((svc) => {
    const list = [...svc.rows.values()].sort((a, b) => (b.outcomes.refused ? 1 : 0) - (a.outcomes.refused ? 1 : 0)
      || (a.method === "GET") - (b.method === "GET") || b.count - a.count);
    return `<h4 class="log-service">${esc(LOG_SERVICES[svc.name] || svc.name)} <span class="muted">${[...svc.hosts].map(esc).join(", ")}
      · ${fmt(svc.count)} request${svc.count === 1 ? "" : "s"}</span></h4>`
      + table(["Method", "What it read", "Requests", "Answers"], list.map((g) => [
        `<span class="nowrap">${g.method === "GET" ? "GET" : `<strong>${esc(g.method)}</strong>`}</span>`, what(g),
        `<span class="nowrap" data-v="${g.count}">${fmt(g.count)}</span>`, `<span class="nowrap">${answers(g.outcomes)}</span>`]), `log-requests-${svc.name}`);
  }).join("");
  return verdict + counts + blocks;
}

// The error for a bug report: subscription, tenant and object IDs hidden, the rest as written.
function logBugReport(e) {
  const hide = (text) => String(text || "").replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, "<id>");
  return hide([`Azure Health Console error ${e.id}`, `When: ${e.at}`, `Where: ${LOG_SOURCES[e.source] || e.source}`,
    `What: ${e.message}${e.type ? ` (${e.type})` : ""}`, Object.keys(e.context || {}).length ? `Context: ${JSON.stringify(e.context)}` : "",
    e.stack ? `\n${e.stack}` : ""].filter(Boolean).join("\n"));
}

$("logs-tools").addEventListener("click", (event) => {
  const kind = event.target.closest("[data-log-kind]");
  if (kind) { logState.kind = kind.dataset.logKind; showLogs(); return; }
  if (event.target.closest("#log-refresh")) loadLogs();
});
$("logs-tools").addEventListener("input", (event) => {
  if (event.target.id === "log-find") { logState.find = event.target.value.trim(); filterLogs(); }
});
$("logs-tools").addEventListener("change", (event) => {
  if (event.target.id === "log-days") { logState.days = Number(event.target.value); loadLogs(); }
});
$("logs-out").addEventListener("click", (event) => {
  const copy = event.target.closest("[data-log-copy]");
  if (!copy) return;
  const e = logState.rows[Number(copy.dataset.logCopy)];
  if (e) copyText(logBugReport(e), copy, "Copied");
});
