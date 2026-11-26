"use strict";

// ------------------------------------------------------------------ triage

const TRIAGE_ORDER = { critical: 0, warning: 1, info: 2 };

// Cards with something to show first (red, yellow, then any with items), then still loading, then empty. Cards that
// arrive later (App Insights, ServiceNow, monitoring tools) are put in place as they land.
function sortTriageCards() {
  const box = $("triage-cards");
  const rank = (el) => (el.classList.contains("critical") ? 0 : el.classList.contains("warning") ? 1
    : el.querySelector("li") ? 2 : el.querySelector(".loading") ? 3 : el.querySelector(".notice, .error") ? 4 : 5);
  const cards = [...box.children].filter((el) => el.classList.contains("card"));
  const sorted = [...cards].sort((a, b) => rank(a) - rank(b) || personaCardRank(a.dataset.card) - personaCardRank(b.dataset.card));
  if (sorted.some((el, i) => el !== cards[i])) for (const el of sorted) box.append(el);
}
new MutationObserver(() => requestAnimationFrame(sortTriageCards)).observe($("triage-cards"), { childList: true });

// Shown by every tab that reads the scope while none is applied: what the tab would read, and the saved scopes to start from.
function pickScopeHtml(reads) {
  const saved = state.savedScopes || [];
  return `<div class="start-hint"><h3>Pick what to look at</h3>
    <p>Nothing is selected yet, so nothing has been queried. ${esc(reads)} Choose <strong>Subscriptions</strong> in the Scope bar
      above and press <strong>Apply</strong>${saved.length ? ", or start from a saved scope:" : "."}</p>
    ${saved.length ? `<div class="start-saved">${saved.map((x, i) => `<button type="button" data-start-saved="${i}">${esc(x.name)}</button>`).join("")}</div>` : ""}
    <p class="muted">Search (top) and Access (your own roles) work without a scope.</p></div>`;
}

async function loadTriage(options = {}) {
  const scope = currentScope();
  const scopeKey = JSON.stringify(scope);
  // Same scope as last time: keep the cards up while loading, and mark what's new against them.
  const previous = state.triageScopeKey === scopeKey ? state.triageLast : null;
  const token = (state.triageToken || 0) + 1;
  state.triageToken = token;
  state.triageLoaded = true;
  state.triageScopeKey = scopeKey;
  state.aksSweep = null;  // a new scope (or Refresh) makes an earlier sweep out of date
  state.aksSweepToken = (state.aksSweepToken || 0) + 1;
  $("triage-notice").innerHTML = scopeNotices();
  if (!scope.subscriptions.length) {
    $("triage-cards").innerHTML = pickScopeHtml("Triage checks the subscriptions in scope for problems.");
    return;
  }
  if (previous && options.inPlace) {
    $("triage-meta").innerHTML = `${esc(state.triageMetaText || "")} · ${loading("refreshing…")}`;
  } else {
    $("triage-meta").textContent = "";
    $("triage-cards").innerHTML = loading(`Checking ${scope.subscriptions.length} subscription(s) for problems…`);
  }
  state.triageBusy = true;
  try {
    const data = await api("api/triage", { params: scopeParams(scope) });
    if (token !== state.triageToken) return;
    const when = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    state.triageMetaText = `${data.subscriptions} subscription(s) in ${scope.label} · checked ${when}`;
    state.triageChecked = `${data.subscriptions} subscription(s)`;
    const problems = data.cards.flatMap((c) => c.items.filter((i) => ["critical", "warning"].includes(i.status)));
    const critical = problems.filter((i) => i.status === "critical").length;
    $("triage-summary").textContent = problems.length
      ? `${problems.length} problem(s) need attention${critical ? `, ${critical} critical` : ""}, in ${data.cards.filter((c) => c.status).length} of ${data.cards.length} areas below. More cards (App Insights, ServiceNow, monitoring tools) may add to it as they load.`
      : `No problems in ${data.subscriptions} subscription(s) across ${data.cards.length} areas; cards still loading may add some.`;
    const marks = triageMarks(previous, data.cards);
    state.triageLast = { at: Date.now(), when, items: triageItemKeys(data.cards), status: Object.fromEntries(data.cards.map((c) => [c.key, c.status])) };
    const cards = [...data.cards].sort((a, b) =>
      (TRIAGE_ORDER[a.status] ?? (a.items.length ? 2 : 3)) - (TRIAGE_ORDER[b.status] ?? (b.items.length ? 2 : 3)));
    $("triage-cards").innerHTML = (data.changes_error ? noticeHtml(data.changes_error) : "") + cards.map((card) => renderCard(card, marks)).join("");
    renderTriageMeta();
    loadServiceNowCards(scope);
    loadMonitorCards(scope);
    loadAppInsightsCard(scope);
  } catch (error) {
    if (token !== state.triageToken) return;
    if (previous && options.inPlace) $("triage-meta").innerHTML = `${esc(state.triageMetaText || "")} · <span class="stale">refresh failed: ${esc(error.message)}</span>`;
    else $("triage-cards").innerHTML = noticeHtml(error);
  } finally {
    if (token === state.triageToken) state.triageBusy = false;
  }
}

// ---- marks: what's new since the previous load of the same scope (no extra calls: the page compares its own results)

const triageItemKey = (card, item) => `${card.key}|${(item.id || "").toLowerCase()}|${item.name}`;
const STATUS_WEIGHT = { critical: 3, warning: 2, info: 1 };

function triageItemKeys(cards) {
  return new Set(cards.flatMap((card) => card.items.map((item) => triageItemKey(card, item))));
}

function triageMarks(previous, cards) {
  if (!previous) return null;
  return {
    since: previous.when,
    isNew: (card, item) => !previous.items.has(triageItemKey(card, item)),
    worse: (card) => (STATUS_WEIGHT[card.status] || 0) > (STATUS_WEIGHT[previous.status[card.key]] || 0),
  };
}

function renderTriageMeta() {
  const minutes = state.triageAuto || 0;
  let text = esc(state.triageMetaText || "");
  if (minutes && state.triageLast) {
    const next = Math.max(0, state.triageLast.at + minutes * 60000 - Date.now());
    text += ` · <span title="Refreshes by itself while this tab is open and visible">auto every ${minutes} min, next in ${Math.ceil(next / 60000)} min</span>`;
  }
  $("triage-meta").innerHTML = text;
}

// Auto-refresh: only on Triage, only while visible, never on top of a load still running.
function triageAutoTick() {
  const minutes = state.triageAuto || 0;
  if (!minutes || state.view !== "triage" || document.hidden || state.triageBusy || !state.triageLast) return;
  if (!currentScope().subscriptions.length) return;
  if (Date.now() - state.triageLast.at >= minutes * 60000) loadTriage({ inPlace: true });
  else renderTriageMeta();
}
setInterval(triageAutoTick, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) triageAutoTick(); });

$("triage-auto").addEventListener("change", (event) => {
  state.triageAuto = Number(event.target.value);
  state.triageAutoOverride = true;  // this tab only; the default is in Settings
  renderTriageMeta();
  triageAutoTick();
});

const SUMMARY_ITEMS = 3;  // a summary-level persona sees the first few, then "Show N more"

// "↗" on a card item that isn't a resource: a Service Health page in the portal, or a public status post.
function itemLinkHtml(link) {
  const url = portalUrl(link);
  if (!url.startsWith("https://")) return "";
  return `<a class="portal-link" href="${safeHref(url)}" target="_blank" rel="noopener" title="${link.health ? "Open in Service Health" : "Open the status post"}">↗</a>`;
}

function renderCard(card, marks = null) {
  const tone = card.status || (card.items.length ? "" : "clear");
  const items = card.items.map((item, index) => {
    const where = [item.display_type, item.resource_group, item.subscription_id ? subName(item.subscription_id) : ""].filter(Boolean).join(" · ");
    const open = item.id ? `data-open="${esc(JSON.stringify({ kind: "resource", id: item.id, name: item.name }))}"` : "";
    const fresh = marks && marks.isNew(card, item);
    return `<li class="${item.id ? "" : "static"} ${fresh ? "is-new" : ""} ${index >= SUMMARY_ITEMS ? "beyond" : ""}" ${open}>${itemPill(item)} <strong>${esc(item.name)}</strong> ${portalLinkHtml(item.id)}${item.link ? itemLinkHtml(item.link) : ""}
      ${item.id ? browseButtonHtml() : ""}
      ${item.id ? `<button type="button" class="link next-step" data-open-tab="timeline" title="Open it on its Timeline: alerts, changes and health, newest first">Timeline</button>` : ""}
      ${fresh ? `<span class="new-chip" title="Not listed at the previous refresh (${esc(marks.since)})">new</span>` : ""}
      ${item.id ? watchButton({ id: item.id, name: item.name, kind: "resource" }) : ""}
      <div class="where">${esc(where)}</div><div>${escTimes(item.detail)}</div>
      ${item.changed ? `<div class="changed" title="${esc(new Date(item.changed.at).toLocaleString())}">↻ ${esc(item.changed.text)} · ${esc(dayTime(item.changed.at))}</div>` : ""}</li>`;
  }).join("");
  const worse = marks && marks.worse(card);
  const fresh = marks ? card.items.filter((item) => marks.isNew(card, item)).length : 0;
  return `<div class="card ${tone} ${worse ? "got-worse" : ""}" data-card="${esc(card.key || "")}">
    <h3>${esc(card.title)}${fresh ? ` <span class="new-chip">${fresh} new</span>` : ""}${worse ? ' <span class="worse-chip">worse</span>' : ""}<span class="count">${card.items.length}${card.items.length >= 100 ? "+" : ""}</span></h3>
    <p class="desc">${esc(card.description)}${CARD_SETTINGS[card.key] ? ` <span class="rule">· ${settingLink(CARD_SETTINGS[card.key], "set in Settings")}</span>` : ""}</p>
    ${card.error ? noticeHtml(card.error) : ""}
    ${card.note ? `<p class="muted card-note">${escTimes(card.note)}</p>` : ""}
    ${!card.error && !card.items.length ? `<p class="ok">✓ Nothing found${state.triageChecked ? ` in ${esc(state.triageChecked)}` : ""}</p>` : ""}
    ${items ? `<ul>${items}</ul>` : ""}
    ${card.items.length > SUMMARY_ITEMS ? `<button type="button" class="link show-all" data-show-all>Show ${card.items.length - SUMMARY_ITEMS} more</button>` : ""}
    ${(card.links || []).length ? `<p class="card-links muted">${card.links.filter((l) => (l.url || "").startsWith("https://")).map((l) =>
      `<a href="${safeHref(l.url)}" target="_blank" rel="noopener">${esc(l.label)} ↗</a>`).join(" · ")}</p>` : ""}
    ${card.key === "aks" ? `<div class="card-actions" id="aks-sweep">${aksSweepHtml()}</div>` : ""}</div>`;
}

// The AKS card's deeper sweep: one request per cluster for pods by phase, optionally Azure's detectors.
function aksSweepHtml() {
  const sweep = state.aksSweep;
  const controls = `<button type="button" id="aks-sweep-run">${sweep && sweep.result ? "Check again" : "Check pods and detectors"}</button>
    <label class="inline"><input type="checkbox" id="aks-sweep-detectors" ${sweep && sweep.detectors ? "checked" : ""}> include Azure's detectors (slower)</label>
    <p class="muted">The card above is one quick query. This asks every cluster in scope for pending, failed or unknown pods,
      and optionally Azure's "Diagnose and solve problems" findings.</p>`;
  if (!sweep) return controls;
  if (sweep.running) return loading(`${sweep.running}…`, "aks-sweep-phase");
  if (sweep.error) return controls + noticeHtml(sweep.error);
  const r = sweep.result;
  const c = r.counts;
  const problems = r.rows.filter((row) => row.status !== "healthy");
  return `<p><strong>${r.clusters} cluster(s) checked:</strong> ${pill("critical")} ${c.critical} · ${pill("warning")} ${c.warning}
      · ${pill("unknown")} ${c.unknown} · ${pill("healthy")} ${c.healthy}</p>
    ${problems.length ? `<ul>${problems.map((row) => `<li data-open="${esc(JSON.stringify({ kind: "resource", id: row.id, name: row.name }))}">
      ${pill(row.status)} <strong>${esc(row.name)}</strong> ${browseButtonHtml()} ${watchButton({ id: row.id, name: row.name, kind: "resource" })}
      <div class="where">${esc(row.resource_group)} · Kubernetes ${esc(row.version || "?")} · ${esc(row.power || "?")}</div>
      <div>${row.problems.map(esc).join("<br>")}</div></li>`).join("")}</ul>`
      : r.clusters ? '<p class="ok">✓ No pending or newly failed pods' + (sweep.detectors ? " and no detector findings" : "") + "</p>" : ""}
    ${controls}`;
}

// checks.py words a pod-phase problem "2 failed in <namespace>[, …]"; those rows link to the Workloads tab.
const AKS_POD_PROBLEM = /^\d+ (failed|pending|unknown) in [^\s,]+(,.*)?$/;

// The same check as Triage's AKS card, for the one cluster whose page is open.
function aksOneHtml(clusterId) {
  const run = (state.aksOne || {})[clusterId.toLowerCase()];
  const controls = `<button type="button" data-aks-one>${run && run.result ? "Check again" : "Check pods and detectors"}</button>
    <label class="inline"><input type="checkbox" data-aks-one-detectors ${run && run.detectors ? "checked" : ""}> include Azure's detectors (slower)</label>`;
  const head = '<h4>Pods and detectors</h4><p class="muted">Asks the cluster for pending, failed or unknown pods, and optionally Azure\'s "Diagnose and solve problems" findings: the same check as Triage\'s AKS card, for this cluster.</p>';
  if (!run) return head + controls;
  if (run.running) return head + loading(`${run.running}…`);
  if (run.error) return head + noticeHtml(run.error) + controls;
  const row = run.result.rows[0];
  if (!row) return head + '<p class="muted">The cluster wasn\'t found in its resource group.</p>' + controls;
  return `${head}<p>${pill(row.status)} Kubernetes ${esc(row.version || "?")} · ${esc(row.power || "?")} · provisioning ${esc(row.provisioning || "?")}</p>
    ${row.problems.length ? `<ul class="aks-one-problems">${row.problems.map((p) => `<li class="static">${esc(p)}${AKS_POD_PROBLEM.test(p)
      ? ` <button type="button" class="link" data-aks-workloads="${esc(clusterId)}">see its pods in Workloads</button>` : ""}</li>`).join("")}</ul>`
      : `<p class="ok">✓ No pending or failed pods${run.detectors ? " and no detector findings" : ""}</p>`}${controls}`;
}

async function runAksOne(clusterId, detectors) {
  const key = clusterId.toLowerCase();
  state.aksOne = state.aksOne || {};
  state.aksOne[key] = { running: "Starting", detectors };
  const paint = () => { const box = $("aks-one"); if (box && box.dataset.cluster.toLowerCase() === key) box.innerHTML = aksOneHtml(clusterId); };
  paint();
  try {
    const { job_id } = await api("api/checks", { method: "POST", body: { check: "aks", id: clusterId, detectors } });
    const job = await pollJob(job_id, { onRunning: (j) => { state.aksOne[key].running = j.phase; paint(); } });
    if (!job || job.status === "cancelled") return;
    state.aksOne[key] = { result: job.result, detectors };
    paint();
  } catch (error) {
    state.aksOne[key] = { error: error.message, detectors };
    paint();
  }
}

document.addEventListener("click", (event) => {
  const workloads = event.target.closest("[data-aks-workloads]");
  if (workloads) { showDetail(workloads.dataset.aksWorkloads, "workloads", { push: true }); return; }
  const button = event.target.closest("[data-aks-one]");
  if (!button) return;
  const box = button.closest("#aks-one");
  runAksOne(box.dataset.cluster, !!box.querySelector("[data-aks-one-detectors]")?.checked);
});

function renderAksSweep() {
  const box = $("aks-sweep");
  if (box) box.innerHTML = aksSweepHtml();
}

async function runAksSweep() {
  const scope = currentScope();
  const token = state.aksSweepToken;
  const detectors = !!($("aks-sweep-detectors") && $("aks-sweep-detectors").checked);
  state.aksSweep = { running: "Starting", detectors };
  renderAksSweep();
  try {
    const body = { check: "aks", subscriptions: scope.subscriptions, detectors };
    if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
    const { job_id } = await api("api/checks", { method: "POST", body });
    const job = await pollJob(job_id, {
      isCurrent: () => token === state.aksSweepToken, failed: "The sweep failed.",
      onRunning: (j) => {
        state.aksSweep.running = j.phase;
        const phase = $("aks-sweep-phase");
        if (phase) phase.textContent = `${j.phase}…`; else renderAksSweep();
      } });
    if (!job || job.status === "cancelled") return;
    state.aksSweep = { result: job.result, detectors };
    renderAksSweep();
  } catch (error) {
    if (token === state.aksSweepToken) { state.aksSweep = { error: error.message, detectors }; renderAksSweep(); }
  }
}

$("triage-cards").addEventListener("click", (event) => {
  if (event.target.id === "aks-sweep-run") { runAksSweep(); return; }
  const all = event.target.closest("[data-show-all]");
  if (all) { all.closest(".card").classList.add("show-all"); all.remove(); return; }
  const next = event.target.closest("[data-open-tab]");
  if (next) {
    const target = JSON.parse(next.closest("li[data-open]").dataset.open);
    state.pendingDetail = { node: target.id, tab: next.dataset.openTab };
    startSnapshot(target);
    return;
  }
  const item = event.target.closest("li[data-open]");
  if (item) startSnapshot(JSON.parse(item.dataset.open));
});
$("triage-refresh").addEventListener("click", () => freshly(() => loadTriage({ inPlace: true })));

