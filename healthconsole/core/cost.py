"""Costs from Azure Cost Management (actual cost as invoiced, or amortized: reservations spread over their days and
charged to what uses them), read-only: a subscription's or resource group's month,
one resource's last 30 days, the scope's subscriptions and groups with what moved and spiked, and resources that cost
money while doing nothing (Resource Graph only). Subscriptions and groups are answered from the viewer's cost store
(`cost_store`), filled once per subscription and kept on disk; only a resource's own tab queries Azure each time."""
import time
import calendar
import functools
import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

from . import cost_store
from .arm import AccessDenied, ArmError, Cancelled
from .cache import TtlCache

API = "2023-11-01"
BUDGETS_API = "2023-05-01"
QUERY_PATH = "{scope}/providers/Microsoft.CostManagement/query"
BUDGETS_PATH = "{scope}/providers/Microsoft.Consumption/budgets"
SCOPE_SECONDS = 3600        # a resource's tab (the only cost page not served from the store)
WINDOW_DAYS = 30             # one month of data per query is one unit of Cost Management's throttle
WATCH_SECONDS = 1.5          # how often the Cost view's job publishes what the store has so far
MAX_PAGES = 20
TOP = 15
SPIKE_FACTOR = 2.0           # a day at least twice the usual (median of the 7 days before)
SPIKE_MIN_SHARE = 0.01       # and at least 1% of the subscription's usual day, so pennies don't spike
ROLE = "Cost Management Reader or Reader on the subscription"
COST_TYPES = {"actual": "ActualCost", "amortized": "AmortizedCost"}
SUBSCRIPTION_LEVEL = "Subscription-level charges"  # cost billed to the subscription, in no resource group
BOOKED_CHARGES = cost_store.BOOKED_CHARGES

_cache = TtlCache("costs", SCOPE_SECONDS)


def _today():
    return datetime.now(timezone.utc).date()


def _day(value):
    text = str(value)[:8]
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if text.isdigit() else str(value)[:10]


def cost_type_of(value):
    """"actual" (the default) or "amortized"; anything else is refused."""
    value = (value or "actual").lower()
    if value not in COST_TYPES:
        raise ValueError(f"cost_type must be one of {', '.join(COST_TYPES)}")
    return value


def _body(start, end, granularity="None", grouping=(), filter_=None, cost_type="actual"):
    dataset = {"granularity": granularity, "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
               "grouping": [{"type": "Dimension", "name": g} for g in grouping]}
    if filter_:
        dataset["filter"] = filter_
    return {"type": COST_TYPES[cost_type], "timeframe": "Custom",
            "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T23:59:59Z"},
            "dataset": dataset}


def _rows(payload):
    props = (payload or {}).get("properties") or {}
    names = [c.get("name") for c in props.get("columns") or []]
    return [dict(zip(names, row)) for row in props.get("rows") or []], props.get("nextLink")


class _Rows(list):
    """A query's rows and when Azure answered them (UTC ISO), so a page served from the cache says how old it is."""
    read_at = ""


def _oldest(*rows):
    return min((getattr(r, "read_at", "") for r in rows if getattr(r, "read_at", "")), default="")


def fetch(client, scope, body):
    """[{column: value}] for one Cost Management query, every page, straight from Azure (paging posts the same body to
    the nextLink). The store and `query` both read through this."""
    path = QUERY_PATH.format(scope=scope)
    rows, next_link = _rows(client.post(path, API, body, cache=False))
    pages = 1
    while next_link and pages < MAX_PAGES:
        parts = urlsplit(next_link)
        params = {k: v for k, v in parse_qsl(parts.query) if k.lower() != "api-version"}
        more, next_link = _rows(client.post(parts.path, API, body, cache=False, params=params))
        rows.extend(more)
        pages += 1
    out = _Rows(rows)
    out.read_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def query(client, scope, body):
    """`fetch`, cached per identity for an hour (a Refresh reads again)."""
    return _cache.get_or_load(_key(client, scope, body), lambda: fetch(client, scope, body),
                              refresh=getattr(client, "is_fresh", False))  # refusals: the ARM client


def _key(client, scope, body):
    identity = client.identity
    return identity.tenant_id, identity.object_id, scope.lower(), json.dumps(body, sort_keys=True)


def _currency(rows):
    return next((r.get("Currency") for r in rows if r.get("Currency")), "")


def _sum(rows):
    return sum(float(r.get("Cost") or 0) for r in rows)


def _denied(error):
    return f"Costs aren't readable here: needs {ROLE}." if isinstance(error, AccessDenied) else \
        f"Azure Cost Management didn't answer: {str(error).splitlines()[0]}"


def month_figures(daily, today, booked=None):
    """From [{date, cost}] since the first of last month: this month so far, last month over the same days, all of
    last month, and where this month is heading. Costs arrive a day or so late, so "so far" ends at the latest day with
    data, and last month is compared up to the same day of the month. `booked` ([{date, cost}], the part that was
    purchases or refunds) is counted once in the projection, never as a daily rate, and last month's purchases on
    later days are expected again (reservations are billed monthly on their purchase day)."""
    first = today.replace(day=1)
    previous = first - timedelta(days=1)
    month, last = first.isoformat()[:7], previous.isoformat()[:7]
    paid = {d["date"]: d["cost"] for d in booked or []}
    this = [d for d in daily if d["date"][:7] == month]
    up_to = max((d["date"] for d in this if d["cost"]), default=None)
    through = int(up_to[8:10]) if up_to else 0
    days = calendar.monthrange(today.year, today.month)[1]
    mtd = sum(d["cost"] for d in this)
    booked_now = sum(c for d, c in paid.items() if d[:7] == month)
    expected = sum(c for d, c in paid.items() if d[:7] == last and through < int(d[8:10]) <= days)
    same_to = f"{last}-{min(through, previous.day):02d}" if through else None
    return {"month_to_date": mtd, "up_to": up_to, "same_days_to": same_to,
            "last_month_same_days": sum(d["cost"] for d in daily if d["date"][:7] == last and int(d["date"][8:10]) <= through),
            "last_month": sum(d["cost"] for d in daily if d["date"][:7] == last),
            "booked": booked_now, "booked_days": [{"date": d, "cost": c} for d, c in sorted(paid.items()) if d[:7] == month and c],
            "booked_expected": expected,
            "projected": booked_now + expected + (mtd - booked_now) / through * days if through else None}


def _last_days(costs, today):
    """[{date, cost}] for each of the last WINDOW_DAYS days, 0 where Azure listed nothing (it leaves those days out)."""
    return [{"date": (today - timedelta(days=i)).isoformat(), "cost": costs.get((today - timedelta(days=i)).isoformat(), 0)}
            for i in range(WINDOW_DAYS - 1, -1, -1)]


def _ranked(totals, top=TOP):
    ordered = sorted(totals.items(), key=lambda kv: -kv[1])
    rest = sum(v for _, v in ordered[top:])
    return [{"name": k, "cost": v} for k, v in ordered[:top]] + ([{"name": "Other", "cost": rest, "other": True}] if rest else [])


def _budgets(client, scope):
    try:
        items = client.get_paged(BUDGETS_PATH.format(scope=scope), BUDGETS_API)
    except ArmError as error:
        return [], (f"Budgets aren't readable here: needs {ROLE}." if isinstance(error, AccessDenied)
                    else f"Budgets weren't read: {str(error).splitlines()[0]}")
    out = []
    for item in items:
        props = item.get("properties") or {}
        amount = float(props.get("amount") or 0)
        spent = float((props.get("currentSpend") or {}).get("amount") or 0)
        forecast = (props.get("forecastSpend") or {}).get("amount")
        out.append({"name": item.get("name") or "", "amount": amount, "time_grain": props.get("timeGrain") or "",
                    "spent": spent, "forecast": float(forecast) if forecast is not None else None,
                    "percent": round(spent / amount * 100, 1) if amount else None,
                    "currency": (props.get("currentSpend") or {}).get("unit") or ""})
    return out, None


def store_for(client, directory, cost_type="actual"):
    """The viewer's cost store for one cost type (one per tenant and identity, kept in `directory`)."""
    identity = client.identity
    return cost_store.for_viewer(directory, identity.tenant_id, identity.object_id, fetch,
                                 functools.partial(_body, cost_type=cost_type), _today, cost_type)


def _store_queries(today, cost_type="actual"):
    start = cost_store.window_start(today)
    return [{"label": "Kept for every subscription: cost by resource group, charge type and day, since the first of last "
                      f"month (the last {cost_store.SETTLE_DAYS} days reread when older than "
                      f"{cost_store.FRESH_SECONDS // 3600} h)",
             "query": json.dumps(_body(start, today, "Daily", ("ResourceGroupName", "ChargeType"), cost_type=cost_type), indent=2)},
            {"label": "Kept for every subscription: this month by resource and service",
             "query": json.dumps(_body(today.replace(day=1), today, "None", ("ResourceId", "ServiceName"), cost_type=cost_type),
                                 indent=2)}]


def _group_of(resource_id):
    parts = resource_id.split("/")
    return parts[4] if len(parts) > 4 and parts[3] == "resourcegroups" else ""


def _daily(entry, group=None, kept="days"):
    """[{date, cost}] for the subscription (all groups) or one group, from a store entry: whole cost, or with
    kept="booked" the part that was purchases or refunds."""
    totals = {}
    for name, days in (entry.get(kept) or {}).items():
        if group is None or name == group:
            for day, cost in days.items():
                totals[day] = totals.get(day, 0) + cost
    return sorted(({"date": d, "cost": c} for d, c in totals.items()), key=lambda d: d["date"])


def _usage(daily, booked):
    """{date: cost} without what was booked: what the scope used, day by day."""
    paid = {d["date"]: d["cost"] for d in booked}
    return {d["date"]: d["cost"] - paid.get(d["date"], 0) for d in daily}


def summary(client, scope, store, today=None, refresh=False):
    """A subscription or resource group: its month (so far, last month, projected), the last 30 days by day, by
    service, its top resource groups (or resources), and its budgets, from the store (read now if it lacks them)."""
    today = today or _today()
    parts = scope.strip("/").split("/")
    sub, group = parts[1].lower(), (parts[3].lower() if len(parts) > 3 else None)
    queries = _store_queries(today, getattr(store, "cost_type", "actual"))
    read_now = []
    try:
        read_now = store.ensure(client, sub, refresh=refresh)
    except Cancelled:
        raise
    except ArmError as error:
        if store.entry(sub) is None or isinstance(error, AccessDenied):
            return {"scope": scope, "error": _denied(error), "required": ROLE if isinstance(error, AccessDenied) else None,
                    "queries": queries}
    entry = store.entry(sub)
    if entry is None:
        return {"scope": scope, "error": f"Costs aren't readable here: needs {ROLE}.", "required": ROLE, "queries": queries}
    daily, booked = _daily(entry, group), _daily(entry, group, "booked")
    pieces = entry.get("parts") or {}
    services, top_parts = {}, {}
    month = today.isoformat()[:7]
    if group is None:
        for name, days in entry["days"].items():
            cost = sum(c for d, c in days.items() if d[:7] == month)
            if cost:
                top_parts[name or SUBSCRIPTION_LEVEL] = {"cost": cost, "services": {}}
    for resource_id, by_service in pieces.items():
        rg = _group_of(resource_id)
        if group is not None and rg != group:
            continue
        for service, cost in by_service.items():
            services[service] = services.get(service, 0) + cost
            key = (resource_id or "(not in a resource group)") if group is not None else (rg or SUBSCRIPTION_LEVEL)
            item = top_parts.setdefault(key, {"cost": 0, "services": {}})
            if group is not None:
                item["cost"] += cost
            item["services"][service] = item["services"].get(service, 0) + cost
    top = [{"name": name, "id": name if name.startswith("/subscriptions/") else None, "cost": p["cost"],
            "subscription_level": group is None and name == SUBSCRIPTION_LEVEL,
            "services": [x for x, _ in sorted(p["services"].items(), key=lambda kv: -kv[1])[:3]]}
           for name, p in sorted(top_parts.items(), key=lambda kv: -kv[1]["cost"])[:TOP]]
    budgets, budget_note = _budgets(client, scope)
    read_at = min(x for x in (entry.get("recent_read"), entry.get("parts_read")) if x) if entry.get("recent_read") else ""
    return {"scope": scope, "currency": entry.get("currency") or "", **month_figures(daily, today, booked),
            "trend": _last_days(_usage(daily, booked), today), "services": _ranked(services, 8), "top": top,
            "top_kind": "resource" if group is not None else "resource group", "top_total": len(top_parts),
            "budgets": budgets, "budget_note": budget_note, "queries": queries, "error": None, "read_at": read_at,
            "stored": True, "parts_pending": not entry.get("parts_read"), "read_now": bool(read_now),
            "updating": store.running() and bool(store.needs(sub))}


def for_resource(client, resource_id, today=None, cost_type="actual"):
    """One resource's last 30 days by day and by meter: one query at its resource group, filtered to its ID."""
    today = today or _today()
    parts = resource_id.strip("/").split("/")
    if len(parts) < 8 or parts[2].lower() != "resourcegroups":
        return {"error": "Costs are read per resource inside a resource group.", "queries": []}
    scope = "/" + "/".join(parts[:4])
    body = _body(min(today.replace(day=1), today - timedelta(days=WINDOW_DAYS - 1)), today, "Daily", ("Meter",),
                 {"dimensions": {"name": "ResourceId", "operator": "In", "values": [resource_id.lower()]}}, cost_type)
    queries = [{"label": "This resource's cost by day and meter", "query": json.dumps(body, indent=2)}]
    try:
        rows = query(client, scope, body)
    except ArmError as error:
        return {"error": _denied(error), "required": ROLE if isinstance(error, AccessDenied) else None, "queries": queries}
    days, meters = {}, {}
    for r in rows:
        cost, day = float(r.get("Cost") or 0), _day(r.get("UsageDate"))
        days[day] = days.get(day, 0) + cost
        meters[r.get("Meter") or "(none)"] = meters.get(r.get("Meter") or "(none)", 0) + cost
    month = today.isoformat()[:7]
    trend = _last_days(days, today)
    return {"currency": _currency(rows), "last_30_days": sum(d["cost"] for d in trend),
            "month_to_date": sum(c for d, c in days.items() if d[:7] == month),
            "up_to": max((d for d, c in days.items() if c), default=None), "trend": trend,
            "meters": _ranked(meters, 10), "queries": queries, "error": None, "read_at": _oldest(rows)}


# ---- the whole scope, from the store

def _week_change(days, today):
    """Last 7 days against the 7 before, ending two days ago (the latest days are still filling in)."""
    end = today - timedelta(days=2)
    recent = sum(days.get((end - timedelta(days=i)).isoformat(), 0) for i in range(7))
    before = sum(days.get((end - timedelta(days=i)).isoformat(), 0) for i in range(7, 14))
    return recent, before


def _spike(days, today, floor, factor=SPIKE_FACTOR):
    """The day two days ago against the median of the 7 before it, when it's at least SPIKE_FACTOR times that and
    above `floor`; None otherwise."""
    day = today - timedelta(days=2)
    cost = days.get(day.isoformat(), 0)
    usual = statistics.median([days.get((day - timedelta(days=i)).isoformat(), 0) for i in range(1, 8)])
    if cost - usual < max(floor, 1.0) or cost < usual * factor:
        return None
    return {"date": day.isoformat(), "cost": cost, "usual": usual, "times": round(cost / usual, 1) if usual else None}


def _month(entry, today):
    """This month so far, last month, and where this month is heading, from a store entry's daily figures."""
    return month_figures(_daily(entry), today, _daily(entry, kept="booked"))


def budgets_for(client, subscriptions):
    """Every budget set on the subscriptions, one read each in parallel; [{subscription_id, ...budget}], notes."""
    def one(sub):
        return sub, _budgets(client, f"/subscriptions/{sub}")

    found, notes = [], []
    with ThreadPoolExecutor(max_workers=cost_store.PARALLEL) as pool:
        for sub, (items, note) in pool.map(one, sorted({s.lower() for s in subscriptions})):
            found += [{"subscription_id": sub, **b} for b in items]
            if note:
                notes.append({"subscription_id": sub, "note": note})
    return {"budgets": found, "notes": notes}


def _subscription_result(sub, rows, today, spike_factor=SPIKE_FACTOR):
    """One subscription and its groups from rows {UsageDate, ResourceGroupName, Cost, Booked}: this month (whole, and
    against the same days last month), the last 30 days, and the weekly change, spikes and trend on usage alone
    (Booked, a purchase or refund, isn't usage: a monthly reservation payment would read as a spike, then a drop)."""
    month = today.isoformat()[:7]
    totals, groups, paid, group_paid = {}, {}, {}, {}
    for r in rows:
        day, cost, booked = _day(r.get("UsageDate")), float(r.get("Cost") or 0), float(r.get("Booked") or 0)
        name = (r.get("ResourceGroupName") or "").lower()
        totals[day] = totals.get(day, 0) + cost
        groups.setdefault(name, {})
        groups[name][day] = groups[name].get(day, 0) + cost
        if booked:
            paid[day] = paid.get(day, 0) + booked
            group_paid.setdefault(name, {})
            group_paid[name][day] = group_paid[name].get(day, 0) + booked
    cutoff = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    usage_all = {d: c - paid.get(d, 0) for d, c in totals.items()}
    usual_day = statistics.median([c for d, c in usage_all.items() if d >= cutoff] or [0])
    up_to = max((d for d, c in totals.items() if c and d[:7] == month), default=None)
    through = int(up_to[8:10]) if up_to else 0
    last = (today.replace(day=1) - timedelta(days=1)).isoformat()[:7]

    def figures(days, booked):
        usage = {d: c - booked.get(d, 0) for d, c in days.items()}
        recent, before = _week_change(usage, today)
        return {"month_to_date": sum(c for d, c in days.items() if d[:7] == month),
                "last_month_same_days": sum(c for d, c in days.items() if d[:7] == last and int(d[8:10]) <= through),
                "last_month": sum(c for d, c in days.items() if d[:7] == last),
                "booked": sum(c for d, c in booked.items() if d[:7] == month),
                "booked_days": sorted(d for d, c in booked.items() if d[:7] == month and c),
                "last_30_days": sum(c for d, c in days.items() if d >= cutoff),
                "last_7_days": recent, "previous_7_days": before, "change": recent - before,
                "trend": [round(usage.get((today - timedelta(days=i)).isoformat(), 0), 2) for i in range(WINDOW_DAYS - 1, -1, -1)]}

    out_groups = []
    for name, days in groups.items():
        booked = group_paid.get(name, {})
        row = {"subscription_id": sub, "name": name or SUBSCRIPTION_LEVEL, "grouped": bool(name), **figures(days, booked),
               "spike": _spike({d: c - booked.get(d, 0) for d, c in days.items()}, today, SPIKE_MIN_SHARE * usual_day, spike_factor) if name else None}
        out_groups.append(row)
    return {"subscription_id": sub, **figures(totals, paid), "currency": _currency(rows), "groups": out_groups,
            "read_at": getattr(rows, "read_at", ""), "up_to": up_to,
            "same_days_to": f"{last}-{min(through, (today.replace(day=1) - timedelta(days=1)).day):02d}" if through else None}


def _entry_rows(entry, today):
    """A store entry as rows {UsageDate, ResourceGroupName, Cost, Booked}, since the first of last month (the month
    comparison needs last month's same days)."""
    since = cost_store.window_start(today).isoformat()
    booked = entry.get("booked") or {}
    rows = _Rows({"UsageDate": day, "ResourceGroupName": group, "Cost": cost, "Booked": booked.get(group, {}).get(day, 0),
                  "Currency": entry.get("currency") or ""}
                 for group, days in entry["days"].items() for day, cost in days.items() if day >= since)
    rows.read_at = entry.get("recent_read") or ""
    return rows


def _stored_result(sub, entry, today, spike_factor=SPIKE_FACTOR):
    """One subscription from its store entry. Until the rest of last month is in (the fill reads the last 30 days of
    every subscription first), last month's figures are None, never a misleading 0."""
    result = {**_subscription_result(sub, _entry_rows(entry, today), today, spike_factor), "month": _month(entry, today)}
    if entry.get("from", "9") <= cost_store.window_start(today).isoformat():
        return result
    for row in [result, *result["groups"]]:
        row["last_month_same_days"] = row["last_month"] = None
    result["month"].update(last_month_same_days=None, last_month=None, projected=None, booked_expected=None)
    result["last_month_pending"] = True
    return result


def from_store(store, subscriptions, today=None, since=None, spike_factor=SPIKE_FACTOR):
    """The Cost view's result for `subscriptions`, from what the store holds now. With `since` (a Refresh's start),
    it also counts the subscriptions reread from Azure since then (`refreshed` of `to_refresh`)."""
    today = today or _today()
    subs = sorted({s.lower() for s in subscriptions})
    state = store.progress(subs)
    results = [_stored_result(sub, store.entry(sub), today, spike_factor) for sub in state["read"]]
    groups = [g for r in results for g in r["groups"]]
    currencies = sorted({r["currency"] for r in results if r["currency"]})
    return {"subscriptions": sorted(results, key=lambda r: -r["last_30_days"]),
            "groups": sorted(groups, key=lambda g: -g["last_30_days"]),
            "spikes": sorted((g for g in groups if g["spike"]), key=lambda g: -(g["spike"]["cost"] - g["spike"]["usual"])),
            "currency": currencies[0] if len(currencies) == 1 else "", "currencies": currencies,
            "refused": state["refused"], "failed": state["failed"], "total": len(subs),
            "read": len(state["read"]) + len(state["refused"]) + len(state["failed"]),
            "done": False, "today": today.isoformat(), "window_days": WINDOW_DAYS, "required": ROLE,
            "read_at": min((r["read_at"] for r in results if r["read_at"]), default=""), "stored": True,
            "fill_error": state["error"], "queries": _store_queries(today, getattr(store, "cost_type", "actual")),
            "cost_type": getattr(store, "cost_type", "actual"),
            "refreshing": since is not None,
            "refreshed": sum(1 for s in subs if since and store.read_since(s, since)),
            "to_refresh": len([s for s in subs if not store.refused(s)]),
            "updating": sum(1 for s in subs if store.updating(s))}


def overview(client, subscriptions, store, everything=(), progress=None, publish=None, today=None, refresh=False,
             spike_factor=SPIKE_FACTOR):
    """Every subscription in scope, by resource group, with spikes: asks the store to fill these first (then the
    rest of `everything`, the viewer's subscriptions) and publishes what it holds until each is in, refused or
    failed. Cancelling (Stop) stops the fill too."""
    today = today or _today()
    subs = sorted({s.lower() for s in subscriptions})
    since = store.now_iso() if refresh else None
    store.want(subs, everything=list(everything) or None, client=client, refresh=refresh)
    try:
        while True:
            result = from_store(store, subs, today, since, spike_factor)
            if progress:
                progress(f"Costs: {result['refreshed']} of {result['to_refresh']} reread" if since
                         else f"Costs: {result['read']} of {len(subs)} subscriptions")
            # Done when each subscription has its last month too (the month comparison needs it), unless refused or
            # failed: otherwise the view would say done while "same days last month" is still being read.
            pending = [s for s in subs if not store.refused(s) and not store.failure(s)
                       and (not store.has_recent(s) or cost_store.EARLIER in store.needs(s))]
            unrefreshed = since and result["refreshed"] < result["to_refresh"]
            if (not pending and not unrefreshed) or not store.running():
                return {**from_store(store, subs, today, since, spike_factor), "done": True}
            if publish:
                publish(result)
            time.sleep(WATCH_SECONDS)
    except Cancelled:
        store.stop()
        raise


# ---- one query for a whole scope, where the viewer can read costs at a management group

_CHAINS = ("resourcecontainers | where type =~ 'microsoft.resources/subscriptions'"
           " | project subscriptionId, chain = properties.managementGroupAncestorsChain")


def _common_group(chains):
    """The closest management group every subscription sits under (chains list the closest first), or None."""
    lists = [[(m.get("name") or "").lower() for m in chain or []] for chain in chains]
    if not lists or not all(lists):
        return None
    shared = set(lists[0]).intersection(*map(set, lists[1:]))
    return next((name for name in lists[0] if name in shared), None)


def totals(client, subscriptions, store=None, today=None, cost_type="actual", spike_factor=SPIKE_FACTOR):
    """The scope's costs from the store when it holds every subscription; else in one query at the management group
    holding them all; else None (the page then runs the Cost view's job, which fills the store)."""
    today = today or _today()
    subs = sorted({s.lower() for s in subscriptions})
    if store is not None and all(store.has_recent(s) or store.refused(s) for s in subs):
        return {**from_store(store, subs, today, spike_factor=spike_factor), "done": True}
    try:
        group = _common_group([r.get("chain") for r in client.query(_CHAINS, subscriptions=subs)])
    except ArmError:
        return None
    if not group:
        return None
    body = _body(cost_store.window_start(today), today, "Daily", ("SubscriptionId", "ChargeType"), cost_type=cost_type)
    try:
        rows = query(client, f"/providers/Microsoft.Management/managementGroups/{group}", body)
    except ArmError:
        return None  # refused or not offered for this billing type: the per-subscription read still works
    wanted = set(subs)
    by_sub = {}
    for r in rows:
        sub = (r.get("SubscriptionId") or "").lower()
        if sub in wanted:
            booked = float(r.get("Cost") or 0) if str(r.get("ChargeType") or "").lower() in BOOKED_CHARGES else 0
            by_sub.setdefault(sub, []).append({**r, "ResourceGroupName": "", "Booked": booked})
    results = [{**_subscription_result(sub, by_sub.get(sub, []), today, spike_factor), "groups": []} for sub in subs]
    currencies = sorted({r["currency"] for r in results if r["currency"]})
    return {"subscriptions": sorted(results, key=lambda r: -r["last_30_days"]), "groups": [], "spikes": [],
            "currency": currencies[0] if len(currencies) == 1 else "", "currencies": currencies, "refused": [],
            "failed": [], "total": len(subs), "read": len(subs), "done": True, "today": today.isoformat(),
            "window_days": WINDOW_DAYS, "required": ROLE, "read_at": getattr(rows, "read_at", ""), "group": group,
            "cost_type": cost_type,
            "queries": [{"label": f"Management group {group}: cost by subscription, charge type and day",
                         "query": json.dumps(body, indent=2)}]}


# ---- waste: billed resources doing nothing, from Resource Graph (no Cost Management call)

_WASTE = [
    ("microsoft.compute/disks", "Unattached managed disk",
     "tostring(properties.diskState) =~ 'Unattached'",
     "strcat(tostring(sku.name), ' · ', tostring(properties.diskSizeGB), ' GB')",
     "Delete it, or snapshot it first if it might be needed."),
    ("microsoft.network/publicipaddresses", "Public IP address not attached",
     "isnull(properties.ipConfiguration) and isnull(properties.natGateway)",
     "strcat(tostring(sku.name), ' · ', tostring(properties.publicIPAllocationMethod))",
     "Delete it if nothing will use it (Standard IPs are billed even when idle)."),
    ("microsoft.web/serverfarms", "App Service plan with no apps",
     "toint(properties.numberOfSites) == 0 and tolower(tostring(sku.tier)) !in ('free', 'shared', 'dynamic')",
     "strcat(tostring(sku.name), ' · ', tostring(sku.capacity), ' instance(s)')",
     "Delete the plan, or move an app onto it."),
    ("microsoft.compute/virtualmachines", "VM stopped but not deallocated (compute still billed)",
     "tostring(properties.extended.instanceView.powerState.code) =~ 'PowerState/stopped'",
     "tostring(properties.hardwareProfile.vmSize)",
     "Stop it from the portal or with deallocate, so compute stops being billed."),
    ("microsoft.compute/snapshots", "Snapshot older than 90 days",
     "todatetime(properties.timeCreated) < ago(90d)",
     "strcat(tostring(properties.diskSizeGB), ' GB · taken ', substring(tostring(properties.timeCreated), 0, 10))",
     "Delete it if it's no longer a restore point anyone needs."),
    ("microsoft.network/loadbalancers", "Standard load balancer with no backend pool",
     "tostring(sku.name) =~ 'Standard' and array_length(properties.backendAddressPools) == 0",
     "strcat(tostring(array_length(properties.loadBalancingRules)), ' rule(s)')",
     "Delete it if nothing is behind it (Standard is billed per rule)."),
]


def waste_query(rg_clause=""):
    parts = [f"resources | where type =~ '{kind}' and {where}{rg_clause}"
             f" | project id, name, type, resourceGroup, subscriptionId, location,"
             f" finding = {json.dumps(finding)}, detail = {detail}, action = {json.dumps(action)}"
             for kind, finding, where, detail, action in _WASTE]
    return parts[0] + " | union " + ", ".join(f"({p})" for p in parts[1:])  # Resource Graph takes only the piped form


def waste(client, subscriptions, rg_clause=""):
    """Resources that are billed while doing nothing, by what's wrong with them. One Resource Graph query."""
    kql = waste_query(rg_clause)
    try:
        rows = client.query(kql, subscriptions=subscriptions)
    except ArmError as error:
        return {"rows": [], "error": str(error).splitlines()[0], "queries": [{"label": "Resource Graph", "query": kql}]}
    out = [{"id": r["id"], "name": r["name"], "type": (r.get("type") or "").lower(), "resource_group": r.get("resourceGroup") or "",
            "subscription_id": r.get("subscriptionId") or "", "location": r.get("location") or "",
            "finding": r.get("finding") or "", "detail": r.get("detail") or "", "action": r.get("action") or ""} for r in rows]
    out.sort(key=lambda r: (r["finding"], r["name"].lower()))
    return {"rows": out, "error": None, "checks": [f for _, f, _, _, _ in _WASTE],
            "queries": [{"label": "Resource Graph", "query": kql}]}


def to_doc(result, sub_names=None):
    """The Cost view as a `tables.render` document: subscriptions, then resource groups."""
    names = sub_names or {}
    cur = result.get("currency") or ""
    money = lambda v: f"{v:,.2f}"  # noqa: E731
    money_or = lambda v: "" if v is None else money(v)  # noqa: E731 (last month not read yet)
    subs = [[names.get(s["subscription_id"], s["subscription_id"]), money(s["month_to_date"]), money_or(s.get("last_month")),
             money_or(s.get("last_month_same_days")),
             money(s.get("booked", 0)), money(s["last_30_days"]),
             money(s["last_7_days"]), money(s["previous_7_days"]), money(s["change"])] for s in result["subscriptions"]]
    groups = [[g["name"], names.get(g["subscription_id"], g["subscription_id"]), money(g["month_to_date"]),
               money_or(g.get("last_month")), money_or(g.get("last_month_same_days")), money(g.get("booked", 0)),
               money(g["last_30_days"]), money(g["change"]),
               f"{money(g['spike']['cost'])} on {g['spike']['date']}" if g["spike"] else ""] for g in result["groups"]]
    cols = lambda *c: [f"{x} ({cur})" if cur and x not in ("Subscription", "Resource group", "Spike") else x for x in c]  # noqa: E731
    kind = "Amortized cost (reservations spread over their days)" if result.get("cost_type") == "amortized" else "Actual cost"
    return {"title": "Costs", "subtitle": f"{kind}, {result['window_days']} days to {result['today']}. "
                                          f"Azure's figures for the last day or two are still filling in.",
            "sections": [
                {"heading": "By subscription", "rows": subs,
                 "columns": cols("Subscription", "This month", "Last month", "Same days last month", "Purchases", "Last 30 days",
                                 "Last 7 days (usage)", "7 days before", "Change")},
                {"heading": "By resource group", "rows": groups,
                 "columns": cols("Resource group", "Subscription", "This month", "Last month", "Same days last month", "Purchases",
                                 "Last 30 days", "Change (usage)", "Spike")}]}
