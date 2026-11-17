import types
import base64

import yaml
import pytest

from healthconsole.core.arm import WriteBlocked, check_read_only
from healthconsole.core.explore import aks_checks, detectors, kube, scaling
from healthconsole.core.explore.details import _generic
from healthconsole.core.model import CRITICAL, HEALTHY, INFO, WARNING

ARM = "https://management.azure.com"
AKS = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/c"


@pytest.mark.parametrize("path", [
    f"{AKS}/listClusterUserCredential",
    f"{AKS}/providers/microsoft.insights/logs",
    "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Sql/servers/x/databases/d/providers/Microsoft.Insights/logs",
])
def test_new_read_posts_allowed(path):
    check_read_only("POST", f"{ARM}{path}")


@pytest.mark.parametrize("path", [
    f"{AKS}/listClusterAdminCredential",
    f"{AKS}/runCommand",
    f"{AKS}/resetServicePrincipalProfile",
    "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/a/listsecrets",
])
def test_other_posts_still_blocked(path):
    with pytest.raises(WriteBlocked):
        check_read_only("POST", f"{ARM}{path}")


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/api/v1/pods", True),
    ("GET", "/apis/autoscaling/v2/horizontalpodautoscalers", True),
    ("GET", "/api/v1/namespaces/shop/pods/web-1/log", True),
    ("GET", "/api/v1/secrets", False),
    ("GET", "/api/v1/namespaces/shop/secrets", False),
    ("GET", "/api/v1/namespaces/shop/pods/web-1/exec", False),
    ("DELETE", "/api/v1/pods", False),
    ("POST", "/api/v1/pods", False),
])
def test_kube_guard(method, path, ok):
    if ok:
        kube.check_kube_read(method, path)
    else:
        with pytest.raises(WriteBlocked):
            kube.check_kube_read(method, path)


def deployment(name, replicas, cpu_req="100m", cpu_lim="200m", mem_req="128Mi", mem_lim="256Mi", ready=None):
    return {"metadata": {"namespace": "shop", "name": name},
            "spec": {"replicas": replicas, "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [
                {"name": "app", "resources": {"requests": {"cpu": cpu_req, "memory": mem_req},
                                              "limits": {"cpu": cpu_lim, "memory": mem_lim}}}]}}},
            "status": {"readyReplicas": replicas if ready is None else ready}}


def pod(name, owner_rs, phase="Running", oom=False):
    status = {"phase": phase, "containerStatuses": [{"name": "app", "restartCount": 1 if oom else 0,
              "lastState": {"terminated": {"reason": "OOMKilled"}} if oom else {}}]}
    return {"metadata": {"namespace": "shop", "name": name, "ownerReferences": [{"kind": "ReplicaSet", "name": owner_rs}]},
            "spec": {"nodeName": "n1", "containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}]},
            "status": status}


def rs(name, deploy):
    return {"metadata": {"namespace": "shop", "name": name, "ownerReferences": [{"kind": "Deployment", "name": deploy}]}}


def hpa(target, current, maximum, minimum=2):
    return {"metadata": {"namespace": "shop", "name": f"{target}-hpa"},
            "spec": {"scaleTargetRef": {"kind": "Deployment", "name": target}, "minReplicas": minimum, "maxReplicas": maximum,
                     "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}}]},
            "status": {"currentReplicas": current, "currentMetrics": [{"type": "Resource", "resource": {"name": "cpu", "current": {"averageUtilization": 95}}}],
                       "conditions": [{"type": "ScalingActive", "status": "True"}]}}


NODE = {"metadata": {"name": "n1", "labels": {"kubernetes.azure.com/agentpool": "user"}},
        "status": {"allocatable": {"cpu": "2", "memory": "4Gi"}, "conditions": [{"type": "Ready", "status": "True"}]}}


def test_aks_checks_current_status():
    objs = {"Deployment": [deployment("web", 4), deployment("solo", 1), deployment("cart", 3, ready=1)]}
    pods = [pod("web-1", "web-rs", oom=True), pod("solo-1", "solo-rs"), pod("pending-1", "web-rs", phase="Pending")]
    metrics = [{"metadata": {"namespace": "shop", "name": "web-1"}, "containers": [{"name": "app", "usage": {"cpu": "190m", "memory": "240Mi"}}]}]
    result = aks_checks.analyze(objs, pods, [rs("web-rs", "web"), rs("solo-rs", "solo")], [], [hpa("web", 4, 4)],
                                [], [NODE], metrics, [{"metadata": {"name": "n1"}, "usage": {"cpu": "500m", "memory": "1Gi"}}])
    problems = {(f["workload"], f["problem"]) for f in result["findings"]}
    text = " | ".join(p for _, p in problems)
    assert "At the maximum 4 replicas" in text
    assert "1 pod(s) were OOMKilled" in text
    assert "of the limit 200m" in text  # CPU throttling
    assert "1 replica and no HPA" in text
    assert "1 of 3 replicas ready" in text
    assert "1 pod(s) are Pending" in text
    assert result["hpas"][0]["current"] == 4
    assert result["pools"][0]["pool"] == "user"


def test_aks_findings_split_now_from_configuration_risks():
    objs = {"Deployment": [deployment("web", 4), deployment("solo", 1), deployment("cart", 3, ready=1)]}
    pods = [pod("web-1", "web-rs", oom=True), pod("solo-1", "solo-rs")]
    result = aks_checks.analyze(objs, pods, [rs("web-rs", "web"), rs("solo-rs", "solo")], [], [hpa("web", 4, 4)],
                                [], [NODE], [], [])
    by_problem = {f["problem"]: f for f in result["findings"]}
    assert by_problem["1 replica and no HPA"]["kind"] == "config"
    assert by_problem["1 replica and no HPA"]["severity"] == "MEDIUM"  # a setup risk is yellow at most
    assert by_problem["At the maximum 4 replicas"]["kind"] == "now"
    assert by_problem["At the maximum 4 replicas"]["severity"] == "HIGH"
    assert by_problem["1 of 3 replicas ready"]["kind"] == "now"
    kinds = [f["kind"] for f in result["findings"]]
    assert kinds == sorted(kinds, key=lambda k: k != "now")  # what's happening now comes first
    assert result["counts_by_kind"]["config"]["HIGH"] == 0


def test_unreadable_kubernetes_lists_are_reported(monkeypatch):
    from healthconsole.core.explore import aks

    class Partial:
        server, source = "https://api", "kubeconfig"

        def get(self, path, params=None, raw=False):
            return {"gitVersion": "v1.30"}

        def items(self, path, params=None):
            if path == "/api/v1/nodes":
                raise kube.KubeUnavailable("No access to the Kubernetes API (/api/v1/nodes, HTTP 403).\nRequired: a role.\nhint")
            return []

    monkeypatch.setattr(kube, "client_for", lambda *a, **k: Partial())
    result = aks.kubernetes(None, "/subscriptions/s/resourceGroups/r/providers/Microsoft.ContainerService/managedClusters/c")
    assert result["available"]
    notes = {n["list"]: n for n in result["unreadable"]}
    assert set(notes) == {"nodes"}
    assert "NotReady" in notes["nodes"]["effect"] and "HTTP 403" in notes["nodes"]["reason"]


def test_kubernetes_with_nothing_readable_is_unavailable(monkeypatch):
    from healthconsole.core.explore import aks

    class Nothing:
        server, source = "https://api", "kubeconfig"

        def get(self, path, params=None, raw=False):
            return {}

        def items(self, path, params=None):
            raise kube.KubeUnavailable("No access to the Kubernetes API (HTTP 403).")

    monkeypatch.setattr(kube, "client_for", lambda *a, **k: Nothing())
    result = aks.kubernetes(None, "/subscriptions/s/resourceGroups/r/providers/Microsoft.ContainerService/managedClusters/c")
    assert result == {"available": False, "reason": "No access to the Kubernetes API (HTTP 403)."}


def test_aks_checks_skip_system_namespaces():
    sys_dep = deployment("coredns", 1)
    sys_dep["metadata"]["namespace"] = "kube-system"
    result = aks_checks.analyze({"Deployment": [sys_dep]}, [], [], [], [], [], [], [], [])
    assert result["workloads"] == []


def test_failed_and_pending_pods_are_listed_with_their_reason():
    evicted = pod("web-1", "web-rs", phase="Failed")
    evicted["status"].update(reason="Evicted", message="The node was low on resource: memory.")
    crashing = pod("web-2", "web-rs", phase="Pending")
    crashing["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ImagePullBackOff", "message": "not found"}}
    system = pod("dns-1", "dns-rs", phase="Failed")
    system["metadata"]["namespace"] = "kube-system"
    rows = aks_checks.analyze({}, [pod("ok", "web-rs"), crashing, evicted, system], [], [], [], [], [], [], [])["not_running"]
    assert [(r["pod"], r["phase"], r["reason"]) for r in rows] == [("web-1", "Failed", "Evicted"), ("web-2", "Pending", "ImagePullBackOff")]
    assert rows[0]["owner"] == "ReplicaSet/web-rs" and "low on resource" in rows[0]["message"]


def test_quantities():
    assert aks_checks.parse_cpu("250m") == 250 and aks_checks.parse_cpu("1") == 1000
    assert aks_checks.parse_mem("1Gi") == 1024 and round(aks_checks.parse_mem("1G")) == 954


def test_scaling_items():
    row = {"sku": {"capacity": 5}, "properties": {"agentPoolProfiles": [
        {"name": "user", "enableAutoScaling": True, "count": 10, "minCount": 2, "maxCount": 10, "mode": "User"},
        {"name": "fixed", "enableAutoScaling": False, "count": 3, "mode": "User"},
        {"name": "sys", "enableAutoScaling": False, "count": 3, "mode": "System"}]}}
    settings = [{"name": "as", "enabled": True, "profiles": [{"capacity": {"minimum": "1", "maximum": "5"}}]}]
    items = {i["name"]: i["status"] for i in scaling.items_for(row, settings)}
    assert items == {"as": CRITICAL, "user": CRITICAL, "fixed": WARNING, "sys": INFO}


def test_detector_categories():
    assert detectors._wanted("Availability and Performance")
    assert detectors._wanted("Node Health")
    assert not detectors._wanted("Child Detector Demo")
    assert not detectors._wanted("Sample Detectors")
    assert not detectors._wanted("Configuration and Management")


def test_detector_insights_parse():
    dataset = [
        {"renderingProperties": {"type": 7}, "table": {
            "columns": [{"columnName": c} for c in ("Status", "Message", "Data.Name", "Data.Value")],
            "rows": [["Critical", "Memory pressure on 2 nodes", "Summary", "<markdown>`2` nodes</markdown>"],
                     ["Critical", "Memory pressure on 2 nodes", "Troubleshooting", "Run `kubectl top nodes`"],
                     ["Success", "fine", "", ""]]}},
        {"renderingProperties": {"type": 26}, "table": {
            "columns": [{"columnName": c} for c in ("Status", "Title", "Description", "Solution")],
            "rows": [["Warning", "Free tier control plane", "No SLA", "Use Standard tier"]]}},
        {"renderingProperties": {"type": 2, "title": "Node NotReady events"}, "table": {
            "columns": [{"columnName": c} for c in ("Timestamp", "Metric", "Events")],
            "rows": [["2026-11-02T05:40:00Z", "pool (00000C)", 4]]}},
        {"renderingProperties": {"type": 23}, "table": {"columns": [], "rows": [["search config"]]}},
    ]
    insights = detectors._insights(dataset)
    memory = insights[0]
    assert memory["status"] == CRITICAL and memory["text"] == "Memory pressure on 2 nodes"
    assert [d["name"] for d in memory["details"]] == ["Summary", "Troubleshooting"]
    assert insights[1] == {"status": HEALTHY, "text": "fine", "details": []}
    risk = insights[2]
    assert risk["status"] == WARNING and [d["name"] for d in risk["details"]] == ["Description", "Solution"]
    tables, notes = detectors._extras(dataset)
    assert tables == [{"title": "Node NotReady events", "columns": ["Timestamp", "Metric", "Events"],
                       "rows": [["2026-11-02T05:40:00Z", "pool (00000C)", "4"]], "total": 1}]
    assert notes == []


def test_private_endpoint_connection_states():
    row = {"location": "canadacentral", "properties": {"privateEndpointConnections": [
        {"properties": {"privateLinkServiceConnectionState": {"status": "Approved"}, "privateEndpoint": {"id": "/x/pe-ok"}}},
        {"properties": {"privateLinkServiceConnectionState": {"status": "Pending"}, "privateEndpoint": {"id": "/x/pe-new"}}}]}}
    facts = {f["label"]: f["status"] for s in _generic(row) for f in s["facts"] if s["title"].startswith("Private")}
    assert facts == {"pe-ok": HEALTHY, "pe-new": WARNING}


@pytest.mark.parametrize("path,role_fragment", [
    (f"{AKS}/listClusterUserCredential", "Cluster User Role"),
    (f"{AKS}/providers/microsoft.insights/logs", "Log Analytics Reader"),
    (f"{AKS}/providers/Microsoft.Insights/metrics", "Monitoring Reader"),
    (f"{AKS}/providers/Microsoft.ResourceHealth/availabilityStatuses/current", "Reader on the resource"),
    ("/subscriptions/s/providers/Microsoft.Insights/eventtypes/management/values", "on the subscription"),
    (f"{AKS}/detectors/NodeMemoryPressure", "Reader on the resource"),
    (AKS, "Reader on the resource, its resource group or subscription"),
])
def test_access_denied_names_the_role(path, role_fragment):
    from healthconsole.core.arm import AccessDenied
    error = AccessDenied(path, "The client 'x' does not have authorization")
    assert role_fragment in error.role
    text = str(error)
    assert "Required:" in text and "PIM" in text and "Azure said: The client 'x'" in text
    assert error.short.startswith("No access to")
    assert error.status == 403


def test_kube_403_names_kubernetes_role(monkeypatch):
    class Response:
        status_code = 403
        def json(self):
            return {"message": "pods is forbidden"}
    client = kube.KubeClient.__new__(kube.KubeClient)
    client.server = "https://api"
    client._token_source = lambda: "t"
    client._session = type("S", (), {"request": lambda *a, **k: Response()})()
    with pytest.raises(kube.KubeUnavailable) as caught:
        client.get("/api/v1/pods")
    assert "RBAC Reader" in str(caught.value) and "pods is forbidden" in str(caught.value)


def test_kube_lists_use_watch_cache():
    seen = {}
    client = kube.KubeClient.__new__(kube.KubeClient)
    client.get = lambda path, params=None, raw=False: seen.setdefault(path, params) and {"items": []}
    client.items("/api/v1/pods")
    client.items("/apis/metrics.k8s.io/v1beta1/pods")
    assert seen["/api/v1/pods"] == {"resourceVersion": "0"}
    assert seen["/apis/metrics.k8s.io/v1beta1/pods"] is None


CA = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n").decode()


class _KubeArm:
    def __init__(self, posts=None):
        self.posts = posts or []
        self.identity = type("I", (), {"tenant_id": "t", "object_id": "o"})()
        self.token_provider = type("T", (), {"get_token": lambda self, scope: f"token-for-{scope}"})()

    def query(self, kql, subscriptions=None):
        return [{"fqdn": "c-abc.hcp.canadacentral.azmk8s.io", "private": "c-abc.privatelink.canadacentral.azmk8s.io", "portal": ""}]

    def api_version(self, resource_id):
        return "2024-10-01"

    def post(self, path, version, body=None, cache=True):
        self.posts.append(path)
        config = {"clusters": [{"name": "c", "cluster": {"server": "https://c-abc.privatelink.canadacentral.azmk8s.io:443",
                                                         "certificate-authority-data": CA}}],
                  "users": [{"name": "u", "user": {"exec": {"command": "kubelogin"}}}]}
        return {"kubeconfigs": [{"value": base64.b64encode(yaml.safe_dump(config).encode()).decode()}]}


def _kubeconfig(tmp_path, server, user, insecure=False):
    cluster = {"server": server, "certificate-authority-data": CA}
    if insecure:
        cluster["insecure-skip-tls-verify"] = True
    path = tmp_path / "config"
    path.write_text(yaml.safe_dump({
        "clusters": [{"name": "c", "cluster": cluster}], "users": [{"name": "u", "user": user}],
        "contexts": [{"name": "my-aks", "context": {"cluster": "c", "user": "u"}}], "current-context": "my-aks"}))
    return path


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(kube, "_desktop_dirs", lambda: [])
    kube._clients.clear()
    monkeypatch.setattr(kube, "KubeClient", lambda server, ca, tokens: types.SimpleNamespace(server=server, tokens=tokens))
    return tmp_path


def test_local_context_is_used_and_exec_is_never_run(isolated, monkeypatch):
    path = _kubeconfig(isolated, "https://c-abc.hcp.canadacentral.azmk8s.io:443",
                       {"exec": {"command": "/bin/rm", "args": ["-rf", "/"]}})
    monkeypatch.setenv("KUBECONFIG", str(path))
    arm = _KubeArm()
    client = kube.client_for(arm, AKS)
    assert "my-aks" in client.source and arm.posts == []
    assert client.tokens() == f"token-for-{kube.AKS_AAD_SERVER_APP}"


def test_falls_back_to_azure_credential(isolated, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", str(_kubeconfig(isolated, "https://other.azmk8s.io", {"token": "x"})))
    arm = _KubeArm()
    client = kube.client_for(arm, AKS)
    assert arm.posts == [f"{AKS}/listClusterUserCredential"]
    assert "fetched from Azure" in client.source


def test_no_fetch_gives_get_credentials_hint(isolated, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", str(_kubeconfig(isolated, "https://c-abc.hcp.canadacentral.azmk8s.io", {"token": "x"},
                                                     insecure=True)))
    with pytest.raises(kube.KubeUnavailable) as caught:
        kube.client_for(_KubeArm(), AKS, {"fetch_credentials": False})
    text = str(caught.value)
    assert "az aks get-credentials -g rg -n c" in text and "skips TLS verification" in text


def test_a_context_whose_server_isnt_https_is_never_used(isolated, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", str(_kubeconfig(isolated, "http://c-abc.hcp.canadacentral.azmk8s.io", {"token": "x"})))
    with pytest.raises(kube.KubeUnavailable) as caught:
        kube.client_for(_KubeArm(), AKS, {"fetch_credentials": False})
    assert "server isn't https" in str(caught.value)


def _secret(name, days=None, enabled=True, content_type=None):
    import time as _time
    attributes = {"enabled": enabled}
    if days is not None:
        attributes["exp"] = int(_time.time() + days * 86400 + 3600)
    props = {"attributes": attributes, "secretUri": "https://v.vault.azure.net/secrets/" + name}
    if content_type:
        props["contentType"] = content_type
    return {"name": name, "properties": props}


def test_secret_expiry_colours():
    from healthconsole.core.explore import keyvault
    from tests.conftest import FakeClient
    vault = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/v"
    client = FakeClient(gets={"/secrets": {"value": [_secret("old", -3), _secret("urgent", 3), _secret("soon", 10),
                                                     _secret("fine", 200), _secret("forever"),
                                                     _secret("off", -50, enabled=False)]},
                              "/keys": {"value": []}})
    result = keyvault.expiry(client, vault, warn_days=30, critical_days=7)
    statuses = {i["name"]: i["status"] for i in result["secrets"]}
    assert statuses == {"old": CRITICAL, "urgent": CRITICAL, "soon": WARNING, "fine": HEALTHY,
                        "forever": INFO, "off": INFO}
    assert [i["name"] for i in result["secrets"]][:3] == ["old", "urgent", "soon"]
    signals = {s.name: s for s in keyvault.signals(result)}
    assert signals["secrets expired"].status == CRITICAL and "old" in signals["secrets expired"].detail
    assert signals["secrets expiring"].status == CRITICAL and "urgent" in signals["secrets expiring"].detail
    assert signals["secrets expiring soon"].status == WARNING and "soon" in signals["secrets expiring soon"].detail


def test_certificates_reported_once_from_their_backing_secret():
    from healthconsole.core.explore import keyvault
    from tests.conftest import FakeClient
    vault = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/v"
    client = FakeClient(gets={"/secrets": {"value": [_secret("tls", 5, content_type="application/x-pkcs12"),
                                                     _secret("db-password", 100)]},
                              "/keys": {"value": [_secret("tls", 5), _secret("signing", 100)]}})
    result = keyvault.expiry(client, vault, warn_days=30, critical_days=7)
    assert [i["name"] for i in result["certificates"]] == ["tls"]
    assert result["certificates"][0]["status"] == CRITICAL
    assert [i["name"] for i in result["secrets"]] == ["db-password"]
    assert [i["name"] for i in result["keys"]] == ["signing"]


def test_vault_scan_adds_app_service_certificates():
    from healthconsole.core import checks
    from tests.conftest import FakeClient, SUB
    vault = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/v"
    vault_row = {"id": vault, "name": "v", "resourceGroup": "rg", "subscriptionId": SUB, "properties": {}}
    cert_row = {"id": "/subscriptions/x/resourceGroups/web/providers/Microsoft.Web/certificates/site",
                "name": "site", "resourceGroup": "web", "subscriptionId": SUB,
                "expires": "2000-01-01T00:00:00Z", "keyVault": ""}
    client = FakeClient(gets={"/secrets": {"value": [_secret("soon", 10)]}, "/keys": {"value": []}},
                        query_handlers=[("microsoft.keyvault/vaults", lambda q: [vault_row]),
                                        ("microsoft.web/certificates", lambda q: [cert_row])])
    result = checks.keyvault_scan(client, [SUB], 30, critical_days=7)
    by_name = {r["name"]: r for r in result["rows"]}
    assert by_name["site"]["source"] == "App Service" and by_name["site"]["status"] == CRITICAL
    assert by_name["soon"]["subscription_id"] == SUB.lower() and by_name["soon"]["kind"] == "secret"
    assert result["counts"]["expired"] == 1 and result["counts"]["warning"] == 1


def test_secret_metadata_access_message():
    from healthconsole.core.arm import AccessDenied
    error = AccessDenied("/subscriptions/s/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/v/secrets")
    assert "never values" in error.what and "Reader on the vault" in error.role


def test_recommendations_include_what_is_inside_the_resource():
    from healthconsole.core.explore import advice
    from tests.conftest import FakeClient

    service = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ApiManagement/service/gw"
    queries = []

    def advisor(kql):
        queries.append(kql)
        return [{"own": False, "n": 288, "parts": ["apis/a", "apis/b"], "category": "Security", "impact": "High",
                 "problem": "p2", "solution": "s2"},
                {"own": True, "n": 1, "parts": [""], "category": "Security", "impact": "Low", "problem": "p1",
                 "solution": "s1"}]

    out = advice.recommendations(FakeClient(query_handlers=[("AdvisorResources", advisor),
                                                            ("securityresources", lambda kql: [])]), service)
    assert [(r["on"], r["count"]) for r in out["advisor"]] == [("", 1), (["apis/a", "apis/b"], 288)]
    assert f'startswith "{service.lower()}/"' in queries[0] and "summarize n = count()" in queries[0]


def test_diagnostics_lists_detectors_then_runs_one_at_a_time(monkeypatch):
    from healthconsole.core.explore import detectors
    listed = [{"name": "health-a", "title": "A", "category": "Availability and Performance"},
              {"name": "other-b", "title": "B", "category": "Configuration"}]
    monkeypatch.setattr(detectors, "available", lambda client, rid: (listed, "2024-01-01", None, None))
    calls = []

    class Arm:
        def get(self, path, version, params=None, tolerate=()):
            calls.append(path)
            return {"properties": {"status": {"statusId": 1}, "dataset": []}}

        def api_version(self, rid):
            return "2024-01-01"
    plan = detectors.plan(Arm(), "/r", only_health=False)
    assert [d["name"] for d in plan["detectors"]] == ["health-a", "other-b"] and not calls  # listing runs nothing
    one = detectors.run_one(Arm(), "/r", "other-b")
    assert one["status"] == "warning" and calls == ["/r/detectors/other-b"]
    assert "error" in detectors.run_one(Arm(), "/r", "missing")
