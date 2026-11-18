import pytest

from tests.conftest import SUB, FakeClient
from healthconsole.core import exposure, inventory

RG = f"/subscriptions/{SUB}/resourceGroups/app/providers"


def row(kind, name="x", **props):
    return {"id": f"{RG}/{kind}/{name}", "name": name, "type": kind, "resourceGroup": "app", "subscriptionId": SUB,
            "location": "canadacentral", **props}


@pytest.mark.parametrize("props,level", [
    (dict(type="microsoft.storage/storageaccounts", acl="allow"), "open"),
    (dict(type="microsoft.storage/storageaccounts", pna="enabled", acl="deny"), "restricted"),
    (dict(type="microsoft.storage/storageaccounts", pna="disabled", acl="allow"), None),
    (dict(type="microsoft.keyvault/vaults", pna="enabled"), "open"),
    (dict(type="microsoft.sql/servers", pna="enabled"), "unchecked"),
    (dict(type="microsoft.sql/servers", pna="securedbyperimeter"), "restricted"),
    (dict(type="microsoft.web/sites"), None),
    (dict(type="microsoft.documentdb/databaseaccounts", pna="enabled"), "open"),
    (dict(type="microsoft.documentdb/databaseaccounts", pna="enabled", ipRules=2), "restricted"),
    (dict(type="microsoft.containerservice/managedclusters"), "open"),
    (dict(type="microsoft.containerservice/managedclusters", authorizedRanges=1), "restricted"),
    (dict(type="microsoft.containerservice/managedclusters", privateCluster="true"), None),
    (dict(type="microsoft.compute/disks", pna="enabled", diskPolicy="denyall"), None),
    (dict(type="microsoft.compute/disks", pna="enabled", diskPolicy="allowprivate"), "restricted"),
    (dict(type="microsoft.compute/disks", pna="enabled", diskPolicy="allowall"), "unchecked"),
])
def test_levels(props, level):
    found = exposure.classify(props)
    assert (found["level"] if found else None) == level


def test_anonymous_blob_access_is_named():
    found = exposure.classify(dict(type="microsoft.storage/storageaccounts", acl="allow", anonymousBlob="true"))
    assert "anonymous blob access allowed" in found["what"]


def test_browse_rows_carry_their_exposure():
    client = FakeClient(query_handlers=[("Resources", lambda kql: [row("microsoft.storage/storageaccounts", "st", acl="allow")])])
    listed = inventory.list_resources(client, [(SUB, "app")])["resources"]
    assert listed[0]["exposure"]["level"] == "open" and "properties.publicNetworkAccess" in client.queries[0]


WEB = "microsoft.web/sites"
ALLOW_ALL = [{"Action": "Allow", "IpAddress": "Any", "Name": "Allow all", "Priority": 2147483647}]
ONE_IP = [{"Action": "Allow", "IpAddress": "203.0.113.7/32", "Priority": 100},
          {"Action": "Deny", "IpAddress": "Any", "Name": "Deny all", "Priority": 2147483647}]


def web_config(site_id, main_default="allow", main=ALLOW_ALL, scm_default="allow", scm=ALLOW_ALL, use_main=False):
    return {"site": site_id.lower(), "mainDefault": main_default, "main": main, "scmDefault": scm_default,
            "scm": scm, "scmUseMain": use_main}


@pytest.mark.parametrize("config,level,what", [
    (dict(), "open", "site and SCM (Kudu) open to all networks"),
    (dict(main_default="deny", main=ONE_IP), "open", "SCM (Kudu) open to all networks; the site has access restrictions"),
    (dict(scm_default="deny", scm=ONE_IP), "open", "site open to all networks; SCM (Kudu) has access restrictions"),
    (dict(main_default="deny", main=ONE_IP, scm_default="deny", scm=ONE_IP), "restricted", None),
    (dict(main_default="deny", main=ONE_IP, use_main=True), "restricted", None),
    (dict(main_default="deny", main=ONE_IP + ALLOW_ALL, use_main=True), "open", None),
])
def test_web_app_access_restrictions_decide_the_level(config, level, what):
    site = row(WEB, "app", pna="enabled")
    site["web"] = web_config(site["id"], **config)
    found = exposure.classify(site)
    assert found["level"] == level and (what is None or found["what"] == what)


def test_web_app_restrictions_come_from_one_narrow_read():
    public, private = row(WEB, "public", pna="enabled"), row(WEB, "private", pna="disabled")
    client = FakeClient(query_handlers=[("appserviceresources", lambda kql: [
        web_config(public["id"], main_default="deny", main=ONE_IP, use_main=True)])])
    sent, error = exposure.add_web_restrictions(client, [public, private, row("microsoft.storage/storageaccounts")])
    assert error is None and len(sent) == 1 and client.queries == sent
    assert public["id"].lower() in sent[0] and private["id"].lower() not in sent[0]
    assert "PublishingPassword" not in sent[0] and "ConnectionStrings" not in sent[0]
    assert exposure.classify(public)["level"] == "restricted" and "web" not in private


def test_no_web_apps_means_no_read():
    client = FakeClient()
    assert exposure.add_web_restrictions(client, [row("microsoft.storage/storageaccounts", acl="allow")]) == ([], None)
    assert client.queries == []


def test_a_failed_web_read_leaves_the_site_not_read():
    from healthconsole.core.arm import ArmError

    def fail(kql):
        raise ArmError("throttled")
    site = row(WEB, "app", pna="enabled")
    sent, error = exposure.add_web_restrictions(FakeClient(query_handlers=[("appserviceresources", fail)]), [site])
    assert error == "throttled" and len(sent) == 1
    assert exposure.classify(site)["level"] == "unchecked"


def test_web_reads_are_batched(monkeypatch):
    monkeypatch.setattr(exposure, "WEB_BATCH", 2)
    client = FakeClient(query_handlers=[("appserviceresources", lambda kql: [])])
    sent, _ = exposure.add_web_restrictions(client, [row(WEB, f"a{i}", pna="enabled") for i in range(5)])
    assert len(sent) == 3


def test_security_and_browse_judge_web_apps_by_their_restrictions():
    from healthconsole.core import security
    site = {**row(WEB, "app", pna="enabled")}
    web = ("appserviceresources", lambda kql: [web_config(site["id"])])
    data = security.posture(FakeClient(query_handlers=[web, ("isnotempty(properties)", lambda kql: [dict(site)])]),
                            [SUB], history=False)
    exposure_rows = [r for r in data["rows"] if r["category"] == "exposure"]
    assert [(r["severity"], r["what"]) for r in exposure_rows] == [("warning", "site and SCM (Kudu) open to all networks")]
    assert any(q["label"] == "web access restrictions" for q in data["queries"])
    browse = FakeClient(query_handlers=[web, ("Resources", lambda kql: [dict(site)])])
    listed = inventory.list_resources(browse, [(SUB, "app")])["resources"]
    assert listed[0]["exposure"]["level"] == "open"


SB, EH, SQL = "microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces", "microsoft.sql/servers"
IP = [{"action": "Allow", "ipMask": "203.0.113.0/24"}]


@pytest.mark.parametrize("props,level", [
    (dict(type="microsoft.search/searchservices", pna="enabled", searchIpRules=0), "open"),
    (dict(type="microsoft.search/searchservices", pna="enabled", searchIpRules=2), "restricted"),
    (dict(type="microsoft.search/searchservices", pna="disabled"), None),
    (dict(type="microsoft.kubernetesconfiguration/privatelinkscopes", pna="enabled"), None),
    (dict(type="microsoft.recoveryservices/vaults", pna="enabled"), "unchecked"),
])
def test_types_judged_from_their_own_properties(props, level):
    found = exposure.classify(props)
    assert (found["level"] if found else None) == level


def test_a_recovery_vault_says_it_has_no_firewall_instead_of_not_read():
    found = exposure.classify(dict(type="microsoft.recoveryservices/vaults", pna="enabled"))
    assert "no IP firewall" in found["what"] and "aren't read" not in found["what"]


@pytest.mark.parametrize("rule_set,level,stored", [
    (dict(defaultAction="Deny", ipRules=IP), "restricted", False),
    (dict(defaultAction="Deny", ipRules=[], virtualNetworkRules=[{"subnet": {}}]), "restricted", False),
    (dict(defaultAction="Deny", ipRules=[]), "open", False),  # no rules: Azure treats Deny as Allow
    (dict(defaultAction="Allow", ipRules=IP), "open", True),  # rules kept but not enforced
    (dict(defaultAction="Allow"), "open", False),
])
def test_a_namespace_rule_set_applies_only_with_deny(rule_set, level, stored):
    found = exposure.classify({**row(SB, pna="enabled"), "ruleSet": rule_set})
    assert found["level"] == level and ("stored rule(s) not applied" in found["what"]) == stored


def fw(start, end):
    return {"properties": {"startIpAddress": start, "endIpAddress": end}}


@pytest.mark.parametrize("rules,level,what", [
    ([fw("0.0.0.0", "255.255.255.255")], "open", "every address"),
    ([fw("0.0.0.0", "0.0.0.0")], "restricted", "every Azure service"),
    ([fw("203.0.113.1", "203.0.113.9"), fw("0.0.0.0", "0.0.0.0")], "restricted", "1 address range(s) and every Azure"),
    ([fw("203.0.113.1", "203.0.113.9")], "restricted", "1 address range(s)"),
    ([], "restricted", "no IP firewall rules"),
])
def test_sql_firewall_rules_decide_the_level(rules, level, what):
    found = exposure.classify({**row(SQL, pna="enabled"), "sqlRules": rules})
    assert found["level"] == level and what in found["what"]


def test_firewall_reads_go_only_to_public_namespaces_and_servers():
    from healthconsole.core.arm import AccessDenied
    sb, eh, sql = row(SB, "sb", pna="enabled"), row(EH, "eh", pna="enabled"), row(SQL, "sql", pna="enabled")
    private, other = row(SB, "private", pna="disabled"), row("microsoft.storage/storageaccounts", "st", pna="enabled")

    def denied(params):
        raise AccessDenied("/eh", "no")
    client = FakeClient(gets={"/sb/networkRuleSets/default": {"properties": {"defaultAction": "Deny", "ipRules": IP}},
                              "/eh/networkRuleSets/default": denied,
                              "/sql/firewallRules": {"value": [fw("0.0.0.0", "255.255.255.255")]}})
    count, error = exposure.add_firewall_rules(client, [sb, eh, sql, private, other])
    assert count == 3 and client.call_count == 3 and error.startswith("1 of 3 not readable")
    assert [exposure.classify(r)["level"] for r in (sb, eh, sql)] == ["restricted", "unchecked", "open"]
    assert "ruleSet" not in private and "ruleSet" not in other


def test_security_reads_firewalls_and_triage_does_not():
    from healthconsole.core import security
    sb = row(SB, "sb", pna="enabled")
    client = FakeClient(query_handlers=[("isnotempty(properties)", lambda kql: [dict(sb)])],
                        gets={"networkRuleSets/default": {"properties": {"defaultAction": "Allow"}}})
    data = security.posture(client, [SUB], history=False)
    assert [r["severity"] for r in data["rows"] if r["category"] == "exposure"] == ["warning"]
    assert any(q["label"] == "firewall rules" for q in data["queries"])
    reads = []
    quiet = FakeClient(query_handlers=[("isnotempty(properties)", lambda kql: [dict(sb)])],
                       gets={"networkRuleSets/default": lambda params: reads.append(params) or {"properties": {}}})
    found, _ = security.exposure_warnings(quiet, [SUB])
    assert found == [] and reads == []


@pytest.mark.parametrize("props,level,what", [
    (dict(type="microsoft.apimanagement/service", pna="enabled", apimVnet="internal"), "restricted", "internal mode"),
    (dict(type="microsoft.apimanagement/service", pna="enabled", apimVnet="none"), "open", "API gateway"),
    (dict(type="microsoft.kusto/clusters", pna="enabled", kustoRanges=0), "open", "all networks"),
    (dict(type="microsoft.kusto/clusters", pna="enabled", kustoRanges=2), "restricted", "allowed IP ranges"),
    (dict(type="microsoft.databricks/workspaces", pna="enabled"), "unchecked", "IP access lists"),
    (dict(type="microsoft.datafactory/factories", pna="enabled"), "unchecked", "no IP firewall"),
])
def test_more_types_are_judged_by_what_they_have(props, level, what):
    found = exposure.classify(props)
    assert found["level"] == level and what in found["what"] and "aren't read here" not in found["what"]


def test_synapse_firewall_rules_are_read_like_sql():
    syn = row("microsoft.synapse/workspaces", "syn", pna="enabled")
    client = FakeClient(gets={"/syn/firewallRules": {"value": [fw("0.0.0.0", "0.0.0.0")]}})
    assert exposure.add_firewall_rules(client, [syn]) == (1, None)
    assert exposure.classify(syn)["level"] == "restricted" and "every Azure service" in exposure.classify(syn)["what"]
