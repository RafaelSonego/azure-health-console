import pytest
from flask import Flask

from healthconsole.core import config, settings_file
from healthconsole.core.explore import workload_path as wp
from healthconsole.web import create_blueprint
from tests.conftest import SUB, FakeClient

CLUSTER = f"/subscriptions/{SUB}/resourceGroups/aks/providers/Microsoft.ContainerService/managedClusters/shop-aks"
APIM = f"/subscriptions/{SUB}/resourceGroups/edge/providers/Microsoft.ApiManagement/service/edge-apim"
AGW = f"/subscriptions/{SUB}/resourceGroups/edge/providers/Microsoft.Network/applicationGateways/edge-agw"
SUBNET = f"/subscriptions/{SUB}/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/vnet/subnets/aks"


def logs_answer(rows_by_marker):
    """A /logs POST answered by the first marker found in the query, as Log Analytics' table shape."""
    def answer(body):
        for marker, rows in rows_by_marker.items():
            if marker in body["query"]:
                columns = list(rows[0]) if rows else ["x"]
                return {"tables": [{"columns": [{"name": c} for c in columns], "rows": [[r[c] for c in columns] for r in rows]}]}
        return {"tables": [{"columns": [{"name": "x"}], "rows": []}]}
    return answer


def diag(dedicated=False):
    return {"value": [{"properties": {"workspaceId": "/w", "logAnalyticsDestinationType": "Dedicated" if dedicated else None,
                                      "logs": [{"category": "allLogs", "enabled": True}]}}]}


# ---- front doors, suggested

def test_apis_are_ranked_by_shared_words_and_by_paths_the_app_serves():
    apis = {"value": [
        {"name": "orders-api", "properties": {"displayName": "Orders", "path": "shop/orders", "serviceUrl": "https://orders.corp.example/orders"}},
        {"name": "legacy", "properties": {"displayName": "Legacy", "path": "old", "serviceUrl": "https://x.corp.example/basket"}},
        {"name": "orders-api;rev=1", "properties": {"displayName": "Orders", "isCurrent": False, "path": "shop/orders",
                                                    "serviceUrl": "https://orders.corp.example/orders"}},
        {"name": "billing", "properties": {"displayName": "Billing", "path": "billing", "serviceUrl": "https://b.corp.example/"}}]}
    client = FakeClient(gets={"/apis": apis},
                        query_handlers=[("microsoft.apimanagement/service", lambda kql: [{"id": APIM, "name": "edge-apim"}])])
    found, notes = wp.apim_candidates(client, [SUB], "orders-service", request_paths=["/basket/7", "/"])
    # a path the app serves outranks a shared word; billing shares nothing; old revisions are skipped
    assert [r["api"] for r in found] == ["legacy", "orders-api"] and not notes
    assert "backend path /basket" in found[0]["why"][0] and "orders" in found[1]["why"][0]


def test_routes_to_the_clusters_load_balancer_or_subnet_are_suggested():
    gw = {"id": AGW, "name": "edge-agw", "props": {
        "httpListeners": [{"id": "l1", "name": "shop-https", "properties": {"hostName": "shop.example.com"}}],
        "backendAddressPools": [{"id": "p1", "name": "aks-pool", "properties": {"backendAddresses": [{"ipAddress": "10.1.2.250"}]}},
                                {"id": "p2", "name": "far-pool", "properties": {"backendAddresses": [{"ipAddress": "10.9.0.4"}]}},
                                {"id": "p3", "name": "subnet-pool", "properties": {"backendAddresses": [{"ipAddress": "10.1.2.9"}]}}],
        "urlPathMaps": [{"id": "m1", "properties": {"pathRules": [{"properties": {"paths": ["/orders/*"], "backendAddressPool": {"id": "p3"}}}]}}],
        "requestRoutingRules": [{"properties": {"httpListener": {"id": "l1"}, "backendAddressPool": {"id": "p1"}}},
                                {"properties": {"httpListener": {"id": "l1"}, "urlPathMap": {"id": "m1"}}},
                                {"properties": {"httpListener": {"id": "l1"}, "backendAddressPool": {"id": "p2"}}}]}}
    client = FakeClient(query_handlers=[
        ("where id =~", lambda kql: [{"node": "mc_aks", "pools": [{"vnetSubnetID": SUBNET}]}]),
        ("microsoft.network/loadbalancers", lambda kql: [{"ip": "10.1.2.250"}]),
        ("microsoft.network/virtualnetworks", lambda kql: [{"prefix": "10.1.2.0/24", "prefixes": None}]),
        ("microsoft.network/applicationgateways", lambda kql: [gw])])
    found, notes = wp.agw_candidates(client, [SUB], "orders", CLUSTER)
    assert [(r["pool"], r["how"]) for r in found] == [("aks-pool", "load balancer"), ("subnet-pool", "subnet")]
    assert found[1]["paths"] == ["/orders/*"] and found[0]["hosts"] == ["shop.example.com"] and not notes


def test_monitoring_tool_apps_with_the_same_name_come_first():
    found = wp.app_candidates(["orders-service", "orders-worker", "billing"], "orders-service")
    assert [a["app"] for a in found] == ["orders-service", "orders-worker"] and found[0]["score"] == 10


# ---- hops, read

def test_an_api_hop_splits_the_gateways_own_errors_from_the_backends():
    rows = {"by period": [{"period": "now", "total": 1000, "errors": 120, "backend_errors": 20, "gateway_errors": 100, "p95_ms": 900},
                          {"period": "week", "total": 1000, "errors": 3, "backend_errors": 3, "gateway_errors": 0, "p95_ms": 200}],
            "bin(TimeGenerated, 1m)": [{"TimeGenerated": "2026-11-23T14:02:00Z", "total": 20, "errors": 9}],
            "by reason": [{"reason": "BackendConnectionFailure", "count": 100}]}
    def by_label(body):
        q = body["query"]
        key = "bin(TimeGenerated, 1m)" if "bin(TimeGenerated, 1m)" in q else "by reason" if "by reason" in q else "by period"
        return logs_answer({"": rows[key]})(body)
    client = FakeClient(gets={"diagnosticSettings": diag()}, posts={"/logs": by_label})
    hop = wp.apim_hop(client, APIM, "orders-api")
    assert hop["available"] and hop["now"]["gateway_errors"] == 100 and hop["week"]["errors"] == 3
    assert hop["details"]["reasons"] == [["BackendConnectionFailure", 100]] and hop["minutes"][0][2] == 9
    assert all("apiId_s =~ \"orders-api\"" in q["query"] for q in hop["queries"])
    assert wp.assess(hop)[0] == "warning"


def test_a_hop_whose_logs_dont_reach_a_workspace_says_so_instead_of_zero():
    hop = wp.agw_hop(FakeClient(gets={"diagnosticSettings": {"value": []}}), AGW, "shop-https", "aks-pool")
    assert not hop["available"] and "doesn't send access logs" in hop["reason"] and wp.assess(hop) == (None, "")


def test_pods_come_from_the_tool_when_container_insights_has_nothing():
    class Tool:
        label = "Tool"

        def workload_pods(self, cluster, namespace, kind, name):
            assert (cluster, namespace, kind, name) == ("shop-aks", "shop", "Deployment", "orders")
            return {"found": True, "now": {"restarts": 12, "errors": 12, "pods": 3, "not_ready": 1},
                    "week": {"restarts": 0, "errors": 0, "pods": 3}, "minutes": [], "queries": [{"account": 1, "nrql": "SELECT …"}]}

    client = FakeClient(posts={"/logs": logs_answer({})})
    link = {"cluster_id": CLUSTER, "namespace": "shop", "kind": "Deployment", "name": "orders"}
    found = wp.read(client, link, Tool(), ("workload pods",))
    pods = next(h for h in found["hops"] if h["hop"] == "pods")
    assert pods["source"] == "Tool" and pods["now"]["restarts"] == 12 and pods["status"] == "critical"
    requests = next(h for h in found["hops"] if h["hop"] == "requests")
    assert not requests["available"] and "can't give requests" in requests["reason"]


def test_the_verdict_names_the_hop_furthest_back_that_broke():
    hop = lambda kind, now, week, available=True: {"hop": kind, "label": wp.HOP_LABELS[kind], "available": available,  # noqa: E731
                                                     "now": now, "week": week, "reason": "" if available else "x"}
    hops = [hop("agw", {"total": 1000, "errors": 2}, {"total": 1000, "errors": 2}),
            hop("apim", {"total": 1000, "errors": 230}, {"total": 1000, "errors": 4}),
            hop("pods", {"restarts": 3, "errors": 3}, {"restarts": 0, "errors": 0}),
            hop("requests", {}, {}, available=False)]
    text = wp.verdict(hops)
    assert text.startswith("Breaks at Pods: 3 restarts") and "Also up: API Management" in text
    assert "Fine: App Gateway" in text and "Not read: Requests" in text
    quiet = [hop("agw", {"total": 10, "errors": 0}, {"total": 10, "errors": 0})]
    assert wp.verdict(quiet).startswith("No hop shows more errors")
    dropped = hop("apim", {"total": 300, "errors": 0}, {"total": 1000, "errors": 0})
    assert wp.assess(dropped) == ("warning", "traffic down 70% (300 requests, same hour last week 1,000)")
    assert wp.assess(hop("apim", {"total": 600, "errors": 0}, {"total": 1000, "errors": 0}))[0] == "healthy"


# ---- links, kept per tenant

def test_a_link_is_saved_replaced_and_removed_and_validated(tmp_path):
    file = tmp_path / "c.yaml"
    key = wp.workload_key(CLUSTER, "shop", "Deployment", "orders")
    link = {"key": key, "name": "orders", "cluster_id": CLUSTER, "namespace": "shop", "kind": "Deployment",
            "apim": [{"service_id": APIM, "api": "orders-api"}], "agw": [], "app": "orders-service", "component": ""}
    settings = settings_file.apply_workload_link(file, "t1", link)
    assert config.workload_links(settings, {"id": "t1"})[0]["apim"] == [{"service_id": APIM, "api": "orders-api"}]
    settings = settings_file.apply_workload_link(file, "t1", {**link, "app": "orders-v2"})
    assert [x["app"] for x in config.workload_links(settings, {"id": "t1"})] == ["orders-v2"]
    settings = settings_file.apply_workload_link(file, "t1", {**link, "apim": [], "app": ""})
    assert config.workload_links(settings, {"id": "t1"}) == []
    with pytest.raises(config.ConfigError, match="service_id, api"):
        config.check_workload_links([{**link, "apim": [{"service_id": APIM}]}])
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.check_workload_links([{**link, "secret": "x"}])


def test_routes_check_the_workload_and_read_from_the_saved_link(tmp_path):
    file = tmp_path / "c.yaml"
    client = FakeClient(posts={"/logs": logs_answer({})},
                        gets={"diagnosticSettings": {"value": []}, "/apis": {"value": []}})
    app = Flask("test", static_folder=None)
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load(file), config_file=file, authorize=lambda: None))
    http = app.test_client()
    workload = {"cluster": CLUSTER, "namespace": "shop", "kind": "Deployment", "workload": "orders"}
    assert http.get("/api/path/read", query_string={**workload, "kind": "Job"}).status_code == 400
    assert http.get("/api/path/read", query_string={**workload, "workload": "Bad Name"}).status_code == 400
    saved = http.post("/api/path/link", json={**workload, "agw": [{"gateway_id": AGW, "listener": "shop-https", "pool": "aks-pool"}]})
    assert saved.status_code == 200 and saved.get_json()["link"]["agw"][0]["pool"] == "aks-pool"
    found = http.get("/api/path/read", query_string=workload).get_json()
    agw = next(h for h in found["hops"] if h["hop"] == "agw")
    assert not agw["available"] and "access logs" in agw["reason"] and found["link"]["name"] == "orders"
    candidates = http.get("/api/path/candidates", query_string={**workload, "subscription": SUB}).get_json()
    assert candidates["link"]["agw"] and candidates["workload"]["key"].endswith("/deployments/orders")
