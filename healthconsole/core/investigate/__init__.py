"""Investigate: from a failing operation to its sample requests, one request end to end (through API Management when
it logs to Application Insights), then down to the pod that served it, its logs at that minute and recent changes.
Every read is a query the page shows; nothing runs until asked."""
from ..arm import ArmError, kql_list
from ..ids import RESOURCE_ID_PATTERN
from ..explore import appinsights as ai
from ..explore.appinsights import COMPONENT_ID, COMPONENT_TYPE
from .appinsights import AppInsightsSource
from .source import WINDOWS, InvestigateError, TraceSource, parse_time  # noqa: F401  (the package's interface)

MAX_COMPONENTS = 500
MAX_CHANGE_TARGETS, MAX_CHANGES, MAX_CHANGE_FIELDS = 20, 50, 6
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


def components(client, subscriptions, rg_clause=""):
    """What can be investigated: the App Insights components in scope, each with its last hour (requests, failed,
    exceptions) against the same hour last week, the apps that report to it, and worst first. The numbers are one
    metrics GET per component, for the first ai.MAX_COMPONENTS A–Z."""
    rows = client.query(f"Resources | where type =~ '{COMPONENT_TYPE}'"
                        f" | project id, name, resourceGroup, subscriptionId{rg_clause} | order by name asc",
                        subscriptions=list(subscriptions) or None)[:MAX_COMPONENTS]
    try:
        apps = _apps(client, subscriptions)
    except ArmError:
        apps = {}
    measured = {row["id"]: (counts, error) for row, counts, error in ai.last_hour(client, rows[:ai.MAX_COMPONENTS])}
    split = ai.server_errors(client, [row for row in rows if measured.get(row["id"], (None,))[0] is not None])
    out = []
    for row in rows:
        counts, error = measured.get(row["id"], (None, None))
        linked = apps.get(row["id"].lower(), {})
        item = {"id": row["id"], "name": row["name"], "resource_group": row.get("resourceGroup") or "",
                "subscription_id": (row.get("subscriptionId") or "").lower(), "apps": sorted(linked),
                "app_ids": [linked[name] for name in sorted(linked) if linked[name]],
                "measured": counts is not None, "unread": bool(error), "status": None,
                "server_errors": split.get(row["id"], (None,))[0], "trend": []}
        if counts is not None:
            item.update(ai.summary(counts))
        out.append(item)
    scope_5xx = sum(c["server_errors"] or 0 for c in out)
    for c in out:
        c["share"] = round(100.0 * c["server_errors"] / scope_5xx, 1) if scope_5xx and c["server_errors"] else None
    out.sort(key=lambda c: (STATUS_RANK.get(c["status"], 2), -(c["server_errors"] or 0), -(c.get("failed") or 0),
                            c["name"].lower()))
    notes = []
    if len(rows) > ai.MAX_COMPONENTS:
        notes.append(f"The last hour was read for the first {ai.MAX_COMPONENTS} of {len(rows)} components (A–Z); "
                     "narrow the scope to measure the rest.")
    unread = sum(1 for c in out if c["unread"])
    if unread:
        notes.append(f"{unread} component(s) couldn't be measured (no access to their metrics).")
    unsplit = sum(1 for _, error in split.values() if error)
    if unsplit:
        notes.append(f"The 5xx count couldn't be read for {unsplit} component(s); their Server errors column is empty.")
    return {"components": out, "notes": notes}


def recent_changes(client, resource_ids, window):
    """What changed on these resources (a component, the apps that report to it, its gateways) in the look-back
    window and the hour before: Resource Graph's change history, newest first."""
    hours = WINDOWS.get(window)
    if hours is None:
        raise ValueError(f"window must be one of {list(WINDOWS)}")
    wanted = list(dict.fromkeys(i.lower() for i in resource_ids))[:MAX_CHANGE_TARGETS]
    if not wanted or not all(RESOURCE_ID_PATTERN.fullmatch(i) for i in wanted):
        raise ValueError("id must be one or more Azure resource IDs")
    query = ("resourcechanges"
             " | extend at = todatetime(properties.changeAttributes.timestamp),"
             " target = tolower(tostring(properties.targetResourceId))"
             f" | where at > ago({hours + 1}h) and target in ({kql_list(wanted)})"
             " | project at, target, change = tostring(properties.changeType),"
             " by = tostring(properties.changeAttributes.changedBy),"
             " client = tostring(properties.changeAttributes.clientType), changes = properties.changes"
             f" | order by at desc | take {MAX_CHANGES}")
    subscriptions = sorted({i.split("/")[2] for i in wanted})
    rows = client.query(query, subscriptions=subscriptions)
    return {"rows": [{"at": str(r.get("at") or ""), "name": (r.get("target") or "").rsplit("/", 1)[-1],
                      "id": r.get("target") or "", "change": r.get("change") or "", "by": r.get("by") or "",
                      "client": r.get("client") or "", "fields": sorted(r.get("changes") or {})[:MAX_CHANGE_FIELDS]}
                     for r in rows],
            "query": query, "hours": hours + 1}


def source_for(client, component_id, also=()):
    """The trace source for one component (with any gateway components a request may also be in)."""
    for value in (component_id, *also):
        if not COMPONENT_ID.fullmatch(value or ""):
            raise ValueError("component must be an Application Insights component ID")
    return AppInsightsSource(client, component_id, also)
