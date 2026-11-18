import pytest

from healthconsole.core.arm import (AccessDenied, ArmClient, ArmError, WriteBlocked,
                                    check_read_only, kql_list)
from healthconsole.core.credentials import Identity, identity_from_token
from tests.conftest import FakeTokens

ARM = "https://management.azure.com"


@pytest.mark.parametrize("method,url", [
    ("GET", f"{ARM}/subscriptions"),
    ("POST", f"{ARM}/providers/Microsoft.ResourceGraph/resources"),
    ("POST", f"{ARM}/subscriptions/x/providers/Microsoft.CostManagement/query"),
    ("POST", f"{ARM}/subscriptions/x/resourceGroups/rg/providers/Microsoft.CostManagement/forecast"),
])
def test_reads_are_allowed(method, url):
    check_read_only(method, url)


@pytest.mark.parametrize("method,url", [
    ("PUT", f"{ARM}/subscriptions/x/resourceGroups/rg"),
    ("PATCH", f"{ARM}/subscriptions/x/resourceGroups/rg"),
    ("DELETE", f"{ARM}/subscriptions/x/resourceGroups/rg"),
    ("POST", f"{ARM}/subscriptions/x/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/s/listKeys"),
    ("POST", f"{ARM}/subscriptions/x/resourceGroups/rg/providers/Microsoft.Web/sites/a/config/appsettings/list"),
    ("POST", f"{ARM}/subscriptions/x/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/c/runCommand"),
    ("POST", f"{ARM}/subscriptions/x/providers/Microsoft.CostManagement/exports/e/run"),
    ("POST", f"{ARM}/subscriptions/x/providers/Microsoft.CostManagement/scheduledActions/a/execute"),
    ("GET", "https://evil.example.com/subscriptions"),
    ("GET", "http://management.azure.com/subscriptions"),
])
def test_everything_else_is_blocked(method, url):
    with pytest.raises(WriteBlocked):
        check_read_only(method, url)


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = b"x"
        self.text = ""

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self.responses.pop(0)


def test_throttling_is_retried(monkeypatch):
    monkeypatch.setattr("healthconsole.core.arm.time.sleep", lambda s: None)
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "1"}),
                           FakeResponse(200, {"value": [1]})])
    client = ArmClient(FakeTokens(), session=session)
    assert client.get("/subscriptions", "2022-12-01") == {"value": [1]}
    assert len(session.calls) == 2


def test_403_is_access_denied():
    client = ArmClient(FakeTokens(), session=FakeSession([FakeResponse(403, {"error": {"message": "nope"}})]))
    with pytest.raises(AccessDenied, match="nope"):
        client.get("/subscriptions", "2022-12-01")


def test_next_link_to_another_host_is_blocked():
    session = FakeSession([FakeResponse(200, {"value": [1], "nextLink": "https://evil.example.com/x"})])
    with pytest.raises(WriteBlocked):
        ArmClient(FakeTokens(), session=session).get_paged("/subscriptions", "2022-12-01")


def test_cache_is_keyed_by_identity():
    class Switchable(FakeTokens):
        identity = Identity("oid-1", "a", "t")

    tokens = Switchable()
    session = FakeSession([FakeResponse(200, {"who": "a"}), FakeResponse(200, {"who": "b"})])
    client = ArmClient(tokens, session=session)
    assert client.get("/subscriptions", "v")["who"] == "a"
    assert client.get("/subscriptions", "v")["who"] == "a"
    tokens.identity = Identity("oid-2", "b", "t")
    assert client.get("/subscriptions", "v")["who"] == "b"
    assert len(session.calls) == 2


def test_one_path_in_two_cases_is_one_request_but_paging_tokens_keep_their_case():
    session = FakeSession([FakeResponse(200, {"value": [1]}), FakeResponse(200, {"value": ["A"]}),
                           FakeResponse(200, {"value": ["a"]})])
    client = ArmClient(FakeTokens(), session=session)
    vault = "/subscriptions/x/resourceGroups/RG/providers/Microsoft.KeyVault/vaults/KV/secrets"
    assert client.get(vault, "v") == client.get(vault.lower(), "v") == {"value": [1]}
    assert len(session.calls) == 1  # the health check and the Details panel no longer list a vault twice
    assert client.get(vault, "v", params={"$skiptoken": "Ab"}) != client.get(vault, "v", params={"$skiptoken": "aB"})
    assert len(session.calls) == 3


def test_resource_graph_pages_follow_skip_token():
    session = FakeSession([FakeResponse(200, {"data": [{"a": 1}], "$skipToken": "next"}),
                           FakeResponse(200, {"data": [{"a": 2}]})])
    rows = ArmClient(FakeTokens(), session=session).query("Resources")
    assert rows == [{"a": 1}, {"a": 2}]
    assert all(method == "POST" for method, _ in session.calls)


def test_resource_graph_stops_paging_at_the_limit():
    session = FakeSession([FakeResponse(200, {"data": [{"a": 1}, {"a": 2}], "$skipToken": "next"}),
                           FakeResponse(200, {"data": [{"a": 3}]})])
    assert ArmClient(FakeTokens(), session=session).query("Resources", limit=2) == [{"a": 1}, {"a": 2}]
    assert len(session.calls) == 1


def test_a_take_that_stops_paging_is_an_error_not_a_silent_cut(monkeypatch):
    from healthconsole.core import arm
    monkeypatch.setattr(arm, "GRAPH_PAGE_SIZE", 2)
    session = FakeSession([FakeResponse(200, {"data": [{"a": 1}, {"a": 2}], "resultTruncated": "true"})])
    with pytest.raises(ArmError, match="blocks paging"):
        ArmClient(FakeTokens(), session=session).query("Resources | take 5000")


def test_kql_list_escapes_quotes():
    assert kql_list(['a"b']) == 'dynamic(["a\\"b"])'


def test_identity_from_token_reads_claims():
    import base64
    import json
    claims = base64.urlsafe_b64encode(json.dumps({"oid": "o", "upn": "u@x", "tid": "t"}).encode()).decode().rstrip("=")
    assert identity_from_token(f"h.{claims}.s") == Identity("o", "u@x", "t")
    assert identity_from_token("garbage").name == "unknown"


def test_a_personal_or_guest_account_is_named_by_its_address_not_its_provider():
    import base64
    import json

    def token(claims):
        return "h." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".s"

    personal = {"oid": "o", "tid": "t", "unique_name": "live.com#me@gmail.com", "email": "me@gmail.com"}
    assert identity_from_token(token(personal)).name == "me@gmail.com"
    assert identity_from_token(token({**personal, "email": None})).name == "me@gmail.com"  # the prefix is dropped
    assert identity_from_token(token({**personal, "upn": "me@work.example"})).name == "me@work.example"  # upn wins


def test_every_request_is_logged_without_tokens(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="healthconsole.calls")
    session = FakeSession([FakeResponse(200, {"value": []})])
    ArmClient(FakeTokens(), session=session).get("/subscriptions", "2022-12-01")
    assert "ARM GET /subscriptions api-version=2022-12-01" in caplog.text
    assert "token" not in caplog.text.lower().replace("tokens", "")


def test_timeout_becomes_a_reported_error():
    import requests
    from healthconsole.core.arm import ArmError, ArmTimeout

    class Hanging:
        def request(self, *args, **kwargs):
            assert kwargs["timeout"] == 10
            raise requests.ReadTimeout("read timed out")

    with pytest.raises(ArmTimeout) as caught:
        ArmClient(FakeTokens(), session=Hanging()).get("/subscriptions/s/x/detectors", "v", timeout=10)
    assert isinstance(caught.value, ArmError)
    assert "didn't answer within 10s" in str(caught.value) and "/detectors" in str(caught.value)


def test_connection_pool_reuses_and_caps_connections():
    from healthconsole.core.arm import MAX_CONNECTIONS
    adapter = ArmClient(FakeTokens())._session.get_adapter(ARM)
    assert adapter._pool_maxsize == MAX_CONNECTIONS == 32
    assert adapter._pool_block is True


def test_a_bound_client_stops_once_its_job_is_cancelled():
    import threading
    from healthconsole.core.arm import Cancelled
    session = FakeSession([FakeResponse(200, {"value": []}), FakeResponse(200, {"value": []})])
    client = ArmClient(FakeTokens(), session=session)
    cancel = threading.Event()
    bound = client.bound(cancel)
    bound.get("/subscriptions", "2022-12-01")
    assert bound.call_count == 1 and client.call_count == 0
    cancel.set()
    with pytest.raises(Cancelled):
        bound.get("/subscriptions/x", "2022-12-01")
    client.get("/subscriptions/x", "2022-12-01")  # the shared client is unaffected
    assert len(session.calls) == 2


def test_each_call_logs_status_and_duration(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="healthconsole.calls")
    ArmClient(FakeTokens(), session=FakeSession([FakeResponse(200, {})])).get("/subscriptions", "2022-12-01")
    assert "status=200 ms=" in caplog.text


def test_cli_sign_in_failures_are_classified():
    from healthconsole.core.credentials import AzureCliTokenProvider, CredentialError

    class Failing:
        def __init__(self, text):
            self.text = text

        def get_token(self, scope):
            raise RuntimeError(self.text)

    cases = [("Please run 'az login' to set up an account", None, "signed_out", "az login"),
             ("Azure CLI not found on path", None, "cli_missing", "az login"),
             ("AADSTS90002: Tenant 'x' not found", "x", "tenant", "az login --tenant x")]
    for text, tenant, reason, command in cases:
        with pytest.raises(CredentialError) as caught:
            AzureCliTokenProvider(tenant_id=tenant, credential=Failing(text)).get_token()
        assert (caught.value.reason, caught.value.command) == (reason, command)


def test_signed_out_api_answers_401_with_what_to_run(tmp_path):
    from flask import Flask
    from healthconsole.core import config
    from healthconsole.core.credentials import CredentialError
    from healthconsole.web import create_blueprint

    def signed_out(tenant):
        raise CredentialError("The Azure CLI couldn't provide a token.", "signed_out", "az login")

    app = Flask("test")
    app.register_blueprint(create_blueprint(signed_out, config.load(tmp_path / "none.yaml"), config_file=tmp_path / "c.yaml"))
    response = app.test_client().get("/api/context?verify=1")
    assert response.status_code == 401
    assert response.get_json()["kind"] == "credential" and response.get_json()["command"] == "az login"


def test_dropped_connections_are_retried_then_reported(monkeypatch):
    import requests

    from healthconsole.core import arm
    from tests.conftest import FakeTokens

    monkeypatch.setattr(arm.time, "sleep", lambda s: None)

    class Response:
        status_code, ok, content = 200, True, b"{}"

        def json(self):
            return {"value": [1]}

    class Session:
        def __init__(self, failures, error=requests.ConnectionError):
            self.failures, self.error, self.calls = failures, error, 0

        def request(self, *a, **k):
            self.calls += 1
            if self.calls <= self.failures:
                raise self.error("connection reset")
            return Response()

    flaky = Session(2)
    assert arm.ArmClient(FakeTokens(), session=flaky).get("/subscriptions/x/a", "1") == {"value": [1]}
    assert flaky.calls == 3
    dead = Session(9)
    with pytest.raises(arm.ArmTimeout, match="failed 3 times"):
        arm.ArmClient(FakeTokens(), session=dead).get("/subscriptions/x/b", "1")
    slow = Session(9, requests.ReadTimeout)
    with pytest.raises(arm.ArmTimeout, match="didn't answer within"):
        arm.ArmClient(FakeTokens(), session=slow).get("/subscriptions/x/c", "1")
    assert slow.calls == 1


def test_keyvault_listing_retries_a_timeout_once():
    from healthconsole.core.arm import ArmTimeout
    from healthconsole.core.explore import keyvault
    from tests.conftest import FakeClient

    calls = {"secrets": 0}

    def secrets(params):
        calls["secrets"] += 1
        if calls["secrets"] == 1:
            raise ArmTimeout("slow")
        return {"value": []}

    result = keyvault.expiry(FakeClient(gets={"/secrets": secrets, "/keys": {"value": []}}), "/subscriptions/x/v")
    assert calls["secrets"] == 2 and result["errors"] == []


def test_a_vaults_own_page_lists_at_most_its_cap_and_says_so():
    from healthconsole.core.explore import keyvault
    from tests.conftest import FakeClient

    class Paged(FakeClient):
        def get_pages(self, path, api_version, params=None, max_pages=10, timeout=None):
            self.asked = max_pages
            return ([{"name": f"s{i}", "properties": {"attributes": {"exp": None}}} for i in range(max_pages * 25)], True)

    client = Paged()
    capped = keyvault.expiry(client, "/subscriptions/x/v", max_pages=keyvault.PAGE_MAX_PAGES)
    assert client.asked == keyvault.PAGE_MAX_PAGES and len(capped["secrets"]) == 1000
    assert any("Only the first 1,000 secrets" in e for e in capped["errors"])
    full = keyvault.expiry(FakeClient(gets={"/secrets": {"value": []}, "/keys": {"value": []}}), "/subscriptions/x/v")
    assert full["errors"] == []  # the Secrets view's scan: no cap, no note


def test_an_html_error_page_is_named_by_its_title_not_shown():
    from healthconsole.core.arm import _error_message

    class Page:
        text = "<!DOCTYPE html>\n<html><head><title>Web App -\n Unavailable</title><style>x</style></head><body>…</body></html>"

        def json(self):
            raise ValueError
    assert _error_message(Page()) == "an error page instead of an answer (Web App - Unavailable)"
    Page.text = "<html><body>down</body></html>"
    assert _error_message(Page()) == "an error page instead of an answer"


def test_error_message_reads_any_json_shape():
    from healthconsole.core.arm import _error_message

    class Response:
        def __init__(self, payload, text=""):
            self.payload, self.text = payload, text

        def json(self):
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload

    assert _error_message(Response("Detector failed to run")) == "Detector failed to run"
    assert _error_message(Response(["a", "b"])) == "['a', 'b']"
    assert _error_message(Response({"error": "busy"})) == "busy"
    assert _error_message(Response({"error": {"message": "Bad", "details": ["x", {"message": "Why"}]}})) == "Bad | Why"
    assert _error_message(Response(ValueError(), "<html>")) == "an error page instead of an answer"
    assert _error_message(Response(ValueError(), "plain text")) == "plain text"


def test_a_fresh_client_asks_azure_again_and_caches_the_new_answer():
    session = FakeSession([FakeResponse(200, {"n": 1}), FakeResponse(200, {"n": 2})])
    client = ArmClient(FakeTokens(), session=session)
    assert client.get("/subscriptions", "v")["n"] == 1
    assert client.fresh().get("/subscriptions", "v")["n"] == 2
    assert client.get("/subscriptions", "v")["n"] == 2 and len(session.calls) == 2


def test_fresh_on_a_request_gives_the_route_a_fresh_client(tmp_path):
    from flask import Flask
    from healthconsole.core import config
    from healthconsole.web import create_blueprint

    session = FakeSession([FakeResponse(200, {"value": []}) for _ in range(3)])
    client = ArmClient(FakeTokens(), session=session)
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load(tmp_path / "none.yaml"), config_file=tmp_path / "c.yaml"))
    web = app.test_client()
    for query in ("", "", "?fresh=1"):
        assert web.get(f"/api/subscriptions{query}").status_code == 200
    assert len(session.calls) == 2


def test_cost_managements_401_for_no_role_is_access_denied_not_a_bad_token():
    response = FakeResponse(401, {"error": {"code": "RBACAccessDenied", "message": "no"}})
    response.text = '{"error":{"code":"RBACAccessDenied"}}'
    client = ArmClient(FakeTokens(), session=FakeSession([response]))
    with pytest.raises(AccessDenied) as raised:
        client.post("/subscriptions/x/providers/Microsoft.CostManagement/query", "v", {})
    assert "Cost Management Reader" in str(raised.value)


def test_cost_managements_own_retry_after_header_is_used():
    from healthconsole.core.arm import _retry_after
    assert _retry_after(FakeResponse(429, headers={"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "21"})) == 21


def test_a_refusal_is_remembered_until_refresh_access_or_a_refresh():
    def denied():
        return FakeResponse(403, {"error": {"message": "no role"}})
    session = FakeSession([denied(), denied(), denied()])
    client = ArmClient(FakeTokens(), session=session)
    for _ in range(2):
        with pytest.raises(AccessDenied, match="no role"):
            client.get("/subscriptions/x/resourceGroups/rg", "v")
    assert len(session.calls) == 1  # the second answer came from memory
    with pytest.raises(AccessDenied):
        client.fresh().get("/subscriptions/x/resourceGroups/rg", "v")
    client.refresh()
    with pytest.raises(AccessDenied):
        client.get("/subscriptions/x/resourceGroups/rg", "v")
    assert len(session.calls) == 3


def test_a_tolerated_refusal_is_not_remembered():
    session = FakeSession([FakeResponse(403), FakeResponse(200, {"ok": 1})])
    client = ArmClient(FakeTokens(), session=session)
    assert client.get("/subscriptions/x/providers/p", "v", tolerate=(403,)) is None
    assert client.get("/subscriptions/x/providers/p", "v", tolerate=(403,)) == {"ok": 1}


def test_a_page_read_stops_waiting_on_throttling_but_a_job_waits(monkeypatch):
    import threading
    from healthconsole.core.arm import Throttled
    slept = []
    monkeypatch.setattr("healthconsole.core.arm.time.sleep", slept.append)
    busy = lambda: FakeResponse(429, headers={"Retry-After": "15"})  # noqa: E731
    page = ArmClient(FakeTokens(), session=FakeSession([busy(), busy(), busy()]))
    with pytest.raises(Throttled) as raised:
        page.get("/subscriptions", "v")
    assert slept == [15] and raised.value.retry_after == 15 and "Try again in 15 s" in str(raised.value)
    slept.clear()
    job = ArmClient(FakeTokens(), session=FakeSession([busy(), busy(), FakeResponse(200, {"ok": 1})])).bound(threading.Event())
    assert job.get("/subscriptions", "v") == {"ok": 1} and slept == [15, 15]


def test_throttling_reaches_the_page_as_its_own_kind(tmp_path):
    from flask import Flask
    from healthconsole.core import config
    from healthconsole.core.arm import Throttled
    from healthconsole.web import create_blueprint

    class Busy:
        identity = FakeTokens.identity

        def get_paged(self, *a, **k):
            raise Throttled(12)
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda t: Busy(), config.load(tmp_path / "n.yaml"), config_file=tmp_path / "c.yaml"))
    answer = app.test_client().get("/api/subscriptions")
    assert answer.status_code == 429 and answer.get_json()["kind"] == "throttled" and answer.get_json()["retry_after"] == 12
