from healthconsole.core.health import HealthOptions
from datetime import datetime, timedelta, timezone

from flask import Flask

from healthconsole.core import baseline, config, report
from healthconsole.core.explore import activity
from healthconsole.core.model import Node, Signal, Snapshot
from healthconsole.web import create_blueprint
from tests.conftest import SUB, FakeClient, rid

APP = rid("rg", "Microsoft.Web/sites", "shop")


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_operations_collapse_by_correlation_and_keep_the_last_status():
    events = [
        {"correlationId": "c1", "operationName": {"value": "write", "localizedValue": "Update site"},
         "status": {"localizedValue": "Started"}, "eventTimestamp": "2026-11-05T10:00:00Z", "caller": "a@x"},
        {"correlationId": "c1", "operationName": {"value": "write", "localizedValue": "Update site"},
         "status": {"localizedValue": "Failed"}, "eventTimestamp": "2026-11-05T10:01:00Z", "caller": "a@x", "level": "Error"},
        {"correlationId": "c2", "operationName": {"value": "restart", "localizedValue": "Restart site"},
         "status": {"localizedValue": "Succeeded"}, "eventTimestamp": "2026-11-05T11:00:00Z", "caller": "b@x"},
    ]
    rows = activity.operations(events)
    assert [r["operation"] for r in rows] == ["Restart site", "Update site"]
    assert rows[1] == {"at": "2026-11-05T10:00:00Z", "operation": "Update site", "status": "Failed", "caller": "a@x",
                       "level": "Error"}


def test_repeated_operations_and_alerts_fold_into_one_row():
    keys = [{"correlationId": f"k{i}", "operationName": {"value": "listSecrets", "localizedValue": "Get subscription keys"},
             "status": {"localizedValue": "Succeeded"}, "eventTimestamp": f"2026-11-05T1{i}:00:00Z", "caller": "gw"}
            for i in range(5)]
    update = {"correlationId": "u", "operationName": {"value": "write", "localizedValue": "Update service"},
              "status": {"localizedValue": "Succeeded"}, "eventTimestamp": "2026-11-05T12:30:00Z", "caller": "me"}
    rows = activity.operations(keys + [update])
    assert [(r["operation"], r.get("count")) for r in rows] == [("Get subscription keys", 5), ("Update service", None)]
    assert rows[0]["first"] == "2026-11-05T10:00:00Z" and rows[0]["at"] == "2026-11-05T14:00:00Z"
    fired = [{"name": "cpu", "severity": 3, "started": f"2026-11-05T1{i}:00:00Z", "condition": "Resolved"} for i in range(4)]
    folded = activity.fold_alerts(fired + [{"name": "mem", "severity": 2, "started": "2026-11-05T09:00:00Z"}])
    assert [(a["name"], a.get("count")) for a in folded] == [("cpu", 4), ("mem", None)]


def test_health_history_is_limited_to_the_window():
    client = FakeClient(gets={"availabilityStatuses": {"value": [
        {"properties": {"occuredTime": _iso(30), "availabilityState": "Unavailable"}},
        {"properties": {"occuredTime": _iso(60 * 30), "availabilityState": "Available"}}]}})
    result = activity.activity(client, APP, hours=6)
    assert [h["state"] for h in result["health_history"]] == ["Unavailable"]


def test_activity_endpoint_window_and_alert_status():
    def alerts(kql):
        return [{"name": "cpu", "severity": "Sev1", "condition": "Fired", "started": _iso(5)},
                {"name": "mild", "severity": "Sev3", "condition": "Resolved", "started": _iso(50)}]

    class Client(FakeClient):
        def refresh(self):
            pass

    fake = Client(query_handlers=[("AlertsManagementResources", alerts)])
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: fake, config.load("/nonexistent"), authorize=lambda: None))
    web = app.test_client()
    assert web.get(f"/api/explore/activity?id={APP}&hours=5").status_code == 400
    body = web.get(f"/api/explore/activity?id={APP}&hours=168").get_json()
    assert [a["status"] for a in body["alerts"]] == ["critical", "info"]
    assert "ago(168h)" in fake.queries[0]


def node(name, *signals):
    return Node(id=rid("rg", "Microsoft.Web/sites", name), name=name, type="microsoft.web/sites", display_type="Web App",
                hop=0, resource_group="rg", signals=list(signals))


def sig(name, value, base, status="info"):
    return Signal("heuristic", name, status, value=value, baseline=base, unit="Count")


def test_baseline_lists_what_doubled_or_halved():
    nodes = [node("a", sig("Requests (Total)", 100, 400), sig("CPU (Average)", 50, 45)),
             node("b", sig("Http5xx (Total)", 12, 0, "warning"), sig("Errors (Total)", 0, 0)),
             node("c", sig("Requests (Total)", 900, 100)),
             node("d", sig("Requests (Total)", 5, None))]
    result = baseline.compare(nodes, 15)
    assert result["compared"] == 3 and result["without_baseline"] == 1
    assert [(m["name"], m["direction"]) for m in result["movers"]] == [("b", "new"), ("c", "up"), ("a", "down")]
    assert baseline.describe(result["movers"][1]) == "9x last week"
    assert baseline.describe(result["movers"][2]) == "4.0x lower than last week"


def test_group_report_has_the_baseline_section():
    nodes = [node("c", sig("Requests (Total)", 900, 100))]
    snap = Snapshot(root_id="/subscriptions/x/resourcegroups/rg", tenant_id="t", taken_by="me", taken_at="now", depth=1,
                    nodes=nodes, edges=[], kind="group", title="Resource group rg")
    snap.baseline = baseline.compare(nodes, 15)
    text = report.render(snap, "md").decode()
    assert "## Against last week (same 15 minutes, a week earlier)" in text and "9x last week" in text
    assert report.render(snap, "pdf").startswith(b"%PDF")
    assert snap.to_dict()["baseline"]["movers"][0]["name"] == "c"


def test_the_activity_log_is_read_in_slices_at_once_and_merged_newest_first():
    from datetime import datetime, timezone
    from healthconsole.core.explore import activity
    import re
    filters = []

    class Pages:  # slices arrive in any order (threads), so each answers from its own start time
        def get_pages(self, path, api_version, params=None, max_pages=10):
            filters.append(params["$filter"])
            start = re.search(r"ge '([^']+)'", params["$filter"]).group(1)
            oldest = start.startswith("2026-11-12T12")
            return [{"eventDataId": f"from {start}", "eventTimestamp": start},
                    {"eventDataId": "edge", "eventTimestamp": "2026-11-12T12:00:00Z"}], oldest
    events, more = activity._activity_log(Pages(), "/subscriptions/s/resourceGroups/r/providers/p/x", "2026-11-12T12:00:00Z",
                                          now=datetime(2026, 11, 13, 12, tzinfo=timezone.utc))
    assert len(filters) == activity.ACTIVITY_LOG_SLICES
    assert sum("eventTimestamp le" not in f for f in filters) == 1  # only the newest slice is open-ended
    times = [e["eventTimestamp"] for e in events]
    assert times == sorted(times, reverse=True) and len(events) == activity.ACTIVITY_LOG_SLICES + 1  # "edge" once
    assert more  # one slice had more pages


def test_the_baseline_change_setting_decides_what_moved():
    nodes = [node("a", sig("Requests (Total)", 300, 100)), node("b", sig("Requests (Total)", 150, 100))]
    assert [m["name"] for m in baseline.compare(nodes, 15)["movers"]] == ["a"]  # default 200%: tripled moved, +50% didn't
    wide = baseline.compare_with(nodes, HealthOptions(baseline_change_percent=400))
    assert wide["movers"] == [] and wide["factor"] == 4  # 300% is under 400%
    narrow = baseline.compare_with(nodes, HealthOptions(baseline_change_percent=150))
    assert [m["name"] for m in narrow["movers"]] == ["a", "b"]


def test_a_mover_carries_azures_display_name():
    shoebox = Signal("profile", "SaturationShoebox (Average)", "info", value=0.4, baseline=0, unit="Percent",
                     label="Overall Vault Saturation")
    movers = baseline.compare([node("kv", shoebox), node("x", sig("Requests (Total)", 9, 0))], 15)["movers"]
    assert {m["name"]: m["label"] for m in movers} == {"kv": "Overall Vault Saturation", "x": "Requests"}
