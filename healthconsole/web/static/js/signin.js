"use strict";

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
    <div class="signin-tenant-field hidden"><label for="signin-screen-tenant">Tenant <span class="muted">(optional: a
      personal account, or a tenant that asks for multi-factor sign-in, needs it)</span></label>
      <input id="signin-screen-tenant" type="text" placeholder="Tenant ID or domain, e.g. contoso.onmicrosoft.com"
        autocomplete="off" value="${esc(info.reason === "tenant" ? state.tenant || "" : "")}"></div>
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
      document.querySelector("#signin .signin-tenant-field").classList.remove("hidden");
      const field = $("signin-screen-tenant");
      field.addEventListener("input", debounce(() => renderSignInControls(box, field.value.trim()), 300));
      renderSignInControls(box, field.value.trim());
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
  showMessage(`Signing ${account} out…`);
  try {
    const response = await fetch(`${BASE}api/signout`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: account }) });
    const data = await response.json();
    if (!response.ok) { showMessage(data.error || "The sign-out didn't work.", true); return; }
  } catch (error) {
    showMessage(error.message, true);
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
  renderSignOutItem();
}

// The account menu's Sign out names the account the page is using; hidden until the Azure CLI's list says which.
function signedInAccount(tenant = state.tenant) {
  return ((state.cliTenants || []).find((t) => t.id === tenant) || {}).account || "";
}
function renderSignOutItem(tenant = state.tenant) {
  const account = signedInAccount(tenant);
  $("signout-account").textContent = account;
  $("signout-menu").classList.toggle("hidden", !account);
}
$("signout-menu").addEventListener("click", () => { const account = signedInAccount(); if (account) signOut(account); });
$("tenant").addEventListener("change", (event) => renderSignOutItem(event.target.value));

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
      ? `<div class="device-code">Open <a href="${safeHref(data.url)}" target="_blank" rel="noopener">${esc(data.url)}</a> on any device
          and enter <code class="command">${esc(data.code)}</code> <button type="button" data-signin-copy="${esc(data.code)}">Copy code</button>
          <button type="button" class="link" data-signin-cancel>cancel</button></div>`
      : `${loading(data.device_code ? "Waiting for the sign-in code…" : "Finish the sign-in in the browser window that opened…")}
          <button type="button" class="link" data-signin-cancel>cancel</button>
          ${data.device_code ? "" : '<p class="muted">No window? Cancel and use a code instead.</p>'}`;
    box._signinTimer = setTimeout(() => signInPoll(box, true), SIGNIN_POLL_MS);
  } else if ((data.skipped || []).length && started && (data.status === "done" || data.status === "failed")) {
    clearInterval(state.signInTimer);  // keep the choice on screen; Continue reloads
    progress.innerHTML = skippedHtml(data, output);
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

// Tenants `az login` skipped (it can't show their MFA prompt without --tenant), each with its own Sign in button.
function skippedHtml(data, output) {
  const signedIn = data.status === "done";
  const rows = data.skipped.map((t) => `<li><strong>${esc(t.name || t.id)}</strong>: ${t.mfa
    ? "Azure asked for multi-factor authentication, which it only shows when you sign in to that tenant on its own."
    : esc(t.reason)} <button type="button" class="primary" data-signin-tenant="${esc(t.id)}">Sign in to ${esc(t.name || "this tenant")}</button></li>`).join("");
  return `<div class="notice">${signedIn ? "Signed in, but this tenant was left out:" : "Nothing was signed in. This tenant needs a sign-in of its own:"}
    <ul>${rows}</ul>${signedIn ? '<button type="button" data-signin-continue>Continue without it</button>' : ""}</div>${output}`;
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-signin-start], [data-signin-code], [data-signin-cancel], [data-signin-copy], " +
    "[data-signin-tenant], [data-signin-continue]");
  if (!el) return;
  const box = el.closest("[data-signin-box]");
  if (el.dataset.signinContinue !== undefined) {
    location.reload();
  } else if (el.dataset.signinTenant && box) {
    if (box.closest("#signin") && $("signin-screen-tenant")) $("signin-screen-tenant").value = el.dataset.signinTenant;
    renderSignInControls(box, el.dataset.signinTenant);
    startSignIn(box, false);
  } else if (el.dataset.signinCopy) {
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
  const account = signedInAccount();
  panel.innerHTML = `<div class="signin-panel-head"><strong>Switch account or tenant</strong>
      <span class="muted">Sign in to switch account, add a tenant (another account's too), or after activating a PIM group
        (Refresh access is enough for Azure roles).</span>
      <span class="spacer"></span><button type="button" class="link" id="signin-panel-close">close</button></div>
    <label class="inline"><input type="radio" name="signin-tenant" value="${esc(state.tenant || "")}" checked> this tenant (${esc(name || "current")})</label>
    <label class="inline"><input type="radio" name="signin-tenant" value=""> my default tenant</label>
    <label class="inline"><input type="radio" name="signin-tenant" value="other"> another tenant</label>
    <input id="signin-other" type="text" class="hidden" placeholder="Tenant ID or domain, e.g. contoso.onmicrosoft.com" autocomplete="off"
      aria-label="Tenant ID or domain">
    <div data-signin-box></div>
    ${account ? `<p class="muted">Signed in to ${esc(name)} as <strong>${esc(account)}</strong>. To sign out, use
      <strong>Sign out</strong> in your account menu.</p>` : ""}`;
  const box = panel.querySelector("[data-signin-box]");
  const other = $("signin-other");
  renderSignInControls(box, state.tenant || "");
  panel.querySelectorAll('input[name="signin-tenant"]').forEach((r) => r.addEventListener("change", () => {
    other.classList.toggle("hidden", r.value !== "other");
    if (r.value === "other") { other.focus(); renderSignInControls(box, other.value.trim()); } else renderSignInControls(box, r.value);
  }));
  other.addEventListener("input", debounce(() => renderSignInControls(box, other.value.trim()), 300));
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

