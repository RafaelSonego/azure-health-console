"use strict";

// ------------------------------------------------------------------ your access here
// On a resource, a resource group or a subscription: your roles that apply there (PIM activations first, with time
// left), and the PIM roles you could activate there, so checking access doesn't mean leaving for the Access tab.


function accessHereSlot(id) {
  return `<div class="card ov-card access-here" data-access-here="${esc(id)}"><h4>Your access here</h4>${loading("Reading your roles…")}</div>`;
}

async function fillAccessHere(root = document) {
  for (const box of root.querySelectorAll("[data-access-here]:not([data-filled])")) {
    box.dataset.filled = "1";
    const id = box.dataset.accessHere;
    let data;
    try { data = await api("api/access/here", { params: { id } }); }
    catch (error) { box.innerHTML = `<h4>Your access here</h4>${noticeHtml(error)}`; continue; }
    box.innerHTML = `<h4>Your access here</h4>${accessHereHtml(data)}`;
  }
}

const CAN_LABEL = { read: { all: "read everything", some: "read some things" }, change: { all: "change everything", some: "change some things" } };
const ADDS_LABEL = { read: "read", change: "change", manage_access: "manage access" };

// One line: what your roles add up to here (control plane only), each part naming the roles that give it.
function canHtml(can) {
  if (!can) return "";
  const part = (key, text, yes) => `<span class="can ${yes ? "can-yes" : "can-no"}" title="${esc(yes && can.by[key].length ? `From ${can.by[key].join(", ")}` : "")}">${esc(text)}</span>`;
  const parts = [
    part("read", CAN_LABEL.read[can.read] || "can't read", !!can.read),
    part("change", CAN_LABEL.change[can.change] || "can't change anything", !!can.change),
    part("manage_access", can.manage_access ? "manage access" : "can't manage access", can.manage_access)];
  return `<div class="can-line"><span class="muted">You can</span> ${parts.join('<span class="muted"> · </span>')}</div>
    ${can.known ? "" : '<p class="muted">Some of your roles\' permissions couldn\'t be read, so this may be less than you have.</p>'}`;
}

function accessHereHtml(data) {
  if (data.error) return `<p class="notice partial">Your roles here couldn't be read: ${esc(data.error)}</p>`;
  const active = data.roles.filter((r) => r.pim_until);
  const standing = {};
  for (const r of data.roles.filter((x) => !x.pim_until)) {
    const key = `${r.role}|${r.from}`;
    const entry = standing[key] || (standing[key] = { role: r.role, from: r.from, direct: false, groups: [] });
    if (r.via) entry.groups.push(r.via); else entry.direct = true;
  }
  const rows = Object.values(standing).sort((a, b) => (a.from !== "here") - (b.from !== "here") || a.role.localeCompare(b.role));
  const how = (e) => [e.direct ? "you directly" : "", e.groups.length ? (e.groups.length === 1 ? `group ${e.groups[0]}` : `${e.groups.length} groups`) : ""]
    .filter(Boolean).join(" and ");
  const parts = [canHtml(data.can)];
  if (active.length) {
    parts.push(active.map((r) => {
      const left = timeLeft(r.pim_until);
      return `<div class="ov-row">${pillHtml(left.tone, "active")} <span class="ov-text"><strong>${esc(r.role)}</strong>
        <span class="muted">· PIM, ${esc(left.text)} · from ${esc(r.from)}${r.via ? ` · via ${esc(r.via)}` : ""}</span></span></div>`;
    }).join(""));
  }
  const more = data.eligible.filter((r) => (r.adds || []).length);
  if (more.length) {
    parts.push(`<div class="can-more">${more.map((r) => `<div><strong>Activate ${esc(r.role)}</strong> to ${esc(r.adds.map((a) => ADDS_LABEL[a]).join(" and "))}
      <span class="muted">· PIM, ${esc(r.scope_name)}${r.via ? ` · via ${esc(r.via)}` : ""}</span></div>`).join("")}
      <a href="${safeHref(tenantPortal(data.activate_url))}" target="_blank" rel="noopener">Activate in the portal ↗</a></div>`);
  }
  const roleRow = (e) => `<div class="ov-row"><span class="ov-text"><strong>${esc(e.role)}</strong> <span class="muted">· from ${esc(e.from)} · ${esc(how(e))}</span></span></div>`;
  const others = data.eligible.filter((r) => !(r.adds || []).length);
  if (rows.length || others.length) {
    parts.push(`<details class="access-roles"><summary class="muted">Your ${rows.length} standing role(s)${others.length ? ` and ${others.length} more you could activate` : ""}</summary>
      ${rows.map(roleRow).join("")}
      ${others.length ? `<div class="muted access-standing-head">Eligible (add nothing above)</div>${others.map((r) => `<div class="ov-row"><span class="ov-text">${esc(r.role)}
        <span class="muted">· ${esc(r.scope_name)}</span></span></div>`).join("")}` : ""}
      ${data.more_eligible ? `<p class="muted">${data.more_eligible} more eligible role(s) in the Access tab.</p>` : ""}</details>`);
  } else if (!active.length) {
    parts.push('<p class="muted">No role of yours is assigned here or above it.</p>');
  }
  if (data.pim_error) parts.push(`<p class="muted">PIM couldn't be read: ${esc(data.pim_error)}</p>`);
  return parts.join("");
}

// ------------------------------------------------------------------ vulnerabilities
// Defender for Cloud's findings for the scope (one per vulnerable package), where they run and whether that's reachable,
// what isn't scanned, and platform checks that need no Defender. Read-only, with your own access.

state.vulnView = "resources";

// The filters open on running + has a fix + High (what to act on now); your own choice is kept in this browser.
const VULN_FILTERS = "ahc.vulnFilters";
function saveVulnFilters() {
  stored.set(VULN_FILTERS, { severity: [...document.querySelectorAll("[data-vuln-sev]")].filter((c) => c.checked).map((c) => c.value),
    fixable: $("vuln-fixable").checked, running: $("vuln-running").checked, cvss: $("vuln-cvss").value });
}
function restoreVulnFilters() {
  const kept = stored.json(VULN_FILTERS, null);
  if (!kept) return;
  document.querySelectorAll("[data-vuln-sev]").forEach((c) => { c.checked = (kept.severity || []).includes(c.value); });
  $("vuln-fixable").checked = !!kept.fixable;
  $("vuln-running").checked = !!kept.running;
  if (kept.cvss) $("vuln-cvss").value = kept.cvss;
}
const vulnFiltered = () => $("vuln-fixable").checked || $("vuln-running").checked || Number($("vuln-cvss").value) > 0
  || !!$("vuln-type").value || [...document.querySelectorAll("[data-vuln-sev]")].some((c) => !c.checked);
restoreVulnFilters();

function vulnParams() {
  const scope = currentScope();
  const params = scopeParams(scope);
  const severity = [...document.querySelectorAll("[data-vuln-sev]")].filter((c) => c.checked).map((c) => c.value);
  if (severity.length < 3) params.severity = severity;
  if ($("vuln-fixable").checked) params.fixable = "1";
  if (Number($("vuln-cvss").value)) params.cvss_min = $("vuln-cvss").value;
  if ($("vuln-type").value) params.type = $("vuln-type").value;
  if ($("vuln-running").checked) params.running = "1";
  const q = $("vuln-search").value.trim();
  if (q) params.q = q;
  return params;
}

const CVE_ID = /^CVE-\d{4}-\d{4,}$/i;
const searchedCves = () => $("vuln-search").value.trim().split(/\s+/).filter((t) => CVE_ID.test(t)).map((t) => t.toUpperCase());

const typeLabel = (rtype) => taxonomyLabel(rtype);
function taxonomyLabel(rtype) {
  const known = { "microsoft.compute/virtualmachines": "Virtual machine", "microsoft.containerservice/managedclusters": "AKS cluster",
    "microsoft.containerregistry/registries": "Container registry", "microsoft.compute/virtualmachinescalesets": "VM scale set",
    "microsoft.hybridcompute/machines": "Arc machine" };
  return known[rtype] || rtype;
}

async function loadVulnerabilities(options = {}) {
  const scope = currentScope();
  state.vulnLoaded = JSON.stringify(scope);
  if (!scope.subscriptions.length) {
    $("vuln-summary").innerHTML = "";
    $("vuln-body").innerHTML = pickScopeHtml("Vulnerabilities are read for the subscriptions in scope.");
    $("vuln-platform").innerHTML = $("vuln-coverage").innerHTML = "";
    return;
  }
  const token = (state.vulnToken || 0) + 1;
  state.vulnToken = token;
  $("vuln-meta").textContent = "";
  $("vuln-body").innerHTML = loading("Reading Defender for Cloud's findings…");
  // Filters and search only change the findings; the platform checks and coverage don't depend on them.
  if (options.findingsOnly) return loadVulnFindings(token);
  $("vuln-summary").innerHTML = "";
  $("vuln-platform").innerHTML = loading("Checking AKS versions, node images, runtimes and TLS…");
  $("vuln-coverage").innerHTML = "";
  api("api/vulnerabilities/platform", { params: scopeParams(scope) })
    .then((data) => {
      if (token !== state.vulnToken) return;
      $("vuln-platform").innerHTML = data.cards.map((c) => renderCard(c)).join("")
        + data.cards.filter((c) => c.note).map((c) => `<p class="muted">${esc(c.note)}</p>`).join("");
    })
    .catch((error) => { if (token === state.vulnToken) $("vuln-platform").innerHTML = noticeHtml(error); });
  return loadVulnFindings(token);
}

async function loadVulnFindings(token) {
  try {
    const data = await api("api/vulnerabilities", { params: vulnParams() });
    if (token !== state.vulnToken) return;
    state.vuln = data;
    renderVulnerabilities();
  } catch (error) {
    if (token === state.vulnToken) $("vuln-body").innerHTML = noticeHtml(error);
  }
}

function renderVulnerabilities() {
  const d = state.vuln;
  const s = d.summary;
  const when = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("vuln-meta").textContent = `${currentScope().label} · read ${when} · Defender refreshes findings up to once a day`;
  const typeSelect = $("vuln-type");
  const chosen = typeSelect.value;
  typeSelect.innerHTML = `<option value="">all types</option>${d.types.map((t) => `<option value="${esc(t.rtype)}" ${t.rtype === chosen ? "selected" : ""}>${esc(typeLabel(t.rtype))} (${t.findings})</option>`).join("")}`;
  const pct = (n) => (s.findings ? Math.round((100 * n) / s.findings) : 0);
  const noDefender = !s.findings && ["off", "unknown"].includes(d.coverage.defender);
  $("vuln-summary").innerHTML = noDefender ? "" : `
    <div class="card ov-card"><span class="muted ov-kicker">Open findings</span><strong class="big">${fmt(s.findings)}</strong>
      <span>${sevCounts({ high: s.high, medium: s.medium, low: s.low }, true)}</span></div>
    <div class="card ov-card"><span class="muted ov-kicker">Resources affected</span><strong class="big">${fmt(s.resources)}</strong>
      <span class="muted">worst CVSS ${s.maxCvss != null ? fmt(s.maxCvss) : "–"}</span></div>
    <div class="card ov-card"><span class="muted ov-kicker">With a fix available</span><strong class="big">${pct(s.fixable)}%</strong>
      <span class="muted">${fmt(s.fixable)} of ${fmt(s.findings)}: updating the package or image closes them</span></div>
    <div class="card ov-card"><span class="muted ov-kicker">Where they are</span>
      <span>${fmt(s.running)} in containers running in AKS</span><span>${fmt(s.images)} in registry images</span>
      <span class="muted">the rest on VMs and machines</span></div>`;
  renderVulnSearchNote();
  renderVulnTable();
  renderVulnCoverage();
}

// A CVE search answers "where is it and what fixes it" before the table.
function renderVulnSearchNote() {
  const d = state.vuln;
  const q = $("vuln-search").value.trim();
  const cves = searchedCves();
  if (!q) { $("vuln-search-note").innerHTML = ""; return; }
  const s = d.summary;
  if (!s.findings) {
    $("vuln-search-note").innerHTML = `<p class="notice partial">Nothing in scope matches <strong>${esc(q)}</strong>${$("vuln-running").checked ? " among what's running" : ""}.
      ${cves.length ? "Either it isn't present, or Defender doesn't scan where it is (see Not scanned below)." : ""}</p>`;
    return;
  }
  const fixes = [...new Set(d.software.flatMap((r) => r.fixedIn || []))].slice(0, 4);
  const packages = d.software.slice(0, 4).map((r) => r.software).filter(Boolean);
  $("vuln-search-note").innerHTML = `<div class="notice partial vuln-answer"><strong>${esc(cves.length ? cves.join(", ") : q)}</strong>:
    ${fmt(s.findings)} finding(s) on ${fmt(s.resources)} resource(s)${s.running ? `, ${fmt(s.running)} in containers running in AKS` : ""}${s.images ? `, ${fmt(s.images)} in registry images` : ""}.
    ${packages.length ? `In ${packages.map(esc).join(", ")}${d.software.length > 4 ? "…" : ""}.` : ""}
    ${fixes.length ? `Fixed in ${fixes.map((f) => `<code>${esc(f)}</code>`).join(", ")}.` : ""}
    ${cves.length === 1 ? `<a href="https://nvd.nist.gov/vuln/detail/${encodeURIComponent(cves[0])}" target="_blank" rel="noopener">${esc(cves[0])} at NVD</a>` : ""}</div>`;
}

function exposureChips(r) {
  const chips = [];
  if (r.running) chips.push('<span class="scope-chip in">running in AKS</span>');
  else if (r.live) chips.push('<span class="scope-chip in">installed</span>');
  if (r.images) chips.push('<span class="scope-chip out">registry images</span>');
  for (const e of r.exposure || []) chips.push(`<span class="worse-chip" title="Reachable from the internet">${esc(e)}</span>`);
  return chips.join(" ");
}

// Defender's own severity words, coloured like the console's statuses (high red, medium yellow, low grey).
function sevCounts(r, long = false) {
  return [r.high && pillHtml("critical", `${fmt(r.high)} high`), r.medium && pillHtml("warning", `${fmt(r.medium)} ${long ? "medium" : "med"}`),
    r.low && pillHtml("info", `${fmt(r.low)} low`)].filter(Boolean).join(" ");
}

function renderVulnTable() {
  const d = state.vuln;
  document.querySelectorAll("[data-vuln-view]").forEach((b) => b.classList.toggle("active", b.dataset.vulnView === state.vulnView));
  if (!d.summary.findings) {
    const defender = d.coverage.defender;
    $("vuln-body").innerHTML = defender === "off" || defender === "unknown"
      ? `<div class="notice partial"><strong>${defender === "off" ? "Microsoft Defender for Cloud's scanning plans are off in this scope" : "Microsoft Defender for Cloud doesn't answer for this scope"},</strong>
          so there are no vulnerability findings to show: that doesn't mean nothing is vulnerable. The platform checks below
          still work. To get findings, turn on <em>Defender for Servers</em> (VMs) and <em>Defender for Containers</em>
          (registry images and running containers) in Defender for Cloud → Environment settings; that's a paid plan and a
          change the console never makes.${defender === "unknown" ? " (No plan settings came back at all: the Microsoft.Security provider may not be registered in these subscriptions.)" : ""}</div>`
      : `<p class="ok">✓ No open findings in scope${vulnFiltered() ? " with these filters" : ""}.</p>
          ${vulnFiltered() ? '<p class="muted">The filters are on (by default: running, has a fix, High). <button type="button" class="link" data-vuln-all>Show everything</button></p>' : ""}
          <p class="muted">Check "Not scanned" below: no findings only counts where Defender scans.</p>`;
    return;
  }
  if (state.vulnView === "cves") {
    const rows = d.cves || [];
    $("vuln-body").innerHTML = `<p class="muted">One row per CVE: the same CVE in many packages, images or image versions is counted once.</p>
      ${rows.length ? `<table class="data-table" data-list="vuln-cves"><thead><tr><th>CVE</th><th>Severity</th><th>Worst CVSS</th><th>Resources</th>
        <th>Findings</th><th>Packages</th><th>With a fix</th><th>Running</th><th>Images</th></tr></thead><tbody>${rows.map((r) => `<tr>
        <td><a href="https://nvd.nist.gov/vuln/detail/${encodeURIComponent(r.cve)}" target="_blank" rel="noopener">${esc(r.cve)}</a></td>
        <td>${pillHtml(SEVERITY_STATUS[(r.severity || "").toUpperCase()] || "info", r.severity || "unrated")}</td>
        <td>${r.maxCvss != null ? fmt(r.maxCvss) : "–"}</td><td>${fmt(r.resources)}</td><td>${fmt(r.findings)}</td>
        <td>${esc((r.packages || []).join(", "))}</td><td>${fmt(r.fixable)}</td><td>${fmt(r.running)}</td><td>${fmt(r.images)}</td></tr>`).join("")}</tbody></table>`
        : '<p class="muted">No CVE IDs in these findings.</p>'}
      ${d.truncated.cves ? '<p class="muted">The 300 worst CVEs are shown; filter or export for the rest.</p>' : ""}`;
    return;
  }
  if (state.vulnView === "software") {
    $("vuln-body").innerHTML = `<table class="data-table vuln-table" data-list="vuln-software" data-nofilter><thead><tr><th>Software</th><th>Resources</th><th>Findings</th><th>Severity</th>
        <th>Worst CVSS</th><th>Fixed in</th><th>On</th></tr></thead><tbody>${d.software.map((r, i) => `
      <tr class="clickable" data-vuln-software="${i}"><td><strong>${esc(r.software || "(unnamed)")}</strong></td><td>${fmt(r.resources)}</td>
        <td>${fmt(r.findings)}</td><td>${sevCounts(r)}</td><td>${r.maxCvss != null ? fmt(r.maxCvss) : "–"}</td>
        <td>${esc((r.fixedIn || []).join(", ")) || '<span class="muted">see the CVEs</span>'}</td>
        <td class="muted">${esc((r.types || []).map(typeLabel).join(", "))}${r.running ? " · running in AKS" : ""}</td></tr>
      <tr class="vuln-detail hidden" id="vuln-sw-${i}" data-attached><td colspan="7"></td></tr>`).join("")}</tbody></table>
      ${d.truncated.software ? '<p class="muted">The 300 worst are shown; filter or export for the rest.</p>' : ""}`;
    return;
  }
  $("vuln-body").innerHTML = `<p class="muted">Ranked by what to fix first: reachable from the internet, then running (not just an image),
      then fixable, then CVSS.</p>
    <table class="data-table vuln-table" data-list="vuln-resources" data-nofilter><thead><tr><th>Resource</th><th>Findings</th><th>Severity</th><th>Worst CVSS</th>
      <th>With a fix</th><th>Running and reachable</th></tr></thead><tbody>${d.resources.map((r, i) => `
    <tr class="clickable" data-vuln-resource="${i}"><td><strong>${esc(r.name)}</strong> ${portalLinkHtml(r.parent)}
        <div class="muted">${esc(typeLabel(r.rtype))} · ${esc(r.resourceGroup)} · ${esc(subName(r.subscriptionId.toLowerCase()))}</div></td>
      <td>${fmt(r.findings)}${r.children ? `<div class="muted">${fmt(r.children)} container(s) or image(s)</div>` : ""}</td>
      <td>${sevCounts(r)}</td><td>${r.maxCvss != null ? fmt(r.maxCvss) : "–"}</td><td>${fmt(r.fixable)}</td><td>${exposureChips(r)}</td></tr>
    <tr class="vuln-detail hidden" id="vuln-res-${i}" data-attached><td colspan="6"></td></tr>`).join("")}</tbody></table>
    ${d.truncated.resources ? '<p class="muted">The 300 highest-priority resources are shown; filter or export for the rest.</p>' : ""}`;
}

// Where a finding is: this resource, a registry image (repository, digest, and its tags on request) or a container
// running in AKS (and the image:tag it runs, on request). `resourceId` is the registry or cluster.
function findingPlace(f, resourceId) {
  if (!f.child) return '<span class="muted">this resource</span>';
  const img = f.image || {};
  if (img.digest) {
    const short = `${img.digest.slice(0, 19)}…`;
    return `${esc(img.repository || f.child)}<div class="muted">registry image</div>
      <div><code title="${esc(img.digest)}">${esc(short)}</code> <button type="button" class="link" data-copy="${esc(img.digest)}">copy</button></div>
      <div class="image-tags" data-tags-for="${esc(img.digest)}"><button type="button" class="link" data-acr-tags
        data-registry="${esc(resourceId)}" data-repository="${esc(img.repository || f.child)}" data-digest="${esc(img.digest)}">find tags</button></div>`;
  }
  if (img.container) {
    const key = `${img.namespace}/${img.container}`;
    return `${esc(img.container)}<div class="muted">running in AKS · ${esc(img.namespace)} · ${esc(img.kind)}</div>
      <div class="running-image" data-running-key="${esc(key)}"><button type="button" class="link" data-k8s-running
        data-cluster="${esc(resourceId)}">running image</button></div>`;
  }
  return `${esc(f.child)}<div class="muted">${esc(f.where)}</div>`;
}

function vulnFindingsHtml(findings, truncated, resourceId = "") {
  if (!findings.length) return '<p class="muted">No open findings.</p>';
  const wanted = new Set(searchedCves());
  const hit = (f) => wanted.size && f.cves.some((c) => wanted.has(c.id));
  if (wanted.size) findings = [...findings].sort((a, b) => hit(b) - hit(a));
  return `<table class="data-table"><thead><tr><th>Package</th><th>Severity</th><th>CVSS</th><th>Installed</th><th>Fixed in</th><th>On</th><th>CVEs</th></tr></thead>
    <tbody>${findings.map((f) => `<tr class="${hit(f) ? "cve-hit" : ""}"><td><strong>${esc(f.software || f.title)}</strong>${hit(f) ? ' <span class="new-chip">has the CVE</span>' : ""}
        ${f.defender ? `<div><a href="${safeHref(tenantPortal(f.defender))}" target="_blank" rel="noopener" title="This finding in Defender for Cloud">Defender ↗</a></div>` : ""}</td>
      <td>${pillHtml(SEVERITY_STATUS[(f.severity || "").toUpperCase()] || "info", f.severity || "unrated")}</td><td>${f.cvss != null ? fmt(f.cvss) : "–"}</td>
      <td>${esc(f.detected)}</td><td>${esc(f.fixed) || (f.hasFix ? '<span class="muted">see the CVEs</span>' : '<span class="muted">no fix yet</span>')}</td>
      <td>${findingPlace(f, resourceId)}</td>
      <td>${f.cves.length ? `<details ${hit(f) ? "open" : ""}><summary>${f.cves.length} CVE${f.cves.length === 1 ? "" : "s"}</summary>${f.cves.map((c) =>
        `<div class="${wanted.has(c.id) ? "cve-match" : ""}"><a href="https://nvd.nist.gov/vuln/detail/${encodeURIComponent(c.id)}" target="_blank" rel="noopener">${esc(c.id)}</a>
          <span class="muted">${esc(c.severity)}${c.fixed ? ` · fixed in ${esc(c.fixed)}` : c.fix ? ` · ${esc(c.fix)}` : ""}</span></div>`).join("")}</details>` : ""}</td></tr>`).join("")}
    </tbody></table>${truncated ? `<p class="muted">The ${findings.length} worst findings are shown.</p>` : ""}`;
}

$("vuln-body").addEventListener("click", async (event) => {
  const row = event.target.closest("[data-vuln-resource], [data-vuln-software]");
  if (!row || event.target.closest("a, details")) return;
  const isResource = row.dataset.vulnResource !== undefined;
  const index = Number(isResource ? row.dataset.vulnResource : row.dataset.vulnSoftware);
  const detail = $(isResource ? `vuln-res-${index}` : `vuln-sw-${index}`);
  if (!detail.classList.contains("hidden")) { detail.classList.add("hidden"); return; }
  detail.classList.remove("hidden");
  const cell = detail.firstElementChild;
  cell.innerHTML = loading("Reading the findings…");
  try {
    if (isResource) {
      const r = state.vuln.resources[index];
      const data = await exploreGet("vulnerabilities", { id: r.parent });
      cell.innerHTML = `<div class="vuln-open"><button type="button" class="link" data-open-vuln="${esc(r.parent)}" data-name="${esc(r.name)}">Open ${esc(r.name)}</button></div>
        ${vulnFindingsHtml(data.findings, data.truncated, r.parent)}`;
    } else {
      const sw = state.vuln.software[index];
      const rows = await api("api/vulnerabilities/software", { params: { ...vulnParams(), name: sw.software } });
      cell.innerHTML = table(["Resource", "Installed", "Fixed in", "Worst CVSS", "Containers or images"], rows.map((r) => [
        `<button type="button" class="link" data-open-vuln="${esc(r.parent)}" data-name="${esc(r.name)}">${esc(r.name)}</button>
          <div class="muted">${esc(typeLabel(r.rtype))} · ${esc(r.resourceGroup)}</div>`,
        esc((r.detected || []).join(", ")), esc((r.fixed || []).join(", ")), r.maxCvss != null ? fmt(r.maxCvss) : "–",
        esc((r.children || []).join(", "))]));
    }
  } catch (error) {
    cell.innerHTML = noticeHtml(error);
  }
});

document.addEventListener("click", (event) => {
  const open = event.target.closest("[data-open-vuln]");
  if (open) startSnapshot({ kind: "resource", id: open.dataset.openVuln, name: open.dataset.name });
});

function renderVulnCoverage() {
  const c = state.vuln.coverage;
  const parts = [];
  for (const p of c.plans_off) {
    parts.push(`<p class="notice"><strong>${esc(p.label)}</strong> is off in ${p.subscriptions.length} subscription(s) in scope:
      ${esc(p.subscriptions.slice(0, 8).map(subName).join(", "))}${p.subscriptions.length > 8 ? ", …" : ""}. Nothing there is scanned.</p>`);
  }
  if (c.machines.length) {
    parts.push(`<details><summary><strong>${c.machines.length}${c.machines_truncated ? "+" : ""} machine(s)</strong> have no vulnerability
        scanner, so they show no findings whatever they run.</summary>
      ${table(["Machine", "Resource group", "Subscription"], c.machines.map((m) => [
        `<button type="button" class="link" data-open-vuln="${esc(m.id)}" data-name="${esc(m.name)}">${esc(m.name)}</button>`,
        esc(m.resource_group), esc(subName(m.subscription_id))]))}</details>`);
  }
  if (c.defender === "unknown") parts.unshift('<p class="notice">Defender for Cloud\'s plan settings couldn\'t be read for this scope, so what is and isn\'t scanned is unknown.</p>');
  $("vuln-coverage").innerHTML = parts.join("") || '<p class="ok">✓ Defender scans every machine, container and image in scope that it knows about.</p>';
}

$("vuln-platform").addEventListener("click", (event) => {
  const item = event.target.closest("li[data-open]");
  if (item) startSnapshot(JSON.parse(item.dataset.open));
});
for (const el of document.querySelectorAll("[data-vuln-sev], #vuln-fixable, #vuln-cvss, #vuln-type, #vuln-running")) {
  el.addEventListener("change", () => { saveVulnFilters(); loadVulnerabilities({ findingsOnly: true }); });
}
function showAllVulns() {
  document.querySelectorAll("[data-vuln-sev]").forEach((c) => { c.checked = true; });
  $("vuln-fixable").checked = $("vuln-running").checked = false;
  $("vuln-cvss").value = "0";
  $("vuln-type").value = "";
  saveVulnFilters();
  loadVulnerabilities({ findingsOnly: true });
}
$("vuln-all").addEventListener("click", showAllVulns);
$("vuln-body").addEventListener("click", (event) => { if (event.target.closest("[data-vuln-all]")) showAllVulns(); });
$("vuln-search").addEventListener("input", debounce(() => loadVulnerabilities({ findingsOnly: true }), 500));
document.querySelectorAll("[data-vuln-view]").forEach((b) => b.addEventListener("click", () => {
  state.vulnView = b.dataset.vulnView;
  if (state.vuln) renderVulnTable();
}));
$("vuln-refresh").addEventListener("click", () => freshly(() => loadVulnerabilities()));
for (const fmtName of ["md", "csv", "pdf"]) {
  $(`vuln-export-${fmtName}`).addEventListener("click", async () => {
    try { await download("api/vulnerabilities/export", { ...vulnParams(), format: fmtName }, `vulnerabilities.${fmtName}`); }
    catch (error) { showMessage(error.message, true); }
  });
}

// A refusal is an answer: say what was refused, the role that opens it, and any PIM role that would (#38).
function accessAnswer(data, targetId) {
  const said = data.required && (data.error || "").includes(data.required);
  const fix = data.kind === "network" || !data.required || said ? "" : `<div class="muted">Needs: ${esc(data.required)}.</div>`;
  return `<div class="notice partial">${esc(data.error)}${fix}</div>${data.kind === "access" && targetId ? pimSlot(targetId, data.required) : ""}`;
}

document.addEventListener("click", async (event) => {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    await copyText(copy.dataset.copy, copy, "copied");
    return;
  }
  const tagsButton = event.target.closest("[data-acr-tags]");
  if (tagsButton) {
    const d = tagsButton.dataset;
    const box = tagsButton.parentElement;
    box.innerHTML = loading("Asking the registry…");
    try {
      const data = await api("api/vulnerabilities/image-tags", { params: { registry: d.registry, repository: d.repository, digest: d.digest } });
      if (data.error) { box.innerHTML = accessAnswer(data, d.registry); fillPimHints(box); return; }
      box.innerHTML = data.tags.length
        ? `tags: ${data.tags.map((t) => `<code>${esc(t)}</code>`).join(" ")}${data.created ? ` <span class="muted">· pushed ${esc(new Date(data.created).toLocaleDateString())}</span>` : ""}`
        : '<span class="muted">no tags point at this image any more (only its digest)</span>';
    } catch (error) { box.innerHTML = noticeHtml(error); }
    return;
  }
  const runningButton = event.target.closest("[data-k8s-running]");
  if (runningButton) {
    const cluster = runningButton.dataset.cluster;
    const scope = runningButton.closest("table") || document;
    const boxes = [...scope.querySelectorAll("[data-running-key]")];
    boxes.forEach((b) => { b.innerHTML = loading("Reading the pods…"); });
    try {
      const data = await api("api/vulnerabilities/running", { params: { id: cluster } });
      if (data.error) {
        boxes.forEach((b, i) => { b.innerHTML = i === 0 ? accessAnswer(data, cluster) : '<span class="muted">see above</span>'; });
        fillPimHints(scope);
        return;
      }
      for (const b of boxes) {
        const found = data.containers[b.dataset.runningKey] || [];
        b.innerHTML = found.length ? found.slice(0, 3).map((c) => `<div><code>${esc(c.image)}</code> <span class="muted">${esc(c.pod)}</span></div>`).join("")
          + (found.length > 3 ? `<div class="muted">and ${found.length - 3} more pod(s)</div>` : "")
          : '<span class="muted">not running now: no pod in that namespace has this container</span>';
      }
    } catch (error) { boxes.forEach((b) => { b.innerHTML = noticeHtml(error); }); }
  }
});

// One resource's tab: its findings and the platform checks for its type.
async function tabVulnerabilities(node, body) {
  const data = await exploreGet("vulnerabilities", node);
  const checks = data.checks.length ? `<h4>Platform checks</h4>${table(["Check", "Value", "Status", "Detail"], data.checks.map((c) => [
    esc(c.name), esc(c.value), pill(c.status), esc(c.detail)]))}` : "";
  const total = data.totals ? data.totals.findings : data.findings.length;
  body.innerHTML = `${checks}<h4>Defender for Cloud findings <span class="muted">${total.toLocaleString()}</span></h4>
    ${total > data.findings.length ? `<p class="muted">Listing the ${data.findings.length} with the highest CVSS of ${total.toLocaleString()}
      (${data.totals.high.toLocaleString()} high); Security → Vulnerabilities groups them all by software.</p>` : ""}
    ${vulnFindingsHtml(data.findings, data.truncated && !data.totals, node.id)}
    <p class="muted">From Microsoft Defender for Cloud, which refreshes them up to once a day. No findings can also mean the resource
      isn't scanned: see the Vulnerabilities tab's "Not scanned".</p>${noticesHtml(data.errors)}`;
}

