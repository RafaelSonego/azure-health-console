"""One pod, opened from a pod's name on the Workloads tab: what `kubectl describe pod` tells (state, conditions, each
container's state and last exit, probes, events, scheduling, volumes) and its real YAML. From the Kubernetes API, else
Container Insights' last inventory row and events, with what that leaves out named."""
import json

from ..arm import ArmError
from . import aks, aks_checks, kube, workload_detail, workload_problems
from .kube_yaml import yaml_of

CI_MISSING = ["conditions", "probes", "scheduling", "volumes", "YAML"]


def _state(state):
    """(state, reason, detail) of a container's state block."""
    for name in ("waiting", "running", "terminated"):
        if name in (state or {}):
            body = state[name] or {}
            detail = body.get("message") or (f"exit code {body['exitCode']}" if "exitCode" in body else "")
            return name, body.get("reason") or "", (detail or "")[:300], body.get("startedAt") or body.get("finishedAt")
    return "unknown", "", "", None


def container_rows(pod, usage):
    """Each container (init and sidecars too) with its spec (requests, limits, probes, use) and its status now."""
    spec, status = pod.get("spec") or {}, pod.get("status") or {}
    by_name = {s["name"]: s for key in ("initContainerStatuses", "containerStatuses") for s in status.get(key) or []}
    described = {c["name"]: c for c in workload_detail.containers(spec, usage)}
    rows = []
    for kind, items in (("init", [c for c in spec.get("initContainers") or [] if c.get("restartPolicy") != "Always"]),
                        ("app", aks_checks.containers_of(spec))):
        for c in items:
            s = by_name.get(c["name"]) or {}
            state, reason, detail, since = _state(s.get("state"))
            last = (s.get("lastState") or {}).get("terminated")
            base = described.get(c["name"]) or {"name": c["name"], "image": c.get("image")}
            rows.append({**base, "role": "sidecar" if c.get("restartPolicy") == "Always" and kind == "app" else kind,
                         "state": state, "reason": reason, "detail": detail, "since": since,
                         "ready": s.get("ready"), "restarts": s.get("restartCount", 0), "image_now": s.get("image"),
                         "last_exit": {"reason": last.get("reason"), "code": last.get("exitCode"), "at": last.get("finishedAt"),
                                       "started": last.get("startedAt")} if last else None})
    return rows


def _scheduling(spec):
    affinity = spec.get("affinity") or {}
    return {"node_selector": spec.get("nodeSelector") or {}, "priority_class": spec.get("priorityClassName") or "",
            "tolerations": [" ".join(filter(None, (t.get("key") or "*", t.get("operator"), t.get("value"), t.get("effect"))))
                            for t in spec.get("tolerations") or []],
            "affinity": sorted(affinity), "spread": len(spec.get("topologySpreadConstraints") or []),
            "service_account": spec.get("serviceAccountName") or "default"}


def _volumes(spec):
    """Names and types only: a Secret or ConfigMap volume is named, never read."""
    out = []
    for v in spec.get("volumes") or []:
        kind = next((k for k in v if k != "name"), "?")
        body = v.get(kind) or {}
        ref = body.get("secretName") or body.get("name") or body.get("claimName") or ""
        out.append({"name": v["name"], "kind": kind, "ref": ref if isinstance(ref, str) else ""})
    return out


def _owner(pod):
    o = next(iter((pod.get("metadata") or {}).get("ownerReferences") or []), {})
    return workload_problems.workload_of(o.get("kind"), o.get("name"))


def from_api(k, namespace, name):
    pod = k.get(f"/api/v1/namespaces/{namespace}/pods/{name}")
    if pod is None:
        return {"available": False, "reason": f"There's no pod {name} in {namespace} now (it may have been replaced)."}
    lists = {"events": (f"/api/v1/namespaces/{namespace}/events", {"fieldSelector": f"involvedObject.name={name}"}),
             "pod_metrics": (f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods", None),
             "nodes": ("/api/v1/nodes", None)}
    data, failed = workload_detail.read_lists(k, lists)
    spec, status, meta = pod.get("spec") or {}, pod.get("status") or {}, pod["metadata"]
    node = next((n for n in data["nodes"] if n["metadata"]["name"] == spec.get("nodeName")), None)
    summary = {"namespace": namespace, "name": name, "phase": status.get("phase"), "reason": status.get("reason") or "",
               "message": (status.get("message") or "")[:300], "node": spec.get("nodeName") or "",
               "pool": aks_checks._pool_of(node) if node else "",
               "zone": ((node or {}).get("metadata", {}).get("labels") or {}).get("topology.kubernetes.io/zone", ""),
               "pod_ip": status.get("podIP") or "", "qos": status.get("qosClass") or "", "created": meta.get("creationTimestamp"),
               "started": status.get("startTime"), "owner": _owner(pod), "deleting": meta.get("deletionTimestamp")}
    events = sorted(data["events"], key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
    return {"available": True, "source": f"Kubernetes API ({k.source})", "summary": summary,
            "conditions": [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason") or "",
                            "message": (c.get("message") or "")[:300], "at": c.get("lastTransitionTime")}
                           for c in status.get("conditions") or []],
            "containers": container_rows(pod, workload_detail._usage(data["pod_metrics"], [pod])),
            "usage_live": any(m["metadata"]["name"] == name for m in data["pod_metrics"]),
            "events": [{"at": e.get("lastTimestamp") or e.get("eventTime"), "type": e.get("type"), "reason": e.get("reason"),
                        "count": e.get("count") or 1, "message": (e.get("message") or "")[:300]} for e in events[:workload_detail.EVENTS_SHOWN]],
            "scheduling": _scheduling(spec), "volumes": _volumes(spec), "missing": [],
            "yaml": yaml_of(pod, "Pod"),
            "unreadable": failed}


_CI_POD = """KubePodInventory | where TimeGenerated > ago(1d) and Namespace == {ns} and Name == {pod}
| summarize arg_max(TimeGenerated, *) by ContainerName
| project TimeGenerated, PodStatus, PodIp, Computer, ControllerKind, ControllerName, PodCreationTimeStamp, PodStartTime,
  Container=tostring(split(ContainerName, '/')[1]), ContainerStatus, ContainerStatusReason, ContainerRestartCount,
  ContainerLastStatus"""
_CI_EVENTS = """KubeEvents | where TimeGenerated > ago(1d) and Namespace == {ns} and Name == {pod}
| summarize Count=sum(Count), Last=max(TimeGenerated), Message=take_any(Message) by Reason, KubeEventType
| top 30 by Last desc"""


def from_container_insights(client, cluster_id, namespace, name, why):
    args = {"ns": json.dumps(namespace), "pod": json.dumps(name)}
    try:
        rows = aks.resource_logs(client, cluster_id, _CI_POD.format(**args), "P1D")
        events = aks.resource_logs(client, cluster_id, _CI_EVENTS.format(**args), "P1D")
    except ArmError as error:
        return {"available": False, "reason": f"{why} Container Insights couldn't be read either: {str(error).splitlines()[0][:200]}"}
    if not rows:
        return {"available": False, "reason": f"{why} Container Insights has no pod {name} in {namespace} in the last day."}
    first = rows[0]
    containers = []
    for r in rows:
        last = r.get("ContainerLastStatus")
        last = json.loads(last) if isinstance(last, str) and last.startswith("{") else last or {}
        containers.append({"name": r.get("Container") or "", "role": "app", "state": (r.get("ContainerStatus") or "").lower(),
                           "reason": r.get("ContainerStatusReason") or "", "detail": "", "since": None, "ready": None,
                           "restarts": r.get("ContainerRestartCount") or 0,
                           "last_exit": {"reason": last.get("reason"), "code": last.get("exitCode"), "at": last.get("finishedAt"),
                                         "started": last.get("startedAt")} if last.get("reason") else None})
    return {"available": True, "source": "Container Insights (last inventory row)", "seen_at": first.get("TimeGenerated"),
            "summary": {"namespace": namespace, "name": name, "phase": first.get("PodStatus"), "reason": "", "message": "",
                        "node": first.get("Computer") or "", "pool": "", "zone": "", "pod_ip": first.get("PodIp") or "", "qos": "",
                        "created": first.get("PodCreationTimeStamp"), "started": first.get("PodStartTime"),
                        "owner": workload_problems.workload_of(first.get("ControllerKind"), first.get("ControllerName")), "deleting": None},
            "conditions": [], "containers": containers, "usage_live": False,
            "events": [{"at": e.get("Last"), "type": e.get("KubeEventType"), "reason": e.get("Reason"), "count": e.get("Count") or 1,
                        "message": (e.get("Message") or "")[:300]} for e in events],
            "scheduling": None, "volumes": [], "yaml": None, "missing": CI_MISSING, "why_not_api": why, "unreadable": {}}


def read(client, cluster_id, namespace, name, kube_settings=None):
    if not aks.K8S_NAME.match(name or "") or not aks.NAMESPACE.match(namespace or ""):
        raise ValueError("namespace and pod must be Kubernetes names")
    k, why = aks._connect(client, cluster_id, kube_settings)
    if k is not None:
        try:
            return from_api(k, namespace, name)
        except kube.KubeUnavailable as error:
            why = str(error)
    return from_container_insights(client, cluster_id, namespace, name, (why or "The Kubernetes API can't be read.").splitlines()[0][:240])
