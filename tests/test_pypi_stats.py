import pytest
from flask import Flask

from healthconsole.core import config, pypi_stats
from healthconsole.core.guarded import ServiceError, WriteBlocked
from healthconsole.web import create_blueprint
from tests.conftest import FakeClient

PYPI = {"info": {"version": "1.1.1"}, "releases": {
    "1.1.0": [{"upload_time_iso_8601": "2026-11-16T20:00:00Z"}], "1.1.1": [{"upload_time_iso_8601": "2026-11-17T21:00:00Z"}],
    "0.9.0": []}}  # a version with no files was never published
RECENT = {"data": {"last_day": 12, "last_week": 40, "last_month": 198}}
OVERALL = {"data": [{"category": "without_mirrors", "date": "2026-11-17", "downloads": 150},
                    {"category": "with_mirrors", "date": "2026-11-17", "downloads": 999},
                    {"category": "without_mirrors", "date": "2026-11-16", "downloads": 48}]}


@pytest.fixture(autouse=True)
def fresh():
    pypi_stats._cache.clear()
    yield
    pypi_stats._cache.clear()


def test_summary_counts_without_mirrors_and_published_releases():
    found = pypi_stats.summarise(PYPI, RECENT, OVERALL)
    assert (found["latest"], found["releases"], found["first_release"]) == ("1.1.1", 2, "2026-11-16")
    assert (found["last_day"], found["last_month"], found["total"], found["counted_from"]) == (12, 198, 198, "2026-11-16")


def test_only_the_three_public_reads_pass_the_guard():
    pypi_stats._CLIENT.check("GET", pypi_stats.URLS["recent"])
    for method, url in (("POST", pypi_stats.URLS["recent"]), ("GET", "https://pypi.org/pypi/other-package/json"),
                        ("GET", "https://evil.example/api/packages/azure-health-console/recent")):
        with pytest.raises(WriteBlocked):
            pypi_stats._CLIENT.check(method, url)


def test_a_failure_is_reported_and_kept_briefly(monkeypatch):
    calls = []

    def down(name, session=None):
        calls.append(name)
        raise ServiceError("pypistats.org couldn't be reached from this machine.", "network")
    monkeypatch.setattr(pypi_stats, "fetch", down)
    assert "couldn't be reached" in pypi_stats.read()["error"]
    pypi_stats.read()
    assert calls == ["pypi"]


def test_about_route(monkeypatch):
    monkeypatch.setattr(pypi_stats, "fetch", lambda name, session=None: {"pypi": PYPI, "recent": RECENT, "overall": OVERALL}[name])
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"), authorize=lambda: None))
    body = app.test_client().get("/api/about/pypi").get_json()
    assert body["latest"] == "1.1.1" and body["error"] is None and body["last_week"] == 40


def test_newer_compares_numbers_not_text():
    assert pypi_stats.newer("1.1.10", "1.1.9") and pypi_stats.newer("1.2.0", "1.1.0")
    assert not pypi_stats.newer("1.1.1", "1.1.2") and not pypi_stats.newer("1.1.1", "1.1.1")
    assert not pypi_stats.newer("1.1.1", "unknown") and not pypi_stats.newer("", "1.1.0")


@pytest.mark.parametrize("prefix,method", [
    ("/Users/me/.local/pipx/venvs/azure-health-console", "pipx"),
    ("C:\\Users\\me\\pipx\\venvs\\azure-health-console", "pipx"),
    ("/opt/homebrew/Cellar/azure-health-console/1.1.1/libexec", "brew"),
    ("/Users/me/project/.venv", "pip"),
])
def test_install_method_from_where_python_lives(prefix, method):
    assert pypi_stats.install_method(prefix, checkout=False) == method
    assert pypi_stats.install_method(prefix, checkout=True) == "source"


def test_update_says_how_to_upgrade_and_reads_once(monkeypatch):
    calls = []
    monkeypatch.setattr(pypi_stats, "install_method", lambda: "pipx")
    monkeypatch.setattr(pypi_stats, "fetch", lambda name, session=None: calls.append(name) or PYPI)
    found = pypi_stats.update("1.1.0")
    assert found["newer"] and found["latest"] == "1.1.1" and found["command"] == ""  # pipx upgrade checks nothing
    assert found["steps"].endswith("#pipx-windows-linux-or-a-mac-without-homebrew")
    assert found["notes"].endswith("/releases/tag/v1.1.1")
    pypi_stats.update("1.1.0")
    assert calls == ["pypi"]
    assert pypi_stats.update("1.1.0", enabled=False)["checked"] is False


def test_a_checkout_never_asks(monkeypatch):
    monkeypatch.setattr(pypi_stats, "install_method", lambda: "source")
    monkeypatch.setattr(pypi_stats, "fetch", lambda *a, **k: pytest.fail("no read from a checkout"))
    assert pypi_stats.update("1.1.2") == {"current": "1.1.2", "method": "source", "newer": False, "latest": "",
                                          "checked": False, "reason": "Running from a checkout: its VERSION file is the version."}


def test_update_route(monkeypatch):
    monkeypatch.setattr(pypi_stats, "install_method", lambda: "brew")
    monkeypatch.setattr(pypi_stats, "fetch", lambda name, session=None: PYPI)
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"), authorize=lambda: None))
    body = app.test_client().get("/api/update").get_json()
    assert body["command"] == "brew upgrade azure-health-console" and body["checked"]


def test_unreachable_names_why_python_failed_where_a_browser_did_not():
    import requests

    class Session:
        def __init__(self, error):
            self.error = error

        def request(self, *args, **kwargs):
            raise self.error

    def said(error):
        with pytest.raises(ServiceError) as caught:
            pypi_stats.fetch("recent", session=Session(error))
        return str(caught.value)

    assert "pypistats.org" in said(requests.exceptions.SSLError("cert")) and "certificate" in said(requests.exceptions.SSLError("x"))
    assert "HTTPS_PROXY" in said(requests.exceptions.ProxyError("x"))
    assert "no answer in time" in said(requests.exceptions.ConnectTimeout("x"))
    assert "DNS" in said(requests.exceptions.ConnectionError("Failed to resolve 'pypistats.org'"))
