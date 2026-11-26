"use strict";

// ------------------------------------------------------------------ cost
// Browse's Cost card on a subscription or group (cost.summary), a resource's Cost tab, the Cost view (one job over the
// scope's subscriptions, cost.overview, plus Billed while idle from Resource Graph). Nothing reads costs until opened.

const moneyText = (v, cur = "") => (v == null ? "–"
  : `${cur ? `${cur} ` : ""}${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const moneyCell = (v, cur = "") => `<span data-v="${Number(v) || 0}">${esc(moneyText(v, cur))}</span>`;

// "+12%" against an earlier figure: a rise of more than 10% reads as worse, a fall as better.
// "Oct 1–13": last month's days this month is compared with (to the latest day both months have, from the data).
function costSameDays(data) {
  const to = (data.subscriptions || []).map((s) => s.same_days_to).filter(Boolean).sort().pop();
  if (!to) return "";
  const day = new Date(`${to}T00:00:00`);
  return `${day.toLocaleDateString(undefined, { month: "short" })} 1–${day.getDate()}`;
}

// "+12%", "−0.4%", "0%": one decimal under 1%, never "−0%". The one way every cost figure states a change.
function changeText(now, before) {
  const pct = ((now - before) / before) * 100;
  const shown = Math.abs(pct) < 1 ? Math.abs(pct).toFixed(1) : Math.abs(pct).toFixed(0);
  return Number(shown) === 0 ? "0%" : `${pct > 0 ? "+" : "−"}${shown}%`;
}

function costDeltaHtml(now, before) {
  if (!before) return now ? '<span class="muted">new</span>' : "";
  const pct = ((now - before) / before) * 100;
  return `<span class="${pct > 10 ? "worse" : pct < -10 ? "better" : "muted"}">${changeText(now, before)}</span>`;
}

// One bar per day, oldest left; hovering a bar names its day and amount.
function costBarsHtml(days, cur, { width = 300, height = 56 } = {}) {
  if (!days || !days.some((d) => d.cost)) return '<p class="muted">No costs in the last 30 days.</p>';
  const top = Math.max(...days.map((d) => d.cost), 0.01);
  const w = width / days.length;
  return `<svg class="cost-bars" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Cost per day, last ${days.length} days">${
    days.map((d, i) => {
      const h = d.cost > 0 ? Math.max(1, ((height - 2) * d.cost) / top) : 0;
      const today = i === days.length - 1;  // every cost chart ends today, which Azure is still filling in
      return `<rect ${today ? 'class="partial" ' : ""}x="${(i * w + 0.5).toFixed(1)}" y="${(height - h).toFixed(1)}" width="${Math.max(1, w - 1).toFixed(1)}" height="${h.toFixed(1)}"><title>${esc(d.date)}: ${esc(moneyText(d.cost, cur))}${today ? " so far (today isn't finished)" : ""}</title></rect>`;
    }).join("")}</svg><div class="cost-axis muted"><span>${esc(days[0].date)}</span><span>today, so far</span></div>`;
}

// A total's parts, each with its share as a bar.
function costPartsHtml(parts, cur, render = (p) => esc(p.name)) {
  const top = Math.max(...parts.map((p) => p.cost), 0.01);
  return `<ul class="cost-parts">${parts.map((p) => `<li><span class="cost-part-name">${render(p)}</span>
    <span class="cost-part-bar"><span style="width:${((100 * Math.max(p.cost, 0)) / top).toFixed(1)}%"></span></span>
    <span class="cost-part-n">${esc(moneyText(p.cost, cur))}</span></li>`).join("")}</ul>`;
}

const COST_ROLE_FIX = "Ask for Cost Management Reader (or Reader) on the subscription; if you're eligible through PIM, activate it, then Refresh access.";
const costNotice = (d) => `${noticeHtml(d.error)}${d.required ? `<p class="muted">${esc(COST_ROLE_FIX)}</p>` : ""}`;

function costSlot(id) {
  return `<div class="card cost-here gs-wide" data-cost-here="${esc(id)}"><h3>Cost</h3>${loading("Loading costs…")}</div>`;
}

async function fillCost(root = document) {
  for (const box of root.querySelectorAll("[data-cost-here]:not([data-filled])")) {
    box.dataset.filled = "1";
    const what = box.dataset.costHere.toLowerCase().includes("/resourcegroups/") ? "resource group" : "subscription";
    box.innerHTML = `<h3>Cost</h3>${loading(state.fresh ? `Getting the latest costs for this ${what} from Azure…` : "Loading costs…")}`;
    // Stored costs answer at once; still waiting after a moment means Azure is being read for the first time.
    const slow = state.fresh ? null : setTimeout(() => {
      box.innerHTML = `<h3>Cost</h3>${loading(`Reading this ${what}'s costs from Azure for the first time (5–15 s). Next time it opens at once.`)}`;
    }, 1200);
    let data;
    try { data = await api("api/cost/summary", { params: costParams({ id: box.dataset.costHere }) }); }
    catch (error) { clearTimeout(slow); box.innerHTML = `<h3>Cost</h3>${noticeHtml(error)}`; continue; }
    clearTimeout(slow);
    box.innerHTML = costSummaryHtml(data);
  }
}

const budgetWarn = () => Number(summaryCfg().budget_warn_percent) || 80;  // summary.budget_warn_percent, as Summary
function budgetHtml(b, cur) {
  const unit = b.currency || cur;
  const forecast = b.forecast != null && b.amount ? (b.forecast / b.amount) * 100 : null;
  const status = b.percent >= 100 ? "critical" : (b.percent >= budgetWarn() || (forecast ?? 0) >= 100) ? "warning" : "";
  return `<li><strong>${esc(b.name)}</strong> <span class="muted">${esc(b.time_grain)}</span> ${usageBar(b.percent, status)}
    <span>${esc(moneyText(b.spent, unit))} of ${esc(moneyText(b.amount, unit))} (${b.percent ?? "–"}%)${b.forecast != null
      ? ` <span class="muted">· forecast ${esc(moneyText(b.forecast, unit))}</span>` : ""}</span></li>`;
}

function costSummaryHtml(d) {
  const head = `<h3>Cost ${portalLinkHtml(d.scope, { label: "Cost analysis ↗", page: "costanalysis", title: "Cost analysis in the Azure portal" })}
    <button type="button" class="link cost-again" data-cost-again title="Read the costs again from Azure">Refresh</button></h3>`;
  if (d.error) return head + costNotice(d);
  const cur = d.currency;
  const sub = d.scope.split("/")[2];
  const partName = (p) => (p.subscription_level ? `<span title="${esc(SUBSCRIPTION_LEVEL_HELP)}">${esc(p.name)}</span>`
    : d.top_kind === "resource group" && !p.name.startsWith("(")
    ? `<button type="button" class="link" data-cost-group-open="${esc(`${sub}/${p.name}`)}">${esc(p.name)}</button>`
    : p.id ? `<button type="button" class="link" data-open="${esc(JSON.stringify({ kind: "resource", id: p.id, name: p.id.split("/").pop() }))}">${esc(p.id.split("/").pop())}</button> ${portalLinkHtml(p.id)}`
      : esc(p.name)) + (p.services && p.services.length ? `<div class="muted">${esc(p.services.join(", "))}</div>` : "");
  return `${head}
    <div class="cost-figures">
      <div><span class="big">${esc(moneyText(d.month_to_date, cur))}</span><div class="muted">this month${d.up_to ? `, up to ${esc(d.up_to)}` : ""}</div></div>
      <div><span class="big">${esc(moneyText(d.last_month_same_days, cur))}</span> ${costDeltaHtml(d.month_to_date, d.last_month_same_days)}<div class="muted">last month by the same day${d.same_days_to ? ` (to ${esc(d.same_days_to)})` : ""}</div></div>
      <div><span class="big">${esc(moneyText(d.last_month, cur))}</span><div class="muted">all of last month</div></div>
      <div title="Usage at its daily rate so far, plus purchases counted once (and last month's later ones expected again)"><span class="big">${d.projected != null ? `≈ ${esc(moneyText(d.projected, cur))}` : "–"}</span><div class="muted">where this month is heading</div></div>
      ${d.booked ? `<div><span class="big">${esc(moneyText(d.booked, cur))}</span><div class="muted">purchases this month (${esc((d.booked_days || []).map((b) => b.date.slice(5)).join(", "))}), in This month${d.booked_expected ? `; ${esc(moneyText(d.booked_expected, cur))} more expected as last month` : ""}</div></div>` : ""}
    </div>
    <h4>Last 30 days, usage</h4>${costBarsHtml(d.trend, cur)}
    <div class="cost-cols">
      <div><h4>By service, this month</h4>${d.services.length ? costPartsHtml(d.services, cur) : '<p class="muted">Nothing this month.</p>'}</div>
      <div><h4>Top ${esc(d.top_kind)}s, this month ${d.top_total > d.top.length ? `<span class="muted">${d.top.length} of ${d.top_total}</span>` : ""}</h4>
        ${d.top.length ? costPartsHtml(d.top, cur, partName) : '<p class="muted">Nothing this month.</p>'}</div>
    </div>
    <h4>Budgets</h4>${d.budget_note ? `<p class="muted">${esc(d.budget_note)}</p>` : d.budgets.length
      ? `<ul class="cost-budgets">${d.budgets.map((b) => budgetHtml(b, cur)).join("")}</ul>
        <p class="muted rule">Yellow from ${budgetWarn()}% spent, or a forecast past 100% · ${settingLink("summary.budget_warn_percent", "change")}</p>` : '<p class="muted">No budget is set here.</p>'}
    <p class="muted">Actual cost, as invoiced, ${d.read_now ? "updated just now from Azure and kept on this machine" : asOfHtml(d.read_at)}.
      Azure's figures for the last day or two are still filling in.</p>
    ${d.updating ? '<p class="muted cost-updating">Newer figures are being read in the background; Refresh in a minute to see them.</p>' : ""}
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(d)}</details>`;
}

// A Cost card's links: a group opens like a click on it in the tree, a resource opens in Browse, Refresh reads again.
function costCardClick(event) {
  const group = event.target.closest("[data-cost-group-open]");
  if (group) {
    const key = group.dataset.costGroupOpen;
    if (appliedGroups().some((g) => groupKey(g) === key)) openGroup(key);
    else flash("That group isn't in the scope in use (a tag filter leaves it out).", true);
    return true;
  }
  const again = event.target.closest("[data-cost-again]");
  if (again) {
    const box = again.closest("[data-cost-here]");
    delete box.dataset.filled;
    freshly(() => fillCost(box.parentElement));
    return true;
  }
  return false;
}

async function tabCost(node, body) {
  const d = await exploreGet("cost", node, costParams());
  const aks = node.type === AKS_TYPE ? `<p class="muted">A cluster's nodes, disks and load balancer are billed in its node
    resource group (usually <code>MC_…</code>): that group's Cost card has them.</p>` : "";
  if (d.error) { body.innerHTML = costNotice(d) + aks; return; }
  const cur = d.currency;
  body.innerHTML = `<div class="cost-figures">
      <div><span class="big">${esc(moneyText(d.month_to_date, cur))}</span><div class="muted">this month${d.up_to ? `, up to ${esc(d.up_to)}` : ""}</div></div>
      <div><span class="big">${esc(moneyText(d.last_30_days, cur))}</span><div class="muted">last 30 days</div></div>
      <div>${portalLinkHtml(`/${node.id.split("/").slice(1, 5).join("/")}`, { label: "Cost analysis ↗", page: "costanalysis", title: "Its resource group's Cost analysis in the Azure portal" })}</div>
    </div>${aks}
    <h4>Last 30 days</h4>${costBarsHtml(d.trend, cur)}
    <h4>By meter, last 30 days</h4>${d.meters.length ? costPartsHtml(d.meters, cur) : '<p class="muted">No costs recorded for it in the last 30 days.</p>'}
    <p class="muted">Actual cost, as invoiced, ${asOfHtml(d.read_at)}. Azure's figures for the last day or two are still filling in.</p>
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(d)}</details>`;
}

// ---- the Cost view

const COST_SUBS = ["spend", "waste"];
state.costSub = stored.choice("ahc.costSub", COST_SUBS, "spend");
state.costGroup = stored.choice("ahc.costGroup", ["subscription", "group", "tag"], "subscription");
// Actual (as invoiced) or amortized (reservations spread over their days, charged to what uses them): every cost read.
state.costType = stored.choice("ahc.costType", ["actual", "amortized"], "actual");
const costParams = (extra = {}) => ({ ...extra, ...(state.costType === "amortized" ? { cost_type: "amortized" } : {}) });
const costKey = (scope) => JSON.stringify({ ...scope, costType: state.costType });
const SUBSCRIPTION_LEVEL = "Subscription-level charges";
const TAG_KEY_CHARS = 32;  // tag pickers show keys and values this long at most (full text on hover)
const shortTag = (text) => (text.length > TAG_KEY_CHARS ? `${text.slice(0, TAG_KEY_CHARS - 1)}…` : text);
const SUBSCRIPTION_LEVEL_HELP = "Billed to the subscription, not to a resource group: reservations, savings plans, Defender plans, support, Marketplace";
const COST_KIND = { actual: "Actual cost, as invoiced", amortized: "Amortized cost: reservations spread over their days and charged to what uses them" };

function showCostSub(sub) {
  state.costSub = sub;
  stored.set("ahc.costSub", sub);
  for (const b of $("cost-subtabs").querySelectorAll("[data-cost-sub]")) b.classList.toggle("active", b.dataset.costSub === sub);
  $("cost-spend").classList.toggle("hidden", sub !== "spend");
  $("cost-waste").classList.toggle("hidden", sub !== "waste");
  const key = JSON.stringify(currentScope());
  if (sub === "spend" && state.costLoaded !== costKey(currentScope())) loadCost();
  if (sub === "waste" && state.wasteLoaded !== key) loadWaste();
}

function costExports(on) {
  for (const fmt of ["md", "csv"]) $(`cost-export-${fmt}`).disabled = !on;
}

async function loadCost() {
  const scope = currentScope();
  state.costLoaded = costKey(scope);
  for (const b of $("cost-type").querySelectorAll("[data-cost-type]")) b.classList.toggle("active", b.dataset.costType === state.costType);
  const token = (state.costToken = (state.costToken || 0) + 1);
  costExports(false);
  state.cost = null;
  $("cost-counters").innerHTML = "";
  $("cost-progress").innerHTML = "";
  if (!scope.subscriptions.length) {
    $("cost-body").innerHTML = pickScopeHtml("Cost reads the costs of the subscriptions in scope.");
    return;
  }
  $("cost-body").innerHTML = loading(`Reading costs for ${scope.subscriptions.length} subscription(s)…`);
  const started = Date.now();
  let baseline = null;
  try {
    const { job_id } = await api("api/cost", { method: "POST", body: costParams({ subscriptions: scope.subscriptions }) });
    state.costJob = job_id;
    const job = await pollJob(job_id, { isCurrent: () => token === state.costToken, failed: "Reading costs failed.", every: 1500,
      onRunning: (j) => {
        if (j.partial && baseline === null) baseline = j.partial.refreshing ? 0 : j.partial.read;  // already kept: arrived at once, not a rate
        renderCostProgress(j.partial, scope.subscriptions.length, started, baseline || 0);
        if (j.partial) { state.cost = j.partial; renderCost(); }
      } });
    if (!job) return;
    $("cost-progress").innerHTML = "";
    if (job.status === "cancelled") { $("cost-progress").innerHTML = '<p class="muted">Stopped. What was read is shown and kept; Refresh reads the rest.</p>'; return; }
    state.cost = job.result;
    renderCost();
    costExports(true);
    doneToast("cost", `Costs for ${scope.subscriptions.length} subscription(s) are read`);
  } catch (error) {
    if (token === state.costToken) { state.costLoaded = ""; $("cost-progress").innerHTML = ""; $("cost-body").innerHTML = noticeHtml(error); }
  }
}

function renderCostProgress(partial, total, started, baseline = 0) {
  if (partial && partial.refreshing) {
    const done = partial.refreshed, of = partial.to_refresh;
    const seconds = (Date.now() - started) / 1000;
    const left = done ? Math.max(0, Math.round(((seconds / done) * (of - done)) / 60)) : null;
    $("cost-progress").innerHTML = `<div class="notice partial">${loading(`Updating costs: ${done} of ${of} subscription(s) reread${
      left !== null && done < of ? ` · about ${left < 1 ? "under a minute" : `${left} min`} left` : ""}. The figures below are the kept ones
      until each subscription is updated.`)} <button type="button" class="link" id="cost-cancel">Stop</button></div>`;
    return;
  }
  const read = partial ? partial.read : 0;
  const seconds = (Date.now() - started) / 1000;
  const fresh = read - baseline;
  const left = fresh > 0 ? Math.max(0, Math.round(((seconds / fresh) * (total - read)) / 60)) : null;
  $("cost-progress").innerHTML = `<div class="notice partial">${loading(`${read} of ${total} subscription(s) read${left !== null && read < total
    ? ` · about ${left < 1 ? "under a minute" : `${left} min`} left (Azure throttles cost reads to about 20 subscriptions a minute)` : ""}`)}
    <button type="button" class="link" id="cost-cancel">Stop</button></div>`;
}

// Cost rows for the chosen grouping, each {name, sub?, key?, month_to_date, last_month, last_month_same_days, booked, last_30_days,
// last_7_days, previous_7_days, change, trend, spike?}; the 7-day figures and the trend are usage (no purchases).
// The Cost view's data for the subscription picked in its own filter (all of the scope when none is).
function costView(data) {
  const pick = $("cost-sub").value;
  const subs = data.subscriptions.map((s) => s.subscription_id);
  $("cost-sub").innerHTML = `<option value="">All subscriptions (${subs.length})</option>${[...subs].sort((a, b) => subName(a).localeCompare(subName(b)))
    .map((id) => `<option value="${esc(id)}" ${id === pick ? "selected" : ""}>${esc(subName(id))}</option>`).join("")}`;
  $("cost-sub").classList.toggle("hidden", subs.length < 2);
  if (!pick || !subs.includes(pick)) return data;
  const only = (rows) => rows.filter((r) => r.subscription_id === pick);
  return { ...data, subscriptions: only(data.subscriptions), groups: only(data.groups), spikes: only(data.spikes) };
}

function costRows(data) {
  if (state.costGroup === "subscription") return data.subscriptions.map((s) => ({ ...s, name: subName(s.subscription_id) }));
  const scoped = currentScope().groups ? new Set(appliedGroups().map(groupKey)) : null;
  const groups = data.groups.filter((g) => !scoped || !g.grouped || scoped.has(`${g.subscription_id}/${g.name}`));
  if (state.costGroup === "group") return groups.map((g) => ({ ...g, key: g.grouped ? `${g.subscription_id}/${g.name}` : null }));
  const tag = $("cost-tag").value;
  const tagsOf = new Map(state.groups.map((g) => [groupKey(g), g.tags || {}]));
  // Every group with the tag in the subscriptions shown, costs or not: a group that cost nothing still belongs to its value.
  const shownSubs = new Set(data.subscriptions.map((s) => s.subscription_id));
  const tagged = {};
  for (const g of state.groups) {
    if (shownSubs.has(g.subscription_id) && (!scoped || scoped.has(groupKey(g)))) {
      const value = (g.tags || {})[tag] || "(no tag)";
      tagged[value] = (tagged[value] || 0) + 1;
    }
  }
  const out = new Map();
  for (const g of groups) {
    const value = g.grouped ? (tagsOf.get(`${g.subscription_id}/${g.name}`) || {})[tag] || "(no tag)" : SUBSCRIPTION_LEVEL;
    const row = out.get(value) || { name: value, count: 0, month_to_date: 0, last_month: 0, last_month_same_days: 0, booked: 0, last_30_days: 0,
      last_7_days: 0, previous_7_days: 0, change: 0, trend: g.trend.map(() => 0) };
    row.count += 1;
    if (g.spike) row.spikes = [...(row.spikes || []), g];
    for (const k of ["month_to_date", "booked", "last_30_days", "last_7_days", "previous_7_days", "change"]) row[k] += g[k] || 0;
    for (const k of ["last_month", "last_month_same_days"]) row[k] = row[k] == null || g[k] == null ? null : row[k] + g[k];
    g.trend.forEach((v, i) => { row.trend[i] += v; });
    out.set(value, row);
  }
  return [...out.values()].map((row) => ({ ...row, tagged: row.name === SUBSCRIPTION_LEVEL ? row.count : tagged[row.name] || row.count }));
}

function renderCost() {
  if (!state.cost) return;
  const data = costView(state.cost);
  const all = state.cost, unreadable = (all.refused || []).length + (all.failed || []).length;
  $("cost-meta").innerHTML = `${esc(currentScope().label)} · ${all.subscriptions.length} of ${all.total || all.subscriptions.length} subscription(s)${
    unreadable ? ` (${unreadable} not readable)` : ""}${data.done ? ` · ${asOfHtml(data.read_at)}` : ""}${data.done && data.updating
    ? ` · <span class="cost-updating">updating ${data.updating} older than 6 h in the background (Refresh in a few minutes)</span>` : ""}`;
  const single = data.currencies.length <= 1;
  const cur = data.currency;
  const sum = (k) => data.subscriptions.reduce((n, s) => n + s[k], 0);
  for (const b of $("cost-group").querySelectorAll("[data-cost-group]")) b.classList.toggle("active", b.dataset.costGroup === state.costGroup);
  const keys = Object.keys(tagCounts(state.groups)).sort(byName);
  const kept = $("cost-tag").value;
  // Tag keys can be any length: each shows at most TAG_KEY_CHARS, the full key is its value and its tooltip.
  $("cost-tag").innerHTML = keys.map((k) => `<option value="${esc(k)}" title="${esc(k)}" ${k === kept ? "selected" : ""}>${esc(shortTag(k))}</option>`).join("");
  $("cost-tag").title = $("cost-tag").value;
  $("cost-tag").classList.toggle("hidden", state.costGroup !== "tag");
  const amount = (v) => `<span class="unit">${esc(cur)}</span>${esc(moneyText(v))}`;
  const sameDays = costSameDays(data);
  const pending = data.subscriptions.filter((s) => s.last_month_same_days == null).length;  // last month still being read
  $("cost-counters").innerHTML = single ? [
    ["This month", amount(sum("month_to_date")), pending ? "reading last month…"
      : sameDays ? `${costDeltaHtml(sum("month_to_date"), sum("last_month_same_days")) || "–"} vs ${esc(sameDays)}` : ""],
    ["Last 30 days", amount(sum("last_30_days")), ""],
    ["Last 7 days", amount(sum("last_7_days")), `${costDeltaHtml(sum("last_7_days"), sum("previous_7_days")) || "–"} vs the 7 days before`],
    ["Spikes", esc(String(data.spikes.length)), ""]].map(([label, n, extra]) => `<div class="counter ${label === "Spikes" && data.spikes.length ? "warning" : "info"}">
      <span class="n">${n}</span><span class="label">${esc(label)}</span>${extra ? `<br><span class="label">${extra}</span>` : ""}</div>`).join("") : "";
  const notes = [
    !single ? `These subscriptions bill in different currencies (${esc(data.currencies.join(", "))}), so nothing is added up across them.` : "",
    data.refused.length ? `Costs aren't readable in ${data.refused.length} subscription(s): ${esc(data.refused.map(subName).join(", "))}. ${esc(COST_ROLE_FIX)}` : "",
    data.failed.length ? `Azure didn't answer for ${data.failed.length} subscription(s) (${esc(data.failed.map((f) => `${subName(f.subscription_id)}: ${f.error}`).join("; "))}); Refresh tries again.` : "",
    currentScope().groups && state.costGroup === "subscription" ? "A tag filter is applied: subscription totals are whole subscriptions; group by resource group to see only the groups in scope." : ""]
    .filter(Boolean).map((t) => `<p class="notice partial">${t}</p>`).join("");
  const text = $("cost-filter").value.trim().toLowerCase();
  const sort = $("cost-sort").value;
  const rows = costRows(data).filter((r) => !text || r.name.toLowerCase().includes(text) || (r.subscription_id && subName(r.subscription_id).toLowerCase().includes(text)));
  const monthRise = (r) => (r.last_month_same_days == null ? 0 : r.month_to_date - r.last_month_same_days);
  rows.sort((a, b) => (sort === "fall" ? a.change - b.change : sort === "month_rise" ? monthRise(b) - monthRise(a) : (b[sort] || 0) - (a[sort] || 0)));
  const money = (v, r) => moneyCell(v, single ? cur : (data.subscriptions.find((s) => s.subscription_id === r.subscription_id) || {}).currency || "");
  const nameCell = (r) => {  // the ⚠ goes right after the name, before a group's subscription line
    const html = nameOnly(r), at = html.indexOf('<div class="muted">');
    return at < 0 ? html + spikeMark(r) : html.slice(0, at) + spikeMark(r) + html.slice(at);
  };
  const nameOnly = (r) => (state.costGroup === "subscription"
    ? `<button type="button" class="link" data-cost-sub-open="${esc(r.subscription_id)}">${esc(r.name)}</button> ${portalLinkHtml(`/subscriptions/${r.subscription_id}`, { label: "↗", page: "costanalysis", title: "Cost analysis in the Azure portal" })}`
    : state.costGroup === "group" ? `${r.key ? `<button type="button" class="link" data-cost-group-open="${esc(r.key)}">${esc(r.name)}</button>`
      : `<span title="${esc(SUBSCRIPTION_LEVEL_HELP)}">${esc(r.name)}</span>`}<div class="muted">${esc(subName(r.subscription_id))}</div>`
      : r.name === SUBSCRIPTION_LEVEL ? `<strong title="${esc(SUBSCRIPTION_LEVEL_HELP)}">${esc(r.name)}</strong>`
        : `<strong>${esc(r.name)}</strong> <span class="muted">${r.tagged} group(s)${r.count < r.tagged ? `, ${r.count} with costs` : ""}</span>`);
  const spikeCell = (r) => (r.spike ? `<span class="worse">${esc(moneyText(r.spike.cost, cur))}</span> <span class="muted">on ${esc(r.spike.date)}, usually ${esc(moneyText(r.spike.usual, cur))}</span>` : "");
  // A row with a spike (its own, or a group's in it) gets a ⚠ whose hover says which; the Spikes card above lists them all.
  const spikesIn = (r) => (r.spike ? [r] : state.costGroup === "subscription" ? data.spikes.filter((g) => g.subscription_id === r.subscription_id) : r.spikes || []);
  const spikeMark = (r) => {
    const found = spikesIn(r);
    return found.length ? ` <span class="worse" title="${esc(found.map((g) => `Spike in ${g.name}: ${moneyText(g.spike.cost, cur)} on ${g.spike.date}, usually ${moneyText(g.spike.usual, cur)}`).join("\n"))}">⚠</span>` : "";
  };
  const columns = ["", "This month", "Last month", "Last 30 days", "Last 7 days", "Change"];
  columns[0] = { subscription: "Subscription", group: "Resource group", tag: $("cost-tag").value || "Tag" }[state.costGroup];
  const spikes = data.spikes.length ? `<div class="card warning"><h3>Spikes <span class="count">${data.spikes.length}</span></h3>
    <p class="desc">Resource groups whose cost two days ago (the latest full day) was at least twice their usual day.</p>
    <ul>${data.spikes.slice(0, 10).map((g) => `<li class="static">${pill("warning")} <strong>${esc(g.name)}</strong> <span class="muted">${esc(subName(g.subscription_id))}</span>
      <div>${spikeCell(g)}</div></li>`).join("")}</ul></div>` : "";
  // All of last month, and this month's change against the same days of it (a half month against a whole one would always fall).
  const sameCell = (r) => (r.last_month == null ? '<span data-v="0" class="muted" title="The rest of last month is being read in the background">reading…</span>'
    : `<span data-v="${monthRise(r)}">${money(r.last_month, r)}</span>${sameDays ? `<div class="muted" title="This month so far against ${esc(sameDays)}: ${esc(moneyText(r.last_month_same_days, cur))}">${
      costDeltaHtml(r.month_to_date, r.last_month_same_days) || "–"} vs ${esc(sameDays)}</div>` : ""}`);
  $("cost-body").innerHTML = `${notes}${spikes}${rows.length ? table(columns, rows.map((r) => [nameCell(r), money(r.month_to_date, r), sameCell(r), money(r.last_30_days, r),
    money(r.last_7_days, r), `<span data-v="${r.change}">${costDeltaHtml(r.last_7_days, r.previous_7_days) || "–"}</span>`]), `cost-${state.costGroup}`)
    : data.read ? '<p class="muted">Nothing matches.</p>' : ""}
    <p class="muted">${esc(COST_KIND[data.cost_type || state.costType])}. This month is compared with the same days of last month;
      purchases such as reservations aren't counted as spikes.</p>
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(data)}</details>`;
  if (state.view === "summary") renderSummaryCost(state.cost);
}

async function loadWaste() {
  const scope = currentScope();
  state.wasteLoaded = JSON.stringify(scope);
  if (!scope.subscriptions.length) { $("waste-body").innerHTML = pickScopeHtml("Billed while idle reads the resources in scope."); return; }
  $("waste-body").innerHTML = loading("Looking for resources billed while idle…");
  let data;
  try { data = await api("api/cost/waste", { params: scopeParams(scope) }); }
  catch (error) { state.wasteLoaded = ""; $("waste-body").innerHTML = noticeHtml(error); return; }
  if (data.error) { $("waste-body").innerHTML = noticeHtml(data.error); return; }
  state.waste = data;
  renderWaste();
}

function renderWaste() {
  const data = state.waste;
  const counts = {};
  for (const r of data.rows) counts[r.finding] = (counts[r.finding] || 0) + 1;
  const body = (part) => `<p class="muted">${esc(part[0].action)}</p>${table(["Resource", "Where", "Detail"], part.map((r) => [
    `<button type="button" class="link" data-open="${esc(JSON.stringify({ kind: "resource", id: r.id, name: r.name }))}">${esc(r.name)}</button> ${portalLinkHtml(r.id)}`,
    `${esc(r.resource_group)}<div class="muted">${esc(subName(r.subscription_id))}${r.location ? ` · ${esc(r.location)}` : ""}</div>`, esc(r.detail)]), "cost-waste")}`;
  $("waste-body").innerHTML = `${data.rows.length ? `<p class="muted">${data.rows.length.toLocaleString()} resource(s). Checked: ${esc(data.checks.join("; "))}.</p>
    ${groupedHtml("waste", data.rows, { groupOf: (r) => ({ key: r.finding, label: r.finding }), body,
      order: (a, b) => b.rows.length - a.rows.length || byName(a.label, b.label) })}`
    : `<p class="ok">✓ Nothing billed while idle. Checked: ${esc(data.checks.join("; "))}.</p>`}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
}

$("cost-subtabs").addEventListener("click", (event) => { const b = event.target.closest("[data-cost-sub]"); if (b) showCostSub(b.dataset.costSub); });
$("cost-group").addEventListener("click", (event) => {
  const b = event.target.closest("[data-cost-group]");
  if (!b) return;
  state.costGroup = b.dataset.costGroup;
  stored.set("ahc.costGroup", state.costGroup);
  renderCost();
});
$("cost-type").addEventListener("click", (event) => {
  const b = event.target.closest("[data-cost-type]");
  if (!b || b.dataset.costType === state.costType) return;
  state.costType = b.dataset.costType;
  stored.set("ahc.costType", state.costType);
  for (const key of [...exploreCache.keys()]) if (key.split("|")[1] === "cost") exploreCache.delete(key);
  loadCost();
});
for (const id of ["cost-tag", "cost-sort", "cost-sub"]) $(id).addEventListener("change", renderCost);
$("cost-filter").addEventListener("input", debounce(renderCost, 150));
$("cost-refresh").addEventListener("click", () => freshly(() => (state.costSub === "waste" ? loadWaste() : loadCost())));
$("cost-progress").addEventListener("click", async (event) => {
  if (event.target.id !== "cost-cancel" || !state.costJob) return;
  try { await api(`api/jobs/${state.costJob}/cancel`, { method: "POST", body: {} }); } catch { /* already finished */ }
});
for (const id of ["cost-body", "waste-body"]) {
  $(id).addEventListener("click", (event) => {
    const open = event.target.closest("[data-open]");
    if (open) { startSnapshot(JSON.parse(open.dataset.open)); return; }
    const sub = event.target.closest("[data-cost-sub-open]");
    if (sub) { showView("browse"); showSubscriptionSummary(sub.dataset.costSubOpen); return; }
    const group = event.target.closest("[data-cost-group-open]");
    if (group) { showView("browse"); costCardClick(event); }
  });
}
for (const fmt of ["md", "csv"]) {
  $(`cost-export-${fmt}`).addEventListener("click", async () => {
    if (!state.costJob) return;
    try { await download(`api/cost/${state.costJob}/export`, { format: fmt }, `costs.${fmt}`); }
    catch (error) { $("cost-body").insertAdjacentHTML("afterbegin", noticeHtml(error)); }
  });
}

