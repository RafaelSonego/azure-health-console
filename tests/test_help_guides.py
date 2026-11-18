import pytest
from flask import Flask

from healthconsole.core import config
from healthconsole.core.explore import help_guides
from healthconsole.web import create_blueprint
from tests.conftest import FakeClient, rid

DB = rid("rg", "Microsoft.Sql/servers", "srv/databases/db")
SERVICES = {"value": [
    {"name": "svc-sql", "properties": {"displayName": "SQL Database",
                                       "resourceTypes": ["MICROSOFT.SQL/SERVERS", "MICROSOFT.SQL/SERVERS/DATABASES"]}},
    {"name": "svc-web", "properties": {"displayName": "Web App", "resourceTypes": ["MICROSOFT.WEB/SITES"]}},
]}
PROBLEMS = {"value": [
    {"name": "p-restore", "properties": {"displayName": "Backup and Restore / Restore a database"}},
    {"name": "p-login", "properties": {"displayName": "Connectivity / Logins and users"}},
]}


def discovery(params):
    assert params == {"$filter": "ProblemClassificationId eq 'p-login'"}
    return {"value": [{"properties": {"solutions": [
        {"solutionId": "guide-1", "solutionType": "SelfHelp", "description": "Manage logins"},
        {"solutionId": "sol-1", "solutionType": "Solutions", "description": "Logins and users"},
        {"solutionId": "SqlLoginError18456", "solutionType": "Diagnostics", "description": "Login error 18456",
         "requiredInputs": ["server_name"]},
    ]}}]}


@pytest.fixture(autouse=True)
def fresh_catalogue():
    help_guides._catalogue.clear()
    yield
    help_guides._catalogue.clear()


def client():
    return FakeClient(gets={
        "/problemClassifications": PROBLEMS,
        "/providers/Microsoft.Support/services": SERVICES,
        "/providers/Microsoft.Help/discoverySolutions": discovery,
        "/providers/Microsoft.Help/selfHelp/guide-1": {"properties": {
            "title": "Logins", "content": '<h2>Logins</h2><p>Use <strong>contained</strong> users.</p><!--abc-->'
                                          '<ul><li><p>One <a href="https://learn.microsoft.com/x">doc</a></p></li>'
                                          '<li><a href="javascript:alert(1)">bad</a></li></ul>'}},
    })


def test_catalogue_walks_service_problem_solution_and_guide():
    fake = client()
    assert help_guides.services(fake, DB) == [{"id": "svc-sql", "name": "SQL Database"}]
    assert help_guides.problems(fake, "svc-sql")[1] == {"id": "p-login", "group": "Connectivity", "name": "Logins and users"}
    kinds = {s["id"]: s["kind"] for s in help_guides.solutions(fake, "p-login")}
    assert kinds == {"guide-1": "guide", "sol-1": "portal", "SqlLoginError18456": "portal"}  # selfHelp refuses Solutions
    guide = help_guides.guide(fake, "guide-1")
    assert guide["title"] == "Logins"
    assert guide["text"] == ("#### Logins\n\nUse **contained** users.\n\n- One [doc](https://learn.microsoft.com/x)\n\n- bad")


def test_catalogue_is_read_once():
    fake = client()
    help_guides.services(fake, DB)
    help_guides.services(fake, rid("rg", "Microsoft.Web/sites", "app"))
    assert fake.call_count == 1


def test_guides_route_steps_and_rejects_odd_ids():
    fake = client()
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: fake, config.load("/nonexistent")))
    web = app.test_client()
    assert web.get(f"/api/explore/guides?id={DB}").get_json()["services"][0]["name"] == "SQL Database"
    assert len(web.get(f"/api/explore/guides?id={DB}&service=svc-sql").get_json()["problems"]) == 2
    assert len(web.get(f"/api/explore/guides?id={DB}&problem=p-login").get_json()["solutions"]) == 3
    assert web.get(f"/api/explore/guides?id={DB}&guide=guide-1").get_json()["title"] == "Logins"
    assert web.get(f"/api/explore/guides?id={DB}&guide=../../subscriptions").status_code == 400
