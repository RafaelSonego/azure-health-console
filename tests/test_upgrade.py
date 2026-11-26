import hashlib
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from healthconsole import release, upgrade
from healthconsole.core.guarded import ServiceError, WriteBlocked

ASSET = "https://release-assets.githubusercontent.com/github-production-release-asset/1/"


def signed(version="9.9.9", tamper=None):
    """A release's three files signed with a fresh key: (files, the key's PEM)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    constraints = f"flask==3.1.0 \\\n    --hash=sha256:{'0' * 64}\nazure-health-console=={version} \\\n    --hash=sha256:{'1' * 64}\n"
    files = {"constraints.txt": constraints.encode()}
    sums = f"{hashlib.sha256(files['constraints.txt']).hexdigest()}  constraints.txt\n".encode()
    files["SHA256SUMS.txt"] = sums
    files["SHA256SUMS.txt.sig"] = key.sign(sums, ec.ECDSA(hashes.SHA256()))
    if tamper:
        files[tamper] = files[tamper] + b"x"
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return files, pem


class GitHub:
    """github.com answers each release file with a redirect to the asset host, as the real one does."""

    def __init__(self, files, missing=False):
        self.files, self.missing, self.urls = files, missing, []

    def request(self, method, url, timeout, allow_redirects, **request):
        self.urls.append(url)
        parts = urlsplit(url)
        name = parts.path.rsplit("/", 1)[-1]
        if parts.netloc == "github.com":
            if self.missing:
                return SimpleNamespace(status_code=404, headers={}, content=b"")
            return SimpleNamespace(status_code=302, headers={"Location": ASSET + name}, content=b"")
        return SimpleNamespace(status_code=200, headers={}, content=self.files[name])


class Runner:
    def __init__(self, fail_at=None):
        self.calls, self.fail_at = [], fail_at

    def __call__(self, argv, cwd=None):
        self.calls.append((argv, cwd and sorted(p.name for p in __import__("pathlib").Path(cwd).iterdir())))
        return SimpleNamespace(returncode=1 if len(self.calls) == self.fail_at else 0)


@pytest.fixture
def key(tmp_path, monkeypatch):
    def use(pem):
        (tmp_path / "key.pem").write_bytes(pem)
        monkeypatch.setattr(release, "PUBLIC_KEY", tmp_path / "key.pem")
    monkeypatch.setattr(upgrade, "__version__", "1.0.0")
    monkeypatch.setattr(upgrade.os, "name", "posix")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/bin/{name}")
    return use


def test_only_this_app_s_release_files_pass_the_guard():
    upgrade._CLIENT.check("GET", upgrade.url_for("SHA256SUMS.txt"))
    upgrade._CLIENT.check("GET", upgrade.url_for("constraints.txt", "1.2.0"))
    upgrade._CLIENT.check("GET", ASSET + "x")
    for method, url in (("POST", upgrade.url_for("SHA256SUMS.txt")),
                        ("GET", "https://github.com/someone/else/releases/latest/download/constraints.txt"),
                        ("GET", "https://github.com/RafaelSonego/azure-health-console/archive/main.zip"),
                        ("GET", "http://release-assets.githubusercontent.com/x"),
                        ("GET", "https://evil.example/RafaelSonego/azure-health-console/releases/x")):
        with pytest.raises(WriteBlocked):
            upgrade._CLIENT.check(method, url)


def test_a_newer_signed_release_installs_with_pipx_from_the_checked_constraints(key):
    files, pem = signed("1.3.0")
    key(pem)
    runner, said = Runner(), []
    assert upgrade.run(method="pipx", session=GitHub(files), runner=runner, out=said.append) == 0
    (shared, _), (install, folder) = runner.calls
    assert shared == ["/bin/pipx", "upgrade-shared"]
    assert install[:3] == ["/bin/pipx", "install", "--force"] and "azure-health-console==1.3.0" in install
    assert install[-1] == "--pip-args=--require-hashes -c constraints.txt" and "constraints.txt" in folder
    assert "Installed 1.3.0" in said[-1]


@pytest.mark.parametrize("tamper", ["SHA256SUMS.txt", "constraints.txt", "SHA256SUMS.txt.sig"])
def test_a_changed_file_installs_nothing(key, tamper):
    files, pem = signed("1.3.0", tamper)
    key(pem)
    runner = Runner()
    with pytest.raises(release.VerifyError):
        upgrade.run(method="pipx", session=GitHub(files), runner=runner, out=lambda _: None)
    assert runner.calls == []


def test_another_key_installs_nothing(key):
    files, _ = signed("1.3.0")
    key(signed()[1])
    runner = Runner()
    with pytest.raises(release.VerifyError, match="wasn't signed"):
        upgrade.run(method="pipx", session=GitHub(files), runner=runner, out=lambda _: None)
    assert runner.calls == []


def test_the_installed_version_or_an_older_one_is_left_alone_unless_asked_for(key):
    files, pem = signed("1.0.0")
    key(pem)
    runner, said = Runner(), []
    assert upgrade.run(method="pipx", session=GitHub(files), runner=runner, out=said.append) == 0
    assert runner.calls == [] and "nothing to install" in said[-1]
    assert upgrade.run("1.0.0", method="pipx", session=GitHub(files), runner=runner, out=said.append) == 0
    assert len(runner.calls) == 2  # an explicit --to reinstalls it


def test_dry_run_checks_but_installs_nothing_and_a_failed_step_stops(key):
    files, pem = signed("1.3.0")
    key(pem)
    runner = Runner()
    assert upgrade.run(dry_run=True, method="pipx", session=GitHub(files), runner=runner, out=lambda _: None) == 0
    assert runner.calls == []
    runner = Runner(fail_at=1)
    assert upgrade.run(method="pipx", session=GitHub(files), runner=runner, out=lambda _: None) == 1
    assert len(runner.calls) == 1


def test_a_missing_release_says_so(key):
    with pytest.raises(ServiceError, match="no SHA256SUMS.txt for release 9.9.9"):
        upgrade.run("9.9.9", method="pipx", session=GitHub({}, missing=True), runner=Runner(), out=lambda _: None)


def test_brew_runs_brew_and_downloads_nothing(key):
    session, runner = GitHub({}), Runner()
    assert upgrade.run(method="brew", session=session, runner=runner, out=lambda _: None) == 0
    assert runner.calls == [(["brew", "upgrade", "azure-health-console"], None)] and session.urls == []


@pytest.mark.parametrize("method, windows, says", [
    ("source", False, "git pull"),
    ("pipx", True, "irm https://raw.githubusercontent.com/RafaelSonego/azure-health-console/main/install.ps1 | iex"),
    ("pip", True, "Close the console"),
])
def test_where_it_can_t_install_it_says_what_to_run_before_downloading(key, monkeypatch, method, windows, says):
    monkeypatch.setattr(upgrade.os, "name", "nt" if windows else "posix")
    session, runner, said = GitHub({}), Runner(), []
    assert upgrade.run(method=method, session=session, runner=runner, out=said.append) == 1
    assert says in said[0] and session.urls == [] and runner.calls == []


def test_no_pipx_on_path_points_at_the_install_line(key, monkeypatch):
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: None)
    argv, note = upgrade.commands("pipx", "1.3.0", windows=False)
    assert argv is None and note.endswith("install.sh | sh")


def test_pip_installs_into_its_own_environment_by_hash():
    argv, _ = upgrade.commands("pip", "1.3.0", windows=False)
    assert argv == [[upgrade.sys.executable, "-m", "pip", "install", "--require-hashes", "-c", "constraints.txt",
                     "azure-health-console==1.3.0"]]


def test_the_cli_upgrade_needs_no_settings_and_fails_loudly(tmp_path, monkeypatch):
    from healthconsole import cli
    monkeypatch.setenv("AHC_CONFIG", str(tmp_path / "broken.yaml"))
    (tmp_path / "broken.yaml").write_text("not_a_setting: 1\n")

    def refuse(version, dry_run):
        raise release.VerifyError("bad")
    monkeypatch.setattr(upgrade, "run", refuse)
    with pytest.raises(SystemExit, match="NOT VERIFIED, nothing installed: bad"):
        cli.main(["upgrade"])
    monkeypatch.setattr(upgrade, "run", lambda version, dry_run: 0 if (version, dry_run) == ("1.2.0", True) else 5)
    with pytest.raises(SystemExit) as done:
        cli.main(["upgrade", "--to", "1.2.0", "--dry-run"])
    assert done.value.code == 0
