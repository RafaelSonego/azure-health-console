from flask import Flask

from healthconsole.core import config, graph, requirements, user_access, vulnerabilities
from tests.conftest import FakeClient, FakeTokens

SUB_A, SUB_B = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
READER, AKS_USER, AKS_READER, CUSTOM = "r1", "a1", "a2", "c1"
PERMISSIONS = {
    READER: [{"actions": ["*/read"]}],
    AKS_USER: [{"actions": ["Microsoft.ContainerService/managedClusters/listClusterUserCredential/action",
                            "Microsoft.ContainerService/managedClusters/read"]}],
    AKS_READER: [{"actions": ["Microsoft.Resources/subscriptions/read"],
                  "dataActions": ["Microsoft.ContainerService/managedClusters/*/read"],
                  "notDataActions": ["Microsoft.ContainerService/managedClusters/secrets/*"]}],
    CUSTOM: [{"actions": ["*/read", "Microsoft.ContainerService/managedClusters/listClusterUserCredential/action"],
              "dataActions": ["Microsoft.ContainerService/managedClusters/*/read"]}],
}
CLUSTER_A = f"/subscriptions/{SUB_A}/resourcegroups/aks/providers/microsoft.containerservice/managedclusters/one"
CLUSTER_B = f"/subscriptions/{SUB_B}/resourcegroups/aks/providers/microsoft.containerservice/managedclusters/two"


def client():
    return FakeClient(query_handlers=[("managedclusters", lambda kql: [
        {"id": CLUSTER_A, "type": "microsoft.containerservice/managedclusters", "insights": True},
        {"id": CLUSTER_B, "type": "microsoft.containerservice/managedclusters", "insights": False}])])


def setup(monkeypatch):
    monkeypatch.setattr(requirements, "role_permissions", lambda c, guids: {g: PERMISSIONS[g] for g in guids if g in PERMISSIONS})
    monkeypatch.setattr(graph, "my_groups", lambda provider: [{"id": "g", "name": "ops"}])
    monkeypatch.setattr(graph, "group_users", lambda provider, group_id: [])
    monkeypatch.setattr(vulnerabilities, "coverage", lambda c, subs: {"plans_off": [{"label": "Defender for Containers", "subscriptions": [SUB_B]}]})


def test_needs_are_checked_by_what_roles_allow(monkeypatch):
    setup(monkeypatch)
    found = {"standing": [{"scope": f"/subscriptions/{SUB_A}", "role_id": READER, "role": "Reader", "via": ""},
                          {"scope": CLUSTER_A, "role_id": CUSTOM, "role": "Ops custom", "via": "ops"}],
             "eligible": [{"scope": f"/subscriptions/{SUB_B}", "role_id": READER, "role": "Reader"},
                          {"scope": CLUSTER_B, "role_id": AKS_USER, "role": "Azure Kubernetes Service Cluster User Role"}]}
    rows = {r["key"]: r for r in requirements.check(client(), FakeTokens(), [SUB_A, SUB_B], found)["rows"]}
    read = rows["read"]
    assert (read["have"], read["eligible"], read["total"], read["status"]) == (1, 1, 2, "warning") and read["activate"] == ["Reader"]
    aks = rows["aks"]  # a custom role with both pieces counts on cluster one; cluster two lacks the Kubernetes read
    assert (aks["have"], aks["eligible"], aks["total"]) == (1, 0, 2) and aks["missing"] == [CLUSTER_B]
    assert rows["graph"]["status"] == "healthy"
    acr = {"registry_pull": True}
    assert not requirements._allowed([{"actions": ["*/read"]}], acr)  # Reader: refused by the registry
    assert requirements._allowed([{"actions": ["Microsoft.ContainerRegistry/registries/pull/read"]}], acr)  # AcrPull
    assert requirements._allowed([{"actions": ["*"]}], acr)  # Owner, Contributor
    assert requirements._allowed([{"dataActions": ["Microsoft.ContainerRegistry/registries/repositories/content/read"]}], acr)
    assert rows["defender"]["have"] == 1 and "off in 1" in rows["defender"]["detail"]
    assert rows["insights"]["have"] == 1 and rows["insights"]["total"] == 2


def test_every_requirement_is_in_the_readme_permissions_table():
    text = open("README.md").read()
    table = text[text.index("## Permissions"):text.index("## Configuration reference")]
    for req in requirements.REQUIREMENTS:
        assert f"| {req['feature']} |" in table, req["feature"]


def test_custom_role_download_reads_only():
    from healthconsole.web import create_blueprint
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"), authorize=lambda: None))
    role = app.test_client().get("/api/access/custom-role").get_json()
    assert role["Actions"][0] == "*/read" and all(a.endswith("/read") or a.endswith("listClusterUserCredential/action")
                                                  for a in role["Actions"])
    assert all(a.endswith("/read") for a in role["DataActions"]) and role["NotDataActions"]
