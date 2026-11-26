from healthconsole.core import checks, health
from healthconsole.core.arm import ArmError
from healthconsole.core.explore import aks
from healthconsole.core.model import HEALTHY, INFO, WARNING, Node
from tests.conftest import FakeClient, rid

CLUSTER = rid("rg", "Microsoft.ContainerService/managedClusters", "shop")


def _series(namespace, phase, values):
    return {"metadatavalues": [{"name": {"value": "namespace"}, "value": namespace},
                               {"name": {"value": "phase"}, "value": phase}],
            "data": [{"average": v} for v in values]}


def _metrics(now, history):
    """`now`: the 15-minute read (namespace, phase, pods); `history`: Failed per namespace, one value an hour."""
    def answer(params):
        if "phase eq 'Failed'" in params["$filter"]:
            assert params["interval"] == "PT1H"
            return {"value": [{"timeseries": [_series(ns, "Failed", v) for ns, v in history.items()]}]}
        return {"value": [{"timeseries": [_series(ns, phase, [pods]) for ns, phase, pods in now]}]}
    return FakeClient(gets={"metrics": answer})


def _signals(client, hours=24):
    return {s.name: s for s in health._aks_signals(client, Node(id=CLUSTER, name="shop", type="x", display_type="AKS", hop=0,
                                                                 subscription_id="s"), hours)}


def test_failed_pods_there_all_day_are_left_over_not_a_warning():
    client = _metrics([("shop", "Running", 10), ("shop", "Failed", 2)], {"shop": [2] * 24})
    found = _signals(client)
    assert found["Failed pods"].status == INFO and "none in the last 24 h" in found["Failed pods"].detail
    assert found["pods"].status == HEALTHY and "newly failed" in found["pods"].detail


def test_a_pod_that_failed_within_the_window_is_a_warning_counting_only_the_new_ones():
    client = _metrics([("shop", "Failed", 3), ("jobs", "Failed", 1)], {"shop": [2] * 20 + [3] * 4, "jobs": [1] * 24})
    failed = _signals(client)["Failed pods"]
    assert failed.status == WARNING and failed.value == 1
    assert failed.detail == "4 failed, 1 in the last 24 h: shop (1)"


def test_hours_without_data_count_as_none_so_the_pods_are_new():
    client = _metrics([("shop", "Failed", 2)], {"shop": [None] * 22 + [2, 2]})
    assert _signals(client)["Failed pods"].status == WARNING


def test_unreadable_history_keeps_every_failed_pod_a_warning():
    def answer(params):
        if "phase eq 'Failed'" in params["$filter"]:
            raise ArmError("throttled")
        return {"value": [{"timeseries": [_series("shop", "Failed", [2])]}]}
    rows = aks.failed_split(FakeClient(gets={"metrics": answer}), CLUSTER, [{"namespace": "shop", "phase": "Failed", "pods": 2}])
    assert rows == [{"namespace": "shop", "phase": "Failed", "pods": 2, "new": 2}]


def test_pending_pods_stay_a_warning_whatever_the_history():
    client = _metrics([("shop", "Pending", 1)], {})
    assert _signals(client)["Pending pods"].status == WARNING


def test_the_aks_sweep_splits_left_over_from_new_the_same_way():
    cluster = {"id": CLUSTER, "name": "shop", "resourceGroup": "rg", "properties": {
        "currentKubernetesVersion": "1.31", "powerState": {"code": "Running"}, "provisioningState": "Succeeded"}}
    client = _metrics([("shop", "Failed", 2), ("jobs", "Failed", 1)], {"shop": [2] * 24, "jobs": [0] * 23 + [1]})
    client.query_handlers = [("managedclusters", lambda kql: [cluster])]
    row = checks.aks_fleet(client, ["s"], leftover_hours=24)["rows"][0]
    assert row["status"] == WARNING
    assert row["problems"] == ["1 failed in jobs", "2 failed in shop, none in the last 24 h: left over"]

    client = _metrics([("shop", "Failed", 2)], {"shop": [2] * 24})
    client.query_handlers = [("managedclusters", lambda kql: [cluster])]
    assert checks.aks_fleet(client, ["s"])["rows"][0]["status"] == HEALTHY


def _pod(name, phase, ready=(True,), restarts=0, owner=None, reason="", finished=None, created="2026-11-01T00:00:00Z"):
    term = {"terminated": {"reason": reason, "finishedAt": finished}} if finished else {}
    return {"metadata": {"namespace": "shop", "name": name, "creationTimestamp": created,
                         "ownerReferences": [owner] if owner else []},
            "spec": {"nodeName": "node-1", "containers": [{"name": f"c{i}"} for i in range(len(ready))]},
            "status": {"phase": phase, "containerStatuses": [{"name": f"c{i}", "ready": r, "restartCount": restarts,
                                                              "state": term} for i, r in enumerate(ready)]}}


def test_pod_rows_list_every_pod_problems_first_with_its_workload():
    from healthconsole.core.explore import aks_checks
    pods = [_pod("web-1", "Running", owner={"kind": "ReplicaSet", "name": "web-5d8f9"}),
            _pod("web-2", "Running", ready=(True, False), restarts=3),
            _pod("batch-x", "Failed", ready=(False,), owner={"kind": "Job", "name": "batch"}, reason="Error",
                 finished="2026-11-02T00:00:00Z"),
            _pod("kube-proxy", "Running")]
    pods[3]["metadata"]["namespace"] = "kube-system"
    owner = {("shop", "web-1"): ("shop", "Deployment", "web")}
    rows = aks_checks.pod_rows(pods, owner, lambda ns: ns != "kube-system")
    assert [r["pod"] for r in rows] == ["batch-x", "web-2", "web-1"]
    failed, not_ready, web = rows
    assert failed["reason"] == "Error" and failed["since"] == "2026-11-02T00:00:00Z" and failed["owner"] == "Job/batch"
    assert (not_ready["ready"], not_ready["containers"], not_ready["restarts"]) == (1, 2, 6)
    assert web["workload"] == "Deployment/web" and web["node"] == "node-1"


def test_job_rows_say_how_each_job_ended_failed_first():
    from healthconsole.core.explore import aks_checks

    def job(name, conditions=(), active=0, cron=None, started="2026-11-20T10:00:00Z"):
        return {"metadata": {"namespace": "shop", "name": name,
                             "ownerReferences": [{"kind": "CronJob", "name": cron}] if cron else []},
                "status": {"conditions": [{"type": c, "status": "True"} for c in conditions], "active": active,
                           "startTime": started, "succeeded": 1 if "Complete" in conditions else 0,
                           "failed": 2 if "Failed" in conditions else 0}}
    rows = aks_checks.job_rows([job("ok", ["Complete"], cron="nightly"), job("now", active=1), job("bad", ["Failed"]),
                                job("ok-older", ["Complete"], started="2026-11-19T10:00:00Z")], lambda ns: True)
    assert [(r["job"], r["state"]) for r in rows] == [("bad", "Failed"), ("now", "Running"), ("ok", "Complete"),
                                                       ("ok-older", "Complete")]
    assert rows[2]["cronjob"] == "nightly" and rows[0]["failed"] == 2


def test_clusters_that_could_not_be_read_sort_after_healthy_ones():
    def cluster(name):
        return {"id": rid("rg", "Microsoft.ContainerService/managedClusters", name), "name": name, "resourceGroup": "rg",
                "properties": {"powerState": {"code": "Running"}, "provisioningState": "Succeeded"}}

    def answer(path, *args, **kwargs):
        if "/a-unread/" in path:
            raise ArmError("throttled")
        return {"value": []}
    client = FakeClient(query_handlers=[("managedclusters", lambda kql: [cluster("a-unread"), cluster("b-fine")])])
    client.get = answer
    rows = checks.aks_fleet(client, ["s"])["rows"]
    assert [(r["name"], r["status"]) for r in rows] == [("b-fine", HEALTHY), ("a-unread", "unknown")]
