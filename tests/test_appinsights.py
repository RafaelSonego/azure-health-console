import pytest

from healthconsole.core.arm import AccessDenied
from healthconsole.core.explore import appinsights

COMPONENT = "/subscriptions/s/resourceGroups/rg/providers/microsoft.insights/components/shop-appi"


def table(columns, rows):
    return {"tables": [{"columns": [{"name": c} for c in columns], "rows": rows}]}


class Client:
    def __init__(self, answers, deny=()):
        self.answers, self.deny, self.calls = answers, deny, []

    def get(self, path, api_version, params=None, **_):
        if path.endswith("/metrics"):
            self.metrics = getattr(self, "metrics", 0) + 1
            return {"value": [{"name": {"value": "requests/failed"}, "timeseries": [{"data": [{"count": 4}, {"count": 9}]}]}]}
        self.calls.append((path, api_version, params))
        section = params["query"].split(" ")[0]
        if section in self.deny:
            raise AccessDenied(path)
        return self.answers(section, params["timespan"])


def test_sections_compare_roles_with_last_week_and_parse_codes():
    calls = {}

    def answers(section, timespan):
        calls.setdefault(section, []).append(timespan)
        return {
            "requests": table(["role", "total", "failed", "p95"], [["api", 200, 20, 900.0]]),
            "exceptions": table(["type", "operation", "role", "total", "last_seen", "trace_id", "instance", "message"],
                                [["DbError", "GET /", "api", 20, "2026-11-04T10:00:00Z", "abcdef0123456789", "pod-1", "dns"]]),
            "dependencies": table(["type", "target", "role", "total", "failed", "codes", "p95_ms", "last_seen", "trace_ids"],
                                  [["SQL", "db", "api", 30, 20, '["0",""]', 50.0, "t", '["abcdef0123456789"]']]),
            "availabilityResults": table(["test", "location", "runs", "failed", "lastSeen"], []),
        }[section]

    client = Client(answers)
    data = appinsights.insights(client, COMPONENT, "1h")
    assert data["errors"] == []
    assert all(path == f"{COMPONENT}/query" and version == "2018-04-20" for path, version, _ in client.calls)
    assert len(client.calls) == 6 and len(calls["requests"]) == 3  # roles, roles a week ago, failing operations
    spans = sorted(calls["requests"])
    assert spans[0] < spans[1]  # one of them is a week earlier
    role = data["roles"][0]
    assert role["failed_pct"] == 10.0 and role["week"]["failed_pct"] == 10.0
    assert data["dependencies"][0]["codes"] == ["0"] and data["dependencies"][0]["trace_id"] == "abcdef0123456789"
    assert data["exceptions"][0]["trace_id"] == "abcdef0123456789"
    assert client.metrics == 2 and data["now"]["failed"] == 9 and data["now"]["trend"] == [4, 9]
    from healthconsole.core.investigate import appinsights as shared
    sent = {q["section"]: q["query"] for q in data["queries"]}
    assert sent["exceptions"] == shared.EXCEPTIONS and sent["dependencies"] == shared.DEPENDENCIES


def test_a_section_without_access_fails_alone_and_says_why():
    client = Client(lambda section, _: table(["role", "total", "failed", "p95"], []), deny=("exceptions",))
    data = appinsights.insights(client, COMPONENT, "6h")
    assert data["exceptions"] == [] and len(data["errors"]) == 1 and "Reader" in data["errors"][0]


def test_unknown_window_is_refused():
    with pytest.raises(ValueError):
        appinsights.insights(Client(lambda *_: {}), COMPONENT, "2d")


def test_roles_seen_only_last_week_are_kept():
    merged = appinsights._merge_roles([], [{"role": "worker", "total": 10, "failed": 5, "p95": 1.0}])
    assert merged == [{"role": "worker", "total": 0, "failed": 0, "p95": None, "failed_pct": None,
                       "week": {"role": "worker", "total": 10, "failed": 5, "p95": 1.0, "failed_pct": 50.0}}]


RG = "/subscriptions/s/resourceGroups/rg/providers"
APP = f"{RG}/Microsoft.Web/sites/shop-api-func"


def component(name):
    return {"id": f"{RG}/microsoft.insights/components/{name}", "name": name, "type": "microsoft.insights/components",
            "tags": {}, "resourceGroup": "rg", "subscriptionId": "s"}


class GraphClient:
    def __init__(self, rows, metrics=None, fail=()):
        self.rows, self.metrics, self.fail, self.queries = rows, metrics or {}, fail, []

    def query(self, kql, subscriptions=None):
        self.queries.append(kql)
        return self.rows

    def get(self, path, api_version, params=None, **_):
        name = path.split("/components/")[1].split("/")[0]
        if name in self.fail:
            raise AccessDenied(path)
        return self.metrics.get(name, {"value": []})


def app_row(tags=None):
    return {"id": APP, "name": "shop-api-func", "type": "Microsoft.Web/sites", "tags": tags or {}, "resourceGroup": "rg", "subscriptionId": "s"}


def test_link_prefers_the_hidden_link_tag_then_an_exact_name_then_asks():
    tagged = app_row({"hidden-link: /app-insights-resource-id": f"{RG}/microsoft.insights/components/elsewhere-appi"})
    link = appinsights.link(GraphClient([tagged, component("shop-api-appi")]), APP)
    assert link["how"] == "tag" and link["component_name"] == "elsewhere-appi"
    old_style = app_row({f"hidden-link:{RG}/microsoft.insights/components/old-appi": "Resource"})
    assert appinsights.link(GraphClient([old_style]), APP)["component_name"] == "old-appi"
    rows = [app_row(), component("shop-api-appi"), component("shop-api-logicapp-appi"), component("billing-appi")]
    link = appinsights.link(GraphClient(rows), APP)
    assert link["how"] == "name" and link["component_name"] == "shop-api-appi" and len(link["candidates"]) == 3
    unrelated = appinsights.link(GraphClient([app_row(), component("billing-appi"), component("orders-appi")]), APP)
    assert unrelated["how"] is None and [c["name"] for c in unrelated["candidates"]] == ["billing-appi", "orders-appi"]
    picked = appinsights.link(GraphClient([app_row()]), APP, f"{RG}/microsoft.insights/components/mine")
    assert picked["how"] == "picked" and picked["component_name"] == "mine"


@pytest.mark.parametrize("counts,expected", [
    ({"requests/count": (100, 100), "requests/failed": (30, 1), "exceptions/count": (0, 0)}, "critical"),
    ({"requests/count": (100, 100), "requests/failed": (6, 1), "exceptions/count": (0, 0)}, "warning"),
    ({"requests/count": (100, 100), "requests/failed": (6, 6), "exceptions/count": (0, 0)}, None),
    ({"requests/count": (100, 100), "requests/failed": (4, 0), "exceptions/count": (0, 0)}, None),
    ({"requests/count": (0, 0), "requests/failed": (0, 0), "exceptions/count": (40, 10)}, "warning"),
    ({"requests/count": (0, 0), "requests/failed": (0, 0), "exceptions/count": (40, 30)}, None),
])
def test_spike_rule(counts, expected):
    verdict = appinsights.assess(counts)
    assert (verdict[0] if verdict else None) == expected


def test_an_exceptions_spike_names_exceptions_as_its_only_cause():
    status, _, causes = appinsights.assess({"requests/count": (796, 639), "requests/failed": (0, 0),
                                            "exceptions/count": (240, 0)})
    assert status == "warning" and causes == ["exceptions"]


def series(*counts):
    return {"timeseries": [{"data": [{"count": c} for c in counts]}]}


def test_spikes_card_reads_last_and_week_buckets_and_counts_what_it_could_not_read():
    metrics = {"shop-appi": {"value": [{"name": {"value": "requests/count"}, **series(100, 0, 100)},
                                       {"name": {"value": "requests/failed"}, **series(1, 0, 30)},
                                       {"name": {"value": "exceptions/count"}, **series(0, 0, 0)}]}}
    rows = [component("shop-appi"), component("quiet-appi"), component("secret-appi")]
    card = appinsights.spikes(GraphClient(rows, metrics, fail=("secret-appi",)), ["s"])
    assert [(i["name"], i["status"]) for i in card["items"]] == [("shop-appi", "critical")]
    assert card["status"] == "critical" and card["checked"] == 3 and "1 component(s) couldn't be read" in card["error"]


def counts_of(total, total_wk, failed, failed_wk, exc=0, exc_wk=0):
    return {"requests/count": (total, total_wk), "requests/failed": (failed, failed_wk), "exceptions/count": (exc, exc_wk)}


def test_a_component_failing_a_lot_as_last_week_is_amber_with_its_reason():
    same = counts_of(2_000, 2_000, 1_500, 1_520, 900, 900)  # 75.0% now, 76.0% last week
    assert appinsights.assess(same) is None  # the spike rule sees no change
    found = appinsights.summary(same)
    assert (found["status"], found["causes"]) == ("warning", ["steady"])
    assert found["why"].startswith("failing a lot, as last week: 75.0% of requests (1,500 of 2,000 failed)")
    assert appinsights.summary(counts_of(1000, 1000, 100, 100))["status"] is None  # 10% stays under the critical rate


def test_the_reason_says_what_moved_against_last_week():
    more = appinsights.summary(counts_of(100, 50, 60, 10))
    assert more["status"] == "critical" and more["why"] == "failing more: 60.0% of requests vs 20.0% last week (60 of 100 failed)"
    throwing = appinsights.summary(counts_of(1_000, 1_000, 50, 300, 300, 100))
    assert throwing["status"] == "warning" and throwing["why"] == "exceptions 3.0× last week (300 vs 100)"
