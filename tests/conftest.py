import pytest

from healthconsole.core import config, cost_store, status_feed
from healthconsole.core.credentials import Identity

REAL_FETCH_XML = status_feed.fetch_xml  # for the tests of the fetch itself
EMPTY_FEED = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Azure Status</title></channel></rss>'


@pytest.fixture(autouse=True)
def no_status_feed(monkeypatch):
    """No test reads the real Azure status feed: an empty one, and a fresh cache, unless a test swaps it."""
    status_feed._cache.clear()
    monkeypatch.setattr(status_feed, "fetch_xml", lambda session=None: EMPTY_FEED)
    yield
    status_feed._cache.clear()


@pytest.fixture(autouse=True)
def cost_store_in_tmp(tmp_path, monkeypatch):
    """No test writes a cost store into the real cache folder, and no fill outlives its test."""
    monkeypatch.setitem(config.DEFAULTS, "cache_dir", str(tmp_path / "cache"))
    yield
    cost_store.reset()

SUB = "11111111-1111-1111-1111-111111111111"


def rid(rg, provider_type, name):
    return f"/subscriptions/{SUB}/resourceGroups/{rg}/providers/{provider_type}/{name}"


class FakeTokens:
    identity = Identity("oid-1", "tester@example.com", "tenant-1")

    def get_token(self, scope=None):
        return "token"


class FakeClient:
    """Answers Resource Graph queries from a table of rows and GETs from a dict."""

    def __init__(self, resources=(), gets=None, query_handlers=None, posts=None):
        self.resources = {r["id"].lower(): r for r in resources}
        self.gets = gets or {}
        self.posts = posts or {}
        self.query_handlers = query_handlers or []
        self.queries = []
        self.posted = []
        self.call_count = 0
        self.identity = FakeTokens.identity

    def query(self, kql, subscriptions=None, limit=None):
        self.call_count += 1
        self.queries.append(kql)
        for marker, handler in self.query_handlers:
            if marker in kql:
                return handler(kql)
        if "tolower(id) in" in kql:
            return [row for key, row in self.resources.items() if f'"{key}"' in kql]
        if "tostring(properties) contains" in kql:
            target = kql.split("contains ")[1].split('"')[1]
            return [row for row in self.resources.values() if target in str(row).lower()]
        return []

    def get(self, path, api_version, params=None, tolerate=(), timeout=None):
        self.call_count += 1
        for marker, value in self.gets.items():
            if marker in path:
                return value(params) if callable(value) else value
        return None

    def get_paged(self, path, api_version, params=None, timeout=None):
        return (self.get(path, api_version, params) or {}).get("value", [])

    def get_pages(self, path, api_version, params=None, max_pages=10, timeout=None):
        return self.get_paged(path, api_version, params), False

    def post(self, path, api_version, body, cache=True, params=None):
        self.call_count += 1
        self.posted.append((path, body, params))
        for marker, value in self.posts.items():
            if marker in path:
                return value(body) if callable(value) else value
        return {}

    def api_version(self, resource_id):
        return "2024-01-01"


@pytest.fixture
def fake_client():
    return FakeClient


@pytest.fixture(autouse=True)
def _no_real_log_folder(tmp_path, monkeypatch):
    """No test may write the console's log under the real home folder: the default points at this test's tmp."""
    from healthconsole.core import config, eventlog
    monkeypatch.setitem(config.DEFAULTS, "logs", {"dir": str(tmp_path / "default-logs"), "keep_days": 30})
    yield
    eventlog.configure(None)
