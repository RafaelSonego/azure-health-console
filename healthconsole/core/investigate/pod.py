"""From a request's role instance down to its pod: the AKS cluster that ran it (Container Insights' pod inventory), the
pod's state then, warning events around that minute and what changed on the cluster in the hours before."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..arm import ArmError
from ..explore import activity, aks, appinsights
from .appinsights import kql_string

AKS_TYPE = "microsoft.containerservice/managedclusters"
MAX_CLUSTERS = 20
EVENT_SPREAD = timedelta(minutes=15)
CHANGES_BEFORE = timedelta(hours=6)
CHANGES_AFTER = timedelta(minutes=10)
ACTIVITY_HOURS = (6, 24, 168)


def _span(at, before, after):
    return f"{appinsights.iso(at - before)}/{appinsights.iso(at + after)}"


def find_query(pod):
    if not aks.K8S_NAME.match(pod or ""):
        raise ValueError(f"'{pod}' isn't a Kubernetes pod name")
    return ("KubePodInventory | where Name == " + kql_string(pod)
            + " | summarize arg_max(TimeGenerated, Namespace, PodStatus, PodCreationTimeStamp, ControllerName,"
              " ControllerKind, ContainerRestartCount, Computer) by Name | take 1")


def find(client, subscriptions, pod, at):
    """Which cluster in scope ran `pod` around `at`, and its state then; with every cluster, to pick from when none
    knows it (no Container Insights, or the instance isn't a pod name)."""
    kql = find_query(pod)
    rows = client.query(f"Resources | where type =~ '{AKS_TYPE}' | project id, name, resourceGroup | order by name asc",
                        subscriptions=list(subscriptions) or None)
    clusters = [{"id": r["id"], "name": r["name"], "resource_group": r.get("resourceGroup") or ""} for r in rows]
    span = _span(at, timedelta(hours=6), timedelta(hours=1))

    def look(cluster):
        try:
            return cluster, aks.resource_logs(client, cluster["id"], kql, span), None
        except ArmError:
            return cluster, [], cluster["name"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(look, clusters[:MAX_CLUSTERS]))
    hits = [(c, r[0]) for c, r, _ in results if r]
    unread = [name for _, _, name in results if name]
    notes = []
    if len(clusters) > MAX_CLUSTERS:
        notes.append(f"Looked in {MAX_CLUSTERS} of {len(clusters)} clusters in scope (A–Z); pick one below if it isn't found.")
    if unread:
        notes.append(f"{len(unread)} cluster(s) couldn't be asked (no Container Insights, or no access): {', '.join(unread[:5])}.")
    found = None
    if hits:
        cluster, row = hits[0]
        found = {"cluster_id": cluster["id"], "cluster_name": cluster["name"], "namespace": row.get("Namespace") or "",
                 "pod": pod, "status": row.get("PodStatus") or "", "started": row.get("PodCreationTimeStamp") or "",
                 "controller": row.get("ControllerName") or "", "controller_kind": row.get("ControllerKind") or "",
                 "restarts": row.get("ContainerRestartCount"), "node": row.get("Computer") or "",
                 "last_seen": row.get("TimeGenerated") or ""}
        if len(hits) > 1:
            notes.append(f"A pod named {pod} was also seen in {', '.join(c['name'] for c, _ in hits[1:])}.")
    return {"found": found, "clusters": clusters, "query": kql, "timespan": span, "notes": notes}


def events_query(namespace, at):
    if not aks.K8S_NAME.match(namespace or ""):
        raise ValueError(f"'{namespace}' isn't a Kubernetes namespace")
    start, end = at - EVENT_SPREAD, at + EVENT_SPREAD
    return (f"KubeEvents | where TimeGenerated between (datetime({appinsights.iso(start)}) .. datetime({appinsights.iso(end)}))"
            f" and Namespace == {kql_string(namespace)} and KubeEventType == 'Warning'"
            " | summarize Count=sum(Count), First=min(TimeGenerated), Last=max(TimeGenerated), Message=take_any(Message)"
            " by Reason, Kind=ObjectKind, Object=Name | order by Last desc | take 50")


def _moment(text):
    """An Azure time (up to 7 fraction digits, Z or an offset) as UTC, or None."""
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _within(rows, field, start, end):
    out = []
    for row in rows:
        moment = _moment(row.get(field))
        if moment and start <= moment <= end:
            out.append(row)
    return out


def changes(client, resource_id, at, namespace=""):
    """What changed on a resource (and, for a cluster with `namespace`, its warning events) around `at`."""
    start, end = at - CHANGES_BEFORE, at + CHANGES_AFTER
    since = (datetime.now(timezone.utc) - start).total_seconds() / 3600
    hours = next((h for h in ACTIVITY_HOURS if h >= since), None)
    out = {"from": appinsights.iso(start), "to": appinsights.iso(end), "changes": [], "operations": [], "events": [],
           "errors": [], "event_query": ""}
    if hours is None:
        out["errors"].append("Changes are only kept here for 7 days; this request is older.")
    else:
        found = activity.activity(client, resource_id, hours)
        out["changes"] = _within(found["changes"], "at", start, end)
        out["operations"] = _within(found["operations"], "at", start, end)
        out["errors"] += found["errors"]
    if namespace:
        out["event_query"] = events_query(namespace, at)
        try:
            out["events"] = aks.resource_logs(client, resource_id, out["event_query"], _span(at, EVENT_SPREAD, EVENT_SPREAD))
        except ArmError as error:
            out["errors"].append(f"Kubernetes events: {str(error)[:200]}")
    return out
