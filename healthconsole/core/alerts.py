"""Alert rules: the server checks each rule on its interval and records what changed since the previous check.
Fires on change, never on state (#70); nothing is sent anywhere, the page asks what's new (#69, #71)."""
import itertools
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import config, inventory, triage
from .model import CRITICAL, HEALTHY, INFO, WARNING

logger = logging.getLogger(__name__)

PROBLEM_CARDS = ("alerts", "failed", "aks", "autoscale", "private_endpoints", "subnets")
HISTORY_HOURS = 24  # default for `alerts.history_hours`
MAX_EVENTS = 500
TICK_SECONDS = 15
UNTAGGED = "__untagged__"  # the page's "(untagged)" tag filter value
_RANK = {HEALTHY: 1, WARNING: 2, CRITICAL: 3}


class CantRun(Exception):
    """The rule's own setup stops it (a saved scope gone, an empty Watchlist): shown with its fix, no alert."""


class CheckFailed(Exception):
    """The check ran and couldn't read what it watches: one alert when it starts failing."""


@dataclass
class Source:
    """What one viewer's rules need, supplied by the front end, so this module has no Flask, files or `az`."""
    client: object
    rules: Callable[[], list]
    saved_scope: Callable[[str], dict | None]
    watchlist_items: Callable[[], list]
    triage_options: Callable[[], dict]
    check_watchlist: Callable[[object, list], list]
    # (client, saved scope, subscriptions, groups, rule) -> [{"prefix", "items", "error", "setup"}], one per connection
    servicenow_records: Callable | None = None
    monitor_records: Callable | None = None


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def _lasted(seconds):
    minutes = round(max(0, seconds) / 60)
    return "under a minute" if minutes < 1 else f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def cards_for(signals):
    cards = set()
    if {"status", "new_red"} & set(signals):
        cards.update(PROBLEM_CARDS)
    if "service_health" in signals:
        cards.add("service_health")
    return cards


def estimate_calls(rule, saved_scope=None, watchlist_items=()):
    """What one check costs, shown before the first check has measured it."""
    if rule["kind"] == "watchlist":
        count = len(watchlist_items)
        return f"{count} quick check{'s' if count != 1 else ''} (one per Watchlist item, as Check now)"
    narrowed = bool(saved_scope and (saved_scope.get("resource_groups") or saved_scope.get("tag_filters")))
    queries = len(cards_for(rule["signals"])) + (1 if narrowed else 0)
    extra = [label for signal, label in (("servicenow", "the ServiceNow incidents (cached 60 s, shared with its tab)"),
                                         ("monitors", "each monitoring connection's open alerts (cached 60 s)"))
             if signal in rule["signals"]]
    if extra:
        queries += 1  # the resources in scope, which records are matched against
    return f"about {queries} Resource Graph queries and 1 subscription list" + (f", plus {' and '.join(extra)}" if extra else "")


def _tags_match(group, filters):
    for key, wanted in filters.items():
        value = group["tags"].get(key.lower())
        if (bool(value) if wanted == UNTAGGED else value != wanted):
            return False
    return True


def resolve_scope(client, scope):
    """A saved scope's subscription IDs and, when it's narrowed, its resource group keys ("sub/rg"), read now."""
    if not scope["subscriptions"]:
        raise CantRun(f"The saved scope '{scope['name']}' has no subscriptions: update it in the Scope bar.")
    subs = inventory.list_subscriptions(client)
    ids = [s["id"] for s in subs if config.matches(scope["subscriptions"], s["name"], s["id"])]
    if not ids:
        raise CheckFailed(f"None of the saved scope's subscriptions ({', '.join(scope['subscriptions'])}) are visible "
                          "now: a PIM role may have ended, or az login is signed in as someone else.")
    if not scope["resource_groups"] and not scope["tag_filters"]:
        return ids, None
    groups = [g for g in inventory.list_resource_groups(client, ids)
              if _tags_match(g, scope["tag_filters"])
              and (not scope["resource_groups"] or config.matches(scope["resource_groups"], g["name"]))]
    if not groups:
        raise CheckFailed(f"No resource groups match the saved scope '{scope['name']}' now.")
    return ids, [f"{g['subscription_id']}/{g['name'].lower()}" for g in groups]


def observe_triage(result):
    """Triage cards as {key: item} for resources, {key: event} for Service Health, and the cards that failed."""
    items, events, failed = {}, {}, {}
    for card in result["cards"]:
        if card["error"]:
            failed[card["key"]] = card["error"]
            continue
        for item in card["items"]:
            if card["key"] == "service_health":
                if item.get("phase", "active") != "active":  # resolved and advisories never raise an alert
                    continue
                events[f"{item['display_type']}|{item['name']}".lower()] = {**item, "card": card["title"]}
            else:
                key = f"{card['key']}|{item['id'].lower()}|{item['name'].lower()}"
                items[key] = {**item, "card": card["title"], "card_key": card["key"]}
    return items, events, failed


RECOVERY_SIGNALS = ("recovered", "check_recovered")
RECOVERED_HEADINGS = {"service_health": "Service Health over", "servicenow": "Incident closed",
                      "monitors": "Alert closed", "last_week": "Back in line with last week"}


def counts(event, dismissed, now):
    """Whether an alert belongs in the bar: new since Dismiss, still open, not a recovery, not acknowledged and not
    snoozed. The page applies the same rule."""
    return (event["seq"] > dismissed and not event.get("resolved_at") and event["signal"] not in RECOVERY_SIGNALS
            and not event.get("acknowledged_at") and event.get("snoozed_until", 0) <= now)


def _fired(signal, status, title, detail="", resource_id="", name="", key="", was=None):
    """An alert to record. `key` names the condition and `was` the status before it, so a later check can tell
    when it's back to how it was (None: back only when it's gone, e.g. a Service Health event)."""
    return {"signal": signal, "status": status, "title": title, "detail": detail, "resource_id": resource_id,
            "name": name, "key": key, "was": was}


def diff_triage(before, items, events, failed, signals):
    """(alerts to raise, the new baseline). A resource not listed before counts as healthy; a card that failed
    this time keeps its previous items, so a failed query never looks like a recovery or a new red."""
    fired = []
    for key, item in items.items():
        prev = before["items"].get(key)
        where = f"{item['card']}: {item['detail']}"
        if prev is None and item["status"] == CRITICAL and "new_red" in signals:
            fired.append(_fired("new_red", CRITICAL, f"New red: {item['name']}", where, item["id"], item["name"],
                                key, HEALTHY))
        elif "status" in signals and _RANK.get(item["status"], 0) > _RANK.get(prev["status"] if prev else HEALTHY, 0):
            was = prev["status"] if prev else "healthy"
            fired.append(_fired("status", item["status"], f"{item['name']}: {was} → {item['status']}", where,
                                item["id"], item["name"], key, was))
    if "service_health" in signals and "service_health" not in failed:
        for key, event in events.items():
            if key not in before["events"]:
                fired.append(_fired("service_health", WARNING if event["status"] == WARNING else INFO,
                                    f"Service Health: {event['name']}", f"{event['display_type']}; {event['detail']}",
                                    name=event["name"], key=f"sh|{key}"))
    kept = {k: v for k, v in before["items"].items() if v["card_key"] in failed}
    baseline = {"items": {**kept, **items},
                "events": before["events"] if "service_health" in failed else events}
    return fired, baseline


def _mover_key(item_id, mover):
    return f"lw|{item_id}|{mover['node_id'].lower()}|{mover['signal']}"


def _item_of(key):
    """The Watchlist item a condition key belongs to (a mover's key carries it second)."""
    return key.split("|")[1] if key.startswith("lw|") else key


def diff_watchlist(before, rows, signals):
    """(alerts, baseline {"status": {id: status}, "movers": {key: mover}}, rows with `was`). Items that couldn't be
    read keep what they had; a new pin is its own starting point, not a change."""
    before = before or {"status": {}, "movers": {}}
    fired, status, movers, unread = [], {}, {}, 0
    for row in rows:
        key = row["id"].lower()
        prev = before["status"].get(key)
        row["was"] = None
        if not row.get("checked_at"):
            unread += 1
            if prev:
                status[key] = prev
            movers.update({k: v for k, v in before["movers"].items() if _item_of(k) == key})
            continue
        for mover in row.get("movers") or []:
            mkey = _mover_key(key, mover)
            movers[mkey] = mover
            if prev and mkey not in before["movers"] and "last_week" in signals:
                fired.append(_fired("last_week", mover["status"],
                                    f"{row['name']}: {mover['signal']} {mover['move']}",
                                    f"{mover['name']}: {mover['value']:g} now, {mover['baseline']:g} last week"
                                    f"{(' ' + mover['unit']) if mover.get('unit') else ''}",
                                    row["id"], row["name"], mkey))
        if row["status"] not in _RANK:
            continue
        if prev and _RANK[row["status"]] > _RANK[prev]:
            row["was"] = prev
            if "status" in signals:
                fired.append(_fired("status", row["status"], f"{row['name']}: {prev} → {row['status']}",
                                    "; ".join(row["problems"][:2]), row["id"], row["name"], key, prev))
        status[key] = row["status"]
    if rows and unread == len(rows):
        raise CheckFailed(rows[0]["problems"][0] if rows[0]["problems"] else "No Watchlist item could be read.")
    return fired, {"status": status, "movers": movers}, rows


def diff_external(before, results, signals):
    """New ServiceNow incidents and monitoring alerts since the last check. A connection that couldn't be read keeps
    its previous records, so a failed read neither resolves nor re-opens anything (#73)."""
    fired, current = [], {}
    for result in results:
        if result.get("error") or result.get("setup") or result.get("truncated"):
            # unread, or cut off at the record limit: absent doesn't mean closed
            current.update({k: v for k, v in before.items() if k.startswith(result["prefix"])})
        if result.get("error") or result.get("setup"):
            continue
        for item in result["items"]:
            current[item["key"]] = item
            if item["key"] not in before and item["signal"] in signals:
                fired.append(_fired(item["signal"], item["status"], item["title"], item["detail"],
                                    item.get("resource_id", ""), item.get("name", ""), item["key"]))
    return fired, current


def summary(rule, baseline, new, at):
    """What a check saw: new alerts (None on the first check, which only records), and the known problems it's
    tracking, which never alert again until they change."""
    out = {"at": at, "new": new, "first": new is None}
    if rule["kind"] == "watchlist":
        statuses = list(baseline["status"].values())
        out.update(items=len(statuses), movers=len(baseline["movers"]))
    else:
        statuses = [v["status"] for v in baseline["items"].values()]
        external = baseline.get("external", {})
        out.update(service_health=len(baseline["events"]),
                   servicenow=sum(1 for k in external if k.startswith("sn|")),
                   monitors=sum(1 for k in external if k.startswith("mon|")))
    out.update(critical=statuses.count(CRITICAL), warning=statuses.count(WARNING))
    return out


@dataclass
class _RuleState:
    fingerprint: str
    baseline: object = None
    last_check: float = 0.0
    next_due: float = 0.0
    running: bool = False
    error: str | None = None
    failing: bool = False
    cant_run: str | None = None
    partial: list = field(default_factory=list)
    calls: int | None = None
    checks: int = 0
    latest: list | None = None
    open: dict = field(default_factory=dict)  # condition key -> what alerted, until it's back to how it was
    present: set | None = None  # the Watchlist's items at the last check (a removed one resolves silently)
    failed_seq: int | None = None
    setup: list = field(default_factory=list)  # signals the rule asks for that its saved scope can't give (#38)
    result: dict | None = None  # what the last successful check saw, shown live, never stored as history


@dataclass
class _Session:
    owner: str
    source: Source
    last_seen: float
    rules: dict = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    dismissed: int = 0
    snoozed_rules: dict = field(default_factory=dict)  # rule name -> until (epoch)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _fingerprint(rule):
    return json.dumps([rule["kind"], rule["scope"], sorted(rule["signals"])])


class AlertRunner:
    """One per server. Sessions are keyed by identity (tenant/object ID), each with its own rules, baselines and
    history. `session_ttl` (hosted) drops a viewer who stopped polling, since their delegated token goes with them."""

    def __init__(self, clock=time.time, session_ttl=None, history_hours=lambda: HISTORY_HOURS):
        self.clock = clock
        self.session_ttl = session_ttl
        self.history_hours = history_hours  # read on every call, so a saved setting applies without a restart
        self._sessions = {}
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._wake = threading.Event()
        self._thread = None
        self._adopt = None
        self.runner_id = uuid.uuid4().hex[:8]  # alert numbers restart with the server; the page keys its cursor by this

    def register(self, owner, source):
        with self._lock:
            session = self._sessions.get(owner)
            if session is None:
                session = self._sessions[owner] = _Session(owner, source, self.clock())
            session.source, session.last_seen = source, self.clock()
        return session

    def start(self, adopt=None):
        """Starts the background checks; `adopt()` returns (owner, source) for the default sign-in, retried each
        tick until it works, so rules run from server start, before any page is open."""
        self._adopt = adopt
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="alert-runner", daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:  # the runner must outlive any one bad check
                logger.exception("alert tick failed")
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    def tick(self):
        if self._adopt:
            try:
                self.register(*self._adopt())
                self._adopt = None
            except Exception as error:
                logger.debug("alerts: default sign-in not usable yet: %s", error)
        now = self.clock()
        with self._lock:
            if self.session_ttl:
                for owner in [o for o, s in self._sessions.items() if now - s.last_seen > self.session_ttl]:
                    del self._sessions[owner]
            sessions = list(self._sessions.values())
        for session in sessions:
            for rule, rule_state in self._sync(session):
                if rule_state.cant_run is None and rule_state.next_due <= self.clock():
                    self._run(session, rule, rule_state)

    def _sync(self, session):
        """Matches the session's state to its current rules: a changed scope or signals starts a new baseline, a
        changed interval only moves the next check. Also re-evaluates why a rule can't run (config only, no calls)."""
        try:
            rules = [r for r in session.source.rules() if r["enabled"]]
        except Exception:
            logger.exception("alerts: couldn't read rules")
            return []
        current = {}
        for rule in rules:
            st = session.rules.get(rule["name"])
            if st is None or st.fingerprint != _fingerprint(rule):
                if st is not None:
                    self._close_all(session, st, "the rule changed")
                st = _RuleState(_fingerprint(rule))
            elif st.last_check and st.next_due:  # 0 means Check now was asked for
                st.next_due = st.last_check + rule["interval_minutes"] * 60
            st.cant_run = self._cant_run(session.source, rule)
            current[rule["name"]] = st
        for name, st in session.rules.items():
            if name not in current:
                self._close_all(session, st, "the rule was turned off or deleted")
        session.rules = current
        return [(rule, current[rule["name"]]) for rule in rules]

    @staticmethod
    def _cant_run(source, rule):
        if rule["kind"] == "watchlist":
            if not source.watchlist_items():
                return "The Watchlist is empty: pin resources or resource groups with + Watch."
            return None
        if source.saved_scope(rule["scope"]) is None:
            return (f"There's no saved scope called '{rule['scope']}' in this tenant: pick another in Settings → "
                    "Alerts, or save one with that name from the Scope bar.")
        return None

    def _run(self, session, rule, st):
        st.running = True
        client = session.source.client
        bound = client.bound(threading.Event()) if hasattr(client, "bound") else client
        calls_before = 0 if bound is not client else getattr(client, "call_count", 0)
        first = st.baseline is None
        try:
            if rule["kind"] == "watchlist":
                fired = self._check_watchlist(session.source, bound, rule, st)
            else:
                fired = self._check_scope(session.source, bound, rule, st)
        except CantRun as reason:
            st.cant_run = str(reason)
        except Exception as error:  # Azure, the session or the token: the watcher must say it stopped (#69)
            if not isinstance(error, CheckFailed):
                logger.info("alert rule %s failed: %s", rule["name"], error)
            st.error = str(error).splitlines()[0] if str(error) else type(error).__name__
            if not st.failing:
                st.failing = True
                st.failed_seq = self._record(session, rule, _fired("check_failed", CRITICAL,
                                                                   f"{rule['name']}: checks are failing", st.error))["seq"]
        else:
            if st.failing:
                self._resolve(session, [st.failed_seq], "checks work again")
                self._record(session, rule, {**_fired("check_recovered", HEALTHY,
                                                      f"{rule['name']}: checks are working again"),
                                             "notify": rule["notify"] and rule.get("notify_recovered", True)})
            st.failing, st.error, st.failed_seq = False, None, None
            if not first:
                for item in fired:
                    self._open(st, item, self._record(session, rule, item))
                self._recover(session, rule, st)
            st.result = summary(rule, st.baseline, None if first else len(fired), self.clock())
        finally:
            st.running = False
            st.calls = getattr(bound, "call_count", 0) - calls_before
            st.last_check = self.clock()
            st.next_due = st.last_check + rule["interval_minutes"] * 60
            st.checks += 1

    @staticmethod
    def _check_scope(source, client, rule, st):
        scope = source.saved_scope(rule["scope"])
        if scope is None:
            raise CantRun(f"There's no saved scope called '{rule['scope']}' in this tenant.")
        subscriptions, groups = resolve_scope(client, scope)
        cards = cards_for(rule["signals"])
        result = (triage.triage(client, subscriptions, source.triage_options(), groups, cards=cards) if cards
                  else {"cards": []})
        items, events, failed = observe_triage(result)
        external = []
        for signal, read in (("servicenow", source.servicenow_records), ("monitors", source.monitor_records)):
            if signal in rule["signals"] and read:
                external += read(client, scope, subscriptions, groups, rule)
        unread = [r for r in external if r.get("error")]
        if (failed and len(failed) == len(result["cards"])) and len(unread) == len(external):
            raise CheckFailed(next(iter(failed.values())).splitlines()[0])
        if not result["cards"] and external and len(unread) == len(external):
            raise CheckFailed(unread[0]["error"])
        titles = {c["key"]: c["title"] for c in result["cards"]}
        st.partial = ([f"{titles[k]}: {v.splitlines()[0]}" for k, v in failed.items()] + [r["error"] for r in unread]
                      + [f"{r['prefix'].split('|')[1]}: more records than the read limit, so a closed one past it can "
                         "look open" for r in external if r.get("truncated")])
        st.setup = [r["setup"] for r in external if r.get("setup")]
        before = st.baseline or {"items": {}, "events": {}, "external": {}}
        fired, st.baseline = diff_triage(before, items, events, failed, rule["signals"])
        more, st.baseline["external"] = diff_external(before.get("external", {}), external, rule["signals"])
        return fired + more

    @staticmethod
    def _check_watchlist(source, client, rule, st):
        items = source.watchlist_items()
        if not items:
            raise CantRun("The Watchlist is empty.")
        rows = source.check_watchlist(client, items)
        fired, st.baseline, st.latest = diff_watchlist(st.baseline, rows, rule["signals"])
        st.present = {i["id"].lower() for i in items}
        st.partial = [f"{r['name']}: {r['problems'][0]}" for r in rows if not r.get("checked_at") and r["problems"]]
        return fired

    def _record(self, session, rule, item):
        now = self.clock()
        event = {"notify": rule["notify"], **{k: v for k, v in item.items() if k not in ("key", "was")},
                 "seq": next(self._seq), "at": _iso(now), "epoch": now, "rule": rule["name"]}
        until = session.snoozed_rules.get(rule["name"], 0)
        if until > now:  # recorded, but quiet until the rule's snooze ends
            event.update(snoozed_until=until, notify=False)
        with session.lock:
            session.events.append(event)
        return event

    @staticmethod
    def _open(st, item, event):
        if not item.get("key"):
            return
        current = st.open.get(item["key"])
        if current:
            current["seqs"].append(event["seq"])
            current["status"] = item["status"]
        else:
            st.open[item["key"]] = {"seqs": [event["seq"]], "was_rank": _RANK.get(item["was"], 0) if item["was"] else None,
                                    "status": item["status"], "since": event["epoch"], "signal": item["signal"],
                                    "name": item["name"] or item["title"], "resource_id": item["resource_id"]}

    def _current(self, rule, st):
        """{condition key: status} as of this check, in the same keys alerts were opened with."""
        if rule["kind"] == "watchlist":
            return {**st.baseline["status"], **{k: v["status"] for k, v in st.baseline["movers"].items()}}
        return {**{k: v["status"] for k, v in st.baseline["items"].items()},
                **{f"sh|{k}": v["status"] for k, v in st.baseline["events"].items()},
                **{k: v["status"] for k, v in st.baseline.get("external", {}).items()}}

    def _recover(self, session, rule, st):
        """Alerts whose condition is back to how it was before: resolved, plus a 'recovered' entry (notified when the
        rule asks for it). A failed read keeps its previous state, so it never counts as a recovery (#73)."""
        if not st.open:
            return
        current = self._current(rule, st)
        for key, condition in list(st.open.items()):
            status = current.get(key)
            if status is not None and (condition["was_rank"] is None or _RANK.get(status, 0) > condition["was_rank"]):
                continue  # still there (one with no earlier status, like an incident, resolves only when gone)
            del st.open[key]
            if rule["kind"] == "watchlist" and st.present is not None and _item_of(key) not in st.present:
                self._resolve(session, condition["seqs"], "removed from the Watchlist")
                continue
            self._resolve(session, condition["seqs"], "back to normal")
            heading = RECOVERED_HEADINGS.get(condition["signal"], "Recovered")
            self._record(session, rule, {
                **_fired("recovered", HEALTHY, f"{heading}: {condition['name']}",
                         f"was {condition['status']} for {_lasted(self.clock() - condition['since'])}",
                         condition["resource_id"], condition["name"]),
                "notify": rule["notify"] and rule.get("notify_recovered", True)})

    def _resolve(self, session, seqs, note):
        now, wanted = self.clock(), set(seqs)
        with session.lock:
            for event in session.events:
                if event["seq"] in wanted and not event.get("resolved_at"):
                    event.update(resolved_at=_iso(now), resolved_epoch=now, resolved_note=note)

    def _close_all(self, session, st, note):
        seqs = [seq for condition in st.open.values() for seq in condition["seqs"]]
        if st.failed_seq:
            seqs.append(st.failed_seq)
        if seqs:
            self._resolve(session, seqs, note)

    def has(self, owner):
        return owner in self._sessions

    def wake(self):
        self._wake.set()

    def run_now(self, owner, name):
        session = self._sessions.get(owner)
        if session is None:
            return False
        if name not in session.rules:
            self._sync(session)  # a rule saved since the last tick
        st = session.rules.get(name)
        if not st:
            return False
        st.next_due = 0
        self._wake.set()
        return True

    def _event(self, owner, seq):
        session = self._sessions.get(owner)
        if session:
            with session.lock:
                return session, next((e for e in session.events if e["seq"] == int(seq)), None)
        return None, None

    def acknowledge(self, owner, seq):
        """Someone's on it: off the bar, still open, and it still resolves (or alerts again if it gets worse)."""
        session, event = self._event(owner, seq)
        if event:
            event["acknowledged_at"] = _iso(self.clock())
        return bool(event)

    def snooze(self, owner, seq, seconds=3600):
        """Off the bar until then; back if it's still open. 0 ends the snooze."""
        session, event = self._event(owner, seq)
        if event:
            event["snoozed_until"] = self.clock() + seconds if seconds else 0
        return bool(event)

    def snooze_rule(self, owner, name, seconds=3600):
        """The rule keeps checking and recording; its alerts stay off the bar and notify nobody until then."""
        session = self._sessions.get(owner)
        if not session:
            return False
        if name not in session.rules:
            self._sync(session)  # a rule saved since the last tick
        if seconds and name not in session.rules:
            return False  # no such rule: say so instead of snoozing a name nothing uses
        if seconds:
            until = session.snoozed_rules[name] = self.clock() + seconds
            with session.lock:  # its alerts already open go quiet too
                for event in session.events:
                    if event["rule"] == name and not event.get("resolved_at") and event.get("snoozed_until", 0) < until:
                        event["snoozed_until"] = until
        else:
            session.snoozed_rules.pop(name, None)
            with session.lock:
                for event in session.events:
                    if event["rule"] == name and event.get("snoozed_until", 0) > self.clock():
                        event["snoozed_until"] = 0
        return True

    def _prune(self, session, now):
        keep = self.history_hours() * 3600
        while session.events and now - session.events[0]["epoch"] > keep:
            session.events.popleft()

    def history(self, owner):
        session = self._sessions.get(owner)
        if not session:
            return []
        with session.lock:
            self._prune(session, self.clock())
            return list(reversed(session.events))

    def dismiss(self, owner, seq):
        session = self._sessions.get(owner)
        if session:
            session.dismissed = max(session.dismissed, int(seq))

    def state(self, owner):
        """Rules with their heartbeat and why they can't run, plus the last `history_hours` of alerts, newest first."""
        now = self.clock()
        session = self._sessions.get(owner)
        if session is None:
            return {"rules": [], "events": [], "dismissed": 0, "unseen": 0, "now": now, "runner_id": self.runner_id,
                    "history_hours": self.history_hours()}
        session.last_seen = now
        with session.lock:
            self._prune(session, now)
            events = list(reversed(session.events))
        try:
            rules = session.source.rules()
        except Exception as error:
            rules, session_error = [], str(error)
        else:
            session_error = None
        out = []
        for rule in rules:
            st = session.rules.get(rule["name"]) if rule["enabled"] else None
            saved = session.source.saved_scope(rule["scope"]) if rule["kind"] == "scope" else None
            snoozed = session.snoozed_rules.get(rule["name"], 0)
            entry = {**rule, "signal_names": [config.ALERT_SIGNALS[s] for s in rule["signals"]],
                     "snoozed_until": snoozed if snoozed > now else None,
                     "estimate": estimate_calls(rule, saved, session.source.watchlist_items()
                                                if rule["kind"] == "watchlist" else ())}
            if st:
                entry.update(last_check=st.last_check or None, next_due=st.next_due if st.last_check else None,
                             running=st.running, error=st.error, failing=st.failing, cant_run=st.cant_run,
                             partial=st.partial, setup=st.setup, calls=st.calls, result=st.result, checks=st.checks, baseline=st.baseline is not None,
                             latest=st.latest)
            out.append(entry)
        return {"rules": out, "events": events, "dismissed": session.dismissed,
                "unseen": sum(1 for e in events if counts(e, session.dismissed, now)), "now": now,
                "runner_id": self.runner_id, "error": session_error, "history_hours": self.history_hours()}
