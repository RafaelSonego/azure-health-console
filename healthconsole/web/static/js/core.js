"use strict";

const BASE = document.currentScript.src.replace(/static\/js\/[^/]*$/, "");

// The local console's key: taken once from the launch address's #fragment (never sent to a server), kept for this
// origin only (port included, unlike a cookie) and sent as a header, which another site can't add.
const LAUNCH_KEY_ITEM = "ahc-launch-key";
const LAUNCH_HEADER = "X-AHC-Key";
(() => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const key = fragment.get("key");
  if (key === null) return;
  if (/^[A-Za-z0-9_-]{32,128}$/.test(key)) localStorage.setItem(LAUNCH_KEY_ITEM, key);
  history.replaceState(null, "", location.pathname + location.search);
})();
const pageFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const key = localStorage.getItem(LAUNCH_KEY_ITEM);
  const target = new URL(input instanceof Request ? input.url : String(input), location.href);
  if (key && target.origin === location.origin) {
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    headers.set(LAUNCH_HEADER, key);
    init = { ...init, headers };
  }
  const response = await pageFetch(input, init);
  if (response.status === 401 && target.origin === location.origin) {
    const body = await response.clone().json().catch(() => ({}));
    if (body.kind === "launch-key") showNoLaunchKey(body.error);
  }
  return response;
};
let launchKeyRefused = false;  // the page is gone: nothing more to send, error reports included
function showNoLaunchKey(text) {
  launchKeyRefused = true;
  localStorage.removeItem(LAUNCH_KEY_ITEM);
  const message = document.createElement("p");
  message.style.cssText = "font-family:sans-serif;margin:2em";
  message.textContent = text || "";
  document.body.replaceChildren(message);
}
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

// Links carry outside data (runbooks, ServiceNow, Advisor): only http(s) ones become links, so `javascript:` never runs.
function safeHref(url) {
  const text = url == null ? "" : String(url);
  try {
    return /^https?:$/.test(new URL(text, location.href).protocol) ? esc(text) : "";
  } catch (error) { return ""; }
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

// Every search box's × (drawn by style.css while it holds text): a click on its right end empties the box, and the
// box's own `input` listeners then redraw whatever it filters.
const nearClear = (event) => event.target.matches && event.target.matches('input[type="search"]') && event.target.value
  && event.clientX > event.target.getBoundingClientRect().right - 26;
document.addEventListener("click", (event) => {
  if (!nearClear(event)) return;
  event.target.value = "";
  event.target.dispatchEvent(new Event("input", { bubbles: true }));
  event.target.style.cursor = "";
});
document.addEventListener("mousemove", (event) => {
  if (event.target.matches && event.target.matches('input[type="search"]')) event.target.style.cursor = nearClear(event) ? "pointer" : "";
});

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


// An error in the page's own script goes to Settings → Logs (each one once, at most 20 per page load).
const pageErrorsSent = new Set();
function reportPageError(message, where, stack) {
  const key = `${message}|${where}`;
  if (launchKeyRefused || pageErrorsSent.has(key) || pageErrorsSent.size >= 20) return;
  pageErrorsSent.add(key);
  api("api/client-errors", { method: "POST", body: { message: String(message || ""), where, stack: stack || "", view: state.view || "" } })
    .catch(() => {});  // the log is a help, never a second error
}
window.addEventListener("error", (event) => reportPageError(event.message,
  `${String(event.filename || "").replace(/^.*\/static\//, "")}:${event.lineno || 0}:${event.colno || 0}`, event.error && event.error.stack));
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  if (reason && reason.signedOut) return;  // the sign-in screen already says it
  reportPageError((reason && reason.message) || String(reason), "a promise nobody waited for", reason && reason.stack);
});
