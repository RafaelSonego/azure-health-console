"use strict";

// ------------------------------------------------------------------ saved scopes
// A named scope per tenant (subscriptions, resource groups, tag filters and the connection of each kind), saved in
// config.yaml and applied in one pick. Anything changed afterwards marks it "(changed)" until saved again.

function scopeSnapshot() {
  return JSON.stringify({ subs: [...state.scope.subs].sort(), groups: [...state.scope.groups].sort(),
    tags: Object.entries(state.scope.tags).sort(), conns: Object.entries(state.connDraft || {}).sort() });
}

function renderSavedButton() {
  const active = state.activeScope;
  const changed = active && active.snapshot !== scopeSnapshot();
  const saved = (state.savedScopes || []).length;
  $("saved-button").textContent = active ? `${active.name}${changed ? " (changed)" : ""}`
    : saved ? `None picked (${saved} saved)` : "None saved yet";
  $("saved-button").classList.toggle("active-scope", !!active);
}
state.markScopeChanged = renderSavedButton;

// What a saved scope holds, by name: `max` names per list (the tooltip passes Infinity).
function scopeSummary(scope, max = 3) {
  const parts = [namesText(scope.subscriptions, max)];
  if (scope.resource_groups.length) parts.push(`groups ${namesText(scope.resource_groups, max)}`);
  if (Object.keys(scope.tag_filters).length) parts.push(Object.entries(scope.tag_filters).map(([k, v]) => `${k}=${v === UNTAGGED ? "(untagged)" : v}`).join(", "));
  if (Object.keys(scope.connections).length) parts.push(Object.values(scope.connections).join(", "));
  return parts.join(" · ");
}

function activeChanged() {
  const active = state.activeScope;
  return active && active.snapshot !== scopeSnapshot() && (state.savedScopes || []).some((s) => s.name === active.name);
}

function currentAsSaved(name) {
  return { name, subscriptions: state.subscriptions.filter((s) => state.scope.subs.has(s.id)).map((s) => s.name),
    resource_groups: state.scope.groups.size ? [...new Set(groupsInScope().map((g) => g.name))] : [],
    tag_filters: { ...state.scope.tags }, connections: { ...(state.connDraft || {}) } };
}

function renderSavedScopes() {
  const scopes = state.savedScopes || [];
  $("saved-changed").classList.toggle("hidden", !activeChanged());
  if (activeChanged()) {
    $("saved-update").textContent = `Update "${state.activeScope.name}"`;
  }
  $("saved-list").innerHTML = scopes.length ? scopes.map((s, i) => `<div class="saved-row">
      <button type="button" class="saved-apply" data-saved-apply="${i}" title="${esc(scopeSummary(s, Infinity))}"><strong>${esc(s.name)}</strong>
        <span class="muted">${esc(scopeSummary(s))}</span></button>
      ${s.legacy ? '<span class="muted">from your old favourites</span> <button type="button" class="link" data-move-legacy>move to saved scopes</button>'
        : `<button type="button" class="link" data-saved-rename="${i}">rename</button>
      <button type="button" class="link" data-saved-delete="${i}">delete</button>`}</div>`).join("")
    : `<p class="hint">No saved scopes for this tenant yet. Pick subscriptions, filters and connections, then
        <strong>Save current as…</strong>. They're kept in config.yaml under this tenant.</p>`;
}

async function applySavedScope(scope) {
  const subs = state.subscriptions.filter((s) => globMatch(scope.subscriptions, s.name) || globMatch(scope.subscriptions, s.id));
  if (!subs.length) { showMessage(`None of ${scope.name}'s subscriptions (${scope.subscriptions.join(", ")}) are visible in this tenant.`, true); return; }
  state.connDraft = { ...(state.connDraft || {}), ...scope.connections };
  renderConnectionPickers();
  state.scope.subs = new Set(subs.map((s) => s.id));
  state.scope.tags = { ...scope.tag_filters };
  state.scope.groups.clear();
  renderSubscriptions();
  await loadGroups();
  if (scope.resource_groups.length) {
    state.scope.groups = new Set(state.groups.filter((g) => globMatch(scope.resource_groups, g.name)).map(groupKey));
  }
  applyScope();  // a saved scope is an explicit choice: no separate Apply
  state.activeScope = { name: scope.name, snapshot: scopeSnapshot() };
  renderSavedButton();
}

// Asks for a scope name until it's one the config accepts; empty means cancelled. Reserved names come from the server.
function askScopeName(question, initial) {
  let ask = question;
  for (;;) {
    const name = (window.prompt(ask, initial) || "").trim();
    if (!(state.reservedScopeNames || []).includes(name.toLowerCase())) return name;
    ask = `"${name}" is reserved: Settings → Open with uses it for ${name.toLowerCase() === "all" ? "every subscription" : "nothing picked"}. Pick another name (e.g. "All subscriptions"):`;
    initial = name.toLowerCase() === "all" ? "All subscriptions" : "";
  }
}

async function storeSavedScopes(scopes, done) {
  try {
    applyScopePayload(await api("api/saved-scopes", { method: "POST", body: { scopes } }));
    renderSavedScopes();
    done();
  } catch (error) { showMessage(`Not saved: ${error.message}`, true); }
}

$("saved-button").addEventListener("click", () => togglePanel("saved-panel"));
document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-start-saved]");
  const scope = button && (state.savedScopes || [])[Number(button.dataset.startSaved)];
  if (scope) applySavedScope(scope);
});
$("saved-panel").addEventListener("click", async (event) => {
  const el = event.target.closest("button");
  if (!el) return;
  const scopes = state.savedScopes || [];
  if (el.id === "saved-save") {
    const name = askScopeName("Name for this scope (e.g. Prod, NonProd):", state.activeScope ? state.activeScope.name : "");
    if (!name) return;
    const existing = scopes.findIndex((s) => s.name.toLowerCase() === name.toLowerCase());
    if (existing >= 0 && !window.confirm(`Replace the saved scope "${scopes[existing].name}"?`)) return;
    const scope = currentAsSaved(name);
    const next = existing >= 0 ? scopes.map((s, i) => (i === existing ? scope : s)) : [...scopes, scope];
    await storeSavedScopes(next, () => { state.activeScope = { name, snapshot: scopeSnapshot() }; renderSavedButton(); showMessage(`Saved scope "${name}".`); });
    return;
  }
  if (el.id === "saved-update" || el.id === "saved-revert") {
    const index = scopes.findIndex((s) => s.name === (state.activeScope || {}).name);
    if (index < 0) return;
    if (el.id === "saved-revert") { $("saved-panel").classList.add("hidden"); await applySavedScope(scopes[index]); return; }
    const name = scopes[index].name;
    await storeSavedScopes(scopes.map((s, i) => (i === index ? currentAsSaved(name) : s)), () => {
      state.activeScope = { name, snapshot: scopeSnapshot() }; renderSavedButton(); renderSavedScopes(); showMessage(`Updated scope "${name}".`);
    });
    return;
  }
  const index = Number(el.dataset.savedApply ?? el.dataset.savedRename ?? el.dataset.savedDelete);
  const scope = scopes[index];
  if (!scope) return;
  if (el.dataset.savedApply !== undefined) { $("saved-panel").classList.add("hidden"); await applySavedScope(scope); return; }
  if (el.dataset.savedRename !== undefined) {
    const name = askScopeName(`New name for "${scope.name}":`, scope.name);
    if (!name || name === scope.name) return;
    await storeSavedScopes(scopes.map((s, i) => (i === index ? { ...s, name } : s)), () => {
      if (state.activeScope && state.activeScope.name === scope.name) { state.activeScope.name = name; renderSavedButton(); }
    });
    return;
  }
  if (el.dataset.savedDelete !== undefined && window.confirm(`Delete the saved scope "${scope.name}"?`)) {
    await storeSavedScopes(scopes.filter((_, i) => i !== index), () => {
      if (state.activeScope && state.activeScope.name === scope.name) { state.activeScope = null; renderSavedButton(); }
    });
  }
});

// ------------------------------------------------------------------ connections in the Scope bar
// One selector per kind this tenant has (ServiceNow, then each monitoring tool), listing its turned-on connections.
// The choice is remembered per tenant in this browser; saved scopes can set it too.

function rememberedConnections(tenantId) {
  return stored.json(`ahc.conn.${tenantId}`, {});
}

function rememberConnections() {
  stored.set(`ahc.conn.${state.connTenant}`, state.selected);
}

function setConnections(data) {
  const byKind = {};
  for (const c of data.servicenow) if (c.enabled) (byKind.servicenow = byKind.servicenow || []).push(c);
  for (const m of data.monitors) if (m.enabled) (byKind[m.kind] = byKind[m.kind] || []).push(m);
  state.connections = byKind;
  state.connTenant = data.tenant.id;
  const remembered = rememberedConnections(data.tenant.id);
  state.selected = {};
  for (const [kind, list] of Object.entries(byKind)) {
    const keep = list.find((c) => c.name === remembered[kind]);
    state.selected[kind] = (keep || list.find((c) => c.ready) || list[0]).name;
  }
  state.connDraft = { ...state.selected };
  state.snEnabled = !!byKind.servicenow;
  renderConnectionPickers();
}

function renderConnectionPickers() {
  const kinds = Object.keys(state.connections || {}).sort((a, b) => (a === "servicenow" ? -1 : b === "servicenow" ? 1 : a.localeCompare(b)));
  $("conn-pickers").innerHTML = kinds.map((kind) => {
    const list = state.connections[kind];
    const picked = state.connDraft[kind];
    const missing = picked && !list.some((c) => c.name === picked);
    return `<label class="conn-picker" title="Which ${esc(kind === "servicenow" ? "ServiceNow" : toolLabel(kind))} connection the tabs and Triage use">
      ${esc(kind === "servicenow" ? "ServiceNow" : toolLabel(kind))}
      <select data-conn-kind="${esc(kind)}">${missing ? `<option selected>⚠ ${esc(picked)} isn't available</option>` : ""}
        ${list.map((c) => `<option value="${esc(c.name)}" ${c.name === picked ? "selected" : ""}>${c.ready ? "" : "⚠ "}${esc(c.name)}</option>`).join("")}
      </select></label>`;
  }).join("");
  renderSavedButton();
}

$("conn-pickers").addEventListener("change", (event) => {
  const kind = event.target.dataset.connKind;
  if (!kind) return;
  state.connDraft[kind] = event.target.value;
  draftChanged();
});

// Apply right after changing subscriptions would take the previous subscriptions' groups: wait for this set's first.
$("scope-apply").addEventListener("click", async () => {
  if (state.groupsFor !== subsKey(state.scope.subs)) {
    $("scope-apply").disabled = true;
    $("scope-pending-text").textContent = "Loading the resource groups of these subscriptions…";
    try {
      while (state.groupsFor !== subsKey(state.scope.subs)) await loadGroups();
    } catch (error) { showMessage(error.message, true); return; } finally { $("scope-apply").disabled = false; }
  }
  applyScope();
});
$("scope-discard").addEventListener("click", () => discardScope());

function connectionsChanged(kind) {
  if (state.markScopeChanged) state.markScopeChanged();
  if (kind === "detail-only") {  // the scope moved too, so its reload already covers the tabs
    if (state.exploreNode && state.snapshot) showDetail(state.exploreNode.id, $("detail").querySelector(".tabs .active")?.dataset.tab || "overview");
    return;
  }
  if (kind === "servicenow" || !kind) { state.snLoaded = false; if (state.view === "servicenow") loadServiceNow(); }
  if (kind !== "servicenow") { state.monLoaded = false; if ((state.view || "").startsWith("tool-")) loadMonitors(); }
  state.triageLoaded = false;
  if (state.view === "triage") loadTriage();
  if (state.exploreNode && state.snapshot) showDetail(state.exploreNode.id, $("detail").querySelector(".tabs .active")?.dataset.tab || "overview");
}

// The ServiceNow tab, and one tab per monitoring tool, appear when this tenant has a turned-on connection of it.
async function refreshIntegrationTabs() {
  try {
    setConnections(await api("api/integrations"));
  } catch { state.connections = {}; state.selected = {}; state.connDraft = {}; state.snEnabled = false; renderConnectionPickers(); }
  $("tab-servicenow").classList.toggle("hidden", !state.snEnabled);
  $("tool-tabs").innerHTML = toolKinds().map((kind) =>
    `<button data-view="tool-${esc(kind)}" role="tab" class="${state.view === `tool-${kind}` ? "active" : ""}">${esc(toolLabel(kind))}</button>`).join("");
  const gone = (state.view === "servicenow" && !state.snEnabled) || (state.view?.startsWith("tool-") && !toolKinds().includes(state.view.slice(5)));
  if (gone) showView("triage");
  renderNavMenus();
}

