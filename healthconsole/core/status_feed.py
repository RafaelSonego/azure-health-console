"""The public Azure status feed: outages wide enough for Microsoft to post on its status page, which can show before
your subscriptions' Service Health does. One GET of a public page, no token and nothing about the viewer sent; it is
empty most of the time. Cached briefly (Triage may refresh every minute), and a failure is cached too."""
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from .cache import TtlCache
from .guarded import GuardedClient, ServiceError, WriteBlocked

HOST = "azure.status.microsoft"
FEED_URL = f"https://{HOST}/en-us/status/feed/"
STATUS_PAGE = f"https://{HOST}/en-us/status/"
TIMEOUT = 10
MAX_BYTES = 1_000_000
CACHE_SECONDS = 120
FAILURE_SECONDS = 60
DETAIL_CHARS = 300
_cache = TtlCache("azure-status-feed", CACHE_SECONDS, access_bound=False)
_TAGS = re.compile(r"<[^>]+>")


class _Feed(GuardedClient):
    label = "STATUS"

    def check(self, method, url, **request):
        parts = urlsplit(url)
        if method != "GET" or parts.scheme != "https" or parts.netloc != HOST or parts.path != urlsplit(FEED_URL).path:
            raise WriteBlocked(f"Only reading the public Azure status feed is allowed, not {method} {parts.netloc}{parts.path}.")

    def unreachable(self, method, url, error):
        return ServiceError(f"{HOST} couldn't be reached from this machine.", "network")


_FEED = _Feed()


def _text(markup):
    words = " ".join(html.unescape(_TAGS.sub(" ", markup or "")).split())
    return words if len(words) <= DETAIL_CHARS else words[:DETAIL_CHARS - 1].rstrip() + "…"


def _iso(rfc822):
    try:
        return parsedate_to_datetime(rfc822).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def parse(xml_bytes):
    """The feed's items, newest first: title, link (https only, else the status page), text, published (ISO)."""
    root = ET.fromstring(xml_bytes)
    items = []
    for node in root.iter("item"):
        link = (node.findtext("link") or "").strip()
        items.append({"title": _text(node.findtext("title")) or "Azure status update",
                      "link": link if link.startswith("https://") else STATUS_PAGE,
                      "text": _text(node.findtext("description")), "published": _iso(node.findtext("pubDate"))})
    return sorted(items, key=lambda i: i["published"], reverse=True)


def fetch_xml(session=None):
    response = _FEED.send(session or requests.Session(), "GET", FEED_URL, TIMEOUT,
                          headers={"Accept": "application/rss+xml, text/xml"}, stream=True)
    if response.status_code != 200:
        raise ServiceError(f"{HOST} answered {response.status_code}.", "other")
    body = response.raw.read(MAX_BYTES + 1, decode_content=True)
    if len(body) > MAX_BYTES:
        raise ServiceError(f"{HOST} sent more than {MAX_BYTES // 1000} KB; not read.", "other")
    return body


def read(refresh=False):
    """{"items": [...], "error": None or why, "read_at": ISO}; never raises."""
    if not refresh:
        hit = _cache.get("feed")
        if hit is not None:
            return hit
    read_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        return _cache.put("feed", {"items": parse(fetch_xml()), "error": None, "read_at": read_at})
    except (ServiceError, ET.ParseError) as failure:
        reason = str(failure) if isinstance(failure, ServiceError) else f"{HOST} sent a feed that couldn't be read."
        return _cache.put("feed", {"items": [], "error": reason, "read_at": read_at}, FAILURE_SECONDS)
