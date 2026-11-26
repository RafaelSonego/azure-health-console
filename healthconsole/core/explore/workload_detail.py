"""One workload, opened from a line on the Workloads tab: what it runs, how its HPA decides, what its containers
request against what they use, where its pods are, and its recent events (Kubernetes API, read when opened); then its
CPU and memory over 7 and 30 days and its runtime's memory, from Container Insights or the monitoring tool."""
import math
import re
from concurrent.futures import ThreadPoolExecutor

from ..arm import ArmError
from ..investigate.appinsights import kql_string
from . import aks, aks_checks, appinsights, kube
from .kube_yaml import yaml_of
from .workload_path import _owner_regex, app_candidates, common_words, tokens

EVENTS_SHOWN = 30
HPA_TOLERANCE = 0.1  # Kubernetes leaves the replica count alone within 10% of the target
# Env vars and arguments that carry a runtime's memory settings; only the matching flags are kept, never the value.
MEMORY_ENV = ("JAVA_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CATALINA_OPTS", "NODE_OPTIONS",
              "GOMEMLIMIT", "DOTNET_GCHeapHardLimit", "DOTNET_GCHeapHardLimitPercent")
MEMORY_FLAG = re.compile(r"(-Xm[xs][0-9]+[kKmMgG]?|-XX:(?:Max|Initial|Min)RAMPercentage=[0-9.]+|-XX:MaxMetaspaceSize=[0-9]+[kKmMgG]?"
                         r"|-XX:MaxDirectMemorySize=[0-9]+[kKmMgG]?|-XX:[+-]UseContainerSupport|--max-old-space-size=[0-9]+)")
PLAIN_VALUE = re.compile(r"^[0-9]+(\.[0-9]+)?[A-Za-z]{0,3}$|^0x[0-9a-fA-F]+$")  # GOMEMLIMIT=6GiB, DOTNET_…=0x…
_UNITS = {"k": 1 / 1024, "m": 1, "g": 1024}


def memory_flags(container):
    """[{source, flag}] for the container's runtime memory settings in its env, command and args. Only flags that
    match MEMORY_FLAG (or a plain number for GOMEMLIMIT and .NET) are returned; the rest of a value is never kept."""
    out = []
    for env in container.get("env") or []:
        name = env.get("name") or ""
        if name not in MEMORY_ENV:
            continue
        if "valueFrom" in env:
            out.append({"source": f"env {name}", "flag": "", "note": "set from a ConfigMap or Secret, not read"})
            continue
        value = str(env.get("value") or "")
        if name.startswith(("GOMEMLIMIT", "DOTNET_")):
            if PLAIN_VALUE.match(value.strip()):
                out.append({"source": f"env {name}", "flag": f"{name}={value.strip()}"})
            continue
        out += [{"source": f"env {name}", "flag": flag} for flag in MEMORY_FLAG.findall(value)]
    for field in ("command", "args"):
        for part in container.get(field) or []:
            out += [{"source": field, "flag": flag} for flag in MEMORY_FLAG.findall(str(part))]
    return out


def heap_max_mib(flags, memory_limit_mib=None):
    """The heap ceiling the flags set, in MiB: -Xmx, or MaxRAMPercentage of the memory limit; None when unset."""
    for f in flags:
        m = re.match(r"-Xmx([0-9]+)([kKmMgG]?)$", f["flag"])
        if m:
            return int(m.group(1)) * _UNITS.get(m.group(2).lower(), 1 / 2**20)
    for f in flags:
        m = re.match(r"-XX:MaxRAMPercentage=([0-9.]+)$", f["flag"])
        if m and memory_limit_mib:
            return float(m.group(1)) / 100 * memory_limit_mib
    return None


# ------------------------------------------------------------------ HPA

def hpa_decision(h):
    """{name, min, max, current, desired, metrics: [{resource, type, target, current, asks}], decides, limited,
    conditions, behavior, last_scale}: per metric, the replicas it alone would ask for, and which one wins."""
    spec, st = h["spec"], h.get("status", {})
    current = st.get("currentReplicas") or 0
    low, high = spec.get("minReplicas", 1), spec["maxReplicas"]
    metrics = []
    for resource, kind, target, now in aks_checks.hpa_targets(h):
        asks = None
        if kind == "Utilization" and isinstance(target, (int, float)) and target and isinstance(now, (int, float)) and current:
            ratio = now / target
            asks = current if abs(ratio - 1) <= HPA_TOLERANCE else math.ceil(current * ratio)
        metrics.append({"resource": resource, "type": kind, "target": target, "current": now, "asks": asks})
    asked = [m for m in metrics if m["asks"] is not None]
    top = max(asked, key=lambda m: m["asks"], default=None)
    conditions = [{k: c.get(k) for k in ("type", "status", "reason", "message", "lastTransitionTime")}
                  for c in st.get("conditions") or []]
    return {"name": h["metadata"]["name"], "min": low, "max": high, "current": current, "desired": st.get("desiredReplicas"),
            "metrics": metrics, "decides": top["resource"] if top else None,
            "wants": top["asks"] if top else None, "limited": bool(top and top["asks"] > high),
            "conditions": conditions, "behavior": spec.get("behavior") or {}, "last_scale": st.get("lastScaleTime")}


# ------------------------------------------------------------------ containers and pods

def _probe(p):
    if not p:
        return None
    how = ("HTTP " + str((p.get("httpGet") or {}).get("path", "/")) if "httpGet" in p else
           "TCP" if "tcpSocket" in p else "exec" if "exec" in p else "gRPC" if "grpc" in p else "?")
    return {"how": how, **{k: p.get(k) for k in ("initialDelaySeconds", "periodSeconds", "timeoutSeconds", "failureThreshold")}}


def _volumes(spec):
    out = {}
    for v in spec.get("volumes") or []:
        kind = next((k for k in v if k != "name"), "?")
        body = v.get(kind) or {}
        out[v["name"]] = {"kind": kind, "memory": kind == "emptyDir" and body.get("medium") == "Memory",
                          "size_limit": body.get("sizeLimit") if kind == "emptyDir" else None}
    return out


def _owned(pods, kind, name, replicasets):
    """The workload's own pods (a selector can match a neighbour's)."""
    owners = {name} if kind != "Deployment" else {
        r["metadata"]["name"] for r in replicasets
        if any(o.get("kind") == "Deployment" and o.get("name") == name for o in r["metadata"].get("ownerReferences") or [])}
    return [p for p in pods if any(o.get("name") in owners for o in p["metadata"].get("ownerReferences") or [])]


def _usage(pod_metrics, pods):
    """{container: [(cpu m, memory Mi)]} from metrics-server, for these pods."""
    names, out = {p["metadata"]["name"] for p in pods}, {}
    for m in pod_metrics:
        if m["metadata"]["name"] in names:
            for c in m.get("containers") or []:
                out.setdefault(c["name"], []).append((aks_checks.parse_cpu(c["usage"].get("cpu")),
                                                      aks_checks.parse_mem(c["usage"].get("memory"))))
    return out


def _share(used, of):
    return round(100 * used / of) if used is not None and of else None


def containers(spec, usage):
    volumes, out = _volumes(spec), []
    for c in aks_checks.containers_of(spec):
        res = c.get("resources") or {}
        req, lim = res.get("requests") or {}, res.get("limits") or {}
        values = usage.get(c["name"]) or []
        cpu = sum(v[0] or 0 for v in values) / len(values) if values else None
        mem = sum(v[1] or 0 for v in values) / len(values) if values else None
        cpu_req, cpu_lim = aks_checks.parse_cpu(req.get("cpu")), aks_checks.parse_cpu(lim.get("cpu"))
        mem_req, mem_lim = aks_checks.parse_mem(req.get("memory")), aks_checks.parse_mem(lim.get("memory"))
        flags = memory_flags(c)
        heap = heap_max_mib(flags, mem_lim)
        mounts = [{"path": m.get("mountPath"), **volumes.get(m.get("name"), {"kind": "?"})} for m in c.get("volumeMounts") or []]
        notes = []
        if heap and mem_req and heap > mem_req:
            notes.append(f"heap max {heap:.0f} MiB is above the memory request ({mem_req:.0f} MiB)")
        if heap and mem_lim and heap > 0.9 * mem_lim:
            notes.append(f"heap max {heap:.0f} MiB leaves little room under the limit ({mem_lim:.0f} MiB) for the rest of the process")
        if any(m.get("memory") for m in mounts):
            notes.append("an emptyDir in memory: what's written there counts as the pod's memory")
        out.append({"name": c["name"], "image": c.get("image"), "sidecar": c.get("restartPolicy") == "Always",
                    "cpu": {"request": cpu_req, "limit": cpu_lim, "used": cpu, "of_request": _share(cpu, cpu_req), "of_limit": _share(cpu, cpu_lim)},
                    "memory": {"request": mem_req, "limit": mem_lim, "used": mem, "of_request": _share(mem, mem_req), "of_limit": _share(mem, mem_lim)},
                    "probes": {k: _probe(c.get(f"{k}Probe")) for k in ("startup", "readiness", "liveness")},
                    "memory_flags": flags, "heap_max": heap, "mounts": mounts, "notes": notes})
    return out


def pod_rows(pods, nodes):
    zone = {n["metadata"]["name"]: (n["metadata"].get("labels") or {}).get("topology.kubernetes.io/zone", "") for n in nodes}
    pool = {n["metadata"]["name"]: aks_checks._pool_of(n) for n in nodes}
    rows = []
    for p in pods:
        st, node = p.get("status") or {}, (p.get("spec") or {}).get("nodeName") or ""
        statuses = st.get("containerStatuses") or []
        last = next(((s.get("lastState") or {}).get("terminated") for s in statuses if (s.get("lastState") or {}).get("terminated")), None)
        waiting = next(((s.get("state") or {}).get("waiting") for s in statuses if (s.get("state") or {}).get("waiting")), None)
        rows.append({"pod": p["metadata"]["name"], "node": node, "zone": zone.get(node, ""), "pool": pool.get(node, ""),
                     "phase": st.get("phase"), "ready": sum(1 for s in statuses if s.get("ready")), "containers": len(statuses),
                     "restarts": sum(s.get("restartCount") or 0 for s in statuses), "started": st.get("startTime"),
                     "waiting": (waiting or {}).get("reason"),
                     "last_exit": {"reason": last.get("reason"), "code": last.get("exitCode"), "at": last.get("finishedAt")} if last else None})
    rows.sort(key=lambda r: (r["ready"] == r["containers"], -r["restarts"], r["pod"]))
    count = lambda key: sorted(((v, sum(1 for r in rows if r[key] == v)) for v in {r[key] for r in rows}), key=lambda x: -x[1])  # noqa: E731
    return rows, {"nodes": count("node"), "zones": count("zone")}


def event_rows(events, name, own=()):
    """Events about the workload, its ReplicaSets and pods (its name plus generated suffixes, so a neighbour named
    `api-worker` isn't `api`'s) and the objects in `own` (its HPA, its pods), newest first."""
    generated = re.compile(rf"^{re.escape(name)}(-[a-z0-9]{{5,10}})?(-[a-z0-9]{{5}}|-[0-9]+)?$")
    mine = [e for e in events if generated.match(str((e.get("involvedObject") or {}).get("name", "")))
            or (e.get("involvedObject") or {}).get("name") in own]
    when = lambda e: e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp") or ""  # noqa: E731
    mine.sort(key=when, reverse=True)
    return [{"at": when(e), "type": e.get("type"), "reason": e.get("reason"), "count": e.get("count") or 1,
             "object": f"{(e.get('involvedObject') or {}).get('kind', '')}/{(e.get('involvedObject') or {}).get('name', '')}",
             "message": (e.get("message") or "")[:300]} for e in mine[:EVENTS_SHOWN]]


# ------------------------------------------------------------------ read

def _overview(kind, obj):
    md, spec, st = obj["metadata"], obj["spec"], obj.get("status") or {}
    template = spec["template"]
    rollout = next((c for c in st.get("conditions") or [] if c.get("type") == "Progressing"), {})
    return {"kind": kind, "namespace": md["namespace"], "name": md["name"], "created": md.get("creationTimestamp"),
            "revision": (md.get("annotations") or {}).get("deployment.kubernetes.io/revision"),
            "desired": st.get("desiredNumberScheduled") if kind == "DaemonSet" else spec.get("replicas", 1),
            "ready": st.get("numberReady") if kind == "DaemonSet" else st.get("readyReplicas", 0),
            "updated": st.get("updatedNumberScheduled") if kind == "DaemonSet" else st.get("updatedReplicas"),
            "strategy": spec.get("strategy") or spec.get("updateStrategy") or {},
            "rollout": {k: rollout.get(k) for k in ("reason", "message", "lastUpdateTime")} if rollout else {},
            "service_account": template["spec"].get("serviceAccountName") or "default",
            "node_selector": template["spec"].get("nodeSelector") or {},
            "images": [c.get("image") for c in aks_checks.containers_of(template["spec"])]}


def _selector(obj):
    labels = ((obj["spec"].get("selector") or {}).get("matchLabels") or {})
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def read_lists(k, lists):
    """({key: items}, {key: why}) for {key: (path, params)}, read in parallel; one list failing leaves the rest."""
    failed = {}

    def fetch(item):
        key, (path, params) = item
        try:
            return key, k.items(path, params)
        except Exception as error:  # a 403 or metrics-server down: said on the page, never fatal
            failed[key] = str(error).splitlines()[0][:200]
            return key, []

    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(fetch, lists.items())), failed


def read(client, cluster_id, namespace, kind, name, kube_settings=None):
    k, why = aks._connect(client, cluster_id, kube_settings)
    if k is None:
        return {"available": False, "reason": why}
    try:
        obj = k.get(f"/apis/apps/v1/namespaces/{namespace}/{kind.lower()}s/{name}")
    except kube.KubeUnavailable as error:
        return {"available": False, "reason": str(error)}
    if obj is None:
        return {"available": False, "reason": f"There's no {kind} {name} in {namespace} now."}
    selector = _selector(obj)
    lists = {"pods": (f"/api/v1/namespaces/{namespace}/pods", {"labelSelector": selector} if selector else None),
             "replicasets": (f"/apis/apps/v1/namespaces/{namespace}/replicasets", None),
             "hpas": (f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers", None),
             "pdbs": (f"/apis/policy/v1/namespaces/{namespace}/poddisruptionbudgets", None),
             "events": (f"/api/v1/namespaces/{namespace}/events", None),
             "pod_metrics": (f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods", {"labelSelector": selector} if selector else None),
             "nodes": ("/api/v1/nodes", None)}
    if kind != "Deployment":
        lists.pop("replicasets")
    data, failed = read_lists(k, lists)
    data.setdefault("replicasets", [])
    pods = _owned(data["pods"], kind, name, data["replicasets"])
    hpa = aks_checks.index_hpas(data["hpas"]).get((namespace, kind, name))
    checks = aks_checks.analyze({kind: [obj]}, pods, data["replicasets"], [], [hpa] if hpa else [], data["pdbs"], [],
                                data["pod_metrics"], [], cfg=aks_checks.Thresholds.from_settings(kube_settings), include_system=True)
    rows, spread = pod_rows(pods, data["nodes"])
    return {"available": True, "source": k.source, "overview": _overview(kind, obj),
            "hpa": hpa_decision(hpa) if hpa else None,
            "containers": containers(obj["spec"]["template"]["spec"], _usage(data["pod_metrics"], pods)),
            "usage_live": bool(data["pod_metrics"]), "pods": rows, "spread": spread,
            "events": event_rows(data["events"], name, {p["metadata"]["name"] for p in pods} | ({hpa["metadata"]["name"]} if hpa else set())),
            "findings": [f for f in checks["findings"] if f["severity"] != "INFO" or f["category"] == "hpa"],
            "yaml": {"workload": yaml_of(obj, kind), "hpa": yaml_of(hpa, "HorizontalPodAutoscaler")},
            "unreadable": {key: why for key, why in failed.items()}}


# ------------------------------------------------------------------ CPU and memory over time, runtime memory

def usage_ci(client, cluster_id, namespace, kind, name):
    """{found, containers: [{container, cpu, memory}], queries} from Container Insights' Perf table: per container,
    p95/p99/max over 7 days and the max over 30 (CPU in cores, memory in bytes)."""
    owner = kql_string(_owner_regex(kind, name))
    kql = (f"let pods = KubePodInventory | where TimeGenerated > ago(30d) and Namespace == {kql_string(namespace)}"
           f" and ControllerName matches regex {owner} | distinct PodUid;"
           " Perf | where TimeGenerated > ago(30d) and ObjectName == 'K8SContainer'"
           " and CounterName in ('cpuUsageNanoCores', 'memoryWorkingSetBytes')"
           " | extend parts = split(InstanceName, '/') | extend PodUid = tostring(parts[array_length(parts) - 2]),"
           " Container = tostring(parts[array_length(parts) - 1]) | where PodUid in (pods)"
           " | extend recent = TimeGenerated > ago(7d)"
           " | summarize p95 = percentile(CounterValue, 95), p99 = percentile(CounterValue, 99), peak = max(CounterValue)"
           " by Container, CounterName, recent")
    sent = [{"label": "Container Insights: CPU and memory per container, 30 days", "query": kql, "timespan": "P30D"}]
    try:
        rows = aks.resource_logs(client, cluster_id, kql, "P30D")
    except ArmError as error:
        return {"found": False, "reason": f"Container Insights couldn't be read: {appinsights.why(error)}", "queries": sent}
    out = {}
    for r in rows:
        scale = 1e9 if r["CounterName"] == "cpuUsageNanoCores" else 1
        what = out.setdefault(r["Container"], {"container": r["Container"], "cpu": {}, "memory": {}})
        blk = what["cpu" if scale != 1 else "memory"]
        blk["max_30d"] = max(blk.get("max_30d") or 0, (r["peak"] or 0) / scale)
        if r["recent"]:
            blk.update(p95=(r["p95"] or 0) / scale, p99=(r["p99"] or 0) / scale, max_7d=(r["peak"] or 0) / scale)
    return {"found": bool(out), "containers": sorted(out.values(), key=lambda c: c["container"]), "queries": sent,
            "reason": "" if out else "Container Insights has no CPU or memory rows for this workload in the last 30 days."}


def runtime_app(names, link, name):
    """(the tool's app for this workload, why): the saved link's app, else one whose words are the workload's."""
    if (link or {}).get("app"):
        return link["app"], "linked to this workload (Path → Link front doors)"
    noise = common_words(names)  # what most of the tool's app names carry (an environment suffix, say)
    wanted = tokens(name) - noise
    for c in app_candidates(names, name):
        if c["score"] >= 10 or (wanted and tokens(c["app"]) - noise == wanted):
            return c["app"], "same name as the workload, apart from words most of the tool's app names share"
    return "", ""


# What sizes a request: memory's 30-day max (it seldom falls back), CPU's 7-day p99 (its max is often start-up).
PEAK_OF = {"memory": ("max_30d", "30-day max"), "cpu": ("p99", "7-day p99")}


def needed_requests(sizing, hpa):
    """The pod request each HPA utilization target needs to be reachable at its peak (the HPA compares the pod's
    summed use with its summed requests): {resource: {peak, basis, target, need}}, CPU in cores, memory in bytes."""
    out = {}
    for m in (hpa or {}).get("metrics") or []:
        if m["type"] != "Utilization" or not isinstance(m["target"], (int, float)) or not m["target"] or m["resource"] not in PEAK_OF:
            continue
        field, basis = PEAK_OF[m["resource"]]
        peak = sum((c.get(m["resource"]) or {}).get(field) or 0 for c in sizing)
        if peak:
            out[m["resource"]] = {"peak": peak, "basis": basis, "target": m["target"], "need": peak / (m["target"] / 100)}
    return out


def _tool(call, label):
    try:
        return call(), ""
    except Exception as error:  # the tool's own error or unreachable: said on the page, never fatal
        return None, f"{label}: {str(error).splitlines()[0][:300]}"


def history(client, link, targets, source=None, can=(), app_names=None):
    """CPU and memory over 7 and 30 days (Container Insights, else the monitoring tool), the pod request each HPA target
    needs at the 30-day peak, and the app's runtime memory when the tool reports it. `targets` is {resource: %}."""
    label = getattr(source, "label", "") or "The monitoring tool"
    cluster, namespace, kind, name = link["cluster_id"], link["namespace"], link["kind"], link["name"]
    usage, notes = usage_ci(client, cluster, namespace, kind, name), []
    usage["source"] = "Container Insights"
    if not usage["found"] and source is not None and "workload usage" in can:
        notes.append(usage["reason"])
        found, why = _tool(lambda: source.workload_usage(cluster.rstrip("/").split("/")[-1], namespace, kind, name), label)
        if found:
            sent = [{"label": f"{label}, account {q['account']}", "query": q["nrql"], "timespan": ""} for q in found["queries"]]
            usage = {**found, "source": label, "queries": usage["queries"] + sent,
                     "reason": "" if found["found"] else f"{label} has no CPU or memory for this workload in the last 30 days."}
        else:
            usage["reason"] = why
    hpa = {"metrics": [{"resource": r, "type": "Utilization", "target": t} for r, t in (targets or {}).items()]}
    runtime = {"found": False, "reason": ""}
    if source is None or "runtime memory" not in can:
        runtime["reason"] = f"{label} can't give an app's runtime memory." if source is not None else \
            "No monitoring tool is picked in the Scope bar, so the app's heap can't be read."
    else:
        app, why = runtime_app(app_names() if app_names else [], link, name)
        if not app:
            runtime["reason"] = (f"No app in {label} has this workload's name; link one in Path → Link front doors.")
        else:
            found, error = _tool(lambda: source.runtime_memory(app), label)
            runtime = {"found": False, **(found or {}), "app": app, "why": why, "source": label,
                       "reason": error or ("" if (found or {}).get("found") else f"{label} reports no runtime memory for {app}"
                                           " (Java agents report heap; others may not).")}
            runtime["queries"] = [{"label": f"{label}, account {q['account']}", "query": q["nrql"], "timespan": ""}
                                  for q in (found or {}).get("queries") or []]
    return {"usage": usage, "needed": needed_requests(usage.get("containers") or [], hpa), "runtime": runtime,
            "notes": [n for n in notes if n]}
