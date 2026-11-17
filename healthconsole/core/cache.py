"""The console's one in-memory cache with an expiry. Every module that reuses a read for a while makes one `TtlCache`
instead of its own dict, so expiry, locking and Refresh access behave the same everywhere. Keys holding a viewer's
results start with their identity (rule 3 of "Hosting it later" in PLAN.md)."""
import threading
import time

_MISSING = object()
_caches = []
_on_forget = []  # stores that aren't a TtlCache but keep access-bound answers (the cost store's refusals)
_registry_lock = threading.Lock()


def on_forget_access(callback):
    """Calls `callback()` on every Refresh access too."""
    with _registry_lock:
        _on_forget.append(callback)


class TtlCache:
    """`ttl` seconds per entry (or per `put`). `access_bound`: the cached reads depend on the viewer's access, so
    Refresh access (after a PIM activation, say) clears them; reference data the same for everyone (built-in role
    names, runtime lists) is kept."""

    def __init__(self, name, ttl, access_bound=True, clock=time.monotonic, register=True):
        self.name, self.ttl, self.access_bound, self._clock = name, ttl, access_bound, clock
        self._items = {}
        self._lock = threading.Lock()
        if register:  # per-object caches (one ArmClient's) clear themselves and stay out of the registry
            with _registry_lock:
                _caches.append(self)

    def get(self, key, default=None):
        """The live value, or `default`; a cached None or empty list counts as a value."""
        with self._lock:
            item = self._items.get(key)
            if item is not None and item[0] > self._clock():
                return item[1]
            self._items.pop(key, None)
            return default

    def put(self, key, value, ttl=None):
        with self._lock:
            self._items[key] = (self._clock() + (self.ttl if ttl is None else ttl), value)
        return value

    def get_or_load(self, key, load, refresh=False, ttl=None):
        """The cached value, or `load()`'s, kept for `ttl`. A load that raises caches nothing, so a refusal or an
        outage is asked again next time. `refresh` reads afresh."""
        if not refresh:
            hit = self.get(key, _MISSING)
            if hit is not _MISSING:
                return hit
        return self.put(key, load(), ttl)

    def pop(self, key, default=None):
        """Take a live value out (a one-time item, like a pending sign-in), or `default` if it expired or never was."""
        with self._lock:
            item = self._items.pop(key, None)
        return item[1] if item is not None and item[0] > self._clock() else default

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self):
        now = self._clock()
        with self._lock:
            return sum(1 for expires, _ in self._items.values() if expires > now)


def forget_access_bound():
    """Refresh access: drop every cache whose reads depend on the viewer's access, keep reference data."""
    with _registry_lock:
        caches, callbacks = list(_caches), list(_on_forget)
    for cache in caches:
        if cache.access_bound:
            cache.clear()
    for callback in callbacks:
        callback()
    return [c.name for c in caches if c.access_bound]


def forget_all():
    """Everything (tests, and a fresh start)."""
    with _registry_lock:
        caches = list(_caches)
    for cache in caches:
        cache.clear()
