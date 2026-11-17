from datetime import date

from healthconsole.core.explore import aks, aks_checks, kube

TODAY = date(2026, 11, 10)


def node(name, pool="user", ready=True, reason="", pressure=None, cordoned=False, image="202610.30.0", cpu="3860m",
         memory="12Gi", pods="110", taints=()):
    conditions = [{"type": "Ready", "status": "True" if ready else "False", "reason": reason, "message": "kubelet stopped"}]
    if pressure:
        conditions.append({"type": pressure, "status": "True", "reason": "KubeletHasPressure"})
    return {"metadata": {"name": name, "creationTimestamp": "2026-10-01T00:00:00Z",
                         "labels": {"kubernetes.azure.com/agentpool": pool, "node.kubernetes.io/instance-type": "Standard_D4s_v5",
                                    "topology.kubernetes.io/zone": "canadacentral-1",
                                    "kubernetes.azure.com/node-image-version": f"AKSUbuntu-2204gen2containerd-{image}"}},
            "spec": {"unschedulable": cordoned, "taints": list(taints)},
            "status": {"conditions": conditions, "allocatable": {"cpu": cpu, "memory": memory, "pods": pods},
                       "nodeInfo": {"kubeletVersion": "v1.34.4", "osImage": "Ubuntu 22.04.5 LTS", "kernelVersion": "5.15",
                                    "containerRuntimeVersion": "containerd://1.7"}}}


def pod(node_name, cpu="500m", memory="1Gi", phase="Running"):
    return {"spec": {"nodeName": node_name, "containers": [{"resources": {"requests": {"cpu": cpu, "memory": memory}}}]},
            "status": {"phase": phase}}


def test_node_rows():
    nodes = [node("a"), node("b", ready=False, reason="KubeletNotReady"), node("c", pressure="MemoryPressure"),
             node("d", cordoned=True, taints=[{"key": "gpu", "value": "true", "effect": "NoSchedule"}]),
             node("e", image="202312.06.0")]
    pods = [pod("a"), pod("a", cpu="1500m", memory="2Gi"), pod("a", phase="Succeeded"), pod("b", phase="Pending")]
    metrics = [{"metadata": {"name": "a"}, "usage": {"cpu": "965m", "memory": "6Gi"}}]
    rows = {r["name"]: r for r in aks_checks.node_rows(nodes, pods, metrics, TODAY)}
    a = rows["a"]
    assert a["status"] == "healthy" and a["pods"] == {"placed": 2, "max": 110}  # the finished pod isn't placed
    assert a["cpu"]["requested_pct"] == 52 and a["cpu"]["used_pct"] == 25 and a["memory"]["requested_pct"] == 25
    assert rows["b"]["status"] == "critical" and rows["b"]["problems"][0].startswith("NotReady: KubeletNotReady")
    assert rows["b"]["cpu"]["used_pct"] is None  # no metrics for it: unknown, not 0%
    assert rows["c"]["status"] == "warning" and "MemoryPressure" in rows["c"]["problems"]
    assert rows["d"]["status"] == "warning" and rows["d"]["taints"] == ["gpu=true:NoSchedule"]
    assert rows["e"]["node_image_age"] == 1070 and "node image 1070 days old" in rows["e"]["problems"]
    order = [r["name"] for r in aks_checks.node_rows(nodes, pods, metrics, TODAY)]
    assert order[0] == "b" and order[-1] == "a"  # NotReady first, healthy last


def test_nodes_through_the_api_and_the_fallback(monkeypatch):
    class Kube:
        source = "kubeconfig context aks-shop"

        def get(self, path, params=None, raw=False):
            return {"gitVersion": "v1.34.4"}

        def items(self, path):
            return {"/api/v1/nodes": [node("a")], "/api/v1/pods": [pod("a")],
                    "/apis/metrics.k8s.io/v1beta1/nodes": []}[path]

    monkeypatch.setattr(kube, "client_for", lambda *a, **k: Kube())
    out = aks.nodes(object(), "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/shop")
    assert out["source"] == "kubernetes" and [n["name"] for n in out["nodes"]] == ["a"]
    assert out["pools"][0]["pool"] == "user" and out["pools"][0]["nodes"] == 1 and out["unreadable"] == []

    def unreachable(*a, **k):
        raise kube.KubeUnavailable("The cluster's API server isn't reachable from this machine.")

    monkeypatch.setattr(kube, "client_for", unreachable)
    monkeypatch.setattr(aks, "resource_logs", lambda client, rid, query, timespan="PT1H": [
        {"Computer": "aks-user-1", "Status": "Ready", "KubeletVersion": "v1.34.4",
         "Labels": '[{"kubernetes.azure.com/agentpool": "user", "node.kubernetes.io/instance-type": "Standard_D4s_v5"}]'},
        {"Computer": "aks-user-2", "Status": "NotReady", "KubeletVersion": "v1.34.4", "Labels": "not json"}])
    out = aks.nodes(object(), "/x")
    assert out["source"] == "container_insights" and "isn't reachable" in out["reason"]
    assert [(n["name"], n["status"], n["pool"]) for n in out["nodes"]] == [("aks-user-1", "healthy", "user"),
                                                                          ("aks-user-2", "critical", "")]
    monkeypatch.setattr(aks, "resource_logs", lambda *a, **k: [])
    out = aks.nodes(object(), "/x")
    assert not out["available"] and "no node data" in out["fallback"]


def test_namespace_pod_list_is_allowed_but_node_writes_are_not():
    import pytest

    from healthconsole.core.guarded import WriteBlocked
    kube.check_kube_read("GET", "/api/v1/nodes")
    for method, path in (("PATCH", "/api/v1/nodes/a"), ("GET", "/api/v1/nodes/a/proxy/logs"), ("POST", "/api/v1/nodes")):
        with pytest.raises(WriteBlocked):
            kube.check_kube_read(method, path)


def test_an_unreachable_cluster_is_tried_once_then_answered_from_memory(monkeypatch):
    aks._unreachable.clear()
    tries = []

    def unreachable(*a, **k):
        tries.append(1)
        raise kube.KubeUnreachable("The cluster's API server isn't reachable from this machine.")

    monkeypatch.setattr(kube, "client_for", unreachable)
    cluster = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/private"
    for _ in range(3):
        assert "isn't reachable" in aks._connect(object(), cluster, None)[1]
    assert len(tries) == 1

    class Fresh:
        is_fresh = True
    aks._connect(Fresh(), cluster, None)
    assert len(tries) == 2  # a Refresh tries the network again


def test_a_refused_cluster_is_asked_again(monkeypatch):
    aks._unreachable.clear()
    tries = []

    def refused(*a, **k):
        tries.append(1)
        raise kube.KubeUnavailable("No access to the Kubernetes API (HTTP 403)")

    monkeypatch.setattr(kube, "client_for", refused)
    aks._connect(object(), "/c", None)
    aks._connect(object(), "/c", None)
    assert len(tries) == 2  # a 403 answers fast; only a missing network path is remembered
