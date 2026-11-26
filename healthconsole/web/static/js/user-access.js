"use strict";

// ------------------------------------------------------------------ someone else's access
// Search as you type (debounced, latest answer wins), then one job: groups and active roles first, eligible roles after.

const USER_SEARCH_MIN = 2;
state.userSearchSeq = 0;
state.userCheckSeq = 0;

const searchUsers = debounce(async () => {
  const text = $("user-search").value.trim();
  const box = $("user-results");
  const seq = ++state.userSearchSeq;
  if (text.length < USER_SEARCH_MIN) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = '<div class="empty-result">Searching…</div>';
  let data;
  try { data = await api("api/access/users", { params: { q: text } }); }
  catch (error) { data = { users: [], error: error.message }; }
  if (seq !== state.userSearchSeq) return;  // a newer search is on its way
  state.userResults = data.users;
  state.userActive = 0;
  box.innerHTML = data.error ? `<div class="empty-result">Users couldn't be searched: ${esc(data.error)}</div>`
    : data.users.length ? data.users.map((u, i) => `<div class="result ${i === 0 ? "active" : ""}" role="option" data-user="${i}">
        <strong>${esc(u.name || u.upn)}</strong> <span class="muted">${esc(u.upn || u.mail)}</span>
        ${u.enabled ? "" : pillHtml("unknown", "disabled")}</div>`).join("")
      + (data.users.length >= 10 ? '<div class="empty-result">First 10 shown: type more to narrow.</div>' : "")
    : '<div class="empty-result">No user\'s name, sign-in name or mail starts with that.</div>';
}, 300);

function pickUser(index) {
  const user = (state.userResults || [])[index];
  if (!user) return;
  $("user-results").classList.add("hidden");
  $("user-search").value = user.name || user.upn;
  checkUser(user);
}

$("user-search").addEventListener("input", searchUsers);
$("user-search").addEventListener("keydown", (event) => {
  const rows = [...$("user-results").querySelectorAll("[data-user]")];
  if (event.key === "Escape") { $("user-results").classList.add("hidden"); return; }
  if (!rows.length || $("user-results").classList.contains("hidden")) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    state.userActive = (state.userActive + (event.key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length;
    rows.forEach((r, i) => r.classList.toggle("active", i === state.userActive));
  } else if (event.key === "Enter") {
    event.preventDefault();
    pickUser(state.userActive);
  }
});
$("user-results").addEventListener("mousedown", (event) => {
  const row = event.target.closest("[data-user]");
  if (row) { event.preventDefault(); pickUser(Number(row.dataset.user)); }
});
$("user-search").addEventListener("blur", () => setTimeout(() => $("user-results").classList.add("hidden"), 150));

async function checkUser(user, options = {}) {
  const seq = ++state.userCheckSeq;
  const scope = currentScope();
  state.userCheck = { user, result: null, phase: "Starting", error: null, filter: state.userCheck ? state.userCheck.filter : "",
    groupFilter: state.userCheck ? state.userCheck.groupFilter : "" };
  if (!scope.subscriptions.length) {
    state.userCheck.noScope = true;
    renderUserCheck();
    return;
  }
  renderUserCheck();
  try {
    const started = await api("api/access/user", { method: "POST",
      body: { user, subscriptions: scope.subscriptions, again: !!options.again, force: !!options.force } });
    if (seq !== state.userCheckSeq) return;
    if (started.too_many) {
      state.userCheck.tooMany = started;
      renderUserCheck();
      return;
    }
    state.userCheck.jobId = started.job_id;
    const show = (j) => { state.userCheck.result = j.result || j.partial; state.userCheck.phase = j.phase; renderUserCheck(); };
    const job = await pollJob(started.job_id, { isCurrent: () => seq === state.userCheckSeq, every: 700, onRunning: show });
    if (!job) return;
    show(job);
    if (job.status === "done") return;
    state.userCheck.phase = "Cancelled";
  } catch (error) {
    if (seq !== state.userCheckSeq) return;
    state.userCheck.error = error.message;
  }
  renderUserCheck();
}

function userCheckRows(rows, filter) {
  if (!filter) return rows;
  return rows.filter((r) => `${r.role} ${r.scope_label} ${r.via} ${r.scope}`.toLowerCase().includes(filter));
}

function renderUserCheck() {
  const box = $("user-access");
  const check = state.userCheck;
  if (!check) { box.innerHTML = ""; return; }
  const u = check.user;
  const head = `<div class="user-head"><strong>${esc(u.name || u.upn)}</strong> <span class="muted">${esc(u.upn || u.mail)}</span>
    ${u.enabled === false ? pillHtml("unknown", "disabled") : ""}<span class="spacer"></span>
    ${check.result && !check.result.done && !check.error && check.phase !== "Cancelled" ? '<button type="button" data-user-cancel>Cancel</button>' : ""}
    <button type="button" data-user-again>Check again</button> <button type="button" class="link" data-user-close>close</button></div>`;
  if (check.error) { box.innerHTML = `${head}${noticeHtml(check.error)}`; return; }
  if (check.noScope) { box.innerHTML = `${head}${pickScopeHtml("Another user's roles are read for the subscriptions in scope (your own, below, need none).")}`; return; }
  if (check.tooMany) {
    box.innerHTML = `${head}<p class="notice">The scope has ${check.tooMany.too_many} subscriptions. Eligible roles take one request per
      subscription (8 at a time), so this is held above ${check.tooMany.limit}. Narrow the scope, or
      <button type="button" data-user-force>check all ${check.tooMany.too_many}</button></p>`;
    return;
  }
  const r = check.result;
  if (!r) { box.innerHTML = head + loading(`${check.phase || "Starting"}…`); return; }
  const filter = (check.filter || "").toLowerCase();
  const active = userCheckRows(r.active, filter);
  const eligible = userCheckRows(r.eligible, filter);
  const via = (row) => (row.via ? esc(row.via) : '<span class="muted">them directly</span>');
  const most = stored.choice("ahc.userOrder", USER_ORDERS, "name") === "most";
  const scopeCell = (row) => `<span title="${esc(row.scope)}">${esc(row.scope_label)}</span>`;
  // Same layout as My access: summary cards, one filter, roles held now, roles they can activate, then groups.
  const granting = r.groups.filter((g) => g.roles).length;
  const groupFilter = (check.groupFilter || "").toLowerCase();  // its own box, as on My access
  const groups = r.groups.filter((g) => !groupFilter || g.name.toLowerCase().includes(groupFilter))
    .sort((a, b) => b.roles - a.roles || a.name.localeCompare(b.name));
  const card = (kicker, value, note) => `<div class="card ov-card"><span class="muted ov-kicker">${kicker}</span>
    <strong class="ov-number">${value}</strong><span class="muted">${note}</span></div>`;
  box.innerHTML = `${head}
    <div class="ov-cards">
      ${card("Roles now", r.active.length, `in ${r.subscriptions} subscription(s) in scope`)}
      ${card("Can activate (PIM)", r.done ? r.eligible.length : "…", r.done ? "eligible roles in scope" : esc(check.phase || "reading"))}
      ${card("Groups", r.groups.length, `${granting} give them a role in scope`)}
    </div>
    ${r.notes.length ? `<div class="notice partial"><ul>${r.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
    <div class="table-tools"><input id="user-filter" type="search" placeholder="Filter roles by role, scope or group, e.g. reader prod" value="${esc(check.filter || "")}">
      <label class="inline" title="How the subscription headings below are ordered">Order <select id="user-order">
        <option value="name" ${most ? "" : "selected"}>by subscription name</option><option value="most" ${most ? "selected" : ""}>most roles first</option></select></label></div>
    <h3>Azure roles they have now <span class="muted">${filter ? `${active.length} of ${r.active.length}` : active.length}</span></h3>
    <p class="muted">Standing assignments and PIM roles they've activated, given to them directly or through a group.</p>
    ${active.length ? scopeGroupsHtml("user-active", active, { filtering: !!filter, most, columns: ["Role", "Scope", "Type", "Via group"],
      cells: (row) => [esc(row.role), scopeCell(row), esc(scopeType(row.scope_type)), via(row)] })
      : '<p class="muted">None in scope.</p>'}
    <h3>Azure roles they can activate <span class="muted">${r.done ? (filter ? `${eligible.length} of ${r.eligible.length}` : eligible.length) : "reading…"}</span></h3>
    ${!r.done ? loading(`${check.phase || "Reading"}…`) : eligible.length ? scopeGroupsHtml("user-eligible", eligible, { filtering: !!filter, most,
      columns: ["Role", "Scope", "Type", "Via group", "Eligible until"],
      cells: (row) => [esc(row.role), scopeCell(row), esc(scopeType(row.scope_type)), via(row),
        row.end ? esc(new Date(row.end).toLocaleDateString()) : '<span class="muted">permanent</span>'] })
      : '<p class="muted">None in scope.</p>'}
    <h3>Groups</h3>
    <div class="ov-cards"><div class="card ov-card"><h4>Groups they're in <span class="muted">(${groupFilter ? `${groups.length} of ` : ""}${r.groups.length})</span></h4>
      <p class="muted">The ones that give them a role in scope first, with how many.</p>
      <input id="user-groups-filter" type="search" placeholder="Filter groups" value="${esc(check.groupFilter || "")}">
      <div id="user-groups-body">${starListHtml("user-groups", groups, (g) => `<div class="star-row"><span class="name">${esc(g.name)}</span>
        ${g.roles ? `<span class="muted">${g.roles} role(s) in scope</span>` : ""}</div>`, "No group matches.")}</div>
    </div></div>`;
}

const USER_ORDERS = ["name", "most"];
$("user-access").addEventListener("change", (event) => {
  if (event.target.id !== "user-order") return;
  stored.set("ahc.userOrder", event.target.value);
  renderUserCheck();
});
$("user-access").addEventListener("input", (event) => {
  const field = { "user-filter": "filter", "user-groups-filter": "groupFilter" }[event.target.id];
  if (!field) return;
  state.userCheck[field] = event.target.value;
  const id = event.target.id, at = event.target.selectionStart;
  renderUserCheck();
  $(id).focus();
  $(id).setSelectionRange(at, at);
});
$("user-access").addEventListener("click", (event) => {
  const el = event.target.closest("button");
  if (!el || !state.userCheck) return;
  if (el.dataset.more) { showMore(el); renderUserCheck(); return; }
  const user = state.userCheck.user;
  if (el.dataset.userClose !== undefined) {
    ++state.userCheckSeq;
    state.userCheck = null;
    $("user-search").value = "";
    renderUserCheck();
  } else if (el.dataset.userAgain !== undefined) checkUser(user, { again: true });
  else if (el.dataset.userForce !== undefined) checkUser(user, { force: true });
  else if (el.dataset.userCancel !== undefined && state.userCheck.jobId) {
    api(`api/jobs/${state.userCheck.jobId}/cancel`, { method: "POST", body: {} }).catch(() => {});
  }
});

