import json
import threading
import time

import pytest
from flask import Flask

from healthconsole import signin
from healthconsole.core import config


class FakeProcess:
    """Stands in for `az login`: prints `lines`, then exits with `code` once `release` is set."""

    def __init__(self, lines, code=0):
        self.lines, self.code = lines, code
        self.release = threading.Event()
        self.terminated = False

    @property
    def stdout(self):
        for line in self.lines:
            yield line + "\n"
        self.release.wait(5)

    def wait(self, timeout=None):
        self.release.wait(timeout or 5)
        return -15 if self.terminated else self.code

    def poll(self):
        return (-15 if self.terminated else self.code) if self.release.is_set() else None

    def terminate(self):
        self.terminated = True
        self.release.set()

    def kill(self):
        self.terminate()


def until(check, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def test_command_is_fixed_and_the_tenant_is_checked(monkeypatch):
    assert signin.command(az="/usr/bin/az") == ["/usr/bin/az", "login", "--allow-no-subscriptions", "--output", "none"]
    assert signin.command("contoso.onmicrosoft.com", True, az="az")[-3:] == ["--tenant", "contoso.onmicrosoft.com",
                                                                            "--use-device-code"]
    assert "--tenant" in signin.command("11111111-2222-3333-4444-555555555555", az="az")
    for bad in ("x; rm -rf /", "--allow-no-subscriptions", "a b", "-t", "contoso"):
        with pytest.raises(signin.SignInError):
            signin.command(bad, az="az")
    monkeypatch.setattr(signin.shutil, "which", lambda name: None)
    with pytest.raises(signin.SignInError, match="isn't installed"):
        signin.command()


class Ran:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_accounts_list_every_signed_in_tenant_once_with_its_account():
    sent = []
    rows = [{"tenantId": "t-work", "tenantDisplayName": "Work", "user": {"name": "me@work.example"}},
            {"tenantId": "t-work", "tenantDisplayName": "Work", "user": {"name": "me@work.example"}},
            {"tenantId": "t-home", "tenantDefaultDomain": "home.onmicrosoft.com", "user": {"name": "me@home.example"}}]
    found = signin.accounts(az="az", run=lambda argv, **kw: sent.append(argv) or Ran(json.dumps(rows)))
    assert sent == [["az", "account", "list", "--all", "--output", "json"]]
    assert [(t["name"], t["account"]) for t in found] == [("home.onmicrosoft.com", "me@home.example"), ("Work", "me@work.example")]


def test_logout_checks_the_user_name_and_reports_the_cli_error():
    sent = []
    signin.logout("me@home.example", az="az", run=lambda argv, **kw: sent.append(argv) or Ran())
    assert sent == [["az", "logout", "--username", "me@home.example"]]
    for bad in ("", "me; rm -rf /", "--all", "not an email"):
        with pytest.raises(signin.SignInError):
            signin.logout(bad, az="az", run=lambda argv, **kw: Ran())
    with pytest.raises(signin.SignInError, match="not logged in"):
        signin.logout("me@home.example", az="az", run=lambda argv, **kw: Ran(returncode=1, stderr="ERROR: User me is not logged in"))


def test_device_code_is_read_and_success_refreshes(monkeypatch):
    monkeypatch.setattr(signin.shutil, "which", lambda name: "/usr/bin/az")
    process = FakeProcess(["\x1b[93mTo sign in, use a web browser to open the page https://microsoft.com/devicelogin and "
                           "enter the code ABCD12345 to authenticate.\x1b[0m"])
    seen, refreshed = {}, []

    def popen(argv, **kwargs):
        seen.update(argv=argv, env=kwargs["env"], stdin=kwargs["stdin"])
        return process

    login = signin.AzLogin(on_success=lambda: refreshed.append(True), popen=popen)
    login.start(device_code=True)
    assert seen["argv"][-1] == "--use-device-code" and seen["env"]["AZURE_CORE_LOGIN_EXPERIENCE_V2"] == "off"
    assert seen["stdin"] == signin.subprocess.DEVNULL
    assert until(lambda: login.state()["code"] == "ABCD12345")
    assert login.state()["url"] == "https://microsoft.com/devicelogin"
    with pytest.raises(signin.SignInError):
        login.start()  # one at a time
    process.release.set()
    assert until(lambda: login.state()["status"] == "done") and until(lambda: refreshed == [True])


def test_failure_and_cancel(monkeypatch):
    monkeypatch.setattr(signin.shutil, "which", lambda name: "/usr/bin/az")
    failing = FakeProcess(["ERROR: AADSTS50020: user account doesn't exist in tenant"], code=1)
    login = signin.AzLogin(popen=lambda argv, **kw: failing)
    login.start("contoso.example.com")
    failing.release.set()
    assert until(lambda: login.state()["status"] == "failed")
    assert "AADSTS50020" in login.state()["message"]

    waiting = FakeProcess([])
    login = signin.AzLogin(popen=lambda argv, **kw: waiting)
    login.start()
    assert login.cancel()["status"] == "cancelled" and waiting.terminated


SKIPPED_LINE = ("WARNING: Authentication failed against tenant 485f5c0c-7ef2-40a3-8039-708bd1035907 'Home': AADSTS50076: "
                "Due to a configuration change made by your administrator, you must use multi-factor authentication.")


def test_a_tenant_az_login_skips_is_named_and_an_empty_sign_in_is_a_failure(monkeypatch):
    monkeypatch.setattr(signin.shutil, "which", lambda name: "/usr/bin/az")
    process = FakeProcess(["WARNING: A web browser has been opened", SKIPPED_LINE, SKIPPED_LINE], code=0)
    refreshed = []
    login = signin.AzLogin(on_success=lambda: refreshed.append(True), popen=lambda argv, **kw: process,
                           list_accounts=lambda: [])
    login.start()
    process.release.set()
    assert until(lambda: login.state()["status"] == "failed")
    state = login.state()
    assert state["skipped"] == [{"id": "485f5c0c-7ef2-40a3-8039-708bd1035907", "name": "Home",
                                 "reason": SKIPPED_LINE.split("'Home': ")[1], "mfa": True}]
    assert "no account was saved: Home" in state["message"] and refreshed == []


def test_a_sign_in_that_saved_an_account_still_lists_the_skipped_tenant(monkeypatch):
    monkeypatch.setattr(signin.shutil, "which", lambda name: "/usr/bin/az")
    process = FakeProcess([SKIPPED_LINE.replace("AADSTS50076", "AADSTS53003").replace("multi-factor", "a compliant")])
    refreshed = []
    login = signin.AzLogin(on_success=lambda: refreshed.append(True), popen=lambda argv, **kw: process,
                           list_accounts=lambda: [{"id": "t-work"}])
    login.start()
    process.release.set()
    assert until(lambda: login.state()["status"] == "done") and until(lambda: refreshed == [True])
    assert [(t["name"], t["mfa"]) for t in login.state()["skipped"]] == [("Home", False)]


def test_only_the_local_app_has_sign_in(monkeypatch):
    from healthconsole.web import create_blueprint, create_local_app
    from tests.conftest import FakeClient

    hosted = Flask("hosted")
    hosted.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"), authorize=lambda: None))
    assert hosted.test_client().get("/api/signin").status_code == 404

    app = create_local_app(config.load("/nonexistent"), port=8765)
    local = app.test_client()
    headers = {"Host": "127.0.0.1:8765"}
    assert local.get("/api/signin", headers=headers).status_code == 401  # no launch key yet
    headers = {**headers, "X-AHC-Key": app.config["AHC_LAUNCH_KEY"]}
    assert local.get("/api/signin", headers=headers).get_json() == {"available": True, "status": "idle", "output": [], "skipped": []}
    bad = local.post("/api/signin", json={"tenant": "a; b"}, headers=headers)
    assert bad.status_code == 400 and "tenant" in bad.get_json()["error"]
    assert local.post("/api/signin", data="tenant=x", headers=headers).status_code == 415  # JSON only
    assert local.get("/api/signin", headers={"Host": "evil.example:8765"}).status_code == 403


def test_a_job_signed_out_midway_says_so():
    from healthconsole.core import jobs
    from healthconsole.core.credentials import CredentialError
    runner = jobs.JobRunner()

    def work(progress, publish):
        raise CredentialError("The Azure CLI couldn't provide a token. Run `az login` and try again.", "signed_out", "az login")
    job_id = runner.start("me", work)
    for _ in range(200):
        job = runner.get(job_id, "me")
        if job["status"] != jobs.RUNNING:
            break
        time.sleep(0.01)
    assert job["status"] == jobs.FAILED and job["credential"] == {"reason": "signed_out", "command": "az login"}
    other = runner.start("me", lambda progress, publish: 1 / 0)
    for _ in range(200):
        if runner.get(other, "me")["status"] != jobs.RUNNING:
            break
        time.sleep(0.01)
    assert runner.get(other, "me")["credential"] is None
