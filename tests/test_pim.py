from datetime import datetime, timedelta, timezone

from flask import Flask

from healthconsole.core import config, pim
from healthconsole.core.arm import ArmError
from tests.conftest import SUB, FakeClient, rid

VAULT = rid("rg-sec", "Microsoft.KeyVault/vaults", "kv1")


def instance(scope, role, role_id, member="Group"):
    return {"properties": {"scope": scope, "roleDefinitionId": f"/providers/x/roleDefinitions/{role_id}",
                           "memberType": member,
                           "expandedProperties": {"scope": {"displayName": scope.rsplit("/", 1)[-1], "type": "x"},
                                                  "roleDefinition": {"displayName": role}}}}


ELIGIBLE = [instance(f"/subscriptions/{SUB}", "Reader", "r1"),
            instance(f"/subscriptions/{SUB}/resourceGroups/rg-sec", "Key Vault Administrator", "k1"),
            instance(f"/subscriptions/{SUB}/resourceGroups/other", "Reader", "r1"),
            instance("/providers/Microsoft.Management/managementGroups/mg1", "Contributor", "c1"),
            instance(f"/subscriptions/{SUB}/resourceGroups/RG-SEC", "Monitoring Reader", "m1")]
def request(scope, role_id, start, duration="PT8H", kind="SelfActivate", status="Provisioned"):
    return {"properties": {"scope": scope, "roleDefinitionId": f"/providers/x/roleDefinitions/{role_id}", "requestType": kind,
                           "status": status, "scheduleInfo": {"startDateTime": start, "expiration": {"duration": duration}}}}


NOW = datetime.now(timezone.utc)
HELD = [request(f"/subscriptions/{SUB}/resourceGroups/rg-sec", "m1", (NOW - timedelta(hours=1)).isoformat())]


def client(eligible=ELIGIBLE, held=HELD):
    def answer(items):
        return lambda params: {"value": items(params) if callable(items) else items}
    return FakeClient(gets={"roleEligibilityScheduleInstances": answer(eligible),
                            "roleAssignmentScheduleRequests": answer(held)})


def setup_function():
    pim.forget()


def test_hints_cover_the_target_rank_and_skip_held_roles():
    result = pim.hints(pim.roles(client()), VAULT, required="Reader on the vault, its resource group or subscription")
    assert [(h["role"], h["coverage"]) for h in result["hints"]] == [
        ("Reader", "yes"), ("Key Vault Administrator", "yes"), ("Contributor", "maybe")]
    assert result["activate_url"].startswith("https://portal.azure.com/")


def test_roles_are_cached_until_forgotten():
    fake = client()
    pim.roles(fake)
    pim.roles(fake)
    assert fake.call_count == 2
    pim.forget()
    pim.roles(fake)
    assert fake.call_count == 4


def test_unreadable_eligibility_is_reported():
    def denied(params):
        raise ArmError("No access", 403)
    result = pim.hints(pim.roles(client(eligible=denied)), VAULT)
    assert result["hints"] == [] and result["error"] == "No access"


def test_roles_you_hold_still_list_in_a_tenant_without_pim():
    def unlicensed(params):
        raise ArmError("The tenant needs to have Microsoft Entra ID P2", 400)
    owned = [{"scope": f"/subscriptions/{SUB}", "role_id": "o1", "role": "Owner", "via": ""}]
    fake = client(eligible=unlicensed)
    result = pim.listing(pim.roles(fake, standing=lambda: owned))
    assert result["error"].startswith("The tenant needs") and result["total"] == 0
    assert [r["role"] for r in result["current"]] == ["Owner"]
    first = fake.call_count  # eligibility and activations are read together
    pim.roles(fake, standing=lambda: owned)
    assert fake.call_count == first  # a missing licence is kept, not reread on every page


def test_listing_filters_and_marks_active():
    result = pim.listing(pim.roles(client()), "reader rg-sec")
    assert result["total"] == 5 and [r["role"] for r in result["rows"]] == ["Monitoring Reader"]
    assert result["rows"][0]["active"]["child"] is False


def test_roles_you_already_hold_are_marked_listed_last_and_not_hinted():
    owned = [{"scope": f"/subscriptions/{SUB}", "role_id": "r1", "via": "ops"}]
    found = pim.roles(client(), standing=lambda: owned)
    rows = pim.listing(found)["rows"]
    held = [(r["scope_name"], r["held"]["same"], r["held"]["via"]) for r in rows if r["held"]]
    assert held == [(SUB, True, "ops"), ("other", False, "ops")]
    assert [bool(r["held"]) for r in rows] == [False, False, False, True, True]  # active first, held last
    assert pim.listing(found)["held"] == 2
    assert "Reader" not in [h["role"] for h in pim.hints(found, VAULT)["hints"]]


def test_unreadable_standing_roles_are_reported_not_fatal():
    def refused():
        raise ArmError("No graph", 403)
    found = pim.roles(client(), standing=refused)
    result = pim.listing(found)
    assert result["standing_error"] == "No graph" and result["held"] == 0 and result["total"] == 5


def test_current_roles_list_standing_and_activated_with_names_and_filters():
    sub = f"/subscriptions/{SUB}"
    found = {"eligible": [], "standing": [
        {"scope": sub, "role_id": "r1", "role": "Reader", "via": "Ops-Readers"},
        {"scope": f"{sub}/resourcegroups/app", "role_id": "r2", "role": "Contributor", "via": ""},
        {"scope": "/subscriptions/other", "role_id": "r1", "role": "Reader", "via": ""}],
        "held": [{"scope": f"{sub}/resourcegroups/app", "role_id": "r2", "end": "2026-11-13T20:00:00Z"}]}
    rows = pim.current(found)
    assert [(r["role"], r["scope_name"], r["until"]) for r in rows][0] == ("Contributor", "app", "2026-11-13T20:00:00Z")
    assert {r["scope_name"] for r in rows} == {"app", SUB, "other"}
    assert [r["role"] for r in pim.current(found, "ops")] == ["Reader"]
    assert len(pim.current(found, subscriptions=[SUB], scope_only=True)) == 2
    assert pim.listing(found)["current_total"] == 3


def test_active_activations():
    scope = f"/subscriptions/{SUB}"
    earlier = (NOW - timedelta(hours=3)).isoformat()
    later = (NOW - timedelta(hours=1)).isoformat()
    requests = [request(scope, "a", earlier, "PT8H"), request(scope, "b", earlier, "PT1H"),
                request(scope, "c", earlier), request(scope, "c", later, kind="SelfDeactivate"),
                request(scope, "d", earlier, "P1D", status="Denied")]
    assert [a["role_id"] for a in pim.active_activations(requests)] == ["a"]


def test_endpoints():
    from healthconsole.web import create_blueprint

    class Client(FakeClient):
        def refresh(self):
            pass
    fake = Client(gets={"roleEligibilityScheduleInstances": {"value": ELIGIBLE}, "roleAssignmentScheduleRequests": {"value": []}})
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: fake, config.load("/nonexistent")))
    web = app.test_client()
    assert web.get("/api/pim/hints?id=nope").status_code == 400
    assert len(web.get(f"/api/pim/hints?id={VAULT}").get_json()["hints"]) == 4
    assert web.get("/api/pim?q=administrator").get_json()["matched"] == 1


def test_in_scope_covers_subscriptions_groups_and_resources():
    groups = [f"/subscriptions/{SUB}/resourcegroups/rg-sec"]
    assert pim.in_scope(f"/subscriptions/{SUB}", [SUB]) == "everything in it"
    assert pim.in_scope(f"/subscriptions/{SUB}", [SUB], groups) == "1 resource group(s)"
    assert pim.in_scope(f"/subscriptions/{SUB}/resourcegroups/rg-sec", [SUB], groups) == "1 resource group"
    assert pim.in_scope(f"/subscriptions/{SUB}/resourcegroups/other", [SUB], groups) == ""
    assert pim.in_scope(VAULT.lower(), [SUB]) == "1 resource"
    assert pim.in_scope("/subscriptions/someone-else", [SUB]) == ""
    assert pim.in_scope("/providers/microsoft.management/managementgroups/mg1", [SUB]).startswith("may cover")


def test_listing_scope_only_via_group_and_active_names():
    eligible = [dict(instance(f"/subscriptions/{SUB}", "Reader", "r1")),
                instance(f"/subscriptions/{SUB}/resourceGroups/rg-sec", "Key Vault Administrator", "k1", member="Direct"),
                instance("/subscriptions/elsewhere", "Reader", "r1")]
    eligible[0]["properties"]["expandedProperties"]["principal"] = {"displayName": "az-readers", "type": "Group"}
    held = [request(f"/subscriptions/{SUB}", "r1", (NOW - timedelta(hours=1)).isoformat())]
    found = pim.roles(client(eligible, held))
    result = pim.listing(found, subscriptions=[SUB], scope_only=True)
    assert [(r["role"], r["via"], r["covers"], bool(r["active"])) for r in result["rows"]] == [
        ("Reader", "az-readers", "everything in it", True), ("Key Vault Administrator", "", "1 resource group", False)]
    assert result["active"][0]["role"] == "Reader" and result["active"][0]["via"] == "az-readers"
    assert pim.listing(found, "az-readers")["matched"] == 1


def test_graph_reads_only_the_own_groups_path_and_follows_pages():
    import pytest as _pytest

    from healthconsole.core import graph
    from healthconsole.core.arm import WriteBlocked
    from tests.conftest import FakeTokens

    user = "https://graph.microsoft.com/v1.0/users/11111111-2222-3333-4444-555555555555"
    for url in ("https://graph.microsoft.com/v1.0/users", user, f"{user}/transitiveMemberOf/microsoft.graph.group"):
        graph.check_graph_read("GET", url)
    for method, url in (("POST", "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group"),
                        ("PATCH", user),
                        ("GET", f"{user}/appRoleAssignments"),
                        ("GET", "https://graph.microsoft.com/v1.0/users/someone@example.com"),
                        ("GET", "https://graph.microsoft.com/v1.0/groups"),
                        ("GET", "https://evil.example/v1.0/me/transitiveMemberOf/microsoft.graph.group")):
        with _pytest.raises(WriteBlocked):
            graph.check_graph_read(method, url)

    class Response:
        def __init__(self, payload, status=200):
            self.payload, self.status_code = payload, status

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.urls = []

        def request(self, method, url, params=None, headers=None, timeout=None, allow_redirects=True):
            self.urls.append(url)
            if len(self.urls) == 1:
                return Response({"value": [{"id": "2", "displayName": "zeta"}],
                                 "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group?$skiptoken=x"})
            return Response({"value": [{"id": "1", "displayName": "alpha"}]})

    graph.forget()
    session = Session()
    assert [g["name"] for g in graph.my_groups(FakeTokens(), session)] == ["alpha", "zeta"]
    assert len(session.urls) == 2
    graph.forget()


def test_groups_endpoint_without_a_cli_sign_in():
    from healthconsole.web import create_blueprint

    class Client(FakeClient):
        def refresh(self):
            pass
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: Client(), config.load("/nonexistent")))
    body = app.test_client().get("/api/access/groups").get_json()
    assert body["groups"] == [] and "no Azure CLI sign-in" in body["error"]
