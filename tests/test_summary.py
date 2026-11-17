import os
import stat
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

from healthconsole.core import config, cost, personas, summary_history, vulnerabilities
from healthconsole.core.arm import AccessDenied
from healthconsole.web import create_blueprint
from tests.conftest import SUB, FakeClient

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
ITEMS = {"problems": {"a": {"label": "app-1: Fired alerts", "status": "warning"},
                      "b": {"label": "db-1: Failed deployments", "status": "critical"}},
         "cost": {"spike|x/rg": {"label": "Cost spike in rg", "status": "warning"}}}


def record(path, items, at, hours=24, keep=8):
    return summary_history.record(path, "scope", items, hours, keep, now=at)


# ---- What changed

def test_the_first_summary_says_when_a_comparison_will_be_ready(tmp_path):
    path = tmp_path / "s.json"
    found = record(path, ITEMS, NOW)
    assert found["since"] is None and found["ready_at"] == (NOW + timedelta(hours=24)).isoformat()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600  # readable only by the viewer


def test_a_day_later_it_lists_new_worse_and_resolved_per_area(tmp_path):
    path = tmp_path / "s.json"
    record(path, ITEMS, NOW)
    later = {"problems": {"a": {"label": "app-1: Fired alerts", "status": "critical"},  # worse
                          "c": {"label": "vm-2: Fired alerts", "status": "warning"}},  # new; b resolved
             "cost": dict(ITEMS["cost"])}  # unchanged
    found = record(path, later, NOW + timedelta(hours=25))
    assert found["since"] == NOW.isoformat()
    problems = found["areas"]["problems"]
    assert {(i["key"], i["was"]) for i in problems["new"]} == {("a", "warning"), ("c", None)}
    assert [i["key"] for i in problems["resolved"]] == ["b"]
    assert found["areas"]["cost"] == {"new": [], "resolved": []}


def test_an_area_missing_from_either_side_is_not_compared(tmp_path):
    """A card that failed now (or wasn't shown then) mustn't read as everything resolved or everything new."""
    path = tmp_path / "s.json"
    record(path, ITEMS, NOW)
    found = record(path, {"problems": ITEMS["problems"], "security": {"k": {"label": "kv", "status": "warning"}}},
                   NOW + timedelta(hours=30))
    assert set(found["areas"]) == {"problems"}


def test_one_snapshot_per_hour_and_the_baseline_is_the_newest_old_enough(tmp_path):
    path = tmp_path / "s.json"
    record(path, ITEMS, NOW)
    record(path, {"problems": {}}, NOW + timedelta(hours=2))
    record(path, {"problems": {"z": {"label": "z", "status": "warning"}}}, NOW + timedelta(hours=2, minutes=30))  # same hour
    found = record(path, {"problems": {}}, NOW + timedelta(hours=27))
    assert found["since"] == (NOW + timedelta(hours=2, minutes=30)).isoformat()  # not NOW: a newer one is a day old
    assert [i["key"] for i in found["areas"]["problems"]["resolved"]] == ["z"]


def test_old_snapshots_are_dropped_after_keep_days(tmp_path):
    path = tmp_path / "s.json"
    record(path, ITEMS, NOW, keep=2)
    found = record(path, ITEMS, NOW + timedelta(days=3), keep=2)
    assert found["since"] is None  # the only older one was past keep_days


def test_the_scope_key_ignores_order_and_case():
    assert summary_history.scope_key(["B", "a"], ["g2", "G1"]) == summary_history.scope_key(["a", "b"], ["g1", "g2"])
    assert summary_history.scope_key(["a"]) != summary_history.scope_key(["a", "b"])


# ---- Cost: where the month is heading, with purchases counted once

def entry(values_by_day, booked_by_day=None):
    return {"days": {"rg": {d.isoformat(): v for d, v in values_by_day.items()}},
            "booked": {"": {d.isoformat(): v for d, v in (booked_by_day or {}).items()}}}


def test_the_month_is_projected_from_the_store_and_last_month_included():
    today = date(2026, 11, 15)
    days = {date(2026, 10, 1) + timedelta(days=i): 10 for i in range(31)}
    days.update({date(2026, 11, 1) + timedelta(days=i): 20 for i in range(14)})  # through the 14th
    month = cost._month(entry(days), today)
    assert month["month_to_date"] == 280 and month["last_month"] == 310 and month["projected"] == 600
    assert month["last_month_same_days"] == 140 and month["same_days_to"] == "2026-10-14" and month["booked"] == 0


def test_purchases_count_once_and_last_months_later_ones_are_expected_again():
    today = date(2026, 11, 15)
    days = {date(2026, 10, 1) + timedelta(days=i): 10 for i in range(31)}
    days.update({date(2026, 11, 1) + timedelta(days=i): 10 for i in range(14)})
    paid = {date(2026, 10, 4): 500, date(2026, 10, 20): 300, date(2026, 11, 4): 500}
    for day, amount in paid.items():
        days[day] += amount  # a day's whole cost includes what was booked on it
    month = cost._month(entry(days, paid), today)
    assert month["booked"] == 500 and month["booked_days"] == [{"date": "2026-11-04", "cost": 500}]
    assert month["booked_expected"] == 300  # Oct 20's purchase hasn't come round yet this month
    assert month["projected"] == 500 + 300 + 10 * 30  # usage at its rate, purchases once each
    assert month["month_to_date"] == 640 and month["last_month_same_days"] == 640  # both months paid on the 4th


def test_budgets_are_read_per_subscription_and_a_refusal_is_a_note():
    other = "22222222-2222-2222-2222-222222222222"

    def budgets(params):
        return {"value": [{"name": "monthly", "properties": {"amount": 100, "timeGrain": "Monthly",
                                                             "currentSpend": {"amount": 85, "unit": "CAD"},
                                                             "forecastSpend": {"amount": 120}}}]}

    class Refusing(FakeClient):
        def get(self, path, api_version, params=None, tolerate=(), timeout=None):
            if other in path:
                raise AccessDenied(path)
            return super().get(path, api_version, params)

    found = cost.budgets_for(Refusing(gets={"budgets": budgets}), [SUB, other.upper()])
    assert [(b["subscription_id"], b["percent"], b["forecast"]) for b in found["budgets"]] == [(SUB, 85.0, 120.0)]
    assert found["notes"][0]["subscription_id"] == other and "needs" in found["notes"][0]["note"]


# ---- Vulnerabilities headline

def test_the_headline_is_two_defender_queries_and_keeps_the_findings_priority():
    rows = [{"parent": "/s/x/vm1", "rtype": "microsoft.compute/virtualmachines", "maxCvss": 9.8, "high": 5, "live": 1, "fixable": 1, "findings": 9},
            {"parent": "/s/x/acr1", "rtype": "microsoft.containerregistry/registries", "maxCvss": 9.9, "high": 9, "live": 0, "fixable": 1, "findings": 90}]
    fake = FakeClient(query_handlers=[
        ("by parent", lambda q: [dict(r) for r in rows]),
        ("dcount(parent)", lambda q: [{"findings": 99, "resources": 2, "high": 14, "fixable": 50, "maxCvss": 9.9}]),
        ("networkinterfaces", lambda q: [])])
    found = vulnerabilities.headline(fake, [SUB], top=1)
    assert found["summary"]["findings"] == 99 and found["summary"]["running"] == 0
    assert [r["name"] for r in found["resources"]] == ["vm1"]  # live outranks a higher CVSS on an image
    assert len([q for q in fake.queries if "| summarize" in q]) == 2  # plus the exposure lookup `findings` shares


# ---- Personas and settings

def test_a_persona_picks_summary_cards_by_known_names_only():
    assert personas.problems({"sre": {"summary": ["problems", "changes"]}}) == []
    assert "unknown ['budget']" in personas.problems({"sre": {"summary": ["budget"]}})[0]
    merged = {p["name"]: p for p in personas.merged({"finops": {"label": "FinOps", "views": ["cost"]}})}
    assert "access" not in merged["manager"]["summary"] and merged["everything"]["summary"] == []  # empty: every card
    assert merged["finops"]["summary"] == []


def test_summary_settings_have_defaults_and_are_type_checked():
    assert config.DEFAULTS["summary"]["compare_hours"] == 24
    with pytest.raises(config.ConfigError):
        config.from_data({"summary": {"compare_hours": "a day"}})


# ---- Routes

def app_for(tmp_path, fake):
    app = Flask("test", static_folder=None)
    settings = config.load(tmp_path / "none.yaml")
    app.register_blueprint(create_blueprint(lambda tenant: fake, settings, config_file=tmp_path / "c.yaml"))
    return app.test_client()


def test_routes(tmp_path):
    web = app_for(tmp_path, FakeClient(gets={"budgets": {"value": []}}))
    assert web.post("/api/summary/changes", json={"subscriptions": [SUB]}).status_code == 400
    first = web.post("/api/summary/changes", json={"subscriptions": [SUB], "items": ITEMS}).get_json()
    assert first["since"] is None
    assert list((tmp_path / "cache").glob("summary-*.json"))
    assert web.get(f"/api/cost/budgets?subscription={SUB}").get_json() == {"budgets": [], "notes": []}
    assert web.post("/api/readiness", json={"subscriptions": [SUB], "peak_end": "2099-01-01", "purpose": "x"}).status_code == 400
    assert "summary_cards" in web.get("/api/personas").get_json()
    web.post("/api/cache/clear")
    assert not list((tmp_path / "cache").glob("summary-*.json"))


# ---- A resource's Overview names its vulnerabilities, so Summary's item lands on the same numbers

AKS = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/aks1"


def test_a_resource_s_totals_are_one_query_on_its_own_findings():
    fake = FakeClient(query_handlers=[("summarize findings", lambda q: [{"findings": 2821, "high": 1877, "fixable": 2821,
                                                                         "running": 2821, "maxCvss": 9.9}])])
    found = vulnerabilities.counts_for(fake, AKS)
    assert found["findings"] == 2821 and found["high"] == 1877 and found["maxCvss"] == 9.9 and found["medium"] == 0
    assert len(fake.queries) == 1 and f'parent == "{AKS.lower()}"' in fake.queries[0]


def test_overview_security_carries_vulnerabilities_and_survives_a_refusal(tmp_path):
    counted = FakeClient(query_handlers=[("summarize findings", lambda q: [{"findings": 3, "high": 1}])])
    found = app_for(tmp_path, counted).get(f"/api/explore/security?id={AKS}").get_json()
    assert found["vulnerabilities"]["findings"] == 3 and "findings" in found  # its settings are still there

    class Refusing(FakeClient):
        def query(self, kql, subscriptions=None):
            if "summarize findings" in kql:
                raise AccessDenied("/providers/Microsoft.ResourceGraph/resources")
            return super().query(kql, subscriptions)

    found = app_for(tmp_path, Refusing()).get(f"/api/explore/security?id={AKS}").get_json()
    assert "error" in found["vulnerabilities"] and "findings" in found


def test_a_capped_findings_list_still_counts_every_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(vulnerabilities, "MAX_ROWS", 2)
    rows = [{"title": f"t{i}", "software": "s", "severity": "High", "cvss": 9.0} for i in range(3)]
    fake = FakeClient(query_handlers=[("summarize findings", lambda q: [{"findings": 2821, "high": 1877}]),
                                      ("order by cvss", lambda q: rows)])
    found = app_for(tmp_path, fake).get(f"/api/explore/vulnerabilities?id={AKS}").get_json()
    assert found["truncated"] and len(found["findings"]) == 2 and found["totals"]["findings"] == 2821
