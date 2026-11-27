from datetime import datetime, timedelta, timezone

from flask import Flask

from healthconsole.core import config
from healthconsole.core.explore import aks_checks, runtime_caps, workload_memory as wm
from healthconsole.web import create_blueprint
from tests.conftest import SUB, FakeClient

CLUSTER = f"/subscriptions/{SUB}/resourceGroups/aks/providers/Microsoft.ContainerService/managedClusters/shop-aks"
MIB = 2**20
T0 = datetime(2026, 11, 20, tzinfo=timezone.utc)


def hour(n):
    return (T0 + timedelta(hours=n)).isoformat().replace("+00:00", "Z")


def table(cols, rows):
    return {"tables": [{"columns": [{"name": c} for c in cols], "rows": rows}]}


# ---- HPA: how long at max, from Kubernetes alone

def hpa(current, maximum, limited=None):
    conditions = [{"type": "ScalingLimited", "status": "True", "reason": "TooManyReplicas", "lastTransitionTime": limited}] if limited else []
    return {"metadata": {"name": "api-hpa"}, "spec": {"minReplicas": 2, "maxReplicas": maximum,
            "scaleTargetRef": {"kind": "Deployment", "name": "api"}}, "status": {"currentReplicas": current, "conditions": conditions}}


def test_at_max_since_comes_from_the_scaling_limited_condition():
    assert aks_checks.at_max_since(hpa(50, 50, "2026-11-24T08:00:00Z")) == "2026-11-24T08:00:00Z"
    assert aks_checks.at_max_since(hpa(30, 50, "2026-11-24T08:00:00Z")) is None  # not at max now
    assert aks_checks.at_max_since(hpa(50, 50)) is None  # at max, but the condition doesn't say since when
    assert aks_checks.since_text("2026-11-24T08:00:00Z", datetime(2026, 11, 27, 12, 30, tzinfo=timezone.utc)) == "3 d 4 h"
    assert aks_checks.since_text("2026-11-27T10:00:00Z", datetime(2026, 11, 27, 12, 30, tzinfo=timezone.utc)) == "2 h 30 min"
    assert aks_checks.describe_hpa(hpa(50, 50, "2026-11-24T08:00:00Z"))["at_max_since"] == "2026-11-24T08:00:00Z"
    found = aks_checks.analyze_hpa(hpa(50, 50, "2026-11-24T08:00:00Z"), [], aks_checks.Thresholds(), "shop", "Deployment/api")
    assert any(f["problem"].startswith("At the maximum 50 replicas for ") for f in found)


def test_memory_of_request_adds_up_every_pod_against_its_request():
    containers = [{"name": "api", "resources": {"requests": {"memory": "1Gi"}}}, {"name": "sidecar", "resources": {}}]
    usage = {("shop", "Deployment", "api", "api"): [("p1", 0, 512), ("p2", 0, 1024)], ("shop", "Deployment", "api", "sidecar"): [("p1", 0, 99)]}
    w = {"ns": "shop", "kind": "Deployment", "name": "api"}
    assert aks_checks.memory_of_request(containers, usage, w) == 75  # 1536 of 2 × 1024; the sidecar has no request
    assert aks_checks.memory_of_request(containers, {}, w) is None


# ---- runtime cap against the limit, per runtime

def container(env=(), args=(), limit="2Gi"):
    return {"name": "app", "env": [{"name": k, "value": v} for k, v in env], "args": list(args),
            "resources": {"limits": {"memory": limit}} if limit else {}}


def test_a_runtime_cap_is_read_for_java_node_go_and_dotnet_and_none_is_a_fact_too():
    cap = lambda c: runtime_caps.runtime_cap(c, 2048)  # noqa: E731
    java = cap(container([("JAVA_OPTS", "-Xmx1536m -Dsecret=hidden")]))
    assert (java["runtime"], java["cap_mib"], java["outside_mib"], java["of_limit"]) == ("Java", 1536, 512, 75)
    default = cap(container([("JAVA_TOOL_OPTIONS", "-Dfoo=bar")]))
    assert default["runtime"] == "Java" and default["cap_mib"] == 512 and "default, 25%" in default["how"]
    assert cap(container([("NODE_OPTIONS", "--max-old-space-size=1024")]))["cap_mib"] == 1024
    assert cap(container([("GOMEMLIMIT", "1GiB")]))["cap_mib"] == 1024
    assert cap(container([("DOTNET_GCHeapHardLimit", "0x40000000")]))["cap_mib"] == 1024
    assert cap(container([("DOTNET_GCHeapHardLimitPercent", "0x4B")]))["cap_mib"] == 1536  # 0x4B = 75%
    none = cap(container([("LOG_LEVEL", "info")]))
    assert (none["runtime"], none["cap_mib"], none["limit_mib"]) == ("", None, 2048)
    assert runtime_caps.workload_caps([container([("NODE_OPTIONS", "--max-old-space-size=512")])])[0]["of_limit"] == 25
    assert "secret" not in str(java)  # only the flag is kept, never the rest of a value


# ---- memory shape

def test_growth_is_the_slope_of_a_pods_own_hours_and_needs_enough_of_them():
    climbing = [[hour(h), (500 + 10 * h) * MIB] for h in range(12)]
    assert wm.growth_per_hour(climbing) == 10.0
    assert wm.growth_per_hour(climbing[:5]) is None  # too few hours to call it
    assert wm.growth_per_hour([[hour(h), 700 * MIB] for h in range(12)]) == 0.0


def test_before_and_after_the_rollout_compare_memory_per_pod_pods_and_restarts():
    old = {"points": [[hour(h), 400 * MIB] for h in range(0, 24)], "restarts": [], "oom": []}
    new = {"points": [[hour(h), 800 * MIB] for h in range(24, 48)], "restarts": [[hour(30), 2]], "oom": [hour(30)]}
    out = wm.around_rollout({"old": old, "new": new}, hour(24))
    assert out["before"] == {"memory_per_pod": 400, "pods": 1.0, "restarts": 0, "hours": 24}
    assert out["after"]["memory_per_pod"] == 800 and out["after"]["restarts"] == 2
    assert wm.around_rollout({"old": old}, hour(24)) is None  # nothing after it: no comparison
    assert wm.around_rollout({"old": old}, None) is None


def test_the_shape_lists_every_pod_and_charts_the_latest():
    pods = {f"p{i}": {"points": [[hour(h), (300 + i) * MIB] for h in range(i, i + 8)], "restarts": [], "oom": []} for i in range(50)}
    out = wm.shape(pods)
    assert len(out["pods"]) == 50 and len(out["chart"]) == wm.CHART_PODS
    assert out["pods"][0]["pod"] == "p49" and {c["pod"] for c in out["chart"]} == {f"p{i}" for i in range(10, 50)}


def ci_answer(mem_rows, life_rows):
    cols = ["part", "Name", "t", "mem", "restarts", "oom"]
    return lambda body: table(cols, [["mem", n, t, v, None, None] for n, t, v in mem_rows]
                              + [["life", n, t, None, r, o] for n, t, r, o in life_rows])


def test_container_insights_series_turn_a_rising_restart_count_into_restarts_and_ooms():
    client = FakeClient(posts={"/logs": ci_answer(
        [("api-1", hour(0), 500 * MIB), ("api-1", hour(1), 520 * MIB)],
        [("api-1", hour(0), 3, 0), ("api-1", hour(1), 5, 1), ("api-1", hour(2), 5, 1)])})
    out = wm.series_ci(client, CLUSTER, "shop", "Deployment", "api")
    assert out["pods"]["api-1"]["restarts"] == [[hour(1), 2]] and out["pods"]["api-1"]["oom"] == [hour(1)]
    sent = client.posted[0][1]["query"]
    assert "summarize v = avg(CounterValue) by Name, Container" in sent  # averaged per container before adding up
    assert "^api-[a-z0-9]{5,10}$" in sent


class Tool:
    label = "Tool"
    capabilities = ("workload usage", "cluster week")

    def workload_memory(self, cluster, namespace, kind, name):
        assert cluster == "shop-aks"
        return {"found": True, "pods": {"api-9": {"points": [[hour(h), 600 * MIB] for h in range(8)], "restarts": [], "oom": []}},
                "queries": [{"account": 1, "nrql": "SELECT …"}]}

    def cluster_week(self, cluster):
        return {"found": True, "workloads": [{"namespace": "shop", "workload": "api", "restarts": 4, "oom_pods": 1,
                                              "max_of_limit": 90, "avg_of_request": 60}],
                "hpas": [{"namespace": "shop", "hpa": "api-hpa", "at_max_percent": 70}], "queries": [{"account": 1, "nrql": "SELECT …"}]}


def test_without_container_insights_the_monitoring_tool_answers_both():
    empty = FakeClient(posts={"/logs": table(["part"], [])})
    workload = {"cluster_id": CLUSTER, "namespace": "shop", "kind": "Deployment", "name": "api"}
    out = wm.read(empty, workload, None, Tool(), Tool.capabilities)
    assert out["source"] == "Tool" and out["pods"][0]["now_mib"] == 600 and len(out["queries"]) == 2
    assert not wm.read(empty, workload, None, None, ())["found"]
    week = wm.cluster_week(empty, CLUSTER, Tool(), Tool.capabilities)
    assert week["source"] == "Tool" and week["hpas"][0]["at_max_percent"] == 70
    ci = FakeClient(posts={"/logs": table(["Namespace", "Workload", "restarts", "oom_pods"], [["shop", "api", 3, 1]])})
    assert wm.cluster_week(ci, CLUSTER, Tool(), Tool.capabilities)["source"] == "Container Insights"


def test_the_routes_check_what_they_are_given():
    client = FakeClient(posts={"/logs": table(["part"], [])})
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load("/nonexistent"), authorize=lambda: None))
    web = app.test_client()
    base = {"cluster": CLUSTER, "namespace": "shop", "kind": "Deployment", "workload": "api"}
    assert web.get("/api/workload/memory", query_string=base).status_code == 200
    assert web.get("/api/workload/memory", query_string={**base, "rollout": "yesterday"}).status_code == 400
    assert web.get("/api/workload/memory", query_string={**base, "kind": "Pod"}).status_code == 400
    assert web.get("/api/explore/memory-scaling", query_string={"id": CLUSTER}).status_code == 200
    assert web.get("/api/explore/memory-scaling", query_string={"id": "/not/a/cluster"}).status_code == 400


def test_new_relic_adds_a_pods_containers_per_hour_and_reads_the_clusters_week():
    from healthconsole.core.integrations import nr_data
    from tests.test_workload_detail import FakeNr
    b = int(T0.timestamp())
    nr = nr_data.NewRelicProvider({"name": "NR", "type": "newrelic", "account_ids": [1], "tenant": "t"}, client=FakeNr([
        [[{"facet": ["api-1", "app"], "beginTimeSeconds": b, "average.memoryWorkingSetBytes": 500 * MIB},
          {"facet": ["api-1", "proxy"], "beginTimeSeconds": b, "average.memoryWorkingSetBytes": 100 * MIB},
          {"facet": ["api-1", "app"], "beginTimeSeconds": b + 3600, "average.memoryWorkingSetBytes": None}],
         [{"facet": "api-1", "podName": "api-1", "beginTimeSeconds": b, "restarts": 2.0, "oom": 3}]],
        [[{"facet": ["shop", "api", None, None], "restarts": 4.0, "oomPods": 1, "maxOfLimit": 91.6, "avgOfRequest": 60.2},
          {"facet": ["kube-system", None, None, "ztunnel"], "restarts": 0.0, "oomPods": 0, "maxOfLimit": None, "avgOfRequest": None}],
         [{"facet": ["shop", "api-hpa"], "atMax": 70.4}]]]))
    memory = nr.workload_memory("shop-aks", "shop", "Deployment", "api", refresh=True)
    pod = memory["pods"]["api-1"]
    assert pod["points"] == [[hour(0), 600 * MIB]]  # two containers added up; the empty hour dropped
    assert pod["restarts"] == [[hour(0), 2]] and pod["oom"] == [hour(0)]
    assert "deploymentName = 'api'" in nr.client.sent[0][0] and "TIMESERIES 1 hour" in nr.client.sent[0][0]
    week = nr.cluster_week("shop-aks", refresh=True)
    assert week["workloads"][0] == {"namespace": "shop", "workload": "api", "restarts": 4, "oom_pods": 1, "max_of_limit": 92, "avg_of_request": 60}
    assert week["workloads"][1]["workload"] == "ztunnel" and week["hpas"] == [{"namespace": "shop", "hpa": "api-hpa", "at_max_percent": 70}]
