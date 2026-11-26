"use strict";

// ------------------------------------------------------------------ secrets
// Every secret, key and certificate expiry in scope. The scan starts in the background with the page
// (the tab's badge shows what's red), and the server reuses it for 10 minutes per scope.

const LONG_EXPIRED_DAYS = 90;  // long past expiry is almost always unused: folded away unless asked for
const SECRET_BUCKETS = ["expired", "critical", "warning", "healthy", "info"];

function secretBucket(r) {
  if (r.status === "critical") return r.days_left != null && r.days_left < 0 ? "expired" : "critical";
  return r.status;
}

// Expired is red; still valid but within the critical days is orange, so the two read apart at a glance.
const BUCKET_RANK = { expired: 0, critical: 1, warning: 2, healthy: 3, info: 4 };
const BUCKET_TONE = { expired: "critical", critical: "soon", warning: "warning", healthy: "healthy", info: "info" };

function secretPill(r) {
  const bucket = secretBucket(r);
  return pillHtml(BUCKET_TONE[bucket], bucket === "info" ? "no expiry" : bucket, { rank: BUCKET_RANK[bucket] });
}

function isLongExpired(r) {
  return secretBucket(r) === "expired" && -r.days_left > LONG_EXPIRED_DAYS;
}

async function loadSecrets(options = {}) {
  const scope = currentScope();
  state.secretsLoaded = JSON.stringify(scope);
  const token = (state.secretsToken = (state.secretsToken || 0) + 1);
  if (!scope.subscriptions.length) {
    state.secrets = { noScope: true };
    renderSecrets();
    return;
  }
  state.secrets = { scanning: "Counting vaults" };
  renderSecrets();
  try {
    const body = { subscriptions: scope.subscriptions, ...options };
    if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
    const started = await api("api/secrets", { method: "POST", body });
    if (token !== state.secretsToken) return;
    if (started.too_many) {
      state.secrets = { tooMany: started.too_many, limit: started.limit };
      renderSecrets();
      return;
    }
    let lost = false;
    const job = await pollJob(started.job_id, {
      isCurrent: () => token === state.secretsToken, failed: "The scan failed.",
      onLost: () => { lost = true; return null; },  // the cached job was cleaned up
      onRunning: (j) => { state.secrets = { scanning: j.phase }; renderSecrets(true); } });
    if (lost) return loadSecrets({ ...options, rescan: true });
    if (!job || job.status === "cancelled") return;  // a newer scope replaced it
    state.secrets = { result: job.result, jobId: started.job_id, at: new Date() };
    doneToast("secrets", "The Secrets scan finished");
    renderSecrets();
  } catch (error) {
    if (token === state.secretsToken) { state.secrets = { error: error.message }; renderSecrets(); }
  }
}

function renderSecretsBadge() {
  const badge = $("secrets-badge");
  const result = state.secrets && state.secrets.result;
  if (!result) {
    badge.className = "badge warning";
    badge.textContent = state.secrets && state.secrets.scanning ? "…" : "";
    badge.classList.toggle("hidden", !(state.secrets && state.secrets.scanning));
    badge.title = "Scanning vaults…";
    return;
  }
  const rows = result.rows.filter((r) => !isLongExpired(r));
  const red = rows.filter((r) => r.status === "critical").length;
  const yellow = rows.filter((r) => r.status === "warning").length;
  const expired = rows.some((r) => secretBucket(r) === "expired");
  badge.className = `badge ${expired ? "" : red ? "soon" : "warning"}`;
  badge.textContent = String(red || yellow);
  badge.classList.toggle("hidden", !red && !yellow);
  badge.title = `${red} expired or expiring within ${result.critical_days} days, ${yellow} within ${result.warn_days} days`;
}

function sortedSecrets(rows) {
  // Red on top: the most recently expired, then what expires soonest; then yellow, soonest first.
  const rank = { expired: 0, critical: 1, warning: 2, healthy: 3, info: 4 };
  return [...rows].sort((a, b) => {
    const ba = secretBucket(a), bb = secretBucket(b);
    if (ba !== bb) return rank[ba] - rank[bb];
    if (ba === "expired") return b.days_left - a.days_left;
    return (a.days_left ?? 1e9) - (b.days_left ?? 1e9);
  });
}

function renderSecrets(progressOnly = false) {
  renderSecretsBadge();
  if (state.view !== "secrets") return;
  const s = state.secrets || {};
  const exportsOn = !!s.result;
  for (const fmt of ["md", "csv", "pdf"]) $(`secrets-export-${fmt}`).disabled = !exportsOn;
  if (s.scanning) {
    const phase = $("secrets-phase");
    if (progressOnly && phase) { phase.textContent = `${s.scanning}…`; return; }
    $("secrets-counters").innerHTML = "";
    $("secrets-body").innerHTML = loading(`${s.scanning}…`, "secrets-phase")
      + '<p class="muted">One request per vault; large scopes take a minute or two. Other tabs keep working.</p>';
    $("secrets-meta").textContent = "";
    return;
  }
  if (s.error) { $("secrets-counters").innerHTML = ""; $("secrets-body").innerHTML = noticeHtml(s.error); return; }
  if (s.noScope) { $("secrets-counters").innerHTML = ""; $("secrets-body").innerHTML = pickScopeHtml("Secrets & certificates are read from the Key Vaults in scope."); return; }
  if (s.tooMany) {
    $("secrets-counters").innerHTML = "";
    $("secrets-body").innerHTML = `<p>${s.tooMany} vaults in scope, more than the automatic limit of ${s.limit}
      (<code>triage.secret_scan_max_vaults</code> in Settings). Narrow the scope above, or scan them all.</p>
      <button type="button" id="secrets-scan-now">Scan ${s.tooMany} vaults now</button>`;
    $("secrets-scan-now").addEventListener("click", () => loadSecrets({ force: true }));
    return;
  }
  if (!s.result) return;
  const result = s.result;
  const includeOld = $("secrets-long-expired").checked;
  const visible = result.rows.filter((r) => includeOld || !isLongExpired(r));
  const count = (bucket) => visible.filter((r) => secretBucket(r) === bucket).length;
  const old = result.rows.filter(isLongExpired).length;
  const needsAction = count("expired") + count("critical") + count("warning");
  const tiles = [
    ["problems", "Needs action: expired, critical and warning", count("expired") ? "critical" : count("critical") ? "soon" : "warning",
      needsAction, ""],
    ["expired", "Expired", "critical", count("expired"), !includeOld && old ? `+${old} expired over ${LONG_EXPIRED_DAYS} days ago` : ""],
    ["critical", `Critical: within ${result.critical_days} days`, "soon", count("critical"), ""],
    ["warning", `Warning: within ${result.warn_days} days`, "warning", count("warning"), ""],
    ["healthy", "Healthy", "healthy", count("healthy"), ""],
    ["info", "No expiry date or disabled", "unknown", count("info"), ""],
  ];
  $("secrets-counters").innerHTML = tiles.map(([key, label, tone, n, extra]) => `
    <button type="button" class="counter ${tone} ${state.secretsFilter === key ? "active" : ""}" data-bucket="${key}"
      aria-pressed="${state.secretsFilter === key}" title="Show ${key === "problems" ? "everything that needs action" : `only: ${esc(label)}`}">
      <span class="n">${n}</span><span class="label">${esc(label)}</span>${extra ? `<br><span class="label">${esc(extra)}</span>` : ""}</button>`).join("")
    + (result.errors.length ? `<div class="counter unknown"><span class="n">${result.errors.length}</span>
      <span class="label">reason(s) some vaults couldn't be read (below)</span></div>` : "");
  $("secrets-meta").textContent = `${result.vaults} Key Vault(s) and App Service certificates · scanned ${s.at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;

  const text = $("secrets-filter").value.trim().toLowerCase();
  const wanted = state.secretsFilter === "problems" ? ["expired", "critical", "warning"] : [state.secretsFilter];
  const rows = sortedSecrets(visible.filter((r) => wanted.includes(secretBucket(r))
    && (!text || [r.name, r.vault, r.resource_group, subName(r.subscription_id), r.kind].some((v) => (v || "").toLowerCase().includes(text)))));
  const columns = [["status", "Status"], ["subscription", "Subscription"], ["resource_group", "Resource group"], ["vault", "Vault"],
    ["kind", "Type"], ["name", "Name"], ["expires", "Expires"], ["days_left", "Days left"]];
  const heading = tiles.find((t) => t[0] === state.secretsFilter)[1];
  $("secrets-body").innerHTML = `<h3>${esc(heading)} <span class="muted">(${rows.length})</span></h3>
    ${rows.length ? `<table class="data-table" data-list="secrets" data-nofilter><thead><tr>${columns.map(([key, label]) =>
      `<th class="${key === "days_left" ? "num" : ""}">${label}</th>`).join("")}<th></th></tr></thead>
      <tbody>${rows.map((r) => `<tr class="clickable" data-open="${esc(JSON.stringify({ kind: "resource", id: r.vault_id, name: r.source === "App Service" ? r.name : r.vault }))}"
        title="${esc(r.note)}">
        <td>${secretPill(r)}</td><td>${esc(subName(r.subscription_id))}</td><td>${esc(r.resource_group)}</td>
        <td>${esc(r.source === "App Service" ? "(App Service)" : r.vault)}</td><td>${esc(r.kind)}</td><td>${esc(r.name)}</td>
        <td>${esc(r.expires || "–")}</td><td class="num">${r.days_left ?? "–"}</td>
        <td>${browseButtonHtml()} ${watchButton({ id: r.vault_id, name: r.vault, kind: "resource" })}</td></tr>`).join("")}</tbody></table>`
      : `<p class="ok">✓ Nothing ${state.secretsFilter === "problems" ? "expired or expiring" : "here"}${text ? " matching the filter" : ""}.</p>`}
    ${result.errors.length ? `<h4>Not readable</h4>${noticesHtml(result.errors)}` : ""}`;
}

$("secrets-counters").addEventListener("click", (event) => {
  const tile = event.target.closest("[data-bucket]");
  if (!tile) return;
  state.secretsFilter = tile.dataset.bucket;
  renderSecrets();
});
$("secrets-body").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-open]");
  if (row) startSnapshot(JSON.parse(row.dataset.open));
});
$("secrets-filter").addEventListener("input", debounce(() => renderSecrets(), 200));
$("secrets-long-expired").addEventListener("change", () => renderSecrets());
$("secrets-refresh").addEventListener("click", () => loadSecrets({ rescan: true }));
for (const fmt of ["md", "csv", "pdf"]) {
  $(`secrets-export-${fmt}`).addEventListener("click", async () => {
    const s = state.secrets;
    if (!s || !s.jobId) return;
    const status = state.secretsFilter === "problems" ? ["critical", "warning"]
      : [state.secretsFilter === "expired" ? "critical" : state.secretsFilter];
    try {
      const params = { format: fmt, status };
      if ($("secrets-long-expired").checked) params.include_old = "1";
      await download(`api/secrets/${s.jobId}/export`, params, `secrets.${fmt}`);
    } catch (error) {
      $("secrets-body").insertAdjacentHTML("afterbegin", noticeHtml(error));
    }
  });
}

