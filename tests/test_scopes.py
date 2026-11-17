from healthconsole.core import scopes, user_access
from tests.conftest import FakeClient

SUB = "/subscriptions/S1"
RG = f"{SUB}/resourceGroups/App-RG"
APP = f"{RG}/providers/Microsoft.Web/sites/shop"
MG = "/providers/Microsoft.Management/managementGroups/platform"


def test_covers_parents_not_children_or_siblings():
    assert scopes.covers(SUB, APP) == "yes" and scopes.covers(RG.lower() + "/", APP) == "yes"  # case and slash ignored
    assert scopes.covers("/", APP) == "yes"
    assert scopes.covers(APP, RG) == ""  # a role on the app doesn't cover its resource group
    # a role on app-rg must not reach app-rg2, whose ID starts with the same characters
    assert scopes.covers(RG, f"{SUB}/resourceGroups/App-RG2/providers/Microsoft.Web/sites/other") == ""
    assert scopes.covers(MG, APP) == "maybe"


def test_kind_label_and_relation():
    assert [scopes.kind(s) for s in ("/", MG, SUB, RG, APP)] == [
        "tenant root", "management group", "subscription", "resource group", "resource"]
    assert scopes.label(APP, {"s1": "sub-a"}) == "sub-a / app-rg / shop"
    assert scopes.label(MG) == "Management group platform" and scopes.label("/") == "Tenant root (/)"
    assert [scopes.relation(s, APP) for s in (APP, RG, SUB, MG, "/")] == [
        "here", "the resource group", "the subscription", "management group platform", "tenant root (/)"]


def test_role_names_asks_resource_graph_only_for_custom_roles():
    queries = []

    def custom(kql):
        queries.append(kql)
        return [{"roleGuid": "c-1", "role": "Platform Ops"}]

    client = FakeClient(query_handlers=[("roledefinitions", custom)])
    names = user_access.role_names(client, {"b-1", "c-1", ""}, known={"b-1": "Reader"})
    assert names == {"b-1": "Reader", "c-1": "Platform Ops"}
    assert len(queries) == 1 and '"c-1"' in queries[0] and '"b-1"' not in queries[0]
    assert user_access.role_names(client, {"b-1"}, known={"b-1": "Reader"}) == {"b-1": "Reader"} and len(queries) == 1
