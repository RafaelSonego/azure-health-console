from datetime import datetime, timedelta, timezone

from healthconsole.core.explore import aks, workload_problems as wp

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
iso = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731


def k8s(**extra):
    return {"available": True, "not_running": [], "findings": [], "workload_rows": [], "rollouts": [], **extra}


def ci(**lists):
    return {"available": True, "hours": {"restarts": 1, "oomkilled": 24, "events": 1}, **lists}


def by_workload(result):
    return {(g["namespace"], g["workload"]): g for g in result["groups"]}


def test_workload_of_names_the_workload_behind_a_pod_owner():
    assert wp.workload_of("ReplicaSet", "web-5d8f9c7b8d") == "Deployment/web"
    assert wp.workload_of("ReplicaSet", "odd") == "ReplicaSet/odd"
    assert wp.workload_of("Job", "nightly-29001234") == "CronJob/nightly"
    assert wp.workload_of("Job", "migrate") == "Job/migrate"
    assert wp.workload_of("StatefulSet", "db") == "StatefulSet/db"
    assert wp.workload_of("", "x") == ""


def test_every_source_about_one_workload_is_one_row():
    pod_owner = {("shop", "web-5d8f9c7b8d-abcde"): ("shop", "Deployment", "web"),
                 ("shop", "web-5d8f9c7b8d-fghij"): ("shop", "Deployment", "web")}
    k = k8s(not_running=[{"namespace": "shop", "pod": "web-5d8f9c7b8d-abcde", "phase": "Pending", "reason": "Unschedulable",
                          "owner": "ReplicaSet/web-5d8f9c7b8d", "since": iso(NOW - timedelta(minutes=5))}],
            findings=[{"kind": "now", "severity": "MEDIUM", "problem": "40 container restart(s) across its pods", "namespace": "shop",
                       "workload": "Deployment/web", "container": "app", "signal": "restarts"},
                      {"kind": "now", "severity": "HIGH", "problem": "At the maximum 6 replicas", "namespace": "shop",
                       "workload": "Deployment/web", "container": ""},
                      {"kind": "config", "severity": "MEDIUM", "problem": "No PodDisruptionBudget", "namespace": "shop",
                       "workload": "Deployment/web"}],
            workload_rows=[{"namespace": "shop", "workload": "Deployment/web", "ready": 4, "desired": 6}])
    c = ci(not_running=[{"Namespace": "shop", "Pod": "web-5d8f9c7b8d-abcde", "Status": "Pending", "Reason": "",
                         "OwnerKind": "ReplicaSet", "Owner": "web-5d8f9c7b8d"}],
           restarts=[{"Namespace": "shop", "Pod": "web-5d8f9c7b8d-abcde", "Container": "app", "Restarts": 5},
                     {"Namespace": "shop", "Pod": "web-5d8f9c7b8d-fghij", "Container": "app", "Restarts": 7}],
           oomkilled=[{"namespace": "shop", "pod": "web-5d8f9c7b8d-fghij", "owner": "ReplicaSet web-5d8f9c7b8d", "cause": "container"}])
    result = wp.problems([], c, k, pod_owner, now=NOW)
    assert list(by_workload(result)) == [("shop", "Deployment/web")]
    web = result["groups"][0]
    texts = {s["type"]: s for s in web["signals"]}
    assert texts["not_running"]["text"] == "1 pod(s) not running: Unschedulable"  # one pod, seen twice
    assert sorted(texts["not_running"]["sources"]) == [wp.CI, wp.K8S]
    assert texts["restarts"]["text"] == "12 restart(s), last hour"  # Container Insights' window replaces the lifetime count
    assert not any("40 container restart" in s["text"] for s in web["signals"])
    assert texts["oom"]["detail"] == "cause: container limit"
    assert texts["k8s"]["text"] == "At the maximum 6 replicas"
    assert not any("PodDisruptionBudget" in s["text"] for s in web["signals"])  # setup risks stay in their own section
    assert web["severity"] == "HIGH" and web["signals"][0]["severity"] == "HIGH"
    assert (web["ready"], web["desired"]) == (4, 6)
    assert web["pods"] == ["web-5d8f9c7b8d-abcde", "web-5d8f9c7b8d-fghij"]
    assert result["verdict"]["workloads"] == 1 and result["verdict"]["critical"] == 1


def test_the_kubernetes_restart_count_stays_when_container_insights_has_none():
    k = k8s(findings=[{"kind": "now", "severity": "MEDIUM", "problem": "9 container restart(s) across its pods", "namespace": "shop",
                       "workload": "Deployment/api", "container": "app", "signal": "restarts"}])
    result = wp.problems([], {"available": False, "reason": "off\nsecond line"}, k, now=NOW)
    assert result["groups"][0]["signals"][0]["text"] == "app: 9 container restart(s) across its pods"
    assert result["verdict"]["missing"] == [{"source": wp.CI, "reason": "off"}]  # first line only, shown on the page
    assert not result["verdict"]["only_counts"]


def test_old_failed_pods_are_listed_apart():
    old = {"namespace": "shop", "pod": "web-5d8f9c7b8d-zzzzz", "phase": "Failed", "reason": "Evicted",
           "owner": "ReplicaSet/web-5d8f9c7b8d", "since": iso(NOW - timedelta(days=3))}
    recent = {**old, "pod": "web-5d8f9c7b8d-yyyyy", "since": iso(NOW - timedelta(hours=2))}
    c = ci(not_running=[{"Namespace": "shop", "Pod": old["pod"], "Status": "Failed", "OwnerKind": "ReplicaSet", "Owner": "web-5d8f9c7b8d"}])
    result = wp.problems([], c, k8s(not_running=[old, recent]), now=NOW)
    assert [r["pod"] for r in result["leftover"]] == [old["pod"]]
    assert result["leftover"][0]["workload"] == "Deployment/web"
    signal = result["groups"][0]["signals"][0]
    assert signal["text"] == "1 pod(s) not running: Evicted" and signal["sources"] == [wp.K8S]  # CI's row for the old one dropped
    assert result["verdict"]["leftover"] == 1


def test_without_the_kubernetes_api_rows_are_grouped_by_their_controller():
    c = ci(hot_containers=[{"Namespace": "shop", "Pod": "cart-7c9d8e6f5a-aaaaa", "Container": "app", "CpuPctOfLimit": 50,
                            "MemPctOfLimit": 93, "OwnerKind": "ReplicaSet", "Owner": "cart-7c9d8e6f5a"}],
           events=[{"Reason": "BackOff", "Namespace": "shop", "Object": "cart-7c9d8e6f5a-aaaaa", "ObjectKind": "Pod", "Count": 4},
                   {"Reason": "Unhealthy", "Namespace": "shop", "Object": "cart-7c9d8e6f5a-aaaaa", "ObjectKind": "Pod", "Count": 2},
                   {"Reason": "NodeNotReady", "Namespace": "", "Object": "aks-user-1", "ObjectKind": "Node", "Count": 1},
                   {"Reason": "FailedGetScale", "Namespace": "shop", "Object": "db", "ObjectKind": "StatefulSet", "Count": 3}])
    result = wp.problems([], c, {"available": False, "reason": "no network path"}, now=NOW)
    groups = by_workload(result)
    assert list(groups)[0] == ("", "")  # the cluster's own come first
    assert groups[("", "")]["signals"][0]["text"] == "warning events, last hour: NodeNotReady ×1"
    cart = groups[("shop", "Deployment/cart")]
    assert [s["text"] for s in cart["signals"]] == ["app near its limits: memory 93% of its limit",
                                                     "warning events, last hour: BackOff ×4, Unhealthy ×2"]
    assert ("shop", "StatefulSet/db") in groups
    assert result["verdict"]["workloads"] == 2 and result["verdict"]["cluster"] == ["warning events, last hour: NodeNotReady ×1"]


def test_only_platform_metrics_still_says_where_pods_are_stuck():
    phases = [{"namespace": "shop", "phase": "Pending", "pods": 3}, {"namespace": "shop", "phase": "Running", "pods": 9}]
    result = wp.problems(phases, {"available": False, "reason": "not sending"}, {"available": False, "reason": "403"}, now=NOW)
    assert [(g["namespace"], g["signals"][0]["text"], g["sources"]) for g in result["groups"]] == [("shop", "3 pod(s) Pending", [wp.PLATFORM])]
    assert result["groups"][0]["severity"] == "MEDIUM"  # a bare count can't tell new failures from leftovers
    assert result["verdict"]["read"] == [wp.PLATFORM] and result["verdict"]["only_counts"]
    assert [m["source"] for m in result["verdict"]["missing"]] == [wp.K8S, wp.CI]


def _rs(name, deploy, revision, created, image):
    return {"metadata": {"namespace": "shop", "name": name, "creationTimestamp": iso(created),
                         "annotations": {"deployment.kubernetes.io/revision": str(revision)},
                         "ownerReferences": [{"kind": "Deployment", "name": deploy}]},
            "spec": {"template": {"spec": {"containers": [{"name": "app", "image": image}]}}}}


def test_a_recent_rollout_names_the_images_it_changed():
    objs = {"Deployment": [{"metadata": {"namespace": "shop", "name": "web"}, "spec": {"replicas": 3}, "status": {"updatedReplicas": 1}},
                           {"metadata": {"namespace": "shop", "name": "old"}, "spec": {"replicas": 1}, "status": {"updatedReplicas": 1}}],
            "StatefulSet": [{"metadata": {"namespace": "shop", "name": "db"}, "status": {"currentRevision": "db-1", "updateRevision": "db-2"}}]}
    sets = [_rs("web-a", "web", 3, NOW - timedelta(days=9), "web:1.2"), _rs("web-b", "web", 4, NOW - timedelta(minutes=18), "web:1.3"),
            _rs("old-a", "old", 1, NOW - timedelta(days=2), "old:1")]
    rolled = wp.rollouts(objs, sets, lambda ns: True, NOW)
    assert rolled[("shop", "Deployment/web")] == {"at": iso(NOW - timedelta(minutes=18)), "revision": 4, "rolling": True,
                                                  "images": [{"container": "app", "from": "web:1.2", "to": "web:1.3"}]}
    assert ("shop", "Deployment/old") not in rolled  # older than the window
    assert rolled[("shop", "StatefulSet/db")]["rolling"] and rolled[("shop", "StatefulSet/db")]["at"] is None
    k = k8s(rollouts=[{"namespace": ns, "workload": w, **r} for (ns, w), r in rolled.items()],
            findings=[{"kind": "now", "severity": "HIGH", "problem": "1 of 3 replicas ready", "namespace": "shop", "workload": "Deployment/web"}])
    result = wp.problems([], {"available": False}, k, now=NOW)
    assert result["groups"][0]["changed"]["revision"] == 4
    assert result["verdict"]["rolled_out"] == ["shop/Deployment/web"]
    assert result["verdict"]["rolling"] == ["shop/Deployment/web", "shop/StatefulSet/db"]


def test_workloads_returns_the_merged_list_and_each_layers_time(monkeypatch):
    monkeypatch.setattr(aks, "pod_phases", lambda client, cluster: [{"namespace": "shop", "phase": "Failed", "pods": 1}])
    monkeypatch.setattr(aks, "container_insights", lambda *a: {"available": False, "reason": "off"})
    monkeypatch.setattr(aks, "_kubernetes", lambda *a: ({"available": False, "reason": "403"}, {}))
    result = aks.workloads(None, "/subscriptions/s/resourceGroups/r/providers/Microsoft.ContainerService/managedClusters/c")
    assert result["problems"]["groups"][0]["signals"][0]["text"] == "1 pod(s) Failed"
    assert set(result["timings"]) == {wp.PLATFORM, wp.CI, wp.K8S}


def test_each_pool_says_its_vm_size_what_a_node_holds_and_its_autoscaler_range(monkeypatch):
    from healthconsole.core.explore import aks_checks

    def node(name, pool, size, cpu, mem, alloc_cpu, alloc_mem):
        return {"metadata": {"name": name, "labels": {"kubernetes.azure.com/agentpool": pool, "node.kubernetes.io/instance-type": size}},
                "status": {"capacity": {"cpu": cpu, "memory": mem}, "allocatable": {"cpu": alloc_cpu, "memory": alloc_mem},
                           "conditions": [{"type": "Ready", "status": "True"}]}}

    nodes = [node("n1", "user", "Standard_D8s_v5", "8", "32Gi", "7820m", "28Gi"),
             node("n2", "user", "Standard_D8s_v5", "8", "32Gi", "7820m", "28Gi"),
             node("n3", "system", "Standard_D4s_v5", "4", "16Gi", "3860m", "12Gi")]
    profiles = {"user": {"autoscale": True, "min": 2, "max": 2, "count": 2, "vm_size": "Standard_D8s_v5"}}
    rows, _ = aks_checks.capacity(nodes, [], [], aks_checks.Thresholds(), profiles)
    user = next(r for r in rows if r["pool"] == "user")
    assert user["nodes"] == 2 and user["shapes"] == [{"vm_size": "Standard_D8s_v5", "cpu": 8000, "memory": 32768,
                                                      "alloc_cpu": 7820, "alloc_memory": 28672}]
    assert user["profile"] == profiles["user"] and user["max_nodes"] == 2 and user["can_grow"] is False
    system = next(r for r in rows if r["pool"] == "system")
    assert system["profile"] is None and system["can_grow"] is None  # the record didn't name it: not known
