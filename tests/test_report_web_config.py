from pathlib import Path
import time

import pytest

from healthconsole.core import config, report
from healthconsole.core.model import CRITICAL, HEALTHY, Edge, Node, Signal, Snapshot
from healthconsole.web import create_blueprint, create_local_app
from tests.conftest import FakeClient, rid
from tests.test_discovery import APP, PLAN, estate


def sample():
    app = Node(id=APP.lower(), name="shop", type="microsoft.web/sites", display_type="Web App",
               hop=0, direction="root", status=CRITICAL,
               signals=[Signal("profile", "Http5xx (Total)", CRITICAL, "too many | errors", 70, "Count", "> 50", 5)])
    plan = Node(id=PLAN.lower(), name="plan", type="microsoft.web/serverfarms",
                display_type="App Service Plan", hop=1, status=HEALTHY)
    return Snapshot(root_id=app.id, tenant_id="t", taken_by="me", taken_at="2026-10-30 21:00 UTC",
                    depth=1, nodes=[app, plan], edges=[Edge(app.id, plan.id, "runs on")])


def test_markdown_has_mermaid_and_escaped_tables():
    text = report.to_markdown(sample())
    assert "```mermaid" in text
    assert "n0 -->|runs on| n1" in text
    assert "too many \\| errors" in text
    assert report.filename(sample(), "md") == "shop-202610302100.md"


def test_svg_and_html_render():
    html = report.to_html(sample())
    assert "<svg" in html and "shop" in html


def test_config_rejects_unknown_keys(tmp_path):
    file = tmp_path / "c.yaml"
    file.write_text("graph:\n  dept: 3\n")
    with pytest.raises(config.ConfigError):
        config.load(file)
    file.write_text("tenants:\n  ABC:\n    favourite_subscriptions: [Prod]\n")
    settings = config.load(file)  # still read, until moved into saved scopes
    assert config.legacy_scope(settings, {"id": "abc"})["scopes"][0]["subscriptions"] == ["Prod"]
    file.write_text("tenants:\n  ABC:\n    subscriptions: [Prod]\n")
    with pytest.raises(config.ConfigError):
        config.load(file)


class WebClient(FakeClient):
    def refresh(self):
        pass


@pytest.fixture
def local_app(monkeypatch):
    settings = config.load("/nonexistent")
    client = WebClient(estate(), gets={"/tenants": {"value": []}})
    app = create_local_app(settings, port=8765)
    # Swap the az-login client for the fake one.
    app.blueprints.clear()
    from flask import Flask
    flask_app = Flask("test")
    flask_app.before_request_funcs = app.before_request_funcs
    flask_app.register_blueprint(create_blueprint(lambda tenant: client, settings, authorize=lambda: None))
    key = app.config["AHC_LAUNCH_KEY"]

    from flask.testing import FlaskClient

    class Keyed(FlaskClient):  # the page the console opened: it sends this start's key
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.environ_base["HTTP_X_AHC_KEY"] = key
    flask_app.test_client_class = Keyed
    return flask_app


def test_only_a_page_holding_the_key_can_use_the_api_and_it_survives_restarts():
    from healthconsole.web import launch_url
    app = create_local_app(config.load("/nonexistent"), port=8765)
    host = {"Host": "127.0.0.1:8765"}
    other = app.test_client()  # another user or program reaching 127.0.0.1
    refused = other.get("/api/signin", headers=host)
    assert refused.status_code == 401 and refused.get_json()["kind"] == "launch-key"  # the page shows how to open it
    assert other.post("/api/settings", json={"values": {}}, headers=host).status_code == 401
    assert other.get("/", headers=host).status_code == 200  # the page itself carries no data
    assert other.get(f"/?key={app.config['AHC_LAUNCH_KEY']}", headers=host).status_code == 200  # a query opens nothing
    assert other.get("/api/signin", query_string={"key": app.config["AHC_LAUNCH_KEY"]}, headers=host).status_code == 401
    assert other.get("/api/signin", headers={**host, "X-AHC-Key": "guess"}).status_code == 401
    key = app.config["AHC_LAUNCH_KEY"]
    assert len(key) >= 32 and create_local_app(config.load("/nonexistent"), port=8765).config["AHC_LAUNCH_KEY"] == key
    key_file = Path(config.load("/nonexistent")["cache_dir"]).expanduser() / "launch-key"
    assert key_file.read_text() == key and oct(key_file.stat().st_mode & 0o777) == "0o600"  # only this user reads it
    url = launch_url(app, 8765)
    assert url == f"http://127.0.0.1:8765/#key={key}"  # a fragment: the browser never sends it to the server
    page = app.test_client()
    opened = page.get("/", headers=host)
    assert opened.status_code == 200 and "Set-Cookie" not in opened.headers  # the page keeps the key, not a cookie
    keyed = {**host, "X-AHC-Key": key}
    assert page.get("/api/signin", headers=keyed).status_code == 200
    assert page.get("/api/signin", headers=host).status_code == 401  # a cookie or nothing: no key


def test_a_browser_with_the_old_cookie_is_handed_the_key_once_and_the_cookie_alone_opens_nothing():
    app = create_local_app(config.load("/nonexistent"), port=8765)
    key, host = app.config["AHC_LAUNCH_KEY"], {"Host": "127.0.0.1:8765"}
    browser = app.test_client()
    browser.set_cookie("ahc_key", key, domain="127.0.0.1")
    assert browser.get("/api/signin", headers=host).status_code == 401
    moved = browser.get("/", headers=host)
    assert moved.status_code == 302 and moved.headers["Location"] == f"/#key={key}"
    assert "ahc_key=;" in moved.headers["Set-Cookie"] and "Max-Age=0" in moved.headers["Set-Cookie"]


def test_other_sites_and_ports_are_refused_even_with_the_key():
    app = create_local_app(config.load("/nonexistent"), port=8765)
    keyed = {"Host": "127.0.0.1:8765", "X-AHC-Key": app.config["AHC_LAUNCH_KEY"]}
    page = app.test_client()
    for site in ("cross-site", "same-site"):  # same-site: a page on another port of 127.0.0.1
        assert page.get("/api/signin", headers={**keyed, "Sec-Fetch-Site": site}).status_code == 403
    assert page.get("/api/signin", headers={**keyed, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    evil = page.post("/api/signin/cancel", json={}, headers={**keyed, "Origin": "http://127.0.0.1:9999"})
    assert evil.status_code == 403
    assert page.post("/api/signin/cancel", json={}, headers={**keyed, "Origin": "http://127.0.0.1:8765"}).status_code == 200
    assert page.get("/integrations/servicenow/callback?state=x", headers={"Host": "127.0.0.1:8765"}).status_code == 400


def test_the_blueprint_needs_the_hosts_check_and_runs_it_first():
    from flask import Flask, jsonify
    with pytest.raises(TypeError):
        create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"))
    hosted = Flask("hosted")
    hosted.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"),
                                               authorize=lambda: (jsonify(error="sign in"), 401)))
    web = hosted.test_client()
    assert web.get("/api/subscriptions").status_code == 401
    assert web.post("/api/cache/clear", data="x").status_code == 415  # a form can't reach an action


def test_loopback_guard(local_app):
    test = local_app.test_client()
    assert test.get("/", headers={"Host": "evil.example.com"}).status_code == 403
    assert test.post("/api/refresh", data="x", headers={"Host": "127.0.0.1:8765"}).status_code == 415


def test_a_resource_group_check_finishes_with_its_baseline(local_app):
    from tests.conftest import SUB
    test = local_app.test_client()
    host = {"Host": "127.0.0.1:8765"}
    job_id = test.post("/api/snapshots", json={"resource_group": f"{SUB}/app-rg"}, headers=host).get_json()["job_id"]
    for _ in range(50):
        body = test.get(f"/api/snapshots/{job_id}", headers=host).get_json()
        if body["status"] != "running":
            break
        time.sleep(0.05)
    assert body["status"] == "done", body  # compare_last_week is on by default, so the baseline runs
    assert body["snapshot"]["baseline"]["window_minutes"] == config.load("/nonexistent")["health"]["window_minutes"]


def test_snapshot_job_and_markdown_export(local_app):
    test = local_app.test_client()
    host = {"Host": "127.0.0.1:8765"}
    started = test.post("/api/snapshots", json={"resource_id": APP, "depth": 1}, headers=host)
    assert started.status_code == 202
    job_id = started.get_json()["job_id"]
    for _ in range(50):
        body = test.get(f"/api/snapshots/{job_id}", headers=host).get_json()
        if body["status"] != "running":
            break
        time.sleep(0.05)
    assert body["status"] == "done", body
    assert body["snapshot"]["root_id"] == APP.lower()
    exported = test.get(f"/api/snapshots/{job_id}/export?format=md", headers=host)
    assert exported.status_code == 200
    assert b"```mermaid" in exported.data


def test_old_tag_filters_become_part_of_the_favourites_scope(tmp_path):
    file = tmp_path / "c.yaml"
    file.write_text("tenants:\n  T1:\n    tag_filters: {Owner: team-a}\n")
    scope = config.legacy_scope(config.load(file), {"id": "t1"})["scopes"][0]
    assert scope == {"name": "Favourites", "subscriptions": ["*"], "resource_groups": [], "tag_filters": {"owner": "team-a"},
                     "connections": {}}
    assert config.legacy_scope(config.load(file), {"id": "t2"}) is None  # per tenant
    file.write_text("resource_group_tags: [owner]\n")
    with pytest.raises(config.ConfigError):
        config.load(file)


def test_resource_groups_carry_lowercased_tags():
    from healthconsole.core import inventory
    client = FakeClient(query_handlers=[("resourcegroups", lambda q: [
        {"id": "/subscriptions/S/resourceGroups/rg", "name": "rg", "subscriptionId": "S",
         "location": "canadacentral", "tags": {"Owner": "team-a"}},
        {"id": "/subscriptions/S/resourceGroups/rg2", "name": "rg2", "subscriptionId": "S", "tags": None}])])
    groups = inventory.list_resource_groups(client, ["s"])
    assert groups[0]["tags"] == {"owner": "team-a"}
    assert groups[1]["tags"] == {}


def test_pdf_is_pure_python_and_survives_non_latin_text():
    snap = sample()
    snap.nodes[0].signals.append(Signal("profile", "Latency ≥ 2s – check…", "warning", "café → 日本 ✓"))
    snap.warnings.append("A note — with “quotes”")
    content = report.render(snap, "pdf")
    assert content.startswith(b"%PDF")
    assert len(content) > 2000


def test_printable_page(local_app):
    test = local_app.test_client()
    host = {"Host": "127.0.0.1:8765"}
    job_id = test.post("/api/snapshots", json={"resource_id": APP, "depth": 1}, headers=host).get_json()["job_id"]
    for _ in range(50):
        if test.get(f"/api/snapshots/{job_id}", headers=host).get_json()["status"] != "running":
            break
        time.sleep(0.05)
    page = test.get(f"/api/snapshots/{job_id}/print", headers=host)
    assert page.status_code == 200 and b"Save as PDF" in page.data and b"<svg" in page.data
    pdf = test.get(f"/api/snapshots/{job_id}/export?format=pdf", headers=host)
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")


def test_old_favourites_match_patterns_and_tenant_by_domain(tmp_path):
    file = tmp_path / "c.yaml"
    file.write_text('tenants:\n  contoso.onmicrosoft.com:\n    favourite_subscriptions: ["app-*-002", "APP-*-003"]\n'
                    '  "*":\n    favourite_subscriptions: [sandbox]\n')
    settings = config.load(file)
    subs = [{"id": "1", "name": "app-dev-002"}, {"id": "2", "name": "app-prod-003"},
            {"id": "3", "name": "app-dev-004"}, {"id": "4", "name": "sandbox"}]
    contoso = {"id": "t1", "domain": "contoso.onmicrosoft.com", "name": "Contoso"}
    favourites = config.find_scope(settings, contoso, "favourites")
    assert favourites["legacy"] and config.scope_subscription_ids(favourites, subs) == ["1", "2"]
    assert config.scope_subscription_ids(config.find_scope(settings, {"id": "other"}, "Favourites"), subs) == ["4"]


def test_open_with_follows_the_old_triage_scope_until_set(tmp_path):
    file = tmp_path / "c.yaml"
    t1 = {"id": "t1"}
    file.write_text("triage:\n  scope: favourites\ntenants:\n  T1:\n    favourite_subscriptions: [prod]\n")
    assert config.open_with(config.load(file), t1) == "Favourites"
    file.write_text("triage:\n  scope: favourites\n")  # no favourites: it always fell back to every subscription
    assert config.open_with(config.load(file), t1) == "all"
    assert config.legacy_scope(config.load(file), t1) == {"scopes": [], "open_with": "all", "keys": ["triage.scope"]}
    file.write_text("triage:\n  scope: all\ntenants:\n  T1:\n    open_with: Peak\n    favourite_subscriptions: [prod]\n")
    settings = config.load(file)
    assert config.open_with(settings, t1) == "Peak" and config.legacy_scope(settings, t1) is None
    assert config.open_with(config.load(tmp_path / "none.yaml"), t1) == "none"


def test_scope_names_none_and_all_are_reserved(tmp_path):
    file = tmp_path / "c.yaml"
    file.write_text("tenants:\n  T1:\n    saved_scopes:\n      - {name: All, subscriptions: [a]}\n")
    with pytest.raises(config.ConfigError, match="reserved"):
        config.load(file)


def test_move_turns_old_favourites_into_saved_scopes(tmp_path):
    from healthconsole.core import settings_file
    file = tmp_path / "c.yaml"
    file.write_text("# keep me\ntriage:\n  scope: favourites\ntenants:\n  t1:\n    favourite_subscriptions: [prod-*]\n"
                    "    favourite_resource_groups: [shop-*]\n    tag_filters: {owner: team-a}\n"
                    "    saved_scopes:\n      - {name: Favourites, subscriptions: [x]}\n")
    t1 = {"id": "t1"}
    legacy = config.legacy_scope(config.load(file), t1)
    assert [s["name"] for s in legacy["scopes"]] == ["Favourites (moved)", "Favourite groups"]
    settings = settings_file.move_legacy_scope(file, "t1", legacy)
    assert config.legacy_scope(settings, t1) is None and config.open_with(settings, t1) == "Favourites (moved)"
    assert [(s["name"], s["resource_groups"], s["tag_filters"]) for s in config.saved_scopes(settings, t1)] == [
        ("Favourites", [], {}), ("Favourites (moved)", [], {"owner": "team-a"}), ("Favourite groups", ["shop-*"], {})]
    text = file.read_text()
    assert "favourite_subscriptions" not in text and "tag_filters: {owner" not in text and "# keep me" in text


def test_cli_scope_choice(tmp_path):
    file = tmp_path / "c.yaml"
    subs = [{"id": "1", "name": "prod-a"}, {"id": "2", "name": "dev-a"}]
    file.write_text("tenants:\n  t1:\n    open_with: Prod\n    saved_scopes:\n      - {name: Prod, subscriptions: [prod-*]}\n")
    settings = config.load(file)
    assert config.chosen_subscriptions(settings, {"id": "t1"}, subs) == ("saved scope Prod", ["1"])
    assert config.chosen_subscriptions(settings, {"id": "t2"}, subs) == ("nothing (Open with)", [])
    with pytest.raises(config.ConfigError):
        config.chosen_subscriptions(settings, {"id": "t1"}, subs, "Nope")


def test_triage_scope_is_validated(tmp_path):
    file = tmp_path / "c.yaml"
    file.write_text("triage:\n  scope: everything\n")
    with pytest.raises(config.ConfigError):
        config.load(file)


def test_settings_save_keeps_comments_validates_and_backs_up(tmp_path):
    from healthconsole.core import settings_file
    file = tmp_path / "config.yaml"
    file.write_text('# my notes\ntenants:\n  "*":\n    favourite_subscriptions:\n      - "*-002"   # all envs\n'
                    "health:\n  window_minutes: 15   # keep me\n")
    settings = settings_file.apply(file, values={"health.window_minutes": 30, "graph.depth": 3})
    text = file.read_text()
    assert "# my notes" in text and "# keep me" in text and '"*-002"   # all envs' in text
    assert settings["health"]["window_minutes"] == 30 and settings["graph"]["depth"] == 3
    assert (tmp_path / "config.yaml.bak").read_text().count("window_minutes: 15") == 1
    settings_file.apply(file, values={"health.window_minutes": settings_file.RESET})
    assert "window_minutes" not in file.read_text()


def test_settings_save_refuses_invalid_values(tmp_path):
    from healthconsole.core import settings_file
    file = tmp_path / "config.yaml"
    file.write_text("graph:\n  depth: 2\n")
    for values in ({"graph.depth": "deep"}, {"nonsense.key": 1}, {"triage.scope": "all"}):  # Open with replaced it
        with pytest.raises(config.ConfigError):
            settings_file.apply(file, values=values)
    assert file.read_text() == "graph:\n  depth: 2\n"


def test_settings_tab_saves_and_applies_live(tmp_path):
    from flask import Flask
    file = tmp_path / "config.yaml"
    file.write_text("# team settings\nhealth:\n  window_minutes: 15\n")
    settings = config.load(file)
    client = WebClient(estate(), gets={"/tenants": {"value": [{"tenantId": "tenant-1", "displayName": "Contoso",
                                                              "defaultDomain": "contoso.onmicrosoft.com"}]},
                                       "/subscriptions": {"value": [{"subscriptionId": "s1", "displayName": "app-dev-002",
                                                                     "state": "Enabled"},
                                                                    {"subscriptionId": "s2", "displayName": "other",
                                                                     "state": "Enabled"}]}})
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, settings, config_file=file, authorize=lambda: None))
    test = app.test_client()

    view = test.get("/api/settings").get_json()
    assert view["exists"] and view["tenant"]["suggested_key"] == "contoso.onmicrosoft.com"
    assert next(f for f in view["fields"] if f["name"] == "health.window_minutes")["value"] == 15

    saved = test.post("/api/settings", json={"values": {"health.window_minutes": 30}})
    assert saved.status_code == 200
    assert settings["health"]["window_minutes"] == 30            # applied live, same dict
    assert "# team settings" in file.read_text() and (tmp_path / "config.yaml.bak").exists()

    # The page adds the tenant ID to every POST body.
    plain = test.post("/api/settings", json={"tenant": "tenant-1", "values": {"health.window_minutes": 20}})
    assert plain.status_code == 200 and settings["health"]["window_minutes"] == 20

    bad = test.post("/api/settings", json={"values": {"graph.depth": -1}})
    assert bad.status_code == 400 and "whole number" in bad.get_json()["error"]
    assert settings["graph"]["depth"] == 2


def test_open_with_and_moving_old_favourites_from_the_page(tmp_path):
    from flask import Flask
    file = tmp_path / "config.yaml"
    file.write_text("triage:\n  scope: favourites\ntenants:\n  contoso.onmicrosoft.com:\n    favourite_subscriptions: [app-*]\n")
    settings = config.load(file)
    client = WebClient(estate(), gets={"/tenants": {"value": [{"tenantId": "tenant-1", "displayName": "Contoso",
                                                              "defaultDomain": "contoso.onmicrosoft.com"}]},
                                       "/subscriptions": {"value": [{"subscriptionId": "s1", "displayName": "app-dev-002",
                                                                     "state": "Enabled"}]}})
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, settings, config_file=file, authorize=lambda: None))
    test = app.test_client()
    context = test.get("/api/context").get_json()
    assert context["open_with"] == "Favourites" and context["legacy_scope"]["keys"] == ["favourite_subscriptions", "triage.scope"]
    assert [(s["name"], s.get("legacy")) for s in context["saved_scopes"]] == [("Favourites", True)]

    # Saving the list never writes the old favourites' scope: it lives in the old keys until Move.
    saved = test.post("/api/saved-scopes", json={"scopes": [{"name": "Peak", "subscriptions": ["app-dev-002"]},
                                                           {**context["saved_scopes"][0]}]}).get_json()
    assert [s["name"] for s in saved["saved_scopes"]] == ["Peak", "Favourites"] and "legacy" not in file.read_text()

    assert test.post("/api/open-with", json={"open_with": "Nope"}).status_code == 400
    moved = test.post("/api/saved-scopes/move-legacy", json={}).get_json()
    assert moved["legacy_scope"] is None and moved["open_with"] == "Favourites"
    assert [s.get("legacy") for s in moved["saved_scopes"]] == [None, None]
    assert test.post("/api/saved-scopes/move-legacy", json={}).status_code == 400
    assert test.post("/api/open-with", json={"open_with": "Peak"}).get_json()["open_with"] == "Peak"
    assert "open_with: Peak" in file.read_text()


def test_context_reports_the_version():
    from flask import Flask

    import healthconsole
    from tests.conftest import FakeClient
    client = FakeClient(gets={"/tenants": {"value": []}, "/subscriptions": {"value": []}})
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: client, config.load("/nonexistent"), authorize=lambda: None))
    version = app.test_client().get("/api/context").get_json()["version"]
    assert version == healthconsole.__version__ == open("VERSION").read().strip()


def test_triage_auto_refresh_takes_listed_minutes_only():
    assert config.from_data({"triage": {"auto_refresh_minutes": 5}})["triage"]["auto_refresh_minutes"] == 5
    assert config.load("/nonexistent")["triage"]["auto_refresh_minutes"] == 0
    for bad in (3, "5", True, -1):
        with pytest.raises(config.ConfigError):
            config.from_data({"triage": {"auto_refresh_minutes": bad}})
