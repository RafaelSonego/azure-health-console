from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from healthconsole.core import alert_check, azure_alerts, config
from healthconsole.core.integrations import nr_data
from healthconsole.core.integrations.provider import ALERT_HISTORY, Provider, ProviderError
from tests.conftest import SUB, FakeClient, rid

SETTINGS = dict(config.DEFAULTS["alert_check"])
START = datetime(2026, 11, 1, tzinfo=timezone.utc)


def hourly(values):
    return {"points": [[(START + timedelta(hours=i)).isoformat(), v] for i, v in enumerate(values)], "bucket_minutes": 60}


def rule(**extra):
    return {"source": "test", "name": "r", "query": {"label": "q", "text": "q"}, "operator": ">", "threshold": 100,
            "window_minutes": 60, "duration_minutes": 60, "additive": False, "function": "max", **extra}


def kinds(result):
    return [(v["status"], v["setting"]) for v in result["verdicts"]]


DAYS = 8 * 24 + 2  # a day more than alert_check.days, plus the bucket still filling in


def test_a_rule_true_most_of_the_time_is_not_new_and_says_which_setting_decided():
    result = alert_check.assess(rule(), hourly([150] * DAYS), SETTINGS)
    n = result["numbers"]
    assert n["values"] == 7 * 24 and n["breaching"] == 7 * 24 and n["breaching_percent"] == 100.0
    assert n["threshold_vs_usual"] == 0.67 and n["now_vs_week"] == 1.0
    assert kinds(result)[0] == ("warning", "alert_check.always_on_percent")
    assert "Not new" in result["verdicts"][0]["text"] and "below the values" in result["verdicts"][0]["text"]
    assert "at least 50%" in result["verdicts"][0]["rule"]


def test_true_now_but_rarely_before_is_unusual_and_a_rare_cleared_one_is_ok():
    values = [10] * (DAYS - 2) + [500, 10]  # the last bucket is still filling in, so "now" is the one before
    assert kinds(alert_check.assess(rule(), hourly(values), SETTINGS))[0] == ("critical", "alert_check.rare_percent")
    cleared = [10] * (DAYS - 5) + [500, 10, 10, 10, 10]
    first = alert_check.assess(rule(), hourly(cleared), SETTINGS)["verdicts"][0]
    assert first["status"] == "ok" and "cleared" in first["text"]


def test_in_between_and_a_rule_below_its_threshold():
    values = [150 if i % 5 == 0 else 50 for i in range(DAYS)]  # true 20% of the time
    assert kinds(alert_check.assess(rule(), hourly(values), SETTINGS))[0][0] == "info"
    low = alert_check.assess(rule(operator="<", threshold=90), hourly([25] * DAYS), SETTINGS)
    assert low["numbers"]["breaching_percent"] == 100.0 and "above the values" in low["verdicts"][0]["text"]


def test_last_week_comparison_uses_the_change_setting_and_handles_zero():
    values = [100] * (DAYS - 2) + [200, 0]
    result = alert_check.assess(rule(threshold=1000), hourly(values), SETTINGS)
    assert result["numbers"]["now_vs_week"] == 2.0 and ("warning", "alert_check.change_percent") in kinds(result)
    calmer = alert_check.assess(rule(threshold=1000), hourly(values), {**SETTINGS, "change_percent": 150})
    assert ("ok", "alert_check.change_percent") in kinds(calmer)  # 2× is within 2.5×
    new = alert_check.assess(rule(threshold=1000), hourly([0] * (DAYS - 2) + [5, 0]), SETTINGS)
    assert any(v["text"].startswith("New since last week") for v in new["verdicts"])
    gap = list(values)
    week = DAYS - 2 - 168
    gap[week - 1:week + 2] = [50, None, 50]  # last week's bucket empty: the nearest one within a bucket stands in
    assert alert_check.assess(rule(threshold=1000), hourly(gap), SETTINGS)["numbers"]["now_vs_week"] == 4.0


def test_too_little_history_and_a_dynamic_threshold():
    assert kinds(alert_check.assess(rule(), hourly([1] * 5), SETTINGS)) == [("unknown", "alert_check.min_buckets")]
    dynamic = alert_check.assess(rule(threshold=None), hourly([1] * DAYS), SETTINGS)
    assert "no fixed threshold" in dynamic["verdicts"][0]["text"] and "breaching" not in dynamic["numbers"]


def test_counts_are_divided_down_to_the_rules_window_and_levels_are_not():
    factor, how = alert_check.scale_for(rule(additive=True, window_minutes=1, function="count"), 30)
    assert factor == pytest.approx(1 / 30) and "divided by 30" in how
    assert alert_check.scale_for(rule(additive=False, window_minutes=1), 30)[0] == 1.0
    series = {**hourly([3000] * DAYS), "bucket_minutes": 30}
    scaled = alert_check.assess(rule(additive=True, window_minutes=1, threshold=50), series, SETTINGS)
    assert scaled["numbers"]["now"] == 100 and scaled["numbers"]["breaching_percent"] == 100.0


def test_the_smallest_bucket_that_fits_one_read():
    assert alert_check.bucket_for(8, 1, 366) == 60  # 8 days in 366 buckets needs 31.5 minutes
    assert alert_check.bucket_for(8, 5, azure_alerts.MAX_POINTS, azure_alerts.GRAINS) == 5  # Azure: the rule's own window


# ---- Azure Monitor metric alerts

ALERT = f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/a1"
RULE = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/lb-availability"
LB = rid("rg", "Microsoft.Network/loadBalancers", "lb")


def azure_client(metric_values, dimensions=None, kind="metricAlerts"):
    rule_id = RULE.replace("metricAlerts", kind)
    rule_body = {"name": "lb-availability", "properties": {"windowSize": "PT5M", "scopes": [LB], "criteria": {"allOf": [
        {"criterionType": "StaticThresholdCriterion", "metricName": "DipAvailability", "metricNamespace": "Microsoft.Network/loadBalancers",
         "operator": "LessThan", "threshold": 90, "timeAggregation": "Average", "dimensions": dimensions}]}}}
    seen = []

    def metrics(params):
        seen.append(params)
        series = [{"metadatavalues": [{"value": name}], "data": [{"timeStamp": (START + timedelta(minutes=5 * i)).isoformat(), "average": v}
                                                                 for i, v in enumerate(values)]} for name, values in metric_values.items()]
        return {"value": [{"timeseries": series, "unit": "Percent"}]}
    client = FakeClient(query_handlers=[("AlertsManagementResources", lambda q: [{"rule": rule_id, "target": LB, "started": ""}])],
                        gets={"metricAlerts/lb-availability": rule_body, "/providers/Microsoft.Insights/metrics": metrics})
    return client, seen


def test_azure_reads_the_rule_and_its_history_at_the_rules_own_window():
    client, seen = azure_client({"": [25] * 2400})
    result = alert_check.check(azure_alerts.AzureMetricAlerts(client), ALERT, SETTINGS)
    r = result["rule"]
    assert (r["operator"], r["threshold"], r["window_minutes"], r["additive"]) == ("<", 90, 5, False)
    assert seen[0]["interval"] == "PT5M" and seen[0]["metricnamespace"] == "Microsoft.Network/loadBalancers"
    assert result["bucket_minutes"] == 5 and "the rule's own window" in result["scaled"]
    assert result["numbers"]["breaching_percent"] == 100.0 and result["source"] == "Azure Monitor"
    assert r["links"] == [{"label": "Alert rule in the portal", "resource": RULE}]


def test_azure_takes_the_dimension_nearest_to_firing_and_filters_included_values():
    dims = [{"name": "BackendIPAddress", "operator": "Include", "values": ["10.0.0.4", "10.0.0.5"]}]
    client, seen = azure_client({"10.0.0.4": [100] * 2400, "10.0.0.5": [40] * 2400}, dims)
    result = alert_check.check(azure_alerts.AzureMetricAlerts(client), ALERT, SETTINGS)
    assert seen[0]["$filter"] == "(BackendIPAddress eq '10.0.0.4' or BackendIPAddress eq '10.0.0.5')"
    assert result["numbers"]["now"] == 40  # a "<" rule fires on the lowest
    assert any("2 dimension values" in note for note in result["notes"])


def test_a_dimension_from_the_rule_cant_rewrite_the_metric_filter():
    dims = [{"name": "Location", "operator": "Include", "values": ["west' or Location eq 'east"]}]
    client, seen = azure_client({"west": [100] * 2400}, dims)
    alert_check.check(azure_alerts.AzureMetricAlerts(client), ALERT, SETTINGS)
    assert seen[0]["$filter"] == "(Location eq 'west'' or Location eq ''east')"  # one literal, the quotes doubled
    bad, _ = azure_client({"": [1]}, [{"name": "x eq 'y' or Location", "operator": "Include", "values": ["a"]}])
    with pytest.raises(azure_alerts.CheckError, match="dimension name"):
        alert_check.check(azure_alerts.AzureMetricAlerts(bad), ALERT, SETTINGS)


def test_azure_names_what_it_cant_check():
    client, _ = azure_client({"": [1]}, kind="scheduledQueryRules")
    with pytest.raises(azure_alerts.CheckError, match="Only metric alert rules"):
        azure_alerts.AzureMetricAlerts(client).alert_rule(ALERT)


# ---- New Relic

class FakeNerdGraph:
    def __init__(self, conditions, rows):
        self.conditions, self.rows, self.sent = conditions, rows, []

    def nrql_conditions(self, account_id, name):
        return self.conditions

    def nrql(self, account_id, nrql):
        self.sent.append((account_id, nrql))
        return self.rows


CONDITION = {"id": 7, "name": "set commands > 100k per min", "enabled": True, "type": "STATIC",
             "nrql": {"query": "SELECT sum(setcommands) FROM AzureRedisCacheSample WHERE name = 'cache'"},
             "terms": [{"operator": "ABOVE", "threshold": 100000, "thresholdDuration": 1800, "priority": "CRITICAL",
                        "thresholdOccurrences": "ALL"}], "signal": {"aggregationWindow": 60}}
ISSUE = {"account_id": 42, "condition": "Set commands > 100K per min", "priority": "critical"}


def nr(conditions, rows=()):
    provider = nr_data.NewRelicProvider({"name": "NR", "type": "newrelic", "account_ids": [42]}, client=FakeNerdGraph(conditions, list(rows)))
    return provider, provider.client


def test_new_relic_finds_the_condition_by_name_and_reads_its_own_query():
    provider, graph = nr([CONDITION], [{"beginTimeSeconds": 1_762_000_000 + 1800 * i, "sum.setcommands": 15_000_000} for i in range(450)])
    assert ALERT_HISTORY in provider.capabilities
    result = alert_check.check(provider, ISSUE, SETTINGS)
    r = result["rule"]
    assert (r["operator"], r["threshold"], r["window_minutes"], r["duration_minutes"], r["additive"]) == (">", 100000, 1, 30, True)
    assert {account for account, _ in graph.sent} == {42} and len(graph.sent) == 7  # 1-min window: 5-min buckets, 7 reads
    assert all(n.startswith(CONDITION["nrql"]["query"] + " SINCE ") and n.endswith(" TIMESERIES 5 minutes LIMIT MAX")
               for _, n in graph.sent)
    assert result["numbers"]["now"] == 15_000_000 / 5  # a 5-minute sum, per the condition's one minute
    assert result["verdicts"][0]["setting"] == "alert_check.always_on_percent"


def test_new_relic_refuses_to_guess_between_conditions_with_one_name():
    provider, _ = nr([CONDITION, {**CONDITION, "id": 8}])
    with pytest.raises(ProviderError, match="2 NRQL conditions"):
        provider.alert_rule(ISSUE)
    with pytest.raises(ProviderError, match="No NRQL condition"):
        nr([])[0].alert_rule(ISSUE)


def test_new_relic_reads_a_rule_only_in_the_connections_own_accounts():
    provider, graph = nr([CONDITION])
    graph.nrql_conditions = lambda account, name: pytest.fail(f"read account {account}")
    with pytest.raises(ProviderError, match="isn't one of NR's accounts"):
        provider.alert_rule({**ISSUE, "account_id": 99})


def test_new_relic_facets_take_the_worst_and_percentiles_are_read():
    provider, _ = nr([CONDITION])
    base = 1_762_000_000
    provider.client.rows = [{"beginTimeSeconds": base, "facet": "a", "sum.setcommands": 5},
                            {"beginTimeSeconds": base, "facet": "b", "sum.setcommands": 9},
                            {"beginTimeSeconds": base + 3600, "facet": "a", "percentile.duration": {"95": 3.5}}]
    series = provider.alert_series(provider.alert_rule(ISSUE), 8)
    assert [p[1] for p in series["points"]] == [9.0, 3.5] and "2 facets" in series["notes"][0]


def test_a_new_relic_query_error_shows_the_nrql_sent():
    provider, graph = nr([CONDITION])

    def refuse(account_id, nrql):
        raise nr_data.NewRelicError("New Relic said: syntax error before:", "access")
    graph.nrql = refuse
    with pytest.raises(nr_data.NewRelicError, match="The NRQL sent: SELECT sum.* UNTIL .*TIMESERIES 5 minutes LIMIT MAX") as raised:
        provider.alert_series(provider.alert_rule(ISSUE), 8)
    assert raised.value.kind == "access"


def test_new_relic_reads_the_conditions_own_window_and_scales_a_rate_of_a_level():
    rate = {**CONDITION, "nrql": {"query": "from AzureRedisCacheSample select rate(latest(setCommands.Total), 1 minute) where x = 'y'"},
            "signal": {"aggregationWindow": 300}}
    provider, graph = nr([rate])
    r = provider.alert_rule(ISSUE)
    assert (r["function"], r["additive"], r["per_bucket"], r["window_minutes"]) == ("rate of latest", False, True, 5)
    assert r["links"] == []  # no entity GUID or runbook on this condition
    linked = nr([{**rate, "entityGuid": "Q09ORA", "runbookUrl": "https://wiki/runbook"}])[0].alert_rule(ISSUE)["links"]
    assert linked == [{"label": "Condition in New Relic", "url": "https://one.newrelic.com/redirect/entity/Q09ORA"},
                      {"label": "Runbook", "url": "https://wiki/runbook"}]
    provider.alert_series(r, 8)
    assert len(graph.sent) == 7 and all("TIMESERIES 5 minutes" in n for _, n in graph.sent)  # 2,304 buckets, 7 reads
    assert alert_check.scale_for(r, 60)[0] == 12  # a 60-minute rate of a level is 12x too small for a 5-minute window
    assert alert_check.scale_for(r, 5)[0] == 1.0
    summed = {**rate, "nrql": {"query": "SELECT rate(sum(x), 1 minute) FROM T"}}
    assert nr([summed])[0].alert_rule(ISSUE)["per_bucket"] is False  # a rate of a sum doesn't change with the bucket
    filtered = {**rate, "nrql": {"query": "From Metric Select filter(max(`cpu_percent`), WHERE entity.name = 'db')"}}
    assert (lambda r: (r["function"], r["additive"]))(nr([filtered])[0].alert_rule(ISSUE)) == ("max", False)


def test_spans_tile_the_days_on_bucket_boundaries():
    now = datetime(2026, 11, 16, 12, 3, 17, tzinfo=timezone.utc)
    parts = alert_check.spans(8, 5, 366, now)
    assert parts[0][0] <= now - timedelta(days=8) and parts[0][0].timestamp() % 300 == 0 and parts[-1][1] == now
    assert all(a[1] == b[0] for a, b in zip(parts, parts[1:])) and all(b - a <= timedelta(minutes=5 * 366) for a, b in parts)
    assert alert_check.bucket_for(8, 5, 366, reads=8) == 5 and alert_check.bucket_for(8, 1, 366, reads=8) == 5
    assert alert_check.bucket_for(8, 7, 366, reads=8) == 7  # the window itself is offered as a size


def test_a_tool_without_alert_history_says_so():
    class Bare(Provider):
        label = "Some tool"
    with pytest.raises(ProviderError, match="Some tool can't give an alert's rule"):
        Bare({"name": "x"}).alert_rule({})


# ---- the route and the settings

def test_the_route_checks_once_and_keeps_the_answer(tmp_path):
    from healthconsole.web import create_blueprint
    client, seen = azure_client({"": [25] * 2400})
    app = Flask("t", static_folder=None)
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load(tmp_path / "none.yaml"), config_file=tmp_path / "c.yaml", authorize=lambda: None))
    web = app.test_client()
    first = web.get(f"/api/alert-check?source=azure&id={ALERT}").get_json()
    assert first["available"] and first["numbers"]["breaching_percent"] == 100.0
    web.get(f"/api/alert-check?source=azure&id={ALERT}")
    assert len(seen) == 1  # kept, not asked again
    assert web.get("/api/alert-check?source=azure&id=../x").status_code == 400
    assert web.get("/api/alert-check?source=other").status_code == 400
    refused = FakeClient(query_handlers=[("AlertsManagementResources", lambda q: [])])
    app2 = Flask("t2", static_folder=None)
    app2.register_blueprint(create_blueprint(lambda tenant: refused, config.load(tmp_path / "none.yaml"), config_file=tmp_path / "c.yaml", authorize=lambda: None))
    gone = app2.test_client().get(f"/api/alert-check?source=azure&id={ALERT}").get_json()
    assert gone == {"available": False, "reason": "That alert isn't in Azure's alert history any more."}


def test_alert_check_settings_are_validated(tmp_path):
    file = tmp_path / "c.yaml"
    for bad in ("alert_check: {rare_percent: 60}", "alert_check: {days: 0}", "alert_check: {always_on_percent: 101}"):
        file.write_text(bad)
        with pytest.raises(config.ConfigError, match="alert_check"):
            config.load(file)


def test_the_command_prints_the_same_check(monkeypatch, capsys, tmp_path):
    from healthconsole import cli
    client, _ = azure_client({"": [25] * 2400})
    monkeypatch.setattr(cli, "_client", lambda args, settings: client)
    monkeypatch.setenv("AHC_CONFIG", str(tmp_path / "none.yaml"))
    cli.main(["alert-check", "--azure", ALERT])
    out = capsys.readouterr().out
    assert "lb-availability (Azure Monitor)" in out and "fires when < 90" in out
    assert "[warning] Not new" in out and "(alert_check.always_on_percent)" in out
    with pytest.raises(SystemExit, match="give --azure"):
        cli.main(["alert-check", "--connection", "NR"])
