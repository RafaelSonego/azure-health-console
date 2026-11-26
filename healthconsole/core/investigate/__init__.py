"""Investigate: from a failing operation to its sample requests, one request end to end (through API Management when
it logs to Application Insights), then down to the pod that served it, its logs at that minute and recent changes.
Every read is a query the page shows; nothing runs until asked."""
import json
from datetime import datetime, timedelta, timezone

from ..arm import ArmError, kql_list
from ..ids import RESOURCE_ID_PATTERN
from ..explore import appinsights as ai
from ..explore.appinsights import COMPONENT_ID, COMPONENT_TYPE
from .appinsights import AppInsightsSource
from .source import WINDOWS, InvestigateError, TraceSource, parse_time  # noqa: F401  (the package's interface)

MAX_COMPONENTS = 1000
MAX_CHANGE_TARGETS, MAX_CHANGES, MAX_CHANGE_FIELDS, CHANGE_VALUE_CHARS = 20, 50, 6, 60
STATUS_RANK = {"critical": 0, "warning": 1}


def _apps(client, subscriptions):
    """{component ID (lower): {name: ID} of the Web and Function Apps whose hidden-link tag points at it}."""
    rows = client.query(f"Resources | where type =~ '{ai.SITE_TYPE}' and tostring(tags) contains '{COMPONENT_TYPE}'"
                        " | project id, name, tags", subscriptions=list(subscriptions) or None)
    out = {}
    for row in rows:
        for key, value in (row.get("tags") or {}).items():
            found = (ai.component_in(value) or ai.component_in(key)) if key.lower().startswith("hidden-link") else ""
            if found:
                out.setdefault(found.lower(), {})[row["name"]] = row.get("id") or ""
    return out


MEASURE_BATCH = 25  # components per /api/investigate/measure call


def measure(client, rows, rules=None):
    """{component ID: its last hour against the same hour last week, 24 h trend and 5xx split}: one metrics GET each
    for the counts and one for the split. `unread` when its metrics couldn't be read, `split_error` for the split."""
    counted = {row["id"]: (counts, error) for row, counts, error in ai.last_hour(client, rows)}
    split = ai.server_errors(client, [row for row in rows if counted[row["id"]][0] is not None])
    out = {}
    for row in rows:
        counts, error = counted[row["id"]]
        fives, split_error, codes = split.get(row["id"], (None, None, {}))
        item = {"measured": counts is not None, "unread": bool(error), "status": None, "server_errors": fives,
                "codes": codes, "split_error": bool(split_error), "trend": []}
        if counts is not None:
            item.update(ai.summary(counts, rules))
        out[row["id"]] = item
    return out


def components(client, subscriptions, rg_clause="", measure_now=True):
    """What can be investigated: the App Insights components in scope, the apps that report to it and, with
    `measure_now`, each one's last hour (see `measure`), worst first. Without it, A–Z and `pending`: the page measures
    them in batches so the first rows show at once."""
    found = client.query(f"Resources | where type =~ '{COMPONENT_TYPE}'"
                         f" | project id, name, resourceGroup, subscriptionId{rg_clause} | order by name asc",
                         subscriptions=list(subscriptions) or None)
    rows = found[:MAX_COMPONENTS]
    try:
        apps = _apps(client, subscriptions)
    except ArmError:
        apps = {}
    measured = measure(client, rows) if measure_now else {}
    out = []
    for row in rows:
        linked = apps.get(row["id"].lower(), {})
        item = {"id": row["id"], "name": row["name"], "resource_group": row.get("resourceGroup") or "",
                "subscription_id": (row.get("subscriptionId") or "").lower(), "apps": sorted(linked),
                "app_ids": [linked[name] for name in sorted(linked) if linked[name]],
                "measured": False, "unread": False, "pending": not measure_now, "status": None,
                "server_errors": None, "codes": {}, "trend": []}
        item.update(measured.get(row["id"], {}))
        out.append(item)
    notes = [f"Listed the first {MAX_COMPONENTS} of {len(found)} components (A–Z); narrow the scope for the rest."] \
        if len(found) > MAX_COMPONENTS else []
    if not measure_now:
        return {"components": out, "notes": notes, "batch": MEASURE_BATCH}
    scope_5xx = sum(c["server_errors"] or 0 for c in out)
    for c in out:
        c["share"] = round(100.0 * c["server_errors"] / scope_5xx, 1) if scope_5xx and c["server_errors"] else None
    out.sort(key=lambda c: (STATUS_RANK.get(c["status"], 2), -(c["server_errors"] or 0), -(c.get("failed") or 0),
                            c["name"].lower()))
    unread = sum(1 for c in out if c["unread"])
    if unread:
        notes.append(f"{unread} component(s) couldn't be measured (no access to their metrics).")
    unsplit = sum(1 for c in out if c.get("split_error"))
    if unsplit:
        notes.append(f"The 5xx count couldn't be read for {unsplit} component(s); their Server errors column is empty.")
    return {"components": out, "notes": notes}


CHANGES_MAX_HOURS = 14 * 24  # Resource Graph keeps 14 days of change history


def recent_changes(client, resource_ids, window):
    """What changed on these resources (a component, the apps that report to it, its gateways) in the look-back
    window and the hour before: Resource Graph's change history, newest first. One read over the 14 days Azure keeps,
    so an empty window can still say when the last change was (`before`)."""
    hours = WINDOWS.get(window)
    if hours is None:
        raise ValueError(f"window must be one of {list(WINDOWS)}")
    wanted = list(dict.fromkeys(i.lower() for i in resource_ids))[:MAX_CHANGE_TARGETS]
    if not wanted or not all(RESOURCE_ID_PATTERN.fullmatch(i) for i in wanted):
        raise ValueError("id must be one or more Azure resource IDs")
    shown_hours = min(hours + 1, CHANGES_MAX_HOURS)
    query = ("resourcechanges"
             " | extend at = todatetime(properties.changeAttributes.timestamp),"
             " target = tolower(tostring(properties.targetResourceId))"
             f" | where at > ago({CHANGES_MAX_HOURS}h) and target in ({kql_list(wanted)})"
             " | project at, target, change = tostring(properties.changeType),"
             " by = tostring(properties.changeAttributes.changedBy),"
             " client = tostring(properties.changeAttributes.clientType), changes = properties.changes"
             f" | order by at desc | take {MAX_CHANGES}")
    subscriptions = sorted({i.split("/")[2] for i in wanted})
    rows = [_change_row(r) for r in client.query(query, subscriptions=subscriptions)]
    since = datetime.now(timezone.utc) - timedelta(hours=shown_hours)
    inside = [r for r in rows if _when(r["at"]) and _when(r["at"]) >= since]
    before = next((r for r in rows if _when(r["at"]) and _when(r["at"]) < since), None)
    return {"rows": inside, "before": before, "query": query, "hours": shown_hours,
            "capped": hours + 1 > CHANGES_MAX_HOURS}


def _when(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _short(value):
    text = value if isinstance(value, str) else json.dumps(value) if value is not None else ""
    return text if len(text) <= CHANGE_VALUE_CHARS else text[:CHANGE_VALUE_CHARS - 1] + "…"


def _change_row(r):
    """One change, with each changed field's value before and after (as Azure recorded them)."""
    changes = r.get("changes") or {}
    fields = [{"name": name, "before": _short((detail or {}).get("previousValue")),
               "after": _short((detail or {}).get("newValue"))}
              for name, detail in sorted(changes.items())[:MAX_CHANGE_FIELDS]]
    return {"at": str(r.get("at") or ""), "name": (r.get("target") or "").rsplit("/", 1)[-1], "id": r.get("target") or "",
            "change": r.get("change") or "", "by": r.get("by") or "", "client": r.get("client") or "",
            "fields": fields, "more_fields": max(len(changes) - MAX_CHANGE_FIELDS, 0)}


def source_for(client, component_id, also=()):
    """The trace source for one component (with any gateway components a request may also be in)."""
    for value in (component_id, *also):
        if not COMPONENT_ID.fullmatch(value or ""):
            raise ValueError("component must be an Application Insights component ID")
    return AppInsightsSource(client, component_id, also)
