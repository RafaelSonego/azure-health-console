"""AKS Workloads' "What's wrong now": every source's rows about this moment, one entry per workload, with which sources
saw each signal; a verdict line over them; Failed pods left over from long ago apart; Deployments rolled out lately."""
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import aks_checks

POD_TEMPLATE_HASH = re.compile(r"-[a-z0-9]{6,10}$")  # a ReplicaSet is <deployment>-<pod template hash>
CRONJOB_RUN = re.compile(r"-\d{8,}$")  # a CronJob's Job is <cronjob>-<scheduled minute>
LEFTOVER = timedelta(hours=24)  # a Failed pod older than this has usually been replaced (aks.leftover_hours)

K8S, CI, PLATFORM = "Kubernetes API", "Container Insights", "platform metrics"
SEVERITY = {"not_running": "HIGH", "oom": "HIGH", "near_limit": "HIGH", "restarts": "MEDIUM", "events": "MEDIUM",
            "pods": "MEDIUM"}  # per signal type; a Kubernetes API finding keeps its own. `pods`: a bare count, maybe leftovers
ORDER = ("HIGH", "MEDIUM")
CLUSTER = ""  # the group key for cluster-wide signals (nodes, pools, Pending pods counted per cluster)


def workload_of(kind, name):
    """'Kind/name' of the workload behind a pod's owner (a ReplicaSet's Deployment, a Job's CronJob), else ''."""
    if not kind or not name:
        return ""
    if kind == "ReplicaSet":
        return f"Deployment/{POD_TEMPLATE_HASH.sub('', name)}" if POD_TEMPLATE_HASH.search(name) else f"ReplicaSet/{name}"
    if kind == "Job":
        return f"CronJob/{CRONJOB_RUN.sub('', name)}" if CRONJOB_RUN.search(name) else f"Job/{name}"
    return f"{kind}/{name}"


def _owner_label(owner):
    """An owner written 'Kind/name' or 'Kind name' as its workload."""
    kind, _, name = str(owner or "").replace(" ", "/", 1).partition("/")
    return workload_of(kind, name)


def _parse(moment):
    try:
        return datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_leftover(row, now, leftover=LEFTOVER):
    since = _parse(row.get("since")) if row.get("phase") == "Failed" else None
    return bool(since and now - since > leftover)


def rollouts(objs, replicasets, in_scope, now, hours=24):
    """Deployments whose newest ReplicaSet appeared within `hours`, with the images it changed; StatefulSets and
    DaemonSets still moving to a new revision (they keep no time for it). Keyed (namespace, 'Kind/name')."""
    owned = defaultdict(list)
    for rs in replicasets:
        meta = rs.get("metadata") or {}
        for o in meta.get("ownerReferences") or []:
            if o.get("kind") == "Deployment":
                revision = (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision", "0")
                owned[(meta.get("namespace"), o.get("name"))].append((int(revision) if str(revision).isdigit() else 0, rs))
    out = {}
    for d in objs.get("Deployment", []):
        meta = d["metadata"]
        if not in_scope(meta.get("namespace")):
            continue
        sets = sorted(owned.get((meta.get("namespace"), meta.get("name")), []), key=lambda x: x[0])
        if not sets:
            continue
        revision, newest = sets[-1]
        at = _parse((newest.get("metadata") or {}).get("creationTimestamp"))
        if not at or now - at > timedelta(hours=hours):
            continue
        images = []
        if len(sets) > 1:
            before = {c["name"]: c.get("image", "") for c in _containers(sets[-2][1])}
            images = [{"container": c["name"], "from": before[c["name"]], "to": c.get("image", "")}
                      for c in _containers(newest) if c["name"] in before and before[c["name"]] != c.get("image", "")]
        status, wanted = d.get("status") or {}, (d.get("spec") or {}).get("replicas", 1)
        out[(meta["namespace"], f"Deployment/{meta['name']}")] = {
            "at": newest["metadata"]["creationTimestamp"], "revision": revision, "images": images,
            "rolling": (status.get("updatedReplicas") or 0) < (wanted or 0)}
    for kind in ("StatefulSet", "DaemonSet"):
        for o in objs.get(kind, []):
            meta, status = o["metadata"], o.get("status") or {}
            if in_scope(meta.get("namespace")) and status.get("updateRevision") and status.get("currentRevision") \
                    and status["updateRevision"] != status["currentRevision"]:
                out[(meta["namespace"], f"{kind}/{meta['name']}")] = {"at": None, "revision": None, "images": [], "rolling": True}
    return out


def _containers(rs):
    return (((rs.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []


def _rows(ci, name):
    value = ci.get(name)
    return value if isinstance(value, list) else []


class _Groups:
    def __init__(self):
        self.groups = {}

    def add(self, namespace, workload, kind, text, source, pods=(), severity=None, detail="", action="", rule=None):
        key = (namespace or "", workload or "")
        g = self.groups.setdefault(key, {"namespace": key[0], "workload": key[1], "signals": [], "pods": set()})
        g["pods"].update(p for p in pods if p)
        for s in g["signals"]:  # the same thing seen by a second source: one line, both sources
            if s["type"] == kind and s["text"] == text:
                if source not in s["sources"]:
                    s["sources"].append(source)
                return
        g["signals"].append({"type": kind, "severity": severity or SEVERITY[kind], "text": text, "sources": [source],
                             "detail": detail, "action": action, **({"rule": rule} if rule else {})})


def _count_text(counter):
    return ", ".join(f"{name} ({n})" if n > 1 else name for name, n in sorted(counter.items(), key=lambda x: -x[1]))


def problems(phases, ci, k, pod_owner=None, now=None, include_system=False, leftover_hours=24):
    """{"groups", "leftover", "verdict"}: `phases` platform metrics' pod phases, `ci` Container Insights' lists and
    `k` the Kubernetes API's result as `workloads()` returns them; `pod_owner` maps (namespace, pod) to (namespace,
    kind, name) when the API was read."""
    now = now or datetime.now(timezone.utc)
    pod_owner = pod_owner or {}
    groups = _Groups()

    seen = {}  # (namespace, pod) -> workload, from any row that named the pod's controller

    def owner(namespace, pod, kind="", name="", label=""):
        known = pod_owner.get((namespace, pod))
        if known:
            return f"{known[1]}/{known[2]}"
        found = workload_of(kind, name) or _owner_label(label)
        if found:
            seen[(namespace, pod)] = found
        return found or seen.get((namespace, pod)) or (f"Pod/{pod}" if pod else "")

    leftover, not_running = [], defaultdict(lambda: {"pods": set(), "reasons": defaultdict(int), "sources": set()})
    leftover_pods = set()
    if k.get("available"):
        for r in k.get("not_running") or []:
            workload = owner(r["namespace"], r["pod"], label=r.get("owner"))
            if is_leftover(r, now, timedelta(hours=leftover_hours)):
                leftover.append({**r, "workload": workload})
                leftover_pods.add((r["namespace"], r["pod"]))
                continue
            n = not_running[(r["namespace"], workload)]
            n["pods"].add(r["pod"])
            n["reasons"][r.get("reason") or r["phase"]] += 1
            n["sources"].add(K8S)
    if ci.get("available"):
        for r in _rows(ci, "not_running"):
            ns, pod = r.get("Namespace") or "", r.get("Pod") or ""
            if (ns, pod) in leftover_pods:
                continue
            n = not_running[(ns, owner(ns, pod, r.get("OwnerKind"), r.get("Owner")))]
            if pod not in n["pods"]:
                n["reasons"][r.get("Reason") or r.get("Status") or "not running"] += 1
            n["pods"].add(pod)
            n["sources"].add(CI)
    for (ns, workload), n in not_running.items():
        for source in sorted(n["sources"]):
            groups.add(ns, workload, "not_running", f"{len(n['pods'])} pod(s) not running: {_count_text(n['reasons'])}",
                       source, n["pods"])

    hours = ci.get("hours") or {}
    if ci.get("available"):
        restarts = defaultdict(lambda: {"n": 0, "pods": set(), "containers": set()})
        for r in _rows(ci, "restarts"):
            ns, pod = r.get("Namespace") or "", r.get("Pod") or ""
            x = restarts[(ns, owner(ns, pod, r.get("OwnerKind"), r.get("Owner")))]
            x["n"] += int(r.get("Restarts") or 0)
            x["pods"].add(pod)
            x["containers"].add(r.get("Container") or "")
        for (ns, workload), x in restarts.items():
            groups.add(ns, workload, "restarts", f"{x['n']} restart(s), last {_span(hours.get('restarts', 1))}", CI, x["pods"],
                       detail=f"containers {', '.join(sorted(filter(None, x['containers'])))}")
        kills = defaultdict(lambda: {"n": 0, "pods": set(), "causes": defaultdict(int)})
        for r in _rows(ci, "oomkilled"):
            ns, pod = r.get("namespace") or "", r.get("pod") or ""
            x = kills[(ns, owner(ns, pod, label=r.get("owner")) if pod else r.get("workload") or "")]
            x["n"] += 1
            x["pods"].add(pod)
            x["causes"][CAUSES.get(r.get("cause"), "can't tell")] += 1
        for (ns, workload), x in kills.items():
            groups.add(ns, workload, "oom", f"{x['n']} OOM kill(s), last {_span(hours.get('oomkilled', 24))}", CI, x["pods"],
                       detail=f"cause: {_count_text(x['causes'])}")
        for r in _rows(ci, "hot_containers"):
            ns, pod = r.get("Namespace") or "", r.get("Pod") or ""
            figures = [f"{label} {fmt_pct(r.get(key))} of its limit" for label, key, limit in
                       (("CPU", "CpuPctOfLimit", 80), ("memory", "MemPctOfLimit", 85)) if (r.get(key) or 0) > limit]
            groups.add(ns, owner(ns, pod, r.get("OwnerKind"), r.get("Owner")), "near_limit",
                       f"{r.get('Container') or 'a container'} near its limits: {', '.join(figures)}", CI, [pod])
        _add_events(groups, _rows(ci, "events"), owner, CI, hours.get("events", 1))
    if not ci.get("available") and k.get("available"):  # no Container Insights: the cluster's own (about an hour)
        _add_events(groups, k.get("events") or [], owner, K8S, k.get("events_hours", 1))

    signals_of = {key: {s["type"] for s in g["signals"]} for key, g in groups.groups.items()}
    if k.get("available"):
        for f in k.get("findings") or []:
            if f.get("kind") != "now" or f.get("severity") not in ORDER:
                continue
            key = (f.get("namespace") or "", f.get("workload") or "")
            already = signals_of.get(key, set())
            if (f.get("signal") == "restarts" and "restarts" in already) or (f.get("signal") == "oom" and "oom" in already):
                continue  # Container Insights already counts these over a window, not since the pod began
            text = f"{f.get('container')}: {f['problem']}" if f.get("container") else f["problem"]
            groups.add(*key, "k8s", text, K8S, severity=f["severity"], detail=f.get("why", ""), action=f.get("action", ""),
                       rule=f.get("rule"))
    elif not ci.get("available") and isinstance(phases, list):
        for r in phases:
            if r.get("phase") not in ("Running", "Succeeded"):
                groups.add(r.get("namespace") or "", "", "pods", f"{r['pods']} pod(s) {r['phase']}", PLATFORM,
                           detail="only counted: which workload, and whether they're new or left over, needs the "
                                  "Kubernetes API or Container Insights")

    # System namespaces (the API's, else Kubernetes' own) are hidden unless asked for, whichever source saw them.
    hidden = set() if include_system else ({s["namespace"] for s in k.get("system_namespaces") or []}
                                           if k.get("available") else set(aks_checks.KUBERNETES_NAMESPACES))
    groups.groups = {key: g for key, g in groups.groups.items() if key[0] not in hidden}
    leftover = [r for r in leftover if r["namespace"] not in hidden]
    rolled = rollouts_of(k)
    sized = {(w["namespace"], w["workload"]): w for w in k.get("workload_rows") or []}
    out = []
    for key, g in groups.groups.items():
        g["signals"].sort(key=lambda s: ORDER.index(s["severity"]))
        g["severity"] = g["signals"][0]["severity"]
        g["sources"] = sorted({src for s in g["signals"] for src in s["sources"]})
        g["pods"] = sorted(g["pods"])
        size = sized.get(key)
        g["ready"], g["desired"] = (size["ready"], size["desired"]) if size else (None, None)
        g["changed"] = rolled.get(key)
        out.append(g)
    out.sort(key=lambda g: (g["workload"] != CLUSTER or g["namespace"] != "", ORDER.index(g["severity"]), -len(g["signals"]),
                            g["namespace"], g["workload"]))
    leftover.sort(key=lambda r: (r["namespace"], r["pod"]))
    return {"groups": out, "leftover": leftover, "verdict": verdict(out, leftover, rolled, phases, ci, k),
            "severity": SEVERITY}


CAUSES = {"container": "container limit", "node": "node memory", "unknown": "can't tell"}


def fmt_pct(value):
    return f"{round(value)}%" if isinstance(value, (int, float)) else "–"


def _add_events(groups, rows, owner, source, hours):
    """Warning event rows (Reason, Namespace, Object, ObjectKind, Count) as one line per workload, node ones per cluster."""
    events = defaultdict(lambda: {"reasons": defaultdict(int), "pods": set()})
    for r in rows:
        ns, obj, kind = r.get("Namespace") or "", r.get("Object") or "", r.get("ObjectKind") or ""
        if kind == "Node":
            key, pod = (CLUSTER, ""), ""
        elif kind in ("Pod", ""):
            key, pod = (ns, owner(ns, obj)), obj
        else:
            key, pod = (ns, workload_of(kind, obj)), ""
        events[key]["reasons"][r.get("Reason") or "Warning"] += int(r.get("Count") or 1)
        events[key]["pods"].add(pod)
    for (ns, workload), x in events.items():
        text = f"warning events, last {_span(hours)}: " + ", ".join(
            f"{reason} ×{n}" for reason, n in sorted(x["reasons"].items(), key=lambda i: -i[1]))
        groups.add(ns, workload, "events", text, source, x["pods"])


def _span(hours):
    hours = int(hours or 1)
    return ("24 h" if hours == 24 else f"{hours // 24} days") if hours >= 24 and hours % 24 == 0 else ("hour" if hours == 1 else f"{hours} h")


def rollouts_of(k):
    return {(r["namespace"], r["workload"]): r for r in k.get("rollouts") or []}


def verdict(groups, leftover, rolled, phases, ci, k):
    """The line above the lists: how many workloads are in trouble, the cluster's own, and what was read to say so."""
    cluster = next((g for g in groups if g["namespace"] == "" and g["workload"] == CLUSTER), None)
    workloads = [g for g in groups if g is not cluster]
    read = [name for name, ok in ((K8S, k.get("available")), (CI, ci.get("available")), (PLATFORM, isinstance(phases, list))) if ok]
    missing = [{"source": name, "reason": (why or "").splitlines()[0][:240] if why else ""} for name, ok, why in (
        (K8S, k.get("available"), k.get("reason")), (CI, ci.get("available"), ci.get("reason"))) if not ok]
    return {"workloads": len(workloads), "critical": sum(1 for g in workloads if g["severity"] == "HIGH"),
            "only_counts": not k.get("available") and not ci.get("available"),
            "cluster": [s["text"] for s in cluster["signals"]] if cluster else [], "leftover": len(leftover),
            "rolled_out": sorted(f"{ns}/{w}" for (ns, w), r in rolled.items() if r.get("at")),
            "rolling": sorted(f"{ns}/{w}" for (ns, w), r in rolled.items() if r.get("rolling")),
            "read": read, "missing": missing}
