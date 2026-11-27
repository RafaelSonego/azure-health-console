import json

from flask import Flask

from healthconsole.core import config
from healthconsole.core.explore import aks, kube, workload_detail as wd
from healthconsole.core.integrations import nr_data
from healthconsole.web import create_blueprint
from tests.conftest import SUB, FakeClient

CLUSTER = f"/subscriptions/{SUB}/resourceGroups/aks/providers/Microsoft.ContainerService/managedClusters/shop-aks"
GIB = 2**30


def deployment():
    container = {"name": "api", "image": "acr.example/api:1.2",
                 "resources": {"requests": {"cpu": "8", "memory": "4Gi"}, "limits": {"cpu": "9500m", "memory": "10Gi"}},
                 "env": [{"name": "JAVA_OPTS", "value": "-Xmx8g -Ddb.password=hunter2 -XX:+UseG1GC"},
                         {"name": "DB_URL", "value": "jdbc://user:hunter2@db"}],
                 "readinessProbe": {"httpGet": {"path": "/ready"}, "periodSeconds": 10},
                 "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}
    return {"metadata": {"name": "api", "namespace": "shop", "creationTimestamp": "2026-10-29T10:00:00Z",
                         "annotations": {"deployment.kubernetes.io/revision": "7"}},
            "spec": {"replicas": 20, "selector": {"matchLabels": {"app": "api"}},
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}},
                     "template": {"metadata": {"labels": {"app": "api"}},
                                  "spec": {"containers": [container], "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory"}}]}}},
            "status": {"readyReplicas": 20, "updatedReplicas": 20,
                       "conditions": [{"type": "Progressing", "reason": "NewReplicaSetAvailable", "lastUpdateTime": "2026-10-29T10:05:00Z"}]}}


HPA = {"metadata": {"name": "api-hpa", "namespace": "shop"},
       "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "api"}, "minReplicas": 13, "maxReplicas": 20,
                "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}},
                            {"type": "Resource", "resource": {"name": "memory", "target": {"type": "Utilization", "averageUtilization": 70}}}]},
       "status": {"currentReplicas": 20, "desiredReplicas": 20,
                  "currentMetrics": [{"type": "Resource", "resource": {"name": "cpu", "current": {"averageUtilization": 4}}},
                                     {"type": "Resource", "resource": {"name": "memory", "current": {"averageUtilization": 117}}}],
                  "conditions": [{"type": "ScalingLimited", "status": "True", "reason": "TooManyReplicas",
                                  "message": "the desired replica count is more than the maximum replica count"}]}}


def pod(name, rs, restarts=0):
    return {"metadata": {"name": name, "namespace": "shop", "ownerReferences": [{"kind": "ReplicaSet", "name": rs}]},
            "spec": {"nodeName": "node-1"},
            "status": {"phase": "Running", "startTime": "2026-11-20T00:00:00Z",
                       "containerStatuses": [{"name": "api", "ready": True, "restartCount": restarts,
                                              "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}} if restarts else {}}]}}


class Kube:
    """The Kubernetes API, answering by path; every path is checked by the real read-only guard first."""
    source = "kubeconfig"
    server = "https://k"

    def __init__(self, obj=None):
        self.obj, self.paths = obj, []

    def get(self, path, params=None):
        kube.check_kube_read("GET", path)
        self.paths.append(path)
        return self.obj

    def items(self, path, params=None):
        kube.check_kube_read("GET", path)
        self.paths.append(path)
        if path.endswith("/pods") and "metrics" not in path:
            return [pod("api-7d9f8-a1", "api-7d9f8", restarts=2), pod("api-worker-5c6b-x", "api-worker-5c6b")]
        if path.endswith("/replicasets"):
            return [{"metadata": {"name": "api-7d9f8", "namespace": "shop", "ownerReferences": [{"kind": "Deployment", "name": "api"}]}},
                    {"metadata": {"name": "api-worker-5c6b", "namespace": "shop", "ownerReferences": [{"kind": "Deployment", "name": "api-worker"}]}}]
        if path.endswith("/horizontalpodautoscalers"):
            return [HPA]
        if path.endswith("/events"):
            return [{"involvedObject": {"kind": "HorizontalPodAutoscaler", "name": "api-hpa"}, "type": "Normal", "reason": "SuccessfulRescale",
                     "lastTimestamp": "2026-11-23T10:00:00Z", "message": "New size: 20"},
                    {"involvedObject": {"kind": "Pod", "name": "api-7d9f8-a1"}, "type": "Normal", "reason": "Started",
                     "lastTimestamp": "2026-11-23T09:00:00Z"},
                    {"involvedObject": {"kind": "Pod", "name": "api-worker-5c6b-x"}, "type": "Warning", "reason": "Unhealthy"},
                    {"involvedObject": {"kind": "Pod", "name": "billing-1"}, "type": "Warning", "reason": "BackOff"}]
        if "metrics.k8s.io" in path:
            return [{"metadata": {"name": "api-7d9f8-a1", "namespace": "shop"}, "containers": [{"name": "api", "usage": {"cpu": "320m", "memory": "4800Mi"}}]}]
        if path == "/api/v1/nodes":
            return [{"metadata": {"name": "node-1", "labels": {"topology.kubernetes.io/zone": "canadacentral-1", "agentpool": "apps"}}}]
        return []


def test_the_hpa_says_which_metric_holds_it_up_and_how_many_pods_each_asks_for(monkeypatch):
    k = Kube(deployment())
    monkeypatch.setattr(aks, "_connect", lambda *a: (k, None))
    out = wd.read(FakeClient(), CLUSTER, "shop", "Deployment", "api")
    h = out["hpa"]
    assert (h["decides"], h["wants"], h["limited"]) == ("memory", 34, True)  # ceil(20 × 117 / 70); cpu alone asks for 2
    assert [m["asks"] for m in h["metrics"]] == [2, 34] and h["conditions"][0]["reason"] == "TooManyReplicas"
    assert any("held there by memory" in f["problem"] for f in out["findings"])
    assert [p["pod"] for p in out["pods"]] == ["api-7d9f8-a1"]  # the selector's neighbour isn't its pod
    assert out["pods"][0]["zone"] == "canadacentral-1" and out["pods"][0]["last_exit"]["reason"] == "OOMKilled"
    assert [e["reason"] for e in out["events"]] == ["SuccessfulRescale", "Started"]  # its HPA and pod, not api-worker's
    assert out["overview"]["revision"] == "7" and out["overview"]["strategy"]["rollingUpdate"]["maxSurge"] == "25%"


def test_containers_show_requests_against_use_and_only_the_runtimes_memory_flags(monkeypatch):
    k = Kube(deployment())
    monkeypatch.setattr(aks, "_connect", lambda *a: (k, None))
    out = wd.read(FakeClient(), CLUSTER, "shop", "Deployment", "api")
    [c] = out["containers"]
    assert c["cpu"]["request"] == 8000 and c["cpu"]["of_request"] == 4 and c["memory"]["of_request"] == 117
    assert [f["flag"] for f in c["memory_flags"]] == ["-Xmx8g"] and c["heap_max"] == 8192
    assert any("above the memory request" in n for n in c["notes"]) and any("emptyDir in memory" in n for n in c["notes"])
    assert "hunter2" not in json.dumps(out)  # no env value leaves the machine's memory beyond the matched flag
    assert all(p.startswith(("/apis/", "/api/")) for p in k.paths) and "/api/v1/namespaces/shop/events" in k.paths


def test_memory_flags_take_only_known_settings():
    flags = wd.memory_flags({"env": [{"name": "JAVA_TOOL_OPTIONS", "valueFrom": {"configMapKeyRef": {"name": "x", "key": "y"}}},
                                     {"name": "NODE_OPTIONS", "value": "--max-old-space-size=1536 --require ./secret.js"},
                                     {"name": "GOMEMLIMIT", "value": "6GiB"}, {"name": "DOTNET_GCHeapHardLimit", "value": "token=abc"}],
                             "args": ["-XX:MaxRAMPercentage=75.0", "--api-key=abc"]})
    assert [f["flag"] for f in flags] == ["", "--max-old-space-size=1536", "GOMEMLIMIT=6GiB", "-XX:MaxRAMPercentage=75.0"]
    assert "not read" in flags[0]["note"] and wd.heap_max_mib(flags[3:], 4096) == 3072


def test_a_missing_workload_or_cluster_says_why(monkeypatch):
    monkeypatch.setattr(aks, "_connect", lambda *a: (Kube(None), None))
    assert "no Deployment api in shop" in wd.read(FakeClient(), CLUSTER, "shop", "Deployment", "api")["reason"]
    monkeypatch.setattr(aks, "_connect", lambda *a: (None, "The cluster's API server isn't reachable from this machine."))
    assert not wd.read(FakeClient(), CLUSTER, "shop", "Deployment", "api")["available"]


def ci_rows(rows):
    def answer(body):
        cols = ["Container", "CounterName", "recent", "p95", "p99", "peak"]
        return {"tables": [{"columns": [{"name": c} for c in cols], "rows": [[r[c] for c in cols] for r in rows]}]}
    return answer


def test_history_gives_the_request_each_hpa_target_needs_at_the_30_day_peak():
    rows = [{"Container": "api", "CounterName": "memoryWorkingSetBytes", "recent": True, "p95": 5.1e9, "p99": 5.2e9, "peak": 5.3e9},
            {"Container": "api", "CounterName": "memoryWorkingSetBytes", "recent": False, "p95": 4.9e9, "p99": 5.0e9, "peak": 5.6e9},
            {"Container": "api", "CounterName": "cpuUsageNanoCores", "recent": True, "p95": 0.4e9, "p99": 0.8e9, "peak": 1.5e9}]
    client = FakeClient(posts={"/logs": ci_rows(rows)})
    link = {"cluster_id": CLUSTER, "namespace": "shop", "kind": "Deployment", "name": "api"}
    out = wd.history(client, link, {"cpu": 70, "memory": 70})
    [c] = out["usage"]["containers"]
    assert c["memory"]["max_30d"] == 5.6e9 and c["memory"]["max_7d"] == 5.3e9 and c["cpu"]["p99"] == 0.8
    assert round(out["needed"]["memory"]["need"] / 1e9, 1) == 8.0 and round(out["needed"]["cpu"]["need"], 2) == 1.14  # CPU from its 7-day p99
    assert "No monitoring tool" in out["runtime"]["reason"] and "ControllerName matches regex" in out["usage"]["queries"][0]["query"]


class Tool:
    label = "Tool"

    def workload_usage(self, cluster, namespace, kind, name):
        assert cluster == "shop-aks"
        return {"found": True, "containers": [{"container": "api", "cpu": {"max_30d": 1.0}, "memory": {"max_30d": 2 * GIB}}],
                "queries": [{"account": 1, "nrql": "SELECT …"}]}

    def runtime_memory(self, app):
        assert app == "orders-prod"
        return {"found": True, "heap_used": 975, "heap_max": 8192, "process": 2600, "heap_daily": [["t", 960], ["t", 990]],
                "queries": [{"account": 1, "nrql": "SELECT …"}]}


def test_history_falls_back_to_the_tool_and_finds_the_app_by_its_words():
    client = FakeClient(posts={"/logs": ci_rows([])})
    link = {"cluster_id": CLUSTER, "namespace": "shop", "kind": "Deployment", "name": "orders"}
    apps = ["orders-prod", "orders-worker-prod", "cart-prod", "search-prod", "users-prod"]  # -prod: the convention
    out = wd.history(client, link, {"memory": 50}, Tool(), ("workload usage", "runtime memory"), lambda: apps)
    assert out["usage"]["source"] == "Tool" and out["needed"]["memory"]["need"] == 4 * GIB
    assert out["runtime"]["app"] == "orders-prod" and out["runtime"]["heap_used"] == 975 and out["notes"]
    assert wd.runtime_app(["orders-prod", "cart", "search", "users"], "orders") == ("", "")  # prod isn't common here
    out = wd.history(client, link, {}, Tool(), ("runtime memory",), lambda: ["billing-prod"])
    assert not out["runtime"].get("app") and "can't be read" in out["runtime"]["reason"]  # no app has its name


class FakeNr:
    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def nrql_many(self, account_id, nrqls):
        self.sent.append(nrqls)
        return self.answers.pop(0)


def test_new_relic_answers_usage_and_runtime_memory():
    nr = nr_data.NewRelicProvider({"name": "NR", "type": "newrelic", "account_ids": [1], "tenant": "t"},
                                  client=FakeNr([[[{"facet": "api", "percentile.cpuUsedCores": {"95": 0.4, "99": 0.8}, "max.cpuUsedCores": 1.5,
                                                    "percentile.memoryWorkingSetBytes": {"95": 5e9, "99": 5.2e9}, "max.memoryWorkingSetBytes": 5.3e9}],
                                                  [{"facet": "api", "max.cpuUsedCores": 6.7, "max.memoryWorkingSetBytes": 5.4e9}]],
                                                 [[{"facet": "Memory/Heap/Used", "average.newrelic.timeslice.value": 975.0},
                                                   {"facet": "Memory/Heap/Max", "average.newrelic.timeslice.value": 8192.0}],
                                                  [{"beginTimeSeconds": 1795000000, "average.newrelic.timeslice.value": 970.0}]]]))
    usage = nr.workload_usage("shop-aks", "shop", "Deployment", "api", refresh=True)
    assert usage["found"] and usage["containers"][0]["cpu"] == {"p95": 0.4, "p99": 0.8, "max_7d": 1.5, "max_30d": 6.7}
    assert "deploymentName = 'api'" in nr.client.sent[0][0]
    runtime = nr.runtime_memory("api-prod", refresh=True)
    assert runtime["found"] and (runtime["heap_used"], runtime["heap_max"]) == (975.0, 8192.0) and runtime["heap_daily"][0][1] == 970.0


def test_routes_check_the_workload_and_the_targets(monkeypatch, tmp_path):
    monkeypatch.setattr(aks, "_connect", lambda *a: (Kube(deployment()), None))
    app = Flask("test", static_folder=None)
    file = tmp_path / "c.yaml"
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(posts={"/logs": ci_rows([])}), config.load(file), config_file=file, authorize=lambda: None))
    http = app.test_client()
    workload = {"cluster": CLUSTER, "namespace": "shop", "kind": "Deployment", "workload": "api"}
    assert http.get("/api/workload/detail", query_string={**workload, "kind": "Job"}).status_code == 400
    assert http.get("/api/workload/detail", query_string=workload).get_json()["hpa"]["decides"] == "memory"
    assert http.get("/api/workload/history", query_string={**workload, "target_cpu": "70%"}).status_code == 400
    assert http.get("/api/workload/history", query_string={**workload, "target_cpu": "70"}).get_json()["usage"]["found"] is False
