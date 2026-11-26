"use strict";

// ------------------------------------------------------------------ watchlist
// One "+ Watch" button on every resource and resource group, wherever it's listed.

function groupWatchItem(subscriptionId, name) {
  return { id: `/subscriptions/${subscriptionId}/resourceGroups/${name}`, name, kind: "resource group" };
}

function isWatched(id) {
  const lowered = (id || "").toLowerCase();
  return (state.watchItems || []).some((w) => w.id.toLowerCase() === lowered);
}

function watchButton(item) {
  const on = isWatched(item.id);
  return `<button type="button" class="watch-btn ${on ? "on" : ""}" data-watch="${esc(JSON.stringify(item))}"
    aria-pressed="${on}" title="${on ? "On the Watchlist; click to remove" : "Add to the Watchlist"}">${on ? "✓ Watching" : "+ Watch"}</button>`;
}

function refreshWatchButtons() {
  document.querySelectorAll("[data-watch]").forEach((button) => {
    const on = isWatched(JSON.parse(button.dataset.watch).id);
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", String(on));
    button.textContent = on ? "✓ Watching" : "+ Watch";
    button.title = on ? "On the Watchlist; click to remove" : "Add to the Watchlist";
  });
  if (state.target) $("watch").textContent = isWatched(currentTargetItem().id) ? "✓ Watching" : "+ Watch";
}

function currentTargetItem() {
  const t = state.target;
  if (t.kind !== "group") return { id: t.id, name: t.name, kind: "resource" };
  const [sub, ...rest] = t.group.split("/");
  return groupWatchItem(sub, rest.join("/"));
}

async function toggleWatch(item) {
  try {
    const removing = isWatched(item.id);
    state.watchItems = removing
      ? await api("api/watchlist/remove", { method: "POST", body: { id: item.id } })
      : await api("api/watchlist", { method: "POST", body: item });
    refreshWatchButtons();
    if (state.view === "watchlist") renderWatchlist();
    if (state.view === "settings") renderSettingsWatchlist();
    flash(removing ? `${item.name} removed from the Watchlist.` : `Watching ${item.name}. See the Watchlist tab.`, false, 3000);
  } catch (error) {
    showMessage(error.message, true);
  }
}

// Captured first, so a watch button inside a clickable row never opens the row.
document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-watch]");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();
  toggleWatch(JSON.parse(button.dataset.watch));
}, true);

async function loadWatchlist() {
  try {
    state.watchItems = await api("api/watchlist");
  } catch (error) {
    $("watchlist-body").innerHTML = noticeHtml(error);
    return;
  }
  mergeWatchRule();
  renderWatchlist();
}

const watchSearch = debounce(async () => {
  const text = $("watch-search").value.trim();
  const box = $("watch-search-results");
  if (text.length < 2) { box.innerHTML = ""; return; }
  box.innerHTML = loading("Searching…");
  try {
    const scope = currentScope();
    const results = await api("api/search", { params: { q: text, ...(scope.subscriptions.length ? scopeParams(scope) : {}) } });
    box.innerHTML = results.length ? `<table>${results.slice(0, 15).map((r) => `<tr>
        <td><strong>${esc(r.name)}</strong> <span class="muted">${esc(r.display_type)}</span> ${scopeChip(r)}
          <div class="muted">${r.kind === "resource" ? esc(r.resource_group) + " · " : ""}${esc(subName(r.subscription_id))}</div></td>
        <td>${watchButton(r.kind === "resource group" ? groupWatchItem(r.subscription_id, r.name) : { id: r.id, name: r.name, kind: "resource" })}</td></tr>`).join("")}</table>`
      : '<p class="muted">Nothing found by that name.</p>';
  } catch (error) {
    box.innerHTML = noticeHtml(error);
  }
}, 300);
$("watch-search").addEventListener("input", watchSearch);

function renderWatchlist() {
  const items = state.watchItems || [];
  $("watchlist-meta").textContent = `${items.length} item(s) · saved in ${state.watchlistPath || "your watchlist file"}`;
  renderWatchRuleLine();
  if (!items.length) {
    $("watchlist-body").innerHTML = `<p class="muted">Nothing on the Watchlist yet. Search above, or click
      <strong>+ Watch</strong> next to any resource or resource group: in Triage, search results, Browse, Secrets or a resource's details.</p>`;
    return;
  }
  $("watchlist-body").innerHTML = `<table><tr><th>Status</th><th>Name</th><th>Problems</th><th>Checked</th><th></th></tr>${items.map((item) => {
    const result = state.watchResults[item.id.toLowerCase()];
    const status = result ? result.status : null;
    const was = result && result.was ? ` <span class="was">was ${esc(result.was)}</span>` : "";
    const counts = result && result.counts ? Object.entries(result.counts).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(" · ") : "";
    return `<tr class="clickable ${result && result.was ? "worse" : ""}" data-open="${esc(JSON.stringify(item.kind === "resource group"
        ? { kind: "group", group: `${item.id.split("/")[2]}/${item.id.split("/").pop()}`, name: item.name }
        : { kind: "resource", id: item.id, name: item.name }))}">
      <td>${status ? pill(status) + was : '<span class="muted">not checked</span>'}</td>
      <td><strong>${esc(item.name)}</strong><div class="muted">${esc(item.kind)}${counts ? " · " + esc(counts) : ""}</div></td>
      <td>${result ? (result.problems.map(esc).join("<br>") || '<span class="ok">✓ none</span>') : ""}</td>
      <td>${esc(result ? result.checked_at : "")}</td>
      <td>${browseButtonHtml()} · <button type="button" class="link" data-remove-watch="${esc(item.id)}">remove</button></td></tr>`;
  }).join("")}</table>`;
}

async function checkWatchlist() {
  if (!(state.watchItems || []).length) return;
  $("watchlist-meta").innerHTML = loading("Starting…", "watch-phase");
  try {
    const { job_id } = await api("api/watchlist/check", { method: "POST", body: {} });
    const job = await pollJob(job_id, { onRunning: (j) => { const phase = $("watch-phase"); if (phase) phase.textContent = `${j.phase}…`; } });
    if (!job || job.status === "cancelled") return;
    for (const row of job.result.rows) {
      const key = row.id.toLowerCase();
      const before = state.watchResults[key];
      row.was = before && STATUS_RANK[row.status] > STATUS_RANK[before.status] ? before.status : null;
      state.watchResults[key] = row;
    }
    state.watchManualAt = serverNow();
    renderWatchlist();
  } catch (error) {
    $("watchlist-meta").textContent = error.message;
  }
}

const STATUS_RANK = { healthy: 0, unknown: 1, info: 1, warning: 2, critical: 3 };

$("watch-check").addEventListener("click", checkWatchlist);
$("watchlist-body").addEventListener("click", async (event) => {
  const remove = event.target.dataset && event.target.dataset.removeWatch;
  if (remove) {
    event.stopPropagation();
    state.watchItems = await api("api/watchlist/remove", { method: "POST", body: { id: remove } });
    renderWatchlist();
    return;
  }
  const row = event.target.closest("tr[data-open]");
  if (row) startSnapshot(JSON.parse(row.dataset.open));
});
$("watch").addEventListener("click", () => state.target && toggleWatch(currentTargetItem()));
$("copy-link").addEventListener("click", copyLink);

