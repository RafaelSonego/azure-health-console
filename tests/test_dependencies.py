import pytest

from tests.conftest import SUB, FakeClient
from healthconsole.core.arm import WriteBlocked
from healthconsole.core.explore import aks, dependencies as deps, kube

AKS = f"/subscriptions/{SUB}/resourceGroups/shop/providers/Microsoft.ContainerService/managedClusters/shop-aks"
ISSUER = "https://canadacentral.oic.prod-aks.azure.com/tenant/cluster/"
IDENTITY = f"/subscriptions/{SUB}/resourceGroups/shop/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shop-id"
REDIS = f"/subscriptions/{SUB}/resourceGroups/shop/providers/Microsoft.Cache/Redis/shop-redis"
COSMOS = f"/subscriptions/{SUB}/resourceGroups/shop/providers/Microsoft.DocumentDB/databaseAccounts/shop-cosmos"
VAULT = f"/subscriptions/{SUB}/resourceGroups/shop/providers/Microsoft.KeyVault/vaults/shop-kv"


def test_host_names_come_from_urls_azure_names_and_storage_connection_strings():
    text = ("REDIS=shop-redis.redis.cache.windows.net:6380,ssl=True;password=never-shown "
            "COSMOS=https://shop-cosmos-canadacentral.documents.azure.com:443/ "
            "DefaultEndpointsProtocol=https;AccountName=shopst;AccountKey=abc;EndpointSuffix=core.windows.net "
            "PRICES=http://prices.shop.svc.cluster.local:3000/v1 LOGFILE=app.config.json CLASS=com.example.Thing "
            "GATEWAY=https://api.example.com/v2")
    assert deps.hosts_in(text) == {"shop-redis.redis.cache.windows.net", "shop-cosmos-canadacentral.documents.azure.com",
                                   "shopst.blob.core.windows.net", "prices.shop.svc.cluster.local", "api.example.com"}
    assert deps._candidates("shop-cosmos-canadacentral.documents.azure.com") == {"shop-cosmos-canadacentral", "shop-cosmos"}
    assert deps._candidates("shopst.privatelink.blob.core.windows.net") == {"shopst"}


class Kube:
    """Records every path; answers a pod and its ConfigMaps."""

    def __init__(self, maps):
        self.paths, self.maps = [], maps

    def get(self, path, params=None, raw=False):
        self.paths.append(path)
        if path.endswith("/pods/api-1"):
            return {"spec": {"serviceAccountName": "shop-sa", "containers": [{"name": "api", "env": [
                {"name": "ORDERS_URL", "value": "https://orders.azurewebsites.net/api"},
                {"name": "REDIS_HOST", "valueFrom": {"configMapKeyRef": {"name": "app", "key": "redis"}}},
                {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}}}],
                "envFrom": [{"configMapRef": {"name": "common"}}, {"secretRef": {"name": "keys"}}]}],
                "volumes": [{"name": "cfg", "configMap": {"name": "files"}}, {"name": "tls", "secret": {"secretName": "tls"}}]}}
        name = path.rsplit("/", 1)[-1]
        return {"data": self.maps[name]} if "/configmaps/" in path and name in self.maps else None


def test_configuration_reads_only_the_keys_used_and_never_a_secret():
    k = Kube({"app": {"redis": "shop-redis.redis.cache.windows.net", "unused": "other.vault.azure.net"},
              "common": {"COSMOS": "https://shop-cosmos.documents.azure.com/"}})
    out = deps.configuration(k, "shop", pod="api-1")
    assert out["hosts"] == {"orders.azurewebsites.net": ["env ORDERS_URL (api)"],
                            "shop-redis.redis.cache.windows.net": ["ConfigMap app, key redis"],
                            "shop-cosmos.documents.azure.com": ["ConfigMap common, key COSMOS"]}
    assert out["secrets"] == ["db", "keys", "tls"] and out["service_account"] == "shop-sa"
    assert out["notes"] == ["ConfigMap files doesn't exist."]
    assert not any("/secrets" in p for p in k.paths)


def test_the_guard_allows_a_configmap_but_never_a_secret():
    kube.check_kube_read("GET", "/api/v1/namespaces/shop/configmaps/app")
    kube.check_kube_read("GET", "/apis/apps/v1/namespaces/shop/deployments/api")
    for path in ("/api/v1/namespaces/shop/secrets/db", "/api/v1/namespaces/shop/secrets", "/api/v1/namespaces/shop/configmaps"):
        with pytest.raises(WriteBlocked):
            kube.check_kube_read("GET", path)


def cluster_client():
    def resources(kql):
        if "managedclusters" in kql:
            return [{"id": AKS, "name": "shop-aks", "subscriptionId": SUB, "issuer": ISSUER}]
        if "userassignedidentities" in kql:
            return [{"id": IDENTITY, "name": "shop-id", "principal": "p-1"}]
        if "databaseaccounts" in kql and "disableLocalAuth" in kql:
            return [{"id": COSMOS, "name": "shop-cosmos", "type": "Microsoft.DocumentDB/databaseAccounts"}]
        if "id in~" in kql:
            return [{"id": VAULT, "name": "shop-kv", "type": "Microsoft.KeyVault/vaults"}]
        if "redis" in kql:
            return [{"id": REDIS, "name": "shop-redis", "type": "Microsoft.Cache/Redis"}]
        return []
    roles = [{"principal": "p-1", "scope": VAULT.lower(), "role": "4633458b-17de-408a-b874-0445c86b69e6"}]
    return FakeClient(query_handlers=[("roleassignments", lambda kql: roles), ("Resources", resources)], gets={
        "/federatedIdentityCredentials": {"value": [{"properties": {"issuer": ISSUER, "subject": "system:serviceaccount:shop:shop-sa"}},
                                                    {"properties": {"issuer": ISSUER, "subject": "system:serviceaccount:other:sa"}}]},
        "/sqlRoleAssignments": {"value": [{"properties": {"principalId": "p-1", "roleDefinitionId": f"{COSMOS}/sqlRoleDefinitions/2"}}]},
        "/sqlRoleDefinitions": {"value": [{"id": f"{COSMOS}/sqlRoleDefinitions/2", "properties": {"roleName": "Data Contributor"}}]},
        "/roleDefinitions": {"value": [{"name": "4633458b-17de-408a-b874-0445c86b69e6", "properties": {"roleName": "Key Vault Secrets User"}}]}})


def test_one_list_from_configuration_and_identity(monkeypatch):
    k = Kube({"app": {"redis": "shop-redis.redis.cache.windows.net"}, "common": {}})
    monkeypatch.setattr(aks, "_connect", lambda *a: (k, None))
    out = deps.dependencies(cluster_client(), [SUB], AKS, "shop", pod="api-1")
    by_name = {r["name"]: r for r in out["resources"]}
    assert by_name["shop-redis"]["source"] == "configuration" and by_name["shop-redis"]["found_in"] == ["ConfigMap app, key redis"]
    assert by_name["shop-kv"]["source"] == "identity" and by_name["shop-kv"]["roles"]
    assert by_name["shop-cosmos"]["data_roles"] == ["Data Contributor"]
    assert [i["subject"] for i in out["identities"]] == ["system:serviceaccount:shop:shop-sa"]  # only its own account
    assert out["other_hosts"] == [{"host": "orders.azurewebsites.net", "found_in": ["env ORDERS_URL (api)"]}]


def test_without_the_kubernetes_api_it_says_so_and_uses_the_namespaces_identities(monkeypatch):
    monkeypatch.setattr(aks, "_connect", lambda *a: (None, "The cluster's API server isn't reachable from this machine."))
    out = deps.dependencies(cluster_client(), [SUB], AKS, "shop", workload=("Deployment", "api"))
    assert not out["config_available"] and "isn't reachable" in out["config_reason"]
    assert {r["source"] for r in out["resources"]} == {"identity"}
    assert [i["subject"] for i in out["identities"]] == ["system:serviceaccount:shop:shop-sa"]
    with pytest.raises(ValueError):
        deps.dependencies(cluster_client(), [SUB], AKS, "shop", workload=("Job", "api"))
    with pytest.raises(ValueError):
        deps.dependencies(cluster_client(), [SUB], AKS, "Bad Name", pod="api-1")


def test_route_needs_a_scope_and_a_workload_and_returns_no_values(monkeypatch):
    from flask import Flask
    from healthconsole.core import config
    from healthconsole.web import create_blueprint
    k = Kube({"app": {"redis": "shop-redis.redis.cache.windows.net;password=hunter2"}, "common": {}})
    monkeypatch.setattr(aks, "_connect", lambda *a: (k, None))
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: cluster_client(), config.load("/nonexistent"), authorize=lambda: None))
    http = app.test_client()
    reply = http.get(f"/api/explore/dependencies?id={AKS}&subscription={SUB}&namespace=shop&pod=api-1")
    assert reply.status_code == 200 and "hunter2" not in reply.get_data(as_text=True)
    assert {r["name"] for r in reply.get_json()["resources"]} >= {"shop-redis", "shop-kv"}
    assert http.get(f"/api/explore/dependencies?id={AKS}&subscription={SUB}&namespace=shop").status_code == 400
    assert http.get(f"/api/explore/dependencies?id={AKS}&namespace=shop&pod=api-1").status_code == 400


# ------------------------------------------------------------------ the picker's names, one namespace at a time

def obj(name):
    return {"metadata": {"name": name}}


def test_a_namespace_role_is_enough_for_its_workload_names(monkeypatch):
    class Namespaced:
        server, source = "https://api", "kubeconfig"
        paths = []

        def get(self, path, params=None, raw=False):
            return {"gitVersion": "v1.30"}

        def items(self, path, params=None):
            self.paths.append(path)
            if path.endswith("/daemonsets"):
                raise kube.KubeUnavailable("No access to the Kubernetes API (HTTP 403).")
            return {"/apis/apps/v1/namespaces/shop/deployments": [obj("web"), obj("api")],
                    "/apis/apps/v1/namespaces/shop/statefulsets": [obj("db")]}[path]

    monkeypatch.setattr(kube, "client_for", lambda *a, **k: Namespaced())
    out = aks.namespace_workloads(FakeClient(), AKS + "-ns", "shop")
    assert out["source"] == "Kubernetes API" and "HTTP 403" in out["notes"][0]
    assert out["workloads"] == [{"kind": "Deployment", "name": "api"}, {"kind": "Deployment", "name": "web"},
                                {"kind": "StatefulSet", "name": "db"}]
    assert all(any(p.match(path) for p in kube._ALLOWED) for path in Namespaced.paths)


def test_without_the_api_container_insights_names_them_and_a_replicaset_is_its_deployment(monkeypatch):
    def unreachable(*a, **k):
        raise kube.KubeUnreachable("The Kubernetes API isn't reachable from here (private cluster?).")

    sent = []
    monkeypatch.setattr(kube, "client_for", unreachable)
    monkeypatch.setattr(aks, "resource_logs", lambda client, rid, query, span: sent.append(query) or [
        {"ControllerKind": "ReplicaSet", "ControllerName": "web-7d9f8c6b4"}, {"ControllerKind": "ReplicaSet", "ControllerName": "web-5c4b9d7f2"},
        {"ControllerKind": "DaemonSet", "ControllerName": "agent"}, {"ControllerKind": "Job", "ControllerName": "nightly-1"}])
    out = aks.namespace_workloads(FakeClient(), AKS + "-ci", "shop")
    assert out["source"] == "Container Insights" and "Namespace == 'shop'" in sent[0]
    assert out["workloads"] == [{"kind": "Deployment", "name": "web"}, {"kind": "DaemonSet", "name": "agent"}]
    assert "isn't reachable" in out["notes"][0]


def test_new_relic_names_a_namespaces_workloads_from_its_container_samples():
    from healthconsole.core.integrations import monitors, nr_data
    from healthconsole.core.integrations.provider import WORKLOADS

    class Nr:
        sent = []

        def nrql_many(self, account_id, nrqls):
            self.sent += nrqls
            return [[{"deploymentName": ["web", "api"], "statefulsetName": ["db"], "daemonsetName": []}]]

    nr = nr_data.NewRelicProvider({"name": "NR", "type": "newrelic", "account_ids": [1], "tenant": "t"}, client=Nr())
    out = nr.workload_names("shop-aks", "shop", refresh=True)
    assert out["found"] and out["workloads"] == [{"kind": "Deployment", "name": "api"}, {"kind": "Deployment", "name": "web"},
                                                  {"kind": "StatefulSet", "name": "db"}]
    assert "clusterName = 'shop-aks' AND namespaceName = 'shop'" in Nr.sent[0] and "jobName" not in Nr.sent[0]
    assert WORKLOADS in monitors.provider_for({"name": "NR", "type": "newrelic"}).capabilities
    with pytest.raises(nr_data.ProviderError):
        nr.workload_names("shop-aks", "shop' OR 1=1")


def test_the_endpoint_refuses_a_namespace_that_isnt_one(tmp_path):
    from flask import Flask
    from healthconsole.core import config
    from healthconsole.web import create_blueprint

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(), {**config.load("/nonexistent"), "cache_dir": str(tmp_path)}, authorize=lambda: None))
    response = app.test_client().get("/api/explore/workload-names", query_string={"id": AKS, "namespace": "Shop'"})
    assert response.status_code == 400
