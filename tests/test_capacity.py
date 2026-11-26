from healthconsole.core.explore import aks_checks


def node(name, pool, cpu="8", mem="32Gi"):
    return {"metadata": {"name": name, "labels": {"kubernetes.azure.com/agentpool": pool}},
            "status": {"capacity": {"cpu": cpu, "memory": mem}, "allocatable": {"cpu": cpu, "memory": mem},
                       "conditions": [{"type": "Ready", "status": "True"}]}}


def pod(ns, name, node_name, cpu, mem="1Gi", owner_rs=None, phase="Running"):
    meta = {"namespace": ns, "name": name, **({"ownerReferences": [{"kind": "ReplicaSet", "name": owner_rs}]} if owner_rs else {})}
    return {"metadata": meta, "spec": {"nodeName": node_name, "containers": [{"name": "app", "resources": {"requests": {"cpu": cpu, "memory": mem}}}]},
            "status": {"phase": phase}}


NODES = [node(f"n{i}", "user") for i in range(8)]
BUSY = [pod("shop", f"p{i}", f"n{i % 8}", "7") for i in range(8)]  # 56 of 64 cores requested: 88%


def pool_findings(profiles):
    rows, found = aks_checks.capacity(NODES, BUSY, [], aks_checks.Thresholds(), profiles)
    return rows[0], [f["problem"] for f in found if f["problem"].startswith("Pool user")]


def test_a_full_pool_that_can_still_grow_is_not_a_finding():
    row, found = pool_findings({"user": {"autoscale": True, "min": 8, "max": 20}})
    assert found == [] and row["can_grow"] is True and row["max_nodes"] == 20
    assert row["req_cpu_at_max"] == 35  # 56 cores of 20 × 8: what the same requests are if the pool scales out fully


def test_a_full_pool_that_cannot_grow_says_why():
    _, at_max = pool_findings({"user": {"autoscale": True, "min": 2, "max": 8}})
    assert at_max == ["Pool user: CPU requests are 88% of allocatable, and it's at its maximum of 8 nodes"]
    _, fixed = pool_findings({"user": {"autoscale": False, "count": 8}})
    assert fixed == ["Pool user: CPU requests are 88% of allocatable, and its size is fixed"]
    row, unknown = pool_findings({})
    assert unknown == ["Pool user: CPU requests are 88% of allocatable, and its autoscaler range couldn't be read"]
    assert row["can_grow"] is None and row["req_cpu_at_max"] is None


def test_top_consumers_add_pods_up_by_workload_against_their_requests():
    pods = [pod("shop", "web-1", "n0", "500m", "512Mi", "web-5d8f9c7b8d"), pod("shop", "web-2", "n1", "500m", "512Mi", "web-5d8f9c7b8d"),
            pod("shop", "cart-1", "n0", "100m", "256Mi", "cart-7c9d8e6f5a"), pod("kube-system", "dns-1", "n0", "100m")]
    owner = {("shop", "web-1"): ("shop", "Deployment", "web"), ("shop", "web-2"): ("shop", "Deployment", "web"),
             ("shop", "cart-1"): ("shop", "Deployment", "cart")}
    usage = lambda ns, name, cpu, mem: {"metadata": {"namespace": ns, "name": name},  # noqa: E731
                                        "containers": [{"name": "app", "usage": {"cpu": cpu, "memory": mem}}]}
    metrics = [usage("shop", "web-1", "900m", "600Mi"), usage("shop", "web-2", "300m", "500Mi"),
               usage("shop", "cart-1", "50m", "900Mi"), usage("kube-system", "dns-1", "2", "4Gi")]
    top = aks_checks.top_consumers(pods, metrics, owner, lambda ns: ns != "kube-system", node_pool={"n0": "user", "n1": "spot"})
    assert [r["workload"] for r in top["by_cpu"]] == ["Deployment/web", "Deployment/cart"]  # system hidden
    web = top["by_cpu"][0]
    assert (web["cpu"], web["cpu_request"], web["memory"], web["pod_count"]) == (1200, 1000, 1100, 2)
    assert [p["pod"] for p in web["pods"]] == ["web-1", "web-2"]  # busiest first
    assert web["pools"] == ["spot", "user"]  # where its pods run
    assert [r["workload"] for r in top["by_memory"]] == ["Deployment/web", "Deployment/cart"]
    assert aks_checks.top_consumers(pods, [], owner, lambda ns: True) == {"available": False, "by_cpu": [], "by_memory": []}
