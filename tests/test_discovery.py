from healthconsole.core.discovery import DiscoveryOptions, discover
from tests.conftest import SUB, FakeClient, rid

APP = rid("app-rg", "Microsoft.Web/sites", "shop")
APP2 = rid("app-rg", "Microsoft.Web/sites", "shop-2")
PLAN = rid("app-rg", "Microsoft.Web/serverfarms", "plan")
VNET = rid("net-rg", "Microsoft.Network/virtualNetworks", "vnet")
SUBNET = f"{VNET}/subnets/apps"
UAMI = rid("app-rg", "Microsoft.ManagedIdentity/userAssignedIdentities", "shop-id")
PE = rid("app-rg", "Microsoft.Network/privateEndpoints", "shop-pe")
ALERT = rid("app-rg", "Microsoft.Insights/metricAlerts", "shop-5xx")
HIDDEN_VAULT = rid("sec-rg", "Microsoft.KeyVault/vaults", "locked")


def row(resource_id, resource_type, properties=None, identity=None):
    return {"id": resource_id, "name": resource_id.rsplit("/", 1)[-1], "type": resource_type.lower(),
            "subscriptionId": SUB, "resourceGroup": resource_id.split("/")[4], "location": "canadacentral",
            "properties": properties or {}, "identity": identity}


def estate():
    return [
        row(APP, "Microsoft.Web/sites",
            {"serverFarmId": PLAN, "virtualNetworkSubnetId": SUBNET,
             "keyVaultReferenceIdentity": HIDDEN_VAULT},
            identity={"type": "UserAssigned", "userAssignedIdentities": {UAMI: {}}}),
        row(APP2, "Microsoft.Web/sites", {"serverFarmId": PLAN}),
        row(PLAN, "Microsoft.Web/serverfarms"),
        row(VNET, "Microsoft.Network/virtualNetworks"),
        row(UAMI, "Microsoft.ManagedIdentity/userAssignedIdentities"),
        row(PE, "Microsoft.Network/privateEndpoints",
            {"privateLinkServiceConnections": [{"properties": {"privateLinkServiceId": APP}}]}),
        row(ALERT, "Microsoft.Insights/metricAlerts", {"scopes": [APP]}),
    ]


def test_follows_references_and_resolves_nested_ids():
    graph = discover(FakeClient(estate()), APP, DiscoveryOptions(depth=1, inbound=False))
    ids = set(graph.nodes)
    assert {APP.lower(), PLAN.lower(), VNET.lower(), UAMI.lower()} <= ids
    labels = {(e.source, e.target): e.label for e in graph.edges}
    assert labels[(APP.lower(), PLAN.lower())] == "runs on"
    assert labels[(APP.lower(), VNET.lower())] == "in network"
    assert labels[(APP.lower(), UAMI.lower())] == "authenticates as"


def test_unreadable_reference_becomes_grey_node():
    graph = discover(FakeClient(estate()), APP, DiscoveryOptions(depth=1, inbound=False))
    vault = graph.nodes[HIDDEN_VAULT.lower()]
    assert not vault.readable
    assert vault.display_type == "Key Vault"


def test_inbound_finds_private_endpoint_but_skips_noise_and_substring_matches():
    graph = discover(FakeClient(estate()), APP, DiscoveryOptions(depth=1))
    assert graph.nodes[PE.lower()].direction == "inbound"
    assert ALERT.lower() not in graph.nodes
    assert APP2.lower() not in graph.nodes


def test_depth_zero_is_just_the_resource():
    graph = discover(FakeClient(estate()), APP, DiscoveryOptions(depth=0))
    assert list(graph.nodes) == [APP.lower()]


def test_max_nodes_stops_with_a_warning():
    graph = discover(FakeClient(estate()), APP, DiscoveryOptions(depth=2, max_nodes=2, inbound=False))
    assert len(graph.nodes) == 2
    assert any("Stopped at 2" in warning for warning in graph.warnings)


def test_unreadable_root():
    graph = discover(FakeClient([]), APP, DiscoveryOptions())
    assert not graph.nodes[APP.lower()].readable
    assert graph.warnings


def test_leaf_types_are_not_expanded():
    other_nic = rid("other-rg", "Microsoft.Network/networkInterfaces", "someone-elses-nic")
    resources = estate()
    for r in resources:
        if r["id"] == VNET:
            r["properties"] = {"subnets": [{"properties": {"ipConfigurations": [{"id": f"{other_nic}/ipConfigurations/c"}]}}]}
    resources.append(row(other_nic, "Microsoft.Network/networkInterfaces"))
    graph = discover(FakeClient(resources), APP, DiscoveryOptions(depth=2, inbound=False))
    assert VNET.lower() in graph.nodes
    assert other_nic.lower() not in graph.nodes
