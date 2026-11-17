import pytest
from flask import Flask

from tests.conftest import SUB, FakeClient
from healthconsole.core import config, security
from healthconsole.core.arm import ArmError
from healthconsole.web import create_blueprint

RG = f"/subscriptions/{SUB}/resourceGroups/app/providers"


def row(kind, name="x", **props):
    return {"id": f"{RG}/{kind}/{name}", "name": name, "type": kind, "resourceGroup": "app", "subscriptionId": SUB,
            "location": "canadacentral", **props}


def found(kind, **props):
    return {(f["category"], f["severity"], f["what"]) for f in security.findings(dict(type=kind, **props))}


def test_settings_are_checked_by_property_on_any_type():
    assert ("keys", "info", "local (key or SAS) authentication allowed") in found("microsoft.whatever/new", localAuth="false")
    assert ("transport", "info", "minimum TLS 1.0") in found("microsoft.servicebus/namespaces", tls="1.0")
    assert ("transport", "info", "HTTP allowed (HTTPS only is off)") in found("microsoft.web/sites", httpsOnly="false")
    assert ("keys", "info", "admin user enabled") in found("microsoft.containerregistry/registries", adminUser="true")
    assert not found("microsoft.servicebus/namespaces", tls="1.2", localAuth="true")


def test_type_specific_defaults():
    assert ("keys", "info", "shared key access allowed") in found("microsoft.storage/storageaccounts", pna="disabled")
    assert not found("microsoft.storage/storageaccounts", pna="disabled", sharedKey="false")
    assert ("keys", "info", "SQL logins allowed (not Entra-only)") in found("microsoft.sql/servers", pna="disabled")
    assert ("deletion", "info", "purge protection off") in found("microsoft.keyvault/vaults", pna="disabled", purge="false")


def test_open_exposure_is_a_warning_and_restricted_is_listed_apart():
    assert ("exposure", "warning", "public endpoint open to all networks") in found(
        "microsoft.keyvault/vaults", pna="enabled", purge="true")
    restricted = found("microsoft.storage/storageaccounts", pna="enabled", acl="deny", sharedKey="false", anonymousBlob="true")
    assert {s for _, s, _ in restricted} == {"restricted", "warning"}  # anonymous blob still warns behind a firewall


@pytest.mark.parametrize("port,ports,risky,label", [
    ("22", [], True, "SSH (22)"), ("*", [], True, "all ports"), ("3000-3400", [], True, "RDP (3389)"),
    ("", ["443", "8443"], False, "port(s) 443, 8443")])
def test_nsg_rules_name_risky_ports(port, ports, risky, label):
    finding = security.nsg_findings([row("microsoft.network/networksecuritygroups", "nsg", rule="r", port=port, ports=ports)])[0]
    assert finding["severity"] == ("warning" if risky else "info") and label in finding["what"]


def test_a_public_ip_is_reported_on_its_vm():
    vm = f"{RG}/Microsoft.Compute/virtualMachines/jump"
    finding = security.ip_findings([{"nic": f"{RG}/microsoft.network/networkinterfaces/jump-nic", "nicName": "jump-nic",
                                     "vm": vm, "resourceGroup": "app", "subscriptionId": SUB}])[0]
    assert finding["id"] == vm and finding["name"] == "jump" and finding["severity"] == "warning"


def posture_client(fail=()):
    st = row("microsoft.storage/storageaccounts", "open-st", acl="allow", sharedKey="false")
    kv = row("microsoft.keyvault/vaults", "kv", pna="disabled", purge="false")

    def answer(name, rows):
        def handler(kql):
            if name in fail:
                raise ArmError("throttled")
            return rows
        return handler
    return FakeClient(query_handlers=[
        ("resourcechanges", answer("changes", [{"target": st["id"].lower(), "created": True, "at": "2026-11-12T09:00:00Z",
                                                "who": "someone@example.com"}])),
        ("networksecuritygroups' | mv-expand", answer("nsg", [row("microsoft.network/networksecuritygroups", "nsg", rule="ssh", port="22")])),
        ("networkinterfaces' | mv-expand", answer("public_ips", [])),
        ("isnotempty(properties)", answer("settings", [st, kv]))])


def test_posture_orders_warnings_first_and_adds_who_created_it():
    data = security.posture(posture_client(), [SUB])
    assert [(r["name"], r["severity"]) for r in data["rows"]] == [("nsg", "warning"), ("open-st", "warning"), ("kv", "info")]
    assert data["rows"][1]["created_by"] == "someone@example.com" and data["counts"]["exposure"]["warning"] == 2
    assert data["counts"]["deletion"]["info"] == 1 and data["errors"] == []


def test_a_failed_read_is_named_and_the_rest_still_stand():
    data = security.posture(posture_client(fail=("changes", "nsg")), [SUB])
    assert [r["name"] for r in data["rows"]] == ["open-st", "kv"] and len(data["errors"]) == 2


def test_the_change_query_avoids_by_as_a_column_and_joins_on_the_server():
    kql = security.queries()["changes"]
    assert "arg_max(at, who)" in kql and " by = " not in kql  # Resource Graph can't parse arg_max(at, by)
    assert "join kind=inner (Resources" in kql


def test_triage_card_lists_every_exposure_warning_once_per_resource_as_info():
    from healthconsole.core import triage
    items = triage._public_access(posture_client(), [SUB], "", {})
    assert sorted(i["name"] for i in items) == ["nsg", "open-st"] and {i["status"] for i in items} == {"info"}
    assert "SSH (22)" in next(i["detail"] for i in items if i["name"] == "nsg")
    # Info for Triage's counts, but labelled and coloured as Security labels the same finding
    assert {(i["badge"]["tone"], i["badge"]["label"]) for i in items} == {("warning", security.EXPOSED)}


def test_an_exposure_warning_carries_the_same_badge_on_security_as_on_triage():
    rows = security.posture(posture_client(), [SUB], history=False)["rows"]
    exposed = [r for r in rows if r["category"] == "exposure" and r["severity"] == "warning"]
    assert exposed and all(r["badge"] == {"tone": "warning", "label": security.EXPOSED} for r in exposed)
    assert not any("badge" in r for r in rows if r["category"] != "exposure" or r["severity"] != "warning")


def test_triage_card_says_so_when_nothing_could_be_read():
    from healthconsole.core import triage
    with pytest.raises(ArmError):
        triage._public_access(posture_client(fail=("settings", "nsg", "public_ips")), [SUB], "", {})


def test_one_resource_gets_its_settings_and_a_vm_its_public_ip():
    vm = f"{RG}/Microsoft.Compute/virtualMachines/jump"
    client = FakeClient(query_handlers=[
        ("networkinterfaces' | mv-expand", lambda kql: [{"nic": f"{RG}/microsoft.network/networkinterfaces/n", "nicName": "n",
                                                        "vm": vm, "resourceGroup": "app", "subscriptionId": SUB}]),
        ("isnotempty(properties)", lambda kql: [])])
    data = security.for_resource(client, vm)
    assert [f["what"] for f in data["findings"]] == ["public IP on its network interface"]
    assert vm.lower() in client.queries[0] and any("where tolower(vm) in" in q for q in client.queries)
    kv = row("microsoft.keyvault/vaults", "kv", pna="enabled", acl="deny", purge="false")
    data = security.for_resource(FakeClient(query_handlers=[("isnotempty(properties)", lambda kql: [kv])]), kv["id"])
    assert [f["severity"] for f in data["findings"]] == ["info", "restricted"] and len(data["queries"]) == 1


def test_route_needs_a_subscription():
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: posture_client(), config.load("/nonexistent")))
    http = app.test_client()
    assert http.get("/api/security").status_code == 400
    assert http.get(f"/api/security?subscription={SUB}").get_json()["counts"]["exposure"]["warning"] == 2
