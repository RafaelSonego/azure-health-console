"use strict";

// ------------------------------------------------------------------ scope
// One scope for every page: subscriptions, optionally chosen resource groups, and tag filters.

async function loadContext() {
  $("scope-summary").innerHTML = loading("Reading your Azure CLI session…");
  const context = await api("api/context", { params: { verify: "1" } });
  applyContext(context);
  showMessage("");
  await loadSubscriptions();
}

async function loadSubscriptions() {
  state.subscriptions = await api("api/subscriptions");
  // triage.scope: "none" opens with nothing picked, so nothing is queried until you choose.
  // Open with: nothing (no queries until you pick), every subscription, or a saved scope.
  const named = !["none", "all"].includes(state.openWith) ? findSavedScope(state.openWith) : null;
  state.openWithMissing = !["none", "all"].includes(state.openWith) && !named ? state.openWith : "";
  state.scope.groups.clear();
  state.scope.tags = {};
  if (named) await applySavedScope(named);
  if (state.applied) return;  // the saved scope applied (one whose subscriptions all vanished applies nothing)
  state.scope.subs = new Set(state.openWith === "all" ? state.subscriptions.map((s) => s.id) : []);
  renderSubscriptions();
  await loadGroups();
  applyScope();
}

const findSavedScope = (name) => (state.savedScopes || []).find((s) => s.name.toLowerCase() === String(name || "").toLowerCase());

function groupKey(group) {
  return `${group.subscription_id}/${group.name.toLowerCase()}`;
}

function subName(id) {
  const sub = state.subscriptions.find((s) => s.id === id);
  return sub ? sub.name : id;
}

const UNTAGGED = "__untagged__";

function matchesTags(group, filters = state.scope.tags) {
  return Object.entries(filters).every(([key, wanted]) => {
    const value = group.tags[key];
    return wanted === UNTAGGED ? !value : value === wanted;
  });
}

function taggedGroups() {
  return state.groups.filter((g) => matchesTags(g));
}

function groupsInScope() {
  const tagged = taggedGroups();
  return state.scope.groups.size ? tagged.filter((g) => state.scope.groups.has(groupKey(g))) : tagged;
}

// The menus edit a draft (state.scope); every tab reads the applied scope, which only Apply changes.
function appliedGroups() {
  return state.applied ? state.applied.groupRows : [];
}

function currentScope() {
  const a = state.applied;
  if (!a) return { subscriptions: [], groups: null, label: "the subscriptions in scope" };
  return {
    subscriptions: [...a.subs],
    groups: a.narrowed ? a.groupRows.map(groupKey) : null,
    label: a.narrowed ? "the resource groups in scope" : "the subscriptions in scope",
  };
}

const sortedEntries = (obj) => JSON.stringify(Object.entries(obj || {}).sort());

function scopeDiff() {
  const a = state.applied;
  if (!a) return "";
  const parts = [];
  const added = [...state.scope.subs].filter((x) => !a.subs.has(x)).length;
  const removed = [...a.subs].filter((x) => !state.scope.subs.has(x)).length;
  if (added || removed) parts.push(`subscriptions ${[added && `+${added}`, removed && `−${removed}`].filter(Boolean).join(" ")}`);
  if (!sameSet(state.scope.groups, a.groupSel)) parts.push("resource groups");
  if (sortedEntries(state.scope.tags) !== sortedEntries(a.tags)) parts.push("tag filters");
  if (sortedEntries(state.connDraft) !== sortedEntries(state.selected)) parts.push("connections");
  return parts.length ? `Not applied yet: ${parts.join(", ")}` : "";
}

function draftChanged() {
  renderScopeBar();
}

$("search-only-scope").addEventListener("change", () => {
  stored.set("ahc.searchOnlyScope", $("search-only-scope").checked ? "1" : "");
  if ($("search").value.trim().length >= 2) runSearch();
});
$("search-only-scope").checked = stored.get("ahc.searchOnlyScope") === "1";

function applyScope() {
  const before = JSON.stringify(currentScope());
  const first = !state.applied;
  state.applied = { subs: new Set(state.scope.subs), groupSel: new Set(state.scope.groups), tags: { ...state.scope.tags },
    groupRows: groupsInScope(), allGroups: state.groups,
    narrowed: state.scope.groups.size > 0 || Object.keys(state.scope.tags).length > 0 };
  const connsMoved = sortedEntries(state.connDraft) !== sortedEntries(state.selected);
  state.selected = { ...state.connDraft };
  if (connsMoved) rememberConnections();
  for (const id of ["subs-panel", "rgs-panel", "saved-panel"]) $(id).classList.add("hidden");
  renderScopeBar();
  renderGroups();
  renderSearchScopeToggle();
  const scopeMoved = first || JSON.stringify(currentScope()) !== before;
  if (scopeMoved) {
    if (state.browseGroup && !appliedGroups().some((g) => groupKey(g) === state.browseGroup)) {
      state.browseGroup = null;
      state.resources = [];
      renderResources();
    }
    reloadAfterScope();
    if (state.target) followTarget(state.target).catch((e) => showMessage(e.message, true));
  }
  if (connsMoved && !first) connectionsChanged(scopeMoved ? "detail-only" : null);
}

function discardScope() {
  const a = state.applied;
  if (!a) return;
  state.scope.subs = new Set(a.subs);
  state.scope.groups = new Set(a.groupSel);
  state.scope.tags = { ...a.tags };
  state.groups = a.allGroups;
  state.groupsFor = subsKey(a.subs);
  state.connDraft = { ...state.selected };
  renderSubscriptions();
  renderGroupPicks();
  renderConnectionPickers();
  renderScopeBar();
}

function scopeParams(scope) {
  const params = { subscription: scope.subscriptions };
  if (scope.groups) { params.group = scope.groups; params.filtered = "1"; }
  return params;
}

// state.groupsFor names the subscriptions state.groups belongs to; a slower, older load never overwrites a newer one.
const subsKey = (subs) => [...subs].sort().join(",");
let groupsToken = 0;

async function loadGroups() {
  const subs = [...state.scope.subs];
  const token = ++groupsToken;
  let groups = [];
  if (subs.length) {
    $("rg-picks").innerHTML = loading("Loading resource groups…");  // the draft's picker: Browse shows the applied scope
    groups = await api("api/resource-groups", { params: { subscription: subs } });
  }
  if (token !== groupsToken) return;
  state.groups = groups;
  state.groupsFor = subsKey(subs);
  const known = new Set(state.groups.map(groupKey));
  for (const key of [...state.scope.groups]) if (!known.has(key)) state.scope.groups.delete(key);
  renderGroupPicks();
}

// Everything that depends on scope re-renders from here.
const reloadAfterScope = debounce(() => {
  // Only when the scope really changed: the page's first load already ran Triage for this scope.
  if (JSON.stringify(currentScope()) !== state.triageScopeKey) {
    state.triageLoaded = false;
    if (state.view === "triage") loadTriage();
  }
  state.networkLoaded = false;
  if (state.view === "network") loadNetwork();
  state.treeFind = null;
  if ($("rg-filter").value.trim()) findInTree();  // the filter's matches are for the scope in use
  if (state.view === "security") showSecSub(state.secSub);
  if (state.view === "cost") showCostSub(state.costSub);
  state.summaryLoaded = "";
  if (state.view === "summary") loadSummary();
  if (state.view === "investigate") loadInvestigate();  // it compares the scope itself: a new one lists its components
  if (state.view === "access") loadAccess();
  state.snLoaded = false;
  if (state.view === "servicenow") loadServiceNow();
  state.monLoaded = false;
  if ((state.view || "").startsWith("tool-")) loadMonitors();
  state.secretsLoaded = "";  // one read per vault: only where it's opened, never behind every Apply
  if (state.view === "secrets") loadSecrets();
}, 500);

// Browse's left side follows what's open on the right. A target outside the scope is named, never added to it:
// the scope also drives ServiceNow, the monitoring tabs and Triage.
function targetPlace(target) {
  if (!target) return null;
  if (target.kind === "group") {
    const [sub, ...rest] = (target.group || "").split("/");
    return sub && rest.length ? { sub: sub.toLowerCase(), rg: rest.join("/") } : null;
  }
  const parts = (target.id || "").split("/");
  return parts.length > 4 && parts[1].toLowerCase() === "subscriptions" ? { sub: parts[2].toLowerCase(), rg: parts[4] } : null;
}

const placeKey = (place) => `${place.sub}/${place.rg.toLowerCase()}`;

function scopeGroupAt(place) {
  return appliedGroups().find((g) => groupKey(g).toLowerCase() === placeKey(place));
}

async function openGroupInList(key) {
  if (state.browseGroup !== key) {
    state.browseGroup = key;
    renderGroups();
    await loadResources();
  } else renderGroups();
  for (const id of ["rgs", "resources"]) {
    const el = $(id).querySelector(".item.open, .item.active");
    if (el) el.scrollIntoView({ block: "nearest" });
  }
}

async function followTarget(target) {
  const place = targetPlace(target);
  const wasShown = state.outside && place && placeKey(state.outside) === placeKey(place) && state.outside.shown;
  state.outside = null;
  if (place && state.applied) {  // no scope applied yet: decided once it is (applyScope → followTarget)
    const group = scopeGroupAt(place);
    if (group) await openGroupInList(groupKey(group));
    else state.outside = { ...place, name: target.name, shown: !!wasShown };
  }
  renderPlace(target);
}

function renderPlace(target = state.target) {
  const place = targetPlace(target);
  const sub = place ? esc(subName(place.sub)) : "";
  $("crumbs").classList.toggle("hidden", !place);
  const subId = place && (appliedGroups().find((g) => g.subscription_id.toLowerCase() === place.sub) || {}).subscription_id;
  const subHtml = subId ? `<button type="button" class="link" data-crumb-sub="${esc(subId)}"
      title="This subscription's page: its groups in scope, costs and your access">${sub}</button>` : `<span>${sub}</span>`;
  $("crumbs").innerHTML = place ? `${subHtml} › <button type="button" class="link" data-crumb-group
      title="This resource group's page, with its resources listed on the left">${esc(place.rg)}</button>${target.kind === "resource"
      ? ` › <strong>${esc(target.name)}</strong>` : ""}${state.outside ? ' <span class="muted">(outside the current scope)</span>' : ""}` : "";
  const out = state.outside;
  $("browse-outside").classList.toggle("hidden", !out);
  $("browse-outside").innerHTML = out ? `ⓘ <strong>${esc(out.name)}</strong> is outside the current scope
      (${sub} · ${esc(out.rg)}).${out.shown ? ` The resources below are from ${esc(out.rg)}.` : ""}
      <div class="outside-actions">${out.shown ? "" : '<button type="button" data-outside="show">Show its resource group</button>'}
      <button type="button" class="link" data-outside="back">Back to scope</button></div>` : "";
}

async function showOutsideGroup() {
  state.outside.shown = true;
  state.browseGroup = placeKey(state.outside);
  renderGroups();
  renderPlace();
  await loadResources();
  const el = $("resources").querySelector(".item.active");
  if (el) el.scrollIntoView({ block: "nearest" });
}

async function backToScope() {
  state.outside = null;
  if (state.browseGroup && !appliedGroups().some((g) => groupKey(g).toLowerCase() === state.browseGroup.toLowerCase())) {
    state.browseGroup = null;
    await loadResources();
  }
  renderGroups();
  renderPlace();
}

$("browse-outside").addEventListener("click", (event) => {
  const action = event.target.dataset && event.target.dataset.outside;
  const run = action === "show" ? showOutsideGroup : action === "back" ? backToScope : null;
  if (run) run().catch((e) => showMessage(e.message, true));
});
$("crumbs").addEventListener("click", (event) => {
  const sub = event.target.closest("[data-crumb-sub]");
  if (sub) { showSubscriptionSummary(sub.dataset.crumbSub); return; }
  if (!event.target.closest("[data-crumb-group]")) return;
  const place = targetPlace(state.target);
  const group = place && scopeGroupAt(place);
  // In scope: the group's page, as a click on it in the list opens; outside it, its resources on the left.
  if (group) openGroup(groupKey(group));
  else if (state.outside) showOutsideGroup().catch((e) => showMessage(e.message, true));
});

function sameSet(a, b) {
  return a.size === b.size && [...a].every((x) => b.has(x));
}

// "a, b +3": the first few names of a list, for a button or a summary.
function namesText(names, max = 2) {
  const sorted = [...names].sort(byName);
  return sorted.length > max ? `${sorted.slice(0, max).join(", ")} +${sorted.length - max}` : sorted.join(", ");
}

function renderScopeBar() {
  const subs = state.scope.subs;
  const subNames = [...subs].map(subName);
  let label;
  if (subs.size === state.subscriptions.length && subs.size) label = `All (${subs.size})`;
  else label = subs.size ? namesText(subNames) : "None picked";
  $("subs-button").textContent = label;
  $("subs-button").title = subNames.length ? `Subscriptions in the scope:\n${[...subNames].sort(byName).join("\n")}` : "Pick subscriptions";
  const tagged = taggedGroups().length;
  const filtered = Object.keys(state.scope.tags).length > 0;
  const picked = state.groups.filter((g) => state.scope.groups.has(groupKey(g)));
  const pickedNames = [...new Set(picked.map((g) => g.name))];  // one name often repeats across environments' subscriptions
  const spread = new Set(picked.map((g) => g.subscription_id)).size;
  $("rgs-button").textContent = picked.length ? `${namesText(pickedNames)}${spread > 1 ? ` (in ${spread} subscriptions)` : ""}`
    : filtered ? `All ${tagged} with these tags` : `All (${tagged})`;
  $("rgs-button").title = picked.length ? `Resource groups in the scope:\n${picked.map((g) => `${g.name}  (${subName(g.subscription_id)})`)
    .sort(byName).join("\n")}` : "";
  renderTagFilters();
  const a = state.applied;
  const pending = scopeDiff();
  $("scope-pending").classList.toggle("hidden", !pending);
  $("scope-pending-text").textContent = pending;
  $("scope-summary").textContent = a ? `In use: ${a.subs.size} subscription(s) · ${a.groupRows.length} resource group(s)` : "";
  $("scope-summary").classList.toggle("hidden", !!pending);
  renderSavedButton();
  $("rg-count").textContent = `(${appliedGroups().length})`;
}

// A picker lists what's in the scope in use first, so a saved scope's items are visible without scrolling. It follows the
// applied scope, not the ticks, so rows don't move while you tick them.
function inUseFirst(rows, inUse, render) {
  const first = rows.filter(inUse);
  if (!first.length || first.length === rows.length) return rows.map(render).join("");
  return `<div class="pick-head">In use (${first.length})</div>${first.map(render).join("")}
    <div class="pick-head">Others (${rows.length - first.length})</div>${rows.filter((r) => !inUse(r)).map(render).join("")}`;
}

// The subscriptions the picker's filter shows; All and Clear act on these alone while a filter is typed.
function shownSubscriptions() {
  const filter = $("sub-filter").value.toLowerCase();
  return state.subscriptions.filter((s) => !filter || s.name.toLowerCase().includes(filter) || s.id.includes(filter));
}

function renderSubscriptions() {
  const rows = shownSubscriptions();
  const filtered = rows.length < state.subscriptions.length;
  $("subs-all").textContent = filtered ? `All shown (${rows.length})` : "All";
  $("subs-none").textContent = filtered ? "Clear shown" : "Clear";
  const applied = state.applied ? state.applied.subs : new Set();
  $("subs").innerHTML = inUseFirst(rows, (s) => applied.has(s.id), (s) => `<div class="sub-row">
      <label><input type="checkbox" data-id="${esc(s.id)}" ${state.scope.subs.has(s.id) ? "checked" : ""}>
        <span>${esc(s.name)}</span>
        ${s.state !== "Enabled" ? `<span class="sub">${esc(s.state)}</span>` : ""}</label></div>`)
    || '<p class="muted">No subscriptions match.</p>';
}

const byName = (a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });

// Resource Graph returns groups in no order.
function sortedGroups(groups) {
  return [...groups].sort((a, b) => byName(a.name, b.name));
}

// Lists drawn as divs (Browse) page like tables do: LIST_PAGE at a time, then "show more" (data-more, read by the list's click handler).
const listShown = {};

// How many rows of `key` to draw, grown by pages (`page` rows each) so the row at `keep` (the open or selected one) is drawn.
function shownLimit(key, keep = -1, page = LIST_PAGE) {
  if (keep >= (listShown[key] || page)) listShown[key] = Math.ceil((keep + 1) / page) * page;
  return listShown[key] || page;
}

function pagedHtml(key, rows, render, keep = -1, page = LIST_PAGE) {
  const limit = shownLimit(key, keep, page);
  return rows.slice(0, limit).map(render).join("") + moreHtml(key, limit, rows.length, page);
}

// A scrolling list box with its "show more" line under it, where it's seen without scrolling to the end.
function starListHtml(key, rows, render, empty) {
  const limit = shownLimit(key, -1, LIST_PAGE);
  if (!rows.length) return `<div class="star-list"><p class="muted tree-note">${empty}</p></div>`;
  return `<div class="star-list">${rows.slice(0, limit).map(render).join("")}</div>${moreHtml(key, limit, rows.length)}`;
}

function moreHtml(key, limit, total, page = LIST_PAGE) {
  return total > limit ? `<div class="list-more muted">Showing ${limit.toLocaleString()} of ${total.toLocaleString()} ·
    <button type="button" class="link" data-more="${esc(key)}" data-page="${page}">show ${Math.min(total - limit, page).toLocaleString()} more</button> ·
    <button type="button" class="link" data-more="${esc(key)}" data-more-all>show all</button></div>` : "";
}

function showMore(button) {
  const key = button.dataset.more, page = Number(button.dataset.page) || LIST_PAGE;
  listShown[key] = "moreAll" in button.dataset ? Infinity : (listShown[key] || page) + page;
}

// Rows under one heading per subscription (management groups and the tenant root last), A–Z, each heading a sortable
// table: the first few open, all of them while filtering; ten headings at a time, then "show more".
const SCOPE_GROUPS_OPEN = 3;
const SCOPE_GROUPS_PAGE = 10;

function scopeGroupOf(scope) {
  const s = (scope || "").toLowerCase();
  const sub = s.match(/^\/subscriptions\/([^/]+)/);
  if (sub) return { key: sub[1], label: subName(sub[1]), rank: 0 };
  return s.includes("/managementgroups/") ? { key: "~mg", label: "Management groups", rank: 1 } : { key: "~root", label: "Tenant root", rank: 2 };
}

// Rows folded under headings: `groupOf(row)` gives {key, label, rank}; headings sort by `order` (rank, then A–Z), each
// drawn by `body(rows)` with `note(rows)` beside its count. One heading draws just its body.
function groupedHtml(key, rows, { groupOf, body, note = () => "", order = (a, b) => a.rank - b.rank || byName(a.label, b.label),
  filtering = false }) {
  const groups = new Map();
  for (const row of rows) {
    const g = groupOf(row);
    if (!groups.has(g.key)) groups.set(g.key, { ...g, rows: [] });
    groups.get(g.key).rows.push(row);
  }
  const sorted = [...groups.values()].sort(order);
  if (sorted.length === 1) return body(sorted[0].rows);
  return pagedHtml(`${key}-groups`, sorted, (g, i) => `<details class="scope-group" ${filtering || i < SCOPE_GROUPS_OPEN ? "open" : ""}>
    <summary><strong>${esc(g.label)}</strong> <span class="muted">${g.rows.length}</span>${note(g.rows)}</summary>${body(g.rows)}</details>`,
  -1, SCOPE_GROUPS_PAGE);
}

// `most`: headings with the most rows first; otherwise subscriptions by name, then management groups and the root.
function scopeGroupsHtml(key, rows, { scopeOf = (r) => r.scope, columns, cells, filtering = false, most = false }) {
  return groupedHtml(key, rows, { groupOf: (r) => scopeGroupOf(scopeOf(r)), body: (part) => table(columns, part.map(cells), key), filtering,
    ...(most ? { order: (a, b) => b.rows.length - a.rows.length || a.rank - b.rank || byName(a.label, b.label) } : {}) });
}

function renderGroupPicks() {
  const filter = $("rg-pick-filter").value.toLowerCase();
  const multiSub = state.scope.subs.size > 1;
  const tagged = taggedGroups();
  const rows = sortedGroups(tagged.filter((g) => !filter || g.name.toLowerCase().includes(filter)));
  const picked = state.applied ? state.applied.groupSel : new Set();
  // No picks means every group (after tag filters) is in: shown as all ticked, never as all unticked.
  const all = state.scope.groups.size === 0;
  const chosen = all ? tagged.length : tagged.filter((g) => state.scope.groups.has(groupKey(g))).length;
  const tags = Object.entries(state.scope.tags).map(([k, v]) => `${k} = ${v === UNTAGGED ? "(none)" : v}`).join(", ");
  $("rg-picks-state").textContent = !tagged.length ? "" : all
    ? `All ${tagged.length} groups${tags ? ` with ${tags}` : ""} are in the scope. Untick any to leave them out.`
    : `${chosen} of ${tagged.length} groups${tags ? ` with ${tags}` : ""} are in the scope.`;
  $("rgs-all").disabled = all;
  $("rgs-all").textContent = all ? "All" : `All ${tagged.length}`;
  const narrowed = filter && rows.length && rows.length < tagged.length;
  $("rgs-shown").disabled = !narrowed;
  $("rgs-shown").textContent = narrowed ? `Only these ${rows.length}` : "Only these";
  $("rgs-shown").title = narrowed ? `Keep only the ${rows.length} groups matching "${filter}"` : "Type in the search box, then keep only the groups it shows";
  $("rg-picks").innerHTML = inUseFirst(rows, (g) => picked.has(groupKey(g)), (g) => `<div class="sub-row">
      <label><input type="checkbox" data-key="${esc(groupKey(g))}" ${all || state.scope.groups.has(groupKey(g)) ? "checked" : ""}>
        <span>${esc(g.name)}</span>${multiSub ? `<span class="sub">${esc(subName(g.subscription_id))}</span>` : ""}</label></div>`)
    || `<p class="muted">${state.groups.length ? "No resource groups match." : "Choose subscriptions first."}</p>`;
}

// Tag keys and values come from the loaded resource groups themselves, never
// from code or config, so any tenant's tagging convention works.
function tagCounts(groups) {
  const keys = {};
  for (const g of groups) {
    for (const [key, value] of Object.entries(g.tags)) {
      keys[key] = keys[key] || { total: 0, values: {} };
      keys[key].total += 1;
      keys[key].values[value] = (keys[key].values[value] || 0) + 1;
    }
  }
  return keys;
}

function renderTagFilters() {
  const base = taggedGroups();
  const keys = tagCounts(base);
  const chips = Object.entries(state.scope.tags).map(([key, value]) =>
    `<span class="chip">${esc(key)} = ${esc(value === UNTAGGED ? "(none)" : value)}
      <button type="button" data-remove="${esc(key)}" title="Remove filter">×</button></span>`).join("");
  const available = Object.entries(keys).filter(([key]) => !(key in state.scope.tags))
    .sort((a, b) => b[1].total - a[1].total || a[0].localeCompare(b[0]));
  if (state.pendingTagKey && !keys[state.pendingTagKey]) state.pendingTagKey = "";
  const keySelect = available.length ? `<select id="tag-key" aria-label="Filter resource groups by tag" title="${esc(state.pendingTagKey || "Filter resource groups by tag")}">
      <option value="">${Object.keys(state.scope.tags).length ? "+ add" : "+ filter by tag"}</option>
      ${available.map(([key, info]) => `<option value="${esc(key)}" title="${esc(key)}" ${key === state.pendingTagKey ? "selected" : ""}>${esc(shortTag(key))} (${info.total})</option>`).join("")}
    </select>` : "";
  let valueSelect = "";
  if (state.pendingTagKey) {
    const info = keys[state.pendingTagKey];
    const values = Object.entries(info.values).sort((a, b) => a[0].localeCompare(b[0]));
    valueSelect = `<select id="tag-value" aria-label="Tag value">
      <option value="">choose a value…</option>
      ${values.map(([value, count]) => `<option value="${esc(value)}" title="${esc(value)}">${esc(shortTag(value))} (${count})</option>`).join("")}
      <option value="${UNTAGGED}" title="${esc(`no ${state.pendingTagKey} tag`)}">(no ${esc(shortTag(state.pendingTagKey))}) (${base.length - info.total})</option>
    </select>`;
  }
  const hidden = state.groups.length - base.length;
  const hiddenNote = hidden > 0 ? `<span class="muted" title="Resource groups in the chosen subscriptions that the tag filters leave out">${hidden} hidden by tag</span>` : "";
  const clear = chips || state.scope.groups.size ? '<button type="button" class="link" id="scope-clear" title="Drop the tag filters and resource group picks (Apply to use it)">Clear filters</button>' : "";
  $("scope-tags").innerHTML = chips + keySelect + valueSelect + hiddenNote + clear;
}

