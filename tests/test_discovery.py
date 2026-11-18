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


def group_client(rows, assignments=(), fail_roles=False):
    from healthconsole.core.arm import ArmError

    def roles(kql):
        if fail_roles:
            raise ArmError("authorizationresources refused")
        return list(assignments)
    return FakeClient(rows, query_handlers=[("roleassignments", roles), ("resourceGroup =~", lambda kql: list(rows))])


def test_a_group_check_keeps_every_member_whatever_max_nodes_says():
    from healthconsole.core.discovery import discover_group
    rows = [row(rid("big-rg", "Microsoft.Storage/storageAccounts", f"st{i:02}"), "Microsoft.Storage/storageAccounts")
            for i in range(5)]
    graph = discover_group(group_client(rows), SUB, "big-rg", DiscoveryOptions(max_nodes=2))
    assert len(graph.nodes) == 5 and not any("Stopped" in w for w in graph.warnings)


AKS = rid("k8s-rg", "Microsoft.ContainerService/managedClusters", "aks")
ACR = rid("k8s-rg", "Microsoft.ContainerRegistry/registries", "acr")
KUBELET = rid("k8s-rg", "Microsoft.ManagedIdentity/userAssignedIdentities", "aks-id")
VAULT = rid("k8s-rg", "Microsoft.KeyVault/vaults", "kv")
ACR_PULL = "7f951dda-4ed3-4680-a7ca-43fe172d538d"


def k8s_rows():
    return [row(AKS, "Microsoft.ContainerService/managedClusters",
                {"identityProfile": {"kubeletidentity": {"objectId": "P-KUBELET", "resourceId": KUBELET}}},
                identity={"type": "UserAssigned", "userAssignedIdentities": {KUBELET: {}}}),
            row(KUBELET, "Microsoft.ManagedIdentity/userAssignedIdentities", {"principalId": "p-kubelet"}),
            row(ACR, "Microsoft.ContainerRegistry/registries"),
            row(VAULT, "Microsoft.KeyVault/vaults", identity={"principalId": "p-vault"})]


def test_role_assignments_draw_who_can_reach_what(monkeypatch):
    from healthconsole.core import user_access
    from healthconsole.core.discovery import discover_group
    monkeypatch.setattr(user_access, "role_names", lambda client, guids, known=None: {ACR_PULL: "AcrPull", "r2": "Reader"})
    assignments = [{"principalId": "p-kubelet", "scope": ACR.lower(), "roleGuid": ACR_PULL},
                   {"principalId": "p-kubelet", "scope": ACR.lower(), "roleGuid": "r2"},
                   {"principalId": "p-vault", "scope": f"/subscriptions/{SUB}/resourcegroups/k8s-rg", "roleGuid": "r2"}]
    client = group_client(k8s_rows(), assignments)
    graph = discover_group(client, SUB, "k8s-rg", DiscoveryOptions())
    roles = [(e.source, e.target, e.label) for e in graph.edges if e.kind == "role"]
    # the kubelet identity is the identity resource, not the cluster; a role on the whole group isn't drawn
    assert roles == [(KUBELET.lower(), ACR.lower(), "AcrPull, Reader")]
    assert any(e.kind == "" and e.source == AKS.lower() and e.target == KUBELET.lower() for e in graph.edges)
    sent = next(q for q in client.queries if "roleassignments" in q)
    assert '"p-kubelet"' in sent and '"p-vault"' in sent


def test_unreadable_role_assignments_are_a_note_not_a_failed_check():
    from healthconsole.core.discovery import discover_group
    graph = discover_group(group_client(k8s_rows(), fail_roles=True), SUB, "k8s-rg", DiscoveryOptions())
    assert len(graph.nodes) == 4 and any("Role assignments not read" in w for w in graph.warnings)


def test_an_aks_clusters_node_pools_are_part_of_it():
    from healthconsole.core.discovery import discover_group
    pool = rid("k8s-rg", "Microsoft.Compute/virtualMachineScaleSets", "aks-pool-vmss")
    rows = [row(AKS, "Microsoft.ContainerService/managedClusters", {"nodeResourceGroup": "k8s-rg"}),
            row(pool, "Microsoft.Compute/virtualMachineScaleSets")]
    client = FakeClient(rows, query_handlers=[("virtualmachinescalesets", lambda kql: [rows[1]]),
                                              ("roleassignments", lambda kql: []), ("resourceGroup =~", lambda kql: list(rows))])
    graph = discover_group(client, SUB, "k8s-rg", DiscoveryOptions())
    assert [(e.label, e.kind) for e in graph.edges if e.target == pool.lower()] == [("node pool", "part")]


def test_roles_on_a_whole_group_go_on_the_holder(monkeypatch):
    from healthconsole.core import user_access
    from healthconsole.core.discovery import discover_group
    monkeypatch.setattr(user_access, "role_names", lambda client, guids, known=None: {"r2": "Reader"})
    group = f"/subscriptions/{SUB}/resourcegroups/k8s-rg"
    graph = discover_group(group_client(k8s_rows(), [{"principalId": "p-vault", "scope": group, "roleGuid": "r2"}]),
                           SUB, "k8s-rg", DiscoveryOptions())
    assert graph.nodes[VAULT.lower()].roles == [{"role": "Reader", "scope": group, "scope_kind": "group", "scope_name": "k8s-rg"}]
    assert not [e for e in graph.edges if e.kind == "role"]


def test_one_resources_picture_adds_what_its_identity_holds_a_role_on(monkeypatch):
    from healthconsole.core import user_access
    monkeypatch.setattr(user_access, "role_names", lambda client, guids, known=None: {ACR_PULL: "AcrPull"})
    client = FakeClient(k8s_rows(), query_handlers=[("roleassignments", lambda kql: [
        {"principalId": "p-kubelet", "scope": f"{ACR.lower()}/repositories/web", "roleGuid": ACR_PULL}])])
    graph = discover(client, AKS, DiscoveryOptions(depth=1, inbound=False))
    assert ACR.lower() in graph.nodes  # not referenced by the cluster: added because its identity can pull from it
    assert [(e.source, e.target, e.label) for e in graph.edges if e.kind == "role"] == [(KUBELET.lower(), ACR.lower(), "AcrPull")]


def test_nested_resource_is_part_of_its_parent_without_managed_by():
    from healthconsole.core import discovery
    server = rid("rg", "Microsoft.Sql/servers", "srv")
    db = {"id": f"{server}/databases/app", "type": "microsoft.sql/servers/databases", "managedBy": "", "properties": {}}
    assert discovery._referenced_ids(db) == {server.lower()}
    assert discovery._referenced_ids({"id": server, "properties": {}}) == set()  # top level: no parent
