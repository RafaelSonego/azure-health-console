"use strict";

// ---------------------------------------------------------------- sections
// One look for every page: a heading and what follows it (up to the next heading of its level), or a `.layer`, becomes
// a box with a header bar (title, its source, ⓘ for the long explanation, its own controls) and a body. A watcher runs
// it on whatever any page renders, so pages don't build it themselves. A critical or warning row colours its side.
const SECTION_SKIP = ".section, .card, .inv-step, .settings-section, .logs-panel, .how-it-works, .persona-first, .start-hint, "
  + ".explore-head, .vnet-head, .view-head, .gs-explain, .signin, .signin-panel, .search-results, .tab-tools, summary, header, td";

function sectionTitle(head) {
  const own = [...head.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" ");
  return own.replace(/\([^)]*\)/g, "").replace(/\s+/g, " ").trim().slice(0, 40);
}

function buildSection(box, head, nodes) {
  box.classList.remove("layer");
  box.classList.add("section");
  const header = document.createElement("header");
  header.className = "section-head";
  const body = document.createElement("div");
  body.className = "section-body";
  if (head) {
    if (head.classList.contains("hidden")) { head.classList.remove("hidden"); box.classList.add("hidden"); }
    const last = [...head.childNodes].reverse().find((n) => n.nodeType === 3 && n.textContent.trim());
    const source = last && /^(.*)\(([^()]+)\)\s*$/.exec(last.textContent);
    if (source) {
      last.textContent = `${source[1].trimEnd()} `;
      head.insertAdjacentHTML("beforeend", `<span class="section-source">${esc(source[2])}</span>`);
    }
    for (const muted of head.querySelectorAll(":scope > span.muted")) {
      const m = /^\((.+)\)$/.exec(muted.textContent.trim());
      if (m && !muted.id && !muted.children.length) { muted.className = "section-source"; muted.textContent = m[1]; }
    }
    header.append(head);
    if (!box.dataset.jump && !nodes.some((n) => n.nodeType === 1 && (n.matches("[data-jump]") || n.querySelector("[data-jump]")))) {
      const title = sectionTitle(head);
      if (title) box.dataset.jump = title;
    }
  }
  const tools = document.createElement("div");
  tools.className = "section-tools";
  header.append(tools);
  box.prepend(header);
  for (const n of nodes) body.append(n);
  const first = [...body.children][0];
  if (first && first.matches("p.muted") && first.textContent.trim().length > 100 && !first.id) {
    first.classList.add("section-info", "hidden");
    tools.insertAdjacentHTML("beforeend", '<button type="button" class="section-info-btn" data-section-info title="What this section shows" aria-expanded="false">ⓘ</button>');
  }
  for (const t of body.querySelectorAll(":scope > [data-section-tools]")) tools.prepend(t);
  box.append(body);
  sectionTone(box);
}

function sectionTone(box) {
  const body = box.querySelector(":scope > .section-body");
  if (!body) return;
  const has = (tone) => body.querySelector(`tbody .pill.${tone}, summary .pill.${tone}, .card.${tone}`);
  box.classList.toggle("tone-critical", !!has("critical"));
  box.classList.toggle("tone-warning", !has("critical") && !!has("warning"));
}

function sectionize(root) {
  if (!root || root.closest(SECTION_SKIP)) return;
  for (const layer of root.querySelectorAll(":scope > section.layer")) {
    const head = layer.querySelector(":scope > h3, :scope > h4");
    buildSection(layer, head, [...layer.childNodes].filter((n) => n !== head));
  }
  const tag = [...root.children].find((el) => el.tagName === "H3" || el.tagName === "H4")?.tagName;
  if (tag) {
    let head = null, nodes = [];
    const flush = () => {
      if (head && nodes.some((n) => n.nodeType === 1)) {
        const box = document.createElement("section");
        head.before(box);
        buildSection(box, head, nodes);
      }
      head = null; nodes = [];
    };
    for (const node of [...root.childNodes]) {
      if (node.nodeType === 1 && node.tagName === tag) { flush(); head = node; continue; }
      if (node.nodeType === 1 && node.matches(".tab-tools, .section, .subtabs, .view-head")) { flush(); continue; }
      if (head) nodes.push(node);
    }
    flush();
  }
  jumpBar(root);
}

// Four or more sections (or folds with a jump stop) and no toolbar of its own: a jump bar above them, kept current.
function jumpBar(root) {
  if (!root.matches("#tab-body, .view, .view > div")) return;
  let tools = root.querySelector(":scope > .tab-tools");
  if (!tools) {
    if (root.querySelectorAll(":scope > .section[data-jump]").length < 4) return;
    tools = document.createElement("div");
    tools.className = "tab-tools";
    tools.dataset.auto = "1";
    tools.innerHTML = '<nav class="jump-bar" aria-label="Sections"></nav>';
    root.querySelector(":scope > .section").before(tools);
  }
  renderTabTools(root);
}

function sectionizeTree(scope) {
  if (!scope || scope.nodeType !== 1 || scope.closest(SECTION_SKIP)) return;
  const parents = new Set();
  if (scope.querySelector(":scope > section.layer, :scope > h3, :scope > h4")) parents.add(scope);
  for (const h of scope.querySelectorAll("h3, h4, section.layer")) parents.add(h.parentElement);
  for (const p of parents) if (p && !p.closest(SECTION_SKIP)) sectionize(p);
}

new MutationObserver((records) => {
  for (const target of new Set(records.map((r) => r.target))) sectionizeTree(target);
}).observe(document.body, { childList: true, subtree: true });

document.addEventListener("click", (event) => {
  const info = event.target.closest("[data-section-info]");
  if (info) {
    const text = info.closest(".section").querySelector(":scope > .section-body > .section-info");
    const open = text.classList.toggle("hidden") === false;
    info.setAttribute("aria-expanded", String(open));
    info.classList.toggle("active", open);
    return;
  }
  // A view, sub-tab or fold may have shown or hidden sections: the automatic jump bars follow.
  setTimeout(() => document.querySelectorAll(".tab-tools[data-auto]").forEach((t) => renderTabTools(t.parentElement)), 0);
});

const CI_LOOK_BACK = [["", "default"], ["1", "1 hour"], ["6", "6 hours"], ["24", "24 hours"], ["72", "3 days"],
  ["168", "7 days"], ["336", "14 days"], ["720", "30 days"]];
const ciSpan = (hours) => (hours >= 24 && hours % 24 === 0 ? `last ${hours / 24 === 1 ? "24 h" : `${hours / 24} days`}` : `last ${hours === 1 ? "hour" : `${hours} h`}`);

// Every workload in scope, problems or not: its name opens Details (describe, pods, events, YAML) and Dependencies;
// its HPA and last rollout as columns.
function allWorkloadsTable(k) {
  const hpas = new Map((k.hpas || []).map((h) => [`${h.namespace}|${h.workload}`, h]));
  const rolled = new Map((k.rollouts || []).map((r) => [`${r.namespace}|${r.workload}`, r]));
  return table(["Namespace", "Kind", "Workload", "Ready", "HPA (min–max)", "HPA now", "Rolled out", "Checks"], (k.workload_rows || []).map((w) => {
    const h = hpas.get(`${w.namespace}|${w.workload}`), short = w.desired != null && (w.ready || 0) < w.desired;
    return [esc(w.namespace), esc(w.kind || w.workload.split("/")[0]), workloadButton(w.namespace, w.workload, w.workload.split("/").slice(1).join("/")),
      w.desired == null ? '<span class="muted">–</span>' : `<span data-v="${w.desired ? (w.ready || 0) / w.desired : 1}">${fmt(w.ready || 0)}/${fmt(w.desired)}${short ? ` ${pill("warning")}` : ""}</span>`,
      h ? `${fmt(h.min)}–${fmt(h.max)}` : '<span class="muted">none</span>',
      h ? `${fmt(h.current)}${h.current >= h.max ? ` ${pillHtml("critical", "at max")}${h.at_max_since ? ` <span class="muted" title="since ${esc(new Date(h.at_max_since).toLocaleString())}">for ${esc(sinceText(h.at_max_since))}</span>` : ""}` : ""}<div class="muted">${esc(h.targets)}</div>` : '<span class="muted">–</span>',
      rolloutHtml(rolled.get(`${w.namespace}|${w.workload}`)),
      w.findings ? `${pill(SEVERITY_STATUS[w.severity] || "info")} ${fmt(w.findings)}` : '<span class="ok">✓</span>'];
  }), "k-workloads");
}

// "a day", "2 days" or "36 h": the left-over age (Settings → AKS).
const hoursText = (h) => (h === 24 ? "a day" : h % 24 === 0 ? `${h / 24} days` : `${h} h`);

// The kinds All workloads lists, the portal's tabs as one picker: [value, label, which list].
const K_KINDS = [["", "All kinds", "workloads"], ["Deployment", "Deployments", "workloads"], ["StatefulSet", "Stateful sets", "workloads"],
  ["DaemonSet", "Daemon sets", "workloads"], ["CronJob", "Cron jobs", "workloads"], ["Job", "Jobs", "jobs"], ["Pod", "Pods", "pods"]];

function podStatusHtml(p, hours) {
  const why = p.reason ? ` <span class="muted">${esc(p.reason)}</span>` : "";
  if (p.phase === "Failed" && p.leftover) {
    return `${pillHtml("info", "Failed", { rank: 3.6, title: `Failed over ${hoursText(hours)} ago: left over, its workload has usually replaced it` })}${why} <span class="muted">· left over</span>`;
  }
  if (p.phase === "Running") return p.ready < p.containers ? `${pillHtml("warning", "Running, not ready")}${why}` : pillHtml("healthy", "Running");
  if (p.phase === "Succeeded") return pillHtml("healthy", "Completed", { rank: 3.5 });
  return `${pillHtml(p.phase === "Unknown" ? "unknown" : "warning", p.phase, { rank: 1 })}${why}`;  // a lost pod, not an unread one
}

function podsTable(k, hours) {
  const rows = k.pod_rows || [];
  if (!rows.length) return '<p class="muted">No pods in the namespaces shown.</p>';
  return table(["Namespace", "Pod", "Status", "Ready", "Restarts", "Since", "Node", "Workload"], rows.map((p) => [
    esc(p.namespace), podButton(p.namespace, p.pod), podStatusHtml(p, hours),
    `<span data-v="${p.containers ? p.ready / p.containers : 1}">${fmt(p.ready)}/${fmt(p.containers)}</span>`, fmt(p.restarts), seen(p.since),
    esc(p.node) || '<span class="muted">not placed</span>',
    p.workload ? workloadButton(p.namespace, p.workload) : esc(p.owner) || '<span class="muted">none</span>']), "k-pods");
}

const JOB_STATE = { Failed: "warning", Running: "info", Pending: "unknown", Complete: "healthy" };
function jobsTable(k) {
  const rows = k.job_rows || [];
  if (!rows.length) return '<p class="muted">No Jobs in the namespaces shown. A Job runs once to completion; Cron jobs start them on a schedule.</p>';
  const took = (j) => (j.finished && j.started ? duration((new Date(j.finished) - new Date(j.started)) / 1000) : j.state === "Running" ? "still running" : "–");
  return table(["Namespace", "Job", "Started by", "Status", "Pods succeeded", "Pods failed", "Started", "Took"], rows.map((j) => [
    esc(j.namespace), esc(j.job), j.cronjob ? `<span class="muted">cron job</span> ${esc(j.cronjob)}` : '<span class="muted">by hand or a tool</span>',
    pillHtml(JOB_STATE[j.state], j.state), fmt(j.succeeded), j.failed ? `<span class="worse">${fmt(j.failed)}</span>` : "0", seen(j.started), took(j)]), "k-jobs");
}

function wireKindPick(body) {
  const pick = body.querySelector("[data-k-kinds]");
  if (!pick) return;
  const show = (kind) => {
    const [, , view] = K_KINDS.find(([v]) => v === kind) || K_KINDS[0];
    pick.querySelectorAll("button").forEach((b) => { b.classList.toggle("active", b.dataset.kKind === kind); b.setAttribute("aria-pressed", String(b.dataset.kKind === kind)); });
    body.querySelectorAll("[data-k-view]").forEach((d) => d.classList.toggle("hidden", d.dataset.kView !== view));
    const list = body.querySelector('[data-k-view="workloads"] table[data-list]');
    if (list) { list.dataset.filterKind = view === "workloads" ? kind : ""; list.dataset.shown = LIST_PAGE; pageList(list); }
    stored.set("ahc.aksKind", kind || null);
  };
  pick.addEventListener("click", (event) => { const b = event.target.closest("[data-k-kind]"); if (b) show(b.dataset.kKind); });
  const saved = stored.choice("ahc.aksKind", K_KINDS.map(([v]) => v), "");
  show(pick.querySelector(`[data-k-kind="${saved}"]`)?.disabled ? "" : saved);  // a kind this cluster lacks opens All kinds
}

// Pods not running now, and apart (folded) the Failed ones past the left-over age: their workload has usually replaced them.
function notRunningHtml(rows, leftover, render, hours = 24) {
  const old = new Set((leftover || []).map((r) => `${r.namespace}/${r.pod}`));
  const live = rows.filter((r) => !old.has(`${r.namespace}/${r.pod}`)), kept = rows.filter((r) => old.has(`${r.namespace}/${r.pod}`));
  return `${live.length ? render(live, "k-not-running") : '<p class="ok">✓ None right now.</p>'}${kept.length
    ? `<details class="leftover"><summary>${fmt(kept.length)} Failed pod${kept.length === 1 ? "" : "s"} over ${hoursText(hours)} old <span class="muted">(left over: usually replaced already)</span></summary>${render(kept, "k-leftover")}</details>` : ""}`;
}

// Amounts written for people: two decimals under one (0.04 cores), one under 100 (31.6, not 32), whole above.
const amountText = (n) => n.toFixed(n < 1 ? 2 : n < 100 ? 1 : 0).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
const coresText = (m) => (m == null ? "–" : `${amountText(m / 1000)} cores`);
const gibText = (mib) => (mib == null ? "–" : `${amountText(mib / 1024)} GiB`);
const pctOf = (part, whole) => (whole ? Math.round((100 * part) / whole) : null);

// Each pool: its VM size once, then CPU and memory as "X of Y requested (N%)" against what the nodes running NOW give
// pods, what's used now, and the same requests if the autoscaler grows it fully. Amber only when it can't grow.
function poolsTable(pools, usage, warn) {
  const shape = (s) => `${s.vm_size || "VM size not labelled"}: ${coresText(s.cpu)} and ${gibText(s.memory)} per node. Each node keeps `
    + `${coresText(s.cpu - s.alloc_cpu)} and ${gibText(s.memory - s.alloc_memory)} for Kubernetes and the OS, so pods can use ${coresText(s.alloc_cpu)} and ${gibText(s.alloc_memory)} of it.`;
  const pool = (p) => `<strong title="${esc((p.shapes || []).map(shape).join("\n"))}">${esc(p.pool)}</strong> ${poolModeHtml(p.mode)}${(p.shapes || []).map((s) =>
    `<div class="muted" title="${esc(shape(s))}"><code>${esc(s.vm_size || "?")}</code> · ${esc(coresText(s.cpu).replace(" cores", " vCPU"))} · ${esc(gibText(s.memory))} per node</div>`).join("")}`;
  const toNodes = (p, label, problems = false) => `<button type="button" class="link" data-goto-nodes="${esc(JSON.stringify({ pool: p.pool, problems }))}"
    title="${problems ? "Its nodes that need a look" : "Its nodes"}, on the Nodes tab: conditions, taints, what runs on each, versions">${label} ›</button>`;
  const nodes = (p) => {
    const pr = p.profile, link = toNodes(p, `${fmt(p.nodes)} node${p.nodes === 1 ? "" : "s"}`);
    if (!pr) return link;
    if (!pr.autoscale) return `${link}<div class="muted">fixed size</div>`;
    const atMax = pr.max != null && p.nodes >= pr.max;
    return `${link}${atMax ? ` ${pillHtml("critical", "at max")}` : ""}<div class="muted" title="The cluster autoscaler adds or removes nodes in this range">autoscale ${fmt(pr.min ?? 0)}–${fmt(pr.max ?? 0)}</div>`;
  };
  const resource = (p, key, text) => {
    const alloc = p[`alloc_${key}`], req = p[`req_${key}`], used = p[`used_${key}`];
    if (!alloc) return '<span class="muted">–</span>';
    const pct = pctOf(req, alloc), amber = warn != null && pct > 100 * warn && p.can_grow !== true;
    const atMax = p[`req_${key}_at_max`], grows = atMax != null && p.max_nodes > p.nodes;
    return `<span data-v="${pct}"><strong>${esc(text(req))} of ${esc(text(alloc))} requested (${pct}%)</strong>
      <div title="${esc(`What pods can use on the ${p.nodes} node(s) running now: ${text(alloc)}`)}">${usageBar(pct, amber ? "warning" : "")}</div>
      <div class="muted">${usage ? `${esc(text(used))} used now (${pctOf(used, alloc)}%)` : "use now: metrics-server isn't answering"}</div>
      ${grows ? `<div class="muted" title="The same requests if the autoscaler grows the pool to its maximum of ${fmt(p.max_nodes)} nodes">if it grows to ${fmt(p.max_nodes)} nodes: ${atMax}% requested</div>` : ""}</span>`;
  };
  return table(["Pool", "Nodes", "Not ready", "CPU", "Memory"], pools.map((p) => [
    pool(p), nodes(p), p.not_ready ? `${pill("critical")} ${toNodes(p, fmt(p.not_ready), true)}` : "0",
    resource(p, "cpu", coresText), resource(p, "mem", gibText)]), "k-pools");
}

// The workloads using the most CPU or memory now (metrics-server), every pod of each wherever it runs: what they use,
// their share of the cluster, against what their pods request, and the pools they run on.
function topConsumersHtml(top, pools, by = "cpu") {
  if (!top || !top.available) return '<p class="muted">metrics-server isn\'t answering, so live use per workload can\'t be read.</p>';
  const total = { cpu: pools.reduce((a, p) => a + (p.alloc_cpu || 0), 0), memory: pools.reduce((a, p) => a + (p.alloc_mem || 0), 0) };
  const fig = (r, key) => {
    const text = key === "cpu" ? coresText : gibText, used = r[key], req = r[`${key}_request`];
    const share = pctOf(used, total[key]);
    return `<span data-v="${used}"><strong>${esc(text(used))} used</strong>${share != null ? ` <span class="muted" title="Of what the whole cluster's nodes give pods: ${esc(text(total[key]))}">(${share}% of the cluster)</span>` : ""}
      <div class="muted">${req ? `${pctOf(used, req)}% of the ${esc(text(req))} its pods request` : "its pods request none"}</div></span>`;
  };
  const rows = top[by === "cpu" ? "by_cpu" : "by_memory"];
  return `<p class="muted">Every pod of each workload, wherever it runs, added up, right now.</p>
    <div class="top-by">Busiest by <button type="button" class="link${by === "cpu" ? " on" : ""}" data-top-by="cpu">CPU</button>
      <button type="button" class="link${by === "memory" ? " on" : ""}" data-top-by="memory">memory</button></div>
    ${table(["Namespace", "Workload", "Pods", "CPU now", "Memory now", "Its busiest pods"], rows.map((r) => [
      esc(r.namespace),
      `${r.workload.startsWith("Pod/") ? esc(r.workload) : workloadButton(r.namespace, r.workload)}${(r.pools || []).length
        ? `<div class="muted">runs on: ${esc(r.pools.join(", "))}</div>` : ""}`,
      fmt(r.pod_count), fig(r, "cpu"), fig(r, "memory"),
      r.pods.map((p) => `<div>${podButton(r.namespace, p.pod)} <span class="muted">${coresText(p.cpu)} · ${gibText(p.memory)}</span></div>`).join("")]), `k-top-${by}`)}`;
}

// Workloads' pages: one answer each, with the toolbar above them all; the last one opened is kept in this browser.
const AKS_SUBS = [["problems", "What's wrong now"], ["workloads", "All workloads"], ["memory", "Memory & scaling"], ["capacity", "Capacity"], ["setup", "Setup risks"], ["evidence", "Pods & events"]];

// Memory & scaling: one row per workload with the facts a memory or scaling problem shows in, side by side and sortable.
// The Kubernetes API gives now (replicas, HPA at max since, memory against request, runtime caps); Container Insights or
// the monitoring tool the week (restarts, OOM kills; the tool also memory against limit and HPA time at max).
const capSummary = (caps) => (caps || []).filter((c) => c.runtime || c.cap_mib != null).map((c) => `${esc(c.runtime)}${c.of_limit != null
  ? ` ${fmt(c.of_limit)}% of limit` : c.cap_mib != null ? ` ${memText(c.cap_mib)}` : " no cap"}`).join("<br>") || '<span class="muted" title="None in its env or arguments; the image or an envFrom may still set one">none in its spec</span>';
function memoryScalingHtml(k, week) {
  const hpas = new Map((k.hpas || []).map((h) => [`${h.namespace}|${h.workload}`, h]));
  const weekRows = new Map((week.workloads || []).map((r) => [`${r.namespace}|${r.workload}`, r]));
  const atMaxShare = new Map((week.hpas || []).map((r) => [`${r.namespace}|${r.hpa}`, r.at_max_percent]));
  const seen = new Set();
  const rows = (k.workload_rows || []).filter((w) => w.kind !== "CronJob").map((w) => {
    const name = w.workload.split("/").slice(1).join("/"), key = `${w.namespace}|${name}`, h = hpas.get(`${w.namespace}|${w.workload}`), r = weekRows.get(key) || {};
    seen.add(key);
    const share = h ? atMaxShare.get(`${w.namespace}|${h.name}`) : undefined;
    return [esc(w.namespace), workloadButton(w.namespace, w.workload, name),
      h ? `<span data-v="${h.max ? h.current / h.max : 0}">${fmt(h.current)} of ${fmt(h.max)}</span>${h.current >= h.max ? ` ${pillHtml("critical", "at max", { rank: null })}` : ""}`
        : w.desired == null ? "–" : `<span data-v="0">${fmt(w.ready || 0)}/${fmt(w.desired)}</span> <span class="muted">no HPA</span>`,
      `<span data-v="${h && h.at_max_since ? Date.now() - new Date(h.at_max_since) : -1}">${h && h.at_max_since ? `for ${esc(sinceText(h.at_max_since))}` : "–"}</span>${share != null
        ? `<div class="muted" data-share="${share}">${fmt(share)}% of the last 7 days</div>` : ""}`,
      w.memory_of_request == null ? '<span class="muted" data-v="-1">–</span>' : `<span data-v="${w.memory_of_request}" class="${w.memory_of_request >= 100 ? "worse" : ""}">${fmt(w.memory_of_request)}%</span>`,
      r.max_of_limit == null ? '<span class="muted" data-v="-1">–</span>' : `<span data-v="${r.max_of_limit}" class="${r.max_of_limit >= 90 ? "worse" : ""}">${fmt(r.max_of_limit)}%</span>`,
      r.restarts == null ? '<span class="muted" data-v="-1">–</span>' : `<span data-v="${r.restarts}" class="${r.restarts ? "worse" : ""}">${fmt(r.restarts)}</span>`,
      r.oom_pods == null ? '<span class="muted" data-v="-1">–</span>' : `<span data-v="${r.oom_pods}" class="${r.oom_pods ? "worse" : ""}">${fmt(r.oom_pods)}</span>`,
      capSummary(w.runtime_caps)];
  });
  const unknown = (week.workloads || []).filter((r) => !seen.has(`${r.namespace}|${r.workload}`) && (r.restarts || r.oom_pods));
  const from = week.found ? `the last 7 days from <strong>${esc(week.source)}</strong>` : `no 7-day source: ${esc(week.reason || "")}`;
  return `<p class="muted">Facts to read a memory or scaling problem from, side by side; sort by any column (click its header). <em>Now</em> comes
      from the Kubernetes API; restarts, OOM kills${week.source && week.source !== "Container Insights" ? ", memory against limit and HPA time at max" : ""} from ${from}.
      A workload's name opens its details, with each pod's memory over the week.</p>
    ${table(["Namespace", "Workload", "Replicas (HPA)", "At max", "Memory now, of request", "Memory peak, of limit (7 d)", "Restarts (7 d)", "Pods OOM-killed (7 d)", "Runtime memory cap"], rows, "k-memory")}
    ${unknown.length ? `<p class="muted">Also in the week, not running now: ${unknown.map((r) => `${esc(r.namespace)}/${esc(r.workload)} (${fmt(r.restarts)} restarts, ${fmt(r.oom_pods)} OOM-killed pods)`).join(", ")}.</p>` : ""}
    <details class="mon-query"><summary>Queries sent</summary>${queryBlock({ queries: week.queries || [] })}</details>`;
}
function subBadge(body, key, n, bad) {
  const badge = body.querySelector(`[data-sub-n="${key}"]`);
  if (!badge) return;
  badge.hidden = !n;
  badge.textContent = n ? fmt(n) : "";
  badge.classList.toggle("critical", !!bad);
}

// The sub-tabs' and kind buttons' counts for what the Namespace pick and Filter leave (their rows, as each table shows).
function recountAksSubs(body, problemsBad) {
  const matching = (key, keep = () => true) => {
    const t = body.querySelector(`table[data-list="${key}"]`);
    return t ? listGroups(t).filter((rows) => rowMatches(t, rows[0], { kind: false }) && keep(t, rows[0])).length : 0;
  };
  const named = (t, row) => { const col = namespaceColumn(t); return col < 0 || !!(row.cells[col] && row.cells[col].textContent.trim()); };
  subBadge(body, "problems", matching("aks-problems", named), problemsBad);
  subBadge(body, "workloads", matching("k-workloads"), false);
  subBadge(body, "setup", matching("k-config"), false);
  const list = body.querySelector('table[data-list="k-workloads"]'), kindCol = list ? headedColumn(list, "Kind") : -1;
  for (const b of body.querySelectorAll("[data-k-kind]")) {
    const kind = b.dataset.kKind, [, , view] = K_KINDS.find(([v]) => v === kind) || K_KINDS[0];
    const n = view === "pods" ? matching("k-pods") : view === "jobs" ? matching("k-jobs")
      : matching("k-workloads", (t, row) => !kind || (kindCol >= 0 && row.cells[kindCol].textContent.trim() === kind));
    b.querySelector(".muted").textContent = fmt(n);
  }
}

function wireAksSubs(body, counts) {
  const show = (key) => {
    body.querySelectorAll(".aks-sub").forEach((d) => d.classList.toggle("hidden", d.dataset.sub !== key));
    body.querySelectorAll("[data-aks-sub]").forEach((b) => { b.classList.toggle("active", b.dataset.aksSub === key); b.setAttribute("aria-selected", String(b.dataset.aksSub === key)); });
    stored.set("ahc.aksSub", key === "problems" ? null : key);
    if (key === "memory" && body.loadMemoryScaling) body.loadMemoryScaling();
    renderTabTools(body);
  };
  for (const [key, [n, bad]] of Object.entries(counts)) subBadge(body, key, n, bad);
  body.querySelector(".aks-subtabs").addEventListener("click", (event) => { const b = event.target.closest("[data-aks-sub]"); if (b) show(b.dataset.aksSub); });
  body.showAksSub = show;
  show(stored.choice("ahc.aksSub", AKS_SUBS.map(([key]) => key), "problems"));
}

async function tabWorkloads(node, body, includeSystem = state.aksSystem || false) {
  body.innerHTML = loading("Reading pods from platform metrics, Container Insights and the Kubernetes API…");
  const data = await exploreGet("workloads", node, { ...(includeSystem ? { system: "1" } : {}), ...(state.aksCiHours ? { ci_hours: state.aksCiHours } : {}) });
  const phases = Array.isArray(data.pod_phases) ? data.pod_phases : [];
  const ci = data.container_insights || {};
  const k = data.kubernetes || {};
  // A copy: New Relic's kills are merged into it, and the read itself is cached and drawn again (Reset, a toggle).
  const problems = structuredClone(data.problems || { groups: [], leftover: [], verdict: { read: [], missing: [], cluster: [], rolled_out: [], rolling: [], leftover: 0 } });
  // Container Insights' lists read one row past their limit: any that came back cut is named, never silently short.
  const CI_NAMES = { not_running: "pods not running", restarts: "restarts", events: "warning events", hot_containers: "near limits", oomkilled: "OOM kills" };
  const ciCut = Object.entries(ci.truncated || {}).map(([key, n]) => `${CI_NAMES[key] || key} at ${fmt(n)}`).join(", ");
  const ciCutHtml = ciCut ? noticeHtml(`Container Insights' lists stopped at their limit: ${ciCut}. What's wrong now may be missing workloads past them; pick a namespace to narrow it.`) : "";

  const oomTool = toolKinds().find((kind) => (state.selected || {})[kind]);
  const lookBack = ci.available || !!oomTool;
  const lb = data.look_back_defaults || {};  // Look back's *default*: each list's own window, from Settings → AKS
  const lookBackDefaults = [["restarts", lb.restarts], ["OOM kills", lb.oomkilled], ["warning events", lb.events]]
    .filter(([, h]) => h).map(([what, h]) => `${what} ${h % 24 || h === 24 ? `${h} h` : `${h / 24} days`}`).join(" · ");
  const showing = [
    lookBack ? `restarts, OOM kills and warning events: ${state.aksCiHours ? esc(ciSpan(Number(state.aksCiHours))) : `default (${esc(lookBackDefaults)})`}; the rest right now` : "",
    k.available ? (includeSystem ? "system namespaces included" : "system namespaces hidden") : ""];
  let html = `<div class="tab-tools" data-showing="${esc(JSON.stringify(showing.filter(Boolean)))}" data-changed="${state.aksCiHours || includeSystem ? 1 : 0}">
    <div class="list-tools aks-tools"><label class="tool-field"><span class="tool-label">Namespace</span><select id="aks-ns" data-ns-filter aria-label="Show one namespace" title="Every section to one namespace; the count is its rows in the sections with problems. System namespaces are hidden from All namespaces: pick one, or All namespaces, including system"><option value="">All namespaces</option></select></label>
      <label class="tool-field"><span class="tool-label">Go to workload</span><span class="go-wrap"><input type="search" id="aks-go" autocomplete="off" spellcheck="false" placeholder="Go to workload…" aria-label="Go to a workload"
        title="Type part of its name (or namespace/name) and pick it: its Details and Dependencies open below" role="combobox" aria-expanded="false" aria-controls="aks-go-list"><span class="go-list" id="aks-go-list" role="listbox" hidden></span></span></label>
      <label class="tool-field"><span class="tool-label">Filter</span><input type="search" id="aks-filter" placeholder="workload, pod, reason…" aria-label="Filter the workload sections"></label>
      ${lookBack ? `<label class="tool-field" title="How far back restarts, OOM kills and warning events look, in Container Insights and ${esc(oomTool ? toolLabel(oomTool) : "the monitoring tool")}. Default: ${esc(lookBackDefaults)} (Settings → AKS). The workspace keeps 30 days by default"><span class="tool-label">Look back</span>
        <select id="ci-look-back">${CI_LOOK_BACK.map(([v, l]) => `<option value="${v}" ${v === (state.aksCiHours || "") ? "selected" : ""}${v ? "" : ` title="${esc(lookBackDefaults)}"`}>${l}</option>`).join("")}</select></label>` : ""}
      <span class="tool-field"><span class="tool-label">Sections</span><span class="tool-links"><button type="button" class="link" data-aks-parts="open">Expand all</button> · <button type="button" class="link" data-aks-parts="close">Collapse all</button></span></span></div>
    <div class="showing muted"></div><nav class="jump-bar" aria-label="Sections"></nav></div><div id="aks-go-out"></div>
    <nav class="subtabs aks-subtabs" aria-label="Workloads">${AKS_SUBS.map(([key, label]) => `<button type="button" data-aks-sub="${key}">${label}<span class="tab-badge" data-sub-n="${key}" hidden></span></button>`).join("")}</nav>
    <div class="aks-sub" data-sub="problems"><section class="layer" id="aks-problems"><h4>What's wrong now</h4><div class="aks-verdict"></div>${ciCutHtml}
      <div class="aks-problem-list"></div><p class="muted">One row per workload, from every source this cluster has; each pod and event behind a line is under <em>Pods &amp; events</em> (<em>details ›</em>).</p></section></div>`;

  const depList = Array.isArray(k.workloads) ? k.workloads : [];
  const depNamespaces = [...new Set(phases.map((r) => r.namespace).filter(Boolean))].sort();
  const needsApi = (what) => `${noticeHtml(k.reason || "The Kubernetes API couldn't be read.")}<p class="muted">${what} come${what.endsWith("s") ? "" : "s"} from the Kubernetes API only.</p>`;
  const findingsTable = (rows, key) => table(["Severity", "Namespace", "Workload", "Container", "Problem", "What to do"], rows
    .filter((f) => f.severity !== "INFO").map((f) => [
      pill(SEVERITY_STATUS[f.severity]) + ` <span class="muted">${esc(f.severity)}</span>`, esc(f.namespace), workloadButton(f.namespace, f.workload),
      esc(f.container), `${esc(f.problem)}<br><span class="muted">${esc(f.why)}</span>`, `${esc(f.action)}${ruleHtml(f.rule)}`]), key);
  const visible = (k.findings || []).filter((f) => f.severity !== "INFO");
  const nowRows = visible.filter((f) => f.kind === "now"), configRows = visible.filter((f) => f.kind === "config");
  const clusterRows = nowRows.filter((f) => !f.workload);
  const systemNote = (k.system_namespaces || []).length ? `<p class="muted">${includeSystem ? "System namespaces, included" : "Hidden from <em>All namespaces</em> as system (pick one in <em>Namespace</em>, or <em>All namespaces, including system</em>)"}:
    ${k.system_namespaces.map((s) => `<span title="${esc(s.why)}">${esc(s.namespace)}</span>`).join(", ")}. Found in the cluster: Kubernetes' own
    namespaces, and those where every workload carries a label AKS or its add-on manager sets.
    <a href="https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/" target="_blank" rel="noopener">docs</a></p>` : "";

  html += `<div class="aks-sub" data-sub="workloads"><section class="layer" data-jump="All workloads"><h4>All workloads ${kubeHowButton("k-workloads")}</h4>
    ${k.available ? `<p class="muted">Everything in the cluster, problems or not, like the portal's Workloads: pick a kind below, and the
      <em>Namespace</em> box at the top narrows it. A workload's name opens its details (describe, pods, events, YAML) and dependencies;
      a pod's opens the pod.</p>${systemNote}
      <div class="seg k-kinds" data-k-kinds role="group" aria-label="Kind to list">${K_KINDS.map(([v, label, view]) => {
        const n = view === "pods" ? (k.pod_rows || []).length : view === "jobs" ? (k.job_rows || []).length
          : (k.workload_rows || []).filter((w) => !v || (w.kind || w.workload.split("/")[0]) === v).length;
        return `<button type="button" data-k-kind="${v}" ${n || !v ? "" : 'disabled title="None in the namespaces shown"'}>${label} <span class="muted">${fmt(n)}</span></button>`; }).join("")}</div>
      <div data-k-view="workloads">${allWorkloadsTable(k)}</div><div data-k-view="jobs" class="hidden">${jobsTable(k)}</div>
      <div data-k-view="pods" class="hidden">${podsTable(k, data.leftover_hours || 24)}</div>` : needsApi("Workloads")}</section></div>`;

  html += `<div class="aks-sub" data-sub="capacity"><section class="layer" data-jump="Node pools"><h4>Node pools ${kubeHowButton("k-pools")}</h4>
    ${k.available ? `${k.usage_available ? "" : '<p class="muted">No live usage: metrics-server isn\'t answering, so only requests are shown.</p>'}
      <p class="muted">Requested and used are shares of what the nodes running <em>now</em> give pods. Where a pool can still grow,
        <em>at max</em> is the same requests if the autoscaler grows it fully; a full pool (past ${Math.round(100 * (k.pool_requests_warn || 0.8))}%,
        ${settingLink("aks.pool_requests_percent", "change")}) is amber only when it can't grow.</p>
      ${poolsTable(k.pools, k.usage_available, k.pool_requests_warn)}
      ${clusterRows.length ? `<h5>The cluster now</h5>${findingsTable(clusterRows, "k-cluster")}` : '<p class="ok">✓ No Pending pods, NotReady nodes or full pools that can\'t grow.</p>'}` : needsApi("Node pools")}</section>
    <section class="layer" data-jump="Top consumers"><h4>Top consumers <span class="muted">· now</span></h4>
      ${k.available ? `<div class="top-consumers">${topConsumersHtml(k.top, k.pools || [])}</div>` : needsApi("Live use per workload")}</section></div>`;

  html += `<div class="aks-sub" data-sub="memory"><section class="layer" data-jump="Memory & scaling"><h4>Memory &amp; scaling</h4>
    ${k.available ? '<div class="k-memory-out"><p class="muted">Read when you open it.</p></div>' : needsApi("Memory & scaling")}</section></div>`;
  html += `<div class="aks-sub" data-sub="setup"><section class="layer" data-jump="Setup risks"><h4>Setup risks ${kubeHowButton("k-config")}</h4>
    ${k.available ? `<p class="muted">How workloads are set up: single replicas, missing requests or limits, HPA and PodDisruptionBudget settings. Worth fixing before a
      peak, but common on healthy clusters, so yellow at most.</p>${configRows.length ? findingsTable(configRows, "k-config") : '<p class="ok">✓ None.</p>'}` : needsApi("Setup risks")}</section></div>`;

  // Pods & events: each pod and event behind What's wrong now, whichever source saw it, one row each.
  const ciRows = (name) => (ci.available && Array.isArray(ci[name]) ? ci[name] : []);
  const span = (name, fallback) => ciSpan((ci.hours || {})[name] || fallback);
  const apiPods = k.available ? (k.not_running || []) : [];
  const ciSeen = new Set(ciRows("not_running").map((r) => `${r.Namespace}/${r.Pod}`));
  const apiSeen = new Set(apiPods.map((r) => `${r.namespace}/${r.pod}`));
  const notRunning = [
    ...apiPods.map((r) => ({ ...r, sources: ["Kubernetes API", ...(ciSeen.has(`${r.namespace}/${r.pod}`) ? ["Container Insights"] : [])] })),
    ...ciRows("not_running").filter((r) => !apiSeen.has(`${r.Namespace}/${r.Pod}`)).map((r) => ({ namespace: r.Namespace, pod: r.Pod, phase: r.Status,
      reason: r.Reason, message: "", owner: r.OwnerKind && r.Owner ? `${r.OwnerKind}/${r.Owner}` : "", since: "", sources: ["Container Insights"] }))];
  const notRunningTable = (rows, key) => table(["Namespace", "Pod", "Phase", "Reason", "Owner", "In this state since", "Seen by", ""], rows.map((r) => [
    esc(r.namespace), podButton(r.namespace, r.pod), pill("warning") + " " + esc(r.phase),
    `${esc(r.reason || "–")}${r.message ? `<div class="muted ai-msg">${esc(r.message)}</div>` : ""}`, workloadButton(r.namespace, r.owner),
    r.since ? `<span title="${esc(r.started ? `started ${new Date(r.started).toLocaleString()}` : "")}">${seen(r.since)}</span>` : "–",
    sourceBadges(r.sources), podLogsButton(r.namespace, r.pod)]), key);
  const apiRestarts = visible.filter((f) => f.signal === "restarts");
  const nothing = !k.available && !ci.available;
  const restartsHtml = ci.available
    ? table(["Namespace", "Pod", "Container", "Restarts", ""], ciRows("restarts").map((r) => [
      esc(r.Namespace), podButton(r.Namespace, r.Pod), esc(r.Container), fmt(r.Restarts), podLogsButton(r.Namespace, r.Pod, r.Container)]), "ev-restarts")
    : apiRestarts.length ? `<p class="muted">From the Kubernetes API: restarts since each pod started (Container Insights would count them over the look-back).</p>
      ${table(["Namespace", "Workload", "Container", "Restarts"], apiRestarts.map((f) => [esc(f.namespace), workloadButton(f.namespace, f.workload), esc(f.container), esc(f.problem)]), "ev-restarts-api")}`
      : '<p class="muted">Needs Container Insights or the Kubernetes API.</p>';
  // Warning events: Container Insights over the look-back, else the cluster's own (Kubernetes keeps about an hour).
  const apiEvents = !ci.available && k.available ? (k.events || []) : [];
  const eventRows = ci.available ? ciRows("events") : apiEvents;
  const eventsHtml = ci.available ? warningEventsTable(ciRows("events"), "ev-events")
    : k.available ? `<p class="muted">From the Kubernetes API: what the cluster still keeps, usually its last hour (Container Insights isn't sending).</p>
      ${k.events_error ? noticeHtml(`The Kubernetes API's warning events couldn't be read: ${k.events_error}`) : ""}
      ${apiEvents.length ? warningEventsTable(apiEvents, "ev-events-api") : k.events_error ? "" : '<p class="muted">None.</p>'}
      ${(k.events_total || 0) > apiEvents.length ? `<p class="muted">The ${fmt(apiEvents.length)} seen most often of ${fmt(k.events_total)}.</p>` : ""}`
    : `<p class="muted">Needs Container Insights, the Kubernetes API (${esc(k.reason || "not read")}) or a monitoring tool; a workload's own events are in its Details.</p>`;
  html += `<div class="aks-sub" data-sub="evidence"><section class="layer"><h4>Pods &amp; events</h4>
    <p class="muted">Each pod and event behind <em>What's wrong now</em>, from every source that saw it. <em>details ›</em> on a line there opens this page filtered to it.</p>
    ${nothing ? noticeHtml(`Neither the Kubernetes API (${k.reason || "not read"}) nor Container Insights (${ci.reason || "not sending"}) can be read, so pods can only be counted.`) : ""}
    ${k.available ? unreadableHtml(k.unreadable || [], node.id) : ""}
    ${ciCutHtml}
    ${aksPart("ev-not-running", "Pods not running", notRunning.length, true, notRunning.length ? `<p class="muted">Failed, Pending or Unknown, with the reason
      Kubernetes gives. Failed pods stay until someone deletes them, so those over ${hoursText(data.leftover_hours || 24)} old are folded apart
      (${settingLink("aks.leftover_hours", "change")}): their workload has usually replaced them.</p>
      ${notRunningHtml(notRunning, problems.leftover, notRunningTable, data.leftover_hours || 24)}` : '<p class="ok">✓ Every pod is running or completed.</p>', "Pods not running")}
    ${aksPart("ev-oom", `OOM kills, ${span("oomkilled", 24)}`, ciRows("oomkilled").length, true, `${ci.available ? oomTable(ciRows("oomkilled"), podLogsButton, "ci-oom", podButton)
      : '<p class="muted">Container Insights isn\'t sending for this cluster.</p>'}${oomTool ? `<h5>Also from ${esc(state.selected[oomTool])}</h5><div id="oom-monitor">${loading(`Reading the cluster's OOM kills from ${state.selected[oomTool]}…`)}</div>` : ""}`, "OOM kills")}
    ${aksPart("ev-restarts", ci.available ? `Restarts, ${span("restarts", 1)}` : "Restarts", ci.available ? ciRows("restarts").length : apiRestarts.length, true, restartsHtml, "Restarts")}
    ${aksPart("ev-hot", "Containers near their limits, last 15 min", ciRows("hot_containers").length, true, ci.available
      ? table(["Namespace", "Pod", "Container", "CPU % of limit", "Memory % of limit", ""], ciRows("hot_containers").map((r) => [
        esc(r.Namespace), podButton(r.Namespace, r.Pod), esc(r.Container), fmt(r.CpuPctOfLimit), fmt(r.MemPctOfLimit), podLogsButton(r.Namespace, r.Pod, r.Container)]), "ev-hot")
      : '<p class="muted">Needs Container Insights; the Kubernetes API\'s live readings are in <em>What\'s wrong now</em>.</p>', "Near limits")}
    ${aksPart("ev-events", ci.available ? `Warning events, ${span("events", 1)}` : "Warning events", eventRows.length, true,
      `${eventsHtml}${oomTool ? `<h5>Also from ${esc(state.selected[oomTool])}</h5><div id="events-monitor">${loading(`Reading the cluster's warning events from ${state.selected[oomTool]}…`)}</div>` : ""}`, "Warning events")}
    </section></div>`;
  body.innerHTML = html;
  renderProblems(body, problems, data.timings);
  body.loadMemoryScaling = () => {  // read once per drawing, when Memory & scaling is first opened
    const out = body.querySelector(".k-memory-out");
    if (!out || out.dataset.read) return;
    out.dataset.read = "1";
    out.innerHTML = loading("Reading the week: restarts, OOM kills, memory and HPA time at max…");
    api("api/explore/memory-scaling", { params: { id: node.id, ...(oomTool ? toolParams(oomTool) : {}) } })
      .catch((error) => ({ found: false, workloads: [], hpas: [], queries: [], reason: error.message }))
      .then((week) => { if (!out.isConnected) return; out.innerHTML = memoryScalingHtml(k, week); if (body.reapplyFilter) body.reapplyFilter(); });
  };
  wireAksSubs(body, {
    problems: [problems.groups.filter((g) => g.namespace || g.workload).length, problems.groups.length > 0],
    workloads: [(k.workload_rows || []).length, false],
    capacity: [clusterRows.length, clusterRows.length > 0],
    setup: [configRows.length, false],
  });
  body.recountAksSubs = () => recountAksSubs(body, problems.groups.length > 0);
  wireAksParts(body);
  wireKindPick(body);
  renderTabTools(body, () => { state.aksCiHours = ""; state.aksSystem = false; state.aksNsPick = null; tabWorkloads(node, body, false); });
  const namespaces = [...depNamespaces, ...depList.map((w) => w.namespace)];
  // A tool's rows skip the server's system-namespace filter, so the same one applies here.
  const systemNs = new Set(k.available ? (k.system_namespaces || []).map((s) => s.namespace) : ["kube-system", "kube-public", "kube-node-lease"]);
  const shownNs = (r) => includeSystem || !systemNs.has(r.namespace ?? r.Namespace);
  const nsChoice = { system: [...systemNs], includeSystem, reload: (system) => { state.aksSystem = system; tabWorkloads(node, body, system); } };
  if (oomTool) loadEventsMonitor(node, oomTool, eventRows).then((extra) => {
    if (!extra || !body.isConnected) return;
    extra.rows = extra.rows.filter(shownNs);
    setPartCount(body, "ev-events", eventRows.length + extra.extra);
    if (!extra.rows.length || !$("aks-problems")) return;
    mergeMonitorEvents(problems, extra.rows, extra.label, extra.hours);
    renderProblems(body, problems, data.timings);
    body.reapplyFilter();
  });
  if (oomTool) loadOomMonitor(node, oomTool, Array.isArray(ci.oomkilled) ? ci.oomkilled : []).then((extra) => {
    if (!extra || !body.isConnected) return;
    extra.rows = extra.rows.filter(shownNs);
    if (extra.byWorkload) extra.byWorkload = extra.byWorkload.filter(shownNs);
    setPartCount(body, "ev-oom", ciRows("oomkilled").length + extra.extra);
    if (!extra.rows.length || !$("aks-problems")) return;
    mergeMonitorOom(problems, extra.rows, extra.label, extra.byWorkload);
    renderProblems(body, problems, data.timings);
    wireNamespacePick(body, node, namespaces, nsChoice);
    body.reapplyFilter();
    renderTabTools(body);
  });
  wireNamespacePick(body, node, namespaces, nsChoice);
  const goList = [...depList];  // plus the workloads other sources named, for clusters whose API can't be read
  for (const g of problems.groups) {
    const at = g.workload.indexOf("/"), kind = g.workload.slice(0, at), name = g.workload.slice(at + 1);
    if (at > 0 && DEP_KINDS.includes(kind) && !goList.some((w) => w.namespace === g.namespace && w.kind === kind && w.name === name)) {
      goList.push({ namespace: g.namespace, kind, name });
    }
  }
  wireGoTo(body, node, goList, oomTool);

  const back = $("ci-look-back");
  if (back) back.addEventListener("change", () => { state.aksCiHours = back.value; tabWorkloads(node, body, includeSystem); });
  body.onclick = (event) => {
    const button = event.target.closest("[data-logs]");
    if (button) podLogsRow(button, node.id, JSON.parse(button.dataset.logs));
    const workload = event.target.closest("[data-workload]");
    if (workload) workloadDetailRow(workload, node.id, JSON.parse(workload.dataset.workload));
    const pod = event.target.closest("[data-pod]");
    if (pod) podDetailRow(pod, node.id, JSON.parse(pod.dataset.pod));
    const goNodes = event.target.closest("[data-goto-nodes]");
    if (goNodes) {  // the Nodes tab, filtered to that pool (and to its nodes that need a look)
      state.nodesPreset = { cluster: node.id, ...JSON.parse(goNodes.dataset.gotoNodes) };
      showDetail(node.id, "nodes", { push: true });
      return;
    }
    const by = event.target.closest("[data-top-by]");
    if (by) body.querySelector(".top-consumers").innerHTML = topConsumersHtml(k.top, k.pools || [], by.dataset.topBy);
    const more = event.target.closest("[data-evidence]");
    if (more) {  // a line's details: Pods & events, filtered to that workload
      const filter = $("aks-filter");
      filter.value = more.dataset.evidence;
      filter.dispatchEvent(new Event("input"));
      body.showAksSub("evidence");
      body.querySelector(".tab-tools").scrollIntoView({ block: "start" });
    }
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

