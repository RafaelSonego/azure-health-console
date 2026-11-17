from datetime import date, timedelta

import pytest
from flask import Flask

import os
import threading
import time
from datetime import datetime, timezone

from healthconsole.core import cache, config, cost, cost_store
from healthconsole.core.arm import AccessDenied, ArmError
from healthconsole.web import create_blueprint
from tests.conftest import SUB, FakeClient, rid

TODAY = date(2026, 11, 13)
GROUP = f"/subscriptions/{SUB}/resourceGroups/app"


def setup_function():
    cost._cache.clear()


class Now:
    def __init__(self):
        self.at = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.at


def store_at(tmp_path, now=None, name="costs.json.gz"):
    return cost_store.Store(tmp_path / name, cost.fetch, cost._body, lambda: TODAY, now or Now())


def _span(body):
    period = body["timePeriod"]
    return date.fromisoformat(period["from"][:10]), date.fromisoformat(period["to"][:10])


def azure(rows_for_day=None, parts=None, purchases=None):
    """A fake Cost Management that honours each query's dates: by resource group, charge type and day (10 a day for
    "app" in October, 20 in November, 1 a day outside any group; `purchases` {date: cost} adds Purchase rows outside
    any group), or this month by resource and service."""
    def day_cost(day, group):
        if rows_for_day:
            return rows_for_day(day, group)
        return {"app": 10 if day.month == 10 else 20, "": 1}[group]

    def handler(body):
        grouping = [g["name"] for g in body["dataset"]["grouping"]]
        start, end = _span(body)
        if grouping == ["ResourceGroupName", "ChargeType"]:
            out, day = [], start
            while day <= end:
                for group in ("app", ""):
                    cost_ = day_cost(day, group)
                    if cost_:
                        out.append([cost_, int(day.strftime("%Y%m%d")), group, "Usage", "CAD"])
                if (purchases or {}).get(day):
                    out.append([purchases[day], int(day.strftime("%Y%m%d")), "", "Purchase", "CAD"])
                day += timedelta(days=1)
            return answer(["Cost", "UsageDate", "ResourceGroupName", "ChargeType", "Currency"], out)
        if grouping == ["ResourceId", "ServiceName"]:
            return answer(["Cost", "ResourceId", "ServiceName", "Currency"], parts if parts is not None else [
                [200, rid("App", "Microsoft.Storage/storageAccounts", "st1"), "Storage", "CAD"],
                [40, rid("App", "Microsoft.KeyVault/vaults", "kv1"), "Key Vault", "CAD"],
                [5, "", "Defender", "CAD"]])
        raise AssertionError(grouping)
    return handler


def answer(columns, rows, next_link=None):
    return {"properties": {"columns": [{"name": c} for c in columns], "rows": rows,
                           **({"nextLink": next_link} if next_link else {})}}


def daily(start, values, **extra):
    return [[v, int((start + timedelta(days=i)).strftime("%Y%m%d")), *extra.values(), "CAD"] for i, v in enumerate(values)]


def by_grouping(body):
    grouping = [g["name"] for g in body["dataset"]["grouping"]]
    if not grouping:  # since the first of last month: 31 days of 10, then 12 days of 20
        return answer(["Cost", "UsageDate", "Currency"], daily(date(2026, 10, 1), [10] * 31 + [20] * 12 + [0]))
    if grouping == ["ResourceGroupName", "ServiceName"]:
        return answer(["Cost", "ResourceGroupName", "ServiceName", "Currency"],
                      [[200, "app", "Storage", "CAD"], [40, "app", "Key Vault", "CAD"], [5, "", "Defender", "CAD"]])
    if grouping == ["ResourceGroupName"]:
        return answer(["Cost", "UsageDate", "ResourceGroupName", "Currency"], [[5, 20261111, "app", "CAD"]])
    raise AssertionError(grouping)


def test_month_figures_compare_the_same_days_and_project_the_month():
    days = [{"date": f"2026-10-{d:02d}", "cost": 10} for d in range(1, 32)] + \
           [{"date": f"2026-11-{d:02d}", "cost": 20} for d in range(1, 13)] + [{"date": "2026-11-13", "cost": 0}]
    f = cost.month_figures(days, TODAY)
    assert f["month_to_date"] == 240 and f["up_to"] == "2026-11-12"
    assert f["last_month_same_days"] == 120 and f["last_month"] == 310
    assert f["projected"] == pytest.approx(240 / 12 * 30)


def test_a_subscription_is_read_once_into_the_store_and_served_from_it(tmp_path):
    budget = {"name": "monthly", "properties": {"amount": 500, "timeGrain": "Monthly", "currentSpend": {"amount": 245, "unit": "CAD"}}}
    fake = FakeClient(posts={"CostManagement/query": azure()}, gets={"budgets": {"value": [budget]}})
    store = store_at(tmp_path)
    s = cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY)
    assert s["error"] is None and s["currency"] == "CAD" and s["stored"]
    assert s["month_to_date"] == 13 * 21 and s["last_month_same_days"] == 13 * 11 and s["last_month"] == 31 * 11
    assert [t["name"] for t in s["top"]] == ["app", cost.SUBSCRIPTION_LEVEL] and s["top"][1]["subscription_level"]
    assert s["top"][0]["services"] == ["Storage", "Key Vault"] and s["services"][0] == {"name": "Storage", "cost": 200}
    assert len(s["trend"]) == 30 and s["trend"][-1] == {"date": "2026-11-13", "cost": 21}
    assert s["budgets"][0]["percent"] == 49.0
    spans = sorted(_span(body) for _, body, _ in fake.posted)  # a page waiting: two queries side by side
    assert spans == [(date(2026, 10, 1), TODAY), (date(2026, 11, 1), TODAY)]
    cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY)
    group = cost.summary(fake, GROUP, store, TODAY)
    assert len(fake.posted) == 2  # the second subscription page and the group page cost no query
    assert group["month_to_date"] == 13 * 20 and [t["name"] for t in group["top"]] == [
        rid("App", "Microsoft.Storage/storageAccounts", "st1").lower(), rid("App", "Microsoft.KeyVault/vaults", "kv1").lower()]
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"
    again = store_at(tmp_path)  # a new process reads the file instead of Azure
    assert cost.summary(fake, GROUP, again, TODAY)["month_to_date"] == 13 * 20 and len(fake.posted) == 2


def test_a_stale_subscription_is_served_then_only_its_last_days_reread(tmp_path):
    now = Now()
    store = store_at(tmp_path, now)
    fake = FakeClient(posts={"CostManagement/query": azure()}, gets={"budgets": {"value": []}})
    cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY)
    now.at += timedelta(seconds=cost_store.FRESH_SECONDS)
    assert store.needs(SUB) == [cost_store.RECENT, cost_store.PARTS]
    store.stop()
    fake.posted.clear()
    fake.posts["CostManagement/query"] = azure(lambda day, group: 99 if (day == TODAY and group == "app") else
                                               {"app": 10 if day.month == 10 else 20, "": 1}[group])
    store.read(fake, SUB, cost_store.RECENT)
    assert _span(fake.posted[0][1]) == (TODAY - timedelta(days=cost_store.SETTLE_DAYS), TODAY)
    assert store.entry(SUB)["days"]["app"]["2026-11-13"] == 99 and store.entry(SUB)["days"]["app"]["2026-10-01"] == 10
    assert store.needs(SUB) == [cost_store.PARTS]


def test_no_cost_access_is_named_remembered_and_asked_again_after_refresh_access(tmp_path):
    def refused(body):
        raise AccessDenied(f"/subscriptions/{SUB}/providers/Microsoft.CostManagement/query", "RBACAccessDenied")
    fake = FakeClient(posts={"CostManagement/query": refused})
    store = store_at(tmp_path)
    cost_store._stores[("t", "t", "t")] = store  # reachable by Refresh access
    s = cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY)
    assert "Cost Management Reader" in s["error"] and s["required"] and store.refused(SUB)
    assert store.needs(SUB) == []  # not asked again on the next view
    cache.forget_access_bound()
    assert not store.refused(SUB) and store.needs(SUB)


def test_other_failures_are_asked_again(tmp_path):
    def down(body):
        raise ArmError("Azure returned 503: busy", 503)
    fake = FakeClient(posts={"CostManagement/query": down})
    store = store_at(tmp_path)
    assert "didn't answer" in cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY)["error"]
    store.stop()
    before = len(fake.posted)
    cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY)
    assert len(fake.posted) > before


def test_pages_post_the_same_body_to_the_next_link():
    pages = iter([answer(["Cost", "UsageDate", "Currency"], [[1, 20261101, "CAD"]],
                         f"https://management.azure.com{GROUP}/providers/Microsoft.CostManagement/query?api-version=2023-11-01&$skiptoken=abc"),
                  answer(["Cost", "UsageDate", "Currency"], [[2, 20261102, "CAD"]])])
    fake = FakeClient(posts={"CostManagement/query": lambda body: next(pages)})
    rows = cost.query(fake, GROUP, {"x": 1})
    assert [r["Cost"] for r in rows] == [1, 2]
    assert fake.posted[1][2] == {"$skiptoken": "abc"} and fake.posted[1][1] == {"x": 1}


def test_a_resource_is_read_at_its_group_filtered_to_its_lowercased_id():
    vault = rid("App", "Microsoft.KeyVault/vaults", "KV1")
    fake = FakeClient(posts={"CostManagement/query": answer(["Cost", "UsageDate", "Meter", "Currency"],
                                                            [[3, 20261112, "Operations", "CAD"], [1, 20261112, "Secrets", "CAD"]])})
    r = cost.for_resource(fake, vault, TODAY)
    path, body, _ = fake.posted[0]
    assert path == f"/subscriptions/{SUB}/resourceGroups/App/providers/Microsoft.CostManagement/query"
    assert body["dataset"]["filter"]["dimensions"]["values"] == [vault.lower()]
    assert r["last_30_days"] == 4 and r["meters"][0]["name"] == "Operations" and r["month_to_date"] == 4
    assert len(r["trend"]) == 30 and r["trend"][0] == {"date": "2026-10-15", "cost": 0}  # days Azure leaves out are 0


def overview_client(spike_day_cost=60):
    def day_cost(day, group):
        if group == "":
            return 0
        if group == "app":  # the fake's "app" group plays the web tier here
            return spike_day_cost if day == TODAY - timedelta(days=2) else 10
    def rows(body):
        grouping = [g["name"] for g in body["dataset"]["grouping"]]
        if grouping == ["ResourceId", "ServiceName"]:
            return answer(["Cost", "ResourceId", "ServiceName", "Currency"], [])
        return azure(day_cost)(body)
    return FakeClient(posts={"CostManagement/query": rows})


@pytest.fixture
def quick_watch(monkeypatch):
    monkeypatch.setattr(cost, "WATCH_SECONDS", 0.01)


def test_the_overview_finds_a_spike_and_the_weekly_change(tmp_path, quick_watch):
    result = cost.overview(overview_client(), [SUB], store_at(tmp_path), today=TODAY)
    assert result["done"] and result["read"] == 1 and result["currency"] == "CAD"
    app = next(g for g in result["groups"] if g["name"] == "app")
    assert app["spike"] == {"date": "2026-11-11", "cost": 60, "usual": 10, "times": 6.0}
    assert app["last_7_days"] == 120 and app["previous_7_days"] == 70 and app["change"] == 50
    assert [g["name"] for g in result["spikes"]] == ["app"]


def test_a_small_rise_is_not_a_spike(tmp_path, quick_watch):
    assert not cost.overview(overview_client(spike_day_cost=11), [SUB], store_at(tmp_path), today=TODAY)["spikes"]


def test_the_overview_reports_refused_subscriptions_apart(tmp_path, quick_watch):
    other = "22222222-2222-2222-2222-222222222222"
    fake = overview_client()
    good = fake.posts["CostManagement/query"]

    def some(body):
        raise AccessDenied("/x/providers/Microsoft.CostManagement/query")
    fake.posts = {other: some, "CostManagement/query": good}
    result = cost.overview(fake, [SUB, other], store_at(tmp_path), today=TODAY)
    assert result["refused"] == [other] and len(result["subscriptions"]) == 1


def test_the_fill_reads_the_scope_whole_first_then_every_last_30_days_before_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(cost_store, "PARALLEL", 1)
    subs = [f"{i}{i}{i}{i}{i}{i}{i}{i}-0000-0000-0000-000000000000" for i in range(1, 4)]
    fake = FakeClient(posts={"CostManagement/query": azure()})
    store = store_at(tmp_path)
    store.want([subs[2]], everything=subs, client=fake)
    store._thread.join(5)
    order = [(path.split("/")[2], [g["name"] for g in body["dataset"]["grouping"]], _span(body)[0]) for path, body, _ in fake.posted]
    assert order[0] == (subs[2], ["ResourceGroupName", "ChargeType"], TODAY - timedelta(days=29))  # the scope's subscription first
    assert order[1] == (subs[2], ["ResourceGroupName", "ChargeType"], date(2026, 10, 1))  # with last month, for the comparison
    recent = [o for o in order[2:] if o[2] == TODAY - timedelta(days=29)]
    assert order[2:4] == recent and {o[0] for o in recent} == set(subs[:2])  # then everyone else's last 30 days
    assert all(o[1] == ["ResourceId", "ServiceName"] for o in order[-3:])
    assert all(not store.needs(s) for s in subs)


def test_a_page_read_goes_before_the_background_fill():
    gate = cost_store.Gate()
    passed = threading.Event()
    with gate.foreground():
        worker = threading.Thread(target=lambda: (gate.background(), passed.set()))
        worker.start()
        time.sleep(0.1)
        assert not passed.is_set()
    worker.join(2)
    assert passed.is_set()


def test_the_window_reaches_back_30_days_even_early_in_march():
    assert cost_store.window_start(date(2026, 11, 13)) == date(2026, 10, 1)
    assert cost_store.window_start(date(2027, 3, 1)) == date(2027, 1, 31)


def test_waste_is_one_query_with_every_check_scoped_and_safely_quoted():
    kql = cost.waste_query(" | where resourceGroup == 'x'")
    assert kql.count(" | where resourceGroup == 'x'") == len(cost._WASTE)
    assert kql.count("| union ") == 1 and kql.count("(resources") == len(cost._WASTE) - 1
    for _, finding, _, _, action in cost._WASTE:
        assert f'"{finding}"' in kql and f'"{action}"' in kql  # double-quoted: "it's" can't end the string
    fake = FakeClient(query_handlers=[("union", lambda q: [{"id": "/x/disk", "name": "d1", "type": "microsoft.compute/disks",
                                                             "finding": "Unattached managed disk", "detail": "P10 · 128 GB"}])])
    assert cost.waste(fake, [SUB])["rows"][0]["name"] == "d1"


def test_routes(tmp_path):
    fake = FakeClient(posts={"CostManagement/query": azure()}, gets={"budgets": {"value": []}})
    app = Flask("test", static_folder=None)
    settings = config.load(tmp_path / "none.yaml")
    assert str(tmp_path) in settings["cache_dir"]  # the test fixture's folder, never the real one
    app.register_blueprint(create_blueprint(lambda tenant: fake, settings, config_file=tmp_path / "c.yaml"))
    web = app.test_client()
    assert web.get("/api/cost/summary?id=/subscriptions/x/resourceGroups/rg/providers/a/b/c").status_code == 400
    assert web.get(f"/api/cost/summary?id=/subscriptions/{SUB}").get_json()["error"] is None
    assert web.post("/api/cost", json={"subscriptions": []}).status_code == 400
    assert web.get(f"/api/cost/waste?subscription={SUB}").status_code == 200
    assert web.get(f"/api/triage/cost?subscription={SUB}").status_code == 404  # Triage never reads costs
    stored = list((tmp_path / "cache").glob("costs-*.json.gz"))
    assert len(stored) == 1
    cost_store.reset()
    found = web.get(f"/api/cost/totals?subscription={SUB}").get_json()  # a new process: served from the file
    assert found["done"] and found["subscriptions"][0]["subscription_id"] == SUB
    assert web.get(f"/api/cost/summary?id=/subscriptions/{SUB}&cost_type=usage").status_code == 400
    fake.posted.clear()
    assert web.get(f"/api/cost/summary?id=/subscriptions/{SUB}&cost_type=amortized").get_json()["error"] is None
    assert {body["type"] for _, body, _ in fake.posted} == {"AmortizedCost"}
    assert len(list((tmp_path / "cache").glob("costs-*-amortized.json.gz"))) == 1
    assert web.post("/api/cache/clear").get_json()["cleared"][0] == "stored costs"
    assert not stored[0].exists() and not list((tmp_path / "cache").glob("costs-*.json.gz"))  # both kinds cleared


def test_totals_read_the_whole_scope_at_the_group_holding_it():
    other = "22222222-2222-2222-2222-222222222222"
    chain = lambda *names: [{"name": n} for n in names]  # noqa: E731
    fake = FakeClient(query_handlers=[("managementGroupAncestorsChain", lambda q: [
        {"subscriptionId": SUB, "chain": chain("team-a", "landing", "root")},
        {"subscriptionId": other, "chain": chain("team-b", "landing", "root")}])],
        posts={"managementGroups/landing/providers/Microsoft.CostManagement/query": answer(
            ["Cost", "UsageDate", "SubscriptionId", "Currency"],
            [[7, 20261112, SUB.upper(), "CAD"], [3, 20261112, other, "CAD"], [99, 20261112, "not-in-scope", "CAD"]])})
    found = cost.totals(fake, [SUB, other], today=TODAY)
    assert found["group"] == "landing" and len(fake.posted) == 1
    assert {s["subscription_id"]: s["month_to_date"] for s in found["subscriptions"]} == {SUB: 7, other: 3}


def test_totals_step_aside_when_the_group_is_refused():
    fake = FakeClient(query_handlers=[("managementGroupAncestorsChain", lambda q: [{"subscriptionId": SUB, "chain": [{"name": "g"}]}])],
                      posts={"CostManagement/query": lambda body: (_ for _ in ()).throw(AccessDenied("/x/providers/Microsoft.CostManagement/query"))})
    assert cost.totals(fake, [SUB], today=TODAY) is None


def test_a_card_says_whether_it_read_azure_just_now(tmp_path):
    fake = FakeClient(posts={"CostManagement/query": azure()}, gets={"budgets": {"value": []}})
    store = store_at(tmp_path)
    first = cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY)
    again = cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY)
    assert first["read_now"] and not again["read_now"] and not again["updating"]
    refreshed = cost.summary(fake, f"/subscriptions/{SUB}", store, TODAY, refresh=True)
    assert refreshed["read_now"]


def test_a_cost_view_refresh_counts_the_subscriptions_reread_not_the_ones_kept(tmp_path, quick_watch):
    other = "22222222-2222-2222-2222-222222222222"
    fake = overview_client()
    store = store_at(tmp_path)
    cost.overview(fake, [SUB, other], store, today=TODAY)
    store._thread.join(5)
    fake.posted.clear()
    published = []
    done = cost.overview(fake, [SUB, other], store, today=TODAY, refresh=True, publish=published.append)
    assert done["refreshing"] and done["refreshed"] == done["to_refresh"] == 2 and done["done"]
    assert all(p["refreshed"] <= p["to_refresh"] and not p["done"] for p in published)
    recent = [_span(body) for _, body, _ in fake.posted if [g["name"] for g in body["dataset"]["grouping"]] == ["ResourceGroupName", "ChargeType"]]
    assert recent and all(start == TODAY - timedelta(days=cost_store.SETTLE_DAYS) for start, _ in recent)  # only the last days


def test_a_monthly_purchase_is_not_a_spike_or_a_drop_and_months_compare_the_same_days(tmp_path, quick_watch):
    paid = {date(2026, 10, 4): 900, date(2026, 11, 4): 900, date(2026, 11, 7): 600}
    fake = FakeClient(posts={"CostManagement/query": azure(purchases=paid)})
    result = cost.overview(fake, [SUB], store_at(tmp_path), today=TODAY)
    none = next(g for g in result["groups"] if g["name"] == cost.SUBSCRIPTION_LEVEL)
    assert none["booked"] == 1500 and none["booked_days"] == ["2026-11-04", "2026-11-07"]
    assert none["month_to_date"] == 13 + 1500  # the whole cost, as invoiced
    assert (none["last_7_days"], none["previous_7_days"], none["change"]) == (7, 7, 0)  # usage only: no -100%
    assert max(none["trend"]) == 1  # the line is usage too
    sub = result["subscriptions"][0]
    assert sub["up_to"] == "2026-11-13" and sub["same_days_to"] == "2026-10-13"
    assert sub["last_month_same_days"] == 13 * 11 + 900 and sub["booked"] == 1500
    assert not result["spikes"]


def test_the_store_keeps_booked_days_and_rereads_replace_them(tmp_path):
    fake = FakeClient(posts={"CostManagement/query": azure(purchases={date(2026, 11, 12): 50})})
    store = store_at(tmp_path)
    store.ensure(fake, SUB)
    assert store.entry(SUB)["booked"] == {"": {"2026-11-12": 50}}
    fake.posts = {"CostManagement/query": azure()}  # the purchase was refunded and Azure's figure changed
    store.read(fake, SUB, cost_store.RECENT)
    assert "booked" in store.entry(SUB) and store.entry(SUB)["booked"] == {}


def test_an_older_store_file_is_read_again(tmp_path):
    import gzip
    import json
    path = tmp_path / "costs.json.gz"
    with gzip.open(path, "wt") as handle:
        json.dump({"format": 1, "subs": {SUB: {"days": {"app": {"2026-11-12": 5}}}}, "refused": {}, "failed": {}}, handle)
    assert store_at(tmp_path).entry(SUB) is None  # no booked part: refilled instead of misread


def test_amortized_cost_has_its_own_store_and_query_type(tmp_path):
    cost_store.reset()
    fake = FakeClient(posts={"CostManagement/query": azure()})
    actual, amortized = cost.store_for(fake, tmp_path), cost.store_for(fake, tmp_path, "amortized")
    assert actual is not amortized and actual.path != amortized.path and amortized.path.name.endswith("-amortized.json.gz")
    amortized.ensure(fake, SUB)
    assert {body["type"] for _, body, _ in fake.posted} == {"AmortizedCost"}
    assert amortized.entry(SUB) and actual.entry(SUB) is None
    assert cost.cost_type_of(None) == "actual" and cost.cost_type_of("Amortized") == "amortized"
    with pytest.raises(ValueError):
        cost.cost_type_of("usage")
    cost_store.reset()


def test_totals_at_a_group_keep_purchases_apart():
    chain = [{"name": "landing"}]
    fake = FakeClient(query_handlers=[("managementGroupAncestorsChain", lambda q: [{"subscriptionId": SUB, "chain": chain}])],
                      posts={"managementGroups/landing/providers/Microsoft.CostManagement/query": answer(
                          ["Cost", "UsageDate", "SubscriptionId", "ChargeType", "Currency"],
                          [[7, 20261112, SUB, "Usage", "CAD"], [900, 20261104, SUB, "Purchase", "CAD"]])})
    found = cost.totals(fake, [SUB], today=TODAY, cost_type="amortized")
    sub = found["subscriptions"][0]
    assert sub["month_to_date"] == 907 and sub["booked"] == 900 and found["cost_type"] == "amortized"
    assert fake.posted[0][1]["type"] == "AmortizedCost"


def test_last_month_is_none_not_zero_until_the_fill_has_read_it(tmp_path):
    fake = FakeClient(posts={"CostManagement/query": azure()})
    store = store_at(tmp_path)
    store.read(fake, SUB, cost_store.RECENT)  # the last 30 days only: what the fill reads first
    early = cost.from_store(store, [SUB], TODAY)["subscriptions"][0]
    assert early["last_month_same_days"] is None and early["last_month"] is None and early["last_month_pending"]
    assert all(g["last_month_same_days"] is None for g in early["groups"])
    assert early["month"]["projected"] is None and early["month"]["last_month"] is None
    store.read(fake, SUB, cost_store.EARLIER)  # then the rest of last month
    later = cost.from_store(store, [SUB], TODAY)["subscriptions"][0]
    assert later["last_month_same_days"] == 13 * 11 and later["last_month"] == 31 * 11 and "last_month_pending" not in later
    app = next(g for g in later["groups"] if g["name"] == "app")
    assert (app["last_month"], app["last_month_same_days"]) == (31 * 10, 13 * 10)  # all of October, and its first 13 days


def test_the_overview_is_done_only_once_last_month_is_in_too(tmp_path, quick_watch):
    fast = azure()

    def slow_last_month(body):  # the agent in CI is slower than a laptop: last month arrives after the 30 days
        if _span(body)[0] == date(2026, 10, 1):
            time.sleep(0.3)
        return fast(body)
    result = cost.overview(FakeClient(posts={"CostManagement/query": slow_last_month}), [SUB], store_at(tmp_path), today=TODAY)
    sub = result["subscriptions"][0]
    assert result["done"] and sub["last_month_same_days"] == 13 * 11 and sub["month"]["last_month"] == 31 * 11
