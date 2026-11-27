"use strict";

// ------------------------------------------------------------------ investigate
// One failing request, step by step: 1 operations failing in a component, 2 sample requests of one, 3 one request end
// to end (with the gateway's spans when API Management logs to App Insights), 4 the pod that served it: its state,
// logs at that minute, warning events and changes before. Each step reads only when clicked and shows its queries.

const INV_KIND_TONE = { request: "info", dependency: "info", exception: "critical", trace: "unknown" };
const INV_PLURAL = { request: "requests", dependency: "dependencies", exception: "exceptions", trace: "log lines" };
const INV_SEVERITIES = [["", "Any level"], ["2", "Warning and worse"], ["3", "Error and worse"]];
const inv = { component: "", gateways: [], untick: new Set(), loaded: "", pending: null, request: null, found: {} };

function invWindow() {
  return stored.choice("ahc.invWindow", AI_WINDOWS, "1h");
}

// Gateway components ticked to read with a trace (all that log to App Insights, unless unticked here).
function invAlso() {
  return [...new Set(inv.gateways.filter((g) => !inv.untick.has(g.id)).flatMap((g) => g.components))];
}

// The picked Look back in words, for column heads: a count next to "3 min ago" must say it covers the whole window.
const INV_SPANS = { "1h": "last hour", "6h": "last 6 hours", "24h": "last 24 hours", "3d": "last 3 days", "7d": "last 7 days",
  "14d": "last 14 days", "30d": "last 30 days" };
const invSpan = () => INV_SPANS[invWindow()] || `last ${invWindow()}`;

function invParams(extra = {}) {
  return { component: inv.component, window: invWindow(), ...extra };
}

const invTime = (iso) => (iso ? `<span title="${esc(iso)}">${esc(new Date(iso).toLocaleTimeString())}</span> ${agoHtml(iso)}` : "–");
const invBox = (id) => $(id).querySelector(".inv-body");

function invStep(id, html) {
  const box = invBox(id);
  box.innerHTML = html;
  $(id).classList.remove("hidden");
  renderInvTrail();
  $(id).scrollIntoView({ behavior: "smooth", block: "start" });
  return box;
}

function invReset(from) {
  const order = ["inv-ops", "inv-samples", "inv-request", "inv-pod"];
  for (const id of order.slice(order.indexOf(from))) $(id).classList.add("hidden");
  renderInvTrail();
}

// Where you are: one crumb per open step (app › what you picked in step 2 › its samples › the request › the pod),
// kept in the sticky bar; a crumb scrolls back to its step, and step 2's unfolds it.
const INV_TRAIL = [["inv-apps", () => inv.componentName], ["inv-ops", () => inv.picked && inv.picked.label],
  ["inv-samples", () => inv.op && `latest ${inv.op.code ? `${codeOf(inv.op.code)}s` : "failures"}`],
  ["inv-request", () => inv.request && `request ${String(inv.request.trace).slice(0, 8)}…`],
  ["inv-pod", () => inv.pod && `pod ${inv.pod.instance}`]];
function renderInvTrail() {
  const crumbs = INV_TRAIL.filter(([id, label]) => !$(id).classList.contains("hidden") && label());
  const trail = $("inv-trail");
  trail.classList.toggle("hidden", !inv.component || inv.sub === "search" || crumbs.length < 2);
  trail.innerHTML = crumbs.map(([id, label], i) => `${i ? '<span class="sep" aria-hidden="true">›</span>' : ""}<button type="button"
    data-inv-goto="${id}" title="${esc(label())}" ${i === crumbs.length - 1 ? 'aria-current="step"' : ""}>${esc(label())}</button>`).join("");
}

// Going deeper from step 2 folds it to its heading, which says what you picked; Show all again opens it at that row.
function invCameFrom(button) {
  const row = button.closest("tr");
  if (!row || !button.closest("#inv-ops")) return;
  $("inv-ops").querySelectorAll("tr.inv-came-from").forEach((r) => r.classList.remove("inv-came-from"));
  row.classList.add("inv-came-from");
  const what = row.cells[0].textContent.trim().replace(/\s+/g, " ").slice(0, 90);
  const code = button.dataset.invOp && JSON.parse(button.dataset.invOp).code;
  inv.picked = { label: code ? `${codeOf(code)} · ${what}` : what, row };
  $("inv-ops").querySelector(".inv-picked").innerHTML = `— you picked <strong>${esc(inv.picked.label)}</strong>`;
  $("inv-ops").querySelector(".inv-picked").classList.remove("hidden");
  $("inv-ops").querySelector(".inv-unfold").classList.remove("hidden");
  $("inv-ops").classList.add("collapsed");
}
// Following a sample folds step 3 to "you're following …"; Back to the samples (there and on step 4) reopens the
// list at that row, marked ✓ followed with the others you've opened, ready for the next one.
function invFollowedSample(button) {
  const row = button.closest("tr");
  if (!row || !button.closest("#inv-samples")) return;
  $("inv-samples").querySelectorAll("tr.inv-came-from").forEach((r) => r.classList.remove("inv-came-from"));
  row.classList.add("inv-came-from");
  if (!row.querySelector(".inv-seen")) button.insertAdjacentHTML("beforebegin", '<span class="inv-seen">✓ followed</span>');
  (inv.followedTraces = inv.followedTraces || new Set()).add(JSON.parse(button.dataset.invFollow).trace);
  inv.followedRow = row;
  const label = [...row.cells].slice(0, 3).map((cell) => cell.textContent.trim().replace(/\s+/g, " ")).join(" · ");
  const picked = $("inv-samples").querySelector(".inv-picked");
  picked.innerHTML = `— you're following <strong>${esc(label)}</strong>`;
  picked.classList.remove("hidden");
  $("inv-samples").querySelector("[data-inv-back-samples]").classList.remove("hidden");
  $("inv-samples").classList.add("collapsed");
}
function invBackToSamples() {
  $("inv-samples").classList.remove("collapsed");
  $("inv-samples").querySelector("[data-inv-back-samples]").classList.add("hidden");
  const row = inv.followedRow;
  if (row && row.isConnected) row.scrollIntoView({ behavior: "smooth", block: "center" });
  else $("inv-samples").scrollIntoView({ behavior: "smooth", block: "start" });
}

function invUnfold() {
  $("inv-ops").classList.remove("collapsed");
  $("inv-ops").querySelector(".inv-unfold").classList.add("hidden");
  const row = inv.picked && inv.picked.row;
  if (row && row.isConnected) row.scrollIntoView({ behavior: "smooth", block: "center" });
  else $("inv-ops").scrollIntoView({ behavior: "smooth", block: "start" });
}

// "40 failed of 400 (10%)", against the same hour last week.
function invLastHour(c) {
  if (c.pending) return '<span class="muted">measuring…</span>';
  if (!c.measured) return `<span class="muted">${c.unread ? "no access to its metrics" : "not measured"}</span>`;
  if (!c.requests) return '<span class="muted">no requests in the last hour</span>';
  const client = codeList(c.codes, "client", 6);
  return `<span class="${(c.causes || []).some((x) => x === "failed" || x === "steady") ? "worse" : ""}"${client ? ` title="4xx, mostly the caller's side: ${esc(client)}"` : ""}>${fmt(c.failed)} failed of ${fmt(c.requests)} (${pctText(c.failed_pct, c.failed)})</span>`;
}

function invWeek(c) {
  if (!c.measured) return "";
  return c.week.requests ? `${fmt(c.week.failed)} of ${fmt(c.week.requests)} (${pctText(c.week.failed_pct, c.week.failed)})` : '<span class="muted">no requests</span>';
}

// What reports to a component: Web/Function Apps by their hidden-link tag, API Management by its loggers.
const invReporters = (c) => [...c.apps, ...(inv.gateways || [])
  .filter((g) => g.components.some((id) => sameId(id, c.id))).map((g) => `API Management ${g.name}`)];

// A few names and "+N more" (all on hover, and in the row's text so the filter finds them): one component with dozens
// of apps widened the column for every row.
const INV_REPORTERS_SHOWN = 3;
function invAppsHtml(c) {
  const names = invReporters(c);
  const rest = names.length - INV_REPORTERS_SHOWN;
  return names.length ? `<div class="inv-reporters" title="${esc(names.join(", "))}">${esc(names.slice(0, INV_REPORTERS_SHOWN).join(", "))}${
    rest > 0 ? ` <span class="muted">+${rest} more</span><span hidden>, ${esc(names.slice(INV_REPORTERS_SHOWN).join(", "))}</span>` : ""}</div>`
    : '<span class="muted" title="Web and Function Apps name their component in a hidden-link tag, API Management in its loggers; AKS apps don\'t">not linked</span>';
}

// Hourly counts as a small grey line, oldest left, scaled to its own peak (shown beside it); the newest point turns
// red only when the last hour has some.
function sparkHtml(values, title) {
  if (!values || values.length < 2) return '<span class="muted">–</span>';
  const peak = Math.max(...values), last = values[values.length - 1];
  title = `${title}: peak ${fmt(peak)}, last hour ${fmt(last)}`;
  const w = 96, h = 22, top = Math.max(peak, 1);
  const pts = values.map((v, i) => [(i * (w - 4)) / (values.length - 1) + 2, h - 2 - ((h - 4) * v) / top]);
  const [lx, ly] = pts[pts.length - 1];
  return `<span class="spark-wrap"><svg class="spark${last ? " hot" : ""}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title>
    <polyline points="${pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}"/><circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2"/></svg>${peak ? `<span class="muted spark-peak">peak ${fmt(peak)}</span>` : ""}</span>`;
}

// HTTP result codes as Azure gives them. "… [not sent in full …]" (API Management) is a response cut short, usually a
// client that hung up; a plain 0 is a function or job not called over HTTP, so it has no status.
const HTTP_NAMES = { 0: "no HTTP status (not called over HTTP)", 400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "not found",
  405: "method not allowed", 408: "request timeout", 409: "conflict", 410: "gone", 412: "precondition failed",
  413: "too large", 415: "unsupported type", 422: "unprocessable", 429: "too many requests", 499: "client closed",
  500: "internal server error", 501: "not implemented", 502: "bad gateway", 503: "service unavailable",
  504: "gateway timeout", 511: "network authentication required" };
const codeOf = (raw) => String(raw ?? "").split(" ")[0];
const codeCut = (raw) => /not sent in full/i.test(String(raw ?? ""));
const codeKind = (raw) => (codeCut(raw) ? "cut" : codeOf(raw) === "0" ? "run" : codeOf(raw).startsWith("5") ? "server" : "client");
const codeName = (raw) => (codeCut(raw) ? (codeOf(raw) === "0" ? "not sent in full" : `${HTTP_NAMES[codeOf(raw)] || ""}, not sent in full`.replace(/^, /, ""))
  : HTTP_NAMES[codeOf(raw)] || "");
const codeList = (codes, kind, n) => Object.entries(codes || {}).filter(([k]) => codeKind(k) === kind)
  .sort((a, b) => b[1] - a[1]).slice(0, n).map(([k, v]) => `${codeOf(k)} ${fmt(v)}`).join(" · ");
const codeSum = (codes, kind) => Object.entries(codes || {}).filter(([k]) => codeKind(k) === kind).reduce((t, [, v]) => t + v, 0);

// 5xx in the last hour, their share of every component's 5xx in scope and the top codes. Failures without a usable
// status get their own amber line, since they aren't 5xx but are rarely the caller's fault.
function invImpact(c) {
  if (!c.measured || c.server_errors == null) return '<span class="muted" data-v="-1">–</span>';
  const cut = codeSum(c.codes, "cut"), runs = codeSum(c.codes, "run");
  const cutLine = (cut ? `<div class="code-cut" title="API Management couldn't send the whole response: usually the caller hung up while the backend was slow. Investigate → By HTTP code says why">⚠ ${fmt(cut)} not sent in full</div>` : "")
    + (runs ? `<div class="code-cut" title="Code 0: a function or job not called over HTTP (a queue, a timer) failed, so there's no HTTP status. Investigate → By HTTP code lists its exceptions">⚠ ${fmt(runs)} failed runs (no HTTP status)</div>` : "");
  if (!c.server_errors) return `<span data-v="0"><span class="muted">none</span>${cutLine}</span>`;
  const top = codeList(c.codes, "server", 3);
  return `<span data-v="${c.server_errors}"><strong>${fmt(c.server_errors)}</strong>${c.share != null ? ` <span class="muted">· ${pctText(c.share, c.server_errors)} of scope</span>` : ""}${top ? `<div class="code-top">${esc(top)}</div>` : ""}${cutLine}</span>`;
}

// Every component is measured, a batch at a time, two batches in flight; rows re-sort worst first as each answers.
async function invMeasureAll(key, size) {
  const ids = inv.components.map((c) => c.id);
  const batches = [];
  for (let i = 0; i < ids.length; i += size) batches.push(ids.slice(i, i + size));
  let next = 0;
  const worker = async () => {
    while (next < batches.length && inv.loaded === key) {
      const batch = batches[next++];
      let got;
      try { got = (await api("api/investigate/measure", { params: { id: batch } })).components; } catch (error) {
        if (error.signedOut) return;
        got = Object.fromEntries(batch.map((id) => [id, { unread: true }]));
      }
      if (inv.loaded !== key) return;
      for (const c of inv.components) if (got[c.id]) Object.assign(c, got[c.id], { pending: false });
      inv.measuring.done += batch.length;
      invSortComponents();
      renderInvApps();
      if (inv.component) invCurrentRefresh();
    }
  };
  await Promise.all([worker(), worker()]);
}

function invSortComponents() {
  const rows = inv.components;
  const scope5xx = rows.reduce((n, c) => n + (c.server_errors || 0), 0);
  for (const c of rows) c.share = scope5xx && c.server_errors ? Math.round(1000 * c.server_errors / scope5xx) / 10 : null;
  const rank = { critical: 0, warning: 1 };
  rows.sort((a, b) => (a.pending ? 1 : 0) - (b.pending ? 1 : 0) || (rank[a.status] ?? 2) - (rank[b.status] ?? 2)
    || (b.server_errors || 0) - (a.server_errors || 0) || (b.failed || 0) - (a.failed || 0) || byName(a.name, b.name));
}

function invMeasureNotes() {
  const rows = inv.components || [];
  const unread = rows.filter((c) => c.unread).length, unsplit = rows.filter((c) => c.split_error).length;
  return [unread ? `${unread} component(s) couldn't be measured (no access to their metrics).` : "",
    unsplit ? `The 5xx count couldn't be read for ${unsplit} component(s); their Server errors column is empty.` : ""].filter(Boolean);
}

// Step 1 lists the components that need a look (a red or amber status, or any server errors in the last hour); the
// quiet ones are counted and one click away, and typing a name searches them all (component or reporting app).
const invNeedsLook = (c) => Boolean(c.status) || (c.server_errors || 0) > 0;
// No pill says why: no worse than last week, so a row with 5xx is listed for those alone.
function invStatusHtml(c) {
  if (c.status) return `${pillHtml(c.status, c.status, { title: c.why || "" })}${aiWhyHtml(c)}`;
  if (!c.measured) return '<span class="muted">–</span>';
  return `<span class="muted" data-v="2" title="Failures and exceptions are no worse than the same hour last week">no worse than last week${
    c.server_errors ? "<br>listed for its 5xx" : ""}</span>`;
}
function invAppsShown(all) {
  const find = (inv.appFind || "").trim().toLowerCase();
  if (find) return all.filter((c) => [c.name, ...invReporters(c)].some((n) => String(n).toLowerCase().includes(find)));
  return inv.appsAll ? all : all.filter(invNeedsLook);
}
function renderInvAppsTools(all, shown) {
  const find = (inv.appFind || "").trim(), measuring = (inv.measuring || {}).done < (inv.measuring || {}).total;
  const hidden = all.length - shown.length;
  $("inv-apps-shown").textContent = !all.length ? ""
    : find ? `${shown.length.toLocaleString()} of ${all.length.toLocaleString()} match "${find}"`
      : inv.appsAll ? `All ${all.length.toLocaleString()} components`
        : `${shown.length.toLocaleString()} need a look (a red or amber status, or server errors in the last hour)`
          + (hidden ? ` · ${hidden.toLocaleString()} ${measuring ? "not measured yet or quiet" : "quiet"}, hidden` : "");
  $("inv-apps-all").textContent = inv.appsAll ? "Only the ones that need a look" : `Show all ${all.length.toLocaleString()}`;
  $("inv-apps-all").classList.toggle("hidden", Boolean(find) || !all.length || (!inv.appsAll && !hidden));
}

function renderInvApps() {
  const all = inv.components || [];
  const rows = invAppsShown(all);
  renderInvAppsTools(all, rows);
  const m = inv.measuring || { done: 0, total: 0 };
  const out = $("inv-apps-out");
  const old = out.querySelector('table[data-list="inv-apps"]');
  const filter = out.querySelector("[data-list-filter]");
  const keep = old ? { shown: old.dataset.shown, text: old.dataset.filterText, focused: filter && document.activeElement === filter } : null;
  const progress = m.done < m.total ? `<p class="muted">${loading(`Measuring the last hour: ${m.done.toLocaleString()} of ${m.total.toLocaleString()} components…`)}</p>` : "";
  out.innerHTML = `${noticesHtml([...(inv.notes || []), ...invMeasureNotes()])}${progress}${rows.length ? table(["", "App Insights component", "Apps that report to it",
    "Last hour", "Last 24 h", "Server errors (5xx)", "Same hour last week", "Exceptions", ""], rows.map((c) => [
      invStatusHtml(c),
      `<strong>${esc(c.name)}</strong> ${portalLinkHtml(c.id)}<div class="muted">${esc([c.resource_group, subName(c.subscription_id)].filter(Boolean).join(" · "))}</div>`,
      invAppsHtml(c),
      invLastHour(c), c.measured ? sparkHtml(c.trend, "Failed requests per hour, last 24 h") : "", invImpact(c), invWeek(c),
      c.measured ? `<span class="${(c.causes || []).includes("exceptions") ? "worse" : ""}">${fmt(c.exceptions)}</span> <span class="muted">(last week ${fmt(c.week.exceptions)})</span>` : "",
      `<button type="button" class="${c.status ? "primary" : "link"}" data-inv-app="${esc(c.id)}">Investigate</button>`]), "inv-apps")
    : !all.length ? '<p class="muted">No Application Insights component in scope: widen the scope, or open one with Search.</p>'
      : (inv.appFind || "").trim() ? '<p class="muted">No component or reporting app has that name in scope.</p>'
        : '<p class="ok">✓ Nothing needs a look in the last hour: no red or amber status and no server errors. <strong>Show all</strong> lists every component.</p>'}`;
  const list = out.querySelector('table[data-list="inv-apps"]');
  if (list) list.dataset.nofilter = "";  // Go to a component above is its search: one box, not two
  if (!keep || !list) return;
  if (keep.shown) list.dataset.shown = keep.shown;  // before the list observer pages it
  if (keep.text) list.dataset.filterText = keep.text;
  setTimeout(() => {
    const box = out.querySelector("[data-list-filter]");
    if (!box || !keep.text) return;
    box.value = keep.text;
    if (keep.focused) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  });
}

function invPick(componentId, name = "") {
  const c = (inv.components || []).find((x) => sameId(x.id, componentId));
  inv.component = componentId;
  inv.componentName = (c && c.name) || name || componentId.split("/").pop();
  invCurrentRefresh();
  $("inv-current").classList.remove("hidden");
  $("inv-tools").classList.remove("hidden");
  $("inv-apps").classList.add("collapsed");
  renderInvSearchApps();
}

// The "Investigating …" line, drawn again when its component's numbers arrive.
function invCurrentRefresh() {
  const componentId = inv.component;
  const c = (inv.components || []).find((x) => sameId(x.id, componentId));
  $("inv-current").innerHTML = `<span class="muted">Investigating</span> <strong>${esc(inv.componentName)}</strong>
    ${c && invReporters(c).length ? `<span class="muted" title="${esc(invReporters(c).join(", "))}">(${esc(invReporters(c).slice(0, INV_REPORTERS_SHOWN).join(", "))}${
      invReporters(c).length > INV_REPORTERS_SHOWN ? ` +${invReporters(c).length - INV_REPORTERS_SHOWN} more` : ""})</span>` : ""}
    ${c ? invLastHour(c) : ""}${c && c.measured ? ` <span class="muted">5xx</span> ${invImpact(c)} ${sparkHtml(c.trend, "Failed requests per hour, last 24 h")}` : ""}
    ${portalLinkHtml(componentId, { label: "Portal ↗", title: "The component in the Azure portal" })}
    ${portalLinkHtml(componentId, { label: "Failures ↗", page: "failures", title: "Its Failures page in the Azure portal" })}
    ${portalLinkHtml(componentId, { label: "Logs ↗", page: "logs", title: "Its Logs page in the Azure portal: paste a query from Query sent" })}
    <button type="button" class="link" data-inv-change>pick another app</button>`;
}

async function loadInvestigate() {
  const scope = currentScope();
  const key = JSON.stringify(scope);
  if (inv.loaded === key && !inv.pending) return;
  inv.loaded = key;
  inv.component = "";
  $("inv-current").classList.add("hidden");
  $("inv-tools").classList.add("hidden");
  $("inv-apps").classList.remove("collapsed");
  invReset("inv-ops");
  inv.components = [];
  $("inv-search-out").innerHTML = "";
  if (!scope.subscriptions.length) {
    $("inv-apps-out").innerHTML = pickScopeHtml("Investigate measures the Application Insights components in scope.");
    renderInvSearchApps();
    $("inv-gateways").innerHTML = "";
    return;
  }
  $("inv-apps-out").innerHTML = loading("Listing the Application Insights components in scope…");
  $("inv-gateways").innerHTML = "";
  renderInvSearchApps();
  let found, gateways;
  try {
    [found, gateways] = await Promise.all([api("api/investigate/components", { params: { ...scopeParams(scope), measure: "0" } }),
      api("api/investigate/gateways", { params: scopeParams(scope) }).catch((error) => ({ gateways: [], notes: [error.message] }))]);
  } catch (error) {
    inv.loaded = "";
    if (!error.signedOut) $("inv-apps-out").innerHTML = noticeHtml(error);
    renderInvSearchApps();
    return;
  }
  inv.components = found.components;
  inv.notes = found.notes || [];
  inv.measuring = { done: 0, total: inv.components.length };
  inv.gateways = gateways.gateways;
  inv.gatewayNotes = gateways.notes || [];
  renderInvApps();
  invMeasureAll(key, found.batch || 25);
  renderInvGateways();
  renderInvSearchApps();
  const pending = inv.pending;
  inv.pending = null;
  if (!pending) return;
  invPick(pending.component);
  await loadInvOperations();
  if (pending.operation != null) loadInvSamples({ operation: pending.operation, role: pending.role || "" });
  else if (pending.request) loadInvRequest(pending.request);
}

// Gateways by what a request can use: App Insights spans (ticked, read with the trace), gateway logs in a workspace
// (step 4's Gateway logs, by time and path), or nothing readable.
function renderInvGateways() {
  const spans = inv.gateways.filter((g) => g.components.length);
  const workspace = inv.gateways.filter((g) => !g.components.length && g.workspace);
  const none = inv.gateways.filter((g) => !g.components.length && !g.workspace);
  const names = (list) => list.map((g) => `${esc(g.name)} ${portalLinkHtml(g.id)}`).join(", ");
  const lines = [
    spans.length ? `<div><span class="muted" title="API Management logs its span of a request to its own App Insights component">When following a request, also read the gateway's spans from:</span>
      ${spans.map((g) => `<label class="inline" title="logs to ${esc(g.components.map((x) => x.split("/").pop()).join(", "))}"><input type="checkbox" data-inv-gw="${esc(g.id)}"
        ${inv.untick.has(g.id) ? "" : "checked"}> ${esc(g.name)} <span class="muted">logs to ${esc(g.components.map((x) => x.split("/").pop()).join(", "))}</span></label> ${portalLinkHtml(g.id)}`).join(" ")}</div>` : "",
    workspace.length ? `<div><span class="muted" title="Their gateway logs share no ID with a request, so they're matched by time and URL path, marked probable">Gateway logs in a workspace, read after you follow a request (<strong>Gateway logs</strong>, by time and path):</span> ${names(workspace)}</div>` : "",
    none.length ? `<div class="muted">No App Insights logger and no gateway logs in a workspace: ${names(none)}</div>` : ""];
  $("inv-gateways").innerHTML = inv.gateways.length ? `${lines.filter(Boolean).join("")}${noticesHtml(inv.gatewayNotes)}`
    : '<span class="muted">No API Management service in scope: following a request reads only the app\'s own spans.</span>';
}

async function loadInvOperations() {
  invReset("inv-ops");
  inv.picked = null;
  $("inv-ops").classList.remove("collapsed");
  $("inv-ops").querySelector(".inv-picked").classList.add("hidden");
  $("inv-ops").querySelector(".inv-unfold").classList.add("hidden");
  $("inv-ops").classList.remove("hidden");
  renderInvTrail();
  $("inv-ops").querySelector("h3 .inv-what").textContent = `· ${inv.componentName}`;
  const c = (inv.components || []).find((x) => sameId(x.id, inv.component));
  const exceptionsFirst = c && (c.causes || []).includes("exceptions") && !c.causes.includes("failed");
  $("inv-ops").querySelector(".inv-parts").classList.toggle("exceptions-first", Boolean(exceptionsFirst));
  inv.found = {};
  invPart("codes", loadInvCodes);
  invPart("exceptions", loadInvExceptions);
  invPart("dependencies", loadInvDependencies);
  invPart("changes", () => loadInvChanges(c));
  invPart("endpoints", loadInvEndpoints);
}

// Step 2's sections fold: open by default, yours kept in this browser (Expand all / Collapse all only for this visit).
// A closed one isn't read until you open it, since 30 days of a busy app can take 10 to 30 seconds each.
const INV_PARTS = { codes: "inv-codes-part", endpoints: "inv-ops-part", exceptions: "inv-exc-part", dependencies: "inv-dep-part", changes: "inv-chg-part" };
inv.partLoaders = {};
function invPart(key, load) {
  const part = $(INV_PARTS[key]), kept = stored.json("ahc.invParts", {})[key];
  inv.partLoaders[key] = load;
  part.dataset.read = "";
  part.querySelector(".inv-part-n").textContent = "";
  if (part.open !== (kept ?? true)) { part.dataset.auto = "1"; part.open = kept ?? true; }
  if (part.open) { part.dataset.read = "1"; load(); } else part.querySelector("div[id$='-out']").innerHTML = '<p class="muted">Not read yet: it reads when you open it.</p>';
}
function invPartCount(key, n, noun) {
  const box = $(INV_PARTS[key]).querySelector(".inv-part-n");
  box.textContent = `· ${fmt(n)} ${noun}${n === 1 ? "" : "s"}`;
  box.classList.toggle("worse", n > 0 && key !== "changes");
}
document.addEventListener("toggle", (event) => {
  const part = event.target;
  if (!part.matches || !part.matches("details.inv-part")) return;
  if (part.dataset.auto) delete part.dataset.auto;
  else { const kept = stored.json("ahc.invParts", {}); kept[part.dataset.part] = part.open; stored.set("ahc.invParts", kept); }
  if (part.open && !part.dataset.read && inv.partLoaders[part.dataset.part]) { part.dataset.read = "1"; inv.partLoaders[part.dataset.part](); }
}, true);
document.addEventListener("click", (event) => {
  const all = event.target.closest && event.target.closest("[data-inv-parts]");
  if (!all) return;
  for (const part of document.querySelectorAll("details.inv-part")) {
    const open = all.dataset.invParts === "open";
    if (part.open !== open) { part.dataset.auto = "1"; part.open = open; }
  }
});

// API Management names an operation "<api>;rev=<n> - <operation>": shown as the operation, its method and path (a recent
// failed request's, no query string), and its API; any other name as it is. Its callers and backends fill in later.
const APIM_OP = /^(.+);rev=(\d+) - (.+)$/;
const epKey = (e) => JSON.stringify([e.operation || "", e.role || "", e.code || ""]);
function endpointHtml(e, { callers = true } = {}) {
  if (!e.operation) return '<span class="muted">(none)</span>';
  const m = APIM_OP.exec(e.operation || "");
  const path = e.path && e.path !== e.operation ? e.path : "";
  return `<strong>${esc(m ? m[3] : e.operation || "–")}</strong>${path
    ? `<div class="muted ai-msg" title="A recent failed request's method and path (without its query string)">${esc(path)}</div>` : ""}${m
    ? `<div class="muted">API ${esc(m[1])} · revision ${esc(m[2])}</div>` : ""}${callers ? `<div class="muted" data-ep-callers="${esc(epKey(e))}"></div>` : ""}`;
}
// An operation in plain text (step headings): "GetPrice (API product)" for API Management, else as it is.
const opText = (operation) => { const m = APIM_OP.exec(operation || ""); return m ? `${m[3]} (API ${m[1]})` : operation || "–"; };
// A service (cloud role): its name kept on one line, and what follows the first space (API Management's region) and the
// instance under it, so a long hyphenated name isn't broken at every hyphen in a narrow column.
function serviceHtml(role, instance = "") {
  if (!role && !instance) return '<span class="muted">(no name)</span>';
  const at = (role || "").indexOf(" "), name = at > 0 ? role.slice(0, at) : role || "", rest = at > 0 ? role.slice(at + 1) : "";
  return `<span class="inv-service">${esc(name)}</span>${rest ? `<div class="muted">${esc(rest)}</div>` : ""}${instance ? `<div class="muted">${esc(instance)}</div>` : ""}`;
}
const backendCell = (e) => `<div data-ep-backend="${esc(epKey(e))}" class="muted">reading…</div>`;
const BACKEND_NOTE = `<p class="muted"><strong>Backend:</strong> the calls these failing requests made that failed too (host, its
  status, how many requests), and how many failed with no backend call failing: those broke in the service itself (an API
  Management policy, for example). <strong>Called by:</strong> the API Management subscription that sent them.</p>`;
const hostOf = (url) => String(url || "").replace(/^[a-z]+:\/\//i, "").replace(/\/$/, "");

// Each listed endpoint's backends and callers (one read for the table), into the cells drawn for them.
async function loadEndpointDetails(box, endpoints) {
  if (!endpoints.length) return;
  const data = await api("api/investigate/endpoint-details", { params: invParams({ endpoints: JSON.stringify(endpoints) }) })
    .catch((error) => (error.signedOut ? null : { rows: [], errors: [error.message] }));
  if (!data || !box.isConnected) return;
  const found = new Map(data.rows.map((r) => [epKey(r), r]));
  const failedRead = (data.errors || []).join(" ");
  box.querySelectorAll("[data-ep-backend]").forEach((cell) => {
    const r = found.get(cell.dataset.epBackend);
    if (!r) { cell.innerHTML = failedRead ? `<span title="${esc(failedRead)}">couldn't be read</span>` : "–"; return; }
    const none = (r.failed || 0) - (r.with_backend || 0);
    const more = (r.backend_count || 0) - r.backends.length;
    cell.classList.remove("muted");
    cell.innerHTML = `${r.backends.map((b) => `<div><span class="ai-msg">${esc(hostOf(b.backend))}</span> ${b.code
      ? `<strong>${esc(b.code)}</strong>` : '<strong title="The call failed without an HTTP status: it timed out or couldn\'t connect">no response</strong>'}
      <span class="muted">· ${fmt(b.requests)}</span></div>`).join("")}${more > 0 ? `<div class="muted">+${fmt(more)} more backend(s)</div>` : ""}${none > 0
      ? `<div class="muted" title="No backend call of these requests failed: they broke in the service itself, or the backend answered and the service still failed">${r.backends.length ? "" : "no backend call failed · "}${fmt(none)} without a backend failure</div>` : ""}`;
  });
  box.querySelectorAll("[data-ep-callers]").forEach((cell) => {
    const r = found.get(cell.dataset.epCallers);
    if (r && r.callers.length) cell.innerHTML = `called by ${r.callers.map((c) => `${esc(c.caller)} <span title="failed requests">(${fmt(c.requests)})</span>`).join(" · ")}`;
  });
}

async function loadInvEndpoints() {
  const out = $("inv-ops-out");
  out.innerHTML = loading("Reading failing operations…");
  let data;
  try { data = await api("api/investigate/operations", { params: invParams() }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error) + invRetryHtml("endpoints");
    return;
  }
  inv.found.operations = data.rows;
  invPartCount("endpoints", data.rows.length, "endpoint");
  out.innerHTML = `${noticesHtml(data.errors)}${invRetryHtml("endpoints", data)}${data.rows.length ? BACKEND_NOTE + table(["Endpoint", "Service", `Failed in the ${invSpan()}`, "Out of", "Failure rate",
    "HTTP status codes", "Backend", "Most recent failure", ""],
    data.rows.map((r) => [endpointHtml(r), serviceHtml(r.role), `<span class="worse">${fmt(r.failed)}</span>`,
      fmt(r.total), pctText(r.failed_pct, r.failed), codes(r.codes), backendCell(r), seen(r.last_seen),
      `<button type="button" class="link" data-inv-op="${esc(JSON.stringify({ operation: r.operation, role: r.role }))}">Samples</button>`]), "inv-ops")
    : invNoneHtml(data, "failed requests")}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
  loadEndpointDetails(out, data.rows.map((r) => ({ operation: r.operation || "", role: r.role || "", code: "" })));
}

// A failed read (no `data`, or `data.errors`) gets its own Try again, which reads only that section again.
const INV_RETRY = { codes: () => loadInvCodes(), endpoints: () => loadInvEndpoints(), exceptions: () => loadInvExceptions(), dependencies: () => loadInvDependencies() };
function invRetryHtml(part, data) {
  return data && !(data.errors || []).length ? ""
    : `<p><button type="button" data-inv-retry="${part}">Try again</button> <span class="muted">reads only this part again</span></p>`;
}

// The all-clear only when every read worked: a source that failed (a timeout) may hold what's missing.
function invNoneHtml(data, what) {
  return (data.errors || []).length ? `<p class="muted">No ${what} in what could be read.</p>`
    : `<p class="ok">✓ No ${what} in the last ${esc(invWindow())}.</p>`;
}

// Failed requests by HTTP code: server errors, then cut short, then the caller's (4xx), each most first; a code opens to
// its busiest endpoints, whose Samples read only that code.
const CODE_ORDER = { server: 0, cut: 1, run: 2, client: 3 };
const CODE_MEANS = { server: "the service failed while answering", cut: "the response stopped before it was complete",
  run: "a run that failed, not called over HTTP", client: "the service refused the caller's request" };
const REASON_HINTS = { ClientConnectionFailure: "the caller closed the connection", Timeout: "the backend didn't answer in time",
  BackendConnectionFailure: "the backend couldn't be reached", ExpressionValueEvaluationFailure: "a policy expression failed",
  TokenExpired: "the caller's token had expired", RateLimitExceeded: "a rate limit was hit" };
function codeReasonsHtml(r) {
  if (!(r.reasons || []).length) return "";
  return `<p class="muted"><strong>Why:</strong> the exceptions logged on these requests, by reason and where it broke.</p>
    ${table(["Reason", "Where", "Requests", "Message", ""], r.reasons.map((x) => {
      const [type, where] = [x.type || "–", (x.problem || "").includes(" at ") ? x.problem.split(" at ").pop() : ""];
      const short = type.split(".").pop();
      return [`<strong>${esc(short)}</strong>${REASON_HINTS[short] ? `<div class="muted">${esc(REASON_HINTS[short])}</div>` : ""}`,
        where ? `<span class="ai-msg">${esc(where)}</span>` : '<span class="muted">–</span>', fmt(x.requests), `<span class="ai-msg muted">${esc(x.message || "")}</span>`,
        x.trace_id ? `<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: x.trace_id, at: x.last_seen }))}">Follow</button>` : ""];
    }), "inv-code-reasons")}`;
}
async function loadInvCodes() {
  const out = $("inv-codes-out");
  out.innerHTML = loading("Reading failures by HTTP code…");
  let data;
  try { data = await api("api/investigate/codes", { params: invParams() }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error) + invRetryHtml("codes");
    return;
  }
  const rows = [...data.rows].sort((a, b) => CODE_ORDER[codeKind(a.code)] - CODE_ORDER[codeKind(b.code)] || b.failed - a.failed);
  invPartCount("codes", rows.length, "code");
  out.innerHTML = `${noticesHtml(data.errors)}${invRetryHtml("codes", data)}${rows.length ? rows.map((r) => {
    const kind = codeKind(r.code), more = r.endpoints > r.top.length;
    const name = codeName(r.code);
    return `<details class="scope-group code-group ${kind}"><summary><span class="code-line"><strong class="code-num">${esc(codeOf(r.code))}</strong>
        <span class="code-name">${esc(name.charAt(0).toUpperCase() + name.slice(1))}</span> <span class="muted">${esc(CODE_MEANS[kind] || "")}</span></span>
        <span class="code-facts"><strong>${fmt(r.failed)}</strong> failed request${r.failed === 1 ? "" : "s"} in the ${esc(invSpan())}
        · <strong>${pctText(r.failed_pct, r.failed)}</strong> of all failures · <strong>${fmt(r.endpoints)}</strong> endpoint${r.endpoints === 1 ? "" : "s"}
        · most recent <strong>${esc(ago(r.last_seen))}</strong></span></summary>
      ${codeReasonsHtml(r)}
      ${r.top.length ? BACKEND_NOTE : ""}${table(["Endpoint", "Service", `${/^\d{3}$/.test(codeOf(r.code)) ? `${codeOf(r.code)}s` : "Failed"} in the ${invSpan()}`, "Backend", "Most recent", ""], r.top.map((e) => [endpointHtml({ ...e, code: r.code }),
        serviceHtml(e.role), `<span class="worse">${fmt(e.failed)}</span>`, backendCell({ ...e, code: r.code }), seen(e.last_seen),
        `<button type="button" class="link" data-inv-op="${esc(JSON.stringify({ operation: e.operation, role: e.role, code: r.code }))}">Samples</button>`]), "inv-codes")}
      ${more ? `<p class="muted">Its ${CODE_TOP_SHOWN} busiest of ${fmt(r.endpoints)} endpoints. Failing endpoints below has the rest by endpoint.</p>` : ""}
      ${r.trace_id ? `<p><button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id, at: r.last_seen }))}">Follow one</button> <span class="muted">a request that failed with ${esc(codeOf(r.code))}, end to end</span></p>` : ""}
    </details>`;
  }).join("") : invNoneHtml(data, "failed requests")}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
  out.querySelectorAll("details.code-group").forEach((group, i) => group.addEventListener("toggle", () => {
    if (!group.open || group.dataset.detailsRead) return;  // read when first opened: over 30 days it takes ~30 s
    group.dataset.detailsRead = "1";
    loadEndpointDetails(group, rows[i].top.map((e) => ({ operation: e.operation || "", role: e.role || "", code: rows[i].code || "" })));
  }));
}
const CODE_TOP_SHOWN = 5;

async function loadInvExceptions() {
  const out = $("inv-exc-out");
  out.innerHTML = loading("Reading exceptions…");
  let data;
  try { data = await api("api/investigate/exceptions", { params: invParams() }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error) + invRetryHtml("exceptions");
    return;
  }
  inv.found.exceptions = data.rows;
  invPartCount("exceptions", data.rows.length, "type");
  out.innerHTML = `${noticesHtml(data.errors)}${invRetryHtml("exceptions", data)}${data.rows.length ? table(["Exception", "Operation", "Service", `Count in the ${invSpan()}`, "Most recent", ""],
    data.rows.map((r) => [`<strong>${esc(r.type || "–")}</strong><div class="ai-msg muted">${esc(r.message || "")}</div>`,
      endpointHtml(r, { callers: false }), serviceHtml(r.role),
      `<span class="worse">${fmt(r.total)}</span>`, seen(r.last_seen),
      r.trace_id ? `<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id, at: r.last_seen, instance: r.instance }))}">Follow</button>` : ""]), "inv-exc")
    : invNoneHtml(data, "exceptions")}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
}

async function loadInvDependencies() {
  const out = $("inv-dep-out");
  out.innerHTML = loading("Reading failing dependencies…");
  let data;
  try { data = await api("api/investigate/dependencies", { params: invParams() }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error) + invRetryHtml("dependencies");
    return;
  }
  inv.found.dependencies = data.rows;
  invPartCount("dependencies", data.rows.length, "target");
  out.innerHTML = `${noticesHtml(data.errors)}${invRetryHtml("dependencies", data)}${data.rows.length ? table(["Type", "Target", "Service", `Failed in the ${invSpan()}`, "Out of", "Failure rate",
    "Result codes", "p95", "Most recent failure", ""], data.rows.map((r) => [esc(r.type || "–"), `<span class="ai-msg">${esc(r.target || "")}</span>`,
      serviceHtml(r.role), `<span class="worse">${fmt(r.failed)}</span>`, fmt(r.total),
      pctText(r.failed_pct, r.failed), codes(r.codes), msText(r.p95_ms), seen(r.last_seen),
      r.trace_id ? `<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id }))}">Follow</button>` : ""]), "inv-dep")
    : invNoneHtml(data, "failed dependency calls")}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
}

// The component, the apps that report to it and the gateways that log to it.
const invChangeTargets = (c) => [inv.component, ...((c && c.app_ids) || []),
  ...(inv.gateways || []).filter((g) => g.components.some((id) => sameId(id, inv.component))).map((g) => g.id)];

// A change's fields as Azure recorded them, before → after: "sku.capacity 3 → 4".
const changeFieldText = (f) => (f.before || f.after ? `${f.name} ${f.before || "(none)"} → ${f.after || "(none)"}` : f.name);
const changeFieldsText = (r) => esc([...r.fields.map(changeFieldText), ...(r.more_fields ? [`${r.more_fields} more`] : [])].join("; "));
const changeFieldsHtml = (r) => (r.fields.length ? `<span class="ai-msg">${r.fields.map((f) => esc(changeFieldText(f))).join("<br>")}
  ${r.more_fields ? `<br><span class="muted">${r.more_fields} more</span>` : ""}</span>` : '<span class="muted">–</span>');

async function loadInvChanges(c) {
  const out = $("inv-chg-out");
  out.innerHTML = loading("Reading recent changes…");
  let data;
  try { data = await api("api/investigate/recent-changes", { params: { id: invChangeTargets(c), window: invWindow() } }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  inv.found.changes = data.rows;
  invPartCount("changes", data.rows.length, "change");
  const before = data.before;
  out.innerHTML = `${data.rows.length ? table(["When", "Resource", "Change", "By", "What changed"], data.rows.map((r) => [invTime(r.at),
      `${esc(r.name)} ${portalLinkHtml(r.id)}`, esc(r.change),
      `${esc(r.by || "unknown")}<div class="muted">${esc(r.client || "")}</div>`, changeFieldsHtml(r)]), "inv-chg")
    : `<p class="muted">No changes in the last ${data.hours} h.${before ? ` The last one was ${esc(ago(before.at))}
        (${esc(new Date(before.at).toLocaleString())}) on ${esc(before.name)}: ${changeFieldsText(before) || esc(before.change)}.`
        : " None in the 14 days Azure keeps either."}</p>`}
    ${data.capped ? '<p class="muted">Changes go back 14 days at most: Azure keeps no older change history.</p>' : ""}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock({ queries: [{ label: "Resource Graph", query: data.query }] })}</details>`;
}

async function loadInvSamples(op, all = false) {
  invReset("inv-samples");
  if (JSON.stringify(op) !== JSON.stringify(inv.op)) inv.followedTraces = new Set();  // ✓ marks belong to one endpoint
  $("inv-samples").classList.remove("collapsed");
  $("inv-samples").querySelector(".inv-picked").classList.add("hidden");
  $("inv-samples").querySelector("[data-inv-back-samples]").classList.add("hidden");
  inv.op = op;
  inv.opAll = all;
  const box = invStep("inv-samples", loading("Reading sample requests…"));
  $("inv-samples").querySelector("h3 .inv-what").textContent = `· ${opText(op.operation)}${op.role ? ` · ${op.role}` : ""}${op.code ? ` · only ${codeOf(op.code)}` : ""}`;
  let data;
  try { data = await api("api/investigate/samples", { params: invParams({ ...op, all: all ? "1" : "0" }) }); } catch (error) {
    if (!error.signedOut) box.innerHTML = noticeHtml(error);
    return;
  }
  box.innerHTML = `<div class="table-tools"><label class="inline"><input type="checkbox" id="inv-all" ${all ? "checked" : ""}> successful ones too</label></div>
    ${noticesHtml(data.errors)}${table(["When", "HTTP status", "Took", "Instance (pod or host)", "URL", ""], data.rows.map((r) => [
      invTime(r.time), String(r.success).toLowerCase() === "true" ? esc(r.code) : `<span class="worse">${esc(r.code)}</span>`,
      msText(r.duration_ms), esc(r.instance || ""), `<span class="ai-msg">${esc(r.url || "")}</span>`,
      `${(inv.followedTraces || new Set()).has(r.trace_id) ? '<span class="inv-seen">✓ followed</span>' : ""}<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id, at: r.time, instance: r.instance, url: r.url }))}">Follow</button>`]), "inv-samples")}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
  $("inv-all").addEventListener("change", (event) => loadInvSamples(op, event.target.checked));
}

function invPath(url) {
  try { return new URL(url).pathname; } catch { return ""; }
}

function invSpanRow(s) {
  const failed = String(s.success).toLowerCase() === "false";
  const detail = s.kind === "exception" ? `<details><summary>${esc((s.message || "").slice(0, 300) || "(no message)")}</summary>
      ${s.stack ? `<pre>${esc(s.stack)}</pre>` : '<p class="muted">No stack was sent.</p>'}</details>`
    : `<span class="ai-msg">${esc((s.message || s.target || "").slice(0, 400))}</span>`;
  return `<tr class="${failed ? "inv-failed" : ""}"><td class="num" data-v="${Number(s.offset_ms)}">+${msLabel(s.offset_ms)}</td>
    <td>${pillHtml(INV_KIND_TONE[s.kind] || "info", s.kind)}${s.gateway ? ' <span class="chip-ok">gateway</span>' : ""}</td>
    <td style="padding-left:${0.5 + s.depth}rem">${esc(s.name || (s.severity ? s.severity : ""))}</td>
    <td>${serviceHtml(s.role, s.instance)}</td>
    <td>${s.duration_ms == null ? "" : msText(Math.round(s.duration_ms))}</td><td>${failed ? `<span class="worse">${esc(s.code || "failed")}</span>` : esc(s.code || "")}</td>
    <td>${detail}</td></tr>`;
}

// Plain text for an incident ticket, from what's already on the page: no new reads.
function invIncidentText() {
  const c = (inv.components || []).find((x) => sameId(x.id, inv.component)) || {};
  const f = inv.found || {}, t = inv.trace || { spans: [] };
  const top = (rows, n, line) => (rows && rows.length ? rows.slice(0, n).map((r) => `- ${line(r)}`) : ["- none"]);
  const codesOf = (r) => ((r.codes || []).length ? `, codes ${r.codes.join("/")}` : "");
  const reporters = c.apps ? invReporters(c) : [];
  const failed = t.spans.find((s) => !s.gateway && String(s.success).toLowerCase() === "false" && s.kind !== "exception");
  const exception = t.spans.find((s) => s.kind === "exception");
  const lines = [`Investigate: ${inv.componentName}${reporters.length ? ` (${reporters.join(", ")})` : ""}`,
    `Component: ${inv.component}`, `Look-back window: ${invWindow()}`];
  if (c.measured) {
    lines.push(`Last hour: ${c.failed} failed of ${c.requests} requests (${pctText(c.failed_pct, c.failed)}), ${c.server_errors ?? "?"} of them 5xx`
      + `${c.share != null ? ` (${pctText(c.share, c.server_errors)} of 5xx in scope)` : ""}, ${c.exceptions} exceptions`,
    `Same hour last week: ${c.week.failed} failed of ${c.week.requests} (${pctText(c.week.failed_pct, c.week.failed)}), ${c.week.exceptions} exceptions`);
  }
  lines.push("", "Failing endpoints:", ...top(f.operations, 3, (r) => `${r.operation || "–"} (${r.role || "no name"}): ${r.failed} of ${r.total} failed${codesOf(r)}`),
    "Exceptions:", ...top(f.exceptions, 3, (r) => `${r.type} x${r.total} in ${r.operation || "–"}: ${(r.message || "").slice(0, 160)}`),
    "Failing dependencies:", ...top(f.dependencies, 3, (r) => `${r.type} ${r.target}: ${r.failed} of ${r.total} failed${codesOf(r)}`),
    "Recent changes:", ...top(f.changes, 5, (r) => `${r.at} ${r.name}: ${r.change} by ${r.by || "unknown"}`));
  if (t.req) {
    lines.push("", `Request ${t.req.trace}${t.at ? ` at ${t.at}` : ""}: ${t.spans.length} spans`);
    if (failed) lines.push(`First failure: ${failed.kind} ${failed.name || ""} ${failed.code || ""} on ${failed.instance || "?"}`.replace(/\s+/g, " "));
    if (exception) lines.push(`Exception: ${exception.name}: ${(exception.message || "").slice(0, 300)}`);
  }
  return lines.join("\n");
}

async function loadInvRequest(req) {
  invReset("inv-request");
  inv.request = req;
  $("inv-request").querySelector("[data-inv-back-samples]").classList.toggle("hidden", $("inv-samples").classList.contains("hidden"));
  const box = invStep("inv-request", loading(`Reading request ${req.trace}…`));
  $("inv-request").querySelector("h3 .inv-what").textContent = `· ${req.trace}`;
  let data;
  try {
    data = await api("api/investigate/trace", { params: invParams({ trace: req.trace, also: invAlso(), ...(req.at ? { at: req.at } : {}) }) });
  } catch (error) {
    if (!error.signedOut) box.innerHTML = noticeHtml(error);
    return;
  }
  const spans = data.rows;
  const root = spans.find((s) => s.kind === "request" && !s.gateway) || {};
  const at = req.at || root.time || (spans[0] || {}).time;
  const instances = [...new Set(spans.filter((s) => !s.gateway && s.instance).map((s) => s.instance))];
  const path = invPath(req.url || root.target || "");
  const gateways = inv.gateways.filter((g) => g.workspace);
  const counts = ["request", "dependency", "exception", "trace"].map((k) => [k, spans.filter((s) => s.kind === k).length]).filter(([, n]) => n);
  inv.trace = { req, spans, at };
  box.innerHTML = `${noticesHtml(data.errors)}
    <p class="muted">${spans.length ? counts.map(([k, n]) => `${n} ${n === 1 ? k : INV_PLURAL[k]}`).join(" · ") : "No spans found for this ID"}
      ${data.truncated ? " · <strong>only the first 500 were read</strong>" : ""}${invAlso().length ? ` · also read ${esc(invAlso().map((c) => c.split("/").pop()).join(", "))}` : ""}</p>
    ${spans.length ? `<table class="data-table inv-trace"><thead><tr><th>At</th><th>What</th><th>Name</th><th>Role · instance</th><th>Duration</th><th>Result</th>
      <th>Detail</th></tr></thead><tbody>${spans.map(invSpanRow).join("")}</tbody></table>` : ""}
    <div class="inv-next"><button type="button" class="primary" data-inv-copy title="A plain-text summary of steps 1, 2 and this request, for the incident">Copy for incident</button>
      ${at ? instances.map((i) => `<button type="button" data-inv-pod="${esc(JSON.stringify({ instance: i, at }))}">Pod ${esc(i)}</button>`).join(" ") : ""}
      ${at ? gateways.map((g) => `<button type="button" data-inv-gwlogs="${esc(JSON.stringify({ id: g.id, name: g.name, at, path, dedicated: g.dedicated }))}"
        title="Its gateway logs share no ID with the request: rows within 2 minutes whose URL contains ${esc(path || "anything")}">Gateway logs: ${esc(g.name)}</button>`).join(" ") : ""}</div>
    <div id="inv-gw-out"></div>
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(data)}</details>`;
}

async function loadInvGatewayLogs(g) {
  const out = $("inv-gw-out");
  out.innerHTML = loading(`Reading ${g.name}'s gateway logs…`);
  let data;
  try { data = await api("api/investigate/gateway-logs", { params: { id: g.id, at: g.at, path: g.path, dedicated: g.dedicated ? "1" : "0" } }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  out.innerHTML = `<h4>${esc(g.name)}: gateway logs within 2 minutes${g.path ? `, URL containing <code>${esc(g.path)}</code>` : ""}</h4>
    <p class="muted">Probable: these rows share no ID with the request, only its time and path.${data.table ? ` From ${esc(data.table)}.` : ""}</p>
    ${noticesHtml(data.errors)}${data.rows.length ? table(["Time", "Method", "URL", "Status", "Backend", "Total", "Backend time", "Error"], data.rows.map((r) => [
      invTime(r.TimeGenerated), esc(r.Method || ""), `<span class="ai-msg">${esc(r.Url || "")}</span>`,
      Number(r.ResponseCode) >= 500 ? `<span class="worse">${esc(r.ResponseCode)}</span>` : esc(r.ResponseCode ?? ""),
      esc(r.BackendResponseCode ?? ""), msText(r.TotalTime), msText(r.BackendTime),
      `<span class="ai-msg">${esc([r.LastErrorReason, r.LastErrorMessage].filter(Boolean).join(": "))}</span>`]), "inv-gw")
      : '<p class="muted">No gateway log rows matched.</p>'}
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(data)}</details>`;
}

async function loadInvPod(p) {
  inv.pod = p;
  const box = invStep("inv-pod", loading(`Looking for pod ${p.instance} in the clusters in scope…`));
  $("inv-pod").querySelector("h3 .inv-what").textContent = `· ${p.instance}`;
  let data;
  try { data = await api("api/investigate/pod", { params: { ...scopeParams(currentScope()), instance: p.instance, at: p.at } }); } catch (error) {
    if (!error.signedOut) box.innerHTML = noticeHtml(error);
    return;
  }
  const f = data.found;
  const picker = `<div class="table-tools"><label>Cluster <select id="inv-cluster">${data.clusters.map((c) =>
      `<option value="${esc(c.id)}" ${f && sameId(c.id, f.cluster_id) ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>
    <label>Namespace <input id="inv-ns" value="${esc(f ? f.namespace : "")}" placeholder="e.g. shop" size="14"></label>
    <button type="button" id="inv-pod-read">${f ? "Read again" : "Read this pod"}</button></div>`;
  box.innerHTML = `${noticesHtml(data.notes)}
    ${f ? factsHtml({ facts: [["Cluster", f.cluster_name], ["Namespace", f.namespace], ["State then", f.status],
      ["Started", f.started ? `${new Date(f.started).toLocaleString()} (${ago(f.started)})` : "–"], ["Restarts", f.restarts ?? "–"],
      ["Owner", [f.controller_kind, f.controller].filter(Boolean).join(" ")], ["Node", f.node]].map(([label, value]) => ({ label, value })) })
      + (f.cluster_id ? `<p class="muted">${portalLinkHtml(f.cluster_id, { label: "Cluster in the portal ↗" })}
          ${portalLinkHtml(f.cluster_id, { label: "Its workloads ↗", page: "workloads", title: "The cluster's Workloads page in the Azure portal" })}</p>` : "")
      : `<p class="notice">No cluster's Container Insights knows a pod named ${esc(p.instance)} around then. If it ran on one of these, pick it and its namespace.</p>`}
    ${data.clusters.length ? picker : '<p class="muted">No AKS cluster in scope.</p>'}
    <div id="inv-pod-out"></div>
    <details class="mon-query"><summary>Query sent</summary>${queryBlock({ label: "Container Insights", queries: [{ label: "each cluster", query: data.query, timespan: data.timespan }] })}</details>`;
  const owner = f ? podWorkload(f.controller_kind, f.controller) : null;
  const read = () => loadInvPodDetail({ ...p, cluster: $("inv-cluster").value, namespace: $("inv-ns").value.trim(), owner });
  if ($("inv-pod-read")) $("inv-pod-read").addEventListener("click", read);
  if (f) read();
}

// The workload behind a pod's owner: a ReplicaSet's Deployment (its name less the pod-template hash), else the owner.
function podWorkload(kind, name) {
  if (!kind || !name) return null;
  if (kind === "ReplicaSet") return /-[a-z0-9]{5,10}$/.test(name) ? { kind: "Deployment", name: name.replace(/-[a-z0-9]{5,10}$/, "") } : null;
  return ["Deployment", "StatefulSet", "DaemonSet"].includes(kind) ? { kind, name } : null;
}

async function loadInvPodDetail(p) {
  const out = $("inv-pod-out");
  if (!p.cluster || !p.namespace) { out.innerHTML = noticeHtml("Pick the cluster and type the namespace."); return; }
  out.innerHTML = loading("Reading the pod's logs at that minute, its OOM kills, its events and the cluster's changes…");
  const [logs, oom, changes] = await Promise.all([
    api("api/investigate/pod-logs", { params: { id: p.cluster, namespace: p.namespace, pod: p.instance, at: p.at } }).catch((error) => ({ error: error.message })),
    api("api/investigate/pod-oom", { params: { id: p.cluster, pod: p.instance, at: p.at } }).catch((error) => ({ error: error.message, kills: [] })),
    api("api/investigate/changes", { params: { id: p.cluster, namespace: p.namespace, at: p.at } }).catch((error) => ({ errors: [error.message], changes: [], operations: [], events: [] }))]);
  const when = new Date(p.at).toLocaleTimeString();
  out.innerHTML = `<h4>Logs within 2 minutes of ${esc(when)} <span class="muted">· ${esc(logs.source || "")}</span></h4>
    ${logs.error ? noticeHtml(logs.error) : ""}${logs.error && !(logs.lines || []).length ? "" : logViewHtml(logs.lines || [])}
    <h4>Depends on <button type="button" class="link" id="inv-deps-read" title="Reads the pod's env and ConfigMaps for host names, and its identity's roles">Read</button></h4>
    <div id="inv-deps"></div>
    <h4>OOM kills of this pod, 30 minutes before to 5 after</h4>
    ${oom.error ? noticeHtml(oom.error) : (oom.kills || []).length ? oomTable(oom.kills, (ns, pod, container, at) => `<button type="button" class="link"
      data-inv-oom-logs="${esc(JSON.stringify({ namespace: ns, pod, container, at }))}">logs</button>`, "inv-oom") : '<p class="muted">None.</p>'}
    <h4>Warning events in ${esc(p.namespace)}, 15 minutes either side</h4>
    ${(changes.events || []).length ? table(["Last", "Reason", "Object", "Count", "Message"], changes.events.map((e) => [invTime(e.Last), esc(e.Reason),
      esc(`${e.Kind || ""} ${e.Object || ""}`.trim()), fmt(e.Count), `<span class="ai-msg">${esc(e.Message || "")}</span>`]), "inv-events")
      : '<p class="muted">None.</p>'}
    <h4>Changes on the cluster, 6 hours before to 10 minutes after</h4>${noticesHtml(changes.errors)}
    ${(changes.changes || []).length ? table(["When", "Change", "By", "Client"], changes.changes.map((c) => [invTime(c.at), esc(c.type), esc(c.by || ""), esc(c.client || "")]), "inv-changes")
      : '<p class="muted">No resource changes.</p>'}
    ${(changes.operations || []).length ? table(["When", "Operation", "Status", "Caller"], changes.operations.map((o) => [invTime(o.at), esc(o.operation),
      esc(o.status || ""), esc(o.caller || "")]), "inv-operations") : '<p class="muted">No control-plane operations.</p>'}`;
  $("inv-deps-read").addEventListener("click", () => loadDependencies($("inv-deps"), { id: p.cluster, namespace: p.namespace, pod: p.instance }));
  out.onclick = (event) => {
    const button = event.target.closest("[data-inv-oom-logs]");
    if (!button) return;
    const k = JSON.parse(button.dataset.invOomLogs);
    const name = `${esc(k.pod)}/${esc(k.container)}, 5 minutes either side of the kill`;
    toggleLogsRow(button, name, async (box, head) => {
      box.innerHTML = loading("Reading the killed instance's logs…");
      const data = await api("api/investigate/pod-logs", { params: { id: p.cluster, namespace: k.namespace, pod: k.pod, container: k.container,
        at: k.at, previous: "1", spread: 5 } }).catch((error) => ({ error: error.message }));
      head.innerHTML = `${name} <span class="muted">· ${esc(data.source || "")}</span>`;
      box.innerHTML = (data.error ? noticeHtml(data.error) : "") + (data.error && !(data.lines || []).length ? "" : logViewHtml(data.lines || []));
    });
  };
}

// Search logs names its component right there: the components in scope, worst first, or the one picked elsewhere.
function renderInvSearchApps() {
  const select = $("inv-search-app");
  const rows = inv.components || [];
  const known = rows.some((c) => sameId(c.id, inv.component));
  const options = rows.map((c) => `<option value="${esc(c.id)}" ${sameId(c.id, inv.component) ? "selected" : ""}>${esc(c.name)}${c.status && c.status !== "healthy" ? ` (${esc(c.status)})` : ""} · ${esc(c.resource_group || "")}</option>`);
  if (inv.component && !known) options.unshift(`<option value="${esc(inv.component)}" selected>${esc(inv.componentName || inv.component.split("/").pop())}</option>`);
  const empty = !currentScope().subscriptions.length ? "Apply a scope first"
    : $("inv-apps-out").querySelector(".loading") ? "Reading components…" : "None in scope (see Failing requests)";
  select.innerHTML = options.length ? `<option value="" ${inv.component ? "" : "selected"}>Choose one…</option>${options.join("")}` : `<option value="">${empty}</option>`;
  select.disabled = !options.length;
  $("inv-search-run").disabled = !inv.component;
  const out = $("inv-search-out");
  if (!inv.component && !out.querySelector("table")) out.innerHTML = `<p class="muted">${options.length
    ? "Choose the App Insights component to search: it's where the app sends its logs."
    : "Investigate reads the Application Insights components in the scope in use; apply a scope to list them."}</p>`;
}

async function runInvSearch() {
  const out = $("inv-search-out");
  if (!inv.component) { renderInvSearchApps(); return; }
  out.innerHTML = loading("Searching logs…");
  let data;
  try {
    data = await api("api/investigate/search", { params: invParams({ text: $("inv-search-text").value, severity: $("inv-severity").value }) });
  } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  out.innerHTML = `${queryBlock(data)}${noticesHtml(data.errors)}
    <p class="muted">${data.rows.length} line(s), newest first${data.truncated ? " · <strong>only the newest 100</strong>" : ""}.</p>
    ${table(["Time", "Level", "Kind", "Role · instance", "Operation", "Message", ""], data.rows.map((r) => [invTime(r.time), esc(r.severity ?? ""),
      esc(r.kind), serviceHtml(r.role, r.instance), endpointHtml(r, { callers: false }),
      `<span class="ai-msg">${esc((r.message || "").slice(0, 500))}</span>`,
      r.trace_id ? `<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id, at: r.time, instance: r.instance }))}">Follow</button>` : ""]), "inv-search")}`;
}

function showInvSub(sub) {
  inv.sub = sub;
  for (const b of $("inv-subtabs").querySelectorAll("[data-inv-sub]")) b.classList.toggle("active", b.dataset.invSub === sub);
  $("inv-flow").classList.toggle("hidden", sub !== "flow");
  $("inv-search").classList.toggle("hidden", sub !== "search");
  renderInvTrail();
  if (sub === "search") renderInvSearchApps();
  // A component picked on Search logs: its operations load when Failing requests is opened, not before.
  else if (inv.component && $("inv-ops").classList.contains("hidden") && !inv.pending) loadInvOperations();
}

// Refresh: the component list again, then every step that's open, in order, with the same picks.
async function refreshInvestigate() {
  if (inv.sub === "search") {
    if (inv.component && $("inv-search-out").innerHTML.trim()) await freshly(runInvSearch);
    return;
  }
  const open = (id) => !$(id).classList.contains("hidden");
  const keep = { component: inv.component, name: inv.componentName, op: open("inv-samples") ? inv.op : null, all: inv.opAll,
    request: open("inv-request") ? inv.request : null, pod: open("inv-pod") ? inv.pod : null };
  await freshly(async () => {
    inv.loaded = "";
    await loadInvestigate();
    if (!keep.component || !inv.loaded) return;
    invPick(keep.component, keep.name);
    await loadInvOperations();
    if (keep.op) await loadInvSamples(keep.op, keep.all);
    if (keep.request) await loadInvRequest(keep.request);
    if (keep.pod) await loadInvPod(keep.pod);
  });
}

// From another tab (App Insights' failing operations): open Investigate on that component and operation.
function investigate(component, operation = null, role = "", request = null) {
  inv.pending = { component, operation, role, request };
  showInvSub("flow");
  showView("investigate");
}

$("inv-apps-out").addEventListener("click", (event) => {
  const b = event.target.closest("[data-inv-app]");
  if (b) { invPick(b.dataset.invApp); loadInvOperations(); }
});
$("inv-current").addEventListener("click", (event) => {
  if (!event.target.closest("[data-inv-change]")) return;
  $("inv-current").classList.add("hidden");
  $("inv-tools").classList.add("hidden");
  $("inv-apps").classList.remove("collapsed");
  invReset("inv-ops");
  inv.component = "";
  renderInvSearchApps();
});
// One Look back at the top, for steps 2 to 4 and Search logs; step 1 is always the last hour, and the page says so.
function renderInvShowing() {
  const w = invWindow();
  $("inv-showing").textContent = `Showing: step 1 the last hour against the same hour last week (always) · steps 2 to 4 and Search logs the last ${w} · step 5 the minutes around the request`;
  document.querySelectorAll("[data-inv-span]").forEach((el) => { el.textContent = `· last ${w}`; });
}
$("inv-window").innerHTML = AI_WINDOWS.map((w) => `<option ${w === invWindow() ? "selected" : ""}>${w}</option>`).join("");
$("inv-window").addEventListener("change", (event) => {
  stored.set("ahc.invWindow", event.target.value);
  renderInvShowing();
  if (inv.component) loadInvOperations();
});
renderInvShowing();
$("inv-severity").innerHTML = INV_SEVERITIES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
$("inv-subtabs").addEventListener("click", (event) => { const b = event.target.closest("[data-inv-sub]"); if (b) showInvSub(b.dataset.invSub); });
$("inv-search-run").addEventListener("click", runInvSearch);
$("inv-search-app").addEventListener("change", (event) => {
  $("inv-search-out").innerHTML = "";
  if (event.target.value) invPick(event.target.value);
  else { inv.component = ""; $("inv-current").classList.add("hidden"); $("inv-tools").classList.add("hidden"); $("inv-apps").classList.remove("collapsed"); invReset("inv-ops"); renderInvSearchApps(); }
});
$("inv-refresh").addEventListener("click", refreshInvestigate);
$("inv-search-text").addEventListener("keydown", (event) => { if (event.key === "Enter") runInvSearch(); });
$("inv-trace-go").addEventListener("click", () => {
  const trace = $("inv-trace").value.trim();
  if (!trace) return;
  if (!inv.component) { flash("Pick the app it belongs to first (Investigate on its row), then Follow.", true); return; }
  showInvSub("flow");
  invReset("inv-samples");
  loadInvRequest({ trace });
});
$("inv-app-find").addEventListener("input", (event) => { inv.appFind = event.target.value; renderInvApps(); });
$("inv-apps-all").addEventListener("click", () => { inv.appsAll = !inv.appsAll; renderInvApps(); });
$("inv-gateways").addEventListener("change", (event) => {
  const box = event.target.closest("[data-inv-gw]");
  if (!box) return;
  if (box.checked) inv.untick.delete(box.dataset.invGw); else inv.untick.add(box.dataset.invGw);
});
$("view-investigate").addEventListener("click", (event) => {
  const hit = (attr) => event.target.closest(`[${attr}]`);
  let b;
  if ((b = hit("data-inv-op"))) { invCameFrom(b); loadInvSamples(JSON.parse(b.dataset.invOp)); }
  if ((b = hit("data-inv-unfold"))) invUnfold();
  else if ((b = hit("data-inv-back-samples"))) invBackToSamples();
  else if ((b = hit("data-inv-goto"))) {
    const target = $(b.dataset.invGoto), folded = target.classList.contains("collapsed");
    if (folded && target.id === "inv-ops") invUnfold();
    else if (folded && target.id === "inv-samples") invBackToSamples();
    else target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  else if ((b = hit("data-inv-retry"))) INV_RETRY[b.dataset.invRetry]();
  else if ((b = hit("data-inv-copy"))) copyText(invIncidentText(), b);
  else if ((b = hit("data-inv-follow"))) {
    if (!b.closest("#inv-samples")) invReset("inv-samples");  // from Exceptions or Search: no sample list behind it
    invCameFrom(b);
    invFollowedSample(b);
    showInvSub("flow");
    loadInvRequest(JSON.parse(b.dataset.invFollow));
  }
  else if ((b = hit("data-inv-pod"))) loadInvPod(JSON.parse(b.dataset.invPod));
  else if ((b = hit("data-inv-gwlogs"))) loadInvGatewayLogs(JSON.parse(b.dataset.invGwlogs));
});

