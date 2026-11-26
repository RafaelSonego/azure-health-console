"use strict";

// ------------------------------------------------------------------ security
// Sub-tabs: Exposure & configuration (core/security.py, one read for the scope) and Vulnerabilities (Defender's CVEs).

// Browse's chip for a resource open to all networks.
const exposureChip = (e) => (e ? `<span class="exp-chip ${esc(e.level)}" title="${esc(e.what)}">public</span>` : "");

const SEC_SUBS = ["config", "vulnerabilities", "audit"];
const SEC_TONE = { warning: "warning", info: "info", restricted: "healthy" };
state.secSub = stored.choice("ahc.secSub", SEC_SUBS, "config");
state.secCategories = new Set();
state.secSeverities = new Set(["warning", "info"]);
state.secGroup = stored.choice("ahc.secGroup", ["resource", "finding"], "resource");
const SEC_ORDER = ["warning", "info", "restricted"];

function showSecSub(sub) {
  state.secSub = sub;
  stored.set("ahc.secSub", sub);
  for (const b of $("sec-subtabs").querySelectorAll("[data-sec-sub]")) b.classList.toggle("active", b.dataset.secSub === sub);
  $("sec-config").classList.toggle("hidden", sub !== "config");
  $("sec-vulns").classList.toggle("hidden", sub !== "vulnerabilities");
  $("sec-audit").classList.toggle("hidden", sub !== "audit");
  if (sub === "audit" && state.auditLoaded !== JSON.stringify(currentScope())) loadAudit();
  if (sub === "config" && state.secLoaded !== JSON.stringify(currentScope())) loadSecurity();
  if (sub === "vulnerabilities" && state.vulnLoaded !== JSON.stringify(currentScope())) loadVulnerabilities();
}

// ------------------------------------------------------------------ access audit
// People who can do more than read without PIM in the scope (Security → Access audit): one job, filtered on the page.
const AUDIT_ORDER = ["manage_access", "change_all", "credentials", "data_write", "data_read", "change_some", "unknown"];
const AUDIT_TONES = { manage_access: "critical", change_all: "critical", credentials: "critical", data_write: "critical",
  data_read: "warning", change_some: "warning", unknown: "unknown" };
const AUDIT_SECONDS_PER_SUB = 0.85;  // measured: 30 subscriptions in about 25 s
state.auditLevels = new Set();

async function loadAudit(options = {}) {
  const scope = currentScope();
  state.auditLoaded = JSON.stringify(scope);
  const token = (state.auditToken = (state.auditToken || 0) + 1);
  state.audit = null;
  $("audit-counters").innerHTML = $("audit-notes").innerHTML = $("audit-saved").innerHTML = $("audit-meta").textContent = "";
  if (!scope.subscriptions.length) {
    $("audit-body").innerHTML = pickScopeHtml("The access audit reads the role assignments of the subscriptions in scope.");
    return;
  }
  $("audit-body").innerHTML = loading("Reading role assignments…");
  try {
    const started = await api("api/security/access-audit", { method: "POST", body: { subscriptions: scope.subscriptions,
      again: !!options.again, force: !!options.force, ...(scope.groups ? { groups: scope.groups, filtered: true } : {}) } });
    if (token !== state.auditToken) return;
    if (started.too_many) {
      $("audit-body").innerHTML = `<div class="notice partial">${fmt(started.too_many)} subscriptions are in scope, more than
        ${fmt(started.limit)}: the audit reads two lists in each, about ${Math.max(1, Math.round(started.too_many * AUDIT_SECONDS_PER_SUB / 60))}
        minute(s). <button type="button" data-audit-force>Run it anyway</button> or narrow the scope.</div>`;
      return;
    }
    const job = await pollJob(started.job_id, { isCurrent: () => token === state.auditToken, every: 800,
      onRunning: (j) => { $("audit-body").innerHTML = loading(`${j.phase || "Reading"}…`); } });
    if (!job) return;
    if (job.status !== "done") { $("audit-body").innerHTML = '<p class="muted">Stopped.</p>'; return; }
    state.audit = { jobId: started.job_id, result: job.result, at: started.saved_at ? new Date(started.saved_at) : new Date(),
      saved: Boolean(started.saved_at) };
    fillAuditPicks();
    renderAudit();
  } catch (error) {
    if (token === state.auditToken && !error.signedOut) $("audit-body").innerHTML = noticeHtml(error);
  }
}

function auditPeople() {
  const people = state.audit.result.people;
  const text = $("audit-filter").value.trim().toLowerCase();
  const role = $("audit-role").value, group = $("audit-group").value, type = $("audit-type").value;
  const direct = $("audit-direct").checked, eligible = $("audit-eligible").checked, guests = $("audit-guests").checked;
  return people.map((p, i) => ({ ...p, index: i, items: p.items.filter((it) => (!direct || !it.via) && (!eligible || it.eligible_too)
      && (!role || it.role === role) && (!group || it.via === group) && (!type || it.type_label === type)) }))
    .filter((p) => p.items.length && (!state.auditLevels.size || state.auditLevels.has(p.level)) && (!guests || p.guest)
      && (!text || `${p.name} ${p.upn}`.toLowerCase().includes(text)));
}

// The Role and Group lists: what the results hold, most people first, keeping a pick that's still there.
function fillAuditPicks() {
  const count = (key) => {
    const people = new Map();
    for (const p of state.audit.result.people) for (const it of p.items) if (it[key]) people.set(it[key], (people.get(it[key]) || new Set()).add(p.id));
    return [...people].map(([name, ids]) => [name, ids.size]).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  };
  for (const [id, key, all] of [["audit-role", "role", "all roles"], ["audit-group", "via", "all groups"], ["audit-type", "type_label", "all types"]]) {
    const select = $(id), chosen = select.value, options = count(key);
    select.innerHTML = `<option value="">${all}</option>${options.map(([name, n]) =>
      `<option value="${esc(name)}" ${name === chosen ? "selected" : ""}>${esc(name)} (${fmt(n)})</option>`).join("")}`;
  }
}

function auditItemsHtml(items, labels) {
  return `<table class="data-table"><thead><tr><th>Role</th><th>Can</th><th>Where</th><th>How</th><th>Until</th><th title="They hold this role standing and can also activate the same role through PIM, so the standing assignment can be removed">Could use PIM instead</th></tr></thead><tbody>
    ${items.map((it) => `<tr><td>${esc(it.role)}</td><td>${pillHtml(AUDIT_TONES[it.level], labels[it.level])}
        ${it.note ? `<div class="muted">${esc(it.note)}</div>` : ""}
        ${it.condition ? `<details class="more"><summary>with a condition</summary><code class="audit-condition">${esc(it.condition)}</code>
          <div class="muted">A condition can limit what this role does (e.g. which roles it may assign): read it before deciding.</div></details>` : ""}</td>
      <td>${esc(it.scope_label)} ${portalLinkHtml(it.scope, { page: "users", title: "Its Access control (IAM) in the Azure portal" })}<div class="muted">${esc(it.type_label)}</div></td>
      <td>${it.via ? `via group <strong>${esc(it.via)}</strong>` : "assigned directly"}</td>
      <td>${it.until ? `active until ${esc(new Date(it.until).toLocaleDateString())}`
        : it.pim_unknown ? '<span class="muted" title="Neither the subscription nor this resource group lets you read Azure\'s PIM records">standing, or a PIM activation (can\'t tell here)</span>'
        : '<span class="muted">standing</span>'}</td>
      <td>${it.eligible_too ? '<span class="worse">yes: they can also activate this role through PIM, so this standing assignment can be removed</span>' : ""}</td></tr>`).join("")}</tbody></table>`;
}

function renderAudit() {
  const r = state.audit.result, s = r.summary, labels = r.labels;
  const at = state.audit.at, when = at.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  $("audit-meta").textContent = `${currentScope().label} · ${s.subscriptions} subscription(s) · ${fmt(s.assignments)} role assignment(s) · read ${when}`;
  // A saved result is a snapshot: access may have changed since, so its age is said above everything else.
  $("audit-saved").innerHTML = state.audit.saved ? `<div class="notice partial">Saved result from <strong>${esc(when)}</strong>
    (${esc(ago(at.toISOString()))}), kept on this machine. Access added or removed since isn't in it:
    <button type="button" data-audit-again>Read it again from Azure</button></div>` : "";
  $("audit-counters").innerHTML = AUDIT_ORDER.filter((k) => s.levels[k]).map((k) => `
    <button type="button" class="counter ${AUDIT_TONES[k]} ${state.auditLevels.has(k) ? "active" : ""}" data-audit-level="${k}"
      aria-pressed="${state.auditLevels.has(k)}" title="Show only the people whose widest access is this"><span class="n">${fmt(s.levels[k])}</span>
      <span class="label">${esc(labels[k])}</span></button>`).join("")
    + `<div class="counter ${s.eligible_too ? "warning" : "healthy"}"><span class="n">${fmt(s.eligible_too)}</span><span class="label">could use PIM instead</span></div>`
    + (s.guests ? `<div class="counter warning"><span class="n">${fmt(s.guests)}</span><span class="label">guest(s)</span></div>` : "");
  const people = auditPeople();
  $("audit-body").innerHTML = !s.people ? '<p class="ok">✓ Nobody in scope can do more than read without PIM.</p>'
    : !people.length ? '<p class="muted">Nobody matches the filters.</p>'
    : `<p class="muted">${fmt(people.length)} of ${fmt(s.people)} people. Widest access first; Show lists each role, where it applies and how they got it.</p>
    <table class="data-table" data-list="audit-people" data-nofilter><thead><tr><th>Person</th><th>Widest access</th><th>Roles</th><th>On</th>
      <th>How</th><th title="They hold this role standing and can also activate the same role through PIM, so the standing assignment can be removed">Could use PIM instead</th><th data-nosort></th></tr></thead><tbody>${people.map((p) => {
      const groups = [...new Set(p.items.filter((it) => it.via).map((it) => it.via))];
      const direct = p.items.filter((it) => !it.via).length;
      const eligible = p.items.filter((it) => it.eligible_too).length;
      return `<tr><td><strong>${esc(p.name || p.id)}</strong>${p.guest ? ' <span class="new-chip">guest</span>' : ""}${p.enabled === false ? ' <span class="muted">(disabled)</span>' : ""}
          <div class="muted">${esc(p.upn || "")}</div></td>
        <td>${pillHtml(AUDIT_TONES[p.level], labels[p.level], { rank: AUDIT_ORDER.indexOf(p.level) })}</td>
        <td data-v="${p.items.length}">${fmt(p.items.length)} <span class="muted">${esc([...new Set(p.items.map((it) => it.role))].slice(0, 3).join(", "))}</span></td>
        <td class="muted">${esc([...new Set(p.items.map((it) => it.type_label))].slice(0, 3).join(", "))}</td>
        <td>${direct ? `${fmt(direct)} direct` : ""}${direct && groups.length ? " · " : ""}${groups.length ? `via ${esc(groups.slice(0, 2).join(", "))}${groups.length > 2 ? ` +${groups.length - 2}` : ""}` : ""}</td>
        <td data-v="${eligible}">${eligible ? `<span class="worse">${fmt(eligible)} role(s)</span>` : ""}</td>
        <td><button type="button" class="link" data-audit-person="${p.index}">Show</button></td></tr>
      <tr class="hidden" data-attached data-audit-detail="${p.index}"><td colspan="7"></td></tr>`;
    }).join("")}</tbody></table>`;
  const orphaned = r.orphaned || [];
  $("audit-notes").innerHTML = (orphaned.length ? `<details class="more audit-orphaned"><summary><strong>${fmt(orphaned.length)} assignment(s)
      to deleted users or groups</strong> <span class="muted">give nobody access; worth removing</span></summary>
      <table class="data-table" data-list="audit-orphaned"><thead><tr><th>Deleted</th><th>Role</th><th>Can</th><th>Where</th></tr></thead><tbody>
      ${orphaned.map((o) => `<tr><td><code>${esc(o.principal_id)}</code><div class="muted">${esc(o.principal_type)}</div></td><td>${esc(o.role)}</td>
        <td>${pillHtml(AUDIT_TONES[o.level], labels[o.level])}</td>
        <td>${esc(o.scope_label)} ${portalLinkHtml(o.scope, { page: "users", title: "Its Access control (IAM) in the Azure portal" })}<div class="muted">${esc(o.type_label)}</div></td></tr>`).join("")}
      </tbody></table></details>` : "")
    + (r.notes.length ? `<h4>Not shown or not checked</h4>${noticesHtml(r.notes)}` : "");
}

$("audit-body").addEventListener("click", (event) => {
  if (event.target.closest("[data-audit-force]")) { loadAudit({ force: true, again: true }); return; }
  const button = event.target.closest("[data-audit-person]");
  if (!button) return;
  const index = Number(button.dataset.auditPerson), row = $("audit-body").querySelector(`[data-audit-detail="${index}"]`);
  const open = row.classList.toggle("hidden") === false;
  button.textContent = open ? "Hide" : "Show";
  if (open) {
    const person = auditPeople().find((p) => p.index === index);
    row.cells[0].innerHTML = auditItemsHtml(person ? person.items : [], state.audit.result.labels);
  }
});
$("audit-counters").addEventListener("click", (event) => {
  const tile = event.target.closest("[data-audit-level]");
  if (!tile || !state.audit) return;
  const key = tile.dataset.auditLevel;
  if (state.auditLevels.has(key)) state.auditLevels.delete(key); else state.auditLevels.add(key);
  renderAudit();
});
$("audit-filter").addEventListener("input", debounce(() => state.audit && renderAudit(), 200));
for (const id of ["audit-direct", "audit-eligible", "audit-guests", "audit-role", "audit-group", "audit-type"]) {
  $(id).addEventListener("change", () => state.audit && renderAudit());
}
$("audit-refresh").addEventListener("click", () => freshly(() => loadAudit({ again: true })));
$("audit-saved").addEventListener("click", (event) => {
  if (event.target.closest("[data-audit-again]")) freshly(() => loadAudit({ again: true }));
});
for (const fmtName of ["md", "csv", "pdf"]) {
  $(`audit-export-${fmtName}`).addEventListener("click", async () => {
    if (!state.audit) return;
    try { await download(`api/security/access-audit/${state.audit.jobId}/export`, { format: fmtName }, `access-audit.${fmtName}`); }
    catch (error) { showMessage(error.message, true); }
  });
}

async function loadSecurity() {
  const scope = currentScope();
  state.secLoaded = JSON.stringify(scope);
  const token = (state.secToken = (state.secToken || 0) + 1);
  if (!scope.subscriptions.length) {
    $("sec-counters").innerHTML = "";
    $("sec-body").innerHTML = pickScopeHtml("Security reads the settings of every resource in scope.");
    return;
  }
  $("sec-counters").innerHTML = "";
  $("sec-body").innerHTML = loading("Reading settings, NSG rules, public IPs and recent changes…");
  try {
    const data = await api("api/security", { params: scopeParams(scope) });
    if (token !== state.secToken) return;
    state.security = data;
    $("sec-meta").innerHTML = `${esc(scope.label)} · ${asOfHtml()}`;
    renderSecurity();
  } catch (error) {
    if (token === state.secToken) { state.secLoaded = ""; $("sec-body").innerHTML = noticeHtml(error); }
  }
}

// Security's area tiles (findings, then how many are warnings), drawn the same on Security and on Summary.
function securityTilesHtml(data, active = new Set()) {
  return data.categories.map(({ key, label }) => {
    const c = data.counts[key];
    const tone = c.warning ? "warning" : c.info ? "info" : "healthy";
    const on = active.has(key);
    return `<button type="button" class="counter ${tone} ${on ? "active" : ""}" data-sec-category="${key}" aria-pressed="${on}"
      title="Show only ${esc(label)} on Security (click again there to show everything)"><span class="n">${c.warning + c.info}</span>
      <span class="label">${esc(label)}</span>${c.warning ? `<br><span class="label">${c.warning} warning${c.warning === 1 ? "" : "s"}</span>` : ""}</button>`;
  }).join("");
}

function renderSecurity() {
  const data = state.security;
  if (!data) return;
  const recent = $("sec-recent").checked;
  const isRecent = (r) => r.created_at || r.changed_at;
  $("sec-counters").innerHTML = securityTilesHtml(data, state.secCategories);
  const text = $("sec-filter").value.trim().toLowerCase();
  const rows = data.rows.filter((r) => state.secSeverities.has(r.severity)
    && (!state.secCategories.size || state.secCategories.has(r.category)) && (!recent || isRecent(r))
    && (!text || [r.name, r.display_type, r.resource_group, r.what, subName(r.subscription_id)].some((v) => (v || "").toLowerCase().includes(text))));
  const label = Object.fromEntries(data.categories.map((c) => [c.key, c.label]));
  const who = (at, by, verb) => (at ? `<div>${verb} ${esc(dayTime(at))}</div><div class="muted">${esc(by || "unknown")}</div>` : "");
  const changed = (r) => (who(r.created_at, r.created_by, "Created") + who(r.changed_at, r.changed_by, "Setting changed")) || '<span class="muted">–</span>';
  const resourceCell = (r) => `<button type="button" class="link" data-open="${esc(JSON.stringify({ kind: "resource", id: r.id, name: r.name }))}"
    title="Open it in Browse"><strong>${esc(r.name)}</strong></button> ${portalLinkHtml(r.id)}<div class="muted">${esc(r.display_type)}</div>`;
  const whereCell = (r) => `${esc(r.resource_group)}<div class="muted">${esc(subName(r.subscription_id))}${r.location ? ` · ${esc(r.location)}` : ""}</div>`;
  const sevPill = (severity) => pillHtml(SEC_TONE[severity], severity, { rank: SEC_ORDER.indexOf(severity) });
  for (const b of $("sec-config").querySelectorAll("[data-sec-group]")) b.classList.toggle("active", b.dataset.secGroup === state.secGroup);
  let listHtml;
  if (state.secGroup === "resource") {
    const byId = new Map();
    for (const r of rows) {
      const key = r.id.toLowerCase();
      if (!byId.has(key)) byId.set(key, { ...r, findings: [] });
      byId.get(key).findings.push(r);
    }
    const groups = [...byId.values()].map((g) => ({ ...g, severity: SEC_ORDER[Math.min(...g.findings.map((f) => SEC_ORDER.indexOf(f.severity)))] }));
    groups.sort((a, b) => SEC_ORDER.indexOf(a.severity) - SEC_ORDER.indexOf(b.severity) || b.findings.length - a.findings.length
      || a.name.localeCompare(b.name));
    const tally = (g) => SEC_ORDER.map((s) => [s, g.findings.filter((f) => f.severity === s).length]).filter(([, n]) => n)
      .map(([s, n]) => `<span class="sec-dot ${s}"></span>${n} ${s}`).join(" &nbsp;");
    const types = new Set(groups.map((g) => g.display_type)).size;
    const resourceTable = (part) => `<table class="data-table" data-list="security-resources"><thead><tr><th></th><th>Resource</th><th>Where</th><th>Areas</th><th>Findings</th>
        <th>Created / changed (last ${data.change_days} days)</th></tr></thead><tbody>${part.map((g) => `
        <tr class="clickable" data-sec-row><td>${sevPill(g.severity)}</td><td>${resourceCell(g)}</td><td>${whereCell(g)}</td>
          <td>${esc([...new Set(g.findings.map((f) => label[f.category] || f.category))].join(", "))}</td>
          <td data-v="${g.findings.length}"><span class="sec-caret" aria-hidden="true">▸</span> ${tally(g)}</td><td>${changed(g)}</td></tr>
        <tr class="vuln-detail hidden" data-attached><td colspan="6"><ul class="sec-findings">${g.findings
          .sort((a, b) => SEC_ORDER.indexOf(a.severity) - SEC_ORDER.indexOf(b.severity)).map((f) =>
          `<li><span class="sec-dot ${esc(f.severity)}" title="${esc(f.severity)}"></span>${esc(f.what)}
            <span class="muted">· ${esc(label[f.category] || f.category)}</span>${f.action ? `<div class="muted">${esc(f.action)}</div>` : ""}</li>`).join("")}</ul></td></tr>`).join("")}
      </tbody></table>`;
    const warned = (part) => part.filter((g) => g.severity === "warning").length;
    listHtml = groups.length ? `<div class="table-tools"><span class="muted">${groups.length.toLocaleString()} resource(s) of ${types} type(s),
        ${rows.length.toLocaleString()} finding(s). Click a row to see its findings.</span><span class="spacer"></span>
        <button type="button" class="link" data-sec-expand="1">Expand all</button> · <button type="button" class="link" data-sec-expand="0">Collapse all</button></div>
      ${groupedHtml("security-types", groups, {
        groupOf: (g) => ({ key: (g.display_type || "").toLowerCase(), label: g.display_type || "Other" }),
        order: (a, b) => (warned(b.rows) > 0) - (warned(a.rows) > 0) || b.rows.length - a.rows.length || byName(a.label, b.label),
        note: (part) => (warned(part) ? ` <span class="sec-dot warning"></span><span class="muted">${warned(part)} with warnings</span>` : ""),
        body: resourceTable, filtering: !!text })}` : "";
  } else {
    listHtml = rows.length ? table(["", "Resource", "Where", "Area", "Finding", "What to do", `Created / changed (last ${data.change_days} days)`],
      rows.map((r) => [sevPill(r.severity), resourceCell(r), whereCell(r), esc(label[r.category] || r.category), esc(r.what), esc(r.action || ""),
        changed(r)]), "security") : "";
  }
  $("sec-body").innerHTML = `${data.truncated ? errorHtml(`A read stopped at ${data.max_rows.toLocaleString()} rows: narrow the scope to see the rest.`) : ""}
    ${(data.risky_ports || []).length ? `<p class="muted rule">Ports that are a warning when open to the internet: ${esc(data.risky_ports.join(", "))} · ${settingLink("security.risky_ports", "change")}</p>` : ""}
    ${(data.errors || []).map((e) => noticeHtml(`Couldn't read ${e}`)).join("")}
    ${listHtml || `<p class="ok">✓ Nothing ${recent ? "created or changed in the last 14 days " : ""}matches what's picked.</p>`}
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(data)}</details>`;
}

$("sec-subtabs").addEventListener("click", (event) => {
  const b = event.target.closest("[data-sec-sub]");
  if (b) showSecSub(b.dataset.secSub);
});
$("sec-counters").addEventListener("click", (event) => {
  const tile = event.target.closest("[data-sec-category]");
  if (!tile) return;
  const key = tile.dataset.secCategory;
  if (state.secCategories.has(key)) state.secCategories.clear(); else state.secCategories = new Set([key]);
  renderSecurity();
});
$("sec-config").addEventListener("change", (event) => {
  if (event.target.matches("[data-sec-sev]")) {
    if (event.target.checked) state.secSeverities.add(event.target.value); else state.secSeverities.delete(event.target.value);
  }
  if (event.target.matches("[data-sec-sev], #sec-recent")) renderSecurity();
});
$("sec-filter").addEventListener("input", () => renderSecurity());
for (const b of $("sec-config").querySelectorAll("[data-sec-group]")) b.classList.toggle("active", b.dataset.secGroup === state.secGroup);
$("sec-config").addEventListener("click", (event) => {
  const b = event.target.closest("[data-sec-group]");
  if (!b) return;
  state.secGroup = b.dataset.secGroup;
  stored.set("ahc.secGroup", state.secGroup);
  renderSecurity();
});
$("sec-body").addEventListener("click", (event) => {
  const open = event.target.closest("[data-open]");
  if (open) { startSnapshot(JSON.parse(open.dataset.open)); return; }
  const more = event.target.closest("[data-more]");
  if (more) { showMore(more); renderSecurity(); return; }
  const all = event.target.closest("[data-sec-expand]");
  if (all) {
    const open = all.dataset.secExpand === "1";
    for (const row of $("sec-body").querySelectorAll("[data-sec-row]")) toggleSecRow(row, open);
    for (const heading of $("sec-body").querySelectorAll("details.scope-group")) heading.open = open;
    return;
  }
  const row = event.target.closest("[data-sec-row]");
  if (row) toggleSecRow(row);
});

function toggleSecRow(row, open = null) {
  const detail = row.nextElementSibling;
  if (!detail || !("attached" in detail.dataset)) return;
  const show = open ?? detail.classList.contains("hidden");
  detail.classList.toggle("hidden", !show);
  row.querySelector(".sec-caret").textContent = show ? "▾" : "▸";
}
$("sec-refresh").addEventListener("click", () => freshly(loadSecurity));

async function loadNetwork() {
  const scope = currentScope();
  state.networkLoaded = true;
  const token = (state.networkToken = (state.networkToken || 0) + 1);
  if (!scope.subscriptions.length) {
    $("network-body").innerHTML = pickScopeHtml("Network reads the VNets and subnets in scope.");
    return;
  }
  $("network-body").innerHTML = loading("Reading VNets and subnets…");
  try {
    const data = await api("api/network", { params: scopeParams(scope) });
    if (token !== state.networkToken) return;
    state.network = data;
    $("network-meta").innerHTML = `${data.vnets.length} VNet(s) in ${esc(scope.label)} · ${asOfHtml()} · subnets turn yellow at ${data.warn_percent}% and red at ${data.critical_percent}% of usable IPs in use`;
    renderNetwork();
  } catch (error) {
    if (token === state.networkToken) $("network-body").innerHTML = noticeHtml(error);
  }
}

function usageBar(percent, status) {
  if (percent == null) return '<span class="muted">–</span>';
  return `<div class="usage" title="${percent}%"><span class="used ${status === "critical" || status === "warning" ? status : ""}"
    style="width:${Math.min(100, percent)}%"></span></div>`;
}

function renderNetwork() {
  const data = state.network;
  if (!data) return;
  const text = $("network-filter").value.trim().toLowerCase();
  const rows = data.vnets.filter((v) => !text || [v.name, v.resource_group, subName(v.subscription_id), v.spaces.join(" ")]
    .some((x) => (x || "").toLowerCase().includes(text)));
  const note = data.usage_reported ? "" : `<p class="notice">Resource Graph didn't report IP usage for these subnets, so only
    address space is shown here. Open a VNet for Azure's own per-subnet usage.</p>`;
  $("network-body").innerHTML = note + (rows.length ? `<table class="data-table"><thead><tr><th>Status</th><th>VNet</th>
      <th>Subscription</th><th>Resource group</th><th>Address space</th><th>Allocated to subnets</th><th class="num">Subnets</th>
      <th>Fullest subnets (IPs in use)</th></tr></thead><tbody>${rows.map((v) => `
    <tr class="clickable ${state.vnetId === v.id ? "selected" : ""}" data-vnet="${esc(v.id)}">
      <td>${pill(v.status)}</td><td>${esc(v.name)}</td><td>${esc(subName(v.subscription_id))}</td><td>${esc(v.resource_group)}</td>
      <td>${esc(v.spaces.join(", "))}</td>
      <td>${usageBar(v.allocated_percent)} <span class="muted">${v.allocated_percent ?? "–"}%</span></td>
      <td class="num">${v.subnets}</td>
      <td>${v.fullest.map((f) => `${esc(f.name)} ${pillHtml(f.status, `${f.percent}%`)}`).join(" ") || '<span class="muted">–</span>'}</td>
    </tr>`).join("")}</tbody></table>` : '<p class="muted">No VNets in this scope.</p>');
}

async function loadVnet(id) {
  state.vnetId = id;
  renderNetwork();
  const box = $("vnet-detail");
  box.innerHTML = loading("Reading the VNet and Azure's subnet usage…");
  box.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const v = await api("api/network/vnet", { params: { id } });
    if (state.vnetId !== id) return;
    renderVnet(v);
  } catch (error) {
    box.innerHTML = noticeHtml(error);
  }
}

function spaceBar(space, subnets) {
  const status = Object.fromEntries(subnets.map((s) => [s.name, s.status]));
  const start = (cidr) => cidr.split("/")[0];
  const base = ipToNumber(start(space.cidr));
  const size = space.total;
  const blocks = space.blocks.map((b) => {
    const offset = ipToNumber(start(b.cidr)) - base;
    const width = 2 ** ((space.cidr.includes(":") ? 128 : 32) - Number(b.cidr.split("/")[1]));
    return `<span class="block ${esc(status[b.subnet] || "")}" style="position:absolute;left:${(100 * offset / size).toFixed(3)}%;width:${Math.max(0.3, 100 * width / size).toFixed(3)}%"
      title="${esc(b.subnet)}: ${esc(b.cidr)}"></span>`;
  }).join("");
  return `<div class="space-bar" style="position:relative">${blocks}</div>`;
}

function ipToNumber(ip) {
  if (ip.includes(":")) return 0;  // IPv6 bars are drawn by order only
  return ip.split(".").reduce((n, part) => n * 256 + Number(part), 0);
}

function renderVnet(v) {
  state.vnet = v;
  const portal = `https://portal.azure.com/#@${state.tenant}/resource${v.id}`;
  // By name until a header is clicked (the shared list helper); CIDRs sort by address, Used by its percentage.
  const subnetRows = [...v.subnets].sort((a, b) => byName(a.name, b.name)).map((s) => `<tr>
    <td>${pill(s.status)}</td><td>${esc(s.name)}</td>
    <td data-v="${s.cidrs[0] ? ipToNumber(s.cidrs[0].split("/")[0]) : ""}">${esc(s.cidrs.join(", "))}</td>
    <td class="num">${s.usable}</td><td class="num">${s.used ?? "–"}</td>
    <td data-v="${s.percent ?? ""}">${s.percent != null ? `${usageBar(s.percent, s.status)} <span class="muted">${s.percent}%</span>` : `<span class="muted">${esc(s.note || "–")}</span>`}</td>
    <td>${esc(s.nsg)}</td><td>${esc(s.route_table)}</td><td>${esc(s.delegations)}</td></tr>`).join("");
  const exportButtons = ["md", "csv", "pdf"].map((fmt) =>
    `<button type="button" data-vnet-export="${fmt}">Export ${fmt === "md" ? "Markdown" : fmt.toUpperCase()}</button>`).join(" ");
  $("vnet-detail").innerHTML = `
    <div class="vnet-head"><h3>${esc(v.name)}</h3><span class="muted">${esc(v.resource_group)} · ${esc(v.location)} ·
      IP usage from ${esc(v.usage_source)}</span><span class="spacer"></span>
      <a href="${safeHref(portal)}" target="_blank" rel="noopener">Open in portal</a> ${exportButtons}</div>
    <h4>Address spaces</h4>
    ${v.spaces.map((sp) => `<div><strong>${esc(sp.cidr)}</strong> <span class="muted">${sp.allocated.toLocaleString()} of
      ${sp.total.toLocaleString()} addresses allocated to subnets (${sp.percent}%) · ${sp.free.toLocaleString()} free · hover a block for its subnet</span>
      ${spaceBar(sp, v.subnets)}</div>`).join("")}
    ${v.outside.length ? `<p class="notice">Subnet prefixes outside every address space: ${esc(v.outside.join(", "))}</p>` : ""}
    <h4>Subnets <span class="muted">(Azure reserves 5 addresses in every IPv4 subnet)</span></h4>
    <table class="data-table" data-list="subnets"><thead><tr><th>Status</th><th>Subnet</th><th>CIDR</th>
      <th class="num">Usable</th><th class="num">In use</th><th>Used</th>
      <th>NSG</th><th>Route table</th><th>Delegations</th></tr></thead><tbody>${subnetRows}</tbody></table>
    <h4>Free ranges</h4>
    ${v.spaces.some((sp) => sp.free_ranges.length) ? `<table class="data-table"><thead><tr><th>Address space</th><th>Free CIDR blocks</th></tr></thead><tbody>
      ${v.spaces.map((sp) => `<tr><td>${esc(sp.cidr)}</td><td>${esc(sp.free_ranges.join(", ") || "none")}</td></tr>`).join("")}</tbody></table>`
      : '<p class="muted">No free address space left.</p>'}
    <h4>Subnet sizes that still fit</h4>
    ${v.sizes.length ? `<table class="data-table"><thead><tr><th>Address space</th><th>Size</th><th>First free</th>
      <th class="num">How many fit</th><th class="num">Usable IPs each</th></tr></thead><tbody>${v.sizes.map((r) => `<tr>
      <td>${esc(r.address_space)}</td><td>${esc(r.prefix)}</td><td>${esc(r.next_cidr)}</td><td class="num">${r.count}</td>
      <td class="num">${r.usable_ips}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">No room for another subnet.</p>'}`;
}

$("network-body").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-vnet]");
  if (row) loadVnet(row.dataset.vnet);
});
$("vnet-detail").addEventListener("click", async (event) => {
  const fmt = event.target.dataset && event.target.dataset.vnetExport;
  if (!fmt || !state.vnetId) return;
  try {
    await download("api/network/vnet/export", { id: state.vnetId, format: fmt }, `vnet.${fmt}`);
  } catch (error) {
    $("vnet-detail").insertAdjacentHTML("afterbegin", noticeHtml(error));
  }
});
$("network-filter").addEventListener("input", debounce(renderNetwork, 200));
$("network-refresh").addEventListener("click", () => freshly(loadNetwork));

