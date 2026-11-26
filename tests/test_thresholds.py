from datetime import date, timedelta

import pytest

from healthconsole.core import baseline, config, cost, triage
from healthconsole.core.explore import aks_checks, workload_problems
from tests.test_explore import NODE, deployment, pod, rs


def _labelled(name, ns, labels):
    d = deployment(name, 2)
    d["metadata"] = {"namespace": ns, "name": name, "labels": labels}
    return d


def test_system_namespaces_are_found_in_the_cluster_not_a_list():
    objs = {"Deployment": [_labelled("coredns", "kube-system", {}),
                           _labelled("gatekeeper", "gatekeeper-system", {"addonmanager.kubernetes.io/mode": "Reconcile"}),
                           _labelled("router", "app-routing-system", {"kubernetes.azure.com/managedby": "aks"}),
                           _labelled("istiod", "istio-system", {"app": "istiod"}),  # installed by the team: theirs
                           _labelled("mixed-a", "mixed", {"addonmanager.kubernetes.io/mode": "Reconcile"}),
                           _labelled("mixed-b", "mixed", {}),
                           _labelled("web", "shop", {"kubernetes.azure.com/managedby": "someone-else"})]}
    found = aks_checks.system_namespaces(objs)
    assert set(found) == {"kube-system", "kube-public", "kube-node-lease", "gatekeeper-system", "app-routing-system"}
    assert found["kube-system"] == "created by Kubernetes"
    assert "addonmanager.kubernetes.io/mode=Reconcile" in found["gatekeeper-system"]
    result = aks_checks.analyze(objs, [], [], [], [], [], [], [], [])
    assert {s["namespace"] for s in result["system_namespaces"]} == set(found)
    assert not any(f["namespace"] in found for f in result["findings"])  # hidden from the checks
    assert any(f["namespace"] == "istio-system" for f in result["findings"])
    shown = aks_checks.analyze(objs, [], [], [], [], [], [], [], [], include_system=True)
    assert any(f["namespace"] == "gatekeeper-system" for f in shown["findings"])


def test_thresholds_come_from_settings_and_each_check_says_what_it_rests_on():
    objs = {"Deployment": [deployment("web", 4)]}
    pods = [pod("web-1", "web-rs")]
    usage = [{"metadata": {"namespace": "shop", "name": "web-1"}, "containers": [{"name": "app", "usage": {"cpu": "50m", "memory": "200Mi"}}]}]

    def memory_findings(percent):
        cfg = aks_checks.Thresholds.from_settings({**config.DEFAULTS["aks"], "memory_near_limit_percent": percent})
        result = aks_checks.analyze(objs, pods, [rs("web-rs", "web")], [], [], [], [NODE], usage, [], cfg=cfg)
        return [f for f in result["findings"] if f["problem"].startswith("Memory") and "of the limit" in f["problem"]]

    assert memory_findings(85) == []  # 200 of 256 MiB is 78%
    found = memory_findings(75)
    assert len(found) == 1
    assert found[0]["rule"] == {"basis": "threshold", "doc": aks_checks.DOCS["memory"],
                                "setting": "aks.memory_near_limit_percent", "value": "over 75% of the limit"}
    no_pdb = aks_checks.analyze(objs, pods, [rs("web-rs", "web")], [], [], [], [NODE], usage, [])["findings"]
    assert next(f for f in no_pdb if f["problem"] == "No PodDisruptionBudget")["rule"]["basis"] == "practice"
    assert aks_checks.Thresholds.from_settings(config.DEFAULTS["aks"]) == aks_checks.Thresholds()  # defaults agree


def test_every_finding_names_its_basis_and_every_docs_link_is_one_of_the_checked_pages():
    objs = {"Deployment": [deployment("web", 4), deployment("solo", 1), deployment("cart", 3, ready=1)]}
    pods = [pod("web-1", "web-rs", oom=True), pod("solo-1", "solo-rs"), pod("p-1", "web-rs", phase="Pending")]
    result = aks_checks.analyze(objs, pods, [rs("web-rs", "web"), rs("solo-rs", "solo")], [], [], [], [NODE], [], [])
    assert result["findings"] and all(f.get("rule", {}).get("basis") in ("kubernetes", "practice", "threshold") for f in result["findings"])
    docs = {f["rule"]["doc"] for f in result["findings"]} - {""}
    assert docs <= set(aks_checks.DOCS.values()) and all(d.startswith("https://") for d in aks_checks.DOCS.values())


def test_system_namespaces_are_hidden_whichever_source_saw_them():
    ci = {"available": True, "hours": {}, "not_running": [
        {"Namespace": "kube-system", "Pod": "coredns-5d8f9c7b8d-abcde", "Status": "Pending", "OwnerKind": "ReplicaSet", "Owner": "coredns-5d8f9c7b8d"},
        {"Namespace": "gatekeeper-system", "Pod": "gk-5d8f9c7b8d-abcde", "Status": "Pending", "OwnerKind": "ReplicaSet", "Owner": "gk-5d8f9c7b8d"},
        {"Namespace": "shop", "Pod": "web-5d8f9c7b8d-abcde", "Status": "Pending", "OwnerKind": "ReplicaSet", "Owner": "web-5d8f9c7b8d"}]}
    k = {"available": True, "system_namespaces": [{"namespace": "kube-system", "why": ""}, {"namespace": "gatekeeper-system", "why": ""}]}
    assert {g["namespace"] for g in workload_problems.problems([], ci, k)["groups"]} == {"shop"}
    assert {g["namespace"] for g in workload_problems.problems([], ci, k, include_system=True)["groups"]} == {"shop", "kube-system", "gatekeeper-system"}
    no_api = workload_problems.problems([], ci, {"available": False})  # only Kubernetes' own are known then
    assert {g["namespace"] for g in no_api["groups"]} == {"shop", "gatekeeper-system"}


def test_the_leftover_window_is_a_setting():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    row = {"namespace": "shop", "pod": "web-x", "phase": "Failed", "owner": "", "since": "2026-11-24T06:00:00Z"}
    k = {"available": True, "not_running": [row]}
    assert workload_problems.problems([], {}, k, now=now)["leftover"] == []  # 6 h: still a problem
    assert len(workload_problems.problems([], {}, k, now=now, leftover_hours=4)["leftover"]) == 1


def test_other_thresholds_follow_their_settings():
    sent = []

    class Client:
        def query(self, kql, subscriptions=None, limit=None):
            sent.append(kql)
            return []

    triage._autoscale(Client(), ["s"], "", {"capacity_near_max_percent": 90})
    triage._service_health(Client(), ["s"], "", {"resolved_hours": 6})
    assert "cap >= 0.9 * maxCap" in sent[0] and "ago(6h)" in sent[1]

    today = date(2026, 11, 24)
    day = (today - timedelta(days=2)).isoformat()
    days = {(today - timedelta(days=2 + i)).isoformat(): 10.0 for i in range(1, 8)} | {day: 25.0}
    assert cost._spike(days, today, 1.0) is not None  # 2.5× the usual day
    assert cost._spike(days, today, 1.0, factor=3.0) is None

    class Signal:
        value, baseline = 15.0, 10.0
    assert baseline._move(Signal()) is None and baseline._move(Signal(), factor=1.4) == ("up", 1.5)


@pytest.mark.parametrize("key", ["aks.memory_near_limit_percent", "aks.node_image_warning_days", "cost.spike_percent",
                                 "vulnerabilities.eol_warning_days", "triage.resolved_hours", "health.baseline_change_percent"])
def test_new_settings_are_on_the_page_and_checked(key, tmp_path):
    section, name = key.split(".")
    assert (section, name) in {(s, k) for s, k, _, _ in config.SCHEMA}
    bad = tmp_path / "c.yaml"
    bad.write_text(f"{section}:\n  {name}: lots\n")
    with pytest.raises(config.ConfigError):
        config.load(str(bad))
    good = tmp_path / "ok.yaml"
    good.write_text(f"{section}:\n  {name}: 7\n")
    assert config.load(str(good))[section][name] == 7


def test_node_image_age_is_read_from_settings_in_security_too(monkeypatch):
    from healthconsole.core import vulnerabilities
    seen = {}
    monkeypatch.setattr(vulnerabilities, "aks_checks", lambda *a, **kw: seen.update(kw) or [])
    monkeypatch.setattr(vulnerabilities, "app_runtime_checks", lambda *a, **kw: seen.update(kw) or [])
    monkeypatch.setattr(vulnerabilities, "tls_checks", lambda *a, **kw: [])
    vulnerabilities.platform(None, ["s"], settings={"aks": {"node_image_warning_days": 30, "node_image_critical_days": 90},
                                                    "vulnerabilities": {"eol_warning_days": 45}})
    assert seen == {"warn_days": 30, "crit_days": 90, "eol_days": 45}


def test_look_backs_default_windows_are_settings(monkeypatch, tmp_path):
    from healthconsole.core.explore import aks
    sent = []
    monkeypatch.setattr(aks, "resource_logs", lambda c, rid, kql, span: sent.append(kql) or [{"x": 1}])
    defaults = aks.look_back_defaults({"restarts_look_back_hours": 6, "oom_look_back_hours": 72, "events_look_back_hours": 24})
    assert defaults == {"restarts": 6, "oomkilled": 72, "events": 24}
    out = aks.container_insights(None, "/subscriptions/s/resourceGroups/r/providers/Microsoft.ContainerService/managedClusters/c",
                                 None, defaults)
    assert out["hours"] == {"restarts": 6, "oomkilled": 72, "events": 24}
    assert any("ago(6h)" in q and "ContainerRestartCount" in q for q in sent) and any("ago(72h)" in q and "OOMKilled" in q for q in sent)
    picked = aks.container_insights(None, "/x", 1, defaults)  # a picked Look back wins over every default
    assert picked["hours"] == {"restarts": 1, "oomkilled": 1, "events": 1}
    assert aks.look_back_defaults(None) == {"restarts": 1, "oomkilled": 24, "events": 1}
    bad = tmp_path / "c.yaml"
    bad.write_text("aks:\n  oom_look_back_hours: 5\n")  # not one of Look back's choices
    with pytest.raises(config.ConfigError):
        config.load(str(bad))
