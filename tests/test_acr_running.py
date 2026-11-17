import pytest
import requests
from flask import Flask

from healthconsole.core import acr, config
from healthconsole.core import vulnerabilities as v
from healthconsole.core.arm import WriteBlocked
from tests.conftest import SUB, FakeClient, FakeTokens, rid

DIGEST = "sha256:" + "a" * 64
LOGIN = "shopregistry.azurecr.io"
REGISTRY = rid("acr-rg", "Microsoft.ContainerRegistry/registries", "shopregistry")
AKS = rid("aks-rg", "Microsoft.ContainerService/managedClusters", "shop")


def test_registry_guard():
    acr.check_registry_request("POST", f"https://{LOGIN}/oauth2/exchange")
    acr.check_registry_request("POST", f"https://{LOGIN}/oauth2/token")
    acr.check_registry_request("GET", f"https://{LOGIN}/acr/v1/team/api/_manifests/{DIGEST}")
    for method, url in (("GET", f"https://evil.example/acr/v1/x/_manifests/{DIGEST}"),
                        ("DELETE", f"https://{LOGIN}/acr/v1/x/_manifests/{DIGEST}"),
                        ("GET", f"https://{LOGIN}/v2/x/blobs/{DIGEST}"),
                        ("GET", f"https://{LOGIN}/acr/v1/_catalog"),
                        ("POST", f"https://{LOGIN}/acr/v1/x/_tags"),
                        ("GET", f"http://{LOGIN}/oauth2/token")):
        with pytest.raises(WriteBlocked):
            acr.check_registry_request(method, url)


class Response:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self.payload, self.text = status, payload or {}, text

    def json(self):
        return self.payload


class Session:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_tags_flow_and_cache():
    acr.forget()
    session = Session([Response(200, {"refresh_token": "r"}), Response(200, {"access_token": "a"}),
                       Response(200, {"manifest": {"tags": ["latest", "1.4.2"], "createdTime": "2026-10-01T00:00:00Z"}})])
    out = acr.tags(FakeTokens(), LOGIN, "team/api", DIGEST, "tenant-1", session)
    assert out["tags"] == ["1.4.2", "latest"]
    assert session.calls[1][2]["data"]["scope"] == "repository:team/api:metadata_read"
    assert session.calls[2][2]["headers"]["Authorization"] == "Bearer a"
    assert acr.tags(FakeTokens(), LOGIN, "team/api", DIGEST, "tenant-1", Session([])) == out  # cached
    acr.forget()


@pytest.mark.parametrize("answers, kind, text", [
    ([Response(401)], "access", "AcrPull"),
    ([Response(200, {"refresh_token": "r"}), Response(403)], "access", "PIM"),
    ([requests.ConnectionError("no route")], "network", "firewall or private endpoint"),
    ([Response(403, text='{"errors":[{"code":"DENIED","message":"client with IP \'203.0.113.9\' is not allowed access. '
                         'Refer https://aka.ms/acr/firewall"}]}')], "network", "a role won't change this"),
    ([Response(200, {"refresh_token": "r"}), Response(200, {"access_token": "a"}), Response(404)], "missing", "deleted"),
])
def test_tags_refusals_say_why(answers, kind, text):
    acr.forget()
    with pytest.raises(acr.RegistryError) as raised:
        acr.tags(FakeTokens(), LOGIN, "team/api", DIGEST, "tenant-1", Session(answers))
    assert raised.value.kind == kind and text in str(raised.value)
    with pytest.raises(acr.RegistryError):
        acr.tags(FakeTokens(), "evil.example", "team/api", DIGEST, "tenant-1", Session([]))


def test_image_refs_and_running_images():
    image = v._image_ref({"childKind": ".containerimage", "child": "team/api",
                          "rid": f"{REGISTRY}/securityEntityData/repositories-team-api-images-{DIGEST}"})
    assert image == {"repository": "team/api", "digest": DIGEST}
    native = "/x/namespace-payments-toplevelworkloadkind-replicaset-pod-api-7d9f-container-api-"
    ref = v._image_ref({"childKind": "K8s-container", "native": native})
    assert ref == {"namespace": "payments", "kind": "replicaset", "pod": "api-7d9f", "container": "api"}

    class Kube:
        def __init__(self):
            self.paths = []

        def items(self, path):
            self.paths.append(path)
            return [{"metadata": {"name": "other-1"}, "spec": {"containers": [{"name": "api", "image": "repo/api:1.3"}]}},
                    {"metadata": {"name": "api-7d9f-xyz"}, "spec": {"containers": [{"name": "api", "image": "repo/api:1.4.2"}]},
                     "status": {"containerStatuses": [{"name": "api", "imageID": "repo/api@" + DIGEST}]}}]

    kube = Kube()
    out = v.running_images(kube, [ref, {**ref, "container": "gone"}])
    assert [c["image"] for c in out["payments/api"]] == ["repo/api:1.4.2", "repo/api:1.3"]  # Defender's pod first
    assert out["payments/gone"] == [] and kube.paths == ["/api/v1/namespaces/payments/pods"]  # one read per namespace


def test_routes_answer_refusals(monkeypatch):
    from healthconsole.core.explore import kube
    from healthconsole.web import create_blueprint

    def handler(kql):
        if "loginServer" in kql:
            return [{"loginServer": LOGIN, "pna": "Disabled"}]
        if "where parent ==" in kql:
            return [{"title": "t", "software": "openssl", "severity": "High", "cvss": 9.8, "fixed": "", "hasFix": True,
                     "child": "api", "childKind": "K8s-container", "rid": AKS, "cves": "[]",
                     "native": "/x/namespace-payments-toplevelworkloadkind-replicaset-pod-api-7d9f-container-api-"}]
        return []

    client = FakeClient(query_handlers=[("", handler)])
    client.token_provider = FakeTokens()
    monkeypatch.setattr(acr, "tags", lambda *a, **k: (_ for _ in ()).throw(acr.RegistryError("can't be reached", "network")))
    monkeypatch.setattr(kube, "client_for", lambda *a, **k: (_ for _ in ()).throw(
        kube.KubeUnavailable("Couldn't read the Kubernetes API (HTTP 403)")))
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load("/nonexistent")))
    http = app.test_client()
    tags = http.get(f"/api/vulnerabilities/image-tags?registry={REGISTRY}&repository=team/api&digest={DIGEST}").get_json()
    assert tags["kind"] == "network" and "public network access is off" in tags["error"]
    running = http.get(f"/api/vulnerabilities/running?id={AKS}").get_json()
    assert running["kind"] == "access" and "Kubernetes" in running["required"] and running["cluster_id"] == AKS
