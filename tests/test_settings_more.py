import pytest

from healthconsole.core import config, security, triage
from healthconsole.core.explore import aks, appinsights, workload_names as wn

AKS = "/subscriptions/s/resourceGroups/r/providers/Microsoft.ContainerService/managedClusters/c"


def test_the_failure_rule_follows_its_settings():
    counts = {"requests/count": (100, 100), "requests/failed": (8, 0), "exceptions/count": (12, 5)}
    assert appinsights.assess(counts)[2] == ["failed", "exceptions"]  # 8 failed (8%) and 12 exceptions (2.4×)
    strict = {"failed_min": 10, "exceptions_percent": 300}
    assert appinsights.assess(counts, strict) is None
    assert appinsights.assess({"requests/count": (100, 100), "requests/failed": (25, 0)})[0] == "critical"  # 25%, 25 failed
    assert appinsights.assess({"requests/count": (100, 100), "requests/failed": (25, 0)}, {"critical_rate_percent": 30})[0] == "warning"


def test_a_naming_convention_is_learned_from_the_names_not_listed():
    names = ["orders-corp-cc-api", "cart-corp-cc-api", "search-corp-cc-api", "users-corp-cc-api"]
    assert wn.common_words(names) == {"corp"}  # cc is under 3 letters, api a plumbing word
    assert wn.common_words(names[:3]) == set()  # too few names to call anything a convention
    picked = wn.app_candidates(names + ["billing-corp-cc-api"], "orders-corp")
    assert [c["app"] for c in picked] == ["orders-corp-cc-api"]  # corp, shared by all, isn't a reason
    assert wn.tokens("orders-v2-prod") == {"orders", "prod"}  # prod is no longer thrown away by a list
    found = wn.app_candidates(["orders-service", "orders-worker", "billing"], "orders-service")
    assert [a["app"] for a in found] == ["orders-service", "orders-worker"] and found[0]["score"] == 10  # same name first


def test_the_oom_cause_thresholds_are_settings():
    row = {"MemBytes": 230 * 2**20, "LimitBytes": 256 * 2**20, "NodeMemBytes": 4 * 2**30, "NodeAllocBytes": 16 * 2**30}
    assert aks.oom_cause(row)[0] == "unknown"  # 230 of 256 MiB is 89.8%: just under the 90% default
    assert aks.oom_cause(row, near_limit=0.85)[0] == "container"
    assert aks.oom_thresholds({"oom_near_limit_percent": 80, "oom_node_full_percent": 99}) == (0.8, 0.99)


def test_a_container_insights_list_says_when_it_was_cut(monkeypatch):
    def logs(client, rid, kql, span):
        if "take 1" in kql and "PodStatus" not in kql:
            return [{"x": 1}]
        if "ContainerRestartCount" in kql:
            assert "top 201 by Restarts" in kql  # one more than shown
            return [{"Namespace": "shop", "Pod": f"p{i}", "Container": "app", "Restarts": 1} for i in range(201)]
        return []
    monkeypatch.setattr(aks, "resource_logs", logs)
    out = aks.container_insights(None, AKS)
    assert len(out["restarts"]) == 200 and out["truncated"] == {"restarts": 200}


def test_a_triage_card_says_when_it_was_cut(monkeypatch):
    many = [{"id": f"/r/{i}", "name": f"r{i}", "status": "warning"} for i in range(triage.MAX_ITEMS + 1)]
    monkeypatch.setattr(triage, "CARDS", [("failed", "Failed deployments", "", lambda *a: many)])
    card = triage.triage(None, ["s"], {}, cards={"failed"})["cards"][0]
    assert len(card["items"]) == triage.MAX_ITEMS and "there are more" in (card.get("note") or "")


def test_risky_ports_are_a_setting():
    rule = {"rule": "allow-es", "port": "9200"}
    assert security._ports(rule) == ("port(s) 9200", False)
    assert security._ports(rule, [22, 9200]) == ("Elasticsearch (9200)", True)
    assert security._ports({"port": "8443"}, [8443]) == ("port (8443)", True)  # a port without a well-known name
    assert config.load("/nonexistent")["security"]["risky_ports"] == list(security.RISKY_PORTS)


@pytest.mark.parametrize("value", ["22", "[0]", "[70000]", "[true]"])
def test_the_ports_setting_is_checked(value, tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text(f"security:\n  risky_ports: {value}\n")
    with pytest.raises(config.ConfigError):
        config.load(str(bad))
