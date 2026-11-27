import types
import pytest

from healthconsole.core import health, profiles
from healthconsole.core.health import HealthOptions, _Check, _evaluate
from healthconsole.core.model import CRITICAL, HEALTHY, INFO, UNKNOWN, WARNING, Node, worst
from healthconsole.core.profiles import MetricSpec, ProfileError
from tests.conftest import SUB, FakeClient, rid

REDIS = rid("rg", "Microsoft.Cache/redis", "cache")


def test_worst_ignores_info_and_unknown():
    assert worst([HEALTHY, INFO, UNKNOWN]) == HEALTHY
    assert worst([HEALTHY, WARNING, CRITICAL]) == CRITICAL
    assert worst([INFO]) == UNKNOWN
    assert worst([]) == UNKNOWN


@pytest.mark.parametrize("value,expected", [(95, CRITICAL), (85, WARNING), (50, HEALTHY)])
def test_profile_thresholds(value, expected):
    check = _Check(MetricSpec("cpu", "Average", "GreaterThan", warn=80, crit=90), "profile")
    assert _evaluate(check, value, None, "Percent", HealthOptions()).status == expected


def test_less_than_thresholds():
    check = _Check(MetricSpec("Availability", "Average", "LessThan", warn=99.9, crit=99), "profile")
    assert _evaluate(check, 98.0, None, "Percent", HealthOptions()).status == CRITICAL
    assert _evaluate(check, 100.0, None, "Percent", HealthOptions()).status == HEALTHY


@pytest.mark.parametrize("severity,expected", [(1, CRITICAL), (2, WARNING), (3, INFO)])
def test_breached_alert_rule_maps_severity(severity, expected):
    check = _Check(MetricSpec("mem", "Average", "GreaterThan", crit=80), "alert rule",
                   rule="mem-high", severity=severity)
    signal = _evaluate(check, 85, None, "Percent", HealthOptions())
    assert signal.status == expected
    assert signal.threshold == "> 80"


def test_rule_not_breached_is_healthy_and_dynamic_is_info():
    healthy = _Check(MetricSpec("mem", "Average", "GreaterThan", crit=80), "alert rule", severity=1)
    assert _evaluate(healthy, 10, None, "", HealthOptions()).status == HEALTHY
    dynamic = _Check(MetricSpec("mem", "Average", "GreaterThan"), "alert rule", severity=1, dynamic=True)
    assert _evaluate(dynamic, 10, None, "", HealthOptions()).status == INFO


def test_no_data_and_baseline_notes():
    check = _Check(MetricSpec("errors", "Total"), "heuristic")
    assert "no data" in _evaluate(check, None, None, "", HealthOptions()).detail
    assert "3.0x last week" in _evaluate(check, 30, 10, "", HealthOptions()).detail


def test_heuristic_picks_problem_metrics_only():
    definitions = {name.lower(): {"name": name, "unit": "", "primary": "Total"}
                   for name in ("Requests", "Http5xx", "ServerErrors", "BytesSent", "Availability")}
    picked = {c.spec.name for c in health._heuristic_checks(definitions, limit=10)}
    assert picked == {"Http5xx", "ServerErrors", "Availability"}


def _node():
    return Node(id=REDIS.lower(), name="cache", type="microsoft.cache/redis", display_type="Redis Cache",
                hop=0, subscription_id=SUB, location="canadacentral",
                properties={"properties": {"provisioningState": "Succeeded"}})


def test_assess_combines_layers():
    rules = [{"scope": REDIS.lower(), "rule": "mem", "severity": 2, "metric": "usedmemorypercentage",
              "namespace": "Microsoft.Cache/redis", "operator": "GreaterThan", "threshold": 80.0,
              "aggregation": "Maximum", "criterion": "StaticThresholdCriterion"}]
    alerts = [{"target": REDIS.lower(), "name": "cpu", "severity": "Sev1", "state": "New",
               "started": "2026-10-30", "service": "Platform"}]
    client = FakeClient(
        gets={
            "availabilityStatuses": {"properties": {"availabilityState": "Available", "summary": "ok"}},
            "metricDefinitions": {"value": [
                {"name": {"value": "usedmemorypercentage"}, "unit": "Percent", "primaryAggregationType": "Maximum"},
                {"name": {"value": "serverLoad"}, "unit": "Percent", "primaryAggregationType": "Maximum"}]},
            "/metrics": {"value": [
                {"name": {"value": "usedmemorypercentage"}, "timeseries": [{"data": [{"maximum": 85.0}]}]},
                {"name": {"value": "serverLoad"}, "timeseries": [{"data": [{"maximum": 20.0}]}]}]},
        },
        query_handlers=[("metricalerts", lambda q: rules),
                        ("AlertsManagementResources", lambda q: alerts)],
    )
    node = _node()
    health.assess(client, [node], HealthOptions(compare_last_week=False), profiles.load())
    by_name = {s.name: s for s in node.signals}
    assert by_name["provisioningState"].status == HEALTHY
    assert by_name["availability"].status == HEALTHY
    assert by_name["cpu"].status == CRITICAL
    assert by_name["usedmemorypercentage (Maximum)"].status in (WARNING, CRITICAL)
    assert by_name["serverLoad (Maximum)"].status == HEALTHY
    assert node.status == CRITICAL


def test_resource_health_not_supported_is_info():
    node = _node()
    health.assess(FakeClient(), [node], HealthOptions(), {})
    availability = next(s for s in node.signals if s.name == "availability")
    assert availability.status == INFO
    assert node.status == HEALTHY


def test_unreadable_nodes_stay_unknown():
    node = _node()
    node.readable = False
    health.assess(FakeClient(), [node], HealthOptions(), {})
    assert node.status == UNKNOWN


def test_built_in_profiles_parse():
    loaded = profiles.load()
    assert "microsoft.web/sites" in loaded
    assert all(p.metrics or p.states for p in loaded.values())


def test_profile_rejects_unknown_keys():
    with pytest.raises(ProfileError, match="unknown key"):
        profiles.parse({"type": "x", "metric": []})
    with pytest.raises(ProfileError, match="unknown key"):
        profiles.parse({"type": "x", "metrics": [{"name": "a", "aggregation": "Total", "threshold": 1}]})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "90"])
def test_profile_thresholds_must_be_finite_numbers(value):
    with pytest.raises(ProfileError, match="finite number"):  # NaN compares false, so it would read as healthy
        profiles.parse({"type": "x", "metrics": [{"name": "a", "aggregation": "Total", "crit": value}]})
    assert profiles.parse({"type": "x", "metrics": [{"name": "a", "aggregation": "Total", "warn": 1, "crit": 2.5}]}).metrics


def test_repeated_alerts_collapse_into_one_signal():
    alerts = [{"name": "rh", "severity": 4, "started": f"2026-10-30T0{i}:00", "service": "Resource Health"}
              for i in range(5)]
    signals = health._alert_signals(alerts, HealthOptions())
    assert len(signals) == 1
    assert "fired 5 times" in signals[0].detail and signals[0].status == INFO


def test_alert_rule_outranks_profile_for_same_metric():
    rules = [{"rule": "team", "severity": 2, "metric": "serverLoad", "namespace": "", "operator": "GreaterThan",
              "threshold": 95.0, "aggregation": "Maximum", "criterion": "StaticThresholdCriterion"}]
    client = FakeClient(gets={
        "metricDefinitions": {"value": [{"name": {"value": "serverLoad"}, "unit": "Percent",
                                         "primaryAggregationType": "Maximum"}]},
        "/metrics": {"value": [{"name": {"value": "serverLoad"}, "timeseries": [{"data": [{"maximum": 92.0}]}]}]},
    })
    signals = health._metric_signals(client, _node(), HealthOptions(compare_last_week=False),
                                     profiles.load()["microsoft.cache/redis"], rules)
    load = [s for s in signals if s.name.startswith("serverLoad")]
    assert [s.source for s in load] == ["alert rule"]
    assert load[0].status == HEALTHY


def test_type_without_signals_says_so():
    node = _node()
    node.properties = {}
    health.assess(FakeClient(), [node], HealthOptions(), {})
    assert any("no health signals" in s.detail for s in node.signals)
    assert node.status == UNKNOWN


def test_one_failing_source_keeps_the_others(monkeypatch):
    from healthconsole.core.arm import ArmTimeout

    def boom(*args, **kwargs):
        raise ArmTimeout("Azure didn't answer within 10s for GET /detectors.")
    monkeypatch.setattr(health, "_detector_signals", boom)
    node = _node()
    health.assess(FakeClient(gets={"availabilityStatuses": {"properties": {"availabilityState": "Available"}}}),
                  [node], HealthOptions(), {})
    by_name = {s.name: s for s in node.signals}
    assert by_name["availability"].status == HEALTHY
    assert by_name["detectors"].status == UNKNOWN and "didn't answer" in by_name["detectors"].detail
    assert node.status == HEALTHY


def test_types_without_detectors_are_remembered(monkeypatch):
    from healthconsole.core.explore import detectors
    detectors._without_detectors.clear()
    client = FakeClient(gets={"/detectors": None})
    storage = rid("rg", "Microsoft.Storage/storageAccounts", "one")
    assert detectors.run(client, storage) == {"supported": False, "results": []}
    calls = client.call_count
    detectors.run(client, rid("rg", "Microsoft.Storage/storageAccounts", "two"))
    assert client.call_count == calls  # second account skipped the listing


def test_detectors_that_never_work_count_as_none_and_401_as_unavailable(monkeypatch):
    from healthconsole.core.arm import ArmError, ArmTimeout
    from healthconsole.core.explore import detectors
    detectors._without_detectors.clear()

    class Client(FakeClient):
        def __init__(self, error):
            super().__init__()
            self.error = error

        def get(self, path, api_version, params=None, tolerate=(), timeout=None):
            raise self.error

    none = {"supported": False, "results": []}
    refused = rid("rg", "Microsoft.Cache/redis", "r")
    out = detectors.run(Client(ArmError("Azure returned 401: Unauthorized.", 401)), refused)
    assert out["supported"] is False and "refused the request (401)" in out["unavailable"]
    apim = rid("rg", "Microsoft.ApiManagement/service", "gw")
    assert detectors.run(Client(ArmError("Azure returned 404: ", 404)), apim) == none
    bus = rid("rg", "Microsoft.ServiceBus/namespaces", "bus")
    assert detectors.run(Client(ArmError("Azure returned 503: unavailable", 503)), bus) == none  # never works: none
    slow = rid("rg", "Microsoft.Storage/storageAccounts", "slow")
    assert detectors.run(Client(ArmTimeout("timed out")), slow) == none
    assert detectors.run(Client(None), slow) == none  # remembered, not asked again


def _impact(service, *regions):
    return [{"ImpactedService": service, "ImpactedRegions": [{"ImpactedRegion": r} for r in regions]}]


def test_service_health_groups_events_with_the_same_title():
    rows = [{"trackingId": t, "title": "VM allocation failures", "eventType": "ServiceIssue", "lastUpdate": u,
             "impact": _impact("Virtual Machines", "Canada Central")} for t, u in (("AAA", "1"), ("BBB", "3"), ("AAA", "2"))]
    rows.append({"trackingId": "CCC", "title": "PostgreSQL maintenance", "eventType": "PlannedMaintenance",
                 "lastUpdate": "2", "impact": _impact("PostgreSQL", "Canada Central")})
    rows.append({"trackingId": "DDD", "title": "Elsewhere", "eventType": "ServiceIssue", "lastUpdate": "9",
                 "impact": _impact("Storage", "West Europe")})
    client = FakeClient(query_handlers=[("ServiceHealthResources", lambda q: rows)])
    node = Node(id=REDIS, name="cache", type="microsoft.cache/redis", display_type="Redis", hop=0, location="canadacentral", subscription_id=SUB)
    events = health._service_health(client, [node], [])
    assert [(e["title"], e["tracking_ids"], e["last_update"]) for e in events] == [
        ("VM allocation failures", ["AAA", "BBB"], "3"), ("PostgreSQL maintenance", ["CCC"], "2")]


def test_a_fired_alert_says_what_fired_and_where_to_look():
    from healthconsole.core import alert_detail
    metric = {"name": "agw-capacity", "severity": 2, "started": "2026-11-11T12:02:26.5851677Z", "service": "Platform",
              "alert_id": "/subscriptions/s/providers/Microsoft.AlertsManagement/alerts/a1",
              "rule": "/subscriptions/s/resourceGroups/rg/providers/microsoft.insights/metricAlerts/agw-capacity",
              "criterion": {"metricName": "CapacityUnits", "timeAggregation": "Average", "operator": "GreaterThan",
                            "threshold": "80", "metricValue": 92.31}, "window": "PT5M"}
    signal = health._alert_signals({"x": [metric]}["x"], HealthOptions())[0]
    assert "Average CapacityUnits was 92.31, alert when > 80 (over 5 min)" in signal.detail
    assert "T12:02:26" not in signal.detail
    assert signal.at == metric["started"] and [l["label"] for l in signal.links] == ["Alert", "Alert rule"]
    log = {"searchQuery": "AppRequests | where Success == false", "metricValue": 12.0, "threshold": "0",
           "operator": "GreaterThan", "timeAggregation": "Count",
           "linkToFilteredSearchResultsUI": "https://portal.azure.com#@t/blade/Microsoft_Azure_Monitoring_Logs/x"}
    assert alert_detail.condition(log, "PT1H") == "Count of query results was 12, alert when > 0 (over 1 h)"
    assert alert_detail.links({"criterion": log})[0]["label"] == "Query results"
    assert alert_detail.links({"criterion": {**log, "linkToFilteredSearchResultsUI": "https://evil.example/x"}}) == []
    assert alert_detail.condition(None) == "" and alert_detail.duration("P1D") == "1 day"


def test_detectors_run_after_every_other_signal_is_published(monkeypatch):
    events = []
    monkeypatch.setattr(health, "_detector_signals", lambda client, node: events.append("detectors") or [
        health.Signal("detector", "Node pressure", WARNING, "memory pressure")])
    node = Node(id=REDIS, name="cache", type="microsoft.cache/redis", display_type="Redis", hop=0, location="canadacentral",
                subscription_id=SUB)
    health.assess(FakeClient(), [node], HealthOptions(), {}, on_quick=lambda: events.append(f"quick:{node.status}"))
    assert events[0].startswith("quick:") and events[1] == "detectors"  # health shown before Azure's detectors answer
    assert node.status == WARNING and any(s.name == "Node pressure" for s in node.signals)
    assert not any((s.source, s.name) == ("console", "signals") for s in node.signals)


def test_without_detectors_nothing_waits(monkeypatch):
    called = []
    monkeypatch.setattr(health, "_detector_signals", lambda client, node: called.append(1) or [])
    node = Node(id=REDIS, name="cache", type="microsoft.cache/redis", display_type="Redis", hop=0, subscription_id=SUB)
    health.assess(FakeClient(), [node], HealthOptions(detectors=False), {}, on_quick=lambda: called.append("quick"))
    assert called == []



def test_metric_signals_carry_the_display_name_and_follow_the_change_setting():
    client = FakeClient(gets={
        "metricDefinitions": {"value": [{"name": {"value": "serverLoad", "localizedValue": "Server Load"}, "unit": "Percent",
                                         "primaryAggregationType": "Maximum"}]},
        "/metrics": {"value": [{"name": {"value": "serverLoad"}, "timeseries": [{"data": [{"maximum": 30.0}]}]}]},
    })
    redis = profiles.load()["microsoft.cache/redis"]
    signals = health._metric_signals(client, _node(), HealthOptions(), redis, [])
    load = next(s for s in signals if s.name.startswith("serverLoad"))
    assert load.label == "Server Load" and load.baseline == 30.0 and "last week" not in load.detail
    assert health._evaluate(health._Check(spec=redis.metrics[0], source="profile"), 30.0, 10.0, "Percent",
                            HealthOptions(baseline_change_percent=400)).detail == ""  # 3x is under the setting's 4x


def test_the_health_check_runs_the_same_detectors_as_the_diagnostics_tab(monkeypatch):
    from healthconsole.core import health
    from healthconsole.core.explore import detectors
    asked = []
    monkeypatch.setattr(detectors, "run", lambda client, rid, **kw: asked.append(kw) or {"results": []})
    health._detector_signals(FakeClient(), types.SimpleNamespace(id="/r"))
    assert asked == [{}]  # no narrower words: the plan's own, as the tab uses
