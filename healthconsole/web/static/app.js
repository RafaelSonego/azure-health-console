"use strict";

const BASE = document.querySelector("script[src*='static/app.js']").src.replace(/static\/app\.js.*$/, "");
const $ = (id) => document.getElementById(id);
const state = {
  tenant: null, openWith: "none", legacyScope: null, view: null, target: null, partialDrawn: false, triageLoaded: false,
  aksSweep: null, watchResults: {}, applied: null, connDraft: {},
  pendingTagKey: "",
  subscriptions: [], groups: [], resources: [],
  scope: { subs: new Set(), groups: new Set(), tags: {} }, browseGroup: null, selectedResource: null,
  jobId: null, snapshot: null, busy: false, graphAuto: false, groupView: "table",
  secrets: null, secretsFilter: "problems", network: null, networkLoaded: false,
};

// Safe in text and in attributes: quotes too, since JSON lives in data-* attributes.
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(text) {
  return (text == null ? "" : String(text)).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

// Server text carries times as "2026-11-15 01:17 UTC" (exports keep that); on the page they read in local time, like the
// rest of the page, with the UTC original on hover.
const UTC_TIME = /(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC/g;
function escTimes(text) {
  return esc(text).replace(UTC_TIME, (whole, day, time) => `<span class="utc" title="${whole}">${esc(dayTime(`${day}T${time}:00Z`))}</span>`);
}

// Browser storage, for per-viewer conveniences only (a window, a sort, a theme). It can be missing or throw (a private
// window, blocked site data), so every read has a fallback and a failed write is dropped.
const stored = {
  get(key, fallback = null) {
    try { const value = localStorage.getItem(key); return value === null ? fallback : value; } catch { return fallback; }
  },
  json(key, fallback) {
    try { const value = localStorage.getItem(key); return value === null ? fallback : JSON.parse(value); } catch { return fallback; }
  },
  choice(key, allowed, fallback) {  // the saved value only while it's still one of `allowed`
    const value = this.get(key);
    return allowed.includes(value) ? value : fallback;
  },
  set(key, value) {  // null removes it; anything but text is saved as JSON
    try {
      if (value == null) localStorage.removeItem(key);
      else localStorage.setItem(key, typeof value === "string" ? value : JSON.stringify(value));
    } catch { /* storage may be unavailable */ }
  },
};

// A Refresh button: every read `work` starts asks Azure again instead of the server's one-minute cache.
async function freshly(work) {
  state.fresh = (state.fresh || 0) + 1;
  try { return await work(); } finally { state.fresh -= 1; }
}

async function api(path, options = {}) {
  const url = new URL(BASE + path.replace(/^\//, ""));
  if (state.tenant) url.searchParams.set("tenant", state.tenant);
  if (state.fresh) url.searchParams.set("fresh", "1");
  for (const [key, value] of Object.entries(options.params || {})) {
    for (const item of [].concat(value)) url.searchParams.append(key, item);
  }
  const init = { method: options.method || "GET", headers: {} };
  if (options.body) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify({ tenant: state.tenant, ...options.body });
  }
  const response = await fetch(url, init);
  if (options.raw && response.status !== 401) return response;
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && payload.kind === "credential") {
    showSignIn(payload);
    const error = new Error("Not signed in to Azure.");
    error.signedOut = true;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(payload.error || `${response.status} ${response.statusText}`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

// Copies text; `button` (if given) says "Copied". When the browser blocks the clipboard, the text is shown to copy by
// hand instead of failing silently. Resolves true when it reached the clipboard.
async function copyText(text, button = null, label = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
    if (button) button.textContent = label;
    return true;
  } catch {
    window.prompt("Copy this:", text);
    return false;
  }
}

// One way to wait for a job (start, poll, result). `onRunning(job)` on each poll while it runs (its phase, a partial
// result); `isCurrent()` false stops quietly (a newer scope or user replaced it); `onLost()` answers when the job
// itself is gone. Resolves with the finished or cancelled job, or null once replaced; throws with the job's error.
async function pollJob(jobId, { onRunning = () => {}, isCurrent = () => true, onLost = null, every = 1000, failed = "The check failed." } = {}) {
  for (;;) {
    let job;
    try {
      job = await api(`api/checks/${jobId}`);
    } catch (error) {
      if (onLost) return onLost(error);
      throw error;
    }
    if (!isCurrent()) return null;
    if (job.status === "running") {
      onRunning(job);
      await new Promise((resolve) => setTimeout(resolve, every));
      continue;
    }
    if (job.status === "failed") throw jobError(job, failed);
    return job;
  }
}

// A failed job's error; signed out while it ran, the sign-in screen instead of the CLI's message.
function jobError(job, fallback) {
  if (job.credential) {
    showSignIn({ ...job.credential, error: job.error });
    const error = new Error("Not signed in to Azure.");
    error.signedOut = true;
    return error;
  }
  return new Error(job.error || fallback);
}

// ------------------------------------------------------------------ sign-in
// Every Azure call needs the Azure CLI session. When it's missing or expired, one screen says what to run,
// and the page carries on by itself once `az login` has worked.

const SIGNIN_CHECK_SECONDS = 5;

function signInText(info) {
  const tenant = state.tenantName || state.tenant || "";
  if (info.reason === "cli_missing") {
    return { title: "Install the Azure CLI to continue",
      lead: "The console signs in through the Azure CLI (<code>az</code>), and it isn't installed on this machine, or isn't on the PATH.",
      first: 'Install it: <a href="https://learn.microsoft.com/cli/azure/install-azure-cli" target="_blank" rel="noopener">learn.microsoft.com/cli/azure/install-azure-cli</a>, then open a new terminal and run' };
  }
  if (info.reason === "tenant") {
    return { title: "Sign in to this tenant to continue",
      lead: `Your Azure CLI session doesn't cover the tenant this page is using${tenant ? ` (<code>${esc(tenant)}</code>)` : ""}.`,
      first: "In a terminal, run" };
  }
  return { title: "Sign in to Azure to continue",
    lead: "The console reads Azure with your Azure CLI session, and that session is signed out or has expired.",
    first: "In a terminal, run" };
}

function showSignIn(info) {
  const text = signInText(info);
  const command = info.command || "az login";
  document.body.classList.add("signed-out");
  $("signin").classList.remove("hidden");
  $("signin").innerHTML = `<div class="signin-card" role="alert">
    <h2>${text.title}</h2>
    <p>${text.lead}</p>
    <div data-signin-box class="signin-quick hidden"></div>
    <p class="signin-or muted hidden">Or do it yourself:</p>
    <ol>
      <li>${text.first} <code class="command">${esc(command)}</code>
        <button type="button" id="signin-copy">Copy</button></li>
      <li>Complete the sign-in in the browser window it opens. If your access comes through PIM, activate it too.</li>
      <li>Come back here: the page checks every ${SIGNIN_CHECK_SECONDS} seconds and carries on by itself.</li>
    </ol>
    <div class="signin-actions"><button type="button" id="signin-retry">I've signed in, check now</button>
      ${state.tenant ? '<button type="button" id="signin-default">Use my default tenant instead</button>' : ""}
      <span id="signin-status" class="muted">${loading("Waiting for sign-in…")}</span></div>
    <details><summary class="muted">What the Azure CLI said</summary><pre class="logs">${esc(info.error || "")}</pre></details>
    <p class="muted">Nothing is lost: the console keeps no data of its own, only your settings and Watchlist files.</p>
  </div>`;
  $("signin-copy").addEventListener("click", () => copyText(command, $("signin-copy")));
  $("signin-retry").addEventListener("click", () => checkSignIn(true));
  const fallback = $("signin-default");
  if (fallback) fallback.addEventListener("click", () => { location.href = BASE; });
  clearInterval(state.signInTimer);
  state.signInTimer = setInterval(checkSignIn, SIGNIN_CHECK_SECONDS * 1000);
  if (info.reason !== "cli_missing") {
    signInAvailable().then((ok) => {
      const box = document.querySelector("#signin [data-signin-box]");
      if (!ok || !box) return;
      box.classList.remove("hidden");
      document.querySelector("#signin .signin-or").classList.remove("hidden");
      renderSignInControls(box, info.reason === "tenant" ? state.tenant || "" : "");
    });
  }
}

async function checkSignIn(manual = false) {
  const url = new URL(`${BASE}api/context`);
  url.searchParams.set("verify", "1");
  if (state.tenant && state.tenantPinned) url.searchParams.set("tenant", state.tenant);  // else: whatever az login is on now
  if (manual && $("signin-status")) $("signin-status").innerHTML = loading("Checking…");
  try {
    const response = await fetch(url);
    if (response.status === 401) {
      if (manual && $("signin-status")) $("signin-status").textContent = "Still not signed in. Run the command above, then try again.";
      return;
    }
  } catch { return; }  // the console itself stopped; keep waiting
  clearInterval(state.signInTimer);
  // Simplest way back to a consistent page: start again, now signed in.
  location.reload();
}

// ------------------------------------------------------------------ Sign in button (local console only)
// The local server can run `az login` itself; a hosted copy has no /api/signin, so the buttons never show there.

const SIGNIN_POLL_MS = 1500;

// Signs one account out of the Azure CLI (after a confirm), then reloads: the CLI's default account, if any is left,
// opens; with none, the sign-in screen does.
async function signOut(account) {
  if (!confirm(`Sign ${account} out of the Azure CLI on this machine? Terminals using az are signed out too.`)) return;
  const out = $("signout-out");
  out.innerHTML = loading(`Signing ${account} out…`);
  try {
    const response = await fetch(`${BASE}api/signout`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: account }) });
    const data = await response.json();
    if (!response.ok) { out.innerHTML = noticeHtml(data.error || "The sign-out didn't work."); return; }
  } catch (error) {
    out.innerHTML = noticeHtml(error);
    return;
  }
  const url = new URL(location.href);
  url.searchParams.delete("tenant");
  location.href = url.toString();
}

// Every tenant the Azure CLI is signed in to, any account: added to the tenant picker, labelled with the account
// when it isn't the one in use, so a personal and a work tenant can sit side by side.
async function mergeCliTenants() {
  if (!(await signInAvailable())) return;
  let data;
  try { data = await (await fetch(`${BASE}api/signin/accounts`)).json(); } catch { return; }
  state.cliTenants = data.tenants || [];
  const select = $("tenant");
  const known = new Set([...select.options].map((o) => o.value));
  const current = ((state.cliTenants.find((t) => t.id === state.tenant) || {}).account || "").toLowerCase();
  for (const t of state.cliTenants) {
    const label = t.account && t.account.toLowerCase() !== current ? `${t.name} (${t.account})` : t.name;
    if (known.has(t.id)) { const option = [...select.options].find((o) => o.value === t.id); if (option) option.textContent = label; continue; }
    select.insertAdjacentHTML("beforeend", `<option value="${esc(t.id)}">${esc(label)}</option>`);
  }
}

async function signInAvailable() {
  if (state.signinAvailable !== undefined) return state.signinAvailable;
  try {
    const response = await fetch(`${BASE}api/signin`);
    state.signinAvailable = response.ok;
  } catch { state.signinAvailable = false; }
  return state.signinAvailable;
}

// Buttons plus live status, rendered into `box`; `tenant` (an ID or domain) adds --tenant.
function renderSignInControls(box, tenant) {
  box.innerHTML = `<div class="signin-run">
      <button type="button" class="primary" data-signin-start>${tenant ? "Sign in to this tenant" : "Sign in"}</button>
      <button type="button" data-signin-code>Use a code instead</button>
      <span class="muted">Runs <code>az login${tenant ? ` --tenant ${esc(tenant)}` : ""}</code> for you: your browser opens for
        the sign-in. It updates the Azure CLI's sign-in on this machine, so terminals use it too.</span>
    </div>
    <div class="signin-progress"></div>`;
  box.dataset.tenant = tenant || "";
  signInPoll(box, false);
}

async function startSignIn(box, deviceCode) {
  const progress = box.querySelector(".signin-progress");
  progress.innerHTML = loading("Starting az login…");
  try {
    const response = await fetch(`${BASE}api/signin`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant: box.dataset.tenant || null, device_code: deviceCode }) });
    const data = await response.json();
    if (!response.ok) { progress.innerHTML = noticeHtml(data.error || "The sign-in couldn't start."); return; }
  } catch (error) {
    progress.innerHTML = noticeHtml(error);
    return;
  }
  signInPoll(box, true);
}

async function signInPoll(box, started) {
  clearTimeout(box._signinTimer);
  let data;
  try { data = await (await fetch(`${BASE}api/signin`)).json(); } catch { return; }
  if (!box.isConnected) return;
  const progress = box.querySelector(".signin-progress");
  const buttons = box.querySelectorAll("[data-signin-start], [data-signin-code]");
  buttons.forEach((b) => { b.disabled = data.status === "running"; });
  const output = (data.output || []).length ? `<details><summary class="muted">What az login said</summary>
    <pre class="logs">${esc(data.output.join("\n"))}</pre></details>` : "";
  if (data.status === "running") {
    progress.innerHTML = data.code
      ? `<div class="device-code">Open <a href="${esc(data.url)}" target="_blank" rel="noopener">${esc(data.url)}</a> on any device
          and enter <code class="command">${esc(data.code)}</code> <button type="button" data-signin-copy="${esc(data.code)}">Copy code</button>
          <button type="button" class="link" data-signin-cancel>cancel</button></div>`
      : `${loading(data.device_code ? "Waiting for the sign-in code…" : "Finish the sign-in in the browser window that opened…")}
          <button type="button" class="link" data-signin-cancel>cancel</button>
          ${data.device_code ? "" : '<p class="muted">No window? Cancel and use a code instead.</p>'}`;
    box._signinTimer = setTimeout(() => signInPoll(box, true), SIGNIN_POLL_MS);
  } else if (data.status === "done" && started) {
    progress.innerHTML = loading("Signed in. Loading your access…");
    clearInterval(state.signInTimer);
    setTimeout(() => location.reload(), 600);
  } else if ((data.status === "failed" || data.status === "cancelled") && started) {
    progress.innerHTML = `${noticeHtml(data.message || "The sign-in didn't finish.")}${output}`;
  } else {
    progress.innerHTML = "";
  }
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-signin-start], [data-signin-code], [data-signin-cancel], [data-signin-copy]");
  if (!el) return;
  const box = el.closest("[data-signin-box]");
  if (el.dataset.signinCopy) {
    await copyText(el.dataset.signinCopy, el);
  } else if (el.dataset.signinCancel !== undefined) {
    await fetch(`${BASE}api/signin/cancel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
    if (box) signInPoll(box, true);
  } else if (box) {
    startSignIn(box, el.dataset.signinCode !== undefined);
  }
});

$("signin-again").addEventListener("click", async () => {
  const panel = $("signin-panel");
  if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const name = state.tenantName || state.tenant;
  const account = ((state.cliTenants || []).find((t) => t.id === state.tenant) || {}).account || "";
  panel.innerHTML = `<div class="signin-panel-head"><strong>Sign in or out</strong>
      <span class="muted">Sign in to switch account, add a tenant (another account's too), or after activating a PIM group
        (Refresh access is enough for Azure roles).</span>
      <span class="spacer"></span><button type="button" class="link" id="signin-panel-close">close</button></div>
    <label class="inline"><input type="radio" name="signin-tenant" value="${esc(state.tenant || "")}" checked> this tenant (${esc(name || "current")})</label>
    <label class="inline"><input type="radio" name="signin-tenant" value=""> my default tenant</label>
    <label class="inline"><input type="radio" name="signin-tenant" value="other"> another tenant</label>
    <input id="signin-other" type="text" class="hidden" placeholder="Tenant ID or domain, e.g. contoso.onmicrosoft.com" autocomplete="off"
      aria-label="Tenant ID or domain">
    <div data-signin-box></div>
    ${account ? `<div class="signout-row"><span class="muted">Signed in to ${esc(name)} as <strong>${esc(account)}</strong>.</span>
      <button type="button" id="signout-run">Sign out ${esc(account)}</button>
      <span class="muted">Runs <code>az logout --username ${esc(account)}</code>: it signs that account out of the Azure CLI on
        this machine, terminals included. Other accounts stay signed in.</span><div id="signout-out"></div></div>` : ""}`;
  const box = panel.querySelector("[data-signin-box]");
  const other = $("signin-other");
  renderSignInControls(box, state.tenant || "");
  panel.querySelectorAll('input[name="signin-tenant"]').forEach((r) => r.addEventListener("change", () => {
    other.classList.toggle("hidden", r.value !== "other");
    if (r.value === "other") { other.focus(); renderSignInControls(box, other.value.trim()); } else renderSignInControls(box, r.value);
  }));
  other.addEventListener("input", debounce(() => renderSignInControls(box, other.value.trim()), 300));
  if ($("signout-run")) $("signout-run").addEventListener("click", () => signOut(account));
  $("signin-panel-close").addEventListener("click", () => panel.classList.add("hidden"));
});

// ------------------------------------------------------------------ CLI session changes
// `az logout` / `az login` in another terminal: checked when the tab gets focus and every couple of minutes, and
// offered as a switch rather than done under your feet mid-incident.

const SESSION_CHECK_SECONDS = 120;

async function checkSession() {
  if (document.hidden || !state.session) return;
  let now;
  try { now = await api("api/session"); } catch { return; }  // signed out: api() has shown the sign-in screen
  const changed = now.object_id !== state.session.object_id || now.tenant_id !== state.session.tenant_id;
  $("session-bar").classList.toggle("hidden", !changed);
  if (changed) {
    $("session-bar").innerHTML = `<span>Your Azure CLI is now signed in as <strong>${esc(now.name)}</strong> in
      <strong>${esc(now.tenant_name)}</strong>; this page is still showing ${esc(state.tenantName || state.tenant)}.</span>
      <button type="button" id="session-switch" class="primary">Switch to it</button>
      <button type="button" class="link" id="session-keep">Keep this tenant</button>`;
    $("session-switch").addEventListener("click", () => { location.href = BASE; });
    $("session-keep").addEventListener("click", () => { state.session = now; $("session-bar").classList.add("hidden"); });
  }
}

async function startSessionWatch() {
  try { state.session = await api("api/session"); } catch { return; }
  setInterval(checkSession, SESSION_CHECK_SECONDS * 1000);
  window.addEventListener("focus", checkSession);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkSession(); });
}

function showMessage(text, isError = false) {
  const box = $("message");
  box.textContent = text || "";
  box.classList.toggle("hidden", !text);
  box.classList.toggle("error", isError);
}

// A message that clears itself, for something that just happened ("Link copied").
function flash(text, isError = false, ms = 3500) {
  showMessage(text, isError);
  setTimeout(() => { if ($("message").textContent === text) showMessage(""); }, ms);
}

// Why a section has nothing to show: a plain notice, or a red error when something failed outright.
// What stopped a read, said the same way everywhere: a string, or an error from api() whose kind names the cause.
const NOTICE_KIND = { throttled: "Azure is busy", credential: "Signed out", unreachable: "Not reachable" };  // access says "No access to …" itself
function noticeParts(what) {
  const kind = what && typeof what === "object" ? (what.payload && what.payload.kind) || what.kind || "" : "";
  const text = what && typeof what === "object" ? what.message : what;
  return { kind: /isn't reachable|not reachable/i.test(text || "") ? "unreachable" : kind, text: text || "" };
}
function noticeHtml(what, cls = "notice") {
  const { kind, text } = noticeParts(what);
  const label = NOTICE_KIND[kind];
  return `<p class="${cls}${kind ? ` notice-${esc(kind)}` : ""}">${label ? `<strong>${esc(label)}:</strong> ` : ""}${esc(text)}</p>`;
}
const errorHtml = (what) => noticeHtml(what, "message error");

// Every status pill: `tone` is its colour (critical, warning, soon, info, healthy, unknown) and `rank` (data-v) sorts a
// list worst first; it defaults to the tone's place in STATUS_ORDER.
function pillHtml(tone, label, { rank = STATUS_ORDER[tone], title = "" } = {}) {
  return `<span class="pill ${esc(tone)}"${rank != null ? ` data-v="${rank}"` : ""}${title ? ` title="${esc(title)}"` : ""}>${esc(label)}</span>`;
}

// An item's pill: its own badge when it has one (an exposure reads "exposed" in Security's colour), else its status.
const itemPill = (item) => (item.badge ? pillHtml(item.badge.tone, item.badge.label, { title: `status: ${item.status}` }) : pill(item.status));

function pill(status, title = "") {
  return pillHtml(status, status, { title });
}

// Why a resource is the colour it is: its worst signal, in words, for the pill's tooltip.
function whyStatus(node) {
  const worst = (node.signals || []).find((s) => s.status === node.status && ["critical", "warning"].includes(s.status));
  return worst ? `${worst.name}: ${worst.detail}`.slice(0, 300) : node.status === "healthy" ? "No signal is warning or critical" : "";
}

// One spinner + elapsed-seconds counter for everything that loads.
function loading(text, id = "") {
  return `<div class="loading" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span>
    <span ${id ? `id="${id}"` : ""}>${esc(text)}</span><span class="elapsed" data-start="${Date.now()}"></span></div>`;
}

setInterval(() => {
  for (const el of document.querySelectorAll(".elapsed[data-start]")) {
    const seconds = Math.floor((Date.now() - Number(el.dataset.start)) / 1000);
    el.textContent = seconds >= 2 ? `${seconds}s` : "";
  }
}, 1000);

// "as of 10:42", plus how old when the answer came from a cache, so every number on the page carries its age.
function asOfHtml(readAt = null) {
  const now = new Date();
  const at = readAt ? new Date(readAt) : now;
  const minutes = Math.round((now - at) / 60000);
  const age = minutes >= 120 ? `${Math.round(minutes / 60)} h` : `${minutes} min`;
  return `<span class="as-of" title="${esc(at.toLocaleString())}">as of ${esc(at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}${
    minutes >= 2 ? ` · kept ${esc(age)} to spare Azure's limits (Refresh reads again)` : ""}</span>`;
}

// A job that finished while you were elsewhere: a notice that opens its view, gone after 20 s.
function doneToast(view, text) {
  if (state.view === view) return;
  let box = $("toasts");
  if (!box) { box = document.createElement("div"); box.id = "toasts"; box.setAttribute("role", "status"); document.body.append(box); }
  const toast = document.createElement("button");
  toast.type = "button";
  toast.className = "toast";
  toast.textContent = `${text} · Open`;
  toast.addEventListener("click", () => { showView(view); toast.remove(); });
  box.append(toast);
  setTimeout(() => toast.remove(), 20000);
}

function debounce(fn, wait = 350) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
}

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

function scopeGroupsHtml(key, rows, { scopeOf = (r) => r.scope, columns, cells, filtering = false }) {
  return groupedHtml(key, rows, { groupOf: (r) => scopeGroupOf(scopeOf(r)), body: (part) => table(columns, part.map(cells), key), filtering });
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
  if (!state.busy || !state.jobId || !navigator.sendBeacon) return;
  const url = new URL(`${BASE}api/jobs/${state.jobId}/cancel`);
  if (state.tenant) url.searchParams.set("tenant", state.tenant);
  try { navigator.sendBeacon(url, new Blob([JSON.stringify({ tenant: state.tenant })], { type: "application/json" })); } catch { /* best effort */ }
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
    ${s.kind === "group" && state.target && state.target.group ? `<button type="button" class="link" data-group-page="${esc(state.target.group)}" title="What's in the group, its cost and Check this group">group summary</button>` : ""}`);

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

const STATUS_ORDER = { critical: 0, warning: 1, unknown: 2, info: 3, healthy: 4 };

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
  box.innerHTML = `<div class="graph-note table-bar"><span>${rows.length} resource(s), worst first</span><span class="min-slot">${minButton("table")}</span></div>`
    + baselineHtml(s) + `<table class="data-table"><thead><tr><th>Status</th><th>Name</th><th>Type</th>
      ${groups.size > 1 ? "<th>Resource group</th>" : ""}<th>Why</th><th>Against last week</th><th></th></tr></thead><tbody>${rows.map((n) => `
    <tr class="clickable ${n.id === selected ? "selected" : ""}" data-node="${esc(n.id)}">
      <td>${pill(n.status)}</td><td>${esc(n.name)}</td><td>${esc(n.display_type)}</td>
      ${groups.size > 1 ? `<td>${esc(n.resource_group)}</td>` : ""}<td>${esc(nodeWhy(n))}</td>
      <td>${(moved[n.id] || []).slice(0, 2).map((m) => `${esc(m.signal.replace(/ \(.*\)$/, ""))} <span class="muted">${esc(moveText(m))}</span>`).join("<br>")}</td>
      <td>${watchButton({ id: n.id, name: n.name, kind: "resource" })}</td></tr>`).join("")}</tbody></table>`;
}

function moveText(m) {
  if (m.direction === "new") return "none last week";
  if (m.direction === "gone") return "none now";
  return m.direction === "up" ? `${fmt(m.ratio)}× last week` : `${fmt(1 / m.ratio)}× lower`;
}

// The group's baseline: metrics that doubled or halved against the same window a week earlier.
function baselineHtml(s) {
  const b = s.baseline;
  if (!s.done || !b || b.window_minutes == null) return "";
  const head = `Against last week: the same ${b.window_minutes} minutes, 7 days earlier`;
  if (!b.movers.length) {
    return `<div class="baseline"><strong>${esc(head)}</strong> <span class="ok">✓ no metric doubled or halved</span>
      <span class="muted">(${b.compared} resource(s) compared${b.without_baseline ? `, ${b.without_baseline} without last week's data` : ""})</span></div>`;
  }
  return `<details class="baseline" open><summary><strong>${esc(head)}</strong> · ${b.movers.length + b.more} metric(s) doubled or halved
      <span class="muted">(${b.compared} resource(s) compared${b.without_baseline ? `, ${b.without_baseline} without last week's data` : ""})</span></summary>
    <table class="data-table"><thead><tr><th></th><th>Resource</th><th>Metric</th><th class="num">Now</th><th class="num">Last week</th><th>Change</th></tr></thead>
    <tbody>${b.movers.map((m) => `<tr class="clickable" data-node="${esc(m.node_id)}"><td>${pill(m.status)}</td><td>${esc(m.name)}</td>
      <td>${esc(m.signal)}</td><td class="num">${fmt(m.value)} ${esc(m.unit || "")}</td><td class="num">${fmt(m.baseline)}</td>
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

// ----------------------------------------------------------------- explore

const AKS_TYPE = "microsoft.containerservice/managedclusters";
const AI_TYPE = "microsoft.insights/components";
const SITE_TYPE = "microsoft.web/sites";
const APIM_TYPE = "microsoft.apimanagement/service";
// "Is it OK and what happened" first, then where else it's reported, then configuration.
const TABS = [
  ["overview", "Overview"], ["timeline", "Timeline"], ["metrics", "Metrics"],
  ["workloads", "Workloads", (n) => n.type === AKS_TYPE],
  ["nodes", "Nodes", (n) => n.type === AKS_TYPE],
  ["appinsights", "Application Insights", (n) => [AI_TYPE, SITE_TYPE, APIM_TYPE].includes(n.type)],
];
const CONFIG_TABS = [["details", "Details"], ["scaling", "Scaling"], ["diagnostics", "Diagnostics"], ["vulnerabilities", "Vulnerabilities"],
  ["recommendations", "Recommendations"], ["cost", "Cost"]];
// Plus ServiceNow and one tab per monitoring tool, when this tenant has a turned-on connection of them.
const allTabs = () => [...TABS, ...(state.snEnabled ? [["servicenow", "ServiceNow"]] : []),
  ...toolKinds().map((kind) => [`tool-${kind}`, toolLabel(kind)]), ...CONFIG_TABS];
const SEVERITY_STATUS = { HIGH: "critical", MEDIUM: "warning", LOW: "info", INFO: "info" };
const PIM_TIP = "If you're eligible for a role through PIM, activate it, then click Refresh access.";
const exploreCache = new Map();

function table(columns, rows, key = columns.join("|")) {
  if (!rows.length) return '<p class="muted">Nothing to show.</p>';
  return `<table data-list="${esc(key)}"><thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${
    rows.map((r) => `<tr>${r.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ lists
// Any <table data-list="key">: a header click sorts it (up, down, then the list's own order; remembered per key in this
// browser), and a long one shows LIST_PAGE rows with "show more". A cell sorts by its own or a child's data-v, else its text.
// A row marked data-attached (a detail row) moves and hides with the row above it.
const LIST_PAGE = 100;
const NUMBER_CELL = /^(-?[\d,]*\.?\d+)\s*(%|ms|s|m|GiB|MiB|cores)?(\s+(\(|of\b|\/|·).*)?$/i;

function listSorts() {
  return stored.json("ahc.listSort", {});
}

function saveListSort(key, sort) {
  const sorts = listSorts();
  if (sort) sorts[key] = sort; else delete sorts[key];
  stored.set("ahc.listSort", sorts);
}

function cellValue(cell) {
  if (!cell) return null;
  const text = String(cell.dataset.v ?? cell.querySelector("[data-v]")?.dataset.v ?? cell.textContent).trim();
  if (text === "" || text === "–") return null;
  const number = text.match(NUMBER_CELL);
  return number ? Number(number[1].replace(/,/g, "")) : text;
}

function compareValues(x, y) {
  if (typeof x === "number" && typeof y === "number") return x - y;
  if (typeof x !== typeof y) return typeof x === "number" ? -1 : 1;
  return x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" });
}

// Each list row with the detail rows attached to it.
function listGroups(table) {
  const groups = [];
  for (const row of table.tBodies[0].rows) {
    if ("attached" in row.dataset && groups.length) groups[groups.length - 1].push(row); else groups.push([row]);
  }
  return groups;
}

function sortList(table, sort) {
  const col = sort ? sort.col : -1;
  const keyed = listGroups(table).map((rows) => ({ rows, i: Number(rows[0].dataset.i), v: sort ? cellValue(rows[0].cells[col]) : null }));
  keyed.sort((a, b) => {
    if (!sort || (a.v === null && b.v === null)) return a.i - b.i;
    if (a.v === null || b.v === null) return a.v === null ? 1 : -1;  // empty cells last, either way
    return sort.dir * compareValues(a.v, b.v) || a.i - b.i;
  });
  table.tBodies[0].append(...keyed.flatMap((k) => k.rows));
  table.querySelectorAll("th[data-col]").forEach((th) => th.setAttribute("aria-sort",
    sort && Number(th.dataset.col) === col ? (sort.dir > 0 ? "ascending" : "descending") : "none"));
  table.listSort = sort;
}

function pageList(table) {
  const groups = listGroups(table);
  const text = (table.dataset.filterText || "").toLowerCase();
  const matching = groups.filter((rows) => !text || rows[0].textContent.toLowerCase().includes(text));
  const shown = new Set(matching.slice(0, Number(table.dataset.shown || LIST_PAGE)));
  for (const rows of groups) rows.forEach((row) => row.classList.toggle("paged-out", !shown.has(rows)));
  let more = table.nextElementSibling && table.nextElementSibling.classList.contains("list-more") ? table.nextElementSibling : null;
  const rest = matching.length - shown.size;
  if (!rest && !text) { if (more) more.remove(); return; }
  if (!more) { more = document.createElement("div"); more.className = "list-more muted"; table.after(more); }
  more.innerHTML = rest ? `Showing ${shown.size.toLocaleString()} of ${matching.length.toLocaleString()}${text ? " matching" : ""} ·
    <button type="button" class="link" data-list-more>show ${Math.min(rest, LIST_PAGE).toLocaleString()} more</button> ·
    <button type="button" class="link" data-list-all>show all</button>`
    : `${matching.length.toLocaleString()} of ${groups.length.toLocaleString()} match`;
}

function initList(table) {
  if (table.dataset.ready || !table.tBodies[0]) return;
  table.dataset.ready = "1";
  [...table.tBodies[0].rows].forEach((row, i) => { row.dataset.i = i; });
  const head = table.tHead && table.tHead.rows[0];
  if (head) [...head.cells].forEach((th, i) => {
    if (!th.textContent.trim() || "nosort" in th.dataset) return;
    th.dataset.col = i;
    th.innerHTML = `<button type="button" class="sort">${th.innerHTML}</button>`;
  });
  const saved = listSorts()[table.dataset.list];
  if (saved && head && head.cells[saved.col] && "col" in head.cells[saved.col].dataset) sortList(table, saved);
  const count = listGroups(table).length;
  if (count > LIST_PAGE && !("nofilter" in table.dataset)) {
    const tools = document.createElement("div");
    tools.className = "list-tools";
    tools.innerHTML = `<input type="search" data-list-filter placeholder="Filter these ${count.toLocaleString()} rows" aria-label="Filter this list">`;
    table.before(tools);
  }
  pageList(table);
}

const listOf = (control) => {
  const near = control.closest(".list-more, .list-tools");
  return near ? (near.classList.contains("list-more") ? near.previousElementSibling : near.nextElementSibling) : control.closest("table[data-list]");
};

new MutationObserver((records) => {
  for (const record of records) for (const node of record.addedNodes) {
    if (node.nodeType !== 1) continue;
    if (node.matches("table[data-list]")) initList(node);
    node.querySelectorAll("table[data-list]").forEach(initList);
  }
}).observe(document.body, { childList: true, subtree: true });

document.addEventListener("click", (event) => {
  const th = event.target.closest("table[data-list] th[data-col]");
  if (th) {
    const table = listOf(th), col = Number(th.dataset.col), current = table.listSort;
    const sort = current && current.col === col ? (current.dir > 0 ? { col, dir: -1 } : null) : { col, dir: 1 };
    saveListSort(table.dataset.list, sort);
    sortList(table, sort);
    pageList(table);
    return;
  }
  const more = event.target.closest("[data-list-more], [data-list-all]");
  if (more) {
    const table = listOf(more);
    table.dataset.shown = "listAll" in more.dataset ? Infinity : Number(table.dataset.shown || LIST_PAGE) + LIST_PAGE;
    pageList(table);
  }
});

document.addEventListener("input", (event) => {
  if (!("listFilter" in event.target.dataset)) return;
  const table = listOf(event.target);
  table.dataset.filterText = event.target.value.trim();
  table.dataset.shown = LIST_PAGE;
  pageList(table);
});

function noticesHtml(texts) {
  return (texts || []).map(noticeHtml).join("");
}

async function exploreGet(view, node, params = {}) {
  const key = `${node.id}|${view}|${JSON.stringify(params)}`;
  if (!exploreCache.has(key)) {
    exploreCache.set(key, api(`api/explore/${view}`, { params: { id: node.id, ...params } })
      .catch((error) => { exploreCache.delete(key); throw error; }));
  }
  return exploreCache.get(key);
}

function showDetail(nodeId, tab = "overview", { push = false } = {}) {
  const node = state.snapshot.nodes.find((n) => n.id === nodeId);
  if (!node) return;
  state.exploreNode = node;
  const portal = `https://portal.azure.com/#@${state.snapshot.tenant_id}/resource${node.id}`;
  const tabs = personaTabs(allTabs());
  if (tab === "activity") tab = "timeline";  // the Activity tab became the Timeline
  if (!tabs.some(([key]) => key === tab)) tab = "overview";
  const visible = node.readable ? tabs.filter(([, , when]) => !when || when(node)) : [TABS[0]];
  if (!visible.some(([key]) => key === tab)) tab = "overview";
  $("detail").innerHTML = `
    <div class="explore-head">
      <div><h3>${esc(node.name)} ${pill(node.status, whyStatus(node))}</h3>
        <div class="muted">${esc(node.display_type)} · ${esc(node.resource_group)} · ${esc(node.location)}</div></div>
      <div class="explore-links"><a href="${esc(portal)}" target="_blank" rel="noopener">Open in portal</a>
        ${node.id !== state.snapshot.root_id && node.readable ? ' · <a href="#" id="focus-node">Check from here</a>' : ""}
        ${watchButton({ id: node.id, name: node.name, kind: "resource" })}</div>
    </div>
    <nav class="tabs">${visible.map(([key, label]) =>
      `<button type="button" data-tab="${key}" class="${key === tab ? "active" : ""}">${label}</button>`).join("")}</nav>
    <div id="tab-body" class="tab-body"></div>`;
  $("detail").classList.remove("hidden");
  const focus = $("focus-node");
  if (focus) focus.addEventListener("click", (event) => {
    event.preventDefault();
    startSnapshot({ kind: "resource", id: node.id, name: node.name });
  });
  $("detail").querySelector(".tabs").addEventListener("click", (event) => {
    const key = event.target.closest("[data-tab]")?.dataset.tab;
    if (key) showDetail(node.id, key, { push: true });
  });
  for (const [key, count] of Object.entries((state.tabBadges || {})[node.id] || {})) {
    const button = $("detail").querySelector(`.tabs [data-tab="${key}"]`);
    if (button && count) button.insertAdjacentHTML("beforeend", ` <span class="tab-badge critical">${count}</span>`);
  }
  state.detailNode = node.id;
  state.detailTab = tab;
  navRecord(push);
  renderTab(node, tab);
}

async function renderTab(node, tab) {
  const body = $("tab-body");
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  const renderers = { overview: tabOverview, metrics: tabMetrics, details: tabDetails, scaling: tabScaling,
    diagnostics: tabDiagnostics, timeline: tabTimeline, recommendations: tabRecommendations, vulnerabilities: tabVulnerabilities, workloads: tabWorkloads, nodes: tabNodes,
    servicenow: tabServiceNow, appinsights: tabAppInsights, cost: tabCost };
  body.innerHTML = loading("Loading…");
  try {
    await (tab.startsWith("tool-") ? tabTool(node, body, tab.slice(5)) : renderers[tab](node, body));
    fillPimHints(body);
  } catch (error) {
    const refused = error.payload && error.payload.kind === "access";
    if (refused) noteBlocked(node.id, node.name, error.payload.required);
    body.innerHTML = errorHtml(error) + (refused ? pimSlot(node.id, error.payload.required) : "");
    fillPimHints(body);
  }
}

// A signal's or event's links (alert_detail.links): the portal page of a resource or an alert, or a portal URL as given.
// A portal link without a directory opens in the browser's last one: with two tenants signed in, PIM could open in the other.
function tenantPortal(url) {
  const tenant = state.tenant || (state.snapshot && state.snapshot.tenant_id) || "";
  return tenant && /^https:\/\/portal\.azure\.com\/#(?!@)/.test(url || "") ? url.replace("https://portal.azure.com/#", `https://portal.azure.com/#@${tenant}/`) : url;
}

function portalUrl(link) {
  const tenant = state.tenant || (state.snapshot && state.snapshot.tenant_id) || "";
  if (link.resource) return `https://portal.azure.com/#@${tenant}/resource${link.resource}`;
  if (link.health) return `https://portal.azure.com/#@${tenant}/blade/Microsoft_Azure_Health/AzureHealthBrowseBlade/${encodeURIComponent(link.health)}`;
  if (link.alert) return `https://portal.azure.com/#@${tenant}/blade/Microsoft_Azure_Monitoring_Alerts/AlertDetails.ReactView/alertId/${encodeURIComponent(link.alert)}`;
  return link.url || "";
}

// "↗" to a resource (or one of its pages) in the Azure portal, in a new tab; clicking it never opens the row it sits in.
function portalLinkHtml(id, { label = "↗", page = "", title = "Open in the Azure portal" } = {}) {
  if (!id || !id.startsWith("/subscriptions/")) return "";
  return `<a class="portal-link" href="${esc(portalUrl({ resource: id }) + (page ? `/${page}` : ""))}" target="_blank" rel="noopener"
    title="${esc(title)}">${esc(label)}</a>`;
}
document.addEventListener("click", (event) => { if (event.target.closest && event.target.closest(".portal-link")) event.stopPropagation(); }, true);

// " · Open <tool> ↗" after a tool tab's header line, to the connection's own home (instance or console).
const toolLink = (url, label) => (url ? ` · ${linksHtml([{ url, label: `Open ${label || "it"}` }])}` : "");

function linksHtml(links) {
  return (links || []).map((l) => `<a href="${esc(portalUrl(l))}" target="_blank" rel="noopener">${esc(l.label)} ↗</a>`).join(" · ");
}

// "Today 12:02", "Yesterday 18:30", "Mon 9 Nov 08:00": a time with its day, for lists that span days.
function dayTime(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return String(iso || "");
  const clockText = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(when).setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return `Today ${clockText}`;
  if (days === 1) return `Yesterday ${clockText}`;
  return `${when.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} ${clockText}`;
}

const SIGNAL_ORDER = { critical: 0, warning: 1, unknown: 2, healthy: 3, info: 4 };

function attentionRow(sig) {
  return `<div class="attn-row">${pill(sig.status)}<div class="attn-text"><strong>${esc(sig.name)}</strong>
      ${sig.value != null ? ` ${fmt(sig.value)} ${esc(sig.unit)}${sig.baseline != null ? ` <span class="muted">(last week ${fmt(sig.baseline)})</span>` : ""}` : ""}
      ${sig.at ? ` <span class="muted" title="${esc(new Date(sig.at).toLocaleString())}">· ${esc(dayTime(sig.at))}</span>` : ""}
      ${sig.detail ? `<div class="muted">${escTimes(sig.detail)}</div>` : ""}
      ${(sig.links || []).length ? `<div class="attn-links">${linksHtml(sig.links)}${alertCheckFor(sig)}</div>` : ""}</div></div>`;
}

// The check for an Azure alert signal: its fired alert's ID is among its links.
function alertCheckFor(sig) {
  const fired = (sig.links || []).find((l) => l.alert);
  return fired ? ` · ${alertCheckButton({ source: "azure", id: fired.alert })}` : "";
}

// The resource's Defender totals under its settings, with the tab that lists them (the same rows Summary counts).
function overviewVulnsHtml(v) {
  if (!v) return "";
  if (v.error) return `<p class="muted">Vulnerabilities weren't read: ${esc(v.error)}</p>`;
  if (!v.findings) return '<p class="muted">No open vulnerability findings from Defender.</p>';
  return `<p>${pill(v.high ? "warning" : "info")} <strong>${v.findings.toLocaleString()} open vulnerability finding(s)</strong>
    <span class="muted">${v.high.toLocaleString()} high, worst CVSS ${esc(String(v.maxCvss ?? "–"))}${v.running ? `, ${v.running.toLocaleString()} in running containers` : ""}${
    v.findings ? `, ${Math.round((100 * v.fixable) / v.findings)}% with a fix` : ""}.</span>
    <button type="button" class="link" data-goto-tab="vulnerabilities">See them on the Vulnerabilities tab</button></p>`;
}

// Overview's Security card: the same checks as Security → Exposure & configuration, for this one resource.
async function renderOverviewSecurity(node) {
  const slot = $("ov-security");
  try {
    const data = await exploreGet("security", node);
    if (!slot.isConnected) return;
    const problems = data.findings.filter((f) => f.severity !== "restricted");
    const fenced = data.findings.filter((f) => f.severity === "restricted");
    slot.innerHTML = `${noticesHtml(data.errors)}${problems.length ? `<ul class="sec-findings">${problems.map((f) =>
        `<li><span class="sec-dot ${esc(f.severity)}" title="${esc(f.severity)}"></span>${esc(f.what)}
          <span class="muted">· ${esc(data.categories[f.category] || f.category)}</span>${f.action ? `<div class="muted">${esc(f.action)}</div>` : ""}</li>`).join("")}</ul>`
      : `<p class="ok">✓ Nothing in its settings${data.errors.length ? " that could be read" : ""}.</p>`}
      ${overviewVulnsHtml(data.vulnerabilities)}
      ${fenced.map((f) => `<p class="muted">Public endpoint behind a firewall: ${esc(f.what)}.</p>`).join("")}
      <p class="muted">Checked: public access and firewall, keys and local auth, TLS and HTTPS, deletion protection${
        data.queries.length > 1 ? ", and its NSG rules or public IPs" : ""}. <button type="button" class="link" data-goto-security>All resources in Security</button></p>`;
  } catch (error) {
    if (slot.isConnected) slot.innerHTML = noticeHtml(error);
  }
}

// Needs attention's App Insights line: the component's last hour (an app's or gateway's, by the tab's own lookup).
async function renderOverviewAppInsights(node) {
  const slot = $("ov-ai-now");
  const pick = node.type === AI_TYPE ? "" : aiPicks()[node.id.toLowerCase()];
  try {
    const data = await exploreGet("appinsights-now", node, pick ? { component: pick } : {});
    if (!slot.isConnected) return;
    if (data.now && data.now.status && $("ov-ok")) $("ov-ok").remove();
    slot.innerHTML = !data.available ? '<p class="muted">Application Insights: no component found for it (see its Application Insights tab).</p>'
      : data.now ? `<div class="ov-ai-label muted">Application Insights${data.link ? ` · ${esc(data.link.component_name)}` : ""}</div>${aiNowHtml(data)}`
      : noticesHtml(data.errors);
  } catch (error) {
    if (slot.isConnected) slot.innerHTML = noticeHtml(`Application Insights: ${error.message}`);
  }
}

// Roles this resource holds, itself or through the identity only it signs in as: on resources in the picture (the
// dashed arrows) and on whole groups or subscriptions (core/discovery._add_role_links).
function rolesHeldHtml(node) {
  const s = state.snapshot;
  if (!s || !s.nodes) return "";
  const byId = new Map(s.nodes.map((n) => [n.id, n]));
  const via = (s.graph && s.graph.via) || {};
  const holders = [node.id, ...Object.entries(via).filter(([, user]) => user === node.id).map(([id]) => id)];
  const heldBy = (id) => (id === node.id ? "itself" : esc((byId.get(id) || {}).name || id));
  const rows = [
    ...s.edges.filter((e) => e.kind === "role" && holders.includes(e.source))
      .map((e) => [esc(e.label), `resource ${esc((byId.get(e.target) || {}).name || e.target)}`, heldBy(e.source)]),
    ...holders.flatMap((id) => ((byId.get(id) || {}).roles || [])
      .map((r) => [esc(r.role), `${esc(r.scope_kind)} ${esc(r.scope_name)}`, heldBy(id)])),
  ];
  return rows.length ? `<h4>Roles it holds</h4>${table(["Role", "On", "Held by"], rows)}` : "";
}

function tabOverview(node, body) {
  const signals = node.signals.map((sig) => [
    `${esc(sig.name)}<br><span class="muted">${esc(sig.source)}</span>`, pill(sig.status),
    `${sig.value != null ? `${fmt(sig.value)} ${esc(sig.unit)}` : "–"}${sig.baseline != null ? `<br><span class="muted">last wk ${fmt(sig.baseline)}</span>` : ""}`,
    `${esc(sig.threshold)}${sig.threshold ? "<br>" : ""}<span class="muted">${esc(sig.detail)}</span>${(sig.links || []).length ? `<div class="attn-links">${linksHtml(sig.links)}</div>` : ""}`]);
  const isVault = node.type === "microsoft.keyvault/vaults" && node.readable;
  const attention = node.signals.filter((sig) => PROBLEM.has(sig.status))
    .sort((a, b) => (SIGNAL_ORDER[a.status] ?? 5) - (SIGNAL_ORDER[b.status] ?? 5));
  body.innerHTML = `${node.readable ? "" : pimSlot(node.id, (node.signals[0] || {}).detail)}
    <div class="ov-cards">
      <div class="card ov-card ov-wide"><h4>Needs attention</h4>
        ${!node.readable ? `<p class="notice">This resource can't be read: it may no longer exist in Azure (Resource Graph can
            list a deleted resource for a while), or your access doesn't cover it. ${esc((node.signals[0] || {}).detail || "")}</p>`
          : attention.length ? attention.map(attentionRow).join("") + '<div id="ov-external"></div>'
          : `<div id="ov-external"></div><p class="ok" id="ov-ok">✓ Nothing needs attention${node.signals.length ? ` · ${node.signals.length} signal(s) checked, all below` : ": no signals for this type"}.</p>`}
        ${node.readable && [AI_TYPE, SITE_TYPE, APIM_TYPE].includes(node.type) ? `<div id="ov-ai-now">${loading("Reading the last hour from Application Insights…")}</div>` : ""}
        <h5 class="ov-sub">Recent events <span class="muted">last 24 hours, newest first</span></h5>
        <div id="ov-timeline">${loading("Reading the last 24 hours…")}</div></div>
      ${accessHereSlot(node.id)}
      ${node.readable ? `<div class="card ov-card"><h4>Security</h4><div id="ov-security">${loading("Checking its settings and vulnerabilities…")}</div></div>` : ""}
      <div class="card ov-card"><h4>Open elsewhere</h4><div id="ov-elsewhere">${state.snEnabled || toolKinds().length ? loading("Reading ServiceNow and monitoring tools…")
        : '<p class="muted">No ServiceNow or monitoring tool is connected for this tenant (Settings → Integrations).</p>'}</div></div>
    </div>
    ${isVault ? `<div id="vault-expiry">${loading("Reading secret and key expiry dates…")}</div>` : ""}
    ${node.type === AKS_TYPE && node.readable ? `<div class="card aks-one" id="aks-one" data-cluster="${esc(node.id)}">${aksOneHtml(node.id)}</div>` : ""}
    <h4 id="signals">Signals</h4>${table(["Signal", "Status", "Value", "Threshold / detail"], signals)}
    ${rolesHeldHtml(node)}
    <p class="muted ov-id"><code>${esc(node.id)}</code></p>`;
  if (isVault) renderVaultExpiry(node);
  if ($("ov-ai-now")) renderOverviewAppInsights(node);
  if ($("ov-security")) renderOverviewSecurity(node);
  fillAccessHere(body);
  if (node.readable) renderOverviewEvents(node);
  else {
    noteBlocked(node.id, node.name, (node.signals[0] || {}).detail);
    const box = $("ov-timeline");  // nothing reads events for a resource that can't be read: say so instead of waiting
    if (box) box.innerHTML = '<p class="muted">Not read, because the resource itself can\'t be read.</p>';
  }
}

const PROBLEM = new Set(["critical", "warning"]);

function setTabBadge(key, count, tone = "critical") {
  if (state.exploreNode) {
    state.tabBadges = state.tabBadges || {};
    (state.tabBadges[state.exploreNode.id] = state.tabBadges[state.exploreNode.id] || {})[key] = count;
  }
  const button = $("detail").querySelector(`.tabs [data-tab="${key}"]`);
  if (!button) return;
  const old = button.querySelector(".tab-badge");
  if (old) old.remove();
  if (count) button.insertAdjacentHTML("beforeend", ` <span class="tab-badge ${tone}">${count}</span>`);
}

async function renderOverviewEvents(node) {
  const { all, results } = await timelineData(node, "24");
  if (!state.exploreNode || state.exploreNode.id !== node.id) return;
  // What ServiceNow and the monitoring tools have open for it, each with the key its timeline event carries.
  const open = [];
  for (const r of results) {
    if (r.kind === "servicenow" && r.raw) {
      for (const i of r.raw.incidents) open.push({ key: snKey(i), status: i.priority === 1 ? "critical" : i.priority === 2 ? "warning" : "info",
        tool: "ServiceNow", what: "ServiceNow incident", text: `${i.number} ${i.title}`, url: i.url });
      setTabBadge("servicenow", r.raw.incidents.length);
    } else if (r.raw && r.kind !== "azure") {
      for (const i of r.raw.issues) open.push({ key: toolKey(r.kind, i), status: i.priority === "critical" ? "critical" : i.priority === "warning" ? "warning" : "info",
        tool: r.name, what: `${r.name} issue`, text: i.title || i.id, url: i.url,
        check: { source: "monitor", ...toolParams(r.kind), account: i.account_id ?? "", condition: i.condition || "", policy: i.policy || "", priority: i.priority_label || "" } });
      // An entity the tool holds in critical/warning with no matched open issue (Triage's card lists it too).
      // Skipped when an open issue covers it: by guid, or by name (New Relic also lists an issue as an entity).
      const covered = new Set(r.raw.issues.flatMap((i) => i.entity_ids || []));
      const titles = new Set(r.raw.issues.map((i) => (i.title || "").toLowerCase()));
      const alerting = (r.raw.entities || []).filter((e) => PROBLEM.has(e.severity) && !titles.has((e.name || "").toLowerCase())
        && !(e.ids || [e.id]).some((id) => covered.has(id)));
      for (const e of alerting) open.push({ key: `${r.kind}:entity:${e.id}`, status: e.severity, tool: r.name,
        what: `${r.name} entity in ${e.severity}${e.type ? ` · ${e.type}` : ""}`, text: e.name, url: e.url });
      setTabBadge(`tool-${r.kind}`, r.raw.issues.length + alerting.length);
    }
  }
  // Each item is shown once: what another tool has open and is urgent goes in Needs attention (the page never says
  // "nothing" while one is open), and the lists below leave out what's already there.
  const external = $("ov-external");
  const urgent = external ? open.filter((o) => PROBLEM.has(o.status)) : [];
  const shown = new Set(urgent.map((o) => o.key));
  if (urgent.length) {
    external.innerHTML = urgent.map((o) => `<div class="attn-row">${pill(o.status)}<div class="attn-text"><strong>${o.url ? monLink(o.url, o.text) : esc(o.text)}</strong>
        <div class="muted">${esc(o.what)}</div>${o.check ? `<div class="attn-links">${alertCheckButton(o.check)}</div>` : ""}</div></div>`).join("");
    const ok = $("ov-ok");
    if (ok) ok.outerHTML = '<p class="muted">Azure\'s own signals: nothing needs attention.</p>';
  }
  const box = $("ov-timeline");
  if (box) {
    const listed = new Set(node.signals.filter((sig) => sig.source === "alert" && PROBLEM.has(sig.status)).map((sig) => sig.name));
    const recent = all.filter((e) => !(e.source === "Alert" && listed.has(e.name)) && !(e.key && shown.has(e.key)));
    box.innerHTML = recent.length ? `${recent.slice(0, 6).map((e) => `<div class="ov-row"><span class="ov-time" title="${esc(new Date(e.at).toLocaleString())}">${esc(dayTime(e.at))}</span>
        ${pill(e.status)} <span class="ov-text"><span class="muted">${esc(e.source)}</span> ${e.url ? monLink(e.url, e.title) : esc(e.title)}</span></div>`).join("")}
        <button type="button" class="link ov-more" data-goto-tab="timeline">Open the timeline (${all.length})</button>`
      : `<p class="muted">${all.length ? "Nothing else in the last 24 hours." : "Nothing in the last 24 hours."}</p>${all.length ? `<button type="button" class="link ov-more" data-goto-tab="timeline">Open the timeline (${all.length})</button>` : ""}`;
  }
  const elsewhere = $("ov-elsewhere");
  if (elsewhere && (state.snEnabled || toolKinds().length)) {
    const unavailable = results.filter((r) => r.unavailable && r.name !== "Azure");
    const rest = open.filter((o) => !shown.has(o.key));
    const above = Object.entries(urgent.reduce((by, o) => ({ ...by, [o.tool]: [...(by[o.tool] || []), o] }), {})).map(([tool, items]) => {
      const counts = ["critical", "warning"].map((s) => [s, items.filter((o) => o.status === s).length]).filter(([, n]) => n);
      return `<p class="muted">${esc(tool)}: ${counts.map(([s, n]) => `${n} ${s}`).join(", ")}, shown in Needs attention.</p>`;
    }).join("");
    elsewhere.innerHTML = above + (rest.length ? rest.slice(0, 5).map((o) => `<div class="ov-row">${pill(o.status)} <span><span class="muted">${esc(o.tool)}</span>
        ${o.url ? monLink(o.url, o.text) : esc(o.text)}</span></div>`).join("")
      : above ? '<p class="ok">✓ Nothing else open that names this resource.</p>' : '<p class="ok">✓ Nothing open that names this resource.</p>')
      + unavailable.map((r) => `<p class="muted">${esc(r.name)}: ${esc(r.unavailable)}</p>`).join("");
  }
  setTabBadge("timeline", all.filter((e) => PROBLEM.has(e.status)).length);
}

$("detail").addEventListener("click", (event) => {
  const go = event.target.closest("[data-goto-tab]");
  if (go && state.exploreNode) { event.preventDefault(); showDetail(state.exploreNode.id, go.dataset.gotoTab); }
});

// Key Vaults lead with the list people open them for: every secret and key, expired first.
async function renderVaultExpiry(node) {
  const box = () => $("vault-expiry");
  try {
    const data = await exploreGet("details", node);
    if (!box()) return;
    const sections = data.sections.filter((s) => /^(Certificates|Secrets|Keys) \(/.test(s.title));
    box().innerHTML = sections.map((s) => {
      const rows = s.facts.filter((f) => f.status);
      return `<h4>${esc(s.title)}</h4>${s.note ? `<p class="muted">${esc(s.note)}</p>` : ""}
        ${rows.length ? table(["Status", "Name", "Expiry"], rows.map((f) => [pill(f.status), esc(f.label), esc(f.value)]))
          : `<p class="muted">${esc((s.facts[0] || {}).value || "None.")}</p>`}`;
    }).join("") + noticesHtml(data.errors);
  } catch (error) {
    if (box()) box().innerHTML = noticeHtml(error);
  }
}

function factsHtml(section) {
  if (section.table) {
    return table(section.table.columns, section.table.rows.map((r) => r.map((cell) => esc(fmt(cell)))));
  }
  return table(["", "", ""], section.facts.map((f) => [esc(f.label), esc(fmt(f.value)), f.status ? pill(f.status) : ""]))
    .replace("<tr><th></th><th></th><th></th></tr>", "");
}

async function tabDetails(node, body) {
  const data = await exploreGet("details", node);
  body.innerHTML = data.sections.map((s) => `<h4>${esc(s.title)}</h4>${s.note ? `<p class="muted">${esc(s.note)}</p>` : ""}${factsHtml(s)}`).join("")
    + noticesHtml(data.errors);
}

async function tabScaling(node, body) {
  const data = await exploreGet("scaling", node);
  body.innerHTML = (data.note ? `<p class="muted">${esc(data.note)}</p>` : "")
    + table(["Kind", "Name", "Current", "Min", "Max", "Status", "Detail"], data.items.map((i) => [
      esc(i.kind), esc(i.name), fmt(i.current), fmt(i.min), fmt(i.max), pill(i.status), esc(i.detail)]))
    + (data.items.length ? "" : '<p class="muted">No autoscale settings or node pools for this resource.</p>')
    + noticesHtml(data.errors);
}

// Azure's detector text is Markdown with some HTML (tables). It's rebuilt from plain text only:
// tables cell by cell, Markdown with everything escaped, links only when http(s). Azure's markup never reaches the page.
const MARK = "\uE000";  // placeholder for a rebuilt table; private-use, so HTML parsing leaves it alone

function azureText(raw) {
  const blocks = [];
  let text = String(raw || "").replace(/<\/?markdown>/gi, "");
  text = text.replace(/<table[\s\S]*?<\/table>/gi, (html) => {
    blocks.push(safeTable(html));
    return `\n${MARK}${blocks.length - 1}${MARK}\n`;
  });
  text = text.replace(/<br\s*\/?>/gi, "\n").replace(/<\/(p|div|li|h\d|tr)>/gi, "\n").replace(/<[^>]+>/g, "");
  return markdownToHtml(decodeEntities(text)).replace(new RegExp(`${MARK}(\\d+)${MARK}`, "g"), (_, i) => blocks[Number(i)]);
}

function decodeEntities(text) {
  const area = document.createElement("textarea");
  area.innerHTML = text;  // a textarea's content is plain text: nothing in it runs
  return area.value;
}

function safeTable(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");  // parsing only; scripts never run
  doc.querySelectorAll("br").forEach((br) => br.replaceWith("\n"));  // keep line breaks inside cells
  const rows = [...doc.querySelectorAll("tr")].map((tr) => [...tr.querySelectorAll("th,td")].map((c) => c.textContent.trim()));
  if (!rows.length) return "";
  const [head, ...body] = doc.querySelector("th") ? rows : [[], ...rows];
  return table(head, body.map((r) => r.map((cell) => esc(cell).replace(/\n+/g, "<br>"))));
}

function inlineMarkdown(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => `<a href="${url}" target="_blank" rel="noopener">${label}</a>`);
}

function markdownToHtml(text) {
  const out = [];
  const lines = text.split(/\r?\n/);
  let paragraph = [];
  let list = null;
  const flush = () => {
    if (paragraph.length) out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
    if (list) out.push(`<ul>${list.map((li) => `<li>${inlineMarkdown(li)}</li>`).join("")}</ul>`);
    paragraph = [];
    list = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.startsWith("```")) {
      flush();
      const code = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith("```"); i++) code.push(lines[i]);
      out.push(`<pre class="code">${esc(code.join("\n").trim())}</pre>`);
      continue;
    }
    if (new RegExp(`^${MARK}\\d+${MARK}$`).test(line)) { flush(); out.push(line); continue; }
    if (!line) { flush(); continue; }
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) { flush(); out.push(`<h5>${inlineMarkdown(heading[1])}</h5>`); continue; }
    const bullet = /^(?:[-*]|\d+\.)\s+(.*)$/.exec(line);
    if (bullet) {
      if (paragraph.length) { out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`); paragraph = []; }
      (list = list || []).push(bullet[1]);
      continue;
    }
    if (list) flush();
    paragraph.push(line);
  }
  flush();
  return out.join("");
}

// Microsoft's troubleshooting catalogue (all reads): service → problem type → its self-help guides, shown here, and
// its solutions and checks, which only the portal opens.
async function helpGuides(node, box, portalLink) {
  box.innerHTML = loading("Listing Microsoft's troubleshooting guides…");
  const { services } = await exploreGet("guides", node);
  if (!services.length) { box.innerHTML = `<p class="muted">Microsoft lists no troubleshooting guides for this type.</p>`; return; }
  const serviceSelect = services.length > 1
    ? `<label class="inline">Service <select id="hg-service"><option value="">Pick one…</option>
        ${services.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select></label>` : "";
  box.innerHTML = `<h4>Troubleshooting guides</h4>
    <p class="muted">Microsoft's own guides for ${esc(services.length > 1 ? "this type" : services[0].name)}, by problem type,
      as the portal's form lists them.</p>
    ${serviceSelect}<span id="hg-problems"></span><div id="hg-solutions"></div>`;
  const problemsBox = box.querySelector("#hg-problems");
  const solutionsBox = box.querySelector("#hg-solutions");
  const showProblems = async (serviceId) => {
    solutionsBox.innerHTML = "";
    if (!serviceId) { problemsBox.innerHTML = ""; return; }
    problemsBox.innerHTML = loading("Listing problem types…");
    const { problems } = await exploreGet("guides", node, { service: serviceId });
    const groups = [...new Set(problems.map((p) => p.group))];
    problemsBox.innerHTML = `<label class="inline">Problem type <select id="hg-problem"><option value="">Pick one…</option>
      ${groups.map((g) => `<optgroup label="${esc(g)}">${problems.filter((p) => p.group === g)
        .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}</optgroup>`).join("")}</select></label>`;
    problemsBox.querySelector("#hg-problem").addEventListener("change", (event) =>
      showSolutions(event.target.value).catch((error) => { solutionsBox.innerHTML = errorHtml(error); }));
  };
  const showSolutions = async (problemId) => {
    if (!problemId) { solutionsBox.innerHTML = ""; return; }
    solutionsBox.innerHTML = loading("Asking Microsoft what it offers…");
    const { solutions } = await exploreGet("guides", node, { problem: problemId });
    const guides = solutions.filter((s) => s.kind === "guide");
    const checks = solutions.filter((s) => s.kind === "portal");
    solutionsBox.innerHTML = (solutions.length ? "" : `<p class="muted">Microsoft offers nothing for this problem type.</p>`)
      + guides.map((g) => `<details class="more" data-guide="${esc(g.id)}"><summary>${esc(g.description || g.id)}</summary>
          <div class="md">${loading("Reading the guide…")}</div></details>`).join("")
      + (checks.length ? `<h5>Only in the portal</h5>
          <p class="muted">Microsoft opens these solutions and checks by creating a Help resource (a write), so the console
            lists them: ${portalLink}.</p>
          <ul>${checks.map((c) => `<li>${esc(c.description || c.id)}</li>`).join("")}</ul>` : "");
    solutionsBox.querySelectorAll("details[data-guide]").forEach((item) => item.addEventListener("toggle", async () => {
      if (!item.open || item.dataset.read) return;
      item.dataset.read = "1";
      const text = item.querySelector(".md");
      try {
        const guide = await exploreGet("guides", node, { guide: item.dataset.guide });
        text.innerHTML = guide.text ? azureText(guide.text) : `<p class="muted">This guide has no text; the portal shows it.</p>`;
      } catch (error) { text.innerHTML = errorHtml(error); delete item.dataset.read; }
    }));
  };
  const serviceBox = box.querySelector("#hg-service");
  if (serviceBox) serviceBox.addEventListener("change", (event) =>
    showProblems(event.target.value).catch((error) => { problemsBox.innerHTML = errorHtml(error); }));
  else await showProblems(services[0].id);
}

const DETECTORS_AT_ONCE = 6;

// The list first (one read), then each detector on its own, six at a time, drawn as it answers: one can take 25 s.
async function tabDiagnostics(node, body, all = false) {
  body.innerHTML = loading("Listing Azure's detectors…");
  const plan = await exploreGet("diagnostics", node, { plan: "1", ...(all ? { all: "1" } : {}) });
  if (plan.error) { body.innerHTML = noticeHtml(plan.error); return; }
  const portal = `https://portal.azure.com/#@${state.snapshot.tenant_id}/resource${node.id}/troubleshoot`;
  const portalLink = `<a href="${esc(portal)}" target="_blank" rel="noopener">Diagnose and solve problems in the portal</a>`;
  if (!plan.supported) {
    body.innerHTML = (plan.unavailable
      ? `<p class="notice">Azure has a detectors API for this resource type, but ${esc(plan.unavailable)}. The console tries
          again in 15 minutes. Meanwhile, ${portalLink} shows them.</p>`
      : `<p class="muted">Azure's API has no working detectors for this resource type, so the console can't run them. The portal's
          page can still offer checks: for many types it runs them through Microsoft's Help service, which starts a diagnostic
          run (a write), and the console only reads. ${portalLink}.</p>`) + `<div id="help-guides"></div>`;
    helpGuides(node, body.querySelector("#help-guides"), portalLink)
      .catch((error) => { body.querySelector("#help-guides").innerHTML = errorHtml(error); });
    return;
  }
  const results = new Map();
  const ORDER = { critical: 0, warning: 1, unknown: 2, info: 3, healthy: 4 };
  const insight = (i) => `<div class="insight">${pill(i.status)} ${esc(i.text)}
    ${i.details.length ? `<details class="more"><summary>View details</summary>
      ${i.details.map((d) => `<h5>${esc(d.name)}</h5><div class="md">${azureText(d.value)}</div>`).join("")}</details>` : ""}</div>`;
  const dataTable = (t) => `<details class="more"><summary>${esc(t.title)} <span class="muted">(${t.total} row${t.total === 1 ? "" : "s"})</span></summary>
    ${table(t.columns, t.rows.map((r) => r.map((v) => esc(v ?? ""))))}
    ${t.total > t.rows.length ? `<p class="muted">First ${t.rows.length} of ${t.total}; the portal has the rest.</p>` : ""}</details>`;
  const item = (r, open) => `<details class="detector" data-detector="${esc(r.name)}" ${open ? "open" : ""}>
    <summary>${pill(r.status)} ${esc(r.title)} <span class="muted">${esc(r.category)}</span></summary>
    ${r.description ? `<p class="muted">${esc(r.description)}</p>` : ""}
    ${(r.insights || []).length ? r.insights.map(insight).join("") : '<p class="muted">No findings text.</p>'}
    ${(r.notes || []).map((n) => `${n.title ? `<h5>${esc(n.title)}</h5>` : ""}<div class="md">${azureText(n.value)}</div>`).join("")}
    ${(r.tables || []).map(dataTable).join("")}
    ${r.error ? `<p class="muted">${esc(r.error)}</p>` : ""}</details>`;
  const draw = () => {
    if (!body.isConnected) return;
    const opened = new Set([...body.querySelectorAll("details[data-detector][open]")].map((d) => d.dataset.detector));
    const done = [...results.values()].sort((a, b) => (ORDER[a.status] ?? 5) - (ORDER[b.status] ?? 5) || (a.title || "").localeCompare(b.title || ""));
    const flagged = done.filter((r) => ["critical", "warning"].includes(r.status));
    const quiet = done.filter((r) => !["critical", "warning"].includes(r.status));
    const waiting = plan.detectors.length - results.size;
    body.innerHTML = `<p class="muted">These are Azure's own checks, the same as the portal's ${portalLink.replace("Diagnose and solve problems in the portal", "Diagnose and solve problems")}
        (look under the category shown next to each one). They cover the <strong>last 24 hours</strong>, so a finding can be
        something that happened earlier today, not the current state, and the severity is Azure's own rating.</p>
      <p class="muted">${results.size} of ${plan.detectors.length} detectors answered${plan.total > plan.detectors.length ? ` (of ${plan.total} Azure offers${all ? "" : ": health, availability, performance and risk"})` : ""}.
        ${all ? "" : '<button type="button" id="run-all-detectors">Run all</button>'}</p>
      ${waiting ? loading(`${waiting} still answering (Azure's detectors take up to half a minute each)`) : ""}
      ${flagged.length ? flagged.map((r) => item(r, true)).join("") : results.size ? '<p>No detector reports a warning or critical finding so far.</p>' : ""}
      ${quiet.length ? `<details ${opened.has("__quiet") ? "open" : ""} data-detector="__quiet"><summary class="muted">${quiet.length} other detector(s)</summary>${quiet.map((r) => item(r, opened.has(r.name))).join("")}</details>` : ""}`;
    const runAll = $("run-all-detectors");
    if (runAll) runAll.addEventListener("click", () => tabDiagnostics(node, body, true).catch((e) => { body.innerHTML = errorHtml(e); }));
  };
  draw();
  const queue = [...plan.detectors];
  const worker = async () => {
    while (queue.length && body.isConnected) {
      const d = queue.shift();
      try { results.set(d.name, await exploreGet("diagnostics", node, { detector: d.name })); }
      catch (error) { results.set(d.name, { ...d, status: "unknown", insights: [], error: error.message }); }
      draw();
    }
  };
  await Promise.all(Array.from({ length: DETECTORS_AT_ONCE }, worker));
}

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
        <p><a href="${esc(tenantPortal(data.activate_url))}" target="_blank" rel="noopener">Activate in the portal</a>, then click Refresh access.
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
}
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

// ------------------------------------------------------------------ timeline
// One list, newest first, of what happened to a resource: Azure's alerts, changes, operations and Resource Health,
// plus ServiceNow and each monitoring tool when connected. "What came first" is the question it answers.

const TIMELINE_WINDOWS = [["6", "6 h"], ["24", "24 h"], ["168", "7 days"]];
const HEALTH_STATUS = { Available: "healthy", Unavailable: "critical", Degraded: "warning" };

function timelineWindow() {
  return stored.choice("ahc.timelineWindow", TIMELINE_WINDOWS.map(([v]) => v), "24");
}

function azureEvents(data) {
  const events = [];
  for (const a of data.alerts) {
    const times = a.count ? ` ${a.count} times since ${new Date(a.first).toLocaleString()}` : "";
    events.push({ at: a.started, source: "Alert", name: a.name, status: a.status, title: `${a.name} fired${times}`, links: a.links,
      detail: [`Sev${fmt(a.severity)}`, a.what, a.service || "Azure Monitor", a.condition === "Fired" ? "still fired" : "", a.count ? "latest shown" : ""].filter(Boolean).join(" · ") });
    if (a.resolved && !a.count) events.push({ at: a.resolved, source: "Alert", status: "healthy", title: `${a.name} resolved`, detail: `Sev${fmt(a.severity)}` });
  }
  for (const c of data.changes) {
    const fields = Object.keys(c.changes || {});
    events.push({ at: c.at, source: "Change", status: c.type === "Delete" ? "warning" : "info", title: `${c.type || "Change"} by ${c.by || "unknown"}`,
      detail: [c.client, fields.slice(0, 6).join(", ") + (fields.length > 6 ? ` +${fields.length - 6}` : "")].filter(Boolean).join(" · ") });
  }
  for (const o of data.operations) {
    const failed = /fail/i.test(o.status || "");
    events.push({ at: o.at, source: "Operation", status: failed ? "critical" : "info",
      title: `${o.operation || "Operation"}${o.count ? ` ×${o.count}` : ""}`,
      detail: [o.status, o.caller, o.count && `first ${new Date(o.first).toLocaleString()}, latest shown`].filter(Boolean).join(" · ") });
  }
  for (const h of data.health_history) {
    events.push({ at: h.at, source: "Resource Health", status: HEALTH_STATUS[h.state] || "unknown", title: h.state || "Unknown",
      detail: [h.summary, h.reason].filter(Boolean).join(" · ") });
  }
  return events;
}

// A change's window as a pill colour: running now or overrunning stands out, closed ones are history.
const SN_CHANGE_TONE = { "in progress": "soon", "past its window": "warning", scheduled: "info", "not scheduled": "unknown", closed: "unknown" };
// What else names a change's resources: its affected CIs and its tasks (when those tables are read).
const snChangeParts = (r) => [(r.affected_cis || []).length ? `Affected CIs: ${r.affected_cis.join(", ")}` : "",
  (r.tasks || []).length ? `Tasks: ${r.tasks.map((t) => `${t.number}${t.state ? ` (${t.state})` : ""}`).join(", ")}` : ""].filter(Boolean).join(" · ");
const snChangeSpan = (d) => `the last ${d.change_history_days} days, now, and the next ${d.change_ahead_days} days`;

// One key per record, shared by the timeline's events and Overview's lists, so each is shown once.
const snKey = (r) => `servicenow:${r.sys_id || r.number}`;
const toolKey = (kind, i) => `${kind}:${i.id}`;

function serviceNowEvents(sn) {
  const priority = (p) => (p === 1 ? "critical" : p === 2 ? "warning" : "info");
  return [
    ...sn.incidents.map((r) => ({ key: snKey(r), at: r.opened_at, source: "ServiceNow", status: priority(r.priority), url: r.url,
      title: `${r.number} opened: ${r.title}`, detail: [r.priority_label, r.state, r.group].filter(Boolean).join(" · ") })),
    ...sn.changes.map((r) => ({ at: r.start, source: "ServiceNow", status: "info", url: r.url,
      title: `${r.number} change ${r.window}: ${r.title}`, detail: [r.type, r.risk && `risk ${r.risk}`, r.end && `ends ${new Date(r.end).toLocaleString()}`].filter(Boolean).join(" · ") })),
    ...[...sn.alerts, ...sn.events].map((r) => ({ at: r.updated_at, source: "ServiceNow", status: "warning", url: r.url,
      title: `${r.number || r.kind}: ${r.title}`, detail: [r.severity && `severity ${r.severity}`, r.source].filter(Boolean).join(" · ") })),
  ];
}

function monitorEvents(kind, data) {
  return data.issues.map((i) => ({ key: toolKey(kind, i), at: i.activated_at, source: toolLabel(kind), url: i.url,
    status: i.priority === "critical" ? "critical" : i.priority === "warning" ? "warning" : "info",
    title: `Issue: ${i.title || i.id}`, detail: [i.priority_label, i.condition, i.policy].filter(Boolean).join(" · ") }));
}

// Every source's events for one resource, newest first; shared by the Timeline tab and the Overview.
async function timelineData(node, hours) {
  const since = Date.now() - Number(hours) * 3600000;
  const sources = [exploreGet("activity", node, { hours }).then((d) => ({ name: "Azure", kind: "azure", events: azureEvents(d), errors: d.errors }))];
  if (state.snEnabled) {
    sources.push(exploreGet("servicenow", node, { connection: state.selected.servicenow || "" })
      .then((d) => (d.available ? { name: "ServiceNow", kind: "servicenow", events: serviceNowEvents(d), raw: d } : { name: "ServiceNow", unavailable: d.reason, fix: d.fix })));
  }
  for (const kind of toolKinds()) {
    sources.push(exploreGet("monitors", node, toolParams(kind))
      .then((d) => (d.available ? { name: toolLabel(kind), kind, events: monitorEvents(kind, d), errors: d.notes, raw: d } : { name: toolLabel(kind), unavailable: d.reason, fix: d.fix })));
  }
  const names = ["Azure", ...(state.snEnabled ? ["ServiceNow"] : []), ...toolKinds().map(toolLabel)];
  const results = await Promise.all(sources.map((p, i) => p.catch((error) => ({ name: names[i], unavailable: error.message }))));
  const all = results.flatMap((r) => r.events || []).filter((e) => e.at && new Date(e.at).getTime() >= since)
    .sort((a, b) => new Date(b.at) - new Date(a.at));
  return { all, results };
}

async function tabTimeline(node, body) {
  const hours = timelineWindow();
  const { all, results } = await timelineData(node, hours);
  const hidden = new Set(state.timelineHidden || []);
  const counts = {};
  for (const e of all) counts[e.source] = (counts[e.source] || 0) + 1;
  const shown = all.filter((e) => !hidden.has(e.source));
  const notes = [
    ...results.filter((r) => r.unavailable).map((r) => `${r.name}: ${r.unavailable}${r.fix ? ` ${r.fix}` : ""}`),
    ...results.flatMap((r) => (r.errors || []).map((e) => `${r.name}: ${e}`)),
    ...(!state.snEnabled && !toolKinds().length ? ["ServiceNow and monitoring tools aren't connected for this tenant, so only Azure's own events show. Settings → Integrations adds them."] : []),
  ];
  let day = "";
  const rows = shown.map((e) => {
    const when = new Date(e.at);
    const label = when.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    const head = label !== day ? `<li class="tl-day">${esc((day = label))}</li>` : "";
    return `${head}<li class="tl-event"><span class="tl-time" title="${esc(when.toLocaleString())}">${esc(when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</span>
      ${pill(e.status)} <span class="tl-source">${esc(e.source)}</span> <strong>${e.url ? monLink(e.url, e.title) : esc(e.title)}</strong>
      ${e.detail ? `<div class="muted tl-detail">${escTimes(e.detail)}</div>` : ""}${(e.links || []).length ? `<div class="attn-links">${linksHtml(e.links)}</div>` : ""}</li>`;
  }).join("");
  body.innerHTML = `<div class="table-tools">
      <label class="inline">Last <select id="tl-window">${TIMELINE_WINDOWS.map(([v, l]) => `<option value="${v}" ${v === hours ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      ${Object.keys(counts).sort().map((src) => `<label class="inline"><input type="checkbox" data-tl-source="${esc(src)}" ${hidden.has(src) ? "" : "checked"}> ${esc(src)} (${counts[src]})</label>`).join("")}
    </div>
    ${notes.length ? `<div class="notice partial"><strong>Not in the timeline</strong><ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
    ${shown.length ? `<ul class="timeline">${rows}</ul>` : `<p class="muted">Nothing happened to this resource in the last ${esc(TIMELINE_WINDOWS.find(([v]) => v === hours)[1])}${all.length ? " from the sources ticked" : ""}.</p>`}`;
  body.querySelector("#tl-window").addEventListener("change", (event) => {
    stored.set("ahc.timelineWindow", event.target.value);
    renderTab(node, "timeline");
  });
  for (const box of body.querySelectorAll("[data-tl-source]")) {
    box.addEventListener("change", () => {
      const off = new Set(state.timelineHidden || []);
      if (box.checked) off.delete(box.dataset.tlSource); else off.add(box.dataset.tlSource);
      state.timelineHidden = [...off];
      renderTab(node, "timeline");
    });
  }
}

async function tabRecommendations(node, body) {
  const data = await exploreGet("recommendations", node);
  const on = (r) => (!r.inside ? '<span class="muted">this resource</span>'
    : `${r.count} inside it<div class="muted">${r.on.map(esc).join(", ")}${r.count > r.on.length ? ", …" : ""}</div>`);
  const note = (rows) => (rows.some((r) => r.inside) ? `<p class="muted">Includes findings on resources inside this one (for
      example an API Management service's APIs or a SQL server's databases), one row per finding with how many it's on.</p>` : "");
  body.innerHTML = `
    <h4>Azure Advisor <span class="muted">${data.advisor.length}</span></h4>${note(data.advisor)}
    ${table(["Impact", "Category", "Problem", "Solution", "On"], data.advisor.map((r) => [
      esc(r.impact), esc(r.category), esc(r.problem), esc(r.solution), on(r)]))}
    <h4>Defender for Cloud, unhealthy <span class="muted">${data.security.length}</span></h4>${note(data.security)}
    ${table(["Severity", "Finding", "Category", "On"], data.security.map((r) => [
      esc(r.severity), esc(r.name), esc(r.category), on(r)]))}
    ${noticesHtml(data.errors)}`;
}

// ------------------------------------------------------------------ application insights
// One component: this window against the same window last week. Several apps often share one, so rows carry the role.

const AI_WINDOWS = ["1h", "6h", "24h"];

function aiWindow() {
  return stored.choice("ahc.aiWindow", AI_WINDOWS, "1h");
}

const pctText = (v) => (v == null ? "–" : `${fmt(v)}%`);
const msText = (v) => (v == null ? "–" : `${fmt(v)} ms`);
const seen = (iso) => agoHtml(iso);
const codes = (list) => esc((list || []).join(", ")) || "–";

// Worse: more than one point above last week's failure rate, or failing now with none last week.
const failingMore = (r) => r.failed_pct != null && r.failed > 0 && (!r.week || r.week.failed_pct == null || r.failed_pct > r.week.failed_pct + 1);

function roleCompare(r) {
  if (!r.week) return '<span class="muted">no requests last week</span>';
  return `${fmt(r.week.total)} · ${pctText(r.week.failed_pct)} failed · p95 ${msText(r.week.p95)}`;
}

// An app's component, picked by hand when Azure doesn't say: remembered per app in this browser.
function aiPicks() {
  return stored.json("ahc.aiPick", {});
}

function setAiPick(appId, componentId) {
  const picks = aiPicks();
  if (componentId) picks[appId.toLowerCase()] = componentId; else delete picks[appId.toLowerCase()];
  stored.set("ahc.aiPick", picks);
}

const AI_HOW = { tag: "its hidden-link tag", name: "probable: a related name in this resource group", picked: "your pick",
  logger: "its App Insights logger" };

function aiLinkHtml(link) {
  const options = link.candidates.map((c) => `<option value="${esc(c.id)}" ${c.id.toLowerCase() === (link.component_id || "").toLowerCase() ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const picker = link.candidates.length ? `<select id="ai-pick"><option value="">${link.component_id ? "Pick another…" : "Pick its component…"}</option>${options}</select>
    ${link.how === "picked" ? '<button type="button" class="link" id="ai-unpick">forget my pick</button>' : ""}` : "";
  if (!link.component_id && link.gateway) {
    return `<p class="notice">This API Management service has no Application Insights logger, so its requests aren't in
      any component. Add one (API Management → Application Insights) to see its failures here and in Investigate.</p>`;
  }
  if (!link.component_id) {
    return `<p class="notice">No Application Insights component is linked to this app: it has no hidden-link tag and no
      component in its resource group has a related name.${link.candidates.length ? " If one of these is it, pick it:" :
      " Its resource group has no components either; search for the component and open it directly."}</p>${picker}`;
  }
  return `<p class="ai-link">Data from <strong>${esc(link.component_name)}</strong>
    <span class="muted">· found by ${esc(AI_HOW[link.how] || link.how)}</span> ${picker}</p>`;
}

// A button that opens Investigate on this component (and an operation, or one request's trace).
const aiInvestigate = (data, target, label, title) =>
  `<button type="button" class="link" title="${esc(title)}" data-investigate="${esc(JSON.stringify({ component: data.component_id, ...target }))}">${esc(label)}</button>`;

// The last hour from metrics, as Investigate's start list shows it.
function aiNowHtml(data) {
  const c = data.now;
  if (!c) return "";
  const row = { ...c, measured: true };
  return `<div class="ai-now">${c.status ? pillHtml(c.status, c.status, { title: c.why || "" }) : ""}
    <span><span class="muted">Last hour</span> ${invLastHour(row)}</span>
    <span><span class="muted">5xx</span> ${invImpact(row)}</span>
    <span title="Failed requests per hour, last 24 h">${sparkHtml(c.trend, "Failed requests per hour, last 24 h")}</span>
    <span><span class="muted">Same hour last week</span> ${invWeek(row)}</span>
    <span><span class="muted">Exceptions</span> <span class="${(c.causes || []).includes("exceptions") ? "worse" : ""}">${fmt(c.exceptions)}</span>
      <span class="muted">(last week ${fmt(c.week.exceptions)})</span></span>
    <span class="spacer"></span>${aiInvestigate(data, {}, "Investigate this app", "Investigate: its failing endpoints, exceptions, dependencies and recent changes")}</div>`;
}

async function tabAppInsights(node, body) {
  const win = aiWindow();
  body.innerHTML = loading("Querying Application Insights…");
  const params = { window: win };
  const pick = node.type === AI_TYPE ? "" : aiPicks()[node.id.toLowerCase()];
  if (pick) params.component = pick;
  const data = await exploreGet("appinsights", node, params);
  const relink = () => {
    const select = $("ai-pick"), unpick = $("ai-unpick");
    const again = () => tabAppInsights(node, body).catch((error) => { body.innerHTML = errorHtml(error); });
    if (select) select.addEventListener("change", () => { if (select.value) { setAiPick(node.id, select.value); again(); } });
    if (unpick) unpick.addEventListener("click", () => { setAiPick(node.id, ""); again(); });
  };
  if (!data.available) {
    body.innerHTML = aiLinkHtml(data.link);
    relink();
    return;
  }
  const span = `${new Date(data.from).toLocaleString()} → ${new Date(data.to).toLocaleTimeString()}`;
  body.innerHTML = `${data.link ? aiLinkHtml(data.link) : ""}
    <div class="ai-head">
      <label>Last <select id="ai-window">${AI_WINDOWS.map((w) => `<option ${w === win ? "selected" : ""}>${w}</option>`).join("")}</select></label>
      <span class="muted">${esc(span)}, compared with the same ${esc(win)} a week ago</span>
    </div>
    ${noticesHtml(data.errors)}
    ${aiNowHtml(data)}
    <h4>Requests by role</h4>${table(["Role", "Requests", "Failed", "p95", "Same window last week"], data.roles.map((r) => [
      esc(r.role || "(no role)"), fmt(r.total), `<span class="${failingMore(r) ? "worse" : ""}">${fmt(r.failed)} (${pctText(r.failed_pct)})</span>`,
      msText(r.p95), roleCompare(r)]))}
    <h4>Failing operations</h4>${table(["Operation", "Role", "Failed", "Out of", "Codes", "Last", ""], data.operations.map((r) => [
      esc(r.operation || "–"), esc(r.role || ""), `${fmt(r.failed)} <span class="muted">(${pctText(r.failed_pct)})</span>`, fmt(r.total),
      codes(r.codes), seen(r.last_seen), aiInvestigate(data, { operation: r.operation, role: r.role }, "Investigate",
        "Its sample requests, one end to end, and the pod that served it")]))}
    <h4>Exceptions</h4>${table(["Exception", "Operation", "Role", "Count", "Last", ""], data.exceptions.map((r) => [
      `<strong>${esc(r.type)}</strong><div class="ai-msg muted">${esc((r.message || "").slice(0, 300))}</div>`, esc(r.operation || ""),
      esc(r.role || ""), fmt(r.total), seen(r.last_seen),
      r.trace_id ? aiInvestigate(data, { trace: r.trace_id, at: r.last_seen, instance: r.instance }, "Follow", "The latest one's request end to end, in Investigate") : ""]))}
    <h4>Failing dependencies</h4>${table(["Type", "Target", "Role", "Failed", "Codes", "p95", "Last", ""], data.dependencies.map((r) => [
      esc(r.type), `<span class="ai-msg">${esc(r.target)}</span>`, esc(r.role || ""), `${fmt(r.failed)} of ${fmt(r.total)}`,
      codes(r.codes), msText(r.p95_ms), seen(r.last_seen),
      r.trace_id ? aiInvestigate(data, { trace: r.trace_id }, "Follow", "A request with one of these failed calls, in Investigate") : ""]))}
    <h4>Availability tests</h4>${data.availability.length ? table(["Test", "Location", "Runs", "Failed", "Last"], data.availability.map((r) => [
      esc(r.test), esc(r.location), fmt(r.runs), r.failed ? `<span class="worse">${fmt(r.failed)}</span>` : "0", seen(r.lastSeen)]))
      : '<p class="muted">No availability test ran in this window.</p>'}
    <details class="mon-query"><summary>Queries sent</summary>${data.queries.map((q) =>
      `<div class="muted">${esc(q.section)}</div><pre>${esc(q.query)}</pre>`).join("")}</details>`;
  relink();
  $("ai-window").addEventListener("change", (event) => {
    stored.set("ahc.aiWindow", event.target.value);
    tabAppInsights(node, body).catch((error) => { body.innerHTML = errorHtml(error); });
  });
}

// ------------------------------------------------------------------ investigate
// One failing request, step by step: 1 operations failing in a component, 2 sample requests of one, 3 one request end
// to end (with the gateway's spans when API Management logs to App Insights), 4 the pod that served it: its state,
// logs at that minute, warning events and changes before. Each step reads only when clicked and shows its queries.

const INV_KIND_TONE = { request: "info", dependency: "info", exception: "critical", trace: "unknown" };
const INV_PLURAL = { request: "requests", dependency: "dependencies", exception: "exceptions", trace: "log lines" };
const INV_SEVERITIES = [["", "Any level"], ["2", "Warning and worse"], ["3", "Error and worse"]];
const inv = { component: "", gateways: [], untick: new Set(), loaded: "", pending: null, request: null, found: {} };

function invWindow() {
  return stored.choice("ahc.invWindow", AI_WINDOWS, "1h");
}

// Gateway components ticked to read with a trace (all that log to App Insights, unless unticked here).
function invAlso() {
  return [...new Set(inv.gateways.filter((g) => !inv.untick.has(g.id)).flatMap((g) => g.components))];
}

function invParams(extra = {}) {
  return { component: inv.component, window: invWindow(), ...extra };
}

const invTime = (iso) => (iso ? `<span title="${esc(iso)}">${esc(new Date(iso).toLocaleTimeString())}</span> ${agoHtml(iso)}` : "–");
const invBox = (id) => $(id).querySelector(".inv-body");

function invStep(id, html) {
  const box = invBox(id);
  box.innerHTML = html;
  $(id).classList.remove("hidden");
  $(id).scrollIntoView({ behavior: "smooth", block: "start" });
  return box;
}

function invReset(from) {
  const order = ["inv-ops", "inv-samples", "inv-request", "inv-pod"];
  for (const id of order.slice(order.indexOf(from))) $(id).classList.add("hidden");
}

// "40 failed of 400 (10%)", against the same hour last week.
function invLastHour(c) {
  if (!c.measured) return `<span class="muted">${c.unread ? "no access to its metrics" : "not measured (see the note above)"}</span>`;
  if (!c.requests) return '<span class="muted">no requests in the last hour</span>';
  return `<span class="${(c.causes || []).includes("failed") ? "worse" : ""}">${fmt(c.failed)} failed of ${fmt(c.requests)} (${pctText(c.failed_pct)})</span>`;
}

function invWeek(c) {
  if (!c.measured) return "";
  return c.week.requests ? `${fmt(c.week.failed)} of ${fmt(c.week.requests)} (${pctText(c.week.failed_pct)})` : '<span class="muted">no requests</span>';
}

// What reports to a component: Web/Function Apps by their hidden-link tag, API Management by its loggers.
const invReporters = (c) => [...c.apps, ...(inv.gateways || [])
  .filter((g) => g.components.some((id) => sameId(id, c.id))).map((g) => `API Management ${g.name}`)];

function invAppsHtml(c) {
  const names = invReporters(c);
  return names.length ? esc(names.join(", "))
    : '<span class="muted" title="Web and Function Apps name their component in a hidden-link tag, API Management in its loggers; AKS apps don\'t">not linked</span>';
}

// Hourly counts as a small line, oldest left; the newest point is marked.
function sparkHtml(values, title) {
  if (!values || values.length < 2) return '<span class="muted">–</span>';
  title = `${title}: peak ${fmt(Math.max(...values))}, last hour ${fmt(values[values.length - 1])}`;
  const w = 96, h = 22, top = Math.max(...values, 1);
  const pts = values.map((v, i) => [(i * (w - 4)) / (values.length - 1) + 2, h - 2 - ((h - 4) * v) / top]);
  const [lx, ly] = pts[pts.length - 1];
  return `<svg class="spark${Math.max(...values) ? "" : " flat"}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title>
    <polyline points="${pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}"/><circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2"/></svg>`;
}

// 5xx in the last hour and their share of every component's 5xx in scope.
function invImpact(c) {
  if (!c.measured || c.server_errors == null) return '<span class="muted" data-v="-1">–</span>';
  if (!c.server_errors) return '<span class="muted" data-v="0">none</span>';
  return `<span data-v="${c.server_errors}"><strong>${fmt(c.server_errors)}</strong>${c.share != null ? ` <span class="muted">· ${c.share}% of scope</span>` : ""}</span>`;
}

function renderInvApps(found) {
  const rows = inv.components || [];
  $("inv-apps-out").innerHTML = `${noticesHtml(found.notes)}${rows.length ? table(["", "App Insights component", "Apps that report to it",
    "Last hour", "Last 24 h", "Server errors (5xx)", "Same hour last week", "Exceptions", ""], rows.map((c) => [
      c.status ? pillHtml(c.status, c.status, { title: c.why || "" }) : '<span class="muted">–</span>',
      `<strong>${esc(c.name)}</strong> ${portalLinkHtml(c.id)}<div class="muted">${esc([c.resource_group, subName(c.subscription_id)].filter(Boolean).join(" · "))}</div>`,
      invAppsHtml(c),
      invLastHour(c), c.measured ? sparkHtml(c.trend, "Failed requests per hour, last 24 h") : "", invImpact(c), invWeek(c),
      c.measured ? `<span class="${(c.causes || []).includes("exceptions") ? "worse" : ""}">${fmt(c.exceptions)}</span> <span class="muted">(last week ${fmt(c.week.exceptions)})</span>` : "",
      `<button type="button" class="${c.status ? "primary" : "link"}" data-inv-app="${esc(c.id)}">Investigate</button>`]), "inv-apps")
    : '<p class="muted">No Application Insights component in scope: widen the scope, or open one with Search.</p>'}`;
}

function invPick(componentId, name = "") {
  const c = (inv.components || []).find((x) => sameId(x.id, componentId));
  inv.component = componentId;
  inv.componentName = (c && c.name) || name || componentId.split("/").pop();
  $("inv-current").innerHTML = `<span class="muted">Investigating</span> <strong>${esc(inv.componentName)}</strong>
    ${c && invReporters(c).length ? `<span class="muted">(${esc(invReporters(c).join(", "))})</span>` : ""}
    ${c ? invLastHour(c) : ""}${c && c.measured ? ` <span class="muted">5xx</span> ${invImpact(c)} ${sparkHtml(c.trend, "Failed requests per hour, last 24 h")}` : ""}
    ${portalLinkHtml(componentId, { label: "Portal ↗", title: "The component in the Azure portal" })}
    ${portalLinkHtml(componentId, { label: "Failures ↗", page: "failures", title: "Its Failures page in the Azure portal" })}
    ${portalLinkHtml(componentId, { label: "Logs ↗", page: "logs", title: "Its Logs page in the Azure portal: paste a query from Query sent" })}
    <button type="button" class="link" data-inv-change>pick another app</button>`;
  $("inv-current").classList.remove("hidden");
  $("inv-tools").classList.remove("hidden");
  $("inv-apps").classList.add("collapsed");
  renderInvSearchApps();
}

async function loadInvestigate() {
  const scope = currentScope();
  const key = JSON.stringify(scope);
  if (inv.loaded === key && !inv.pending) return;
  inv.loaded = key;
  inv.component = "";
  $("inv-current").classList.add("hidden");
  $("inv-tools").classList.add("hidden");
  $("inv-apps").classList.remove("collapsed");
  invReset("inv-ops");
  inv.components = [];
  $("inv-search-out").innerHTML = "";
  if (!scope.subscriptions.length) {
    $("inv-apps-out").innerHTML = pickScopeHtml("Investigate measures the Application Insights components in scope.");
    renderInvSearchApps();
    $("inv-gateways").innerHTML = "";
    return;
  }
  $("inv-apps-out").innerHTML = loading("Measuring the last hour of every component in scope…");
  $("inv-gateways").innerHTML = "";
  renderInvSearchApps();
  let found, gateways;
  try {
    [found, gateways] = await Promise.all([api("api/investigate/components", { params: scopeParams(scope) }),
      api("api/investigate/gateways", { params: scopeParams(scope) }).catch((error) => ({ gateways: [], notes: [error.message] }))]);
  } catch (error) {
    inv.loaded = "";
    if (!error.signedOut) $("inv-apps-out").innerHTML = noticeHtml(error);
    renderInvSearchApps();
    return;
  }
  inv.components = found.components;
  inv.gateways = gateways.gateways;
  inv.gatewayNotes = gateways.notes || [];
  renderInvApps(found);
  renderInvGateways();
  renderInvSearchApps();
  const pending = inv.pending;
  inv.pending = null;
  if (!pending) return;
  invPick(pending.component);
  await loadInvOperations();
  if (pending.operation != null) loadInvSamples({ operation: pending.operation, role: pending.role || "" });
  else if (pending.request) loadInvRequest(pending.request);
}

function renderInvGateways() {
  const rows = inv.gateways.map((g) => {
    const where = [g.components.length ? `logs to ${g.components.map((c) => c.split("/").pop()).join(", ")}` : "",
      g.workspace ? "gateway logs in a workspace" : "", g.unnamed_loggers ? `${g.unnamed_loggers} logger(s) without a component ID` : "",
      g.error].filter(Boolean).join(" · ") || "no App Insights logger, no gateway logs";
    return `<label class="inline" title="${esc(where)}"><input type="checkbox" data-inv-gw="${esc(g.id)}" ${g.components.length ? "" : "disabled"}
      ${g.components.length && !inv.untick.has(g.id) ? "checked" : ""}> ${esc(g.name)} <span class="muted">${esc(where)}</span></label> ${portalLinkHtml(g.id)}`;
  });
  $("inv-gateways").innerHTML = rows.length
    ? `<span class="muted" title="API Management logs its span of a request to its own App Insights component">When following a request, also read the gateway's spans from:</span> ${rows.join(" ")}${noticesHtml(inv.gatewayNotes)}`
    : '<span class="muted">No API Management service in scope: following a request reads only the app\'s own spans.</span>';
}

async function loadInvOperations() {
  invReset("inv-ops");
  $("inv-ops").classList.remove("hidden");
  $("inv-ops").querySelector("h3 .inv-what").textContent = `· ${inv.componentName}`;
  const c = (inv.components || []).find((x) => sameId(x.id, inv.component));
  const exceptionsFirst = c && (c.causes || []).includes("exceptions") && !c.causes.includes("failed");
  $("inv-ops").querySelector(".inv-parts").classList.toggle("exceptions-first", Boolean(exceptionsFirst));
  inv.found = {};
  loadInvExceptions();
  loadInvDependencies();
  loadInvChanges(c);
  const out = $("inv-ops-out");
  out.innerHTML = loading("Reading failing operations…");
  let data;
  try { data = await api("api/investigate/operations", { params: invParams() }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  inv.found.operations = data.rows;
  out.innerHTML = `${noticesHtml(data.errors)}${data.rows.length ? table(["Endpoint", "Service", "Failed requests", "Out of", "Failure rate",
    "HTTP status codes", "Last failure", ""],
    data.rows.map((r) => [esc(r.operation || "–"), esc(r.role || "") || '<span class="muted">(no name)</span>', `<span class="worse">${fmt(r.failed)}</span>`,
      fmt(r.total), pctText(r.failed_pct), codes(r.codes), seen(r.last_seen),
      `<button type="button" class="link" data-inv-op="${esc(JSON.stringify({ operation: r.operation, role: r.role }))}">Samples</button>`]), "inv-ops")
    : `<p class="ok">✓ No failed requests in the last ${esc(invWindow())}.</p>`}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
}

async function loadInvExceptions() {
  const out = $("inv-exc-out");
  out.innerHTML = loading("Reading exceptions…");
  let data;
  try { data = await api("api/investigate/exceptions", { params: invParams() }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  inv.found.exceptions = data.rows;
  out.innerHTML = `${noticesHtml(data.errors)}${data.rows.length ? table(["Exception", "Operation", "Service", "Count", "Last seen", ""],
    data.rows.map((r) => [`<strong>${esc(r.type || "–")}</strong><div class="ai-msg muted">${esc(r.message || "")}</div>`,
      esc(r.operation || "") || '<span class="muted">(none)</span>', esc(r.role || "") || '<span class="muted">(no name)</span>',
      `<span class="worse">${fmt(r.total)}</span>`, seen(r.last_seen),
      r.trace_id ? `<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id, at: r.last_seen, instance: r.instance }))}">Follow</button>` : ""]), "inv-exc")
    : `<p class="ok">✓ No exceptions in the last ${esc(invWindow())}.</p>`}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
}

async function loadInvDependencies() {
  const out = $("inv-dep-out");
  out.innerHTML = loading("Reading failing dependencies…");
  let data;
  try { data = await api("api/investigate/dependencies", { params: invParams() }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  inv.found.dependencies = data.rows;
  out.innerHTML = `${noticesHtml(data.errors)}${data.rows.length ? table(["Type", "Target", "Service", "Failed", "Out of", "Failure rate",
    "Result codes", "p95", "Last failure", ""], data.rows.map((r) => [esc(r.type || "–"), `<span class="ai-msg">${esc(r.target || "")}</span>`,
      esc(r.role || "") || '<span class="muted">(no name)</span>', `<span class="worse">${fmt(r.failed)}</span>`, fmt(r.total),
      pctText(r.failed_pct), codes(r.codes), msText(r.p95_ms), seen(r.last_seen),
      r.trace_id ? `<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id }))}">Follow</button>` : ""]), "inv-dep")
    : `<p class="ok">✓ No failed dependency calls in the last ${esc(invWindow())}.</p>`}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
}

// The component, the apps that report to it and the gateways that log to it.
const invChangeTargets = (c) => [inv.component, ...((c && c.app_ids) || []),
  ...(inv.gateways || []).filter((g) => g.components.some((id) => sameId(id, inv.component))).map((g) => g.id)];

async function loadInvChanges(c) {
  const out = $("inv-chg-out");
  out.innerHTML = loading("Reading recent changes…");
  let data;
  try { data = await api("api/investigate/recent-changes", { params: { id: invChangeTargets(c), window: invWindow() } }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  inv.found.changes = data.rows;
  out.innerHTML = `${data.rows.length ? table(["When", "Resource", "Change", "By", "What changed"], data.rows.map((r) => [invTime(r.at),
      `${esc(r.name)} ${portalLinkHtml(r.id)}`, esc(r.change),
      `${esc(r.by || "unknown")}<div class="muted">${esc(r.client || "")}</div>`,
      r.fields.length ? `<span class="ai-msg">${esc(r.fields.join(", "))}</span>` : '<span class="muted">–</span>']), "inv-chg")
    : `<p class="muted">No changes in the last ${data.hours} h.</p>`}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock({ queries: [{ label: "Resource Graph", query: data.query }] })}</details>`;
}

async function loadInvSamples(op, all = false) {
  invReset("inv-samples");
  inv.op = op;
  inv.opAll = all;
  const box = invStep("inv-samples", loading("Reading sample requests…"));
  $("inv-samples").querySelector("h3 .inv-what").textContent = `· ${op.operation || "–"}${op.role ? ` · ${op.role}` : ""}`;
  let data;
  try { data = await api("api/investigate/samples", { params: invParams({ ...op, all: all ? "1" : "0" }) }); } catch (error) {
    if (!error.signedOut) box.innerHTML = noticeHtml(error);
    return;
  }
  box.innerHTML = `<div class="table-tools"><label class="inline"><input type="checkbox" id="inv-all" ${all ? "checked" : ""}> successful ones too</label></div>
    ${noticesHtml(data.errors)}${table(["When", "HTTP status", "Took", "Instance (pod or host)", "URL", ""], data.rows.map((r) => [
      invTime(r.time), String(r.success).toLowerCase() === "true" ? esc(r.code) : `<span class="worse">${esc(r.code)}</span>`,
      msText(r.duration_ms), esc(r.instance || ""), `<span class="ai-msg">${esc(r.url || "")}</span>`,
      `<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id, at: r.time, instance: r.instance, url: r.url }))}">Follow</button>`]), "inv-samples")}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
  $("inv-all").addEventListener("change", (event) => loadInvSamples(op, event.target.checked));
}

function invPath(url) {
  try { return new URL(url).pathname; } catch { return ""; }
}

function invSpanRow(s) {
  const failed = String(s.success).toLowerCase() === "false";
  const detail = s.kind === "exception" ? `<details><summary>${esc((s.message || "").slice(0, 300) || "(no message)")}</summary>
      ${s.stack ? `<pre>${esc(s.stack)}</pre>` : '<p class="muted">No stack was sent.</p>'}</details>`
    : `<span class="ai-msg">${esc((s.message || s.target || "").slice(0, 400))}</span>`;
  return `<tr class="${failed ? "inv-failed" : ""}"><td class="num">+${fmt(s.offset_ms)} ms</td>
    <td>${pillHtml(INV_KIND_TONE[s.kind] || "info", s.kind)}${s.gateway ? ' <span class="chip-ok">gateway</span>' : ""}</td>
    <td style="padding-left:${0.5 + s.depth}rem">${esc(s.name || (s.severity ? s.severity : ""))}</td>
    <td>${esc(s.role || "")}<div class="muted">${esc(s.instance || "")}</div></td>
    <td>${s.duration_ms == null ? "" : msText(Math.round(s.duration_ms))}</td><td>${failed ? `<span class="worse">${esc(s.code || "failed")}</span>` : esc(s.code || "")}</td>
    <td>${detail}</td></tr>`;
}

// Plain text for an incident ticket, from what's already on the page: no new reads.
function invIncidentText() {
  const c = (inv.components || []).find((x) => sameId(x.id, inv.component)) || {};
  const f = inv.found || {}, t = inv.trace || { spans: [] };
  const top = (rows, n, line) => (rows && rows.length ? rows.slice(0, n).map((r) => `- ${line(r)}`) : ["- none"]);
  const codesOf = (r) => ((r.codes || []).length ? `, codes ${r.codes.join("/")}` : "");
  const reporters = c.apps ? invReporters(c) : [];
  const failed = t.spans.find((s) => !s.gateway && String(s.success).toLowerCase() === "false" && s.kind !== "exception");
  const exception = t.spans.find((s) => s.kind === "exception");
  const lines = [`Investigate: ${inv.componentName}${reporters.length ? ` (${reporters.join(", ")})` : ""}`,
    `Component: ${inv.component}`, `Look-back window: ${invWindow()}`];
  if (c.measured) {
    lines.push(`Last hour: ${c.failed} failed of ${c.requests} requests (${pctText(c.failed_pct)}), ${c.server_errors ?? "?"} of them 5xx`
      + `${c.share != null ? ` (${c.share}% of 5xx in scope)` : ""}, ${c.exceptions} exceptions`,
    `Same hour last week: ${c.week.failed} failed of ${c.week.requests} (${pctText(c.week.failed_pct)}), ${c.week.exceptions} exceptions`);
  }
  lines.push("", "Failing endpoints:", ...top(f.operations, 3, (r) => `${r.operation || "–"} (${r.role || "no name"}): ${r.failed} of ${r.total} failed${codesOf(r)}`),
    "Exceptions:", ...top(f.exceptions, 3, (r) => `${r.type} x${r.total} in ${r.operation || "–"}: ${(r.message || "").slice(0, 160)}`),
    "Failing dependencies:", ...top(f.dependencies, 3, (r) => `${r.type} ${r.target}: ${r.failed} of ${r.total} failed${codesOf(r)}`),
    "Recent changes:", ...top(f.changes, 5, (r) => `${r.at} ${r.name}: ${r.change} by ${r.by || "unknown"}`));
  if (t.req) {
    lines.push("", `Request ${t.req.trace}${t.at ? ` at ${t.at}` : ""}: ${t.spans.length} spans`);
    if (failed) lines.push(`First failure: ${failed.kind} ${failed.name || ""} ${failed.code || ""} on ${failed.instance || "?"}`.replace(/\s+/g, " "));
    if (exception) lines.push(`Exception: ${exception.name}: ${(exception.message || "").slice(0, 300)}`);
  }
  return lines.join("\n");
}

async function loadInvRequest(req) {
  invReset("inv-request");
  inv.request = req;
  const box = invStep("inv-request", loading(`Reading request ${req.trace}…`));
  $("inv-request").querySelector("h3 .inv-what").textContent = `· ${req.trace}`;
  let data;
  try {
    data = await api("api/investigate/trace", { params: invParams({ trace: req.trace, also: invAlso(), ...(req.at ? { at: req.at } : {}) }) });
  } catch (error) {
    if (!error.signedOut) box.innerHTML = noticeHtml(error);
    return;
  }
  const spans = data.rows;
  const root = spans.find((s) => s.kind === "request" && !s.gateway) || {};
  const at = req.at || root.time || (spans[0] || {}).time;
  const instances = [...new Set(spans.filter((s) => !s.gateway && s.instance).map((s) => s.instance))];
  const path = invPath(req.url || root.target || "");
  const gateways = inv.gateways.filter((g) => g.workspace);
  const counts = ["request", "dependency", "exception", "trace"].map((k) => [k, spans.filter((s) => s.kind === k).length]).filter(([, n]) => n);
  inv.trace = { req, spans, at };
  box.innerHTML = `${noticesHtml(data.errors)}
    <p class="muted">${spans.length ? counts.map(([k, n]) => `${n} ${n === 1 ? k : INV_PLURAL[k]}`).join(" · ") : "No spans found for this ID"}
      ${data.truncated ? " · <strong>only the first 500 were read</strong>" : ""}${invAlso().length ? ` · also read ${esc(invAlso().map((c) => c.split("/").pop()).join(", "))}` : ""}</p>
    ${spans.length ? `<table class="data-table inv-trace"><thead><tr><th>At</th><th>What</th><th>Name</th><th>Role · instance</th><th>Duration</th><th>Result</th>
      <th>Detail</th></tr></thead><tbody>${spans.map(invSpanRow).join("")}</tbody></table>` : ""}
    <div class="inv-next"><button type="button" class="primary" data-inv-copy title="A plain-text summary of steps 1, 2 and this request, for the incident">Copy for incident</button>
      ${at ? instances.map((i) => `<button type="button" data-inv-pod="${esc(JSON.stringify({ instance: i, at }))}">Pod ${esc(i)}</button>`).join(" ") : ""}
      ${at ? gateways.map((g) => `<button type="button" data-inv-gwlogs="${esc(JSON.stringify({ id: g.id, name: g.name, at, path }))}"
        title="Its gateway logs share no ID with the request: rows within 2 minutes whose URL contains ${esc(path || "anything")}">Gateway logs: ${esc(g.name)}</button>`).join(" ") : ""}</div>
    <div id="inv-gw-out"></div>
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(data)}</details>`;
}

async function loadInvGatewayLogs(g) {
  const out = $("inv-gw-out");
  out.innerHTML = loading(`Reading ${g.name}'s gateway logs…`);
  let data;
  try { data = await api("api/investigate/gateway-logs", { params: { id: g.id, at: g.at, path: g.path } }); } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  out.innerHTML = `<h4>${esc(g.name)}: gateway logs within 2 minutes${g.path ? `, URL containing <code>${esc(g.path)}</code>` : ""}</h4>
    <p class="muted">Probable: these rows share no ID with the request, only its time and path.${data.table ? ` From ${esc(data.table)}.` : ""}</p>
    ${noticesHtml(data.errors)}${data.rows.length ? table(["Time", "Method", "URL", "Status", "Backend", "Total", "Backend time", "Error"], data.rows.map((r) => [
      invTime(r.TimeGenerated), esc(r.Method || ""), `<span class="ai-msg">${esc(r.Url || "")}</span>`,
      Number(r.ResponseCode) >= 500 ? `<span class="worse">${esc(r.ResponseCode)}</span>` : esc(r.ResponseCode ?? ""),
      esc(r.BackendResponseCode ?? ""), msText(r.TotalTime), msText(r.BackendTime),
      `<span class="ai-msg">${esc([r.LastErrorReason, r.LastErrorMessage].filter(Boolean).join(": "))}</span>`]), "inv-gw")
      : '<p class="muted">No gateway log rows matched.</p>'}
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(data)}</details>`;
}

async function loadInvPod(p) {
  inv.pod = p;
  const box = invStep("inv-pod", loading(`Looking for pod ${p.instance} in the clusters in scope…`));
  $("inv-pod").querySelector("h3 .inv-what").textContent = `· ${p.instance}`;
  let data;
  try { data = await api("api/investigate/pod", { params: { ...scopeParams(currentScope()), instance: p.instance, at: p.at } }); } catch (error) {
    if (!error.signedOut) box.innerHTML = noticeHtml(error);
    return;
  }
  const f = data.found;
  const picker = `<div class="table-tools"><label>Cluster <select id="inv-cluster">${data.clusters.map((c) =>
      `<option value="${esc(c.id)}" ${f && sameId(c.id, f.cluster_id) ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>
    <label>Namespace <input id="inv-ns" value="${esc(f ? f.namespace : "")}" placeholder="e.g. shop" size="14"></label>
    <button type="button" id="inv-pod-read">${f ? "Read again" : "Read this pod"}</button></div>`;
  box.innerHTML = `${noticesHtml(data.notes)}
    ${f ? factsHtml({ facts: [["Cluster", f.cluster_name], ["Namespace", f.namespace], ["State then", f.status],
      ["Started", f.started ? `${new Date(f.started).toLocaleString()} (${ago(f.started)})` : "–"], ["Restarts", f.restarts ?? "–"],
      ["Owner", [f.controller_kind, f.controller].filter(Boolean).join(" ")], ["Node", f.node]].map(([label, value]) => ({ label, value })) })
      + (f.cluster_id ? `<p class="muted">${portalLinkHtml(f.cluster_id, { label: "Cluster in the portal ↗" })}
          ${portalLinkHtml(f.cluster_id, { label: "Its workloads ↗", page: "workloads", title: "The cluster's Workloads page in the Azure portal" })}</p>` : "")
      : `<p class="notice">No cluster's Container Insights knows a pod named ${esc(p.instance)} around then. If it ran on one of these, pick it and its namespace.</p>`}
    ${data.clusters.length ? picker : '<p class="muted">No AKS cluster in scope.</p>'}
    <div id="inv-pod-out"></div>
    <details class="mon-query"><summary>Query sent</summary>${queryBlock({ label: "Container Insights", queries: [{ label: "each cluster", query: data.query, timespan: data.timespan }] })}</details>`;
  const read = () => loadInvPodDetail({ ...p, cluster: $("inv-cluster").value, namespace: $("inv-ns").value.trim() });
  if ($("inv-pod-read")) $("inv-pod-read").addEventListener("click", read);
  if (f) read();
}

async function loadInvPodDetail(p) {
  const out = $("inv-pod-out");
  if (!p.cluster || !p.namespace) { out.innerHTML = noticeHtml("Pick the cluster and type the namespace."); return; }
  out.innerHTML = loading("Reading the pod's logs at that minute, its events and the cluster's changes…");
  const [logs, changes] = await Promise.all([
    api("api/investigate/pod-logs", { params: { id: p.cluster, namespace: p.namespace, pod: p.instance, at: p.at } }).catch((error) => ({ error: error.message })),
    api("api/investigate/changes", { params: { id: p.cluster, namespace: p.namespace, at: p.at } }).catch((error) => ({ errors: [error.message], changes: [], operations: [], events: [] }))]);
  const when = new Date(p.at).toLocaleTimeString();
  out.innerHTML = `<h4>Logs within 2 minutes of ${esc(when)} <span class="muted">· ${esc(logs.source || "")}</span></h4>
    ${logs.error ? noticeHtml(logs.error) : ""}${(logs.lines || []).length ? `<pre class="logs">${esc(logs.lines.join("\n"))}</pre>` : logs.error ? "" : '<p class="muted">No lines.</p>'}
    <h4>Warning events in ${esc(p.namespace)}, 15 minutes either side</h4>
    ${(changes.events || []).length ? table(["Last", "Reason", "Object", "Count", "Message"], changes.events.map((e) => [invTime(e.Last), esc(e.Reason),
      esc(`${e.Kind || ""} ${e.Object || ""}`.trim()), fmt(e.Count), `<span class="ai-msg">${esc(e.Message || "")}</span>`]), "inv-events")
      : '<p class="muted">None.</p>'}
    <h4>Changes on the cluster, 6 hours before to 10 minutes after</h4>${noticesHtml(changes.errors)}
    ${(changes.changes || []).length ? table(["When", "Change", "By", "Client"], changes.changes.map((c) => [invTime(c.at), esc(c.type), esc(c.by || ""), esc(c.client || "")]), "inv-changes")
      : '<p class="muted">No resource changes.</p>'}
    ${(changes.operations || []).length ? table(["When", "Operation", "Status", "Caller"], changes.operations.map((o) => [invTime(o.at), esc(o.operation),
      esc(o.status || ""), esc(o.caller || "")]), "inv-operations") : '<p class="muted">No control-plane operations.</p>'}`;
}

// Search logs names its component right there: the components in scope, worst first, or the one picked elsewhere.
function renderInvSearchApps() {
  const select = $("inv-search-app");
  const rows = inv.components || [];
  const known = rows.some((c) => sameId(c.id, inv.component));
  const options = rows.map((c) => `<option value="${esc(c.id)}" ${sameId(c.id, inv.component) ? "selected" : ""}>${esc(c.name)}${c.status && c.status !== "healthy" ? ` (${esc(c.status)})` : ""} · ${esc(c.resource_group || "")}</option>`);
  if (inv.component && !known) options.unshift(`<option value="${esc(inv.component)}" selected>${esc(inv.componentName || inv.component.split("/").pop())}</option>`);
  const empty = !currentScope().subscriptions.length ? "Apply a scope first"
    : $("inv-apps-out").querySelector(".loading") ? "Reading components…" : "None in scope (see Failing requests)";
  select.innerHTML = options.length ? `<option value="" ${inv.component ? "" : "selected"}>Choose one…</option>${options.join("")}` : `<option value="">${empty}</option>`;
  select.disabled = !options.length;
  $("inv-search-run").disabled = !inv.component;
  const out = $("inv-search-out");
  if (!inv.component && !out.querySelector("table")) out.innerHTML = `<p class="muted">${options.length
    ? "Choose the App Insights component to search: it's where the app sends its logs."
    : "Investigate reads the Application Insights components in the scope in use; apply a scope to list them."}</p>`;
}

async function runInvSearch() {
  const out = $("inv-search-out");
  if (!inv.component) { renderInvSearchApps(); return; }
  out.innerHTML = loading("Searching logs…");
  let data;
  try {
    data = await api("api/investigate/search", { params: invParams({ text: $("inv-search-text").value, severity: $("inv-severity").value }) });
  } catch (error) {
    if (!error.signedOut) out.innerHTML = noticeHtml(error);
    return;
  }
  out.innerHTML = `${queryBlock(data)}${noticesHtml(data.errors)}
    <p class="muted">${data.rows.length} line(s), newest first${data.truncated ? " · <strong>only the newest 100</strong>" : ""}.</p>
    ${table(["Time", "Level", "Kind", "Role · instance", "Operation", "Message", ""], data.rows.map((r) => [invTime(r.time), esc(r.severity ?? ""),
      esc(r.kind), `${esc(r.role || "")}<div class="muted">${esc(r.instance || "")}</div>`, esc(r.operation || ""),
      `<span class="ai-msg">${esc((r.message || "").slice(0, 500))}</span>`,
      r.trace_id ? `<button type="button" class="link" data-inv-follow="${esc(JSON.stringify({ trace: r.trace_id, at: r.time, instance: r.instance }))}">Follow</button>` : ""]), "inv-search")}`;
}

function showInvSub(sub) {
  inv.sub = sub;
  for (const b of $("inv-subtabs").querySelectorAll("[data-inv-sub]")) b.classList.toggle("active", b.dataset.invSub === sub);
  $("inv-flow").classList.toggle("hidden", sub !== "flow");
  $("inv-search").classList.toggle("hidden", sub !== "search");
  if (sub === "search") renderInvSearchApps();
  // A component picked on Search logs: its operations load when Failing requests is opened, not before.
  else if (inv.component && $("inv-ops").classList.contains("hidden") && !inv.pending) loadInvOperations();
}

// Refresh: the component list again, then every step that's open, in order, with the same picks.
async function refreshInvestigate() {
  if (inv.sub === "search") {
    if (inv.component && $("inv-search-out").innerHTML.trim()) await freshly(runInvSearch);
    return;
  }
  const open = (id) => !$(id).classList.contains("hidden");
  const keep = { component: inv.component, name: inv.componentName, op: open("inv-samples") ? inv.op : null, all: inv.opAll,
    request: open("inv-request") ? inv.request : null, pod: open("inv-pod") ? inv.pod : null };
  await freshly(async () => {
    inv.loaded = "";
    await loadInvestigate();
    if (!keep.component || !inv.loaded) return;
    invPick(keep.component, keep.name);
    await loadInvOperations();
    if (keep.op) await loadInvSamples(keep.op, keep.all);
    if (keep.request) await loadInvRequest(keep.request);
    if (keep.pod) await loadInvPod(keep.pod);
  });
}

// From another tab (App Insights' failing operations): open Investigate on that component and operation.
function investigate(component, operation = null, role = "", request = null) {
  inv.pending = { component, operation, role, request };
  showInvSub("flow");
  showView("investigate");
}

$("inv-apps-out").addEventListener("click", (event) => {
  const b = event.target.closest("[data-inv-app]");
  if (b) { invPick(b.dataset.invApp); loadInvOperations(); }
});
$("inv-current").addEventListener("click", (event) => {
  if (!event.target.closest("[data-inv-change]")) return;
  $("inv-current").classList.add("hidden");
  $("inv-tools").classList.add("hidden");
  $("inv-apps").classList.remove("collapsed");
  invReset("inv-ops");
  inv.component = "";
  renderInvSearchApps();
});
$("inv-window").innerHTML = AI_WINDOWS.map((w) => `<option ${w === invWindow() ? "selected" : ""}>${w}</option>`).join("");
$("inv-window").addEventListener("change", (event) => { stored.set("ahc.invWindow", event.target.value); if (inv.component) loadInvOperations(); });
$("inv-severity").innerHTML = INV_SEVERITIES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
$("inv-subtabs").addEventListener("click", (event) => { const b = event.target.closest("[data-inv-sub]"); if (b) showInvSub(b.dataset.invSub); });
$("inv-search-run").addEventListener("click", runInvSearch);
$("inv-search-app").addEventListener("change", (event) => {
  $("inv-search-out").innerHTML = "";
  if (event.target.value) invPick(event.target.value);
  else { inv.component = ""; $("inv-current").classList.add("hidden"); $("inv-tools").classList.add("hidden"); $("inv-apps").classList.remove("collapsed"); invReset("inv-ops"); renderInvSearchApps(); }
});
$("inv-refresh").addEventListener("click", refreshInvestigate);
$("inv-search-text").addEventListener("keydown", (event) => { if (event.key === "Enter") runInvSearch(); });
$("inv-trace-go").addEventListener("click", () => {
  const trace = $("inv-trace").value.trim();
  if (!trace) return;
  if (!inv.component) { flash("Pick the app it belongs to first (Investigate on its row), then Follow.", true); return; }
  showInvSub("flow");
  invReset("inv-samples");
  loadInvRequest({ trace });
});
$("inv-gateways").addEventListener("change", (event) => {
  const box = event.target.closest("[data-inv-gw]");
  if (!box) return;
  if (box.checked) inv.untick.delete(box.dataset.invGw); else inv.untick.add(box.dataset.invGw);
});
$("view-investigate").addEventListener("click", (event) => {
  const hit = (attr) => event.target.closest(`[${attr}]`);
  let b;
  if ((b = hit("data-inv-op"))) loadInvSamples(JSON.parse(b.dataset.invOp));
  else if ((b = hit("data-inv-copy"))) copyText(invIncidentText(), b);
  else if ((b = hit("data-inv-follow"))) {
    if (!b.closest("#inv-samples")) invReset("inv-samples");  // from Exceptions or Search: no sample list behind it
    showInvSub("flow");
    loadInvRequest(JSON.parse(b.dataset.invFollow));
  }
  else if ((b = hit("data-inv-pod"))) loadInvPod(JSON.parse(b.dataset.invPod));
  else if ((b = hit("data-inv-gwlogs"))) loadInvGatewayLogs(JSON.parse(b.dataset.invGwlogs));
});

// ------------------------------------------------------------------ metrics

const RANGES = ["1h", "6h", "24h", "7d"];

function watchedMetrics(node) {
  const names = new Set();
  for (const sig of node.signals) {
    const match = /^(.+) \((Average|Maximum|Minimum|Total|Count)\)$/.exec(sig.name);
    if (match) names.add(match[1]);
  }
  return names;
}

async function tabMetrics(node, body) {
  const defs = await exploreGet("metric-definitions", node);
  if (!defs.length) {
    body.innerHTML = '<p class="muted">This resource type has no platform metrics.</p>';
    return;
  }
  const watched = watchedMetrics(node);
  const option = (m) => `<option value="${esc(m.name)}">${esc(m.label)}${m.unit ? ` (${esc(m.unit)})` : ""}</option>`;
  const pinned = defs.filter((m) => watched.has(m.name));
  const others = defs.filter((m) => !watched.has(m.name));
  body.innerHTML = `
    <div class="metric-controls">
      <select id="m-name">${pinned.length ? `<optgroup label="Watched">${pinned.map(option).join("")}</optgroup>` : ""}
        <optgroup label="All metrics">${others.map(option).join("")}</optgroup></select>
      <select id="m-agg"></select>
      <select id="m-split"></select>
      <span class="range">${RANGES.map((r) => `<button type="button" data-range="${r}" class="${r === "6h" ? "active" : ""}">${r}</button>`).join("")}</span>
    </div>
    <div class="chart-wrap"><canvas id="m-chart" aria-label="Metric chart" role="img"></canvas></div>
    <div id="m-table"></div>`;
  const pick = { range: "6h" };
  const sync = () => {
    const def = defs.find((m) => m.name === $("m-name").value);
    const aggs = def.aggregations.length ? def.aggregations : [def.primary];
    $("m-agg").innerHTML = aggs.map((a) => `<option ${a === def.primary ? "selected" : ""}>${esc(a)}</option>`).join("");
    $("m-split").innerHTML = `<option value="">No split</option>${def.dimensions.map((d) => `<option value="${esc(d)}">Split by ${esc(d)}</option>`).join("")}`;
  };
  const draw = async () => {
    const def = defs.find((m) => m.name === $("m-name").value);
    $("m-table").innerHTML = loading("Loading metric…");
    const data = await exploreGet("metric-series", node, {
      metric: def.name, aggregation: $("m-agg").value, range: pick.range, split: $("m-split").value });
    drawChart(node, def, data);
  };
  $("m-name").addEventListener("change", () => { sync(); draw(); });
  $("m-agg").addEventListener("change", draw);
  $("m-split").addEventListener("change", draw);
  body.querySelector(".range").addEventListener("click", (event) => {
    if (!event.target.dataset.range) return;
    pick.range = event.target.dataset.range;
    body.querySelectorAll(".range button").forEach((b) => b.classList.toggle("active", b.dataset.range === pick.range));
    draw();
  });
  sync();
  await draw();
}

function thresholdsFor(node, metric) {
  const out = [];
  for (const sig of node.signals) {
    if (!sig.name.startsWith(`${metric} (`) || !sig.threshold) continue;
    for (const m of sig.threshold.matchAll(/(warn|crit)?\s*[<>≤≥]\s*([\d.]+)/g)) {
      out.push({ label: m[1] === "warn" ? `warning ${m[2]}` : `critical ${m[2]}`, value: Number(m[2]), warn: m[1] === "warn" });
    }
  }
  return out;
}

function drawChart(node, def, data) {
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  state.chartArgs = [node, def, data];  // redrawn as is when the theme changes
  const series = data.series || [];
  const hasData = series.some((s) => s.points.some((p) => p[1] != null));
  if (data.error || !hasData) {
    $("m-table").innerHTML = `<p class="muted">${esc(data.error || "No data in this range.")}</p>`;
    return;
  }
  const longRange = series[0].points.length && (Date.parse(series[0].points.at(-1)[0]) - Date.parse(series[0].points[0][0])) > 864e5;
  const labels = series[0].points.map((p) => new Date(p[0]).toLocaleString([], longRange
    ? { month: "short", day: "numeric", hour: "2-digit" } : { hour: "2-digit", minute: "2-digit" }));
  const datasets = series.map((s, i) => ({
    label: s.label, data: s.points.map((p) => p[1]), borderColor: cssVar(`--series-${(i % 8) + 1}`),
    backgroundColor: cssVar(`--series-${(i % 8) + 1}`), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
    spanGaps: true, tension: 0,
  }));
  for (const t of thresholdsFor(node, def.name)) {
    datasets.push({ label: t.label, data: labels.map(() => t.value), borderColor: cssVar(t.warn ? "--warning" : "--critical"),
      borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 0 });
  }
  state.chart = new Chart($("m-chart"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: datasets.length > 1, labels: { color: cssVar("--text"), boxWidth: 12, boxHeight: 2 } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.y)} ${data.unit || ""}` } },
      },
      scales: {
        x: { ticks: { color: cssVar("--muted"), maxTicksLimit: 8 }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { color: cssVar("--muted") }, grid: { color: cssVar("--border") },
          title: { display: !!data.unit, text: data.unit, color: cssVar("--muted") } },
      },
    },
  });
  const stats = series.map((s) => {
    const values = s.points.map((p) => p[1]).filter((v) => v != null);
    const last = values.at(-1);
    const avg = values.reduce((a, b) => a + b, 0) / (values.length || 1);
    return [esc(s.label), fmt(last), fmt(Math.max(...values)), fmt(avg)];
  });
  $("m-table").innerHTML = table(["Series", "Latest", "Max", "Average"], stats)
    + (data.truncated ? '<p class="muted">Showing the top 8 series.</p>' : "");
}

// ---------------------------------------------------------------- workloads

function podLogsButton(namespace, pod, container) {
  const payload = esc(JSON.stringify({ namespace, pod: pod || "", container: container || "" }));
  return `<button type="button" class="link" data-logs="${payload}">logs</button>`;
}

// The lists a Kubernetes read couldn't get, and what that leaves out, so a gap never reads as "healthy".
// ------------------------------------------------------------------ nodes (AKS)
// Every node with its pool, readiness and pressure, what's placed on it against what it holds, and its versions;
// the pools in total above. From the Kubernetes API, or Container Insights' inventory when the API can't be reached.

const cores = (m) => `${(Number(m || 0) / 1000).toFixed(1)}`;
const gib = (mi) => `${(Number(mi || 0) / 1024).toFixed(1)}`;

function shareCell(x, unit) {
  if (!x || x.requested_pct == null) return '<span class="muted">–</span>';
  const tone = (pct) => (pct >= 90 ? "critical" : pct >= 75 ? "warning" : "");
  const amount = unit === "cores" ? cores : gib;
  return `<span class="${tone(x.requested_pct)}">${x.requested_pct}% requested</span>${x.used_pct != null ? `<br><span class="muted ${tone(x.used_pct)}">${x.used_pct}% used</span>` : ""}
    <div class="muted">${amount(x.requested)} of ${amount(x.allocatable)} ${unit}</div>`;
}

async function tabNodes(node, body) {
  const data = await exploreGet("nodes", node);
  if (!data.available) {
    body.innerHTML = `<div class="notice partial"><strong>The nodes couldn't be read.</strong> ${esc(data.reason || "")}
        ${data.fallback ? `<div class="muted">${esc(data.fallback)}</div>` : ""}</div>${pimSlot(node.id, "Azure Kubernetes Service RBAC Reader")}`;
    fillPimHints(body);
    return;
  }
  const rows = data.nodes;
  const fromInsights = data.source === "container_insights";
  const count = (f) => rows.filter(f).length;
  const notReady = count((r) => !r.ready), cordoned = count((r) => r.unschedulable), problems = count((r) => r.problems.length);
  const pools = data.pools || [];
  body.innerHTML = `
    ${fromInsights ? `<div class="notice partial">The Kubernetes API couldn't be reached (${esc(data.reason || "")}), so this is Container Insights'
      node inventory from the last 30 minutes: readiness, pool and versions, without capacity, conditions or taints.</div>` : ""}
    <p><strong>${rows.length} node(s)</strong>${pools.length ? ` in ${pools.length} pool(s)` : ""} ·
      ${notReady ? `${pill("critical")} ${notReady} not ready` : '<span class="ok">✓ all ready</span>'}
      ${cordoned ? ` · ${pill("warning")} ${cordoned} cordoned` : ""} ${problems ? ` · ${problems} with something to look at` : ""}
      ${data.via ? `<br><span class="muted">via ${esc(data.via)} · pool scaling settings are on the Scaling tab</span>` : ""}</p>
    ${unreadableHtml(data.unreadable, node.id)}
    ${pools.length ? `<h4>Pools</h4>${table(["Pool", "Nodes", "Not ready", "CPU (cores) allocatable / requested / used", "Memory (GiB) allocatable / requested / used"],
      pools.map((p) => [esc(p.pool), fmt(p.nodes), p.not_ready ? `${pill("critical")} ${fmt(p.not_ready)}` : "0",
        `${cores(p.alloc_cpu)} / ${cores(p.req_cpu)} / ${p.used_cpu ? cores(p.used_cpu) : "–"}`,
        `${gib(p.alloc_mem)} / ${gib(p.req_mem)} / ${p.used_mem ? gib(p.used_mem) : "–"}`]))}` : ""}
    <h4>Nodes</h4>
    <div class="table-tools"><input id="nodes-filter" type="search" placeholder="Filter by node, pool, VM size, zone or version">
      <label class="inline"><input type="checkbox" id="nodes-problems"> only nodes with something to look at</label></div>
    <div id="nodes-table"></div>`;
  const render = () => {
    const words = $("nodes-filter").value.toLowerCase().split(/\s+/).filter(Boolean);
    const only = $("nodes-problems").checked;
    const shown = rows.filter((r) => (!only || r.problems.length)
      && words.every((w) => `${r.name} ${r.pool} ${r.vm_size} ${r.zone} ${r.kubelet} ${r.node_image || ""}`.toLowerCase().includes(w)));
    $("nodes-table").innerHTML = shown.length ? `<table class="data-table nodes-table"><thead><tr><th>Node</th><th>State</th>
        ${fromInsights ? "" : "<th>CPU</th><th>Memory</th><th>Pods</th>"}<th>Versions</th><th>Age</th></tr></thead><tbody>${shown.map((r, i) => `
      <tr class="clickable" data-node-row="${i}"><td>${pill(r.status)} <strong>${esc(r.name)}</strong>
          <div class="muted">${esc([r.pool, r.mode, r.vm_size, r.zone].filter(Boolean).join(" · "))}</div></td>
        <td>${r.ready ? "Ready" : '<span class="critical">NotReady</span>'}${r.problems.filter((x) => !x.startsWith("NotReady")).map((x) => `<div class="warning">${esc(x)}</div>`).join("")}
          ${r.taints && r.taints.length ? `<div class="muted">${r.taints.length} taint(s)</div>` : ""}</td>
        ${fromInsights ? "" : `<td>${shareCell(r.cpu, "cores")}</td><td>${shareCell(r.memory, "GiB")}</td>
          <td>${r.pods ? `${fmt(r.pods.placed)}${r.pods.max ? ` of ${fmt(r.pods.max)}` : ""}` : "–"}</td>`}
        <td>kubelet ${esc(r.kubelet || "?")}${r.node_image ? `<div class="muted">${esc(r.node_image)}${r.node_image_age != null ? ` (${r.node_image_age} days)` : ""}</div>` : ""}</td>
        <td class="nowrap">${r.age_days != null ? `${r.age_days} days` : r.last_seen ? `seen ${esc(new Date(r.last_seen).toLocaleTimeString())}` : "–"}</td></tr>
      <tr class="hidden node-detail" id="node-detail-${i}"><td colspan="${fromInsights ? 4 : 7}">${nodeDetailHtml(r)}</td></tr>`).join("")}</tbody></table>`
      : '<p class="muted">No node matches.</p>';
    $("nodes-table").onclick = (event) => {
      const tr = event.target.closest("[data-node-row]");
      if (tr) $(`node-detail-${tr.dataset.nodeRow}`).classList.toggle("hidden");
    };
  };
  $("nodes-filter").addEventListener("input", render);
  $("nodes-problems").addEventListener("change", render);
  render();
  fillPimHints(body);
}

function nodeDetailHtml(r) {
  const facts = [["OS image", r.os_image], ["Kernel", r.kernel], ["Container runtime", r.runtime], ["Node image", r.node_image]]
    .filter(([, v]) => v);
  return `<div class="node-facts">${facts.map(([k, v]) => `<div><span class="muted">${esc(k)}</span> ${esc(v)}</div>`).join("")}</div>
    ${r.taints && r.taints.length ? `<div><span class="muted">Taints</span> ${r.taints.map((t) => `<code>${esc(t)}</code>`).join(" ")}</div>` : ""}
    ${r.conditions && r.conditions.length ? table(["Condition", "Status", "Reason", "Message"], r.conditions.map((c) => [
      esc(c.type), (c.type === "Ready" ? c.status === "True" : c.status !== "True") ? '<span class="ok">' + esc(c.status) + "</span>" : `<span class="critical">${esc(c.status)}</span>`,
      esc(c.reason), esc(c.message)])) : ""}`;
}

// The Kubernetes lists a read couldn't get and what that leaves out, so a gap never reads as "healthy" (Workloads, Nodes).
function unreadableHtml(unreadable, resourceId) {
  if (!unreadable || !unreadable.length) return "";
  return `<div class="notice partial"><strong>Not everything was readable, so some checks didn't run</strong>
        (missing here doesn't mean healthy):<ul>${unreadable.map((u) => `<li><code>${esc(u.list)}</code>: ${esc(u.effect)}.
          <span class="muted">${esc(u.reason)}</span></li>`).join("")}</ul>${esc(PIM_TIP)}${pimSlot(resourceId, "Azure Kubernetes Service RBAC Reader")}</div>`;
}

async function tabWorkloads(node, body, includeSystem = false) {
  body.innerHTML = loading("Reading pods from platform metrics, Container Insights and the Kubernetes API…");
  const data = await exploreGet("workloads", node, includeSystem ? { system: "1" } : {});
  const phases = Array.isArray(data.pod_phases) ? data.pod_phases : [];
  const problem = phases.filter((r) => r.phase !== "Running");
  const running = phases.filter((r) => r.phase === "Running").reduce((a, r) => a + r.pods, 0);
  const ci = data.container_insights || {};
  const k = data.kubernetes || {};

  let html = `<section class="layer"><h4>Pods (platform metrics, every cluster)</h4>
    <p>${running} running${problem.length ? "" : ", none pending or failed"}.</p>
    ${problem.length ? table(["Namespace", "Phase", "Pods"], problem.map((r) => [esc(r.namespace), pill(r.phase === "Failed" ? "critical" : "warning") + " " + esc(r.phase), fmt(r.pods)])) : ""}
    ${data.pod_phases && data.pod_phases.error ? `<p class="muted">${esc(data.pod_phases.error)}</p>` : ""}</section>`;

  html += `<section class="layer"><h4>Container Insights</h4>`;
  if (!ci.available) {
    html += noticeHtml(ci.reason || "Not available.");
  } else {
    const rows = (name) => (Array.isArray(ci[name]) ? ci[name] : []);
    html += `<h5>Pods not running <span class="muted">last 15 min</span></h5>${table(["Namespace", "Pod", "Status", "Reason", ""], rows("not_running").map((r) => [
        esc(r.Namespace), esc(r.Pod), esc(r.Status), esc(r.Reason), podLogsButton(r.Namespace, r.Pod)]))}
      <h5>Containers near their limits (last 15 min)</h5>${table(["Namespace", "Pod", "Container", "CPU % of limit", "Memory % of limit", ""], rows("hot_containers").map((r) => [
        esc(r.Namespace), esc(r.Pod), esc(r.Container), fmt(r.CpuPctOfLimit), fmt(r.MemPctOfLimit), podLogsButton(r.Namespace, r.Pod, r.Container)]))}
      <h5>Restarts, last hour</h5>${table(["Namespace", "Pod", "Container", "Restarts", ""], rows("restarts").map((r) => [
        esc(r.Namespace), esc(r.Pod), esc(r.Container), fmt(r.Restarts), podLogsButton(r.Namespace, r.Pod, r.Container)]))}
      <h5>OOMKilled, last 24 h</h5>${table(["Namespace", "Container", "Pods", "Last"], rows("oomkilled").map((r) => [
        esc(r.Namespace), esc(r.Container), fmt(r.Pods), esc(r.Last)]))}
      <h5>Warning events, last hour</h5>${table(["Reason", "Namespace", "Object", "Count", "Message"], rows("events").map((r) => [
        esc(r.Reason), esc(r.Namespace), esc(r.Object), fmt(r.Count), esc((r.Message || "").slice(0, 160))]))}`;
  }
  html += "</section>";

  html += `<section class="layer"><h4>Workload checks (Kubernetes API)</h4>`;
  if (!k.available) {
    html += noticeHtml(k.reason || "Not available.");
    html += `<p class="muted">${ci.available ? "Without the Kubernetes API, the pods that aren't running are listed from Container Insights above."
      : "Neither the Kubernetes API nor Container Insights can be read for this cluster, so its pods are only counted (platform metrics, top)."}</p>`;
  } else {
    const now = k.counts_by_kind.now;
    const config = k.counts_by_kind.config;
    const findingsTable = (rows) => table(["Severity", "Namespace", "Workload", "Container", "Problem", "What to do"], rows
      .filter((f) => f.severity !== "INFO").slice(0, 200).map((f) => [
        pill(SEVERITY_STATUS[f.severity]) + ` <span class="muted">${esc(f.severity)}</span>`, esc(f.namespace), esc(f.workload),
        esc(f.container), `${esc(f.problem)}<br><span class="muted">${esc(f.why)}</span>`, esc(f.action)]));
    const unreadable = k.unreadable || [];
    html += `<p><strong>Happening now:</strong> ${pill("critical")} ${now.HIGH} · ${pill("warning")} ${now.MEDIUM}
        · <strong>Configuration risks:</strong> ${pill("warning")} ${config.MEDIUM} · ${pill("info")} ${config.LOW}
        ${k.usage_available ? "" : ' · <span class="muted">no live usage (metrics-server unavailable)</span>'}
        <br><span class="muted">via ${esc(k.source || "")}</span>
        <label class="inline"><input type="checkbox" id="k-system" ${includeSystem ? "checked" : ""}> include system namespaces</label></p>
      ${unreadableHtml(unreadable, node.id)}
      <h5>Pods not running</h5>
      <p class="muted">Failed, Pending or Unknown pods right now, with the reason Kubernetes gives (Evicted, Error,
        ImagePullBackOff…). Failed pods stay listed until deleted, so old ones can show here.</p>
      ${(k.not_running || []).length ? table(["Namespace", "Pod", "Phase", "Reason", "Owner", "Since", ""], k.not_running.map((r) => [
        esc(r.namespace), esc(r.pod), pill(r.phase === "Failed" ? "critical" : "warning") + " " + esc(r.phase),
        `${esc(r.reason || "–")}${r.message ? `<div class="muted ai-msg">${esc(r.message)}</div>` : ""}`, esc(r.owner),
        r.since ? seen(r.since) : "–", podLogsButton(r.namespace, r.pod)]), "k-not-running")
        : '<p class="ok">✓ Every pod is running or completed.</p>'}
      <h5>Happening now</h5>
      <p class="muted">Problems at this moment: pods pending or not ready, HPAs at their maximum or unable to scale,
        containers near their limits, OOMKilled, nodes NotReady.</p>
      ${findingsTable(k.findings.filter((f) => f.kind === "now"))}
      <h5>HPAs</h5>${table(["Namespace", "Workload", "Min", "Max", "Current", "Targets (now)"], k.hpas.map((h) => [
        esc(h.namespace), esc(h.workload), fmt(h.min), fmt(h.max),
        `${fmt(h.current)}${h.current >= h.max ? " " + pill("critical") : ""}`, esc(h.targets)]))}
      <h5>Node pools</h5>${table(["Pool", "Nodes", "Not ready", "CPU alloc / req / used (m)", "Memory alloc / req / used (Mi)"], k.pools.map((p) => [
        esc(p.pool), fmt(p.nodes), fmt(p.not_ready), `${fmt(p.alloc_cpu)} / ${fmt(p.req_cpu)} / ${fmt(p.used_cpu)}`,
        `${fmt(p.alloc_mem)} / ${fmt(p.req_mem)} / ${fmt(p.used_mem)}`]))}
      <h5>Configuration risks</h5>
      <p class="muted">How workloads are set up: single replicas, missing requests or limits, HPA and PodDisruptionBudget
        settings. Worth fixing before a peak, but common on healthy clusters, so yellow at most.</p>
      ${findingsTable(k.findings.filter((f) => f.kind === "config"))}`;
  }
  html += `</section><section class="layer hidden" id="logs-box"></section>`;
  body.innerHTML = html;

  const toggle = $("k-system");
  if (toggle) toggle.addEventListener("change", () => tabWorkloads(node, body, toggle.checked));
  body.onclick = async (event) => {
    const raw = event.target.dataset && event.target.dataset.logs;
    if (!raw) return;
    const target = JSON.parse(raw);
    const box = $("logs-box");
    box.classList.remove("hidden");
    box.innerHTML = `<h4>Logs: ${esc(target.namespace)}/${esc(target.pod || "*")}${target.container ? "/" + esc(target.container) : ""}</h4>${loading("Loading logs…")}`;
    try {
      const logs = await api("api/explore/logs", { params: { id: node.id, ...target, lines: 200 } });
      box.innerHTML = `<h4>Logs: ${esc(target.namespace)}/${esc(target.pod || "*")} <span class="muted">via ${esc(logs.source || "")}</span></h4>
        ${logs.error ? noticeHtml(logs.error) : ""}<pre class="logs">${esc((logs.lines || []).join("\n"))}</pre>`;
    } catch (error) {
      box.innerHTML = errorHtml(error);
    }
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };
}

async function exportSnapshot(fmt) {
  try {
    await download(`api/snapshots/${state.jobId}/export`, { format: fmt }, `snapshot.${fmt}`);
  } catch (error) {
    showMessage(error.message, true);
  }
}

// Saves a server-made file (Markdown, CSV, PDF) under the name the server chose.
async function download(path, params, fallback) {
  const response = await api(path, { params, raw: true });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `Export failed (${response.status}).`);
  }
  const blob = await response.blob();
  const name = (response.headers.get("Content-Disposition") || "").match(/filename="?([^";]+)"?/);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name ? name[1] : fallback;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// ------------------------------------------------------------------- wiring

const reloadGroups = debounce(() => {
  if (state.groupsFor === subsKey(state.scope.subs)) return;  // Apply already loaded them
  loadGroups().then(draftChanged).catch((e) => showMessage(e.message, true));
}, 400);

// scope bar: dropdown panels
function togglePanel(id) {
  for (const other of ["subs-panel", "rgs-panel", "saved-panel"]) if (other !== id) $(other).classList.add("hidden");
  const panel = $(id);
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden")) {
    if (id === "rgs-panel") renderGroupPicks(); else if (id === "saved-panel") renderSavedScopes(); else renderSubscriptions();
    const search = panel.querySelector("input[type=search]");
    if (search) search.focus();
  }
}
$("subs-button").addEventListener("click", () => togglePanel("subs-panel"));
$("rgs-button").addEventListener("click", () => togglePanel("rgs-panel"));
document.addEventListener("click", (event) => {
  if (!event.target.closest(".dropdown")) for (const id of ["subs-panel", "rgs-panel", "saved-panel", "deps-panel", "export-panel"]) $(id).classList.add("hidden");
});

// Toolbar menus: Dependencies (its options) and Export (formats and the link). A pick closes the menu.
for (const [button, panel] of [["deps-menu", "deps-panel"], ["export-menu", "export-panel"]]) {
  $(button).addEventListener("click", () => {
    for (const other of ["deps-panel", "export-panel"]) if (other !== panel) $(other).classList.add("hidden");
    $(panel).classList.toggle("hidden");
  });
}
for (const id of ["export-md", "export-pdf", "print", "copy-link", "deps"]) {
  $(id).addEventListener("click", () => { $("export-panel").classList.add("hidden"); $("deps-panel").classList.add("hidden"); });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") for (const id of ["subs-panel", "rgs-panel"]) $(id).classList.add("hidden");
});

$("subs").addEventListener("change", (event) => {
  const id = event.target.dataset.id;
  if (event.target.checked) state.scope.subs.add(id); else state.scope.subs.delete(id);
  renderScopeBar();
  reloadGroups();
});
$("subs-all").addEventListener("click", () => {
  for (const s of shownSubscriptions()) state.scope.subs.add(s.id);
  renderSubscriptions();
  reloadGroups();
});
$("subs-none").addEventListener("click", () => {
  for (const s of shownSubscriptions()) state.scope.subs.delete(s.id);
  renderSubscriptions();
  reloadGroups();
});
$("sub-filter").addEventListener("input", renderSubscriptions);
$("graph").addEventListener("change", (event) => {
  if (event.target.id !== "graph-all" || !state.snapshot) return;
  state.graphAll = event.target.checked;
  drawGraph(state.snapshot);
});

$("rg-picks").addEventListener("change", (event) => {
  const key = event.target.dataset.key;
  const tagged = taggedGroups().map(groupKey);
  if (!state.scope.groups.size) state.scope.groups = new Set(tagged);  // unticking from "all": every other group stays
  if (event.target.checked) state.scope.groups.add(key); else state.scope.groups.delete(key);
  if (!state.scope.groups.size) {  // an empty pick list means "all", the opposite of unticking the last one
    state.scope.groups.add(key);
    flash("At least one resource group stays in the scope; to look at none, clear the subscriptions instead.", true, 4000);
  } else if (tagged.every((k) => state.scope.groups.has(k))) state.scope.groups.clear();  // everything ticked is "all" again
  renderGroupPicks();
  draftChanged();
});
$("rgs-all").addEventListener("click", () => {
  state.scope.groups.clear();
  renderGroupPicks();
  draftChanged();
});
$("rgs-shown").addEventListener("click", () => {
  const filter = $("rg-pick-filter").value.toLowerCase();
  state.scope.groups = new Set(taggedGroups().filter((g) => !filter || g.name.toLowerCase().includes(filter)).map(groupKey));
  renderGroupPicks();
  draftChanged();
});
$("rg-pick-filter").addEventListener("input", renderGroupPicks);

$("scope-tags").addEventListener("change", (event) => {
  if (event.target.id === "tag-key") {
    state.pendingTagKey = event.target.value;
    renderTagFilters();
    return;
  }
  if (event.target.id === "tag-value" && event.target.value) {
    state.scope.tags[state.pendingTagKey] = event.target.value;
    state.pendingTagKey = "";
    draftChanged();
  }
});
$("scope-tags").addEventListener("click", (event) => {
  if (event.target.id === "scope-clear") {
    state.scope.tags = {};
    state.scope.groups = new Set();
    state.pendingTagKey = "";
    renderGroupPicks();
    draftChanged();
    return;
  }
  const key = event.target.dataset.remove;
  if (!key) return;
  delete state.scope.tags[key];
  draftChanged();
});

// browse: resource groups in scope, then the resources of the one you open
$("rg-filter").addEventListener("input", () => { renderGroups(); findInTree(); });
$("rgs").addEventListener("keydown", (event) => {
  const head = (event.key === "Enter" || event.key === " ") && event.target.closest("[data-type-toggle]");
  if (head) { event.preventDefault(); foldType(head.dataset.foldKey, head.dataset.typeToggle); }
});
$("rgs").addEventListener("click", (event) => {
  if (event.target.closest("[data-watch]")) return;
  const more = event.target.closest("[data-more]");
  if (more) { showMore(more); renderGroups(); return; }
  const page = event.target.closest("[data-sub-summary]");
  if (page) { showSubscriptionSummary(page.dataset.subSummary); return; }
  const sub = event.target.closest("[data-sub-toggle]");
  if (sub) {
    state.subOpen[sub.dataset.subToggle] = sub.getAttribute("aria-expanded") !== "true";
    renderGroups();
    return;
  }
  const res = event.target.closest(".item.res");
  if (res) { startSnapshot(res.dataset.id); return; }
  const typeHead = event.target.closest("[data-type-toggle]");
  if (typeHead) { foldType(typeHead.dataset.foldKey, typeHead.dataset.typeToggle); return; }
  const typeAll = event.target.closest("[data-type-all]");
  if (typeAll) { foldType(typeAll.dataset.foldKey, null, typeAll.dataset.typeAll); return; }
  const typeSort = event.target.closest("[data-type-sort]");
  if (typeSort) { state.typeSort = typeSort.dataset.typeSort; stored.set("browse.typeSort", state.typeSort); renderGroups(); return; }
  const fold = event.target.closest("[data-find-fold]");
  if (fold && state.treeFind && state.treeFind.byGroup) {
    state.findClosed = fold.dataset.findFold === "close"
      ? new Set(appliedGroups().filter((g) => state.treeFind.byGroup.has(groupKey(g).toLowerCase())).map(groupKey)) : new Set();
    renderGroups();
    return;
  }
  const item = event.target.closest(".item.grp");
  if (!item) return;
  const key = item.dataset.key;
  const found = state.treeFind && state.treeFind.byGroup && $("rg-filter").value.trim() ? state.treeFind.byGroup.get(key.toLowerCase()) : null;
  if (found && event.target.closest("[data-toggle]")) {  // a filter match: the arrow folds its matches, like a subscription
    if (state.findClosed.has(key)) state.findClosed.delete(key); else state.findClosed.add(key);
    renderGroups();
    return;
  }
  if (event.target.closest("[data-toggle]") && state.browseGroup === key) {  // the arrow closes an open group
    state.browseGroup = null;
    state.resources = [];
    renderGroups();
    return;
  }
  openGroup(key);
});

// Each group's last finished check, so going back to the group shows it again until that group is checked anew.
const GROUP_CHECKS_KEPT = 10;
state.groupChecks = new Map();

function keepGroupCheck(target, snapshot) {
  const key = target.group.toLowerCase();
  state.groupChecks.delete(key);
  state.groupChecks.set(key, { target: { ...target }, snapshot });
  while (state.groupChecks.size > GROUP_CHECKS_KEPT) state.groupChecks.delete(state.groupChecks.keys().next().value);
}

function showGroupCheck(key) {
  const kept = state.groupChecks.get(key.toLowerCase());
  hideGroupSummary();
  state.target = kept.target;
  state.snapshot = kept.snapshot;
  state.selectedResource = null;
  state.exploreNode = null;
  state.partialDrawn = false;
  renderPlace(state.target);
  renderSnapshot();
  for (const id of ["export-md", "export-pdf", "print", "watch", "copy-link", "export-menu"]) $(id).disabled = false;
  $("rerun").disabled = false;
  updateBrowseControls();
  refreshWatchButtons();
}

// A group picked on the left (or from a Cost card): its last check if it has one, else its summary; and its
// resources listed under it.
function openGroup(key) {
  if (state.groupChecks.has(key.toLowerCase()) && !state.busy) showGroupCheck(key);
  else showGroupSummary(key);
  if (state.browseGroup !== key) {
    state.browseGroup = key;
    loadResources().catch((e) => showMessage(e.message, true));
  } else renderGroups();
}

const GS_TYPES = 8;  // resource types the group summary shows as tiles before "other"

// A group picked on the left: what's in it, and Check this group. A check, or opening a resource, replaces it.
function showGroupSummary(key) {
  state.groupSummary = key;
  document.querySelector("#view-browse main").classList.add("showing-group");
  renderGroupSummary();
}

function hideGroupSummary() {
  state.groupSummary = null;
  document.querySelector("#view-browse main").classList.remove("showing-group");
  $("group-summary").classList.add("hidden");
}

function renderGroupSummary() {
  const key = state.groupSummary;
  const group = appliedGroups().find((g) => groupKey(g) === key);
  if (!group) { hideGroupSummary(); return; }
  const loaded = state.browseGroup === key && !state.resourcesLoading;
  // The group's workloads first, by role (compute, hosting, data…: the diagram's bands), then by count; plumbing
  // (network interfaces, private endpoints, system topics…) in one tile at the end.
  const byType = {};
  if (loaded) for (const r of state.resources) {
    const t = byType[r.display_type] || (byType[r.display_type] = { n: 0, rank: r.tier_rank ?? 99, plumbing: !!r.plumbing });
    t.n += 1;
  }
  const main = Object.entries(byType).filter(([, t]) => !t.plumbing)
    .sort((a, b) => a[1].rank - b[1].rank || b[1].n - a[1].n || byName(a[0], b[0])).map(([name, t]) => [name, t.n]);
  const plumbing = Object.entries(byType).filter(([, t]) => t.plumbing).sort((a, b) => b[1].n - a[1].n);
  const shown = main.slice(0, GS_TYPES);
  const others = main.slice(GS_TYPES);
  // Each tile opens just its types in the tree on the left.
  const tile = (n, label, types, title = "", extra = "") => `<button type="button" class="gs-type${extra}" data-show-types="${esc(JSON.stringify(types))}"
    title="${esc(title ? `${title}. Show them on the left` : "Show them on the left")}"><span class="n">${n}</span><span class="muted">${esc(label)}</span></button>`;
  const listed = (rows) => rows.map(([name, n]) => `${n} ${name}`).join(", ");
  const tiles = shown.map(([t, n]) => tile(n, t, [t])).join("")
    + (others.length ? tile(others.reduce((s, [, n]) => s + n, 0), "other", others.map(([t]) => t), listed(others)) : "")
    + (plumbing.length ? tile(plumbing.reduce((s, [, t]) => s + t.n, 0), "supporting (network, endpoints, topics…)",
      plumbing.map(([t]) => t), listed(plumbing.map(([name, t]) => [name, t.n])), " gs-plumbing") : "");
  const kept = state.groupChecks.get(key.toLowerCase());
  const last = kept ? kept.snapshot : state.snapshot && state.snapshot.kind === "group"
    && sameId(state.snapshot.root_id, `/subscriptions/${key.split("/")[0]}/resourcegroups/${group.name}`) ? state.snapshot : null;
  $("group-summary").classList.remove("hidden");
  $("group-summary").innerHTML = `<div class="crumbs"><button type="button" class="link" data-sub-summary="${esc(group.subscription_id)}">${esc(subName(group.subscription_id))}</button> › <strong>${esc(group.name)}</strong></div>
    <div class="gs-head"><h2>${esc(group.name)}</h2>
      <span class="muted">Resource group · ${esc(subName(group.subscription_id))}${loaded ? ` · ${state.resources.length} resources` : ""}${last ? ` · last checked ${escTimes(last.taken_at)}` : " · not checked yet"}</span>
      <span class="spacer"></span>
      ${kept ? `<button type="button" class="primary" data-show-check="${esc(key)}" title="Back to the check taken ${esc(kept.snapshot.taken_at)}, without checking again">Show last check</button>` : ""}
      <button type="button" ${kept ? "" : 'class="primary"'} data-check-group="${esc(group.subscription_id)}/${esc(group.name)}">${kept ? "Check again" : "Check this group"}</button>
      ${watchButton(groupWatchItem(group.subscription_id, group.name))}</div>
    ${loaded ? `<div class="gs-types">${tiles}</div>` : loading("Loading resources…")}
    <div class="gs-cards">${costSlot(`/subscriptions/${group.subscription_id}/resourceGroups/${group.name}`)}${accessHereSlot(`/subscriptions/${group.subscription_id}/resourceGroups/${group.name}`)}
    <div class="card gs-explain"><h3>What "Check this group" does</h3>
      <p class="muted">Reads every resource's health, then lists them worst first, with what changed against last week above
        the table. Pick a resource on the left to look at just that one instead.</p></div></div>`;
  fillAccessHere($("group-summary"));
  fillCost($("group-summary"));
}

// A subscription, from the crumb above a group: its groups in scope, and your access there.
function showSubscriptionSummary(subId) {
  state.groupSummary = null;
  document.querySelector("#view-browse main").classList.add("showing-group");
  const groups = appliedGroups().filter((g) => g.subscription_id === subId);
  $("group-summary").classList.remove("hidden");
  $("group-summary").innerHTML = `<div class="crumbs"><strong>${esc(subName(subId))}</strong></div>
    <div class="gs-head"><h2>${esc(subName(subId))}</h2>
      <span class="muted">Subscription · ${esc(subId)} · ${groups.length} resource group(s) in scope</span></div>
    <div class="gs-cards">${costSlot(`/subscriptions/${subId}`)}${accessHereSlot(`/subscriptions/${subId}`)}
      <div class="card gs-explain"><h3>Its resource groups</h3><p class="muted">Pick one on the left to see what's in it and check it.</p></div></div>`;
  fillAccessHere($("group-summary"));
  fillCost($("group-summary"));
}

$("group-summary").addEventListener("click", (event) => {
  if (costCardClick(event)) return;
  const open = event.target.closest("[data-open]");
  if (open) { startSnapshot(JSON.parse(open.dataset.open)); return; }
  const sub = event.target.closest("[data-sub-summary]");
  if (sub) { showSubscriptionSummary(sub.dataset.subSummary); return; }
  const types = event.target.closest("[data-show-types]");
  if (types && state.groupSummary) { showTypesInTree(`res:${state.groupSummary}`, JSON.parse(types.dataset.showTypes)); return; }
  const back = event.target.closest("[data-show-check]");
  if (back && state.groupChecks.has(back.dataset.showCheck.toLowerCase())) { showGroupCheck(back.dataset.showCheck); return; }
  const b = event.target.closest("[data-check-group]");
  if (!b) return;
  const group = b.dataset.checkGroup;
  startSnapshot({ kind: "group", group, name: group.split("/").slice(1).join("/") });
});
$("rerun").addEventListener("click", () => state.target && freshly(() => startSnapshot(state.target)));
$("deps").addEventListener("click", () => {
  const t = state.target;
  if (!t || t.kind === "group") return;
  startSnapshot(t.depth > 0 ? { ...t, depth: 0, inbound: false }
    : { ...t, depth: Number($("depth").value), inbound: $("inbound").checked });
});
$("group-view").addEventListener("click", (event) => {
  const view = event.target.dataset && event.target.dataset.groupview;
  if (!view || view === state.groupView) return;
  state.groupView = view;
  state.partialDrawn = false;
  if (state.snapshot && state.snapshot.kind === "group") {
    if (view === "table") renderGroupTable(state.snapshot);
    else { $("graph").classList.remove("as-table"); drawGraph(state.snapshot); state.partialDrawn = !state.snapshot.done; }
  }
  updateBrowseControls();
});
$("export-md").addEventListener("click", () => exportSnapshot("md"));
$("export-pdf").addEventListener("click", () => exportSnapshot("pdf"));
$("print").addEventListener("click", () => {
  const url = new URL(`${BASE}api/snapshots/${state.jobId}/print`);
  if (state.tenant) url.searchParams.set("tenant", state.tenant);
  window.open(url, "_blank", "noopener");
});
$("tenant").addEventListener("change", async (event) => {
  state.tenant = event.target.value;
  state.tenantPinned = true;
  state.browseGroup = null;
  state.selectedResource = null;
  state.triageLoaded = false;
  state.watchResults = {};
  state.alerts = null;
  state.alertEdit = null;
  state.snLoaded = false;
  state.monLoaded = false;
  state.sn = null;
  state.mon = null;
  try {
    await loadContextForTenant();
    await refreshIntegrationTabs();  // each tenant has its own ServiceNow and monitoring tools
    if (state.view === "settings") loadIntegrations();
    if (state.view === "servicenow") loadServiceNow();
    if ((state.view || "").startsWith("tool-")) loadMonitors();
    state.watchItems = await api("api/watchlist");
    refreshWatchButtons();
    if (state.view === "watchlist") renderWatchlist();
    pollAlerts();
    if (state.view === "triage") await loadTriage();
    updateUrl();
  } catch (error) { showMessage(error.message, true); }
});
$("clear-cache").addEventListener("click", async () => {
  if (!window.confirm("Forget the costs kept on this machine and every answer kept in memory? The next cost page reads Azure again (a big scope takes minutes).")) return;
  try {
    await api("api/cache/clear", { method: "POST", body: {} });
    flash("Cached data cleared.");
  } catch (error) { showMessage(error.message, true); }
});
$("refresh").addEventListener("click", async () => {
  try {
    await api("api/refresh", { method: "POST", body: {} });
    await loadContextForTenant();
    if (state.view === "triage") await loadTriage();
    else if (state.target) await startSnapshot(state.target);
  } catch (error) { showMessage(error.message, true); }
});

async function loadContextForTenant() {
  const context = await api("api/context");
  applyContext(context);
  await loadSubscriptions();
}

// ------------------------------------------------------------------ context & scope

// Saved scopes, Open with and the old favourites still to move, from /api/context and every scope save.
function applyScopePayload(payload) {
  state.savedScopes = payload.saved_scopes || [];
  state.openWith = payload.open_with || "none";
  state.reservedScopeNames = payload.reserved_names || [];
  state.legacyScope = payload.legacy_scope || null;
  if (state.alerts) {  // alert rules pick from the same scopes: a new one is offered now, not at the next alert poll
    state.alerts.saved_scopes = state.savedScopes.map((x) => x.name);
    state.alerts.scope_connections = Object.fromEntries(state.savedScopes.map((x) => [x.name, x.connections || {}]));
    if (state.view === "settings" && !state.alertEdit) renderAlertRules();
  }
}

function applyContext(context) {
  state.tenant = context.identity.tenant_id;
  state.tenantName = (context.tenants.find((t) => t.id === state.tenant) || {}).name || "";
  for (const a of document.querySelectorAll("a[data-portal]")) a.href = tenantPortal(a.dataset.portal);
  applyScopePayload(context);
  state.activeScope = null;
  state.configPath = context.config_path;
  state.watchlistPath = context.watchlist_path;
  state.version = context.version || "";
  if (!state.updateTimer) { checkUpdate(); state.updateTimer = setInterval(checkUpdate, UPDATE_CHECK_HOURS * 3600 * 1000); }
  $("about-version").textContent = context.version ? `version ${context.version}` : "";
  $("about-config").textContent = context.config_path || "";
  $("about-watchlist").textContent = context.watchlist_path || "";
  if (state.triageAutoOverride === undefined) state.triageAuto = context.triage_refresh_minutes || 0;
  state.summaryCfg = context.summary || {};
  $("identity").textContent = context.identity.name;
  $("account-who").textContent = context.identity.name;
  $("account-avatar").textContent = (context.identity.name || "?").trim().charAt(0).toUpperCase();
  $("account-tenant").textContent = state.tenantName ? `Tenant ${state.tenantName}` : "";
  $("tenant").innerHTML = context.tenants.map((t) =>
    `<option value="${esc(t.id)}" ${t.id === state.tenant ? "selected" : ""}>${esc(t.name)}</option>`).join("");
  if (state.signinAvailable) mergeCliTenants();
  $("depth").value = String(Math.min(4, Math.max(1, context.graph.depth || 1)));
  $("inbound").checked = !!context.graph.inbound;
  state.graphAuto = !!context.graph.auto;
}

function globMatch(patterns, value) {
  const lowered = (value || "").toLowerCase();
  return (patterns || []).some((pattern) => {
    const regex = new RegExp(`^${pattern.toLowerCase().replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`);
    return regex.test(lowered);
  });
}

// ------------------------------------------------------------------ views & links

function showView(view) {
  if (view === "vulnerabilities" || view === "exposure") {  // old links: both live under Security now
    state.secSub = view === "vulnerabilities" ? "vulnerabilities" : "config";
    view = "security";
  }
  const isTool = view.startsWith("tool-");
  if ((isTool && !toolKinds().includes(view.slice(5))) || (!isTool && !$(`view-${view}`))) view = "triage";  // e.g. an old ?view=checks link
  state.view = view;
  const section = view.startsWith("tool-") ? "monitors" : view;
  for (const name of ["triage", "investigate", "readiness", "browse", "secrets", "network", "servicenow", "monitors", "watchlist", "access", "settings", "security", "cost", "summary"]) {
    $(`view-${name}`).classList.toggle("hidden", name !== section);
  }
  document.querySelectorAll(".views button[data-view]").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
    b.setAttribute("aria-selected", b.dataset.view === view ? "true" : "false");
  });
  renderNavMenus();
  if (view === "access") loadAccess();
  if (view === "readiness") renderReadiness();
  if (view === "security") showSecSub(state.secSub);
  if (view === "cost") showCostSub(state.costSub);
  if (view === "summary" && state.summaryLoaded !== JSON.stringify(currentScope())) loadSummary();
  if (view === "triage") $("triage-auto").value = String(state.triageAuto || 0);
  if (view === "triage" && !state.triageLoaded) loadTriage();
  if (view === "secrets") { if (state.secretsLoaded !== JSON.stringify(currentScope())) loadSecrets(); else renderSecrets(); }
  if (view === "network" && !state.networkLoaded) loadNetwork();
  if (view === "servicenow" && !state.snLoaded) loadServiceNow();
  if (view.startsWith("tool-")) {
    state.toolKind = view.slice(5);
    if (state.monLoaded !== `${state.toolKind}|${state.selected[state.toolKind]}`) loadMonitors();
  }
  if (view === "investigate") loadInvestigate();
  if (view === "watchlist") loadWatchlist();
  if (view === "settings") loadSettings();
  navRecord(true);
  updateUrl();
}

// Back and Forward move through what was opened in this session (views, checks, tabs). The address stays the same,
// so a reload still opens Triage; going back to another resource checks it again.
let navRestoring = false;

function navEntry() {
  const browse = state.view === "browse";
  return { view: state.view, target: browse ? state.target || null : null,
    node: browse ? state.detailNode || null : null, tab: browse && state.detailNode ? state.detailTab : null };
}

function navRecord(push) {
  if (navRestoring) return;
  const entry = navEntry(), current = history.state;
  if (current && JSON.stringify(current) === JSON.stringify(entry)) return;
  if (push && current && current.view) history.pushState(entry, "", location.href);
  else history.replaceState(entry, "", location.href);
}

window.addEventListener("popstate", (event) => {
  const entry = event.state;
  if (!entry || !entry.view) return;
  navRestoring = true;
  try {
    if (entry.view === "browse" && entry.target && JSON.stringify(entry.target) !== JSON.stringify(state.target)) {
      state.pendingDetail = entry.node ? { node: entry.node, tab: entry.tab || "overview" } : null;
      startSnapshot(entry.target);  // switches to Browse at once; the check carries on by itself
    } else {
      if (entry.view !== state.view) showView(entry.view);
      if (entry.node && state.snapshot && state.snapshot.nodes.some((n) => n.id === entry.node)) showDetail(entry.node, entry.tab || "overview");
    }
  } finally {
    navRestoring = false;
  }
});

// The address bar keeps only the tenant, so a reload (or a restored tab) always opens on Triage;
// Copy link builds the full link to what's on screen.
// The address bar keeps the tenant only when it was chosen (the Tenant picker or a link); otherwise a reload follows
// whatever `az login` is signed in to now. Copied links always carry it.
function updateUrl() {
  const url = new URL(location.href);
  url.search = "";
  if (state.tenant && state.tenantPinned) url.searchParams.set("tenant", state.tenant);
  history.replaceState(history.state, "", url);
}

function viewLink() {
  const url = new URL(location.href);
  url.search = "";
  if (state.tenant) url.searchParams.set("tenant", state.tenant);
  if (state.view && state.view !== "browse") url.searchParams.set("view", state.view);
  if (state.view === "browse" && state.target) {
    if (state.target.kind === "group") url.searchParams.set("rg", state.target.group);
    else {
      url.searchParams.set("id", state.target.id);
      if (state.target.depth > 0) url.searchParams.set("depth", String(state.target.depth));
    }
    const root = state.snapshot && state.snapshot.root_id;
    if (state.detailNode && (state.target.kind === "group" || !sameId(state.detailNode, root))) url.searchParams.set("node", state.detailNode);
    if (state.detailNode && state.detailTab && state.detailTab !== "overview") url.searchParams.set("tab", state.detailTab);
  }
  return url.toString();
}

async function copyLink() {
  if (await copyText(viewLink())) {
    flash("Link copied. It opens this view for anyone running the console with access to it.", false, 3000);
  }
}

// ------------------------------------------------------------------ search

let searchToken = 0;
// Search covers every subscription you can read, so it finds what an incident or a colleague names; the scope in use
// ranks first and each result says whether it's in it. "only in scope" narrows to it.
function searchParams(text) {
  const scope = currentScope();
  const params = { q: text, ...(scope.subscriptions.length ? scopeParams(scope) : {}) };
  if ($("search-only-scope").checked && scope.subscriptions.length) params.only_scope = "1";
  return params;
}

function scopeChip(r) {
  if (r.in_scope === true) return '<span class="scope-chip in">in scope</span>';
  if (r.in_scope === false) return '<span class="scope-chip out">outside scope</span>';
  return "";
}

function renderSearchScopeToggle() {
  const none = !currentScope().subscriptions.length;
  $("search-only-scope").disabled = none;
  $("search-only-scope").parentElement.title = none
    ? "No scope applied yet: search covers every subscription you can read"
    : "Search every subscription you can read (in-scope results first), or only the scope in use";
}

const runSearch = debounce(async () => {
  const text = $("search").value.trim();
  const box = $("search-results");
  if (text.length < 2) { box.classList.add("hidden"); return; }
  const token = ++searchToken;
  box.classList.remove("hidden");
  const params = searchParams(text);
  box.innerHTML = loading(params.only_scope ? "Searching the scope in use…" : "Searching every subscription…");
  try {
    const results = await api("api/search", { params });
    if (token !== searchToken) return;
    state.searchResults = results;
    box.innerHTML = results.length ? results.map((r, i) => `
      <div class="result ${i === 0 ? "active" : ""}" role="option" data-index="${i}">
        <strong>${esc(r.name)}</strong> <span class="muted">${esc(r.display_type)}</span> ${scopeChip(r)}
        <span class="sub">${r.kind === "resource" ? esc(r.resource_group) + " · " : ""}${esc(subName(r.subscription_id))}</span>
        ${watchButton(r.kind === "resource group" ? groupWatchItem(r.subscription_id, r.name) : { id: r.id, name: r.name, kind: "resource" })}</div>`).join("")
      : params.only_scope ? '<div class="empty-result">Nothing by that name in the scope in use. Untick "only in scope" to search every subscription.</div>'
      : '<div class="empty-result">Nothing found by that name (only what your session can read is searched).</div>';
  } catch (error) {
    box.innerHTML = `<div class="empty-result">${esc(error.message)}</div>`;
  }
}, 300);

function openSearchResult(index) {
  const r = (state.searchResults || [])[index];
  if (!r) return;
  $("search-results").classList.add("hidden");
  $("search").value = "";
  if (r.kind === "resource group") startSnapshot({ kind: "group", group: `${r.subscription_id}/${r.name}`, name: r.name });
  else startSnapshot({ kind: "resource", id: r.id, name: r.name });
}

$("search").addEventListener("input", runSearch);
$("search").addEventListener("keydown", (event) => {
  const items = [...document.querySelectorAll("#search-results .result")];
  const active = items.findIndex((el) => el.classList.contains("active"));
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!items.length) return;
    const next = (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items.forEach((el, i) => el.classList.toggle("active", i === next));
    items[next].scrollIntoView({ block: "nearest" });
  } else if (event.key === "Enter") {
    event.preventDefault();
    openSearchResult(Math.max(active, 0));
  } else if (event.key === "Escape") {
    $("search-results").classList.add("hidden");
  }
});
$("search-results").addEventListener("click", (event) => {
  const item = event.target.closest(".result");
  if (item) openSearchResult(Number(item.dataset.index));
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".search")) $("search-results").classList.add("hidden");
});

// ------------------------------------------------------------------ triage

const TRIAGE_ORDER = { critical: 0, warning: 1, info: 2 };

// Cards with something to show first (red, yellow, then any with items), then still loading, then empty. Cards that
// arrive later (App Insights, ServiceNow, monitoring tools) are put in place as they land.
function sortTriageCards() {
  const box = $("triage-cards");
  const rank = (el) => (el.classList.contains("critical") ? 0 : el.classList.contains("warning") ? 1
    : el.querySelector("li") ? 2 : el.querySelector(".loading") ? 3 : el.querySelector(".notice, .error") ? 4 : 5);
  const cards = [...box.children].filter((el) => el.classList.contains("card"));
  const sorted = [...cards].sort((a, b) => rank(a) - rank(b) || personaCardRank(a.dataset.card) - personaCardRank(b.dataset.card));
  if (sorted.some((el, i) => el !== cards[i])) for (const el of sorted) box.append(el);
}
new MutationObserver(() => requestAnimationFrame(sortTriageCards)).observe($("triage-cards"), { childList: true });

// Shown by every tab that reads the scope while none is applied: what the tab would read, and the saved scopes to start from.
function pickScopeHtml(reads) {
  const saved = state.savedScopes || [];
  return `<div class="start-hint"><h3>Pick what to look at</h3>
    <p>Nothing is selected yet, so nothing has been queried. ${esc(reads)} Choose <strong>Subscriptions</strong> in the Scope bar
      above and press <strong>Apply</strong>${saved.length ? ", or start from a saved scope:" : "."}</p>
    ${saved.length ? `<div class="start-saved">${saved.map((x, i) => `<button type="button" data-start-saved="${i}">${esc(x.name)}</button>`).join("")}</div>` : ""}
    <p class="muted">Search (top) and Access (your own roles) work without a scope.</p></div>`;
}

async function loadTriage(options = {}) {
  const scope = currentScope();
  const scopeKey = JSON.stringify(scope);
  // Same scope as last time: keep the cards up while loading, and mark what's new against them.
  const previous = state.triageScopeKey === scopeKey ? state.triageLast : null;
  const token = (state.triageToken || 0) + 1;
  state.triageToken = token;
  state.triageLoaded = true;
  state.triageScopeKey = scopeKey;
  state.aksSweep = null;  // a new scope (or Refresh) makes an earlier sweep out of date
  state.aksSweepToken = (state.aksSweepToken || 0) + 1;
  $("triage-notice").innerHTML = scopeNotices();
  if (!scope.subscriptions.length) {
    $("triage-cards").innerHTML = pickScopeHtml("Triage checks the subscriptions in scope for problems.");
    return;
  }
  if (previous && options.inPlace) {
    $("triage-meta").innerHTML = `${esc(state.triageMetaText || "")} · ${loading("refreshing…")}`;
  } else {
    $("triage-meta").textContent = "";
    $("triage-cards").innerHTML = loading(`Checking ${scope.subscriptions.length} subscription(s) for problems…`);
  }
  state.triageBusy = true;
  try {
    const data = await api("api/triage", { params: scopeParams(scope) });
    if (token !== state.triageToken) return;
    const when = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    state.triageMetaText = `${data.subscriptions} subscription(s) in ${scope.label} · checked ${when}`;
    state.triageChecked = `${data.subscriptions} subscription(s)`;
    const problems = data.cards.flatMap((c) => c.items.filter((i) => ["critical", "warning"].includes(i.status)));
    const critical = problems.filter((i) => i.status === "critical").length;
    $("triage-summary").textContent = problems.length
      ? `${problems.length} problem(s) need attention${critical ? `, ${critical} critical` : ""}, in ${data.cards.filter((c) => c.status).length} of ${data.cards.length} areas below. More cards (App Insights, ServiceNow, monitoring tools) may add to it as they load.`
      : `No problems in ${data.subscriptions} subscription(s) across ${data.cards.length} areas; cards still loading may add some.`;
    const marks = triageMarks(previous, data.cards);
    state.triageLast = { at: Date.now(), when, items: triageItemKeys(data.cards), status: Object.fromEntries(data.cards.map((c) => [c.key, c.status])) };
    const cards = [...data.cards].sort((a, b) =>
      (TRIAGE_ORDER[a.status] ?? (a.items.length ? 2 : 3)) - (TRIAGE_ORDER[b.status] ?? (b.items.length ? 2 : 3)));
    $("triage-cards").innerHTML = (data.changes_error ? noticeHtml(data.changes_error) : "") + cards.map((card) => renderCard(card, marks)).join("");
    renderTriageMeta();
    loadServiceNowCards(scope);
    loadMonitorCards(scope);
    loadAppInsightsCard(scope);
  } catch (error) {
    if (token !== state.triageToken) return;
    if (previous && options.inPlace) $("triage-meta").innerHTML = `${esc(state.triageMetaText || "")} · <span class="stale">refresh failed: ${esc(error.message)}</span>`;
    else $("triage-cards").innerHTML = noticeHtml(error);
  } finally {
    if (token === state.triageToken) state.triageBusy = false;
  }
}

// ---- marks: what's new since the previous load of the same scope (no extra calls: the page compares its own results)

const triageItemKey = (card, item) => `${card.key}|${(item.id || "").toLowerCase()}|${item.name}`;
const STATUS_WEIGHT = { critical: 3, warning: 2, info: 1 };

function triageItemKeys(cards) {
  return new Set(cards.flatMap((card) => card.items.map((item) => triageItemKey(card, item))));
}

function triageMarks(previous, cards) {
  if (!previous) return null;
  return {
    since: previous.when,
    isNew: (card, item) => !previous.items.has(triageItemKey(card, item)),
    worse: (card) => (STATUS_WEIGHT[card.status] || 0) > (STATUS_WEIGHT[previous.status[card.key]] || 0),
  };
}

function renderTriageMeta() {
  const minutes = state.triageAuto || 0;
  let text = esc(state.triageMetaText || "");
  if (minutes && state.triageLast) {
    const next = Math.max(0, state.triageLast.at + minutes * 60000 - Date.now());
    text += ` · <span title="Refreshes by itself while this tab is open and visible">auto every ${minutes} min, next in ${Math.ceil(next / 60000)} min</span>`;
  }
  $("triage-meta").innerHTML = text;
}

// Auto-refresh: only on Triage, only while visible, never on top of a load still running.
function triageAutoTick() {
  const minutes = state.triageAuto || 0;
  if (!minutes || state.view !== "triage" || document.hidden || state.triageBusy || !state.triageLast) return;
  if (!currentScope().subscriptions.length) return;
  if (Date.now() - state.triageLast.at >= minutes * 60000) loadTriage({ inPlace: true });
  else renderTriageMeta();
}
setInterval(triageAutoTick, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) triageAutoTick(); });

$("triage-auto").addEventListener("change", (event) => {
  state.triageAuto = Number(event.target.value);
  state.triageAutoOverride = true;  // this tab only; the default is in Settings
  renderTriageMeta();
  triageAutoTick();
});

const SUMMARY_ITEMS = 3;  // a summary-level persona sees the first few, then "Show N more"

// "↗" on a card item that isn't a resource: a Service Health page in the portal, or a public status post.
function itemLinkHtml(link) {
  const url = portalUrl(link);
  if (!url.startsWith("https://")) return "";
  return `<a class="portal-link" href="${esc(url)}" target="_blank" rel="noopener" title="${link.health ? "Open in Service Health" : "Open the status post"}">↗</a>`;
}

function renderCard(card, marks = null) {
  const tone = card.status || (card.items.length ? "" : "clear");
  const items = card.items.map((item, index) => {
    const where = [item.display_type, item.resource_group, item.subscription_id ? subName(item.subscription_id) : ""].filter(Boolean).join(" · ");
    const open = item.id ? `data-open="${esc(JSON.stringify({ kind: "resource", id: item.id, name: item.name }))}"` : "";
    const fresh = marks && marks.isNew(card, item);
    return `<li class="${item.id ? "" : "static"} ${fresh ? "is-new" : ""} ${index >= SUMMARY_ITEMS ? "beyond" : ""}" ${open}>${itemPill(item)} <strong>${esc(item.name)}</strong> ${portalLinkHtml(item.id)}${item.link ? itemLinkHtml(item.link) : ""}
      ${item.id ? `<button type="button" class="link next-step" data-open-tab="timeline" title="Open it on its Timeline: alerts, changes and health, newest first">Timeline</button>` : ""}
      ${fresh ? `<span class="new-chip" title="Not listed at the previous refresh (${esc(marks.since)})">new</span>` : ""}
      ${item.id ? watchButton({ id: item.id, name: item.name, kind: "resource" }) : ""}
      <div class="where">${esc(where)}</div><div>${escTimes(item.detail)}</div>
      ${item.changed ? `<div class="changed" title="${esc(new Date(item.changed.at).toLocaleString())}">↻ ${esc(item.changed.text)} · ${esc(dayTime(item.changed.at))}</div>` : ""}</li>`;
  }).join("");
  const worse = marks && marks.worse(card);
  const fresh = marks ? card.items.filter((item) => marks.isNew(card, item)).length : 0;
  return `<div class="card ${tone} ${worse ? "got-worse" : ""}" data-card="${esc(card.key || "")}">
    <h3>${esc(card.title)}${fresh ? ` <span class="new-chip">${fresh} new</span>` : ""}${worse ? ' <span class="worse-chip">worse</span>' : ""}<span class="count">${card.items.length}${card.items.length >= 100 ? "+" : ""}</span></h3>
    <p class="desc">${esc(card.description)}</p>
    ${card.error ? noticeHtml(card.error) : ""}
    ${card.note ? `<p class="muted card-note">${escTimes(card.note)}</p>` : ""}
    ${!card.error && !card.items.length ? `<p class="ok">✓ Nothing found${state.triageChecked ? ` in ${esc(state.triageChecked)}` : ""}</p>` : ""}
    ${items ? `<ul>${items}</ul>` : ""}
    ${card.items.length > SUMMARY_ITEMS ? `<button type="button" class="link show-all" data-show-all>Show ${card.items.length - SUMMARY_ITEMS} more</button>` : ""}
    ${(card.links || []).length ? `<p class="card-links muted">${card.links.filter((l) => (l.url || "").startsWith("https://")).map((l) =>
      `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)} ↗</a>`).join(" · ")}</p>` : ""}
    ${card.key === "aks" ? `<div class="card-actions" id="aks-sweep">${aksSweepHtml()}</div>` : ""}</div>`;
}

// The AKS card's deeper sweep: one request per cluster for pods by phase, optionally Azure's detectors.
function aksSweepHtml() {
  const sweep = state.aksSweep;
  const controls = `<button type="button" id="aks-sweep-run">${sweep && sweep.result ? "Check again" : "Check pods and detectors"}</button>
    <label class="inline"><input type="checkbox" id="aks-sweep-detectors" ${sweep && sweep.detectors ? "checked" : ""}> include Azure's detectors (slower)</label>
    <p class="muted">The card above is one quick query. This asks every cluster in scope for pending, failed or unknown pods,
      and optionally Azure's "Diagnose and solve problems" findings.</p>`;
  if (!sweep) return controls;
  if (sweep.running) return loading(`${sweep.running}…`, "aks-sweep-phase");
  if (sweep.error) return controls + noticeHtml(sweep.error);
  const r = sweep.result;
  const c = r.counts;
  const problems = r.rows.filter((row) => row.status !== "healthy");
  return `<p><strong>${r.clusters} cluster(s) checked:</strong> ${pill("critical")} ${c.critical} · ${pill("warning")} ${c.warning}
      · ${pill("unknown")} ${c.unknown} · ${pill("healthy")} ${c.healthy}</p>
    ${problems.length ? `<ul>${problems.map((row) => `<li data-open="${esc(JSON.stringify({ kind: "resource", id: row.id, name: row.name }))}">
      ${pill(row.status)} <strong>${esc(row.name)}</strong> ${watchButton({ id: row.id, name: row.name, kind: "resource" })}
      <div class="where">${esc(row.resource_group)} · Kubernetes ${esc(row.version || "?")} · ${esc(row.power || "?")}</div>
      <div>${row.problems.map(esc).join("<br>")}</div></li>`).join("")}</ul>`
      : r.clusters ? '<p class="ok">✓ No pending or failed pods' + (sweep.detectors ? " and no detector findings" : "") + "</p>" : ""}
    ${controls}`;
}

// checks.py words a pod-phase problem "2 failed in <namespace>"; those rows link to the Workloads tab.
const AKS_POD_PROBLEM = /^\d+ (failed|pending|unknown) in \S+$/;

// The same check as Triage's AKS card, for the one cluster whose page is open.
function aksOneHtml(clusterId) {
  const run = (state.aksOne || {})[clusterId.toLowerCase()];
  const controls = `<button type="button" data-aks-one>${run && run.result ? "Check again" : "Check pods and detectors"}</button>
    <label class="inline"><input type="checkbox" data-aks-one-detectors ${run && run.detectors ? "checked" : ""}> include Azure's detectors (slower)</label>`;
  const head = '<h4>Pods and detectors</h4><p class="muted">Asks the cluster for pending, failed or unknown pods, and optionally Azure\'s "Diagnose and solve problems" findings: the same check as Triage\'s AKS card, for this cluster.</p>';
  if (!run) return head + controls;
  if (run.running) return head + loading(`${run.running}…`);
  if (run.error) return head + noticeHtml(run.error) + controls;
  const row = run.result.rows[0];
  if (!row) return head + '<p class="muted">The cluster wasn\'t found in its resource group.</p>' + controls;
  return `${head}<p>${pill(row.status)} Kubernetes ${esc(row.version || "?")} · ${esc(row.power || "?")} · provisioning ${esc(row.provisioning || "?")}</p>
    ${row.problems.length ? `<ul class="aks-one-problems">${row.problems.map((p) => `<li class="static">${esc(p)}${AKS_POD_PROBLEM.test(p)
      ? ` <button type="button" class="link" data-aks-workloads="${esc(clusterId)}">see its pods in Workloads</button>` : ""}</li>`).join("")}</ul>`
      : `<p class="ok">✓ No pending or failed pods${run.detectors ? " and no detector findings" : ""}</p>`}${controls}`;
}

async function runAksOne(clusterId, detectors) {
  const key = clusterId.toLowerCase();
  state.aksOne = state.aksOne || {};
  state.aksOne[key] = { running: "Starting", detectors };
  const paint = () => { const box = $("aks-one"); if (box && box.dataset.cluster.toLowerCase() === key) box.innerHTML = aksOneHtml(clusterId); };
  paint();
  try {
    const { job_id } = await api("api/checks", { method: "POST", body: { check: "aks", id: clusterId, detectors } });
    const job = await pollJob(job_id, { onRunning: (j) => { state.aksOne[key].running = j.phase; paint(); } });
    if (!job || job.status === "cancelled") return;
    state.aksOne[key] = { result: job.result, detectors };
    paint();
  } catch (error) {
    state.aksOne[key] = { error: error.message, detectors };
    paint();
  }
}

document.addEventListener("click", (event) => {
  const workloads = event.target.closest("[data-aks-workloads]");
  if (workloads) { showDetail(workloads.dataset.aksWorkloads, "workloads", { push: true }); return; }
  const button = event.target.closest("[data-aks-one]");
  if (!button) return;
  const box = button.closest("#aks-one");
  runAksOne(box.dataset.cluster, !!box.querySelector("[data-aks-one-detectors]")?.checked);
});

function renderAksSweep() {
  const box = $("aks-sweep");
  if (box) box.innerHTML = aksSweepHtml();
}

async function runAksSweep() {
  const scope = currentScope();
  const token = state.aksSweepToken;
  const detectors = !!($("aks-sweep-detectors") && $("aks-sweep-detectors").checked);
  state.aksSweep = { running: "Starting", detectors };
  renderAksSweep();
  try {
    const body = { check: "aks", subscriptions: scope.subscriptions, detectors };
    if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
    const { job_id } = await api("api/checks", { method: "POST", body });
    const job = await pollJob(job_id, {
      isCurrent: () => token === state.aksSweepToken, failed: "The sweep failed.",
      onRunning: (j) => {
        state.aksSweep.running = j.phase;
        const phase = $("aks-sweep-phase");
        if (phase) phase.textContent = `${j.phase}…`; else renderAksSweep();
      } });
    if (!job || job.status === "cancelled") return;
    state.aksSweep = { result: job.result, detectors };
    renderAksSweep();
  } catch (error) {
    if (token === state.aksSweepToken) { state.aksSweep = { error: error.message, detectors }; renderAksSweep(); }
  }
}

$("triage-cards").addEventListener("click", (event) => {
  if (event.target.id === "aks-sweep-run") { runAksSweep(); return; }
  const all = event.target.closest("[data-show-all]");
  if (all) { all.closest(".card").classList.add("show-all"); all.remove(); return; }
  const next = event.target.closest("[data-open-tab]");
  if (next) {
    const target = JSON.parse(next.closest("li[data-open]").dataset.open);
    state.pendingDetail = { node: target.id, tab: next.dataset.openTab };
    startSnapshot(target);
    return;
  }
  const item = event.target.closest("li[data-open]");
  if (item) startSnapshot(JSON.parse(item.dataset.open));
});
$("triage-refresh").addEventListener("click", () => freshly(() => loadTriage({ inPlace: true })));

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
      <a href="${esc(tenantPortal(data.activate_url))}" target="_blank" rel="noopener">Activate in the portal ↗</a></div>`);
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
      : `<p class="ok">✓ No open findings in scope${$("vuln-fixable").checked || Number($("vuln-cvss").value) ? " with these filters" : ""}.</p>
          <p class="muted">Check "Not scanned" below: no findings only counts where Defender scans.</p>`;
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
    <tbody>${findings.map((f) => `<tr class="${hit(f) ? "cve-hit" : ""}"><td><strong>${esc(f.software || f.title)}</strong>${hit(f) ? ' <span class="new-chip">has the CVE</span>' : ""}</td>
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
  el.addEventListener("change", () => loadVulnerabilities({ findingsOnly: true }));
}
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
        <td>${watchButton({ id: r.vault_id, name: r.vault, kind: "resource" })}</td></tr>`).join("")}</tbody></table>`
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

// ------------------------------------------------------------------ pre-peak readiness
// One job over the whole scope: scaling headroom, vCPU quota, subnet IPs, single points, HPAs, certificates.

const READINESS_PROBLEMS = ["critical", "warning", "unknown"];

function defaultPeakEnd() {
  const saved = stored.get("ahc.peakEnd");
  if (saved && saved >= isoDay(new Date())) return saved;
  return isoDay(new Date(Date.now() + 14 * 86400000));
}

function isoDay(moment) {
  return `${moment.getFullYear()}-${String(moment.getMonth() + 1).padStart(2, "0")}-${String(moment.getDate()).padStart(2, "0")}`;
}

function readinessRow(item) {
  const where = [item.display_type, item.subscription_id ? subName(item.subscription_id) : "", item.resource_group].filter(Boolean).join(" · ");
  const open = item.id && item.id.includes("/providers/")
    ? `class="clickable" data-open="${esc(JSON.stringify({ kind: "resource", id: item.id, name: item.name.split(" / ")[0] }))}"` : "";
  return `<tr ${open}><td>${pill(item.status)}</td><td><strong>${esc(item.name)}</strong> ${item.id && item.id.includes("/providers/") ? portalLinkHtml(item.id) : ""}<div class="where">${esc(where)}</div></td>
    <td>${esc(item.detail)}</td><td>${esc(item.fix || "")}</td></tr>`;
}

function readinessTable(items) {
  return `<table class="data-table"><thead><tr><th>Status</th><th>Name</th><th>Detail</th><th>Fix</th></tr></thead>
    <tbody>${items.map(readinessRow).join("")}</tbody></table>`;
}

function renderReadiness() {
  if (!$("readiness-peak-end").value) $("readiness-peak-end").value = defaultPeakEnd();
  const r = state.readiness || {};
  const running = !!r.running;
  $("readiness-run").disabled = running;
  $("readiness-run").textContent = r.result ? "Run again" : "Run";
  $("readiness-cancel").classList.toggle("hidden", !running);
  for (const fmt of ["md", "pdf"]) $(`readiness-export-${fmt}`).disabled = !r.result;
  if (running) {
    $("readiness-counters").innerHTML = "";
    $("readiness-body").innerHTML = loading(`${r.running}…`, "readiness-phase")
      + '<p class="muted">Kubernetes and Key Vault take one request per cluster and vault; other tabs keep working.</p>';
    return;
  }
  if (r.error) { $("readiness-counters").innerHTML = ""; $("readiness-body").innerHTML = noticeHtml(r.error); return; }
  if (!r.result) {
    $("readiness-counters").innerHTML = "";
    $("readiness-meta").textContent = "";
    $("readiness-body").innerHTML = currentScope().subscriptions.length ? '<p class="muted">Pick the date the peak ends, then Run.</p>'
      : pickScopeHtml("Readiness checks the subscriptions in scope before a peak.");
    return;
  }
  const res = r.result;
  const unchecked = res.sections.reduce((n, s) => n + s.unchecked.length + (s.error ? 1 : 0), 0);
  $("readiness-meta").textContent = `peak ends ${res.peak_end} (${res.days_to_peak_end} day(s)) · ${res.subscriptions} subscription(s) · ${res.generated_at}`;
  $("readiness-counters").innerHTML = [["critical", "would stop scaling or is down to its limit"],
    ["warning", "close to a limit or a single point"], ["healthy", "with room to spare"]]
    .map(([tone, label]) => `<div class="counter ${tone}"><span class="n">${res.counts[tone]}</span><span class="label">${label}</span></div>`).join("")
    + (unchecked ? `<div class="counter unknown"><span class="n">${unchecked}</span><span class="label">thing(s) couldn't be checked (named below, with the fix)</span></div>` : "");
  const stale = r.scopeKey !== JSON.stringify(currentScope())
    ? '<p class="notice">The scope changed since this ran. Run again to report on the new scope.</p>' : "";
  $("readiness-body").innerHTML = stale + res.sections.map((section) => {
    const problems = section.items.filter((i) => READINESS_PROBLEMS.includes(i.status));
    const rest = section.items.filter((i) => !READINESS_PROBLEMS.includes(i.status));
    const notes = [...(section.error ? [`Couldn't read: ${section.error}`] : []), ...section.unchecked];
    return `<div class="card ${section.status || "clear"}">
      <h3>${esc(section.title)}<span class="count">${problems.length}</span></h3>
      <p class="desc">${esc(section.description)}</p>
      ${section.summary ? `<p class="muted">${esc(section.summary)}</p>` : ""}
      ${section.skipped ? '<p class="muted">Not run: tick "Kubernetes and Key Vault checks" and Run again.</p>' : ""}
      ${notes.length ? `<div class="notice partial"><strong>Not checked</strong><ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
      ${problems.length ? readinessTable(problems) : section.skipped || section.error ? ""
        : notes.length ? '<p class="muted">Nothing found in what could be read.</p>' : '<p class="ok">✓ Nothing that would stop scaling</p>'}
      ${rest.length ? `<details class="more"><summary>${rest.length} with room to spare or for information</summary>${readinessTable(rest)}</details>` : ""}
    </div>`;
  }).join("");
}

async function runReadiness() {
  const scope = currentScope();
  if (!scope.subscriptions.length) { renderReadiness(); return; }
  const peakEnd = $("readiness-peak-end").value;
  stored.set("ahc.peakEnd", peakEnd);
  const token = (state.readinessToken = (state.readinessToken || 0) + 1);
  state.readiness = { running: "Starting", scopeKey: JSON.stringify(scope) };
  renderReadiness();
  try {
    const body = { subscriptions: scope.subscriptions, peak_end: peakEnd, deep: $("readiness-deep").checked,
      include_secrets: $("readiness-secrets").checked };
    if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
    const { job_id } = await api("api/readiness", { method: "POST", body });
    state.readiness.jobId = job_id;
    const job = await pollJob(job_id, {
      isCurrent: () => token === state.readinessToken, failed: "The report failed.",
      onRunning: (j) => {
        state.readiness.running = j.phase;
        const phase = $("readiness-phase");
        if (phase) phase.textContent = `${j.phase}…`; else renderReadiness();
      } });
    if (!job) return;
    if (job.status === "cancelled") { state.readiness = null; renderReadiness(); return; }
    state.readiness = { result: job.result, jobId: job_id, scopeKey: state.readiness.scopeKey };
    doneToast("readiness", "The readiness report is ready");
    renderReadiness();
  } catch (error) {
    if (token === state.readinessToken) { state.readiness = { error: error.message }; renderReadiness(); }
  }
}

$("triage-readiness").addEventListener("click", () => showView("readiness"));
$("readiness-run").addEventListener("click", () => runReadiness());
$("readiness-cancel").addEventListener("click", async () => {
  const r = state.readiness;
  if (r && r.jobId) { try { await api(`api/jobs/${r.jobId}/cancel`, { method: "POST", body: {} }); } catch { /* already finished */ } }
});
$("readiness-body").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-open]");
  if (row) startSnapshot(JSON.parse(row.dataset.open));
});
for (const fmt of ["md", "pdf"]) {
  $(`readiness-export-${fmt}`).addEventListener("click", async () => {
    const r = state.readiness;
    if (!r || !r.jobId || !r.result) return;
    try {
      await download(`api/readiness/${r.jobId}/export`, { format: fmt }, `readiness.${fmt}`);
    } catch (error) {
      $("readiness-body").insertAdjacentHTML("afterbegin", noticeHtml(error));
    }
  });
}

// ------------------------------------------------------------------ network
// Every VNet in scope (two Resource Graph queries), then one VNet in depth: like azure-subnet-check.

// ------------------------------------------------------------------ personas
// How a kind of user looks at the console (core/personas.py): home view, menu, card and tab order, detail. Never what
// they can read: their Azure roles decide that, and every view stays one click away under More.

state.personas = [];
state.persona = null;

async function loadPersonas() {
  let data;
  try { data = await api("api/personas"); } catch { data = { personas: [], default: "" }; }
  state.personas = data.personas || [];
  state.summaryCards = data.summary_cards || [];
  const saved = data.configured || stored.get("ahc.persona");  // Settings' persona opens every start; View as switches after
  state.personaChosen = state.personas.some((p) => p.name === saved);
  state.persona = state.personas.find((p) => p.name === saved) || state.personas.find((p) => p.name === data.default) || null;
  showPersonaPick();
  $("persona").innerHTML = state.personas.map((p) => `<option value="${esc(p.name)}" title="${esc(p.about)}" ${state.persona && p.name === state.persona.name ? "selected" : ""}>${esc(p.label)}</option>`).join("");
  applyPersona();
}

const personaHome = () => (state.persona && state.persona.home) || "triage";
const personaDetail = () => (state.persona && state.persona.detail) || "full";

function inPersona(view) {
  const p = state.persona;
  if (!p || view === "settings") return true;
  return p.views.includes(view.startsWith("tool-") || view === "servicenow" ? "tools" : view);
}

function choosePersona(name) {
  const p = state.personas.find((x) => x.name === name);
  if (!p) return;
  state.persona = p;
  state.personaChosen = true;
  stored.set("ahc.persona", name);
  $("persona").value = name;
  showPersonaPick();
  applyPersona();
  state.summaryLoaded = "";
  showView(p.home);
}

function applyPersona() {
  document.body.classList.toggle("persona-summary", personaDetail() === "summary");
  const more = $("menu-more");
  more.innerHTML = "";
  for (const b of document.querySelectorAll(".views [data-view]")) {
    if (b.closest("#menu-more")) continue;
    const off = !inPersona(b.dataset.view);
    b.classList.toggle("persona-off", off);
    if (off && !b.classList.contains("hidden")) {
      more.insertAdjacentHTML("beforeend", `<button data-view="${esc(b.dataset.view)}" role="tab">${esc(b.dataset.short || b.firstChild.textContent.trim())}</button>`);
    }
  }
  for (const menu of document.querySelectorAll(".views .nav-menu:not(#more-menu)")) {
    const entries = [...menu.querySelectorAll(".nav-panel [data-view]")].filter((b) => !b.classList.contains("hidden"));
    menu.classList.toggle("persona-off", entries.length > 0 && entries.every((b) => b.classList.contains("persona-off")));
  }
  $("more-menu").classList.toggle("hidden", !more.children.length);
}

// A resource's tabs, the persona's first (in its order), then the rest as before; its first one is where a resource opens.
function personaTabs(tabs) {
  const order = (state.persona && state.persona.tabs) || [];
  const rank = (key) => (order.includes(key) ? order.indexOf(key) : order.length);
  return [...tabs].sort((a, b) => rank(a[0]) - rank(b[0]));
}
const personaCardRank = (key) => {
  const order = (state.persona && state.persona.cards) || [];
  return order.includes(key) ? order.indexOf(key) : order.length;
};

// First run: one banner under the scope bar until a persona is picked; Not now hides it for this page.
function showPersonaPick() {
  const box = $("persona-first");
  const show = !state.personaChosen && !state.personaDeferred && state.personas.length > 0;
  box.classList.toggle("hidden", !show);
  box.innerHTML = show ? personaPickHtml() : "";
}

function personaPickHtml() {
  return `<div class="persona-first-head"><h3>How do you mostly use this?</h3>
    <button type="button" class="link" id="persona-later">Not now</button></div>
    <p class="muted">It sets your home page and what leads the menu; you can change it any time (View as, top right), and
      nothing is hidden: the rest is under More.</p>
    <div class="persona-choices">${state.personas.map((p) => `<button type="button" data-pick-persona="${esc(p.name)}">
      <strong>${esc(p.label)}</strong><span class="muted">${esc(p.about)}</span></button>`).join("")}</div>`;
}

$("persona").addEventListener("change", (event) => choosePersona(event.target.value));
document.addEventListener("click", (event) => {
  const pick = event.target.closest && event.target.closest("[data-pick-persona]");
  if (pick) choosePersona(pick.dataset.pickPersona);
  if (event.target.id === "persona-later") { state.personaDeferred = true; showPersonaPick(); }
});

// ------------------------------------------------------------------ summary
// The headline of each view in a few lines, for anyone who needs "are we OK" before the detail.

state.summaryParts = {};

// Summary: one card per view's headline for the scope in use. The persona picks the cards and their order (`summary`
// in personas; empty = every card); counts and thresholds come from Settings → Summary.
const SUMMARY_TITLES = { problems: ["Problems now", "triage"], changes: ["What changed", ""], cost: ["Cost", "cost"],
  security: ["Security", "security"], readiness: ["Readiness", "readiness"], access: ["Your access", "access"] };
const SUMMARY_RECORDED = ["problems", "security", "cost", "readiness"];  // what What changed compares
const STATUS_WORD = { critical: "red", warning: "yellow", healthy: "green" };
state.summaryCards = [];
const summaryCfg = () => state.summaryCfg || {};
const summaryTop = () => Math.max(1, Number(summaryCfg().top_items) || 1);

function summaryCardKeys() {
  const wanted = (state.persona && state.persona.summary && state.persona.summary.length) ? state.persona.summary : state.summaryCards;
  return wanted.filter((k) => SUMMARY_TITLES[k]);
}

function summaryCard(key, body) {
  const [title, view] = SUMMARY_TITLES[key];
  return `<div class="card summary-card" data-summary="${key}"><h3>${esc(title)}
    ${view ? `<button type="button" class="link" data-show-view="${view}">Open ${esc(title)} →</button>` : ""}</h3>${body}</div>`;
}

// `tone` colours the card's chip in the status line (critical, warning, healthy, or none); `items` feed What changed.
function setSummary(key, html, text, { tone, items } = {}) {
  const box = document.querySelector(`[data-summary="${key}"]`);
  if (box) box.innerHTML = box.querySelector("h3").outerHTML + html;
  state.summaryParts[key] = text;
  if (tone !== undefined) { state.summaryTones[key] = tone; renderSummaryStatus(); }
  if (items !== undefined) summaryRecorded(key, items);
  $("summary-line").textContent = summaryCardKeys().map((k) => state.summaryParts[k]).filter(Boolean).slice(0, 2).join(" ");
}

function summaryStatusText() {
  return summaryCardKeys().filter((k) => k in state.summaryTones).map((k) => `${SUMMARY_TITLES[k][0]}: ${
    STATUS_WORD[state.summaryTones[k]] || "not checked"}`).join(" · ");
}

// A chip's card: scrolled into view when it isn't, then outlined for a moment (so a click always shows something).
function spotlightSummary(key) {
  const card = document.querySelector(`[data-summary="${key}"]`);
  if (!card) return;
  const box = card.getBoundingClientRect();
  if (box.top < 0 || box.top > window.innerHeight * 0.6) card.scrollIntoView({ behavior: "smooth", block: "start" });  // its heading in view
  for (const c of document.querySelectorAll(".summary-card.spotlight")) c.classList.remove("spotlight");
  void card.offsetWidth;  // restart the animation on a second click
  card.classList.add("spotlight");
  state.summarySpot = key;
  for (const chip of document.querySelectorAll("[data-summary-jump]")) chip.setAttribute("aria-pressed", String(chip.dataset.summaryJump === key));
}

function renderSummaryStatus() {
  $("summary-status").innerHTML = summaryCardKeys().filter((k) => k in state.summaryTones).map((k) => {
    const tone = state.summaryTones[k] || "unknown";
    return `<button type="button" class="status-chip ${tone}" data-summary-jump="${k}" aria-pressed="${state.summarySpot === k}" title="${esc(STATUS_WORD[tone] || "not checked")}: show its card">
      <span class="dot ${tone}" aria-hidden="true"></span>${esc(SUMMARY_TITLES[k][0])}</button>`;
  }).join("");
}

// An item opens where its finding is shown: the resource (on `tab` when given), or a view (`view`).
const summaryLi = (item, body, { tab = "", view = "" } = {}) => `<li ${view ? `data-show-view="${esc(view)}"`
  : item.id ? `data-open="${esc(JSON.stringify({ kind: "resource", id: item.id, name: item.name }))}"${tab ? ` data-tab="${esc(tab)}"` : ""}`
  : 'class="static"'}>${body}</li>`;

async function loadSummary(refresh = false) {
  const scope = currentScope();
  state.summaryLoaded = JSON.stringify(scope);
  const token = (state.summaryToken = (state.summaryToken || 0) + 1);
  state.summaryParts = {};
  state.summaryTones = {};
  state.summaryItems = {};
  state.summarySpot = "";
  $("summary-line").textContent = "";
  $("summary-status").innerHTML = "";
  if (!scope.subscriptions.length) {
    $("summary-meta").textContent = "";
    $("summary-cards").innerHTML = pickScopeHtml("Summary reads the subscriptions in scope.");
    return;
  }
  const keys = summaryCardKeys();
  state.summaryWaiting = new Set(SUMMARY_RECORDED.filter((k) => keys.includes(k)));  // only what this persona shows
  state.summaryCurrent = () => token === state.summaryToken;
  $("summary-meta").innerHTML = `${esc(scope.label)} · ${asOfHtml()}`;
  const first = { problems: loading("Checking the scope for problems…"), changes: loading("Waiting for the other cards…"),
    cost: loading("Reading costs…"), security: loading("Reading exposure, settings and vulnerabilities…"),
    readiness: loading("Running Readiness's quick checks…"), access: loading("Reading what your access covers…") };
  $("summary-cards").innerHTML = keys.map((k) => summaryCard(k, first[k])).join("");
  const run = (work) => (refresh ? freshly(work) : work());
  const loaders = { problems: summaryProblems, cost: summaryCost, security: summarySecurity, readiness: summaryReadiness, access: summaryAccess };
  for (const k of keys) if (loaders[k]) run(() => loaders[k](scope));
  if (!state.summaryWaiting.size) summaryChanges(scope, state.summaryCurrent);
}

// Each area reports what it listed once; when every area expected has answered, the snapshot is kept and compared.
function summaryRecorded(key, items) {
  if (!state.summaryWaiting || !state.summaryWaiting.has(key)) return;
  if (items) state.summaryItems[key] = items;
  state.summaryWaiting.delete(key);
  if (!state.summaryWaiting.size) summaryChanges(currentScope(), state.summaryCurrent);
}

async function summaryProblems(scope) {
  let data;
  try { data = await api("api/triage", { params: scopeParams(scope) }); }
  catch (error) { setSummary("problems", noticeHtml(error), "", { tone: null, items: null }); return; }
  const items = data.cards.flatMap((c) => c.items.filter((i) => ["critical", "warning"].includes(i.status)).map((i) => ({ ...i, card: c.title })));
  const critical = items.filter((i) => i.status === "critical").length;
  const groups = new Set(items.map((i) => i.resource_group).filter(Boolean)).size;
  const text = items.length ? `${items.length} problem(s) need attention${critical ? ` (${critical} critical)` : ""}${groups ? ` in ${groups} resource group(s)` : ""}.`
    : `No problems found in ${data.subscriptions} subscription(s).`;
  const top = items.sort((a, b) => (a.status === "critical" ? 0 : 1) - (b.status === "critical" ? 0 : 1)).slice(0, summaryTop());
  const recorded = Object.fromEntries(items.map((i) => [`${i.card}|${i.id || i.name}`, { label: `${i.name}: ${i.card}`, status: i.status }]));
  setSummary("problems", `<p class="big-line">${esc(text)}</p>${top.length ? `<ul class="summary-top">${top.map((i) => summaryLi(i, `${pill(i.status)}
    <strong>${esc(i.name)}</strong> <span class="muted">${esc(i.card)}${i.resource_group ? ` · ${esc(i.resource_group)}` : ""}</span>`)).join("")}</ul>` : ""}`,
  text, { tone: critical ? "critical" : items.length ? "warning" : "healthy", items: recorded });
}

async function summaryCost(scope) {
  let totals = null;
  try { totals = await api("api/cost/totals", { params: costParams(scopeParams(scope)) }); } catch { totals = null; }
  if (totals && totals.available !== false) { renderSummaryCost(totals); return; }
  if (state.cost && state.costLoaded === costKey(scope)) { renderSummaryCost(state.cost); return; }
  setSummary("cost", loading(`Reading costs for ${scope.subscriptions.length} subscription(s): Azure throttles these, so a big scope takes a few minutes the first time`), "");
  loadCost();  // the Cost view's own read: Summary shows it as it fills in (renderCost calls back here)
}

// The scope's 30 days, one figure per day, from each subscription's trend (oldest first, ending today).
function scopeDays(data) {
  const subs = data.subscriptions.filter((s) => s.trend && s.trend.length);
  if (!subs.length || !data.today) return [];
  const n = subs[0].trend.length;
  const end = new Date(`${data.today}T00:00:00Z`);
  return Array.from({ length: n }, (_, i) => ({ date: new Date(end - (n - 1 - i) * 86400000).toISOString().slice(0, 10),
    cost: subs.reduce((sum, s) => sum + (s.trend[i] || 0), 0) }));
}

function renderSummaryCost(data) {
  if (!document.querySelector('[data-summary="cost"]')) return;
  const cur = data.currencies && data.currencies.length > 1 ? "" : data.currency;
  const sum = (k) => data.subscriptions.reduce((n, s) => n + s[k], 0);
  if (!data.subscriptions.length) {
    setSummary("cost", data.done ? `<p class="muted">Costs aren't readable here. ${esc(COST_ROLE_FIX)}</p>`
      : loading(`${data.read || 0} of ${data.total} subscription(s) read…`), "", data.done ? { tone: null, items: null } : {});
    return;
  }
  const money = (v) => moneyText(v, cur);
  const pct = (now, before) => (before ? changeText(now, before) : "");
  const month = sum("month_to_date"), week = sum("last_7_days"), before = sum("previous_7_days");
  const months = data.subscriptions.map((s) => s.month).filter(Boolean);
  const whole = months.length === data.subscriptions.length;
  const projected = whole && months.every((m) => m.projected != null) ? months.reduce((n, m) => n + m.projected, 0) : null;
  const lastMonth = whole && months.every((m) => m.last_month != null) ? months.reduce((n, m) => n + m.last_month, 0) : null;
  const sameDays = data.subscriptions.some((s) => s.last_month_same_days == null) ? null : sum("last_month_same_days");
  const range = costSameDays(data);
  const upTo = data.subscriptions.map((s) => s.up_to).filter(Boolean).sort().pop();
  const unreadable = (data.refused || []).length + (data.failed || []).length;
  const covered = `${data.subscriptions.length} of ${data.total || data.subscriptions.length} subscription(s)`;
  const spikes = data.spikes || [];
  // [label, figure, comparison]: the card's lines, and the same lines as text to copy into a status update
  const lines = [
    [`This month${upTo ? ` (to ${upTo.slice(5)})` : ""}`, money(month), sameDays && range ? `${pct(month, sameDays)} vs ${range}` : data.done ? "" : "reading last month…"],
    projected != null ? ["Heading for", `≈ ${money(projected)}`, lastMonth ? `${pct(projected, lastMonth)} vs last month's ${money(lastMonth)}` : ""] : null,
    before ? ["Last 7 days", money(week), `${pct(week, before)} vs the 7 days before`] : null,
    ["Spikes", String(spikes.length), spikes.length ? `${spikes.slice(0, 2).map((g) => g.name).join(", ")}${spikes.length > 2 ? `, +${spikes.length - 2} more` : ""}` : ""],
  ].filter(Boolean);
  const text = [`Cost, ${covered}${unreadable ? ` (${unreadable} not readable)` : ""}:`,
    ...lines.map(([k, v, extra]) => `- ${k}: ${v}${extra ? ` (${extra})` : ""}`)].join("\n");
  const top = [...data.subscriptions].sort((a, b) => b.month_to_date - a.month_to_date).slice(0, summaryTop());
  const recorded = Object.fromEntries(spikes.map((g) => [`spike|${g.subscription_id}/${g.name}`,
    { label: `Cost spike in ${g.name} (${subName(g.subscription_id)})`, status: "warning" }]));
  state.summaryCostView = { data, cur, text, html: `
    <p class="muted">${esc(covered)}${unreadable ? ` · <span title="${esc(COST_ROLE_FIX)}">${unreadable} not readable</span>` : ""} · ${esc(COST_KIND[data.cost_type || state.costType] || "")}</p>
    ${data.done ? "" : `<p class="muted">${loading(`${data.read} of ${data.total} subscription(s) read so far`)}</p>`}
    <div class="cost-lines">${lines.map(([k, v, extra]) => `<div class="cost-line"><div class="muted">${esc(k)}</div>
      <div><strong class="cost-figure">${esc(v)}</strong>${extra ? ` <span class="muted">${esc(extra)}</span>` : ""}</div></div>`).join("")}</div>
    ${costBarsHtml(scopeDays(data), cur, { height: 40 })}
    <p class="summary-sub"><strong>Biggest this month</strong></p>
    <ul class="summary-top">${top.map((s) => `<li class="static"><strong>${esc(subName(s.subscription_id))}</strong> <span class="muted">${esc(money(s.month_to_date))}</span></li>`).join("")}</ul>
    <div class="summary-budgets"></div>
    <p class="muted">${data.done ? asOfHtml(data.read_at) : ""}</p>`, recorded, tone: spikes.length ? "warning" : "healthy" };
  setSummary("cost", state.summaryCostView.html, data.done ? text : "");
  const slot = document.querySelector('[data-summary="cost"] .summary-budgets');
  if (slot && data.done) slot.innerHTML = loading("Reading budgets…");
  state.summaryCostRefused = data.done ? (data.refused || []).length : null;
  if (data.done) summaryBudgets(currentScope(), state.summaryCurrent);
}

async function summaryBudgets(scope, current) {
  let found;
  try { found = await api("api/cost/budgets", { params: scopeParams(scope) }); }
  catch (error) { found = { budgets: [], notes: [{ note: error.message }] }; }
  const view = state.summaryCostView;
  if (!current() || !view) return;
  const warn = Number(summaryCfg().budget_warn_percent) || 100;
  const rows = found.budgets.map((b) => {
    const spent = b.percent ?? 0;
    const forecast = b.forecast != null && b.amount ? (b.forecast / b.amount) * 100 : null;
    const status = spent >= 100 ? "critical" : (spent >= warn || (forecast ?? 0) >= 100) ? "warning" : "healthy";
    return { ...b, spent_pct: spent, forecast_pct: forecast, status };
  }).sort((a, b) => b.spent_pct - a.spent_pct);
  const flagged = rows.filter((b) => b.status !== "healthy");
  const line = rows.length ? (flagged.length ? `${flagged.length} of ${rows.length} need attention (${warn}% or more spent, or forecast past 100%).`
    : `all ${rows.length} under ${warn}%.`) : "none set on these subscriptions.";
  const html = `<p class="summary-sub"><strong>Budgets:</strong> ${esc(line)}</p>${flagged.length ? `<ul class="summary-top">${flagged.slice(0, summaryTop()).map((b) => `<li class="static">
    ${pill(b.status)} <strong>${esc(b.name)}</strong> <span class="muted">${esc(subName(b.subscription_id))} · ${b.spent_pct.toFixed(0)}% of
    ${esc(moneyText(b.amount, b.currency))} spent${b.forecast_pct != null ? `, forecast ${b.forecast_pct.toFixed(0)}%` : ""} (${esc(b.time_grain.toLowerCase())})</span></li>`).join("")}</ul>` : ""}
    ${found.notes.length ? `<p class="muted">Budgets not readable in ${found.notes.length} subscription(s)${found.notes[0].subscription_id
      ? ` (${esc(found.notes.map((n) => subName(n.subscription_id)).slice(0, 3).join(", "))}${found.notes.length > 3 ? ", …" : ""})` : ""}.</p>` : ""}`;
  view.html = view.html.replace('<div class="summary-budgets"></div>', `<div class="summary-budgets">${html}</div>`);
  const tone = rows.some((b) => b.status === "critical") ? "critical" : (flagged.length || view.tone === "warning") ? "warning" : "healthy";
  const items = { ...view.recorded, ...Object.fromEntries(flagged.map((b) => [`budget|${b.subscription_id}/${b.name}`,
    { label: `Budget ${b.name} at ${b.spent_pct.toFixed(0)}%`, status: b.status }])) };
  setSummary("cost", view.html, `${view.text}\n- Budgets: ${line}`, { tone, items });
}

async function summarySecurity(scope) {
  const vulns = api("api/vulnerabilities/headline", { params: scopeParams(scope) }).catch((error) => ({ error: error.message }));
  let data;
  try { data = await api("api/security", { params: scopeParams(scope) }); }
  catch (error) { setSummary("security", noticeHtml(error), "", { tone: null, items: null }); return; }
  const warnings = data.rows.filter((r) => r.severity === "warning");
  const resources = new Set(warnings.map((r) => r.id.toLowerCase())).size;
  const labels = Object.fromEntries(data.categories.map((c) => [c.key, c.label]));
  const tiles = securityTilesHtml(data);
  const top = warnings.slice(0, summaryTop());
  const text = warnings.length ? `${warnings.length} security warning(s) on ${resources} resource(s).` : "No security warnings in the settings checked.";
  const v = await vulns;
  const s = v && v.summary;
  const reachable = s ? v.resources.filter((r) => r.exposure.length && r.high) : [];
  const vulnText = !s ? "" : s.findings ? `${s.findings.toLocaleString()} open vulnerability finding(s) (${s.high.toLocaleString()} high) on ${s.resources} resource(s); ${
    s.running.toLocaleString()} in running containers; ${s.findings ? Math.round((100 * s.fixable) / s.findings) : 0}% have a fix.` : "No open vulnerability findings from Defender.";
  const vulnTop = s ? v.resources.map((r) => summaryLi({ id: r.parent, name: r.name }, `${pill(r.exposure.length && r.high ? "critical" : r.high ? "warning" : "info")}
    <strong>${esc(r.name)}</strong> <span class="muted">${esc(typeLabel(r.rtype))} · worst CVSS ${esc(String(r.maxCvss ?? "–"))}${r.running ? " · running in AKS" : ""}${
    r.exposure.length ? ` · reachable: ${esc(r.exposure.join(", "))}` : ""}</span>`, { tab: "vulnerabilities" })).join("") : "";
  const recorded = Object.fromEntries([
    ...warnings.map((r) => [`${r.id}|${r.category}|${r.what}`, { label: `${r.name}: ${r.what}`, status: "warning" }]),
    ...(s ? v.resources.map((r) => [`vuln|${r.parent}`, { label: `${r.name}: vulnerabilities, worst CVSS ${r.maxCvss}`,
      status: r.exposure.length && r.high ? "critical" : r.high ? "warning" : "info" }]) : [])]);
  setSummary("security", `<p class="big-line">${esc(text)}</p><div class="counters summary-tiles">${tiles}</div>
    ${top.length ? `<ul class="summary-top">${top.map((r) => summaryLi(r, `${itemPill({ ...r, status: r.severity })} <strong>${esc(r.name)}</strong>
      <span class="muted">${esc(r.display_type)} · ${esc(r.what)} (${esc((labels[r.category] || r.category).toLowerCase())})</span>`)).join("")}</ul>` : ""}
    <p class="summary-sub"><button type="button" class="link" data-show-vulns><strong>Vulnerabilities</strong></button> ${v && v.error ? esc(v.error) : esc(vulnText)}</p>
    ${vulnTop ? `<ul class="summary-top">${vulnTop}</ul>` : ""}`,
  [text, vulnText].filter(Boolean).join(" "),
  { tone: reachable.length ? "critical" : (warnings.length || (s && s.high)) ? "warning" : "healthy", items: recorded });
}

async function summaryReadiness(scope) {
  const full = state.readiness && state.readiness.result && state.readiness.scopeKey === JSON.stringify(scope) ? state.readiness.result : null;
  if (full) { renderSummaryReadiness(full); return; }
  if (!summaryCfg().readiness_quick) {
    setSummary("readiness", `<p class="muted">Not run for this scope yet. Before a busy period, Readiness says what would stop
      the scope from scaling and what expires before it ends. (Settings → Summary runs its quick checks here automatically.)</p>`, "", { items: null });
    return;
  }
  const current = state.summaryCurrent;
  try {
    const body = { subscriptions: scope.subscriptions, peak_end: defaultPeakEnd(), deep: false, purpose: "summary" };
    if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
    const { job_id } = await api("api/readiness", { method: "POST", body });
    const job = await pollJob(job_id, { isCurrent: current, failed: "Readiness's quick checks failed." });
    if (!job || !current()) return;
    if (job.status !== "done") { setSummary("readiness", '<p class="muted">Stopped.</p>', "", { items: null }); return; }
    if (!(state.readiness && (state.readiness.result || state.readiness.running))) {
      state.readiness = { result: job.result, jobId: job_id, scopeKey: JSON.stringify(scope) };  // Readiness shows the same report
    }
    renderSummaryReadiness(job.result);
  } catch (error) {
    if (current()) setSummary("readiness", noticeHtml(error), "", { tone: null, items: null });
  }
}

function renderSummaryReadiness(r) {
  const quick = !r.deep;
  const c = r.counts || {};
  const found = r.sections.flatMap((s) => s.items.filter((i) => ["critical", "warning"].includes(i.status)).map((i) => ({ ...i, section: s.title })));
  const unread = r.sections.filter((s) => s.error).map((s) => s.title);
  const text = `Readiness: ${c.critical || 0} critical and ${c.warning || 0} warning finding(s) for a peak ending ${r.peak_end}.`;
  const recorded = Object.fromEntries(found.map((i) => [`${i.section}|${i.id || i.name}`, { label: `${i.name}: ${i.section}`, status: i.status }]));
  setSummary("readiness", `<p class="big-line">${esc(text)}</p>
    ${found.length ? `<ul class="summary-top">${found.slice(0, summaryTop()).map((i) => summaryLi(i, `${pill(i.status)} <strong>${esc(i.name)}</strong>
      <span class="muted">${esc(i.section)} · ${esc(i.detail)}</span>`, { view: "readiness" })).join("")}</ul>` : ""}
    ${unread.length ? `<p class="muted">Not read: ${esc(unread.join(", "))}.</p>` : ""}
    <p class="muted">${quick ? `Quick checks (scaling, vCPU quota, subnets, single instances), for the peak date last used on Readiness
      (${esc(r.peak_end)}, change it there). Readiness shows the same report and can add Kubernetes and Key Vault checks.` : `The last full Readiness report, ${esc(r.generated_at)}.`}</p>`,
  text, { tone: c.critical ? "critical" : c.warning ? "warning" : "healthy", items: recorded });
}

async function summaryAccess(scope) {
  let pimData = null;
  try { pimData = await api("api/pim", { params: scopeParams(scope) }); } catch { pimData = null; }
  const lines = [`You can read ${state.subscriptions.length} subscription(s) in this tenant; ${scope.subscriptions.length} are in scope.`];
  if (pimData && !pimData.error) lines.push(`${pimData.current_total ?? 0} role(s) you hold; ${pimData.total} you could activate in PIM.`);
  else if (pimData && pimData.error) lines.push("PIM isn't available or readable here, so only the roles you hold count.");
  setSummary("access", `<ul class="summary-top">${lines.map((l) => `<li class="static">${esc(l)}</li>`).join("")}</ul>`, "");
}

const AREA_TITLES = { problems: "Problems", security: "Security", cost: "Cost", readiness: "Readiness" };

async function summaryChanges(scope, current) {
  if (!Object.keys(state.summaryItems).length) {
    setSummary("changes", '<p class="muted">Nothing to compare: no card answered.</p>', "");
    return;
  }
  const body = { subscriptions: scope.subscriptions, items: state.summaryItems };
  if (scope.groups) { body.groups = scope.groups; body.filtered = true; }
  let data;
  try { data = await api("api/summary/changes", { method: "POST", body }); }
  catch (error) { if (current()) setSummary("changes", noticeHtml(error), ""); return; }
  if (!current()) return;
  if (!data.since) {
    setSummary("changes", `<p class="muted">Summary keeps what it lists each time it opens, on this machine. The first comparison is
      ready ${esc(new Date(data.ready_at).toLocaleString())} (${esc(String(summaryCfg().compare_hours))} h after the first summary of this scope).</p>`, "");
    return;
  }
  const areas = Object.entries(data.areas);
  const fresh = areas.reduce((n, [, a]) => n + a.new.length, 0);
  const gone = areas.reduce((n, [, a]) => n + a.resolved.length, 0);
  const since = `${new Date(data.since).toLocaleString()} (${ago(data.since)})`;
  const text = fresh || gone ? `Since ${since}: ${fresh} new or worse, ${gone} resolved.` : `Nothing new or resolved since ${since}.`;
  const top = summaryTop();
  const blocks = areas.filter(([, a]) => a.new.length || a.resolved.length).map(([area, a]) => `<p class="summary-sub"><strong>${esc(AREA_TITLES[area] || area)}</strong>
    ${a.new.length} new or worse · ${a.resolved.length} resolved</p><ul class="summary-top">
    ${a.new.slice(0, top).map((i) => `<li class="static">${pill(i.status || "info")} ${esc(i.label)}${i.was ? ` <span class="muted">(was ${esc(i.was)})</span>` : ""}</li>`).join("")}
    ${a.resolved.slice(0, top).map((i) => `<li class="static"><span class="better">✓</span> ${esc(i.label)} <span class="muted">resolved</span></li>`).join("")}</ul>`).join("");
  setSummary("changes", `<p class="big-line">${esc(text)}</p>${blocks}`, text);
}

$("summary-refresh").addEventListener("click", () => loadSummary(true));
$("summary-copy").addEventListener("click", (event) => {
  const scope = currentScope();
  const text = [`${scope.label}, ${new Date().toLocaleString()}`, summaryStatusText(),
    ...summaryCardKeys().map((k) => state.summaryParts[k]).filter(Boolean)].filter(Boolean).join("\n");
  copyText(text, event.currentTarget);
});
$("view-summary").addEventListener("click", (event) => {
  const go = event.target.closest("[data-show-view]");
  if (go) { showView(go.dataset.showView); return; }
  const jump = event.target.closest("[data-summary-jump]");
  if (jump) { spotlightSummary(jump.dataset.summaryJump); return; }
  if (event.target.closest("[data-show-vulns]")) { state.secSub = "vulnerabilities"; showView("security"); return; }
  const area = event.target.closest("[data-sec-category]");
  if (area) { state.secSub = "config"; state.secCategories = new Set([area.dataset.secCategory]); showView("security"); if (state.security) renderSecurity(); return; }
  const item = event.target.closest("li[data-open]");
  if (!item) return;
  const target = JSON.parse(item.dataset.open);
  if (item.dataset.tab) state.pendingDetail = { node: target.id, tab: item.dataset.tab };
  startSnapshot(target);
});

// ------------------------------------------------------------------ cost
// Browse's Cost card on a subscription or group (cost.summary), a resource's Cost tab, the Cost view (one job over the
// scope's subscriptions, cost.overview, plus Billed while idle from Resource Graph). Nothing reads costs until opened.

const moneyText = (v, cur = "") => (v == null ? "–"
  : `${cur ? `${cur} ` : ""}${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const moneyCell = (v, cur = "") => `<span data-v="${Number(v) || 0}">${esc(moneyText(v, cur))}</span>`;

// "+12%" against an earlier figure: a rise of more than 10% reads as worse, a fall as better.
// "Oct 1–13": last month's days this month is compared with (to the latest day both months have, from the data).
function costSameDays(data) {
  const to = (data.subscriptions || []).map((s) => s.same_days_to).filter(Boolean).sort().pop();
  if (!to) return "";
  const day = new Date(`${to}T00:00:00`);
  return `${day.toLocaleDateString(undefined, { month: "short" })} 1–${day.getDate()}`;
}

// "+12%", "−0.4%", "0%": one decimal under 1%, never "−0%". The one way every cost figure states a change.
function changeText(now, before) {
  const pct = ((now - before) / before) * 100;
  const shown = Math.abs(pct) < 1 ? Math.abs(pct).toFixed(1) : Math.abs(pct).toFixed(0);
  return Number(shown) === 0 ? "0%" : `${pct > 0 ? "+" : "−"}${shown}%`;
}

function costDeltaHtml(now, before) {
  if (!before) return now ? '<span class="muted">new</span>' : "";
  const pct = ((now - before) / before) * 100;
  return `<span class="${pct > 10 ? "worse" : pct < -10 ? "better" : "muted"}">${changeText(now, before)}</span>`;
}

// One bar per day, oldest left; hovering a bar names its day and amount.
function costBarsHtml(days, cur, { width = 300, height = 56 } = {}) {
  if (!days || !days.some((d) => d.cost)) return '<p class="muted">No costs in the last 30 days.</p>';
  const top = Math.max(...days.map((d) => d.cost), 0.01);
  const w = width / days.length;
  return `<svg class="cost-bars" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Cost per day, last ${days.length} days">${
    days.map((d, i) => {
      const h = d.cost > 0 ? Math.max(1, ((height - 2) * d.cost) / top) : 0;
      const today = i === days.length - 1;  // every cost chart ends today, which Azure is still filling in
      return `<rect ${today ? 'class="partial" ' : ""}x="${(i * w + 0.5).toFixed(1)}" y="${(height - h).toFixed(1)}" width="${Math.max(1, w - 1).toFixed(1)}" height="${h.toFixed(1)}"><title>${esc(d.date)}: ${esc(moneyText(d.cost, cur))}${today ? " so far (today isn't finished)" : ""}</title></rect>`;
    }).join("")}</svg><div class="cost-axis muted"><span>${esc(days[0].date)}</span><span>today, so far</span></div>`;
}

// A total's parts, each with its share as a bar.
function costPartsHtml(parts, cur, render = (p) => esc(p.name)) {
  const top = Math.max(...parts.map((p) => p.cost), 0.01);
  return `<ul class="cost-parts">${parts.map((p) => `<li><span class="cost-part-name">${render(p)}</span>
    <span class="cost-part-bar"><span style="width:${((100 * Math.max(p.cost, 0)) / top).toFixed(1)}%"></span></span>
    <span class="cost-part-n">${esc(moneyText(p.cost, cur))}</span></li>`).join("")}</ul>`;
}

const COST_ROLE_FIX = "Ask for Cost Management Reader (or Reader) on the subscription; if you're eligible through PIM, activate it, then Refresh access.";
const costNotice = (d) => `${noticeHtml(d.error)}${d.required ? `<p class="muted">${esc(COST_ROLE_FIX)}</p>` : ""}`;

function costSlot(id) {
  return `<div class="card cost-here gs-wide" data-cost-here="${esc(id)}"><h3>Cost</h3>${loading("Loading costs…")}</div>`;
}

async function fillCost(root = document) {
  for (const box of root.querySelectorAll("[data-cost-here]:not([data-filled])")) {
    box.dataset.filled = "1";
    const what = box.dataset.costHere.toLowerCase().includes("/resourcegroups/") ? "resource group" : "subscription";
    box.innerHTML = `<h3>Cost</h3>${loading(state.fresh ? `Getting the latest costs for this ${what} from Azure…` : "Loading costs…")}`;
    // Stored costs answer at once; still waiting after a moment means Azure is being read for the first time.
    const slow = state.fresh ? null : setTimeout(() => {
      box.innerHTML = `<h3>Cost</h3>${loading(`Reading this ${what}'s costs from Azure for the first time (5–15 s). Next time it opens at once.`)}`;
    }, 1200);
    let data;
    try { data = await api("api/cost/summary", { params: costParams({ id: box.dataset.costHere }) }); }
    catch (error) { clearTimeout(slow); box.innerHTML = `<h3>Cost</h3>${noticeHtml(error)}`; continue; }
    clearTimeout(slow);
    box.innerHTML = costSummaryHtml(data);
  }
}

function budgetHtml(b, cur) {
  const unit = b.currency || cur;
  const status = b.percent >= 100 ? "critical" : b.percent >= 80 ? "warning" : "";
  return `<li><strong>${esc(b.name)}</strong> <span class="muted">${esc(b.time_grain)}</span> ${usageBar(b.percent, status)}
    <span>${esc(moneyText(b.spent, unit))} of ${esc(moneyText(b.amount, unit))} (${b.percent ?? "–"}%)${b.forecast != null
      ? ` <span class="muted">· forecast ${esc(moneyText(b.forecast, unit))}</span>` : ""}</span></li>`;
}

function costSummaryHtml(d) {
  const head = `<h3>Cost ${portalLinkHtml(d.scope, { label: "Cost analysis ↗", page: "costanalysis", title: "Cost analysis in the Azure portal" })}
    <button type="button" class="link cost-again" data-cost-again title="Read the costs again from Azure">Refresh</button></h3>`;
  if (d.error) return head + costNotice(d);
  const cur = d.currency;
  const sub = d.scope.split("/")[2];
  const partName = (p) => (p.subscription_level ? `<span title="${esc(SUBSCRIPTION_LEVEL_HELP)}">${esc(p.name)}</span>`
    : d.top_kind === "resource group" && !p.name.startsWith("(")
    ? `<button type="button" class="link" data-cost-group-open="${esc(`${sub}/${p.name}`)}">${esc(p.name)}</button>`
    : p.id ? `<button type="button" class="link" data-open="${esc(JSON.stringify({ kind: "resource", id: p.id, name: p.id.split("/").pop() }))}">${esc(p.id.split("/").pop())}</button> ${portalLinkHtml(p.id)}`
      : esc(p.name)) + (p.services && p.services.length ? `<div class="muted">${esc(p.services.join(", "))}</div>` : "");
  return `${head}
    <div class="cost-figures">
      <div><span class="big">${esc(moneyText(d.month_to_date, cur))}</span><div class="muted">this month${d.up_to ? `, up to ${esc(d.up_to)}` : ""}</div></div>
      <div><span class="big">${esc(moneyText(d.last_month_same_days, cur))}</span> ${costDeltaHtml(d.month_to_date, d.last_month_same_days)}<div class="muted">last month by the same day${d.same_days_to ? ` (to ${esc(d.same_days_to)})` : ""}</div></div>
      <div><span class="big">${esc(moneyText(d.last_month, cur))}</span><div class="muted">all of last month</div></div>
      <div title="Usage at its daily rate so far, plus purchases counted once (and last month's later ones expected again)"><span class="big">${d.projected != null ? `≈ ${esc(moneyText(d.projected, cur))}` : "–"}</span><div class="muted">where this month is heading</div></div>
      ${d.booked ? `<div><span class="big">${esc(moneyText(d.booked, cur))}</span><div class="muted">purchases this month (${esc((d.booked_days || []).map((b) => b.date.slice(5)).join(", "))}), in This month${d.booked_expected ? `; ${esc(moneyText(d.booked_expected, cur))} more expected as last month` : ""}</div></div>` : ""}
    </div>
    <h4>Last 30 days, usage</h4>${costBarsHtml(d.trend, cur)}
    <div class="cost-cols">
      <div><h4>By service, this month</h4>${d.services.length ? costPartsHtml(d.services, cur) : '<p class="muted">Nothing this month.</p>'}</div>
      <div><h4>Top ${esc(d.top_kind)}s, this month ${d.top_total > d.top.length ? `<span class="muted">${d.top.length} of ${d.top_total}</span>` : ""}</h4>
        ${d.top.length ? costPartsHtml(d.top, cur, partName) : '<p class="muted">Nothing this month.</p>'}</div>
    </div>
    <h4>Budgets</h4>${d.budget_note ? `<p class="muted">${esc(d.budget_note)}</p>` : d.budgets.length
      ? `<ul class="cost-budgets">${d.budgets.map((b) => budgetHtml(b, cur)).join("")}</ul>` : '<p class="muted">No budget is set here.</p>'}
    <p class="muted">Actual cost, as invoiced, ${d.read_now ? "updated just now from Azure and kept on this machine" : asOfHtml(d.read_at)}.
      Azure's figures for the last day or two are still filling in.</p>
    ${d.updating ? '<p class="muted cost-updating">Newer figures are being read in the background; Refresh in a minute to see them.</p>' : ""}
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(d)}</details>`;
}

// A Cost card's links: a group opens like a click on it in the tree, a resource opens in Browse, Refresh reads again.
function costCardClick(event) {
  const group = event.target.closest("[data-cost-group-open]");
  if (group) {
    const key = group.dataset.costGroupOpen;
    if (appliedGroups().some((g) => groupKey(g) === key)) openGroup(key);
    else flash("That group isn't in the scope in use (a tag filter leaves it out).", true);
    return true;
  }
  const again = event.target.closest("[data-cost-again]");
  if (again) {
    const box = again.closest("[data-cost-here]");
    delete box.dataset.filled;
    freshly(() => fillCost(box.parentElement));
    return true;
  }
  return false;
}

async function tabCost(node, body) {
  const d = await exploreGet("cost", node, costParams());
  const aks = node.type === AKS_TYPE ? `<p class="muted">A cluster's nodes, disks and load balancer are billed in its node
    resource group (usually <code>MC_…</code>): that group's Cost card has them.</p>` : "";
  if (d.error) { body.innerHTML = costNotice(d) + aks; return; }
  const cur = d.currency;
  body.innerHTML = `<div class="cost-figures">
      <div><span class="big">${esc(moneyText(d.month_to_date, cur))}</span><div class="muted">this month${d.up_to ? `, up to ${esc(d.up_to)}` : ""}</div></div>
      <div><span class="big">${esc(moneyText(d.last_30_days, cur))}</span><div class="muted">last 30 days</div></div>
      <div>${portalLinkHtml(`/${node.id.split("/").slice(1, 5).join("/")}`, { label: "Cost analysis ↗", page: "costanalysis", title: "Its resource group's Cost analysis in the Azure portal" })}</div>
    </div>${aks}
    <h4>Last 30 days</h4>${costBarsHtml(d.trend, cur)}
    <h4>By meter, last 30 days</h4>${d.meters.length ? costPartsHtml(d.meters, cur) : '<p class="muted">No costs recorded for it in the last 30 days.</p>'}
    <p class="muted">Actual cost, as invoiced, ${asOfHtml(d.read_at)}. Azure's figures for the last day or two are still filling in.</p>
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(d)}</details>`;
}

// ---- the Cost view

const COST_SUBS = ["spend", "waste"];
state.costSub = stored.choice("ahc.costSub", COST_SUBS, "spend");
state.costGroup = stored.choice("ahc.costGroup", ["subscription", "group", "tag"], "subscription");
// Actual (as invoiced) or amortized (reservations spread over their days, charged to what uses them): every cost read.
state.costType = stored.choice("ahc.costType", ["actual", "amortized"], "actual");
const costParams = (extra = {}) => ({ ...extra, ...(state.costType === "amortized" ? { cost_type: "amortized" } : {}) });
const costKey = (scope) => JSON.stringify({ ...scope, costType: state.costType });
const SUBSCRIPTION_LEVEL = "Subscription-level charges";
const TAG_KEY_CHARS = 32;  // tag pickers show keys and values this long at most (full text on hover)
const shortTag = (text) => (text.length > TAG_KEY_CHARS ? `${text.slice(0, TAG_KEY_CHARS - 1)}…` : text);
const SUBSCRIPTION_LEVEL_HELP = "Billed to the subscription, not to a resource group: reservations, savings plans, Defender plans, support, Marketplace";
const COST_KIND = { actual: "Actual cost, as invoiced", amortized: "Amortized cost: reservations spread over their days and charged to what uses them" };

function showCostSub(sub) {
  state.costSub = sub;
  stored.set("ahc.costSub", sub);
  for (const b of $("cost-subtabs").querySelectorAll("[data-cost-sub]")) b.classList.toggle("active", b.dataset.costSub === sub);
  $("cost-spend").classList.toggle("hidden", sub !== "spend");
  $("cost-waste").classList.toggle("hidden", sub !== "waste");
  const key = JSON.stringify(currentScope());
  if (sub === "spend" && state.costLoaded !== costKey(currentScope())) loadCost();
  if (sub === "waste" && state.wasteLoaded !== key) loadWaste();
}

function costExports(on) {
  for (const fmt of ["md", "csv"]) $(`cost-export-${fmt}`).disabled = !on;
}

async function loadCost() {
  const scope = currentScope();
  state.costLoaded = costKey(scope);
  for (const b of $("cost-type").querySelectorAll("[data-cost-type]")) b.classList.toggle("active", b.dataset.costType === state.costType);
  const token = (state.costToken = (state.costToken || 0) + 1);
  costExports(false);
  state.cost = null;
  $("cost-counters").innerHTML = "";
  $("cost-progress").innerHTML = "";
  if (!scope.subscriptions.length) {
    $("cost-body").innerHTML = pickScopeHtml("Cost reads the costs of the subscriptions in scope.");
    return;
  }
  $("cost-body").innerHTML = loading(`Reading costs for ${scope.subscriptions.length} subscription(s)…`);
  const started = Date.now();
  let baseline = null;
  try {
    const { job_id } = await api("api/cost", { method: "POST", body: costParams({ subscriptions: scope.subscriptions }) });
    state.costJob = job_id;
    const job = await pollJob(job_id, { isCurrent: () => token === state.costToken, failed: "Reading costs failed.", every: 1500,
      onRunning: (j) => {
        if (j.partial && baseline === null) baseline = j.partial.refreshing ? 0 : j.partial.read;  // already kept: arrived at once, not a rate
        renderCostProgress(j.partial, scope.subscriptions.length, started, baseline || 0);
        if (j.partial) { state.cost = j.partial; renderCost(); }
      } });
    if (!job) return;
    $("cost-progress").innerHTML = "";
    if (job.status === "cancelled") { $("cost-progress").innerHTML = '<p class="muted">Stopped. What was read is shown and kept; Refresh reads the rest.</p>'; return; }
    state.cost = job.result;
    renderCost();
    costExports(true);
    doneToast("cost", `Costs for ${scope.subscriptions.length} subscription(s) are read`);
  } catch (error) {
    if (token === state.costToken) { state.costLoaded = ""; $("cost-progress").innerHTML = ""; $("cost-body").innerHTML = noticeHtml(error); }
  }
}

function renderCostProgress(partial, total, started, baseline = 0) {
  if (partial && partial.refreshing) {
    const done = partial.refreshed, of = partial.to_refresh;
    const seconds = (Date.now() - started) / 1000;
    const left = done ? Math.max(0, Math.round(((seconds / done) * (of - done)) / 60)) : null;
    $("cost-progress").innerHTML = `<div class="notice partial">${loading(`Updating costs: ${done} of ${of} subscription(s) reread${
      left !== null && done < of ? ` · about ${left < 1 ? "under a minute" : `${left} min`} left` : ""}. The figures below are the kept ones
      until each subscription is updated.`)} <button type="button" class="link" id="cost-cancel">Stop</button></div>`;
    return;
  }
  const read = partial ? partial.read : 0;
  const seconds = (Date.now() - started) / 1000;
  const fresh = read - baseline;
  const left = fresh > 0 ? Math.max(0, Math.round(((seconds / fresh) * (total - read)) / 60)) : null;
  $("cost-progress").innerHTML = `<div class="notice partial">${loading(`${read} of ${total} subscription(s) read${left !== null && read < total
    ? ` · about ${left < 1 ? "under a minute" : `${left} min`} left (Azure throttles cost reads to about 20 subscriptions a minute)` : ""}`)}
    <button type="button" class="link" id="cost-cancel">Stop</button></div>`;
}

// Cost rows for the chosen grouping, each {name, sub?, key?, month_to_date, last_month, last_month_same_days, booked, last_30_days,
// last_7_days, previous_7_days, change, trend, spike?}; the 7-day figures and the trend are usage (no purchases).
// The Cost view's data for the subscription picked in its own filter (all of the scope when none is).
function costView(data) {
  const pick = $("cost-sub").value;
  const subs = data.subscriptions.map((s) => s.subscription_id);
  $("cost-sub").innerHTML = `<option value="">All subscriptions (${subs.length})</option>${[...subs].sort((a, b) => subName(a).localeCompare(subName(b)))
    .map((id) => `<option value="${esc(id)}" ${id === pick ? "selected" : ""}>${esc(subName(id))}</option>`).join("")}`;
  $("cost-sub").classList.toggle("hidden", subs.length < 2);
  if (!pick || !subs.includes(pick)) return data;
  const only = (rows) => rows.filter((r) => r.subscription_id === pick);
  return { ...data, subscriptions: only(data.subscriptions), groups: only(data.groups), spikes: only(data.spikes) };
}

function costRows(data) {
  if (state.costGroup === "subscription") return data.subscriptions.map((s) => ({ ...s, name: subName(s.subscription_id) }));
  const scoped = currentScope().groups ? new Set(appliedGroups().map(groupKey)) : null;
  const groups = data.groups.filter((g) => !scoped || !g.grouped || scoped.has(`${g.subscription_id}/${g.name}`));
  if (state.costGroup === "group") return groups.map((g) => ({ ...g, key: g.grouped ? `${g.subscription_id}/${g.name}` : null }));
  const tag = $("cost-tag").value;
  const tagsOf = new Map(state.groups.map((g) => [groupKey(g), g.tags || {}]));
  // Every group with the tag in the subscriptions shown, costs or not: a group that cost nothing still belongs to its value.
  const shownSubs = new Set(data.subscriptions.map((s) => s.subscription_id));
  const tagged = {};
  for (const g of state.groups) {
    if (shownSubs.has(g.subscription_id) && (!scoped || scoped.has(groupKey(g)))) {
      const value = (g.tags || {})[tag] || "(no tag)";
      tagged[value] = (tagged[value] || 0) + 1;
    }
  }
  const out = new Map();
  for (const g of groups) {
    const value = g.grouped ? (tagsOf.get(`${g.subscription_id}/${g.name}`) || {})[tag] || "(no tag)" : SUBSCRIPTION_LEVEL;
    const row = out.get(value) || { name: value, count: 0, month_to_date: 0, last_month: 0, last_month_same_days: 0, booked: 0, last_30_days: 0,
      last_7_days: 0, previous_7_days: 0, change: 0, trend: g.trend.map(() => 0) };
    row.count += 1;
    if (g.spike) row.spikes = [...(row.spikes || []), g];
    for (const k of ["month_to_date", "booked", "last_30_days", "last_7_days", "previous_7_days", "change"]) row[k] += g[k] || 0;
    for (const k of ["last_month", "last_month_same_days"]) row[k] = row[k] == null || g[k] == null ? null : row[k] + g[k];
    g.trend.forEach((v, i) => { row.trend[i] += v; });
    out.set(value, row);
  }
  return [...out.values()].map((row) => ({ ...row, tagged: row.name === SUBSCRIPTION_LEVEL ? row.count : tagged[row.name] || row.count }));
}

function renderCost() {
  if (!state.cost) return;
  const data = costView(state.cost);
  const all = state.cost, unreadable = (all.refused || []).length + (all.failed || []).length;
  $("cost-meta").innerHTML = `${esc(currentScope().label)} · ${all.subscriptions.length} of ${all.total || all.subscriptions.length} subscription(s)${
    unreadable ? ` (${unreadable} not readable)` : ""}${data.done ? ` · ${asOfHtml(data.read_at)}` : ""}${data.done && data.updating
    ? ` · <span class="cost-updating">updating ${data.updating} older than 6 h in the background (Refresh in a few minutes)</span>` : ""}`;
  const single = data.currencies.length <= 1;
  const cur = data.currency;
  const sum = (k) => data.subscriptions.reduce((n, s) => n + s[k], 0);
  for (const b of $("cost-group").querySelectorAll("[data-cost-group]")) b.classList.toggle("active", b.dataset.costGroup === state.costGroup);
  const keys = Object.keys(tagCounts(state.groups)).sort(byName);
  const kept = $("cost-tag").value;
  // Tag keys can be any length: each shows at most TAG_KEY_CHARS, the full key is its value and its tooltip.
  $("cost-tag").innerHTML = keys.map((k) => `<option value="${esc(k)}" title="${esc(k)}" ${k === kept ? "selected" : ""}>${esc(shortTag(k))}</option>`).join("");
  $("cost-tag").title = $("cost-tag").value;
  $("cost-tag").classList.toggle("hidden", state.costGroup !== "tag");
  const amount = (v) => `<span class="unit">${esc(cur)}</span>${esc(moneyText(v))}`;
  const sameDays = costSameDays(data);
  const pending = data.subscriptions.filter((s) => s.last_month_same_days == null).length;  // last month still being read
  $("cost-counters").innerHTML = single ? [
    ["This month", amount(sum("month_to_date")), pending ? "reading last month…"
      : sameDays ? `${costDeltaHtml(sum("month_to_date"), sum("last_month_same_days")) || "–"} vs ${esc(sameDays)}` : ""],
    ["Last 30 days", amount(sum("last_30_days")), ""],
    ["Last 7 days", amount(sum("last_7_days")), `${costDeltaHtml(sum("last_7_days"), sum("previous_7_days")) || "–"} vs the 7 days before`],
    ["Spikes", esc(String(data.spikes.length)), ""]].map(([label, n, extra]) => `<div class="counter ${label === "Spikes" && data.spikes.length ? "warning" : "info"}">
      <span class="n">${n}</span><span class="label">${esc(label)}</span>${extra ? `<br><span class="label">${extra}</span>` : ""}</div>`).join("") : "";
  const notes = [
    !single ? `These subscriptions bill in different currencies (${esc(data.currencies.join(", "))}), so nothing is added up across them.` : "",
    data.refused.length ? `Costs aren't readable in ${data.refused.length} subscription(s): ${esc(data.refused.map(subName).join(", "))}. ${esc(COST_ROLE_FIX)}` : "",
    data.failed.length ? `Azure didn't answer for ${data.failed.length} subscription(s) (${esc(data.failed.map((f) => `${subName(f.subscription_id)}: ${f.error}`).join("; "))}); Refresh tries again.` : "",
    currentScope().groups && state.costGroup === "subscription" ? "A tag filter is applied: subscription totals are whole subscriptions; group by resource group to see only the groups in scope." : ""]
    .filter(Boolean).map((t) => `<p class="notice partial">${t}</p>`).join("");
  const text = $("cost-filter").value.trim().toLowerCase();
  const sort = $("cost-sort").value;
  const rows = costRows(data).filter((r) => !text || r.name.toLowerCase().includes(text) || (r.subscription_id && subName(r.subscription_id).toLowerCase().includes(text)));
  const monthRise = (r) => (r.last_month_same_days == null ? 0 : r.month_to_date - r.last_month_same_days);
  rows.sort((a, b) => (sort === "fall" ? a.change - b.change : sort === "month_rise" ? monthRise(b) - monthRise(a) : (b[sort] || 0) - (a[sort] || 0)));
  const money = (v, r) => moneyCell(v, single ? cur : (data.subscriptions.find((s) => s.subscription_id === r.subscription_id) || {}).currency || "");
  const nameCell = (r) => {  // the ⚠ goes right after the name, before a group's subscription line
    const html = nameOnly(r), at = html.indexOf('<div class="muted">');
    return at < 0 ? html + spikeMark(r) : html.slice(0, at) + spikeMark(r) + html.slice(at);
  };
  const nameOnly = (r) => (state.costGroup === "subscription"
    ? `<button type="button" class="link" data-cost-sub-open="${esc(r.subscription_id)}">${esc(r.name)}</button> ${portalLinkHtml(`/subscriptions/${r.subscription_id}`, { label: "↗", page: "costanalysis", title: "Cost analysis in the Azure portal" })}`
    : state.costGroup === "group" ? `${r.key ? `<button type="button" class="link" data-cost-group-open="${esc(r.key)}">${esc(r.name)}</button>`
      : `<span title="${esc(SUBSCRIPTION_LEVEL_HELP)}">${esc(r.name)}</span>`}<div class="muted">${esc(subName(r.subscription_id))}</div>`
      : r.name === SUBSCRIPTION_LEVEL ? `<strong title="${esc(SUBSCRIPTION_LEVEL_HELP)}">${esc(r.name)}</strong>`
        : `<strong>${esc(r.name)}</strong> <span class="muted">${r.tagged} group(s)${r.count < r.tagged ? `, ${r.count} with costs` : ""}</span>`);
  const spikeCell = (r) => (r.spike ? `<span class="worse">${esc(moneyText(r.spike.cost, cur))}</span> <span class="muted">on ${esc(r.spike.date)}, usually ${esc(moneyText(r.spike.usual, cur))}</span>` : "");
  // A row with a spike (its own, or a group's in it) gets a ⚠ whose hover says which; the Spikes card above lists them all.
  const spikesIn = (r) => (r.spike ? [r] : state.costGroup === "subscription" ? data.spikes.filter((g) => g.subscription_id === r.subscription_id) : r.spikes || []);
  const spikeMark = (r) => {
    const found = spikesIn(r);
    return found.length ? ` <span class="worse" title="${esc(found.map((g) => `Spike in ${g.name}: ${moneyText(g.spike.cost, cur)} on ${g.spike.date}, usually ${moneyText(g.spike.usual, cur)}`).join("\n"))}">⚠</span>` : "";
  };
  const columns = ["", "This month", "Last month", "Last 30 days", "Last 7 days", "Change"];
  columns[0] = { subscription: "Subscription", group: "Resource group", tag: $("cost-tag").value || "Tag" }[state.costGroup];
  const spikes = data.spikes.length ? `<div class="card warning"><h3>Spikes <span class="count">${data.spikes.length}</span></h3>
    <p class="desc">Resource groups whose cost two days ago (the latest full day) was at least twice their usual day.</p>
    <ul>${data.spikes.slice(0, 10).map((g) => `<li class="static">${pill("warning")} <strong>${esc(g.name)}</strong> <span class="muted">${esc(subName(g.subscription_id))}</span>
      <div>${spikeCell(g)}</div></li>`).join("")}</ul></div>` : "";
  // All of last month, and this month's change against the same days of it (a half month against a whole one would always fall).
  const sameCell = (r) => (r.last_month == null ? '<span data-v="0" class="muted" title="The rest of last month is being read in the background">reading…</span>'
    : `<span data-v="${monthRise(r)}">${money(r.last_month, r)}</span>${sameDays ? `<div class="muted" title="This month so far against ${esc(sameDays)}: ${esc(moneyText(r.last_month_same_days, cur))}">${
      costDeltaHtml(r.month_to_date, r.last_month_same_days) || "–"} vs ${esc(sameDays)}</div>` : ""}`);
  $("cost-body").innerHTML = `${notes}${spikes}${rows.length ? table(columns, rows.map((r) => [nameCell(r), money(r.month_to_date, r), sameCell(r), money(r.last_30_days, r),
    money(r.last_7_days, r), `<span data-v="${r.change}">${costDeltaHtml(r.last_7_days, r.previous_7_days) || "–"}</span>`]), `cost-${state.costGroup}`)
    : data.read ? '<p class="muted">Nothing matches.</p>' : ""}
    <p class="muted">${esc(COST_KIND[data.cost_type || state.costType])}. This month is compared with the same days of last month;
      purchases such as reservations aren't counted as spikes.</p>
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock(data)}</details>`;
  if (state.view === "summary") renderSummaryCost(state.cost);
}

async function loadWaste() {
  const scope = currentScope();
  state.wasteLoaded = JSON.stringify(scope);
  if (!scope.subscriptions.length) { $("waste-body").innerHTML = pickScopeHtml("Billed while idle reads the resources in scope."); return; }
  $("waste-body").innerHTML = loading("Looking for resources billed while idle…");
  let data;
  try { data = await api("api/cost/waste", { params: scopeParams(scope) }); }
  catch (error) { state.wasteLoaded = ""; $("waste-body").innerHTML = noticeHtml(error); return; }
  if (data.error) { $("waste-body").innerHTML = noticeHtml(data.error); return; }
  state.waste = data;
  renderWaste();
}

function renderWaste() {
  const data = state.waste;
  const counts = {};
  for (const r of data.rows) counts[r.finding] = (counts[r.finding] || 0) + 1;
  const body = (part) => `<p class="muted">${esc(part[0].action)}</p>${table(["Resource", "Where", "Detail"], part.map((r) => [
    `<button type="button" class="link" data-open="${esc(JSON.stringify({ kind: "resource", id: r.id, name: r.name }))}">${esc(r.name)}</button> ${portalLinkHtml(r.id)}`,
    `${esc(r.resource_group)}<div class="muted">${esc(subName(r.subscription_id))}${r.location ? ` · ${esc(r.location)}` : ""}</div>`, esc(r.detail)]), "cost-waste")}`;
  $("waste-body").innerHTML = `${data.rows.length ? `<p class="muted">${data.rows.length.toLocaleString()} resource(s). Checked: ${esc(data.checks.join("; "))}.</p>
    ${groupedHtml("waste", data.rows, { groupOf: (r) => ({ key: r.finding, label: r.finding }), body,
      order: (a, b) => b.rows.length - a.rows.length || byName(a.label, b.label) })}`
    : `<p class="ok">✓ Nothing billed while idle. Checked: ${esc(data.checks.join("; "))}.</p>`}
    <details class="mon-query"><summary>Query sent</summary>${queryBlock(data)}</details>`;
}

$("cost-subtabs").addEventListener("click", (event) => { const b = event.target.closest("[data-cost-sub]"); if (b) showCostSub(b.dataset.costSub); });
$("cost-group").addEventListener("click", (event) => {
  const b = event.target.closest("[data-cost-group]");
  if (!b) return;
  state.costGroup = b.dataset.costGroup;
  stored.set("ahc.costGroup", state.costGroup);
  renderCost();
});
$("cost-type").addEventListener("click", (event) => {
  const b = event.target.closest("[data-cost-type]");
  if (!b || b.dataset.costType === state.costType) return;
  state.costType = b.dataset.costType;
  stored.set("ahc.costType", state.costType);
  for (const key of [...exploreCache.keys()]) if (key.split("|")[1] === "cost") exploreCache.delete(key);
  loadCost();
});
for (const id of ["cost-tag", "cost-sort", "cost-sub"]) $(id).addEventListener("change", renderCost);
$("cost-filter").addEventListener("input", debounce(renderCost, 150));
$("cost-refresh").addEventListener("click", () => freshly(() => (state.costSub === "waste" ? loadWaste() : loadCost())));
$("cost-progress").addEventListener("click", async (event) => {
  if (event.target.id !== "cost-cancel" || !state.costJob) return;
  try { await api(`api/jobs/${state.costJob}/cancel`, { method: "POST", body: {} }); } catch { /* already finished */ }
});
for (const id of ["cost-body", "waste-body"]) {
  $(id).addEventListener("click", (event) => {
    const open = event.target.closest("[data-open]");
    if (open) { startSnapshot(JSON.parse(open.dataset.open)); return; }
    const sub = event.target.closest("[data-cost-sub-open]");
    if (sub) { showView("browse"); showSubscriptionSummary(sub.dataset.costSubOpen); return; }
    const group = event.target.closest("[data-cost-group-open]");
    if (group) { showView("browse"); costCardClick(event); }
  });
}
for (const fmt of ["md", "csv"]) {
  $(`cost-export-${fmt}`).addEventListener("click", async () => {
    if (!state.costJob) return;
    try { await download(`api/cost/${state.costJob}/export`, { format: fmt }, `costs.${fmt}`); }
    catch (error) { $("cost-body").insertAdjacentHTML("afterbegin", noticeHtml(error)); }
  });
}

// ------------------------------------------------------------------ security
// Sub-tabs: Exposure & configuration (core/security.py, one read for the scope) and Vulnerabilities (Defender's CVEs).

// Browse's chip for a resource open to all networks.
const exposureChip = (e) => (e ? `<span class="exp-chip ${esc(e.level)}" title="${esc(e.what)}">public</span>` : "");

const SEC_SUBS = ["config", "vulnerabilities"];
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
  if (sub === "config" && state.secLoaded !== JSON.stringify(currentScope())) loadSecurity();
  if (sub === "vulnerabilities" && state.vulnLoaded !== JSON.stringify(currentScope())) loadVulnerabilities();
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
      <a href="${esc(portal)}" target="_blank" rel="noopener">Open in portal</a> ${exportButtons}</div>
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
      <td><button type="button" class="link" data-remove-watch="${esc(item.id)}">remove</button></td></tr>`;
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

// ------------------------------------------------------------------ settings


async function loadSettings() {
  $("settings-message").innerHTML = "";
  loadIntegrations();
  renderAlertRules();
  $("settings-body").innerHTML = loading("Reading settings…");
  try {
    state.settings = await api("api/settings");
  } catch (error) {
    $("settings-body").innerHTML = noticeHtml(error);
    return;
  }
  renderSettings();
}

function fieldInput(field) {
  const id = `set-${field.name}`;
  const value = field.value;
  if (field.name === "persona") {  // its choices are the personas, built in and from the file
    const option = (name, label) => `<option value="${esc(name)}" ${name === (value || "") ? "selected" : ""}>${esc(label)}</option>`;
    return `<select id="${esc(id)}">${option("", "Not set: ask on first run")}${(state.personas || []).map((p) => option(p.name, p.label)).join("")}</select>`;
  }
  if (field.type === "bool") return `<input type="checkbox" id="${esc(id)}" ${value ? "checked" : ""}>`;
  if (field.type === "int") return `<input type="number" min="0" step="1" id="${esc(id)}" value="${esc(value)}">`;
  if (Array.isArray(field.type)) {
    const labels = OPTION_LABELS[field.name] || {};
    return `<select id="${esc(id)}">${field.type.map((c) => `<option value="${esc(c)}" ${c === value ? "selected" : ""}>${esc(labels[c] || c)}</option>`).join("")}</select>`;
  }
  return `<input type="text" id="${esc(id)}" value="${esc(value ?? "")}" placeholder="${esc(field.default ?? "(empty)")}">`;
}

function readField(field) {
  const el = $(`set-${field.name}`);
  if (field.type === "bool") return el.checked;
  if (field.type === "int") return el.value === "" ? null : Number(el.value);
  if (Array.isArray(field.type) && typeof field.type[0] === "number") return Number(el.value);
  return el.value;
}

// Plain names for the settings; the config key is shown small beside each, for people editing the file.
const SETTING_LABELS = {
  "triage.scope": "When the page opens", "triage.auto_refresh_minutes": "Refresh Triage by itself", "triage.changes_hours": "Changes shown on problems, looking back (hours)",
  "triage.include_system_changes": "Include changes made by Azure itself", "triage.secret_scan_max_vaults": "Scan Key Vaults automatically up to (vaults)",
  "triage.azure_status_feed": "Read the public Azure status feed", "updates.check": "Tell me when a new version is out",
  "graph.auto": "Draw dependencies on every click", "graph.depth": "Hops to follow", "graph.max_nodes": "Stop drawing at (resources)",
  "graph.inbound": "Also find what points at a resource", "graph.inbound_limit": "At most this many inbound resources",
  "graph.inbound_scope": "Where to look for inbound references",
  "health.window_minutes": "Metric window for \u201cnow\u201d (minutes)", "health.compare_last_week": "Compare with the same window last week",
  "health.changes_hours": "Changes shown per resource (hours)", "health.alert_hours": "Ignore stateless alerts older than (hours)",
  "health.critical_max_severity": "Critical from alert severity (Sev0 is worst)", "health.warning_max_severity": "Warning from alert severity",
  "health.heuristic_limit": "Metrics picked for types without a profile", "health.expiry_warning_days": "Expiry turns yellow (days before)",
  "health.expiry_critical_days": "Expiry turns red (days before)", "health.detectors": "Run Azure's detectors in each check",
  "network.subnet_warning_percent": "Subnet turns yellow at (% of IPs used)", "network.subnet_critical_percent": "Subnet turns red at (% of IPs used)",
  "aks.fetch_credentials": "Fetch the AKS user credential when no kubeconfig matches",
  "summary.top_items": "Items listed per card", "summary.readiness_quick": "Run Readiness's quick checks on open",
  "summary.budget_warn_percent": "Budget turns yellow at (% spent)", "summary.compare_hours": "What changed compares with (hours ago)",
  "summary.keep_days": "Keep summaries for (days)",
  "alert_check.days": "Days of history to read", "alert_check.min_buckets": "Fewest values before a verdict",
  "alert_check.always_on_percent": "Fires on ordinary values at (% of history)", "alert_check.rare_percent": "Unusual at (% of history)",
  "alert_check.change_percent": "A change against last week at (%)",
  persona: "Open as (View as)", default_tenant: "Tenant to open with", watchlist_file: "Watchlist file", profiles_dir: "Health profiles folder",
  exports_dir: "Exports folder", pdf_font: "PDF font (.ttf)",
};
const OPTION_LABELS = {
  "triage.auto_refresh_minutes": { 0: "Off", 1: "Every minute", 2: "Every 2 minutes", 5: "Every 5 minutes", 10: "Every 10 minutes", 15: "Every 15 minutes" },
  "graph.inbound_scope": { subscription: "The same subscription", tenant: "The whole tenant (slower)" },
  persona: { "": "ask on first run" },
};
const PANE_OF = { updates: "general", triage: "general", summary: "general", health: "thresholds", network: "thresholds", alert_check: "thresholds", graph: "graph", aks: "aks", files: "files", persona: "persona" };

function settingRows(fields) {
  return fields.map((f) => {
    const differs = JSON.stringify(f.value) !== JSON.stringify(f.default);
    const shown = OPTION_LABELS[f.name] ? OPTION_LABELS[f.name][f.default] || f.default : f.default;
    return `<div class="set-row field ${differs ? "differs" : ""}" data-name="${esc(f.name)}">
      <label for="set-${esc(f.name)}">${esc(SETTING_LABELS[f.name] || f.name)}</label>
      <div>${fieldInput(f)}</div>
      <div class="help">${esc(f.help)} <span class="muted">Default: ${esc(shown === "" || shown == null ? "empty" : shown)}.</span>
        ${differs ? `<button type="button" class="link" data-reset="${esc(f.name)}">reset</button>` : ""} <code>${esc(f.name)}</code></div></div>`;
  }).join("");
}

// ------------------------------------------------------------------ scopes: Open with and the old favourites
const OPEN_WITH_LABELS = { none: "Nothing until I pick a scope", all: "Every subscription I can read" };

function openWithOptions(current) {
  const names = (state.savedScopes || []).map((s) => s.name);
  const missing = current && !OPEN_WITH_LABELS[current] && !findSavedScope(current) ? [current] : [];
  const option = (value, label) => `<option value="${esc(value)}" ${value.toLowerCase() === String(current || "").toLowerCase() ? "selected" : ""}>${esc(label)}</option>`;
  return [option("none", OPEN_WITH_LABELS.none), option("all", OPEN_WITH_LABELS.all),
    ...names.map((n) => option(n, `Saved scope: ${n}`)), ...missing.map((n) => option(n, `${n} (no longer a saved scope)`))].join("");
}

function savedScopeList(scopes) {
  return (scopes || []).length ? `<ul class="scope-list">${scopes.map((s) => `<li><strong>${esc(s.name)}</strong>
      <span class="muted">${esc(scopeSummary(s))}${s.legacy ? " · from your old favourites" : ""}</span></li>`).join("")}</ul>
    <p class="muted">Rename or delete them in the Scope bar's <strong>Saved scopes</strong> menu.</p>`
    : `<p class="muted">No saved scopes yet. Pick subscriptions, tag filters and groups in the Scope bar, then
      <strong>Saved scopes → Save current as…</strong>.</p>`;
}

// Rule #38: old settings still in use are named with their fix, here and on Triage.
function legacyScopeHtml() {
  const legacy = state.legacyScope;
  if (!legacy) return "";
  const keys = legacy.keys.map((k) => `<code>${esc(k)}</code>`).join(", ");
  const into = legacy.scopes.length ? `the saved scope${legacy.scopes.length === 1 ? "" : "s"} ${legacy.scopes.map((n) => `<strong>${esc(n)}</strong>`).join(" and ")}`
    : "Open with";
  return `<div class="notice">Your config still has ${keys} from before saved scopes. They work for now as ${into}
      (opening with ${esc(OPEN_WITH_LABELS[legacy.open_with] ? legacy.open_with : `"${legacy.open_with}"`)}).
      <button type="button" data-move-legacy>Move them into saved scopes</button>
      <span class="muted">Writes them under this tenant in config.yaml and removes the old keys (a .bak copy is kept).</span></div>`;
}

function scopeNotices() {
  const parts = [];
  if (state.legacyScope) parts.push(legacyScopeHtml());
  if (state.openWithMissing) parts.push(`<p class="notice">Open with is set to "${esc(state.openWithMissing)}", which isn't a saved scope
    any more, so the page opened with nothing. Pick another in Settings → General.</p>`);
  return parts.join("");
}

async function afterScopeSettings(payload, message) {
  applyScopePayload(payload);
  state.openWithMissing = "";
  if (state.settings) {
    Object.assign(state.settings, { saved_scopes: payload.saved_scopes, open_with: payload.open_with, legacy_scope: payload.legacy_scope });
    if (state.view === "settings") renderSettings();
  }
  renderSavedScopes();
  $("triage-notice").innerHTML = scopeNotices();
  flash(message, false, 4000);
}

async function saveOpenWith(value) {
  try {
    await afterScopeSettings(await api("api/open-with", { method: "POST", body: { open_with: value } }),
      `The page now opens with ${{ none: "nothing selected", all: "every subscription" }[value] || `the saved scope ${value}`}.`);
  } catch (error) { showMessage(`Not saved: ${error.message}`, true); }
}

async function moveLegacyScope() {
  try {
    await afterScopeSettings(await api("api/saved-scopes/move-legacy", { method: "POST", body: {} }),
      "Moved: your old favourites and filters are saved scopes now.");
  } catch (error) { showMessage(`Not moved: ${error.message}`, true); }
}

document.addEventListener("click", (event) => {
  if (event.target.closest("#triage-notice [data-move-legacy], #saved-panel [data-move-legacy]")) moveLegacyScope();
});

function renderSettings() {
  const s = state.settings;
  const t = s.tenant;
  $("settings-path").textContent = `${s.path}${s.exists ? "" : " (not created yet; Save creates it)"}`;
  const panes = { general: [], thresholds: [], graph: [], aks: [], files: [] };
  panes.persona = [];
  for (const field of s.fields) panes[PANE_OF[field.name] || PANE_OF[field.section] || "files"].push(field);
  const bySection = (pane, section) => panes[pane].filter((f) => f.section === section);
  $("settings-body").innerHTML = `
    <div class="settings-section" data-pane="general">
      <h3>Persona</h3>${settingRows(panes.persona)}
      <h3>Scopes <span class="muted">for ${esc(t.name)}</span></h3>
      ${legacyScopeHtml()}
      <div class="set-row"><label for="set-open-with">Open with</label>
        <div><select id="set-open-with">${openWithOptions(s.open_with)}</select></div>
        <div class="help">What the page looks at when it opens. <strong>Nothing</strong> queries nothing until you pick a
          scope. Saved as soon as you change it. <code>open_with</code></div></div>
      <div id="settings-scopes">${savedScopeList(s.saved_scopes)}</div>
      <h3>Triage</h3>${settingRows(bySection("general", "triage"))}
      <h3>Summary</h3>${settingRows(bySection("general", "summary"))}
      <h3>Updates</h3>${settingRows(bySection("general", "updates"))}
    </div>
    <div class="settings-section" data-pane="thresholds">
      <h3>Health</h3>${settingRows(bySection("thresholds", "health"))}
      <h3>Network</h3>${settingRows(bySection("thresholds", "network"))}
      <h3>Is this alert right?</h3>${settingRows(bySection("thresholds", "alert_check"))}
    </div>
    <div class="settings-section" data-pane="graph"><h3>Dependency graph</h3>${settingRows(panes.graph)}</div>
    <div class="settings-section" data-pane="aks"><h3>AKS</h3>${settingRows(panes.aks)}
      <p class="muted">Kubeconfig files read besides the usual ones: ${esc((s.kubeconfig_paths || []).join(", ") || "none")}.
        Lists like this are edited in the file (<code>aks.kubeconfig_paths</code>), then Reload from file.</p></div>
    <div class="settings-section" data-pane="files"><h3>Files</h3>${settingRows(panes.files)}
      <h3>Watchlist</h3><div id="settings-watchlist"></div></div>`;
  state.settingsResets = new Set();
  renderSettingsWatchlist();
  showSettingsPane(state.settingsPane || "general");
  $("settings-save").disabled = true;
  $("settings-bar").classList.add("hidden");
}

function showSettingsPane(pane) {
  state.settingsPane = pane;
  for (const el of document.querySelectorAll(".settings-main [data-pane]")) el.classList.toggle("hidden", el.dataset.pane !== pane);
  for (const b of document.querySelectorAll("#settings-nav [data-pane]")) b.classList.toggle("active", b.dataset.pane === pane);
}

$("settings-nav").addEventListener("click", (event) => {
  const b = event.target.closest("[data-pane]");
  if (b) showSettingsPane(b.dataset.pane);
});

function renderSettingsWatchlist() {
  const box = $("settings-watchlist");
  if (!box) return;
  const items = state.watchItems || [];
  box.innerHTML = `<p class="muted">Saved in <code>${esc(state.watchlistPath || "")}</code>. Add with <strong>+ Watch</strong>
      next to any resource or resource group (Triage, search, Browse, Secrets, a resource's details), or from the Watchlist tab.</p>
    ${items.length ? `<table>${items.map((item) => `<tr><td><strong>${esc(item.name)}</strong>
        <div class="muted">${esc(item.kind)}</div></td><td>${watchButton(item)}</td></tr>`).join("")}</table>`
      : '<p class="muted">Nothing on the Watchlist yet.</p>'}`;
}

function settingsChanges() {
  const s = state.settings;
  const values = {};
  for (const field of s.fields) {
    if (state.settingsResets.has(field.name)) { values[field.name] = "__default__"; continue; }
    const now = readField(field);
    if (now !== field.value && !(now === "" && field.value == null)) values[field.name] = now;
  }
  return values;
}

function markSettingsDirty() {
  const values = settingsChanges();
  document.querySelectorAll("#settings-body .field").forEach((el) => el.classList.toggle("changed", el.dataset.name in values));
  const names = Object.keys(values).map((n) => SETTING_LABELS[n] || n);
  $("settings-save").disabled = !names.length;
  $("settings-bar").classList.toggle("hidden", !names.length);
  $("settings-changes").textContent = `${names.length} unsaved change${names.length === 1 ? "" : "s"}: ${names.join(", ")}`;
}

$("settings-discard").addEventListener("click", () => { renderSettings(); $("settings-message").innerHTML = ""; });

$("settings-body").addEventListener("input", markSettingsDirty);
$("settings-body").addEventListener("change", (event) => {
  if (event.target.id === "set-open-with") { saveOpenWith(event.target.value); return; }
  markSettingsDirty();
});
$("settings-body").addEventListener("click", (event) => {
  if (event.target.closest("[data-move-legacy]")) { moveLegacyScope(); return; }
  const name = event.target.dataset && event.target.dataset.reset;
  if (!name) return;
  const field = state.settings.fields.find((f) => f.name === name);
  state.settingsResets.add(name);
  const el = $(`set-${name}`);
  if (field.type === "bool") el.checked = !!field.default; else el.value = field.default ?? "";
  markSettingsDirty();
});

$("settings-save").addEventListener("click", async () => {
  const values = settingsChanges();
  $("settings-save").disabled = true;
  $("settings-message").innerHTML = loading("Saving…");
  try {
    const result = await api("api/settings", { method: "POST", body: { values } });
    state.settings = result.settings;
    await loadPersonas();  // a new Open as applies now, not only at the next start
    renderSettings();
    $("settings-message").innerHTML = `<p class="message">Saved and applied. The previous file is kept as ${esc(result.backup)}.</p>`;
  } catch (error) {
    $("settings-message").innerHTML = `<p class="notice">Not saved: ${esc(error.message)}</p>`;
    markSettingsDirty();
  }
});
$("settings-reload").addEventListener("click", async () => {
  $("settings-message").innerHTML = loading("Reloading…");
  try {
    state.settings = await api("api/settings/reload", { method: "POST", body: {} });
    await loadPersonas();
    renderSettings();
    $("settings-message").innerHTML = '<p class="message">Reloaded from the file and applied.</p>';
    await loadContextForTenant();
  } catch (error) {
    $("settings-message").innerHTML = noticeHtml(error);
  }
});

// ------------------------------------------------------------------ ServiceNow
// Active incidents, changes now or in the next 24 hours, and Event Management alerts and events, each with the
// Azure resources it names. Shown only when ServiceNow is turned on; when it can't be read, the page says why.

const SN_PRIORITY_TONE = { 1: "critical", 2: "soon", 3: "warning" };
const SN_CONFIDENCE = { exact: "exact match (Azure ID)", name: "matched by name", possible: "possible match (short or common name)" };

function snPill(record) {
  const tone = SN_PRIORITY_TONE[record.priority] || "info";
  return pillHtml(tone, `P${record.priority}`, { rank: record.priority, title: record.priority_label });
}

// "3 h 2 min ago" with the exact time on hover; a list sorts it by the time itself.
function agoHtml(when) {
  if (when == null || when === "") return "–";
  const epoch = typeof when === "number" ? when : new Date(when).getTime() / 1000;
  if (Number.isNaN(epoch)) return esc(when);
  return `<span data-v="${epoch}" title="${esc(new Date(epoch * 1000).toLocaleString())}">${esc(ago(when))}</span>`;
}

// `when` is an ISO string (Azure, ServiceNow) or epoch seconds (the alert runner, on the server's clock).
function ago(when) {
  if (when == null || when === "") return "";
  const epoch = typeof when === "number" ? when : new Date(when).getTime() / 1000;
  if (Number.isNaN(epoch)) return String(when);
  const seconds = serverNow() - epoch;
  if (seconds < 0) return `in ${duration(-seconds)}`;
  if (seconds < 45) return "just now";
  if (seconds < 48 * 3600) return `${duration(seconds)} ago`;
  return new Date(epoch * 1000).toLocaleDateString();
}

const MATCH_VIA = { "configuration item": "from its CI", "affected CI": "from an affected CI", "change task": "from a change task" };

function resourceChips(matches, limit = 4) {
  if (!matches.length) return '<span class="muted">none found</span>';
  const chips = matches.slice(0, limit).map((m) => {
    const isGroup = m.display_type === "Resource group";
    const open = isGroup ? { kind: "group", group: `${m.subscription_id}/${m.name}`, name: m.name }
      : { kind: "resource", id: m.id, name: m.name };
    return `<button type="button" class="sn-res ${esc(m.confidence)}" data-open="${esc(JSON.stringify(open))}"
      title="${esc(`${m.display_type} in ${m.resource_group} · ${SN_CONFIDENCE[m.confidence]}${MATCH_VIA[m.via] ? `, ${MATCH_VIA[m.via]}` : ""}`)}">${esc(m.name)}</button>`;
  }).join("");
  return chips + (matches.length > limit ? ` <span class="muted">+${matches.length - limit}</span>` : "");
}

function snUnavailable(info) {
  return `<div class="notice partial"><strong>${esc(info.reason)}</strong>
    ${(info.problems || []).length ? `<ul>${info.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    ${info.fix ? `<p>${esc(info.fix)} <button type="button" class="link" data-goto="settings">Open Settings</button></p>` : ""}</div>`;
}

async function loadServiceNow(refresh = false) {
  const scope = currentScope();
  const token = (state.snToken = (state.snToken || 0) + 1);
  state.snLoaded = true;
  if (!scope.subscriptions.length) {
    $("sn-body").innerHTML = pickScopeHtml("ServiceNow shows the incidents, changes and alerts for the resources in scope.");
    return;
  }
  $("sn-body").innerHTML = loading("Reading ServiceNow and matching records to Azure resources…");
  try {
    const params = { ...scopeParams(scope), connection: state.selected.servicenow || "" };
    if (refresh) params.refresh = "1";
    const data = await api("api/servicenow", { params });
    if (token !== state.snToken) return;
    state.sn = data;
    renderServiceNow();
  } catch (error) {
    if (token === state.snToken && !error.signedOut) $("sn-body").innerHTML = noticeHtml(error);
  }
}

// "Names a resource in scope" means an exact or name match; a possible match (a short or plain name) can be chance.
const namesResource = (r) => r.matches.some((m) => m.confidence !== "possible");

function snVisible(records) {
  const text = $("sn-filter-text").value.trim().toLowerCase();
  const scopeOnly = $("sn-scope-only").checked;
  return records.filter((r) => (!scopeOnly || namesResource(r))
    && (!text || [r.number, r.title, r.description, r.group, r.assigned_to, r.ci, ...r.matches.map((m) => m.name)]
      .some((v) => (v || "").toLowerCase().includes(text))));
}

// A header click sorts (the shared list helper); its own order is ServiceNow's: priority, then most recently updated.
function snHead(columns) {
  return `<thead><tr>${columns.map(([, label]) => `<th>${label}</th>`).join("")}</tr></thead>`;
}

// Why a table wasn't read: a warning where its list would be (a ✓ there would claim it was read and empty).
function snNote(key, label) {
  const note = (state.sn.notes || {})[key];
  return note ? noticeHtml(label ? `${label}: ${note}` : note) : "";
}

function renderServiceNow() {
  const data = state.sn;
  if (!data) return;
  if (!data.available) {
    $("sn-meta").textContent = "";
    $("sn-body").innerHTML = snUnavailable(data);
    return;
  }
  const maxPriority = Number($("sn-priority").value || 9);
  const incidents = snVisible(data.incidents).filter((r) => r.priority <= maxPriority);
  const changes = snVisible(data.changes);
  const alerts = snVisible(data.alerts);
  const events = snVisible(data.events);
  const host = data.instance.replace(/^https:\/\//, "");
  const matched = data.incidents.filter(namesResource).length;
  $("sn-meta").innerHTML = `${esc(`${data.name} · ${host} · read ${ago(data.fetched_at)} · ${data.incidents.length} active incident(s), ${matched} naming Azure resources in scope`)}${toolLink(data.instance, "ServiceNow")}`;
  const scopeOnly = $("sn-scope-only").checked;
  const hidden = (all, shown, what) => (scopeOnly && all.length > shown.length
    ? `<p class="muted">${all.length - shown.length} other active ${what} don't name any Azure resource in scope.
        <button type="button" class="link" data-show-all>Show them</button></p>` : "");
  // What a list leaves out: the cap (a setting), and what ServiceNow's read rules hid of what was asked for.
  const truncated = (key, label, setting = "Most records per query") => {
    const total = (data.totals || {})[key], cap = (data.caps || {})[key] || data.limit, hidden = (data.hidden || {})[key];
    const capped = data.truncated[key] ? `Only the first ${cap.toLocaleString()}${total ? ` of ${total.toLocaleString()}` : ""} ${label} were read: `
      + `narrow the filter or raise "${setting}" in Settings → Integrations → ServiceNow → What to read.` : "";
    const hid = hidden ? `ServiceNow's read rules hid ${hidden.toLocaleString()} of ${data.truncated[key] ? `those ${cap.toLocaleString()}`
      : total.toLocaleString()} ${label} from ${data.who}: the list shows only what it can read.` : "";
    return [capped, hid].filter(Boolean).map((t) => noticeHtml(t)).join("");
  };
  const cmdb = (r) => (r.cmdb_note ? `<div class="sn-cmdb" title="The CMDB doesn't map this Azure resource">⚠ CMDB: ${esc(r.cmdb_note)}</div>` : "");
  const link = (r) => `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.number || "open")}</a>`;
  // The same table and query as a ServiceNow list: it runs as you there, so it shows what your own access can read.
  const inSn = (...lists) => {
    const found = lists.filter(([key]) => (data.lists || {})[key]).map(([key, label]) => ({ url: data.lists[key], label }));
    return found.length ? ` <span class="h-links" title="The same query in ServiceNow, read with your own access">${linksHtml(found)}</span>` : "";
  };
  $("sn-body").innerHTML = `
    <h3>Incidents <span class="muted">(${incidents.length})</span>${inSn(["incident", "Open this list in ServiceNow"])}</h3>
    ${snNote("incident", "Incidents")}${truncated("incident", "incidents", "Incidents: most records")}${hidden(data.incidents.filter((r) => r.priority <= maxPriority), incidents, "incident(s)")}
    ${incidents.length ? `<table class="data-table" data-list="sn-incident">${snHead([["priority", "Priority"], ["number", "Number"],
      ["title", "Short description"], ["state", "State"], ["group", "Assignment group"], ["opened_at", "Opened"],
      ["matches", "Azure resources it names"]])}<tbody>${incidents.map((r) => `<tr>
      <td>${snPill(r)}</td><td>${link(r)}</td><td>${esc(r.title)}${cmdb(r)}</td><td>${esc(r.state)}</td>
      <td>${esc(r.group)}${r.assigned_to ? `<div class="muted">${esc(r.assigned_to)}</div>` : ""}</td>
      <td>${agoHtml(r.opened_at)}</td><td>${resourceChips(r.matches)}</td></tr>`).join("")}</tbody></table>`
      : snNote("incident", "") ? "" : `<p class="ok">✓ No active incidents${scopeOnly ? " name an Azure resource in scope" : " match these filters"}.</p>`}
    <h3>Changes: ${esc(snChangeSpan(data))} <span class="muted">(${changes.length})</span>${inSn(["change_request", "Open changes in ServiceNow"], ["change_closed", "Closed ones"])}</h3>
    ${snNote("change_request", "Change requests")}${truncated("change_request", "open changes", "Changes: most records")}
    ${truncated("change_closed", "closed changes", "Changes: most records")}${hidden(data.changes, changes, "change(s)")}
    ${changes.length ? `<table class="data-table" data-list="sn-change">${snHead([["window", "When"], ["number", "Number"],
      ["title", "Short description"], ["state", "State"], ["outcome", "Outcome"], ["start", "Window"], ["group", "Assignment group"],
      ["matches", "Azure resources it names"]])}<tbody>${changes.map((r) => `<tr>
      <td>${pillHtml(SN_CHANGE_TONE[r.window] || "info", r.window)}</td><td>${link(r)}</td>
      <td>${esc(r.title)}${snChangeParts(r) ? `<div class="muted">${esc(snChangeParts(r))}</div>` : ""}${cmdb(r)}</td><td>${esc(r.state)}</td>
      <td>${r.outcome ? `<span class="${/unsuccess/i.test(r.outcome) ? "worse" : ""}">${esc(r.outcome)}</span>` : '<span class="muted">–</span>'}</td>
      <td>${r.start ? `${esc(new Date(r.start).toLocaleString())} → ${esc(r.end ? new Date(r.end).toLocaleString() : "?")}` : '<span class="muted">no dates</span>'}</td>
      <td>${esc(r.group)}</td><td>${resourceChips(r.matches)}</td></tr>`).join("")}</tbody></table>`
      : snNote("change_request", "") ? "" : `<p class="ok">✓ No changes in ${esc(snChangeSpan(data))}.</p>`}
    ${snNote("change_closed", "Closed changes")}${snNote("task_ci", "Affected CIs")}${snNote("change_task", "Change tasks")}
    <h3>Event Management alerts <span class="muted">(${alerts.length})</span>${inSn(["em_alert", "Open this list in ServiceNow"])}</h3>
    ${snNote("em_alert", "Alerts")}
    ${alerts.length ? snEventTable("alert", alerts) : snNote("em_alert", "") ? "" : '<p class="ok">✓ No open alerts.</p>'}
    <h3>Event Management events, last 24 hours <span class="muted">(${events.length})</span>${inSn(["em_event", "Open this list in ServiceNow"])}</h3>
    ${snNote("em_event", "Events")}
    ${events.length ? snEventTable("event", events) : snNote("em_event", "") ? "" : '<p class="ok">✓ No events.</p>'}
    <p class="muted">Matching: an Azure resource ID on the record's configuration item is an exact match; a distinctive resource
      name in ${esc("the record's text")} is a name match; short or common names are only "possible". ⚠ CMDB marks records
      whose configuration item doesn't map the resource they name.</p>`;
}

function snEventTable(table, records) {
  return `<table class="data-table" data-list="sn-${table}">${snHead([["severity", "Severity"], ["number", "Number"], ["title", "Description"],
    ["source", "Source"], ["state", "State"], ["updated_at", "Updated"], ["matches", "Azure resources it names"]])}<tbody>${
    records.map((r) => `<tr>
    <td>${esc(r.severity)}</td><td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.number || "open")}</a></td>
    <td>${esc(r.title)}</td><td>${esc(r.source)}</td><td>${esc(r.state)}</td><td>${agoHtml(r.updated_at)}</td>
    <td>${resourceChips(r.matches)}</td></tr>`).join("")}</tbody></table>`;
}

$("sn-body").addEventListener("click", (event) => {
  if (event.target.dataset && event.target.dataset.showAll !== undefined) {
    $("sn-scope-only").checked = false;
    rememberScopeOnly();
    renderServiceNow();
    return;
  }
  const chip = event.target.closest("[data-open]");
  if (chip) { startSnapshot(JSON.parse(chip.dataset.open)); return; }
  if (event.target.dataset && event.target.dataset.goto) showView(event.target.dataset.goto);
});
$("sn-refresh").addEventListener("click", () => loadServiceNow(true));
for (const id of ["sn-filter-text", "sn-priority", "sn-scope-only"]) $(id).addEventListener("input", debounce(renderServiceNow, 150));

// Scope-only is the default (a real instance holds every team's incidents) and the choice is remembered.
// Collapsed to one line by default; opening or closing it is remembered for every snapshot.
function serviceHealthHtml(events) {
  const open = stored.get("ahc.serviceHealthOpen") === "1";
  const issues = events.filter((e) => e.type === "ServiceIssue").length;
  const parts = [issues && `${issues} service issue${issues === 1 ? "" : "s"}`,
    events.length - issues && `${events.length - issues} planned maintenance`].filter(Boolean).join(", ");
  const ids = (e) => (e.tracking_ids || []).length > 1 ? ` <span class="muted">${e.tracking_ids.length} events: ${esc(e.tracking_ids.join(", "))}</span>`
    : (e.tracking_ids || []).length ? ` <span class="muted">${esc(e.tracking_ids[0])}</span>` : "";
  return `<details ${open ? "open" : ""}>
    <summary><strong>Active Service Health in these regions:</strong> ${esc(parts)}</summary><ul>${
    events.map((e) => `<li><b>${esc(e.type)}</b>: ${esc(e.title)} (${esc(e.services.join(", "))})${ids(e)}</li>`).join("")}</ul></details>`;
}

$("service-health").addEventListener("toggle", (event) => {
  stored.set("ahc.serviceHealthOpen", event.target.open ? "1" : "0");
}, true);

function rememberScopeOnly() {
  stored.set("ahc.snScopeOnly", $("sn-scope-only").checked ? "1" : "0");
}
$("sn-scope-only").addEventListener("change", rememberScopeOnly);
$("sn-scope-only").checked = stored.get("ahc.snScopeOnly") !== "0";

// Browse → a resource → ServiceNow: the records that name it (or, for a resource group, anything in it).
async function tabServiceNow(node, body) {
  const data = await exploreGet("servicenow", node, { connection: state.selected.servicenow || "" });
  if (!data.available) { body.innerHTML = snUnavailable(data); return; }
  const rows = [...data.incidents.map((r) => ({ ...r, what: "Incident" })), ...data.changes.map((r) => ({ ...r, what: `Change (${r.window})` })),
    ...data.alerts.map((r) => ({ ...r, what: "Alert" })), ...data.events.map((r) => ({ ...r, what: "Event" }))];
  const unread = Object.entries(data.notes || {});
  const notes = noticesHtml(unread.map(([tbl, note]) => `${tbl}: ${note}`));
  body.innerHTML = `<p class="muted">Active records in ${esc(data.name)} (${esc(data.instance.replace(/^https:\/\//, ""))}) that name this resource, read
      ${esc(ago(data.fetched_at))}.${toolLink(data.instance, "ServiceNow")}</p>${notes}
    ${rows.length ? table(["Type", "Record", "Description", "State", "How it matched"], rows.map((r) => {
      const m = r.matches.find((x) => x.id.toLowerCase() === node.id.toLowerCase()) || r.matches[0];
      return [esc(r.what) + (r.priority ? ` ${snPill(r)}` : ""), `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.number || "open")}</a>`,
        `${esc(r.title)}${r.ci ? `<div class="muted">CI: ${r.ci_url ? monLink(r.ci_url, r.ci) : esc(r.ci)}</div>` : ""}${
          r.cmdb_note ? `<div class="sn-cmdb">⚠ CMDB: ${esc(r.cmdb_note)}</div>` : ""}`, esc(r.state),
        esc(m ? SN_CONFIDENCE[m.confidence] : "")];
    })) : unread.length ? "" : '<p class="ok">✓ No active incidents, changes, alerts or events name this resource.</p>'}`;
}

// Triage: two cards from ServiceNow, added after the Azure cards (seconds, cached for a minute).
async function loadServiceNowCards(scope) {
  if (!state.snEnabled) return;
  const token = state.triageToken;
  const holder = document.createElement("div");
  holder.className = "card";
  holder.id = "card-servicenow";
  holder.innerHTML = `<h3>ServiceNow</h3>${loading("Reading incidents and changes…")}`;
  $("triage-cards").prepend(holder);
  let data;
  try {
    data = await api("api/servicenow", { params: { ...scopeParams(scope), connection: state.selected.servicenow || "" } });
  } catch (error) {
    if (!error.signedOut) holder.innerHTML = `<h3>ServiceNow</h3>${noticeHtml(error)}`;
    return;
  }
  if (token !== state.triageToken) return;
  if (!data.available) { holder.innerHTML = `<h3>ServiceNow</h3>${snUnavailable(data)}`; return; }
  const allUrgent = data.incidents.filter((r) => r.priority <= 3);
  const allChanges = data.changes;  // in the connection's span: recent, in progress, scheduled
  const urgent = allUrgent.filter(namesResource);
  const changes = allChanges.filter(namesResource);
  const others = (all, shown, what) => (all.length > shown.length ? `<p class="muted">+${all.length - shown.length} other ${what}
    ${all.length - shown.length === 1 ? "doesn't" : "don't"} name anything in scope (ServiceNow tab).</p>` : "");
  const item = (r, extra) => `<li class="static">${extra} <a href="${esc(r.url)}" target="_blank" rel="noopener"><strong>${esc(r.number)}</strong></a>
    ${esc(r.title)}<div class="where">${esc(r.group || "")}${r.matches.length ? " · " : ""}${resourceChips(r.matches, 3)}</div></li>`;
  const card = (key, title, desc, rows, tone, render, extra, note = "") => `<div class="card ${rows.length ? tone : note ? "unknown" : "clear"}" id="card-${key}">
    <h3>${title}<span class="count">${rows.length}</span></h3><p class="desc">${desc}</p>${note ? noticeHtml(note) : ""}
    ${rows.length ? `<ul>${rows.map(render).join("")}</ul>` : note ? "" : '<p class="ok">✓ Nothing naming a resource in scope</p>'}${extra}</div>`;
  const via = ` <span class="muted">· ${esc(data.name)}</span>`;
  holder.outerHTML = card("sn-incidents", `ServiceNow: P1–P3 incidents${via}`, "Active priority 1 to 3 incidents naming Azure resources in scope",
    urgent, urgent.some((r) => r.priority === 1) ? "critical" : "warning", (r) => item(r, snPill(r)),
    others(allUrgent, urgent, "P1–P3 incident" + (allUrgent.length - urgent.length === 1 ? "" : "s")), (data.notes || {}).incident)
    + card("sn-changes", `ServiceNow: changes${via}`, `Change requests in ${snChangeSpan(data)} that name Azure resources in scope (often the cause)`,
      changes, changes.some((r) => ["in progress", "past its window"].includes(r.window)) ? "warning" : "info",
      (r) => item(r, `${pillHtml(SN_CHANGE_TONE[r.window] || "info", r.window)}${r.start ? ` <span class="muted">${esc(dayTime(r.start))}</span>` : ""}`),
      others(allChanges, changes, "change" + (allChanges.length - changes.length === 1 ? "" : "s")),
      (data.notes || {}).change_request);
}

$("triage-cards").addEventListener("click", (event) => {
  const chip = event.target.closest(".sn-res[data-open]");
  if (chip) { event.stopPropagation(); startSnapshot(JSON.parse(chip.dataset.open)); }
}, true);

// ------------------------------------------------------------------ monitoring tools
// One tab, Triage card and resource tab per tool (New Relic, Datadog, Grafana), each showing the one connection
// picked in the Scope bar. Drawn by the same code for every tool: what differs comes from the provider.

const MON_SEVERITY_TONE = { critical: "critical", warning: "warning", ok: "healthy", unknown: "unknown" };
const MON_SEVERITY_LABEL = { critical: "Critical", warning: "Warning", ok: "Not alerting", unknown: "No alerts set" };
const MON_PRIORITY_RANK = { critical: 0, warning: 1, info: 2 };
const MON_CONFIDENCE = { ...SN_CONFIDENCE, exact: "exact match (Azure ID in a tag)", picked: "picked by name" };

const monPill = (severity) => pillHtml(MON_SEVERITY_TONE[severity] || "unknown", MON_SEVERITY_LABEL[severity] || severity);
const issueTone = (i) => (i.priority === "critical" ? "critical" : i.priority === "warning" ? "warning" : "info");
const issuePill = (i) => pillHtml(issueTone(i), i.priority_label || "Issue");
const monLink = (url, text) => (url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text));
// One line per source when several entities share a name (e.g. the Azure and Kubernetes views of one cluster).
const entityType = (e) => ((e.sources || []).length > 1
  ? e.sources.map((s) => `<div class="entity-source">${monLink(s.url, s.type || s.type_class || "entity")} ${monPill(s.severity)}${
    s.reporting ? "" : ' <span class="muted">(not reporting)</span>'}</div>`).join("")
  : `${esc(e.type)}${e.type_class && e.type_class !== e.type ? `<div class="muted">${esc(e.type_class)}</div>` : ""}`);
const connTitle = (c) => [c.name, c.description].filter(Boolean).join(" · ");
const toolLabel = (kind) => MONITOR_LABELS[kind] || kind;
const toolKinds = () => Object.keys(state.connections || {}).filter((k) => k !== "servicenow");
const sortEntities = (rows) => [...rows].sort((a, b) => (({ critical: 0, warning: 1, ok: 2 }[a.severity] ?? 3)
  - ({ critical: 0, warning: 1, ok: 2 }[b.severity] ?? 3)) || a.name.localeCompare(b.name));
const sortIssues = (rows) => [...rows].sort((a, b) => (MON_PRIORITY_RANK[a.priority] ?? 3) - (MON_PRIORITY_RANK[b.priority] ?? 3)
  || String(b.activated_at).localeCompare(String(a.activated_at)));

function toolParams(kind, extra = {}) {
  return { ...extra, connection: (state.selected || {})[kind] || "" };
}

async function loadMonitors(refresh = false) {
  const kind = state.toolKind;
  const scope = currentScope();
  const token = (state.monToken = (state.monToken || 0) + 1);
  state.monLoaded = `${kind}|${state.selected[kind]}`;
  $("mon-title").textContent = toolLabel(kind);
  if (!scope.subscriptions.length) {
    $("mon-body").innerHTML = pickScopeHtml(`${toolLabel(kind)} shows what it reports for the resources in scope.`);
    return;
  }
  $("mon-meta").textContent = "";
  $("mon-body").innerHTML = loading(`Reading ${state.selected[kind] || toolLabel(kind)} and matching what it reports to Azure resources…`);
  try {
    const params = toolParams(kind, scopeParams(scope));
    if (refresh) params.refresh = "1";
    const data = await api("api/monitors", { params });
    if (token !== state.monToken) return;
    state.mon = data;
    renderMonitors();
  } catch (error) {
    if (token === state.monToken && !error.signedOut) $("mon-body").innerHTML = noticeHtml(error);
  }
}

function monVisible(rows, fields) {
  const text = $("mon-filter-text").value.trim().toLowerCase();
  const scopeOnly = $("mon-scope-only").checked;
  return rows.filter((r) => (!scopeOnly || namesResource(r))
    && (!text || [...fields(r), ...r.matches.map((m) => m.name)].some((v) => (v || "").toLowerCase().includes(text))));
}

function renderMonitors() {
  const c = state.mon;
  const kind = state.toolKind;
  renderQueryBar();
  if (!c) return;
  if (!c.available) {
    $("mon-meta").innerHTML = c.name ? esc(connTitle(c)) + toolLink(c.console, c.label) : "";
    $("mon-body").innerHTML = snUnavailable(c);
    return;
  }
  const criticalOnly = $("mon-severity").value === "critical";
  const scopeOnly = $("mon-scope-only").checked;
  $("mon-meta").innerHTML = `${esc(`${connTitle(c)} · read ${ago(c.fetched_at)}`)}${toolLink(c.console, c.label)}`;
  const open = c.issues.filter(namesResource).length + c.entities.filter(namesResource).length;
  $("mon-alert-count").textContent = String(open);
  $("mon-alert-count").classList.toggle("hidden", !open);
  const allIssues = sortIssues(c.issues.filter((i) => !criticalOnly || i.priority === "critical"));
  const allEntities = sortEntities(c.entities.filter((e) => !criticalOnly || e.severity === "critical"));
  const issues = monVisible(allIssues, (i) => [i.title, i.condition, i.policy, ...i.entities]);
  const entities = monVisible(allEntities, (e) => [e.name, e.type, e.account]);
  const hasLogs = (c.capabilities || []).includes("logs");
  const hidden = (all, shown, one, many) => {
    const n = all.length - shown.length;
    return scopeOnly && n > 0 ? `<p class="muted">${n} other ${n === 1 ? one : many} ${n === 1 ? "doesn't" : "don't"} match any Azure
      resource in scope. <button type="button" class="link" data-show-all>Show them</button></p>` : "";
  };
  $("mon-body").innerHTML = `
    ${noticesHtml(c.notes)}
    ${c.truncated ? `<p class="notice">Only part of what's alerting was read (the first entities and open issues ${esc(c.label)} returned).</p>` : ""}
    <h3>Open issues <span class="muted">(${issues.length})</span></h3>${hidden(allIssues, issues, "open issue", "open issues")}
    ${issues.length ? `<table class="data-table" data-list="mon-issues"><thead><tr><th>Priority</th><th>Issue</th><th>Condition</th><th>Entities</th>
      <th>Activated</th><th>Azure resources it matches</th>${hasLogs ? "<th></th>" : ""}</tr></thead><tbody>${issues.map((i) => `<tr>
      <td>${issuePill(i)}</td><td>${monLink(i.url, i.title || i.id)}<div>${(c.capabilities || []).includes("alert history")
        ? alertCheckButton({ source: "monitor", ...toolParams(kind), account: i.account_id ?? "", condition: i.condition || "", policy: i.policy || "", priority: i.priority_label || "" }) : ""}</div></td>
      <td>${esc(i.condition)}${i.policy ? `<div class="muted">${esc(i.policy)}</div>` : ""}</td>
      <td>${esc(i.entities.slice(0, 3).join(", "))}${i.entities.length > 3 ? ` <span class="muted">+${i.entities.length - 3}</span>` : ""}</td>
      <td>${agoHtml(i.activated_at)}</td><td>${resourceChips(i.matches)}</td>
      ${hasLogs ? `<td>${logsButton(i.entity_ids || [], i.account_id, i.entities.join(", ") || i.title, true, "", i.matches)}${queryLink(i.matches)}</td>` : ""}</tr>`).join("")}</tbody></table>`
      : `<p class="ok">✓ No open issues${scopeOnly ? " match an Azure resource in scope" : " match these filters"}.</p>`}
    <h3>Alerting entities <span class="muted">(${entities.length})</span></h3>${hidden(allEntities, entities, "alerting entity", "alerting entities")}
    ${entities.length ? `<table class="data-table" data-list="mon-entities"><thead><tr><th>Severity</th><th>Entity</th><th>Type</th><th>Account</th>
      <th>Azure resources it matches</th>${hasLogs ? "<th></th>" : ""}</tr></thead><tbody>${entities.map((e) => `<tr>
      <td>${monPill(e.severity)}</td><td>${monLink(e.url, e.name)}${e.reporting ? "" : ' <span class="muted">(not reporting)</span>'}</td>
      <td>${entityType(e)}</td><td>${esc(e.account)}</td><td>${resourceChips(e.matches)}</td>
      ${hasLogs ? `<td>${logsButton(e.ids || [e.id], e.account_id, e.name, true, e.name, e.matches)}${queryLink(e.matches)}</td>` : ""}</tr>`).join("")}</tbody></table>`
      : `<p class="ok">✓ Nothing alerting${scopeOnly ? " matches an Azure resource in scope" : " matches these filters"}.</p>`}
    <p class="muted">Matching: a tag whose value is an Azure resource ID is an exact match; an entity named like a resource, or a
      distinctive resource name in its name, is a name match; short or common names inside a longer name are only "possible".</p>`;
}

// The first Azure resource a row matches, opened in Query (errors, slow transactions, or logs by name).
function queryLink(matches) {
  const m = (matches || []).find((x) => x.id && x.display_type !== "Resource group");
  return m ? ` <button type="button" class="link" data-mon-query="${esc(JSON.stringify({ id: m.id, name: m.name }))}" title="Open ${esc(m.name)} in Query">Query</button>` : "";
}

$("mon-body").addEventListener("click", (event) => {
  const logs = event.target.closest("[data-logs]");
  if (logs) { toggleRowLogs(logs); return; }
  const query = event.target.closest("[data-mon-query]");
  if (query) {
    showMonSub("query");
    pickQueryResource(JSON.parse(query.dataset.monQuery));
    return;
  }
  if (event.target.dataset && event.target.dataset.showAll !== undefined) {
    $("mon-scope-only").checked = false;
    rememberMonScopeOnly();
    renderMonitors();
    return;
  }
  const chip = event.target.closest("[data-open]");
  if (chip) { startSnapshot(JSON.parse(chip.dataset.open)); return; }
  if (event.target.dataset && event.target.dataset.goto) showView(event.target.dataset.goto);
});
$("mon-refresh").addEventListener("click", () => loadMonitors(true));
for (const id of ["mon-filter-text", "mon-severity", "mon-scope-only"]) $(id).addEventListener("input", debounce(renderMonitors, 150));

function rememberMonScopeOnly() {
  stored.set("ahc.monScopeOnly", $("mon-scope-only").checked ? "1" : "0");
}
$("mon-scope-only").addEventListener("change", rememberMonScopeOnly);
$("mon-scope-only").checked = stored.get("ahc.monScopeOnly") !== "0";

// Browse → a resource → <tool>: what the selected connection says about it, then its logs on request.
async function tabTool(node, body, kind) {
  const c = await exploreGet("monitors", node, toolParams(kind));
  if (!c.available) { body.innerHTML = snUnavailable(c); return; }
  const how = (r) => { const m = r.matches.find((x) => x.id.toLowerCase() === node.id.toLowerCase()) || r.matches[0]; return esc(m ? MON_CONFIDENCE[m.confidence] : ""); };
  body.innerHTML = `<p class="muted">${esc(connTitle(c))} · read ${esc(ago(c.fetched_at))}.${toolLink(c.console, c.label)}</p>
    ${(c.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join("")}
    <h4>Entities matching this resource</h4>
    ${c.entities.length ? table(["State", "Entity", "Type", "Account", "How it matched"], sortEntities(c.entities).map((e) => [monPill(e.severity),
      monLink(e.url, e.name) + (e.reporting ? "" : ' <span class="muted">(not reporting)</span>'), entityType(e), esc(e.account), how(e)]))
      : `<p class="muted">No ${esc(c.label)} entity matches this resource by Azure ID or name.</p>`}
    <h4>Open issues</h4>
    ${c.issues.length ? table(["Priority", "Open issue", "Condition", "Activated", "How it matched"], sortIssues(c.issues).map((i) => [issuePill(i),
      monLink(i.url, i.title || i.id), esc(i.condition), agoHtml(i.activated_at), how(i)]))
      : '<p class="ok">✓ No open issues touch this resource.</p>'}
    <h4>Logs</h4>
    ${c.capabilities.includes("logs") ? `<p class="muted">Read from ${esc(c.name)} only when you click Load. The cost follows how many
        lines it scans (the window × the partitions), so a shorter window or one partition keeps it down.</p>
      <div data-tool-logs>${loading("Finding what can be read…")}</div>` : `<p class="muted">${esc(c.label)} doesn't provide logs here.</p>`}`;
  const box = body.querySelector("[data-tool-logs]");
  if (box) mountResourceLogs(box, kind, node);
}

// A resource's logs: the plan first, so the partitions and the query are there before the first Load.
async function mountResourceLogs(box, kind, target) {
  const request = (path, extra) => api(path, { params: toolParams(kind, { id: target.id, ...extra }) });
  let plan;
  try { plan = await request("api/monitors/plan", { minutes: 30 }); } catch (error) {
    if (!error.signedOut) box.innerHTML = noticeHtml(error);
    return null;
  }
  if (!plan.available) { box.innerHTML = snUnavailable(plan); return null; }
  return readPanel(box, { request: (p) => request("api/monitors/logs", p), partitions: plan.logs.partitions,
    search: plan.search, azureIds: plan.azure_ids || [], byId: plan.by_id !== false, preview: { ...plan, ...plan.logs },
    onOptions: async (o) => {
      const again = await request("api/monitors/plan", { minutes: 30, ...o });
      return again.available ? { ...again, ...again.logs } : null;
    } });
}

// ------------------------------------------------------------------ reading a monitoring tool
// One panel for every read of a connection (the Query tab, an Alerts row's Logs, a resource's tool tab): window,
// partition, text and the name search; the query on top of the answer, "Will send" before the first read; Load older.

const READ_MINUTES = [[15, "15 minutes"], [30, "30 minutes"], [60, "1 hour"], [360, "6 hours"]];
const PARTITION_HELP = "Log stores: Log, plus Log_<name> for each data partition set up. All reads every one; one scans less.";
const Q_TEXT = { logs: "Message contains… (optional)", errors: "Error message contains… (optional)",
  slow: "Transaction name contains… (optional)" };
const Q_COLUMNS = { appName: "App", transactionName: "Transaction", name: "Transaction", "error.class": "Error class",
  errors: "Errors", last_message: "Last message", last_seen: "Last seen", p95_s: "p95", avg_s: "Average", max_s: "Max",
  calls: "Calls", account: "Account" };
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

// The query a read sends (`sent`) or would send, with Copy and the tool's own console: one per account (NRQL) or one
// per place it's read (KQL, with its time span).
// ------------------------------------------------------------------ Is this alert right?
// Any alert whose source gives its rule and history (Azure metric alerts, a monitoring tool's conditions) gets the same
// arithmetic and documented limits (core/alert_check.py; README → "Is this alert right?"). Only when clicked.
function alertCheckButton(ref) {
  return `<button type="button" class="link alert-check-btn" data-alert-check="${esc(JSON.stringify(ref))}"
    title="Read the alert's rule and the last days of what it watches: is it unusual, or does it fire on normal values?">Is this alert right?</button>`;
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest && event.target.closest("[data-alert-check]");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();  // never opens the row it sits in
  const row = button.closest("tr, .attn-row, li, .ov-row") || button.parentElement;
  const next = row.nextElementSibling;
  if (next && next.classList.contains("alert-check")) { next.remove(); return; }
  const panel = document.createElement(row.tagName === "TR" ? "tr" : "div");
  panel.className = "alert-check";
  if (row.tagName === "TR") panel.innerHTML = `<td colspan="${row.children.length}"></td>`;
  row.after(panel);
  const slot = row.tagName === "TR" ? panel.firstElementChild : panel;
  slot.innerHTML = loading("Reading the alert's rule and the last days of what it watches…");
  try {
    slot.innerHTML = alertCheckHtml(await api("api/alert-check", { params: JSON.parse(button.dataset.alertCheck) }));
  } catch (error) { slot.innerHTML = noticeHtml(error); }
}, true);

function alertCheckHtml(d) {
  if (!d.available) return `<p class="notice">${esc(d.reason || "This alert can't be checked.")}</p>${d.fix ? `<p class="muted">${esc(d.fix)}</p>` : ""}`;
  const n = d.numbers || {}, r = d.rule || {};
  const num = (v) => (v == null ? "–" : fmt(v));
  const rows = [
    ["Now", num(n.now), n.now_at ? dayTime(n.now_at) : ""],
    ["Same time last week", num(n.week_ago), ""],
    ["Usual (median)", num(n.usual), ""],
    r.threshold != null ? ["Threshold", `${r.operator || ""} ${num(r.threshold)}`, ""] : null,
    n.breaching != null ? ["Rule true", `${n.breaching} of ${n.values} buckets (${n.breaching_percent}%)`, ""] : null,
    ["Now ÷ last week", num(n.now_vs_week), ""],
    n.threshold_vs_usual != null ? ["Threshold ÷ usual", num(n.threshold_vs_usual), ""] : null,
  ].filter(Boolean);
  const verdicts = d.verdicts.map((v) => `<li>${pill(v.status === "ok" ? "healthy" : v.status)} ${esc(v.text)}
      <div class="muted">Rule: ${esc(v.rule)}${v.setting ? ` · <code>${esc(v.setting)}</code> in Settings` : ""}</div></li>`).join("");
  const text = [`Is this alert right? ${r.name} (${d.source})`,
    ...d.verdicts.map((v) => `- ${v.text} (rule: ${v.rule}${v.setting ? `, ${v.setting}` : ""})`),
    ...rows.map(([k, v, extra]) => `${k}: ${v}${extra ? ` (${extra})` : ""}`),
    `Rule as the source defines it: ${r.query ? r.query.text : ""}`].join("\n");
  return `<div class="alert-check-body">
    <ul class="ac-verdicts">${verdicts}</ul>
    ${(r.links || []).length ? `<p class="attn-links">${linksHtml(r.links)}</p>` : ""}
    ${alertCheckChart(d)}
    <table class="ac-numbers"><tbody>${rows.map(([k, v, extra]) => `<tr><th>${esc(k)}</th><td>${esc(v)}${extra ? ` <span class="muted">${esc(extra)}</span>` : ""}</td></tr>`).join("")}</tbody></table>
    <details class="mon-query"><summary>How it was read</summary>
      <p><strong>${esc(d.source)}</strong> · ${esc(r.name)}${r.duration_minutes ? ` · must hold ${esc(fmt(r.duration_minutes))} min` : ""}</p>
      ${r.query ? `<div class="muted">${esc(r.query.label)}</div><pre>${esc(r.query.text)}</pre>` : ""}
      <p class="muted">The last ${esc(String(d.days))} days, ${esc(d.scaled)}; the latest bucket is left out while it fills in.</p>
      ${(d.notes || []).map((x) => `<p class="muted">${esc(x)}</p>`).join("")}
      ${queryBlock({ queries: d.queries })}
    </details>
    <button type="button" class="link" data-copy-check="${esc(text)}">Copy</button></div>`;
}

// The last days as a line, with the threshold dashed: a picture of "how often was the rule true".
function alertCheckChart(d) {
  const points = (d.points || []).filter((p) => p[1] != null);
  if (points.length < 2) return "";
  const r = d.rule || {};
  const values = points.map((p) => p[1]).concat(r.threshold != null ? [r.threshold] : []);
  const lo = Math.min(...values, 0), hi = Math.max(...values) || 1;
  const w = 600, h = 90, y = (v) => h - 4 - ((v - lo) / (hi - lo || 1)) * (h - 8);
  const line = points.map((p, i) => `${((i / (points.length - 1)) * w).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  const limit = r.threshold != null ? `<line x1="0" x2="${w}" y1="${y(r.threshold).toFixed(1)}" y2="${y(r.threshold).toFixed(1)}" class="ac-threshold"/>` : "";
  return `<svg class="ac-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img"
      aria-label="The last ${esc(String(d.days))} days of what the rule watches${r.threshold != null ? ", with its threshold" : ""}">${limit}<polyline points="${line}" class="ac-line"/></svg>
    <div class="ac-axis muted"><span>${esc(dayTime(points[0][0]))}</span><span>${r.threshold != null ? `dashed: threshold ${esc(r.operator)} ${esc(fmt(r.threshold))}` : ""}</span><span>${esc(dayTime(points[points.length - 1][0]))}</span></div>`;
}

document.addEventListener("click", (event) => {
  const copy = event.target.closest && event.target.closest("[data-copy-check]");
  if (copy) copyText(copy.dataset.copyCheck, copy);
});

function queryBlock(data, sent = true) {
  const queries = (data && data.queries) || [];
  if (!queries.length) return "";
  const textOf = (q) => q.nrql || q.query;
  const nameOf = (q) => (q.account != null ? `account ${q.account}` : q.label || "");
  const text = queries.map(textOf).join("\n\n");
  const count = queries.length > 1 ? `${queries.length} ${queries[0].account != null ? "accounts" : "queries"}` : nameOf(queries[0]);
  return `<div class="q-query"><div class="q-query-head"><strong>${sent ? "Query sent" : "Will send"}</strong>
      <span class="muted">${esc([data.label, count].filter(Boolean).join(" · "))}</span>
      <span class="spacer"></span><button type="button" class="link" data-copy-query="${esc(text)}">Copy</button>
      ${data.console ? `<a href="${esc(data.console)}" target="_blank" rel="noopener">Open ${esc(data.label)}</a>` : ""}</div>
    ${queries.map((q) => `${queries.length > 1 || q.timespan ? `<div class="muted">${esc([queries.length > 1 ? nameOf(q) : "", q.timespan ? `over ${q.timespan}` : ""]
      .filter(Boolean).join(" · ").replace(/^account/, "Account"))}</div>` : ""}<pre>${esc(textOf(q))}</pre>`).join("")}</div>`;
}

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-goto-security]")) { state.secSub = "config"; showView("security"); return; }
  const open = event.target.closest("[data-investigate]");
  if (open) {
    const t = JSON.parse(open.dataset.investigate);
    investigate(t.component, t.operation ?? null, t.role || "", t.trace ? { trace: t.trace, at: t.at, instance: t.instance } : null);
    return;
  }
  const copy = event.target.closest("[data-copy-query]");
  if (copy) copyText(copy.dataset.copyQuery, copy);
});

function partitionOptions(found, keep = "") {
  return `<option value="">${found.length ? `All partitions (${found.length})` : "All partitions"}</option>${
    found.map((p) => `<option value="${esc(p)}" ${p === keep ? "selected" : ""}>${esc(p)}</option>`).join("")}`;
}

// The lines one or more reads returned, newest first, each expandable to every attribute; Load older when cut.
function logsHtml(data, lines = data.lines || []) {
  if (!data.available) return snUnavailable(data);
  const top = queryBlock(data) + noticesHtml(data.notes);
  if (data.note) return `${top}<p class="muted">${esc(data.note)}</p>`;
  const from = [data.entities.length ? `from ${esc(data.entities.join(", "))}` : "", data.search ? `mentioning ${esc(data.search)}` : ""]
    .filter(Boolean).join(" or ");
  const read = [...new Set((data.queries || []).flatMap((q) => q.partitions || []))];
  const w = data.window || {};
  const span = w.since ? ` · ${clock(w.since)}–${w.until ? clock(w.until) : "now"}` : "";
  const oldest = lines.length ? Math.min(...lines.map((l) => l.ms).filter(Boolean)) : 0;
  return `${top}<p class="muted">${lines.length} line(s)${from ? ` ${from}` : ""}${read.length ? ` · read ${esc(read.join(", "))}` : ""}${span}, newest first.</p>
    ${lines.length ? `<table class="data-table mon-logs"><thead><tr><th>Time</th><th>Level</th><th>Entity</th><th>Message (click for every attribute)</th></tr></thead>
      <tbody>${lines.map((l) => `<tr><td title="${esc(l.time)}">${esc(l.time ? new Date(l.time).toLocaleTimeString() : "")}</td>
        <td>${esc(l.level)}</td><td>${l.via === "mention" ? `<span class="muted" title="Found because it mentions the name; it may come from something else">mentions ${esc(data.search)}</span>`
          : `${esc(l.entity || "entity")}`}</td>
        <td><details><summary>${esc(l.message.slice(0, 300) || "(no message)")}</summary><pre>${esc(JSON.stringify(l.attributes, null, 2))}</pre>
          ${l.message.length > 300 ? `<pre>${esc(l.message)}</pre>` : ""}</details></td></tr>`).join("")}</tbody></table>`
      : '<p class="ok">✓ No log lines in this window.</p>'}
    ${data.truncated && oldest > (w.since || 0) ? `<p><button type="button" data-r-older>Load older</button>
      <span class="muted">Each read takes the newest 100 lines; this reads the 100 before ${esc(clock(oldest))}, in the same window.</span></p>` : ""}`;
}

function reportCell(column, value) {
  if (value == null || value === "") return "–";
  if (column === "last_seen") return seen(value);
  if (column.endsWith("_s") && typeof value === "number") return value < 1 ? `${fmt(Math.round(value * 1000))} ms` : `${fmt(Math.round(value * 100) / 100)} s`;
  if (column === "last_message") return `<span class="ai-msg">${esc(String(value).slice(0, 300))}</span>`;
  return esc(typeof value === "number" ? fmt(value) : String(value));
}

function reportHtml(data) {
  if (!data.available) return snUnavailable(data);
  return `${queryBlock(data)}${noticesHtml(data.notes)}
    <p class="muted">${esc(data.report_label)}: ${data.rows.length} row(s)${data.entities.length ? ` from ${esc(data.entities.join(", "))} or` : ""}
      for apps named like the resource · last ${esc(data.minutes)} min${data.truncated ? " · <strong>only the first rows were read</strong>" : ""}.</p>
    ${data.rows.length ? table([...data.columns.map((c) => Q_COLUMNS[c] || c)], data.rows.map((r) => data.columns.map((c) => reportCell(c, r[c]))))
      : `<p class="ok">✓ No ${esc(data.report_label.toLowerCase())} in this window. Only apps with a ${esc(data.label)} APM agent report them.</p>`}`;
}

// `request(params)` makes the call; `preview` (a plan) is shown as "Will send" until the first read; `onOptions(o)`
// returns the plan with the name search or Azure ID match ticked off (`o`), to update that preview. Returns { run }.
function readPanel(box, { kind = "logs", request, partitions = [], search = "", azureIds = [], byId = true, preview = null, onOptions = null }) {
  const isLogs = kind === "logs";
  const root = document.createElement("div");
  root.innerHTML = `<div class="table-tools read-tools">
      <label>Last <select data-r-minutes>${READ_MINUTES.map(([v, l]) => `<option value="${v}" ${v === 30 ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      ${isLogs ? `<label title="${esc(PARTITION_HELP)}">Partition <select data-r-part>${partitionOptions(partitions)}</select></label>` : ""}
      <input type="search" data-r-text placeholder="${esc(Q_TEXT[kind] || "Contains… (optional)")}" aria-label="Contains">
      ${isLogs && search ? `<label class="inline" title="Lines that mention ${esc(search)} anywhere, even with no entity linked; they may come from other apps.">
        <input type="checkbox" data-r-mentions checked> also lines mentioning <code>${esc(search)}</code></label>` : ""}
      ${isLogs && azureIds.length ? `<label class="inline" title="Lines whose resourceId is ${azureIds.length === 1 ? "this resource's" : "these resources'"} Azure ID, as Azure's diagnostic logs carry it when forwarded, with no entity linked.">
        <input type="checkbox" data-r-byid ${byId ? "checked" : ""}> also lines tagged with ${azureIds.length === 1 ? "its" : "their"} Azure ID</label>` : ""}
      <button type="button" class="primary" data-r-run>${isLogs ? "Load logs" : "Run"}</button></div>
    <div data-r-preview>${preview ? queryBlock(preview, false) : ""}</div><div data-r-out></div>`;
  box.replaceChildren(root);
  const q = (selector) => root.querySelector(selector);
  const out = q("[data-r-out]");
  let last = null, lines = [];
  const options = () => {  // the ticked-off matches, as the routes read them
    const o = {};
    if (q("[data-r-mentions]") && !q("[data-r-mentions]").checked) o.mentions = "0";
    if (q("[data-r-byid]") && !q("[data-r-byid]").checked) o.by_id = "0";
    return o;
  };
  const params = (older) => {
    const p = { minutes: q("[data-r-minutes]").value, text: q("[data-r-text]").value };
    if (isLogs) Object.assign(p, { partition: q("[data-r-part]").value ? [q("[data-r-part]").value] : [] }, options());
    if (older) Object.assign(p, { since: last.window.since, until: Math.min(...lines.map((l) => l.ms).filter(Boolean)) });
    return p;
  };
  const run = async (older = false) => {
    const button = older && q("[data-r-older]");
    if (button) { button.disabled = true; button.textContent = "Loading older…"; } else out.innerHTML = loading("Reading…");
    try {
      const data = await request(params(older));
      if (isLogs && data.partitions && data.partitions.length) {  // every answer refreshes them, keeping the pick
        const select = q("[data-r-part]");
        select.innerHTML = partitionOptions(data.partitions, select.value);
      }
      lines = older && data.available ? [...lines, ...data.lines] : data.lines || [];
      last = data;
      q("[data-r-preview]").innerHTML = "";
      out.innerHTML = isLogs ? logsHtml(data, lines) : reportHtml(data);
    } catch (error) {
      if (!error.signedOut) out.innerHTML = noticeHtml(error);
    }
  };
  q("[data-r-run]").addEventListener("click", () => run());
  q("[data-r-text]").addEventListener("keydown", (event) => { if (event.key === "Enter") run(); });
  root.addEventListener("click", (event) => { if (event.target.closest("[data-r-older]")) run(true); });
  for (const box of [q("[data-r-mentions]"), q("[data-r-byid]")]) {
    if (box && onOptions) box.addEventListener("change", async () => {
      const plan = await onOptions(options()).catch(() => null);
      if (plan && !last) q("[data-r-preview]").innerHTML = queryBlock(plan, false);
    });
  }
  return { run };
}

// ------------------------------------------------------------------ the tool tab's Query
// 1 a resource in scope, 2 what the tool has for it (the plan: entities and how they matched, the name search, the
// partitions, the queries; nothing is read yet), 3 what to read, in the shared panel.

function showMonSub(sub) {
  const usable = state.mon && state.mon.available;
  state.monSub = usable ? sub : "alerts";
  $("mon-subtabs").classList.toggle("hidden", !usable);
  $("mon-q").classList.toggle("hidden", state.monSub !== "query");
  $("mon-alerts").classList.toggle("hidden", state.monSub !== "alerts");
  for (const b of $("mon-subtabs").querySelectorAll("[data-mon-sub]")) b.classList.toggle("active", b.dataset.monSub === state.monSub);
  if (state.monSub === "query") renderQueryList();
}

$("mon-subtabs").addEventListener("click", (event) => {
  const b = event.target.closest("[data-mon-sub]");
  if (b) showMonSub(b.dataset.monSub);
});

// Resources this connection already matched (from its alerting entities and open issues): suggested before typing.
function monMatched() {
  const out = new Map();
  for (const r of [...((state.mon || {}).issues || []), ...((state.mon || {}).entities || [])]) {
    for (const m of r.matches || []) if (m.id && m.display_type !== "Resource group" && !out.has(m.id.toLowerCase())) out.set(m.id.toLowerCase(), m);
  }
  return out;
}

function queryItem(r, matched) {
  const picked = state.monQuery && state.monQuery.id.toLowerCase() === r.id.toLowerCase();
  return `<li role="option" aria-selected="${picked}" class="${picked ? "picked" : ""}" data-pick="${esc(JSON.stringify({ id: r.id, name: r.name }))}">
    <span><strong>${esc(r.name)}</strong> <span class="muted">${esc([r.display_type, r.resource_group].filter(Boolean).join(" · "))}</span></span>
    ${matched.has(r.id.toLowerCase()) ? `<span class="chip-ok">alerting in ${esc(toolLabel(state.toolKind))}</span>` : ""}</li>`;
}

// An entity found by name in the tool itself, for apps that match no Azure resource.
function entityItem(e) {
  const pick = { entity: true, id: `entity:${e.ids[0]}`, ids: e.ids, account: e.account_id, name: e.name, apm: e.apm };
  const picked = state.monQuery && state.monQuery.id === pick.id;
  return `<li role="option" aria-selected="${picked}" class="${picked ? "picked" : ""}" data-pick="${esc(JSON.stringify(pick))}">
    <span><strong>${esc(e.name)}</strong> <span class="muted">${esc([e.type, e.account].filter(Boolean).join(" · "))}</span></span>
    ${e.apm ? '<span class="chip-ok">APM</span>' : ""}</li>`;
}

function renderQueryList(found = null, entities = state.monEntities || []) {
  const list = $("mon-q-list");
  const matched = monMatched();
  const text = $("mon-q-res").value.trim();
  if (found) {
    const inTool = entities.length ? `<li class="q-list-head muted">In ${esc(toolLabel(state.toolKind))}, by name</li>${entities.slice(0, 20).map(entityItem).join("")}` : "";
    list.innerHTML = (found.length ? `${entities.length ? '<li class="q-list-head muted">Azure resources in scope</li>' : ""}${found.slice(0, 50).map((r) => queryItem(r, matched)).join("")}`
      : `<li class="muted">Nothing in scope has that in its name${entities.length ? "" : `, nor does any ${esc(toolLabel(state.toolKind))} entity`}.</li>`) + inTool;
  } else if (text.length < 2) {
    const picks = [...matched.values()];
    list.innerHTML = picks.length ? `<li class="q-list-head muted">Alerting in ${esc(toolLabel(state.toolKind))}</li>${picks.slice(0, 20).map((r) => queryItem(r, matched)).join("")}`
      : '<li class="muted">Type at least 2 letters of a resource name.</li>';
  }
}

const Q_PICK = '<p class="muted">Pick a resource: this shows what will be read, before anything is.</p>';

// A new connection starts the Query over: its plan, partitions and results belong to it.
function renderQueryBar() {
  const c = state.mon;
  showMonSub(state.monSub || "alerts");
  if (!c || !c.available) return;
  for (const el of $("mon-q").querySelectorAll("[data-tool-label]")) el.textContent = toolLabel(state.toolKind);
  const conn = `${state.toolKind}|${state.selected[state.toolKind]}`;
  if (state.monQueryConn === conn) return;
  state.monQueryConn = conn;
  state.monQuery = null;
  state.monPlan = null;
  $("mon-q-res").value = "";
  $("mon-q-plan").innerHTML = Q_PICK;
  $("mon-q-kinds").innerHTML = "";
  $("mon-q-kind-note").textContent = "";
  $("mon-q-out").innerHTML = "";
}

const findResources = debounce(async () => {
  const text = $("mon-q-res").value.trim();
  if (text.length < 2) { renderQueryList(); return; }
  if (state.monQuery && state.monQuery.name === text) return;
  let found = [], entities = [];
  try {
    [found, entities] = await Promise.all([api("api/monitors/find", { params: { ...scopeParams(currentScope()), q: text } }),
      api("api/monitors/entities", { params: toolParams(state.toolKind, { q: text }) }).catch(() => [])]);
  } catch { return; }
  if ($("mon-q-res").value.trim() !== text) return;
  state.monFound = found;
  state.monEntities = entities;
  renderQueryList(found, entities);
}, 250);

function pickQueryResource(target) {
  state.monQuery = target;
  $("mon-q-res").value = target.name;
  renderQueryList(state.monFound && (target.entity || state.monFound.some((r) => r.id === target.id)) ? state.monFound : null);
  loadQueryPlan();
}

// A resource is asked by its Azure ID; an entity picked by name by its IDs and account, on the entity routes.
const ENTITY_ROUTES = { "api/monitors/plan": "api/monitors/entity-plan", "api/monitors/logs": "api/monitors/entity-logs",
  "api/monitors/report": "api/monitors/entity-report" };

function queryRequest(path, extra) {
  const t = state.monQuery;
  const target = t.entity ? { entity: t.ids, account: t.account, name: t.name, apm: t.apm ? "1" : "0" } : { id: t.id };
  return api(t.entity ? ENTITY_ROUTES[path] : path, { params: toolParams(state.toolKind, { ...target, ...extra }) });
}

async function loadQueryPlan() {
  const target = state.monQuery;
  $("mon-q-plan").innerHTML = loading(`Asking ${state.selected[state.toolKind] || toolLabel(state.toolKind)} what it has for ${target.name}…`);
  $("mon-q-kinds").innerHTML = "";
  $("mon-q-out").innerHTML = "";
  let plan;
  try { plan = await queryRequest("api/monitors/plan", { minutes: 30 }); } catch (error) {
    if (!error.signedOut) $("mon-q-plan").innerHTML = noticeHtml(error);
    return;
  }
  if (state.monQuery !== target) return;
  state.monPlan = plan;
  $("mon-q-plan").innerHTML = planHtml(plan, target);
  if (!plan.available) return;
  const kinds = queryKinds(plan);
  if (!kinds.some((k) => k.key === state.monKind && k.runnable)) state.monKind = (kinds.find((k) => k.runnable) || {}).key;
  renderQueryKinds();
}

function planHtml(plan, target) {
  if (!plan.available) return snUnavailable(plan);
  const entities = plan.entities.map((e) => `<li>${monLink(e.url, e.name)} <span class="muted">${esc([e.type, e.account].filter(Boolean).join(" · "))}
    · ${esc(MON_CONFIDENCE[e.how] || e.how)}</span>${e.apm ? ' <span class="chip-ok">APM</span>' : ""}</li>`).join("");
  const sources = [
    entities ? `<li>Lines from ${plan.entities.length === 1 ? "this entity" : "these entities"}:<ul>${entities}</ul></li>` : "",
    plan.attribute ? `<li>Lines whose <code>${esc(plan.attribute)}</code> is <code>${esc(target.name)}</code> (saved for this resource type in Settings).</li>` : "",
    plan.search ? `<li>Lines that mention <code>${esc(plan.search)}</code> anywhere: finds logs with no entity linked, and may include other apps'.
      Untick it below to leave it out.</li>` : ""].filter(Boolean).join("");
  const parts = plan.logs ? plan.logs.partitions : [];
  return `${noticesHtml(plan.notes)}
    ${sources ? `<ul class="q-sources">${sources}</ul>` : ""}
    ${!entities ? `<p class="muted">No ${esc(plan.label)} entity matches ${esc(target.name)} by Azure ID or name. Type its
      ${esc(plan.label)} app or entity name instead to pick it there.</p>` : ""}
    ${plan.search_note ? `<p class="muted">${esc(plan.search_note)}</p>` : ""}
    ${plan.logs && plan.logs.note ? noticeHtml(plan.logs.note) : ""}
    ${plan.logs ? `<p class="muted" title="${esc(PARTITION_HELP)}">Log partitions: ${parts.length ? esc(parts.join(", ")) : "none found"}.</p>` : ""}
    ${noticesHtml(plan.logs ? plan.logs.notes : [])}`;
}

function queryKinds(plan) {
  return [...(plan.logs ? [{ key: "logs", label: "Logs", runnable: !plan.logs.note, note: plan.logs.note || "" }] : []), ...(plan.reports || [])];
}

function renderQueryKinds() {
  const kinds = queryKinds(state.monPlan);
  $("mon-q-kinds").innerHTML = kinds.map((k) => `<button type="button" data-q-kind="${esc(k.key)}" class="${k.key === state.monKind ? "active" : ""}"
    aria-pressed="${k.key === state.monKind}" ${k.runnable ? "" : "disabled"} title="${esc(k.note)}">${esc(k.label)}</button>`).join("");
  const chosen = kinds.find((k) => k.key === state.monKind);
  $("mon-q-kind-note").textContent = chosen ? chosen.note : kinds.length ? "Nothing here can be read for this resource." : "";
  if (chosen) mountQueryPanel(chosen);
}

function mountQueryPanel(chosen) {
  const plan = state.monPlan;
  const isLogs = chosen.key === "logs";
  readPanel($("mon-q-out"), {
    kind: chosen.key,
    request: (p) => queryRequest(isLogs ? "api/monitors/logs" : "api/monitors/report", isLogs ? p : { ...p, kind: chosen.key }),
    partitions: isLogs ? plan.logs.partitions : [], search: isLogs ? plan.search : "",
    azureIds: isLogs ? plan.azure_ids || [] : [], byId: plan.by_id !== false,
    preview: { ...plan, queries: isLogs ? plan.logs.queries : chosen.queries },
    onOptions: isLogs ? async (o) => {
      const again = await queryRequest("api/monitors/plan", { minutes: 30, ...o });
      return again.available ? { ...again, ...again.logs } : null;
    } : null,
  });
}

$("mon-q-kinds").addEventListener("click", (event) => {
  const b = event.target.closest("[data-q-kind]");
  if (!b || b.disabled) return;
  state.monKind = b.dataset.qKind;
  renderQueryKinds();
});
$("mon-q-res").addEventListener("input", () => {
  if (state.monQuery && state.monQuery.name !== $("mon-q-res").value.trim()) state.monQuery = null;
  findResources();
});
$("mon-q-list").addEventListener("click", (event) => {
  const item = event.target.closest("[data-pick]");
  if (item) pickQueryResource(JSON.parse(item.dataset.pick));
});

// A Logs button on a tool-tab row: that entity's (or that issue's entities') logs, in the shared panel under the row.
// `matches` are the Azure resources the row matched: their IDs also find forwarded Azure logs with no entity.
function logsButton(ids, account, label, hasLogs, name = "", matches = []) {
  if (!hasLogs || !ids.length || !account) return "";
  const resources = matches.map((m) => m.id).filter((id) => /\/providers\//i.test(id || "")).slice(0, 20);
  return `<button type="button" class="link" data-logs="${esc(JSON.stringify({ ids, account, label, name, resources }))}">Logs</button>`;
}

function toggleRowLogs(button) {
  const row = button.closest("tr");
  const open = row.nextElementSibling && row.nextElementSibling.classList.contains("logs-row");
  if (open) { row.nextElementSibling.remove(); button.textContent = "Logs"; return; }
  const target = JSON.parse(button.dataset.logs);
  const panel = document.createElement("tr");
  panel.className = "logs-row";
  panel.dataset.attached = "";
  panel.innerHTML = `<td colspan="${row.children.length}"><div class="mon-logs-panel"><strong>Logs: ${esc(target.label)}</strong><div></div></div></td>`;
  row.after(panel);
  button.textContent = "Hide logs";
  readPanel(panel.querySelector(".mon-logs-panel > div"), { search: target.name, azureIds: target.resources || [],
    request: (p) => api("api/monitors/entity-logs", { params: toolParams(state.toolKind, {
      entity: target.ids, account: target.account, name: target.name || "", resource: target.resources || [], ...p }) }) }).run();
}

// Triage: one "alerting" card per tool, from its selected connection.
// Triage: App Insights failure spikes from metrics (one GET per component, capped), after the Azure cards.
async function loadAppInsightsCard(scope) {
  const token = state.triageToken;
  const holder = document.createElement("div");
  holder.className = "card";
  holder.innerHTML = `<h3>App Insights: failure spikes</h3>${loading("Comparing the last hour with last week…")}`;
  $("triage-cards").append(holder);
  let card;
  try {
    card = await api("api/triage/appinsights", { params: scopeParams(scope) });
  } catch (error) {
    if (!error.signedOut && token === state.triageToken) holder.innerHTML = `<h3>App Insights: failure spikes</h3>${noticeHtml(error)}`;
    return;
  }
  if (token !== state.triageToken) return;
  if (!card.total) { holder.remove(); return; }  // no components in scope: nothing to say
  holder.outerHTML = renderCard({ ...card, description: `${card.description} (${card.checked} component${card.checked === 1 ? "" : "s"} checked)` });
}

async function loadMonitorCards(scope) {
  for (const kind of toolKinds()) loadToolCard(kind, scope);
}

async function loadToolCard(kind, scope) {
  const token = state.triageToken;
  const label = toolLabel(kind);
  const holder = document.createElement("div");
  holder.className = "card";
  holder.innerHTML = `<h3>${esc(label)}</h3>${loading("Reading what's alerting…")}`;
  $("triage-cards").prepend(holder);
  let c;
  try {
    c = await api("api/monitors", { params: toolParams(kind, scopeParams(scope)) });
  } catch (error) {
    if (!error.signedOut) holder.innerHTML = `<h3>${esc(label)}</h3>${noticeHtml(error)}`;
    return;
  }
  if (token !== state.triageToken) return;
  const title = `${esc(label)}: alerting <span class="muted">· ${esc(state.selected[kind] || "")}</span>`;
  if (!c.available) { holder.outerHTML = `<div class="card warning" id="card-tool-${kind}"><h3>${title}</h3>${snUnavailable(c)}</div>`; return; }
  const all = sortEntities(c.entities.filter((e) => e.severity === "critical" || e.severity === "warning"));
  const rows = all.filter(namesResource);
  const others = all.length - rows.length;
  holder.outerHTML = `<div class="card ${rows.length ? (rows.some((e) => e.severity === "critical") ? "critical" : "warning") : "clear"}" id="card-tool-${kind}">
    <h3>${title}<span class="count">${rows.length}</span></h3>
    <p class="desc">Entities in critical or warning that match Azure resources in scope (${esc(connTitle(c))})</p>
    ${rows.length ? `<ul>${rows.slice(0, 15).map((e) => `<li class="static">${monPill(e.severity)} ${monLink(e.url, e.name)}
      <div class="where">${esc(e.type)} · ${resourceChips(e.matches, 3)}</div></li>`).join("")}</ul>`
      : '<p class="ok">✓ Nothing alerting matches a resource in scope</p>'}
    ${rows.length > 15 ? `<p class="muted">+${rows.length - 15} more in the ${esc(label)} tab.</p>` : ""}
    ${others ? `<p class="muted">+${others} other alerting entit${others === 1 ? "y doesn't" : "ies don't"} match anything in scope (${esc(label)} tab).</p>` : ""}
  </div>`;
}

// ------------------------------------------------------------------ saved scopes
// A named scope per tenant (subscriptions, resource groups, tag filters and the connection of each kind), saved in
// config.yaml and applied in one pick. Anything changed afterwards marks it "(changed)" until saved again.

function scopeSnapshot() {
  return JSON.stringify({ subs: [...state.scope.subs].sort(), groups: [...state.scope.groups].sort(),
    tags: Object.entries(state.scope.tags).sort(), conns: Object.entries(state.connDraft || {}).sort() });
}

function renderSavedButton() {
  const active = state.activeScope;
  const changed = active && active.snapshot !== scopeSnapshot();
  const saved = (state.savedScopes || []).length;
  $("saved-button").textContent = active ? `${active.name}${changed ? " (changed)" : ""}`
    : saved ? `None picked (${saved} saved)` : "None saved yet";
  $("saved-button").classList.toggle("active-scope", !!active);
}
state.markScopeChanged = renderSavedButton;

// What a saved scope holds, by name: `max` names per list (the tooltip passes Infinity).
function scopeSummary(scope, max = 3) {
  const parts = [namesText(scope.subscriptions, max)];
  if (scope.resource_groups.length) parts.push(`groups ${namesText(scope.resource_groups, max)}`);
  if (Object.keys(scope.tag_filters).length) parts.push(Object.entries(scope.tag_filters).map(([k, v]) => `${k}=${v === UNTAGGED ? "(untagged)" : v}`).join(", "));
  if (Object.keys(scope.connections).length) parts.push(Object.values(scope.connections).join(", "));
  return parts.join(" · ");
}

function activeChanged() {
  const active = state.activeScope;
  return active && active.snapshot !== scopeSnapshot() && (state.savedScopes || []).some((s) => s.name === active.name);
}

function currentAsSaved(name) {
  return { name, subscriptions: state.subscriptions.filter((s) => state.scope.subs.has(s.id)).map((s) => s.name),
    resource_groups: state.scope.groups.size ? [...new Set(groupsInScope().map((g) => g.name))] : [],
    tag_filters: { ...state.scope.tags }, connections: { ...(state.connDraft || {}) } };
}

function renderSavedScopes() {
  const scopes = state.savedScopes || [];
  $("saved-changed").classList.toggle("hidden", !activeChanged());
  if (activeChanged()) {
    $("saved-update").textContent = `Update "${state.activeScope.name}"`;
  }
  $("saved-list").innerHTML = scopes.length ? scopes.map((s, i) => `<div class="saved-row">
      <button type="button" class="saved-apply" data-saved-apply="${i}" title="${esc(scopeSummary(s, Infinity))}"><strong>${esc(s.name)}</strong>
        <span class="muted">${esc(scopeSummary(s))}</span></button>
      ${s.legacy ? '<span class="muted">from your old favourites</span> <button type="button" class="link" data-move-legacy>move to saved scopes</button>'
        : `<button type="button" class="link" data-saved-rename="${i}">rename</button>
      <button type="button" class="link" data-saved-delete="${i}">delete</button>`}</div>`).join("")
    : `<p class="hint">No saved scopes for this tenant yet. Pick subscriptions, filters and connections, then
        <strong>Save current as…</strong>. They're kept in config.yaml under this tenant.</p>`;
}

async function applySavedScope(scope) {
  const subs = state.subscriptions.filter((s) => globMatch(scope.subscriptions, s.name) || globMatch(scope.subscriptions, s.id));
  if (!subs.length) { showMessage(`None of ${scope.name}'s subscriptions (${scope.subscriptions.join(", ")}) are visible in this tenant.`, true); return; }
  state.connDraft = { ...(state.connDraft || {}), ...scope.connections };
  renderConnectionPickers();
  state.scope.subs = new Set(subs.map((s) => s.id));
  state.scope.tags = { ...scope.tag_filters };
  state.scope.groups.clear();
  renderSubscriptions();
  await loadGroups();
  if (scope.resource_groups.length) {
    state.scope.groups = new Set(state.groups.filter((g) => globMatch(scope.resource_groups, g.name)).map(groupKey));
  }
  applyScope();  // a saved scope is an explicit choice: no separate Apply
  state.activeScope = { name: scope.name, snapshot: scopeSnapshot() };
  renderSavedButton();
}

// Asks for a scope name until it's one the config accepts; empty means cancelled. Reserved names come from the server.
function askScopeName(question, initial) {
  let ask = question;
  for (;;) {
    const name = (window.prompt(ask, initial) || "").trim();
    if (!(state.reservedScopeNames || []).includes(name.toLowerCase())) return name;
    ask = `"${name}" is reserved: Settings → Open with uses it for ${name.toLowerCase() === "all" ? "every subscription" : "nothing picked"}. Pick another name (e.g. "All subscriptions"):`;
    initial = name.toLowerCase() === "all" ? "All subscriptions" : "";
  }
}

async function storeSavedScopes(scopes, done) {
  try {
    applyScopePayload(await api("api/saved-scopes", { method: "POST", body: { scopes } }));
    renderSavedScopes();
    done();
  } catch (error) { showMessage(`Not saved: ${error.message}`, true); }
}

$("saved-button").addEventListener("click", () => togglePanel("saved-panel"));
document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-start-saved]");
  const scope = button && (state.savedScopes || [])[Number(button.dataset.startSaved)];
  if (scope) applySavedScope(scope);
});
$("saved-panel").addEventListener("click", async (event) => {
  const el = event.target.closest("button");
  if (!el) return;
  const scopes = state.savedScopes || [];
  if (el.id === "saved-save") {
    const name = askScopeName("Name for this scope (e.g. Prod, NonProd):", state.activeScope ? state.activeScope.name : "");
    if (!name) return;
    const existing = scopes.findIndex((s) => s.name.toLowerCase() === name.toLowerCase());
    if (existing >= 0 && !window.confirm(`Replace the saved scope "${scopes[existing].name}"?`)) return;
    const scope = currentAsSaved(name);
    const next = existing >= 0 ? scopes.map((s, i) => (i === existing ? scope : s)) : [...scopes, scope];
    await storeSavedScopes(next, () => { state.activeScope = { name, snapshot: scopeSnapshot() }; renderSavedButton(); showMessage(`Saved scope "${name}".`); });
    return;
  }
  if (el.id === "saved-update" || el.id === "saved-revert") {
    const index = scopes.findIndex((s) => s.name === (state.activeScope || {}).name);
    if (index < 0) return;
    if (el.id === "saved-revert") { $("saved-panel").classList.add("hidden"); await applySavedScope(scopes[index]); return; }
    const name = scopes[index].name;
    await storeSavedScopes(scopes.map((s, i) => (i === index ? currentAsSaved(name) : s)), () => {
      state.activeScope = { name, snapshot: scopeSnapshot() }; renderSavedButton(); renderSavedScopes(); showMessage(`Updated scope "${name}".`);
    });
    return;
  }
  const index = Number(el.dataset.savedApply ?? el.dataset.savedRename ?? el.dataset.savedDelete);
  const scope = scopes[index];
  if (!scope) return;
  if (el.dataset.savedApply !== undefined) { $("saved-panel").classList.add("hidden"); await applySavedScope(scope); return; }
  if (el.dataset.savedRename !== undefined) {
    const name = askScopeName(`New name for "${scope.name}":`, scope.name);
    if (!name || name === scope.name) return;
    await storeSavedScopes(scopes.map((s, i) => (i === index ? { ...s, name } : s)), () => {
      if (state.activeScope && state.activeScope.name === scope.name) { state.activeScope.name = name; renderSavedButton(); }
    });
    return;
  }
  if (el.dataset.savedDelete !== undefined && window.confirm(`Delete the saved scope "${scope.name}"?`)) {
    await storeSavedScopes(scopes.filter((_, i) => i !== index), () => {
      if (state.activeScope && state.activeScope.name === scope.name) { state.activeScope = null; renderSavedButton(); }
    });
  }
});

// ------------------------------------------------------------------ connections in the Scope bar
// One selector per kind this tenant has (ServiceNow, then each monitoring tool), listing its turned-on connections.
// The choice is remembered per tenant in this browser; saved scopes can set it too.

function rememberedConnections(tenantId) {
  return stored.json(`ahc.conn.${tenantId}`, {});
}

function rememberConnections() {
  stored.set(`ahc.conn.${state.connTenant}`, state.selected);
}

function setConnections(data) {
  const byKind = {};
  for (const c of data.servicenow) if (c.enabled) (byKind.servicenow = byKind.servicenow || []).push(c);
  for (const m of data.monitors) if (m.enabled) (byKind[m.kind] = byKind[m.kind] || []).push(m);
  state.connections = byKind;
  state.connTenant = data.tenant.id;
  const remembered = rememberedConnections(data.tenant.id);
  state.selected = {};
  for (const [kind, list] of Object.entries(byKind)) {
    const keep = list.find((c) => c.name === remembered[kind]);
    state.selected[kind] = (keep || list.find((c) => c.ready) || list[0]).name;
  }
  state.connDraft = { ...state.selected };
  state.snEnabled = !!byKind.servicenow;
  renderConnectionPickers();
}

function renderConnectionPickers() {
  const kinds = Object.keys(state.connections || {}).sort((a, b) => (a === "servicenow" ? -1 : b === "servicenow" ? 1 : a.localeCompare(b)));
  $("conn-pickers").innerHTML = kinds.map((kind) => {
    const list = state.connections[kind];
    const picked = state.connDraft[kind];
    const missing = picked && !list.some((c) => c.name === picked);
    return `<label class="conn-picker" title="Which ${esc(kind === "servicenow" ? "ServiceNow" : toolLabel(kind))} connection the tabs and Triage use">
      ${esc(kind === "servicenow" ? "ServiceNow" : toolLabel(kind))}
      <select data-conn-kind="${esc(kind)}">${missing ? `<option selected>⚠ ${esc(picked)} isn't available</option>` : ""}
        ${list.map((c) => `<option value="${esc(c.name)}" ${c.name === picked ? "selected" : ""}>${c.ready ? "" : "⚠ "}${esc(c.name)}</option>`).join("")}
      </select></label>`;
  }).join("");
  renderSavedButton();
}

$("conn-pickers").addEventListener("change", (event) => {
  const kind = event.target.dataset.connKind;
  if (!kind) return;
  state.connDraft[kind] = event.target.value;
  draftChanged();
});

// Apply right after changing subscriptions would take the previous subscriptions' groups: wait for this set's first.
$("scope-apply").addEventListener("click", async () => {
  if (state.groupsFor !== subsKey(state.scope.subs)) {
    $("scope-apply").disabled = true;
    $("scope-pending-text").textContent = "Loading the resource groups of these subscriptions…";
    try {
      while (state.groupsFor !== subsKey(state.scope.subs)) await loadGroups();
    } catch (error) { showMessage(error.message, true); return; } finally { $("scope-apply").disabled = false; }
  }
  applyScope();
});
$("scope-discard").addEventListener("click", () => discardScope());

function connectionsChanged(kind) {
  if (state.markScopeChanged) state.markScopeChanged();
  if (kind === "detail-only") {  // the scope moved too, so its reload already covers the tabs
    if (state.exploreNode && state.snapshot) showDetail(state.exploreNode.id, $("detail").querySelector(".tabs .active")?.dataset.tab || "overview");
    return;
  }
  if (kind === "servicenow" || !kind) { state.snLoaded = false; if (state.view === "servicenow") loadServiceNow(); }
  if (kind !== "servicenow") { state.monLoaded = false; if ((state.view || "").startsWith("tool-")) loadMonitors(); }
  state.triageLoaded = false;
  if (state.view === "triage") loadTriage();
  if (state.exploreNode && state.snapshot) showDetail(state.exploreNode.id, $("detail").querySelector(".tabs .active")?.dataset.tab || "overview");
}

// The ServiceNow tab, and one tab per monitoring tool, appear when this tenant has a turned-on connection of it.
async function refreshIntegrationTabs() {
  try {
    setConnections(await api("api/integrations"));
  } catch { state.connections = {}; state.selected = {}; state.connDraft = {}; state.snEnabled = false; renderConnectionPickers(); }
  $("tab-servicenow").classList.toggle("hidden", !state.snEnabled);
  $("tool-tabs").innerHTML = toolKinds().map((kind) =>
    `<button data-view="tool-${esc(kind)}" role="tab" class="${state.view === `tool-${kind}` ? "active" : ""}">${esc(toolLabel(kind))}</button>`).join("");
  const gone = (state.view === "servicenow" && !state.snEnabled) || (state.view?.startsWith("tool-") && !toolKinds().includes(state.view.slice(5)));
  if (gone) showView("triage");
  renderNavMenus();
}

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
    <p class="muted">Each tool gets its own tab and Triage card, showing the connection picked in the Scope bar. New Relic connects now; Datadog and
      Grafana can be saved and connect in a later version.</p>
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
        window.open(url, "_blank", "noopener");
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

// ------------------------------------------------------------------ start

document.querySelector(".views").addEventListener("click", (event) => {
  const menu = event.target.closest(".nav-menu-btn");
  if (menu) {
    for (const p of document.querySelectorAll(".nav-panel")) if (p.id !== menu.dataset.menu) p.classList.add("hidden");
    $(menu.dataset.menu).classList.toggle("hidden");
    return;
  }
  const view = event.target.closest("[data-view]")?.dataset.view;
  if (view) {
    for (const p of document.querySelectorAll(".nav-panel")) p.classList.add("hidden");
    showView(view);
  }
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".nav-menu")) for (const p of document.querySelectorAll(".nav-panel")) p.classList.add("hidden");
});

// A menu in the top bar is underlined, and names the open view, when that view lives in it.
function renderNavMenus() {
  if (state.personas.length) applyPersona();  // the Tools menu gains entries as connections load
  for (const button of document.querySelectorAll(".nav-menu-btn")) {
    const panel = $(button.dataset.menu);
    const open = panel.querySelector(`[data-view="${CSS.escape(state.view || "")}"]`);
    button.classList.toggle("active", !!open);
    button.querySelector(".nav-current").textContent = open ? `: ${open.dataset.short || open.textContent.trim()}` : "";
  }
  const tools = !$("tab-servicenow").classList.contains("hidden") || $("tool-tabs").children.length > 0;
  $("tools-menu").classList.toggle("hidden", !tools);
}

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
    <div class="table-tools"><input id="user-filter" type="search" placeholder="Filter roles by role, scope or group, e.g. reader prod" value="${esc(check.filter || "")}"></div>
    <h3>Azure roles they have now <span class="muted">${filter ? `${active.length} of ${r.active.length}` : active.length}</span></h3>
    <p class="muted">Standing assignments and PIM roles they've activated, given to them directly or through a group.</p>
    ${active.length ? scopeGroupsHtml("user-active", active, { filtering: !!filter, columns: ["Role", "Scope", "Type", "Via group"],
      cells: (row) => [esc(row.role), scopeCell(row), esc(scopeType(row.scope_type)), via(row)] })
      : '<p class="muted">None in scope.</p>'}
    <h3>Azure roles they can activate <span class="muted">${r.done ? (filter ? `${eligible.length} of ${r.eligible.length}` : eligible.length) : "reading…"}</span></h3>
    ${!r.done ? loading(`${check.phase || "Reading"}…`) : eligible.length ? scopeGroupsHtml("user-eligible", eligible, { filtering: !!filter,
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

function renderAlertList() {
  const a = state.alerts;
  const exportLink = (fmt, label) => `<a href="${esc(`${BASE}api/alerts/export?format=${fmt}${state.tenant ? `&tenant=${encodeURIComponent(state.tenant)}` : ""}`)}">${label}</a>`;
  $("alert-list").innerHTML = a.events.length ? `<div class="alert-export muted">Export: ${exportLink("md", "Markdown")} ·
      ${exportLink("csv", "CSV")} · ${exportLink("pdf", "PDF")}</div>
    <table><tr><th>When</th><th>Status</th><th>Alert</th><th>Rule</th><th>Detail</th><th></th></tr>
    ${a.events.map((e) => `<tr class="${unseenAlerts().includes(e) ? "new" : ""} ${e.resource_id ? "clickable" : ""}"
        ${e.resource_id ? `data-open="${esc(JSON.stringify({ kind: "resource", id: e.resource_id, name: e.name || e.resource_id.split("/").pop() }))}"` : ""}>
      <td title="${esc(e.at)}">${esc(new Date(e.epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</td>
      <td>${pill(e.status)}${e.resolved_at ? `<div class="ok" title="${esc(e.resolved_note || "")}">✓ resolved ${esc(new Date(e.resolved_epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</div>` : ""}</td>
      <td><strong>${esc(e.title)}</strong>${e.resolved_note && e.resolved_note !== "back to normal" ? `<div class="muted">${esc(e.resolved_note)}</div>` : ""}</td>
      <td>${esc(e.rule)}</td><td>${escTimes(e.detail)}</td><td class="alert-actions">${alertActions(e)}</td></tr>`).join("")}</table>
    <p class="muted">The last 24 hours, kept in the console's memory: restarting it clears this and records a new starting point.</p>`
    : '<p class="muted">No alerts in the last 24 hours.</p>';
}

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

// ------------------------------------------------------------------ theme
// Like rafaelsonego.com: System by default, Light or Dark when picked (kept in this browser). The graph and charts read
// colours when drawn, so a change repaints them.

function currentTheme() {
  return stored.choice("ahc.theme", ["light", "dark"], "system");
}

function applyTheme(value) {
  if (value === "light" || value === "dark") document.documentElement.setAttribute("data-theme", value);
  else document.documentElement.removeAttribute("data-theme");
  document.querySelectorAll(".theme-switch button").forEach((b) => b.classList.toggle("active", b.dataset.themeValue === value));
  if (state.chart && state.chartArgs) drawChart(...state.chartArgs);
}

document.querySelectorAll(".theme-switch button").forEach((button) => button.addEventListener("click", () => {
  const value = button.dataset.themeValue;
  stored.set("ahc.theme", value === "system" ? null : value);
  applyTheme(value);
}));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (currentTheme() === "system") applyTheme("system"); });
applyTheme(currentTheme());

// About is a dialog over whatever is open: Esc, the close link or a click outside it closes it.
$("about-open").addEventListener("click", () => { $("about").showModal(); fillAboutPypi(); });

// A newer release on PyPI (the server checks an installed copy only, at most every 6 hours): a dot on the account
// button and a menu line until "Not now", kept per version in this browser; About says how to upgrade.
const UPDATE_CHECK_HOURS = 6;
async function checkUpdate() {
  try { state.update = await api("api/update"); } catch { return; }
  showUpdate();
}

function showUpdate() {
  const u = state.update || {};
  const dismissed = stored.get("ahc.update.dismissed") === u.latest;
  $("update-dot").classList.toggle("hidden", !u.newer || dismissed);
  $("update-open").classList.toggle("hidden", !u.newer);
  if (u.newer) $("update-text").innerHTML = `${esc(u.latest)} is out<small>You have ${esc(u.current)} · how to upgrade</small>`;
  $("about-update").innerHTML = u.newer ? `<div class="about-update"><strong>${esc(u.latest)} is out</strong> <span class="muted">you have ${esc(u.current)}</span>
      <p>Upgrade with <code>${esc(u.command)}</code> <button type="button" class="link" data-copy-update>copy</button>, then restart the console.</p>
      <p class="muted">${u.notes ? `<a href="${esc(u.notes)}" target="_blank" rel="noopener">What's new in ${esc(u.latest)}</a> · ` : ""}
        ${dismissed ? "Hidden from the menu until the next version." : '<button type="button" class="link" data-update-later>Not now</button> hides the dot until the next version.'}</p></div>` : "";
}

$("update-open").addEventListener("click", () => { $("about").showModal(); fillAboutPypi(); });
$("about").addEventListener("click", (event) => {
  if (event.target.closest("[data-update-later]") && state.update) { stored.set("ahc.update.dismissed", state.update.latest); showUpdate(); }
  if (event.target.closest("[data-copy-update]") && state.update) navigator.clipboard?.writeText(state.update.command).then(() => showMessage("Copied."), () => {});
});

// Read when About opens (kept 6 h on the server): the latest release and pypistats' downloads, without mirrors.
async function fillAboutPypi(refresh = false) {
  const box = $("about-pypi");
  box.innerHTML = loading("Reading PyPI…");
  let d;
  try { d = await api("api/about/pypi", { params: refresh ? { refresh: "1" } : {} }); } catch (error) { box.innerHTML = noticeHtml(error.message); return; }
  const links = `<a href="${esc(d.project_page)}" target="_blank" rel="noopener">Project page</a> ·
    <a href="${esc(d.stats_page)}" target="_blank" rel="noopener">pypistats</a> ·
    <button type="button" class="link" id="about-pypi-refresh">Read again</button>`;
  if (d.error) { box.innerHTML = `<p class="muted">${esc(d.error)}</p><p class="muted">${links}</p>`; wireAboutPypi(); return; }
  const n = (v) => (v == null ? "–" : Number(v).toLocaleString());
  const parts = (v) => String(v).split(".").map((x) => parseInt(x, 10) || 0);
  const newer = (a, b) => { const [x, y] = [parts(a), parts(b)]; for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] > y[i]; return false; };
  const current = state.version && d.latest && newer(d.latest, state.version)
    ? ` <span class="min-note">you have ${esc(state.version)}</span>` : "";
  box.innerHTML = `<div class="about-stats">
      <div><span class="n">${esc(d.latest || "–")}</span><span class="muted">latest${current}</span></div>
      <div><span class="n">${n(d.last_day)}</span><span class="muted">downloads, last day</span></div>
      <div><span class="n">${n(d.last_week)}</span><span class="muted">last week</span></div>
      <div><span class="n">${n(d.last_month)}</span><span class="muted">last month</span></div>
      <div><span class="n">${n(d.total)}</span><span class="muted">in all${d.counted_from ? `, since ${esc(d.counted_from)}` : ""}</span></div></div>
    <p class="muted">${d.releases} release(s)${d.first_release ? `, the first on ${esc(d.first_release)}` : ""}. Downloads from pypistats.org, without
      mirrors, updated once a day; installs through brew or pipx count too. Read ${esc(ago(d.read_at))}. ${links}</p>`;
  wireAboutPypi();
}

function wireAboutPypi() {
  const again = $("about-pypi-refresh");
  if (again) again.addEventListener("click", () => fillAboutPypi(true));
}

// The account menu (header, right): sign-in, Refresh access, Sign in / out, theme and About in one place.
$("account-button").addEventListener("click", () => {
  for (const id of ["subs-panel", "rgs-panel", "saved-panel"]) $(id).classList.add("hidden");
  $("account-panel").classList.toggle("hidden");
});
document.addEventListener("click", (event) => {
  const panel = $("account-panel");
  if (panel.classList.contains("hidden")) return;
  const inside = event.target.closest(".account");
  if (!inside || event.target.closest("#refresh, #signin-again, #about-open")) panel.classList.add("hidden");
});
$("about-close").addEventListener("click", () => $("about").close());
$("about").addEventListener("click", (event) => { if (event.target === $("about")) $("about").close(); });

// The title goes back to Triage in place (keeping the scope); a new tab or window opens the console fresh.
$("home").addEventListener("click", (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  showView(personaHome());
});

// The header, menus and scope bar wrap with the window, so Browse's height is measured from where they end.
function fitTopChrome() {
  const bars = ["alert-bar", "session-bar", "persona-first"].map($).concat([".topbar", ".viewbar", ".scopebar"].map((q) => document.querySelector(q)));
  const bottom = Math.max(0, ...bars.filter((el) => el && el.offsetParent !== null).map((el) => el.getBoundingClientRect().bottom + scrollY));
  const value = `${Math.ceil(bottom)}px`;
  if (document.documentElement.style.getPropertyValue("--top-chrome") !== value) document.documentElement.style.setProperty("--top-chrome", value);
}
if (window.ResizeObserver) {  // the bars only: watching the body would loop, since the value resizes it
  const watch = new ResizeObserver(() => requestAnimationFrame(fitTopChrome));
  for (const el of [...[".topbar", ".viewbar", ".scopebar"].map((q) => document.querySelector(q)),
    ...["alert-bar", "session-bar", "persona-first"].map($)]) if (el) watch.observe(el);
}
addEventListener("resize", fitTopChrome);

async function start() {
  const params = new URLSearchParams(location.search);
  if (params.get("tenant")) { state.tenant = params.get("tenant"); state.tenantPinned = true; }
  state.legacyWatchMinutes = Number(stored.get("ahc.watchInterval", 0)) / 60;
  try {
    await refreshIntegrationTabs();
    await loadPersonas();
    await loadContext();
    state.groupView = state.graphAuto ? "graph" : "table";
    state.watchItems = await api("api/watchlist");
    startSessionWatch();
    startAlerts();
    loadAccessBadge();
    if (await signInAvailable()) { $("signin-again").classList.remove("hidden"); mergeCliTenants(); }
  } catch (error) {
    if (error.signedOut) return;  // the sign-in screen is up and takes over from here
    $("scope-summary").textContent = "";
    showView("triage");
    $("triage-cards").innerHTML = `<p class="notice">The console couldn't start: ${esc(error.message)}</p>
      <button type="button" onclick="location.reload()">Try again</button>`;
    return;
  }
  if (params.get("tab") || params.get("node")) {
    state.pendingDetail = { node: params.get("node") || params.get("id") || "", tab: params.get("tab") || "overview" };
  }
  if (params.get("id")) {
    const depth = Number(params.get("depth") || 0);
    startSnapshot({ kind: "resource", id: params.get("id"), name: params.get("id").split("/").pop(),
      depth, inbound: depth > 0 && $("inbound").checked });
  } else if (params.get("rg")) {
    const group = params.get("rg");
    startSnapshot({ kind: "group", group, name: group.split("/").slice(1).join("/") });
  } else {
    showView(params.get("view") || personaHome());
  }
}

start();
