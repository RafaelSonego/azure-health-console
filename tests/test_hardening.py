"""The 2026-11-26 security scan's findings, one test (or a few) each: they fail if the fix is undone."""
import os
import socket
import stat
import threading
import time
from datetime import datetime, timezone

import pytest
import requests

from healthconsole.core import checks, config, guarded, jobs, localfiles, profiles, readiness
from healthconsole.core.arm import WriteBlocked
from healthconsole.core.explore import dependencies, keyvault, kube
from healthconsole.core.integrations import servicenow
from healthconsole.core.model import CRITICAL, INFO
from tests.conftest import SUB, FakeClient, rid

NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)
CLUSTER = rid("rg-aks", "Microsoft.ContainerService/managedClusters", "aks1")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ------------------------------------------------------------------ Key Vault expiry (#11, and the inconclusive one)

def entry(exp):
    return {"name": "s", "properties": {"attributes": {"enabled": True, "exp": exp}}}


def test_an_epoch_zero_expiry_is_a_real_date_long_expired():
    item = keyvault._item(entry(0), 30, NOW)
    assert item["expires"] == "1970-01-01" and item["status"] == CRITICAL and item["days_left"] < 0


@pytest.mark.parametrize("exp", ["soon", 10 ** 20, float("nan"), True, [1]])
def test_an_expiry_that_isnt_a_usable_number_is_named_not_a_crash(exp):
    item = keyvault._item(entry(exp), 30, NOW)
    assert item["status"] == INFO and item["days_left"] is None and "isn't a date" in item["note"]


# ------------------------------------------------------------------ Readiness's Key Vault scan is bounded (#7)

def vault_client(names, asked):
    vaults = [{"id": rid("rg", "Microsoft.KeyVault/vaults", n), "name": n, "resourceGroup": "rg", "subscriptionId": SUB}
              for n in names]

    def listing(path):
        return lambda params: asked.append(path) or {"value": []}

    gets = {f"/vaults/{n}/{kind}": listing(f"{n}/{kind}") for n in names for kind in ("secrets", "keys")}
    return FakeClient(query_handlers=[("microsoft.keyvault/vaults", lambda q: vaults)], gets=gets)


def test_readiness_reads_at_most_the_vault_limit_and_says_which_were_left():
    asked = []
    items, notes, summary = readiness.certificate_section(
        vault_client(["a", "b", "c", "d", "e"], asked), [SUB], None, NOW.date(), 30, False, lambda m: None,
        today=NOW.date(), max_vaults=2)
    assert sorted({p.split("/")[0] for p in asked}) == ["a", "b"]
    assert any("3 of 5 vaults not read" in n and "c, d, e" in n for n in notes)
    assert summary.startswith("2 vault(s)")


def test_readiness_without_secrets_doesnt_list_keys():
    asked = []
    readiness.certificate_section(vault_client(["a"], asked), [SUB], None, NOW.date(), 30, False, lambda m: None,
                                  today=NOW.date(), max_vaults=10)
    assert asked == ["a/secrets"]  # certificates are found through the secrets list
    asked.clear()
    readiness.certificate_section(vault_client(["a"], asked), [SUB], None, NOW.date(), 30, True, lambda m: None,
                                  today=NOW.date(), max_vaults=10)
    assert sorted(asked) == ["a/keys", "a/secrets"]


def test_the_secrets_view_scan_still_reads_every_vault():
    asked = []
    result = checks.keyvault_scan(vault_client(["a", "b", "c"], asked), [SUB], 30)
    assert result["vaults"] == 3 and result["skipped"] == [] and len(asked) == 6


# ------------------------------------------------------------------ Resource Graph values are literals (#10)

def test_a_crafted_cluster_id_never_reaches_the_query():
    client = FakeClient()
    for crafted in (CLUSTER + "' or id != '", CLUSTER + "\" or 1==1 //", f"/subscriptions/{SUB}/resourcegroups/rg",
                    "/subscriptions/x/../providers/microsoft.containerservice/managedclusters/a"):
        with pytest.raises(ValueError, match="isn't an AKS cluster"):
            dependencies._cluster(client, crafted)
    assert client.queries == []


def test_a_cluster_query_is_a_literal_scoped_to_its_subscription():
    seen = {}
    client = FakeClient(query_handlers=[("managedclusters", lambda q: [{"id": CLUSTER, "name": "aks1", "subscriptionId": SUB,
                                                                        "issuer": ""}, {"id": "/other", "name": "x"}])])
    client.query = lambda kql, subscriptions=None, limit=None: seen.update(kql=kql, subs=subscriptions) or [
        {"id": CLUSTER, "name": "aks1", "subscriptionId": SUB}, {"id": CLUSTER + "2", "name": "other"}]
    row = dependencies._cluster(client, CLUSTER)
    assert row["name"] == "aks1" and seen["subs"] == [SUB] and f'id =~ "{CLUSTER}"' in seen["kql"]


# ------------------------------------------------------------------ jobs can't pile up (#13)

def test_a_caller_gets_a_clear_refusal_past_the_running_limit_and_a_running_job_is_never_dropped():
    runner = jobs.JobRunner(max_jobs=2, max_running_per_owner=3, max_running=4)
    release = threading.Event()

    def slow(progress, publish):
        release.wait(5)
        return 1

    mine = [runner.start("me", slow, kind=f"k{i}") for i in range(3)]
    with pytest.raises(jobs.TooManyJobs, match="already running"):
        runner.start("me", slow, kind="k4")
    runner.start("you", slow)
    with pytest.raises(jobs.TooManyJobs):  # the whole process's limit
        runner.start("them", slow)
    assert all(runner.get(job_id, "me") for job_id in mine)  # over max_jobs, but running: kept
    release.set()
    for _ in range(200):
        if not runner._active:
            break
        time.sleep(0.01)
    assert runner._active == {}
    assert runner.get(runner.start("me", lambda p, s: 2), "me")


def test_the_api_answers_429_when_too_many_jobs_run(monkeypatch):
    from healthconsole.web import create_blueprint
    from flask import Flask
    app = Flask("t", static_folder=None)
    app.register_blueprint(create_blueprint(lambda tenant=None: FakeClient(), config.load("/nonexistent"),
                                            authorize=lambda: None))
    monkeypatch.setattr(jobs.JobRunner, "start", lambda *a, **k: (_ for _ in ()).throw(jobs.TooManyJobs("3 checks")))
    answer = app.test_client().post("/api/readiness", json={"subscriptions": [SUB], "peak_end": "2099-01-01"})
    assert answer.status_code == 429 and answer.get_json()["kind"] == "busy"


# ------------------------------------------------------------------ Kubernetes answers have a ceiling (#12)

def test_a_kubernetes_answer_over_the_limit_is_refused_while_reading(monkeypatch):
    monkeypatch.setattr(kube, "MAX_RESPONSE_BYTES", 10)
    closed = []

    class Big:
        status_code, headers, encoding = 200, {}, "utf-8"

        def iter_content(self, size):
            yield b'{"items": ['
            yield b"[]" * 1000
            raise AssertionError("read past the limit")

        def raise_for_status(self):
            pass

        def close(self):
            closed.append(True)

    client = kube.KubeClient.__new__(kube.KubeClient)
    client.server, client._token_source = "https://api.example", lambda: "t"
    client._session = type("S", (), {"request": lambda *a, **k: Big()})()
    with pytest.raises(kube.KubeUnavailable, match="over 0 MB.*one namespace"):
        client.get("/api/v1/pods")
    assert closed == [True]


# ------------------------------------------------------------------ ServiceNow never goes to a local address (#1)

@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "x.localhost", "127.0.0.1", "127.9.9.9:8443", "[::1]",
                                  "169.254.169.254", "0.0.0.0", "[::ffff:127.0.0.1]"])
def test_a_local_servicenow_host_is_refused_before_sending(host):
    with pytest.raises(WriteBlocked, match="local address"):
        guarded.refuse_local_host("ServiceNow", host)


@pytest.mark.parametrize("host", ["dev1.service-now.com", "10.1.2.3", "servicenow.corp.example:8443", "192.168.1.5"])
def test_an_internal_or_public_servicenow_host_is_allowed(host):
    guarded.refuse_local_host("ServiceNow", host)  # an internal instance over the VPN keeps working


def test_a_name_that_resolves_to_a_local_address_gets_nothing():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    session = requests.Session()
    session.mount("https://", guarded.NotLocalAdapter())
    try:
        with pytest.raises(requests.ConnectionError, match="a local address; nothing was sent"):
            session.get(f"https://localhost:{port}/api/now/table/incident", auth=("me", "secret"), timeout=5)
        listener.settimeout(1)
        connection, _ = listener.accept()
        connection.settimeout(1)
        assert connection.recv(1024) == b""  # not even the TLS hello, so no credential
        connection.close()
    finally:
        listener.close()


def test_the_servicenow_client_uses_the_adapter_and_drops_userinfo():
    client = servicenow.ServiceNowClient({"instance": "https://user:pw@dev1.service-now.com/nav_to.do",
                                          "tables": ["incident"], "auth": "basic", "username": "me", "name": "SN"})
    assert client.instance == "https://dev1.service-now.com" and client.host == "dev1.service-now.com"
    assert isinstance(client._session.get_adapter("https://dev1.service-now.com"), guarded.NotLocalAdapter)
    assert servicenow.normalise_instance("https://sn.corp.example:8443/x") == "https://sn.corp.example:8443"
    assert servicenow.normalise_instance("https://sn.corp.example:notaport") == ""
    with pytest.raises(WriteBlocked, match="local address"):
        servicenow.ServiceNowClient({"instance": "https://127.0.0.1", "tables": ["incident"], "auth": "basic",
                                     "username": "me", "name": "SN"}).check("GET", "https://127.0.0.1/api/now/table/incident")


# ------------------------------------------------------------------ files other accounts could change (#5, #6, #16–#20)

def test_a_folder_others_can_write_above_the_data_folder_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    with pytest.raises(localfiles.UnsafePath, match="can be changed by other users.*chmod go-w"):
        localfiles.private_dir(shared / "cache")


def test_a_folder_of_another_user_is_refused_not_used(tmp_path, monkeypatch):
    folder = tmp_path / "cache"
    folder.mkdir()
    monkeypatch.setattr(localfiles, "_mine", lambda: os.getuid() + 1)  # as if it were someone else's
    with pytest.raises(localfiles.UnsafePath, match="belongs to another user"):
        localfiles.private_dir(folder)


def test_a_launch_key_file_that_isnt_private_is_replaced(tmp_path):
    from healthconsole.web import _launch_key
    planted = "k" * 40
    path = tmp_path / "launch-key"
    path.write_text(planted)
    os.chmod(path, 0o644)  # readable by others
    first = _launch_key(path)
    assert first != planted and path.read_text() == first and mode(path) == 0o600
    assert _launch_key(path) == first  # a private one is kept across restarts


def test_a_symlinked_launch_key_is_never_followed(tmp_path):
    from healthconsole.web import _launch_key
    target = tmp_path / "known.txt"
    target.write_text("k" * 40)
    os.chmod(target, 0o600)
    path = tmp_path / "launch-key"
    path.symlink_to(target)
    key = _launch_key(path)
    assert key != "k" * 40 and not path.is_symlink() and target.read_text() == "k" * 40


def test_a_launch_key_in_a_shared_cache_lasts_one_start_only(tmp_path):
    from healthconsole.web import _launch_key
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    (shared / "cache").mkdir()
    key = _launch_key(shared / "cache" / "launch-key")
    assert len(key) >= 32 and not (shared / "cache" / "launch-key").exists()


def test_settings_under_a_folder_others_can_change_are_refused_and_your_own_are_tightened(tmp_path):
    team = tmp_path / "team"
    (team / "ahc").mkdir(parents=True)
    (team / "ahc" / "config.yaml").write_text("default_tenant: contoso.example\n")
    os.chmod(team, 0o777)  # anyone could rename ahc/ and put their own config.yaml in its place
    try:
        with pytest.raises(config.ConfigError, match="changed by other users"):
            config.load(team / "ahc" / "config.yaml")
    finally:
        os.chmod(team, 0o755)
    os.chmod(team / "ahc", 0o777)
    os.chmod(team / "ahc" / "config.yaml", 0o666)
    assert config.load(team / "ahc" / "config.yaml")["default_tenant"] == "contoso.example"
    assert mode(team / "ahc") == 0o755 and mode(team / "ahc" / "config.yaml") == 0o644  # yours: others lose write


def test_a_huge_settings_file_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(localfiles, "MAX_TRUSTED_BYTES", 100)
    path = tmp_path / "config.yaml"
    path.write_text("# " + "x" * 200 + "\n")
    with pytest.raises(config.ConfigError, match="over the 100"):
        config.load(path)


def test_a_watchlist_others_can_change_isnt_read(tmp_path):
    from healthconsole.core.watchlist import Watchlist
    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "watchlist.yaml"
    path.write_text("items: []\n")
    os.chmod(tmp_path, 0o777)
    try:
        with pytest.raises(localfiles.UnsafePath):
            Watchlist(path).items()
    finally:
        os.chmod(tmp_path, 0o755)


def test_profiles_others_can_change_or_too_big_are_refused(tmp_path, monkeypatch):
    folder = tmp_path / "profiles"
    folder.mkdir()
    (folder / "vm.yaml").write_text("type: microsoft.compute/virtualmachines\nmetrics: []\n")
    assert "microsoft.compute/virtualmachines" in profiles.load(folder)
    os.chmod(folder / "vm.yaml", 0o666)
    profiles.load(folder)
    assert mode(folder / "vm.yaml") == 0o644
    monkeypatch.setattr(profiles, "MAX_PROFILE_BYTES", 10)
    with pytest.raises(profiles.ProfileError, match="over the 10"):
        profiles.load(folder)


@pytest.mark.parametrize("operator,warn,crit", [("GreaterThan", 90, 80), ("LessThan", 10, 20)])
def test_a_profile_whose_critical_comes_before_its_warning_is_refused(operator, warn, crit):
    with pytest.raises(profiles.ProfileError, match="must be past"):
        profiles.parse({"type": "t", "metrics": [{"name": "m", "aggregation": "Average", "operator": operator,
                                                  "warn": warn, "crit": crit}]})


def test_cost_and_audit_caches_another_account_could_swap_are_ignored(tmp_path, monkeypatch):
    from healthconsole.core import audit_store
    path = audit_store.path_for(tmp_path, "t", "o")
    audit_store._save(path, {"format": audit_store.FORMAT, "scopes": {"x": {}}})
    assert audit_store._load(path)["scopes"] == {"x": {}}
    monkeypatch.setattr(localfiles, "_mine", lambda: os.getuid() + 1)
    assert audit_store._load(path)["scopes"] == {}


# ------------------------------------------------------------------ signed releases (#9) and the verified install

def signed_release(tmp_path, raw_signature=False):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = ec.generate_private_key(ec.SECP256R1())
    files = {"azure_health_console-9.9.9-py3-none-any.whl": b"wheel", "azure_health_console-9.9.9.tar.gz": b"sdist",
             "requirements-release.txt": b"flask==3.1.0 --hash=sha256:00\n"}
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    import hashlib
    sums = "".join(f"{hashlib.sha256(c).hexdigest()}  {n}\n" for n, c in files.items()).encode()
    (tmp_path / "SHA256SUMS.txt").write_bytes(sums)
    signature = key.sign(sums, ec.ECDSA(hashes.SHA256()))
    if raw_signature:  # what Key Vault's sign returns: r and s, 32 bytes each
        r, s = decode_dss_signature(signature)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    (tmp_path / "SHA256SUMS.txt.sig").write_bytes(signature)
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


@pytest.mark.parametrize("raw", [False, True])
def test_a_signed_release_verifies_and_names_each_file(tmp_path, raw):
    from healthconsole import release
    pem = signed_release(tmp_path, raw)
    (tmp_path / "azure_health_console-9.9.9.tar.gz").unlink()
    assert dict(release.verify(tmp_path, pem)) == {"azure_health_console-9.9.9-py3-none-any.whl": "ok",
                                                    "azure_health_console-9.9.9.tar.gz": "not downloaded",
                                                    "requirements-release.txt": "ok"}


def test_a_swapped_file_list_or_wheel_or_an_extra_wheel_is_refused(tmp_path):
    from healthconsole import release
    pem = signed_release(tmp_path)
    sums = (tmp_path / "SHA256SUMS.txt").read_bytes()
    (tmp_path / "SHA256SUMS.txt").write_bytes(sums.replace(b"  requirements", b"  requirementz"))
    with pytest.raises(release.VerifyError, match="wasn't signed"):
        release.verify(tmp_path, pem)
    (tmp_path / "SHA256SUMS.txt").write_bytes(sums)
    (tmp_path / "azure_health_console-9.9.9-py3-none-any.whl").write_bytes(b"evil")
    with pytest.raises(release.VerifyError, match="SHA-256 differs"):
        release.verify(tmp_path, pem)
    (tmp_path / "azure_health_console-9.9.9-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "azure_health_console-9.9.10-py3-none-any.whl").write_bytes(b"other")
    with pytest.raises(release.VerifyError, match="isn't in the signed"):
        release.verify(tmp_path, pem)


def test_another_key_or_no_key_verifies_nothing(tmp_path):
    from healthconsole import release
    signed_release(tmp_path)
    (tmp_path / "other").mkdir()
    other = signed_release(tmp_path / "other")
    with pytest.raises(release.VerifyError, match="wasn't signed"):
        release.verify(tmp_path, other)
    with pytest.raises(release.VerifyError, match="no usable release key"):
        release.verify(tmp_path, b"not a key")


def test_the_cli_verify_needs_no_settings_and_fails_loudly(tmp_path, monkeypatch, capsys):
    from healthconsole import cli, release
    monkeypatch.setattr(release, "PUBLIC_KEY", tmp_path / "key.pem")
    (tmp_path / "key.pem").write_bytes(signed_release(tmp_path))
    monkeypatch.setenv("AHC_CONFIG", str(tmp_path / "broken.yaml"))
    (tmp_path / "broken.yaml").write_text("not_a_setting: 1\n")
    cli.main(["verify", str(tmp_path)])
    assert "Verified" in capsys.readouterr().out
    (tmp_path / "requirements-release.txt").write_text("evil")
    with pytest.raises(SystemExit, match="NOT VERIFIED"):
        cli.main(["verify", str(tmp_path)])


def test_the_package_carries_the_release_key_as_an_ec_p256_public_key():
    from healthconsole import release
    release._key(release.PUBLIC_KEY.read_bytes())  # fails until the key's public half replaces the placeholder


def test_the_readme_shows_the_release_key_s_fingerprint_for_a_first_install():
    import hashlib
    from pathlib import Path
    from cryptography.hazmat.primitives import serialization
    from healthconsole import release
    key = release._key(release.PUBLIC_KEY.read_bytes())  # fails until the placeholder is replaced, like the test above
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    readme = (Path(release.__file__).resolve().parents[1] / "README.md").read_text()
    assert f"`{hashlib.sha256(der).hexdigest()}`" in readme  # openssl pkey -pubin -outform DER | openssl dgst -sha256


def test_the_readme_installs_and_upgrades_only_through_checked_files():
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    section = readme[readme.index("### Install by hand"):readme.index("### Check what you install")]
    installs = [line for line in section.splitlines() if line.startswith("pipx install")]
    assert installs and all('--pip-args="--require-hashes -c constraints.txt"' in line for line in installs)
    assert section.count("-signature SHA256SUMS.txt.sig SHA256SUMS.txt") == 2 and "azure-health-console verify" in section


def test_the_release_key_file_is_committed_not_ignored():
    import shutil
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    if not shutil.which("git") or not (root / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(["git", "check-ignore", "-q", "healthconsole/release-signing.pem"], cwd=root)
    assert ignored.returncode == 1  # *.pem is ignored; this public key is the exception, or the export would lack it
