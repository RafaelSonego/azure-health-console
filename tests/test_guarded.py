"""Every outbound client shares one skeleton: its own allow-list is checked before anything is sent."""
import pytest
import requests

from healthconsole.core import acr, graph, guarded
from healthconsole.core.arm import ArmClient
from healthconsole.core.explore import kube
from healthconsole.core.integrations import newrelic, servicenow
from tests.conftest import FakeTokens

DIGEST = "sha256:" + "a" * 64


class Recording:
    """A session that records requests; `fail` makes every request raise it, as a dead network would."""

    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def request(self, method, url, **kwargs):
        self.sent.append((method, url))
        if self.fail:
            raise self.fail
        raise AssertionError("a refused request reached the network")


def _kube(session):
    client = kube.KubeClient.__new__(kube.KubeClient)
    client.server, client._token_source, client._session = "https://api.example", lambda: "t", session
    return client


def _kube_at(server):
    def make(session):
        client = _kube(session)
        client.server = server
        return client
    return make


def _servicenow(session):
    return servicenow.ServiceNowClient({"instance": "https://dev1.service-now.com", "tables": ["incident"], "auth": "basic",
                                        "username": "me", "name": "SN"}, session, typed={"password": "pw"})


def _newrelic(session):
    return newrelic.NewRelicClient({"name": "NR", "region": "us"}, session, typed={"api_key": "NRAK-x"})


REFUSED = {
    "ARM: a PUT": lambda s: ArmClient(FakeTokens(), session=s)._request(
        "PUT", "https://management.azure.com/subscriptions/x/resourceGroups/rg", use_cache=False),
    "ARM: listKeys": lambda s: ArmClient(FakeTokens(), session=s)._request(
        "POST", "https://management.azure.com/subscriptions/x/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/a/listKeys"),
    "Graph: the groups list": lambda s: graph._get(FakeTokens(), "/v1.0/groups", {}, s),
    "registry: a delete": lambda s: acr._send(s, "DELETE", f"https://shopregistry.azurecr.io/acr/v1/api/_manifests/{DIGEST}"),
    "registry: the catalog": lambda s: acr._send(s, "GET", "https://shopregistry.azurecr.io/acr/v1/_catalog"),
    "Kubernetes: secrets": lambda s: _kube(s).get("/api/v1/secrets"),
    "Kubernetes: exec": lambda s: _kube(s).get("/api/v1/namespaces/a/pods/b/exec"),
    "Kubernetes: a server over http (the token would travel in clear)": lambda s: _kube_at("http://api.example")(s).get("/api/v1/pods"),
    "ServiceNow: a table not chosen": lambda s: _servicenow(s).table("sys_user_has_password"),
    "New Relic: a mutation": lambda s: _newrelic(s).query("mutation { alertsPolicyDelete(id: 1) { id } }"),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_refused_request_never_leaves_the_machine(name):
    session = Recording()
    with pytest.raises(guarded.WriteBlocked):
        REFUSED[name](session)
    assert session.sent == []


UNREACHABLE = {
    "registry": (lambda s: acr._send(s, "POST", "https://shopregistry.azurecr.io/oauth2/exchange"), acr.RegistryError,
                 "firewall or private endpoint"),
    "Kubernetes": (lambda s: _kube(s).get("/api/v1/pods"), kube.KubeUnavailable, "VPN"),
    "Graph": (lambda s: graph._get(FakeTokens(), "/v1.0/me/transitiveMemberOf/microsoft.graph.group", {}, s),
              graph.ArmError, "couldn't be reached"),
    "ServiceNow": (lambda s: _servicenow(s).table("incident"), servicenow.ServiceNowError, "didn't answer"),
    "New Relic": (lambda s: _newrelic(s).query("{ actor { user { name } } }"), newrelic.NewRelicError, "didn't answer"),
}


@pytest.mark.parametrize("name", sorted(UNREACHABLE))
def test_an_unreachable_host_is_explained_in_that_clients_words(name):
    call, error_type, words = UNREACHABLE[name]
    with pytest.raises(error_type) as raised:
        call(Recording(fail=requests.ConnectionError("no route to host")))
    assert words in str(raised.value)


def test_every_client_is_a_guarded_client_and_check_is_required():
    for cls in (ArmClient, kube.KubeClient, acr._Registry, graph._Graph, servicenow.ServiceNowClient, newrelic.NewRelicClient):
        assert issubclass(cls, guarded.GuardedClient), cls
    for refusal in (servicenow.WriteBlocked, newrelic.WriteBlocked):
        assert issubclass(refusal, guarded.WriteBlocked)

    class Forgetful(guarded.GuardedClient):
        pass
    with pytest.raises(TypeError):
        Forgetful()  # like a Java class that doesn't implement the interface: it can't be built


def test_one_log_line_per_request(caplog):
    class Ok:
        status_code = 204

    class Client(guarded.GuardedClient):
        label = "TEST"

        def check(self, method, url, **request):
            pass

    with caplog.at_level("INFO", logger="healthconsole.calls"):
        Client().send(type("S", (), {"request": lambda *a, **k: Ok()})(), "GET", "https://x.example/a/b?token=secret", 5)
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert line.startswith("TEST GET /a/b status=204 ms=") and "secret" not in line  # the path only, never the query


# ---- redirects: followed only for reads, each hop through the client's own check

class Redirecting:
    def __init__(self, status, location=None):
        self.status_code, self.headers = status, ({"Location": location} if location else {})


class Routes:
    """Answers each URL from `routes`, recording what was asked (and that redirects were never left to Requests)."""

    def __init__(self, routes):
        self.routes, self.asked = routes, []

    def request(self, method, url, timeout=None, allow_redirects=True, **request):
        assert allow_redirects is False
        self.asked.append((method, url))
        return self.routes[url]


class OnlyGood(guarded.GuardedClient):
    label = "TEST"

    def check(self, method, url, **request):
        if not url.startswith("https://good.example/"):
            raise guarded.WriteBlocked(f"refused {url}")


def test_a_read_follows_a_redirect_the_client_allows():
    session = Routes({"https://good.example/a": Redirecting(302, "/b"), "https://good.example/b": Redirecting(200)})
    assert OnlyGood().send(session, "GET", "https://good.example/a", 5).status_code == 200
    assert session.asked == [("GET", "https://good.example/a"), ("GET", "https://good.example/b")]


def test_a_redirect_elsewhere_is_refused_before_anything_is_sent_there():
    session = Routes({"https://good.example/a": Redirecting(302, "http://169.254.169.254/latest/meta-data")})
    with pytest.raises(guarded.WriteBlocked, match="refused http://169.254.169.254"):
        OnlyGood().send(session, "GET", "https://good.example/a", 5)
    assert session.asked == [("GET", "https://good.example/a")]


def test_a_redirect_on_anything_but_a_read_is_not_followed():
    session = Routes({"https://good.example/token": Redirecting(307, "https://good.example/other")})
    with pytest.raises(guarded.WriteBlocked, match="not followed"):
        OnlyGood().send(session, "POST", "https://good.example/token", 5, data={"secret": "x"})
    assert session.asked == [("POST", "https://good.example/token")]


def test_redirect_loops_stop():
    session = Routes({"https://good.example/a": Redirecting(302, "/a")})
    with pytest.raises(guarded.WriteBlocked, match="more than"):
        OnlyGood().send(session, "GET", "https://good.example/a", 5)
    assert len(session.asked) == guarded.MAX_REDIRECTS + 1
