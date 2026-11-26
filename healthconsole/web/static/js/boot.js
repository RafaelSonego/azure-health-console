"use strict";

// ------------------------------------------------------------------ theme
// Like rafaelsonego.com: System by default, Light or Dark when picked (kept in this browser). The graph and charts read
// colours when drawn, so a change repaints them.

function currentTheme() {
  return stored.choice("ahc.theme", ["light", "dark"], "system");
}

function applyTheme(value) {
  if (value === "light" || value === "dark") document.documentElement.setAttribute("data-theme", value);
  else document.documentElement.removeAttribute("data-theme");
  document.querySelectorAll(".theme-switch button[data-theme-value]").forEach((b) => b.classList.toggle("active", b.dataset.themeValue === value));
  if (state.chart && state.chartArgs) drawChart(...state.chartArgs);
}

document.querySelectorAll(".theme-switch button[data-theme-value]").forEach((button) => button.addEventListener("click", () => {
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
      <p>${u.command ? `Upgrade with <code>${esc(u.command)}</code> <button type="button" class="link" data-copy-update>copy</button>, then restart the console.`
        : `Upgrade with <a href="${safeHref(u.steps)}" target="_blank" rel="noopener">the upgrade steps</a>: they check the new version's files before installing them (<code>azure-health-console verify</code>). Then restart the console.`}</p>
      <p class="muted">${u.notes ? `<a href="${safeHref(u.notes)}" target="_blank" rel="noopener">What's new in ${esc(u.latest)}</a> · ` : ""}
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
  const links = `<a href="${safeHref(d.project_page)}" target="_blank" rel="noopener">Project page</a> ·
    <a href="${safeHref(d.stats_page)}" target="_blank" rel="noopener">pypistats</a> ·
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
  if (!inside || event.target.closest("#refresh, #signin-again, #signout-menu, #about-open")) panel.classList.add("hidden");
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

// Partial-result notes (some subscriptions didn't answer, a list was cut) get a ×. A closed note stays away until its
// text changes (Refresh brings another answer) or the page reloads; every page's notes get it, wherever they're drawn.
const closedNotices = new Set();
function closableNotices(root) {
  const notes = root.matches(".notice.partial") ? [root] : [...root.querySelectorAll(".notice.partial")];
  for (const note of notes) {
    if (note.dataset.noticeKey) continue;
    const key = note.textContent.trim();
    if (closedNotices.has(key)) { note.remove(); continue; }
    note.dataset.noticeKey = key;
    note.insertAdjacentHTML("afterbegin", '<button type="button" class="notice-close" data-notice-close title="Close this note (it comes back if it changes)" aria-label="Close">×</button>');
  }
}
new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => n.nodeType === 1 && closableNotices(n))))
  .observe(document.body, { childList: true, subtree: true });
closableNotices(document.body);
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-notice-close]");
  if (!button) return;
  const note = button.closest(".notice.partial");
  closedNotices.add(note.dataset.noticeKey);
  note.remove();
});

// Browse's list folds away for more room (« in its heading), remembered in this browser. Folded, the strip on the left
// says what's hidden (In scope, its count, the filter if one is set), so it reads as the way back.
{
  const layout = $("view-browse");
  const describe = () => {
    $("pane-unfold-count").textContent = $("rg-count").textContent;
    const filter = $("rg-filter").value.trim();
    $("pane-unfold-filter").textContent = filter ? `filter: ${filter}` : "";
    $("pane-unfold-filter").classList.toggle("hidden", !filter);
    $("pane-unfold").title = `Show the list of groups and resources${filter ? ` (filtered: ${filter})` : ""}`;
  };
  const fold = (folded) => {
    if (folded) describe();
    layout.classList.toggle("pane-folded", folded);
    $("pane-fold").setAttribute("aria-expanded", String(!folded));
    stored.set("ahc.browsePaneFolded", folded ? "1" : null);
  };
  $("pane-fold").addEventListener("click", () => fold(true));
  $("pane-unfold").addEventListener("click", () => fold(false));
  new MutationObserver(describe).observe($("rg-count"), { childList: true, characterData: true, subtree: true });
  $("rg-filter").addEventListener("input", describe);
  if (stored.get("ahc.browsePaneFolded") === "1") fold(true);
}

// Back to the top on every page once its header has scrolled away (TOP_AFTER px): the window scrolls the views, `main`
// a resource's page. Scroll events don't bubble, so one listener in the capture phase hears whichever of them scrolls.
const TOP_AFTER = 200;
{
  const button = $("to-top"), main = document.querySelector("main");
  const pane = () => document.querySelector("body.layout-panes .view:not(.hidden):not(.layout)");  // a view scrolling itself
  const scrolled = () => Math.max(window.scrollY, main && main.offsetParent ? main.scrollTop : 0, pane()?.scrollTop || 0);
  const update = () => button.classList.toggle("hidden", scrolled() < TOP_AFTER);
  document.addEventListener("scroll", update, { passive: true, capture: true });
  window.addEventListener("resize", update, { passive: true });
  button.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (main) main.scrollTo({ top: 0, behavior: "smooth" });
    pane()?.scrollTo({ top: 0, behavior: "smooth" });
  });
}

// Layout: classic (the page scrolls) or panes (the bars stay put; each part of the window scrolls once). Account menu,
// or ?layout=panes|classic; kept in this browser.
function applyLayout(choice) {
  const panes = choice === "panes";
  stored.set("ahc.layout", panes ? "panes" : null);
  if (panes) window.scrollTo(0, 0);  // the window itself no longer scrolls
  document.body.classList.toggle("layout-panes", panes);
  document.querySelectorAll(".layout-switch button").forEach((b) => b.classList.toggle("active", b.dataset.layoutValue === (panes ? "panes" : "classic")));
}
applyLayout(new URLSearchParams(location.search).get("layout") || stored.get("ahc.layout") || "classic");
document.querySelectorAll(".layout-switch button").forEach((b) => b.addEventListener("click", () => applyLayout(b.dataset.layoutValue)));
// Each view's pinned header height, so table headers stick just under it.
{
  const heads = new ResizeObserver((entries) => entries.forEach(({ target }) => target.closest(".view")
    ?.style.setProperty("--head-h", `${Math.ceil(target.getBoundingClientRect().height)}px`)));
  document.querySelectorAll(".view > .view-head, #view-browse .pane-head").forEach((head) => heads.observe(head));
}

sectionizeTree(document.body);
start();
