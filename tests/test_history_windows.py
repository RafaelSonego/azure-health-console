"""How far back each history reads, and that a long window reaches the sources (each keeps its own limit)."""
from datetime import datetime, timedelta, timezone

from flask import Flask

from healthconsole.core import config
from healthconsole.core.explore import activity, aks, appinsights
from healthconsole.core.investigate import source
from healthconsole.core.integrations import provider
from tests.conftest import FakeClient
from tests.test_appinsights import COMPONENT, Client, table

RESOURCE = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/shop"


def test_windows_reach_thirty_days():
    assert source.WINDOWS["30d"] == 720 and appinsights.WINDOWS["14d"] == 336
    assert activity.HOURS[-1] == 720 and aks.CI_HOURS[-1] == 720 and provider.LOG_WINDOWS[-1] == 10080


def test_thirty_days_of_app_insights_skip_the_last_week_comparison():
    spans = []

    def answers(section, timespan):
        spans.append((section, timespan))
        return {"requests": table(["role", "total", "failed", "p95"], [["api", 10, 1, 5.0]]),
                "exceptions": table(["type"], []), "dependencies": table(["type"], []),
                "availabilityResults": table(["test"], [])}[section]
    data = appinsights.insights(Client(answers), COMPONENT, "30d")
    assert data["errors"] == [] and data["roles"][0]["week"] is None
    start, end = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in spans[0][1].split("/"))
    assert timedelta(days=29) < end - start <= timedelta(days=30)
    assert sum(1 for s, _ in spans if s == "requests") == 2  # roles and failing operations; no week-earlier read


class TimelineClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.pages = []

    def get_pages(self, path, api_version, params=None, max_pages=10, timeout=None):
        self.pages.append(params["$filter"])
        return [], False


def test_timeline_reads_thirty_days_but_changes_stop_at_fourteen():
    client = TimelineClient()
    out = activity.activity(client, RESOURCE, 720)
    changes = next(q for q in client.queries if "resourcechanges" in q)
    alerts = next(q for q in client.queries if "AlertsManagementResources" in q)
    assert "ago(336h)" in changes and "ago(720h)" in alerts
    assert any("14 days" in e for e in out["errors"])
    first = min(f.split("'")[1] for f in client.pages)  # the Activity Log's oldest slice starts 30 days back
    assert datetime.now(timezone.utc) - datetime.fromisoformat(first.replace("Z", "+00:00")) > timedelta(days=29.9)


def test_timeline_route_accepts_the_new_windows_only():
    from healthconsole.web import create_blueprint
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: TimelineClient(), config.load("/nonexistent"), authorize=lambda: None))
    http = app.test_client()
    assert http.get(f"/api/explore/activity?id={RESOURCE}&hours=336").status_code == 200
    assert http.get(f"/api/explore/activity?id={RESOURCE}&hours=48").status_code == 400
    assert http.get(f"/api/explore/workloads?id={RESOURCE}&ci_hours=5").status_code == 400


def test_container_insights_look_back(monkeypatch):
    sent = []

    def logs(client, cluster_id, query, timespan="PT1H"):
        sent.append((query, timespan))
        return [{"x": 1}]
    monkeypatch.setattr(aks, "resource_logs", logs)
    out = aks.container_insights(None, "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/c", 168)
    restarts = next((q, t) for q, t in sent if "ContainerRestartCount" in q)
    events = next((q, t) for q, t in sent if "KubeEvents" in q)
    assert "ago(168h)" in restarts[0] and restarts[1] == "P7D" and "ago(168h)" in events[0]
    assert out["hours"] == {"restarts": 168, "oomkilled": 168, "events": 168}
    sent.clear()
    out = aks.container_insights(None, "c", None)  # unpicked: the windows it always had
    assert any("ago(1h)" in q and "ContainerRestartCount" in q for q, _ in sent)
    assert any("ago(24h)" in q and "OOMKilled" in q for q, _ in sent)
    assert out["hours"]["oomkilled"] == 24
