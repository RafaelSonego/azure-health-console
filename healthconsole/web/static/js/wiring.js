"use strict";

// ------------------------------------------------------------------- wiring

const reloadGroups = debounce(() => {
  if (state.groupsFor === subsKey(state.scope.subs)) return;  // Apply already loaded them
  loadGroups().then(draftChanged).catch((e) => showMessage(e.message, true));
}, 400);

// scope bar: dropdown panels
function togglePanel(id) {
  for (const other of ["subs-panel", "rgs-panel", "saved-panel"]) if (other !== id) $(other).classList.add("hidden");
  const panel = $(id);
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden")) {
    if (id === "rgs-panel") renderGroupPicks(); else if (id === "saved-panel") renderSavedScopes(); else renderSubscriptions();
    const search = panel.querySelector("input[type=search]");
    if (search) search.focus();
  }
}
$("subs-button").addEventListener("click", () => togglePanel("subs-panel"));
$("rgs-button").addEventListener("click", () => togglePanel("rgs-panel"));
document.addEventListener("click", (event) => {
  if (!event.target.closest(".dropdown")) for (const id of ["subs-panel", "rgs-panel", "saved-panel", "deps-panel", "export-panel"]) $(id).classList.add("hidden");
});

// Toolbar menus: Dependencies (its options) and Export (formats and the link). A pick closes the menu.
for (const [button, panel] of [["deps-menu", "deps-panel"], ["export-menu", "export-panel"]]) {
  $(button).addEventListener("click", () => {
    for (const other of ["deps-panel", "export-panel"]) if (other !== panel) $(other).classList.add("hidden");
    $(panel).classList.toggle("hidden");
  });
}
for (const id of ["export-md", "export-pdf", "print", "copy-link", "deps"]) {
  $(id).addEventListener("click", () => { $("export-panel").classList.add("hidden"); $("deps-panel").classList.add("hidden"); });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") for (const id of ["subs-panel", "rgs-panel"]) $(id).classList.add("hidden");
});

$("subs").addEventListener("change", (event) => {
  const id = event.target.dataset.id;
  if (event.target.checked) state.scope.subs.add(id); else state.scope.subs.delete(id);
  renderScopeBar();
  reloadGroups();
});
$("subs-all").addEventListener("click", () => {
  for (const s of shownSubscriptions()) state.scope.subs.add(s.id);
  renderSubscriptions();
  reloadGroups();
});
$("subs-none").addEventListener("click", () => {
  for (const s of shownSubscriptions()) state.scope.subs.delete(s.id);
  renderSubscriptions();
  reloadGroups();
});
$("sub-filter").addEventListener("input", renderSubscriptions);
$("graph").addEventListener("change", (event) => {
  if (event.target.id !== "graph-all" || !state.snapshot) return;
  state.graphAll = event.target.checked;
  drawGraph(state.snapshot);
});

$("rg-picks").addEventListener("change", (event) => {
  const key = event.target.dataset.key;
  const tagged = taggedGroups().map(groupKey);
  if (!state.scope.groups.size) state.scope.groups = new Set(tagged);  // unticking from "all": every other group stays
  if (event.target.checked) state.scope.groups.add(key); else state.scope.groups.delete(key);
  if (!state.scope.groups.size) {  // an empty pick list means "all", the opposite of unticking the last one
    state.scope.groups.add(key);
    flash("At least one resource group stays in the scope; to look at none, clear the subscriptions instead.", true, 4000);
  } else if (tagged.every((k) => state.scope.groups.has(k))) state.scope.groups.clear();  // everything ticked is "all" again
  renderGroupPicks();
  draftChanged();
});
$("rgs-all").addEventListener("click", () => {
  state.scope.groups.clear();
  renderGroupPicks();
  draftChanged();
});
$("rgs-shown").addEventListener("click", () => {
  const filter = $("rg-pick-filter").value.toLowerCase();
  state.scope.groups = new Set(taggedGroups().filter((g) => !filter || g.name.toLowerCase().includes(filter)).map(groupKey));
  renderGroupPicks();
  draftChanged();
});
$("rg-pick-filter").addEventListener("input", renderGroupPicks);

$("scope-tags").addEventListener("change", (event) => {
  if (event.target.id === "tag-key") {
    state.pendingTagKey = event.target.value;
    renderTagFilters();
    return;
  }
  if (event.target.id === "tag-value" && event.target.value) {
    state.scope.tags[state.pendingTagKey] = event.target.value;
    state.pendingTagKey = "";
    draftChanged();
  }
});
$("scope-tags").addEventListener("click", (event) => {
  if (event.target.id === "scope-clear") {
    state.scope.tags = {};
    state.scope.groups = new Set();
    state.pendingTagKey = "";
    renderGroupPicks();
    draftChanged();
    return;
  }
  const key = event.target.dataset.remove;
  if (!key) return;
  delete state.scope.tags[key];
  draftChanged();
});

// browse: resource groups in scope, then the resources of the one you open
$("rg-filter").addEventListener("input", () => { renderGroups(); findInTree(); });
$("rgs").addEventListener("keydown", (event) => {
  const head = (event.key === "Enter" || event.key === " ") && event.target.closest("[data-type-toggle]");
  if (head) { event.preventDefault(); foldType(head.dataset.foldKey, head.dataset.typeToggle); }
});
$("rgs").addEventListener("click", (event) => {
  if (event.target.closest("[data-watch]")) return;
  const fav = event.target.closest("[data-fav]");
  if (fav) { toggleFavourite(JSON.parse(fav.dataset.fav)); return; }
  if (event.target.closest("[data-fav-toggle]")) { state.favOpen = !state.favOpen; renderGroups(); return; }
  const more = event.target.closest("[data-more]");
  if (more) { showMore(more); renderGroups(); return; }
  const page = event.target.closest("[data-sub-summary]");
  if (page) { showSubscriptionSummary(page.dataset.subSummary); return; }
  const sub = event.target.closest("[data-sub-toggle]");
  if (sub) {
    state.subOpen[sub.dataset.subToggle] = sub.getAttribute("aria-expanded") !== "true";
    renderGroups();
    return;
  }
  const res = event.target.closest(".item.res");
  if (res) { startSnapshot(res.dataset.name ? { kind: "resource", id: res.dataset.id, name: res.dataset.name } : res.dataset.id); return; }
  const typeHead = event.target.closest("[data-type-toggle]");
  if (typeHead) { foldType(typeHead.dataset.foldKey, typeHead.dataset.typeToggle); return; }
  const typeAll = event.target.closest("[data-type-all]");
  if (typeAll) { foldType(typeAll.dataset.foldKey, null, typeAll.dataset.typeAll); return; }
  const typeSort = event.target.closest("[data-type-sort]");
  if (typeSort) { state.typeSort = typeSort.dataset.typeSort; stored.set("browse.typeSort", state.typeSort); renderGroups(); return; }
  const fold = event.target.closest("[data-find-fold]");
  if (fold && state.treeFind && state.treeFind.byGroup) {
    state.findClosed = fold.dataset.findFold === "close"
      ? new Set(appliedGroups().filter((g) => state.treeFind.byGroup.has(groupKey(g).toLowerCase())).map(groupKey)) : new Set();
    renderGroups();
    return;
  }
  const item = event.target.closest(".item.grp");
  if (!item) return;
  const key = item.dataset.key;
  const found = state.treeFind && state.treeFind.byGroup && $("rg-filter").value.trim() ? state.treeFind.byGroup.get(key.toLowerCase()) : null;
  if (found && event.target.closest("[data-toggle]")) {  // a filter match: the arrow folds its matches, like a subscription
    if (state.findClosed.has(key)) state.findClosed.delete(key); else state.findClosed.add(key);
    renderGroups();
    return;
  }
  if (event.target.closest("[data-toggle]") && state.browseGroup === key) {  // the arrow closes an open group
    state.browseGroup = null;
    state.resources = [];
    renderGroups();
    return;
  }
  openGroup(key);
});

// Each group's last finished check, so going back to the group shows it again until that group is checked anew.
const GROUP_CHECKS_KEPT = 10;
state.groupChecks = new Map();

function keepGroupCheck(target, snapshot) {
  const key = target.group.toLowerCase();
  state.groupChecks.delete(key);
  state.groupChecks.set(key, { target: { ...target }, snapshot });
  while (state.groupChecks.size > GROUP_CHECKS_KEPT) state.groupChecks.delete(state.groupChecks.keys().next().value);
}

function showGroupCheck(key) {
  const kept = state.groupChecks.get(key.toLowerCase());
  hideGroupSummary();
  state.target = kept.target;
  state.snapshot = kept.snapshot;
  state.selectedResource = null;
  state.exploreNode = null;
  state.partialDrawn = false;
  renderPlace(state.target);
  renderSnapshot();
  for (const id of ["export-md", "export-pdf", "print", "watch", "copy-link", "export-menu"]) $(id).disabled = false;
  $("rerun").disabled = false;
  updateBrowseControls();
  refreshWatchButtons();
}

// A group picked on the left (or from a Cost card): its last check if it has one, else its summary; and its
// resources listed under it.
function openGroup(key) {
  if (state.groupChecks.has(key.toLowerCase()) && !state.busy) showGroupCheck(key);
  else showGroupSummary(key);
  if (state.browseGroup !== key) {
    state.browseGroup = key;
    loadResources().catch((e) => showMessage(e.message, true));
  } else renderGroups();
}

const GS_TYPES = 8;  // resource types the group summary shows as tiles before "other"

// A group picked on the left: what's in it, and Check this group. A check, or opening a resource, replaces it.
function showGroupSummary(key) {
  state.groupSummary = key;
  document.querySelector("#view-browse main").classList.add("showing-group");
  renderGroupSummary();
}

function hideGroupSummary() {
  state.groupSummary = null;
  document.querySelector("#view-browse main").classList.remove("showing-group");
  $("group-summary").classList.add("hidden");
}

function renderGroupSummary() {
  const key = state.groupSummary;
  const group = appliedGroups().find((g) => groupKey(g) === key);
  if (!group) { hideGroupSummary(); return; }
  const loaded = state.browseGroup === key && !state.resourcesLoading;
  // The group's workloads first, by role (compute, hosting, data…: the diagram's bands), then by count; plumbing
  // (network interfaces, private endpoints, system topics…) in one tile at the end.
  const byType = {};
  if (loaded) for (const r of state.resources) {
    const t = byType[r.display_type] || (byType[r.display_type] = { n: 0, rank: r.tier_rank ?? 99, plumbing: !!r.plumbing });
    t.n += 1;
  }
  const main = Object.entries(byType).filter(([, t]) => !t.plumbing)
    .sort((a, b) => a[1].rank - b[1].rank || b[1].n - a[1].n || byName(a[0], b[0])).map(([name, t]) => [name, t.n]);
  const plumbing = Object.entries(byType).filter(([, t]) => t.plumbing).sort((a, b) => b[1].n - a[1].n);
  const shown = main.slice(0, GS_TYPES);
  const others = main.slice(GS_TYPES);
  // Each tile opens just its types in the tree on the left.
  const tile = (n, label, types, title = "", extra = "") => `<button type="button" class="gs-type${extra}" data-show-types="${esc(JSON.stringify(types))}"
    title="${esc(title ? `${title}. Show them on the left` : "Show them on the left")}"><span class="n">${n}</span><span class="muted">${esc(label)}</span></button>`;
  const listed = (rows) => rows.map(([name, n]) => `${n} ${name}`).join(", ");
  const tiles = shown.map(([t, n]) => tile(n, t, [t])).join("")
    + (others.length ? tile(others.reduce((s, [, n]) => s + n, 0), "other", others.map(([t]) => t), listed(others)) : "")
    + (plumbing.length ? tile(plumbing.reduce((s, [, t]) => s + t.n, 0), "supporting (network, endpoints, topics…)",
      plumbing.map(([t]) => t), listed(plumbing.map(([name, t]) => [name, t.n])), " gs-plumbing") : "");
  const kept = state.groupChecks.get(key.toLowerCase());
  const last = kept ? kept.snapshot : state.snapshot && state.snapshot.kind === "group"
    && sameId(state.snapshot.root_id, `/subscriptions/${key.split("/")[0]}/resourcegroups/${group.name}`) ? state.snapshot : null;
  $("group-summary").classList.remove("hidden");
  $("group-summary").innerHTML = `<div class="crumbs"><button type="button" class="link" data-sub-summary="${esc(group.subscription_id)}">${esc(subName(group.subscription_id))}</button> › <strong>${esc(group.name)}</strong></div>
    <div class="gs-head"><h2>${esc(group.name)}</h2>
      <span class="muted">Resource group · ${esc(subName(group.subscription_id))}${loaded ? ` · ${state.resources.length} resources` : ""}${last ? ` · last checked ${escTimes(last.taken_at)}` : " · not checked yet"}</span>
      <span class="spacer"></span>
      ${kept ? `<button type="button" class="primary" data-show-check="${esc(key)}" title="Back to the check taken ${esc(kept.snapshot.taken_at)}, without checking again">Show last check</button>` : ""}
      <button type="button" ${kept ? "" : 'class="primary"'} data-check-group="${esc(group.subscription_id)}/${esc(group.name)}">${kept ? "Check again" : "Check this group"}</button>
      ${watchButton(groupWatchItem(group.subscription_id, group.name))}</div>
    ${loaded ? `<div class="gs-types">${tiles}</div>` : loading("Loading resources…")}
    <div class="gs-cards">${costSlot(`/subscriptions/${group.subscription_id}/resourceGroups/${group.name}`)}${accessHereSlot(`/subscriptions/${group.subscription_id}/resourceGroups/${group.name}`)}
    <div class="card gs-explain"><h3>What "Check this group" does</h3>
      <p class="muted">Reads every resource's health, then lists them worst first, with what changed against last week above
        the table. Pick a resource on the left to look at just that one instead.</p></div></div>`;
  fillAccessHere($("group-summary"));
  fillCost($("group-summary"));
}

// A subscription, from the crumb above a group: its groups in scope, and your access there.
function showSubscriptionSummary(subId) {
  state.groupSummary = null;
  document.querySelector("#view-browse main").classList.add("showing-group");
  const groups = appliedGroups().filter((g) => g.subscription_id === subId);
  $("group-summary").classList.remove("hidden");
  $("group-summary").innerHTML = `<div class="crumbs"><strong>${esc(subName(subId))}</strong></div>
    <div class="gs-head"><h2>${esc(subName(subId))}</h2>
      <span class="muted">Subscription · ${esc(subId)} · ${groups.length} resource group(s) in scope</span></div>
    <div class="gs-cards">${costSlot(`/subscriptions/${subId}`)}${accessHereSlot(`/subscriptions/${subId}`)}
      <div class="card gs-explain"><h3>Its resource groups</h3><p class="muted">Pick one on the left to see what's in it and check it.</p></div></div>`;
  fillAccessHere($("group-summary"));
  fillCost($("group-summary"));
}

$("group-summary").addEventListener("click", (event) => {
  if (costCardClick(event)) return;
  const open = event.target.closest("[data-open]");
  if (open) { startSnapshot(JSON.parse(open.dataset.open)); return; }
  const sub = event.target.closest("[data-sub-summary]");
  if (sub) { showSubscriptionSummary(sub.dataset.subSummary); return; }
  const types = event.target.closest("[data-show-types]");
  if (types && state.groupSummary) { showTypesInTree(`res:${state.groupSummary}`, JSON.parse(types.dataset.showTypes)); return; }
  const back = event.target.closest("[data-show-check]");
  if (back && state.groupChecks.has(back.dataset.showCheck.toLowerCase())) { showGroupCheck(back.dataset.showCheck); return; }
  const b = event.target.closest("[data-check-group]");
  if (!b) return;
  const group = b.dataset.checkGroup;
  startSnapshot({ kind: "group", group, name: group.split("/").slice(1).join("/") });
});
$("rerun").addEventListener("click", () => state.target && freshly(() => startSnapshot(state.target)));
$("deps").addEventListener("click", () => {
  const t = state.target;
  if (!t || t.kind === "group") return;
  startSnapshot(t.depth > 0 ? { ...t, depth: 0, inbound: false }
    : { ...t, depth: Number($("depth").value), inbound: $("inbound").checked });
});
$("group-view").addEventListener("click", (event) => {
  const view = event.target.dataset && event.target.dataset.groupview;
  if (!view || view === state.groupView) return;
  state.groupView = view;
  state.partialDrawn = false;
  if (state.snapshot && state.snapshot.kind === "group") {
    if (view === "table") renderGroupTable(state.snapshot);
    else { $("graph").classList.remove("as-table"); drawGraph(state.snapshot); state.partialDrawn = !state.snapshot.done; }
  }
  updateBrowseControls();
});
$("export-md").addEventListener("click", () => exportSnapshot("md"));
$("export-pdf").addEventListener("click", () => exportSnapshot("pdf"));
$("print").addEventListener("click", async () => {
  const tab = window.open("", "_blank");  // opened now, while it's still the click, or the browser blocks it
  try {
    const response = await api(`api/snapshots/${state.jobId}/print`, { raw: true });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || `Print failed (${response.status}).`);
    tab.location = URL.createObjectURL(new Blob([await response.text()], { type: "text/html" }));
  } catch (error) {
    tab.close();
    showMessage(error.message, true);
  }
});
$("tenant").addEventListener("change", async (event) => {
  state.tenant = event.target.value;
  state.tenantPinned = true;
  state.browseGroup = null;
  state.selectedResource = null;
  state.triageLoaded = false;
  state.watchResults = {};
  state.alerts = null;
  state.alertEdit = null;
  state.snLoaded = false;
  state.monLoaded = false;
  state.sn = null;
  state.mon = null;
  try {
    await loadContextForTenant();
    await refreshIntegrationTabs();  // each tenant has its own ServiceNow and monitoring tools
    if (state.view === "settings") loadIntegrations();
    if (state.view === "servicenow") loadServiceNow();
    if ((state.view || "").startsWith("tool-")) loadMonitors();
    state.watchItems = await api("api/watchlist");
    refreshWatchButtons();
    if (state.view === "watchlist") renderWatchlist();
    pollAlerts();
    if (state.view === "triage") await loadTriage();
    updateUrl();
  } catch (error) { showMessage(error.message, true); }
});
$("clear-cache").addEventListener("click", async () => {
  if (!window.confirm("Forget the costs kept on this machine and every answer kept in memory? The next cost page reads Azure again (a big scope takes minutes).")) return;
  try {
    await api("api/cache/clear", { method: "POST", body: {} });
    flash("Cached data cleared.");
  } catch (error) { showMessage(error.message, true); }
});
$("refresh").addEventListener("click", async () => {
  try {
    await api("api/refresh", { method: "POST", body: {} });
    const kept = state.applied, active = state.activeScope;
    if (!kept) await loadContextForTenant();
    else {  // keep the scope in use: Open with is for opening the page, not for every refresh
      applyContext(await api("api/context"));
      state.activeScope = active;
      state.subscriptions = await api("api/subscriptions");
      const known = new Set(state.subscriptions.map((s) => s.id));
      Object.assign(state.scope, { subs: new Set([...kept.subs].filter((id) => known.has(id))),
        groups: new Set(kept.groupSel), tags: { ...kept.tags } });
      renderSubscriptions();
      await loadGroups();
      applyScope();
    }
    // New access can change every page: mark them all stale, then reload only the one on screen.
    Object.assign(state, { triageLoaded: false, networkLoaded: false, snLoaded: false, monLoaded: false,
      summaryLoaded: "", secretsLoaded: "", vulnLoaded: "", auditLoaded: "", costLoaded: "", wasteLoaded: "", groupsLoaded: false });
    if (state.view === "browse" && state.target) await startSnapshot(state.target);
    else showView(state.view);
    flash("Access refreshed: cached tokens and results dropped.");
  } catch (error) { showMessage(error.message, true); }
});

async function loadContextForTenant() {
  const context = await api("api/context");
  applyContext(context);
  await loadSubscriptions();
}

