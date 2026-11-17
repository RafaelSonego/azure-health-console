"""AKS workloads in layers, each used only when the cluster has it: platform metrics (every
cluster), Container Insights (when it sends data), and the Kubernetes API (when reachable)."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..arm import ArmError
from ..cache import TtlCache
from ..health import METRICS_API_VERSION
from . import aks_checks, kube

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


_CI_QUERIES = {
    "not_running": """KubePodInventory | where TimeGenerated > ago(15m)
| summarize arg_max(TimeGenerated, PodStatus, Namespace, Name, ContainerStatusReason) by PodUid
| where PodStatus !in ('Running', 'Succeeded')
| project Namespace, Pod=Name, Status=PodStatus, Reason=ContainerStatusReason | take 50""",
    "restarts": """KubePodInventory | where TimeGenerated > ago(1h) and isnotempty(ContainerName)
| summarize Restarts=max(ContainerRestartCount) - min(ContainerRestartCount), Total=max(ContainerRestartCount)
  by Namespace, Pod=Name, Container=tostring(split(ContainerName, '/')[1])
| where Restarts > 0 | top 25 by Restarts""",
    "oomkilled": """KubePodInventory | where TimeGenerated > ago(24h) and ContainerLastStatus contains 'OOMKilled'
| summarize Pods=dcount(Name), Last=max(TimeGenerated) by Namespace, Container=tostring(split(ContainerName, '/')[1])
| top 25 by Pods""",
    "events": """KubeEvents | where TimeGenerated > ago(1h) and KubeEventType == 'Warning'
| summarize Count=sum(Count), Last=max(TimeGenerated), Message=take_any(Message) by Reason, Namespace, Object=Name
| top 25 by Count""",
    "hot_containers": """Perf | where TimeGenerated > ago(15m) and ObjectName == 'K8SContainer'
  and CounterName in ('cpuUsageNanoCores', 'cpuLimitNanoCores', 'memoryWorkingSetBytes', 'memoryLimitBytes')
| summarize cpu=maxif(CounterValue, CounterName == 'cpuUsageNanoCores'), cpuLimit=maxif(CounterValue, CounterName == 'cpuLimitNanoCores'),
  mem=maxif(CounterValue, CounterName == 'memoryWorkingSetBytes'), memLimit=maxif(CounterValue, CounterName == 'memoryLimitBytes') by InstanceName
| extend CpuPctOfLimit=round(100.0 * cpu / cpuLimit, 0), MemPctOfLimit=round(100.0 * mem / memLimit, 0)
| where CpuPctOfLimit > 80 or MemPctOfLimit > 85
| extend parts=split(InstanceName, '/')
| extend PodUid=tostring(parts[array_length(parts) - 2]), Container=tostring(parts[array_length(parts) - 1])
| join kind=leftouter (KubePodInventory | where TimeGenerated > ago(15m) | distinct PodUid, Namespace, Pod=Name) on PodUid
| summarize CpuPctOfLimit=max(CpuPctOfLimit), MemPctOfLimit=max(MemPctOfLimit) by Namespace, Pod, Container
| top 25 by MemPctOfLimit""",
}


def container_insights(client, cluster_id):
    try:
        alive = resource_logs(client, cluster_id, "KubePodInventory | where TimeGenerated > ago(30m) | take 1", "PT1H")
    except ArmError as error:
        return {"available": False, "reason": f"Log query failed: {str(error)[:200]}"}
    if not alive:
        return {"available": False,
                "reason": "No Container Insights data in the last 30 minutes (not enabled, or not sending)."}

    def run(item):
        name, query = item
        try:
            return name, resource_logs(client, cluster_id, query, "P1D")
        except ArmError as error:
            return name, {"error": str(error)[:200]}

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = dict(pool.map(run, _CI_QUERIES.items()))
    return {"available": True, **results}


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


def kubernetes(client, cluster_id, include_system=False, kube_settings=None):
    k, why = _connect(client, cluster_id, kube_settings)
    if k is None:
        return {"available": False, "reason": why}
    data, failed = read_lists(k, list(LISTS))
    if len(failed) == len(LISTS):
        return {"available": False, "reason": next(iter(failed.values()))}
    objs = {"Deployment": data["deployments"], "StatefulSet": data["statefulsets"],
            "DaemonSet": data["daemonsets"], "CronJob": data["cronjobs"]}
    result = aks_checks.analyze(objs, data["pods"], data["replicasets"], data["jobs"], data["hpas"],
                                data["pdbs"], data["nodes"], data["pod_metrics"], data["node_metrics"],
                                include_system=include_system)
    return {"available": True, "server": k.server, "source": k.source, **result, "unreadable": unreadable_notes(failed)}


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
        data, failed = read_lists(k, ["nodes", "pods", "node_metrics"])
        if "nodes" not in failed:
            pools, _findings = aks_checks.capacity(data["nodes"], data["pods"], data["node_metrics"], aks_checks.Thresholds())
            return {"available": True, "source": "kubernetes", "via": k.source,
                    "nodes": aks_checks.node_rows(data["nodes"], data["pods"], data["node_metrics"]),
                    "pools": pools, "unreadable": unreadable_notes(failed)}
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
                    "vm_size": labels.get("node.kubernetes.io/instance-type") or "",
                    "zone": labels.get("topology.kubernetes.io/zone") or "", "kubelet": r.get("KubeletVersion") or "",
                    "problems": [] if ready else [f"status {r.get('Status') or 'unknown'}"], "last_seen": r.get("LastSeen")})
    return {"available": True, "source": "container_insights", "reason": why, "nodes": out, "pools": []}


def workloads(client, cluster_id, include_system=False, kube_settings=None):
    """All three layers in parallel; each reports whether it was available and why not."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        phases = pool.submit(pod_phases, client, cluster_id)
        insights = pool.submit(container_insights, client, cluster_id)
        api = pool.submit(kubernetes, client, cluster_id, include_system, kube_settings)
        try:
            phase_rows = phases.result()
        except ArmError as error:
            phase_rows = {"error": str(error)[:200]}
        return {"pod_phases": phase_rows, "container_insights": insights.result(), "kubernetes": api.result()}


def _window_clause(around, spread):
    """(KQL time filter, timespan, what to call the window): the last 30 minutes, or `spread` minutes either side of
    `around`."""
    if around is None:
        return "TimeGenerated > ago(30m)", "PT1H", "in the last 30 minutes"
    start, end = around - timedelta(minutes=spread), around + timedelta(minutes=spread)
    return (f"TimeGenerated between (datetime({_iso(start)}) .. datetime({_iso(end)}))", f"{_iso(start)}/{_iso(end)}",
            f"within {spread} minutes of {_iso(around)}")


def _kube_lines(k, namespace, pod, container, lines, around, spread):
    """The pod's own log from the API server; with `around`, the lines time-stamped in that window (the API can only
    start at a time, so the end is cut here)."""
    params = {"container": container} if container else {}
    if around is None:
        params["tailLines"] = str(lines)
    else:
        params.update(sinceTime=_iso(around - timedelta(minutes=spread)), timestamps="true", limitBytes=str(KUBE_LOG_BYTES))
    text = k.get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", params=params, raw=True)
    found = (text or "").splitlines()
    if around is None:
        return found[-lines:]
    end = _iso(around + timedelta(minutes=spread))[:19]
    return [line for line in found if line[:19] <= end][:lines]


KUBE_LOG_BYTES = 2_000_000


def logs(client, cluster_id, namespace, pod=None, container=None, lines=200, source="auto", kube_settings=None,
         around=None, spread=2):
    """Opt-in container logs: the Kubernetes API when reachable, else Container Insights. With `around` (a datetime),
    only the lines within `spread` minutes of it; Container Insights answers when the API has none left for then."""
    for value in (namespace, pod, container):
        if value is not None and not K8S_NAME.match(value):
            return {"error": f"'{value}' isn't a valid Kubernetes name."}
    lines = max(10, min(int(lines), 1000))
    when, timespan, window = _window_clause(around, spread)
    if source in ("auto", "kubernetes") and pod:
        try:
            k = kube.client_for(client, cluster_id, kube_settings)
            found = _kube_lines(k, namespace, pod, container, lines, around, spread)
            if found or around is None or source == "kubernetes":
                return {"source": "kubernetes", "lines": found}
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
