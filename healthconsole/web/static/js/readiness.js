"use strict";

// ------------------------------------------------------------------ pre-peak readiness
// One job over the whole scope: scaling headroom, vCPU quota, subnet IPs, single points, HPAs, certificates.

const READINESS_PROBLEMS = ["critical", "warning", "unknown"];

function defaultPeakEnd() {
  const saved = stored.get("ahc.peakEnd");
  if (saved && saved >= isoDay(new Date())) return saved;
  return isoDay(new Date(Date.now() + 14 * 86400000));
}

function isoDay(moment) {
  return `${moment.getFullYear()}-${String(moment.getMonth() + 1).padStart(2, "0")}-${String(moment.getDate()).padStart(2, "0")}`;
}

function readinessRow(item) {
  const where = [item.display_type, item.subscription_id ? subName(item.subscription_id) : "", item.resource_group].filter(Boolean).join(" · ");
  const open = item.id && item.id.includes("/providers/")
    ? `class="clickable" data-open="${esc(JSON.stringify({ kind: "resource", id: item.id, name: item.name.split(" / ")[0] }))}"` : "";
  return `<tr ${open}><td>${pill(item.status)}</td><td><strong>${esc(item.name)}</strong> ${item.id && item.id.includes("/providers/") ? `${portalLinkHtml(item.id)} ${browseButtonHtml()}` : ""}<div class="where">${esc(where)}</div></td>
    <td>${esc(item.detail)}</td><td>${esc(item.fix || "")}</td></tr>`;
}

function readinessTable(items) {
  return `<table class="data-table"><thead><tr><th>Status</th><th>Name</th><th>Detail</th><th>Fix</th></tr></thead>
    <tbody>${items.map(readinessRow).join("")}</tbody></table>`;
}

function renderReadiness() {
  if (!$("readiness-peak-end").value) $("readiness-peak-end").value = defaultPeakEnd();
  const r = state.readiness || {};
  const running = !!r.running;
  $("readiness-run").disabled = running;
  $("readiness-run").textContent = r.result ? "Run again" : "Run";
  $("readiness-cancel").classList.toggle("hidden", !running);
  for (const fmt of ["md", "pdf"]) $(`readiness-export-${fmt}`).disabled = !r.result;
  if (running) {
    $("readiness-counters").innerHTML = "";
    $("readiness-body").innerHTML = loading(`${r.running}…`, "readiness-phase")
      + '<p class="muted">Kubernetes and Key Vault take one request per cluster and vault; other tabs keep working.</p>';
    return;
  }
  if (r.error) { $("readiness-counters").innerHTML = ""; $("readiness-body").innerHTML = noticeHtml(r.error); return; }
  if (!r.result) {
    $("readiness-counters").innerHTML = "";
    $("readiness-meta").textContent = "";
    $("readiness-body").innerHTML = currentScope().subscriptions.length ? '<p class="muted">Pick the date the peak ends, then Run.</p>'
      : pickScopeHtml("Readiness checks the subscriptions in scope before a peak.");
    return;
  }
  const res = r.result;
  const unchecked = res.sections.reduce((n, s) => n + s.unchecked.length + (s.error ? 1 : 0), 0);
  $("readiness-meta").textContent = `peak ends ${res.peak_end} (${res.days_to_peak_end} day(s)) · ${res.subscriptions} subscription(s) · ${res.generated_at}`;
  $("readiness-counters").innerHTML = [["critical", "would stop scaling or is down to its limit"],
    ["warning", "close to a limit or a single point"], ["healthy", "with room to spare"]]
    .map(([tone, label]) => `<div class="counter ${tone}"><span class="n">${res.counts[tone]}</span><span class="label">${label}</span></div>`).join("")
    + (unchecked ? `<div class="counter unknown"><span class="n">${unchecked}</span><span class="label">thing(s) couldn't be checked (named below, with the fix)</span></div>` : "");
  const stale = r.scopeKey !== JSON.stringify(currentScope())
    ? '<p class="notice">The scope changed since this ran. Run again to report on the new scope.</p>' : "";
  $("readiness-body").innerHTML = stale + res.sections.map((section) => {
    const problems = section.items.filter((i) => READINESS_PROBLEMS.includes(i.status));
    const rest = section.items.filter((i) => !READINESS_PROBLEMS.includes(i.status));
    const notes = [...(section.error ? [`Couldn't read: ${section.error}`] : []), ...section.unchecked];
    return `<div class="card ${section.status || "clear"}">
      <h3>${esc(section.title)}<span class="count">${problems.length}</span></h3>
      <p class="desc">${esc(section.description)}</p>
      ${section.summary ? `<p class="muted">${esc(section.summary)}</p>` : ""}
      ${section.skipped ? '<p class="muted">Not run: tick "Kubernetes and Key Vault checks" and Run again.</p>' : ""}
      ${notes.length ? `<div class="notice partial"><strong>Not checked</strong><ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
      ${problems.length ? readinessTable(problems) : section.skipped || section.error ? ""
        : notes.length ? '<p class="muted">Nothing found in what could be read.</p>' : '<p class="ok">✓ Nothing that would stop scaling</p>'}
      ${rest.length ? `<details class="more"><summary>${rest.length} with room to spare or for information</summary>${readinessTable(rest)}</details>` : ""}
    </div>`;
  }).join("");
}

async function runReadiness() {
  const scope = currentScope();
  if (!scope.subscriptions.length) { renderReadiness(); return; }
  const peakEnd = $("readiness-peak-end").value;
  stored.set("ahc.peakEnd", peakEnd);
  const token = (state.readinessToken = (state.readinessToken || 0) + 1);
  state.readiness = { running: "Starting", scopeKey: JSON.stringify(scope) };
  renderReadiness();
  try {
    const body = { subscriptions: scope.subscriptions, peak_end: peakEnd, deep: $("readiness-deep").checked,
      include_secrets: $("readiness-secrets").checked };
    if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
    const { job_id } = await api("api/readiness", { method: "POST", body });
    state.readiness.jobId = job_id;
    const job = await pollJob(job_id, {
      isCurrent: () => token === state.readinessToken, failed: "The report failed.",
      onRunning: (j) => {
        state.readiness.running = j.phase;
        const phase = $("readiness-phase");
        if (phase) phase.textContent = `${j.phase}…`; else renderReadiness();
      } });
    if (!job) return;
    if (job.status === "cancelled") { state.readiness = null; renderReadiness(); return; }
    state.readiness = { result: job.result, jobId: job_id, scopeKey: state.readiness.scopeKey };
    doneToast("readiness", "The readiness report is ready");
    renderReadiness();
  } catch (error) {
    if (token === state.readinessToken) { state.readiness = { error: error.message }; renderReadiness(); }
  }
}

$("triage-readiness").addEventListener("click", () => showView("readiness"));
$("readiness-run").addEventListener("click", () => runReadiness());
$("readiness-cancel").addEventListener("click", async () => {
  const r = state.readiness;
  if (r && r.jobId) { try { await api(`api/jobs/${r.jobId}/cancel`, { method: "POST", body: {} }); } catch { /* already finished */ } }
});
$("readiness-body").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-open]");
  if (row) startSnapshot(JSON.parse(row.dataset.open));
});
for (const fmt of ["md", "pdf"]) {
  $(`readiness-export-${fmt}`).addEventListener("click", async () => {
    const r = state.readiness;
    if (!r || !r.jobId || !r.result) return;
    try {
      await download(`api/readiness/${r.jobId}/export`, { format: fmt }, `readiness.${fmt}`);
    } catch (error) {
      $("readiness-body").insertAdjacentHTML("afterbegin", noticeHtml(error));
    }
  });
}

// ------------------------------------------------------------------ network
// Every VNet in scope (two Resource Graph queries), then one VNet in depth: like azure-subnet-check.

// ------------------------------------------------------------------ personas
// How a kind of user looks at the console (core/personas.py): home view, menu, card and tab order, detail. Never what
// they can read: their Azure roles decide that, and every view stays one click away under More.

state.personas = [];
state.persona = null;

async function loadPersonas() {
  let data;
  try { data = await api("api/personas"); } catch { data = { personas: [], default: "" }; }
  state.personas = data.personas || [];
  state.summaryCards = data.summary_cards || [];
  const saved = data.configured || stored.get("ahc.persona");  // Settings' persona opens every start; View as switches after
  state.personaChosen = state.personas.some((p) => p.name === saved);
  state.persona = state.personas.find((p) => p.name === saved) || state.personas.find((p) => p.name === data.default) || null;
  showPersonaPick();
  $("persona").innerHTML = state.personas.map((p) => `<option value="${esc(p.name)}" title="${esc(p.about)}" ${state.persona && p.name === state.persona.name ? "selected" : ""}>${esc(p.label)}</option>`).join("");
  applyPersona();
}

const personaHome = () => (state.persona && state.persona.home) || "triage";
const personaDetail = () => (state.persona && state.persona.detail) || "full";

function inPersona(view) {
  const p = state.persona;
  if (!p || view === "settings") return true;
  return p.views.includes(view.startsWith("tool-") || view === "servicenow" ? "tools" : view);
}

function choosePersona(name) {
  const p = state.personas.find((x) => x.name === name);
  if (!p) return;
  state.persona = p;
  state.personaChosen = true;
  stored.set("ahc.persona", name);
  $("persona").value = name;
  showPersonaPick();
  applyPersona();
  state.summaryLoaded = "";
  showView(p.home);
}

function applyPersona() {
  document.body.classList.toggle("persona-summary", personaDetail() === "summary");
  const more = $("menu-more");
  more.innerHTML = "";
  for (const b of document.querySelectorAll(".views [data-view]")) {
    if (b.closest("#menu-more")) continue;
    const off = !inPersona(b.dataset.view);
    b.classList.toggle("persona-off", off);
    if (off && !b.classList.contains("hidden")) {
      more.insertAdjacentHTML("beforeend", `<button data-view="${esc(b.dataset.view)}" role="tab">${esc(b.dataset.short || b.firstChild.textContent.trim())}</button>`);
    }
  }
  for (const menu of document.querySelectorAll(".views .nav-menu:not(#more-menu)")) {
    const entries = [...menu.querySelectorAll(".nav-panel [data-view]")].filter((b) => !b.classList.contains("hidden"));
    menu.classList.toggle("persona-off", entries.length > 0 && entries.every((b) => b.classList.contains("persona-off")));
  }
  $("more-menu").classList.toggle("hidden", !more.children.length);
}

// A resource's tabs, the persona's first (in its order), then the rest as before; its first one is where a resource opens.
function personaTabs(tabs) {
  const order = (state.persona && state.persona.tabs) || [];
  const rank = (key) => (order.includes(key) ? order.indexOf(key) : order.length);
  return [...tabs].sort((a, b) => rank(a[0]) - rank(b[0]));
}
const personaCardRank = (key) => {
  const order = (state.persona && state.persona.cards) || [];
  return order.includes(key) ? order.indexOf(key) : order.length;
};

// First run: one banner under the scope bar until a persona is picked; Not now hides it for this page.
function showPersonaPick() {
  const box = $("persona-first");
  const show = !state.personaChosen && !state.personaDeferred && state.personas.length > 0;
  box.classList.toggle("hidden", !show);
  box.innerHTML = show ? personaPickHtml() : "";
}

function personaPickHtml() {
  return `<div class="persona-first-head"><h3>How do you mostly use this?</h3>
    <button type="button" class="link" id="persona-later">Not now</button></div>
    <p class="muted">It sets your home page and what leads the menu; you can change it any time (View as, top right), and
      nothing is hidden: the rest is under More.</p>
    <div class="persona-choices">${state.personas.map((p) => `<button type="button" data-pick-persona="${esc(p.name)}">
      <strong>${esc(p.label)}</strong><span class="muted">${esc(p.about)}</span></button>`).join("")}</div>`;
}

$("persona").addEventListener("change", (event) => choosePersona(event.target.value));
document.addEventListener("click", (event) => {
  const pick = event.target.closest && event.target.closest("[data-pick-persona]");
  if (pick) choosePersona(pick.dataset.pickPersona);
  if (event.target.id === "persona-later") { state.personaDeferred = true; showPersonaPick(); }
});

