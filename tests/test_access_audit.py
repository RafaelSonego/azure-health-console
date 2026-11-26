import pytest

from healthconsole.core import access_audit, config, graph, user_access
from healthconsole.core.arm import ArmError
from tests.conftest import FakeClient, FakeTokens

SUB_A, SUB_B = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
MG = "/providers/Microsoft.Management/managementGroups/platform"
ANN, BOB, CID, DEE, EVE, FAY, GUS, HAL = (f"aaaaaaaa-0000-0000-0000-00000000000{i}" for i in range(1, 9))
OPS, APP = "bbbbbbbb-0000-0000-0000-000000000001", "cccccccc-0000-0000-0000-000000000001"
OWNER, CONTRIBUTOR, READER, SUPPORT, SECRETS, AKS_USER = (f"dddddddd-0000-0000-0000-00000000000{i}" for i in range(1, 7))
CUSTOM = "eeeeeeee-0000-0000-0000-000000000001"
ROLES = {
    OWNER: ("Owner", [{"actions": ["*"]}]),
    CONTRIBUTOR: ("Contributor", [{"actions": ["*"], "notActions": ["Microsoft.Authorization/*/Write",
                                                                    "Microsoft.Authorization/*/Delete"]}]),
    READER: ("Reader", [{"actions": ["*/read"]}]),
    SUPPORT: ("Support Request Contributor", [{"actions": ["Microsoft.Authorization/*/read", "Microsoft.Support/*"]}]),
    SECRETS: ("Key Vault Secrets User", [{"actions": [], "dataActions": [
        "Microsoft.KeyVault/vaults/secrets/getSecret/action", "Microsoft.KeyVault/vaults/secrets/readMetadata/action"]}]),
    AKS_USER: ("Azure Kubernetes Service Cluster User Role", [{"actions": [
        "Microsoft.ContainerService/managedClusters/listClusterUserCredential/action",
        "Microsoft.ContainerService/managedClusters/read"]}]),
}
OLD_CLUSTER = f"/subscriptions/{SUB_A}/resourcegroups/aks-rg/providers/microsoft.containerservice/managedclusters/old"


def instance(principal, kind, role, scope, assignment="Assigned", end=None, name=""):
    return {"properties": {
        "principalId": principal, "principalType": kind, "scope": scope, "assignmentType": assignment,
        "roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{role}", "endDateTime": end,
        "expandedProperties": {"principal": {"type": kind, "displayName": name or principal[-1]},
                               "roleDefinition": {"displayName": ROLES.get(role, ("Custom",))[0]},
                               "scope": {"displayName": scope.rsplit("/", 1)[-1]}}}}


ASSIGNED = {
    SUB_A: [instance(ANN, "User", OWNER, f"/subscriptions/{SUB_A}", name="Ann"),
            instance(OPS, "Group", CONTRIBUTOR, f"/subscriptions/{SUB_A}/resourceGroups/app-rg", name="ops"),
            instance(OPS, "Group", CONTRIBUTOR, MG, name="ops"),  # inherited: repeats under every subscription
            instance(APP, "ServicePrincipal", OWNER, f"/subscriptions/{SUB_A}"),
            instance(CID, "User", OWNER, f"/subscriptions/{SUB_A}", assignment="Activated"),
            instance(DEE, "User", READER, f"/subscriptions/{SUB_A}"),
            instance(EVE, "User", SUPPORT, f"/subscriptions/{SUB_A}"),
            instance(FAY, "User", CONTRIBUTOR, f"/subscriptions/{SUB_A}", end="2026-12-01T00:00:00Z", name="Fay"),
            instance(FAY, "User", CUSTOM, f"/subscriptions/{SUB_A}/resourceGroups/other-rg", name="Fay"),
            instance(GUS, "User", AKS_USER, f"/subscriptions/{SUB_A}/resourceGroups/aks-rg", name="Gus")],
    SUB_B: [instance(OPS, "Group", CONTRIBUTOR, MG, name="ops"),
            instance(ANN, "User", SECRETS, f"/subscriptions/{SUB_B}/resourceGroups/kv-rg/providers/Microsoft.KeyVault/vaults/kv", name="Ann"),
            instance(HAL, "User", AKS_USER, f"/subscriptions/{SUB_B}", name="Hal")],  # only clusters with Entra ID there
}
ELIGIBLE = {SUB_A: [instance(ANN, "User", OWNER, MG, name="Ann")], SUB_B: []}
PEOPLE = {ANN: "Ann", BOB: "Bob", FAY: "Fay", GUS: "Gus", HAL: "Hal"}
CONDITION = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[x] ForAnyOfAnyValues:GuidEquals {abc}))"


class AuditClient(FakeClient):
    def __init__(self):
        super().__init__(query_handlers=[
            ("aadProfile", lambda kql: [{"id": OLD_CLUSTER, "name": "old"}]),
            ("properties.condition", lambda kql: [{"scope": f"/subscriptions/{SUB_A}", "principal": ANN, "roleGuid": OWNER,
                                                   "condition": CONDITION}])])

    def get(self, path, api_version, params=None, tolerate=(), timeout=None):
        sub = path.split("/")[2] if path.startswith("/subscriptions/") else ""
        if path.endswith("roleAssignmentScheduleInstances"):
            return {"value": ASSIGNED[sub]}
        if path.endswith("roleEligibilityScheduleInstances"):
            return {"value": ELIGIBLE[sub]}
        if path.endswith("roleDefinitions"):
            return {"value": [{"name": g, "properties": {"roleName": n, "permissions": p}} for g, (n, p) in ROLES.items()]}
        if path == "/subscriptions":
            return {"value": [{"subscriptionId": SUB_A, "displayName": "sub-a"}, {"subscriptionId": SUB_B, "displayName": "sub-b"}]}
        return None


def user(user_id, guest=False):
    return {"id": user_id, "name": PEOPLE[user_id], "upn": f"{PEOPLE[user_id].lower()}@example.com", "mail": "",
            "enabled": True, "guest": guest}


@pytest.fixture
def run(monkeypatch):
    user_access._built_in.clear()
    monkeypatch.setattr(graph, "group_users", lambda provider, group_id: [user(BOB, guest=True), user(ANN)])
    monkeypatch.setattr(graph, "user", lambda provider, user_id: user(user_id))
    monkeypatch.setattr(graph, "group", lambda provider, group_id: {"id": group_id, "name": "ops"})
    return lambda **kw: access_audit.audit(AuditClient(), FakeTokens(), [SUB_A, SUB_B],
                                           ignore=config.DEFAULTS["audit"]["ignore_actions"], **kw)


def test_levels_come_from_what_a_role_allows_not_its_name():
    assert access_audit.level(ROLES[OWNER][1]) == "manage_access"
    assert access_audit.level(ROLES[CONTRIBUTOR][1]) == "change_all"
    assert access_audit.level([{"actions": ["Microsoft.Web/sites/restart/action", "*/read"]}]) == "change_some"
    assert access_audit.level(ROLES[READER][1]) == ""
    assert access_audit.level(ROLES[SECRETS][1]) == "credentials"  # getSecret hands over the secret's value
    assert access_audit.level([{"dataActions": ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/*"]}]) == "data_write"
    assert access_audit.level([{"actions": ["Microsoft.HybridConnectivity/endpoints/listCredentials/action"]}]) == "credentials"
    assert access_audit.level([{"actions": ["Microsoft.Storage/storageAccounts/listkeys/action", "*/read"]}]) == "credentials"
    assert access_audit.level([{"actions": ["Microsoft.DocumentDB/databaseAccounts/readonlykeys/action"]}]) == "credentials"
    kv_admin = [{"actions": ["Microsoft.Resources/deployments/*"], "dataActions": ["Microsoft.KeyVault/vaults/*"]}]
    assert access_audit.level(kv_admin) == "credentials"  # its wildcard includes getSecret
    assert access_audit.level(ROLES[AKS_USER][1]) == "credentials"
    assert access_audit.level(ROLES[SUPPORT][1]) == "change_some"  # support requests count unless ignored
    assert access_audit.level(ROLES[SUPPORT][1], ignore=["Microsoft.Support/*"]) == ""


@pytest.mark.parametrize("permissions, expected", [
    # reads Azure names as actions don't count, by their verb (Azure's own vocabulary)
    ([{"actions": ["*/read", "Microsoft.NetApp/locations/checknameavailability/action",
                   "Microsoft.Network/applicationGateways/backendhealth/action",
                   "Microsoft.Network/vpnGateways/getbgppeerstatus/action", "Microsoft.Resources/deployments/exportTemplate/action",
                   "Microsoft.RecoveryServices/Vaults/backupJobsExport/action", "Microsoft.CostManagement/reports/action",
                   "Microsoft.Cdn/profiles/Usages/action", "Microsoft.AzureSphere/catalogs/certificates/retrieveCertChain/action",
                   "Microsoft.AzureStackHCI/EdgeMachines/Read/*"]}], ""),
    # reader roles that show no data: Key Vault Reader (metadata), Log Analytics Reader (logs), AKS RBAC Reader
    ([{"actions": ["Microsoft.Insights/alertRules/*", "Microsoft.Support/*"],
       "dataActions": ["Microsoft.KeyVault/vaults/*/read", "Microsoft.KeyVault/vaults/secrets/readMetadata/action"]}], ""),
    ([{"actions": ["*/read", "Microsoft.OperationalInsights/workspaces/search/action"],
       "dataActions": ["Microsoft.OperationalInsights/workspaces/tables/data/read",
                       "Microsoft.OperationalInsights/workspaces/jobs/export/action"]}], ""),
    ([{"dataActions": ["Microsoft.ContainerService/managedClusters/apps/deployments/read"]}], ""),
    ([{"actions": ["Microsoft.Resources/deployments/*", "Microsoft.Cdn/operationresults/*"]}], ""),  # harmless writes
    # seeing data counts, above changing some things
    ([{"actions": ["Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action"],
       "dataActions": ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"]}], "data_read"),
    ([{"actions": ["Microsoft.Web/sites/restart/action"],
       "dataActions": ["Microsoft.Storage/storageAccounts/queueServices/queues/messages/read"]}], "data_read"),
    ([{"actions": ["Microsoft.Storage/storageAccounts/listKeys/action", "Microsoft.Storage/storageAccounts/ListAccountSas/action"]}],
     "credentials"),  # Reader and Data Access: a key is full access to the data
    ([{"actions": ["Microsoft.Compute/disks/beginGetAccess/action"]}], "credentials"),
    ([{"dataActions": ["Microsoft.HybridCompute/machines/WACLoginAsAdmin/action"]}], "credentials"),  # admin sign-in
    ([{"actions": ["Microsoft.Compute/virtualMachines/start/action", "Microsoft.Compute/virtualMachines/deallocate/action"]}],
     "change_some"),
])
def test_reader_roles_count_only_when_they_change_something_or_see_data(permissions, expected):
    assert access_audit.level(permissions, config.DEFAULTS["audit"]["ignore_actions"]) == expected


def test_audit_lists_people_who_can_do_more_than_read_without_pim(run):
    result = run()
    people = {p["name"]: p for p in result["people"]}
    assert set(people) == {"Ann", "Bob", "Fay", "Gus"}  # not the app, the PIM activation, the reader or the support role
    assert result["people"][0]["name"] == "Ann" and people["Ann"]["level"] == "manage_access"
    ann = [(i["role"], i["via"], i["eligible_too"]) for i in people["Ann"]["items"]]
    assert ("Owner", "", True) in ann  # standing Owner while also eligible for it through PIM
    assert ("Contributor", "ops", False) in ann and ("Key Vault Secrets User", "", False) in ann
    assert sum(1 for i in people["Ann"]["items"] if i["scope"] == MG) == 1  # the inherited row once, not per subscription
    assert people["Bob"]["guest"] and {i["via"] for i in people["Bob"]["items"]} == {"ops"}
    fay = {i["role"]: i for i in people["Fay"]["items"]}
    assert fay["Contributor"]["until"] == "2026-12-01T00:00:00Z" and fay["Custom"]["level"] == "unknown"
    gus = people["Gus"]["items"][0]  # the AKS user credential counts where a cluster has no Entra ID; Hal's has it
    assert gus["level"] == "credentials" and "1 cluster(s) without Entra ID: old" in gus["note"]
    owner = next(i for i in people["Ann"]["items"] if i["role"] == "Owner")
    assert owner["condition"] == CONDITION and all(not i["condition"] for i in people["Bob"]["items"])
    assert result["summary"]["people"] == 4 and result["summary"]["levels"]["manage_access"] == 1
    secrets = next(i for i in people["Ann"]["items"] if i["role"] == "Key Vault Secrets User")
    assert (secrets["resource_type"], secrets["type_label"]) == ("microsoft.keyvault/vaults", "Key Vault")
    assert {i["type_label"] for i in people["Ann"]["items"] if i["role"] == "Owner"} == {"Subscription"}
    assert result["summary"]["eligible_too"] == 1 and result["summary"]["guests"] == 1


def test_resource_type_of_a_scope():
    child = f"/subscriptions/{SUB_A}/resourceGroups/r/providers/Microsoft.Sql/servers/s/databases/d"
    assert access_audit.resource_type(child)[0] == "microsoft.sql/servers/databases"
    container = f"/subscriptions/{SUB_A}/resourceGroups/r/providers/Microsoft.Storage/storageAccounts/s/blobServices/default/containers/c"
    assert access_audit.resource_type(container) == ("microsoft.storage/storageaccounts/blobservices/containers",
                                                     "Storage Account › containers")
    assert access_audit.resource_type(f"/subscriptions/{SUB_A}/resourceGroups/r") == ("", "Resource group")
    assert access_audit.resource_type(MG)[1] == "Management group"


def test_narrowed_to_resource_groups_keeps_roles_above_them(run):
    people = {p["name"]: p for p in run(groups=[f"{SUB_A}/other-rg"])["people"]}
    assert all("app-rg" not in i["scope"] for i in people["Ann"]["items"])
    assert any(i["scope"] == f"/subscriptions/{SUB_A}" for i in people["Ann"]["items"])
    assert any(i["scope"].endswith("other-rg") for i in people["Fay"]["items"])


def test_ignore_actions_must_be_a_list_of_actions():
    assert config.from_data({"audit": {"ignore_actions": ["Microsoft.Support/*", "Microsoft.Resources/tags/*"]}})["audit"]["ignore_actions"][1] == "Microsoft.Resources/tags/*"
    with pytest.raises(config.ConfigError):
        config.from_data({"audit": {"ignore_actions": "Microsoft.Support/*"}})


def test_audit_api_runs_as_a_job_and_exports(run, monkeypatch, tmp_path):
    import time

    from flask import Flask

    from healthconsole.web import create_blueprint

    client = AuditClient()
    client.token_provider = FakeTokens()
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, {**config.load("/nonexistent"), "cache_dir": str(tmp_path)}, authorize=lambda: None))
    http = app.test_client()
    assert http.post("/api/security/access-audit", json={}).status_code == 400
    many = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(access_audit.MAX_SUBSCRIPTIONS + 1)]
    assert http.post("/api/security/access-audit", json={"subscriptions": many}).get_json()["too_many"] == 101
    job_id = http.post("/api/security/access-audit", json={"subscriptions": [SUB_A, SUB_B]}).get_json()["job_id"]
    for _ in range(100):
        job = http.get(f"/api/checks/{job_id}").get_json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "done" and job["result"]["summary"]["people"] == 4
    assert http.post("/api/security/access-audit", json={"subscriptions": [SUB_B, SUB_A]}).get_json() == {"job_id": job_id, "cached": True, "saved_at": None}
    csv = http.get(f"/api/security/access-audit/{job_id}/export?format=csv").get_data(as_text=True)
    assert "Manages access" in csv and "Owner" in csv and "via group" not in csv and "ops" in csv


SUB_C = "33333333-3333-3333-3333-333333333333"
IVY, JOE, KIM, GONE = (f"ffffffff-0000-0000-0000-00000000000{i}" for i in range(1, 5))
C_RG, C_RG2 = f"/subscriptions/{SUB_C}/resourcegroups/c-rg", f"/subscriptions/{SUB_C}/resourcegroups/c-rg2"


def graph_row(principal, kind, role, scope, sub):
    return {"principalId": principal, "principalType": kind, "roleGuid": role, "scope": scope, "subscriptionId": sub}


class GapClient(AuditClient):
    """SUB_C refuses the subscription-level read (no Reader there); c-rg is readable, c-rg2 isn't. Resource Graph also
    holds an assignment the instances API left out, and one to a user who was deleted."""

    def __init__(self):
        super().__init__()
        self.query_handlers.insert(0, ("principalType in ('User', 'Group')", lambda kql: [
            graph_row(ANN, "User", OWNER, f"/subscriptions/{SUB_A}", SUB_A),  # also an instance: not doubled
            graph_row(IVY, "User", CONTRIBUTOR, f"/subscriptions/{SUB_A}/resourceGroups/extra-rg", SUB_A),
            graph_row(GONE, "User", CONTRIBUTOR, f"/subscriptions/{SUB_A}/resourceGroups/old-rg", SUB_A),
            graph_row(OPS, "Group", CONTRIBUTOR, C_RG, SUB_C), graph_row(JOE, "User", OWNER, C_RG, SUB_C),
            graph_row(KIM, "User", OWNER, C_RG2, SUB_C)]))

    def get(self, path, api_version, params=None, tolerate=(), timeout=None):
        if path.startswith(C_RG2):
            raise ArmError("Azure returned 400: The requestor does not have permissions for this request.", 400)
        if path.startswith(C_RG + "/"):
            if path.endswith("roleAssignmentScheduleInstances"):
                return {"value": [instance(OPS, "Group", CONTRIBUTOR, C_RG, name="ops"),
                                  instance(JOE, "User", OWNER, C_RG, assignment="Activated", name="Joe")]}
            return {"value": []}
        if path.startswith(f"/subscriptions/{SUB_C}/providers"):
            raise ArmError("Azure returned 400: The requestor does not have permissions for this request.", 400)
        return super().get(path, api_version, params, tolerate, timeout)


def test_gaps_in_the_instances_read_are_filled_from_resource_graph(monkeypatch):
    user_access._built_in.clear()
    names = {**PEOPLE, IVY: "Ivy", JOE: "Joe", KIM: "Kim"}

    def one_user(provider, user_id):
        if user_id == GONE:
            raise ArmError("Microsoft Graph answered 404: Request_ResourceNotFound", 404)
        return {"id": user_id, "name": names[user_id], "upn": "", "mail": "", "enabled": True, "guest": False}
    monkeypatch.setattr(graph, "group_users", lambda provider, group_id: [one_user(provider, BOB)])
    monkeypatch.setattr(graph, "user", one_user)
    monkeypatch.setattr(graph, "group", lambda provider, group_id: {"id": group_id, "name": "ops"})
    result = access_audit.audit(GapClient(), FakeTokens(), [SUB_A, SUB_B, SUB_C], ignore=config.DEFAULTS["audit"]["ignore_actions"])
    people = {p["name"]: p for p in result["people"]}
    assert "Ivy" in people  # left out by the instances API, kept from Resource Graph
    assert "Joe" not in people  # the resource group's instances say it's a PIM activation
    kim = people["Kim"]["items"][0]
    assert kim["pim_unknown"] and kim["scope"].endswith("c-rg2")  # nobody could read that group's instances
    assert any(i["scope"] == C_RG and i["via"] == "ops" for i in people["Bob"]["items"])
    ann_owner = [i for i in people["Ann"]["items"] if i["role"] == "Owner" and i["scope"] == f"/subscriptions/{SUB_A}"]
    assert len(ann_owner) == 1
    assert [o["principal_id"] for o in result["orphaned"]] == [GONE] and "Gone" not in people
    assert result["summary"]["refused_subscriptions"] == 1 and result["summary"]["orphaned"] == 1
    assert any("resource group by resource group (1 of 2" in n for n in result["notes"])


VAULT = f"/subscriptions/{SUB_A}/resourceGroups/kv-rg/providers/Microsoft.KeyVault/vaults/old-kv"
POLICY_USER, POLICY_GROUP, POLICY_APP, POLICY_GONE = (f"99999999-0000-0000-0000-00000000000{i}" for i in range(1, 5))


def test_access_policy_levels():
    assert access_audit.policy_level(["Get", "List"], [], [], []) == "credentials"  # a secret's value
    assert access_audit.policy_level(["List"], ["Get", "List"], ["Get"], []) == ""  # metadata only
    assert access_audit.policy_level(["List", "Set"], [], [], []) == "data_write"
    assert access_audit.policy_level([], ["Decrypt", "Get"], [], []) == "data_read"  # uses a key without having it
    assert access_audit.policy_level([], ["Release"], [], []) == "credentials"
    assert access_audit.policy_level([], [], [], ["getsas"]) == "credentials"


def test_vault_access_policies_join_the_audit(run, monkeypatch):
    def entry(oid, secrets=(), keys=()):
        return {"id": VAULT, "subscriptionId": SUB_A, "objectId": oid, "secrets": list(secrets), "keys": list(keys),
                "certificates": [], "storage": []}
    entries = [entry(POLICY_USER, ["Get", "List"]), entry(POLICY_GROUP, ["Set"]), entry(POLICY_APP, ["Get"]),
               entry(POLICY_GONE, ["Get"]), entry(FAY, ["List"])]  # list only: metadata, not counted
    objects = {POLICY_USER: {"type": "user", "id": POLICY_USER, "name": "Pat", "upn": "pat@example.com", "guest": False, "enabled": True},
               POLICY_GROUP: {"type": "group", "id": POLICY_GROUP, "name": "kv-admins", "upn": "", "guest": False, "enabled": True},
               POLICY_APP: {"type": "servicePrincipal", "id": POLICY_APP, "name": "app", "upn": "", "guest": False, "enabled": True}}

    def look(provider, oid):
        if oid == POLICY_GONE:
            raise ArmError("Microsoft Graph answered 404", 404)
        return objects[oid]
    monkeypatch.setattr(graph, "directory_object", look)
    monkeypatch.setitem(PEOPLE, POLICY_USER, "Pat")
    original = AuditClient.__init__

    def with_policies(self):
        original(self)
        self.query_handlers.insert(0, ("accessPolicies", lambda kql: entries))
    monkeypatch.setattr(AuditClient, "__init__", with_policies)
    result = run()
    people = {p["name"]: p for p in result["people"]}
    pat = people["Pat"]["items"][0]
    assert pat["level"] == "credentials" and pat["role"] == "Key Vault access policy: secrets get, list"
    assert pat["type_label"] == "Key Vault" and not pat["until"] and not pat["eligible_too"]
    assert any(i["role"].startswith("Key Vault access policy") and i["via"] == "kv-admins" for i in people["Bob"]["items"])
    assert all(not i["role"].startswith("Key Vault access policy") for i in people["Fay"]["items"])
    assert any(o["principal_id"] == POLICY_GONE for o in result["orphaned"])
    assert POLICY_APP not in {p["id"] for p in result["people"]}
    assert any("still use access policies" in n for n in result["notes"])


def _wait(http, job_id):
    import time
    for _ in range(200):
        job = http.get(f"/api/checks/{job_id}").get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    return job


def _audit_app(tmp_path, keep_days=7):
    from flask import Flask

    from healthconsole.web import create_blueprint

    client = AuditClient()
    client.token_provider = FakeTokens()
    settings = config.load("/nonexistent")
    settings = {**settings, "cache_dir": str(tmp_path), "audit": {**settings["audit"], "keep_days": keep_days}}
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, settings, authorize=lambda: None))
    return app.test_client()


def test_a_finished_audit_is_saved_and_opens_from_disk_after_a_restart(run, monkeypatch, tmp_path):
    calls = []
    real = access_audit.audit
    monkeypatch.setattr(access_audit, "audit", lambda *a, **k: calls.append(1) or real(*a, **k))
    http = _audit_app(tmp_path)
    first = http.post("/api/security/access-audit", json={"subscriptions": [SUB_A, SUB_B]}).get_json()
    assert "saved_at" not in first and _wait(http, first["job_id"])["status"] == "done" and len(calls) == 1
    saved_file = next(tmp_path.glob("audit-*.json.gz"))
    assert saved_file.stat().st_mode & 0o777 == 0o600

    http = _audit_app(tmp_path)  # a restart: nothing in memory
    again = http.post("/api/security/access-audit", json={"subscriptions": [SUB_B, SUB_A]}).get_json()
    assert again["saved_at"] and _wait(http, again["job_id"])["result"]["summary"]["people"] == 4 and len(calls) == 1
    assert "Owner" in http.get(f"/api/security/access-audit/{again['job_id']}/export?format=csv").get_data(as_text=True)

    fresh = http.post("/api/security/access-audit", json={"subscriptions": [SUB_A, SUB_B], "again": True}).get_json()
    assert "saved_at" not in fresh and _wait(http, fresh["job_id"])["status"] == "done" and len(calls) == 2

    assert http.post("/api/cache/clear", json={}).get_json()["ok"] and not saved_file.exists()


def test_a_saved_audit_skips_the_many_subscriptions_question_but_not_other_scopes(run, tmp_path):
    from healthconsole.core import audit_store
    many = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(access_audit.MAX_SUBSCRIPTIONS + 1)]
    ignore = config.load("/nonexistent")["audit"]["ignore_actions"]
    path = audit_store.path_for(tmp_path, FakeClient().identity.tenant_id, FakeClient().identity.object_id)
    audit_store.put(path, audit_store.scope_key(many, None, ignore), {"people": [], "summary": {"people": 0}}, 7)
    http = _audit_app(tmp_path)
    assert http.post("/api/security/access-audit", json={"subscriptions": many}).get_json()["saved_at"]
    assert http.post("/api/security/access-audit", json={"subscriptions": many, "again": True}).get_json()["too_many"] == 101
    assert http.post("/api/security/access-audit", json={"subscriptions": many[:-1] + [SUB_A]}).get_json()["too_many"] == 101


def test_audit_store_keeps_days_and_keys_by_ignore_list(tmp_path):
    from datetime import datetime, timedelta, timezone

    from healthconsole.core import audit_store
    path, now = tmp_path / "a.json.gz", datetime(2026, 11, 21, tzinfo=timezone.utc)
    key = audit_store.scope_key([SUB_A, SUB_B], None, ["Microsoft.Support/*"])
    assert key == audit_store.scope_key([SUB_B.upper(), SUB_A], [], ["Microsoft.Support/*"])
    assert key != audit_store.scope_key([SUB_A, SUB_B], None, [])
    audit_store.put(path, key, {"n": 1}, 7, now=now)
    assert audit_store.get(path, key, 7, now=now + timedelta(days=6))["result"] == {"n": 1}
    assert audit_store.get(path, key, 7, now=now + timedelta(days=8)) is None
    assert audit_store.get(path, key, 0, now=now) is None
    assert audit_store.put(tmp_path / "b.json.gz", key, {"n": 1}, 0) is None and not (tmp_path / "b.json.gz").exists()
