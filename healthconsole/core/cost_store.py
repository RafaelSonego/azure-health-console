"""Every subscription's costs, read once and kept on disk, so a subscription, a resource group, Summary and the Cost
view are filtered from what's stored instead of each asking Azure. Per subscription it holds cost by resource group
and day since the first of last month, and this month by resource and service. Azure updates costs a few times a day
and settles a day's figure within about three days, so a refresh rereads only the last few days. Each day also
keeps what of its cost was booked (purchases and refunds, e.g. a reservation's monthly payment), so usage can be told
from a payment. Actual and amortized cost are kept apart, each in its own file.

The first fill runs in the background, in the order that serves the page soonest: the scope in use first, last month
included (the Cost view compares with its same days), then the last 30 days for every other subscription, then the
rest of last month, then this month by resource. A read the page is waiting
on goes first (`Gate`)."""
import gzip
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .arm import AccessDenied, ArmError, Cancelled
from .cache import on_forget_access
from .credentials import CredentialError
from .localfiles import private_dir

FORMAT = 2                   # 2: days also keep their booked part (ChargeType); older files are read again
BOOKED_CHARGES = ("purchase", "refund")  # ChargeType values that are payments, not usage
FRESH_SECONDS = 6 * 3600     # older than this, the next view rereads the last SETTLE_DAYS
SETTLE_DAYS = 3              # Azure can still change a day's cost this long after it
RECENT_DAYS = 30
REFUSED_SECONDS = 6 * 3600   # a subscription whose costs were refused is asked again after this (or Refresh access)
PARALLEL = 4                 # Cost Management throttles by data read; more at once only meets 429s sooner
SAVE_EVERY = 10              # subscriptions read between writes to disk
FOREGROUND_WAIT_SECONDS = 30
ACTIVE_STATES = ("enabled", "warned", "pastdue")
RECENT, EARLIER, PARTS = "recent", "earlier", "parts"
# The fill's order: the scope in use whole (its month comparison needs last month), then every other subscription's
# last 30 days (the Cost view), then their rest of last month, then this month by resource. (phase, wanted only)
ORDER = ((RECENT, True), (EARLIER, True), (RECENT, False), (EARLIER, False), (PARTS, False))
WHOLE = "whole"  # a page waiting on a subscription not yet stored: the whole window in one query (with PARTS beside it)


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="seconds")


def window_start(today):
    """The first day kept: the first of last month, or 30 days back if that's earlier (early March)."""
    return min((today.replace(day=1) - timedelta(days=1)).replace(day=1), today - timedelta(days=RECENT_DAYS - 1))


class Gate:
    """Reads the page is waiting on go before background ones: a background read waits while any foreground read
    is in flight (at most FOREGROUND_WAIT_SECONDS, so a busy page can't stall the fill for good)."""

    def __init__(self):
        self._foreground = 0
        self._cond = threading.Condition()

    def foreground(self):
        gate = self

        class _Hold:
            def __enter__(self):
                with gate._cond:
                    gate._foreground += 1

            def __exit__(self, *exc):
                with gate._cond:
                    gate._foreground -= 1
                    gate._cond.notify_all()

        return _Hold()

    def background(self):
        with self._cond:
            self._cond.wait_for(lambda: self._foreground == 0, timeout=FOREGROUND_WAIT_SECONDS)


class Store:
    """One viewer's costs in one tenant. `fetch(client, scope, body)` returns Cost Management rows; `today` and
    `now` are injectable for tests. Thread-safe; the file is written whole to a temporary name, then renamed."""
    cost_type = "actual"

    def __init__(self, path, fetch, body, today, now=_now):
        self.path, self._fetch, self._body, self._today, self._now = path, fetch, body, today, now
        self._lock = threading.RLock()
        self.gate = Gate()
        self._data = self._load()
        self._wanted, self._all = [], []
        self._tried = {RECENT: set(), EARLIER: set(), PARTS: set()}  # this run's attempts, so a failure isn't looped
        self._thread, self._cancel, self._client = None, None, None
        self.error = None
        self._dirty = 0

    # ---- disk

    def _load(self):
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("format") == FORMAT:
                return data
        except (OSError, ValueError):
            pass
        return {"format": FORMAT, "subs": {}, "refused": {}, "failed": {}}

    def save(self):
        with self._lock:
            text = json.dumps(self._data, separators=(",", ":"))
            self._dirty = 0
        private_dir(self.path.parent)
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".costs-", suffix=".tmp")
        try:
            os.chmod(temp, 0o600)
            with os.fdopen(handle, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as out:
                out.write(text.encode("utf-8"))
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def clear(self):
        """Forget everything, in memory and on disk (the account menu's Clear cached data)."""
        self.stop()
        with self._lock:
            self._data = {"format": FORMAT, "subs": {}, "refused": {}, "failed": {}}
        self.path.unlink(missing_ok=True)

    def forget_refusals(self):
        """Refresh access: a new role may make refused subscriptions readable."""
        with self._lock:
            self._data["refused"].clear()
            self._data["failed"].clear()

    # ---- what's stored

    def entry(self, sub):
        with self._lock:
            return json.loads(json.dumps(self._data["subs"].get(sub.lower()))) if sub.lower() in self._data["subs"] else None

    def refused(self, sub):
        with self._lock:
            when = self._data["refused"].get(sub.lower())
        return bool(when) and (self._now() - datetime.fromisoformat(when)).total_seconds() < REFUSED_SECONDS

    def failure(self, sub):
        with self._lock:
            return self._data["failed"].get(sub.lower())

    def _age(self, iso):
        return (self._now() - datetime.fromisoformat(iso)).total_seconds() if iso else float("inf")

    def needs(self, sub, refresh=False):
        """The phases still to read for `sub`, in order: an empty list means it's complete and fresh. An entry is a
        dict: days {group: {day: cost}}, from, to, recent_read, stale, currency, parts, parts_month, parts_read."""
        if self.refused(sub):
            return []
        today, entry = self._today(), self.entry(sub)
        if not entry or "recent_read" not in entry:
            return [RECENT, EARLIER, PARTS]
        out = []
        if refresh or entry.get("stale") or self._age(entry["recent_read"]) >= FRESH_SECONDS or entry["to"] < today.isoformat():
            out.append(RECENT)
        if entry["from"] > window_start(today).isoformat():
            out.append(EARLIER)
        if refresh or entry.get("stale") or entry.get("parts_month") != today.isoformat()[:7] or \
                self._age(entry.get("parts_read")) >= FRESH_SECONDS:
            out.append(PARTS)
        return out

    def has_recent(self, sub):
        entry = self.entry(sub)
        return bool(entry and "recent_read" in entry)

    # ---- reading Azure

    def _range(self, phase, entry):
        today = self._today()
        start = window_start(today)
        has_days = bool(entry and "recent_read" in entry)
        if phase == WHOLE:
            return start, today
        if phase == RECENT:
            if has_days and entry["to"] >= (today - timedelta(days=RECENT_DAYS - SETTLE_DAYS)).isoformat():
                return max(start, date.fromisoformat(min(entry["to"], today.isoformat())) - timedelta(days=SETTLE_DAYS)), today
            return today - timedelta(days=RECENT_DAYS - 1), today
        if phase == EARLIER:
            if not has_days:
                return None
            end = date.fromisoformat(entry["from"]) - timedelta(days=1)
            return (start, end) if end >= start else None
        return today.replace(day=1), today

    def read(self, client, sub, phase):
        """Reads one phase of one subscription into the store. Raises ArmError (AccessDenied is also recorded)."""
        sub = sub.lower()
        span = self._range(phase, self.entry(sub))
        if span is None:
            return
        start, end = span
        if phase == PARTS:
            body = self._body(start, end, "None", ("ResourceId", "ServiceName"))
        else:
            body = self._body(start, end, "Daily", ("ResourceGroupName", "ChargeType"))
        try:
            rows = self._fetch(client, f"/subscriptions/{sub}", body)
        except AccessDenied:
            with self._lock:
                self._data["refused"][sub] = _iso(self._now())
                self._data["subs"].pop(sub, None)
            raise
        except ArmError as error:
            with self._lock:
                self._data["failed"][sub] = str(error).splitlines()[0][:300]
            raise
        with self._lock:
            self._data["failed"].pop(sub, None)
            self._data["refused"].pop(sub, None)
            current = dict(self._data["subs"].get(sub) or {"days": {}, "currency": ""})
            if phase == PARTS:
                parts = {}
                for r in rows:
                    key = (r.get("ResourceId") or "").lower()
                    service = r.get("ServiceName") or "(none)"
                    parts.setdefault(key, {})
                    parts[key][service] = round(parts[key].get(service, 0) + float(r.get("Cost") or 0), 4)
                current.update(parts=parts, parts_month=start.isoformat()[:7], parts_read=_iso(self._now()))
            else:
                self._merge_days(current, rows, start, end)
                if phase in (RECENT, WHOLE):
                    current["recent_read"] = _iso(self._now())
                    current.pop("stale", None)
            if rows and not current.get("currency"):
                current["currency"] = next((r.get("Currency") for r in rows if r.get("Currency")), "")
            self._data["subs"][sub] = current
            self._dirty += 1

    def _merge_days(self, current, rows, start, end):
        """Replaces every day in [start, end] with `rows`, drops days before the window, widens from/to. `days` holds
        each day's whole cost and `booked` the part of it that was purchases or refunds."""
        lo, hi = start.isoformat(), end.isoformat()
        days, booked = current["days"], current.setdefault("booked", {})
        for kept in (days, booked):
            for group in kept.values():
                for day in [d for d in group if lo <= d <= hi]:
                    del group[day]
        for r in rows:
            text = str(r.get("UsageDate"))[:8]
            day = f"{text[:4]}-{text[4:6]}-{text[6:8]}" if text.isdigit() else str(r.get("UsageDate"))[:10]
            group, cost = (r.get("ResourceGroupName") or "").lower(), float(r.get("Cost") or 0)
            days.setdefault(group, {})
            days[group][day] = round(days[group].get(day, 0) + cost, 4)
            if str(r.get("ChargeType") or "").lower() in BOOKED_CHARGES and cost:
                booked.setdefault(group, {})
                booked[group][day] = round(booked[group].get(day, 0) + cost, 4)
        keep_from = window_start(self._today()).isoformat()
        for kept in (days, booked):
            for name in list(kept):
                kept[name] = {d: c for d, c in kept[name].items() if d >= keep_from}
                if not kept[name]:
                    del kept[name]
        current["from"] = max(min(lo, current.get("from", lo)), keep_from)
        current["to"] = max(hi, current.get("to", hi))

    def ensure(self, client, sub, phases=(RECENT, EARLIER, PARTS), refresh=False):
        """Reads now what a page needs of `sub` and the store lacks, before the background fill. What's stored but
        stale is served as is and reread in the background, unless `refresh` (a Refresh button) asks now. Returns
        the phases read now (empty: answered from what's stored)."""
        entry = self.entry(sub)
        missing = self.needs(sub, refresh)
        if refresh:
            todo = [p for p in phases if p in missing]
        else:
            have_days = bool(entry and "recent_read" in entry)
            have_parts = bool(entry and entry.get("parts_month") == self._today().isoformat()[:7])
            todo = [p for p in phases if p in missing and not (p == RECENT and have_days) and not (p == PARTS and have_parts)]
        if RECENT in todo and EARLIER in todo:
            todo = [WHOLE] + [p for p in todo if p not in (RECENT, EARLIER)]
        if todo:
            with self.gate.foreground(), ThreadPoolExecutor(max_workers=len(todo)) as pool:
                futures = [pool.submit(self.read, client, sub, phase) for phase in todo]
                errors = [f.exception() for f in futures]  # every read answered (or refused) before deciding
            access = next((e for e in errors if isinstance(e, (AccessDenied, Cancelled, CredentialError))), None)
            other = next((e for e in errors if e is not None), None)
            if access or other:
                raise access or other
            self.save()
        if self.needs(sub):
            self.want([sub], client=client)
        return todo

    # ---- the background fill

    def want(self, subs, everything=None, client=None, refresh=False):
        """Puts `subs` first in the fill (and `everything`, the viewer's subscriptions, after), and starts the fill if
        it isn't running. `refresh` rereads them even if fresh."""
        with self._lock:
            first = [s.lower() for s in subs]
            self._wanted = first + [s for s in self._wanted if s not in first]
            for tried in self._tried.values():  # asked for again: worth another try in this run
                tried.difference_update(first)
            if everything is not None:
                self._all = [s.lower() for s in everything]
            if refresh:
                for sub in first:
                    if sub in self._data["subs"]:
                        self._data["subs"][sub]["stale"] = True
                    self._data["refused"].pop(sub, None)
            if client is not None and hasattr(client, "bound"):
                if self._cancel is None or self._cancel.is_set():
                    self._cancel = threading.Event()
                self._client = client.bound(self._cancel)
            elif client is not None:
                self._client = client
            if self._client is None or (self._thread and self._thread.is_alive()):
                return
            self.error = None
            self._tried = {RECENT: set(), EARLIER: set(), PARTS: set()}
            self._thread = threading.Thread(target=self._fill, daemon=True, name="cost-store fill")
            self._thread.start()

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def stop(self):
        if self._cancel:
            self._cancel.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _next(self, phase, tried, wanted_only=False):
        """The next subscriptions needing `phase`, wanted ones first (or only), leaving out those tried in this run."""
        with self._lock:
            order = list(self._wanted) if wanted_only else self._wanted + [x for x in self._all if x not in self._wanted]
        return [x for x in order if x not in tried and phase in self.needs(x)]

    def _fill(self):
        """Each phase over every subscription, PARALLEL at a time, saving every few; a subscription that fails is tried
        once per run unless asked for again (the next view or Refresh starts another try)."""
        try:
            while True:  # passes until one finds nothing: a Refresh mid-run marks subscriptions stale again
                worked = False
                for step, (phase, only) in enumerate(ORDER):
                    restart = False
                    while True:
                        with self._lock:
                            batch = self._next(phase, self._tried[phase], only)[:PARALLEL * SAVE_EVERY // 4 or 1]
                            self._tried[phase].update(batch)
                        if not batch:
                            break
                        worked = True
                        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
                            list(pool.map(lambda x, p=phase: self._one(x, p), batch))
                        self.save()
                        if any(self._next(p, self._tried[p], o) for p, o in ORDER[:step]):
                            restart = True  # newly wanted subscriptions go before the rest
                            break
                    if restart:
                        break
                if not worked:
                    break
        except Cancelled:
            pass
        except CredentialError as error:
            self.error = str(error)
        finally:
            if self._dirty:
                self.save()

    def _one(self, sub, phase):
        if self._cancel is not None and self._cancel.is_set():
            raise Cancelled()
        self.gate.background()
        try:
            self.read(self._client, sub, phase)
        except AccessDenied:
            pass
        except ArmError:
            pass  # recorded in `failed`; a later view asks again

    def read_since(self, sub, since):
        """Whether `sub`'s recent days were read from Azure at or after `since` (ISO), and not marked stale since (a
        Refresh marks them, and only a real reread clears the mark)."""
        entry = self.entry(sub)
        return bool(entry and entry.get("recent_read", "") >= since and not entry.get("stale"))

    def updating(self, sub):
        """Stored, but older than FRESH_SECONDS and being reread in the background."""
        return self.has_recent(sub) and self.running() and RECENT in self.needs(sub)

    def now_iso(self):
        return _iso(self._now())

    def progress(self, subs):
        """How far `subs` are: read (last 30 days in), refused, failed, and whether all of them are complete."""
        subs = [s.lower() for s in subs]
        refused = [s for s in subs if self.refused(s)]
        failed = [{"subscription_id": s, "error": self.failure(s)} for s in subs if self.failure(s) and not self.refused(s)
                  and not self.has_recent(s)]
        read = [s for s in subs if self.has_recent(s)]
        return {"read": read, "refused": refused, "failed": failed, "total": len(subs),
                "complete": all(not self.needs(s) for s in subs), "running": self.running(), "error": self.error}


_stores = {}
_stores_lock = threading.Lock()


def for_viewer(directory, tenant_id, object_id, fetch, body, today, cost_type="actual"):
    """The store for one viewer in one tenant and one cost type (one per process, so every request shares its fill).
    Amortized cost has its own file, filled only once someone asks for it."""
    key = (str(Path(directory).expanduser()), tenant_id.lower(), object_id.lower(), cost_type)
    with _stores_lock:
        if key not in _stores:
            suffix = "" if cost_type == "actual" else f"-{cost_type}"
            path = Path(key[0]) / f"costs-{key[1]}-{key[2]}{suffix}.json.gz"
            _stores[key] = Store(path, fetch, body, today)
            _stores[key].cost_type = cost_type
        return _stores[key]


def forget_refusals():
    with _stores_lock:
        stores = list(_stores.values())
    for store in stores:
        store.forget_refusals()


def reset():
    """Tests: stop every fill and drop the stores (files stay)."""
    with _stores_lock:
        stores = list(_stores.values())
        _stores.clear()
    for store in stores:
        store.stop()


on_forget_access(forget_refusals)
