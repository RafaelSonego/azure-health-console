import pytest

from healthconsole.core import cache
from healthconsole.core.cache import TtlCache


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_entries_expire_on_their_own_lifetime():
    clock = Clock()
    c = TtlCache("test expiry", 60, clock=clock, register=False)
    c.put("a", 1)
    c.put("b", 2, ttl=5)
    clock.now += 10
    assert c.get("a") == 1 and c.get("b") is None  # b's own 5 s passed, a's 60 s didn't
    clock.now += 51
    assert c.get("a") is None and len(c) == 0


def test_get_or_load_keeps_empty_results_but_never_a_failure():
    c = TtlCache("test loads", 60, register=False)
    calls = []
    assert c.get_or_load("k", lambda: calls.append(1) or []) == []
    assert c.get_or_load("k", lambda: calls.append(1) or ["new"]) == []  # an empty list is a value, not a miss
    assert len(calls) == 1

    def refused():
        raise PermissionError("403")
    with pytest.raises(PermissionError):
        c.get_or_load("denied", refused)
    assert c.get_or_load("denied", lambda: "after PIM") == "after PIM"  # the refusal wasn't kept
    assert c.get_or_load("k", lambda: ["fresh"], refresh=True) == ["fresh"]


def test_pop_takes_a_live_item_once():
    clock = Clock()
    c = TtlCache("test pop", 600, clock=clock, register=False)
    c.put("state", {"verifier": "v"})
    assert c.pop("state") == {"verifier": "v"} and c.pop("state") is None
    c.put("old", 1)
    clock.now += 601
    assert c.pop("old") is None  # expired: a stale sign-in link can't complete


def test_refresh_access_clears_access_bound_caches_and_keeps_reference_data():
    mine = TtlCache("test viewer reads", 60)
    reference = TtlCache("test reference data", 60, access_bound=False)
    private = TtlCache("test unregistered", 60, register=False)
    for c in (mine, reference, private):
        c.put("k", "v")
    cleared = cache.forget_access_bound()
    assert "test viewer reads" in cleared and "test reference data" not in cleared
    assert mine.get("k") is None and reference.get("k") == "v" and private.get("k") == "v"


def test_refresh_route_clears_integration_and_pim_caches_but_not_built_in_roles(tmp_path):
    from flask import Flask

    from healthconsole.core import config, pim, user_access
    from healthconsole.core.integrations import matching
    from healthconsole.web import create_blueprint
    from tests.conftest import FakeClient

    class Client(FakeClient):
        def refresh(self):
            pass

    matching._cache.put("inventory-key", ["before PIM"])
    pim._cache.put("me", {"eligible": [], "held": [], "error": None})
    user_access._built_in.put("tenant-1", {"r": "Reader"})
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: Client(), config.load(tmp_path / "none.yaml"), authorize=lambda: None))
    assert app.test_client().post("/api/refresh", json={}).get_json()["ok"]
    assert matching._cache.get("inventory-key") is None and pim._cache.get("me") is None
    assert user_access._built_in.get("tenant-1") == {"r": "Reader"}
    user_access._built_in.clear()
