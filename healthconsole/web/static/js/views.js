"use strict";

// ------------------------------------------------------------------ context & scope

// Saved scopes, Open with and the old favourites still to move, from /api/context and every scope save.
function applyScopePayload(payload) {
  state.savedScopes = payload.saved_scopes || [];
  state.openWith = payload.open_with || "none";
  state.reservedScopeNames = payload.reserved_names || [];
  state.legacyScope = payload.legacy_scope || null;
  if (state.alerts) {  // alert rules pick from the same scopes: a new one is offered now, not at the next alert poll
    state.alerts.saved_scopes = state.savedScopes.map((x) => x.name);
    state.alerts.scope_connections = Object.fromEntries(state.savedScopes.map((x) => [x.name, x.connections || {}]));
    if (state.view === "settings" && !state.alertEdit) renderAlertRules();
  }
}

function applyContext(context) {
  state.tenant = context.identity.tenant_id;
  state.tenantName = (context.tenants.find((t) => t.id === state.tenant) || {}).name || "";
  for (const a of document.querySelectorAll("a[data-portal]")) a.href = tenantPortal(a.dataset.portal);
  applyScopePayload(context);
  state.activeScope = null;
  state.configPath = context.config_path;
  state.watchlistPath = context.watchlist_path;
  state.version = context.version || "";
  if (!state.updateTimer) { checkUpdate(); state.updateTimer = setInterval(checkUpdate, UPDATE_CHECK_HOURS * 3600 * 1000); }
  $("about-version").textContent = context.version ? `version ${context.version}` : "";
  $("about-config").textContent = context.config_path || "";
  $("about-watchlist").textContent = context.watchlist_path || "";
  if (state.triageAutoOverride === undefined) state.triageAuto = context.triage_refresh_minutes || 0;
  state.summaryCfg = context.summary || {};
  $("identity").textContent = context.identity.name;
  $("account-who").textContent = context.identity.name;
  $("account-avatar").textContent = (context.identity.name || "?").trim().charAt(0).toUpperCase();
  $("account-tenant").textContent = state.tenantName ? `Tenant ${state.tenantName}` : "";
  $("tenant").innerHTML = context.tenants.map((t) =>
    `<option value="${esc(t.id)}" ${t.id === state.tenant ? "selected" : ""}>${esc(t.name)}</option>`).join("");
  if (state.signinAvailable) mergeCliTenants();
  $("depth").value = String(Math.min(4, Math.max(1, context.graph.depth || 1)));
  $("inbound").checked = !!context.graph.inbound;
  state.graphAuto = !!context.graph.auto;
}

function globMatch(patterns, value) {
  const lowered = (value || "").toLowerCase();
  return (patterns || []).some((pattern) => {
    const regex = new RegExp(`^${pattern.toLowerCase().replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`);
    return regex.test(lowered);
  });
}

// ------------------------------------------------------------------ views & links

function showView(view) {
  if (view === "vulnerabilities" || view === "exposure") {  // old links: both live under Security now
    state.secSub = view === "vulnerabilities" ? "vulnerabilities" : "config";
    view = "security";
  }
  const isTool = view.startsWith("tool-");
  if ((isTool && !toolKinds().includes(view.slice(5))) || (!isTool && !$(`view-${view}`))) view = "triage";  // e.g. an old ?view=checks link
  state.view = view;
  const section = view.startsWith("tool-") ? "monitors" : view;
  for (const name of ["triage", "investigate", "readiness", "browse", "secrets", "network", "servicenow", "monitors", "watchlist", "access", "settings", "security", "cost", "summary"]) {
    $(`view-${name}`).classList.toggle("hidden", name !== section);
  }
  document.querySelectorAll(".views button[data-view]").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
    b.setAttribute("aria-selected", b.dataset.view === view ? "true" : "false");
  });
  renderNavMenus();
  if (view === "access") { loadAccess(); if (state.accessSub === "needs") loadNeeds(); }
  if (view === "readiness") renderReadiness();
  if (view === "security") showSecSub(state.secSub);
  if (view === "cost") showCostSub(state.costSub);
  if (view === "summary" && state.summaryLoaded !== JSON.stringify(currentScope())) loadSummary();
  if (view === "triage") $("triage-auto").value = String(state.triageAuto || 0);
  if (view === "triage" && !state.triageLoaded) loadTriage();
  if (view === "secrets") { if (state.secretsLoaded !== JSON.stringify(currentScope())) loadSecrets(); else renderSecrets(); }
  if (view === "network" && !state.networkLoaded) loadNetwork();
  if (view === "servicenow" && !state.snLoaded) loadServiceNow();
  if (view.startsWith("tool-")) {
    state.toolKind = view.slice(5);
    if (state.monLoaded !== `${state.toolKind}|${state.selected[state.toolKind]}`) loadMonitors();
  }
  if (view === "investigate") loadInvestigate();
  if (view === "watchlist") loadWatchlist();
  if (view === "settings") loadSettings();
  navRecord(true);
  updateUrl();
}

// Back and Forward move through what was opened in this session (views, checks, tabs). The address stays the same,
// so a reload still opens Triage; going back to another resource checks it again.
let navRestoring = false;

function navEntry() {
  const browse = state.view === "browse";
  return { view: state.view, target: browse ? state.target || null : null,
    node: browse ? state.detailNode || null : null, tab: browse && state.detailNode ? state.detailTab : null };
}

function navRecord(push) {
  if (navRestoring) return;
  const entry = navEntry(), current = history.state;
  if (current && JSON.stringify(current) === JSON.stringify(entry)) return;
  if (push && current && current.view) history.pushState(entry, "", location.href);
  else history.replaceState(entry, "", location.href);
}

window.addEventListener("popstate", (event) => {
  const entry = event.state;
  if (!entry || !entry.view) return;
  navRestoring = true;
  try {
    if (entry.view === "browse" && entry.target && JSON.stringify(entry.target) !== JSON.stringify(state.target)) {
      state.pendingDetail = entry.node ? { node: entry.node, tab: entry.tab || "overview" } : null;
      startSnapshot(entry.target);  // switches to Browse at once; the check carries on by itself
    } else {
      if (entry.view !== state.view) showView(entry.view);
      if (entry.node && state.snapshot && state.snapshot.nodes.some((n) => n.id === entry.node)) showDetail(entry.node, entry.tab || "overview");
    }
  } finally {
    navRestoring = false;
  }
});

// The address bar keeps only the tenant, so a reload (or a restored tab) always opens on Triage;
// Copy link builds the full link to what's on screen.
// The address bar keeps the tenant only when it was chosen (the Tenant picker or a link); otherwise a reload follows
// whatever `az login` is signed in to now. Copied links always carry it.
function updateUrl() {
  const url = new URL(location.href);
  url.search = "";
  if (state.tenant && state.tenantPinned) url.searchParams.set("tenant", state.tenant);
  history.replaceState(history.state, "", url);
}

function viewLink() {
  const url = new URL(location.href);
  url.search = "";
  if (state.tenant) url.searchParams.set("tenant", state.tenant);
  if (state.view && state.view !== "browse") url.searchParams.set("view", state.view);
  if (state.view === "browse" && state.target) {
    if (state.target.kind === "group") url.searchParams.set("rg", state.target.group);
    else {
      url.searchParams.set("id", state.target.id);
      if (state.target.depth > 0) url.searchParams.set("depth", String(state.target.depth));
    }
    const root = state.snapshot && state.snapshot.root_id;
    if (state.detailNode && (state.target.kind === "group" || !sameId(state.detailNode, root))) url.searchParams.set("node", state.detailNode);
    if (state.detailNode && state.detailTab && state.detailTab !== "overview") url.searchParams.set("tab", state.detailTab);
  }
  return url.toString();
}

async function copyLink() {
  if (await copyText(viewLink())) {
    flash("Link copied. It opens this view for anyone running the console with access to it.", false, 3000);
  }
}

// ------------------------------------------------------------------ search

let searchToken = 0;
// Search covers every subscription you can read, so it finds what an incident or a colleague names; the scope in use
// ranks first and each result says whether it's in it. "only in scope" narrows to it.
function searchParams(text) {
  const scope = currentScope();
  const params = { q: text, ...(scope.subscriptions.length ? scopeParams(scope) : {}) };
  if ($("search-only-scope").checked && scope.subscriptions.length) params.only_scope = "1";
  return params;
}

function scopeChip(r) {
  if (r.in_scope === true) return '<span class="scope-chip in">in scope</span>';
  if (r.in_scope === false) return '<span class="scope-chip out">outside scope</span>';
  return "";
}

function renderSearchScopeToggle() {
  const none = !currentScope().subscriptions.length;
  $("search-only-scope").disabled = none;
  $("search-only-scope").parentElement.title = none
    ? "No scope applied yet: search covers every subscription you can read"
    : "Search every subscription you can read (in-scope results first), or only the scope in use";
}

const runSearch = debounce(async () => {
  const text = $("search").value.trim();
  const box = $("search-results");
  if (text.length < 2) { box.classList.add("hidden"); return; }
  const token = ++searchToken;
  box.classList.remove("hidden");
  const params = searchParams(text);
  box.innerHTML = loading(params.only_scope ? "Searching the scope in use…" : "Searching every subscription…");
  try {
    const results = await api("api/search", { params });
    if (token !== searchToken) return;
    state.searchResults = results;
    box.innerHTML = results.length ? results.map((r, i) => `
      <div class="result ${i === 0 ? "active" : ""}" role="option" data-index="${i}">
        <strong>${esc(r.name)}</strong> <span class="muted">${esc(r.display_type)}</span> ${scopeChip(r)}
        <span class="sub">${r.kind === "resource" ? esc(r.resource_group) + " · " : ""}${esc(subName(r.subscription_id))}</span>
        ${watchButton(r.kind === "resource group" ? groupWatchItem(r.subscription_id, r.name) : { id: r.id, name: r.name, kind: "resource" })}</div>`).join("")
      : params.only_scope ? '<div class="empty-result">Nothing by that name in the scope in use. Untick "only in scope" to search every subscription.</div>'
      : '<div class="empty-result">Nothing found by that name (only what your session can read is searched).</div>';
  } catch (error) {
    box.innerHTML = `<div class="empty-result">${esc(error.message)}</div>`;
  }
}, 300);

function openSearchResult(index) {
  const r = (state.searchResults || [])[index];
  if (!r) return;
  $("search-results").classList.add("hidden");
  $("search").value = "";
  if (r.kind === "resource group") startSnapshot({ kind: "group", group: `${r.subscription_id}/${r.name}`, name: r.name });
  else startSnapshot({ kind: "resource", id: r.id, name: r.name });
}

$("search").addEventListener("input", runSearch);
$("search").addEventListener("keydown", (event) => {
  const items = [...document.querySelectorAll("#search-results .result")];
  const active = items.findIndex((el) => el.classList.contains("active"));
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!items.length) return;
    const next = (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items.forEach((el, i) => el.classList.toggle("active", i === next));
    items[next].scrollIntoView({ block: "nearest" });
  } else if (event.key === "Enter") {
    event.preventDefault();
    openSearchResult(Math.max(active, 0));
  } else if (event.key === "Escape") {
    $("search-results").classList.add("hidden");
  }
});
$("search-results").addEventListener("click", (event) => {
  const item = event.target.closest(".result");
  if (item) openSearchResult(Number(item.dataset.index));
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".search")) $("search-results").classList.add("hidden");
});

