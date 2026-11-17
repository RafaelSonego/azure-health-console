import re
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from tests.conftest import SUB, FakeClient
from healthconsole.core import config
from healthconsole.core.arm import AccessDenied, ArmError
from healthconsole.core.explore import activity, aks, kube
from healthconsole.core.investigate import gateway, pod, source
from healthconsole.core.investigate.appinsights import AppInsightsSource
from healthconsole.web import create_blueprint

APP = f"/subscriptions/{SUB}/resourceGroups/shop/providers/microsoft.insights/components/shop-appi"
APIM_AI = f"/subscriptions/{SUB}/resourceGroups/edge/providers/microsoft.insights/components/edge-appi"
APIM = f"/subscriptions/{SUB}/resourceGroups/edge/providers/Microsoft.ApiManagement/service/edge-apim"
AKS = f"/subscriptions/{SUB}/resourceGroups/shop/providers/Microsoft.ContainerService/managedClusters/shop-aks"
AKS2 = f"/subscriptions/{SUB}/resourceGroups/shop/providers/Microsoft.ContainerService/managedClusters/other-aks"
AT = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
AT_Z = AT.strftime("%Y-%m-%dT%H:%M:%SZ")
TRACE = "0af7651916cd43dd8448eb211c80319c"


def table(columns, rows):
    return {"tables": [{"columns": [{"name": c} for c in columns], "rows": rows}]}


SPAN_COLUMNS = ["time", "kind", "name", "role", "instance", "success", "duration_ms", "code", "target", "message",
                "stack", "severity", "id", "parent"]


class Insights(FakeClient):
    """Answers App Insights `/query` GETs per component."""

    def __init__(self, answers, deny=(), **kwargs):
        super().__init__(**kwargs)
        self.answers, self.deny, self.sent = answers, deny, []

    def get(self, path, api_version, params=None, tolerate=(), timeout=None):
        if path.endswith("/query"):
            component = path[: -len("/query")]
            self.sent.append((component, params["query"], params["timespan"]))
            if component in self.deny:
                raise AccessDenied(path)
            return self.answers(component, params["query"])
        return super().get(path, api_version, params, tolerate, timeout)


def test_a_trace_is_read_in_the_app_and_the_gateway_component_as_one_tree():
    def answers(component, kql):
        if component == APIM_AI:
            return table(SPAN_COLUMNS, [["2026-11-11T10:00:00.000Z", "request", "GET /shop/orders", "edge-apim", "gw-1",
                                         "False", 812.0, "500", "https://edge/shop/orders", "", "", None, "g1", TRACE]])
        return table(SPAN_COLUMNS, [
            ["2026-11-11T10:00:00.050Z", "request", "GET /orders", "orders", "orders-7d9f-abcde", "False", 760.0, "500",
             "http://orders/orders", "", "", None, "r1", "g1"],
            ["2026-11-11T10:00:00.300Z", "exception", "DbError", "orders", "orders-7d9f-abcde", "False", None, "", "",
             "timeout", "x" * 9000, 3, "e1", "r1"]])

    client = Insights(answers)
    data = AppInsightsSource(client, APP, also=[APIM_AI, APP]).trace(TRACE, AT)
    assert [c for c, _, _ in client.sent] == [APP, APIM_AI]  # the app itself isn't read twice
    assert all(f'operation_Id == "{TRACE}"' in kql for _, kql, _ in client.sent)
    spans = data["rows"]
    assert [s["kind"] for s in spans] == ["request", "request", "exception"]
    assert [s["depth"] for s in spans] == [0, 1, 2] and spans[0]["gateway"] and not spans[1]["gateway"]
    assert spans[1]["offset_ms"] == 50 and spans[2]["severity"] == "Error" and len(spans[2]["stack"]) == 8000
    start, end = client.sent[0][2].split("/")
    assert start < AT.strftime("%Y-%m-%dT%H:%M:%SZ") < end


def test_a_gateway_without_access_fails_alone():
    client = Insights(lambda c, q: table(SPAN_COLUMNS, []), deny=(APIM_AI,))
    data = AppInsightsSource(client, APP, also=[APIM_AI]).trace(TRACE)
    assert data["rows"] == [] and len(data["errors"]) == 1 and "edge-appi" in data["errors"][0]


def test_samples_quote_what_the_page_sends_and_failed_only_by_default():
    client = Insights(lambda c, q: table(["time", "trace_id"], []))
    ai = AppInsightsSource(client, APP)
    ai.samples('GET /x" | take 1 //', "orders", "1h")
    ai.samples("GET /x", "orders", "1h", failed=False)
    first, second = client.sent[0][1], client.sent[1][1]
    assert 'operation_Name == "GET /x\\" | take 1 //"' in first and "success == false" in first
    assert "success == false" not in second


def test_search_filters_text_and_severity_and_names_levels():
    client = Insights(lambda c, q: table(["time", "kind", "severity", "message"], [["t", "trace", 2, "slow db"]]))
    data = AppInsightsSource(client, APP).search("slow", "2", "6h")
    kql = client.sent[0][1]
    assert 'message contains "slow"' in kql and "severity >= 2" in kql and data["rows"][0]["severity"] == "Warning"
    assert "where" not in AppInsightsSource(client, APP).search_queries("", "", "1h")[0]["query"].split("|", 3)[-1]


def test_operations_add_the_failure_rate():
    client = Insights(lambda c, q: table(["operation", "role", "total", "failed", "codes", "last_seen"],
                                         [["GET /orders", "orders", 200, 50, '["500"]', "t"]]))
    row = AppInsightsSource(client, APP).operations("1h")["rows"][0]
    assert row["failed_pct"] == 25.0 and row["codes"] == ["500"]


def test_exceptions_are_grouped_by_type_with_the_latest_trace_and_a_short_message():
    client = Insights(lambda c, q: table(["type", "operation", "role", "total", "last_seen", "trace_id", "instance", "message"],
                                         [["System.TimeoutException", "SyncJob", "jobs", 240, "t", TRACE, "host-1", "x" * 900]]))
    data = AppInsightsSource(client, APP).exceptions("24h")
    row, kql = data["rows"][0], client.sent[0][1]
    assert kql.startswith("exceptions") and "arg_max(timestamp, operation_Id" in kql and "by type" in kql
    assert row["trace_id"] == TRACE and row["total"] == 240 and len(row["message"]) == 300
    assert data["queries"][0]["query"] == kql


def test_no_query_names_a_column_time_or_kind_which_the_endpoint_rejects():
    source = AppInsightsSource(Insights(lambda c, q: table([], [])), APP)
    queries = [*source.operations_queries("1h"), *source.exceptions_queries("1h"), *source.dependencies_queries("1h"),
               *source.samples_queries("GET /", "api", "1h"), *source.trace_queries(TRACE, AT),
               *source.search_queries("x", "2", "1h")]
    for query in queries:
        assert not re.search(r"\b(time|kind)\b", query["query"]), query["query"]


def test_rows_get_time_and_kind_back():
    client = Insights(lambda c, q: table(["timestamp", "span_kind", "message"], [["t", "trace", "hello"]]))
    row = AppInsightsSource(client, APP).search("", "", "1h")["rows"][0]
    assert row["time"] == "t" and row["kind"] == "trace" and "timestamp" not in row


@pytest.mark.parametrize("bad", ["", "short", "abc' or 1==1", "x" * 65])
def test_trace_ids_are_checked(bad):
    with pytest.raises(ValueError):
        AppInsightsSource(Insights(lambda c, q: {}), APP).trace_queries(bad)


def test_tree_survives_parent_loops():
    spans = source.tree([{"time": "2026-11-11T10:00:00Z", "id": "a", "parent": "b"},
                         {"time": "2026-11-11T10:00:01Z", "id": "b", "parent": "a"}])
    assert len(spans) == 2 and all(s["depth"] <= 8 for s in spans)


# ---------------------------------------------------------------- the gateway

def test_gateways_say_where_each_service_logs():
    client = FakeClient(
        query_handlers=[("apimanagement", lambda kql: [{"id": APIM, "name": "edge-apim", "resourceGroup": "edge",
                                                        "subscriptionId": SUB}])],
        gets={"/loggers": {"value": [
            {"properties": {"loggerType": "applicationInsights", "resourceId": APIM_AI}},
            {"properties": {"loggerType": "applicationInsights", "credentials": {"instrumentationKey": "{{k}}"}}},
            {"properties": {"loggerType": "azureEventHub"}}]},
              "/diagnosticSettings": {"value": [{"properties": {
                  "workspaceId": "/w", "logAnalyticsDestinationType": "Dedicated",
                  "logs": [{"category": "GatewayLogs", "enabled": True}]}}]}})
    found = gateway.gateways(client, [SUB])["gateways"][0]
    assert found["components"] == [APIM_AI] and found["unnamed_loggers"] == 1
    assert found["workspace"] and found["dedicated"]


def test_gateway_without_workspace_logs():
    client = FakeClient(gets={"/loggers": {"value": []}, "/diagnosticSettings": {"value": [{"properties": {
        "workspaceId": "/w", "logs": [{"category": "WebSocketConnectionLogs", "enabled": True}]}}]}})
    assert gateway._workspace_logs(client, APIM) == (False, False)


def test_the_audit_group_alone_is_not_gateway_logs():
    client = FakeClient(gets={"/diagnosticSettings": {"value": [{"properties": {
        "workspaceId": "/w", "logs": [{"categoryGroup": "audit", "enabled": True}]}}]}})
    assert gateway._workspace_logs(client, APIM) == (False, False)


def test_gateway_logs_fall_back_to_azure_diagnostics(monkeypatch):
    sent = []

    def resource_logs(client, rid, kql, timespan):
        sent.append(kql)
        if kql.startswith("ApiManagementGatewayLogs"):
            raise ArmError("Failed to resolve table")
        return [{"Url": "https://edge/shop/orders/7", "ResponseCode": 500}]

    monkeypatch.setattr(aks, "resource_logs", resource_logs)
    data = gateway.logs(FakeClient(), APIM, AT, "/orders/7")
    assert data["table"] == "AzureDiagnostics" and data["probable"] and len(data["rows"]) == 1
    assert all('contains "/orders/7"' in kql for kql in sent)
    with pytest.raises(ValueError):
        gateway.log_queries(APIM, AT, "orders\" | take 1")


# ---------------------------------------------------------------- the pod

def test_the_pod_is_found_in_the_cluster_that_ran_it(monkeypatch):
    def resource_logs(client, rid, kql, timespan):
        if rid == AKS2:
            raise ArmError("no workspace")
        return [{"Name": "orders-7d9f-abcde", "Namespace": "shop", "PodStatus": "Running", "ContainerRestartCount": 3}]

    monkeypatch.setattr(aks, "resource_logs", resource_logs)
    client = FakeClient(query_handlers=[("managedclusters", lambda kql: [
        {"id": AKS2, "name": "other-aks"}, {"id": AKS, "name": "shop-aks"}])])
    data = pod.find(client, [SUB], "orders-7d9f-abcde", AT)
    assert data["found"]["cluster_id"] == AKS and data["found"]["namespace"] == "shop" and data["found"]["restarts"] == 3
    assert any("other-aks" in n for n in data["notes"]) and len(data["clusters"]) == 2
    with pytest.raises(ValueError):
        pod.find(client, [SUB], "Not A Pod!", AT)


def test_changes_keep_the_hours_before_the_request(monkeypatch):
    before, long_before = AT - timedelta(hours=1), AT - timedelta(hours=9)
    monkeypatch.setattr(activity, "activity", lambda client, rid, hours: {
        "changes": [{"at": before.isoformat()}, {"at": long_before.isoformat()}],
        "operations": [{"at": before.strftime("%Y-%m-%dT%H:%M:%S.1234567Z")}], "errors": []})
    monkeypatch.setattr(aks, "resource_logs", lambda *a: [{"Reason": "BackOff"}])
    data = pod.changes(FakeClient(), AKS, AT, "shop")
    assert len(data["changes"]) == 1 and len(data["operations"]) == 1 and data["events"] == [{"Reason": "BackOff"}]
    assert "Namespace == \"shop\"" in data["event_query"]


class Kube:
    def __init__(self, text):
        self.text, self.params = text, None

    def get(self, path, params=None, raw=False):
        self.params = params
        return self.text


def test_pod_logs_at_a_minute_from_the_api_keep_only_that_window(monkeypatch):
    inside = (AT + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    after = (AT + timedelta(minutes=9)).strftime("%Y-%m-%dT%H:%M:%S")
    k = Kube(f"{inside}.1Z boom\n{after}.1Z later")
    monkeypatch.setattr(kube, "client_for", lambda *a: k)
    data = aks.logs(FakeClient(), AKS, "shop", "orders-1", around=AT)
    assert data == {"source": "kubernetes", "lines": [f"{inside}.1Z boom"]}
    assert k.params["timestamps"] == "true" and "tailLines" not in k.params and k.params["sinceTime"] < inside


def test_pod_logs_at_a_minute_fall_back_to_container_insights(monkeypatch):
    monkeypatch.setattr(kube, "client_for", lambda *a: Kube(""))
    sent = []
    monkeypatch.setattr(aks, "resource_logs", lambda client, rid, kql, span: sent.append((kql, span)) or [])
    data = aks.logs(FakeClient(), AKS, "shop", "orders-1", around=AT)
    assert data["source"] == "container insights" and "within 2 minutes" in data["error"]
    assert "TimeGenerated between" in sent[0][0] and "ago(30m)" not in sent[0][0] and "/" in sent[0][1]


# ---------------------------------------------------------------- the web layer

@pytest.fixture
def web():
    client = Insights(lambda c, q: table(SPAN_COLUMNS, []), query_handlers=[
        ("microsoft.insights/components", lambda kql: [{"id": APP, "name": "shop-appi", "resourceGroup": "shop",
                                                       "subscriptionId": SUB}])])
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load("/nonexistent")))
    return app.test_client(), client


def test_routes_check_their_inputs_and_say_why(web):
    http, _ = web
    cases = [("/api/investigate/trace?component=/subscriptions/x/evil&trace=" + TRACE, "component must be"),
             (f"/api/investigate/trace?component={APP}&trace=bad'", "trace must be"),
             (f"/api/investigate/trace?component={APP}&trace={TRACE}&at=yesterday", "ISO time"),
             (f"/api/investigate/operations?component={APP}&window=2d", "window must be"),
             (f"/api/investigate/exceptions?component={APP}&window=2d", "window must be"),
             (f"/api/investigate/dependencies?component={APP}&window=2d", "window must be"),
             ("/api/investigate/recent-changes?id=/subscriptions/x/evil", "resource IDs"),
             (f"/api/investigate/gateway-logs?id={AKS}&at={AT_Z}", "API Management"),
             ("/api/investigate/components", "subscription")]
    for url, why in cases:
        reply = http.get(url)
        assert reply.status_code == 400 and why in reply.get_json()["error"], url


def test_api_management_finds_its_component_by_its_loggers():
    other = APIM_AI.replace("edge-appi", "edge-other-appi") if "edge-appi" in APIM_AI else APIM_AI + "x"
    loggers = {"value": [{"properties": {"loggerType": "applicationInsights", "resourceId": APIM_AI}},
                         {"properties": {"loggerType": "applicationInsights", "resourceId": other}}]}
    client = FakeClient(gets={"/loggers": loggers})
    found = gateway.link(client, APIM)
    assert found["how"] == "logger" and found["component_id"] == APIM_AI and len(found["candidates"]) == 2
    assert gateway.link(client, APIM, other)["how"] == "picked"
    assert gateway.link(client, APIM, APP)["how"] == "logger"  # a pick it doesn't log to is ignored
    assert gateway.link(FakeClient(gets={"/loggers": {"value": []}}), APIM)["gateway"] is True


def test_overview_line_reads_the_last_hour_of_a_gateway_component():
    loggers = {"value": [{"properties": {"loggerType": "applicationInsights", "resourceId": APIM_AI}}]}
    metrics = {"value": [{"name": {"value": "requests/failed"}, "timeseries": [{"data": [{"count": 1}, {"count": 7}]}]}]}
    client = FakeClient(gets={"/loggers": loggers, "/metrics": metrics})
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load("/nonexistent")))
    data = app.test_client().get(f"/api/explore/appinsights-now?id={APIM}").get_json()
    assert data["available"] and data["component_id"] == APIM_AI and data["link"]["how"] == "logger"
    assert data["now"]["failed"] == 7 and data["now"]["trend"] == [1, 7] and "roles" not in data


def test_routes_answer_with_their_queries(web):
    http, client = web
    reply = http.get(f"/api/investigate/trace?component={APP}&also={APIM_AI}&trace={TRACE}&at={AT_Z}")
    assert reply.status_code == 200 and len(reply.get_json()["queries"]) == 2
    assert {c for c, _, _ in client.sent} == {APP, APIM_AI}
    found = http.get(f"/api/investigate/components?subscription={SUB}").get_json()
    assert found["components"][0]["name"] == "shop-appi"


def split_series(*code_counts):
    return {"value": [{"name": {"value": "requests/failed"}, "timeseries": [
        {"metadatavalues": [{"name": {"value": "request/resultCode"}, "value": code}], "data": [{"count": n}]}
        for code, n in code_counts]}]}


def test_server_errors_count_only_5xx_and_ask_for_the_result_code_split():
    from healthconsole.core.explore import appinsights as ai
    sent = []
    client = FakeClient(gets={"/metrics": lambda params: sent.append(params) or split_series(("500", 30), ("503", 12), ("404", 900))})
    assert ai.server_errors(client, [{"id": APP}]) == {APP: (42, None)}
    assert sent[0]["$filter"] == "request/resultCode eq '*'" and sent[0]["metricnames"] == "requests/failed"


def test_buckets_keep_the_last_24_hours_as_a_trend():
    from healthconsole.core.explore import appinsights as ai
    counts = ai._buckets({"value": [{"name": {"value": "requests/failed"},
                                     "timeseries": [{"data": [{"count": n} for n in range(169)]}]}]})
    assert counts["requests/failed"] == (168, 0) and counts["trend"]["requests/failed"] == list(range(145, 169))


def test_quiet_components_sort_by_5xx_with_their_share_of_scope(monkeypatch):
    big, small = APP.replace("shop-appi", "edge-appi"), APP
    client = FakeClient(query_handlers=[("microsoft.insights/components", lambda kql: [
        {"id": small, "name": "shop-appi"}, {"id": big, "name": "edge-appi"}])])
    from healthconsole.core import investigate
    from healthconsole.core.explore import appinsights as ai
    steady = {"requests/count": (1000, 1000), "requests/failed": (15, 19), "exceptions/count": (0, 0),
              "trend": {"requests/failed": [19, 15]}}
    monkeypatch.setattr(ai, "last_hour", lambda c, rows: [(r, steady, None) for r in rows])
    monkeypatch.setattr(ai, "server_errors", lambda c, rows: {small: (10, None), big: (30, None)})
    found = investigate.components(client, [SUB])
    assert [c["name"] for c in found["components"]] == ["edge-appi", "shop-appi"]
    assert [c["share"] for c in found["components"]] == [75.0, 25.0] and found["components"][0]["status"] is None
    assert found["components"][0]["trend"] == [19, 15]


def test_an_unreadable_5xx_split_leaves_the_column_empty_and_says_so(monkeypatch):
    client = FakeClient(query_handlers=[("microsoft.insights/components", lambda kql: [{"id": APP, "name": "shop-appi"}])])
    from healthconsole.core import investigate
    from healthconsole.core.explore import appinsights as ai
    monkeypatch.setattr(ai, "last_hour", lambda c, rows: [(r, {}, None) for r in rows])
    monkeypatch.setattr(ai, "server_errors", lambda c, rows: {APP: (None, ArmError("400"))})
    found = investigate.components(client, [SUB])
    assert found["components"][0]["server_errors"] is None and "5xx count couldn't be read" in found["notes"][0]


def test_dependencies_keep_one_failed_trace_and_the_failure_rate():
    client = Insights(lambda c, q: table(["type", "target", "role", "total", "failed", "codes", "p95_ms", "last_seen", "trace_ids"],
                                         [["SQL", "db.example", "api", 200, 50, '["-2"]', 900, "t", f'["{TRACE}"]']]))
    data = AppInsightsSource(client, APP).dependencies("6h")
    row = data["rows"][0]
    assert row["trace_id"] == TRACE and row["failed_pct"] == 25.0 and row["codes"] == ["-2"] and "trace_ids" not in row
    assert client.sent[0][1].startswith("dependencies") and "where failed > 0" in client.sent[0][1]


def test_recent_changes_read_every_target_in_their_subscriptions():
    other = f"/subscriptions/{'1' * 8}-1111-1111-1111-{'1' * 12}/resourceGroups/web/providers/Microsoft.Web/sites/shop-api"
    seen = {}
    client = FakeClient(query_handlers=[("resourcechanges", lambda kql: seen.update(kql=kql) or [
        {"at": "2026-11-12T14:02:00Z", "target": other.lower(), "change": "Update", "by": "someone",
         "client": "portal", "changes": {"properties.siteConfig.appSettings": {}, "tags.x": {}}}])])
    from healthconsole.core import investigate
    found = investigate.recent_changes(client, [APP, other, APP.upper()], "6h")
    assert found["hours"] == 7 and "ago(7h)" in seen["kql"] and APP.lower() in seen["kql"] and other.lower() in seen["kql"]
    assert found["rows"][0]["name"] == "shop-api" and found["rows"][0]["fields"] == ["properties.siteConfig.appSettings", "tags.x"]


@pytest.mark.parametrize("ids,window", [([], "1h"), (["/subscriptions/x/evil"], "1h"), ([APP], "2d")])
def test_recent_changes_check_their_inputs(ids, window):
    from healthconsole.core import investigate
    with pytest.raises(ValueError):
        investigate.recent_changes(FakeClient(), ids, window)


def test_the_start_list_puts_the_failing_app_first_with_its_last_hour(monkeypatch):
    quiet = APP.replace("shop-appi", "a-quiet-appi")
    client = FakeClient(query_handlers=[
        ("microsoft.web/sites", lambda kql: [{"name": "shop-api", "tags": {f"hidden-link:{APP}": "Resource"}}]),
        ("microsoft.insights/components", lambda kql: [{"id": quiet, "name": "a-quiet-appi"}, {"id": APP, "name": "shop-appi"}])])
    from healthconsole.core import investigate
    from healthconsole.core.explore import appinsights as ai
    counts = {APP: {"requests/count": (400, 380), "requests/failed": (40, 2), "exceptions/count": (50, 5)},
              quiet: {"requests/count": (10, 10), "requests/failed": (0, 0), "exceptions/count": (0, 0)}}
    monkeypatch.setattr(ai, "last_hour", lambda c, rows: [(r, counts[r["id"]], None) for r in rows])
    found = investigate.components(client, [SUB])["components"]
    assert [c["name"] for c in found] == ["shop-appi", "a-quiet-appi"]
    first = found[0]
    assert first["status"] == "warning" and first["failed_pct"] == 10.0 and first["week"]["failed_pct"] == 0.5
    assert first["causes"] == ["failed", "exceptions"]
    assert first["apps"] == ["shop-api"] and found[1]["status"] is None and found[1]["causes"] == []
