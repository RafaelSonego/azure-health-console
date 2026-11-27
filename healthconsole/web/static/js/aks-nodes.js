"use strict";

// ------------------------------------------------------------------ nodes (AKS)
// Every node with its pool, readiness and pressure, what's placed on it against what it holds, and its versions;
// the pools in total above. From the Kubernetes API, or Container Insights' inventory when the API can't be reached.

const cores = (m) => `${(Number(m || 0) / 1000).toFixed(1)}`;
const gib = (mi) => `${(Number(mi || 0) / 1024).toFixed(1)}`;
// A pool's mode as a tag: System pools run the cluster's own pods (CoreDNS, metrics-server…), User pools the team's.
const poolModeHtml = (mode) => (mode === "System"
  ? pillHtml("info", "System pool", { rank: null, title: "Runs the cluster's own pods (CoreDNS, metrics-server, konnectivity…). AKS needs at least one." })
  : mode ? `<span class="muted" title="Runs the team's workloads">${esc(mode.toLowerCase())} pool</span>` : "");

function shareCell(x, unit, warn = 0.8) {  // warn: aks.pool_requests_percent, as Capacity
  if (!x || x.requested_pct == null) return '<span class="muted">–</span>';
  const tone = (pct) => (pct >= 100 ? "critical" : pct > 100 * warn ? "warning" : "");
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
  const preset = state.nodesPreset && state.nodesPreset.cluster === node.id ? state.nodesPreset : null;
  state.nodesPreset = null;  // once: a later visit opens unfiltered
  body.innerHTML = `${preset ? '<p><button type="button" class="link" id="nodes-back" title="Back to the Workloads tab\'s Capacity page">← back to Capacity</button></p>' : ""}
    ${fromInsights ? `<div class="notice partial">The Kubernetes API couldn't be reached (${esc(data.reason || "")}), so this is Container Insights'
      node inventory from the last 30 minutes: readiness, pool and versions, without capacity, conditions or taints.</div>` : ""}
    <p><strong>${rows.length} node(s)</strong>${pools.length ? ` in ${pools.length} pool(s)` : ""} ·
      ${notReady ? `${pill("critical")} ${notReady} not ready` : '<span class="ok">✓ all ready</span>'}
      ${cordoned ? ` · ${pill("warning")} ${cordoned} cordoned` : ""} ${problems ? ` · ${problems} with something to look at` : ""}
      ${data.via ? `<br><span class="muted">via ${esc(data.via)} · pool scaling settings are on the Scaling tab</span>` : ""}</p>
    ${unreadableHtml(data.unreadable, node.id)}
    ${pools.length ? `<h4>Pools ${kubeHowButton("nodes-pools")}</h4>${table(["Pool", "Nodes", "Not ready", "CPU (cores) allocatable / requested / used", "Memory (GiB) allocatable / requested / used"],
      pools.map((p) => [`${esc(p.pool)} ${poolModeHtml(p.mode)}`, fmt(p.nodes), p.not_ready ? `${pill("critical")} ${fmt(p.not_ready)}` : "0",
        `${cores(p.alloc_cpu)} / ${cores(p.req_cpu)} / ${p.used_cpu ? cores(p.used_cpu) : "–"}`,
        `${gib(p.alloc_mem)} / ${gib(p.req_mem)} / ${p.used_mem ? gib(p.used_mem) : "–"}`]))}` : ""}
    <h4>Nodes ${kubeHowButton("nodes-list")}</h4>
    <div class="table-tools"><input id="nodes-filter" type="search" placeholder="Filter by node, pool, VM size, zone or version">
      <label class="inline"><input type="checkbox" id="nodes-problems"> only nodes with something to look at</label>
      ${data.pool_requests_warn ? `<span class="muted rule">Requested turns amber past ${Math.round(100 * data.pool_requests_warn)}% · ${settingLink("aks.pool_requests_percent", "change")}</span>` : ""}</div>
    <div id="nodes-table"></div>`;
  const render = () => {
    const words = $("nodes-filter").value.toLowerCase().split(/\s+/).filter(Boolean);
    const only = $("nodes-problems").checked;
    const shown = rows.filter((r) => (!only || r.problems.length)
      && words.every((w) => `${r.name} ${r.pool} ${r.vm_size} ${r.zone} ${r.kubelet} ${r.node_image || ""}`.toLowerCase().includes(w)));
    $("nodes-table").innerHTML = shown.length ? `<table class="data-table nodes-table"><thead><tr><th>Node</th><th>State</th>
        ${fromInsights ? "" : "<th>CPU</th><th>Memory</th><th>Pods</th>"}<th>Versions</th><th>Age</th></tr></thead><tbody>${shown.map((r, i) => `
      <tr class="clickable" data-node-row="${i}"><td>${pill(r.status)} <strong>${esc(r.name)}</strong>
          <div class="muted">${esc([r.pool, r.vm_size, r.zone].filter(Boolean).join(" · "))} ${poolModeHtml(r.mode)}</div></td>
        <td>${r.ready ? "Ready" : '<span class="critical">NotReady</span>'}${r.problems.filter((x) => !x.startsWith("NotReady")).map((x) => `<div class="warning">${esc(x)}</div>`).join("")}
          ${r.taints && r.taints.length ? `<div class="muted">${r.taints.length} taint(s)</div>` : ""}</td>
        ${fromInsights ? "" : `<td>${shareCell(r.cpu, "cores", data.pool_requests_warn)}</td><td>${shareCell(r.memory, "GiB", data.pool_requests_warn)}</td>
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
  if (preset) {
    $("nodes-filter").value = preset.pool || "";
    $("nodes-problems").checked = !!preset.problems;
    $("nodes-back").addEventListener("click", () => { stored.set("ahc.aksSub", "capacity"); showDetail(node.id, "workloads", { push: true }); });
  }
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

// The kubectl behind each Workloads and Nodes section: one list, so the ⓘ next to a section and the kubectl tab agree.
const NOT_RUNNING = "kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded";
const KUBE_HOW = {
  platform: { title: "Pods by phase", from: "Azure platform metric kube_pod_status_phase, last 15 min. Closest kubectl:",
    cmd: "kubectl get pods -A --no-headers | awk '{print $4}' | sort | uniq -c" },
  "ci-not-running": { title: "Pods not running (Container Insights)", from: "Container Insights (KubePodInventory, last 15 min). Closest kubectl:", cmd: NOT_RUNNING },
  "ci-hot": { title: "Containers near their limits", from: "Container Insights (Perf, last 15 min). Now, and the limits to compare with:",
    cmd: String.raw`kubectl top pods -A --containers
kubectl get pods -A -o custom-columns='NS:.metadata.namespace,POD:.metadata.name,CPU_LIMIT:.spec.containers[*].resources.limits.cpu,MEM_LIMIT:.spec.containers[*].resources.limits.memory'` },
  "ci-restarts": { title: "Restarts", from: "Container Insights (last hour). kubectl's RESTARTS counts since the pod started:",
    cmd: "kubectl get pods -A --sort-by='.status.containerStatuses[0].restartCount'" },
  "ci-oom": { title: "OOMKilled", from: "Container Insights: each kill's pod and node (KubePodInventory), the kernel's message (KubeEvents OOMKilling on the node: \"Memory cgroup\" is the container's limit, \"Out of memory\" the node) and memory just before (Perf). With kubectl:",
    cmd: String.raw`kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{"\t"}{.spec.nodeName}{"\t"}{.status.containerStatuses[*].lastState.terminated.reason}{"\t"}{.status.containerStatuses[*].lastState.terminated.finishedAt}{"\n"}{end}' | grep OOMKilled
kubectl get events -A --field-selector reason=OOMKilling
kubectl logs -n <namespace> <pod> -c <container> --previous` },
  "ci-events": { title: "Warning events", from: "Container Insights (KubeEvents, last hour). Kubernetes keeps events about an hour:",
    cmd: "kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp" },
  "k-not-running": { title: "Pods not running", from: "Kubernetes API, the pod list. The same with kubectl:", cmd: `${NOT_RUNNING} -o wide` },
  "k-now": { title: "Happening now", from: "Kubernetes API: pods, HPAs and nodes, with live usage from metrics-server:",
    cmd: "kubectl get pods -A | grep -vE 'Running|Completed'\nkubectl get hpa -A\nkubectl top pods -A --containers\nkubectl get nodes" },
  "k-workloads": { title: "All workloads", from: "Kubernetes API: Deployments, StatefulSets, DaemonSets, CronJobs, Jobs, pods, HPAs and ReplicaSets:",
    cmd: "kubectl get deploy,sts,ds,cronjob,job -A\nkubectl get pods -A -o wide\nkubectl get hpa -A\nkubectl describe deploy -n <namespace> <name>" },
  "k-pools": { title: "Node pools", from: "Allocatable and requested: each node's Allocated resources. Used: metrics-server.",
    cmd: "kubectl get nodes -L kubernetes.azure.com/agentpool\nkubectl describe nodes | grep -A 8 'Allocated resources'\nkubectl top nodes" },
  "k-config": { title: "Configuration risks", from: "Kubernetes API: replicas, requests and limits, PodDisruptionBudgets:",
    cmd: String.raw`kubectl get deploy,sts -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,REPLICAS:.spec.replicas'
kubectl get pods -A -o custom-columns='NS:.metadata.namespace,POD:.metadata.name,CPU_REQ:.spec.containers[*].resources.requests.cpu,MEM_REQ:.spec.containers[*].resources.requests.memory,MEM_LIMIT:.spec.containers[*].resources.limits.memory'
kubectl get pdb -A` },
  "nodes-pools": { title: "Pools (Nodes tab)", from: "Requested is a node's Allocated resources → Requests (what pods reserve); used is kubectl top:",
    cmd: "kubectl describe nodes | grep -A 8 'Allocated resources'\nkubectl top nodes" },
  "nodes-list": { title: "Nodes", from: "Kubernetes API, the node list:",
    cmd: "kubectl get nodes -o wide -L kubernetes.azure.com/agentpool,topology.kubernetes.io/zone,node.kubernetes.io/instance-type\nkubectl describe node <node>" },
};
// Pods & events' parts explain themselves with the source lists they merge (kubectl first, the closest one).
const EV_HOW = { "ev-not-running": "k-not-running", "ev-oom": "ci-oom", "ev-restarts": "ci-restarts", "ev-hot": "ci-hot", "ev-events": "ci-events" };
for (const [key, from] of Object.entries(EV_HOW)) Object.defineProperty(KUBE_HOW, key, { value: KUBE_HOW[from], enumerable: false });

const kubeHowButton = (key) => `<button type="button" class="kube-info" data-kube-how="${esc(key)}" title="How to see this with kubectl" aria-label="How to see this with kubectl">ⓘ</button>`;
const kubeCmdHtml = (cmd) => `<div class="kube-cmd"><pre class="logs">${esc(cmd)}</pre><button type="button" class="link" data-copy="${esc(cmd)}">copy</button></div>`;

// ⓘ opens the command under its heading (and never toggles the fold it sits in).
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-kube-how]");
  if (!button) return;
  event.preventDefault();
  const anchor = button.closest("summary, h4, h5") || button;
  const next = anchor.nextElementSibling;
  if (next && next.classList.contains("kube-how")) { next.remove(); return; }
  const how = KUBE_HOW[button.dataset.kubeHow];
  anchor.insertAdjacentHTML("afterend", `<div class="kube-how"><p class="muted">${esc(how.from)}</p>${kubeCmdHtml(how.cmd)}
    <p class="muted">The console leaves system namespaces out of <em>All namespaces</em> unless you pick one, or <em>All namespaces, including system</em>; <code>-A</code> shows every namespace, <code>-n &lt;namespace&gt;</code> one. The kubectl tab has the rest.</p></div>`);
  const part = button.closest("details");
  if (part) part.open = true;
});

async function tabKubectl(node) {
  const sub = node.id.split("/")[2], rg = node.resource_group, name = node.name;
  const az = `--resource-group ${rg} --name ${name}`, pools = `--resource-group ${rg} --cluster-name ${name}`;
  const steps = (key, rows) => table(["What", "Command", "When"], rows.map(([what, cmd, when]) => [esc(what), kubeCmdHtml(cmd), esc(when)]), `kubectl-${key}`);
  const part = (title, note, html, open = false) => {
    const key = `kubectl-${title.split(".")[0]}`, kept = stored.json("ahc.aksParts", {})[key];
    return `<details class="aks-part kubectl-part" data-part="${key}" data-default="${open ? 1 : 0}" ${(kept ?? open) ? "open" : ""}><summary><strong>${esc(title)}</strong>
    ${note ? `<span class="muted">${esc(note)}</span>` : ""}</summary>${html}</details>`;
  };
  return `<p class="muted">Commands to run in your own terminal: the console only reads, never changes the cluster. Replace
      <code>&lt;namespace&gt;</code>, <code>&lt;pod&gt;</code>, <code>&lt;deployment&gt;</code>, <code>&lt;node&gt;</code> and the like;
      this cluster's name, group and subscription are filled in. Handy everywhere: <code>-o wide</code> (more columns),
      <code>-o yaml</code> (everything), <code>-l app=web</code> (by label), <code>-w</code> (watch for changes).</p>
    <div class="list-tools aks-tools"><button type="button" class="link" data-aks-parts="open">Expand all</button> · <button type="button" class="link" data-aks-parts="close">Collapse all</button></div>
    ${part("1. Connect", "once per cluster", steps("connect", [
      ["Get the credentials", `az aks get-credentials ${az} --subscription ${sub}`, "Adds the cluster to ~/.kube/config and makes it the current context."],
      ["Sign in with your Entra ID", "kubelogin convert-kubeconfig -l azurecli", "Clusters with Entra ID: reuse your az login (and the PIM roles you activated)."],
      ["Check where you are", "kubectl config current-context\nkubectl config get-contexts", "Before anything else: is this the cluster you mean?"],
      ["Switch cluster", "kubectl config use-context <context>", "Several clusters in your kubeconfig: pick one."],
      ["What you're allowed to do", "kubectl auth can-i --list\nkubectl auth can-i delete pods -n <namespace>", "Your role in this cluster: read only, or more."],
      ["A private API server", `az aks command invoke ${az} --command "kubectl get pods -A"`, "No network path to a private cluster: Azure runs the command for you (in a short-lived pod in the cluster)."]]), true)}
    ${part("2. What the console shows", "the same as the ⓘ on Workloads and Nodes",
      table(["Section", "Command", "Where the console reads it"], Object.values(KUBE_HOW).map((h) => [esc(h.title), kubeCmdHtml(h.cmd), esc(h.from)]), "kubectl-how"), true)}
    ${part("3. Find your way", "", steps("find", [
      ["Namespaces", "kubectl get namespaces", "Where workloads live; most commands take -n <namespace>, or -A for all."],
      ["Everything in a namespace", "kubectl get all -n <namespace>", "Deployments, ReplicaSets, pods, services at once."],
      ["Which images run where", String.raw`kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.containers[*].image}{"\n"}{end}'`, "The image:tag behind each pod, e.g. to match a vulnerability."],
      ["Pods on one node", "kubectl get pods -A -o wide --field-selector spec.nodeName=<node>", "What a node problem affects."],
      ["Labels", "kubectl get pods -n <namespace> --show-labels", "The labels services, HPAs and selectors use."],
      ["Watch a rollout or a fix", "kubectl get pods -n <namespace> -w", "Live: pods appearing, getting ready, restarting."],
      ["What a field means", "kubectl explain deployment.spec.strategy\nkubectl api-resources", "The built-in docs for any field, and every resource type the cluster has."]]))}
    ${part("4. Look closer at one workload", "", steps("closer", [
      ["Why a pod is failing", "kubectl describe pod -n <namespace> <pod>", "Events at the bottom: scheduling, image pulls, probes, OOMKilled."],
      ["Its logs", "kubectl logs -n <namespace> <pod> --all-containers --tail=200", "What the app wrote. Add -f to follow, --since=1h for a window."],
      ["Logs before the last restart", "kubectl logs -n <namespace> <pod> --previous", "A crash loop: the run that crashed, not the new one."],
      ["Logs of every pod of a workload", "kubectl logs -n <namespace> -l app=<label> --all-containers --tail=50 --prefix", "Every replica at once, each line prefixed with its pod (the label from --show-labels)."],
      ["Events for one pod", "kubectl get events -n <namespace> --field-selector involvedObject.name=<pod>", "Only this pod's events."],
      ["A deployment's state", "kubectl describe deploy -n <namespace> <deployment>\nkubectl rollout status deploy -n <namespace> <deployment>", "Ready replicas, the rollout in progress, its conditions."],
      ["Its rollout history", "kubectl rollout history deploy -n <namespace> <deployment>", "Which revision runs, before an undo."],
      ["Usage against requests", "kubectl top pod -n <namespace> <pod> --containers\nkubectl get pod -n <namespace> <pod> -o jsonpath='{.spec.containers[*].resources}'", "Near its limit, or reserving far more than it uses?"],
      ["Why it won't scale", "kubectl describe hpa -n <namespace> <name>", "The HPA's events: missing metrics, at its max, no requests set."],
      ["One node", "kubectl describe node <node>", "Conditions (pressure, NotReady), taints, Allocated resources, the pods on it."],
      ["Cluster autoscaler", "kubectl get configmap -n kube-system cluster-autoscaler-status -o yaml", "Why nodes were or weren't added or removed."]]))}
    ${part("5. Network", "", steps("network", [
      ["Services and their pods", "kubectl get svc -n <namespace>\nkubectl get endpointslices -n <namespace>", "A service with no endpoints has no ready pod behind it: traffic goes nowhere."],
      ["Ingress", "kubectl get ingress -A\nkubectl describe ingress -n <namespace> <name>", "Hosts, paths and the service each sends to."],
      ["Network policies", "kubectl get networkpolicy -A", "Rules that may block traffic between pods."],
      ["Reach a service from your machine", "kubectl port-forward -n <namespace> svc/<service> 8080:80", "Then open http://localhost:8080: tests the app without the ingress or load balancer."],
      ["DNS and reachability from inside", "kubectl run tmp-net -n <namespace> --rm -it --restart=Never --image=busybox:1.36 -- nslookup <service>.<namespace>.svc.cluster.local", "Starts a temporary pod (removed on exit): does the name resolve inside the cluster?"]]))}
    ${part("6. Config, secrets and storage", "", steps("config", [
      ["ConfigMaps", "kubectl get configmap -n <namespace>\nkubectl describe configmap -n <namespace> <name>", "Settings mounted into pods or set as environment."],
      ["Secrets (names only)", "kubectl get secrets -n <namespace>", "Which exist. -o yaml shows their values (base64 is not encryption): treat that output as secret."],
      ["Environment of a deployment", "kubectl set env deploy/<deployment> -n <namespace> --list", "Reads the variables; with --list it changes nothing."],
      ["Volumes", "kubectl get pvc -A\nkubectl get pv\nkubectl get storageclass", "A Pending claim keeps its pod Pending: describe it for the reason."]]))}
    ${part("7. Jobs and CronJobs", "", steps("jobs", [
      ["List them", "kubectl get cronjobs,jobs -A", "Schedules, the last run, and whether runs succeeded."],
      ["A job's logs", "kubectl logs -n <namespace> job/<job>", "What a run printed."],
      ["Why a job failed", "kubectl describe job -n <namespace> <job>", "Backoff limit, deadlines, the failed pods."]]))}
    ${part("8. Debug inside a container", "these can change what runs: use with care", steps("debug", [
      ["A shell in the container", "kubectl exec -it -n <namespace> <pod> -c <container> -- sh", "Look at files, env and the network from where the app runs."],
      ["A debug container next to it", "kubectl debug -it -n <namespace> <pod> --image=busybox:1.36 --target=<container>", "Images without a shell: adds a temporary container sharing its processes."],
      ["On a node", "kubectl debug node/<node> -it --image=mcr.microsoft.com/cbl-mariner/busybox:2.0", "A shell on the node (host files under /host). Delete the debug pod afterwards."]]))}
    ${part("9. The cluster in Azure (az)", "", steps("azure", [
      ["The cluster", `az aks show ${az} --subscription ${sub} -o table`, "Version, provisioning and power state, as Azure sees it."],
      ["Node pools", `az aks nodepool list ${pools} --subscription ${sub} -o table`, "Size, count, autoscaler min/max, version per pool."],
      ["Upgrades available", `az aks get-upgrades ${az} --subscription ${sub} -o table`, "Which Kubernetes versions you can move to."],
      ["Can it pull from a registry", `az aks check-acr ${az} --subscription ${sub} --acr <registry>.azurecr.io`, "ImagePullBackOff from ACR: checks the cluster's identity can pull."]]))}
    ${part("10. Make changes", "you run these; check the context first", `<div class="notice partial">These change the cluster. Run them only
        where you're allowed to, and prefer the team's deployment pipeline for anything that should last: a change made here
        is undone by the next deploy.</div>${steps("changes", [
      ["Restart a deployment", "kubectl rollout restart deploy -n <namespace> <deployment>", "Pods stuck or holding bad state: replaces them one by one, keeping the service up."],
      ["Roll back", "kubectl rollout undo deploy -n <namespace> <deployment>", "A bad release: back to the previous revision (--to-revision=N for an older one)."],
      ["Pause and resume a rollout", "kubectl rollout pause deploy -n <namespace> <deployment>\nkubectl rollout resume deploy -n <namespace> <deployment>", "Stop a release half way while you look."],
      ["Change the image", "kubectl set image deploy/<deployment> -n <namespace> <container>=<image>:<tag>", "A hotfix image now; the pipeline should follow."],
      ["Scale", "kubectl scale deploy -n <namespace> <deployment> --replicas=3", "More or fewer pods now. An HPA overrides this: change its min/max instead."],
      ["Change an HPA's range", String.raw`kubectl patch hpa -n <namespace> <name> -p '{"spec":{"minReplicas":3,"maxReplicas":10}}'`, "At its max during a peak: raise maxReplicas (if the nodes have room)."],
      ["Change requests or limits", "kubectl set resources deploy -n <namespace> <deployment> --requests=cpu=250m,memory=256Mi --limits=memory=512Mi", "To try a value now; then put it in the manifest."],
      ["Run a CronJob now", "kubectl create job -n <namespace> --from=cronjob/<cronjob> <cronjob>-manual", "Start one run outside its schedule."],
      ["Clean up old Failed pods", "kubectl delete pods -A --field-selector=status.phase=Failed", "The pods Workloads marks as leftover; their workloads already replaced them."],
      ["A pod stuck Terminating", "kubectl delete pod -n <namespace> <pod> --grace-period=0 --force", "Last resort: Kubernetes stops waiting for it; the node may still run it for a while."],
      ["Take a node out", "kubectl cordon <node>\nkubectl drain <node> --ignore-daemonsets --delete-emptydir-data", "A bad node: no new pods, then move the running ones (respects PodDisruptionBudgets)."],
      ["Put it back", "kubectl uncordon <node>", "After the fix: the node takes pods again."],
      ["Node pool size", `az aks nodepool update ${pools} --subscription ${sub} --name <pool> --update-cluster-autoscaler --min-count 2 --max-count 10`, "A pool at its max: let the autoscaler add nodes (check VM quota and subnet IPs)."],
      ["Upgrade", `az aks upgrade ${az} --subscription ${sub} --kubernetes-version <version>`, "Planned work: control plane, then pools; nodes are replaced one by one."]])}`)}`;
}

// Workloads' sections fold: with problems open (unless `openByDefault` is false), the rest closed, yours kept in this
// browser. An empty one is a closed grey line; each one is a stop on the tab's jump bar (`data-jump`).
function aksPart(key, title, count, problem, inner, jump = title.split(",")[0], openByDefault = problem) {
  const kept = stored.json("ahc.aksParts", {})[key];
  const open = count > 0 && (kept === undefined ? openByDefault : kept);
  return `<details class="aks-part${count ? "" : " empty"}" data-part="${esc(key)}" data-default="${openByDefault && count > 0 ? 1 : 0}" ${open ? "open" : ""}
    data-jump="${esc(jump)}" data-count="${count}" data-problem="${problem && count ? 1 : 0}">
    <summary><strong>${esc(title)}</strong> <span class="muted">${count ? fmt(count) : "none"}</span>${problem && count ? " " + pill("warning") : ""}
      ${KUBE_HOW[key] ? kubeHowButton(key) : ""}</summary>${inner}</details>`;
}

// One filter over every table in the tab, by text and by namespace: sections with a match open, the rest hide. A
// section with no Namespace column stays for a namespace (node pools). Tables in a workload's own panel are left alone.
const tabLists = (root) => [...root.querySelectorAll("table[data-list]")].filter((t) => !t.closest(".logs-panel, #dep-out, #path-out"));

function filterAksParts(body, text, ns = "") {
  for (const t of tabLists(body)) {
    t.dataset.filterText = text;
    t.dataset.filterNs = ns;
    t.dataset.shown = LIST_PAGE;
    pageList(t);
  }
  for (const part of body.querySelectorAll("details.aks-part")) {
    const matches = tabLists(part).reduce((n, t) => n + listGroups(t).filter((rows) => rowMatches(t, rows[0])).length, 0);
    const filtered = !!(text || ns);
    if (part.dataset.total === undefined) part.dataset.total = part.dataset.count;
    part.dataset.count = filtered && tabLists(part).length ? matches : part.dataset.total;
    part.classList.toggle("hidden", filtered && !matches);
    const open = filtered ? matches > 0 : stored.json("ahc.aksParts", {})[part.dataset.part] ?? part.dataset.default === "1";
    if (part.open !== open) { part.dataset.auto = "1"; part.open = open; }
  }
}

// Rows per namespace in the sections with problems, for the namespace picker's counts.
function namespaceProblems(body) {
  const counts = {};
  const merged = tabLists(body).filter((t) => t.dataset.list === "aks-problems");
  for (const t of merged.length ? merged : tabLists(body).filter((t) => t.closest('[data-problem="1"]') || t.dataset.list === "pod-phases")) {
    const col = namespaceColumn(t);
    if (col >= 0) for (const rows of listGroups(t)) {
      const ns = rows[0].cells[col] && rows[0].cells[col].textContent.trim();
      if (ns) counts[ns] = (counts[ns] || 0) + 1;
    }
  }
  return counts;
}

function wireAksParts(body) {
  if (!body.aksFoldsWired) {  // once per tab body (it outlives each drawing): any fold in it, drawn now or later
    body.aksFoldsWired = true;
    body.addEventListener("toggle", (event) => {
      const part = event.target;
      if (!part.matches || !part.matches("details.aks-part")) return;
      if (part.dataset.auto) { delete part.dataset.auto; return; }  // the filter moved it, not you
      if (!part.dataset.part) return;
      const parts = stored.json("ahc.aksParts", {});
      parts[part.dataset.part] = part.open;
      stored.set("ahc.aksParts", parts);
      renderTabTools(body);
    }, true);
  }
  const filter = $("aks-filter"), nsPick = $("aks-ns");
  const apply = () => {
    filterAksParts(body, filter ? filter.value.trim() : "", nsPick ? nsPick.value.replace(ALL_WITH_SYSTEM, "") : "");
    if (body.recountAksSubs) body.recountAksSubs();
    renderTabTools(body);
  };
  if (filter) filter.addEventListener("input", apply);
  if (nsPick) nsPick.addEventListener("change", apply);
  body.reapplyFilter = apply;
  for (const button of body.querySelectorAll("[data-aks-parts]")) button.addEventListener("click", () => {
    const open = button.dataset.aksParts === "open";
    body.querySelectorAll("details.aks-part:not(.hidden)").forEach((part) => { if (part.open !== open) { part.dataset.auto = "1"; part.open = open; } });
    forgetAksParts(body);  // for this visit only: the next one opens at the defaults again
  });
}

// Drop the open/close kept for this tab's sections, so they go back to their defaults (ones with problems open).
function forgetAksParts(body) {
  const parts = stored.json("ahc.aksParts", {});
  body.querySelectorAll("details.aks-part[data-part]").forEach((part) => delete parts[part.dataset.part]);
  stored.set("ahc.aksParts", Object.keys(parts).length ? parts : null);
}

const keptAksParts = (body) => {
  const parts = stored.json("ahc.aksParts", {});
  return [...body.querySelectorAll("details.aks-part[data-part]")].some((part) => parts[part.dataset.part] !== undefined);
};

// A tab's toolbar (`.tab-tools`, kept at the top while you scroll): "Showing:" what's in effect with Reset, and a jump
// bar to every `[data-jump]` section. `reset` puts the tab's own settings back; the filter and kept folds are cleared here.
function renderTabTools(body, reset) {
  const tools = body.querySelector(".tab-tools");
  if (!tools) return;
  if (reset) tools.reset = reset;
  const filter = tools.querySelector('input[type="search"]:not([role="combobox"])'), nsPick = tools.querySelector("[data-ns-filter]");
  const text = filter ? filter.value.trim() : "", ns = nsPick ? nsPick.value.replace(ALL_WITH_SYSTEM, "") : "";
  const folded = keptAksParts(body);
  const parts = [...JSON.parse(tools.dataset.showing || "[]"), ...(ns ? [`namespace ${esc(ns)}`] : []), ...(text ? [`filter “${esc(text)}”`] : []),
    ...(folded ? ["sections you opened or closed"] : [])];
  const changed = tools.dataset.changed === "1" || !!text || !!ns || folded;
  if (tools.querySelector(".showing")) tools.querySelector(".showing").innerHTML = parts.length
    ? `Showing: ${parts.join(" · ")}${changed ? ' · <button type="button" class="link" data-tools-reset>Reset</button>' : ""}` : "";
  const all = [...body.querySelectorAll("[data-jump]")];
  all.forEach((el) => delete el.dataset.jumpAt);
  tools.querySelector(".jump-bar").innerHTML = all.filter((el) => !el.closest(".hidden")).map((el, i) => {
    el.dataset.jumpAt = i;
    const n = el.dataset.count;
    return `<button type="button" class="jump${el.dataset.problem === "1" ? " problem" : ""}${n === "0" ? " none" : ""}" data-jump-to="${i}">${esc(el.dataset.jump)}${
      n !== undefined ? ` <span class="jump-n">${fmt(Number(n))}</span>` : ""}</button>`;
  }).join("");
  if (tools.wired) return;
  tools.wired = true;
  tools.addEventListener("click", (event) => {
    const stop = event.target.closest("[data-jump-to]");
    if (stop) {
      const el = body.querySelector(`[data-jump-at="${stop.dataset.jumpTo}"]`);
      if (!el) return;
      if (el.tagName === "DETAILS" && !el.open) { el.dataset.auto = "1"; el.open = true; }
      el.style.scrollMarginTop = `${tools.offsetHeight + 8}px`;
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (!event.target.closest("[data-tools-reset]")) return;
    if (filter) filter.value = "";
    forgetAksParts(body);
    const redraws = tools.dataset.changed === "1" && tools.reset;
    if (nsPick && nsPick.value) { nsPick.value = ""; state.aksNsPick = null; if (!redraws) nsPick.dispatchEvent(new Event("change")); }
    if (redraws) tools.reset();
    else if (filter) filter.dispatchEvent(new Event("input"));
    else renderTabTools(body);
  });
}

