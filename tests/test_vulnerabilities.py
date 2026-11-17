import json
from datetime import date

from healthconsole.core import vulnerabilities as v
from tests.conftest import SUB, FakeClient, rid

TODAY = date(2026, 11, 9)
AKS = rid("aks-rg", "Microsoft.ContainerService/managedClusters", "shop")
VM = rid("vm-rg", "Microsoft.Compute/virtualMachines", "jump")

VERSIONS = {"values": [
    {"version": "1.36", "capabilities": {"supportPlan": ["KubernetesOfficial", "AKSLongTermSupport"]},
     "patchVersions": {"1.36.4": {}, "1.36.5": {}}},
    {"version": "1.33", "capabilities": {"supportPlan": ["AKSLongTermSupport"]}, "patchVersions": {"1.33.13": {}}},
    {"version": "1.32", "capabilities": {"supportPlan": ["AKSLongTermSupport"]}, "patchVersions": {"1.32.11": {}}},
    {"version": "1.38", "isPreview": True, "capabilities": {"supportPlan": ["KubernetesOfficial"]}, "patchVersions": {"1.38.0": {}}},
]}


def cluster(name, version, plan="KubernetesOfficial", image="AKSUbuntu-2204gen2containerd-202611.01.0"):
    return {"id": rid("aks-rg", "Microsoft.ContainerService/managedClusters", name), "name": name, "location": "canadacentral",
            "resourceGroup": "aks-rg", "subscriptionId": SUB, "version": version, "plan": plan,
            "pools": [{"name": "system", "nodeImageVersion": image}]}


def test_aks_support_and_node_images():
    v.forget()
    clusters = [cluster("old", "1.27.7"), cluster("lts-only", "1.32.11"), cluster("lts", "1.32.11", plan="AKSLongTermSupport"),
                cluster("behind", "1.36.4"), cluster("current", "1.36.5", image="AKSUbuntu-2204gen2containerd-202312.06.0")]
    client = FakeClient(query_handlers=[("managedclusters", lambda kql: clusters)], gets={"kubernetesVersions": VERSIONS})
    versions, images = v.aks_checks(client, [SUB], today=TODAY)
    by_name = {i["name"]: i for i in versions["items"]}
    assert by_name["old"]["status"] == "critical" and "out of support" in by_name["old"]["detail"]
    assert "1.32 to 1.36" in by_name["old"]["detail"]  # the preview 1.38 isn't counted as supported
    assert by_name["lts-only"]["status"] == "critical" and "Long Term Support" in by_name["lts-only"]["detail"]
    assert "lts" not in by_name and "current" not in by_name
    assert by_name["behind"]["status"] == "warning" and "1.36.5" in by_name["behind"]["detail"]
    assert [(i["name"], i["status"]) for i in images["items"]] == [("current", "critical")]
    calls = client.call_count
    v.aks_checks(client, [SUB], today=TODAY)
    assert client.call_count == calls + 1  # the clusters query again; the version list is cached


def test_runtimes_against_azure_dates():
    stacks = {"linux:node|20-lts": {"eol": "2026-04-30", "deprecated": False},
              "linux:dotnetcore|8.0": {"eol": "2026-11-10", "deprecated": False},
              "linux:python|3.12": {"eol": "2028-10-31", "deprecated": False}}
    assert v.judge_runtime("NODE|20-lts", "linux", stacks, TODAY)[0] == "critical"
    assert "1 days" in v.judge_runtime("DOTNETCORE|8.0", "linux", stacks, TODAY)[1]
    assert v.judge_runtime("PYTHON|3.12", "linux", stacks, TODAY) is None
    assert "retired" in v.judge_runtime("NODE|14-lts", "linux", stacks, TODAY)[1]
    assert v.judge_runtime("DOCKER|nginx:latest", "linux", stacks, TODAY) is None  # images are Defender's job


def test_tls_and_http():
    rows = [{"id": rid("rg", "Microsoft.Storage/storageAccounts", "old"), "name": "old", "type": "microsoft.storage/storageaccounts",
             "resourceGroup": "rg", "subscriptionId": SUB, "tls": "TLS1_0", "https": "true"},
            {"id": rid("rg", "Microsoft.Web/sites", "web"), "name": "web", "type": "microsoft.web/sites", "resourceGroup": "rg",
             "subscriptionId": SUB, "tls": "", "https": "false"}]
    card = v.tls_checks(FakeClient(query_handlers=[("minimumTlsVersion", lambda kql: rows)]), [SUB])
    assert [i["detail"] for i in card["items"]] == ["Accepts TLS 1.0 (1.2 is the minimum to keep).", "Allows plain HTTP."]


def test_findings_rank_reachable_and_running_first():
    def resources(kql):
        return [{"parent": VM.lower(), "rtype": "microsoft.compute/virtualmachines", "subscriptionId": SUB, "resourceGroup": "vm-rg",
                 "findings": 2, "high": 1, "medium": 1, "low": 0, "fixable": 2, "maxCvss": 9.9, "live": 2, "running": 0,
                 "images": 0, "children": 0},
                {"parent": AKS.lower(), "rtype": "microsoft.containerservice/managedclusters", "subscriptionId": SUB,
                 "resourceGroup": "aks-rg", "findings": 900, "high": 400, "medium": 400, "low": 100, "fixable": 900,
                 "maxCvss": 9.8, "live": 300, "running": 300, "images": 0, "children": 12}]

    handlers = [("by parent, rtype, subscriptionId", resources), ("by software", lambda kql: []),
                ("by rtype", lambda kql: []), ("resources=dcount(parent)", lambda kql: [{"findings": 902, "resources": 2}]),
                ("apiServerAccessProfile", lambda kql: [{"id": AKS.lower(), "type": "microsoft.containerservice/managedclusters",
                                                         "pna": "", "privateCluster": "false"}]),
                ("networkinterfaces", lambda kql: [])]
    client = FakeClient(query_handlers=handlers)
    out = v.findings(client, [SUB], filters={"severity": ["High", "Bogus"], "fixable": True, "cvss_min": 7})
    assert [r["name"] for r in out["resources"]] == ["shop", "jump"]  # reachable (public API server) first
    assert out["resources"][0]["exposure"] == ["API server public"]
    kql = next(q for q in client.queries if "by parent, rtype" in q)
    assert 'severity in~ (dynamic(["High"]))' in kql and "where hasFix" in kql and "cvss >= 7.0" in kql
    assert out["summary"]["findings"] == 902 and out["summary"]["high"] == 0


def test_resource_findings_parse_cves_and_where():
    cves = json.dumps([{"CveId": "CVE-2026-1", "Severity": "High", "FixStatus": "FixAvailable", "FixedVersion": "1.2"}])
    rows = [{"title": "Update openssl", "software": "openssl", "severity": "High", "cvss": 9.8, "fixed": "", "hasFix": True,
             "child": "payments-api", "childKind": "K8s-container", "detected": '["3.0.1"]', "cves": cves}]
    out = v.for_resource(FakeClient(query_handlers=[("where parent ==", lambda kql: rows)]), AKS)
    f = out["findings"][0]
    assert f["where"] == "running in AKS" and f["detected"] == "3.0.1" and f["cves"][0]["fixed"] == "1.2"


def test_coverage_names_unscanned_machines_and_plans_off():
    def handler(kql):
        if "pricings" in kql:
            return [{"subscriptionId": SUB, "plan": "VirtualMachines", "tier": "Free"},
                    {"subscriptionId": SUB, "plan": "Containers", "tier": "Standard"}]
        return [{"rid": VM.lower(), "subscriptionId": SUB, "resourceGroup": "vm-rg"}]
    out = v.coverage(FakeClient(query_handlers=[("securityresources", handler)]), [SUB])
    assert out["machines"][0]["name"] == "jump"
    assert [p["plan"] for p in out["plans_off"]] == ["VirtualMachines"]
    assert out["defender"] == "partial"
    nothing = v.coverage(FakeClient(query_handlers=[("securityresources", lambda kql: [])]), [SUB])
    assert nothing["defender"] == "unknown"  # e.g. a personal tenant where Microsoft.Security was never registered
    free = v.coverage(FakeClient(query_handlers=[("pricings", lambda kql: [{"subscriptionId": SUB, "plan": "VirtualMachines", "tier": "Free"},
                                                                            {"subscriptionId": SUB, "plan": "Containers", "tier": "Free"}])]), [SUB])
    assert free["defender"] == "off" and len(free["plans_off"]) == 2


def test_routes_and_export():
    from flask import Flask

    from healthconsole.core import config
    from healthconsole.web import create_blueprint

    v.forget()

    def handler(kql):
        if "resources=dcount(parent)" in kql and "by software" not in kql:
            return [{"findings": 3, "resources": 1, "high": 2, "medium": 1, "low": 0, "fixable": 3, "running": 3, "images": 0}]
        if "by parent, rtype, subscriptionId" in kql:
            return [{"parent": AKS.lower(), "rtype": "microsoft.containerservice/managedclusters", "subscriptionId": SUB,
                     "resourceGroup": "aks-rg", "findings": 3, "high": 2, "medium": 1, "low": 0, "fixable": 3, "maxCvss": 9.8,
                     "live": 3, "running": 3, "images": 0, "children": 1}]
        if "by software" in kql:
            return [{"software": "openssl", "resources": 1, "findings": 3, "high": 2, "medium": 1, "low": 0, "fixable": 3,
                     "maxCvss": 9.8, "fixedIn": ["3.0.15"], "types": ["microsoft.containerservice/managedclusters"], "running": 3}]
        if "managedclusters" in kql and "supportPlan" in kql:
            return [cluster("shop", "1.27.7")]
        return []

    client = FakeClient(query_handlers=[("", handler)], gets={"kubernetesVersions": VERSIONS, "AppStacks": {"value": []},
                                                              "/subscriptions": {"value": [{"subscriptionId": SUB, "displayName": "sub-a"}]}})
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load("/nonexistent")))
    http = app.test_client()
    assert http.get("/api/vulnerabilities").status_code == 400  # a scope is needed
    data = http.get(f"/api/vulnerabilities?subscription={SUB}&severity=High&fixable=1").get_json()
    assert data["resources"][0]["name"] == "shop" and data["coverage"]["machines"] == []
    assert http.get(f"/api/vulnerabilities?subscription={SUB}&cvss_min=abc").status_code == 400
    cards = http.get(f"/api/vulnerabilities/platform?subscription={SUB}").get_json()["cards"]
    assert [c["key"] for c in cards] == ["aks_versions", "aks_node_images", "runtimes", "tls"]
    text = http.get(f"/api/vulnerabilities/export?subscription={SUB}&format=md").get_data(as_text=True)
    assert "# Vulnerabilities" in text and "| shop |" in text and "openssl" in text and "out of support" in text


def test_search_terms_cve_exact_and_and_running():
    kql = v._base({"q": "cve-2026-1234 openssl", "running": True})
    assert 'contains "\\"CVE-2026-1234\\""' in kql  # the ID with its JSON quotes: CVE-2026-12345 doesn't match
    assert 'software contains "openssl"' in kql and "where live" in kql
    assert kql.count("| where (") == 0 and kql.count(" | where tostring(add.CvesDetails) contains") == 2  # one clause per term, ANDed
    assert len(v.search_terms("a b c d e f g")) == 5 and v.search_terms("x" * 500) == ["x" * 100]
    partial = v._term_clause("CVE-2026-12")
    assert 'contains "CVE-2026-12"' in partial and "software contains" in partial  # a partial ID matches as text
    hostile = v._term_clause('"; drop')
    assert 'contains "\\"; drop"' in hostile and 'contains ""; drop' not in hostile  # the quote is escaped, never closing the string
