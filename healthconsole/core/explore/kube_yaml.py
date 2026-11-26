"""A Kubernetes object as YAML, the way `kubectl get -o yaml` shows it (less managedFields). Pages get it with every env
value hidden; the values are read again, and sent, only when someone asks to see them (`read`, values=True)."""
import copy

from ..yamlio import dump
from . import aks, kube

HIDDEN = "‹hidden›"
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"  # the applied spec again, env values included


def _pod_specs(obj):
    """The pod specs inside a pod, a workload (its template) or a CronJob (its job's template)."""
    spec = obj.get("spec") or {}
    if "containers" in spec:
        return [spec]
    template = spec.get("template") or ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
    return [template["spec"]] if template.get("spec") else []


def masked(obj):
    """A copy with every env value (and the last-applied annotation, which repeats them) replaced by HIDDEN; references
    (secretKeyRef, configMapKeyRef, fieldRef) are names, so they stay."""
    out = copy.deepcopy(obj)
    annotations = (out.get("metadata") or {}).get("annotations") or {}
    if LAST_APPLIED in annotations:
        annotations[LAST_APPLIED] = HIDDEN
    for spec in _pod_specs(out):
        for key in ("containers", "initContainers", "ephemeralContainers"):
            for c in spec.get(key) or []:
                for env in c.get("env") or []:
                    if "value" in env:
                        env["value"] = HIDDEN
    return out


KINDS = {  # kind: (apiVersion, how it's read: one GET, or a namespace list picked by name)
    "Pod": ("v1", "/api/v1/namespaces/{ns}/pods/{name}"),
    "Deployment": ("apps/v1", "/apis/apps/v1/namespaces/{ns}/deployments/{name}"),
    "StatefulSet": ("apps/v1", "/apis/apps/v1/namespaces/{ns}/statefulsets/{name}"),
    "DaemonSet": ("apps/v1", "/apis/apps/v1/namespaces/{ns}/daemonsets/{name}"),
    "HorizontalPodAutoscaler": ("autoscaling/v2", "list:/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers"),
}
SHORT = {"Pod": "pod", "Deployment": "deployment", "StatefulSet": "statefulset", "DaemonSet": "daemonset", "HorizontalPodAutoscaler": "hpa"}


def _clean(obj, kind):
    clean = {**({"apiVersion": KINDS[kind][0]} if "apiVersion" not in obj else {}),
             **({"kind": kind} if "kind" not in obj else {}), **copy.deepcopy(obj)}
    (clean.get("metadata") or {}).pop("managedFields", None)
    return clean


def yaml_of(obj, kind):
    """{"yaml" (env values hidden), "hides" (whether any were), "command", "kind", "namespace", "name"}, or None."""
    if not obj:
        return None
    clean, meta = _clean(obj, kind), obj.get("metadata") or {}
    hidden = masked(clean)
    return {"yaml": dump(hidden), "hides": hidden != clean, "kind": kind, "namespace": meta.get("namespace", ""),
            "name": meta.get("name", ""), "command": f"kubectl get {SHORT[kind]} -n {meta.get('namespace', '')} {meta.get('name', '')} -o yaml"}


def read(client, cluster_id, namespace, kind, name, values=False, kube_settings=None):
    """The object read now, as `yaml_of` gives it, or with the env values as they are when `values`."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    if not aks.NAMESPACE.match(namespace or "") or not aks.K8S_NAME.match(name or ""):
        raise ValueError("namespace and name must be Kubernetes names")
    k, why = aks._connect(client, cluster_id, kube_settings)
    if k is None:
        return {"available": False, "reason": why}
    path = KINDS[kind][1].format(ns=namespace, name=name)
    try:
        if path.startswith("list:"):
            obj = next((o for o in k.items(path[5:]) if o["metadata"]["name"] == name), None)
        else:
            obj = k.get(path)
    except kube.KubeUnavailable as error:
        return {"available": False, "reason": str(error)}
    if obj is None:
        return {"available": False, "reason": f"There's no {kind} {name} in {namespace} now."}
    out = yaml_of(obj, kind)
    if values:
        out["yaml"], out["values"] = dump(_clean(obj, kind)), True
    return {"available": True, **out}
