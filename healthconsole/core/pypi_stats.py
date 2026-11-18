"""This console's public numbers on PyPI, for About: the latest release and its downloads (pypistats.org, without
mirrors). Three GETs of public pages, read only when About opens; no token and nothing about the viewer sent."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .cache import TtlCache
from .guarded import GuardedClient, ServiceError, WriteBlocked

PACKAGE = "azure-health-console"
RELEASE_NOTES = "https://github.com/RafaelSonego/azure-health-console/releases/tag/v{version}"
UPGRADE = {"brew": f"brew upgrade {PACKAGE}", "pipx": f"pipx upgrade {PACKAGE}", "pip": f"pip install -U {PACKAGE}"}
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
        return ServiceError(f"{urlsplit(url).netloc} couldn't be reached from this machine.", "network")


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
    if "/pipx/venvs/" in path:
        return "pipx"
    if "/cellar/" in path or "/homebrew/" in path or "/linuxbrew/" in path:
        return "brew"
    return "pip"


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
    return {**base, "checked": True, "latest": latest, "newer": newer(latest, current), "error": hit["error"],
            "command": UPGRADE[method], "notes": RELEASE_NOTES.format(version=latest) if latest else ""}
