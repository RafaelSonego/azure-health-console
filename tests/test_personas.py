import pytest
from flask import Flask

from healthconsole.core import config, personas
from healthconsole.web import create_blueprint


def test_built_ins_use_only_names_the_console_knows():
    assert personas.problems(personas.BUILT_IN) == []
    assert {p["name"] for p in personas.merged()} == set(personas.BUILT_IN)
    assert all(p["home"] in p["views"] for p in personas.merged())


def test_a_config_can_change_a_built_in_and_add_its_own():
    merged = {p["name"]: p for p in personas.merged({"sre": {"home": "investigate"},
                                                       "finops": {"label": "FinOps", "views": ["cost", "summary"]}})}
    assert merged["sre"]["home"] == "investigate" and merged["sre"]["label"] == "SRE / on-call"
    assert merged["finops"]["views"][0] == "cost" and merged["finops"]["built_in"] is False


@pytest.mark.parametrize("entry,words", [
    ({"x": {"views": ["triage"]}}, "needs at least a label"),
    ({"sre": {"views": ["nope"]}}, "unknown ['nope']"),
    ({"sre": {"home": "nowhere"}}, "home: unknown"),
    ({"sre": {"colour": "red"}}, "unknown key(s) ['colour']"),
    ({"sre": {"detail": "loud"}}, "detail: choose from"),
])
def test_config_mistakes_are_named(entry, words):
    with pytest.raises(config.ConfigError, match=None) as raised:
        config.from_data({"personas": entry})
    assert words in str(raised.value)


def test_route(tmp_path):
    app = Flask("t", static_folder=None)
    app.register_blueprint(create_blueprint(lambda t: None, config.from_data({"personas": {"sre": {"home": "investigate"}}}),
                                            config_file=tmp_path / "c.yaml", authorize=lambda: None))
    body = app.test_client().get("/api/personas").get_json()
    assert body["default"] == "everything" and next(p for p in body["personas"] if p["name"] == "sre")["home"] == "investigate"
    assert body["configured"] == ""


def test_the_persona_to_open_with_is_a_defined_one(tmp_path):
    assert config.from_data({"persona": "manager"})["persona"] == "manager"
    assert config.from_data({"persona": "finops", "personas": {"finops": {"label": "FinOps", "views": ["cost"]}}})["persona"] == "finops"
    with pytest.raises(config.ConfigError, match="persona 'nobody' isn't defined"):
        config.from_data({"persona": "nobody"})
    app = Flask("t", static_folder=None)
    app.register_blueprint(create_blueprint(lambda t: None, config.from_data({"persona": "sre"}), config_file=tmp_path / "c.yaml", authorize=lambda: None))
    assert app.test_client().get("/api/personas").get_json()["configured"] == "sre"


def test_settings_saves_the_persona_and_refuses_an_unknown_one(tmp_path):
    from tests.conftest import FakeClient
    app = Flask("t", static_folder=None)
    app.register_blueprint(create_blueprint(lambda t=None: FakeClient(), config.from_data({}), config_file=tmp_path / "c.yaml", authorize=lambda: None))
    web = app.test_client()
    field = next(f for f in web.get("/api/settings").get_json()["fields"] if f["name"] == "persona")
    assert field["value"] == "" and field["type"] == "text"
    assert web.post("/api/settings", json={"values": {"persona": "security"}}).status_code == 200
    assert "persona: security" in (tmp_path / "c.yaml").read_text()
    assert web.get("/api/personas").get_json()["configured"] == "security"
    refused = web.post("/api/settings", json={"values": {"persona": "nobody"}})
    assert refused.status_code == 400 and "isn't defined" in refused.get_json()["error"]
