import pytest

from healthconsole.core import config, integrations, settings_file
from healthconsole.core.integrations import newrelic, secrets, servicenow

PDI = "https://dev1.service-now.com"


class MemoryKeyring:
    """Stands in for the OS keychain, so tests never touch the real one."""

    name = "test keychain"

    def __init__(self):
        self.items = {}

    def get_keyring(self):
        return self

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        self.items[(service, account)] = value

    def delete_password(self, service, account):
        self.items.pop((service, account), None)


@pytest.fixture
def keychain(monkeypatch):
    store = MemoryKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: store)
    for name in ("AHC_SERVICENOW_PASSWORD", "AHC_SERVICENOW_INSTANCE", "AHC_MONITOR_NEW_RELIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return store


@pytest.fixture(autouse=True)
def no_remembered_hidden_tables():
    servicenow.forget_hidden()
    yield
    servicenow.forget_hidden()


def sn_cfg(**overrides):
    cfg = dict(config.SERVICENOW_DEFAULTS)
    cfg.update({"enabled": True, "instance": PDI, "username": "admin", **overrides})
    return cfg


class Response:
    def __init__(self, status, payload=None):
        self.status_code, self._payload, self.reason, self.text = status, payload or {}, "", ""

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


class Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return next(r for marker, r in self.routes if marker in url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return next(r for marker, r in self.routes if marker in url)

    def request(self, method, url, **kwargs):  # what the guarded clients call
        return self.get(url, **kwargs) if method == "GET" else self.post(url, **kwargs)


# ------------------------------------------------------------------ settings


def test_integration_settings_are_validated():
    settings = config.from_data({"integrations": {"servicenow": {"enabled": True, "instance": PDI},
                                                  "monitors": [{"name": "NR", "type": "newrelic", "account_ids": [1]}]}})
    only = settings["integrations"]["servicenow"]  # one block, as saved before: one connection called ServiceNow
    assert [c["name"] for c in only] == ["ServiceNow"] and only[0]["tables"] == list(config.SERVICENOW_TABLES)
    assert settings["integrations"]["monitors"][0]["enabled"] is False
    two = config.from_data({"integrations": {"servicenow": [{"name": "SN Prod", "instance": PDI}, {"name": "SN NonProd"}]}})
    assert [c["name"] for c in two["integrations"]["servicenow"]] == ["SN Prod", "SN NonProd"]
    bad = [{"servicenow": {"instance": "http://x"}}, {"servicenow": [{"name": "a", "auth": "magic"}]},
           {"servicenow": [{"name": "a", "password": "x"}]}, {"servicenow": [{"name": "a"}, {"name": "A"}]},
           {"servicenow": [{"name": ""}]},
           {"monitors": [{"name": "a", "type": "splunk"}]}, {"monitors": [{"name": "a", "type": "newrelic", "site": "x"}]},
           {"monitors": [{"name": "a", "type": "newrelic"}, {"name": "A", "type": "datadog"}]}]
    for data in bad:
        with pytest.raises(config.ConfigError):
            config.from_data({"integrations": data})


def test_saving_integrations_never_writes_a_secret(tmp_path):
    file = tmp_path / "config.yaml"
    file.write_text("# mine\ntriage:\n  scope: all\n")
    settings = settings_file.apply_integrations(
        file, "contoso.example", servicenow=[{"name": "SN", "enabled": True, "instance": PDI, "username": "me"}],
        monitors=[{"name": "NR", "type": "newrelic", "enabled": True, "account_ids": [5]}])
    text = file.read_text()
    assert "# mine" in text and "dev1.service-now.com" in text and "account_ids" in text
    assert settings["tenants"]["contoso.example"]["integrations"]["servicenow"][0]["instance"] == PDI
    with pytest.raises(config.ConfigError):
        settings_file.apply_integrations(file, "contoso.example", servicenow=[{"name": "SN", "password": "secret"}])
    with pytest.raises(config.ConfigError):
        settings_file.apply_integrations(file, "*", servicenow=[{"name": "SN", "enabled": True}])
    assert "secret" not in file.read_text()


WORK = {"id": "tenant-1", "domain": "contoso.example", "name": "Contoso"}
HOME = {"id": "tenant-2", "domain": "me.example", "name": "Me"}


def test_each_tenant_has_its_own_integrations_and_none_borrow_another():
    settings = config.from_data({"tenants": {
        "contoso.example": {"integrations": {"servicenow": {"enabled": True, "instance": "https://work.service-now.com"}}},
        "tenant-2": {"integrations": {"monitors": [{"name": "New Relic", "type": "newrelic", "account_ids": [2]}]}},
        "*": {"integrations": {"servicenow": {"enabled": True, "instance": "https://leak.service-now.com"}}}}})
    key, work = config.tenant_integrations(settings, WORK)
    assert key == "contoso.example" and work["servicenow"][0]["instance"] == "https://work.service-now.com"
    assert config.tenant_integrations(settings, HOME)[1]["servicenow"] == []  # "*" is never used
    key, other = config.tenant_integrations(settings, {"id": "tenant-3"})
    assert key is None and other == {"servicenow": [], "monitors": []}
    assert config.tenant_key(settings, WORK) == "contoso.example" and config.tenant_key(settings, {"id": "t9"}) == "t9"
    with pytest.raises(config.ConfigError):
        config.from_data({"tenants": {"x": {"integrations": {"servicenow": {"instance": "http://plain"}}}}})


def test_monitor_keys_are_kept_per_tenant(keychain):
    nr = {"name": "New Relic", "type": "newrelic", "enabled": True, "account_ids": [1]}
    work, home = integrations.bind({"servicenow": {}, "monitors": [nr]}, "tenant-1"), \
        integrations.bind({"servicenow": {}, "monitors": [nr]}, "tenant-2")
    target, scope, _ = integrations.secret_target(work, "monitor:New Relic")
    secrets.put(target, scope, "api_key", "NRAK-work")
    assert integrations.monitor_status(work["monitors"][0])["ready"]
    assert not integrations.monitor_status(home["monitors"][0])["ready"]  # same name, other tenant: no key


def test_global_integrations_move_into_a_tenant_with_their_keys(tmp_path, keychain):
    file = tmp_path / "config.yaml"
    file.write_text("integrations:\n  servicenow:\n    enabled: true\n    instance: https://dev1.service-now.com\n"
                    "  monitors:\n    - name: New Relic\n      type: newrelic\n      account_ids: [1]\n")
    settings = config.load(file)
    assert config.legacy_integrations(settings)
    secrets.put("monitor:New Relic", "newrelic", "api_key", "NRAK-old")
    integrations.move_monitor_secrets(settings["integrations"]["monitors"], "tenant-1")
    moved = settings_file.move_legacy_integrations(file, "contoso.example")
    assert config.legacy_integrations(moved) is None and "integrations:" in file.read_text()
    assert config.tenant_integrations(moved, WORK)[1]["servicenow"][0]["instance"] == PDI
    assert secrets.get("monitor:New Relic", "tenant-1", "api_key") == "NRAK-old"
    assert secrets.get("monitor:New Relic", "newrelic", "api_key") is None
    with pytest.raises(config.ConfigError):
        settings_file.move_legacy_integrations(file, "contoso.example")


# ------------------------------------------------------------------ secrets and status


def test_secrets_go_to_the_keychain_per_instance(keychain, monkeypatch):
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw1")
    assert secrets.get("servicenow", "dev1.service-now.com", "password") == "pw1"
    assert secrets.get("servicenow", "work.service-now.com", "password") is None
    monkeypatch.setenv("AHC_SERVICENOW_PASSWORD", "from-env")
    assert secrets.get("servicenow", "work.service-now.com", "password") is None  # no instance named: not sent
    monkeypatch.setenv("AHC_SERVICENOW_INSTANCE", "work")
    assert secrets.source("servicenow", "work.service-now.com", "password") == "environment"
    assert secrets.get("servicenow", "dev1.service-now.com", "password") == "pw1"  # another instance: its own


def test_an_environment_secret_goes_only_to_the_instance_it_names(keychain, monkeypatch):
    monkeypatch.setenv("AHC_SERVICENOW_PASSWORD", "env-pw")
    monkeypatch.setenv("AHC_SERVICENOW_INSTANCE", "https://dev1.service-now.com/nav_to.do")
    assert servicenow.ServiceNowClient(sn_cfg(instance="https://dev1.service-now.com")).secret("password") == "env-pw"
    for elsewhere in ("https://attacker.example", "https://dev1.service-now.com.attacker.example", "dev2"):
        assert servicenow.ServiceNowClient(sn_cfg(instance=elsewhere)).secret("password") is None
    moved = integrations.servicenow_status(sn_cfg(instance="https://attacker.example"))
    assert not moved["ready"] and any("AHC_SERVICENOW_INSTANCE=attacker.example" in p for p in moved["problems"])
    assert "env-pw" not in str(moved)


def test_status_says_what_is_missing(keychain):
    st = integrations.servicenow_status(sn_cfg(username=""))
    assert not st["ready"]
    assert any("username" in p for p in st["problems"]) and any("No password yet" in p for p in st["problems"])
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw")
    assert integrations.servicenow_status(sn_cfg())["ready"]
    browser = integrations.servicenow_status(sn_cfg(auth="oauth_browser"))
    assert any("client ID" in p for p in browser["problems"]) and any("Not signed in" in p for p in browser["problems"])
    nr = integrations.monitor_status({"name": "New Relic", "type": "newrelic", "enabled": True})
    assert any("account IDs" in p for p in nr["problems"]) and any("API key" in p for p in nr["problems"])
    dd = integrations.monitor_status({"name": "DD", "type": "datadog", "enabled": True, "site": "datadoghq.com"})
    assert any("later version" in p for p in dd["problems"])


def test_no_keychain_points_to_environment_variables(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    assert not secrets.keychain()["available"]
    with pytest.raises(secrets.SecretStoreError, match="AHC_SERVICENOW_PASSWORD"):
        secrets.put("servicenow", "dev1.service-now.com", "password", "x")


# ------------------------------------------------------------------ read-only guards


@pytest.mark.parametrize("method,url,ok", [
    ("GET", f"{PDI}/api/now/table/incident", True),
    ("GET", f"{PDI}/api/now/table/sys_user", True),
    ("POST", f"{PDI}/oauth_token.do", True),
    ("POST", f"{PDI}/api/now/table/incident", False),
    ("PATCH", f"{PDI}/api/now/table/incident/abc", False),
    ("GET", f"{PDI}/api/now/table/sys_user_password", False),
    ("GET", "https://evil.example.com/api/now/table/incident", False),
    ("GET", "http://dev1.service-now.com/api/now/table/incident", False),
])
def test_servicenow_guard(method, url, ok):
    check = lambda: servicenow.check_read_only(method, url, "dev1.service-now.com", ["incident"])  # noqa: E731
    if ok:
        check()
    else:
        with pytest.raises(servicenow.WriteBlocked):
            check()


@pytest.mark.parametrize("query,ok", [
    ("{ actor { user { name } } }", True),
    ("query Issues { actor { accounts { id } } }", True),
    ('{ actor { entitySearch(query: "name = \'mutation\'") { count } } }', True),
    ("# mutation in a comment\n{ actor { user { name } } }", True),
    ("mutation { alertsPolicyDelete(accountId: 1, id: 2) { id } }", False),
    ("query A { actor { user { name } } } mutation B { x }", False),
    ("subscription { x }", False),
])
def test_newrelic_guard(query, ok):
    check = lambda: newrelic.check_read_only("POST", newrelic.ENDPOINTS["us"], {"query": query})  # noqa: E731
    if ok:
        check()
    else:
        with pytest.raises(newrelic.WriteBlocked):
            check()


def test_newrelic_guard_refuses_other_hosts_and_methods():
    with pytest.raises(newrelic.WriteBlocked):
        newrelic.check_read_only("POST", "https://evil.example.com/graphql", {"query": "{ x }"})
    with pytest.raises(newrelic.WriteBlocked):
        newrelic.check_read_only("GET", newrelic.ENDPOINTS["us"], {"query": "{ x }"})


# ------------------------------------------------------------------ clients


def test_servicenow_test_reports_each_table(keychain):
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw")
    session = Session([("sys_user", Response(200, {"result": [{"name": {"display_value": "Ada Admin"}}]})),
                       ("/incident", Response(200, {"result": []})),
                       ("/change_request", Response(403)),
                       ("/task_ci", Response(200, {"result": []})), ("/change_task", Response(200, {"result": []})),
                       ("/em_alert", Response(400, {"error": {"message": "Invalid table em_alert"}})),
                       ("/em_event", Response(400, {"error": {"message": "Invalid table em_event"}}))])
    result = servicenow.ServiceNowClient(sn_cfg(), session).test()
    by_name = {t["name"]: t for t in result["tables"]}
    assert result["who"] == "Ada Admin" and result["ok"]
    assert by_name["incident"]["ok"] and "needs a role" in by_name["change_request"]["detail"]
    assert "isn't available" in by_name["em_alert"]["detail"]
    assert all(call[0] == "GET" and call[2]["auth"] == ("admin", "pw") for call in session.calls)


def test_servicenow_client_credentials_sign_in(keychain):
    secrets.put("servicenow", "dev1.service-now.com", "client_secret", "s3cret")
    session = Session([("oauth_token.do", Response(200, {"access_token": "tok", "expires_in": 1800})),
                       ("/incident", Response(200, {"result": [{"number": "INC1"}]}))])
    client = servicenow.ServiceNowClient(sn_cfg(auth="oauth_client", client_id="cid", tables=["incident"]), session)
    client.forget_token()
    assert client.table("incident") == [{"number": "INC1"}]
    token_call = session.calls[0]
    assert token_call[0] == "POST" and token_call[2]["data"]["grant_type"] == "client_credentials"
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer tok"


def test_browser_sign_in_keeps_the_refresh_token(keychain):
    cfg = sn_cfg(auth="oauth_browser", client_id="cid")
    url, state, verifier = servicenow.start_browser_sign_in(cfg, "http://127.0.0.1:8765/integrations/servicenow/callback")
    assert url.startswith(f"{PDI}/oauth_auth.do?") and "code_challenge_method=S256" in url and state in url
    session = Session([("oauth_token.do", Response(200, {"access_token": "a", "refresh_token": "r1"}))])
    servicenow.finish_browser_sign_in(cfg, "code1", "http://127.0.0.1:8765/cb", verifier, session)
    assert secrets.get("servicenow", "dev1.service-now.com", "refresh_token") == "r1"
    assert session.calls[0][2]["data"]["code_verifier"] == verifier


def test_newrelic_test_checks_account_ids(keychain):
    secrets.put("monitor:New Relic", "newrelic", "api_key", "NRAK-x")
    session = Session([("graphql", Response(200, {"data": {"actor": {"user": {"name": "Ada"},
                                                                     "accounts": [{"id": 1, "name": "Prod"}]}}}))])
    result = newrelic.NewRelicClient({"name": "New Relic", "type": "newrelic", "account_ids": [1, 2]}, session).test()
    assert result["who"] == "Ada" and not result["ok"]
    assert [a["ok"] for a in result["accounts"]] == [True, False]
    assert session.calls[0][2]["headers"]["API-Key"] == "NRAK-x"


# ------------------------------------------------------------------ web


def tenant_app(tmp_path, settings=None):
    """The real blueprint with a fake Azure client signed in to tenant-1 (Contoso)."""
    from flask import Flask
    from healthconsole.web import create_blueprint
    from tests.conftest import FakeClient

    fake = FakeClient(gets={"/tenants": {"value": [{"tenantId": "tenant-1", "displayName": "Contoso",
                                                     "defaultDomain": "contoso.example"}]}})
    settings = settings or config.load(tmp_path / "none.yaml")
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: fake, settings, config_file=tmp_path / "c.yaml", authorize=lambda: None))
    return app.test_client(), settings


def test_integration_endpoints_never_return_secrets(tmp_path, keychain):
    web, settings = tenant_app(tmp_path)
    assert web.get("/api/integrations").get_json()["tenant"] == {"id": "tenant-1", "name": "Contoso", "key": None,
                                                                  "save_key": "contoso.example"}
    early = web.post("/api/integrations/secret", json={"target": "servicenow:SN", "field": "password", "value": "pw"})
    assert early.status_code == 400 and "Save the connection first" in early.get_json()["error"]
    web.post("/api/integrations/servicenow", json={"connections": [{"name": "SN", "enabled": True}]})
    no_host = web.post("/api/integrations/secret", json={"target": "servicenow:SN", "field": "password", "value": "pw"})
    assert no_host.status_code == 400 and "instance URL first" in no_host.get_json()["error"]
    saved = web.post("/api/integrations/servicenow", json={"connections": [
        {"name": "SN", "enabled": True, "instance": "dev1", "username": "me"}]})
    assert saved.get_json()["servicenow"][0]["problems"] == [
        "No password yet: type it and Save, or set the AHC_SERVICENOW_PASSWORD environment variable with "
        "AHC_SERVICENOW_INSTANCE=dev1.service-now.com."]
    stored = web.post("/api/integrations/secret", json={"target": "servicenow:SN", "field": "password", "value": "pw-123"})
    assert "pw-123" not in stored.get_data(as_text=True)
    assert stored.get_json()["servicenow"][0]["secrets"]["password"] == "keychain"
    assert web.post("/api/integrations/secret", json={"target": "servicenow:SN", "field": "refresh_token",
                                                      "value": "x"}).status_code == 400
    sign_in = web.post("/api/integrations/servicenow/sign-in", json={"name": "SN"})
    assert sign_in.status_code == 400 and "OAuth" in sign_in.get_json()["error"]
    callback = web.get("/integrations/servicenow/callback?state=forged&code=x")
    assert callback.status_code == 400 and b"expired" in callback.data
    assert settings["tenants"]["contoso.example"]["integrations"]["servicenow"][0]["instance"] == PDI
    assert settings["integrations"]["servicenow"] == []  # nothing global
    nothing = web.get(f"/api/servicenow?subscription={SUBID}").get_json()
    assert not nothing["available"] and "No ServiceNow connection is selected" in nothing["reason"]
    gone = web.get(f"/api/servicenow?subscription={SUBID}&connection=Other").get_json()
    assert "no ServiceNow connection called Other" in gone["reason"]


def test_settings_offer_to_move_global_integrations(tmp_path, keychain):
    (tmp_path / "c.yaml").write_text("integrations:\n  servicenow:\n    instance: https://dev1.service-now.com\n")
    web, _ = tenant_app(tmp_path, config.load(tmp_path / "c.yaml"))
    before = web.get("/api/integrations").get_json()
    assert before["legacy"] == {"servicenow": [PDI], "monitors": []} and before["servicenow_settings"] == []
    after = web.post("/api/integrations/move-legacy", json={}).get_json()
    assert after["legacy"] is None and after["servicenow_settings"][0]["instance"] == PDI
    assert after["tenant"]["key"] == "contoso.example"


@pytest.mark.parametrize("typed,saved", [
    ("dev1", "https://dev1.service-now.com"),
    ("dev1.service-now.com", "https://dev1.service-now.com"),
    ("https://dev1.service-now.com/nav_to.do?uri=incident.do", "https://dev1.service-now.com"),
    ("http://Company.Service-Now.com/", "https://company.service-now.com"),
    ("https://servicenow.example.org", "https://servicenow.example.org"),
    ("  ", ""),
])
def test_instance_url_is_normalised(typed, saved):
    assert servicenow.normalise_instance(typed) == saved


def test_test_uses_what_is_typed_and_saves_nothing(keychain, monkeypatch):
    integ = {"servicenow": [dict(config.SERVICENOW_DEFAULTS)], "monitors": []}
    seen = {}

    def fake_test(self):
        seen["password"], seen["instance"] = self.secret("password"), self.instance
        return {"ok": True, "who": "Ada", "tables": [{"name": "incident", "ok": True, "detail": "readable"}]}

    monkeypatch.setattr(servicenow.ServiceNowClient, "test", fake_test)
    result = integrations.test(integ, "servicenow:ServiceNow", {"instance": "dev1", "username": "admin"}, {"password": "typed-pw"})
    assert result["ok"] and "dev1.service-now.com as Ada" in result["message"]
    assert seen == {"password": "typed-pw", "instance": "https://dev1.service-now.com"}
    assert integ["servicenow"][0]["instance"] == ""  # nothing saved
    assert secrets.get("servicenow", "dev1.service-now.com", "password") is None
    missing = integrations.test(integ, "servicenow:ServiceNow", {"instance": "dev1", "username": "admin"}, {})
    assert not missing["ok"] and missing["message"].startswith("Can't test yet")


def test_a_saved_or_environment_secret_is_never_sent_to_another_instance(keychain, monkeypatch):
    integ = {"servicenow": [{**config.SERVICENOW_DEFAULTS, "instance": "https://dev1.service-now.com", "username": "admin"}],
             "monitors": []}
    monkeypatch.setenv(secrets.env_name("servicenow", "password"), "env-pw")
    monkeypatch.setenv(secrets.SERVICENOW_INSTANCE_ENV, "dev1")
    sent = []
    monkeypatch.setattr(servicenow.ServiceNowClient, "test", lambda self: sent.append((self.instance, self.secret("password")))
                        or {"ok": True, "who": "", "tables": [{"name": "incident", "ok": True, "detail": ""}]})
    elsewhere = integrations.test(integ, "servicenow:ServiceNow", {"instance": "https://attacker.example"}, {})
    assert not elsewhere["ok"] and "type its password" in elsewhere["message"] and sent == []
    assert integrations.test(integ, "servicenow:ServiceNow", {"instance": "https://attacker.example"}, {"password": "typed"})["ok"]
    assert integrations.test(integ, "servicenow:ServiceNow")["ok"]  # the saved instance keeps its saved secret
    assert sent == [("https://attacker.example", "typed"), ("https://dev1.service-now.com", "env-pw")]


# ------------------------------------------------------------------ ServiceNow data and matching

from datetime import datetime, timedelta, timezone  # noqa: E402

from healthconsole.core.integrations import sn_data  # noqa: E402

SUBID = "11111111-1111-1111-1111-111111111111"
APP_ID = f"/subscriptions/{SUBID}/resourceGroups/shop-rg/providers/Microsoft.Web/sites/shop-api-prod"
WEB_ID = f"/subscriptions/{SUBID}/resourceGroups/shop-rg/providers/Microsoft.Web/sites/web"
RG_ID = f"/subscriptions/{SUBID}/resourceGroups/shop-rg"
RESOURCES = [{"id": APP_ID, "name": "shop-api-prod", "type": "Microsoft.Web/sites", "resourceGroup": "shop-rg", "subscriptionId": SUBID},
             {"id": WEB_ID, "name": "web", "type": "Microsoft.Web/sites", "resourceGroup": "shop-rg", "subscriptionId": SUBID},
             {"id": RG_ID, "name": "shop-rg", "type": "microsoft.resources/subscriptions/resourcegroups",
              "resourceGroup": "shop-rg", "subscriptionId": SUBID}]


def cell(value, display=None):
    return {"value": value, "display_value": display if display is not None else value}


def sn_time(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def test_matching_confidence_and_cmdb_notes():
    index, by_id = sn_data.name_index(RESOURCES)
    found = sn_data.match(["New Relic: shop-api-prod error rate high, see web logs"], [], index, by_id)
    assert [(m["name"], m["confidence"]) for m in found] == [("shop-api-prod", "name"), ("web", "possible")]
    exact = sn_data.match(["nothing here"], [APP_ID.upper()], index, by_id)
    assert exact[0]["confidence"] == "exact" and exact[0]["via"] == "configuration item"
    record = {"ci": "", "ci_ids": []}
    assert "No configuration item" in sn_data.cmdb_note(record, found)
    assert "has no Azure resource ID" in sn_data.cmdb_note({"ci": "shop app", "ci_ids": []}, found)
    assert sn_data.cmdb_note({"ci": "shop app", "ci_ids": [APP_ID]}, exact) == ""
    assert sn_data.cmdb_note(record, [m for m in found if m["confidence"] == "possible"]) == ""  # too weak to blame the CMDB


SUB_ROW = {"id": f"/subscriptions/{SUBID}", "name": "app-prod-014", "type": "microsoft.resources/subscriptions",
           "resourceGroup": "", "subscriptionId": SUBID}


def test_matching_tries_the_parts_of_a_hostname():
    index, by_id = sn_data.name_index(RESOURCES)
    found = sn_data.match(["5xx from https://shop-api-prod.azurewebsites.net/health and v1.0"], [], index, by_id)
    assert [(m["name"], m["confidence"]) for m in found] == [("shop-api-prod", "name")]


def test_matching_finds_a_subscription_by_name_or_id_without_a_cmdb_note():
    index, by_id = sn_data.name_index(RESOURCES + [SUB_ROW])
    by_name = sn_data.match(["vCPU quota exceeded in app-prod-014"], [], index, by_id)
    assert [(m["name"], m["confidence"], m["display_type"]) for m in by_name] == [("app-prod-014", "name", "Subscription")]
    by_id_text = sn_data.match([f"Policy denied in subscription {SUBID.upper()}"], [], index, by_id)
    assert [(m["name"], m["confidence"], m["via"]) for m in by_id_text] == [("app-prod-014", "exact", "text")]
    assert sn_data.cmdb_note({"ci": "", "ci_ids": []}, by_name) == ""
    assert sn_data.hits_resource(by_name, f"/subscriptions/{SUBID}") and not sn_data.hits_resource(by_name, RG_ID)


def test_collect_matches_incidents_and_windows_changes(monkeypatch):
    sn_data._cache.clear()
    now = datetime.now(timezone.utc)
    rows = {
        "incident": [{"sys_id": cell("a1"), "number": cell("INC001"), "priority": cell("1", "1 - Critical"),
                      "short_description": cell("shop-api-prod returning 500s"), "description": cell(""),
                      "state": cell("2", "In Progress"), "cmdb_ci": cell("", ""), "opened_at": cell(sn_time(now))}],
        "change_request": [
            {"sys_id": cell("c1"), "number": cell("CHG1"), "short_description": cell("Patch shop-api-prod"),
             "start_date": cell(sn_time(now - timedelta(hours=1))), "end_date": cell(sn_time(now + timedelta(hours=1)))},
            {"sys_id": cell("c2"), "number": cell("CHG2"), "short_description": cell("Next week"),
             "start_date": cell(sn_time(now + timedelta(days=7))), "end_date": cell(sn_time(now + timedelta(days=8)))},
            {"sys_id": cell("c3"), "number": cell("CHG3"), "short_description": cell("Overran"),
             "start_date": cell(sn_time(now - timedelta(days=2))), "end_date": cell(sn_time(now - timedelta(days=1)))},
            {"sys_id": cell("c5"), "number": cell("CHG5"), "short_description": cell("Next month"),
             "start_date": cell(sn_time(now + timedelta(days=30))), "end_date": cell(sn_time(now + timedelta(days=31)))},
            {"sys_id": cell("c6"), "number": cell("CHG6"), "short_description": cell("No dates yet")}],
        "change_closed": [
            {"sys_id": cell("c4"), "number": cell("CHG4"), "short_description": cell("Resize shop-api-prod"),
             "start_date": cell(sn_time(now - timedelta(days=10))), "end_date": cell(sn_time(now - timedelta(days=10))),
             "close_code": cell("unsuccessful", "Unsuccessful")}],
        # CHG2's affected CI carries web's Azure ID; CHG6's change task names shop-api-prod in its text
        "task_ci": [{"task": cell("c2"), "ci_item": cell("ci9", "web-frontend"), "ci_item.object_id": cell(WEB_ID)}],
        "change_task": [{"change_request": cell("c6"), "number": cell("CTASK1"), "short_description": cell("Restart shop-api-prod"),
                         "state": cell("1", "Open")}],
    }
    sent = []

    def read(self, name, query="", fields=(), limit=100, offset=0):  # one page, with ServiceNow's total
        if name.startswith("em_"):
            raise servicenow.ServiceNowError(f"The {name} table isn't available on this instance.", "missing", 400)
        sent.append((name, query))
        found = rows["change_closed" if name == "change_request" and query.startswith("active=false") else name]
        return found[offset:offset + limit], len(found)

    monkeypatch.setattr(servicenow.ServiceNowClient, "_read", read)
    data = sn_data.collect(sn_cfg(), RESOURCES)
    incident = data["incidents"][0]
    assert incident["priority"] == 1 and incident["url"].endswith("/incident.do?sys_id=a1")
    assert incident["matches"][0]["name"] == "shop-api-prod" and "No configuration item" in incident["cmdb_note"]
    assert incident["ci_url"] == ""  # no configuration item, no CMDB link
    # each list opens in ServiceNow with the very query sent (encoded); a table not read has none
    sent_incident = next(q for t, q in sent if t == "incident")
    assert data["lists"]["incident"] == sn_data.list_url(data["instance"], "incident", sent_incident)
    assert data["lists"]["change_closed"].split("sysparm_query=")[1].startswith("active%3Dfalse%5Eclosed_at%3E%3D")
    assert "change_request_list.do" in data["lists"]["change_closed"] and set(data["lists"]) >= {"incident", "change_request", "em_alert"}
    assert [(c["number"], c["window"]) for c in data["changes"]] == [
        ("CHG1", "in progress"), ("CHG3", "past its window"), ("CHG2", "scheduled"), ("CHG6", "not scheduled"),
        ("CHG4", "closed")]  # CHG5 starts past change_ahead_days (14)
    assert data["changes"][4]["outcome"] == "Unsuccessful"
    chg2, chg6 = data["changes"][2], data["changes"][3]
    assert [(m["name"], m["confidence"], m["via"]) for m in chg2["matches"]] == [("web", "exact", "affected CI")]
    assert chg2["affected_cis"] == ["web-frontend"] and chg2["cmdb_note"] == ""  # an affected CI with an Azure ID maps it
    assert [(m["name"], m["via"]) for m in chg6["matches"]] == [("shop-api-prod", "name")]
    assert chg6["tasks"] == [{"number": "CTASK1", "title": "Restart shop-api-prod", "state": "Open"}]
    # Affected CIs are read through their change's own filter, open and closed apart: never a list of change IDs
    asked = sorted(q.split("^ORDERBY")[0] for t, q in sent if t == "task_ci")
    assert len(asked) == 2 and asked[0].startswith("task.active=false^task.closed_at>=") and asked[1] == "task.active=true"
    changes_asked = [q for t, q in sent if t == "change_request"]
    assert any(q.startswith("active=false^closed_at>=") and q.endswith("ORDERBYDESCclosed_at") for q in changes_asked)
    assert any(q.startswith("active=true^start_date<=") and "^ORstart_dateISEMPTY^end_date>=" in q for q in changes_asked)
    assert "isn't available" in data["notes"]["em_alert"] and "incident" not in data["notes"]
    by_group = sn_data.for_resource(data, RG_ID)
    assert [i["number"] for i in by_group["incidents"]] == ["INC001"]
    assert sn_data.for_resource(data, WEB_ID)["incidents"] == []


def test_default_incident_filter_leaves_out_resolved(monkeypatch):
    sn_data._cache.clear()
    seen = {}
    monkeypatch.setattr(servicenow.ServiceNowClient, "_read",
                        lambda self, name, query="", fields=(), limit=100, offset=0: (seen.setdefault(name, query) and [], 0))
    sn_data.collect(sn_cfg(), RESOURCES)
    assert seen["incident"] == "active=true^state!=6^ORDERBYpriority^ORDERBYDESCsys_updated_on"


def test_unselected_tables_say_so(monkeypatch):
    sn_data._cache.clear()
    monkeypatch.setattr(servicenow.ServiceNowClient, "table", lambda self, *a, **k: [])
    data = sn_data.collect(sn_cfg(tables=["incident"]), RESOURCES)
    assert "isn't selected" in data["notes"]["change_request"]


# ------------------------------------------------------------------ monitoring tools

from healthconsole.core.integrations import monitors  # noqa: E402

NR = {"name": "New Relic", "type": "newrelic", "enabled": True, "region": "us", "account_ids": [1]}
DB_ID = f"/subscriptions/{SUBID}/resourceGroups/shop-rg/providers/Microsoft.Sql/servers/sql1/databases/orders"
RESOURCES_DB = RESOURCES + [{"id": DB_ID, "name": "orders", "type": "Microsoft.Sql/servers/databases",
                             "resourceGroup": "shop-rg", "subscriptionId": SUBID}]


def nr_entity(guid, name, severity, tags=None):
    return {"guid": guid, "name": name, "entityType": "APM_APPLICATION_ENTITY", "domain": "APM", "alertSeverity": severity,
            "reporting": True, "permalink": f"https://one.newrelic.com/redirect/entity/{guid}", "account": {"id": 1, "name": "Prod"},
            "tags": [{"key": k, "values": v} for k, v in (tags or {}).items()]}


class GraphSession:
    """Answers NerdGraph by what the query asks for; `issues` may be an error message instead of a list."""

    def __init__(self, entities, issues, named=(), logs=()):
        self.entities, self.issues, self.named, self.logs, self.calls = entities, issues, list(named), list(logs), []
        self.event_types = [{"eventType": "Log"}]

    def request(self, method, url, json=None, **kwargs):
        return self.post(url, json=json, **kwargs)

    def post(self, url, json=None, **kwargs):
        self.calls.append(json)
        q, v = json["query"], json["variables"]
        if "nrql(query:" in q:
            rows = self.event_types if v["q"].startswith("SHOW EVENT TYPES") else self.logs
            return Response(200, {"data": {"actor": {"account": {"nrql": {"results": rows}}}}})
        if "entitySearch" in q:
            rows = self.named if "name LIKE" in v["q"] else self.entities
            first = v["cursor"] is None
            page = {"nextCursor": "c2" if first and len(rows) > 1 else None, "entities": rows[:1] if first else rows[1:]}
            return Response(200, {"data": {"actor": {"entitySearch": {"count": len(rows), "results": page}}}})
        if isinstance(self.issues, str):
            return Response(200, {"data": {"actor": {"account": {"aiIssues": {"issues": None}}}},
                                  "errors": [{"message": self.issues}]})
        return Response(200, {"data": {"actor": {"account": {"aiIssues": {"issues": {"nextCursor": None, "issues": self.issues}}}}}})


@pytest.fixture
def nr_session(monkeypatch, keychain):
    secrets.put("monitor:New Relic", "newrelic", "api_key", "NRAK-x")
    sn_data._cache.clear()

    def install(session):
        monkeypatch.setattr(newrelic.requests, "Session", lambda: session)
        return session
    return install


def test_alerting_entities_and_issues_are_matched(nr_session):
    session = nr_session(GraphSession(
        [nr_entity("g1", "shop-api-prod", "CRITICAL"), nr_entity("g2", "orders-db", "WARNING", {"azure.resourceId": [DB_ID]}),
         nr_entity("g3", "someone-else", "WARNING")],
        [{"issueId": "i1", "title": ["Error rate high"], "priority": "CRITICAL", "entityGuids": ["g1"],
          "entityNames": ["shop-api-prod"], "activatedAt": 1_700_000_000_000, "conditionName": ["Errors"], "policyName": ["API"]}]))
    data = monitors.connection(NR, RESOURCES_DB)
    assert data["available"] and [e["name"] for e in data["entities"]] == ["shop-api-prod", "orders-db", "someone-else"]
    app, db, other = data["entities"]
    assert app["severity"] == "critical" and app["matches"][0]["confidence"] == "name"
    assert db["matches"][0] == {**db["matches"][0], "id": DB_ID, "confidence": "exact", "via": "tag"}
    assert other["matches"] == []
    issue = data["issues"][0]
    assert issue["title"] == "Error rate high" and issue["condition"] == "Errors" and issue["url"] == app["url"]
    assert issue["matches"][0]["id"] == APP_ID and issue["activated_at"].startswith("2023-11-14")
    search = session.calls[0]
    assert search["variables"]["q"] == "accountId IN ('1') AND alertSeverity IN ('CRITICAL', 'WARNING')"
    assert len(session.calls) == 3  # two entity pages, one issues page


def test_issues_that_cannot_be_read_are_a_note_not_an_empty_list(nr_session):
    nr_session(GraphSession([nr_entity("g1", "shop-api-prod", "CRITICAL")], "Field 'aiIssues' isn't available"))
    data = monitors.connection(NR, RESOURCES)
    assert data["available"] and data["issues"] == []
    assert data["notes"] == ["Open issues in account 1 couldn't be read: New Relic said: Field 'aiIssues' isn't available"]


def test_resource_view_adds_entities_found_by_name(nr_session):
    nr_session(GraphSession([], [], named=[nr_entity("g9", "shop-api-prod", "NOT_ALERTING"), nr_entity("g8", "shop-api-prod-old", "CRITICAL")]))
    data = monitors.for_resource(NR, APP_ID, "shop-api-prod", RESOURCES)
    assert [(e["name"], e["severity"]) for e in data["entities"]] == [("shop-api-prod", "ok")]
    assert [e["name"] for e in monitors.for_resource(NR, RG_ID, "shop-rg", RESOURCES)["entities"]] == ["shop-api-prod"]
    assert monitors.for_resource(NR, WEB_ID, "web", RESOURCES)["entities"] == []  # a name search hit that isn't this resource


def test_an_entity_named_exactly_like_a_resource_is_a_name_match(nr_session):
    nr_session(GraphSession([nr_entity("g1", "web", "CRITICAL"), nr_entity("g2", "web checkout", "WARNING")], []))
    entities = {e["name"]: e for e in monitors.connection(NR, RESOURCES)["entities"]}
    assert entities["web"]["matches"][0]["confidence"] == "name"
    assert entities["web checkout"]["matches"][0]["confidence"] == "possible"


def test_a_connection_that_cannot_be_read_says_why(keychain):
    assert monitors.connection({**NR, "account_ids": []}, RESOURCES)["problems"][0].startswith("No account IDs")
    datadog = monitors.connection({"name": "DD", "type": "datadog", "enabled": True, "site": "datadoghq.com"}, RESOURCES)
    assert not datadog["available"] and any("later version" in p for p in datadog["problems"])


def test_monitors_endpoint_without_connections_says_what_to_do(tmp_path):
    web, _ = tenant_app(tmp_path)
    body = web.get(f"/api/monitors?subscription={SUBID}").get_json()
    assert not body["available"] and "No monitoring connection is selected" in body["reason"] and "Contoso" in body["fix"]


def test_logs_are_read_by_the_entities_that_match_the_resource(nr_session):
    session = nr_session(GraphSession([nr_entity("g1", "shop-api-prod", "CRITICAL")], [], logs=[
        {"timestamp": 1_700_000_000_000, "message": "boom", "level": "ERROR", "entity.guid": "g1", "trace.id": "t1"},
        {"timestamp": 1_700_000_060_000, "message": "later", "entity.guid": "g1"}]))
    data = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30, "it's", by_id=False)
    assert data["available"] and data["description"] == "US · account 1" and data["entities"] == ["shop-api-prod"]
    assert [line["message"] for line in data["lines"]] == ["later", "boom"]  # newest first
    assert data["lines"][1]["level"] == "ERROR" and data["lines"][1]["entity"] == "shop-api-prod"
    assert data["lines"][1]["attributes"] == {"level": "ERROR", "entity.guid": "g1", "trace.id": "t1"}
    nrql = session.calls[-1]["variables"]["q"]
    assert nrql == ("SELECT * FROM Log WHERE (entity.guid IN ('g1') OR allColumnSearch('shop-api-prod', insensitive: true)) "
                    "AND message LIKE '%it\\'s%' SINCE 30 minutes ago LIMIT 100")
    assert session.calls[-1]["variables"]["id"] == 1


def test_logs_without_a_matching_entity_say_so(nr_session):
    nr_session(GraphSession([], [], named=[]))
    data = monitors.logs(NR, WEB_ID, "web", RESOURCES, 15, by_id=False)
    assert data["lines"] == [] and "No New Relic entity matches" in data["note"]
    with pytest.raises(ValueError):
        monitors.logs(NR, WEB_ID, "web", RESOURCES, 7)


def test_log_errors_are_reported(nr_session, monkeypatch):
    nr_session(GraphSession([nr_entity("g1", "shop-api-prod", "CRITICAL")], []))
    monkeypatch.setattr(newrelic.NewRelicClient, "nrql", lambda self, a, q: (_ for _ in ()).throw(
        newrelic.NewRelicError("New Relic said: NRQL Syntax error", "access")))
    data = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30)
    assert not data["available"] and "Syntax error" in data["reason"]


def test_settings_know_what_each_tool_answers():
    caps = monitors.capabilities()
    assert caps["newrelic"] == {"connected": True, "capabilities": ["alerts", "entities", "logs", "alert history",
                                                                        "Kubernetes OOM kills", "Kubernetes warning events",
                                                                        "Kubernetes workloads by namespace",
                                                                        "an app's requests per minute", "a workload's pod restarts",
                                                                        "a workload's CPU and memory over 30 days",
                                                                        "an app's heap and process memory"]}
    assert caps["grafana"]["connected"] is False


# ------------------------------------------------------------------ saved scopes

def test_saved_scopes_are_per_tenant_and_validated(tmp_path):
    web, settings = tenant_app(tmp_path)
    prod = {"name": "Prod", "subscriptions": ["app-prod-*"], "resource_groups": [], "tag_filters": {"env": "prod"},
            "connections": {"servicenow": "SN Prod", "newrelic": "NR Prod"}}
    saved = web.post("/api/saved-scopes", json={"scopes": [prod]}).get_json()
    assert saved["saved_scopes"] == [prod]
    assert config.saved_scopes(settings, WORK) == [prod] and config.saved_scopes(settings, HOME) == []
    assert "resource_groups" not in (tmp_path / "c.yaml").read_text()  # empty parts aren't written
    for bad in ([{"name": ""}], [{"name": "a"}, {"name": "A"}], [{"name": "a", "subscriptions": "x"}],
                [{"name": "a", "connections": {"newrelic": 1}}], [{"name": "a", "colour": "red"}]):
        assert web.post("/api/saved-scopes", json={"scopes": bad}).status_code == 400
    assert web.post("/api/saved-scopes", json={"scopes": []}).get_json()["saved_scopes"] == []
    assert saved["reserved_names"] == list(config.OPEN_WITH)  # the page's name prompt checks these before saving
    refused = web.post("/api/saved-scopes", json={"scopes": [{"name": "All ", "subscriptions": ["*"]}]})
    assert refused.status_code == 400 and "'All ' is reserved" in refused.get_json()["error"]
    assert config.saved_scopes(settings, WORK) == []  # nothing written
    star = config.from_data({"tenants": {"*": {"saved_scopes": [{"name": "Everywhere"}]}}})
    assert config.saved_scopes(star, WORK) == []  # never borrowed from "*"


def test_session_reads_the_cli_default_tenant_fresh(tmp_path):
    web, _ = tenant_app(tmp_path)
    assert web.get("/api/session").get_json() == {"tenant_id": "tenant-1", "tenant_name": "Contoso",
                                                  "object_id": "oid-1", "name": "tester@example.com"}


def test_each_connection_keeps_its_own_keychain_item(keychain):
    """Two New Relic keys and a ServiceNow password: three items, none overwriting another."""
    integ = integrations.bind({"servicenow": [{**config.SERVICENOW_DEFAULTS, "name": "SN", "instance": PDI}],
                               "monitors": [{"name": "NR Prod", "type": "newrelic"}, {"name": "NR NonProd", "type": "newrelic"}]},
                              "tenant-1")
    for target, field, value in (("servicenow:SN", "password", "pw"), ("monitor:NR Prod", "api_key", "NRAK-prod"),
                                 ("monitor:NR NonProd", "api_key", "NRAK-nonprod")):
        secrets.put(*integrations.secret_target(integ, target)[:2], field, value)
    assert sorted(account for _, account in keychain.items) == [
        "monitor:NR NonProd|tenant-1|api_key", "monitor:NR Prod|tenant-1|api_key", "servicenow|dev1.service-now.com|password"]
    assert secrets.get("servicenow", "dev1.service-now.com", "password") == "pw"


@pytest.mark.parametrize("names,message", [
    (["NR Prod", "NR Prod"], "Two monitoring connections are called 'NR Prod'."),
    (["NR Prod", "nr-prod"], "'NR Prod' and 'nr-prod' are too alike"),
    (["   "], "needs a name"),
    (["|||"], "can't be used as a name"),
    (["NR|Prod"], "can't be used as a name"),
    (["x" * 41], "can't be used as a name"),
])
def test_connection_names_are_checked_with_a_plain_message(tmp_path, names, message):
    web, _ = tenant_app(tmp_path)
    reply = web.post("/api/integrations/monitors", json={"monitors": [{"name": n, "type": "newrelic"} for n in names]})
    assert reply.status_code == 400 and message in reply.get_json()["error"]
    assert not reply.get_json()["error"].startswith(str(tmp_path))  # no file path on the card
    assert not (tmp_path / "c.yaml").exists()


def test_good_names_save(tmp_path):
    web, _ = tenant_app(tmp_path)
    good = ["NR Prod", "NR NonProd", "New Relic (EU) 2", "dd_prod.v1"]
    assert web.post("/api/integrations/monitors", json={"monitors": [{"name": n, "type": "newrelic"} for n in good]}).status_code == 200
    assert web.post("/api/integrations/servicenow", json={"connections": [{"name": "SN Prod"}, {"name": "sn_prod"}]}).status_code == 400


def test_keychain_items_are_named_for_what_they_hold(keychain):
    secrets.put("monitor:NR Prod", "tenant-1", "api_key", "NRAK-1")
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw")
    assert sorted(service for service, _ in keychain.items) == [
        "Azure Health Console - NR Prod (API key)",
        "Azure Health Console - ServiceNow dev1.service-now.com (password)"]


def test_items_stored_under_the_old_name_move_on_first_read(keychain):
    account = "monitor:NR Prod|tenant-1|api_key"
    keychain.set_password("azure-health-console", account, "NRAK-old")
    assert secrets.get("monitor:NR Prod", "tenant-1", "api_key") == "NRAK-old"
    assert keychain.items == {("Azure Health Console - NR Prod (API key)", account): "NRAK-old"}
    secrets.delete("monitor:NR Prod", "tenant-1", "api_key")
    assert keychain.items == {}


def test_logs_for_entities_picked_on_the_tab(nr_session):
    session = nr_session(GraphSession([], [], logs=[{"timestamp": 1_700_000_000_000, "message": "boom", "entity.name": "checkout",
                                                     "entity.guid": "MXxBUE18QVBQTElDQVRJT058MQ"}]))
    data = monitors.entity_logs(NR, ["MXxBUE18QVBQTElDQVRJT058MQ"], 1, 15)
    assert data["available"] and data["entities"] == ["checkout"] and data["lines"][0]["message"] == "boom"
    assert session.calls[-1]["variables"]["q"].startswith("SELECT * FROM Log WHERE entity.guid IN ('MXxBUE18QVBQTElDQVRJT058MQ')")
    for ids, account, minutes in ((["MXxBUE18QVBQTElDQVRJT058MQ"], 2, 15), (["x' OR '1'='1"], 1, 15), ([], 1, 15),
                                  (["MXxBUE18QVBQTElDQVRJT058MQ"], 1, 7)):
        with pytest.raises(ValueError):  # another account, an injection attempt, nothing, or an odd window
            monitors.entity_logs(NR, ids, account, minutes)


def test_issues_carry_their_entities_and_account(nr_session):
    nr_session(GraphSession([], [{"issueId": "i1", "title": ["x"], "entityGuids": ["g1", "g2"], "entityNames": ["a", "b"],
                                  "deepLinkUrl": ["https://one.newrelic.com/launcher/q1"]}]))
    issue = monitors.connection(NR, RESOURCES)["issues"][0]
    assert issue["entity_ids"] == ["g1", "g2"] and issue["account_id"] == 1
    assert issue["url"] == "https://one.newrelic.com/launcher/q1"  # New Relic's link for the issue (a list of one)


def test_a_query_conditions_issue_matches_the_resource_its_condition_names(nr_session):
    nr_session(GraphSession([], [
        {"issueId": "i1", "title": ["Metric query result is = 0.0 for 10 minutes on 'shop-api-prod - CPU Equals Zero'"],
         "entityGuids": [], "entityNames": ["Metric query"], "conditionName": ["shop-api-prod - CPU Equals Zero"]},
        {"issueId": "i2", "title": ["Metric query result is > 1 on 'web latency'"], "entityGuids": [],
         "entityNames": ["Metric query"], "conditionName": ["web latency"]}]))
    by_id = {i["id"]: i for i in monitors.connection(NR, RESOURCES)["issues"]}
    assert [m["id"] for m in by_id["i1"]["matches"]] == [APP_ID] and by_id["i1"]["matches"][0]["confidence"] == "name"
    assert by_id["i2"]["matches"] == []  # "web" is too plain to trust in free text
    assert monitors.for_resource(NR, APP_ID, "shop-api-prod", RESOURCES)["issues"][0]["id"] == "i1"


def test_entities_show_their_specific_type(nr_session):
    raw = {**nr_entity("MXxBUE18QVBQTElDQVRJT04", "aks-1", "CRITICAL"), "entityType": "GENERIC_INFRASTRUCTURE_ENTITY",
           "type": "KUBERNETESCLUSTER"}
    nr_session(GraphSession([raw], []))
    entity = monitors.connection(NR, RESOURCES)["entities"][0]
    assert (entity["type"], entity["type_class"]) == ("KUBERNETESCLUSTER", "Generic Infrastructure")


def test_entities_with_one_name_are_one_row_with_each_source(nr_session):
    azure = {**nr_entity("MXxBenVyZUVOVElUWQ", "aks-shop-prod", "NOT_CONFIGURED"), "type": "AZUREKUBERNETESSERVICE"}
    kube = {**nr_entity("MXxLOFNDTFVTVEVSMQ", "aks-shop-prod", "CRITICAL"), "type": "KUBERNETESCLUSTER"}
    other_account = {**nr_entity("MXxPVEhFUkFDQ1Q", "aks-shop-prod", "WARNING"), "account": {"id": 2, "name": "Other"}}
    nr_session(GraphSession([azure, kube, other_account], []))
    rows = monitors.connection(NR, RESOURCES)["entities"]
    assert len(rows) == 2  # same name in another account stays apart
    merged = next(r for r in rows if len(r["sources"]) == 2)
    assert merged["severity"] == "critical" and merged["ids"] == ["MXxLOFNDTFVTVEVSMQ", "MXxBenVyZUVOVElUWQ"]
    assert [s["type"] for s in merged["sources"]] == ["KUBERNETESCLUSTER", "AZUREKUBERNETESSERVICE"]
    assert merged["type"] == "KUBERNETESCLUSTER · AZUREKUBERNETESSERVICE"


def test_logs_read_every_partition_unless_narrowed(nr_session):
    session = nr_session(GraphSession([nr_entity("MXxBUE18QVBQTElDQVRJT04", "shop-api-prod", "CRITICAL")], [],
                                      logs=[{"timestamp": 1, "message": "x"}]))
    session.event_types = [{"eventTypes": ["Transaction", "Log_AKS", "Log", "LogX", "K8sPodSample"]}]  # New Relic's shape
    data = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30)
    assert data["partitions"] == ["Log", "Log_AKS"] and data["queries"][0]["partitions"] == ["Log", "Log_AKS"]
    assert session.calls[-1]["variables"]["q"].startswith("SELECT * FROM Log, Log_AKS WHERE (entity.guid IN")
    narrowed = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30, partitions=["Log_AKS"])
    assert session.calls[-1]["variables"]["q"].startswith("SELECT * FROM Log_AKS WHERE") and narrowed["partitions"] == ["Log", "Log_AKS"]
    missing = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30, partitions=["Log_Nope"])
    assert missing["lines"] == [] and "has no Log_Nope partition" in missing["notes"][0]
    with pytest.raises(ValueError):
        monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30, partitions=["Log; DROP"])


def test_partitions_that_cant_be_listed_fall_back_to_log(nr_session, monkeypatch):
    nr_session(GraphSession([nr_entity("MXxBUE18QVBQTElDQVRJT04", "shop-api-prod", "CRITICAL")], [], logs=[]))
    real = newrelic.NewRelicClient.nrql
    def nrql(self, account, q):
        if q.startswith("SHOW"):
            raise newrelic.NewRelicError("New Relic said: not allowed", "access")
        return real(self, account, q)
    monkeypatch.setattr(newrelic.NewRelicClient, "nrql", nrql)
    data = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30)
    assert data["queries"][0]["partitions"] == ["Log"] and "couldn't be listed" in data["notes"][0]


def test_partitions_also_read_one_row_per_type_and_say_when_none(nr_session):
    session = nr_session(GraphSession([nr_entity("MXxBUE18QVBQTElDQVRJT04", "shop-api-prod", "CRITICAL")], [], logs=[]))
    session.event_types = [{"eventType": "Log_A"}, {"eventType": "Log"}]
    assert monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30)["partitions"] == ["Log", "Log_A"]
    sn_data._cache.clear()
    session.event_types = [{"eventTypes": ["Transaction"]}]
    data = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30)
    assert data["queries"][0]["partitions"] == ["Log"] and "No log event types were found" in data["notes"][0]


AKS_ID = f"/subscriptions/{SUBID}/resourceGroups/shop-rg/providers/Microsoft.ContainerService/managedClusters/aks-shop-01"


def test_logs_found_by_a_named_attribute_even_without_an_entity(nr_session):
    session = nr_session(GraphSession([], [], named=[], logs=[{"timestamp": 1, "message": "pod started", "cluster_name": "aks-shop-01"}]))
    session.event_types = [{"eventTypes": ["Log", "Log_Apps"]}]
    monitor = {**NR, "log_attributes": {"microsoft.containerservice/managedclusters": "cluster_name"}}
    data = monitors.logs(monitor, AKS_ID, "aks-shop-01", RESOURCES, 30, by_id=False)
    assert data["attribute"] == "cluster_name" and data["lines"][0]["message"] == "pod started"
    assert session.calls[-1]["variables"]["q"] == ("SELECT * FROM Log, Log_Apps WHERE (`cluster_name` = 'aks-shop-01' OR "
                                                   "allColumnSearch('aks-shop-01', insensitive: true)) SINCE 30 minutes ago LIMIT 100")
    assert session.calls[-1]["variables"]["id"] == 1  # the connection's own account
    assert data["lines"][0]["via"] == "entity"  # its attribute names the cluster


def test_attribute_and_entities_are_either_or(nr_session):
    session = nr_session(GraphSession([nr_entity("MXxBUE18QVBQTElDQVRJT04", "shop-api-prod", "CRITICAL")], [], logs=[]))
    monitor = {**NR, "log_attributes": {"microsoft.web/sites": "app_name"}}
    monitors.logs(monitor, APP_ID, "shop-api-prod", RESOURCES, 15, by_id=False)
    assert ("WHERE (entity.guid IN ('MXxBUE18QVBQTElDQVRJT04') OR `app_name` = 'shop-api-prod' OR "
            "allColumnSearch('shop-api-prod', insensitive: true)) SINCE") in session.calls[-1]["variables"]["q"]


@pytest.mark.parametrize("mapping,ok", [
    ({"microsoft.containerservice/managedclusters": "cluster_name"}, True),
    ({"microsoft.web/sites": "App Name"}, True),
    ({"managedclusters": "cluster_name"}, False),
    ({"microsoft.web/sites": "a`b"}, False),
    ({"microsoft.web/sites": "x' OR '1'='1"}, False),
])
def test_log_attribute_mapping_is_validated(mapping, ok):
    data = {"tenants": {"t": {"integrations": {"monitors": [{**NR, "log_attributes": mapping}]}}}}
    if ok:
        config.from_data(data)
    else:
        with pytest.raises(config.ConfigError):
            config.from_data(data)


def test_load_logs_finds_lines_by_entity_or_by_name_and_says_which(nr_session):
    session = nr_session(GraphSession([nr_entity("MXxBUE18QVBQTElDQVRJT04", "shop-api-prod", "CRITICAL")], [], logs=[
        {"timestamp": 2, "message": "from the app", "entity.guid": "MXxBUE18QVBQTElDQVRJT04"},
        {"timestamp": 1, "message": "proxy called shop-api-prod/health"}]))
    data = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 15)
    assert [(line["message"], line["via"]) for line in data["lines"]] == [("from the app", "entity"),
                                                                          ("proxy called shop-api-prod/health", "mention")]
    assert data["search"] == "shop-api-prod" and data["notes"] == []


def test_generic_names_are_not_searched(nr_session):
    session = nr_session(GraphSession([], [], named=[nr_entity("MXxBUE18V0VCRU5USVRZ", "web", "CRITICAL")], logs=[]))
    data = monitors.logs(NR, WEB_ID, "web", RESOURCES, 15)
    assert "allColumnSearch" not in session.calls[-1]["variables"]["q"] and "too short, common or spaced" in data["notes"][-1]
    nothing = monitors.logs(NR, f"/subscriptions/{SUBID}/resourceGroups/shop-rg/providers/Microsoft.Web/sites/api", "api", RESOURCES, 15, by_id=False)
    assert nothing["lines"] == [] and "too short, common or spaced" in nothing["note"]


def test_logs_carrying_the_resources_azure_id_are_read_and_attributed(nr_session):
    forwarded = {"timestamp": 1_762_000_000_000, "message": "", "resourceId": WEB_ID.upper(), "category": "ConnectedClientList"}
    session = nr_session(GraphSession([], [], named=[], logs=[forwarded]))
    data = monitors.logs(NR, WEB_ID, "web", RESOURCES, 15)
    assert f"WHERE lower(`resourceId`) IN ('{WEB_ID.lower()}') SINCE" in session.calls[-1]["variables"]["q"]
    assert data["by_id"] and data["azure_ids"] == [WEB_ID] and data["lines"][0]["via"] == "entity" and not data["note"]
    plan = monitors.plan(NR, WEB_ID, "web", RESOURCES, 15, by_id=False)
    assert plan["azure_ids"] == [WEB_ID] and not plan["by_id"] and not plan["logs"]["queries"]


def test_row_logs_read_the_azure_ids_the_row_matched(nr_session):
    session = nr_session(GraphSession([], [], logs=[]))
    data = monitors.entity_logs(NR, ["MXxBUE18QVBQTElDQVRJT04"], 1, 15, resource_ids=[APP_ID])
    assert f"OR lower(`resourceId`) IN ('{APP_ID.lower()}'))" in session.calls[-1]["variables"]["q"] and data["by_id"]
    monitors.entity_logs(NR, ["MXxBUE18QVBQTElDQVRJT04"], 1, 15, resource_ids=[APP_ID], by_id=False)
    assert "resourceId" not in session.calls[-1]["variables"]["q"]
    with pytest.raises(ValueError, match="Azure resource IDs"):
        monitors.entity_logs(NR, ["MXxBUE18QVBQTElDQVRJT04"], 1, 15, resource_ids=["') OR true OR ('"])


def test_row_logs_also_search_the_entity_name(nr_session):
    session = nr_session(GraphSession([], [], logs=[]))
    monitors.entity_logs(NR, ["MXxBUE18QVBQTElDQVRJT04"], 1, 15, name="aks-shop-01")
    assert "OR allColumnSearch('aks-shop-01', insensitive: true))" in session.calls[-1]["variables"]["q"]


def test_slow_transactions_are_faceted_by_app_and_name_with_percentiles_unwrapped(nr_session):
    session = nr_session(GraphSession([nr_entity("g1", "shop-api-prod", "CRITICAL")], [], logs=[
        {"facet": ["shop-api-prod", "WebTransaction/GET /cart"], "p95_s": {"95": 2.5}, "avg_s": 0.4, "max_s": 9.1, "calls": 120}]))
    data = monitors.report(NR, APP_ID, "shop-api-prod", RESOURCES, 60, "slow", "cart")
    assert data["available"] and data["report_label"] == "Slow transactions" and data["entities"] == ["shop-api-prod"]
    assert data["rows"] == [{"appName": "shop-api-prod", "name": "WebTransaction/GET /cart", "p95_s": 2.5, "avg_s": 0.4,
                             "max_s": 9.1, "calls": 120, "account": 1}]
    nrql = session.calls[-1]["variables"]["q"]
    assert nrql == ("SELECT percentile(duration, 95) AS 'p95_s', average(duration) AS 'avg_s', max(duration) AS 'max_s',"
                    " count(*) AS 'calls' FROM Transaction WHERE (entityGuid IN ('g1') OR lower(appName) = 'shop-api-prod')"
                    " AND name LIKE '%cart%' FACET appName, name SINCE 60 minutes ago LIMIT 50")


def test_errors_report_reads_by_app_name_when_no_entity_matches_and_refuses_unknown_kinds(nr_session):
    session = nr_session(GraphSession([], [], named=[], logs=[
        {"facet": ["shop-api-prod", "GET /cart", "KeyError"], "errors": 4, "last_message": "boom", "last_seen": 1_700_000_000_000}]))
    data = monitors.report(NR, APP_ID, "shop-api-prod", RESOURCES, 30, "errors")
    assert data["rows"][0]["error.class"] == "KeyError" and data["rows"][0]["last_seen"].startswith("2023-11-14")
    assert "WHERE lower(appName) = 'shop-api-prod' FACET appName, transactionName, error.class" in session.calls[-1]["variables"]["q"]
    assert monitors.report(NR, APP_ID, "shop-api-prod", RESOURCES, 30, "bogus")["available"] is False
    with pytest.raises(ValueError):
        monitors.report(NR, APP_ID, "shop-api-prod", RESOURCES, 7, "errors")


def test_browser_sign_in_callback_finishes_with_the_pending_sign_in(tmp_path, keychain, monkeypatch):
    """The success path: the state from the sign-in URL comes back once, and the token exchange gets that sign-in's
    own verifier and connection; a second use of the same state is refused."""
    from urllib.parse import parse_qs, urlsplit

    from healthconsole.core.integrations import servicenow as sn

    web, settings = tenant_app(tmp_path)
    web.post("/api/integrations/servicenow", json={"connections": [
        {"name": "SN", "enabled": True, "instance": "dev1", "auth": "oauth_browser", "client_id": "app-123"}]})
    started = web.post("/api/integrations/servicenow/sign-in", json={"name": "SN"}).get_json()
    query = parse_qs(urlsplit(started["url"]).query)
    state = query["state"][0]
    finished = {}
    monkeypatch.setattr(sn, "finish_browser_sign_in",
                        lambda cfg, code, redirect, verifier: finished.update(name=cfg["name"], code=code, verifier=verifier))
    ok = web.get(f"/integrations/servicenow/callback?state={state}&code=abc")
    assert ok.status_code == 200 and b"Signed in to SN" in ok.data
    assert finished["name"] == "SN" and finished["code"] == "abc"
    # the verifier is the one this sign-in started with: its challenge is in the URL ServiceNow was sent to
    import base64
    import hashlib
    challenge = base64.urlsafe_b64encode(hashlib.sha256(finished["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"][0] == challenge
    again = web.get(f"/integrations/servicenow/callback?state={state}&code=abc")
    assert again.status_code == 400 and b"expired" in again.data  # used once


def test_every_service_error_shares_one_base_and_one_answer():
    from healthconsole.core import acr, guarded
    from healthconsole.core.integrations import failed, newrelic, provider, servicenow, unavailable
    errors = [newrelic.NewRelicError("Bad key.", "auth"), servicenow.ServiceNowError("Gone.", "missing", 404),
              acr.RegistryError("Blocked.", "network"), provider.ProviderError("Down.", "network")]
    assert all(isinstance(e, guarded.ServiceError) for e in errors)
    assert isinstance(errors[0], provider.ProviderError)  # monitors catch New Relic's own error without re-wrapping it
    assert failed(errors[0]) == {"available": False, "reason": "Bad key.", "problems": [],
                                 "fix": "Check it with Test connection in Settings → Integrations."}
    assert failed(errors[2])["fix"] == ""
    assert unavailable("SN", [], enabled=False)["reason"] == "SN is turned off."
    assert unavailable("NR", ["API key missing"])["problems"] == ["API key missing"]
    assert unavailable("NR", []) is None


def test_the_plan_shows_what_will_be_read_without_reading_logs(nr_session):
    session = nr_session(GraphSession([nr_entity("g1", "shop-api-prod", "CRITICAL")], [], logs=[{"message": "never read"}]))
    session.event_types = [{"eventType": "Log"}, {"eventType": "Log_app"}, {"eventType": "Transaction"}]
    plan = monitors.plan(NR, APP_ID, "shop-api-prod", RESOURCES, 30, by_id=False)
    sent = [c["variables"].get("q", "") for c in session.calls]
    assert not any(q.startswith("SELECT") for q in sent)  # the plan builds queries, it never runs one
    assert plan["entities"] == [{"name": "shop-api-prod", "type": "Apm Application", "account": "Prod",
                                 "url": "https://one.newrelic.com/redirect/entity/g1", "how": "name", "apm": True}]
    assert plan["mentions"] and plan["logs"]["partitions"] == ["Log", "Log_app"]
    assert plan["logs"]["queries"][0]["nrql"].startswith("SELECT * FROM Log, Log_app WHERE (entity.guid IN ('g1') OR allColumnSearch(")
    errors = next(r for r in plan["reports"] if r["key"] == "errors")
    assert errors["runnable"] and errors["note"] == "" and "FROM TransactionError" in errors["queries"][0]["nrql"]
    no_search = monitors.plan(NR, APP_ID, "shop-api-prod", RESOURCES, 30, mentions=False, by_id=False)
    assert not no_search["mentions"] and "allColumnSearch" not in no_search["logs"]["queries"][0]["nrql"]


def test_reports_say_up_front_when_no_apm_app_matches(nr_session):
    infra = {**nr_entity("g9", "shop-api-prod", "CRITICAL"), "entityType": "INFRASTRUCTURE_HOST_ENTITY", "domain": "INFRA"}
    nr_session(GraphSession([infra], []))
    report = next(r for r in monitors.plan(NR, APP_ID, "shop-api-prod", RESOURCES, 30)["reports"] if r["key"] == "slow")
    assert report["runnable"] and "None of the matched entities is an APM app" in report["note"]
    assert "It still looks for apps named 'shop-api-prod'" in report["note"]


def test_load_older_reads_the_same_window_up_to_the_oldest_line(nr_session):
    session = nr_session(GraphSession([nr_entity("g1", "shop-api-prod", "CRITICAL")], [], logs=[]))
    first = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30)
    assert first["window"]["minutes"] == 30 and "SINCE 30 minutes ago" in session.calls[-1]["variables"]["q"]
    since, until = first["window"]["since"], first["window"]["since"] + 60_000
    older = monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 30, until=until, since=since)
    assert older["window"] == {"minutes": 30, "since": since, "until": until}
    assert f"SINCE {since} UNTIL {until} LIMIT 100" in session.calls[-1]["variables"]["q"]
    with pytest.raises(ValueError):  # a window wider than asked for, or in the future, is refused
        monitors.logs(NR, APP_ID, "shop-api-prod", RESOURCES, 15, until=until, since=since - 3_600_000)


def test_nothing_to_read_still_lists_the_partitions(nr_session):
    session = nr_session(GraphSession([], [], named=[]))
    session.event_types = [{"eventType": "Log"}, {"eventType": "Log_app"}]
    data = monitors.logs(NR, WEB_ID, "web", RESOURCES, 15, by_id=False)
    assert data["partitions"] == ["Log", "Log_app"] and data["queries"] == [] and "nothing to read" in data["note"]


# ------------------------------------------------------------------ Query from a New Relic entity or app name

def test_entities_found_by_name_stay_in_the_connections_accounts(nr_session):
    other = {**nr_entity("g7", "orders api", "WARNING"), "account": {"id": 99, "name": "Elsewhere"}}
    session = nr_session(GraphSession([], [], named=[nr_entity("g6", "orders api", "NOT_ALERTING"), other]))
    found = monitors.find_entities(NR, "orders api")
    assert [(e["name"], e["account_id"], e["apm"]) for e in found] == [("orders api", 1, True)]
    assert "name LIKE 'orders api'" in session.calls[0]["variables"]["q"]
    assert monitors.find_entities(NR, "x") == []


def test_an_entity_plan_shows_what_logs_and_reports_send(nr_session):
    nr_session(GraphSession([], []))
    plan = monitors.entity_plan(NR, ["MXxBUE18QVBQTElDQVRJT058MQ"], 1, "orders-api", apm=True)
    assert plan["available"] and plan["entities"][0]["how"] == "picked"
    assert "entity.guid IN ('MXxBUE18QVBQTElDQVRJT058MQ')" in plan["logs"]["queries"][0]["nrql"]
    errors = next(r for r in plan["reports"] if r["key"] == "errors")
    assert errors["runnable"] and "lower(appName) = 'orders-api'" in errors["queries"][0]["nrql"]
    not_apm = monitors.entity_plan(NR, ["MXxBUE18QVBQTElDQVRJT058MQ"], 1, "orders-api", apm=False)
    assert "isn't an APM app" in next(r for r in not_apm["reports"] if r["key"] == "errors")["note"]


@pytest.mark.parametrize("call", [
    lambda: monitors.entity_report(NR, ["MXxBUE18QVBQTElDQVRJT058MQ"], 2, 30, "errors", name="x"),
    lambda: monitors.entity_plan(NR, ["bad id!"], 1, "x", apm=True),
])
def test_entity_questions_check_the_account_and_ids(nr_session, call):
    nr_session(GraphSession([], []))
    with pytest.raises(ValueError):
        call()


# ------------------------------------------------------------------ an instance that isn't answering as ServiceNow


class HtmlResponse(Response):
    def __init__(self, status, html):
        super().__init__(status)
        self.text = html

    def json(self):
        raise ValueError("not JSON")


ASLEEP_PAGE = "<html><head><title>500 Internal Server Error</title></head><body><h1>500</h1></body></html>"


@pytest.mark.parametrize("response", [HtmlResponse(500, ASLEEP_PAGE),  # what a sleeping developer instance answered
                                      HtmlResponse(200, "<html><title>Instance Hibernating</title></html>"),
                                      Response(503)])
def test_an_instance_not_answering_as_servicenow_is_unreachable_with_a_readable_reason(keychain, response):
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw")
    client = servicenow.ServiceNowClient(sn_cfg(tables=["incident"]), Session([("/incident", response), ("sys_user", response)]))
    with pytest.raises(servicenow.ServiceNowError) as caught:
        client.table("incident")
    assert caught.value.kind == "unreachable" and "asleep" in str(caught.value) and "<html" not in str(caught.value)
    with pytest.raises(servicenow.ServiceNowError):  # Test connection says so too, instead of listing each table
        client.test()


def test_an_unreachable_instance_fails_the_whole_read_not_each_table(monkeypatch):
    def table(self, name, query="", fields=(), limit=100):
        raise servicenow.ServiceNowError("ServiceNow (dev1) isn't answering (500 Internal Server Error).", "unreachable", 500)

    monkeypatch.setattr(servicenow.ServiceNowClient, "table", table)
    with pytest.raises(servicenow.ServiceNowError) as caught:
        sn_data.collect(sn_cfg(), RESOURCES, refresh=True)
    answer = integrations.failed(caught.value)  # what every ServiceNow place shows: the tab, Triage, a resource
    assert answer["available"] is False and "isn't answering" in answer["reason"] and "Test connection" in answer["fix"]


class PagedSession:
    """A change_request table of `rows` rows, answered a page at a time like ServiceNow (optionally without a total)."""

    def __init__(self, rows, total_header=True):
        self.rows, self.total_header, self.asked = rows, total_header, []

    def request(self, method, url, params=None, **kwargs):
        offset, limit = int(params.get("sysparm_offset", 0)), int(params["sysparm_limit"])
        self.asked.append(offset)
        response = Response(200, {"result": [{"n": i} for i in range(offset, min(offset + limit, self.rows))]})
        response.headers = {"X-Total-Count": str(self.rows)} if self.total_header else {}
        return response


@pytest.mark.parametrize("total_header", [True, False])
def test_paged_reads_cover_every_row_once_and_stop_at_the_cap(keychain, total_header):
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw")
    session = PagedSession(1250, total_header)
    client = servicenow.ServiceNowClient(sn_cfg(tables=["change_request"]), session)
    rows, total = client.table_pages("change_request", "active=false^ORDERBYDESCclosed_at", page=500, cap=5000)
    assert [r["n"] for r in rows] == list(range(1250)) and total == 1250
    assert sorted(session.asked) == [0, 500, 1000]  # each page once, never overlapping
    capped, total = servicenow.ServiceNowClient(sn_cfg(tables=["change_request"]), PagedSession(1250, total_header)).table_pages(
        "change_request", "x^ORDERBYsys_id", page=500, cap=600)
    assert len(capped) == 600 and (total == 1250 if total_header else total >= 600)  # the page says only 600 of 1250


def test_a_capped_change_read_is_flagged_so_the_page_says_so(monkeypatch):
    sn_data._cache.clear()

    def pages(self, name, query="", fields=(), page=500, cap=5000):  # the cap reached on closed changes only
        return ([{"sys_id": {"value": f"c{i}"}} for i in range(cap)], 6363) if query.startswith("active=false") else ([], 0)

    monkeypatch.setattr(servicenow.ServiceNowClient, "table_pages", pages)
    monkeypatch.setattr(servicenow.ServiceNowClient, "table", lambda self, *a, **k: [])
    data = sn_data.collect(sn_cfg(tables=["incident", "change_request"], change_max_records=50), RESOURCES, refresh=True)
    assert data["truncated"]["change_closed"] and data["totals"]["change_closed"] == 6363
    assert not data["truncated"]["change_request"]


def test_records_counted_but_all_hidden_are_no_access_not_nothing(keychain):
    """ServiceNow counts matches before its read rules, then drops every row the account can't read."""
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw")
    hidden = Response(200, {"result": []})
    hidden.headers = {"X-Total-Count": "6363"}
    client = servicenow.ServiceNowClient(sn_cfg(username="svc.reader", tables=["change_request"]),
                                         Session([("/change_request", hidden), ("sys_user", Response(200, {"result": []}))]))
    with pytest.raises(servicenow.ServiceNowError) as caught:
        client.table("change_request", "active=false")
    assert caught.value.kind == "access" and "6,363" in str(caught.value) and "svc.reader" in str(caught.value)
    tested = client.test()  # Test connection: not readable, with that reason
    assert not tested["ok"] and "shows none" in tested["tables"][0]["detail"]
    empty = Response(200, {"result": []})
    empty.headers = {"X-Total-Count": "0"}
    assert servicenow.ServiceNowClient(sn_cfg(tables=["change_request"]), Session([("/change_request", empty)])).table(
        "change_request") == []  # truly nothing is still nothing


def hiding(names):
    """A table() for the named tables that ServiceNow's read rules hide; the others read as empty."""
    def table(self, name, query="", fields=(), limit=100):
        if name in names:
            raise servicenow.ServiceNowError(f"ServiceNow has 10 matching {name} records but shows none.", "access", 200)
        return []

    def pages(self, name, query="", fields=(), page=500, cap=5000):
        return table(self, name), 0
    return table, pages


def test_an_account_that_can_read_nothing_gets_one_message_with_what_to_ask_for(monkeypatch):
    sn_data._cache.clear()
    table, pages = hiding(set(config.SERVICENOW_TABLES))
    monkeypatch.setattr(servicenow.ServiceNowClient, "table", table)
    monkeypatch.setattr(servicenow.ServiceNowClient, "table_pages", pages)
    with pytest.raises(servicenow.ServiceNowError) as caught:
        sn_data.collect(sn_cfg(username="svc.reader"), RESOURCES, refresh=True)
    message = str(caught.value)
    assert caught.value.kind == "access" and message.startswith("svc.reader can't read any")
    assert "itil" in message and all(t in message for t in config.SERVICENOW_TABLES)
    assert "Test connection" in integrations.failed(caught.value)["fix"]


def test_an_account_that_can_read_some_tables_keeps_per_table_warnings(monkeypatch):
    sn_data._cache.clear()
    table, pages = hiding({"change_request", "task_ci", "change_task"})
    monkeypatch.setattr(servicenow.ServiceNowClient, "table", table)
    monkeypatch.setattr(servicenow.ServiceNowClient, "table_pages", pages)
    data = sn_data.collect(sn_cfg(), RESOURCES, refresh=True)
    assert "shows none" in data["notes"]["change_request"] and "incident" not in data["notes"]


def test_test_connection_says_what_access_to_ask_for_when_nothing_is_readable(keychain, monkeypatch):
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw")
    monkeypatch.setattr(servicenow.ServiceNowClient, "test", lambda self: {
        "ok": False, "who": "", "tables": [{"name": t, "ok": False, "detail": "shows none"} for t in ("incident", "change_request")]})
    integ = {"servicenow": [sn_cfg(username="svc.reader")], "monitors": []}
    answer = integrations.test(integ, "servicenow:ServiceNow")
    assert not answer["ok"] and "none of the tables are readable" in answer["message"]
    assert "Ask your ServiceNow admins" in answer["message"] and "svc.reader" in answer["message"]


def test_a_fully_hidden_table_is_judged_on_a_few_pages_then_remembered(keychain):
    secrets.put("servicenow", "dev1.service-now.com", "password", "pw")

    class Hidden(PagedSession):
        def request(self, method, url, params=None, **kwargs):
            response = super().request(method, url, params=params, **kwargs)
            response._payload = {"result": []}  # counted, then all removed by read rules
            return response

    session = Hidden(6363)
    client = servicenow.ServiceNowClient(sn_cfg(username="svc.reader", tables=["change_request"]), session)
    with pytest.raises(servicenow.ServiceNowError) as caught:
        client.table_pages("change_request", "x^ORDERBYsys_id", page=500, cap=5000)
    assert "none (the first 2,000)" in str(caught.value) and len(session.asked) == servicenow.PAGE_PARALLEL  # not 10 pages
    with pytest.raises(servicenow.ServiceNowError):
        client.table_pages("change_request", "y^ORDERBYsys_id", page=500, cap=5000)
    assert len(session.asked) == servicenow.PAGE_PARALLEL  # remembered: no new requests
    servicenow.forget_hidden()  # Refresh asks again
    with pytest.raises(servicenow.ServiceNowError):
        client.table_pages("change_request", "y^ORDERBYsys_id", page=500, cap=5000)
    assert len(session.asked) == 2 * servicenow.PAGE_PARALLEL


def test_incidents_are_read_up_to_their_own_cap_and_say_what_was_hidden(monkeypatch):
    sn_data._cache.clear()
    visible = [{"sys_id": cell(f"i{n}"), "number": cell(f"INC{n}"), "priority": cell("2")} for n in range(213)]

    def pages(self, name, query="", fields=(), page=500, cap=5000):
        return (visible, 22603) if name == "incident" else ([], 0)

    monkeypatch.setattr(servicenow.ServiceNowClient, "table_pages", pages)
    monkeypatch.setattr(servicenow.ServiceNowClient, "table", lambda self, *a, **k: [])
    data = sn_data.collect(sn_cfg(tables=["incident"], incident_max_records=500), RESOURCES, refresh=True)
    assert len(data["incidents"]) == 213 and data["caps"]["incident"] == 500
    assert data["truncated"]["incident"] and data["hidden"]["incident"] == 287  # of the first 500 asked for


def test_every_nerdgraph_query_closes_its_braces():
    class Recording(newrelic.NewRelicClient):
        def __init__(self):
            self.monitor, self.last_errors, self.sent = {"name": "NR", "account_ids": [1]}, [], []

        def query(self, text, variables=None):
            self.sent.append(text)
            return {}
    client = Recording()
    client.test()
    client.entities("name = 'x'", 10)
    client.open_issues(1, 3, 10)
    client.nrql_conditions(1, "x")
    client.nrql(1, "SELECT 1 FROM Log")
    assert len(client.sent) == 5
    for text in client.sent:
        depth = 0
        for ch in text:
            depth += {"{": 1, "}": -1}.get(ch, 0)
            assert depth >= 0, text
        assert depth == 0, text


def test_a_name_glued_to_more_text_by_an_underscore_still_matches():
    group = {"id": "/subscriptions/s/resourceGroups/shop-prod-cc-rg", "name": "shop-prod-cc-rg",
             "type": "microsoft.resources/subscriptions/resourcegroups"}
    underscored = {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/api_v2", "name": "api_v2",
                   "type": "microsoft.web/sites"}
    index, by_id = sn_data.name_index([group, underscored])
    text = "Operation 5xx errors | shop-prod-cc-rg_prod query result is > 4.0 for 10 minutes on - Operation 5xx errors -"
    assert [(m["name"], m["confidence"]) for m in sn_data.match([text], [], index, by_id)] == [("shop-prod-cc-rg", "name")]
    assert [m["name"] for m in sn_data.match(["api_v2 is down"], [], index, by_id)] == ["api_v2"]  # whole word still counts
