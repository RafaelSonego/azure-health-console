"""This console's public numbers on PyPI, for About: the latest release and its downloads (pypistats.org, without
mirrors). Three GETs of public pages, read only when About opens; no token and nothing about the viewer sent."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .cache import TtlCache
from .guarded import GuardedClient, ServiceError, WriteBlocked

PACKAGE = "azure-health-console"
RELEASE_NOTES = "https://github.com/RafaelSonego/azure-health-console/releases/tag/v{version}"
INSTALL_SCRIPT = "https://raw.githubusercontent.com/RafaelSonego/azure-health-console/main/install"
INSTALL_SH = f"curl -fsSL {INSTALL_SCRIPT}.sh | sh"
INSTALL_PS1 = f"irm {INSTALL_SCRIPT}.ps1 | iex"
UPGRADE_STEPS = "https://github.com/RafaelSonego/azure-health-console#upgrade"
PROJECT_PAGE = f"https://pypi.org/project/{PACKAGE}/"
STATS_PAGE = f"https://pypistats.org/packages/{PACKAGE}"
URLS = {
    "pypi": f"https://pypi.org/pypi/{PACKAGE}/json",
    "recent": f"https://pypistats.org/api/packages/{PACKAGE}/recent",
    "overall": f"https://pypistats.org/api/packages/{PACKAGE}/overall?mirrors=false",
}
_ALLOWED = {(urlsplit(u).netloc, urlsplit(u).path) for u in URLS.values()}
TIMEOUT = 10
CACHE_SECONDS = 6 * 3600  # pypistats updates once a day
FAILURE_SECONDS = 600
_cache = TtlCache("pypi stats", CACHE_SECONDS, access_bound=False)


class _Public(GuardedClient):
    label = "PYPI"

    def check(self, method, url, **request):
        parts = urlsplit(url)
        if method != "GET" or parts.scheme != "https" or (parts.netloc, parts.path) not in _ALLOWED:
            raise WriteBlocked(f"Only reading this app's public PyPI pages is allowed, not {method} {parts.netloc}{parts.path}.")

    def unreachable(self, method, url, error):
        return ServiceError(f"{urlsplit(url).netloc} couldn't be reached from this machine: {why_unreachable(error)}", "network")


def why_unreachable(error):
    """Why Python couldn't connect when a browser on the same machine can: it has its own certificates and proxy settings."""
    if isinstance(error, requests.exceptions.SSLError):
        return ("Python doesn't trust the site's HTTPS certificate. A company proxy that inspects HTTPS signs sites with "
                "its own certificate, which your browser trusts and Python doesn't.")
    if isinstance(error, requests.exceptions.ProxyError):
        return "the proxy refused the connection (Python uses HTTPS_PROXY, not the browser's proxy settings)."
    if isinstance(error, requests.exceptions.Timeout):
        return ("no answer in time. A firewall may be dropping it: Python goes out directly unless HTTPS_PROXY is set, "
                "while your browser can use a proxy (PAC) Python doesn't read.")
    if "resolve" in str(error).lower() or "nodename" in str(error).lower():
        return "its name couldn't be resolved (DNS), so this network may need a proxy (HTTPS_PROXY)."
    return f"the connection failed ({type(error).__name__})."


_CLIENT = _Public()


def fetch(name, session=None):
    url = URLS[name]
    response = _CLIENT.send(session or requests.Session(), "GET", url, TIMEOUT, headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise ServiceError(f"{urlsplit(url).netloc} answered {response.status_code}.", "other")
    try:
        return response.json()
    except ValueError as error:
        raise ServiceError(f"{urlsplit(url).netloc} sent something that isn't JSON.", "other") from error


def summarise(pypi, recent, overall):
    info = (pypi or {}).get("info") or {}
    releases = {v: files for v, files in ((pypi or {}).get("releases") or {}).items() if files}
    first = min((f.get("upload_time_iso_8601") or "" for files in releases.values() for f in files), default="")
    days = [d for d in (overall or {}).get("data") or [] if d.get("category") == "without_mirrors"]
    counts = (recent or {}).get("data") or {}
    return {"latest": info.get("version") or "", "releases": len(releases), "first_release": first[:10],
            "last_day": counts.get("last_day"), "last_week": counts.get("last_week"), "last_month": counts.get("last_month"),
            "total": sum(d.get("downloads") or 0 for d in days), "counted_from": min((d["date"] for d in days if d.get("date")), default=""),
            "project_page": PROJECT_PAGE, "stats_page": STATS_PAGE}


def read(refresh=False):
    """The summary, plus "error" and "read_at"; never raises. A failure is kept briefly so About doesn't wait each time."""
    if not refresh:
        hit = _cache.get("stats")
        if hit is not None:
            return hit
    read_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        found = summarise(fetch("pypi"), fetch("recent"), fetch("overall"))
        return _cache.put("stats", {**found, "error": None, "read_at": read_at})
    except ServiceError as failure:
        return _cache.put("stats", {"error": str(failure), "read_at": read_at,
                                    "project_page": PROJECT_PAGE, "stats_page": STATS_PAGE}, FAILURE_SECONDS)


def _parts(version):
    out = []
    for piece in str(version).split(".")[:3]:
        digits = "".join(c for c in piece if c.isdigit())
        out.append(int(digits) if digits else 0)
    return out + [0] * (3 - len(out))


def newer(latest, current):
    return bool(latest and current and current != "unknown") and _parts(latest) > _parts(current)


def install_method(prefix=None, checkout=None):
    """How this copy was installed, from where its Python lives: "source" (a checkout with its VERSION file),
    "pipx", "brew" or "pip"."""
    if checkout is None:
        checkout = (Path(__file__).resolve().parents[2] / "VERSION").is_file()
    if checkout:
        return "source"
    path = str(prefix or sys.prefix).replace("\\", "/").lower()
    if "/pipx/venvs/" in path or (Path(prefix or sys.prefix) / "pipx_metadata.json").is_file():  # any PIPX_HOME
        return "pipx"
    if "/cellar/" in path or "/homebrew/" in path or "/linuxbrew/" in path:
        return "brew"
    return "pip"


def upgrade_command(method, windows=None):
    """The one line that upgrades this copy, and whether the console must be stopped first (Windows can't replace a
    running program's files). pipx and pip check the release's signed files before installing."""
    windows = os.name == "nt" if windows is None else windows
    if method == "brew":
        return f"brew upgrade {PACKAGE}", False
    if method in ("pipx", "pip"):
        return (INSTALL_PS1, True) if windows else (f"{PACKAGE} upgrade", False)
    return "", False


def update(current, enabled=True, refresh=False):
    """Whether a newer release is out than `current`; checks only an installed copy, at most every 6 hours."""
    method = install_method()
    base = {"current": current, "method": method, "newer": False, "latest": ""}
    if not enabled:
        return {**base, "checked": False, "reason": "Update checks are off (updates.check)."}
    if method == "source":
        return {**base, "checked": False, "reason": "Running from a checkout: its VERSION file is the version."}
    if not refresh:
        hit = _cache.get("latest")
    if refresh or hit is None:
        try:
            hit = _cache.put("latest", {"latest": ((fetch("pypi").get("info") or {}).get("version") or ""), "error": None})
        except ServiceError as failure:
            hit = _cache.put("latest", {"latest": "", "error": str(failure)}, FAILURE_SECONDS)
    latest = hit["latest"]
    command, stop_first = upgrade_command(method)
    return {**base, "checked": True, "latest": latest, "newer": newer(latest, current), "error": hit["error"],
            "command": command, "stop_first": stop_first, "steps": "" if command else UPGRADE_STEPS,
            "notes": RELEASE_NOTES.format(version=latest) if latest else ""}
