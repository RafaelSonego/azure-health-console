"""AKS workloads in layers, each used only when the cluster has it: platform metrics (every
cluster), Container Insights (when it sends data), and the Kubernetes API (when reachable)."""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from ..arm import ArmError
from ..cache import TtlCache
from ..config import LOOK_BACK_HOURS
from ..health import METRICS_API_VERSION
from . import aks_checks, kube, workload_problems

LOGS_API_VERSION = "2018-03-01-preview"
UNREACHABLE_SECONDS = 120
K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
PROBLEM_PHASES = ("Failed", "Pending", "Unknown")


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def pod_phases(client, cluster_id):
    """Pods per namespace and phase from the platform metric every AKS cluster emits."""
    end = datetime.now(timezone.utc).replace(microsecond=0)
    payload = client.get(f"{cluster_id}/providers/Microsoft.Insights/metrics", METRICS_API_VERSION, params={
        "metricnames": "kube_pod_status_phase", "aggregation": "Average", "interval": "PT5M",
        "timespan": f"{_iso(end - timedelta(minutes=15))}/{_iso(end)}",
        "$filter": "phase eq '*' and namespace eq '*'", "top": "200",
    }, tolerate=(400, 404)) or {}
    rows = []
    for item in payload.get("value", []):
        for ts in item.get("timeseries") or []:
            meta = {m["name"]["value"]: m.get("value") for m in ts.get("metadatavalues") or []}
            values = [p.get("average") for p in ts.get("data") or [] if p.get("average") is not None]
            if values and values[-1]:
                rows.append({"namespace": meta.get("namespace"), "phase": meta.get("phase"), "pods": round(values[-1])})
    return sorted(rows, key=lambda r: (r["phase"] not in PROBLEM_PHASES, r["namespace"] or ""))


def failed_lowest(client, cluster_id, hours=24):
    """{namespace: fewest Failed pods in any hour of the last `hours`}. Failed pods stay until deleted, so pods past
    that floor failed within the window; an hour without data counts as none."""
    end = datetime.now(timezone.utc).replace(microsecond=0)
    payload = client.get(f"{cluster_id}/providers/Microsoft.Insights/metrics", METRICS_API_VERSION, params={
        "metricnames": "kube_pod_status_phase", "aggregation": "Average", "interval": "PT1H",
        "timespan": f"{_iso(end - timedelta(hours=hours))}/{_iso(end)}",
        "$filter": "phase eq 'Failed' and namespace eq '*'", "top": "200",
    }, tolerate=(400, 404)) or {}
    lowest = {}
    for item in payload.get("value", []):
        for ts in item.get("timeseries") or []:
            meta = {m["name"]["value"]: m.get("value") for m in ts.get("metadatavalues") or []}
            values = [p.get("average") or 0 for p in ts.get("data") or []]
            if values:
                lowest[meta.get("namespace")] = round(min(values))
    return lowest


def failed_split(client, cluster_id, phases, hours=24):
    """Each Failed row of `pod_phases` with `new`: how many failed within `hours` (all of them when the history
    can't be read, so a real failure is never hidden)."""
    rows = [dict(r) for r in phases if r["phase"] == "Failed"]
    try:
        lowest = failed_lowest(client, cluster_id, hours) if rows else {}
    except ArmError:
        lowest = {}
    for r in rows:
        r["new"] = max(0, r["pods"] - lowest.get(r["namespace"], 0))
    return rows


def resource_logs(client, resource_id, query, timespan="PT1H"):
    """Rows of a KQL query over the logs a resource sends (its workspace, read resource-centric)."""
    payload = client.post(f"{resource_id}/providers/microsoft.insights/logs", LOGS_API_VERSION,
                          body={"query": query, "timespan": timespan})
    tables = payload.get("tables") or payload.get("Tables") or []
    if not tables:
        return []
    table = tables[0]
    columns = [c.get("name") or c.get("ColumnName") for c in table.get("columns") or table.get("Columns") or []]
    return [dict(zip(columns, row)) for row in table.get("rows") or table.get("Rows") or []]


# Container Insights' look-back for restarts, OOMKilled and warning events: the workspace keeps 30 days by default.
CI_HOURS = LOOK_BACK_HOURS
_CI_LOOK_BACK = {"restarts": 1, "oomkilled": 24, "events": 1}  # hours, when no look-back is picked (aks.*_look_back_hours)


def look_back_defaults(kube_settings=None):
    """Each list's window on Look back's *default*, from Settings → AKS."""
    aks = kube_settings or {}
    return {"restarts": aks.get("restarts_look_back_hours", _CI_LOOK_BACK["restarts"]),
            "oomkilled": aks.get("oom_look_back_hours", _CI_LOOK_BACK["oomkilled"]),
            "events": aks.get("events_look_back_hours", _CI_LOOK_BACK["events"])}


_CI_QUERIES = {
    "not_running": """KubePodInventory | where TimeGenerated > ago(15m)
| summarize arg_max(TimeGenerated, PodStatus, Namespace, Name, ContainerStatusReason, ControllerKind, ControllerName) by PodUid
| where PodStatus !in ('Running', 'Succeeded')
| project Namespace, Pod=Name, Status=PodStatus, Reason=ContainerStatusReason, OwnerKind=ControllerKind, Owner=ControllerName | take {limit}""",
    "restarts": """KubePodInventory | where TimeGenerated > ago({hours}h) and isnotempty(ContainerName)
| summarize Restarts=max(ContainerRestartCount) - min(ContainerRestartCount), Total=max(ContainerRestartCount),
  OwnerKind=take_any(ControllerKind), Owner=take_any(ControllerName) by Namespace, Pod=Name, Container=tostring(split(ContainerName, '/')[1])
| where Restarts > 0 | top {limit} by Restarts""",
    "oomkilled": None,  # built by oom_query()
    "events": """KubeEvents | where TimeGenerated > ago({hours}h) and KubeEventType == 'Warning'
| summarize Count=sum(Count), Last=max(TimeGenerated), Message=take_any(Message) by Reason, Namespace, Object=Name, ObjectKind
| top {limit} by Count""",
    "hot_containers": """Perf | where TimeGenerated > ago(15m) and ObjectName == 'K8SContainer'
  and CounterName in ('cpuUsageNanoCores', 'cpuLimitNanoCores', 'memoryWorkingSetBytes', 'memoryLimitBytes')
| summarize cpu=maxif(CounterValue, CounterName == 'cpuUsageNanoCores'), cpuLimit=maxif(CounterValue, CounterName == 'cpuLimitNanoCores'),
  mem=maxif(CounterValue, CounterName == 'memoryWorkingSetBytes'), memLimit=maxif(CounterValue, CounterName == 'memoryLimitBytes') by InstanceName
| extend CpuPctOfLimit=round(100.0 * cpu / cpuLimit, 0), MemPctOfLimit=round(100.0 * mem / memLimit, 0)
| where CpuPctOfLimit > 80 or MemPctOfLimit > 85
| extend parts=split(InstanceName, '/')
| extend PodUid=tostring(parts[array_length(parts) - 2]), Container=tostring(parts[array_length(parts) - 1])
| join kind=leftouter (KubePodInventory | where TimeGenerated > ago(15m)
  | distinct PodUid, Namespace, Pod=Name, OwnerKind=ControllerKind, Owner=ControllerName) on PodUid
| summarize CpuPctOfLimit=max(CpuPctOfLimit), MemPctOfLimit=max(MemPctOfLimit) by Namespace, Pod, Container, OwnerKind, Owner
| top {limit} by MemPctOfLimit""",
}


# One row per kill, dated by the kill (a pod reports OOMKilled as its last state until it stops again), with what
# tells a container's own limit from a node out of memory: the kernel's line (AKS node-problem-detector), then Perf.
_OOM_QUERY = """let kills = KubePodInventory | where {inventory} and ContainerLastStatus contains 'OOMKilled'{pod}
| extend LastState=parse_json(ContainerLastStatus), Container=tostring(split(ContainerName, '/')[1])
| extend KilledAt=coalesce(todatetime(LastState.finishedAt), TimeGenerated)
| where {killed}
| summarize arg_max(TimeGenerated, Namespace, Name, Computer, ControllerKind, ControllerName) by PodUid, Container, KilledAt
| top {limit} by KilledAt desc
| project PodUid, Container, KilledAt, Namespace, Pod=Name, Node=Computer, OwnerKind=ControllerKind, Owner=ControllerName;
let t0 = toscalar(kills | summarize min(KilledAt)) - 10m;
let t1 = toscalar(kills | summarize max(KilledAt)) + 10m;
let mem = Perf | where TimeGenerated between (t0 .. t1) and ObjectName == 'K8SContainer'
  and CounterName in ('memoryWorkingSetBytes', 'memoryLimitBytes')
| extend parts=split(InstanceName, '/')
| extend PodUid=tostring(parts[array_length(parts) - 2]), Container=tostring(parts[array_length(parts) - 1])
| join kind=inner (kills | project PodUid, Container, KilledAt) on PodUid, Container
| where TimeGenerated between ((KilledAt - 5m) .. (KilledAt + 1m))
| summarize MemBytes=maxif(CounterValue, CounterName == 'memoryWorkingSetBytes'),
  LimitBytes=maxif(CounterValue, CounterName == 'memoryLimitBytes') by PodUid, Container, KilledAt;
let node = Perf | where TimeGenerated between (t0 .. t1) and ObjectName == 'K8SNode'
  and CounterName in ('memoryWorkingSetBytes', 'memoryAllocatableBytes')
| join kind=inner (kills | distinct Node, KilledAt) on $left.Computer == $right.Node
| where TimeGenerated between ((KilledAt - 5m) .. (KilledAt + 1m))
| summarize NodeMemBytes=maxif(CounterValue, CounterName == 'memoryWorkingSetBytes'),
  NodeAllocBytes=maxif(CounterValue, CounterName == 'memoryAllocatableBytes') by Node, KilledAt;
let kernel = KubeEvents | where TimeGenerated between (t0 .. t1 + 1h) and ObjectKind == 'Node'
  and Reason in ('OOMKilling', 'SystemOOM')
| extend Seen=coalesce(LastSeen, TimeGenerated)
| join kind=inner (kills | distinct Node, KilledAt) on $left.Name == $right.Node
| where Seen between ((KilledAt - 5m) .. (KilledAt + 5m))
| summarize arg_min(abs(Seen - KilledAt), Kernel=Message),
  SystemOom=countif(Reason == 'SystemOOM' or Message startswith 'Out of memory') by Node, KilledAt;
kills | join kind=leftouter mem on PodUid, Container, KilledAt
| join kind=leftouter node on Node, KilledAt
| join kind=leftouter kernel on Node, KilledAt
| project KilledAt, Namespace, Pod, Container, Node, OwnerKind, Owner, MemBytes, LimitBytes, NodeMemBytes,
  NodeAllocBytes, Kernel, SystemOom
| order by KilledAt desc"""
OOM_NEAR_LIMIT = 0.9  # of the container's limit, at the last reading before the kill
OOM_NODE_FULL = 0.95  # of the node's allocatable memory
OOM_BEFORE, OOM_AFTER = timedelta(minutes=30), timedelta(minutes=5)  # Investigate: kills this close to the request


def oom_query(hours=None, pod=None, around=None, limit=25):
    """The kills in the last `hours`, or (Investigate) one pod's kills just before and after `around`."""
    if around is None:
        inventory, killed = f"TimeGenerated > ago({hours}h)", f"KilledAt > ago({hours}h)"
    else:
        start, end = _iso(around - OOM_BEFORE), _iso(around + OOM_AFTER)
        inventory = f"TimeGenerated between (datetime({start}) .. datetime({_iso(around + timedelta(hours=6))}))"
        killed = f"KilledAt between (datetime({start}) .. datetime({end}))"
    if pod is not None and not K8S_NAME.match(pod):
        raise ValueError(f"'{pod}' isn't a Kubernetes pod name")
    return _OOM_QUERY.format(inventory=inventory, killed=killed, pod=f" and Name == {json.dumps(pod)}" if pod else "", limit=limit)


def _mib(value):
    return round(value / 1048576) if value else None


def oom_cause(row, near_limit=None, node_full=None):
    """(cause, sure, why) for one kill: 'container' (its own memory limit), 'node' (the node ran out) or 'unknown'."""
    kernel = row.get("Kernel") or ""
    if kernel.startswith("Memory cgroup out of memory"):
        return "container", True, "The kernel says the container's own memory limit was hit (Memory cgroup out of memory)."
    if kernel.startswith("Out of memory") or row.get("SystemOom"):
        return "node", True, "The kernel says the node itself ran out of memory, so it killed a process to free some."
    mem, limit = row.get("MemBytes"), row.get("LimitBytes")
    node_mem, node_alloc = row.get("NodeMemBytes"), row.get("NodeAllocBytes")
    # With no limit set, Container Insights reports the node's capacity as the limit.
    near_limit, full_at = near_limit or OOM_NEAR_LIMIT, node_full or OOM_NODE_FULL
    has_limit = bool(limit) and not (node_alloc and limit >= node_alloc)
    node_full = bool(node_mem and node_alloc and node_mem / node_alloc >= full_at)
    if has_limit and mem and mem / limit >= near_limit:
        return "container", False, (f"No kernel message was collected, but it used {_mib(mem)} of its {_mib(limit)} MiB "
                                    "limit just before the kill.")
    if node_full:
        return "node", False, (f"No kernel message was collected, but the node was at {round(100 * node_mem / node_alloc)}% "
                               "of its allocatable memory just before the kill.")
    if limit and not has_limit:
        return "node", False, ("It has no memory limit, so only the node running out of memory can kill it; no kernel "
                               "message was collected to confirm it.")
    if mem and has_limit:
        return "unknown", False, (f"It used {_mib(mem)} of its {_mib(limit)} MiB limit at the last reading (once a minute) "
                                  "and the node had room: a spike between readings can't be seen.")
    return "unknown", False, ("Neither the kernel's message nor memory readings for that minute were collected "
                              "(the workspace may not keep Perf or node events, or it was too long ago).")


def oom_thresholds(aks_settings=None):
    """(near the container's limit, node full) as fractions, from settings' aks.oom_*_percent."""
    aks = aks_settings or {}
    return aks.get("oom_near_limit_percent", 100 * OOM_NEAR_LIMIT) / 100, aks.get("oom_node_full_percent", 100 * OOM_NODE_FULL) / 100


def oom_rows(rows, thresholds=None):
    """The query's rows as the page shows them, each with its cause."""
    near, full = thresholds or (None, None)
    out = []
    for r in rows:
        cause, sure, why = oom_cause(r, near, full)
        limit, node_alloc = r.get("LimitBytes"), r.get("NodeAllocBytes")
        no_limit = bool(limit and node_alloc and limit >= node_alloc)
        out.append({"killed_at": r.get("KilledAt"), "namespace": r.get("Namespace") or "", "pod": r.get("Pod") or "",
                    "container": r.get("Container") or "", "node": r.get("Node") or "",
                    "owner": " ".join(filter(None, (r.get("OwnerKind"), r.get("Owner")))),
                    "workload": workload_problems.workload_of(r.get("OwnerKind"), r.get("Owner")),
                    "memory_mib": _mib(r.get("MemBytes")), "limit_mib": None if no_limit else _mib(limit),
                    "no_limit": no_limit,
                    "node_pct": round(100 * r["NodeMemBytes"] / node_alloc) if r.get("NodeMemBytes") and node_alloc else None,
                    "cause": cause, "sure": sure, "why": why, "kernel": r.get("Kernel") or ""})
    return out


def oom_kills(client, cluster_id, pod, around, aks_settings=None):
    """Investigate: the pod's OOM kills just before or after `around`, with their causes."""
    end = around + timedelta(hours=6)
    query = oom_query(pod=pod, around=around)
    rows = resource_logs(client, cluster_id, query, f"{_iso(around - OOM_BEFORE - timedelta(minutes=10))}/{_iso(end)}")
    return {"kills": oom_rows(rows, oom_thresholds(aks_settings)), "query": query}


def _timespan(hours):
    return f"P{hours // 24}D" if hours % 24 == 0 else f"PT{hours}H"


CI_LIMITS = {"not_running": 500, "restarts": 200, "events": 200, "hot_containers": 200, "oomkilled": 200}  # rows shown


def container_insights(client, cluster_id, hours=None, defaults=None, oom_thresholds=None):
    """`hours` (one of CI_HOURS) is how far back restarts, OOMKilled and warning events look, else each its own
    (`defaults`, look_back_defaults); the rest stay "now"."""
    defaults = defaults or _CI_LOOK_BACK
    try:
        alive = resource_logs(client, cluster_id, "KubePodInventory | where TimeGenerated > ago(30m) | take 1", "PT1H")
    except ArmError as error:
        return {"available": False, "reason": f"Log query failed: {str(error)[:200]}"}
    if not alive:
        return {"available": False,
                "reason": "Container Insights isn't sending for this cluster: nothing arrived in the last 30 minutes (not turned on, "
                          "or its agent is down), so its lists are skipped."}

    def run(item):
        name, query = item
        back, limit = hours or defaults.get(name, 1), CI_LIMITS[name]
        try:
            if name == "oomkilled":
                rows = oom_rows(resource_logs(client, cluster_id, oom_query(back, limit=limit + 1), _timespan(max(back, 24))),
                                oom_thresholds)
            else:
                rows = resource_logs(client, cluster_id, query.replace("{hours}", str(back)).replace("{limit}", str(limit + 1)),
                                     _timespan(max(back, 24)))
            if len(rows) > limit:  # one more than shown came back: say so, never cut silently
                cut[name] = limit
            return name, rows[:limit]
        except ArmError as error:
            return name, {"error": str(error)[:200]}

    cut = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = dict(pool.map(run, _CI_QUERIES.items()))
    results["truncated"] = cut
    return {"available": True, "hours": {name: hours or defaults[name] for name in _CI_LOOK_BACK}, **results}


LISTS = {
    "deployments": "/apis/apps/v1/deployments", "statefulsets": "/apis/apps/v1/statefulsets",
    "daemonsets": "/apis/apps/v1/daemonsets", "cronjobs": "/apis/batch/v1/cronjobs",
    "replicasets": "/apis/apps/v1/replicasets", "jobs": "/apis/batch/v1/jobs",
    "pods": "/api/v1/pods", "nodes": "/api/v1/nodes",
    "hpas": "/apis/autoscaling/v2/horizontalpodautoscalers", "pdbs": "/apis/policy/v1/poddisruptionbudgets",
    "pod_metrics": "/apis/metrics.k8s.io/v1beta1/pods", "node_metrics": "/apis/metrics.k8s.io/v1beta1/nodes",
}


# A cluster with no network path costs a connect timeout per try: remembered for a while, so reopening it is instant.
_unreachable = TtlCache("unreachable clusters", UNREACHABLE_SECONDS)


def _connect(client, cluster_id, kube_settings):
    """(Kubernetes client, None) or (None, why it can't be used): the role, the network path or the credential."""
    key = cluster_id.lower()
    known = None if getattr(client, "is_fresh", False) else _unreachable.get(key)
    if known:
        return None, known
    try:
        k = kube.client_for(client, cluster_id, kube_settings)
        k.get("/version")
        return k, None
    except kube.KubeUnreachable as error:
        _unreachable.put(key, str(error))
        return None, str(error)
    except (kube.KubeUnavailable, ArmError) as error:
        return None, str(error)


def read_lists(k, names):
    """({name: items}, {name: why it failed}) for the lists in `LISTS`, in parallel; a list the identity can't read
    is reported, never mistaken for an empty one (Workloads and Nodes both read through this)."""
    failed = {}

    def fetch(name):
        try:
            return name, k.items(LISTS[name])
        except kube.KubeUnavailable as error:  # usually a 403: this list is outside the identity's role
            failed[name] = str(error)
            return name, []
        except Exception as error:  # one list failing shouldn't sink the rest
            failed[name] = str(error).splitlines()[0][:200]
            return name, []

    with ThreadPoolExecutor(max_workers=6) as pool:
        data = dict(pool.map(fetch, names))
    return data, failed


API_EVENT_LIMIT = 200  # warning events listed (one row per reason and object); the rest are counted


def api_warning_events(items, hours, now=None):
    """The cluster's Warning events from the Kubernetes API, one row per reason and object, shaped like Container
    Insights' (Reason, Namespace, Object, ObjectKind, Count, Last, Message), newest Last within `hours`, most first.
    Kubernetes itself keeps events about an hour, so a longer look-back still shows only what it kept."""
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    rows = {}
    for e in items:
        if e.get("type") != "Warning":
            continue
        obj, series = e.get("involvedObject") or {}, e.get("series") or {}
        last = workload_problems._parse(e.get("lastTimestamp") or series.get("lastObservedTime") or e.get("eventTime")
                                        or (e.get("metadata") or {}).get("creationTimestamp"))
        if last is None or last < since:
            continue
        key = (e.get("reason") or "Warning", obj.get("namespace") or "", obj.get("name") or "", obj.get("kind") or "")
        row = rows.setdefault(key, {"Reason": key[0], "Namespace": key[1], "Object": key[2], "ObjectKind": key[3],
                                    "Count": 0, "Last": last, "Message": ""})
        row["Count"] += int(e.get("count") or series.get("count") or 1)
        if last >= row["Last"]:
            row["Last"], row["Message"] = last, (e.get("message") or "")[:500]
    out = sorted(rows.values(), key=lambda r: (-r["Count"], r["Reason"], r["Object"]))
    return [{**r, "Last": _iso(r["Last"])} for r in out]


def _read_warning_events(k):
    """(items, why they couldn't be read or None)."""
    try:
        return k.items("/api/v1/events", {"fieldSelector": "type=Warning"}), None
    except kube.KubeUnavailable as error:
        return [], str(error)
    except Exception as error:  # the events read failing mustn't sink the lists
        return [], str(error).splitlines()[0][:200]


def kubernetes(client, cluster_id, include_system=False, kube_settings=None):
    return _kubernetes(client, cluster_id, include_system, kube_settings)[0]


def _kubernetes(client, cluster_id, include_system=False, kube_settings=None, events_hours=1):
    """(result, {(namespace, pod): (namespace, kind, name)}): the owner index stays server side, for `problems`."""
    k, why = _connect(client, cluster_id, kube_settings)
    if k is None:
        return {"available": False, "reason": why}, {}
    with ThreadPoolExecutor(max_workers=2) as side:  # the pools' autoscaler ranges and the events, read while the lists are
        profiles = side.submit(pool_profiles, client, cluster_id)
        warnings = side.submit(_read_warning_events, k)
        data, failed = read_lists(k, list(LISTS))
    if len(failed) == len(LISTS):
        return {"available": False, "reason": next(iter(failed.values()))}, {}
    objs = {"Deployment": data["deployments"], "StatefulSet": data["statefulsets"],
            "DaemonSet": data["daemonsets"], "CronJob": data["cronjobs"]}
    aks_settings = kube_settings or {}
    result = aks_checks.analyze(objs, data["pods"], data["replicasets"], data["jobs"], data["hpas"],
                                data["pdbs"], data["nodes"], data["pod_metrics"], data["node_metrics"],
                                cfg=aks_checks.Thresholds.from_settings(aks_settings), include_system=include_system,
                                profiles=profiles.result())
    pod_owner = aks_checks.map_pods(data["pods"], data["replicasets"], data["jobs"])[0]
    system = {s["namespace"] for s in result.get("system_namespaces", [])}
    in_scope = (lambda ns: True) if include_system else (lambda ns: ns not in system)
    rolled = workload_problems.rollouts(objs, data["replicasets"], in_scope, datetime.now(timezone.utc),
                                        aks_settings.get("rollout_hours", 24))
    leftover = timedelta(hours=aks_settings.get("leftover_hours", 24))
    for row in result.get("pod_rows", []):
        row["leftover"] = workload_problems.is_leftover(row, datetime.now(timezone.utc), leftover)
    result.get("pod_rows", []).sort(key=lambda r: r["leftover"])  # stable: left over last, the rest as they were
    event_items, events_error = warnings.result()
    events = api_warning_events(event_items, events_hours)
    return {"available": True, "server": k.server, "source": k.source, **result, "workload_rows": result.get("workloads", []),
            "workloads": workload_names(objs), "rollouts": [{"namespace": ns, "workload": w, **r} for (ns, w), r in rolled.items()],
            "unreadable": unreadable_notes(failed), "events": events[:API_EVENT_LIMIT], "events_total": len(events),
            "events_hours": events_hours, "events_error": events_error}, pod_owner


NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
WORKLOAD_KINDS = (("Deployment", "deployments"), ("StatefulSet", "statefulsets"), ("DaemonSet", "daemonsets"))
_POD_TEMPLATE_HASH = workload_problems.POD_TEMPLATE_HASH


def namespace_workloads(client, cluster_id, namespace, kube_settings=None):
    """One namespace's Deployments, StatefulSets and DaemonSets, for when the cluster-wide lists can't be read: that
    namespace's own lists (a role on just that namespace is enough), else Container Insights' pod inventory over the
    last hour. {"source", "workloads": [{kind, name}], "notes"}: no source and an empty list when neither works."""
    notes = []
    k, why = _connect(client, cluster_id, kube_settings)
    if k is not None:
        found, failed = [], []
        for kind, plural in WORKLOAD_KINDS:
            try:
                found += [{"kind": kind, "name": o["metadata"].get("name", "")}
                          for o in k.items(f"/apis/apps/v1/namespaces/{namespace}/{plural}")]
            except kube.KubeUnavailable as error:
                failed.append(str(error).splitlines()[0][:200])
        if len(failed) < len(WORKLOAD_KINDS):
            return {"source": "Kubernetes API", "workloads": _by_kind(found), "notes": failed}
        notes.append(failed[0])
    else:
        notes.append((why or "The Kubernetes API can't be reached.").splitlines()[0][:200])
    query = (f"KubePodInventory | where TimeGenerated > ago(1h) and Namespace == '{namespace}'"
             " | distinct ControllerKind, ControllerName")
    try:
        rows = resource_logs(client, cluster_id, query, "PT1H")
    except ArmError as error:
        rows = []
        notes.append(f"Container Insights couldn't be read: {str(error).splitlines()[0][:150]}")
    found = []
    for r in rows:
        kind, name = r.get("ControllerKind") or "", r.get("ControllerName") or ""
        if kind == "ReplicaSet":  # a Deployment's pods belong to its ReplicaSet: <deployment>-<pod template hash>
            kind, name = "Deployment", _POD_TEMPLATE_HASH.sub("", name)
        if kind in dict(WORKLOAD_KINDS) and name:
            found.append({"kind": kind, "name": name})
    if found:
        return {"source": "Container Insights", "workloads": _by_kind(found), "notes": notes}
    return {"source": "", "workloads": [], "notes": notes}


def _by_kind(items):
    order = [kind for kind, _ in WORKLOAD_KINDS]
    unique = {(w["kind"], w["name"]) for w in items if w["name"]}
    return [{"kind": kind, "name": name} for kind, name in sorted(unique, key=lambda w: (order.index(w[0]), w[1]))]


def workload_names(objs):
    """Every Deployment, StatefulSet and DaemonSet by namespace, for the Dependencies picker (system ones too)."""
    return sorted(({"namespace": o["metadata"].get("namespace", ""), "kind": kind, "name": o["metadata"].get("name", "")}
                   for kind in ("Deployment", "StatefulSet", "DaemonSet") for o in objs[kind]),
                  key=lambda w: (w["namespace"], w["kind"], w["name"]))


# What each list feeds, so a list the identity can't read is reported, never mistaken for "no problems".
_LIST_EFFECTS = {
    "nodes": "node pools, NotReady nodes and the Pending-pod and capacity checks are missing",
    "node_metrics": "node usage is missing from the node pool table",
    "pod_metrics": "live CPU and memory usage per container is missing, so the near-limit checks can't run",
    "pods": "restarts, OOMKilled and Pending pods are missing",
    "hpas": "HPA checks are missing",
    "pdbs": "PodDisruptionBudget checks are missing",
    "replicasets": "pods can't be matched to their Deployments",
    "jobs": "pods can't be matched to their CronJobs",
    "deployments": "Deployments aren't checked", "statefulsets": "StatefulSets aren't checked",
    "daemonsets": "DaemonSets aren't checked", "cronjobs": "CronJobs aren't checked",
}


def unreadable_notes(failed):
    notes = []
    for name, reason in sorted(failed.items()):
        lines = [line for line in reason.splitlines() if line.strip()]
        why = " ".join(line for line in lines if line.startswith(("No access", "Required"))) or (lines[0] if lines else "")
        notes.append({"list": name, "effect": _LIST_EFFECTS.get(name, ""), "reason": why})
    return notes


_CI_NODES = """KubeNodeInventory | where TimeGenerated > ago(30m)
| summarize arg_max(TimeGenerated, *) by Computer
| project Computer, Status, KubeletVersion, KubernetesProviderID, CreationTimeStamp, Labels, LastSeen=TimeGenerated
| order by Computer asc"""


def nodes(client, cluster_id, kube_settings=None):
    """The cluster's nodes one by one, and its pools in total: from the Kubernetes API (nodes, the pods placed on
    them, metrics-server) when it can be reached, else from Container Insights' node inventory (less detail)."""
    k, why = _connect(client, cluster_id, kube_settings)
    if k is not None:
        with ThreadPoolExecutor(max_workers=1) as side:
            profiles = side.submit(pool_profiles, client, cluster_id)
            data, failed = read_lists(k, ["nodes", "pods", "node_metrics"])
        if "nodes" not in failed:
            aks_settings = kube_settings or {}
            pools, _findings = aks_checks.capacity(data["nodes"], data["pods"], data["node_metrics"],
                                                   aks_checks.Thresholds.from_settings(aks_settings), profiles.result())
            return {"available": True, "source": "kubernetes", "via": k.source,
                    "nodes": aks_checks.node_rows(data["nodes"], data["pods"], data["node_metrics"],
                                                  warn_days=aks_settings.get("node_image_warning_days")),
                    "pools": pools, "pool_requests_warn": aks_checks.Thresholds.from_settings(aks_settings).pool_requests,
                    "unreadable": unreadable_notes(failed)}
        why = failed["nodes"]
    try:
        rows = resource_logs(client, cluster_id, _CI_NODES, "PT1H")
    except ArmError as error:
        return {"available": False, "reason": why, "fallback": f"Container Insights couldn't be read either: {str(error)[:200]}"}
    if not rows:
        return {"available": False, "reason": why,
                "fallback": "Container Insights has no node data from the last 30 minutes (not enabled, or not sending)."}
    out = []
    for r in rows:
        try:
            labels = json.loads(r.get("Labels") or "[]")
            labels = labels[0] if isinstance(labels, list) and labels else labels if isinstance(labels, dict) else {}
        except ValueError:
            labels = {}
        ready = (r.get("Status") or "").lower() == "ready"
        out.append({"name": r.get("Computer") or "", "status": "healthy" if ready else "critical", "ready": ready,
                    "pool": labels.get("kubernetes.azure.com/agentpool") or labels.get("agentpool") or "",
                    "vm_size": labels.get("node.kubernetes.io/instance-type") or "", "mode": labels.get("kubernetes.azure.com/mode") or "",
                    "zone": labels.get("topology.kubernetes.io/zone") or "", "kubelet": r.get("KubeletVersion") or "",
                    "problems": [] if ready else [f"status {r.get('Status') or 'unknown'}"], "last_seen": r.get("LastSeen")})
    return {"available": True, "source": "container_insights", "reason": why, "nodes": out, "pools": []}


def pool_profiles(client, cluster_id):
    """{pool: {autoscale, min, max, count, vm_size, mode}} from the cluster's ARM record, {} when it can't be read."""
    try:
        rows = client.query(f"Resources | where tolower(id) == {json.dumps(cluster_id.lower())} | project properties")
    except Exception:  # an extra beside the Kubernetes lists: never the reason they don't show
        return {}
    pools = ((rows[0].get("properties") or {}).get("agentPoolProfiles") or []) if rows else []
    return {p.get("name"): {"autoscale": bool(p.get("enableAutoScaling")), "min": p.get("minCount"), "max": p.get("maxCount"),
                            "count": p.get("count"), "vm_size": p.get("vmSize"), "mode": p.get("mode") or ""} for p in pools}


def workloads(client, cluster_id, include_system=False, kube_settings=None, ci_hours=None):
    """All three layers in parallel; each reports whether it was available and why not, and how long it took
    (`timings`, seconds), then one list of what's wrong now across them (`problems`)."""
    def timed(work, *args):
        start = time.monotonic()
        try:
            return work(*args), round(time.monotonic() - start, 1)
        except ArmError as error:
            return {"error": str(error)[:200]}, round(time.monotonic() - start, 1)

    with ThreadPoolExecutor(max_workers=3) as pool:
        phases = pool.submit(timed, pod_phases, client, cluster_id)
        insights = pool.submit(timed, container_insights, client, cluster_id, ci_hours, look_back_defaults(kube_settings),
                               oom_thresholds(kube_settings))
        api = pool.submit(timed, _kubernetes, client, cluster_id, include_system, kube_settings,
                          ci_hours or look_back_defaults(kube_settings)["events"])
        (phase_rows, t_phases), (ci, t_ci), ((k, pod_owner), t_k) = phases.result(), insights.result(), api.result()
    return {"pod_phases": phase_rows, "container_insights": ci, "kubernetes": k,
            "problems": workload_problems.problems(phase_rows, ci, k, pod_owner, include_system=include_system,
                                                   leftover_hours=(kube_settings or {}).get("leftover_hours", 24)),
            "timings": {workload_problems.PLATFORM: t_phases, workload_problems.CI: t_ci, workload_problems.K8S: t_k},
            "look_back_defaults": look_back_defaults(kube_settings), "leftover_hours": (kube_settings or {}).get("leftover_hours", 24)}


def _window_clause(around, spread):
    """(KQL time filter, timespan, what to call the window): the last 30 minutes, or `spread` minutes either side of
    `around`."""
    if around is None:
        return "TimeGenerated > ago(30m)", "PT1H", "in the last 30 minutes"
    start, end = around - timedelta(minutes=spread), around + timedelta(minutes=spread)
    return (f"TimeGenerated between (datetime({_iso(start)}) .. datetime({_iso(end)}))", f"{_iso(start)}/{_iso(end)}",
            f"within {spread} minutes of {_iso(around)}")


def _kube_lines(k, namespace, pod, container, lines, around, spread, previous=False):
    """The pod's own log from the API server; with `around`, the lines time-stamped in that window (the API can only
    start at a time, so the end is cut here). `previous`: the stopped instance's, none when it's gone."""
    params = {"container": container} if container else {}
    if previous:
        params["previous"] = "true"
    if around is None:
        params["tailLines"] = str(lines)
    else:
        params.update(sinceTime=_iso(around - timedelta(minutes=spread)), timestamps="true", limitBytes=str(KUBE_LOG_BYTES))
    try:
        text = k.get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", params=params, raw=True)
    except requests.HTTPError:
        if not previous:
            raise
        text = ""  # 400: no stopped instance is kept for this container any more
    found = (text or "").splitlines()
    if around is None:
        return found[-lines:]
    end = _iso(around + timedelta(minutes=spread))[:19]
    return [line for line in found if line[:19] <= end][:lines]


KUBE_LOG_BYTES = 2_000_000


def logs(client, cluster_id, namespace, pod=None, container=None, lines=200, source="auto", kube_settings=None,
         around=None, spread=2, previous=False):
    """Opt-in container logs: the Kubernetes API when reachable, else Container Insights. With `around` (a datetime),
    only the lines within `spread` minutes of it; Container Insights answers when the API has none left for then.
    `previous` reads the container's last stopped instance (the one an OOM kill or crash ended)."""
    for value in (namespace, pod, container):
        if value is not None and not K8S_NAME.match(value):
            return {"error": f"'{value}' isn't a valid Kubernetes name."}
    lines = max(10, min(int(lines), 1000))
    when, timespan, window = _window_clause(around, spread)
    if source in ("auto", "kubernetes") and pod:
        try:
            k = kube.client_for(client, cluster_id, kube_settings)
            found = _kube_lines(k, namespace, pod, container, lines, around, spread, previous)
            if found or (around is None and not previous) or source == "kubernetes":
                return {"source": "kubernetes" + (", the stopped instance" if previous else ""), "lines": found}
        except (kube.KubeUnavailable, ArmError) as error:
            if source == "kubernetes":
                return {"source": "kubernetes", "error": str(error)}
    where = [f"PodNamespace == {json.dumps(namespace)}"]
    if pod:
        where.append(f"PodName == {json.dumps(pod)}")
    if container:
        where.append(f"ContainerName == {json.dumps(container)}")
    v2 = (f"ContainerLogV2 | where {when} and {' and '.join(where)}"
          f" | project TimeGenerated, Pod=PodName, Container=ContainerName, Message=tostring(LogMessage)"
          f" | order by TimeGenerated desc | take {lines}")
    # Clusters on the legacy schema write ContainerLog, which only has a container ID.
    inventory = [f"Namespace == {json.dumps(namespace)}"]
    if pod:
        inventory.append(f"Name == {json.dumps(pod)}")
    legacy = (f"let ids = KubePodInventory | where {when} and {' and '.join(inventory)}"
              f" | distinct ContainerID, Pod=Name, Container=tostring(split(ContainerName, '/')[1]);"
              f" ContainerLog | where {when} | join kind=inner ids on ContainerID"
              + (f" | where Container == {json.dumps(container)}" if container else "")
              + f" | project TimeGenerated, Pod, Container, Message=LogEntry | order by TimeGenerated desc | take {lines}")
    try:
        rows = resource_logs(client, cluster_id, v2, timespan) or resource_logs(client, cluster_id, legacy, timespan)
    except ArmError as error:
        return {"source": "container insights", "error": str(error)[:300]}
    if not rows:
        return {"source": "container insights", "lines": [],
                "error": f"No log lines {window}. Container Insights may exclude this namespace."}
    return {"source": "container insights",
            "lines": [f"{r.get('TimeGenerated')} {r.get('Pod')}/{r.get('Container')}: {r.get('Message')}"
                      for r in reversed(rows)]}
