"use strict";

// ------------------------------------------------------------------ alerts
// The server runs the rules (#69); the page asks what's new, shows one bar on every tab and notifies once per alert.

const ALERT_POLL_SECONDS = 20;
const ALERT_MINUTE_CHOICES = [2, 3, 5, 10, 15, 30];
const SN_PRIORITIES = [[1, "P1 only"], [2, "P1 and P2"], [3, "P1 to P3"], [4, "P1 to P4"], [5, "any priority"]];
const signalKeys = (kind) => (state.alerts ? state.alerts.signals_by_kind[kind] : []);
const defaultSignals = (kind) => (state.alerts ? [...state.alerts.default_signals[kind]] : []);
const hhmm = (epoch) => new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function serverNow() {
  return Date.now() / 1000 + (state.alertSkew || 0);
}

function duration(seconds) {
  const minutes = Math.round(Math.max(0, seconds) / 60);
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function startAlerts() {
  pollAlerts();
  setInterval(pollAlerts, ALERT_POLL_SECONDS * 1000);
  setInterval(renderAlertBar, 10 * 1000);  // keeps "2 min ago" moving between polls
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollAlerts(); });
}

async function pollAlerts() {
  let result;
  try { result = await api("api/alerts"); } catch { return; }
  setAlerts(result);
}

function setAlerts(result) {
  state.alerts = result;
  state.alertSkew = result.now - Date.now() / 1000;
  notifyAlerts(result);
  renderAlertBar();
  if (state.view === "settings" && !state.alertEdit) renderAlertRules();
  if (state.view === "watchlist") { mergeWatchRule(); renderWatchlist(); }
}

const RECOVERY_SIGNALS = ["recovered", "check_recovered"];

// What the bar counts, the same rule as the server's: new since Dismiss, still open, not a recovery, not acknowledged,
// not snoozed.
function isOpen(e) {
  return !e.resolved_at && !RECOVERY_SIGNALS.includes(e.signal);
}

function unseenAlerts() {
  const a = state.alerts;
  const now = serverNow();
  return a ? a.events.filter((e) => e.seq > a.dismissed && isOpen(e) && !e.acknowledged_at && !((e.snoozed_until || 0) > now)) : [];
}

function recentRecoveries() {
  const a = state.alerts;
  return a ? a.events.filter((e) => e.seq > a.dismissed && RECOVERY_SIGNALS.includes(e.signal)) : [];
}

function heartbeat(a) {
  const on = a.rules.filter((r) => r.enabled);
  if (!on.length) return { html: "Alerts: every rule is off", bad: false };
  const parts = [];
  const checked = on.filter((r) => r.last_check);
  const due = on.filter((r) => r.next_due && !r.cant_run).map((r) => r.next_due);
  if (checked.length) parts.push(`last check ${ago(Math.max(...checked.map((r) => r.last_check)))}`);
  else if (on.some((r) => !r.cant_run)) parts.push("first check starting");
  if (due.length) {
    const next = Math.min(...due) - serverNow();
    parts.push(next <= 0 ? "next due now" : `next in ${duration(next)}`);
  }
  const late = checked.some((r) => serverNow() - r.last_check > 2 * r.interval_minutes * 60 + 60);
  const broken = on.filter((r) => r.cant_run || r.failing).length;
  let html = `Alerts: ${esc(parts.join(", "))}`;
  if (late) html += ' · <span class="stale">checks are late (was the computer asleep?)</span>';
  if (!a.background) html += ' · <span class="stale">rules run only on Check now here</span>';
  if (broken) html += ` · <button type="button" class="link" data-alert-settings>${broken} rule${broken === 1 ? "" : "s"} can't run</button>`;
  return { html, bad: late || broken > 0 || !a.background };
}

function renderAlertBar() {
  const a = state.alerts;
  const bar = $("alert-bar");
  const show = a && (a.rules.length || a.events.length);
  bar.classList.toggle("hidden", !show);
  const unseen = unseenAlerts();
  document.title = unseen.length ? `(${unseen.length}) Azure Health Console` : "Azure Health Console";
  if (!show) return;
  bar.classList.toggle("firing", unseen.length > 0);
  $("alert-summary").innerHTML = unseen.length
    ? `🔔 ${unseen.length} new alert${unseen.length === 1 ? "" : "s"}: ${unseen.slice(0, 2).map((e) => esc(e.title)).join(" · ")}${unseen.length > 2 ? " …" : ""}`
    : a.rules.length ? `No new alerts${recentRecoveries().length ? ` · <span class="ok">${recentRecoveries().length} recovered</span>` : ""}` : "";
  const beat = heartbeat(a);
  $("alert-heartbeat").innerHTML = a.rules.length ? beat.html : "";
  $("alert-heartbeat").title = a.rules.filter((r) => r.enabled && r.result)
    .map((r) => `${r.name}: ${ruleResult(r).replace(/<[^>]+>/g, "")}`).join("\n");
  $("alert-heartbeat").classList.toggle("alert-heartbeat-bad", beat.bad);
  $("alert-dismiss").classList.toggle("hidden", !unseen.length && !recentRecoveries().length);
  $("alert-history").textContent = $("alert-list").classList.contains("hidden") ? `History (${a.events.length})` : "Hide history";
  if (!$("alert-list").classList.contains("hidden")) renderAlertList();
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-alert-export]");
  if (!button) return;
  const fmt = button.dataset.alertExport;
  download("api/alerts/export", { format: fmt }, `alerts.${fmt}`).catch((error) => showMessage(error.message, true));
});

function renderAlertList() {
  const a = state.alerts;
  const exportLink = (fmt, label) => `<button type="button" class="link" data-alert-export="${fmt}">${label}</button>`;
  $("alert-list").innerHTML = a.events.length ? `<div class="alert-export muted">Export: ${exportLink("md", "Markdown")} ·
      ${exportLink("csv", "CSV")} · ${exportLink("pdf", "PDF")}</div>
    <table><tr><th>When</th><th>Status</th><th>Alert</th><th>Rule</th><th>Detail</th><th></th></tr>
    ${a.events.map((e) => `<tr class="${unseenAlerts().includes(e) ? "new" : ""} ${e.resource_id ? "clickable" : ""}"
        ${e.resource_id ? `data-open="${esc(JSON.stringify({ kind: "resource", id: e.resource_id, name: e.name || e.resource_id.split("/").pop() }))}"` : ""}>
      <td title="${esc(e.at)}">${esc(new Date(e.epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</td>
      <td>${pill(e.status)}${e.resolved_at ? `<div class="ok" title="${esc(e.resolved_note || "")}">✓ resolved ${esc(new Date(e.resolved_epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</div>` : ""}</td>
      <td><strong>${esc(e.title)}</strong>${e.resolved_note && e.resolved_note !== "back to normal" ? `<div class="muted">${esc(e.resolved_note)}</div>` : ""}</td>
      <td>${esc(e.rule)}</td><td>${escTimes(e.detail)}</td><td class="alert-actions">${e.resource_id ? browseButtonHtml() + " " : ""}${alertActions(e)}</td></tr>`).join("")}</table>
    <p class="muted">The last ${alertHours(a)}, kept in the console's memory: restarting it clears this and records a new starting point.</p>`
    : `<p class="muted">No alerts in the last ${alertHours(a)}.</p>`;
}

const alertHours = (a) => ((a.history_hours || 24) === 1 ? "hour" : `${a.history_hours || 24} hours`);

function alertActions(e) {
  if (!isOpen(e)) return "";
  const now = serverNow();
  const parts = [];
  if (e.acknowledged_at) parts.push('<span class="muted">acknowledged</span>');
  else parts.push(`<button type="button" class="link" data-alert-ack="${e.seq}">acknowledge</button>`);
  if ((e.snoozed_until || 0) > now) {
    parts.push(`<span class="muted">snoozed until ${esc(hhmm(e.snoozed_until))}</span>
      <button type="button" class="link" data-alert-unsnooze="${e.seq}">wake</button>`);
  } else if (!e.acknowledged_at) parts.push(`<button type="button" class="link" data-alert-snooze="${e.seq}">snooze 1 h</button>`);
  return parts.join(" ");
}

function alertCursor() {
  return stored.json("ahc.alertSeen", null);
}

// One notification per alert across tabs: a shared cursor, and the tag lets the browser merge duplicates.
function notifyAlerts(a) {
  const top = a.events.length ? a.events[0].seq : 0;
  const cursor = alertCursor();
  const runner = a.runner_id;
  const seen = !cursor ? top : cursor.runner === runner ? cursor.seq : 0;  // first visit: don't replay old alerts
  stored.set("ahc.alertSeen", { runner, seq: Math.max(seen, top) });
  const fresh = a.events.filter((e) => e.seq > seen && e.notify);
  if (!fresh.length || !window.Notification || Notification.permission !== "granted") return;
  try {
    const shown = fresh.length > 3 ? [{ title: `${fresh.length} new alerts`, detail: fresh.slice(0, 3).map((e) => e.title).join("\n"), seq: top }] : fresh;
    for (const e of shown) {
      const note = new Notification(e.title, { body: e.rule ? `${e.rule}: ${e.detail || ""}` : e.detail, tag: `ahc-${runner}-${e.seq}` });
      note.onclick = () => {
        window.focus();
        if (e.resource_id) startSnapshot({ kind: "resource", id: e.resource_id, name: e.name || e.resource_id.split("/").pop() });
      };
    }
  } catch { /* notifications unavailable */ }
}

function openAlertSettings(edit = null) {
  state.settingsPane = "alerts";
  state.alertEdit = edit;
  if (state.view === "settings") { showSettingsPane("alerts"); renderAlertRules(); } else showView("settings");
}

$("alert-bar").addEventListener("click", async (event) => {
  if (event.target.closest("[data-alert-settings]")) { openAlertSettings(); return; }
  if (event.target.id === "alert-history") {
    $("alert-list").classList.toggle("hidden");
    renderAlertBar();
    return;
  }
  if (event.target.id === "alert-dismiss") {
    const top = state.alerts.events.length ? state.alerts.events[0].seq : 0;
    try { setAlerts(await api("api/alerts/dismiss", { method: "POST", body: { seq: top } })); } catch (error) { showMessage(error.message, true); }
    return;
  }
  const action = event.target.closest("[data-alert-ack], [data-alert-snooze], [data-alert-unsnooze]");
  if (action) {
    const d = action.dataset;
    try {
      setAlerts(d.alertAck ? await api("api/alerts/acknowledge", { method: "POST", body: { seq: Number(d.alertAck) } })
        : await api("api/alerts/snooze", { method: "POST", body: { seq: Number(d.alertSnooze || d.alertUnsnooze), minutes: d.alertSnooze ? 60 : 0 } }));
    } catch (error) { showMessage(error.message, true); }
    return;
  }
  const row = event.target.closest("tr[data-open]");
  if (row) startSnapshot(JSON.parse(row.dataset.open));
});

// ---- Settings → Alerts

function editableRule(r) {
  return { name: r.name, kind: r.kind, scope: r.kind === "scope" ? r.scope : null, signals: [...r.signals],
    interval_minutes: r.interval_minutes, notify: r.notify, notify_recovered: r.notify_recovered !== false, enabled: r.enabled,
    servicenow_priority: r.servicenow_priority || 2 };
}

// What the last check saw, in one line: "nothing new · tracking 2 red, 4 yellow · 1 open incident".
function ruleResult(r) {
  const x = r.result;
  if (!x) return "";
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const head = x.first ? "Starting point recorded" : x.new ? `<strong>${plural(x.new, "new alert")}</strong>` : "nothing new";
  const known = [x.critical && `${x.critical} red`, x.warning && `${x.warning} yellow`].filter(Boolean).join(", ");
  const parts = [known ? `tracking ${known}` : r.kind === "watchlist" ? `${plural(x.items, "item")} all clear` : "nothing red or yellow"];
  if (x.service_health) parts.push(plural(x.service_health, "Service Health event"));
  if (x.servicenow) parts.push(plural(x.servicenow, "open incident"));
  if (x.monitors) parts.push(plural(x.monitors, "open monitoring alert"));
  if (x.movers) parts.push(`${plural(x.movers, "metric")} off last week`);
  return `${head} · ${parts.join(" · ")}`;
}

function ruleStatus(r) {
  if (!r.enabled) return '<span class="muted">off</span>';
  const snoozed = r.snoozed_until ? `<div class="stale">Snoozed until ${esc(hhmm(r.snoozed_until))}: still checking and recording, quiet until then.</div>` : "";
  const setup = (r.setup || []).length ? `<div class="notice partial">Some signals can't be read:<ul>${r.setup.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
  if (r.cant_run) return `${pillHtml("warning", "can't run")} ${esc(r.cant_run)}`;
  const parts = [];
  if (r.running) parts.push("checking now…");
  if (r.failing) parts.push(`${pillHtml("critical", "failing")} ${esc(r.error || "")}`);
  if (r.last_check) parts.push(`last check ${esc(ago(r.last_check))}${r.result && !r.failing ? `: ${ruleResult(r)}` : ""}`);
  else if (!r.running) parts.push("first check starting");
  if (r.next_due) parts.push(`next in ${esc(duration(r.next_due - serverNow()))}`);
  if (r.checks === 1 && !r.failing) parts.push("alerts from the next check on");
  let html = parts.join(" · ");
  if (r.partial && r.partial.length) html += `<div class="notice partial">Couldn't read this time (not counted as a change):<ul>${r.partial.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>`;
  html += `<div class="muted">${r.calls != null ? `${r.calls} Azure call${r.calls === 1 ? "" : "s"} last check` : `Each check: ${esc(r.estimate)}`}</div>`;
  return snoozed + html + setup;
}

function notificationNotice(a) {
  if (!a.rules.some((r) => r.enabled && r.notify)) return "";
  if (!window.Notification) return '<p class="notice">This browser can\'t show notifications. The bar at the top still shows every alert.</p>';
  if (Notification.permission === "denied") {
    return `<p class="notice">Browser notifications are blocked for this page. Allow them in the browser's site settings for
      ${esc(location.host)}, or untick <strong>Browser notification</strong> on the rules. The bar at the top still shows every alert.</p>`;
  }
  if (Notification.permission === "default") {
    return `<p class="notice">The browser hasn't been asked to show notifications yet.
      <button type="button" data-alert-permission>Allow browser notifications</button></p>`;
  }
  return "";
}

function renderAlertRules() {
  const box = $("alerts-body");
  if (!box) return;
  const a = state.alerts;
  if (!a) { box.innerHTML = loading("Reading alert rules…"); pollAlerts(); return; }
  const notices = [];
  if (!a.background) notices.push('<p class="notice">This console was started without background checks, so rules run only when you press <strong>Check now</strong>.</p>');
  if (a.error) notices.push(`<p class="notice">The rules couldn't be read: ${esc(a.error)}</p>`);
  if (state.legacyWatchMinutes && !a.rules.some((r) => r.kind === "watchlist")) {
    notices.push(`<p class="notice">The Watchlist's Auto-check (every ${state.legacyWatchMinutes} min, set in this browser) is now an alert rule,
      and it no longer runs from the page. <button type="button" data-alert-legacy>Set it up as a rule</button></p>`);
  }
  notices.push(notificationNotice(a));
  const rows = a.rules.map((r, i) => `<tr>
      <td><strong>${esc(r.name)}</strong></td>
      <td>${r.kind === "watchlist" ? "The Watchlist" : `Saved scope <strong>${esc(r.scope)}</strong>`}</td>
      <td>${r.signal_names.map(esc).join("<br>")}</td>
      <td>every ${r.interval_minutes} min<br>${r.notify ? `banner + notification${r.notify_recovered !== false ? ", also on recovery" : ""}` : "banner"}</td>
      <td>${ruleStatus(r)}</td>
      <td>${r.enabled && !r.cant_run ? `<button type="button" data-alert-run="${i}">Check now</button>
          <button type="button" class="link" data-alert-rule-snooze="${i}">${r.snoozed_until ? "wake" : "snooze 1 h"}</button>` : ""}
        <button type="button" class="link" data-alert-edit="${i}">edit</button>
        <button type="button" class="link" data-alert-delete="${i}">delete</button></td></tr>`).join("");
  box.innerHTML = `${notices.join("")}
    ${a.rules.length ? `<table class="alert-rules"><tr><th>Rule</th><th>Watches</th><th>Alerts when</th><th>Checks</th><th>State</th><th></th></tr>${rows}</table>`
      : '<p class="muted">No alert rules for this tenant yet.</p>'}
    ${state.alertEdit ? alertForm(a, state.alertEdit) : '<button type="button" data-alert-add>Add a rule</button>'}
    <p class="muted">Kept in config.yaml under this tenant (<code>alert_rules</code>). A rule watches a saved scope (save one from the
      Scope bar) or the Watchlist. The first check records a starting point; after that a rule alerts when a status gets worse or
      something new appears.</p>`;
}

function newRule(a, kind) {
  kind = kind || (a.saved_scopes.length ? "scope" : "watchlist");
  return { index: -1, rule: { name: kind === "watchlist" ? "Watchlist" : a.saved_scopes[0], kind,
    scope: kind === "scope" ? a.saved_scopes[0] : null, signals: defaultSignals(kind), servicenow_priority: 2,
    interval_minutes: kind === "watchlist" && state.legacyWatchMinutes ? Math.max(a.minutes.min, state.legacyWatchMinutes) : 5,
    notify: true, notify_recovered: true, enabled: true } };
}

function alertForm(a, edit) {
  const r = edit.rule;
  const floor = r.kind === "watchlist" ? a.minutes.min : a.minutes.scope_min;
  const scopes = [...a.saved_scopes];
  if (r.kind === "scope" && r.scope && !scopes.includes(r.scope)) scopes.push(r.scope);
  const watches = [
    ...scopes.map((name) => `<option value="scope:${esc(name)}" ${r.kind === "scope" && r.scope === name ? "selected" : ""}>Saved scope: ${esc(name)}${a.saved_scopes.includes(name) ? "" : " (missing)"}</option>`),
    `<option value="watchlist" ${r.kind === "watchlist" ? "selected" : ""}>The Watchlist</option>`,
  ].join("");
  const minutes = ALERT_MINUTE_CHOICES.filter((m) => m <= a.minutes.max).map((m) =>
    `<option value="${m}" ${m === r.interval_minutes ? "selected" : ""} ${m < floor ? "disabled" : ""}>every ${m} min${m < floor ? " (scopes: 5 min or more)" : ""}</option>`).join("");
  const conns = (r.kind === "scope" && a.scope_connections[r.scope]) || {};
  const monitorsNamed = Object.entries(conns).filter(([k]) => k !== "servicenow").map(([, v]) => v);
  const via = {
    servicenow: conns.servicenow ? `uses ${conns.servicenow}, from the saved scope` : "the saved scope has no ServiceNow connection: pick one in the Scope bar, then Saved scopes → Update",
    monitors: monitorsNamed.length ? `uses ${monitorsNamed.join(", ")}, from the saved scope` : "the saved scope has no monitoring connection: pick one in the Scope bar, then Saved scopes → Update",
    last_week: "from the metrics each check already reads; it doesn't judge direction",
  };
  const signals = signalKeys(r.kind).map((key) => `<label><input type="checkbox" data-alert-signal="${key}" ${r.signals.includes(key) ? "checked" : ""}> ${esc(a.signals[key])}
      ${via[key] ? `<span class="muted">(${esc(via[key])})</span>` : ""}</label>
      ${key === "servicenow" ? `<label class="indent">Priority <select id="alert-sn-priority">${SN_PRIORITIES.map(([v, l]) => `<option value="${v}" ${v === (r.servicenow_priority || 2) ? "selected" : ""}>${l}</option>`).join("")}</select>
        <span class="muted">only incidents naming a resource in scope by its ID or a distinctive name</span></label>` : ""}`).join("");
  const cost = r.kind === "watchlist"
    ? `${(state.watchItems || []).length} quick check(s) per run, one per Watchlist item`
    : `about 7 Resource Graph queries per check across the scope, whatever its size${r.signals.some((x) => x === "servicenow" || x === "monitors")
      ? ", plus one more for the resources records are matched against, and the saved scope's ServiceNow or monitoring reads (cached 60 s)" : ""}`;
  return `<div class="alert-form" id="alert-form">
    <label for="alert-name">Name</label><input id="alert-name" value="${esc(r.name)}" maxlength="60">
    <label for="alert-watches">Watches</label><select id="alert-watches">${watches}</select>
    <span>Alert when</span><div class="signals">${signals}</div>
    <label for="alert-minutes">Check</label><select id="alert-minutes">${minutes}</select>
    <span></span><span class="muted">${esc(cost)}</span>
    <span>Tell me</span><div><label><input type="checkbox" checked disabled> Banner on every tab</label>
      <label><input id="alert-notify" type="checkbox" ${r.notify ? "checked" : ""}> Browser notification</label>
      <label><input id="alert-notify-recovered" type="checkbox" ${r.notify_recovered !== false ? "checked" : ""}> …and one when it's back to normal</label></div>
    <span></span><label><input id="alert-enabled" type="checkbox" ${r.enabled ? "checked" : ""}> On</label>
    ${!a.saved_scopes.length ? '<p class="muted full">No saved scopes in this tenant yet: pick a scope in the Scope bar and use Saved scopes → Save current as… to watch it.</p>' : ""}
    <div id="alert-form-error" class="full"></div>
    <div class="full"><button type="button" class="primary" data-alert-save>Save rule</button> <button type="button" data-alert-cancel>Cancel</button></div>
  </div>`;
}

function readAlertForm() {
  const edit = state.alertEdit;
  const watches = $("alert-watches").value;
  const kind = watches === "watchlist" ? "watchlist" : "scope";
  const signals = [...document.querySelectorAll("[data-alert-signal]")].filter((c) => c.checked).map((c) => c.dataset.alertSignal);
  const priority = $("alert-sn-priority");
  edit.rule = { ...edit.rule, name: $("alert-name").value.trim(), kind, scope: kind === "scope" ? watches.slice(6) : null,
    servicenow_priority: priority ? Number(priority.value) : edit.rule.servicenow_priority || 2,
    signals, interval_minutes: Number($("alert-minutes").value), notify: $("alert-notify").checked,
    notify_recovered: $("alert-notify-recovered").checked, enabled: $("alert-enabled").checked };
  return edit;
}

async function saveAlertRules(rules, onError) {
  try {
    setAlerts(await api("api/alerts/rules", { method: "POST", body: { rules } }));
    return true;
  } catch (error) {
    onError(error.message);
    return false;
  }
}

const defaultRuleName = (rule) => (rule.kind === "watchlist" ? "Watchlist" : rule.scope);

$("alerts-body").addEventListener("change", (event) => {
  if (event.target.id !== "alert-watches") return;
  const before = { ...state.alertEdit.rule };
  const edit = readAlertForm();
  if (!edit.rule.name || edit.rule.name === defaultRuleName(before)) edit.rule.name = defaultRuleName(edit.rule);  // untouched names follow
  const kind = edit.rule.kind;
  if (edit.kindWas !== kind) {
    edit.rule.signals = defaultSignals(kind);
    if (kind === "scope") edit.rule.interval_minutes = Math.max(edit.rule.interval_minutes, state.alerts.minutes.scope_min);
    edit.kindWas = kind;
  }
  renderAlertRules();
});

$("alerts-body").addEventListener("click", async (event) => {
  const el = event.target.closest("button");
  if (!el) return;
  const a = state.alerts;
  const index = Number(el.dataset.alertRun ?? el.dataset.alertEdit ?? el.dataset.alertDelete ?? el.dataset.alertRuleSnooze);
  if (el.dataset.alertPermission !== undefined) {
    Notification.requestPermission().then(renderAlertRules).catch(() => {});
  } else if (el.dataset.alertAdd !== undefined) {
    state.alertEdit = newRule(a);
    state.alertEdit.kindWas = state.alertEdit.rule.kind;
    renderAlertRules();
  } else if (el.dataset.alertLegacy !== undefined) {
    state.alertEdit = newRule(a, "watchlist");
    state.alertEdit.kindWas = "watchlist";
    renderAlertRules();
  } else if (el.dataset.alertEdit !== undefined) {
    state.alertEdit = { index, rule: editableRule(a.rules[index]), kindWas: a.rules[index].kind };
    renderAlertRules();
  } else if (el.dataset.alertCancel !== undefined) {
    state.alertEdit = null;
    renderAlertRules();
  } else if (el.dataset.alertSave !== undefined) {
    const edit = readAlertForm();
    if (edit.rule.notify && window.Notification && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});  // asked while the click still counts as the user's
    }
    const rules = a.rules.map(editableRule);
    if (edit.index >= 0) rules[edit.index] = edit.rule; else rules.push(edit.rule);
    state.alertEdit = null;
    const saved = await saveAlertRules(rules, (message) => {
      state.alertEdit = edit;
      renderAlertRules();
      $("alert-form-error").innerHTML = noticeHtml(message);
    });
    if (saved && edit.rule.kind === "watchlist") {
      state.legacyWatchMinutes = 0;
      stored.set("ahc.watchInterval", null);
    }
  } else if (el.dataset.alertDelete !== undefined) {
    if (!confirm(`Delete the alert rule "${a.rules[index].name}"?`)) return;
    await saveAlertRules(a.rules.filter((_, i) => i !== index).map(editableRule), (message) => showMessage(message, true));
  } else if (el.dataset.alertRuleSnooze !== undefined) {
    const rule = a.rules[index];
    try { setAlerts(await api("api/alerts/snooze", { method: "POST", body: { rule: rule.name, minutes: rule.snoozed_until ? 0 : 60 } })); }
    catch (error) { showMessage(error.message, true); }
  } else if (el.dataset.alertRun !== undefined) {
    el.disabled = true;
    el.textContent = "Checking…";
    try { setAlerts(await api("api/alerts/run", { method: "POST", body: { name: a.rules[index].name } })); }
    catch (error) { showMessage(error.message, true); }
    if (a.background) setTimeout(pollAlerts, 3000);  // the background runner picks it up within a moment
  }
});

// ---- the Watchlist's rule

function watchRule() {
  return state.alerts && state.alerts.rules.find((r) => r.kind === "watchlist");
}

function mergeWatchRule() {
  const rule = watchRule();
  if (!rule || !rule.latest || !rule.last_check || rule.last_check < (state.watchManualAt || 0)) return;
  for (const row of rule.latest) state.watchResults[row.id.toLowerCase()] = row;
}

function renderWatchRuleLine() {
  const box = $("watch-rule");
  const rule = watchRule();
  if (rule && rule.enabled) {
    box.innerHTML = `Auto-check every ${rule.interval_minutes} min${rule.last_check ? `, last ${esc(ago(rule.last_check))}` : ""}
      ${rule.cant_run || rule.failing ? `<span class="stale">(${esc(rule.cant_run || rule.error)})</span>` : ""}
      <button type="button" class="link" data-alert-settings>change</button>`;
  } else {
    box.innerHTML = `Auto-check ${rule ? "off" : "not set up"}${state.legacyWatchMinutes ? " (it moved from this page to an alert rule)" : ""}
      <button type="button" class="link" data-alert-settings>${rule ? "turn on" : "set up"}</button>`;
  }
}

$("watch-rule").addEventListener("click", (event) => {
  if (!event.target.closest("[data-alert-settings]")) return;
  const rule = watchRule();
  if (rule) openAlertSettings({ index: state.alerts.rules.indexOf(rule), rule: editableRule(rule), kindWas: "watchlist" });
  else if (state.alerts) openAlertSettings({ ...newRule(state.alerts, "watchlist"), kindWas: "watchlist" });
});

