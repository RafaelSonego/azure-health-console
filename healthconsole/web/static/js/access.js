"use strict";

// ------------------------------------------------------------------ PIM hints
// Where something isn't readable: the eligible roles that would cover it, so the fix is "activate X", not "ask someone".

const SCOPE_TYPES = { resourcegroup: "resource group", managementgroup: "management group" };
const scopeType = (t) => SCOPE_TYPES[t] || t || "scope";

function pimSlot(id, required = "") {
  return `<div class="pim-hints" data-pim-id="${esc(id)}" data-pim-required="${esc(required || "")}"></div>`;
}

function fillPimHints(root = document) {
  for (const slot of root.querySelectorAll(".pim-hints[data-pim-id]:not([data-filled])")) {
    slot.dataset.filled = "1";
    api("api/pim/hints", { params: { id: slot.dataset.pimId, required: slot.dataset.pimRequired } }).then((data) => {
      if (data.error) { slot.innerHTML = `<p class="muted">PIM eligibility couldn't be read: ${esc(data.error)}</p>`; return; }
      if (slot.dataset.compact) {
        const h = data.hints[0];
        slot.innerHTML = h ? `<span class="muted">Activate</span> ${esc(h.role)} <span class="muted">on ${esc(scopeType(h.scope_type))}</span> ${esc(h.scope_name)}`
          : '<span class="muted">No eligible role covers it; ask for the role named above.</span>';
        return;
      }
      if (!data.hints.length) { slot.innerHTML = '<p class="muted">PIM: you have no eligible role that covers this.</p>'; return; }
      slot.innerHTML = `<p><strong>You're eligible through PIM for:</strong></p><ul>${data.hints.map((h) => `<li>${esc(h.role)} on
        ${esc(scopeType(h.scope_type))} <strong>${esc(h.scope_name)}</strong>${h.member_type === "Group" ? ' <span class="muted">(via a group)</span>' : ""}${h.coverage === "maybe" ? ' <span class="muted">(management group; may cover this)</span>' : ""}</li>`).join("")}</ul>
        ${data.more ? `<p class="muted">and ${data.more} more (Settings → PIM).</p>` : ""}
        <p><a href="${safeHref(tenantPortal(data.activate_url))}" target="_blank" rel="noopener">Activate in the portal</a>, then click Refresh access.
          <button type="button" class="link" data-goto="access">See all in Access</button></p>`;
    }).catch(() => { slot.innerHTML = ""; });
  }
}

// ------------------------------------------------------------------ access
// PIM: what's active (with time left), what you could activate that covers the scope in use, what was refused
// this session, and the groups you're in. Read-only; activation happens in the portal.

function timeLeft(iso) {
  const minutes = Math.max(0, Math.round((new Date(iso) - Date.now()) / 60000));
  const until = new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const text = minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
  return { text: `${text} left (until ${until})`, tone: minutes < 60 ? "warning" : "healthy" };
}

// Remembered while the page is open: what a read refused, so Access can say which role would open it.
function noteBlocked(id, name, required) {
  state.blocked = state.blocked || [];
  if (!id || state.blocked.some((b) => sameId(b.id, id))) return;
  state.blocked.unshift({ id, name: name || id.split("/").pop(), required: required || "" });
  state.blocked = state.blocked.slice(0, 10);
}

function showAccessSub(sub) {
  for (const b of $("access-subtabs").querySelectorAll("[data-access-sub]")) b.classList.toggle("active", b.dataset.accessSub === sub);
  $("access-mine").classList.toggle("hidden", sub !== "mine");
  $("access-someone").classList.toggle("hidden", sub !== "someone");
  $("access-needs").classList.toggle("hidden", sub !== "needs");
  state.accessSub = sub;
  if (sub === "needs" && state.needsLoaded !== JSON.stringify(currentScope())) loadNeeds();
}

// Access → What the console needs: requirements.REQUIREMENTS checked against your roles in the scope.
async function loadNeeds(fresh = false) {
  const scope = currentScope();
  state.needsLoaded = JSON.stringify(scope);
  const token = (state.needsToken = (state.needsToken || 0) + 1);
  $("needs-body").innerHTML = loading("Checking your roles against what each part needs…");
  try {
    const data = await (fresh ? freshly : (f) => f())(() => api("api/access/needs", { params: scopeParams(scope) }));
    if (token !== state.needsToken) return;
    const you = (r) => {
      if (r.total === undefined) return r.detail ? `${pill(r.status || "info")} ${esc(r.detail)}` : '<span class="muted">not checked</span>';
      if (!r.total) return `<span class="muted">${esc(r.detail || `no ${r.unit} in scope`)}</span>`;
      const parts = [`${pill(r.status)} ${fmt(r.have)} of ${fmt(r.total)} ${esc(r.unit)}`];
      if (r.eligible) parts.push(`<div>activate ${esc(r.activate.join(", "))} for ${fmt(r.eligible)} more <a href="${safeHref(tenantPortal(PIM_ACTIVATE_URL))}" target="_blank" rel="noopener">Activate in the portal</a></div>`);
      if (r.missing && r.missing.length) parts.push(`<details class="more"><summary>${fmt(r.total - r.have - (r.eligible || 0))} without it</summary>${r.missing.map((id) => `<div>${esc(id.split("/").pop())} ${portalLinkHtml(id)}</div>`).join("")}</details>`);
      if (r.detail) parts.push(`<div class="muted">${esc(r.detail)}</div>`);
      return parts.join("");
    };
    $("needs-body").innerHTML = `${data.pim_error ? `<p class="muted">PIM eligibility couldn't be read (${esc(data.pim_error)}), so only roles you hold are counted.</p>` : ""}
      ${table(["Feature", "Needs", "You", "Without it"], data.rows.map((r) => [`<strong>${esc(r.feature)}</strong>`,
        `${esc(r.needs)}${r.note ? `<div class="muted">${esc(r.note)}</div>` : ""}`, you(r), `<span class="muted">${esc(r.without)}</span>`]), "needs")}
      <p class="muted">The README's Permissions section has the exact actions for each, for a custom role.</p>`;
  } catch (error) {
    if (token === state.needsToken) $("needs-body").innerHTML = noticeHtml(error);
  }
}
$("needs-refresh").addEventListener("click", () => loadNeeds(true));
$("needs-role").addEventListener("click", () => {
  download("api/access/custom-role", {}, "azure-health-console-reader.json").catch((error) => showMessage(error.message, true));
});
$("access-subtabs").addEventListener("click", (event) => {
  const b = event.target.closest("[data-access-sub]");
  if (b) showAccessSub(b.dataset.accessSub);
});

async function loadAccess(refresh = false) {
  const scope = currentScope();
  const params = { q: $("pim-filter").value, ...(scope.subscriptions.length ? scopeParams(scope) : {}),
    ...($("pim-scope-only").checked ? { scope_only: "1" } : {}), ...(refresh ? { refresh: "1" } : {}) };
  $("pim-body").innerHTML = loading("Reading your PIM roles…");
  try {
    const data = await api("api/pim", { params });
    state.access = data;
    renderAccessBadge();
    state.currentRows = data.current || [];
    $("current-count").textContent = `${data.current_matched ?? 0} of ${data.current_total ?? 0}${(data.current_matched || 0) > state.currentRows.length ? `, first ${state.currentRows.length} shown` : ""}`;
    renderCurrentRoles();
    if (data.error) { $("pim-count").textContent = ""; $("pim-body").innerHTML = `<p class="notice">PIM eligibility couldn't be read: ${esc(data.error)}</p>`; loadGroups_(refresh); return; }
    $("pim-count").textContent = `${data.matched} of ${data.total} eligible role(s)${data.matched > data.rows.length ? `, first ${data.rows.length} shown` : ""}`
      + (data.held ? ` · ${data.held} you already hold (listed last)` : "")
      + (!scope.subscriptions.length && $("pim-scope-only").checked ? " · no scope applied, so all are shown" : "")
      + (data.standing_error ? ` · the roles you already hold couldn't be read (${data.standing_error}), so some may be yours already` : "");
    state.accessRows = data.rows;
    renderPimRows();
    renderAccessCards();
  } catch (error) {
    $("pim-body").innerHTML = noticeHtml(error);
  }
  loadGroups_(refresh);
}

function renderCurrentRoles() {
  const rows = state.currentRows || [];
  const unread = state.access && state.access.standing_error;
  $("current-body").innerHTML = unread ? noticeHtml(`The roles you hold couldn't be read: ${unread}`)
    : rows.length ? scopeGroupsHtml("current", rows, {
      filtering: !!$("pim-filter").value.trim(),
      columns: ["Role", "Scope", "Type", "Via group", "Covers in your scope", "How"],
      cells: (r) => {
        const left = r.until && timeLeft(r.until);
        return [esc(r.role), esc(r.scope_name), esc(scopeType(r.scope_type)), r.via ? esc(r.via) : '<span class="muted">you directly</span>',
          esc(r.covers || "–"), left ? `${pillHtml(left.tone, "PIM, active")} <span class="muted">${esc(left.text)}</span>` : '<span class="muted">standing</span>'];
      } })
    : '<p class="muted">No Azure roles found for you here (try unticking "only roles that cover the scope in use").</p>';
}

// How an eligible role is already held: "standing, via Ops-Readers" or "on subscription Prod, directly".
function heldText(held) {
  const where = held.same ? "standing" : `on ${scopeType(held.kind)} ${held.kind === "subscription" ? subName(held.scope.split("/")[2]) : held.scope.split("/").pop()}`;
  return `${where}, ${held.via ? `via ${held.via}` : "directly"}: no need to activate`;
}

function renderPimRows() {
  const rows = state.accessRows || [];
  $("pim-body").innerHTML = rows.length ? scopeGroupsHtml("pim", rows, {
    filtering: !!$("pim-filter").value.trim(),
    columns: ["Role", "Scope", "Type", "Via group", "Covers in your scope", "Now"],
    cells: (r) => {
      const left = r.active && timeLeft(r.active.until);
      return [esc(r.role), esc(r.scope_name), esc(scopeType(r.scope_type)), r.via ? esc(r.via) : '<span class="muted">you directly</span>',
        esc(r.covers || "–"), left ? `${pillHtml(left.tone, "active")} <span class="muted">${esc(left.text)}${r.active.child ? ", on a resource below it" : ""}</span>`
          : r.held ? `${pillHtml("healthy", "already yours")} <span class="muted">${esc(heldText(r.held))}</span>`
          : '<span class="muted">eligible</span>'];
    } })
    : '<p class="muted">No eligible role matches. Untick "only roles that cover the scope in use" to see them all.</p>';
}

$("pim-body").addEventListener("click", (event) => {
  const more = event.target.closest("[data-more]");
  if (more) { showMore(more); renderPimRows(); }
});
$("current-body").addEventListener("click", (event) => {
  const more = event.target.closest("[data-more]");
  if (more) { showMore(more); renderCurrentRoles(); }
});

function renderAccessBadge() {
  const n = state.access && state.access.active ? state.access.active.length : 0;
  $("access-badge").textContent = n ? `${n} active` : "";
  $("access-badge").classList.toggle("hidden", !n);
}

async function renderAccessCards() {
  const active = (state.access && state.access.active) || [];
  const blocked = state.blocked || [];
  const cards = active.map((a) => {
    const left = timeLeft(a.until);
    return `<div class="card ov-card"><span class="muted ov-kicker">Active now</span><strong>${esc(a.role)}</strong>
      <span class="muted">${esc(scopeType(a.scope_type))} ${esc(a.scope_type === "subscription" ? subName(a.scope_name) : a.scope_name)}${a.via ? ` · via ${esc(a.via)}` : ""}</span>
      <span class="${left.tone === "warning" ? "warn-text" : "ok"}">${esc(left.text)}</span></div>`;
  });
  if (!active.length) cards.push('<div class="card ov-card"><span class="muted ov-kicker">Active now</span><p class="muted">No PIM role is active.</p></div>');
  cards.push(`<div class="card ov-card ${blocked.length ? "warn-card" : ""}"><span class="muted ov-kicker">Refused in this session</span>
    ${blocked.length ? blocked.map((b, i) => `<div><strong>${esc(b.name)}</strong> <span class="muted">${esc(b.required)}</span>
      <div class="pim-hints" data-pim-id="${esc(b.id)}" data-pim-required="${esc(b.required)}" data-compact="1" id="blocked-${i}"></div></div>`).join("")
      : '<p class="muted">Nothing has been refused since the page opened.</p>'}</div>`);
  $("access-cards").innerHTML = cards.join("");
  fillPimHints($("access-cards"));
}

async function loadGroups_(refresh = false) {
  if (state.groupsLoaded && !refresh) { renderGroupsList(); return; }
  $("groups-body").innerHTML = loading("Reading your groups from Microsoft Graph…");
  try {
    const data = await api("api/access/groups", { params: refresh ? { refresh: "1" } : {} });
    state.myGroups = data.groups;
    state.groupsError = data.error;
    state.groupsLoaded = true;
  } catch (error) {
    state.groupsError = error.message;
  }
  renderGroupsList();
}

function renderGroupsList() {
  if (state.groupsError) { $("groups-body").innerHTML = `<p class="notice">Your groups couldn't be read: ${esc(state.groupsError)}</p>`; return; }
  const groups = state.myGroups || [];
  const filter = $("groups-filter").value.trim().toLowerCase();
  const rows = groups.filter((g) => !filter || g.name.toLowerCase().includes(filter));
  $("groups-count").textContent = `(${groups.length})`;
  $("groups-body").innerHTML = starListHtml("my-groups", rows, (g) => `<div class="star-row"><span class="name">${esc(g.name)}</span></div>`,
    "No group matches.");
}

$("groups-body").addEventListener("click", (event) => {
  const more = event.target.closest("[data-more]");
  if (more) { showMore(more); renderGroupsList(); }
});

$("access-refresh").addEventListener("click", () => loadAccess(true));
document.addEventListener("click", (event) => {
  const go = event.target.closest && event.target.closest(".pim-hints [data-goto]");
  if (go) showView(go.dataset.goto);
});
// The tab's "N active" badge, without opening it (the roles are cached server-side for 5 minutes).
function loadAccessBadge() {
  api("api/pim").then((data) => { state.access = data; renderAccessBadge(); }).catch(() => {});
}
$("pim-filter").addEventListener("input", debounce(() => loadAccess(), 250));
$("pim-scope-only").addEventListener("change", () => loadAccess());
$("groups-filter").addEventListener("input", renderGroupsList);

