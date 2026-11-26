"use strict";

// ------------------------------------------------------------------ integrations
// ServiceNow and monitoring-tool connections. One form per card, read top to bottom: what to connect to,
// how to sign in (password or key right there), then Test (checks what's typed, saves nothing) and Save.
// Fields go to config.yaml; secrets go to the OS keychain and never come back to the page.

const snTables = () => Object.entries((state.integrations || {}).servicenow_tables || {});
const SN_AUTH = [["basic", "Username and password"], ["oauth_browser", "OAuth: sign in with your own ServiceNow login"],
  ["oauth_client", "OAuth: client credentials (service account)"]];
const SN_SECRETS = { basic: ["password"], oauth_client: ["client_secret"], oauth_browser: ["client_secret"] };
// Offered when adding or changing a connection; oauth_client (service account) stays hidden, but oauth_browser lets a
// user sign in with their own ServiceNow login — the path for SSO instances where basic auth over REST is blocked.
const SN_AUTH_OFFERED = ["basic", "oauth_browser"];
const MONITOR_LABELS = { newrelic: "New Relic", datadog: "Datadog", grafana: "Grafana" };
const MONITOR_SECRETS = { newrelic: ["api_key"], datadog: ["api_key", "app_key"], grafana: ["token"] };
const SECRET_LABELS = { password: "Password", client_secret: "Client secret", refresh_token: "ServiceNow sign-in",
  api_key: "API key", app_key: "Application key", token: "Service account token" };

async function loadIntegrations() {
  const box = $("integrations-body");
  box.innerHTML = loading("Reading integrations…");
  try {
    state.integrations = await api("api/integrations");
    state.snDrafts = {};
    renderIntegrations();
  } catch (error) {
    box.innerHTML = noticeHtml(error);
  }
}

function statusBlock(st, offText) {
  if (!st.enabled) {
    return `<p class="int-status off">Off: ${esc(offText)}</p>`;
  }
  if (st.ready) return '<p class="int-status ready">✓ On and configured. Test connection checks that it can read.</p>';
  return `<div class="int-status missing"><strong>On, but not ready yet:</strong><ul>${st.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>`;
}

// A secret box that's always usable: what's stored is said next to it, and Save stores what's typed.
function secretField(field, st) {
  const where = st && st.secrets ? st.secrets[field] : null;
  const label = SECRET_LABELS[field];
  const now = where === "environment" ? `from the ${esc(st.env[field])} environment variable`
    : where === "keychain" ? `✓ stored in your ${esc(state.integrations.keychain.backend || "keychain")}` : "not stored yet";
  return `<label>${esc(label)}
    <input type="password" autocomplete="new-password" data-secret="${esc(field)}"
      placeholder="${where === "keychain" ? "•••••••• (type to replace)" : `Type the ${esc(label.toLowerCase())}`}">
    <span class="int-hint ${where ? "ok" : ""}">${now}${where === "keychain" ? ` · <button type="button" class="link" data-secret-remove="${esc(field)}">remove</button>` : ""}</span></label>`;
}

function testResult(target) {
  const result = (state.integrationTests || {})[target];
  if (!result) return "";
  if (result.running) return loading("Testing the connection…");
  return `<div class="int-result ${result.ok ? "ok" : "bad"}"><strong>${result.ok ? "✓ " : "✗ "}${esc(result.message)}</strong>
    ${(result.details || []).length ? `<ul>${result.details.map((d) => `<li>${d.ok ? "✓" : "✗"} ${esc(d.name)}${d.ok ? "" : `: ${esc(d.detail)}`}</li>`).join("")}</ul>` : ""}</div>`;
}

function renderIntegrations() {
  const data = state.integrations;
  const keychain = data.keychain.available ? ""
    : `<p class="notice">${esc(data.keychain.hint)} Each secret box names the variable to set before starting the console.</p>`;
  const tenantName = data.tenant.name;
  $("integrations-title").textContent = `Integrations for ${tenantName}`;
  const where = data.tenant.key ? `Saved under <code>tenants.${esc(data.tenant.key)}.integrations</code>.`
    : `Nothing set for this tenant yet; saving creates <code>tenants.${esc(data.tenant.save_key)}.integrations</code>.`;
  const legacy = data.legacy ? `<div class="notice partial"><strong>Integrations set before they were per tenant</strong>
      <p>${[...data.legacy.servicenow.map((i) => `ServiceNow (${esc(i.replace(/^https:\/\//, ""))})`), ...data.legacy.monitors.map(esc)].join(", ")}
        ${"are saved for every tenant, so no tenant uses them now. Move them to"} <strong>${esc(tenantName)}</strong>
        if they belong here (their keys move with them); otherwise switch tenant first.</p>
      <p><button type="button" id="int-move-legacy" class="primary">Move them to ${esc(tenantName)}</button></p></div>` : "";
  $("integrations-body").innerHTML = `${keychain}${legacy}<p class="muted">${where}</p>
    <h4 class="int-section">ServiceNow</h4>
    <p class="muted">One connection per instance (e.g. prod and non-prod). Turned-on connections are listed in the Scope bar,
      where you pick the one the ServiceNow tab and Triage use.</p>
    ${data.servicenow_settings.map((c, i) => snCard(c, data.servicenow[i])).join("") || '<p class="muted">No ServiceNow connections yet.</p>'}
    <div class="int-actions"><button type="button" id="sn-add">Add ServiceNow connection</button></div>
    <div class="int-note" data-note="sn-general"></div>
    <h4 class="int-section">Monitoring tools</h4>
    <p class="muted">Each tool gets its own tab and Triage card, showing the connection picked in the Scope bar.</p>
    ${data.monitor_settings.map((m, i) => monitorCard(m, data.monitors[i])).join("") || '<p class="muted">No connections yet.</p>'}
    <div class="int-actions"><select id="monitor-new-type">${data.monitor_types.map((t) => `<option value="${t}">${MONITOR_LABELS[t] || t}</option>`).join("")}</select>
      <button type="button" id="monitor-add">Add connection</button></div>
    <div class="int-note" data-note="general"></div>`;
  renderIntegrationNote();
}

function snCard(saved, st) {
  const cfg = { ...saved, ...((state.snDrafts || {})[saved.name] || {}) };
  const data = state.integrations;
  const basic = cfg.auth === "basic";
  const signIn = cfg.auth === "oauth_browser" ? `<div class="int-oauth">
      <p><strong>One-time setup on the instance:</strong> System OAuth → Application Registry → New → <em>Create an OAuth API endpoint
        for external clients</em> (on newer releases it's under <em>[Deprecated UI]</em> — that one's correct), with this redirect URL:</p>
      <p><code class="command">${esc(data.redirect_uri)}</code> <button type="button" data-copy="${esc(data.redirect_uri)}">Copy</button>
        <span class="muted">It includes this console's port, so start it on the same port each time.</span></p>
      <p><button type="button" data-sn-sign-in>Sign in with ServiceNow</button>
        <span class="${st.secrets.refresh_token ? "ok" : "muted"}">${st.secrets.refresh_token ? "✓ signed in" : "not signed in yet (Save first)"}</span></p>
    </div>` : "";
  const v = (key) => esc(cfg[key] ?? "");
  return `<div class="int-card" data-sn="${esc(saved.name)}">
      <div class="int-head"><h4>${esc(saved.name)} <span class="muted">ServiceNow</span></h4>
        <label class="int-switch"><input type="checkbox" data-s="enabled" ${cfg.enabled ? "checked" : ""}> Enabled</label></div>
      ${statusBlock(st, "turn it on to list it in the Scope bar for the ServiceNow tab: incidents, change requests, alerts and events.")}
      <div class="int-form">
        <label>Name <input type="text" data-s="name" value="${v("name")}">
          <span class="int-hint">Shown in the Scope bar, e.g. "ServiceNow Prod". Its password stays with the instance.</span></label>
        <label>Instance <input type="text" data-s="instance" value="${v("instance")}" placeholder="dev12345, or https://yourcompany.service-now.com" autocomplete="off">
          <span class="int-hint">The name is enough: <code>dev12345</code> becomes https://dev12345.service-now.com.</span></label>
        <label>Sign in with <select data-s="auth">${SN_AUTH.filter(([val]) => SN_AUTH_OFFERED.includes(val) || val === cfg.auth).map(([val, l]) => `<option value="${val}" ${val === cfg.auth ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
        ${basic ? `<label>Username <input type="text" data-s="username" value="${v("username")}" autocomplete="off"></label>`
          : `<label>OAuth client ID <input type="text" data-s="client_id" value="${v("client_id")}" autocomplete="off">
              <span class="int-hint">From the OAuth app in ServiceNow's Application Registry.</span></label>`}
        ${(SN_SECRETS[cfg.auth] || []).map((f) => secretField(f, st)).join("")}
      </div>
      ${signIn}
      <details class="int-advanced"><summary>What to read <span class="muted">(tables, filter, matching: defaults are fine to start)</span></summary>
        <div class="int-form">
          <fieldset><legend>Tables</legend>${snTables().map(([t, l]) => `<label class="inline-check"><input type="checkbox" data-sn-table="${t}"
            ${cfg.tables.includes(t) ? "checked" : ""}> ${esc(l)} <code>${t}</code></label>`).join("")}
            <span class="int-hint">A table the instance doesn't have, or you can't read, is reported by Test and skipped.</span></fieldset>
          <label>Incident filter <input type="text" data-s="incident_filter" value="${v("incident_filter")}">
            <span class="int-hint">Incidents only (changes use the days below). A ServiceNow encoded query; <code>active=true</code> includes Resolved (state 6) until it auto-closes.</span></label>
          <label>Look for Azure resource names in <input type="text" data-s="match_fields" value="${esc(cfg.match_fields.join(", "))}">
            <span class="int-hint">Field names, comma separated.</span></label>
          <label>Most records per query <input type="number" min="1" data-s="max_records" value="${v("max_records")}">
            <span class="int-hint">The page size for every read, and the most Event Management alerts and events read.</span></label>
          <label>Incidents: most records <input type="number" min="1" max="50000" data-s="incident_max_records" value="${v("incident_max_records")}">
            <span class="int-hint">Incidents matching the filter are read up to this many, highest priority first, a page at a time; the tab says when it's reached.</span></label>
          <label>Changes: days back <input type="number" min="0" max="365" data-s="change_history_days" value="${v("change_history_days")}">
            <span class="int-hint">Changes closed in this many days, with their outcome; open ones that ended before it are left out.</span></label>
          <label>Changes: days ahead <input type="number" min="0" max="365" data-s="change_ahead_days" value="${v("change_ahead_days")}">
            <span class="int-hint">Scheduled changes starting within this many days.</span></label>
          <label>Changes: most records <input type="number" min="1" max="50000" data-s="change_max_records" value="${v("change_max_records")}">
            <span class="int-hint">Open and closed changes are each read up to this many, a page at a time; the tab says when it's reached.</span></label>
          <label>Also look for Azure resource names in a change's <input type="text" data-s="change_match_fields" value="${esc((cfg.change_match_fields || []).join(", "))}">
            <span class="int-hint">Change request and change task fields, comma separated; its affected CIs and tasks are read when ticked above.</span></label>
        </div>
      </details>
      <div class="int-actions"><button type="button" data-sn-test>Test connection</button>
        <button type="button" data-sn-save class="primary">Save</button>
        <button type="button" data-sn-remove>Remove connection</button>
        <span class="muted">Test checks what's typed and saves nothing. Save keeps the settings in config.yaml and the
          ${basic ? "password" : "secret"} in your keychain.</span></div>
      <div class="int-note" data-note="sn:${esc(saved.name)}"></div>
      <div class="int-test">${testResult(`servicenow:${saved.name}`)}</div>
    </div>`;
}

function capabilityLine(type) {
  const caps = (state.integrations.monitor_capabilities || {})[type];
  return caps ? `<p class="int-hint">${caps.connected ? "Answers" : "Will answer, once it connects"}: ${esc(caps.capabilities.join(" · "))}.</p>` : "";
}

function monitorCard(m, st) {
  const target = `monitor:${m.name}`;
  const fields = {
    newrelic: `<label>Region <select data-m="region">${["us", "eu"].map((r) => `<option value="${r}" ${r === (m.region || "us") ? "selected" : ""}>${r.toUpperCase()}</option>`).join("")}</select></label>
      <label>Account IDs <input type="text" data-m="account_ids" value="${esc((m.account_ids || []).join(", "))}" placeholder="1234567, 7654321">
        <span class="int-hint">New Relic → Administration → Access management → Accounts.</span></label>
      ${Object.keys(m.log_attributes || {}).length ? `<div class="int-hint">Logs also matched by name: ${Object.entries(m.log_attributes)
        .map(([type, attr]) => `<code>${esc(type)}</code> → <code>${esc(attr)}</code>`).join(", ")} (set in config.yaml).</div>` : ""}`,
    datadog: `<label>Site <input type="text" data-m="site" value="${esc(m.site || "")}" placeholder="datadoghq.com"></label>`,
    grafana: `<label>URL <input type="text" data-m="url" value="${esc(m.url || "")}" placeholder="https://grafana.example.com"></label>
      <label>Sign in with <select data-m="auth">${[["token", "Service account token"], ["azure", "Azure Managed Grafana (your az login)"]]
        .map(([v, l]) => `<option value="${v}" ${v === (m.auth || "token") ? "selected" : ""}>${l}</option>`).join("")}</select></label>`,
  }[m.type];
  const secretsFor = m.type === "grafana" && m.auth === "azure" ? [] : MONITOR_SECRETS[m.type];
  const keyHint = m.type === "newrelic" ? '<span class="int-hint">A User key, starting with NRAK-.</span>' : "";
  return `<div class="int-card" data-monitor="${esc(m.name)}">
    <div class="int-head"><h4>${esc(m.name)}${m.name === MONITOR_LABELS[m.type] ? "" : ` <span class="muted">${esc(MONITOR_LABELS[m.type] || m.type)}</span>`}</h4>
      <label class="int-switch"><input type="checkbox" data-m="enabled" ${m.enabled ? "checked" : ""}> Enabled</label></div>
    ${statusBlock(st, "turn it on to show its alerts in the Monitors tab and on Triage.")}
    ${capabilityLine(m.type)}
    <div class="int-form"><label>Name <input type="text" data-m="name" value="${esc(m.name)}">
        <span class="int-hint">Renaming means typing its key again.</span></label>${fields}
      ${secretsFor.map((f) => secretField(f, st)).join("")}${keyHint}</div>
    <div class="int-actions">${st.kind === "newrelic" ? '<button type="button" data-monitor-test>Test connection</button>' : ""}
      <button type="button" data-monitor-save class="primary">Save</button>
      <button type="button" data-monitor-remove>Remove connection</button></div>
    <div class="int-note" data-note="${esc(m.name)}"></div>
    <div class="int-test">${testResult(target)}</div>
  </div>`;
}

// ---------------------------------------------------------------- reading the forms

function readServiceNowCard(card, original) {
  const saved = { ...original, ...((state.snDrafts || {})[original.name] || {}) };
  const value = (key) => { const el = card.querySelector(`[data-s="${key}"]`); return el ? (el.type === "checkbox" ? el.checked : el.value.trim()) : saved[key]; };
  return {
    name: value("name") || original.name, enabled: value("enabled"), instance: value("instance"), auth: value("auth"),
    username: value("username"), client_id: value("client_id"), incident_filter: value("incident_filter"),
    max_records: Number(value("max_records")) || 500,
    match_fields: String(value("match_fields") ?? "").split(",").map((f) => f.trim()).filter(Boolean),
    change_match_fields: String(value("change_match_fields") ?? "").split(",").map((f) => f.trim()).filter(Boolean),
    change_history_days: Number(value("change_history_days")), change_ahead_days: Number(value("change_ahead_days")),
    change_max_records: Number(value("change_max_records")), incident_max_records: Number(value("incident_max_records")),
    tables: [...card.querySelectorAll("[data-sn-table]")].filter((c) => c.checked).map((c) => c.dataset.snTable),
  };
}

const snCardOf = (name) => document.querySelector(`[data-sn="${CSS.escape(name)}"]`);

function typedSecrets(card) {
  const typed = {};
  card.querySelectorAll("[data-secret]").forEach((input) => { if (input.value.trim()) typed[input.dataset.secret] = input.value.trim(); });
  return typed;
}

function readMonitor(card, original) {
  const value = (key) => { const el = card.querySelector(`[data-m="${key}"]`); return el ? (el.type === "checkbox" ? el.checked : el.value.trim()) : undefined; };
  const m = { name: value("name") || original.name, type: original.type, enabled: value("enabled") };
  if (original.type === "newrelic") {
    if (original.log_attributes) m.log_attributes = original.log_attributes;  // set in config.yaml, kept on Save
    m.region = value("region");
    m.account_ids = (value("account_ids") || "").split(/[\s,]+/).filter(Boolean).map(Number).filter((n) => Number.isInteger(n));
  }
  if (original.type === "datadog" && value("site")) m.site = value("site");
  if (original.type === "grafana") { if (value("url")) m.url = value("url"); m.auth = value("auth"); }
  return m;
}

// ---------------------------------------------------------------- actions

async function storeSecrets(target, typed) {
  for (const [field, value] of Object.entries(typed)) {
    state.integrations = await api("api/integrations/secret", { method: "POST", body: { target, field, value } });
  }
  const names = Object.keys(typed).map((f) => SECRET_LABELS[f].toLowerCase());
  return names.length ? ` The ${names.join(" and ")} ${names.length === 1 ? "is" : "are"} stored in your keychain.` : "";
}

// Saves the whole list (settings first, then any secret typed on the card), so nothing typed is lost to a redraw.
async function saveServiceNow(connections, note, typed = null, name = null) {
  try {
    state.integrations = await api("api/integrations/servicenow", { method: "POST", body: { connections } });
  } catch (error) {
    integrationMessage(`Not saved: ${error.message}`, true);
    return;
  }
  if (name) state.noteTarget = `sn:${name}`;  // as for monitors: a failure stays on the card that was edited
  state.snDrafts = {};
  state.snLoaded = false;
  refreshIntegrationTabs();
  let extra = "";
  let failed = false;
  if (typed && name) {
    try { extra = await storeSecrets(`servicenow:${name}`, typed); } catch (error) { extra = ` But the secret wasn't stored: ${error.message}`; failed = true; }
  }
  renderIntegrations();
  integrationMessage(`${note}${extra}`, failed);
}

async function saveMonitors(monitors, note, typed = null, name = null) {
  try {
    state.integrations = await api("api/integrations/monitors", { method: "POST", body: { monitors } });
  } catch (error) {
    integrationMessage(`Not saved: ${error.message}`, true);
    return;
  }
  if (name) state.noteTarget = name;  // after a rename, the card has its new name; a failure stays on the old card
  state.monLoaded = false;
  refreshIntegrationTabs();
  let extra = "";
  let failed = false;
  if (typed && name) {
    try { extra = await storeSecrets(`monitor:${name}`, typed); } catch (error) { extra = ` But the key wasn't stored: ${error.message}`; failed = true; }
  }
  renderIntegrations();
  integrationMessage(`${note}${extra}`, failed);
}

async function runTest(target, values, typed) {
  state.integrationTests = { ...(state.integrationTests || {}), [target]: { running: true } };
  const cardOf = () => (target.startsWith("servicenow:") ? snCardOf(target.slice(11))
    : document.querySelector(`[data-monitor="${CSS.escape(target.slice(8))}"]`));
  const card = cardOf();
  if (card) card.querySelector(".int-test").innerHTML = testResult(target);
  try {
    state.integrationTests[target] = await api("api/integrations/test", { method: "POST", body: { target, values, secrets: typed } });
  } catch (error) {
    state.integrationTests[target] = { ok: false, message: error.message };
  }
  const fresh = cardOf();
  if (fresh) fresh.querySelector(".int-test").innerHTML = testResult(target);  // only the result: typed values stay
}

// Messages show inside the card that was used, next to its buttons.
function integrationMessage(text, isError = false) {
  state.integrationNote = text ? { target: state.noteTarget || "general", text, isError } : null;
  renderIntegrationNote();
}

function renderIntegrationNote() {
  document.querySelectorAll(".int-note").forEach((el) => { el.innerHTML = ""; });
  const note = state.integrationNote;
  if (!note) return;
  const slot = [...document.querySelectorAll(".int-note")].find((el) => el.dataset.note === note.target)
    || document.querySelector('.int-note[data-note="general"]');
  if (slot) slot.innerHTML = `<p class="${note.isError ? "notice" : "message"}">${esc(note.text)}</p>`;
}

function noteTargetOf(el) {
  const sn = el.closest("[data-sn]");
  if (sn) return `sn:${sn.dataset.sn}`;
  const card = el.closest("[data-monitor]");
  return card ? card.dataset.monitor : "general";
}

$("integrations-body").addEventListener("change", async (event) => {
  const el = event.target;
  state.noteTarget = noteTargetOf(el);
  const snCardEl = el.closest("[data-sn]");
  if (snCardEl && (el.dataset.s === "enabled" || el.dataset.s === "auth")) {
    const data = state.integrations;
    const index = data.servicenow_settings.findIndex((c) => c.name === snCardEl.dataset.sn);
    const original = data.servicenow_settings[index];
    if (el.dataset.s === "auth") {  // show that sign-in's fields, keeping what's typed; saved with Save
      state.snDrafts = { ...(state.snDrafts || {}), [original.name]: readServiceNowCard(snCardEl, original) };
      renderIntegrations();
      integrationMessage("Sign-in method changed. Fill in the fields below, then Test and Save.");
      return;
    }
    const next = [...data.servicenow_settings];  // the switch saves the whole card, so it sticks with what's typed
    next[index] = readServiceNowCard(snCardEl, original);
    state.noteTarget = `sn:${next[index].name}`;
    await saveServiceNow(next, `${next[index].name} turned ${el.checked ? "on" : "off"}. Everything on the card is saved.`,
      typedSecrets(snCardEl), next[index].name);
    return;
  }
  const card = el.closest("[data-monitor]");
  if (card && el.dataset.m === "enabled") {
    const data = state.integrations;
    const index = data.monitor_settings.findIndex((m) => m.name === card.dataset.monitor);
    const next = [...data.monitor_settings];
    next[index] = readMonitor(card, data.monitor_settings[index]);
    await saveMonitors(next, `${next[index].name} turned ${el.checked ? "on" : "off"}. Everything on the card is saved.`,
      typedSecrets(card), next[index].name);
  }
});

$("integrations-body").addEventListener("click", async (event) => {
  const el = event.target;
  if (el.tagName !== "BUTTON") return;
  state.noteTarget = noteTargetOf(el);
  const data = state.integrations;
  if (el.dataset.copy) {
    await copyText(el.dataset.copy, el);
    return;
  }
  const snEl = el.closest("[data-sn]");
  if (snEl && !el.dataset.secretRemove && !el.dataset.copy) {
    const index = data.servicenow_settings.findIndex((c) => c.name === snEl.dataset.sn);
    const original = data.servicenow_settings[index];
    if (el.dataset.snTest !== undefined) { await runTest(`servicenow:${original.name}`, readServiceNowCard(snEl, original), typedSecrets(snEl)); return; }
    if (el.dataset.snSave !== undefined) {
      const next = [...data.servicenow_settings];
      next[index] = readServiceNowCard(snEl, original);
      await saveServiceNow(next, `${next[index].name} saved.`, typedSecrets(snEl), next[index].name);
      return;
    }
    if (el.dataset.snRemove !== undefined) {
      if (!window.confirm(`Remove the ${original.name} connection? Its password stays in your keychain (other connections to the same instance use it).`)) return;
      state.noteTarget = "sn-general";
      await saveServiceNow(data.servicenow_settings.filter((_, i) => i !== index), `${original.name} removed.`);
      return;
    }
    if (el.dataset.snSignIn !== undefined) {
      try {
        const { url } = await api("api/integrations/servicenow/sign-in", { method: "POST", body: { name: original.name } });
        if (safeHref(url)) window.open(url, "_blank", "noopener");
        integrationMessage("Finish signing in on the ServiceNow page that opened, then come back and click Test connection.");
      } catch (error) { integrationMessage(error.message, true); }
      return;
    }
  }
  if (el.id === "sn-add") {
    const taken = new Set(data.servicenow_settings.map((c) => c.name.toLowerCase()));
    let name = "ServiceNow";
    for (let n = 2; taken.has(name.toLowerCase()); n += 1) name = `ServiceNow ${n}`;
    state.noteTarget = `sn:${name}`;
    await saveServiceNow([...data.servicenow_settings, { name, enabled: false }], `${name} added. Fill it in, then Test and Save.`);
    return;
  }
  if (el.dataset.secretRemove) {
    const card = el.closest("[data-monitor]");
    const target = card ? `monitor:${card.dataset.monitor}` : `servicenow:${el.closest("[data-sn]").dataset.sn}`;
    try {
      state.integrations = await api("api/integrations/secret", { method: "POST", body: { target, field: el.dataset.secretRemove, remove: true } });
      renderIntegrations();
      integrationMessage(`${SECRET_LABELS[el.dataset.secretRemove]} removed from your keychain.`);
    } catch (error) { integrationMessage(error.message, true); }
    return;
  }
  if (el.id === "int-move-legacy") {
    try {
      state.integrations = await api("api/integrations/move-legacy", { method: "POST", body: {} });
    } catch (error) { integrationMessage(`Not moved: ${error.message}`, true); return; }
    state.snLoaded = false;
    state.monLoaded = false;
    await refreshIntegrationTabs();
    renderIntegrations();
    integrationMessage(`Moved to ${state.integrations.tenant.name}.`);
    return;
  }
  if (el.id === "monitor-add") {
    const type = $("monitor-new-type").value;
    const taken = new Set(data.monitor_settings.map((m) => m.name.toLowerCase()));
    let name = MONITOR_LABELS[type];
    for (let n = 2; taken.has(name.toLowerCase()); n += 1) name = `${MONITOR_LABELS[type]} ${n}`;
    state.noteTarget = name;
    await saveMonitors([...data.monitor_settings, { name, type, enabled: false }], `${name} added. Fill it in, then Test and Save.`);
    return;
  }
  const card = el.closest("[data-monitor]");
  if (!card) return;
  const index = data.monitor_settings.findIndex((m) => m.name === card.dataset.monitor);
  const original = data.monitor_settings[index];
  if (el.dataset.monitorTest !== undefined) {
    await runTest(`monitor:${original.name}`, readMonitor(card, original), typedSecrets(card));
  } else if (el.dataset.monitorSave !== undefined) {
    const next = [...data.monitor_settings];
    next[index] = readMonitor(card, original);
    await saveMonitors(next, `${next[index].name} saved.`, typedSecrets(card), next[index].name);
  } else if (el.dataset.monitorRemove !== undefined) {
    if (!window.confirm(`Remove the ${original.name} connection? Its stored key stays in your keychain until you remove it there.`)) return;
    state.noteTarget = "general";
    await saveMonitors(data.monitor_settings.filter((_, i) => i !== index), `${original.name} removed.`);
  }
});

