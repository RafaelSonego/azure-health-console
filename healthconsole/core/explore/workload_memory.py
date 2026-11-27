"""A workload's memory over the last 7 days, pod by pod, and a cluster's week: facts to read a memory problem from,
never a verdict. Each pod's working set per hour with its restarts and OOM kills, how fast it grew since it started,
and the 24 hours before the last rollout against the 24 after. Container Insights first, else the monitoring tool."""
from datetime import datetime, timedelta

from ..arm import ArmError
from ..investigate.appinsights import kql_string
from . import aks, appinsights
from .workload_names import owner_regex

WEEK = "P7D"
CHART_PODS = 40  # pods drawn on the chart; every pod is in the table
MIN_POINTS = 6  # hours a pod needs before its growth per hour is worth a number
WINDOW_HOURS = 24  # each side of the last rollout
MIB = 2**20


def _ci_series_kql(namespace, kind, name):
    owner = kql_string(owner_regex(kind, name))
    pods = (f"KubePodInventory | where TimeGenerated > ago(7d) and Namespace == {kql_string(namespace)}"
            f" and ControllerName matches regex {owner}")
    # A container's samples averaged per hour, then a pod's containers added up: never raw samples summed.
    return (f"let pods = {pods} | distinct PodUid, Name;"
            " let mem = Perf | where TimeGenerated > ago(7d) and ObjectName == 'K8SContainer' and CounterName == 'memoryWorkingSetBytes'"
            " | extend parts = split(InstanceName, '/') | extend PodUid = tostring(parts[array_length(parts) - 2]),"
            " Container = tostring(parts[array_length(parts) - 1]) | join kind=inner pods on PodUid"
            " | summarize v = avg(CounterValue) by Name, Container, t = bin(TimeGenerated, 1h)"
            " | summarize mem = sum(v) by Name, t | extend part = 'mem';"
            f" let life = {pods} | summarize restarts = max(ContainerRestartCount), oom = countif(ContainerLastStatus has 'OOMKilled')"
            " by Name, ContainerName, t = bin(TimeGenerated, 1h) | summarize restarts = sum(restarts), oom = sum(oom) by Name, t"
            " | extend part = 'life';"
            " union mem, life | project part, Name, t, mem, restarts, oom")


def _iso(value):
    return str(value).replace("+00:00", "Z")


def series_ci(client, cluster_id, namespace, kind, name):
    """{found, pods: {pod: {points, restarts, oom}}, queries, reason}: per pod, from Container Insights."""
    kql = _ci_series_kql(namespace, kind, name)
    sent = [{"label": "Container Insights: memory per pod and hour, with restarts, 7 days", "query": kql, "timespan": WEEK}]
    try:
        rows = aks.resource_logs(client, cluster_id, kql, WEEK)
    except ArmError as error:
        return {"found": False, "pods": {}, "queries": sent, "reason": f"Container Insights couldn't be read: {appinsights.why(error)}"}
    pods, life = {}, {}
    for r in rows:
        if r.get("part") == "mem" and r.get("mem") is not None:
            pods.setdefault(r["Name"], {"points": [], "restarts": [], "oom": []})["points"].append([_iso(r["t"]), r["mem"]])
        elif r.get("part") == "life":
            life.setdefault(r["Name"], []).append((_iso(r["t"]), int(r.get("restarts") or 0), int(r.get("oom") or 0)))
    for pod, hours in life.items():  # the restart count is cumulative: a rise from one hour to the next is a restart
        if pod not in pods:
            continue
        hours.sort()
        for (_, before, _), (at, after, oom) in zip(hours, hours[1:]):
            if after > before:
                pods[pod]["restarts"].append([at, after - before])
                if oom:
                    pods[pod]["oom"].append(at)
    for p in pods.values():
        p["points"].sort()
    return {"found": bool(pods), "pods": pods, "queries": sent,
            "reason": "" if pods else "Container Insights has no memory rows for this workload in the last 7 days."}


def _when(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def growth_per_hour(points):
    """MiB per hour, the least-squares slope of the pod's memory over its own hours; None under MIN_POINTS."""
    xs = [(_when(t) - _when(points[0][0])).total_seconds() / 3600 for t, _ in points] if points else []
    if len(points) < MIN_POINTS or not xs[-1]:
        return None
    ys = [v / MIB for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 1) if den else None


def _median(values):
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None


def around_rollout(pods, rollout_at):
    """The 24 hours before the last rollout against the 24 after: memory per pod (MiB), pods running, restarts.
    None when there's no rollout in the week, or too little data on one side."""
    at = _when(rollout_at) if rollout_at else None
    if not at:
        return None
    sides = {}
    for side, start, end in (("before", at - timedelta(hours=WINDOW_HOURS), at), ("after", at, at + timedelta(hours=WINDOW_HOURS))):
        values, per_hour, restarts = [], {}, 0
        for p in pods.values():
            for t, v in p["points"]:
                when = _when(t)
                if start <= when < end:
                    values.append(v / MIB)
                    per_hour[t] = per_hour.get(t, 0) + 1
            restarts += sum(n for t, n in p["restarts"] if start <= _when(t) < end)
        if len(per_hour) < 2:
            return None
        sides[side] = {"memory_per_pod": round(sum(values) / len(values)), "pods": round(sum(per_hour.values()) / len(per_hour), 1),
                       "restarts": restarts, "hours": len(per_hour)}
    return {"at": rollout_at, **sides}


def shape(pods, rollout_at=None, now=None):
    """Per pod: started, memory now and at its peak (MiB), growth per hour, restarts and OOM kills; the chart's pods (the
    CHART_PODS still running or most recently seen); the median growth; and the rollout comparison."""
    rows = []
    for name, p in pods.items():
        pts = p["points"]
        if not pts:
            continue
        rows.append({"pod": name, "started": pts[0][0], "last_seen": pts[-1][0], "now_mib": round(pts[-1][1] / MIB),
                     "peak_mib": round(max(v for _, v in pts) / MIB), "growth_mib_h": growth_per_hour(pts),
                     "restarts": sum(n for _, n in p["restarts"]), "oom": len(p["oom"])})
    rows.sort(key=lambda r: (r["last_seen"], r["growth_mib_h"] or 0), reverse=True)
    drawn = {r["pod"] for r in rows[:CHART_PODS]}
    chart = [{"pod": n, "points": [[t, round(v / MIB)] for t, v in p["points"]], "restarts": p["restarts"], "oom": p["oom"]}
             for n, p in pods.items() if n in drawn]
    return {"pods": rows, "chart": chart, "median_growth_mib_h": _median(r["growth_mib_h"] for r in rows),
            "rollout": around_rollout(pods, rollout_at)}


def read(client, workload, rollout_at=None, source=None, can=()):
    """A workload's memory week: Container Insights, else the monitoring tool's (`workload usage`)."""
    label = getattr(source, "label", "") or "The monitoring tool"
    cluster, namespace, kind, name = workload["cluster_id"], workload["namespace"], workload["kind"], workload["name"]
    found = series_ci(client, cluster, namespace, kind, name)
    found["source"] = "Container Insights"
    if not found["found"] and source is not None and "workload usage" in can:
        try:
            tool = source.workload_memory(cluster.rstrip("/").split("/")[-1], namespace, kind, name)
        except Exception as error:  # the tool's own error or unreachable: said on the page, never fatal
            found["reason"] += f" {label}: {str(error).splitlines()[0][:300]}"
        else:
            sent = [{"label": f"{label}, account {q['account']}", "query": q["nrql"], "timespan": ""} for q in tool["queries"]]
            found = {**tool, "source": label, "queries": found["queries"] + sent,
                     "reason": "" if tool["found"] else f"{found['reason']} {label} has none either."}
    out = {"found": found["found"], "source": found["source"], "queries": found["queries"], "reason": found.get("reason", "").strip()}
    return {**out, **shape(found["pods"], rollout_at)} if found["found"] else out


# ------------------------------------------------------------------ a cluster's week (Workloads → Memory & scaling)

def _ci_week_kql():
    return ("KubePodInventory | where TimeGenerated > ago(7d) and isnotempty(ControllerName)"
            " | extend Workload = iif(ControllerKind == 'ReplicaSet', replace_regex(ControllerName, '-[a-z0-9]{5,10}$', ''), ControllerName)"
            " | summarize restarts = max(ContainerRestartCount) - min(ContainerRestartCount), oom = countif(ContainerLastStatus has 'OOMKilled')"
            " by Namespace, Workload, ContainerName, PodUid"
            " | summarize restarts = sum(restarts), oom_pods = dcountif(PodUid, oom > 0) by Namespace, Workload")


def cluster_week(client, cluster_id, source=None, can=()):
    """{found, source, workloads: [{namespace, workload, restarts, oom_pods, max_of_limit?, avg_of_request?}],
    hpas: [{namespace, hpa, at_max_percent}], queries, reason}: Container Insights' restarts and OOM kills (it keeps no
    HPA history), else the monitoring tool's, which also has memory against limit and request, and HPA time at max."""
    label = getattr(source, "label", "") or "The monitoring tool"
    kql = _ci_week_kql()
    sent = [{"label": "Container Insights: restarts and OOM kills per workload, 7 days", "query": kql, "timespan": WEEK}]
    reason = ""
    try:
        rows = aks.resource_logs(client, cluster_id, kql, WEEK)
    except ArmError as error:
        rows, reason = [], f"Container Insights couldn't be read: {appinsights.why(error)}"
    if rows:
        return {"found": True, "source": "Container Insights", "queries": sent, "hpas": [], "reason": "",
                "workloads": [{"namespace": r["Namespace"], "workload": r["Workload"], "restarts": int(r.get("restarts") or 0),
                               "oom_pods": int(r.get("oom_pods") or 0)} for r in rows]}
    reason = reason or "Container Insights has no pods for this cluster in the last 7 days."
    if source is not None and "cluster week" in can:
        try:
            tool = source.cluster_week(cluster_id.rstrip("/").split("/")[-1])
        except Exception as error:  # the tool's own error or unreachable: said on the page, never fatal
            return {"found": False, "source": "", "queries": sent, "workloads": [], "hpas": [],
                    "reason": f"{reason} {label}: {str(error).splitlines()[0][:300]}"}
        tool_sent = [{"label": f"{label}, account {q['account']}", "query": q["nrql"], "timespan": ""} for q in tool["queries"]]
        return {**tool, "source": label, "queries": sent + tool_sent, "reason": "" if tool["found"] else f"{reason} {label} has none either."}
    return {"found": False, "source": "", "queries": sent, "workloads": [], "hpas": [], "reason": reason}
