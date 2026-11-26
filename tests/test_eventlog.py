import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from healthconsole.core import config, eventlog, jobs
from healthconsole.core.guarded import GuardedClient, WriteBlocked
from tests.conftest import FakeClient


@pytest.fixture
def log(tmp_path):
    eventlog.configure(tmp_path / "logs", keep_days=30)
    yield tmp_path / "logs"
    eventlog.configure(None)


def test_off_by_default_writes_nothing(tmp_path):
    eventlog.configure(None)
    assert eventlog.record_error("x", source="server") is None
    eventlog.audit("settings_save", kind="change", outcome=200)
    eventlog.count_request("ARM", "GET", "https://management.azure.com/subscriptions", 200)
    assert eventlog.read("errors") == [] and not list(tmp_path.iterdir())


def test_errors_get_an_id_hide_credentials_and_stay_private(log):
    try:
        raise ValueError("boom with Bearer abc.def.ghi and https://x.blob.core.windows.net/c?sv=1&sig=SECRETSIG")
    except ValueError as error:
        error_id = eventlog.record_error(str(error), source="server", error=error, user="ana", user_id="t/1")
    [row] = eventlog.read("errors")
    assert row["id"] == error_id and len(error_id) == 8 and row["type"] == "ValueError" and "raise ValueError" in row["stack"]
    text = json.dumps(row)
    assert "abc.def.ghi" not in text and "SECRETSIG" not in text and "Bearer [hidden]" in text and "sig=[hidden]" in text
    [file] = log.glob("errors-*.jsonl")
    assert stat.S_IMODE(os.stat(file).st_mode) == 0o600 and stat.S_IMODE(os.stat(log).st_mode) == 0o700


def test_audit_reads_newest_first_and_filters_by_user(log):
    eventlog.audit("settings_save", kind="change", outcome=200, user="ana", user_id="t/1", detail={"changed": ["a.b"]})
    eventlog.audit("access_user", kind="sensitive", outcome=202, user="bo", user_id="t/2")
    assert [e["action"] for e in eventlog.read("audit")] == ["access_user", "settings_save"]
    assert [e["user"] for e in eventlog.read("audit", user_id="t/1")] == ["ana"]


@pytest.mark.parametrize("url, pattern", [
    ("https://management.azure.com/subscriptions/0b1c2d3e-0000-0000-0000-000000000000/resourceGroups/shop-rg/providers/"
     "Microsoft.Web/sites/shop-web/config/web?api-version=1", "/subscriptions/{…}/resourceGroups/{…}/providers/Microsoft.Web/sites/{…}/config/{…}"),
    ("https://management.azure.com/providers/Microsoft.ResourceGraph/resources", "/providers/Microsoft.ResourceGraph/resources"),
    ("https://management.azure.com/subscriptions/s/resourceGroups/r/providers/Microsoft.ContainerService/managedClusters/c/"
     "providers/Microsoft.Insights/metrics", "/subscriptions/{…}/resourceGroups/{…}/providers/Microsoft.ContainerService/"
                                            "managedClusters/{…}/providers/Microsoft.Insights/metrics"),
    ("https://aks.example/api/v1/namespaces/shop/pods/web-1/log", "/api/v1/namespaces/{…}/pods/{…}/log"),
    ("https://aks.example/api/v1/pods", "/api/v1/pods"),
    ("https://dev.service-now.com/api/now/table/incident", "/api/now/table/incident"),
])
def test_paths_group_names_and_ids(url, pattern):
    assert eventlog.path_pattern(url) == pattern


class _Refusing(GuardedClient):
    label = "ARM"

    def check(self, method, url, **request):
        if method != "GET":
            raise WriteBlocked("not a read")


class _Session:
    def __init__(self):
        self.sent = []

    def request(self, method, url, **kwargs):
        self.sent.append((method, url))
        return type("R", (), {"status_code": 200, "headers": {}})()


def test_requests_are_counted_by_service_method_and_path_and_refusals_never_leave(log):
    session, client = _Session(), _Refusing()
    for name in ("a", "b"):
        client.send(session, "GET", f"https://management.azure.com/subscriptions/{name}/resourcegroups", 5)
    with pytest.raises(WriteBlocked):
        client.send(session, "DELETE", "https://management.azure.com/subscriptions/a/resourceGroups/r", 5)
    assert [m for m, _ in session.sent] == ["GET", "GET"]
    rows = {(r["method"], r["path"], r["outcome"]): r["count"] for r in eventlog.read("requests")}
    assert rows == {("GET", "/subscriptions/{…}/resourcegroups", "2xx"): 2,
                    ("DELETE", "/subscriptions/{…}/resourceGroups/{…}", "refused"): 1}


def test_todays_counts_carry_on_after_a_restart(log):
    eventlog.count_request("ARM", "GET", "https://management.azure.com/subscriptions", 200)
    eventlog.flush()
    eventlog.configure(log, keep_days=30)
    eventlog.count_request("ARM", "GET", "https://management.azure.com/subscriptions", 200)
    assert [r["count"] for r in eventlog.read("requests")] == [2]


def test_days_past_keep_days_are_deleted(log):
    old = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%d")
    recent = (datetime.now(timezone.utc) - timedelta(days=29)).strftime("%Y-%m-%d")
    for day in (old, recent):
        (log / f"audit-{day}.jsonl").write_text("{}\n")
    eventlog.prune()
    assert sorted(f.name for f in log.iterdir()) == [f"audit-{recent}.jsonl"]


def test_a_failed_job_says_its_error_id(log):
    runner = jobs.JobRunner()

    def work(progress, publish):
        raise RuntimeError("the read broke")
    job_id = runner.start("t/1", work, kind="cost")
    import time
    for _ in range(100):
        job = runner.get(job_id, "t/1")
        if job["status"] != jobs.RUNNING:
            break
        time.sleep(0.01)
    [row] = eventlog.read("errors")
    assert job["status"] == jobs.FAILED and f"error ID {row['id']}" in job["error"]
    assert row["source"] == "job" and row["user_id"] == "t/1" and row["context"] == {"job": "cost"}


def _http(config_file):
    """Always a scratch config file: without one the blueprint would save to the real one."""
    from healthconsole.web import create_blueprint
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant=None: FakeClient(), config.load("/nonexistent"),
                                            config_file=str(config_file), authorize=lambda: None))
    return app, app.test_client()


def test_a_bug_answers_with_an_error_id_and_lands_in_the_log(log, tmp_path):
    app, http = _http(tmp_path / "config.yaml")

    def broken():
        raise KeyError("missing")
    app.view_functions["healthconsole.logs_view"] = broken  # any blueprint route hitting a bug
    answer = http.get("/api/logs")
    [row] = eventlog.read("errors")
    assert answer.status_code == 500 and answer.json["kind"] == "internal" and answer.json["error_id"] == row["id"]
    assert f"Error ID {row['id']}" in answer.json["error"] and row["context"] == {"method": "GET", "path": "/api/logs"}
    assert row["type"] == "KeyError" and "raise KeyError" in row["stack"]


def test_actions_are_audited_with_their_kind_and_reads_are_not(log, tmp_path):
    _, http = _http(tmp_path / "config.yaml")
    http.post("/api/cache/clear", json={})
    saved = http.post("/api/settings", json={"values": {"triage.auto_refresh_minutes": 5}})
    http.get("/api/settings")
    events = eventlog.read("audit")
    assert saved.status_code == 200 and (tmp_path / "config.yaml").is_file()
    assert [(e["action"], e["kind"], e["outcome"]) for e in events] == [("settings_save", "change", 200), ("cache_clear", "change", 200)]
    assert events[0]["detail"] == {"changed": ["triage.auto_refresh_minutes"]} and events[0]["user"]


def test_a_new_log_folder_or_keep_days_applies_when_saved(tmp_path):
    eventlog.configure(config.DEFAULTS["logs"]["dir"], 30)  # running with the settings' own folder
    _, http = _http(tmp_path / "config.yaml")
    moved = tmp_path / "moved"
    assert http.post("/api/settings", json={"values": {"logs.dir": str(moved), "logs.keep_days": 14}}).status_code == 200
    assert eventlog.folder() == moved and eventlog.read("audit")[0]["action"] == "settings_save"


def test_page_errors_are_logged_a_few_a_minute(log, tmp_path):
    _, http = _http(tmp_path / "config.yaml")
    answers = [http.post("/api/client-errors", json={"message": f"x is undefined {i}", "where": "js/cost.js:10:5",
                                                     "stack": "at f (cost.js:10)"}) for i in range(35)]
    rows = eventlog.read("errors", limit=100)
    assert len(rows) == 30 and answers[0].json["error_id"] and answers[-1].json.get("dropped")
    assert rows[-1]["source"] == "page" and rows[-1]["stack"] == "at f (cost.js:10)"
    assert eventlog.read("audit") == []  # an error, not an action


def test_logs_api_reads_each_kind(log, tmp_path):
    _, http = _http(tmp_path / "config.yaml")
    eventlog.audit("cache_clear", kind="change", outcome=200)
    answer = http.get("/api/logs?kind=audit&days=7").json
    assert answer["enabled"] and answer["days"] == 7 and answer["rows"][0]["action"] == "cache_clear"
    assert http.get("/api/logs?kind=nope").status_code == 400
