"use strict";

// ------------------------------------------------------------------ summary
// The headline of each view in a few lines, for anyone who needs "are we OK" before the detail.

state.summaryParts = {};

// Summary: one card per view's headline for the scope in use. The persona picks the cards and their order (`summary`
// in personas; empty = every card); counts and thresholds come from Settings → Summary.
const SUMMARY_TITLES = { problems: ["Problems now", "triage"], changes: ["What changed", ""], cost: ["Cost", "cost"],
  security: ["Security", "security"], readiness: ["Readiness", "readiness"], access: ["Your access", "access"] };
const SUMMARY_RECORDED = ["problems", "security", "cost", "readiness"];  // what What changed compares
const STATUS_WORD = { critical: "red", warning: "yellow", healthy: "green" };
state.summaryCards = [];
const summaryCfg = () => state.summaryCfg || {};
const summaryTop = () => Math.max(1, Number(summaryCfg().top_items) || 1);

function summaryCardKeys() {
  const wanted = (state.persona && state.persona.summary && state.persona.summary.length) ? state.persona.summary : state.summaryCards;
  return wanted.filter((k) => SUMMARY_TITLES[k]);
}

function summaryCard(key, body) {
  const [title, view] = SUMMARY_TITLES[key];
  return `<div class="card summary-card" data-summary="${key}"><h3>${esc(title)}
    ${view ? `<button type="button" class="link" data-show-view="${view}">Open ${esc(title)} →</button>` : ""}</h3>${body}</div>`;
}

// `tone` colours the card's chip in the status line (critical, warning, healthy, or none); `items` feed What changed.
function setSummary(key, html, text, { tone, items } = {}) {
  const box = document.querySelector(`[data-summary="${key}"]`);
  if (box) box.innerHTML = box.querySelector("h3").outerHTML + html;
  state.summaryParts[key] = text;
  if (tone !== undefined) { state.summaryTones[key] = tone; renderSummaryStatus(); }
  if (items !== undefined) summaryRecorded(key, items);
  $("summary-line").textContent = summaryCardKeys().map((k) => state.summaryParts[k]).filter(Boolean).slice(0, 2).join(" ");
}

function summaryStatusText() {
  return summaryCardKeys().filter((k) => k in state.summaryTones).map((k) => `${SUMMARY_TITLES[k][0]}: ${
    STATUS_WORD[state.summaryTones[k]] || "not checked"}`).join(" · ");
}

// A chip's card: scrolled into view when it isn't, then outlined for a moment (so a click always shows something).
function spotlightSummary(key) {
  const card = document.querySelector(`[data-summary="${key}"]`);
  if (!card) return;
  const box = card.getBoundingClientRect();
  if (box.top < 0 || box.top > window.innerHeight * 0.6) card.scrollIntoView({ behavior: "smooth", block: "start" });  // its heading in view
  for (const c of document.querySelectorAll(".summary-card.spotlight")) c.classList.remove("spotlight");
  void card.offsetWidth;  // restart the animation on a second click
  card.classList.add("spotlight");
  state.summarySpot = key;
  for (const chip of document.querySelectorAll("[data-summary-jump]")) chip.setAttribute("aria-pressed", String(chip.dataset.summaryJump === key));
}

function renderSummaryStatus() {
  $("summary-status").innerHTML = summaryCardKeys().filter((k) => k in state.summaryTones).map((k) => {
    const tone = state.summaryTones[k] || "unknown";
    return `<button type="button" class="status-chip ${tone}" data-summary-jump="${k}" aria-pressed="${state.summarySpot === k}" title="${esc(STATUS_WORD[tone] || "not checked")}: show its card">
      <span class="dot ${tone}" aria-hidden="true"></span>${esc(SUMMARY_TITLES[k][0])}</button>`;
  }).join("");
}

// An item opens where its finding is shown: the resource (on `tab` when given), or a view (`view`).
const summaryLi = (item, body, { tab = "", view = "" } = {}) => `<li ${view ? `data-show-view="${esc(view)}"`
  : item.id ? `data-open="${esc(JSON.stringify({ kind: "resource", id: item.id, name: item.name }))}"${tab ? ` data-tab="${esc(tab)}"` : ""}`
  : 'class="static"'}>${body}${view ? ` ${browseButtonHtml("Show")}` : item.id ? ` ${browseButtonHtml()}` : ""}</li>`;

async function loadSummary(refresh = false) {
  const scope = currentScope();
  state.summaryLoaded = JSON.stringify(scope);
  const token = (state.summaryToken = (state.summaryToken || 0) + 1);
  state.summaryParts = {};
  state.summaryTones = {};
  state.summaryItems = {};
  state.summarySpot = "";
  $("summary-line").textContent = "";
  $("summary-status").innerHTML = "";
  if (!scope.subscriptions.length) {
    $("summary-meta").textContent = "";
    $("summary-cards").innerHTML = pickScopeHtml("Summary reads the subscriptions in scope.");
    return;
  }
  const keys = summaryCardKeys();
  const expected = SUMMARY_RECORDED.filter((k) => keys.includes(k));  // only what this persona shows
  state.summaryWaiting = new Set(expected);
  state.summaryChanges = { expected, answered: new Set(), areas: {}, missing: [], unread: [], since: undefined };
  state.summaryCurrent = () => token === state.summaryToken;
  $("summary-meta").innerHTML = `${esc(scope.label)} · ${asOfHtml()}`;
  const first = { problems: loading("Checking the scope for problems…"), changes: loading("Looking for an earlier summary of this scope…"),
    cost: loading("Reading costs…"), security: loading("Reading exposure, settings and vulnerabilities…"),
    readiness: loading("Running Readiness's quick checks…"), access: loading("Reading what your access covers…") };
  $("summary-cards").innerHTML = keys.map((k) => summaryCard(k, first[k])).join("");
  const run = (work) => (refresh ? freshly(work) : work());
  const loaders = { problems: summaryProblems, cost: summaryCost, security: summarySecurity, readiness: summaryReadiness, access: summaryAccess };
  if (expected.length) summaryBaseline(scope, state.summaryCurrent);
  else renderSummaryChanges();
  for (const k of keys) if (loaders[k]) run(() => loaders[k](scope));
}

// Each area reports what it listed once and is kept and compared on its own, so a slow card holds up only itself.
function summaryRecorded(key, items) {
  if (!state.summaryWaiting || !state.summaryWaiting.has(key)) return;
  state.summaryWaiting.delete(key);
  if (!items) { state.summaryChanges.unread.push(key); state.summaryChanges.answered.add(key); renderSummaryChanges(); return; }
  state.summaryItems[key] = items;
  summaryChangesFor(currentScope(), key, items, state.summaryCurrent);
}

async function summaryProblems(scope) {
  let data;
  try { data = await api("api/triage", { params: scopeParams(scope) }); }
  catch (error) { setSummary("problems", noticeHtml(error), "", { tone: null, items: null }); return; }
  const items = data.cards.flatMap((c) => c.items.filter((i) => ["critical", "warning"].includes(i.status)).map((i) => ({ ...i, card: c.title })));
  const critical = items.filter((i) => i.status === "critical").length;
  const groups = new Set(items.map((i) => i.resource_group).filter(Boolean)).size;
  const text = items.length ? `${items.length} problem(s) need attention${critical ? ` (${critical} critical)` : ""}${groups ? ` in ${groups} resource group(s)` : ""}.`
    : `No problems found in ${data.subscriptions} subscription(s).`;
  const top = items.sort((a, b) => (a.status === "critical" ? 0 : 1) - (b.status === "critical" ? 0 : 1)).slice(0, summaryTop());
  const recorded = Object.fromEntries(items.map((i) => [`${i.card}|${i.id || i.name}`, { label: `${i.name}: ${i.card}`, status: i.status }]));
  setSummary("problems", `<p class="big-line">${esc(text)}</p>${top.length ? `<ul class="summary-top">${top.map((i) => summaryLi(i, `${pill(i.status)}
    <strong>${esc(i.name)}</strong> <span class="muted">${esc(i.card)}${i.resource_group ? ` · ${esc(i.resource_group)}` : ""}</span>`)).join("")}</ul>` : ""}`,
  text, { tone: critical ? "critical" : items.length ? "warning" : "healthy", items: recorded });
}

async function summaryCost(scope) {
  let totals = null;
  try { totals = await api("api/cost/totals", { params: costParams(scopeParams(scope)) }); } catch { totals = null; }
  if (totals && totals.available !== false) { renderSummaryCost(totals); return; }
  if (state.cost && state.costLoaded === costKey(scope)) { renderSummaryCost(state.cost); return; }
  setSummary("cost", loading(`Reading costs for ${scope.subscriptions.length} subscription(s): Azure throttles these, so a big scope takes a few minutes the first time`), "");
  loadCost();  // the Cost view's own read: Summary shows it as it fills in (renderCost calls back here)
}

// The scope's 30 days, one figure per day, from each subscription's trend (oldest first, ending today).
function scopeDays(data) {
  const subs = data.subscriptions.filter((s) => s.trend && s.trend.length);
  if (!subs.length || !data.today) return [];
  const n = subs[0].trend.length;
  const end = new Date(`${data.today}T00:00:00Z`);
  return Array.from({ length: n }, (_, i) => ({ date: new Date(end - (n - 1 - i) * 86400000).toISOString().slice(0, 10),
    cost: subs.reduce((sum, s) => sum + (s.trend[i] || 0), 0) }));
}

function renderSummaryCost(data) {
  if (!document.querySelector('[data-summary="cost"]')) return;
  const cur = data.currencies && data.currencies.length > 1 ? "" : data.currency;
  const sum = (k) => data.subscriptions.reduce((n, s) => n + s[k], 0);
  if (!data.subscriptions.length) {
    setSummary("cost", data.done ? `<p class="muted">Costs aren't readable here. ${esc(COST_ROLE_FIX)}</p>`
      : loading(`${data.read || 0} of ${data.total} subscription(s) read…`), "", data.done ? { tone: null, items: null } : {});
    return;
  }
  const money = (v) => moneyText(v, cur);
  const pct = (now, before) => (before ? changeText(now, before) : "");
  const month = sum("month_to_date"), week = sum("last_7_days"), before = sum("previous_7_days");
  const months = data.subscriptions.map((s) => s.month).filter(Boolean);
  const whole = months.length === data.subscriptions.length;
  const projected = whole && months.every((m) => m.projected != null) ? months.reduce((n, m) => n + m.projected, 0) : null;
  const lastMonth = whole && months.every((m) => m.last_month != null) ? months.reduce((n, m) => n + m.last_month, 0) : null;
  const sameDays = data.subscriptions.some((s) => s.last_month_same_days == null) ? null : sum("last_month_same_days");
  const range = costSameDays(data);
  const upTo = data.subscriptions.map((s) => s.up_to).filter(Boolean).sort().pop();
  const unreadable = (data.refused || []).length + (data.failed || []).length;
  const covered = `${data.subscriptions.length} of ${data.total || data.subscriptions.length} subscription(s)`;
  const spikes = data.spikes || [];
  // [label, figure, comparison]: the card's lines, and the same lines as text to copy into a status update
  const lines = [
    [`This month${upTo ? ` (to ${upTo.slice(5)})` : ""}`, money(month), sameDays && range ? `${pct(month, sameDays)} vs ${range}` : data.done ? "" : "reading last month…"],
    projected != null ? ["Heading for", `≈ ${money(projected)}`, lastMonth ? `${pct(projected, lastMonth)} vs last month's ${money(lastMonth)}` : ""] : null,
    before ? ["Last 7 days", money(week), `${pct(week, before)} vs the 7 days before`] : null,
    ["Spikes", String(spikes.length), spikes.length ? `${spikes.slice(0, 2).map((g) => g.name).join(", ")}${spikes.length > 2 ? `, +${spikes.length - 2} more` : ""}` : ""],
  ].filter(Boolean);
  const text = [`Cost, ${covered}${unreadable ? ` (${unreadable} not readable)` : ""}:`,
    ...lines.map(([k, v, extra]) => `- ${k}: ${v}${extra ? ` (${extra})` : ""}`)].join("\n");
  const top = [...data.subscriptions].sort((a, b) => b.month_to_date - a.month_to_date).slice(0, summaryTop());
  const recorded = Object.fromEntries(spikes.map((g) => [`spike|${g.subscription_id}/${g.name}`,
    { label: `Cost spike in ${g.name} (${subName(g.subscription_id)})`, status: "warning" }]));
  state.summaryCostView = { data, cur, text, html: `
    <p class="muted">${esc(covered)}${unreadable ? ` · <span title="${esc(COST_ROLE_FIX)}">${unreadable} not readable</span>` : ""} · ${esc(COST_KIND[data.cost_type || state.costType] || "")}</p>
    ${data.done ? "" : `<p class="muted">${loading(`${data.read} of ${data.total} subscription(s) read so far`)}</p>`}
    <div class="cost-lines">${lines.map(([k, v, extra]) => `<div class="cost-line"><div class="muted">${esc(k)}</div>
      <div><strong class="cost-figure">${esc(v)}</strong>${extra ? ` <span class="muted">${esc(extra)}</span>` : ""}</div></div>`).join("")}</div>
    ${costBarsHtml(scopeDays(data), cur, { height: 40 })}
    <p class="summary-sub"><strong>Biggest this month</strong></p>
    <ul class="summary-top">${top.map((s) => `<li class="static"><strong>${esc(subName(s.subscription_id))}</strong> <span class="muted">${esc(money(s.month_to_date))}</span></li>`).join("")}</ul>
    <div class="summary-budgets"></div>
    <p class="muted">${data.done ? asOfHtml(data.read_at) : ""}</p>`, recorded, tone: spikes.length ? "warning" : "healthy" };
  setSummary("cost", state.summaryCostView.html, data.done ? text : "");
  const slot = document.querySelector('[data-summary="cost"] .summary-budgets');
  if (slot && data.done) slot.innerHTML = loading("Reading budgets…");
  state.summaryCostRefused = data.done ? (data.refused || []).length : null;
  if (data.done) summaryBudgets(currentScope(), state.summaryCurrent);
}

async function summaryBudgets(scope, current) {
  let found;
  try { found = await api("api/cost/budgets", { params: scopeParams(scope) }); }
  catch (error) { found = { budgets: [], notes: [{ note: error.message }] }; }
  const view = state.summaryCostView;
  if (!current() || !view) return;
  const warn = Number(summaryCfg().budget_warn_percent) || 100;
  const rows = found.budgets.map((b) => {
    const spent = b.percent ?? 0;
    const forecast = b.forecast != null && b.amount ? (b.forecast / b.amount) * 100 : null;
    const status = spent >= 100 ? "critical" : (spent >= warn || (forecast ?? 0) >= 100) ? "warning" : "healthy";
    return { ...b, spent_pct: spent, forecast_pct: forecast, status };
  }).sort((a, b) => b.spent_pct - a.spent_pct);
  const flagged = rows.filter((b) => b.status !== "healthy");
  const line = rows.length ? (flagged.length ? `${flagged.length} of ${rows.length} need attention (${warn}% or more spent, or forecast past 100%).`
    : `all ${rows.length} under ${warn}%.`) : "none set on these subscriptions.";
  const html = `<p class="summary-sub"><strong>Budgets:</strong> ${esc(line)}</p>${flagged.length ? `<ul class="summary-top">${flagged.slice(0, summaryTop()).map((b) => `<li class="static">
    ${pill(b.status)} <strong>${esc(b.name)}</strong> <span class="muted">${esc(subName(b.subscription_id))} · ${b.spent_pct.toFixed(0)}% of
    ${esc(moneyText(b.amount, b.currency))} spent${b.forecast_pct != null ? `, forecast ${b.forecast_pct.toFixed(0)}%` : ""} (${esc(b.time_grain.toLowerCase())})</span></li>`).join("")}</ul>` : ""}
    ${found.notes.length ? `<p class="muted">Budgets not readable in ${found.notes.length} subscription(s)${found.notes[0].subscription_id
      ? ` (${esc(found.notes.map((n) => subName(n.subscription_id)).slice(0, 3).join(", "))}${found.notes.length > 3 ? ", …" : ""})` : ""}.</p>` : ""}`;
  view.html = view.html.replace('<div class="summary-budgets"></div>', `<div class="summary-budgets">${html}</div>`);
  const tone = rows.some((b) => b.status === "critical") ? "critical" : (flagged.length || view.tone === "warning") ? "warning" : "healthy";
  const items = { ...view.recorded, ...Object.fromEntries(flagged.map((b) => [`budget|${b.subscription_id}/${b.name}`,
    { label: `Budget ${b.name} at ${b.spent_pct.toFixed(0)}%`, status: b.status }])) };
  setSummary("cost", view.html, `${view.text}\n- Budgets: ${line}`, { tone, items });
}

async function summarySecurity(scope) {
  const vulns = api("api/vulnerabilities/headline", { params: scopeParams(scope) }).catch((error) => ({ error: error.message }));
  let data;
  try { data = await api("api/security", { params: scopeParams(scope) }); }
  catch (error) { setSummary("security", noticeHtml(error), "", { tone: null, items: null }); return; }
  const warnings = data.rows.filter((r) => r.severity === "warning");
  const resources = new Set(warnings.map((r) => r.id.toLowerCase())).size;
  const labels = Object.fromEntries(data.categories.map((c) => [c.key, c.label]));
  const tiles = securityTilesHtml(data);
  const top = warnings.slice(0, summaryTop());
  const text = warnings.length ? `${warnings.length} security warning(s) on ${resources} resource(s).` : "No security warnings in the settings checked.";
  const v = await vulns;
  const s = v && v.summary;
  const reachable = s ? v.resources.filter((r) => r.exposure.length && r.high) : [];
  const vulnText = !s ? "" : s.findings ? `${s.findings.toLocaleString()} open vulnerability finding(s) (${s.high.toLocaleString()} high) on ${s.resources} resource(s); ${
    s.running.toLocaleString()} in running containers; ${s.findings ? Math.round((100 * s.fixable) / s.findings) : 0}% have a fix.` : "No open vulnerability findings from Defender.";
  const vulnTop = s ? v.resources.map((r) => summaryLi({ id: r.parent, name: r.name }, `${pill(r.exposure.length && r.high ? "critical" : r.high ? "warning" : "info")}
    <strong>${esc(r.name)}</strong> <span class="muted">${esc(typeLabel(r.rtype))} · worst CVSS ${esc(String(r.maxCvss ?? "–"))}${r.running ? " · running in AKS" : ""}${
    r.exposure.length ? ` · reachable: ${esc(r.exposure.join(", "))}` : ""}</span>`, { tab: "vulnerabilities" })).join("") : "";
  const recorded = Object.fromEntries([
    ...warnings.map((r) => [`${r.id}|${r.category}|${r.what}`, { label: `${r.name}: ${r.what}`, status: "warning" }]),
    ...(s ? v.resources.map((r) => [`vuln|${r.parent}`, { label: `${r.name}: vulnerabilities, worst CVSS ${r.maxCvss}`,
      status: r.exposure.length && r.high ? "critical" : r.high ? "warning" : "info" }]) : [])]);
  setSummary("security", `<p class="big-line">${esc(text)}</p><div class="counters summary-tiles">${tiles}</div>
    ${top.length ? `<ul class="summary-top">${top.map((r) => summaryLi(r, `${itemPill({ ...r, status: r.severity })} <strong>${esc(r.name)}</strong>
      <span class="muted">${esc(r.display_type)} · ${esc(r.what)} (${esc((labels[r.category] || r.category).toLowerCase())})</span>`)).join("")}</ul>` : ""}
    <p class="summary-sub"><button type="button" class="link" data-show-vulns><strong>Vulnerabilities</strong></button> ${v && v.error ? esc(v.error) : esc(vulnText)}</p>
    ${vulnTop ? `<ul class="summary-top">${vulnTop}</ul>` : ""}`,
  [text, vulnText].filter(Boolean).join(" "),
  { tone: reachable.length ? "critical" : (warnings.length || (s && s.high)) ? "warning" : "healthy", items: recorded });
}

async function summaryReadiness(scope) {
  const full = state.readiness && state.readiness.result && state.readiness.scopeKey === JSON.stringify(scope) ? state.readiness.result : null;
  if (full) { renderSummaryReadiness(full); return; }
  if (!summaryCfg().readiness_quick) {
    setSummary("readiness", `<p class="muted">Not run for this scope yet. Before a busy period, Readiness says what would stop
      the scope from scaling and what expires before it ends. (Settings → Summary runs its quick checks here automatically.)</p>`, "", { items: null });
    return;
  }
  const current = state.summaryCurrent;
  try {
    const body = { subscriptions: scope.subscriptions, peak_end: defaultPeakEnd(), deep: false, purpose: "summary" };
    if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
    const { job_id } = await api("api/readiness", { method: "POST", body });
    const job = await pollJob(job_id, { isCurrent: current, failed: "Readiness's quick checks failed." });
    if (!job || !current()) return;
    if (job.status !== "done") { setSummary("readiness", '<p class="muted">Stopped.</p>', "", { items: null }); return; }
    if (!(state.readiness && (state.readiness.result || state.readiness.running))) {
      state.readiness = { result: job.result, jobId: job_id, scopeKey: JSON.stringify(scope) };  // Readiness shows the same report
    }
    renderSummaryReadiness(job.result);
  } catch (error) {
    if (current()) setSummary("readiness", noticeHtml(error), "", { tone: null, items: null });
  }
}

function renderSummaryReadiness(r) {
  const quick = !r.deep;
  const c = r.counts || {};
  const found = r.sections.flatMap((s) => s.items.filter((i) => ["critical", "warning"].includes(i.status)).map((i) => ({ ...i, section: s.title })));
  const unread = r.sections.filter((s) => s.error).map((s) => s.title);
  const text = `Readiness: ${c.critical || 0} critical and ${c.warning || 0} warning finding(s) for a peak ending ${r.peak_end}.`;
  const recorded = Object.fromEntries(found.map((i) => [`${i.section}|${i.id || i.name}`, { label: `${i.name}: ${i.section}`, status: i.status }]));
  setSummary("readiness", `<p class="big-line">${esc(text)}</p>
    ${found.length ? `<ul class="summary-top">${found.slice(0, summaryTop()).map((i) => summaryLi(i, `${pill(i.status)} <strong>${esc(i.name)}</strong>
      <span class="muted">${esc(i.section)} · ${esc(i.detail)}</span>`, { view: "readiness" })).join("")}</ul>` : ""}
    ${unread.length ? `<p class="muted">Not read: ${esc(unread.join(", "))}.</p>` : ""}
    <p class="muted">${quick ? `Quick checks (scaling, vCPU quota, subnets, single instances), for the peak date last used on Readiness
      (${esc(r.peak_end)}, change it there). Readiness shows the same report and can add Kubernetes and Key Vault checks.` : `The last full Readiness report, ${esc(r.generated_at)}.`}</p>`,
  text, { tone: c.critical ? "critical" : c.warning ? "warning" : "healthy", items: recorded });
}

async function summaryAccess(scope) {
  let pimData = null;
  try { pimData = await api("api/pim", { params: scopeParams(scope) }); } catch { pimData = null; }
  const lines = [`You can read ${state.subscriptions.length} subscription(s) in this tenant; ${scope.subscriptions.length} are in scope.`];
  if (pimData && !pimData.error) lines.push(`${pimData.current_total ?? 0} role(s) you hold; ${pimData.total} you could activate in PIM.`);
  else if (pimData && pimData.error) lines.push("PIM isn't available or readable here, so only the roles you hold count.");
  setSummary("access", `<ul class="summary-top">${lines.map((l) => `<li class="static">${esc(l)}</li>`).join("")}</ul>`, "");
}

const AREA_TITLES = { problems: "Problems", security: "Security", cost: "Cost", readiness: "Readiness" };

// Whether this scope has a summary old enough to compare with, known before any card answers.
async function summaryBaseline(scope, current) {
  let data;
  try { data = await api("api/summary/baseline", { params: scopeParams(scope) }); } catch { return; }
  const ch = state.summaryChanges;
  if (!current() || ch.since !== undefined) return;  // a card's own answer got here first
  ch.since = data.since;
  ch.readyAt = data.ready_at;
  renderSummaryChanges();
}

async function summaryChangesFor(scope, key, items, current) {
  const body = { subscriptions: scope.subscriptions, items: { [key]: items } };
  if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
  let data;
  try { data = await api("api/summary/changes", { method: "POST", body }); }
  catch (error) { if (current()) { state.summaryChanges.unread.push(key); state.summaryChanges.answered.add(key); renderSummaryChanges(); } return; }
  if (!current()) return;
  const ch = state.summaryChanges;
  ch.answered.add(key);
  if (ch.since === undefined || data.since === null) { ch.since = data.since; ch.readyAt = data.ready_at; }
  if (data.since && data.areas[key]) ch.areas[key] = data.areas[key];
  else if (data.since) ch.missing.push(key);  // not in the earlier summary: nothing to compare it with
  renderSummaryChanges();
}

function renderSummaryChanges() {
  const ch = state.summaryChanges;
  const names = (keys) => keys.map((k) => AREA_TITLES[k] || k).join(", ");
  if (!ch.expected.length) {
    setSummary("changes", '<p class="muted">Nothing to compare: this view shows none of the cards What changed compares.</p>', "");
    return;
  }
  const pending = ch.expected.filter((k) => !ch.answered.has(k));
  const still = pending.length ? `<p class="muted">${loading(`Still reading ${names(pending)}: compared as each answers.`)}</p>` : "";
  if (ch.since === undefined) { setSummary("changes", loading("Looking for an earlier summary of this scope…"), ""); return; }
  if (ch.since === null) {
    setSummary("changes", `<p class="muted">First summary of this scope (${esc(String(currentScope().subscriptions.length))}
      subscription(s)): nothing to compare with yet. Summary keeps what it lists each time it opens, on this machine; the
      first comparison is ready ${esc(new Date(ch.readyAt).toLocaleString())} (${esc(String(summaryCfg().compare_hours))} h
      after the first summary of this scope).</p>`, "");
    return;
  }
  const areas = ch.expected.filter((k) => ch.areas[k]).map((k) => [k, ch.areas[k]]);
  const fresh = areas.reduce((n, [, a]) => n + a.new.length, 0);
  const gone = areas.reduce((n, [, a]) => n + a.resolved.length, 0);
  const since = `${new Date(ch.since).toLocaleString()} (${ago(ch.since)})`;
  const text = !areas.length ? (pending.length ? "" : "Nothing to compare: no card answered.")
    : fresh || gone ? `Since ${since}: ${fresh} new or worse, ${gone} resolved${pending.length ? " so far" : ""}.`
      : `Nothing new or resolved since ${since}${pending.length ? " so far" : ""}.`;
  const top = summaryTop();
  const blocks = areas.filter(([, a]) => a.new.length || a.resolved.length).map(([area, a]) => `<p class="summary-sub"><strong>${esc(AREA_TITLES[area] || area)}</strong>
    ${a.new.length} new or worse · ${a.resolved.length} resolved</p><ul class="summary-top">
    ${a.new.slice(0, top).map((i) => `<li class="static">${pill(i.status || "info")} ${esc(i.label)}${i.was ? ` <span class="muted">(was ${esc(i.was)})</span>` : ""}</li>`).join("")}
    ${a.resolved.slice(0, top).map((i) => `<li class="static"><span class="better">✓</span> ${esc(i.label)} <span class="muted">resolved</span></li>`).join("")}</ul>`).join("");
  const notes = [ch.unread.length ? `${names(ch.unread)} couldn't be read, so ${ch.unread.length === 1 ? "it isn't" : "they aren't"} compared.` : "",
    ch.missing.length ? `${names(ch.missing)} ${ch.missing.length === 1 ? "wasn't" : "weren't"} in the earlier summary: nothing to compare with yet.` : ""].filter(Boolean);
  setSummary("changes", `${text ? `<p class="big-line">${esc(text)}</p>` : ""}${blocks}${still}${notes.map((n) => `<p class="muted">${esc(n)}</p>`).join("")}`,
    pending.length ? "" : text);
}

$("summary-refresh").addEventListener("click", () => loadSummary(true));
$("summary-copy").addEventListener("click", (event) => {
  const scope = currentScope();
  const text = [`${scope.label}, ${new Date().toLocaleString()}`, summaryStatusText(),
    ...summaryCardKeys().map((k) => state.summaryParts[k]).filter(Boolean)].filter(Boolean).join("\n");
  copyText(text, event.currentTarget);
});
$("view-summary").addEventListener("click", (event) => {
  const go = event.target.closest("[data-show-view]");
  if (go) { showView(go.dataset.showView); return; }
  const jump = event.target.closest("[data-summary-jump]");
  if (jump) { spotlightSummary(jump.dataset.summaryJump); return; }
  if (event.target.closest("[data-show-vulns]")) { state.secSub = "vulnerabilities"; showView("security"); return; }
  const area = event.target.closest("[data-sec-category]");
  if (area) { state.secSub = "config"; state.secCategories = new Set([area.dataset.secCategory]); showView("security"); if (state.security) renderSecurity(); return; }
  const item = event.target.closest("li[data-open]");
  if (!item) return;
  const target = JSON.parse(item.dataset.open);
  if (item.dataset.tab) state.pendingDetail = { node: target.id, tab: item.dataset.tab };
  startSnapshot(target);
});

