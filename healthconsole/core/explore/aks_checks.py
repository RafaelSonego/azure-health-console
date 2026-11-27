"""Workload checks ported from aks-check (aks_rightsizing.py), reworked for one current reading:
no sampling and no peak forecast, just what is wrong right now and what to do about it."""
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import runtime_caps

KUBERNETES_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")  # created by Kubernetes itself (kube- is reserved)
# Labels with which AKS and its add-on manager mark what they deploy: a namespace holding only such workloads is theirs.
MANAGED_LABELS = (("addonmanager.kubernetes.io/mode", None), ("kubernetes.azure.com/managedby", "aks"))
SCALABLE = ("Deployment", "StatefulSet")
SEVERITIES = ("HIGH", "MEDIUM", "LOW", "INFO")

# Official pages behind each check (each opened and read before it was listed here).
K8S = "https://kubernetes.io/docs/"
DOCS = {
    "hpa": K8S + "tasks/run-application/horizontal-pod-autoscale/",
    "resources": K8S + "concepts/configuration/manage-resources-containers/",
    "memory": K8S + "tasks/configure-pod-container/assign-memory-resource/",
    "cpu": K8S + "tasks/configure-pod-container/assign-cpu-resource/",
    "disruptions": K8S + "concepts/workloads/pods/disruptions/",
    "lifecycle": K8S + "concepts/workloads/pods/pod-lifecycle/",
    "eviction": K8S + "concepts/scheduling-eviction/node-pressure-eviction/",
    "deployment": K8S + "concepts/workloads/controllers/deployment/",
    "namespaces": K8S + "concepts/overview/working-with-objects/namespaces/",
    "aks-resources": "https://learn.microsoft.com/azure/aks/developer-best-practices-resource-management",
    "aks-pdb": "https://learn.microsoft.com/azure/aks/operator-best-practices-scheduler",
    "aks-autoscaler": "https://learn.microsoft.com/azure/aks/cluster-autoscaler",
    "aks-node-image": "https://learn.microsoft.com/azure/aks/node-image-upgrade",
}
KUBERNETES, PRACTICE, THRESHOLD = "kubernetes", "practice", "threshold"


def rule(basis, doc="", setting="", value=""):
    """What a finding rests on: Kubernetes' own behaviour, a recommended practice, or the console's threshold (a
    setting, with the value it used); and the page that explains it."""
    return {"basis": basis, "doc": DOCS.get(doc, ""), "setting": f"aks.{setting}" if setting else "", "value": value}


@dataclass
class Thresholds:
    """The console's rules of thumb, from Settings → AKS (percents there, fractions here)."""
    cpu_idle: float = 0.30
    cpu_above_request: float = 0.90
    cpu_near_limit: float = 0.80
    memory_near_limit: float = 0.85
    hpa_target_high: int = 85
    hpa_near_max: float = 0.80
    hpa_overshoot: float = 1.5
    pool_requests: float = 0.80
    restarts_warning: int = 5

    @classmethod
    def from_settings(cls, aks):
        aks, pct = aks or {}, lambda key, default: (aks.get(key, default * 100)) / 100  # noqa: E731
        return cls(cpu_idle=pct("cpu_idle_percent", 0.30), cpu_above_request=pct("cpu_above_request_percent", 0.90),
                   cpu_near_limit=pct("cpu_near_limit_percent", 0.80), memory_near_limit=pct("memory_near_limit_percent", 0.85),
                   hpa_target_high=aks.get("hpa_target_high_percent", 85), hpa_near_max=pct("hpa_near_max_percent", 0.80),
                   hpa_overshoot=pct("hpa_overshoot_percent", 1.5), pool_requests=pct("pool_requests_percent", 0.80),
                   restarts_warning=aks.get("restarts_warning", 5))


def system_namespaces(objs):
    """{namespace: why} for the namespaces that aren't the team's: Kubernetes' own, and those whose every workload
    carries a label AKS or its add-on manager puts on what it deploys (read from the workloads, never a list)."""
    found = {ns: "created by Kubernetes" for ns in KUBERNETES_NAMESPACES}
    by_ns = defaultdict(list)
    for items in objs.values():
        for o in items:
            by_ns[o["metadata"].get("namespace", "")].append((o["metadata"].get("labels") or {}))
    for ns, labels in by_ns.items():
        if ns in found or not labels:
            continue
        marks = [next((f"{k}={lab[k]}" for k, want in MANAGED_LABELS if k in lab and (want is None or lab[k] == want)), None)
                 for lab in labels]
        if all(marks):
            found[ns] = f"every workload is marked {sorted(set(marks))[0]}"
    return found


# ------------------------------------------------------------------ quantities


def parse_cpu(q):
    """Kubernetes CPU quantity -> millicores."""
    if q in (None, ""):
        return None
    q = str(q)
    if q.endswith("n"):
        return float(q[:-1]) / 1e6
    if q.endswith("u"):
        return float(q[:-1]) / 1e3
    if q.endswith("m"):
        return float(q[:-1])
    return float(q) * 1000


_BIN = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DEC = {"k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "m": 1e-3}


def parse_mem(q):
    """Kubernetes memory quantity -> MiB."""
    if q in (None, ""):
        return None
    q = str(q)
    for suffix, multiplier in _BIN.items():
        if q.endswith(suffix):
            return float(q[:-2]) * multiplier / 2**20
    if q[-1] in _DEC:
        return float(q[:-1]) * _DEC[q[-1]] / 2**20
    return float(q) / 2**20


def fcpu(v):
    return "-" if v is None else f"{v:.0f}m"


def fmem(v):
    return "-" if v is None else f"{v:.0f}Mi"


NOW, CONFIG = "now", "config"


def finding(sev, cat, problem, why="", action="", ns="", workload="", container="", kind=NOW, signal="", basis=None):
    """`kind` is NOW (happening at this moment) or CONFIG (a setup risk); a CONFIG finding is yellow at
    most, since healthy clusters often have them."""
    if kind == CONFIG and sev == "HIGH":
        sev = "MEDIUM"
    return {"severity": sev, "category": cat, "problem": problem, "why": why, "action": action,
            "namespace": ns, "workload": workload, "container": container, "kind": kind,
            **({"signal": signal} if signal else {}),  # `signal`: what Container Insights also counts (restarts, oom)
            **({"rule": basis} if basis else {})}


# ------------------------------------------------------------------ workloads


def containers_of(pod_spec):
    """App containers plus native sidecars (init containers with restartPolicy Always)."""
    sidecars = [c for c in pod_spec.get("initContainers", []) or [] if c.get("restartPolicy") == "Always"]
    return (pod_spec.get("containers") or []) + sidecars


def pod_requests(pod_spec):
    cpu = mem = 0.0
    for c in containers_of(pod_spec):
        r = (c.get("resources") or {}).get("requests") or {}
        cpu += parse_cpu(r.get("cpu")) or 0
        mem += parse_mem(r.get("memory")) or 0
    return cpu, mem


def _template(kind, obj):
    if kind == "CronJob":
        return obj["spec"]["jobTemplate"]["spec"]["template"]
    return obj["spec"]["template"]


def build_workloads(objs):
    out = []
    for kind, items in objs.items():
        for o in items:
            md, st = o["metadata"], o.get("status", {})
            if kind == "DaemonSet":
                desired, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
            elif kind == "CronJob":
                desired, ready = None, len(st.get("active", []) or [])
            else:
                desired, ready = o["spec"].get("replicas", 1), st.get("readyReplicas", 0)
            tpl = _template(kind, o)
            out.append(dict(kind=kind, ns=md["namespace"], name=md["name"], desired=desired, ready=ready,
                            spec=tpl["spec"], labels=(tpl.get("metadata") or {}).get("labels") or {}))
    return sorted(out, key=lambda w: (w["ns"], w["kind"], w["name"]))


def map_pods(pods, replicasets, jobs):
    """pod -> owning workload; restarts and OOMKilled pods per container."""
    rs_to_dep, job_to_cj = {}, {}
    for rs in replicasets:
        for o in rs["metadata"].get("ownerReferences", []):
            if o["kind"] == "Deployment":
                rs_to_dep[(rs["metadata"]["namespace"], rs["metadata"]["name"])] = o["name"]
    for j in jobs:
        for o in j["metadata"].get("ownerReferences", []):
            if o["kind"] == "CronJob":
                job_to_cj[(j["metadata"]["namespace"], j["metadata"]["name"])] = o["name"]

    pod_owner, restarts, oom_pods = {}, defaultdict(int), defaultdict(int)
    for p in pods:
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        owner = None
        for o in p["metadata"].get("ownerReferences", []):
            if o["kind"] == "ReplicaSet" and (ns, o["name"]) in rs_to_dep:
                owner = ("Deployment", rs_to_dep[(ns, o["name"])])
            elif o["kind"] in ("StatefulSet", "DaemonSet"):
                owner = (o["kind"], o["name"])
            elif o["kind"] == "Job" and (ns, o["name"]) in job_to_cj:
                owner = ("CronJob", job_to_cj[(ns, o["name"])])
        if not owner:
            continue
        key = (ns, *owner)
        pod_owner[(ns, name)] = key
        st = p.get("status", {})
        for cs in (st.get("containerStatuses") or []) + (st.get("initContainerStatuses") or []):
            k = key + (cs["name"],)
            restarts[k] += cs.get("restartCount", 0)
            reasons = {(cs.get(s) or {}).get("terminated", {}).get("reason") for s in ("state", "lastState")}
            if "OOMKilled" in reasons:
                oom_pods[k] += 1
    return pod_owner, restarts, oom_pods


def usage_by_container(pod_metrics, pod_owner):
    """metrics.k8s.io pods -> {(ns, kind, name, container): [(pod, cpu_m, mem_mi)]}: one current reading."""
    usage = defaultdict(list)
    for item in pod_metrics:
        ns, pod = item["metadata"]["namespace"], item["metadata"]["name"]
        owner = pod_owner.get((ns, pod))
        if not owner:
            continue
        for c in item.get("containers") or []:
            u = c.get("usage") or {}
            usage[owner + (c["name"],)].append((pod, parse_cpu(u.get("cpu")) or 0, parse_mem(u.get("memory")) or 0))
    return usage


def memory_of_request(containers, usage, w):
    """Memory in use now (metrics-server) as a % of what its pods request, every container added up; None without a
    reading or a request."""
    used = requested = 0
    for c in containers:
        values = usage.get((w["ns"], w["kind"], w["name"], c["name"])) or []
        req = parse_mem(((c.get("resources") or {}).get("requests") or {}).get("memory"))
        if values and req:
            used += sum(v[2] for v in values)
            requested += req * len(values)
    return round(100 * used / requested) if requested else None


# ------------------------------------------------------------------ HPA


def index_hpas(hpas):
    return {(h["metadata"]["namespace"], h["spec"]["scaleTargetRef"]["kind"],
             h["spec"]["scaleTargetRef"]["name"]): h for h in hpas}


def hpa_targets(h):
    """[(resource, target_type, target_value, current_value)] (autoscaling/v2)."""
    spec, st = h["spec"], h.get("status", {})
    cur = {}
    for m in st.get("currentMetrics", []) or []:
        if m.get("type") in ("Resource", "ContainerResource"):
            blk = m["resource" if m["type"] == "Resource" else "containerResource"]
            c = blk.get("current", {})
            cur[blk["name"]] = c.get("averageUtilization", c.get("averageValue"))
    out = []
    for m in spec.get("metrics", []) or []:
        mtype = m.get("type")
        if mtype in ("Resource", "ContainerResource"):
            blk = m["resource" if mtype == "Resource" else "containerResource"]
            t = blk["target"]
            out.append((blk["name"], t["type"], t.get("averageUtilization", t.get("averageValue")),
                        cur.get(blk["name"])))
        else:
            blk = m.get(mtype[:1].lower() + mtype[1:], {}) if mtype else {}
            out.append(((blk.get("metric") or {}).get("name", mtype), mtype, "-", "-"))
    return out


def at_max_since(h):
    """When the HPA reached its maxReplicas, from Kubernetes' own ScalingLimited condition (True, TooManyReplicas, with
    the time it changed): no history source needed. None when it isn't at max, or the condition doesn't say."""
    st = h.get("status", {})
    if (st.get("currentReplicas") or 0) < h["spec"]["maxReplicas"]:
        return None
    c = next((c for c in st.get("conditions") or [] if c.get("type") == "ScalingLimited"), None)
    return c.get("lastTransitionTime") if c and c.get("status") == "True" and c.get("reason") == "TooManyReplicas" else None


def since_text(stamp, now=None):
    """"3 d 4 h", "5 h 20 min", "12 min": how long ago an ISO time was."""
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return ""
    minutes = max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds() // 60))
    days, hours = minutes // 1440, minutes % 1440 // 60
    return f"{days} d {hours} h" if days else f"{hours} h {minutes % 60} min" if hours else f"{minutes} min"


def _held_by_memory(h):
    """Memory, not CPU, is what holds the HPA up."""
    best = None
    for r, tt, t, c in hpa_targets(h):
        if tt == "Utilization" and isinstance(t, (int, float)) and t and isinstance(c, (int, float)):
            if best is None or c / t > best[1] / best[2]:
                best = (r, c, t)
    if not best or best[0] != "memory" or best[1] < best[2]:
        return False
    cpu = next(((c, t) for r, tt, t, c in hpa_targets(h)
                if r == "cpu" and tt == "Utilization" and isinstance(c, (int, float))), None)
    return not (cpu and cpu[0] >= cpu[1])


def analyze_hpa(h, containers, cfg, ns, workload):
    f = []
    spec, st = h["spec"], h.get("status", {})
    mn, mx = spec.get("minReplicas", 1), spec["maxReplicas"]
    cur = st.get("currentReplicas", 0)
    at = dict(ns=ns, workload=workload)

    if mn == mx:
        f.append(finding("MEDIUM", "hpa", f"HPA min and max are both {mn}",
                         "the HPA can never add pods, so it behaves like a fixed replica count",
                         f"Set maxReplicas above {mn} if this service should scale with traffic.", **at, kind=CONFIG,
                         basis=rule(KUBERNETES, "hpa")))
    elif mn < 2:
        f.append(finding("MEDIUM", "availability", "HPA minReplicas is 1",
                         "when that single pod restarts or its node is drained, the service is down until a new pod is ready",
                         "Set minReplicas to at least 2 and add a PodDisruptionBudget.", **at, kind=CONFIG,
                         basis=rule(PRACTICE, "aks-pdb")))
    since = at_max_since(h)
    for_how_long = f" for {since_text(since)}" if since else ""
    if cur >= mx:
        if _held_by_memory(h):
            f.append(finding("HIGH", "scaling", f"At the maximum {mx} replicas{for_how_long}, held there by memory",
                             "memory, not traffic, pushes the HPA up, so raising maxReplicas may only add idle pods",
                             "Check memory at idle; raise the memory request or scale on CPU, then decide maxReplicas.", **at,
                             basis=rule(KUBERNETES, "hpa")))
        else:
            f.append(finding("HIGH", "scaling", f"At the maximum {mx} replicas{for_how_long}",
                             "it cannot add pods if traffic grows, so the existing pods take all the extra load",
                             "Raise maxReplicas and check the node pools can fit the extra pods.", **at,
                             basis=rule(KUBERNETES, "hpa")))
    elif mx and cur >= cfg.hpa_near_max * mx:
        f.append(finding("MEDIUM", "scaling", f"{cur} of max {mx} replicas",
                         "little room left if traffic keeps growing",
                         "Consider raising maxReplicas before the peak.", **at,
                         basis=rule(THRESHOLD, "hpa", "hpa_near_max_percent", f"{cfg.hpa_near_max:.0%} of maxReplicas")))

    for res, ttype, target, current in hpa_targets(h):
        if ttype != "Utilization":
            continue
        missing = [c["name"] for c in containers if not ((c.get("resources") or {}).get("requests") or {}).get(res)]
        if missing:
            f.append(finding("HIGH", "hpa", f"HPA scales on {res} %, but {', '.join(missing)} has no {res} request",
                             "the HPA calculates usage as a % of the request; without one it cannot scale",
                             f"Add a {res} request to {', '.join(missing)}.", **at, kind=CONFIG, basis=rule(KUBERNETES, "hpa")))
        if isinstance(target, (int, float)):
            if target > cfg.hpa_target_high:
                f.append(finding("MEDIUM", "hpa", f"HPA {res} target is {target}% (high)",
                                 "pods are almost full before the HPA adds more, so a spike hits busy pods",
                                 "Lower the target to around 60-75%.", **at, kind=CONFIG,
                                 basis=rule(THRESHOLD, "hpa", "hpa_target_high_percent", f"a target above {cfg.hpa_target_high}%")))
            if isinstance(current, (int, float)) and current > target * cfg.hpa_overshoot:
                f.append(finding("HIGH", "hpa", f"HPA {res} is at {current}% of request vs target {target}%",
                                 "pods use far more than they request, so the HPA overreacts and the request is too low",
                                 f"Raise the {res} request, then re-check maxReplicas.", **at,
                                 basis=rule(THRESHOLD, "hpa", "hpa_overshoot_percent", f"{cfg.hpa_overshoot:g}× the target")))
        if res == "memory":
            f.append(finding("INFO", "hpa", "HPA scales on memory",
                             "memory seldom drops with traffic, so the HPA scales up but rarely back down",
                             "Prefer CPU or requests per second unless the app frees memory under low load.", **at, kind=CONFIG,
                             basis=rule(PRACTICE, "hpa")))

    for c in st.get("conditions", []) or []:
        if c["type"] in ("ScalingActive", "AbleToScale") and c["status"] == "False":
            f.append(finding("HIGH", "hpa", f"HPA cannot scale: {c['type']} is False ({c.get('reason')})",
                             f"Kubernetes says: {c.get('message', '')[:160]}",
                             "Fix the cause (usually missing metrics or a missing request); the replica count is frozen.", **at,
                             basis=rule(KUBERNETES, "hpa")))
    return f


def describe_hpa(h):
    spec, st = h["spec"], h.get("status", {})
    targets = ", ".join(
        f"{r} {v}{'%' if tt == 'Utilization' else ''} (now {c if c is not None else '?'}"
        f"{'%' if tt == 'Utilization' and c is not None else ''})" for r, tt, v, c in hpa_targets(h))
    return {"name": h["metadata"]["name"], "min": spec.get("minReplicas", 1), "max": spec["maxReplicas"],
            "current": st.get("currentReplicas"), "desired": st.get("desiredReplicas"), "targets": targets,
            "at_max_since": at_max_since(h)}


# ------------------------------------------------------------------ PDB


def selector_matches(sel, labels):
    for k, v in (sel.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for e in sel.get("matchExpressions") or []:
        k, op, vals = e["key"], e["operator"], e.get("values") or []
        if ((op == "In" and labels.get(k) not in vals)
                or (op == "NotIn" and k in labels and labels[k] in vals)
                or (op == "Exists" and k not in labels)
                or (op == "DoesNotExist" and k in labels)):
            return False
    return True


def analyze_pdb(w, pdbs):
    at = dict(ns=w["ns"], workload=f"{w['kind']}/{w['name']}")
    matching = [p for p in pdbs.get(w["ns"], [])
                if p["spec"].get("selector") is not None and selector_matches(p["spec"]["selector"], w["labels"])]
    if not matching:
        if (w["desired"] or 0) >= 2:
            return [finding("LOW", "availability", "No PodDisruptionBudget",
                            "node upgrades or scale-down may evict all replicas at once",
                            "Add a PodDisruptionBudget, e.g. maxUnavailable: 1.", **at, kind=CONFIG,
                            basis=rule(PRACTICE, "aks-pdb"))]
        return []
    return [finding("MEDIUM", "availability", f"PodDisruptionBudget {p['metadata']['name']} allows 0 evictions",
                    "node drains, upgrades and autoscaler scale-down get stuck on these pods",
                    "Use maxUnavailable: 1, or a minAvailable lower than the replica count.", **at, kind=CONFIG,
                    basis=rule(KUBERNETES, "disruptions"))
            for p in matching if p.get("status", {}).get("disruptionsAllowed") == 0 and w["ready"]]


# ------------------------------------------------------------------ containers


def analyze_container(c, u, cfg, kind, has_hpa, oom_pods, at):
    res = c.get("resources") or {}
    req, lim = res.get("requests") or {}, res.get("limits") or {}
    cpu_req, cpu_lim = parse_cpu(req.get("cpu")), parse_cpu(lim.get("cpu"))
    mem_req, mem_lim = parse_mem(req.get("memory")), parse_mem(lim.get("memory"))
    at = dict(at, container=c["name"])
    f = []
    cpu_now = max((x[1] for x in u), default=None)
    mem_now = max((x[2] for x in u), default=None)

    if cpu_req is None:
        f.append(finding("MEDIUM", "cpu", "No CPU request",
                         "nodes get overpacked and an HPA cannot calculate CPU %", "Set a CPU request.", **at, kind=CONFIG,
                         basis=rule(PRACTICE, "aks-resources")))
    if mem_req is None:
        f.append(finding("MEDIUM", "memory", "No memory request",
                         "no memory is reserved, so it is among the first pods evicted", "Set a memory request.", **at, kind=CONFIG,
                         basis=rule(PRACTICE, "aks-resources")))
    if mem_lim is None:
        f.append(finding("LOW", "memory", "No memory limit",
                         "a leak can use up the node's memory", "Set a memory limit.", **at, kind=CONFIG,
                         basis=rule(PRACTICE, "aks-resources")))
    if oom_pods:
        f.append(finding("HIGH", "memory", f"{oom_pods} pod(s) were OOMKilled",
                         f"the container exceeded its memory limit ({fmem(mem_lim)}) and restarted",
                         "Raise the memory limit.", **at, signal="oom", basis=rule(KUBERNETES, "memory")))
    if cpu_now is not None:
        if cpu_lim and cpu_now / cpu_lim > cfg.cpu_near_limit:
            f.append(finding("HIGH", "cpu", f"CPU {fcpu(cpu_now)} is {cpu_now / cpu_lim:.0%} of the limit {fcpu(cpu_lim)}",
                             "at the limit the container is throttled; real demand is higher than measured",
                             "Raise or remove the CPU limit.", **at,
                             basis=rule(THRESHOLD, "cpu", "cpu_near_limit_percent", f"over {cfg.cpu_near_limit:.0%} of the limit")))
        if cpu_req and cpu_now / cpu_req > cfg.cpu_above_request:
            f.append(finding("MEDIUM", "cpu", f"CPU {fcpu(cpu_now)} is {cpu_now / cpu_req:.0%} of the request {fcpu(cpu_req)}",
                             "it relies on spare node CPU and slows down when the node is busy",
                             "Raise the CPU request.", **at,
                             basis=rule(THRESHOLD, "resources", "cpu_above_request_percent", f"over {cfg.cpu_above_request:.0%} of the request")))
        elif cpu_req and cpu_now / cpu_req < cfg.cpu_idle:
            f.append(finding("LOW", "cpu", f"CPU {fcpu(cpu_now)} is {cpu_now / cpu_req:.0%} of the request {fcpu(cpu_req)}",
                             "reserved CPU sits unused right now",
                             "Confirm with busy-period usage before lowering.", **at, kind=CONFIG,
                             basis=rule(THRESHOLD, "aks-resources", "cpu_idle_percent", f"under {cfg.cpu_idle:.0%} of the request")))
    if mem_now is not None:
        if mem_lim and mem_now / mem_lim > cfg.memory_near_limit and not oom_pods:
            f.append(finding("HIGH", "memory", f"Memory {fmem(mem_now)} is {mem_now / mem_lim:.0%} of the limit {fmem(mem_lim)}",
                             "if memory reaches the limit the container is OOMKilled", "Raise the memory limit.", **at,
                             basis=rule(THRESHOLD, "memory", "memory_near_limit_percent", f"over {cfg.memory_near_limit:.0%} of the limit")))
        if mem_req and mem_now > mem_req:
            f.append(finding("MEDIUM", "memory", f"Memory {fmem(mem_now)} is above the request {fmem(mem_req)}",
                             "pods above their request are evicted first under node memory pressure",
                             "Raise the memory request.", **at, basis=rule(KUBERNETES, "eviction")))
    return dict(cpu_req=cpu_req, cpu_lim=cpu_lim, mem_req=mem_req, mem_lim=mem_lim,
                cpu_now=cpu_now, mem_now=mem_now, findings=f)


# ------------------------------------------------------------------ capacity


def _pool_of(node):
    labels = node["metadata"].get("labels", {})
    return labels.get("kubernetes.azure.com/agentpool") or labels.get("agentpool") or "default"


def capacity(nodes, pods, node_metrics, cfg, profiles=None):
    """Each pool's nodes, what they give pods, what pods request and use, against the nodes running now; with the
    autoscaler's range (`profiles`, from the cluster's record) a full pool that can still grow isn't a finding."""
    profiles = profiles or {}
    pools = defaultdict(lambda: dict(nodes=0, not_ready=0, alloc_cpu=0.0, alloc_mem=0.0,
                                     req_cpu=0.0, req_mem=0.0, used_cpu=0.0, used_mem=0.0))
    node_pool, shapes, modes = {}, defaultdict(dict), {}  # pool -> {vm size: one node's capacity and allocatable}
    for n in nodes:
        name = n["metadata"]["name"]
        pool = pools[_pool_of(n)]
        node_pool[name] = _pool_of(n)
        pool["nodes"] += 1
        modes[_pool_of(n)] = (n["metadata"].get("labels") or {}).get(_NODE_LABELS["mode"]) or modes.get(_pool_of(n), "")
        size = (n["metadata"].get("labels") or {}).get(_NODE_LABELS["vm_size"], "")
        cap, alloc = n["status"].get("capacity") or {}, n["status"].get("allocatable") or {}
        shapes[_pool_of(n)].setdefault(size, {"vm_size": size, "cpu": parse_cpu(cap.get("cpu")), "memory": parse_mem(cap.get("memory")),
                                              "alloc_cpu": parse_cpu(alloc.get("cpu")), "alloc_memory": parse_mem(alloc.get("memory"))})
        pool["alloc_cpu"] += parse_cpu(n["status"]["allocatable"]["cpu"])
        pool["alloc_mem"] += parse_mem(n["status"]["allocatable"]["memory"])
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"].get("conditions", [])):
            pool["not_ready"] += 1
    pending = 0
    for pod in pods:
        phase = pod.get("status", {}).get("phase")
        pending += phase == "Pending"
        if phase not in ("Running", "Pending"):
            continue
        cpu, mem = pod_requests(pod["spec"])
        node = pod["spec"].get("nodeName")
        if node in node_pool:
            pools[node_pool[node]]["req_cpu"] += cpu
            pools[node_pool[node]]["req_mem"] += mem
    for item in node_metrics:
        name = item["metadata"]["name"]
        if name in node_pool:
            usage = item.get("usage") or {}
            pools[node_pool[name]]["used_cpu"] += parse_cpu(usage.get("cpu")) or 0
            pools[node_pool[name]]["used_mem"] += parse_mem(usage.get("memory")) or 0

    f = []
    if pending:
        f.append(finding("HIGH", "capacity", f"{pending} pod(s) are Pending",
                         "pods that cannot be placed do not serve traffic",
                         "Check FailedScheduling events for the reason.", basis=rule(KUBERNETES, "lifecycle")))
    not_ready = sum(p["not_ready"] for p in pools.values())
    if not_ready:
        f.append(finding("HIGH", "capacity", f"{not_ready} node(s) are NotReady",
                         "pods on them may be down and capacity is lower than it looks", "Check those nodes.",
                         basis=rule(KUBERNETES)))
    growth = {}
    for name, p in sorted(pools.items()):
        prof = profiles.get(name)
        top = prof.get("max") if prof and prof.get("autoscale") else (p["nodes"] if prof else None)
        can_grow = None if top is None else p["nodes"] < top  # None: the cluster's record couldn't be read
        growth[name] = {"profile": prof, "max_nodes": top, "can_grow": can_grow,
                        **{f"req_{key}_at_max": round(100 * p[f"req_{key}"] / (p[f"alloc_{key}"] / p["nodes"] * top))
                           if top and p["nodes"] and p[f"alloc_{key}"] else None for key in ("cpu", "mem")}}
        for label, key in (("CPU", "cpu"), ("memory", "mem")):
            alloc, req = p[f"alloc_{key}"], p[f"req_{key}"]
            if alloc and req / alloc > cfg.pool_requests and can_grow is not True:
                why = ("it's at its maximum of {} nodes".format(top) if prof and prof.get("autoscale") else "its size is fixed"
                       if prof else "its autoscaler range couldn't be read")
                f.append(finding("MEDIUM", "capacity", f"Pool {name}: {label} requests are {req / alloc:.0%} of allocatable, and {why}",
                                 "new pods stop fitting soon, and no node will be added for them",
                                 "Raise the pool's maximum (or add a pool), and check VM quota and subnet IPs.",
                                 basis=rule(THRESHOLD, "aks-autoscaler", "pool_requests_percent", f"over {cfg.pool_requests:.0%} of allocatable")))
    rows = [{"pool": name, "mode": modes.get(name) or (profiles.get(name) or {}).get("mode") or "",
             **{k: round(v) if isinstance(v, float) else v for k, v in p.items()}, **growth[name],
             "shapes": [{k: round(v) if isinstance(v, float) else v for k, v in s.items()} for s in shapes[name].values()]}
            for name, p in sorted(pools.items())]
    return rows, f


def top_consumers(pods, pod_metrics, pod_owner, in_scope, limit=10, node_pool=None):
    """The workloads using the most CPU and memory now (metrics-server), each against what its pods request and its
    share of what the nodes give pods, with its busiest pods and the pools they run on (`node_pool`: node -> pool).
    Empty when metrics-server isn't answering."""
    if not pod_metrics:
        return {"available": False, "by_cpu": [], "by_memory": []}
    node_pool = node_pool or {}
    running = [p for p in pods if (p.get("status") or {}).get("phase") == "Running"]
    requests = {(p["metadata"]["namespace"], p["metadata"]["name"]): pod_requests(p.get("spec") or {}) for p in running}
    pool_of_pod = {(p["metadata"]["namespace"], p["metadata"]["name"]): node_pool.get((p.get("spec") or {}).get("nodeName"))
                   for p in running}
    agg = {}
    for m in pod_metrics:
        ns, pod = m["metadata"]["namespace"], m["metadata"]["name"]
        if not in_scope(ns):
            continue
        cpu = sum(parse_cpu((c.get("usage") or {}).get("cpu")) or 0 for c in m.get("containers") or [])
        mem = sum(parse_mem((c.get("usage") or {}).get("memory")) or 0 for c in m.get("containers") or [])
        owner = pod_owner.get((ns, pod))
        key = (ns, f"{owner[1]}/{owner[2]}" if owner else f"Pod/{pod}")
        w = agg.setdefault(key, {"namespace": ns, "workload": key[1], "cpu": 0.0, "memory": 0.0, "cpu_request": 0.0,
                                 "memory_request": 0.0, "pods": [], "pools": set()})
        if pool_of_pod.get((ns, pod)):
            w["pools"].add(pool_of_pod[(ns, pod)])
        req_cpu, req_mem = requests.get((ns, pod), (0.0, 0.0))
        w["cpu"] += cpu
        w["memory"] += mem
        w["cpu_request"] += req_cpu
        w["memory_request"] += req_mem
        w["pods"].append({"pod": pod, "cpu": round(cpu), "memory": round(mem)})
    rows = []
    for w in agg.values():
        w["pods"].sort(key=lambda x: -x["cpu"])
        rows.append({**{k: round(v) if isinstance(v, float) else v for k, v in w.items() if k not in ("pods", "pools")},
                     "pod_count": len(w["pods"]), "pods": w["pods"][:5], "pools": sorted(w["pools"])})
    return {"available": True, "by_cpu": sorted(rows, key=lambda r: -r["cpu"])[:limit],
            "by_memory": sorted(rows, key=lambda r: -r["memory"])[:limit]}


# ------------------------------------------------------------------ nodes, one by one

NODE_IMAGE_WARN_DAYS, NODE_IMAGE_CRIT_DAYS = 60, 180
_PRESSURES = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
_NODE_LABELS = {"vm_size": "node.kubernetes.io/instance-type", "zone": "topology.kubernetes.io/zone",
                "node_image": "kubernetes.azure.com/node-image-version", "mode": "kubernetes.azure.com/mode"}


def node_image_age(version, today):
    """Days since the date stamped in an AKS node image version (AKSUbuntu-2204gen2containerd-202601.07.0)."""
    found = re.search(r"(\d{4})(\d{2})\.(\d{2})", version or "")
    if not found:
        return None
    try:
        return (today - date(int(found.group(1)), int(found.group(2)), int(found.group(3)))).days
    except ValueError:
        return None


def _days_since(timestamp, today):
    try:
        return (today - datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).date()).days
    except ValueError:
        return None


def _share(part, whole):
    return round(100 * part / whole) if whole else None


def node_rows(nodes, pods, node_metrics, today=None, warn_days=None):
    """One row per node: pool, readiness and pressure, cordoned, taints, versions, VM size, zone, node image age, and
    CPU, memory and pods against what it can hold (requests of the pods placed on it, usage from metrics-server)."""
    today = today or datetime.now(timezone.utc).date()
    usage = {m["metadata"]["name"]: m.get("usage") or {} for m in node_metrics}
    placed = defaultdict(lambda: {"cpu": 0.0, "mem": 0.0, "pods": 0})
    for pod in pods:
        node = (pod.get("spec") or {}).get("nodeName")
        if node and (pod.get("status") or {}).get("phase") in ("Running", "Pending"):
            cpu, mem = pod_requests(pod["spec"])
            placed[node]["cpu"] += cpu
            placed[node]["mem"] += mem
            placed[node]["pods"] += 1
    rows = []
    for n in nodes:
        meta, status, spec = n.get("metadata") or {}, n.get("status") or {}, n.get("spec") or {}
        name, labels, info = meta.get("name", ""), meta.get("labels") or {}, status.get("nodeInfo") or {}
        conditions = {c.get("type"): c for c in status.get("conditions") or []}
        ready = (conditions.get("Ready") or {}).get("status") == "True"
        alloc = status.get("allocatable") or {}
        alloc_cpu, alloc_mem = parse_cpu(alloc.get("cpu")), parse_mem(alloc.get("memory"))
        used = usage.get(name, {})
        used_cpu, used_mem = parse_cpu(used.get("cpu")), parse_mem(used.get("memory"))
        image = labels.get(_NODE_LABELS["node_image"]) or ""
        image_age = node_image_age(image, today)
        problems, status_word = [], "healthy"
        if not ready:
            reason = conditions.get("Ready") or {}
            problems.append(f"NotReady: {reason.get('reason') or 'no Ready condition'}"
                            + (f" ({reason.get('message')[:120]})" if reason.get("message") else ""))
            status_word = "critical"
        for kind in _PRESSURES:
            if (conditions.get(kind) or {}).get("status") == "True":
                problems.append(kind)
                status_word = "critical" if status_word == "critical" else "warning"
        if spec.get("unschedulable"):
            problems.append("cordoned: no new pods are placed on it")
            status_word = "critical" if status_word == "critical" else "warning"
        if image_age is not None and image_age >= (warn_days or NODE_IMAGE_WARN_DAYS):
            problems.append(f"node image {image_age} days old")
            if status_word == "healthy":
                status_word = "warning"
        rows.append({
            "name": name, "pool": _pool_of(n), "status": status_word, "ready": ready, "problems": problems,
            "unschedulable": bool(spec.get("unschedulable")),
            "taints": [f"{t.get('key')}{'=' + t['value'] if t.get('value') else ''}:{t.get('effect')}" for t in spec.get("taints") or []],
            "conditions": [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason") or "",
                            "message": (c.get("message") or "")[:200]} for c in status.get("conditions") or []],
            "kubelet": info.get("kubeletVersion") or "", "os_image": info.get("osImage") or "",
            "kernel": info.get("kernelVersion") or "", "runtime": info.get("containerRuntimeVersion") or "",
            "vm_size": labels.get(_NODE_LABELS["vm_size"]) or "", "zone": labels.get(_NODE_LABELS["zone"]) or "",
            "mode": labels.get(_NODE_LABELS["mode"]) or "", "node_image": image, "node_image_age": image_age,
            "age_days": _days_since(meta.get("creationTimestamp"), today),
            # millicores and MiB, like `capacity()`: the page shows cores and GiB for both
            "cpu": {"allocatable": round(alloc_cpu), "requested": round(placed[name]["cpu"]),
                    "used": round(used_cpu) if used else None,
                    "requested_pct": _share(placed[name]["cpu"], alloc_cpu), "used_pct": _share(used_cpu, alloc_cpu) if used else None},
            "memory": {"allocatable": round(alloc_mem), "requested": round(placed[name]["mem"]),
                       "used": round(used_mem) if used else None,
                       "requested_pct": _share(placed[name]["mem"], alloc_mem), "used_pct": _share(used_mem, alloc_mem) if used else None},
            "pods": {"placed": placed[name]["pods"], "max": int(alloc.get("pods") or 0) or None},
        })
    order = {"critical": 0, "warning": 1, "healthy": 2}
    rows.sort(key=lambda r: (order[r["status"]], r["pool"], r["name"]))
    return rows


# ------------------------------------------------------------------ entry point


MAX_NOT_RUNNING = 100


def _state_since(phase, meta, status):
    """When the pod entered its phase: a Failed pod when its containers stopped (else when Ready went False), so an
    old leftover reads as old; a Pending or Unknown one since it was created."""
    if phase == "Failed":
        finished = [s["finishedAt"] for c in status.get("containerStatuses") or []
                    for s in ((c.get("state") or {}).get("terminated") or {},) if s.get("finishedAt")]
        ready = next((c.get("lastTransitionTime") for c in status.get("conditions") or [] if c.get("type") == "Ready"), None)
        return max(finished) if finished else ready or status.get("startTime") or meta.get("creationTimestamp") or ""
    return meta.get("creationTimestamp") or status.get("startTime") or ""


def not_running(pods, in_scope):
    """Pods in scope that are neither Running nor Succeeded (Failed, Pending, Unknown), with why: the pod's own reason
    (Evicted, …) or its first waiting or terminated container's."""
    rows = []
    for pod in pods:
        meta, status = pod.get("metadata") or {}, pod.get("status") or {}
        phase = status.get("phase") or "Unknown"
        if phase in ("Running", "Succeeded") or not in_scope(meta.get("namespace")):
            continue
        states = [s for c in status.get("containerStatuses") or [] for s in ((c.get("state") or {}).get("waiting"),
                                                                             (c.get("state") or {}).get("terminated")) if s]
        state = states[0] if states else {}
        owner = next(iter(meta.get("ownerReferences") or []), {})
        rows.append({"namespace": meta.get("namespace") or "", "pod": meta.get("name") or "", "phase": phase,
                     "reason": status.get("reason") or state.get("reason") or "",
                     "message": (status.get("message") or state.get("message") or "")[:300],
                     "owner": f"{owner['kind']}/{owner['name']}" if owner.get("kind") else "",
                     "since": _state_since(phase, meta, status),
                     "started": status.get("startTime") or meta.get("creationTimestamp") or ""})
    rows.sort(key=lambda r: (r["phase"] != "Failed", r["namespace"], r["pod"]))
    return rows[:MAX_NOT_RUNNING]


_POD_ORDER = {"Failed": 0, "Pending": 1, "Unknown": 2, "Running": 3, "Succeeded": 4}


def pod_rows(pods, pod_owner, in_scope):
    """Every pod in scope, the portal's Pods list: phase and why, ready containers, restarts, node, the workload that
    owns it, and when it entered its phase. Not-ready pods first."""
    rows = []
    for pod in pods:
        meta, status, spec = pod.get("metadata") or {}, pod.get("status") or {}, pod.get("spec") or {}
        ns, name = meta.get("namespace") or "", meta.get("name") or ""
        if not in_scope(ns):
            continue
        phase = status.get("phase") or "Unknown"
        cs = status.get("containerStatuses") or []
        states = [x for c in cs for x in ((c.get("state") or {}).get("waiting"), (c.get("state") or {}).get("terminated")) if x]
        owned = pod_owner.get((ns, name))
        ref = next(iter(meta.get("ownerReferences") or []), {})
        rows.append({"namespace": ns, "pod": name, "phase": phase,
                     "reason": status.get("reason") or (states[0].get("reason") if states else "") or "",
                     "ready": sum(1 for c in cs if c.get("ready")), "containers": len(spec.get("containers") or []) or len(cs),
                     "restarts": sum(c.get("restartCount", 0) for c in cs), "node": spec.get("nodeName") or "",
                     "workload": f"{owned[1]}/{owned[2]}" if owned else "",
                     "owner": f"{ref['kind']}/{ref['name']}" if ref.get("kind") else "",
                     "since": _state_since(phase, meta, status)})
    not_ready = lambda r: r["phase"] == "Running" and r["ready"] < r["containers"]  # noqa: E731
    rows.sort(key=lambda r: (0.5 if not_ready(r) else _POD_ORDER.get(r["phase"], 2), r["namespace"], r["pod"]))
    return rows


def job_rows(jobs, in_scope):
    """Every Job in scope with how it ended: Failed, Complete, Running (active pods) or Pending, and its CronJob."""
    rows = []
    for job in jobs:
        meta, status = job.get("metadata") or {}, job.get("status") or {}
        if not in_scope(meta.get("namespace")):
            continue
        done = {c.get("type") for c in status.get("conditions") or [] if c.get("status") == "True"}
        cron = next((o["name"] for o in meta.get("ownerReferences") or [] if o.get("kind") == "CronJob"), "")
        rows.append({"namespace": meta.get("namespace") or "", "job": meta.get("name") or "", "cronjob": cron,
                     "state": "Failed" if "Failed" in done else "Complete" if "Complete" in done
                     else "Running" if status.get("active") else "Pending",
                     "succeeded": status.get("succeeded", 0), "failed": status.get("failed", 0),
                     "started": status.get("startTime") or meta.get("creationTimestamp") or "",
                     "finished": status.get("completionTime") or ""})
    order = {"Failed": 0, "Running": 1, "Pending": 2, "Complete": 3}
    rows.sort(key=lambda r: (order[r["state"]], r["namespace"], -_epoch(r["started"]), r["job"]))
    return rows


def _epoch(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def analyze(objs, pods, replicasets, jobs, hpas, pdbs, nodes, pod_metrics, node_metrics,
            cfg=None, include_system=False, profiles=None):
    cfg = cfg or Thresholds()
    system = system_namespaces(objs)
    in_scope = (lambda ns: True) if include_system else (lambda ns: ns not in system)
    workloads = [w for w in build_workloads(objs) if in_scope(w["ns"])]
    pod_owner, restarts, oom = map_pods(pods, replicasets, jobs)
    usage = usage_by_container(pod_metrics, pod_owner)
    hpa_index = index_hpas(hpas)
    pdb_index = defaultdict(list)
    for p in pdbs:
        pdb_index[p["metadata"]["namespace"]].append(p)

    findings, workload_rows, hpa_rows = [], [], []
    for w in workloads:
        label = f"{w['kind']}/{w['name']}"
        at = dict(ns=w["ns"], workload=label)
        hpa = hpa_index.get((w["ns"], w["kind"], w["name"]))
        containers = containers_of(w["spec"])
        own = []
        if w["kind"] in SCALABLE:
            if hpa:
                own += analyze_hpa(hpa, containers, cfg, w["ns"], label)
                hpa_rows.append({"namespace": w["ns"], "workload": label, **describe_hpa(hpa)})
            elif (w["desired"] or 0) == 1:
                own.append(finding("HIGH", "availability", "1 replica and no HPA",
                                   "a restart means downtime and it cannot scale with traffic",
                                   "Run at least 2 replicas or add an HPA, plus a PodDisruptionBudget.", **at, kind=CONFIG,
                                   basis=rule(PRACTICE, "aks-pdb")))
            if w["desired"] and (w["ready"] or 0) < w["desired"]:
                own.append(finding("HIGH", "availability", f"{w['ready'] or 0} of {w['desired']} replicas ready",
                                   "the service runs below its intended capacity",
                                   "Check the pods' events and logs.", **at, basis=rule(KUBERNETES, "deployment")))
            own += analyze_pdb(w, pdb_index)
        for c in containers:
            key = (w["ns"], w["kind"], w["name"], c["name"])
            result = analyze_container(c, usage.get(key, []), cfg, w["kind"], bool(hpa), oom.get(key, 0), at)
            own += result["findings"]
            if restarts.get(key):
                own.append(finding("INFO" if restarts[key] < cfg.restarts_warning else "MEDIUM", "stability",
                                   f"{restarts[key]} container restart(s) across its pods",
                                   "restarts mean crashes, failed probes or OOM", "Check the container's last state and logs.",
                                   **dict(at, container=c["name"]), signal="restarts",
                                   basis=rule(THRESHOLD, "lifecycle", "restarts_warning", f"{cfg.restarts_warning} restarts or more")))
        worst = min((SEVERITIES.index(x["severity"]) for x in own), default=None)
        workload_rows.append({"namespace": w["ns"], "workload": label, "kind": w["kind"], "desired": w["desired"], "ready": w["ready"],
                              "hpa": bool(hpa), "severity": SEVERITIES[worst] if worst is not None else None,
                              "findings": len(own), "memory_of_request": memory_of_request(containers, usage, w),
                              "runtime_caps": runtime_caps.workload_caps(containers)})
        findings += own

    pool_rows, capacity_findings = capacity(nodes, pods, node_metrics, cfg, profiles)
    findings = capacity_findings + findings
    findings.sort(key=lambda x: (x["kind"] != NOW, SEVERITIES.index(x["severity"])))
    counts = {s: sum(1 for x in findings if x["severity"] == s) for s in SEVERITIES}
    counts_by_kind = {kind: {s: sum(1 for x in findings if x["kind"] == kind and x["severity"] == s) for s in SEVERITIES}
                      for kind in (NOW, CONFIG)}
    return {"findings": findings, "counts": counts, "counts_by_kind": counts_by_kind, "workloads": workload_rows, "hpas": hpa_rows,
            "pools": pool_rows, "pool_requests_warn": cfg.pool_requests, "usage_available": bool(pod_metrics),
            "not_running": not_running(pods, in_scope), "pod_rows": pod_rows(pods, pod_owner, in_scope),
            "job_rows": job_rows(jobs, in_scope),
            "top": top_consumers(pods, pod_metrics, pod_owner, in_scope, node_pool={n["metadata"]["name"]: _pool_of(n) for n in nodes}),
            "system_namespaces": [{"namespace": ns, "why": why} for ns, why in sorted(system.items())]}
