from datetime import datetime, timedelta, timezone

import pytest
import requests
from flask import Flask

from tests.conftest import SUB, FakeClient
from healthconsole.core import config
from healthconsole.core.explore import aks, kube
from healthconsole.web import create_blueprint

AKS = f"/subscriptions/{SUB}/resourceGroups/shop/providers/Microsoft.ContainerService/managedClusters/shop-aks"
AT = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
AT_Z = AT.strftime("%Y-%m-%dT%H:%M:%SZ")
MIB = 1048576
NODE = {"NodeMemBytes": 8000 * MIB, "NodeAllocBytes": 16000 * MIB}
KILL = {"KilledAt": AT_Z, "Namespace": "shop", "Pod": "orders-7df-x1", "Container": "orders", "Node": "aks-pool-0",
        "OwnerKind": "ReplicaSet", "Owner": "orders-7df"}


def cause(**row):
    return aks.oom_cause(row)[:2]


def test_the_kernels_message_decides_container_or_node():
    assert cause(Kernel="Memory cgroup out of memory: Killed process 1 (java)", **NODE) == ("container", True)
    assert cause(Kernel="Out of memory: Killed process 1 (java)", MemBytes=10, LimitBytes=100 * MIB) == ("node", True)
    assert cause(SystemOom=1) == ("node", True)


def test_without_the_kernels_message_memory_readings_suggest_a_cause():
    assert cause(MemBytes=250 * MIB, LimitBytes=256 * MIB, **NODE) == ("container", False)
    assert cause(MemBytes=100 * MIB, LimitBytes=256 * MIB, NodeMemBytes=15800 * MIB, NodeAllocBytes=16000 * MIB) == ("node", False)
    # no limit set: Container Insights reports the node's capacity as the limit
    assert cause(MemBytes=100 * MIB, LimitBytes=16384 * MIB, **NODE) == ("node", False)
    spike = aks.oom_cause({"MemBytes": 100 * MIB, "LimitBytes": 256 * MIB, **NODE})
    assert spike[0] == "unknown" and "between readings" in spike[2]
    assert aks.oom_cause({})[0] == "unknown"


def test_each_kill_is_a_row_with_its_pod_node_memory_and_cause():
    [row] = aks.oom_rows([{**KILL, "MemBytes": 250 * MIB, "LimitBytes": 256 * MIB, **NODE,
                           "Kernel": "Memory cgroup out of memory: Killed process 1 (java)"}])
    assert row == {"killed_at": AT_Z, "namespace": "shop", "pod": "orders-7df-x1", "container": "orders",
                   "node": "aks-pool-0", "owner": "ReplicaSet orders-7df", "workload": "ReplicaSet/orders-7df", "memory_mib": 250, "limit_mib": 256,
                   "no_limit": False, "node_pct": 50, "cause": "container", "sure": True, "why": row["why"],
                   "kernel": "Memory cgroup out of memory: Killed process 1 (java)"}
    [unlimited] = aks.oom_rows([{**KILL, "MemBytes": 1, "LimitBytes": 16384 * MIB, **NODE}])
    assert unlimited["no_limit"] and unlimited["limit_mib"] is None


def test_the_look_back_counts_kills_by_when_they_happened(monkeypatch):
    sent = []

    def logs(client, cluster_id, query, timespan="PT1H"):
        sent.append(query)
        return [KILL] if "OOMKilled" in query else [{"x": 1}]
    monkeypatch.setattr(aks, "resource_logs", logs)
    out = aks.container_insights(None, AKS, 168)
    oom = next(q for q in sent if "OOMKilled" in q)
    # a pod keeps OOMKilled as its last state for good, so the kill's own time is what's filtered
    assert "KilledAt > ago(168h)" in oom and "LastState.finishedAt" in oom and "OOMKilling" in oom
    assert out["oomkilled"][0]["pod"] == "orders-7df-x1" and out["oomkilled"][0]["cause"] == "unknown"


def test_investigate_reads_one_pods_kills_around_the_request(monkeypatch):
    sent = []
    monkeypatch.setattr(aks, "resource_logs", lambda c, rid, kql, span: sent.append((kql, span)) or [KILL])
    out = aks.oom_kills(FakeClient(), AKS, "orders-7df-x1", AT)
    assert out["kills"][0]["node"] == "aks-pool-0"
    kql, span = sent[0]
    assert 'Name == "orders-7df-x1"' in kql and "KilledAt between (datetime(" in kql and "ago(" not in kql
    assert "/" in span
    with pytest.raises(ValueError):
        aks.oom_query(pod="bad'pod", around=AT)


class Kube:
    def __init__(self, text="", error=None):
        self.text, self.error, self.params = text, error, None

    def get(self, path, params=None, raw=False):
        self.params = params
        if self.error:
            raise self.error
        return self.text


def test_logs_of_the_killed_instance_come_from_previous(monkeypatch):
    inside = (AT + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    k = Kube(f"{inside}.1Z java.lang.OutOfMemoryError")
    monkeypatch.setattr(kube, "client_for", lambda *a: k)
    data = aks.logs(FakeClient(), AKS, "shop", "orders-1", "orders", around=AT, spread=5, previous=True)
    assert data["lines"] == [f"{inside}.1Z java.lang.OutOfMemoryError"] and "stopped instance" in data["source"]
    assert k.params["previous"] == "true" and k.params["container"] == "orders"


def test_a_gone_instance_falls_back_to_container_insights(monkeypatch):
    k = Kube(error=requests.HTTPError("400 previous terminated container not found"))
    monkeypatch.setattr(kube, "client_for", lambda *a: k)
    sent = []
    monkeypatch.setattr(aks, "resource_logs", lambda client, rid, kql, span: sent.append(kql) or [])
    data = aks.logs(FakeClient(), AKS, "shop", "orders-1", around=AT, spread=5, previous=True)
    assert data["source"] == "container insights" and "within 5 minutes" in data["error"] and sent
    with pytest.raises(requests.HTTPError):  # without previous, an API error isn't hidden
        aks.logs(FakeClient(), AKS, "shop", "orders-1", around=AT)


@pytest.fixture
def http():
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"), authorize=lambda: None))
    return app.test_client()


def test_routes_pass_the_kill_through(http, monkeypatch):
    seen = {}
    monkeypatch.setattr(aks, "logs", lambda *a, **kw: seen.update(kw) or {"lines": []})
    monkeypatch.setattr(aks, "oom_kills", lambda client, rid, pod, at, aks_settings=None: {"kills": [{"pod": pod, "at": at.isoformat()}],
                                                                                         "settings": sorted(aks_settings)})
    assert http.get(f"/api/explore/logs?id={AKS}&namespace=shop&pod=p&at={AT_Z}&previous=1&spread=5").status_code == 200
    assert seen["previous"] and seen["spread"] == 5 and seen["around"] == AT
    assert http.get(f"/api/explore/logs?id={AKS}&namespace=shop&pod=p").status_code == 200
    assert seen["around"] is None and not seen["previous"] and seen["spread"] == 2
    assert http.get(f"/api/explore/logs?id={AKS}&namespace=shop&spread=99").status_code == 400
    reply = http.get(f"/api/investigate/pod-oom?id={AKS}&pod=orders-1&at={AT_Z}").get_json()
    assert reply["kills"][0]["pod"] == "orders-1" and "oom_near_limit_percent" in reply["settings"]  # the route passes the settings
    assert http.get(f"/api/investigate/pod-oom?id={AKS}&pod=orders-1").status_code == 400


# ---------------------------------------------------------------- New Relic as a second source

from healthconsole.core.integrations import monitors, nr_data  # noqa: E402
from healthconsole.core.integrations.newrelic import NewRelicClient  # noqa: E402
from healthconsole.core.integrations.provider import OOM_KILLS  # noqa: E402

KILL_MS = int(AT.timestamp() * 1000)
KERNEL = "Memory cgroup out of memory: Killed process 26684 (node) anon-rss:2036248kB"


def restart(pod, node, after_s, **extra):
    return {"timestamp": KILL_MS + after_s * 1000, "podName": pod, "containerName": "api", "namespaceName": "shop",
            "nodeName": node, "deploymentName": "api-stable", **extra}


def test_each_node_event_is_matched_to_the_container_that_restarted_just_after():
    events = [{"timestamp": KILL_MS, "node": "n1", "reason": "OOMKilling", "message": KERNEL},
              {"timestamp": KILL_MS + 4000, "node": "n1", "reason": "SystemOOM", "message": "System OOM encountered"},
              {"timestamp": KILL_MS + 3600_000, "node": "n2", "reason": "OOMKilling", "message": KERNEL}]
    restarts = [restart("other", "n2", 9), restart("api-1", "n1", 9), restart("api-2", "n1", 600)]
    kills = nr_data._oom_match(events, restarts)
    assert [(k[1], (k[3] or {}).get("podName")) for k in kills] == [("n2", None), ("n1", "api-1")]
    assert len(kills[1][2]["also"]) == 1  # the SystemOOM 4 s later is the same kill


class FakeNr:
    def __init__(self, clusters, events, restarts, memory):
        self.answers = [[[{"clusters": clusters}], events, restarts], memory]
        self.sent = []

    def nrql_many(self, account_id, nrqls):
        self.sent.append(nrqls)
        return self.answers[len(self.sent) - 1]


def provider(fake):
    return nr_data.NewRelicProvider({"name": "NR", "type": "newrelic", "account_ids": [1], "tenant": "t"}, client=fake)


def test_new_relic_gives_the_kill_its_pod_memory_and_cause():
    fake = FakeNr(["shop-aks"], [{"timestamp": KILL_MS, "node": "n1", "reason": "OOMKilling", "message": KERNEL}],
                  [restart("api-1", "n1", 9)],
                  [[{"mem": 8000 * MIB, "alloc": 16000 * MIB}], [{"mem": 1932 * MIB, "lim": 2048 * MIB}]])
    out = provider(fake).oom_kills("shop-aks", 24, refresh=True)
    [row] = aks.oom_rows(out["rows"])
    assert (row["pod"], row["node"], row["owner"], row["memory_mib"], row["limit_mib"]) == ("api-1", "n1", "Deployment api-stable", 1932, 2048)
    assert (row["cause"], row["sure"], row["node_pct"]) == ("container", True, 50)
    assert out["found"] and "clusterName = 'shop-aks'" in fake.sent[0][1] and "SINCE 24 hours ago" in fake.sent[0][2]
    assert len(fake.sent[1]) == 2 and "podName = 'api-1'" in fake.sent[1][1] and f"UNTIL {KILL_MS + 60000}" in fake.sent[1][0]
    assert OOM_KILLS in monitors.provider_for({"name": "NR", "type": "newrelic"}).capabilities


def test_no_limit_in_new_relic_reads_as_no_limit_and_an_unknown_cluster_says_so():
    fake = FakeNr(["shop-aks"], [{"timestamp": KILL_MS, "node": "n1", "reason": "OOMKilling", "message": ""}],
                  [restart("api-1", "n1", 9)], [[{"mem": 8000 * MIB, "alloc": 16000 * MIB}], [{"mem": 900 * MIB, "lim": None}]])
    [row] = aks.oom_rows(provider(fake).oom_kills("shop-aks", 24, refresh=True)["rows"])
    assert row["no_limit"] and row["cause"] == "node" and not row["sure"]
    missing = provider(FakeNr(["a-aks", "b-aks"], [], [], [])).oom_kills("shop-aks", 24, refresh=True)
    assert not missing["found"] and "no Kubernetes data for a cluster named shop-aks (it knows a-aks, b-aks)" in missing["notes"][0]
    with pytest.raises(nr_data.ProviderError):
        provider(FakeNr([], [], [], [])).oom_kills("bad' OR 1=1", 24)


def test_nrql_many_sends_one_aliased_request(monkeypatch):
    client = NewRelicClient({"name": "NR", "type": "newrelic"})
    seen = {}

    def query(text, variables=None):
        seen.update(text=text, variables=variables)
        return {"actor": {"account": {"q0": {"results": [{"a": 1}]}, "q1": {"results": []}}}}
    monkeypatch.setattr(client, "query", query)
    assert client.nrql_many(7, ["SELECT 1", "SELECT 2"]) == [[{"a": 1}], []]
    assert "q1: nrql(query: $q1)" in seen["text"] and seen["variables"] == {"id": 7, "q0": "SELECT 1", "q1": "SELECT 2"}
    assert client.nrql_many(7, []) == []


def test_route_reads_the_scopes_connection(tmp_path, monkeypatch):
    from tests.test_integrations import NR, tenant_app
    settings = config.from_data({"tenants": {"contoso.example": {"integrations": {"monitors": [NR]}}}})
    http, _ = tenant_app(tmp_path, settings)
    asked = {}

    class Fake:
        label, capabilities = "New Relic", (OOM_KILLS,)

        def oom_kills(self, name, hours, refresh=False):
            asked.update(name=name, hours=hours)
            return {"found": True, "rows": [{**KILL, "Kernel": KERNEL}], "queries": [{"account": 1, "nrql": "x"}], "notes": []}
    monkeypatch.setattr(monitors, "provider_for", lambda monitor: Fake())
    data = http.get(f"/api/explore/oom-kills-monitor?id={AKS}&ci_hours=168&connection=New Relic").get_json()
    assert data["available"] and data["rows"][0]["cause"] == "container" and asked == {"name": "shop-aks", "hours": 168}
    assert not http.get(f"/api/explore/oom-kills-monitor?id={AKS}").get_json()["available"]  # no connection picked
    assert http.get(f"/api/explore/oom-kills-monitor?id={AKS}&ci_hours=5&connection=New Relic").status_code == 400


# ---------------------------------------------------------------- every kill counted, nothing cut silently (2026-11-26)

def test_two_oom_killings_on_one_node_seconds_apart_are_two_kills():
    events = [{"timestamp": KILL_MS, "node": "n1", "reason": "OOMKilling", "message": KERNEL},
              {"timestamp": KILL_MS + 3000, "node": "n1", "reason": "OOMKilling", "message": KERNEL},
              {"timestamp": KILL_MS + 5000, "node": "n1", "reason": "SystemOOM", "message": "System OOM encountered"}]
    kills = nr_data._oom_match(events, [restart("api-1", "n1", 9), restart("api-2", "n1", 12)])
    assert len(kills) == 2  # the SystemOOM pairs with the second, never a second OOMKilling with the first
    assert len(kills[0][2]["also"]) == 1 and "also" not in kills[1][2]


class ManyKills:
    """30 kills on 30 nodes, each with its restart; memory answers for whatever is asked."""

    def __init__(self, n=30):
        self.events = [{"timestamp": KILL_MS + i * 60000, "node": f"n{i}", "reason": "OOMKilling", "message": KERNEL}
                       for i in range(n)]
        self.restarts = [restart(f"api-{i}", f"n{i}", 9 + i * 60) for i in range(n)]
        self.sent = []

    def nrql_many(self, account_id, nrqls):
        self.sent.append(nrqls)
        if len(self.sent) == 1:
            return [[{"clusters": ["shop-aks"]}], self.events, self.restarts]
        return [[{"mem": 900 * MIB, "lim": 2048 * MIB, "alloc": 16000 * MIB}] for _ in nrqls]


def test_every_kill_is_counted_and_listed_and_only_the_newest_get_memory_reads():
    fake = ManyKills(30)
    out = provider(fake).oom_kills("shop-aks", 24, refresh=True)
    assert out["total"] == 30 and len(out["rows"]) == 30 and out["detailed"] == nr_data.OOM_DETAIL_LIMIT
    assert len(fake.sent[1]) == 2 * nr_data.OOM_DETAIL_LIMIT  # two reads per detailed kill, no more
    assert out["rows"][0]["Pod"] == "api-29" and out["rows"][0]["MemBytes"] and out["rows"][-1]["MemBytes"] is None
    assert out["by_owner"] == [{"Namespace": "shop", "OwnerKind": "Deployment", "Owner": "api-stable",
                                "Pods": sorted(f"api-{i}" for i in range(30)), "Kills": 30}]
    assert "LIMIT MAX" in fake.sent[0][1] and "LIMIT MAX" in fake.sent[0][2]


def test_rows_past_the_list_limit_are_still_counted(monkeypatch):
    monkeypatch.setattr(nr_data, "OOM_ROW_LIMIT", 10)
    out = provider(ManyKills(30)).oom_kills("shop-aks", 24, refresh=True)
    assert len(out["rows"]) == 10 and out["total"] == 30 and out["by_owner"][0]["Kills"] == 30


def test_a_read_that_hits_new_relics_limit_says_so(monkeypatch):
    monkeypatch.setattr(nr_data, "NRQL_MAX", 30)
    out = provider(ManyKills(30)).oom_kills("shop-aks", 24, refresh=True)
    assert any("its most, 30 OOM events" in n and "shorter Look back" in n for n in out["notes"])
    assert any("30 container restarts" in n for n in out["notes"])


def test_the_route_returns_the_total_and_every_workloads_count(tmp_path, monkeypatch):
    from tests.test_integrations import NR, tenant_app
    settings = config.from_data({"tenants": {"contoso.example": {"integrations": {"monitors": [NR]}}}})
    http, _ = tenant_app(tmp_path, settings)

    class Fake:
        label, capabilities = "New Relic", (OOM_KILLS,)

        def oom_kills(self, name, hours, refresh=False):
            return {"found": True, "rows": [{**KILL, "Kernel": KERNEL}], "total": 412, "detailed": 1, "queries": [], "notes": [],
                    "by_owner": [{"Namespace": "shop", "OwnerKind": "ReplicaSet", "Owner": "orders-7df9c8b6d", "Pods": ["orders-7df-x1"],
                                  "Kills": 412}]}
    monkeypatch.setattr(monitors, "provider_for", lambda monitor: Fake())
    data = http.get(f"/api/explore/oom-kills-monitor?id={AKS}&connection=New Relic").get_json()
    assert data["total"] == 412 and data["by_workload"] == [{"namespace": "shop", "workload": "Deployment/orders",
                                                             "pods": ["orders-7df-x1"], "kills": 412}]


# ---------------------------------------------------------------- warning events, from any source

from healthconsole.core.integrations.provider import WARNING_EVENTS  # noqa: E402


class EventsNr:
    def __init__(self, rows):
        self.rows, self.sent = rows, []

    def nrql_many(self, account_id, nrqls):
        self.sent.append(nrqls)
        return [[{"clusters": ["shop-aks"]}], self.rows]


def test_new_relic_gives_warning_events_shaped_like_container_insights():
    fake = EventsNr([{"facet": ["BackOff", "shop", "api-7d9f8c6b5-x2x9z", "Pod"], "Count": 6, "Last": KILL_MS,
                      "Message": "Back-off restarting failed container"},
                     {"facet": ["OOMKilling", None, "aks-pool-0", "Node"], "Count": 55, "Last": KILL_MS, "Message": KERNEL}])
    out = provider(fake).warning_events("shop-aks", 1, refresh=True)
    assert [(r["Reason"], r["Namespace"], r["ObjectKind"], r["Count"]) for r in out["rows"]] == [
        ("OOMKilling", "", "Node", 55), ("BackOff", "shop", "Pod", 6)]
    assert out["total"] == 2 and out["rows"][0]["Last"].startswith(AT_Z[:16])
    nrql = fake.sent[0][1]
    assert "event.type = 'Warning'" in nrql and "clusterName = 'shop-aks'" in nrql and "SINCE 1 hours ago LIMIT MAX" in nrql
    assert WARNING_EVENTS in monitors.provider_for({"name": "NR", "type": "newrelic"}).capabilities
    with pytest.raises(nr_data.ProviderError):
        provider(fake).warning_events("x' OR 1=1", 1)


def test_the_events_route_asks_any_tool_with_the_capability(tmp_path, monkeypatch):
    from tests.test_integrations import NR, tenant_app
    settings = config.from_data({"tenants": {"contoso.example": {"integrations": {"monitors": [NR]}}}})
    http, _ = tenant_app(tmp_path, settings)

    class Fake:
        label, capabilities = "Some tool", (WARNING_EVENTS,)

        def warning_events(self, name, hours, refresh=False):
            return {"found": True, "rows": [{"Reason": "BackOff", "Namespace": "shop", "Object": "p", "ObjectKind": "Pod",
                                             "Count": 3, "Last": AT_Z, "Message": "m"}], "total": 9, "queries": [], "notes": []}
    monkeypatch.setattr(monitors, "provider_for", lambda monitor: Fake())
    data = http.get(f"/api/explore/warning-events-monitor?id={AKS}&ci_hours=24&connection=New Relic").get_json()
    assert data["available"] and data["total"] == 9 and data["rows"][0]["Reason"] == "BackOff" and data["hours"] == 24

    class NoEvents:
        label, capabilities = "Other tool", (OOM_KILLS,)
    monkeypatch.setattr(monitors, "provider_for", lambda monitor: NoEvents())
    refused = http.get(f"/api/explore/warning-events-monitor?id={AKS}&connection=New Relic").get_json()
    assert not refused["available"] and "can't give a cluster's warning events" in refused["reason"]


def test_the_kubernetes_api_gives_warning_events_without_any_monitoring_tool():
    now = datetime(2026, 11, 26, 17, 0, tzinfo=timezone.utc)
    items = [{"type": "Warning", "reason": "BackOff", "count": 4, "message": "old", "lastTimestamp": "2026-11-26T16:40:00Z",
              "involvedObject": {"kind": "Pod", "namespace": "shop", "name": "api-1"}},
             {"type": "Warning", "reason": "BackOff", "count": 2, "message": "newer", "lastTimestamp": "2026-11-26T16:50:00Z",
              "involvedObject": {"kind": "Pod", "namespace": "shop", "name": "api-1"}},
             {"type": "Warning", "reason": "OOMKilling", "series": {"count": 7, "lastObservedTime": "2026-11-26T16:55:00Z"},
              "message": KERNEL, "involvedObject": {"kind": "Node", "name": "aks-pool-0"}},
             {"type": "Warning", "reason": "FailedMount", "lastTimestamp": "2026-11-26T14:00:00Z",
              "involvedObject": {"kind": "Pod", "namespace": "shop", "name": "old"}},
             {"type": "Normal", "reason": "Pulled", "lastTimestamp": "2026-11-26T16:59:00Z", "involvedObject": {}}]
    rows = aks.api_warning_events(items, 1, now)
    assert [(r["Reason"], r["Object"], r["Count"]) for r in rows] == [("OOMKilling", "aks-pool-0", 7), ("BackOff", "api-1", 6)]
    assert rows[1]["Message"] == "newer" and rows[1]["Last"] == "2026-11-26T16:50:00Z"
    kube.check_kube_read("GET", "/api/v1/events")  # allowed: cluster-wide, read only


def test_without_container_insights_what_s_wrong_now_takes_the_cluster_s_warning_events():
    from healthconsole.core.explore import workload_problems as wp
    k = {"available": True, "events_hours": 1, "events": [
        {"Reason": "BackOff", "Namespace": "shop", "Object": "api-1", "ObjectKind": "Pod", "Count": 6},
        {"Reason": "OOMKilling", "Namespace": "", "Object": "aks-pool-0", "ObjectKind": "Node", "Count": 7}]}
    out = wp.problems([], {"available": False}, k, {("shop", "api-1"): ("shop", "Deployment", "api")})
    lines = {(g["workload"], s["text"], tuple(s["sources"])) for g in out["groups"] for s in g["signals"] if s["type"] == "events"}
    assert ("Deployment/api", "warning events, last hour: BackOff ×6", (wp.K8S,)) in lines
    assert ("", "warning events, last hour: OOMKilling ×7", (wp.K8S,)) in lines
    with_ci = wp.problems([], {"available": True, "events": [], "hours": {}}, k)
    assert not any(s["type"] == "events" for g in with_ci["groups"] for s in g["signals"])  # CI's own window wins
