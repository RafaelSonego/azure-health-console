import json

import pytest
from flask import Flask

from tests.conftest import FakeClient
from healthconsole.core import config
from healthconsole.core.explore import aks, kube_yaml, pod_detail
from healthconsole.web import create_blueprint

AKS = "/subscriptions/s/resourceGroups/r/providers/Microsoft.ContainerService/managedClusters/c"
SECRET = "Server=db;Password=hunter2"

POD = {"apiVersion": "v1", "kind": "Pod",
       "metadata": {"namespace": "shop", "name": "web-5d8f9c7b8d-abcde", "creationTimestamp": "2026-11-24T10:00:00Z",
                    "managedFields": [{"manager": "kubelet"}], "ownerReferences": [{"kind": "ReplicaSet", "name": "web-5d8f9c7b8d"}],
                    "annotations": {kube_yaml.LAST_APPLIED: json.dumps({"env": SECRET})}},
       "spec": {"nodeName": "aks-user-1", "serviceAccountName": "web", "nodeSelector": {"pool": "user"},
                "tolerations": [{"key": "spot", "operator": "Equal", "value": "true", "effect": "NoSchedule"}],
                "initContainers": [{"name": "migrate", "image": "web-migrate:1"},
                                   {"name": "proxy", "image": "proxy:2", "restartPolicy": "Always"}],
                "containers": [{"name": "app", "image": "web:1.3",
                                "env": [{"name": "DB", "value": SECRET},
                                        {"name": "KEY", "valueFrom": {"secretKeyRef": {"name": "web-secrets", "key": "key"}}}],
                                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
                                "readinessProbe": {"httpGet": {"path": "/ready"}, "periodSeconds": 5}}],
                "volumes": [{"name": "creds", "secret": {"secretName": "web-secrets"}}, {"name": "tmp", "emptyDir": {}}]},
       "status": {"phase": "Running", "podIP": "10.0.0.7", "qosClass": "Burstable", "startTime": "2026-11-24T10:00:05Z",
                  "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady", "lastTransitionTime": "2026-11-24T10:01:00Z"}],
                  "initContainerStatuses": [{"name": "migrate", "state": {"terminated": {"reason": "Completed", "exitCode": 0}}},
                                            {"name": "proxy", "ready": True, "state": {"running": {"startedAt": "2026-11-24T10:00:10Z"}}}],
                  "containerStatuses": [{"name": "app", "ready": False, "restartCount": 4, "image": "web:1.3",
                                         "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 40s"}},
                                         "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-11-24T10:05:00Z"}}}]}}
HPA = {"metadata": {"namespace": "shop", "name": "web", "managedFields": [{}]}, "spec": {"minReplicas": 2, "maxReplicas": 6}}


class Kube:
    server, source = "https://api", "kubeconfig"

    def __init__(self):
        self.calls = []

    def get(self, path, params=None, raw=False):
        self.calls.append((path, params))
        return POD if path.endswith("/pods/web-5d8f9c7b8d-abcde") else {"gitVersion": "v1.30"} if path == "/version" else None

    def items(self, path, params=None):
        self.calls.append((path, params))
        if path.endswith("/events"):
            return [{"type": "Warning", "reason": "BackOff", "count": 9, "message": "Back-off restarting", "lastTimestamp": "2026-11-24T10:06:00Z"}]
        if path.endswith("/nodes"):
            return [{"metadata": {"name": "aks-user-1", "labels": {"kubernetes.azure.com/agentpool": "user", "topology.kubernetes.io/zone": "1"}}}]
        if path.endswith("horizontalpodautoscalers"):
            return [HPA]
        return []


@pytest.fixture
def api(monkeypatch):
    k = Kube()
    monkeypatch.setattr(aks, "_connect", lambda *a: (k, None))
    return k


def test_yaml_hides_env_values_and_the_last_applied_copy_but_keeps_references():
    y = kube_yaml.yaml_of(POD, "Pod")
    assert SECRET not in y["yaml"] and "hunter2" not in y["yaml"] and y["hides"]
    assert "secretKeyRef" in y["yaml"] and "web-secrets" in y["yaml"]
    assert "managedFields" not in y["yaml"] and "managedFields" in POD["metadata"]  # the object itself is untouched
    assert y["command"] == "kubectl get pod -n shop web-5d8f9c7b8d-abcde -o yaml"
    template = {"metadata": {"namespace": "shop", "name": "web"},
                "spec": {"template": {"spec": {"containers": [{"name": "app", "env": [{"name": "A", "value": "x1"}]}]}}}}
    assert "x1" not in kube_yaml.yaml_of(template, "Deployment")["yaml"]
    hpa = kube_yaml.yaml_of(HPA, "HorizontalPodAutoscaler")
    assert hpa["yaml"].startswith("apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler") and not hpa["hides"]


def test_values_only_when_asked(api):
    hidden = kube_yaml.read(None, AKS, "shop", "Pod", "web-5d8f9c7b8d-abcde")
    shown = kube_yaml.read(None, AKS, "shop", "Pod", "web-5d8f9c7b8d-abcde", values=True)
    assert "hunter2" not in json.dumps(hidden) and "hunter2" in shown["yaml"] and shown["values"]
    assert kube_yaml.read(None, AKS, "shop", "HorizontalPodAutoscaler", "web")["name"] == "web"  # picked from the namespace's list
    with pytest.raises(ValueError):
        kube_yaml.read(None, AKS, "shop", "Secret", "web-secrets")


def test_pod_details_from_the_api(api):
    d = pod_detail.read(None, AKS, "shop", "web-5d8f9c7b8d-abcde")
    assert d["available"] and "hunter2" not in json.dumps(d)  # the page's first read never carries env values
    assert d["summary"]["owner"] == "Deployment/web" and (d["summary"]["pool"], d["summary"]["zone"]) == ("user", "1")
    assert d["conditions"][0]["reason"] == "ContainersNotReady"
    roles = {c["name"]: c for c in d["containers"]}
    assert roles["migrate"]["role"] == "init" and roles["migrate"]["reason"] == "Completed"
    assert roles["proxy"]["role"] == "sidecar" and roles["proxy"]["state"] == "running"
    app = roles["app"]
    assert (app["state"], app["reason"], app["restarts"]) == ("waiting", "CrashLoopBackOff", 4)
    assert app["last_exit"]["reason"] == "OOMKilled" and app["last_exit"]["code"] == 137
    assert app["probes"]["readiness"]["how"] == "HTTP /ready" and app["memory"]["limit"] == 256
    assert d["events"][0]["reason"] == "BackOff"
    assert ("/api/v1/namespaces/shop/events", {"fieldSelector": "involvedObject.name=web-5d8f9c7b8d-abcde"}) in api.calls
    assert d["scheduling"]["tolerations"] == ["spot Equal true NoSchedule"]
    assert {"name": "creds", "kind": "secret", "ref": "web-secrets"} in d["volumes"]
    assert d["yaml"]["hides"] and d["missing"] == []


def test_without_the_api_container_insights_fills_what_it_can(monkeypatch):
    monkeypatch.setattr(aks, "_connect", lambda *a: (None, "No network path to the API server.\nmore"))
    rows = [{"TimeGenerated": "2026-11-24T10:06:00Z", "PodStatus": "Running", "PodIp": "10.0.0.7", "Computer": "aks-user-1",
             "ControllerKind": "ReplicaSet", "ControllerName": "web-5d8f9c7b8d", "Container": "app", "ContainerStatus": "Waiting",
             "ContainerStatusReason": "CrashLoopBackOff", "ContainerRestartCount": 4,
             "ContainerLastStatus": json.dumps({"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-11-24T10:05:00Z"})}]
    sent = []
    monkeypatch.setattr(aks, "resource_logs", lambda c, rid, kql, span: sent.append(kql) or (rows if "KubePodInventory" in kql else []))
    d = pod_detail.read(None, AKS, "shop", "web-5d8f9c7b8d-abcde")
    assert d["available"] and d["source"].startswith("Container Insights")
    assert d["why_not_api"] == "No network path to the API server."
    assert d["summary"]["owner"] == "Deployment/web"
    assert d["containers"][0]["reason"] == "CrashLoopBackOff" and d["containers"][0]["last_exit"]["code"] == 137
    assert d["missing"] == pod_detail.CI_MISSING and d["yaml"] is None
    assert '"web-5d8f9c7b8d-abcde"' in sent[0]
    monkeypatch.setattr(aks, "resource_logs", lambda *a: [])
    gone = pod_detail.read(None, AKS, "shop", "web-5d8f9c7b8d-abcde")
    assert not gone["available"] and "No network path" in gone["reason"]


def test_routes_check_their_inputs(api):
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"), authorize=lambda: None))
    http = app.test_client()
    assert http.get(f"/api/pod/detail?cluster={AKS}&namespace=shop&pod=web-5d8f9c7b8d-abcde").get_json()["available"]
    assert http.get(f"/api/pod/detail?cluster={AKS}&namespace=shop&pod=Bad_Name").status_code == 400
    assert http.get("/api/pod/detail?cluster=/subscriptions/s&namespace=shop&pod=x").status_code == 400
    page = http.get(f"/api/kube/yaml?cluster={AKS}&namespace=shop&kind=Pod&name=web-5d8f9c7b8d-abcde").get_json()
    assert "hunter2" not in page["yaml"]
    assert "hunter2" in http.get(f"/api/kube/yaml?cluster={AKS}&namespace=shop&kind=Pod&name=web-5d8f9c7b8d-abcde&values=1").get_json()["yaml"]
    assert http.get(f"/api/kube/yaml?cluster={AKS}&namespace=shop&kind=Secret&name=x").status_code == 400
