import os
import stat
from datetime import date

import pytest

from healthconsole.core import readiness, tables
from healthconsole.core.arm import ArmError
from tests.conftest import SUB, FakeClient, rid

TODAY = date(2026, 11, 5)
PEAK = date(2026, 12, 2)
OPTIONS = {"subnet_warning_percent": 80, "subnet_critical_percent": 90, "expiry_warning_days": 30}
CLUSTER = rid("rg-aks", "Microsoft.ContainerService/managedClusters", "aks1")
VNET = rid("rg-net", "Microsoft.Network/virtualNetworks", "vnet1").lower()
SUBNET = f"{VNET}/subnets/aks"


def pool(name, **overrides):
    row = {"id": CLUSTER, "name": "aks1", "type": "microsoft.containerservice/managedclusters", "kind": None,
           "resourceGroup": "rg-aks", "subscriptionId": SUB, "location": "canadacentral", "plugin": "azure",
           "pluginMode": "", "pool": name, "mode": "User", "vmSize": "Standard_D4s_v5", "autoscale": True,
           "nodes": 3, "minCount": 2, "maxCount": 10, "maxPods": 30, "priority": "", "subnetId": SUBNET,
           "podSubnetId": "", "power": "Running"}
    row.update(overrides)
    return row


def plan(name, cap, **overrides):
    row = {"id": rid("rg-web", "Microsoft.Web/serverfarms", name), "name": name, "type": "microsoft.web/serverfarms",
           "kind": "app", "resourceGroup": "rg-web", "subscriptionId": SUB, "cap": cap, "sku": "P1v3", "sites": 2}
    row.update(overrides)
    return row


def subnet_row(used, prefix="10.0.0.0/24"):
    return {"vnetId": VNET, "vnet": "vnet1", "resourceGroup": "rg-net", "subscriptionId": SUB, "subnet": "aks",
            "subnetId": SUBNET, "prefix": prefix, "prefixes": None, "used": used, "links": None, "nsg": "",
            "routeTable": "", "delegations": None}


USAGES = {"value": [
    {"name": {"value": "cores", "localizedValue": "Total Regional vCPUs"}, "currentValue": 90, "limit": 100},
    {"name": {"value": "standardDSv5Family", "localizedValue": "Standard DSv5 Family vCPUs"},
     "currentValue": 12, "limit": 100},
]}
SKUS = {"value": [{"resourceType": "virtualMachines", "name": "Standard_D4s_v5", "family": "standardDSv5Family",
                   "capabilities": [{"name": "vCPUs", "value": "4"}]}]}


def client(pools=(), plans=(), autoscaled=(), subnets=(), gets=None):
    return FakeClient(
        query_handlers=[("mv-expand pool", lambda q: list(pools)), ("autoscalesettings", lambda q: list(autoscaled)),
                        ("serverfarms", lambda q: list(plans)), ("virtualnetworks", lambda q: list(subnets))],
        gets=gets if gets is not None else {"Compute/locations": USAGES, "Compute/skus": SKUS})


@pytest.fixture(autouse=True)
def clear_sku_cache():
    readiness._sku_cache.clear()


def section(result, key):
    return next(s for s in result["sections"] if s["key"] == key)


def test_ips_per_node_follows_the_network_plugin():
    assert readiness.ips_per_node({"plugin": "azure", "pluginMode": "", "maxPods": 30}) == (31, 0)
    assert readiness.ips_per_node({"plugin": "azure", "pluginMode": "overlay", "maxPods": 250}) == (1, 0)
    assert readiness.ips_per_node({"plugin": "kubenet", "maxPods": 110}) == (1, 0)
    assert readiness.ips_per_node({"plugin": "azure", "maxPods": 30, "podSubnetId": "x"}) == (1, 30)


def test_scaling_and_single_points():
    pools = [pool("busy", nodes=10), pool("fixed", autoscale=False, nodes=1, maxCount=None, minCount=None),
             pool("sys", mode="System", autoscale=False, nodes=3, maxCount=None)]
    result = readiness.run(client(pools, [plan("one", 1), plan("two", 4)]), [SUB], OPTIONS, PEAK, deep=False,
                           today=TODAY)
    scaling = {i["name"]: i for i in section(result, "scaling")["items"]}
    assert scaling["aks1 / busy"]["status"] == "critical" and "Raise the pool's maxCount" in scaling["aks1 / busy"]["fix"]
    assert scaling["aks1 / fixed"]["status"] == "warning"
    assert scaling["aks1 / sys"]["status"] == "info"
    assert scaling["two"]["status"] == "info" and "no autoscale" in scaling["two"]["detail"]
    singles = {i["name"] for i in section(result, "single_points")["items"]}
    assert singles == {"one", "aks1 / fixed"}
    assert section(result, "hpas")["skipped"] and section(result, "certificates")["skipped"]


def test_quota_flags_family_and_regional_total():
    # 7 more D4s_v5 nodes = 28 vCPUs; regional total has 10 free, family has 88 free.
    result = readiness.run(client([pool("p")]), [SUB], OPTIONS, PEAK, deep=False, today=TODAY)
    items = {i["name"]: i for i in section(result, "quota")["items"]}
    assert items["Total regional vCPUs · canadacentral"]["status"] == "critical"
    assert "can add up to 28" in items["Total regional vCPUs · canadacentral"]["detail"]
    assert items["Standard DSv5 Family vCPUs · canadacentral"]["status"] == "healthy"


def test_quota_spot_and_unknown_sizes():
    pools = [pool("spot", priority="Spot"), pool("odd", vmSize="Standard_Weird")]
    usages = {"value": USAGES["value"] + [{"name": {"value": "lowPriorityCores"}, "currentValue": 0, "limit": 10}]}
    result = readiness.run(client(pools, gets={"Compute/locations": usages, "Compute/skus": SKUS}), [SUB], OPTIONS,
                           PEAK, deep=False, today=TODAY)
    quota = section(result, "quota")
    spot = next(i for i in quota["items"] if i["name"].startswith("Spot"))
    assert spot["status"] == "critical"  # 28 needed, 10 available
    assert any("Standard_Weird" in n for n in quota["unchecked"])


def test_quota_unreadable_is_named_with_its_fix():
    def denied(params):
        raise ArmError("No access", 403)
    result = readiness.run(client([pool("p")], gets={"Compute/locations": denied}), [SUB], OPTIONS, PEAK,
                           deep=False, today=TODAY)
    assert "needs Reader on the subscription" in section(result, "quota")["unchecked"][0]


def test_aks_subnet_headroom():
    # Azure CNI: 7 more nodes x 31 IPs = 217; a /24 has 251 usable, 100 used -> 151 free.
    result = readiness.run(client([pool("p")], subnets=[subnet_row(100)]), [SUB], OPTIONS, PEAK, deep=False,
                           today=TODAY)
    item = next(i for i in section(result, "subnets")["items"] if i["display_type"] == "AKS subnet")
    assert item["status"] == "critical" and "need 217" in item["detail"]
    overlay = readiness.run(client([pool("p", pluginMode="overlay")], subnets=[subnet_row(100)]), [SUB], OPTIONS,
                            PEAK, deep=False, today=TODAY)
    item = next(i for i in section(overlay, "subnets")["items"] if i["display_type"] == "AKS subnet")
    assert item["status"] == "healthy"


def test_kubernetes_sections(monkeypatch):
    hpa_rows = [{"namespace": "shop", "workload": "Deployment/cart", "min": 2, "max": 10, "current": 10, "targets": "cpu 70%"},
                {"namespace": "shop", "workload": "Deployment/web", "min": 2, "max": 10, "current": 3, "targets": "cpu 70%"}]
    findings = [
        {"severity": "HIGH", "category": "availability", "problem": "1 replica and no HPA", "why": "downtime",
         "action": "Run 2.", "namespace": "shop", "workload": "Deployment/solo", "kind": "config"},
        {"severity": "HIGH", "category": "hpa", "problem": "HPA cannot scale", "why": "", "action": "Fix metrics.",
         "namespace": "shop", "workload": "Deployment/web", "kind": "now"},
        {"severity": "HIGH", "category": "capacity", "problem": "4 pod(s) are Pending", "why": "", "action": "Scale.",
         "namespace": "", "workload": "", "kind": "now"},
    ]
    monkeypatch.setattr(readiness.aks, "kubernetes", lambda c, cid, kube_settings=None: {
        "available": True, "findings": findings, "hpas": hpa_rows, "unreadable": []})
    monkeypatch.setattr(readiness, "keyvault_scan", lambda *a, **k: {"vaults": 0, "rows": [], "errors": []})
    result = readiness.run(client([pool("p")]), [SUB], OPTIONS, PEAK, deep=True, today=TODAY)
    hpas = {i["name"]: i for i in section(result, "hpas")["items"]}
    assert hpas["aks1 / shop/Deployment/cart"]["status"] == "critical"
    assert hpas["aks1 / shop/Deployment/web"]["status"] == "critical"
    assert "HPA cannot scale" in hpas["aks1 / shop/Deployment/web"]["detail"]
    assert any(i["name"] == "aks1 / shop/Deployment/solo" for i in section(result, "single_points")["items"])
    assert any(i["display_type"] == "AKS capacity" for i in section(result, "scaling")["items"])


def test_kubernetes_unreadable_cluster_is_named(monkeypatch):
    monkeypatch.setattr(readiness.aks, "kubernetes", lambda c, cid, kube_settings=None: {
        "available": False, "reason": "403 Forbidden\nmore"})
    monkeypatch.setattr(readiness, "keyvault_scan", lambda *a, **k: {"vaults": 0, "rows": [], "errors": []})
    result = readiness.run(client([pool("p")]), [SUB], OPTIONS, PEAK, deep=True, today=TODAY)
    assert section(result, "hpas")["unchecked"] == ["aks1: Kubernetes not readable (403 Forbidden)"]


def test_certificates_are_judged_against_the_peak_end(monkeypatch):
    def row(name, days, kind="certificate"):
        return {"vault": "kv1", "vault_id": "/kv1", "resource_group": "rg", "subscription_id": SUB, "kind": kind,
                "name": name, "enabled": True, "days_left": days, "expires": "2026-12-01" if days is not None else None,
                "note": "", "status": "warning"}
    rows = [row("before", 20), row("after", 40), row("fine", 200), row("none", None), row("ancient", -400),
            row("secret", 1, kind="secret")]
    monkeypatch.setattr(readiness, "keyvault_scan", lambda *a, **k: {"vaults": 1, "rows": rows, "errors": []})
    monkeypatch.setattr(readiness.aks, "kubernetes", lambda *a, **k: {"available": True, "findings": [], "hpas": []})
    result = readiness.run(client(), [SUB], OPTIONS, PEAK, deep=True, today=TODAY)
    certs = {i["name"]: i["status"] for i in section(result, "certificates")["items"]}
    assert certs == {"kv1 / before": "critical", "kv1 / after": "warning"}
    assert "1 fine past the peak" in section(result, "certificates")["summary"]
    with_secrets = readiness.run(client(), [SUB], OPTIONS, PEAK, deep=True, include_secrets=True, today=TODAY)
    assert "kv1 / secret" in {i["name"] for i in section(with_secrets, "certificates")["items"]}


def test_parse_peak_end():
    assert readiness.parse_peak_end("2026-12-02", TODAY) == PEAK
    with pytest.raises(ValueError, match="in the past"):
        readiness.parse_peak_end("2026-11-01", TODAY)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        readiness.parse_peak_end("soon", TODAY)


def test_export_renders_markdown_and_pdf():
    result = readiness.run(client([pool("p", nodes=10)], [plan("one", 1)]), [SUB], OPTIONS, PEAK, deep=False,
                           today=TODAY)
    doc = readiness.to_doc(result, {SUB: "Prod"})
    text = tables.render(doc, "md").decode()
    assert "# Pre-peak readiness" in text and "Peak ends 2026-12-02 (27 day(s) away)" in text
    assert "Prod / rg-aks" in text and "Not run (deep checks off)" in text
    assert tables.render(doc, "pdf").startswith(b"%PDF")
    problems_only = readiness.to_doc(result, include_healthy=False)
    assert all(r[0] in ("critical", "warning") for s in problems_only["sections"] for r in s["rows"] if s["columns"][0] == "Status")


def test_web_job_and_export():
    import time as clock

    from flask import Flask

    from healthconsole.core import config
    from healthconsole.web import create_blueprint

    settings = config.load("/nonexistent")
    fake = client([pool("p", nodes=10)], [plan("one", 1)])
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: fake, settings, authorize=lambda: None))
    web = app.test_client()
    assert web.post("/api/readiness", json={"subscriptions": [SUB], "peak_end": "2001-01-01"}).status_code == 400
    started = web.post("/api/readiness", json={"subscriptions": [SUB], "peak_end": "2999-01-01", "deep": False})
    assert started.status_code == 202
    job_id = started.get_json()["job_id"]
    for _ in range(50):
        job = web.get(f"/api/checks/{job_id}").get_json()
        if job["status"] != "running":
            break
        clock.sleep(0.05)
    assert job["status"] == "done"
    assert job["result"]["counts"]["critical"] >= 1
    exported = web.get(f"/api/readiness/{job_id}/export?format=md&healthy=0")
    assert exported.status_code == 200 and b"# Pre-peak readiness" in exported.data


def test_cli_writes_the_report(tmp_path, monkeypatch, capsys):
    from healthconsole import cli

    class Client(FakeClient):
        def get(self, path, api_version, params=None, tolerate=(), timeout=None):
            if path == "/subscriptions":
                return {"value": [{"subscriptionId": SUB, "displayName": "prod-one", "state": "Enabled"}]}
            return super().get(path, api_version, params, tolerate, timeout)

    fake = Client(query_handlers=[("mv-expand pool", lambda q: [pool("p", nodes=10)])],
                  gets={"/tenants": {"value": []}, "Compute/locations": USAGES, "Compute/skus": SKUS})
    monkeypatch.setattr(cli, "_client", lambda args, settings: fake)
    old = os.umask(0o022)
    try:
        cli.main(["--config", str(tmp_path / "none.yaml"), "readiness", "--peak-end", "2999-01-01",
                  "--subscription", "prod-*", "--no-deep", "--out", str(tmp_path)])
    finally:
        os.umask(old)
    written = list(tmp_path.glob("Pre-peak-readiness-*.md"))
    assert len(written) == 1 and "prod-one / rg-aks" in written[0].read_text()
    assert stat.S_IMODE(written[0].stat().st_mode) == 0o600  # reports name tenants and resources: yours only
    with pytest.raises(SystemExit, match="no subscriptions"):
        cli.main(["--config", str(tmp_path / "none.yaml"), "readiness", "--peak-end", "2999-01-01", "--no-deep"])


def test_sku_list_is_fetched_once_per_region_under_parallel_use():
    calls = []

    def skus(params):
        calls.append(params)
        return SKUS

    fake = client(gets={"Compute/skus": skus})
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as workers:
        list(workers.map(lambda _: readiness._vm_sizes(fake, SUB, "canadacentral"), range(8)))
    assert len(calls) == 1


def test_kubenet_subnet_fix_does_not_suggest_overlay():
    result = readiness.run(client([pool("p", plugin="kubenet")], subnets=[subnet_row(250)]), [SUB], OPTIONS, PEAK,
                           deep=False, today=TODAY)
    item = next(i for i in section(result, "subnets")["items"] if i["display_type"] == "AKS subnet")
    assert item["status"] == "critical" and "Overlay" not in item["fix"] and "(+7 nodes × 1 IP)" in item["detail"]


def test_csv_exports_never_carry_a_live_spreadsheet_formula():
    doc = {"title": "t", "sections": [{"heading": "h", "columns": ["Name", "Cost"],
                                       "rows": [["=HYPERLINK(\"http://x\",\"click\")", "-12.50"], ["@SUM(A1)", -3],
                                                [" +cmd", "1,234.00"], ["web-01", None]]}]}
    text = tables.to_csv(doc)
    assert text.splitlines() == ["Name,Cost", "\"'=HYPERLINK(\"\"http://x\"\",\"\"click\"\")\",-12.50", "'@SUM(A1),-3",
                                 "' +cmd,\"1,234.00\"", "web-01,"]
