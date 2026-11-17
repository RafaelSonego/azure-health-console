import pytest
from flask import Flask

from healthconsole.core import alerts, config, settings_file
from healthconsole.core.arm import ArmError

from tests.conftest import SUB, FakeClient, rid

APP = rid("app-rg", "Microsoft.Web/sites", "shop")
DB = rid("data-rg", "Microsoft.Sql/servers", "orders")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def alert_row(resource_id, worst):
    return {"id": resource_id, "type": "microsoft.web/sites", "resourceGroup": "app-rg", "subscriptionId": SUB,
            "alerts": 1, "worst": worst, "lastFired": "2026-11-09T10:00:00Z", "names": ["cpu"]}


def client_with(state):
    """`state` is mutated by the test between ticks: alert rows, Service Health titles, or an error."""
    def alerts_rows(kql):
        if state.get("fail"):
            raise ArmError(state["fail"])
        return state.get("alerts", [])

    def health_rows(kql):
        return [{"title": t, "eventType": "ServiceIssue", "subscriptions": 1, "events": 1, "impacts": []}
                for t in state.get("health", [])]

    subs = {"value": [{"subscriptionId": SUB, "displayName": "sub-a", "state": "Enabled", "tenantId": "tenant-1"}]}
    return FakeClient(query_handlers=[("microsoft.alertsmanagement/alerts", alerts_rows),
                                      ("ServiceHealthResources", health_rows)],
                      gets={"/subscriptions": subs})


def source(client, rules, scopes=None, items=None, rows=None):
    scopes = scopes if scopes is not None else [{"name": "Shop", "subscriptions": ["sub-a"], "resource_groups": [],
                                                 "tag_filters": {}, "connections": {}}]
    return alerts.Source(
        client=client, rules=lambda: rules,
        saved_scope=lambda name: next((s for s in scopes if s["name"].lower() == (name or "").lower()), None),
        watchlist_items=lambda: items if items is not None else [],
        triage_options=lambda: {**config.DEFAULTS["health"], **config.DEFAULTS["triage"], **config.DEFAULTS["network"]},
        check_watchlist=lambda bound, wanted: rows(wanted) if callable(rows) else rows)


def scope_rule(**extra):
    return {"name": "Shop alerts", "kind": "scope", "scope": "Shop", "signals": ["status", "new_red", "service_health"],
            "interval_minutes": 5, "notify": True, "enabled": True, **extra}


def test_rule_validation():
    config.check_alert_rules([{"name": "w", "watchlist": True, "interval_minutes": 2}])
    config.check_alert_rules([{"name": "s", "scope": "Shop", "signals": ["new_red"]}])
    bad = [
        [{"name": "x", "scope": "Shop", "watchlist": True}],
        [{"name": "x"}],
        [{"name": "x", "scope": "Shop", "interval_minutes": 2}],  # a scope's floor is 5
        [{"name": "x", "watchlist": True, "interval_minutes": 31}],
        [{"name": "x", "watchlist": True, "signals": ["service_health"]}],
        [{"name": "x", "scope": "Shop", "signals": []}],
        [{"name": "x", "scope": "Shop", "sound": True}],
        [{"name": "x", "scope": "a"}, {"name": "X", "scope": "b"}],
    ]
    for rules in bad:
        with pytest.raises(config.ConfigError):
            config.check_alert_rules(rules)


def test_rules_are_saved_per_tenant_with_defaults(tmp_path):
    file = tmp_path / "config.yaml"
    file.write_text("# mine\ntriage:\n  scope: none\n")
    new = settings_file.apply_alert_rules(file, "contoso.example", [
        {"name": "Shop alerts", "kind": "scope", "scope": "Shop", "signals": ["new_red"], "interval_minutes": 10},
        {"name": "Watchlist", "kind": "watchlist", "interval_minutes": 2, "notify": False}])
    assert "# mine" in file.read_text()
    rules = config.alert_rules(new, {"id": "t", "domain": "contoso.example"})
    assert rules[0] == {"name": "Shop alerts", "kind": "scope", "scope": "Shop", "signals": ["new_red"],
                        "servicenow_priority": 2, "interval_minutes": 10, "notify": True, "notify_recovered": True, "enabled": True}
    assert rules[1]["kind"] == "watchlist" and rules[1]["signals"] == ["status"] and rules[1]["notify"] is False
    assert config.alert_rules(new, {"id": "other"}) == []
    with pytest.raises(config.ConfigError):
        settings_file.apply_alert_rules(file, "contoso.example", [{"name": "bad", "kind": "scope", "scope": "Shop",
                                                                    "interval_minutes": 2}])
    assert "Shop alerts" in file.read_text()  # nothing written when invalid


def test_scope_rule_fires_on_change_never_on_state():
    state, clock = {"alerts": [alert_row(APP, 2)]}, Clock()
    runner = alerts.AlertRunner(clock=clock)
    runner.register("t/o", source(client_with(state), [scope_rule()]))

    runner.tick()  # the starting point: already yellow, no alert
    assert runner.state("t/o")["events"] == []
    first = runner.state("t/o")["rules"][0]["result"]
    assert first["first"] is True and first["new"] is None and first["warning"] == 1 and first["critical"] == 0

    clock.now += 60
    runner.tick()  # not due yet
    assert runner.state("t/o")["rules"][0]["checks"] == 1

    state["alerts"] = [alert_row(APP, 0), alert_row(DB, 0)]
    state["health"] = ["Storage degraded"]
    clock.now += 300
    runner.tick()
    titles = [e["title"] for e in runner.state("t/o")["events"]]
    assert "shop: warning → critical" in titles
    result = runner.state("t/o")["rules"][0]["result"]
    assert result["new"] == 3 and result["critical"] == 2 and result["service_health"] == 1
    assert "New red: orders" in titles
    assert "Service Health: Storage degraded" in titles

    clock.now += 300
    runner.tick()  # still red: nothing new
    assert len(runner.state("t/o")["events"]) == 3
    assert runner.state("t/o")["rules"][0]["result"]["new"] == 0  # "nothing new", still tracking two reds


def test_an_alert_resolves_when_back_to_how_it_was_and_says_so():
    state, clock = {"alerts": [alert_row(APP, 2)]}, Clock()
    runner = alerts.AlertRunner(clock=clock)
    runner.register("t/o", source(client_with(state), [scope_rule(notify_recovered=False)]))
    runner.tick()
    state["alerts"], state["health"] = [alert_row(APP, 0)], ["Storage degraded"]
    clock.now += 300
    runner.tick()
    assert runner.state("t/o")["unseen"] == 2

    state["alerts"] = [alert_row(APP, 2)]  # still yellow, as at the start: back to how it was
    clock.now += 300
    runner.tick()
    view = runner.state("t/o")
    assert view["unseen"] == 1  # the Service Health event is still on
    recovered = view["events"][0]
    assert recovered["signal"] == "recovered" and recovered["title"] == "Recovered: shop"
    assert recovered["notify"] is False and recovered["detail"] == "was critical for 5 min"
    assert next(e for e in view["events"] if e["title"].startswith("shop:"))["resolved_note"] == "back to normal"

    state["health"] = []
    clock.now += 300
    runner.tick()
    view = runner.state("t/o")
    assert view["unseen"] == 0 and view["events"][0]["title"] == "Service Health over: Storage degraded"

    state["alerts"] = [alert_row(APP, 0)]  # red again: a new alert, not the old one
    clock.now += 300
    runner.tick()
    assert runner.state("t/o")["unseen"] == 1


def test_a_failed_card_is_not_a_recovery():
    state, clock = {"alerts": [alert_row(APP, 0)]}, Clock()
    runner = alerts.AlertRunner(clock=clock)
    runner.register("t/o", source(client_with(state), [scope_rule(signals=["status", "new_red"])]))
    runner.tick()
    state["fail"] = "Resource Graph throttled"
    clock.now += 300
    runner.tick()  # other cards answered, so the check ran; the alerts card is listed as partial
    rule = runner.state("t/o")["rules"][0]
    assert rule["partial"] and not rule["failing"]
    state.pop("fail")
    clock.now += 300
    runner.tick()  # the same red resource is back: not new
    assert runner.state("t/o")["events"] == []


def test_failing_checks_alert_once_and_recover():
    clock = Clock()
    runner = alerts.AlertRunner(clock=clock)
    scopes = [{"name": "Shop", "subscriptions": ["gone-*"], "resource_groups": [], "tag_filters": {}, "connections": {}}]
    src = source(client_with({}), [scope_rule()], scopes=scopes)
    runner.register("t/o", src)
    runner.tick()
    clock.now += 300
    runner.tick()
    events = runner.state("t/o")["events"]
    assert [e["signal"] for e in events] == ["check_failed"]
    assert "PIM role" in events[0]["detail"]
    scopes[0]["subscriptions"] = ["sub-a"]
    clock.now += 300
    runner.tick()
    events = runner.state("t/o")["events"]
    assert events[0]["signal"] == "check_recovered" and events[1]["resolved_note"] == "checks work again"
    assert runner.state("t/o")["unseen"] == 0


def test_a_rule_that_cant_run_says_why_and_makes_no_calls():
    client = client_with({})
    runner = alerts.AlertRunner(clock=Clock())
    runner.register("t/o", source(client, [scope_rule(scope="Missing"),
                                           {"name": "Watch", "kind": "watchlist", "scope": None, "signals": ["status"],
                                            "interval_minutes": 2, "notify": True, "enabled": True}]))
    runner.tick()
    rules = runner.state("t/o")["rules"]
    assert "no saved scope called 'Missing'" in rules[0]["cant_run"]
    assert "Watchlist is empty" in rules[1]["cant_run"]
    assert client.call_count == 0


def test_watchlist_rule_compares_each_item_and_keeps_unread_ones():
    items = [{"id": APP, "name": "shop", "kind": "resource"}, {"id": DB, "name": "orders", "kind": "resource"}]
    statuses = {"app": "healthy", "db": "warning"}

    def rows(wanted):
        out = []
        for item, key in zip(wanted, ("app", "db")):
            status = statuses[key]
            out.append({**item, "status": "unknown" if status == "error" else status, "counts": {},
                        "problems": ["timed out"] if status == "error" else [],
                        "checked_at": "" if status == "error" else "now"})
        return out

    clock = Clock()
    runner = alerts.AlertRunner(clock=clock)
    rule = {"name": "Watch", "kind": "watchlist", "scope": None, "signals": ["status"], "interval_minutes": 2,
            "notify": True, "enabled": True}
    runner.register("t/o", source(client_with({}), [rule], items=items, rows=rows))
    runner.tick()
    statuses.update(app="error", db="critical")
    clock.now += 120
    runner.tick()
    state = runner.state("t/o")
    assert [e["title"] for e in state["events"]] == ["orders: warning → critical"]
    assert state["rules"][0]["latest"][1]["was"] == "warning"
    statuses.update(app="critical")
    clock.now += 120
    runner.tick()  # the item that couldn't be read kept "healthy", so this is a change
    assert runner.state("t/o")["events"][0]["title"] == "shop: healthy → critical"
    items.pop(1)  # orders unpinned while red: resolved without a "recovered" entry
    clock.now += 120
    runner.tick()
    events = runner.state("t/o")["events"]
    assert next(e for e in events if e["title"].startswith("orders"))["resolved_note"] == "removed from the Watchlist"
    assert not any(e["signal"] == "recovered" for e in events)


def test_changed_signals_start_a_new_baseline_and_ttl_drops_sessions():
    state, clock = {"alerts": [alert_row(APP, 0)]}, Clock()
    rules = [scope_rule(signals=["service_health"])]
    runner = alerts.AlertRunner(clock=clock, session_ttl=600)
    runner.register("t/o", source(client_with(state), rules))
    runner.tick()
    rules[0] = scope_rule(signals=["new_red"])
    clock.now += 10
    runner.tick()  # a new rule shape checks at once and records a new starting point
    assert runner.state("t/o")["events"] == [] and runner.state("t/o")["rules"][0]["checks"] == 1
    clock.now += 700
    runner.tick()
    assert not runner.has("t/o")


def test_alerts_api(tmp_path):
    from healthconsole.web import create_blueprint

    file = tmp_path / "c.yaml"
    file.write_text("tenants:\n  tenant-1:\n    saved_scopes:\n      - name: Shop\n        subscriptions: [sub-a]\n")
    settings = config.load(file)
    state = {"alerts": [alert_row(APP, 2)]}
    fake = client_with(state)
    fake.gets["/tenants"] = {"value": [{"tenantId": "tenant-1", "displayName": "Tenant", "defaultDomain": ""}]}
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(lambda tenant: fake, settings, config_file=file))
    http = app.test_client()

    empty = http.get("/api/alerts").get_json()
    assert empty["rules"] == [] and empty["saved_scopes"] == ["Shop"] and empty["background"] is False

    bad = http.post("/api/alerts/rules", json={"rules": [{"name": "x", "kind": "scope", "scope": "Shop",
                                                          "interval_minutes": 2}]})
    assert bad.status_code == 400 and "interval_minutes" in bad.get_json()["error"]

    saved = http.post("/api/alerts/rules", json={"rules": [{"name": "Shop alerts", "kind": "scope", "scope": "Shop"}]})
    assert saved.status_code == 200 and saved.get_json()["rules"][0]["interval_minutes"] == 5
    assert "Shop alerts" in file.read_text()

    first = http.post("/api/alerts/run", json={"name": "Shop alerts"}).get_json()
    assert first["rules"][0]["checks"] == 1 and first["events"] == []
    state["alerts"] = [alert_row(APP, 0)]
    after = http.post("/api/alerts/run", json={"name": "Shop alerts"}).get_json()
    assert after["unseen"] == 1 and after["events"][0]["title"] == "shop: warning → critical"
    assert http.post("/api/alerts/run", json={"name": "nope"}).status_code == 404

    dismissed = http.post("/api/alerts/dismiss", json={"seq": after["events"][0]["seq"]}).get_json()
    assert dismissed["unseen"] == 0 and len(dismissed["events"]) == 1


def sn_item(number, status="critical"):
    return {"key": f"sn|prod|{number}", "signal": "servicenow", "status": status, "title": f"{number}: shop down",
            "detail": "prod · names shop", "resource_id": APP, "name": number}


def test_servicenow_and_monitor_records_alert_when_new_and_resolve_when_closed():
    clock, feed = Clock(), {"items": [sn_item("INC1")], "error": None, "truncated": False}
    seen = []

    def records(client, scope, subscriptions, groups, rule):
        seen.append(rule["signals"])
        return [{"prefix": "sn|prod|", "items": list(feed["items"]), "error": feed["error"], "truncated": feed["truncated"]}]

    def no_monitor(client, scope, subscriptions, groups, rule):
        return [{"prefix": "mon|", "items": [], "setup": "Monitoring: the saved scope 'Shop' has no monitoring connection."}]

    runner = alerts.AlertRunner(clock=clock)
    src = source(client_with({}), [scope_rule(signals=["servicenow", "monitors"])])
    src.servicenow_records, src.monitor_records = records, no_monitor
    runner.register("t/o", src)
    runner.tick()  # INC1 was already open: the starting point
    rule = runner.state("t/o")["rules"][0]
    assert rule["setup"] == ["Monitoring: the saved scope 'Shop' has no monitoring connection."]
    assert not any("ServiceHealthResources" in q for q in src.client.queries)  # no Triage card is needed

    feed["items"] = [sn_item("INC1"), sn_item("INC2", "warning")]
    clock.now += 300
    runner.tick()
    assert [e["title"] for e in runner.state("t/o")["events"]] == ["INC2: shop down"]

    feed["error"] = "ServiceNow (prod): timed out"
    feed["items"] = []
    clock.now += 300
    runner.tick()  # unread: nothing closes
    assert runner.state("t/o")["unseen"] == 1 and runner.state("t/o")["rules"][0]["partial"] == ["ServiceNow (prod): timed out"]

    feed.update(error=None, truncated=True)
    clock.now += 300
    runner.tick()  # cut off at the limit: absent isn't closed either
    assert runner.state("t/o")["unseen"] == 1

    feed["truncated"] = False
    clock.now += 300
    runner.tick()
    events = runner.state("t/o")["events"]
    assert events[0]["title"] == "Incident closed: INC2" and runner.state("t/o")["unseen"] == 0


def test_watchlist_last_week_movers_alert_and_settle():
    items = [{"id": APP, "name": "shop", "kind": "resource"}]
    movers = {"now": []}

    def rows(wanted):
        return [{**wanted[0], "status": "healthy", "counts": {}, "problems": [], "checked_at": "now",
                 "movers": movers["now"]}]

    mover = {"node_id": APP, "name": "shop", "signal": "Requests", "unit": "", "value": 400.0, "baseline": 100.0,
             "status": "info", "move": "4x last week"}
    clock = Clock()
    runner = alerts.AlertRunner(clock=clock)
    rule = {"name": "Watch", "kind": "watchlist", "scope": None, "signals": ["status", "last_week"],
            "interval_minutes": 2, "notify": True, "enabled": True}
    runner.register("t/o", source(client_with({}), [rule], items=items, rows=rows))
    runner.tick()
    movers["now"] = [mover]
    clock.now += 120
    runner.tick()
    event = runner.state("t/o")["events"][0]
    assert event["title"] == "shop: Requests 4x last week" and event["detail"] == "shop: 400 now, 100 last week"
    movers["now"] = []
    clock.now += 120
    runner.tick()
    assert runner.state("t/o")["events"][0]["title"] == "Back in line with last week: shop"


def test_acknowledge_and_snooze():
    state, clock = {"alerts": [alert_row(APP, 2)]}, Clock()
    runner = alerts.AlertRunner(clock=clock)
    runner.register("t/o", source(client_with(state), [scope_rule()]))
    runner.tick()
    state["alerts"] = [alert_row(APP, 0)]
    clock.now += 300
    runner.tick()
    seq = runner.state("t/o")["events"][0]["seq"]

    assert runner.snooze("t/o", seq, 3600) and runner.state("t/o")["unseen"] == 0
    clock.now += 3601
    assert runner.state("t/o")["unseen"] == 1  # still open after the hour: back on the bar
    assert runner.acknowledge("t/o", seq) and runner.state("t/o")["unseen"] == 0
    assert runner.state("t/o")["events"][0]["acknowledged_at"]

    state["alerts"] = [alert_row(APP, 0), alert_row(DB, 0)]
    clock.now += 300
    runner.tick()
    assert runner.state("t/o")["unseen"] == 1  # orders, new and open

    assert not runner.snooze_rule("t/o", "Shop alertz", 3600)  # a typo is refused, not recorded as snoozed
    assert runner.state("t/o")["unseen"] == 1
    assert runner.snooze_rule("t/o", "Shop alerts", 3600)
    assert runner.state("t/o")["unseen"] == 0  # the rule's open alerts go quiet too
    cache = rid("cache-rg", "Microsoft.Cache/redis", "cache")
    state["alerts"] = [alert_row(APP, 0), alert_row(DB, 0), alert_row(cache, 0)]
    clock.now += 300
    runner.tick()
    view = runner.state("t/o")
    quiet = view["events"][0]
    assert quiet["title"] == "New red: cache" and quiet["notify"] is False and view["unseen"] == 0
    assert view["rules"][0]["snoozed_until"]
    runner.snooze_rule("t/o", "Shop alerts", 0)
    assert runner.state("t/o")["unseen"] == 2 and runner.state("t/o")["rules"][0]["snoozed_until"] is None


def test_alert_actions_and_export_api(tmp_path):
    from healthconsole.web import create_blueprint

    file = tmp_path / "c.yaml"
    file.write_text("tenants:\n  tenant-1:\n    saved_scopes:\n      - name: Shop\n        subscriptions: [sub-a]\n"
                    "    alert_rules:\n      - name: Shop alerts\n        scope: Shop\n")
    state = {"alerts": [alert_row(APP, 2)]}
    fake = client_with(state)
    fake.gets["/tenants"] = {"value": [{"tenantId": "tenant-1", "displayName": "Tenant", "defaultDomain": ""}]}
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(lambda tenant: fake, config.load(file), config_file=file))
    http = app.test_client()
    http.post("/api/alerts/run", json={"name": "Shop alerts"})
    state["alerts"] = [alert_row(APP, 0)]
    seq = http.post("/api/alerts/run", json={"name": "Shop alerts"}).get_json()["events"][0]["seq"]

    assert http.post("/api/alerts/snooze", json={"seq": seq}).get_json()["unseen"] == 0
    assert http.post("/api/alerts/snooze", json={"seq": seq, "minutes": 0}).get_json()["unseen"] == 1
    assert http.post("/api/alerts/snooze", json={"seq": seq, "minutes": -5}).status_code == 400
    assert http.post("/api/alerts/acknowledge", json={"seq": seq}).get_json()["unseen"] == 0
    assert http.post("/api/alerts/acknowledge", json={"seq": 999}).status_code == 404
    assert http.post("/api/alerts/snooze", json={"rule": "Shop alerts"}).get_json()["rules"][0]["snoozed_until"]

    md = http.get("/api/alerts/export?format=md")
    text = md.get_data(as_text=True)
    assert md.status_code == 200 and "# Alert history" in text and "shop: warning → critical" in text
    assert "acknowledged" in text
    csv_text = http.get("/api/alerts/export?format=csv").get_data(as_text=True)
    assert csv_text.lstrip("﻿").startswith("When (UTC),Status,Alert,Rule,Detail,State,Resource")


def test_servicenow_reader_filters_by_priority_and_strong_matches(tmp_path, monkeypatch):
    from healthconsole.core import integrations
    from healthconsole.core.integrations import sn_data
    from healthconsole.web import create_blueprint

    file = tmp_path / "c.yaml"
    file.write_text("tenants:\n  tenant-1:\n    integrations:\n      servicenow:\n        - name: prod\n"
                    "          enabled: true\n          instance: https://example.service-now.com\n"
                    "    saved_scopes:\n      - name: Shop\n        subscriptions: [sub-a]\n"
                    "        connections: {servicenow: prod}\n      - name: Bare\n        subscriptions: [sub-a]\n"
                    "    alert_rules:\n      - name: Shop alerts\n        scope: Shop\n        signals: [servicenow]\n"
                    "      - name: Bare alerts\n        scope: Bare\n        signals: [servicenow]\n")
    monkeypatch.setattr(integrations, "servicenow_status", lambda cfg, typed=None: {"problems": []})
    incidents = []

    def incident(number, priority, confidence):
        return {"number": number, "priority": priority, "priority_label": f"{priority} - x", "title": "down",
                "group": "ops", "matches": [{"id": APP, "name": "shop", "confidence": confidence}]}

    monkeypatch.setattr(sn_data, "collect", lambda cfg, resources, refresh=False: {
        "incidents": list(incidents), "truncated": {"incident": False}})
    fake = client_with({})
    fake.gets["/tenants"] = {"value": [{"tenantId": "tenant-1", "displayName": "Tenant", "defaultDomain": ""}]}
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(lambda tenant: fake, config.load(file), config_file=file))
    http = app.test_client()
    first = http.post("/api/alerts/run", json={"name": "Bare alerts"}).get_json()
    bare = next(r for r in first["rules"] if r["name"] == "Bare alerts")
    assert "has no ServiceNow connection" in bare["setup"][0]
    http.post("/api/alerts/run", json={"name": "Shop alerts"})
    incidents[:] = [incident("INC1", 1, "exact"), incident("INC2", 3, "exact"), incident("INC3", 2, "possible"),
                    incident("INC4", 2, "name")]
    after = http.post("/api/alerts/run", json={"name": "Shop alerts"}).get_json()
    titles = sorted(e["title"] for e in after["events"])
    assert titles == ["INC1 (1 - x): down", "INC4 (2 - x): down"]  # P3 is below the threshold; "possible" is too weak
    assert {e["status"] for e in after["events"]} == {"critical", "warning"}


def test_resolved_events_and_advisories_never_raise_a_service_health_alert():
    from healthconsole.core import triage
    rows = []
    client = FakeClient(query_handlers=[("ServiceHealthResources", lambda q: rows)],
                        gets={"/subscriptions": {"value": []}})
    options = {"changes_hours": 1, "include_system_changes": False}
    _, before_events, _ = alerts.observe_triage(triage.triage(client, [SUB], options, cards={"service_health"}))
    baseline = {"items": {}, "events": before_events}
    rows[:] = [{"title": t, "eventType": k, "phase": p, "subscriptions": 1, "events": 1, "impacts": [], "updated": 0}
               for t, k, p in (("Old outage", "ServiceIssue", "resolved"), ("Retire v1", "HealthAdvisory", "active"),
                               ("Patch now", "SecurityAdvisory", "active"), ("Storage down", "ServiceIssue", "active"))]
    items, events, failed = alerts.observe_triage(triage.triage(client, [SUB], options, cards={"service_health"}))
    fired, _ = alerts.diff_triage(baseline, items, events, failed, ["service_health"])
    assert [f["title"] for f in fired] == ["Service Health: Storage down"]
