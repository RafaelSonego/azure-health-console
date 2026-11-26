"""What happened to a resource lately: alerts (fired and resolved), changes, control-plane
operations and Resource Health history."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..arm import AccessDenied, ArmError
from ..health import RESOURCE_HEALTH_API_VERSION, _parse_severity
from .. import alert_detail, ids

ACTIVITY_LOG_API_VERSION = "2015-04-01"
ACTIVITY_LOG_SLICES = 4
ACTIVITY_LOG_PAGES = 12  # ~2,000 events: routine key reads (API Management logs ~100 a day) mustn't hide real changes
ACTIVITY_LOG_FIELDS = "eventTimestamp,operationName,status,caller,level,correlationId,eventDataId"
FOLD_AT = 3  # identical rows (same operation or alert, caller and outcome) this many times become one, with a count
ALERT_ROWS = 500


HOURS = (6, 24, 72, 168, 336, 720)  # up to 30 days: alerts keep 30, the Activity Log 90
CHANGES_MAX_HOURS = 14 * 24  # Resource Graph keeps 14 days of change history


def activity(client, resource_id, hours=24):
    out = {"alerts": [], "changes": [], "operations": [], "health_history": [], "errors": []}
    target = json.dumps(resource_id.lower())

    try:
        rows = client.query(
            "AlertsManagementResources | where type =~ 'microsoft.alertsmanagement/alerts'"
            " | extend e = properties.essentials"
            f" | where tolower(tostring(e.targetResource)) == {target}"
            f" | where todatetime(e.startDateTime) > ago({int(hours)}h) or tostring(e.monitorCondition) == 'Fired'"
            " | project name, severity=tostring(e.severity), condition=tostring(e.monitorCondition),"
            " state=tostring(e.alertState), started=tostring(e.startDateTime),"
            " resolved=tostring(e.monitorConditionResolvedDateTime), service=tostring(e.monitorService),"
            + alert_detail.FIELDS +
            f" | order by started desc | take {ALERT_ROWS}")
        out["alerts"] = fold_alerts([{**row, "severity": _parse_severity(row.get("severity")),
                                      "what": alert_detail.condition(row.get("criterion"), row.get("window") or ""),
                                      "links": alert_detail.links(row)} for row in rows])
    except ArmError as error:
        out["errors"].append(f"alerts: {error}")

    try:
        out["changes"] = client.query(
            "resourcechanges"
            f" | where tolower(tostring(properties.targetResourceId)) == {target}"
            f" | extend at = todatetime(properties.changeAttributes.timestamp) | where at > ago({min(int(hours), CHANGES_MAX_HOURS)}h)"
            " | project at, type=tostring(properties.changeType),"
            " by=tostring(properties.changeAttributes.changedBy),"
            " client=tostring(properties.changeAttributes.clientType), changes=properties.changes"
            " | order by at desc | take 50")
        if hours > CHANGES_MAX_HOURS:
            out["errors"].append("Changes go back 14 days at most: Azure keeps no older change history.")
    except ArmError as error:
        out["errors"].append(f"changes: {error}")

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        events, more = _activity_log(client, resource_id, since)
        out["operations"] = operations(events)
        if more:
            out["errors"].append(f"Activity Log: {len(events)} events were read, the latest of each part of the window; some in between weren't.")
    except AccessDenied as error:
        out["errors"].append(str(error))
    except ArmError as error:
        out["errors"].append(f"Activity Log: {error}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        payload = client.get(f"{resource_id}/providers/Microsoft.ResourceHealth/availabilityStatuses",
                             RESOURCE_HEALTH_API_VERSION, tolerate=(400, 404, 409, 422)) or {}
        for status in payload.get("value", [])[:100]:
            props = status.get("properties") or {}
            if _before(props.get("occuredTime") or props.get("occurredTime") or props.get("reportedTime"), cutoff):
                continue
            out["health_history"].append({
                "at": props.get("occuredTime") or props.get("occurredTime") or props.get("reportedTime"),
                "state": props.get("availabilityState"),
                "summary": props.get("summary"),
                "reason": props.get("reasonType"),
            })
    except ArmError as error:
        out["errors"].append(f"Resource Health history: {error}")
    return out


def _activity_log(client, resource_id, since, now=None):
    """(events, whether pages were left). The window is read as ACTIVITY_LOG_SLICES time slices at once: a busy
    resource's pages come one nextLink after another (~2 s each), so one long read took 20 s."""
    start = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    end = now or datetime.now(timezone.utc)
    step = (end - start) / ACTIVITY_LOG_SLICES
    edges = [(start + step * i).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(ACTIVITY_LOG_SLICES)] + [None]
    path = f"/subscriptions/{ids.subscription_of(resource_id)}/providers/Microsoft.Insights/eventtypes/management/values"

    def one(i):
        upper = f" and eventTimestamp le '{edges[i + 1]}'" if edges[i + 1] else ""
        return client.get_pages(path, ACTIVITY_LOG_API_VERSION, params={
            "$filter": f"eventTimestamp ge '{edges[i]}'{upper} and resourceUri eq '{resource_id}'",
            "$select": ACTIVITY_LOG_FIELDS}, max_pages=max(1, ACTIVITY_LOG_PAGES // ACTIVITY_LOG_SLICES))

    with ThreadPoolExecutor(max_workers=ACTIVITY_LOG_SLICES) as pool:
        parts = list(pool.map(one, range(ACTIVITY_LOG_SLICES)))
    events = {}
    for found, _more in parts:
        for event in found:
            events.setdefault(event.get("eventDataId") or id(event), event)  # a slice edge can repeat one
    ordered = sorted(events.values(), key=lambda e: e.get("eventTimestamp") or "", reverse=True)
    return ordered, any(more for _found, more in parts)


def fold_alerts(alerts):
    """A stateless alert firing every few minutes becomes one row with how many times, first and latest."""
    groups = {}
    for alert in alerts:
        groups.setdefault((alert.get("name"), alert.get("severity")), []).append(alert)
    out = []
    for rows in groups.values():
        if len(rows) < FOLD_AT:
            out += rows
            continue
        latest = max(rows, key=lambda r: r.get("started") or "")
        out.append({**latest, "count": len(rows), "first": min(r.get("started") or "" for r in rows),
                    "condition": "Fired" if any(r.get("condition") == "Fired" for r in rows) else latest.get("condition")})
    return sorted(out, key=lambda r: r.get("started") or "", reverse=True)


def _before(text, cutoff):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")) < cutoff
    except ValueError:
        return False


def operations(events, limit=100):
    """One row per operation: Azure logs Started, Accepted and Succeeded as separate events with one correlation ID."""
    by_operation = {}
    for event in events:
        key = (event.get("correlationId") or event.get("eventDataId") or id(event),
               (event.get("operationName") or {}).get("value"))
        row = by_operation.get(key)
        at = event.get("eventTimestamp")
        if row is None:
            row = by_operation[key] = {"at": at, "operation": (event.get("operationName") or {}).get("localizedValue"),
                                       "status": None, "caller": event.get("caller"), "level": event.get("level"),
                                       "_last": ""}
        row["at"] = min(filter(None, [row["at"], at]), default=None)
        if (at or "") >= row["_last"]:
            row["_last"] = at or ""
            row["status"] = (event.get("status") or {}).get("localizedValue")
            row["level"] = event.get("level") or row["level"]
    folded = {}
    for row in by_operation.values():
        row.pop("_last")
        folded.setdefault((row["operation"], row["status"], row["caller"]), []).append(row)
    rows = []
    for same in folded.values():
        if len(same) < FOLD_AT:
            rows += same
            continue
        same.sort(key=lambda r: r["at"] or "")
        rows.append({**same[-1], "count": len(same), "first": same[0]["at"]})
    return sorted(rows, key=lambda r: r["at"] or "", reverse=True)[:limit]
