"""`azure-health-console upgrade`: download a release's signed file list and constraints from GitHub, check them with
the key inside the version already installed (`release.verify`), then install that release the way this copy was
installed, with pip checking every file against the constraints' SHA-256."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import __version__, release
from .core.guarded import GuardedClient, ServiceError, WriteBlocked
from .core.pypi_stats import INSTALL_PS1, INSTALL_SH, PACKAGE, install_method, newer, why_unreachable

REPO = "RafaelSonego/azure-health-console"
RELEASES = f"https://github.com/{REPO}/releases"
FILES = (release.SUMS, release.SIGNATURE, "constraints.txt")
ASSET_HOSTS = ("release-assets.githubusercontent.com", "objects.githubusercontent.com")  # where GitHub redirects
TIMEOUT = 30


class _Releases(GuardedClient):
    label = "GITHUB"

    def check(self, method, url, **request):
        parts = urlsplit(url)
        ours = parts.netloc == "github.com" and parts.path.startswith(f"/{REPO}/releases/")
        if method != "GET" or parts.scheme != "https" or not (ours or parts.netloc in ASSET_HOSTS):
            raise WriteBlocked(f"Only reading this app's GitHub release files is allowed, not {method} {parts.netloc}{parts.path}.")

    def unreachable(self, method, url, error):
        return ServiceError(f"GitHub couldn't be reached from this machine: {why_unreachable(error)}", "network")


_CLIENT = _Releases()


def url_for(name, version=None):
    return f"{RELEASES}/download/v{version}/{name}" if version else f"{RELEASES}/latest/download/{name}"


def download(folder, version=None, session=None):
    """The signed list, its signature and constraints.txt of `version` (default: the latest release) into `folder`."""
    session = session or requests.Session()
    for name in FILES:
        response = _CLIENT.send(session, "GET", url_for(name, version), TIMEOUT)
        if response.status_code == 404:
            raise ServiceError(f"GitHub has no {name} for {'release ' + version if version else 'the latest release'}. "
                               f"Releases: {RELEASES}", "missing")
        if response.status_code != 200:
            raise ServiceError(f"GitHub answered {response.status_code} for {name}.", "other")
        (Path(folder) / name).write_bytes(response.content)


def released_version(folder):
    """The console's version that a verified constraints.txt pins."""
    match = re.search(rf"^{PACKAGE}==([0-9][0-9A-Za-z.+-]*)", (Path(folder) / "constraints.txt").read_text("utf-8"), re.M)
    if not match:
        raise release.VerifyError(f"constraints.txt doesn't pin {PACKAGE}: don't install from it.")
    return match.group(1)


def commands(method, version, windows=None):
    """What installs `version` for this install method: (argv lists run in the download folder, or None, and a note)."""
    windows = os.name == "nt" if windows is None else windows
    spec = f"{PACKAGE}=={version}"
    if method == "brew":
        return [["brew", "upgrade", PACKAGE]], ""
    if method == "source":
        return None, "This copy runs from a checkout: update it with git (git pull), not this command."
    if windows:  # a running program's files can't be replaced on Windows, and this command runs from them
        return None, f"Close the console (every window running it), then run in PowerShell:\n\n    {INSTALL_PS1}\n"
    if method == "pipx":
        pipx = shutil.which("pipx")
        if not pipx:
            return None, f"pipx isn't on your PATH. Run this instead: {INSTALL_SH}"
        python = getattr(sys, "_base_executable", "") or sys.executable  # not this venv's: pipx replaces it
        return [[pipx, "upgrade-shared"],
                [pipx, "install", "--force", "--python", python, spec,
                 "--pip-args=--require-hashes -c constraints.txt"]], ""
    return [[sys.executable, "-m", "pip", "install", "--require-hashes", "-c", "constraints.txt", spec]], ""


def run(version=None, dry_run=False, out=print, method=None, session=None, runner=subprocess.run):
    """Check and install a release; returns the exit code. Brew checks its own files, so it skips the download."""
    method = method or install_method()
    if method == "brew":
        argv, _ = commands(method, version or "")
        out(f"Installed with Homebrew: {shlex.join(argv[0])}")
        return 0 if dry_run else runner(argv[0]).returncode
    argv, note = commands(method, "")
    if argv is None:  # a checkout, Windows, no pipx: said before anything is downloaded
        out(note)
        return 1
    with tempfile.TemporaryDirectory(prefix="azure-health-console-") as folder:
        out(f"Downloading {'release ' + version if version else 'the latest release'}'s signed file list from GitHub…")
        download(folder, version, session)
        release.verify(folder)
        found = released_version(folder)
        out(f"Verified {found} with the release key inside {__version__}.")
        if version is None and not newer(found, __version__):
            out(f"{__version__} is the newest release: nothing to install.")
            return 0
        for step in commands(method, found)[0]:
            out(f"  {shlex.join(step)}")
            if not dry_run and runner(step, cwd=folder).returncode:
                out("That step failed: nothing else ran. Its output above says why.")
                return 1
    out("Dry run: nothing installed." if dry_run else f"Installed {found}. Restart the console if it's running.")
    return 0
