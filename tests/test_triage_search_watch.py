import time

from healthconsole.core import checks, search, triage
from healthconsole.core.discovery import DiscoveryOptions, discover_group
from healthconsole.core.watchlist import Watchlist
from tests.conftest import SUB, FakeClient, rid
from tests.test_discovery import APP, PE, PLAN, UAMI, VNET, estate


def test_search_ranks_exact_first():
    rows = [
        {"id": rid("rg", "Microsoft.Web/sites", "shop-api-old"), "name": "shop-api-old", "type": "microsoft.web/sites",
         "resourceKind": "app", "resourceGroup": "rg", "subscriptionId": "other"},
        {"id": rid("rg", "Microsoft.Web/sites", "shop-api"), "name": "shop-api", "type": "microsoft.web/sites",
         "resourceKind": "app", "resourceGroup": "rg", "subscriptionId": SUB},
        {"id": f"/subscriptions/{SUB}/resourceGroups/shop-rg", "name": "shop-rg",
         "type": "microsoft.resources/subscriptions/resourcegroups", "resourceKind": "", "resourceGroup": "shop-rg",
         "subscriptionId": SUB},
        {"id": rid("rg", "Microsoft.Insights/metricAlerts", "shop-alert"), "name": "shop-alert",
         "type": "microsoft.insights/metricalerts", "resourceKind": "", "resourceGroup": "rg", "subscriptionId": SUB},
    ]
    client = FakeClient(query_handlers=[("union", lambda q: rows)])
    results = search.search(client, "shop-api")
    assert [r["name"] for r in results][:2] == ["shop-api", "shop-api-old"]
    assert results[0]["display_type"] == "Web App" and "favourite" not in results[0]
    assert all(r["name"] != "shop-alert" for r in search.search(client, "shop"))
    assert any(r["kind"] == "resource group" for r in search.search(client, "shop"))


def test_search_ranks_the_scope_in_use_and_can_keep_only_it():
    queries = []

    def handler(kql):
        queries.append(kql)
        return [{"id": rid("rg", "Microsoft.Web/sites", n), "name": n, "type": "microsoft.web/sites", "resourceKind": "",
                 "resourceGroup": g, "subscriptionId": SUB, "inScope": g == "mine"}
                for n, g in (("api-orders", "other"), ("api-cart", "mine"), ("api", "other"))]

    client = FakeClient(query_handlers=[("contains", handler)])
    results = search.search(client, "api", subscriptions=[SUB], groups=[f"{SUB}/mine"])
    assert [(r["name"], r["in_scope"]) for r in results] == [("api", False), ("api-cart", True), ("api-orders", False)]
    kql = queries[-1]
    assert "order by exact desc, inScope desc, prefix desc" in kql and kql.index("order by") < kql.index("take")
    assert f'"{SUB}/mine"' in kql and "| where inScope" not in kql
    search.search(client, "api", subscriptions=[SUB], only_scope=True)
    assert "| where inScope" in queries[-1]
    assert search.search(client, "api")[0]["in_scope"] is None and "inScope = false" in queries[-1]
    assert search.search(client, "s") == []


def test_triage_cards_isolate_failures():
    def boom(kql):
        raise __import__("healthconsole.core.arm", fromlist=["ArmError"]).ArmError("Query is invalid")
    alerts = [{"id": APP, "type": "microsoft.web/sites", "resourceGroup": "app-rg", "subscriptionId": SUB,
               "alerts": 3, "worst": 1, "lastFired": "2026-11-02T10:00:00Z", "names": ["5xx"]}]
    client = FakeClient(query_handlers=[("AlertsManagementResources", lambda q: alerts), ("resourcechanges", boom)])
    options = {"critical_max_severity": 1, "warning_max_severity": 2, "alert_hours": 24,
               "changes_hours": 1, "include_system_changes": False}
    result = triage.triage(client, [SUB], options)
    cards = {c["key"]: c for c in result["cards"]}
    assert cards["alerts"]["status"] == "critical" and cards["alerts"]["items"][0]["name"] == "shop"
    assert "changes" not in cards and "couldn't be read" in result["changes_error"]
    assert "changed" not in cards["alerts"]["items"][0]
    assert cards["failed"]["items"] == [] and cards["failed"]["error"] is None


def test_a_problem_resource_says_what_changed_on_it_and_nothing_else_is_listed():
    alerts = [{"id": APP, "type": "microsoft.web/sites", "resourceGroup": "app-rg", "subscriptionId": SUB,
               "alerts": 1, "worst": 2, "lastFired": "2026-11-02T10:00:00Z", "names": ["5xx"]}]
    sent = []

    def changes(kql):
        sent.append(kql)
        return [{"id": APP.lower(), "changes": 2, "lastChange": "2026-11-02T09:40:00Z", "who": ["dev@example.com"],
                 "kinds": ["Update"]}]
    client = FakeClient(query_handlers=[("AlertsManagementResources", lambda q: alerts), ("resourcechanges", changes)])
    options = {"critical_max_severity": 1, "warning_max_severity": 2, "alert_hours": 24,
               "changes_hours": 3, "include_system_changes": False}
    item = {c["key"]: c for c in triage.triage(client, [SUB], options)["cards"]}["alerts"]["items"][0]
    assert item["changed"]["text"] == "Changed 2× (Update) in the last 3 h, latest by dev@example.com"
    assert len(sent) == 1 and APP.lower() in sent[0]


def test_rg_clause_limits_to_groups():
    assert triage.rg_clause(None) == ""
    assert '"s1/my-rg"' in triage.rg_clause(["S1/My-RG"])


def test_group_discovery_links_members_and_outside_references():
    rows = estate()
    members = [r for r in rows if r["resourceGroup"] == "app-rg" and "metricalerts" not in r["type"]]
    client = FakeClient(rows, query_handlers=[("resourceGroup =~", lambda q: members)])
    graph = discover_group(client, SUB, "app-rg", DiscoveryOptions())
    members = {n for n, node in graph.nodes.items() if node.hop == 0}
    assert {APP.lower(), PLAN.lower(), UAMI.lower(), PE.lower()} <= members
    assert graph.nodes[VNET.lower()].hop == 1
    pairs = {(e.source, e.target) for e in graph.edges}
    assert (APP.lower(), PLAN.lower()) in pairs and (PE.lower(), APP.lower()) in pairs


def test_watchlist_file_round_trip(tmp_path):
    watch = Watchlist(tmp_path / "nested" / "watchlist.yaml")
    assert watch.items() == []
    watch.add(APP, "shop")
    watch.add(APP.upper(), "duplicate")
    watch.add(f"/subscriptions/{SUB}/resourceGroups/app-rg", "app-rg", "resource group")
    assert [(i["name"], i["kind"]) for i in watch.items()] == [("shop", "resource"), ("app-rg", "resource group")]
    assert watch.path.read_text().startswith("# Managed by Azure Health Console")
    assert [i["name"] for i in watch.remove(APP)] == ["app-rg"]


def test_keyvault_scan_sorts_and_reports_access_errors():
    vaults = [{"id": rid("rg", "Microsoft.KeyVault/vaults", "a"), "name": "a", "resourceGroup": "rg", "subscriptionId": SUB},
              {"id": rid("rg", "Microsoft.KeyVault/vaults", "b"), "name": "b", "resourceGroup": "rg", "subscriptionId": SUB}]
    expired = {"value": [{"name": "old", "properties": {"attributes": {"enabled": True, "exp": int(time.time()) - 86400}}}]}

    def get(params):
        return expired

    client = FakeClient(query_handlers=[("microsoft.keyvault/vaults", lambda q: vaults)],
                        gets={"/vaults/a/secrets": get, "/vaults/a/keys": {"value": []},
                              "/vaults/b/secrets": {"value": []}, "/vaults/b/keys": {"value": []}})
    result = checks.keyvault_scan(client, [SUB], 30)
    assert result["vaults"] == 2 and result["counts"]["critical"] == 1
    assert result["rows"][0]["name"] == "old" and result["rows"][0]["vault"] == "a"


def test_watchlist_is_per_tenant_and_keeps_old_items_everywhere(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text("items:\n- id: /subscriptions/s/resourceGroups/legacy\n  name: legacy\n  kind: resource group\n")
    watch = Watchlist(path)
    watch.add(APP, "shop", tenant="tenant-a")
    watch.add(PLAN, "plan", tenant="tenant-b")
    assert [i["name"] for i in watch.items("tenant-a")] == ["legacy", "shop"]
    assert [i["name"] for i in watch.items("TENANT-B")] == ["legacy", "plan"]
    assert len(watch.items()) == 3
    assert [i["name"] for i in watch.remove(APP, "tenant-a")] == ["legacy"]


def test_vault_errors_are_summarised_by_reason():
    errors = [f"v{i}: No access to Key Vault secret and key metadata." for i in range(5)] + ["x: Azure didn't answer"]
    lines = checks.summarise_errors(errors)
    assert lines[0].startswith("5 vault(s): No access") and "v0, v1, v2…" in lines[0]
    assert lines[1].startswith("1 vault(s): Azure didn't answer")


def test_triage_secrets_respects_the_vault_limit(tmp_path):
    from flask import Flask
    from healthconsole.core import config
    from healthconsole.web import create_blueprint
    from tests.test_report_web_config import WebClient
    settings = config.load(tmp_path / "none.yaml")
    settings["triage"]["secret_scan_max_vaults"] = 1
    client = WebClient(query_handlers=[("summarize n=count()", lambda q: [{"n": 3}]),
                                       ("microsoft.keyvault/vaults", lambda q: [])])
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, settings, config_file=tmp_path / "c.yaml", authorize=lambda: None))
    test = app.test_client()
    assert test.post("/api/secrets", json={"subscriptions": [SUB]}).get_json() == {"too_many": 3, "limit": 1}
    forced = test.post("/api/secrets", json={"subscriptions": [SUB], "force": True}).get_json()
    assert forced["job_id"]
    again = test.post("/api/secrets", json={"subscriptions": [SUB]}).get_json()
    assert again == {"job_id": forced["job_id"], "cached": True}


def test_triage_service_health_is_one_row_per_title():
    import json
    rows = [{"title": "VM allocation failures", "eventType": "ServiceIssue", "phase": "active", "subscriptions": 2,
             "events": 3, "updated": 0,
             "impacts": [json.dumps([{"ImpactedService": "Virtual Machines", "ImpactedRegions": [{"ImpactedRegion": "Canada Central"}]}]),
                         json.dumps([{"ImpactedService": "Virtual Machines", "ImpactedRegions": [{"ImpactedRegion": "Canada East"}]}]),
                         "not json"]}]
    client = FakeClient(query_handlers=[("ServiceHealthResources", lambda q: rows)])
    items, note = triage._service_health(client, [SUB], "", {})
    assert note.startswith("Public Azure status page: no Azure-wide outage posted (read ")  # the empty feed, said so
    assert len(items) == 1 and items[0]["detail"] == "Virtual Machines; Canada Central, Canada East; 2 subscription(s); 3 events"
    assert items[0]["phase"] == "active" and items[0]["link"] == {"health": "serviceIssues"}
    assert "by title=" in client.queries[0]


def _sh(title, kind, phase="active", subscriptions=1, updated=0):
    return {"title": title, "eventType": kind, "phase": phase, "subscriptions": subscriptions, "events": 1,
            "impacts": [], "updated": updated}


def test_service_status_orders_outages_incidents_maintenance_resolved_then_advisories(monkeypatch):
    from healthconsole.core import status_feed
    from healthconsole.core.model import INFO, WARNING
    feed = (b'<rss><channel><item><title>Azure Front Door &amp; CDN</title><link>javascript:alert(1)</link>'
            b'<description>&lt;p&gt;Customers may see &lt;b&gt;errors&lt;/b&gt;&lt;/p&gt;</description>'
            b'<pubDate>Sat, 14 Nov 2026 21:05:00 Z</pubDate></item></channel></rss>')
    monkeypatch.setattr(status_feed, "fetch_xml", lambda session=None: feed)
    ticks = 639302025037058607  # 2026-11-13 21:35 UTC, as Service Health stores it
    rows = [_sh("Retire API v1", "HealthAdvisory", subscriptions=2), _sh("Fix MFA", "HealthAdvisory", subscriptions=9),
            _sh("Patch now", "SecurityAdvisory"), _sh("Old outage", "ServiceIssue", "resolved", updated=ticks),
            _sh("Host updates", "PlannedMaintenance"), _sh("Storage down", "ServiceIssue")]
    client = FakeClient(query_handlers=[("ServiceHealthResources", lambda q: rows)])
    items, note = triage._service_health(client, [SUB], "", {})
    assert [i["name"] for i in items] == ["Azure Front Door & CDN", "Storage down", "Host updates", "Old outage",
                                          "Patch now", "2 health advisories"]
    assert [i["status"] for i in items] == [WARNING, WARNING, INFO, INFO, INFO, INFO]
    assert [i["phase"] for i in items] == ["active", "active", "active", "resolved", "advisory", "advisory"]
    outage = items[0]
    assert outage["detail"] == "Posted 2026-11-14 21:05 UTC: Customers may see errors"
    assert outage["link"] == {"url": status_feed.STATUS_PAGE}  # a non-https link is never passed to the page
    assert items[3]["detail"] =="Resolved, last update 2026-11-13 21:35 UTC; all regions; 1 subscription(s)"
    assert items[5]["detail"] == "Retirements and action required; widest: Fix MFA · Retire API v1"
    kql = client.queries[0]
    assert "tolong(ago(24h))" in kql and "'HealthAdvisory'" in kql
    assert note.startswith("Public Azure status page: 1 Azure-wide outage(s) posted, listed first (read ")


def test_service_status_feed_can_be_off_and_its_failure_is_a_note_not_an_error(monkeypatch):
    from healthconsole.core import status_feed
    from healthconsole.core.guarded import ServiceError
    calls = []

    def down(session=None):
        calls.append(1)
        raise ServiceError("azure.status.microsoft couldn't be reached from this machine.", "network")

    monkeypatch.setattr(status_feed, "fetch_xml", down)
    client = FakeClient(query_handlers=[("ServiceHealthResources", lambda q: [_sh("Storage down", "ServiceIssue")])])
    assert [i["name"] for i in triage._service_health(client, [SUB], "", {"azure_status_feed": False})] == ["Storage down"]
    assert calls == []
    result = triage.triage(client, [SUB], {"changes_hours": 1, "include_system_changes": False}, cards={"service_health"})
    card = result["cards"][0]
    assert card["error"] is None and [i["name"] for i in card["items"]] == ["Storage down"]
    assert "couldn't be reached" in card["note"] and "triage.azure_status_feed" in card["note"]
    assert [l["label"] for l in card["links"]] == ["Azure status", "Microsoft 365 service health"]
    assert all(l["url"].startswith("https://") for l in card["links"])
    triage.triage(client, [SUB], {"changes_hours": 1, "include_system_changes": False}, cards={"service_health"})
    assert calls == [1]  # the failure is kept a minute, not retried on every refresh


def test_types_are_found_by_their_label_azure_name_or_id():
    from healthconsole.core import taxonomy
    assert "microsoft.containerservice/managedclusters" in taxonomy.types_called("Kubernetes")
    assert "microsoft.containerservice/managedclusters" in taxonomy.types_called("aks")
    assert "microsoft.cache/redis" in taxonomy.types_called("azure cache")
    assert taxonomy.types_called("") == [] and taxonomy.types_called("no-such-word-anywhere") == []


def test_browse_filter_finds_resources_in_scope_by_name_or_type():
    from healthconsole.core import inventory
    aks = {"id": rid("rg", "Microsoft.ContainerService/managedClusters", "blue"), "name": "blue",
           "type": "microsoft.containerservice/managedclusters", "kind": "", "resourceGroup": "rg", "subscriptionId": SUB.upper()}
    noise = {"id": rid("rg", "Microsoft.Insights/metricAlerts", "kube-alert"), "name": "kube-alert",
             "type": "microsoft.insights/metricalerts", "kind": "", "resourceGroup": "rg", "subscriptionId": SUB}
    client = FakeClient(query_handlers=[("order by name asc", lambda kql: [aks, noise])])
    found = inventory.find_resources(client, "kubernetes", [SUB], groups=[f"{SUB}/rg"])
    assert [(r["name"], r["display_type"], r["subscription_id"]) for r in found["resources"]] == [("blue", "AKS Cluster", SUB)]
    kql = client.queries[-1]
    assert "name contains \"kubernetes\"" in kql and '"microsoft.containerservice/managedclusters"' in kql
    assert f'"{SUB}/rg"' in kql and not found["truncated"]  # tag-filtered groups narrow it
    assert inventory.find_resources(client, "k", [SUB]) == {"resources": [], "truncated": False}  # too short: no query
    assert inventory.find_resources(client, "blue", []) == {"resources": [], "truncated": False}  # no scope: no query
    many = FakeClient(query_handlers=[("order by name asc", lambda kql: [aks] * 4)])
    assert inventory.find_resources(many, "blue", [SUB], limit=3)["truncated"] is True


def test_browse_lists_alert_rules_and_similar_apart_not_dropped():
    from healthconsole.core import inventory
    rows = [{"id": rid("rg", "Microsoft.Web/sites", "api"), "name": "api", "type": "microsoft.web/sites", "kind": "app",
             "resourceGroup": "rg", "subscriptionId": SUB},
            {"id": rid("rg", "Microsoft.Insights/metricAlerts", "cpu-high"), "name": "cpu-high",
             "type": "microsoft.insights/metricalerts", "kind": "", "resourceGroup": "rg", "subscriptionId": SUB}]
    client = FakeClient(query_handlers=[("properties.provisioningState", lambda kql: rows)])
    listed = inventory.list_resources(client, [(SUB, "rg")])
    assert [r["name"] for r in listed["resources"]] == ["api"]
    assert listed["hidden"] == 1 and [(r["name"], r["display_type"]) for r in listed["hidden_resources"]] == [("cpu-high", "Metric Alert")]



def test_watchlist_check_of_a_resource_group_reads_its_baseline():
    from healthconsole.core import profiles, watchlist
    from healthconsole.core.health import HealthOptions
    item = {"id": f"/subscriptions/{SUB}/resourceGroups/app-rg", "name": "app-rg", "kind": "resource group"}
    rows = watchlist.check(FakeClient(estate()), [item], DiscoveryOptions(depth=0, inbound=False),
                           HealthOptions(detectors=False), profiles.load())
    assert rows[0]["checked_at"] and rows[0]["movers"] == []  # the baseline call raised before, ending the whole check
