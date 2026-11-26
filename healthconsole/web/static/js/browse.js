"use strict";

// ------------------------------------------------------------------ browse

// Browse's left side is one tree: groups in scope, and the open group's resources by type under it.
// With several subscriptions, groups sit under one heading each: open up to SUBS_OPEN, or when a filter is typed.
const SUBS_OPEN = 3;

// The filter's resource side: one query across the scope by name or type ("kubernetes" finds AKS), latest wins.
const findInTree = debounce(async () => {
  const text = $("rg-filter").value.trim();
  const seq = (state.treeFindSeq = (state.treeFindSeq || 0) + 1);
  const scope = currentScope();
  if (text.length < 2 || !scope.subscriptions.length) { state.treeFind = null; renderGroups(); return; }
  state.treeFind = { text, loading: true, byGroup: new Map() };
  renderGroups();
  let data;
  try { data = await api("api/resources/find", { params: { q: text, ...scopeParams(scope) } }); }
  catch (error) {
    if (seq === state.treeFindSeq && !error.signedOut) { state.treeFind = { text, error: error.message, byGroup: new Map() }; renderGroups(); }
    return;
  }
  if (seq !== state.treeFindSeq) return;
  const byGroup = new Map();
  for (const r of data.resources) {
    const key = `${r.subscription_id}/${r.resource_group}`.toLowerCase();
    byGroup.set(key, [...(byGroup.get(key) || []), r]);
  }
  state.treeFind = { text, byGroup, truncated: data.truncated, count: data.resources.length };
  state.findClosed = new Set();
  renderGroups();
}, 300);

state.findClosed = new Set();  // groups collapsed in the filter's results; a new filter opens them all again

function treeFindNote(found) {
  if (!found) return "";
  if (found.loading) return `<p class="muted tree-note">${loading("Finding resources in scope by name or type…")}</p>`;
  if (found.error) return noticeHtml(found.error);
  const groups = found.byGroup.size;
  const fold = groups > 1 ? ` <button type="button" class="link" data-find-fold="close">collapse all</button> ·
    <button type="button" class="link" data-find-fold="open">expand all</button>` : "";
  return `<p class="muted tree-note">${found.count ? `${found.count}${found.truncated ? "+" : ""} resource(s) by name or type in ${groups} group(s)` : "No resource's name or type matches"}${found.truncated ? ": the first ones only, type more to narrow" : ""}.${fold}</p>`;
}

function renderGroups() {
  const filter = $("rg-filter").value.trim().toLowerCase();
  const multiSub = (state.applied ? state.applied.subs.size : 0) > 1;
  const tagKeys = Object.keys(state.applied ? state.applied.tags : {});
  const resMatch = (r) => !filter || r.name.toLowerCase().includes(filter) || r.display_type.toLowerCase().includes(filter);
  const found = filter && state.treeFind && state.treeFind.text.toLowerCase() === filter ? state.treeFind : null;
  const hits = (g) => (found && !found.loading ? found.byGroup.get(groupKey(g).toLowerCase()) : null);
  const rows = sortedGroups(appliedGroups().filter((g) => !filter || g.name.toLowerCase().includes(filter) || hits(g)
    || (groupKey(g) === state.browseGroup && state.resources.some(resMatch))));
  const groupHtml = (g) => {
    const key = groupKey(g);
    const open = state.browseGroup === key;
    const matches = hits(g);
    const shown = matches ? !state.findClosed.has(key) : open;
    const tags = tagKeys.map((k) => g.tags[k]).filter(Boolean).join(" · ");
    const count = matches ? `<span class="count" title="Resources matching the filter">${matches.length}</span>`
      : open && state.resources.length ? `<span class="count">${state.resources.length}</span>` : "";
    const children = matches ? treeResources(matches, `find:${key}`, false)
      : open ? treeResources(filter ? state.resources.filter(resMatch) : state.resources, `res:${key}`, state.resourcesLoading,
        filter ? state.settingsLike.filter(resMatch) : state.settingsLike) : "";
    return `<div class="item grp ${open ? "open" : ""} ${state.groupSummary === key ? "active" : ""}" data-key="${esc(key)}" role="treeitem" aria-expanded="${shown}">
      <button type="button" class="chev" data-toggle aria-label="${shown ? "Close" : "Open"} ${esc(g.name)}">${shown ? "▾" : "▸"}</button>
      <span class="grp-name">${esc(g.name)}${tags ? `<span class="meta">${esc(tags)}</span>` : ""}</span>${count}</div>
      ${shown ? `<div class="tree-children" role="group">${children}</div>` : ""}`;
  };
  const openAt = (list) => list.findIndex((g) => groupKey(g) === state.browseGroup);
  let html;
  if (!multiSub) {
    const only = rows.length ? rows[0].subscription_id : null;
    html = (only ? `<div class="item sub-head" role="treeitem"><span class="grp-name">${esc(subName(only))}</span>
      <button type="button" class="link sub-page" data-sub-summary="${esc(only)}" title="Its costs, budgets and your access">cost</button></div>` : "")
      + pagedHtml("rgs", rows, groupHtml, openAt(rows));
  }
  else {
    const bySub = new Map();
    for (const g of rows) bySub.set(g.subscription_id, [...(bySub.get(g.subscription_id) || []), g]);
    const subs = [...bySub.keys()].sort((a, b) => byName(subName(a), subName(b)));
    state.subOpen = state.subOpen || {};
    html = subs.map((id) => {
      const list = bySub.get(id);
      const open = !!filter || openAt(list) >= 0 || (state.subOpen[id] ?? subs.length <= SUBS_OPEN);
      return `<div class="item sub-head" data-sub-toggle="${esc(id)}" role="treeitem" aria-expanded="${open}">
        <button type="button" class="chev" aria-label="${open ? "Close" : "Open"} ${esc(subName(id))}">${open ? "▾" : "▸"}</button>
        <span class="grp-name">${esc(subName(id))}</span><span class="count">${list.length}</span>
        <button type="button" class="link sub-page" data-sub-summary="${esc(id)}" title="Its costs, budgets and your access">cost</button></div>
        ${open ? `<div class="tree-children" role="group">${pagedHtml(`rgs:${id}`, list, groupHtml, openAt(list))}</div>` : ""}`;
    }).join("");
  }
  $("rgs").innerHTML = treeFindNote(found) + html
    || (state.groups.length || currentScope().subscriptions.length ? '<p class="muted">Nothing matches.</p>'
      : pickScopeHtml("Browse lists the resource groups and resources in scope."));
  $("rg-count").textContent = `(${appliedGroups().length} groups)`;
}

const DOT_STATUS = { critical: "critical", warning: "warning", healthy: "healthy", unknown: "unknown" };

function lastStatus(id) {
  const node = state.snapshot && state.snapshot.nodes.find((n) => sameId(n.id, id));
  return node ? DOT_STATUS[node.status] || "unknown" : "";
}

// A group's resources under one foldable heading per type, busiest first: "Function App (15)". A group with more than
// TYPE_FOLD_AT resources opens folded; the type of the resource in view stays open; alert rules and similar come last.
const TYPE_FOLD_AT = 20;
const SETTINGS_LIKE = "Alert rules and similar";
state.typeFold = {};
state.settingsLike = [];
const TYPE_SORTS = { busiest: "busiest first", service: "by service" };
state.typeSort = stored.choice("browse.typeSort", Object.keys(TYPE_SORTS), "busiest");

// "service" orders by ARM type, so a namespace's types sit together and a parent precedes its children.
function sortTypes(types, by) {
  const arm = (rows) => rows.map((r) => (r.type || "").toLowerCase()).sort()[0] || "￿";
  const cmp = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
  return [...types].sort(by === "service" ? (a, b) => cmp(arm(a[1]), arm(b[1])) || a[0].localeCompare(b[0])
    : (a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
}

function treeResourceRow(r) {
  const failed = r.provisioning_state && r.provisioning_state !== "Succeeded";
  const busy = sameId(state.loadingResource, r.id) ? '<span class="spinner small" aria-label="checking"></span>' : "";
  return `<div class="item res ${sameId(state.selectedResource, r.id) ? "active" : ""}" data-id="${esc(r.id)}" role="treeitem">
    <span class="dot ${lastStatus(r.id)}" aria-hidden="true"></span><span class="res-name">${esc(r.name)}</span>${busy}${failed ? `<span class="sub">${esc(r.provisioning_state)}</span>` : ""}
    ${r.exposure && r.exposure.level === "open" ? exposureChip(r.exposure) : ""}
    ${watchButton({ id: r.id, name: r.name, kind: "resource" })}</div>`;
}

function treeResources(all, key, busy = state.resourcesLoading, settingsLike = []) {
  if (busy) return loading("Loading resources…");
  if (!all.length && !settingsLike.length) return '<p class="muted tree-note">No resources.</p>';
  const byType = new Map();
  for (const r of all) byType.set(r.display_type, [...(byType.get(r.display_type) || []), r]);
  const types = sortTypes([...byType.entries()], state.typeSort);
  if (settingsLike.length) types.push([SETTINGS_LIKE, settingsLike]);
  const selectedType = (types.find(([, rows]) => rows.some((r) => sameId(r.id, state.selectedResource))) || [])[0];
  const fold = state.typeFold[key] || (state.typeFold[key] = {
    closed: new Set([...(all.length > TYPE_FOLD_AT ? types.map(([t]) => t) : []), SETTINGS_LIKE].filter((t) => t !== selectedType)),
    touched: new Set() });
  if (selectedType && fold.closed.has(selectedType) && !fold.touched.has(selectedType)) fold.closed.delete(selectedType);
  const tools = types.length > 1 ? `<div class="type-tools muted"><button type="button" class="link" data-type-all="close" data-fold-key="${esc(key)}">fold all</button> ·
    <button type="button" class="link" data-type-all="open" data-fold-key="${esc(key)}">open all</button>
    <span class="type-sort">sort: ${Object.entries(TYPE_SORTS).map(([value, label]) => `<button type="button" class="link ${state.typeSort === value ? "active" : ""}"
      data-type-sort="${value}" aria-pressed="${state.typeSort === value}">${label}</button>`).join(" | ")}</span></div>` : "";
  return tools + types.map(([type, rows]) => {
    const closed = fold.closed.has(type);
    const at = rows.findIndex((r) => sameId(r.id, state.selectedResource));
    const pageKey = `${key}:${type}`;
    const limit = shownLimit(pageKey, at);
    return `<div class="group type-head" role="button" tabindex="0" aria-expanded="${!closed}" data-type-toggle="${esc(type)}" data-fold-key="${esc(key)}">
      <span class="chev" aria-hidden="true">${closed ? "▸" : "▾"}</span><span class="type-name">${esc(type)}</span> <span class="type-count">${rows.length}</span></div>
      ${closed ? "" : rows.slice(0, limit).map(treeResourceRow).join("") + moreHtml(pageKey, limit, rows.length)}`;
  }).join("");
}

// From a group summary tile: those types open, every other one folded, the first scrolled into view.
function showTypesInTree(key, types) {
  const heads = [...document.querySelectorAll(`#rgs [data-fold-key="${CSS.escape(key)}"][data-type-toggle]`)].map((el) => el.dataset.typeToggle);
  const fold = state.typeFold[key];
  if (!fold || !heads.length) return;
  fold.closed = new Set(heads.filter((t) => !types.includes(t)));
  for (const t of heads) fold.touched.add(t);
  renderGroups();
  const first = [...document.querySelectorAll(`#rgs [data-fold-key="${CSS.escape(key)}"][data-type-toggle]`)]
    .find((el) => types.includes(el.dataset.typeToggle));
  if (first) { first.scrollIntoView({ block: "start", behavior: "smooth" }); first.focus({ preventScroll: true }); }
}

function foldType(key, type, all = null) {
  const fold = state.typeFold[key];
  if (!fold) return;
  if (all) {
    const heads = [...document.querySelectorAll(`#rgs [data-fold-key="${CSS.escape(key)}"][data-type-toggle]`)].map((el) => el.dataset.typeToggle);
    fold.closed = all === "close" ? new Set(heads) : new Set();
    for (const t of heads) fold.touched.add(t);
  } else {
    if (fold.closed.has(type)) fold.closed.delete(type); else fold.closed.add(type);
    fold.touched.add(type);
  }
  renderGroups();
}

async function loadResources() {
  if (!state.browseGroup) {
    state.resources = [];
    renderResources();
    return;
  }
  state.resourcesLoading = true;
  state.resources = [];
  state.settingsLike = [];
  renderGroups();
  try {
    const result = await api("api/resources", { params: { group: [state.browseGroup] } });
    state.resources = result.resources;
    state.settingsLike = result.hidden_resources || [];
  } finally {
    state.resourcesLoading = false;
  }
  renderResources();
}

const sameId = (a, b) => !!a && !!b && a.toLowerCase() === b.toLowerCase();

function renderResources() {
  renderGroups();
  if (state.groupSummary) renderGroupSummary();
}

// ---------------------------------------------------------------- snapshots

function targetOf(value) {
  if (typeof value !== "string") return value;
  const known = state.resources.find((r) => r.id === value);
  return { kind: "resource", id: value, name: known ? known.name : value.split("/").pop() };
}

function setBusy(on) {
  state.busy = on;
  document.body.classList.toggle("busy", on);
  if (!on) { const hint = $("lock-hint"); if (hint) hint.classList.add("hidden"); }
}

// One check at a time: while one runs, whatever would start another is locked, with Cancel as the way out.
function lockHint() {
  let hint = $("lock-hint");
  if (!hint) {
    document.body.insertAdjacentHTML("beforeend", '<div id="lock-hint" class="lock-hint" role="alert"></div>');
    hint = $("lock-hint");
  }
  hint.innerHTML = `<span>Still checking <strong>${esc(state.target ? state.target.name : "")}</strong>.
    Wait for it to finish, or cancel it first.</span><button type="button" data-cancel-check>Cancel check</button>`;
  hint.classList.remove("hidden");
  clearTimeout(state.hintTimer);
  state.hintTimer = setTimeout(() => hint.classList.add("hidden"), 5000);
}

const LOCKED = ".scopebar, #rgs, #resources, .card li[data-open], tr[data-open], .sn-res, #search-results .result, #refresh";
document.addEventListener("click", (event) => {
  if (!event.target.closest) return;
  if (event.target.closest("[data-cancel-check]")) { event.preventDefault(); cancelCheck(); return; }
  if (!state.busy || event.target.closest("[data-watch]")) return;
  if (event.target.closest(LOCKED)) {
    event.preventDefault();
    event.stopPropagation();
    lockHint();
  }
}, true);

async function cancelCheck() {
  const jobId = state.jobId;
  if (!jobId || !state.busy) return;
  state.jobId = null;  // the poll loop stops at its next turn
  state.cancelled = true;
  try { await api(`api/jobs/${jobId}/cancel`, { method: "POST", body: {} }); } catch { /* already finished */ }
}

// Closing or reloading the tab stops a running check too (the server also cancels it when the next one starts).
window.addEventListener("pagehide", () => {
  if (!state.busy || !state.jobId) return;
  const url = new URL(`${BASE}api/jobs/${state.jobId}/cancel`);
  if (state.tenant) url.searchParams.set("tenant", state.tenant);
  fetch(url, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tenant: state.tenant }) }).catch(() => { /* best effort */ });
});

// A resource opens on its own health (fast); its dependencies are drawn when asked, or always with graph.auto.
async function startSnapshot(value, options = {}) {
  if (state.busy) { lockHint(); return; }
  const target = { ...targetOf(value) };
  if (target.kind === "resource") {
    const auto = state.graphAuto ? Number($("depth").value) : 0;
    target.depth = options.depth ?? target.depth ?? auto;
    target.inbound = options.inbound ?? target.inbound ?? (target.depth > 0 && $("inbound").checked);
  }
  hideGroupSummary();
  state.detailNode = null;
  state.diagramFilter = "";
  if (target.kind === "group") state.groupView = state.graphAuto ? "graph" : "table";  // Graph is a one-off look
  state.target = target;
  state.selectedResource = target.kind === "resource" ? target.id : null;
  state.loadingResource = state.selectedResource;
  state.partialDrawn = false;
  state.quickDrawn = false;
  state.cancelled = false;
  setBusy(true);
  showView("browse");
  exploreCache.clear();
  renderResources();
  followTarget(target).catch((e) => showMessage(e.message, true));
  for (const id of ["rerun", "deps", "deps-menu", "watch", "copy-link", "export-md", "export-pdf", "print", "export-menu"]) $(id).disabled = true;
  $("detail").classList.add("hidden");
  $("graph").classList.remove("as-table");
  document.querySelector(".workspace").classList.remove("solo");
  for (const id of ["summary", "service-health", "notes"]) $(id).classList.add("hidden");
  showMessage("");
  state.diagramSeq = (state.diagramSeq || 0) + 1;  // a diagram still on its way doesn't replace the new check
  const what = target.kind === "group" ? `every resource in <strong>${esc(target.name)}</strong>`
    : target.depth > 0 ? `<strong>${esc(target.name)}</strong> and what it depends on (${target.depth} hop${target.depth === 1 ? "" : "s"})`
      : `<strong>${esc(target.name)}</strong>`;
  $("graph").innerHTML = `<div class="graph-loading">
    <div class="graph-loading-title">Checking ${what}</div>
    ${loading("Starting…", "graph-phase")}
    <p class="muted">${target.kind === "resource" && !target.depth ? "Usually a few seconds; AKS clusters take longer."
      : "Most checks take 10–30 seconds; AKS clusters and deeper graphs take longer."}
      Other resources are locked until it finishes.</p>
    <button type="button" class="cancel-check" data-cancel-check>Cancel</button></div>`;
  let job_id = null;
  try {
    const body = target.kind === "group" ? { resource_group: target.group }
      : { resource_id: target.id, depth: target.depth, inbound: target.inbound };
    ({ job_id } = await api("api/snapshots", { method: "POST", body }));
    if (state.cancelled) {
      api(`api/jobs/${job_id}/cancel`, { method: "POST", body: {} }).catch(() => {});
    } else {
      state.jobId = job_id;
      await poll(job_id);
    }
    if (state.cancelled) {
      $("summary").classList.add("hidden");
      $("graph").innerHTML = `<p class="empty">Cancelled. Pick another resource, or click Check again to run this one again.</p>`;
    }
  } catch (error) {
    if (job_id && state.jobId !== job_id) return;  // replaced by a newer check while its detectors ran
    showMessage(error.message, true);
    $("graph").innerHTML = '<p class="empty">The check didn\'t finish. See the message above, then try Check again.</p>';
  }
  if (job_id && state.jobId !== job_id) return;  // a newer check owns the page now
  setBusy(false);
  state.loadingResource = null;
  renderResources();
  $("rerun").disabled = !state.target;
  updateBrowseControls();
}

async function poll(jobId) {
  while (state.jobId === jobId) {
    const job = await api(`api/snapshots/${jobId}`);
    if (state.jobId !== jobId) return;
    if (job.status === "running") {
      if (job.partial && (job.partial.pending || []).length) {
        if (!state.quickDrawn) {  // health now, detectors later: unlock, so opening something else replaces this check
          state.quickDrawn = true; state.snapshot = job.partial; renderSnapshot();
          if (job.partial.kind === "group" && state.target && state.target.group) {  // kept even if another check replaces it
            keepGroupCheck(state.target, { ...job.partial, pending: [], warnings: [...(job.partial.warnings || []),
              "Azure's detectors hadn't answered when this check was left; Check again to include them."] });
          }
          setBusy(false); state.loadingResource = null; renderResources(); $("rerun").disabled = false; updateBrowseControls();
        }
      } else if (job.partial) renderPartial(job.partial, job.phase);
      const phase = $("graph-phase");
      if (phase) phase.textContent = `${job.phase}…`;
      await new Promise((resolve) => setTimeout(resolve, 900));
      continue;
    }
    if (job.status === "cancelled") throw new Error("This check was cancelled: another check started (perhaps in another tab).");
    if (job.status === "failed") throw jobError(job, "The snapshot failed.");
    if (state.quickDrawn && state.detailNode) state.pendingDetail = { node: state.detailNode, tab: state.detailTab };  // stay on your tab
    state.quickDrawn = false;
    state.snapshot = job.snapshot;
    if (job.snapshot.kind === "group" && state.target && state.target.group) keepGroupCheck(state.target, job.snapshot);
    showMessage("");
    renderSnapshot();
    for (const id of ["export-md", "export-pdf", "print", "watch", "copy-link", "export-menu"]) $(id).disabled = false;
    refreshWatchButtons();
    return;
  }
}

function updateBrowseControls() {
  const t = state.target;
  const isGroup = t && t.kind === "group";
  $("deps-controls").classList.toggle("hidden", !!isGroup);
  $("group-view").classList.toggle("hidden", !isGroup);
  $("deps").disabled = !t || isGroup || state.busy;
  $("deps-menu").disabled = $("deps").disabled;
  $("deps").textContent = t && !isGroup && t.depth > 0 ? "Hide dependencies" : "Show dependencies";
  $("deps-menu").textContent = t && !isGroup && t.depth > 0 ? `Dependencies (${t.depth} hop${t.depth === 1 ? "" : "s"})` : "Dependencies";
  document.querySelectorAll("#group-view button").forEach((b) => b.classList.toggle("active", b.dataset.groupview === state.groupView));
}

function renderSnapshot() {
  const s = state.snapshot;
  const c = s.counts;
  $("summary").classList.remove("hidden");
  const single = s.kind !== "group" && s.nodes.length === 1;  // the resource's own header carries its status
  const waiting = (s.pending || []).includes("detectors")
    ? `<div class="pending-line">${loading("Azure's detectors (Diagnose and solve problems) are still answering; this updates when they do")}</div>` : "";
  $("summary").innerHTML = waiting + (single ? `<span class="muted">Checked ${escTimes(s.taken_at)} by ${esc(s.taken_by)} · ${s.arm_calls} Azure calls</span>`
    : `${pill(s.status)} <strong>${s.kind === "group" ? esc(s.title) + ": " : ""}${s.nodes.length} resource${s.nodes.length === 1 ? "" : "s"}</strong>
    <span class="muted">${c.critical} critical · ${c.warning} warning · ${c.healthy} healthy · ${c.unknown} unknown ·
    taken ${escTimes(s.taken_at)} by ${esc(s.taken_by)} · ${s.arm_calls} Azure calls</span>
    ${s.kind === "group" && state.target && state.target.group ? `<button type="button" class="primary" data-group-page="${esc(state.target.group)}" title="Back to the group's page: what's in it, its cost and your access. This check stays one click away (Show last check)">‹ Group page (cost, access)</button>` : ""}`);

  const events = s.service_health || [];
  $("service-health").classList.toggle("hidden", !events.length);
  $("service-health").innerHTML = events.length ? serviceHealthHtml(events) : "";

  $("notes").classList.toggle("hidden", !s.warnings.length);
  $("notes").innerHTML = s.warnings.length ? `<strong>Notes</strong><ul>${s.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";

  const solo = s.kind !== "group" && !(state.target && state.target.depth > 0);
  document.querySelector(".workspace").classList.toggle("solo", solo);
  if (solo && $("graph").querySelector(".graph-loading")) $("graph").innerHTML = "";  // hidden, but its spinner kept counting
  if (s.kind === "group" && state.groupView === "table") renderGroupTable(s);
  else if (!solo) drawGraph(s);
  const pending = state.pendingDetail;
  state.pendingDetail = null;
  const linked = pending && s.nodes.find((n) => sameId(n.id, pending.node));
  if (s.kind === "group") {
    $("detail").classList.remove("hidden");
    $("detail").innerHTML = `<p class="muted">Click any ${state.groupView === "table" ? "row" : "box"} to see its signals, metrics, details and more.</p>`;
    if (linked) showDetail(linked.id, pending.tab);
  } else {
    showDetail(linked ? linked.id : s.root_id, linked ? pending.tab : ((state.persona && state.persona.tabs[0]) || "overview"));
  }
  updateBrowseControls();
}

const STATUS_ORDER = { critical: 0, warning: 1, info: 2, healthy: 3, unknown: 4 };

function nodeWhy(node) {
  const problems = node.signals.filter((sig) => sig.status === "critical" || sig.status === "warning");
  if (problems.length) return problems.slice(0, 2).map((sig) => `${sig.name}: ${sig.detail || sig.status}`).join("; ");
  if (!node.readable && node.signals[0]) return node.signals[0].detail;
  return node.signals.length ? "" : "checking…";
}

// A resource group reads best as a list, worst first; the graph is one click away.
function renderGroupTable(s) {
  state.diagramSeq = (state.diagramSeq || 0) + 1;  // a diagram still on its way doesn't replace the table
  const box = $("graph");
  box.classList.remove("has-diagram");
  box.classList.add("as-table");
  box.classList.toggle("graph-min", !!state.tableMin);
  const groups = new Set(s.nodes.map((n) => n.resource_group.toLowerCase()));
  const rows = [...s.nodes].sort((a, b) => (STATUS_ORDER[a.status] ?? 5) - (STATUS_ORDER[b.status] ?? 5)
    || a.display_type.localeCompare(b.display_type) || a.name.localeCompare(b.name));
  const selected = state.exploreNode && state.exploreNode.id;
  const b = s.baseline || {};
  const moved = {};
  for (const m of b.movers || []) (moved[m.node_id] = moved[m.node_id] || []).push(m);
  box.innerHTML = `<div class="graph-note table-bar"><span>${rows.length} resource(s), worst first: critical, warning, info, healthy, then unknown (couldn't be read). Click a column header to sort by it.</span><span class="min-slot">${minButton("table")}</span></div>`
    + baselineHtml(s) + `<table class="data-table" data-list="group-resources"><thead><tr><th>Status</th><th>Name</th><th>Type</th>
      ${groups.size > 1 ? "<th>Resource group</th>" : ""}<th>Why</th><th>Against last week</th><th></th></tr></thead><tbody>${rows.map((n) => `
    <tr class="clickable ${n.id === selected ? "selected" : ""}" data-node="${esc(n.id)}">
      <td>${pill(n.status)}</td><td>${esc(n.name)}</td><td>${esc(n.display_type)}</td>
      ${groups.size > 1 ? `<td>${esc(n.resource_group)}</td>` : ""}<td>${esc(nodeWhy(n))}</td>
      <td>${(moved[n.id] || []).slice(0, 2).map((m) => `<span title="${esc(m.signal)}">${esc(m.label || m.signal.replace(/ \(.*\)$/, ""))}</span>
        <span class="muted">${esc(moveValues(m))} · ${esc(moveText(m))}</span>`).join("<br>")}</td>
      <td>${watchButton({ id: n.id, name: n.name, kind: "resource" })}</td></tr>`).join("")}</tbody></table>`;
}

// Azure's metric units, short: "0.4%" rather than "0.4 Percent".
const METRIC_UNITS = { Percent: "%", MilliSeconds: " ms", Seconds: " s", Bytes: " B", BytesPerSecond: " B/s",
  CountPerSecond: "/s", Count: "" };
const metricValue = (value, unit) => `${fmt(value)}${unit in METRIC_UNITS ? METRIC_UNITS[unit] : unit ? ` ${unit}` : ""}`;
const moveValues = (m) => `${metricValue(m.value, m.unit)} now, ${metricValue(m.baseline, m.unit)} last week`;
// "doubled or halved" at the default 200%; the setting's own factor otherwise.
const moveRule = (b) => (!b.factor || b.factor === 2 ? "doubled or halved" : `moved ${fmt(b.factor)}× either way`);

function moveText(m) {
  if (m.direction === "new") return "none last week";
  if (m.direction === "gone") return "none now";
  return m.direction === "up" ? `${fmt(m.ratio)}× last week` : `${fmt(1 / m.ratio)}× lower`;
}

// The group's baseline: metrics that moved by the setting's factor against the same window a week earlier.
function baselineHtml(s) {
  const b = s.baseline;
  if (!s.done || !b || b.window_minutes == null) return "";
  const head = `Against last week: the same ${b.window_minutes} minutes, 7 days earlier`;
  if (!b.movers.length) {
    return `<div class="baseline"><strong>${esc(head)}</strong> <span class="ok">✓ no metric ${esc(moveRule(b))}</span>
      <span class="muted">(${b.compared} resource(s) compared${b.without_baseline ? `, ${b.without_baseline} without last week's data` : ""})</span></div>`;
  }
  return `<details class="baseline" open><summary><strong>${esc(head)}</strong> · ${b.movers.length + b.more} metric(s) ${esc(moveRule(b))}
      <span class="muted">(${b.compared} resource(s) compared${b.without_baseline ? `, ${b.without_baseline} without last week's data` : ""})</span></summary>
    <table class="data-table"><thead><tr><th></th><th>Resource</th><th>Metric</th><th class="num">Now</th><th class="num">Last week</th><th>Change</th></tr></thead>
    <tbody>${b.movers.map((m) => `<tr class="clickable" data-node="${esc(m.node_id)}"><td>${pill(m.status)}</td><td>${esc(m.name)}</td>
      <td title="${esc(m.signal)}">${esc(m.label ? `${m.label} (${m.signal.split(" (")[1] || ""}` : m.signal)}</td>
      <td class="num">${esc(metricValue(m.value, m.unit))}</td><td class="num">${esc(metricValue(m.baseline, m.unit))}</td>
      <td>${m.direction === "up" || m.direction === "new" ? "▲" : "▼"} ${esc(moveText(m))}</td></tr>`).join("")}</tbody></table>
    ${b.more ? `<p class="muted">${b.more} more not shown.</p>` : ""}
    <p class="muted">Up isn't always bad (more traffic) and down isn't always good (traffic lost); it shows what's different, not what's wrong.</p></details>`;
}

$("graph").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-node]");
  if (!row || event.target.closest("[data-watch]")) return;
  showDetail(row.dataset.node, "overview", { push: true });
  document.querySelectorAll("#graph tr.selected").forEach((r) => r.classList.remove("selected"));
  row.classList.add("selected");
  $("detail").scrollIntoView({ behavior: "smooth", block: "start" });  // it's drawn under the table, often off screen
});
$("summary").addEventListener("click", (event) => {
  const page = event.target.closest("[data-group-page]");
  if (!page) return;
  const key = page.dataset.groupPage.toLowerCase();
  const group = appliedGroups().find((g) => groupKey(g).toLowerCase() === key);
  if (group) showGroupSummary(groupKey(group));
});

// The graph appears as soon as discovery finishes; boxes take their colour as each check completes.
function renderPartial(partial, phase) {
  state.snapshot = partial;
  $("summary").classList.remove("hidden");
  const panelShown = !!$("graph").querySelector(".graph-loading");  // it has its own Cancel
  $("summary").innerHTML = `${loading(`${phase}…`, panelShown ? "" : "graph-phase")}
    ${panelShown ? "" : '<button type="button" class="cancel-check" data-cancel-check>Cancel</button>'}`;
  if (partial.kind !== "group" && !(state.target && state.target.depth > 0)) return;  // one resource: nothing to draw yet
  if (partial.kind === "group" && state.groupView === "table") {
    renderGroupTable(partial);
    return;
  }
  if (!state.partialDrawn) {
    drawGraph(partial);
    state.partialDrawn = true;
  } else {
    updateStatuses(partial);
  }
}

function updateStatuses(s) {
  const svg = $("graph").querySelector(".diagram svg");
  if (svg) {  // recolour in place; a left-out item that now has a problem needs a redraw
    for (const node of s.nodes) {
      const g = svg.querySelector(`.dg-node[data-id="${CSS.escape(node.id)}"]`);
      if (!g) { if (GRAPH_PROBLEM.has(node.status) && !state.graphAll) { drawGraph(s); return; } continue; }
      for (const el of [g, g.querySelector(".dg-dot")]) {
        if (!el) continue;
        for (const c of [...el.classList]) if (c.startsWith("st-")) el.classList.remove(c);
        el.classList.add(`st-${node.status}`);
      }
    }
  }
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function librariesMissing() {
  const missing = [["Chart", "chart"]].filter(([name]) => typeof window[name] === "undefined");
  return missing.length ? `The ${missing.map(([, what]) => what).join(" and ")} library didn't load. It ships with the
    console (static/vendor), so something on this machine is blocking local files; try a hard refresh (Ctrl/Cmd+Shift+R).` : "";
}

const GRAPH_PROBLEM = new Set(["critical", "warning"]);
const NO_FOLD = { hidden: [], locked: [], parts: {}, left: {} };

// What was left out and why, with Show everything; `tools` is the diagram's zoom bar.
function graphNoteHtml(fold, tools = "") {
  const left = state.graphAll ? "" : Object.entries(fold.left || {}).map(([t, n]) => `${n} ${t}`).join(", ");
  const marks = [fold.locked.length ? "🔒 marks what a private endpoint serves" : "",
    Object.keys(fold.parts).length ? "+N counts what's part of a resource (an AKS cluster's node pools)" : ""].filter(Boolean);
  if (!left && !state.graphAll && !tools) return "";
  return `<div class="graph-note"><span>${left ? `Not drawn: ${esc(left)}${marks.length ? ` (${marks.join("; ")})` : ""}; the Table lists
    everything.` : ""}</span><label><input type="checkbox" id="graph-all" ${state.graphAll ? "checked" : ""}> Show everything</label>${tools}</div>`;
}

// Every graph (a group, or a resource and its dependencies) is drawn by the server (core/diagram.py) from the snapshot
// this page holds, at 100% and scrolled; the bar lights the problems, zooms, fits, goes full screen or opens a tab.
function drawGraph(s) {
  drawDiagram(s);
}

// Minimize / Show for the panel's current view (graph or table), each kept on its own until a reload.
const MIN_VIEWS = { graph: ["graphMin", "graph"], table: ["tableMin", "table"] };
const minButton = (view) => {
  const [key, what] = MIN_VIEWS[view];
  return state[key] ? `<span class="min-note">The ${what} is minimized</span><button type="button" class="primary" data-dz="min" data-min-view="${view}"
    title="Show the ${what} here again">Show ${what}</button>`
    : `<button type="button" data-dz="min" data-min-view="${view}" title="Fold the ${what} to this bar, for room below">Minimize</button>`;
};

const diagramTools = () => `<span class="dg-tools"><input type="search" class="dg-filter" placeholder="Filter: name or type"
  aria-label="Filter the diagram by name or type" value="${esc(state.diagramFilter || "")}"><span class="dg-count"></span>
  <button type="button" data-dz="problems" title="Light every critical or warning box and what it connects to">Problems</button>
  <button type="button" data-dz="-1" title="Zoom out">−</button><span class="dg-zoom">100%</span>
  <button type="button" data-dz="1" title="Zoom in">+</button><button type="button" data-dz="fit" title="Fit the width">Fit</button>
  <button type="button" data-dz="full" title="Full screen">⤢</button><button type="button" data-dz="tab">Open in new tab</button>
  ${minButton("graph")}</span>`;

const diagramTitle = (s) => s.title
  || `${(s.nodes.find((n) => n.id === s.root_id) || {}).name || "Resource"} and what it connects to`;

async function fetchDiagram(s, page = false) {
  const response = await api("api/diagram", { method: "POST", raw: true, body: {
    snapshot: s, show_all: !!state.graphAll, title: diagramTitle(s), page,
    note: page ? $("graph").querySelector(".graph-note span")?.textContent.replace(/\s+/g, " ").trim() || "" : "" } });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || `${response.status} ${response.statusText}`);
  return response.text();
}

async function drawDiagram(s) {
  const seq = (state.diagramSeq = (state.diagramSeq || 0) + 1);
  const graph = $("graph");
  const shown = graph.querySelector(".diagram svg");  // kept on screen until the new one arrives
  graph.classList.add("has-diagram");
  graph.classList.toggle("graph-min", !!state.graphMin);
  const note = graphNoteHtml(state.graphAll ? NO_FOLD : s.graph || NO_FOLD, diagramTools());
  if (!shown) graph.innerHTML = `${note}<div class="diagram">${loading("Drawing the diagram…")}</div>`;
  let svg;
  try {
    svg = await fetchDiagram(s);
  } catch (error) {
    if (seq === state.diagramSeq) graph.querySelector(".diagram").innerHTML = noticeHtml(error.message);
    return;
  }
  if (seq !== state.diagramSeq) return;
  const scroll = graph.querySelector(".diagram") ? [graph.querySelector(".diagram").scrollLeft, graph.querySelector(".diagram").scrollTop] : [0, 0];
  graph.innerHTML = `${note}<div class="diagram">${svg}</div>`;
  zoomDiagram(state.diagramZoom || 1);
  if (state.diagramFilter) filterDiagram(state.diagramFilter, false);
  else diagramFocusing.refresh();
  [graph.querySelector(".diagram").scrollLeft, graph.querySelector(".diagram").scrollTop] = scroll;
}

function zoomDiagram(z) {
  const svg = $("graph").querySelector(".diagram svg");
  if (!svg || !svg.viewBox.baseVal) return;
  state.diagramZoom = Math.max(0.2, Math.min(3, z));
  svg.setAttribute("width", svg.viewBox.baseVal.width * state.diagramZoom);
  svg.setAttribute("height", svg.viewBox.baseVal.height * state.diagramZoom);
  const label = $("graph").querySelector(".dg-zoom");
  if (label) label.textContent = `${Math.round(state.diagramZoom * 100)}%`;
}

async function openDiagramTab() {
  const tab = window.open("", "_blank");  // opened now, while it's still the click, or the browser blocks it
  try {
    const html = await fetchDiagram(state.snapshot, true);
    tab.location = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  } catch (error) {
    tab.close();
    showMessage(error.message, true);
  }
}

$("graph").addEventListener("click", (event) => {
  const tool = event.target.closest("[data-dz]");
  if (tool) {
    const box = $("graph").querySelector(".diagram");
    const svg = box && box.querySelector("svg");
    if (tool.dataset.dz === "fit" && svg) zoomDiagram((box.clientWidth - 24) / svg.viewBox.baseVal.width);
    else if (tool.dataset.dz === "full") (document.fullscreenElement ? document.exitFullscreen() : $("graph").requestFullscreen()).catch(() => {});
    else if (tool.dataset.dz === "tab") openDiagramTab();
    else if (tool.dataset.dz === "min") {
      const view = tool.dataset.minView;
      const key = MIN_VIEWS[view][0];
      state[key] = !state[key];
      const graph = $("graph");  // a height dragged with the corner is inline: set aside while folded
      if (state[key]) { state.panelHeight = graph.style.height; graph.style.height = ""; } else graph.style.height = state.panelHeight || "";
      graph.classList.toggle("graph-min", state[key]);
      const slot = tool.closest(".min-slot, .dg-tools");
      slot.querySelectorAll(".min-note, [data-dz='min']").forEach((el) => el.remove());
      slot.insertAdjacentHTML("beforeend", minButton(view));
    }
    else if (tool.dataset.dz === "problems") { if (!diagramFocusing.problems()) showMessage("Nothing in this picture is critical or warning."); }
    else zoomDiagram((state.diagramZoom || 1) + Number(tool.dataset.dz) * 0.1);
    return;
  }
  const hit = event.target.closest(".dg-node[data-id]");
  if (!hit) return;
  for (const g of $("graph").querySelectorAll(".dg-node.selected")) g.classList.remove("selected");
  hit.classList.add("selected");
  showDetail(hit.dataset.id, "overview", { push: true });
  $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
});
const diagramFocusing = diagramFocus($("graph"));  // static/diagram.js

// Lights the boxes whose name or type names hold every word typed (and what they connect to); `jump` scrolls to the first.
function filterDiagram(text, jump = true) {
  state.diagramFilter = text;
  const found = diagramFocusing.filter(text);
  const count = $("graph").querySelector(".dg-count");
  if (count) count.textContent = text.trim() ? `${found} match${found === 1 ? "" : "es"}` : "";
  const first = jump && found ? diagramFocusing.first() : null;
  if (first) first.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
}
$("graph").addEventListener("input", (event) => {
  if (event.target.classList.contains("dg-filter")) filterDiagram(event.target.value);
});
$("graph").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.closest(".dg-node[data-id]")) event.target.closest(".dg-node[data-id]").dispatchEvent(new MouseEvent("click", { bubbles: true }));
});

function fmt(value) {
  if (value == null || value === "") return "–";
  if (typeof value !== "number") return String(value);
  return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}


