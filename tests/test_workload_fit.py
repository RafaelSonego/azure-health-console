from flask import Flask

from healthconsole.core import config
from healthconsole.core.explore import aks, kube, workload_fit as wf
from healthconsole.core.explore.workload_detail import hpa_decision
from healthconsole.web import create_blueprint
from tests.conftest import FakeClient
from tests.test_workload_detail import CLUSTER, HPA, deployment

NODES = [f"n{i}" for i in range(1, 5)]


def workload(cpu="2", memory="4Gi", strategy=None):
    obj = deployment()
    obj["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"] = {"cpu": cpu, "memory": memory}
    if strategy:
        obj["spec"]["strategy"] = strategy
    return obj


def node(name, pool="apps", ready=True, cordoned=False):
    return {"metadata": {"name": name, "labels": {"agentpool": pool}}, "spec": {"unschedulable": cordoned},
            "status": {"allocatable": {"cpu": "16", "memory": "64Gi"}, "conditions": [{"type": "Ready", "status": str(ready)}]}}


def a_pod(name, on, cpu, memory, owner=("ReplicaSet", "api-7d9f8"), phase="Running"):
    return {"metadata": {"name": name, "namespace": "shop", "ownerReferences": [{"kind": owner[0], "name": owner[1]}]},
            "spec": {"nodeName": on, "containers": [{"name": "c", "resources": {"requests": {"cpu": cpu, "memory": memory}}}]},
            "status": {"phase": phase}}


def cluster(own_per_node=5):
    """4 nodes of 16 cores / 64 GiB; on each a DaemonSet pod (200m, 512Mi), another app (2 cores, 8 GiB) and 5 of ours."""
    own = [a_pod(f"api-7d9f8-{n}{i}", n, "2", "4Gi") for n in NODES for i in range(own_per_node)]
    others = [p for n in NODES for p in (a_pod(f"agent-{n}", n, "200m", "512Mi", ("DaemonSet", "agent")),
                                         a_pod(f"shop-{n}", n, "2", "8Gi", ("ReplicaSet", "shop-1")))]
    others.append(a_pod("done", "n1", "8", "32Gi", phase="Succeeded"))  # a finished pod holds nothing
    return own, own + others, [node(n) for n in NODES] + [node("sys-1", pool="system")]


def test_the_hpa_arithmetic_with_the_new_request_gives_the_replicas_and_they_fit():
    own, pods, nodes = cluster()
    out = wf.plan("Deployment", workload(), own, pods, nodes, hpa_decision(HPA), {"api": (1200, 8192)})
    # memory 117% of 4 GiB is 58.5% of 8 GiB: ceil(20 × 58.5 / 70) = 17; CPU 4% of 2 cores is 6.7% of 1.2 cores: 2
    assert out["replicas"]["after"] == 17 and out["replicas"]["why"] == "its HPA: memory asks for 17"
    assert [(m["resource"], m["after"], m["asks"]) for m in out["replicas"]["metrics"]] == [("cpu", 7, 2), ("memory", 58, 17)]
    [apps] = out["pools"]  # only the pool its pods run on; the system pool isn't counted
    assert out["fits"] and (apps["pods_before"], apps["pods_after"]) == (20, 17)
    assert (apps["before_mem"], apps["after_mem"]) == (4 * 8704 + 20 * 4096, 4 * 8704 + 17 * 8192)
    assert apps["alloc_mem"] == 4 * 65536 and out["per_pod"] == {"before": [2000, 4096], "after": [1200, 8192]}
    assert out["rollout"]["surge"] == 5 and out["rollout"]["room_now"] == 12 and out["rollout"]["note"] == ""


def test_pods_that_dont_fit_say_how_many_nodes_are_missing_and_if_the_autoscaler_can_add_them():
    own, pods, nodes = cluster()
    strategy = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "100%", "maxUnavailable": 0}}
    profiles = {"apps": {"autoscale": True, "min": 3, "max": 6, "count": 4, "vm_size": "Standard_D16s_v5"}}
    out = wf.plan("Deployment", workload(strategy=strategy), own, pods, nodes, hpa_decision(HPA), {"api": (1200, 16384)}, profiles=profiles)
    # 16 GiB: memory asks for 9, raised to the HPA's minimum of 13; 3 fit per node (56,832 MiB free), 12 in all
    assert out["replicas"]["after"] == 13 and "raised to its minimum of 13" in out["replicas"]["why"]
    assert not out["fits"] and out["short"] == 1
    # an empty node keeps 65,024 MiB after its DaemonSet pod: 3 more per node, so 1 node, and the autoscaler has 2 left
    assert out["missing"] == {"pool": "apps", "pods": 1, "per_node": 3, "nodes": 1, "room": 2}
    assert "waits for new nodes" in out["rollout"]["note"] and out["rollout"]["surge"] == 13


def test_not_ready_and_cordoned_nodes_and_a_given_replica_count():
    own, pods, nodes = cluster()
    nodes[3] = node("n4", cordoned=True)
    out = wf.plan("Deployment", workload(), own, pods, nodes, hpa_decision(HPA), {"api": (1200, 8192)}, replicas=19)
    assert out["replicas"] == {"now": 20, "after": 19, "why": "the replicas you gave", "metrics": []}
    assert out["short"] == 1 and any("cordoned are left out" in n for n in out["notes"])  # 6 per node on the 3 left


def test_one_node_with_room_during_a_rollout_is_named():
    own, pods, nodes = cluster()
    for p in pods:  # fill n2–n4 with another app, so only n1 has room next to the old pods
        if p["metadata"]["name"].startswith("shop-") and p["spec"]["nodeName"] != "n1":
            p["spec"]["containers"][0]["resources"]["requests"] = {"cpu": "5", "memory": "20Gi"}
    out = wf.plan("Deployment", workload(), own, pods, nodes, None, {"api": (1200, 8192)})
    assert out["replicas"]["why"] == "no HPA: it keeps its fixed replicas" and out["replicas"]["after"] == 20
    assert out["rollout"]["nodes_with_room"] == 1 and out["rollout"]["note"].startswith("Only n1 has room")


def test_the_route_validates_and_reads_only(monkeypatch, tmp_path):
    own, pods, nodes = cluster()

    class Kube:
        source = "kubeconfig"
        paths = []

        def get(self, path, params=None):
            kube.check_kube_read("GET", path)
            return workload()

        def items(self, path, params=None):
            kube.check_kube_read("GET", path)
            self.paths.append((path, params))
            return {"/api/v1/nodes": nodes, "/api/v1/pods": pods, "/api/v1/namespaces/shop/pods": own,
                    "/apis/autoscaling/v2/namespaces/shop/horizontalpodautoscalers": [HPA],
                    "/apis/apps/v1/namespaces/shop/replicasets": [{"metadata": {"name": "api-7d9f8", "ownerReferences": [
                        {"kind": "Deployment", "name": "api"}]}}]}.get(path, [])

    k = Kube()
    monkeypatch.setattr(aks, "_connect", lambda *a: (k, None))
    file = tmp_path / "c.yaml"
    arm = {"properties": {"agentPoolProfiles": [{"name": "apps", "enableAutoScaling": True, "minCount": 3, "maxCount": 6, "count": 4}]}}
    client = FakeClient(query_handlers=[("managedclusters/shop-aks", lambda kql: [arm])])
    app = Flask("test", static_folder=None)
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load(file), config_file=file, authorize=lambda: None))
    http = app.test_client()
    base = {"cluster": CLUSTER, "namespace": "shop", "kind": "Deployment", "workload": "api"}
    assert http.get("/api/workload/fit", query_string=base).status_code == 400
    assert http.get("/api/workload/fit", query_string={**base, "container": "api:1.2:8192"}).status_code == 400
    assert http.get("/api/workload/fit", query_string={**base, "container": "api:1200:8192", "replicas": "0"}).status_code == 400
    assert "no container web" in http.get("/api/workload/fit", query_string={**base, "container": "web:1:1"}).get_json()["reason"]
    out = http.get("/api/workload/fit", query_string={**base, "container": "api:1200:8192"}).get_json()
    assert out["fits"] and out["replicas"]["after"] == 17 and out["pools"][0]["autoscaler"]["max"] == 6
    assert ("/api/v1/pods", {"fieldSelector": "status.phase!=Succeeded,status.phase!=Failed"}) in k.paths
