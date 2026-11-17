import pytest
import time

from flask import Flask

from healthconsole.core import config, graph, user_access
from healthconsole.core.arm import AccessDenied
from tests.conftest import FakeClient, FakeTokens

USER = "aaaaaaaa-0000-0000-0000-000000000001"
GROUP = "bbbbbbbb-0000-0000-0000-000000000002"
SUB_A, SUB_B = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
READER, CONTRIBUTOR = "acdd72a7-3385-48ef-bd42-f606fba81ae7", "b24988ac-6180-42a0-ab88-20f7382dd24c"
MG = "/providers/Microsoft.Management/managementGroups/platform"


def eligible(scope, via=None):
    return {"properties": {"scope": scope, "roleDefinitionId": f"/providers/x/roleDefinitions/{CONTRIBUTOR}",
                           "memberType": "Group" if via else "Direct",
                           "expandedProperties": {"roleDefinition": {"displayName": "Contributor"},
                                                  "principal": {"type": "Group" if via else "User",
                                                                "displayName": via or "someone"}}}}


class AccessClient(FakeClient):
    def __init__(self, refuse=()):
        self.refuse = refuse
        self.paths = []
        super().__init__(query_handlers=[
            ("principalId in", lambda kql: [
                {"principalId": USER, "scope": f"/subscriptions/{SUB_A}/resourceGroups/app-rg", "roleGuid": READER, "role": None},
                {"principalId": GROUP, "scope": f"/subscriptions/{SUB_B}", "roleGuid": "custom-1", "role": "App Operator"}]),
            ("managementGroupAncestorsChain", lambda kql: [{"subscriptionId": SUB_A, "parent": "platform"},
                                                          {"subscriptionId": SUB_B, "parent": "platform"}]),
        ])

    def get(self, path, api_version, params=None, tolerate=(), timeout=None):
        self.paths.append(path)
        if any(s in path for s in self.refuse):
            raise AccessDenied(path)
        if path.endswith("roleEligibilityScheduleInstances"):
            sub = path.split("/")[2]
            return {"value": [eligible(MG, via="ops"), eligible(f"/subscriptions/{sub}/resourceGroups/data-rg")]}
        if path.endswith("roleAssignments"):
            return {"value": [{"properties": {"scope": MG, "principalId": GROUP,
                                              "roleDefinitionId": f"/providers/x/roleDefinitions/{READER}"}},
                              {"properties": {"scope": f"/subscriptions/{SUB_A}", "principalId": USER,
                                              "roleDefinitionId": f"/providers/x/roleDefinitions/{READER}"}}]}
        if path.endswith("roleDefinitions"):
            return {"value": [{"name": READER, "properties": {"roleName": "Reader"}},
                              {"name": CONTRIBUTOR, "properties": {"roleName": "Contributor"}}]}
        if path == "/subscriptions":
            return {"value": [{"subscriptionId": SUB_A, "displayName": "sub-a"}, {"subscriptionId": SUB_B, "displayName": "sub-b"}]}
        return None


def setup(monkeypatch):
    user_access._built_in.clear()
    monkeypatch.setattr(graph, "user_groups", lambda provider, user_id: [{"id": GROUP, "name": "ops"}])


def test_check_reads_groups_active_management_group_and_eligible_roles(monkeypatch):
    setup(monkeypatch)
    client = AccessClient()
    published = []
    result = user_access.check(client, FakeTokens(), {"id": USER, "name": "Ann"}, [SUB_A, SUB_B],
                               publish=published.append)
    assert published[0]["done"] is False and published[0]["eligible"] == []
    active = {(r["scope_label"], r["role"], r["via"]) for r in result["active"]}
    assert active == {("sub-a / app-rg", "Reader", ""), ("sub-b", "App Operator", "ops"),
                      ("Management group platform", "Reader", "ops")}
    # one assignments read for the one parent management group, not one per subscription
    assert sum(p.endswith("roleAssignments") for p in client.paths) == 1
    # the management-group eligibility repeats per subscription but is listed once
    assert sorted(r["scope_label"] for r in result["eligible"]) == ["Management group platform", "sub-a / data-rg",
                                                                      "sub-b / data-rg"]
    assert result["groups"] == [{"id": GROUP, "name": "ops", "roles": 3}]
    assert {r["scope_label"]: r["scope_type"] for r in result["active"]} == {
        "sub-a / app-rg": "resource group", "sub-b": "subscription", "Management group platform": "management group"}
    # built-in names are read once and reused
    user_access.check(client, FakeTokens(), {"id": USER}, [SUB_A])
    assert sum(p.endswith("roleDefinitions") for p in client.paths) == 1


def test_refused_reads_are_named_not_fatal(monkeypatch):
    setup(monkeypatch)
    result = user_access.check(AccessClient(refuse=(f"{SUB_B}/providers/Microsoft.Authorization/roleEligibility",)),
                               FakeTokens(), {"id": USER}, [SUB_A, SUB_B])
    assert any("1 subscription(s)" in n for n in result["notes"])
    assert {r["scope_label"] for r in result["eligible"]} == {"Management group platform", "sub-a / data-rg"}


def test_user_search_needs_two_characters_escapes_quotes_and_finds_ids():
    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.calls = []

        def request(self, method, url, params=None, headers=None, timeout=None, allow_redirects=True):
            self.calls.append((url, params))
            if url.endswith(USER):
                return Response({"id": USER, "displayName": "Ann", "userPrincipalName": "ann@example.com"})
            return Response({"value": [{"id": USER, "displayName": "O'Neil", "accountEnabled": False}]})

    graph.forget()
    session = Session()
    assert graph.search_users(FakeTokens(), "a", session) == []
    found = graph.search_users(FakeTokens(), "o'n", session)
    assert found[0]["name"] == "O'Neil" and found[0]["enabled"] is False
    assert "startswith(displayName,'o''n')" in session.calls[0][1]["$filter"] and session.calls[0][1]["$top"] == "10"
    graph.search_users(FakeTokens(), "o'n", session)
    assert len(session.calls) == 1  # cached
    assert graph.search_users(FakeTokens(), USER, session)[0]["upn"] == "ann@example.com"
    graph.forget()


def test_user_access_api(monkeypatch):
    from healthconsole.web import create_blueprint

    setup(monkeypatch)
    client = AccessClient()
    client.token_provider = FakeTokens()
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load("/nonexistent")))
    http = app.test_client()
    assert http.post("/api/access/user", json={"user": {"id": "nope"}, "subscriptions": [SUB_A]}).status_code == 400
    assert http.post("/api/access/user", json={"user": {"id": USER}}).status_code == 400
    many = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(user_access.MAX_SUBSCRIPTIONS + 1)]
    assert http.post("/api/access/user", json={"user": {"id": USER}, "subscriptions": many}).get_json()["too_many"] == 101

    job_id = http.post("/api/access/user", json={"user": {"id": USER, "name": "Ann"}, "subscriptions": [SUB_A]}).get_json()["job_id"]
    for _ in range(100):
        job = http.get(f"/api/checks/{job_id}").get_json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "done" and job["result"]["user"]["name"] == "Ann"
    again = http.post("/api/access/user", json={"user": {"id": USER}, "subscriptions": [SUB_A]}).get_json()
    assert again == {"job_id": job_id, "cached": True}


def test_my_access_here(monkeypatch):
    from healthconsole.core import pim

    me = "oid-1"
    rg = f"/subscriptions/{SUB_A}/resourcegroups/app-rg"
    cluster = f"{rg}/providers/microsoft.containerservice/managedclusters/shop"
    held_end = "2099-01-01T00:00:00+00:00"
    monkeypatch.setattr(pim, "roles", lambda client, refresh=False, standing=None: {
        "eligible": [{"scope": rg, "scope_id": rg, "scope_name": "app-rg", "scope_type": "resource group", "role": "Contributor",
                      "role_id": CONTRIBUTOR, "member_type": "Group", "end": None, "via": "ops"}],
        "held": [{"scope": rg, "role_id": READER, "end": held_end}], "error": None})
    monkeypatch.setattr(graph, "my_groups", lambda provider: [{"id": GROUP, "name": "ops"}])
    user_access._built_in.clear()

    class Client(FakeClient):
        def get(self, path, api_version, params=None, tolerate=(), timeout=None):
            if path.endswith("roleAssignments"):
                return {"value": [
                    {"properties": {"scope": rg, "principalId": me, "roleDefinitionId": f"/x/roleDefinitions/{READER}"}},
                    {"properties": {"scope": cluster, "principalId": GROUP, "roleDefinitionId": f"/x/roleDefinitions/{CONTRIBUTOR}"}},
                    {"properties": {"scope": f"{cluster}/agentpools/system", "principalId": me,
                                    "roleDefinitionId": f"/x/roleDefinitions/{CONTRIBUTOR}"}}]}
            if path.endswith("roleDefinitions"):
                return {"value": [{"name": READER, "properties": {"roleName": "Reader"}},
                                  {"name": CONTRIBUTOR, "properties": {"roleName": "Contributor"}}]}
            return None

    out = user_access.my_access(Client(), FakeTokens(), cluster)
    roles = [(r["role"], r["from"], r["via"], bool(r["pim_until"])) for r in out["roles"]]
    assert roles == [("Reader", "the resource group", "", True), ("Contributor", "here", "ops", False)]  # PIM first
    # Contributor is eligible at the resource group and not activated, so it's offered (it covers more than the
    # cluster-level Contributor held through the group); the role on a node pool below isn't listed as applying here.
    assert [r["role"] for r in out["eligible"]] == ["Contributor"] and len(out["roles"]) == 2
    assert out["roles"][0]["pim_until"] == held_end


def test_single_cluster_check_only_checks_that_cluster(monkeypatch):
    from healthconsole.core import checks
    from healthconsole.web import create_blueprint

    seen = {}

    def fleet(client, subscriptions, groups=None, with_detectors=False, progress=None, only_id=None):
        seen.update(subscriptions=subscriptions, groups=groups, only_id=only_id, detectors=with_detectors)
        return {"clusters": 1, "rows": [], "counts": {}}

    monkeypatch.setattr(checks, "aks_fleet", fleet)
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent")))
    http = app.test_client()
    cluster = f"/subscriptions/{SUB_A}/resourceGroups/app-rg/providers/Microsoft.ContainerService/managedClusters/shop"
    job = http.post("/api/checks", json={"check": "aks", "id": cluster, "detectors": True}).get_json()["job_id"]
    for _ in range(100):
        if http.get(f"/api/checks/{job}").get_json()["status"] != "running":
            break
        time.sleep(0.02)
    assert seen == {"subscriptions": [SUB_A], "groups": [f"{SUB_A}/app-rg"], "only_id": cluster, "detectors": True}
    assert http.post("/api/checks", json={"check": "aks", "id": "/nonsense"}).status_code == 400


@pytest.mark.parametrize("permissions, expected", [
    ([{"actions": ["*"], "notActions": []}], {"read": "all", "change": "all", "manage_access": True}),
    ([{"actions": ["*"], "notActions": ["Microsoft.Authorization/*/Write", "Microsoft.Authorization/*/Delete"]}],
     {"read": "all", "change": "all", "manage_access": False}),
    ([{"actions": ["*/read"], "notActions": []}], {"read": "all", "change": "", "manage_access": False}),
    ([{"actions": ["*/read", "Microsoft.Insights/alertRules/*"], "notActions": []}], {"read": "all", "change": "some", "manage_access": False}),
    ([{"actions": ["Microsoft.Authorization/roleAssignments/write", "*/read"], "notActions": []}],
     {"read": "all", "change": "", "manage_access": True}),
    ([], {"read": "", "change": "", "manage_access": False}),
])
def test_what_a_role_lets_you_do(permissions, expected):
    assert user_access.capabilities(permissions) == expected


def test_roles_add_up_and_an_eligible_role_says_what_it_adds():
    perms = {"reader": [{"actions": ["*/read"]}], "contrib": [{"actions": ["*"], "notActions": ["Microsoft.Authorization/*/Write"]}]}
    held = [{"role_id": "reader", "role": "Reader"}]
    can = user_access.combined(held, perms)
    assert can["read"] == "all" and can["change"] == "" and can["by"]["read"] == ["Reader"] and can["known"]
    assert user_access._adds(can, user_access.capabilities(perms["contrib"])) == ["change"]
    assert not user_access.combined([{"role_id": "custom", "role": "Custom"}], perms)["known"]


def test_standing_reads_your_own_roles_through_groups_in_batches(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(graph, "my_groups", lambda provider: [{"id": GROUP, "name": "ops"}])
    monkeypatch.setattr(user_access, "PRINCIPAL_BATCH", 1)
    client = AccessClient()
    client.identity = type("Identity", (), {"object_id": USER, "tenant_id": "t"})()
    rows = user_access.standing(client, FakeTokens())()
    # you and your one group: one Resource Graph query each, plus one read for the parent management group
    assert sum("principalId in" in q for q in client.queries) == 2
    assert {(r["scope"], r["role_id"], r["via"]) for r in rows} >= {
        (f"/subscriptions/{SUB_B}", "custom-1", "ops"), (MG, READER, "ops")}
