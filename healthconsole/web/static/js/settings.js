"use strict";

// ------------------------------------------------------------------ settings


async function loadSettings() {
  $("settings-message").innerHTML = "";
  loadIntegrations();
  renderAlertRules();
  $("settings-body").innerHTML = loading("Reading settings…");
  try {
    state.settings = await api("api/settings");
  } catch (error) {
    $("settings-body").innerHTML = noticeHtml(error);
    return;
  }
  renderSettings();
}

function fieldInput(field) {
  const id = `set-${field.name}`;
  const value = field.value;
  if (field.name === "persona") {  // its choices are the personas, built in and from the file
    const option = (name, label) => `<option value="${esc(name)}" ${name === (value || "") ? "selected" : ""}>${esc(label)}</option>`;
    return `<select id="${esc(id)}">${option("", "Not set: ask on first run")}${(state.personas || []).map((p) => option(p.name, p.label)).join("")}</select>`;
  }
  if (field.type === "bool") return `<input type="checkbox" id="${esc(id)}" ${value ? "checked" : ""}>`;
  if (field.type === "int") return `<input type="number" min="0" step="1" id="${esc(id)}" value="${esc(value)}">`;
  if (field.type === "ports") return `<input type="text" id="${esc(id)}" value="${esc((value || []).join(", "))}" placeholder="22, 3389">`;
  if (Array.isArray(field.type)) {
    const labels = OPTION_LABELS[field.name] || {};
    return `<select id="${esc(id)}">${field.type.map((c) => `<option value="${esc(c)}" ${c === value ? "selected" : ""}>${esc(labels[c] || c)}</option>`).join("")}</select>`;
  }
  return `<input type="text" id="${esc(id)}" value="${esc(value ?? "")}" placeholder="${esc(field.default ?? "(empty)")}">`;
}

function readField(field) {
  const el = $(`set-${field.name}`);
  if (field.type === "bool") return el.checked;
  if (field.type === "int") return el.value === "" ? null : Number(el.value);
  if (field.type === "ports") return el.value.split(/[\s,]+/).filter(Boolean).map(Number);
  if (Array.isArray(field.type) && typeof field.type[0] === "number") return Number(el.value);
  return el.value;
}

// Plain names for the settings; the config key is shown small beside each, for people editing the file.
const SETTING_LABELS = {
  "triage.scope": "When the page opens", "triage.auto_refresh_minutes": "Refresh Triage by itself", "triage.changes_hours": "Changes shown on problems, looking back (hours)",
  "triage.include_system_changes": "Include changes made by Azure itself", "triage.secret_scan_max_vaults": "Scan Key Vaults automatically up to (vaults)",
  "triage.azure_status_feed": "Read the public Azure status feed", "updates.check": "Tell me when a new version is out",
  "graph.auto": "Draw dependencies on every click", "graph.depth": "Hops to follow", "graph.max_nodes": "Stop drawing at (resources)",
  "graph.inbound": "Also find what points at a resource", "graph.inbound_limit": "At most this many inbound resources",
  "graph.inbound_scope": "Where to look for inbound references",
  "health.window_minutes": "Metric window for \u201cnow\u201d (minutes)", "health.compare_last_week": "Compare with the same window last week",
  "health.changes_hours": "Changes shown per resource (hours)", "health.alert_hours": "Ignore stateless alerts older than (hours)",
  "health.critical_max_severity": "Critical from alert severity (Sev0 is worst)", "health.warning_max_severity": "Warning from alert severity",
  "health.heuristic_limit": "Metrics picked for types without a profile", "health.expiry_warning_days": "Expiry turns yellow (days before)",
  "health.expiry_critical_days": "Expiry turns red (days before)", "health.detectors": "Run Azure's detectors in each check",
  "network.subnet_warning_percent": "Subnet turns yellow at (% of IPs used)", "network.subnet_critical_percent": "Subnet turns red at (% of IPs used)",
  "aks.fetch_credentials": "Fetch the AKS user credential when no kubeconfig matches",
  "aks.memory_near_limit_percent": "Memory near its limit at (% of the limit)", "aks.cpu_near_limit_percent": "CPU throttled at (% of the limit)",
  "aks.cpu_above_request_percent": "CPU above its request at (% of the request)", "aks.cpu_idle_percent": "CPU unused below (% of the request)",
  "aks.hpa_target_high_percent": "HPA target counts as high above (%)", "aks.hpa_near_max_percent": "HPA near its maximum at (% of maxReplicas)",
  "aks.hpa_overshoot_percent": "HPA metric far over its target at (% of the target)", "aks.pool_requests_percent": "Node pool nearly full at (% of allocatable requested)",
  "aks.restarts_warning": "Restarts turn yellow from", "aks.leftover_hours": "Failed pods are left over after (hours)",
  "aks.rollout_hours": "A rollout is recent within (hours)", "aks.node_image_warning_days": "Node image turns yellow at (days old)",
  "aks.node_image_critical_days": "Node image turns red at (days old)",
  "aks.restarts_look_back_hours": "Look back's default for restarts", "aks.oom_look_back_hours": "Look back's default for OOM kills",
  "aks.events_look_back_hours": "Look back's default for warning events",
  "aks.oom_near_limit_percent": "OOM kill: probably its own limit at (% of the limit)", "aks.oom_node_full_percent": "OOM kill: probably the node at (% of node memory)",
  "appinsights.failed_min": "Spike: failed requests at least", "appinsights.failed_rate_points": "Spike: failure rate up by (points)",
  "appinsights.exceptions_min": "Spike: exceptions at least", "appinsights.exceptions_percent": "Spike: exceptions at (% of last week)",
  "appinsights.critical_rate_percent": "Spike turns red at (% failure rate)", "appinsights.critical_failed_min": "...with failed requests at least",
  "path.drop_percent": "Path: traffic dropped under (% of last week)", "path.drop_min": "...when last week had requests at least",
  "path.restarts_min": "Path: pod restarts rising from", "path.restarts_critical": "...red from",
  "security.risky_ports": "Ports that are a warning when open to the internet",
  "triage.capacity_near_max_percent": "Autoscale near its maximum at (% of the maximum)", "triage.resolved_hours": "Show resolved Service Health issues for (hours)",
  "health.baseline_change_percent": "Moved against last week at (% of last week)", "cost.spike_percent": "Cost spike at (% of a usual day)",
  "vulnerabilities.eol_warning_days": "Runtime end of support turns yellow (days before)",
  "summary.top_items": "Items listed per card", "summary.readiness_quick": "Run Readiness's quick checks on open",
  "summary.budget_warn_percent": "Budget turns yellow at (% spent)", "summary.compare_hours": "What changed compares with (hours ago)",
  "logs.dir": "Log folder", "logs.keep_days": "Days the log keeps",
  "summary.keep_days": "Keep summaries for (days)",
  "audit.keep_days": "Keep the last result for (days, 0 = never)",
  "alerts.history_hours": "Keep the Alerts bar's alerts for (hours)",
  "alert_check.days": "Days of history to read", "alert_check.min_buckets": "Fewest values before a verdict",
  "alert_check.always_on_percent": "Fires on ordinary values at (% of history)", "alert_check.rare_percent": "Unusual at (% of history)",
  "alert_check.change_percent": "A change against last week at (%)",
  persona: "Open as (View as)", default_tenant: "Tenant to open with", watchlist_file: "Watchlist file", profiles_dir: "Health profiles folder",
  exports_dir: "Exports folder", pdf_font: "PDF font (.ttf)",
};
const OPTION_LABELS = {
  "triage.auto_refresh_minutes": { 0: "Off", 1: "Every minute", 2: "Every 2 minutes", 5: "Every 5 minutes", 10: "Every 10 minutes", 15: "Every 15 minutes" },
  "graph.inbound_scope": { subscription: "The same subscription", tenant: "The whole tenant (slower)" },
  ...Object.fromEntries(["restarts", "oom", "events"].map((k) => [`aks.${k}_look_back_hours`,
    { 1: "1 hour", 6: "6 hours", 24: "24 hours", 72: "3 days", 168: "7 days", 336: "14 days", 720: "30 days" }])),
  persona: { "": "ask on first run" },
};
const PANE_OF = { updates: "general", triage: "general", summary: "general", alerts: "general", audit: "general", health: "thresholds", network: "thresholds", alert_check: "thresholds", cost: "thresholds", vulnerabilities: "thresholds",
  appinsights: "thresholds", path: "thresholds", security: "thresholds", graph: "graph", aks: "aks", files: "files", persona: "persona", logs: "files" };

function settingRows(fields) {
  return fields.map((f) => {
    const differs = JSON.stringify(f.value) !== JSON.stringify(f.default);
    const shown = OPTION_LABELS[f.name] ? OPTION_LABELS[f.name][f.default] || f.default : f.default;
    return `<div class="set-row field ${differs ? "differs" : ""}" data-name="${esc(f.name)}">
      <label for="set-${esc(f.name)}">${esc(SETTING_LABELS[f.name] || f.name)}</label>
      <div>${fieldInput(f)}</div>
      <div class="help">${esc(f.help)} <span class="muted">Default: ${esc(shown === "" || shown == null ? "empty" : shown)}.</span>
        ${differs ? `<button type="button" class="link" data-reset="${esc(f.name)}">reset</button>` : ""} <code>${esc(f.name)}</code></div></div>`;
  }).join("");
}

// ------------------------------------------------------------------ scopes: Open with and the old favourites
const OPEN_WITH_LABELS = { none: "Nothing until I pick a scope", all: "Every subscription I can read" };

function openWithOptions(current) {
  const names = (state.savedScopes || []).map((s) => s.name);
  const missing = current && !OPEN_WITH_LABELS[current] && !findSavedScope(current) ? [current] : [];
  const option = (value, label) => `<option value="${esc(value)}" ${value.toLowerCase() === String(current || "").toLowerCase() ? "selected" : ""}>${esc(label)}</option>`;
  return [option("none", OPEN_WITH_LABELS.none), option("all", OPEN_WITH_LABELS.all),
    ...names.map((n) => option(n, `Saved scope: ${n}`)), ...missing.map((n) => option(n, `${n} (no longer a saved scope)`))].join("");
}

function savedScopeList(scopes) {
  return (scopes || []).length ? `<ul class="scope-list">${scopes.map((s) => `<li><strong>${esc(s.name)}</strong>
      <span class="muted">${esc(scopeSummary(s))}${s.legacy ? " · from your old favourites" : ""}</span></li>`).join("")}</ul>
    <p class="muted">Rename or delete them in the Scope bar's <strong>Saved scopes</strong> menu.</p>`
    : `<p class="muted">No saved scopes yet. Pick subscriptions, tag filters and groups in the Scope bar, then
      <strong>Saved scopes → Save current as…</strong>.</p>`;
}

// Rule #38: old settings still in use are named with their fix, here and on Triage.
function legacyScopeHtml() {
  const legacy = state.legacyScope;
  if (!legacy) return "";
  const keys = legacy.keys.map((k) => `<code>${esc(k)}</code>`).join(", ");
  const into = legacy.scopes.length ? `the saved scope${legacy.scopes.length === 1 ? "" : "s"} ${legacy.scopes.map((n) => `<strong>${esc(n)}</strong>`).join(" and ")}`
    : "Open with";
  return `<div class="notice">Your config still has ${keys} from before saved scopes. They work for now as ${into}
      (opening with ${esc(OPEN_WITH_LABELS[legacy.open_with] ? legacy.open_with : `"${legacy.open_with}"`)}).
      <button type="button" data-move-legacy>Move them into saved scopes</button>
      <span class="muted">Writes them under this tenant in config.yaml and removes the old keys (a .bak copy is kept).</span></div>`;
}

function scopeNotices() {
  const parts = [];
  if (state.legacyScope) parts.push(legacyScopeHtml());
  if (state.openWithMissing) parts.push(`<p class="notice">Open with is set to "${esc(state.openWithMissing)}", which isn't a saved scope
    any more, so the page opened with nothing. Pick another in Settings → General.</p>`);
  return parts.join("");
}

async function afterScopeSettings(payload, message) {
  applyScopePayload(payload);
  state.openWithMissing = "";
  if (state.settings) {
    Object.assign(state.settings, { saved_scopes: payload.saved_scopes, open_with: payload.open_with, legacy_scope: payload.legacy_scope });
    if (state.view === "settings") renderSettings();
  }
  renderSavedScopes();
  $("triage-notice").innerHTML = scopeNotices();
  flash(message, false, 4000);
}

async function saveOpenWith(value) {
  try {
    await afterScopeSettings(await api("api/open-with", { method: "POST", body: { open_with: value } }),
      `The page now opens with ${{ none: "nothing selected", all: "every subscription" }[value] || `the saved scope ${value}`}.`);
  } catch (error) { showMessage(`Not saved: ${error.message}`, true); }
}

async function moveLegacyScope() {
  try {
    await afterScopeSettings(await api("api/saved-scopes/move-legacy", { method: "POST", body: {} }),
      "Moved: your old favourites and filters are saved scopes now.");
  } catch (error) { showMessage(`Not moved: ${error.message}`, true); }
}

document.addEventListener("click", (event) => {
  if (event.target.closest("#triage-notice [data-move-legacy], #saved-panel [data-move-legacy]")) moveLegacyScope();
});

// The Thresholds pane's sections, in order; one mapped in PANE_OF but missing here would never be drawn (tested).
const THRESHOLD_SECTIONS = [["health", "Health"], ["network", "Network"], ["appinsights", "App Insights: failure spikes"],
  ["path", "Path of a workload"], ["cost", "Cost"], ["security", "Security"], ["vulnerabilities", "Vulnerabilities"],
  ["alert_check", "Is this alert right?"]];

function renderSettings() {
  const s = state.settings;
  const t = s.tenant;
  $("settings-path").textContent = `${s.path}${s.exists ? "" : " (not created yet; Save creates it)"}`;
  const panes = { general: [], thresholds: [], graph: [], aks: [], files: [] };
  panes.persona = [];
  for (const field of s.fields) panes[PANE_OF[field.name] || PANE_OF[field.section] || "files"].push(field);
  const bySection = (pane, section) => panes[pane].filter((f) => f.section === section);
  $("settings-body").innerHTML = `
    <div class="settings-section" data-pane="general">
      <h3>Persona</h3>${settingRows(panes.persona)}
      <h3>Scopes <span class="muted">for ${esc(t.name)}</span></h3>
      ${legacyScopeHtml()}
      <div class="set-row"><label for="set-open-with">Open with</label>
        <div><select id="set-open-with">${openWithOptions(s.open_with)}</select></div>
        <div class="help">What the page looks at when it opens. <strong>Nothing</strong> queries nothing until you pick a
          scope. Saved as soon as you change it. <code>open_with</code></div></div>
      <div id="settings-scopes">${savedScopeList(s.saved_scopes)}</div>
      <h3>Triage</h3>${settingRows(bySection("general", "triage"))}
      <h3>Summary</h3>${settingRows(bySection("general", "summary"))}
      <h3>Alerts</h3>${settingRows(bySection("general", "alerts"))}
      <h3>Access audit</h3>${settingRows(bySection("general", "audit"))}
      <h3>Updates</h3>${settingRows(bySection("general", "updates"))}
    </div>
    <div class="settings-section" data-pane="thresholds">${THRESHOLD_SECTIONS.map(([section, title]) =>
      bySection("thresholds", section).length ? `<h3>${esc(title)}</h3>${settingRows(bySection("thresholds", section))}` : "").join("")}
    </div>
    <div class="settings-section" data-pane="graph"><h3>Dependency graph</h3>${settingRows(panes.graph)}</div>
    <div class="settings-section" data-pane="aks"><h3>AKS</h3>${settingRows(panes.aks)}
      <p class="muted">Kubeconfig files read besides the usual ones: ${esc((s.kubeconfig_paths || []).join(", ") || "none")}.
        Lists like this are edited in the file (<code>aks.kubeconfig_paths</code>), then Reload from file.</p></div>
    <div class="settings-section" data-pane="files"><h3>Files</h3>${settingRows(panes.files)}
      <h3>Watchlist</h3><div id="settings-watchlist"></div></div>`;
  state.settingsResets = new Set();
  renderSettingsWatchlist();
  showSettingsPane(state.settingsPane || "general");
}

const AUTOSAVE_PANES = new Set(["integrations", "alerts"]);  // their switches and cards save on their own
const VIEW_PANES = new Set(["logs"]);  // nothing to save there

// Save/Discard stay in the header; on a pane that saves by itself they show only while other panes have changes.
function updateSettingsHead(names) {
  const auto = (AUTOSAVE_PANES.has(state.settingsPane) || VIEW_PANES.has(state.settingsPane)) && !names.length;
  $("settings-save").disabled = $("settings-discard").disabled = !names.length;
  $("settings-save").classList.toggle("hidden", auto);
  $("settings-discard").classList.toggle("hidden", auto);
  $("settings-autosave").classList.toggle("hidden", !AUTOSAVE_PANES.has(state.settingsPane));
  $("settings-changes").textContent = names.length
    ? `${names.length} unsaved change${names.length === 1 ? "" : "s"}: ${names.join(", ")}` : "";
}

function showSettingsPane(pane) {
  state.settingsPane = pane;
  for (const el of document.querySelectorAll(".settings-main [data-pane]")) el.classList.toggle("hidden", el.dataset.pane !== pane);
  for (const b of document.querySelectorAll("#settings-nav [data-pane]")) b.classList.toggle("active", b.dataset.pane === pane);
  if (state.settings) updateSettingsHead(Object.keys(settingsChanges()).map((n) => SETTING_LABELS[n] || n));
  if (pane === "logs") showLogs();
}

$("settings-nav").addEventListener("click", (event) => {
  const b = event.target.closest("[data-pane]");
  if (b) showSettingsPane(b.dataset.pane);
});

function renderSettingsWatchlist() {
  const box = $("settings-watchlist");
  if (!box) return;
  const items = state.watchItems || [];
  box.innerHTML = `<p class="muted">Saved in <code>${esc(state.watchlistPath || "")}</code>. Add with <strong>+ Watch</strong>
      next to any resource or resource group (Triage, search, Browse, Secrets, a resource's details), or from the Watchlist tab.</p>
    ${items.length ? `<table>${items.map((item) => `<tr><td><strong>${esc(item.name)}</strong>
        <div class="muted">${esc(item.kind)}</div></td><td>${watchButton(item)}</td></tr>`).join("")}</table>`
      : '<p class="muted">Nothing on the Watchlist yet.</p>'}`;
}

// Lists (ports) compare by their contents: two arrays are never === even with the same numbers.
const sameSetting = (a, b) => (Array.isArray(a) || Array.isArray(b) ? JSON.stringify(a || []) === JSON.stringify(b || []) : a === b);

function settingsChanges() {
  const s = state.settings;
  const values = {};
  for (const field of s.fields) {
    if (state.settingsResets.has(field.name)) { values[field.name] = "__default__"; continue; }
    const now = readField(field);
    if (!sameSetting(now, field.value) && !(now === "" && field.value == null)) values[field.name] = now;
  }
  return values;
}

function markSettingsDirty() {
  const values = settingsChanges();
  document.querySelectorAll("#settings-body .field").forEach((el) => el.classList.toggle("changed", el.dataset.name in values));
  const names = Object.keys(values).map((n) => SETTING_LABELS[n] || n);
  updateSettingsHead(names);
}

$("settings-discard").addEventListener("click", () => { renderSettings(); $("settings-message").innerHTML = ""; });

$("settings-body").addEventListener("input", markSettingsDirty);
$("settings-body").addEventListener("change", (event) => {
  if (event.target.id === "set-open-with") { saveOpenWith(event.target.value); return; }
  markSettingsDirty();
});
$("settings-body").addEventListener("click", (event) => {
  if (event.target.closest("[data-move-legacy]")) { moveLegacyScope(); return; }
  const name = event.target.dataset && event.target.dataset.reset;
  if (!name) return;
  const field = state.settings.fields.find((f) => f.name === name);
  state.settingsResets.add(name);
  const el = $(`set-${name}`);
  if (field.type === "bool") el.checked = !!field.default; else el.value = field.default ?? "";
  markSettingsDirty();
});

$("settings-save").addEventListener("click", async () => {
  const values = settingsChanges();
  $("settings-save").disabled = true;
  $("settings-message").innerHTML = loading("Saving…");
  try {
    const result = await api("api/settings", { method: "POST", body: { values } });
    state.settings = result.settings;
    await loadPersonas();  // a new Open as applies now, not only at the next start
    renderSettings();
    $("settings-message").innerHTML = `<p class="message">Saved and applied. The previous file is kept as ${esc(result.backup)}.</p>`;
  } catch (error) {
    $("settings-message").innerHTML = `<p class="notice">Not saved: ${esc(error.message)}</p>`;
    markSettingsDirty();
  }
});
$("settings-reload").addEventListener("click", async () => {
  $("settings-message").innerHTML = loading("Reloading…");
  try {
    state.settings = await api("api/settings/reload", { method: "POST", body: {} });
    await loadPersonas();
    renderSettings();
    $("settings-message").innerHTML = '<p class="message">Reloaded from the file and applied.</p>';
    await loadContextForTenant();
  } catch (error) {
    $("settings-message").innerHTML = noticeHtml(error);
  }
});

