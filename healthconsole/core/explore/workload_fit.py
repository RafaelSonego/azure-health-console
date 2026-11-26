"""Would a change fit: a workload's pods with new requests, against the nodes they can run on now. How many pods the
HPA would then ask for (its own arithmetic with the new request), where they'd fit node by node, the pools' requests
before and after, the nodes missing when they don't, and whether a rolling update can start. Kubernetes API reads only."""
import math

from . import aks, aks_checks, kube
from .workload_detail import HPA_TOLERANCE, _owned, _selector, hpa_decision, read_lists

LIVE = ("Running", "Pending")


def _ready(node):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", []))


def _alloc(node):
    a = node["status"].get("allocatable") or {}
    return aks_checks.parse_cpu(a.get("cpu")) or 0.0, aks_checks.parse_mem(a.get("memory")) or 0.0


def _daemonset(pod):
    return any(o.get("kind") == "DaemonSet" for o in pod["metadata"].get("ownerReferences") or [])


def pod_request(spec, new=None):
    """(cpu m, memory MiB) one pod requests, with `new` {container: (cpu, mem)} in place of those containers' own."""
    cpu = mem = 0.0
    for c in aks_checks.containers_of(spec):
        r = (c.get("resources") or {}).get("requests") or {}
        own = (aks_checks.parse_cpu(r.get("cpu")) or 0, aks_checks.parse_mem(r.get("memory")) or 0)
        c_cpu, c_mem = (new or {}).get(c["name"], own)
        cpu, mem = cpu + c_cpu, mem + c_mem
    return cpu, mem


def replicas_after(decision, old, new, fixed):
    """(replicas, why, [per metric]): the HPA's own arithmetic with the new request (a utilization is use over the
    summed request, so it scales by old/new), within min and max; `fixed` replicas when there's no HPA."""
    if not decision:
        return fixed, "no HPA: it keeps its fixed replicas", []
    current, rows = decision["current"] or fixed, []
    for m in decision["metrics"]:
        index = {"cpu": 0, "memory": 1}.get(m["resource"])
        if m["type"] == "Utilization" and index is not None and isinstance(m["current"], (int, float)) and m["target"] and new[index]:
            now = m["current"] * old[index] / new[index]
            ratio = now / m["target"]
            asks = current if abs(ratio - 1) <= HPA_TOLERANCE else math.ceil(current * ratio)
            rows.append({"resource": m["resource"], "now": m["current"], "after": round(now), "target": m["target"], "asks": asks})
        elif m["asks"] is not None:
            rows.append({"resource": m["resource"], "now": m["current"], "after": m["current"], "target": m["target"], "asks": m["asks"]})
    if not rows:
        return current, "its HPA has no metric to recompute: it keeps today's replicas", []
    top = max(rows, key=lambda r: r["asks"])
    replicas = min(max(top["asks"], decision["min"]), decision["max"])
    why = f"its HPA: {top['resource']} asks for {top['asks']}"
    if top["asks"] > decision["max"]:
        why += f", held at its maximum of {decision['max']}"
    elif top["asks"] < decision["min"]:
        why += f", raised to its minimum of {decision['min']}"
    return replicas, why, rows


def _fits(free, per_pod):
    """How many pods of `per_pod` fit in `free` (cpu, mem); a zero request doesn't limit."""
    counts = [math.floor(f / p) for f, p in zip(free, per_pod) if p > 0]
    return max(0, min(counts)) if counts else 10**6


def _surge(kind, strategy, replicas):
    """(max surge, max unavailable) in pods: a Deployment's RollingUpdate (25% each by default); Recreate stops them
    all first; StatefulSets and DaemonSets replace one pod in place."""
    if strategy.get("type") == "Recreate":
        return 0, replicas
    if kind != "Deployment":
        return 0, 1
    ru = strategy.get("rollingUpdate") or {}

    def pods(value, default, up):
        value = default if value is None else value
        if isinstance(value, str) and value.endswith("%"):
            share = replicas * float(value[:-1]) / 100
            return math.ceil(share) if up else math.floor(share)
        return int(value)
    return pods(ru.get("maxSurge"), "25%", True), pods(ru.get("maxUnavailable"), "25%", False)


def plan(kind, obj, own, all_pods, nodes, decision, new, replicas=None, profiles=None):
    """The fit, from lists already read: `own` the workload's pods, `all_pods` every live pod, `new` {container: (cpu m,
    memory MiB)}, `replicas` to override the HPA's arithmetic, `profiles` {pool: autoscaler facts} from ARM."""
    spec = obj["spec"]["template"]["spec"]
    old, after_pod = pod_request(spec), pod_request(spec, new)
    own_live = [p for p in own if (p.get("status") or {}).get("phase") in LIVE]
    fixed = len(own_live) if kind == "DaemonSet" else spec_replicas(obj, own_live)
    if replicas:
        count, why, metrics = replicas, "the replicas you gave", []
    elif kind == "DaemonSet":
        count, why, metrics = fixed, "a DaemonSet: one pod per node it runs on", []
    else:
        count, why, metrics = replicas_after(decision, old, after_pod, fixed)

    pool_of = {n["metadata"]["name"]: aks_checks._pool_of(n) for n in nodes}
    own_names = {p["metadata"]["name"] for p in own}
    on_node = {}
    for p in own_live:
        node = p["spec"].get("nodeName")
        if node:
            on_node[node] = on_node.get(node, 0) + 1
    pools = sorted({pool_of[n] for n in on_node if n in pool_of})
    selector = spec.get("nodeSelector") or {}
    if not pools:
        pools = sorted({pool_of[n["metadata"]["name"]] for n in nodes
                        if all((n["metadata"].get("labels") or {}).get(k) == v for k, v in selector.items())})
    notes = []
    if not on_node:
        notes.append("None of its pods is on a node now, so every pool its node selector allows is counted.")

    rows = {}
    for n in nodes:
        name = n["metadata"]["name"]
        if pool_of[name] not in pools:
            continue
        cpu, mem = _alloc(n)
        usable = _ready(n) and not n["spec"].get("unschedulable")
        rows[name] = {"node": name, "pool": pool_of[name], "alloc": [cpu, mem], "others": [0.0, 0.0], "daemons": [0.0, 0.0],
                      "own": on_node.get(name, 0), "usable": usable}
    for p in all_pods:
        node = (p.get("spec") or {}).get("nodeName")
        if node not in rows or (p.get("status") or {}).get("phase") not in LIVE or p["metadata"]["name"] in own_names:
            continue
        cpu, mem = aks_checks.pod_requests(p["spec"])
        rows[node]["others"][0] += cpu
        rows[node]["others"][1] += mem
        if _daemonset(p):
            rows[node]["daemons"][0] += cpu
            rows[node]["daemons"][1] += mem

    usable = [r for r in rows.values() if r["usable"]]
    if len(usable) < len(rows):
        notes.append(f"{len(rows) - len(usable)} node(s) NotReady or cordoned are left out.")
    for r in usable:
        free = [a - o for a, o in zip(r["alloc"], r["others"])]
        r["room_after"] = _fits(free, after_pod)
        r["room_now"] = _fits([f - r["own"] * x for f, x in zip(free, old)], after_pod)

    # Steady state: each new pod to the node with the most room left, as the scheduler's spreading would.
    placed = {r["node"]: 0 for r in usable}
    if kind == "DaemonSet":
        for r in usable:
            placed[r["node"]] = 1 if r["room_after"] >= 1 else 0
        short = sum(1 for r in usable if r["room_after"] < 1)
    else:
        left = count
        while left:
            best = max(usable, key=lambda r: r["room_after"] - placed[r["node"]], default=None)
            if not best or best["room_after"] - placed[best["node"]] < 1:
                break
            placed[best["node"]] += 1
            left -= 1
        short = left

    pool_rows = []
    for pool in pools:
        mine = [r for r in rows.values() if r["pool"] == pool]
        alloc = [sum(r["alloc"][i] for r in mine) for i in (0, 1)]
        others = [sum(r["others"][i] for r in mine) for i in (0, 1)]
        before_pods = sum(r["own"] for r in mine)
        after_pods = sum(placed.get(r["node"], 0) for r in mine)
        pool_rows.append({"pool": pool, "nodes": len(mine), "usable": sum(1 for r in mine if r["usable"]),
                          "alloc_cpu": round(alloc[0]), "alloc_mem": round(alloc[1]),
                          "before_cpu": round(others[0] + before_pods * old[0]), "before_mem": round(others[1] + before_pods * old[1]),
                          "after_cpu": round(others[0] + after_pods * after_pod[0]), "after_mem": round(others[1] + after_pods * after_pod[1]),
                          "pods_before": before_pods, "pods_after": after_pods, "autoscaler": (profiles or {}).get(pool)})

    missing = None
    if short:
        # A new node of the pool that holds most of them: its allocatable less what DaemonSets put on every node.
        pool = max(pool_rows, key=lambda r: r["pods_after"] + r["pods_before"])["pool"] if pool_rows else None
        sample = [r for r in rows.values() if r["pool"] == pool]
        per_node = max((_fits([a - d for a, d in zip(r["alloc"], r["daemons"])], after_pod) for r in sample), default=0)
        auto = (profiles or {}).get(pool) or {}
        need = math.ceil(short / per_node) if per_node else None
        missing = {"pool": pool, "pods": short, "per_node": per_node, "nodes": need,
                   "room": (auto["max"] - auto["count"]) if auto.get("autoscale") and auto.get("max") is not None else None}

    surge, unavailable = _surge(kind, obj["spec"].get("strategy") or obj["spec"].get("updateStrategy") or {}, count)
    room_now = sum(r["room_now"] for r in usable)
    with_room = sorted((r for r in usable if r["room_now"] >= 1), key=lambda r: -r["room_now"])
    rollout = {"surge": surge, "unavailable": unavailable, "room_now": room_now, "nodes_with_room": len(with_room),
               "nodes": [{"node": r["node"], "pods": r["room_now"]} for r in with_room[:5]]}
    if kind == "DaemonSet":
        rollout["note"] = "A DaemonSet replaces its pod on each node in place, so only the steady state above matters."
    elif surge and room_now < surge and not unavailable:
        rollout["note"] = (f"The rollout starts {surge} new pod(s) before stopping any old one (maxUnavailable 0), and "
                           f"only {room_now} fit next to the old pods now: it waits for new nodes.")
    elif surge and room_now == 0:
        rollout["note"] = ("No node has room for a new pod next to the old ones, so it can only move by stopping "
                           f"{unavailable} old pod(s) at a time (maxUnavailable).")
    elif len(with_room) == 1 and surge > 1:
        rollout["note"] = f"Only {with_room[0]['node']} has room for new pods next to the old ones, so the first ones all start there."
    else:
        rollout["note"] = ""
    notes.append("Taints, affinity and topology spread aren't modelled: a pod counts for any node of the pools it runs on now.")
    return {"available": True, "per_pod": {"before": [round(old[0]), round(old[1])], "after": [round(after_pod[0]), round(after_pod[1])]},
            "replicas": {"now": len(own_live), "after": count, "why": why, "metrics": metrics},
            "fits": not short, "short": short, "missing": missing, "pools": pool_rows, "rollout": rollout, "notes": notes}


def spec_replicas(obj, live):
    value = obj["spec"].get("replicas")
    return value if isinstance(value, int) else len(live)


pool_profiles = aks.pool_profiles


def read(client, cluster_id, namespace, kind, name, new, replicas=None, kube_settings=None):
    """`plan` for one workload, reading its object, its pods, its HPA, the nodes and every live pod on them."""
    k, why = aks._connect(client, cluster_id, kube_settings)
    if k is None:
        return {"available": False, "reason": why}
    try:
        obj = k.get(f"/apis/apps/v1/namespaces/{namespace}/{kind.lower()}s/{name}")
    except kube.KubeUnavailable as error:
        return {"available": False, "reason": str(error)}
    if obj is None:
        return {"available": False, "reason": f"There's no {kind} {name} in {namespace} now."}
    known = {c["name"] for c in aks_checks.containers_of(obj["spec"]["template"]["spec"])}
    if set(new) - known:
        return {"available": False, "reason": f"{name} has no container {', '.join(sorted(set(new) - known))}."}
    selector = _selector(obj)
    lists = {"pods": (f"/api/v1/namespaces/{namespace}/pods", {"labelSelector": selector} if selector else None),
             "hpas": (f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers", None),
             "nodes": ("/api/v1/nodes", None),
             "all_pods": ("/api/v1/pods", {"fieldSelector": "status.phase!=Succeeded,status.phase!=Failed"})}
    if kind == "Deployment":
        lists["replicasets"] = (f"/apis/apps/v1/namespaces/{namespace}/replicasets", None)
    data, failed = read_lists(k, lists)
    for key in ("nodes", "all_pods"):
        if key in failed:
            return {"available": False, "reason": f"The {'nodes' if key == 'nodes' else 'pods of every namespace'} couldn't be read: {failed[key]}"}
    own = _owned(data["pods"], kind, name, data.get("replicasets", []))
    hpa = aks_checks.index_hpas(data["hpas"]).get((namespace, kind, name))
    out = plan(kind, obj, own, data["all_pods"], data["nodes"], hpa_decision(hpa) if hpa else None, new, replicas,
               pool_profiles(client, cluster_id))
    if "hpas" in failed:
        out["notes"].insert(0, f"Its HPA couldn't be read ({failed['hpas']}), so the replicas stay as they are.")
    return out
