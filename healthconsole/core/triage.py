"""The "What's wrong now" landing page: one Resource Graph query per card, all in parallel,
scoped to the chosen subscriptions (and resource-group tags). Seconds, even across many subscriptions."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import alert_detail, network, security, status_feed, taxonomy
from .cards import card as build_card, item as build_item
from .arm import ArmError, kql_list, rg_clause
from .model import CRITICAL, INFO, WARNING

MAX_ITEMS = 100  # per card; each reads one more, so a cut card says so
CHANGE_IDS = 500  # problem resources whose changes are read, in one query
NEAR_MAX = 0.8


def _noise_clause(column="type"):
    return f"tolower({column}) !in ({kql_list(taxonomy.NOISE_TYPES)})"


_item = build_item  # kept under this name: Readiness builds on it


def _alerts(client, scope, rg_clause, options):
    crit, warn, hours = options["critical_max_severity"], options["warning_max_severity"], options["alert_hours"]
    rows = client.query(
        "AlertsManagementResources | where type =~ 'microsoft.alertsmanagement/alerts'"
        " | extend e = properties.essentials"
        " | where tostring(e.monitorCondition) == 'Fired' and tostring(e.alertState) != 'Closed'"
        f" | where tobool(e.isStatefulAlert) or todatetime(e.startDateTime) > ago({int(hours)}h)"
        " | extend sev = toint(substring(tostring(e.severity), 3))"
        f" | where sev <= {int(warn)}"
        " | extend id = tostring(e.targetResource), resourceGroup = tostring(e.targetResourceGroup),"
        " type = tostring(e.targetResourceType)"
        f"{rg_clause}"
        " | summarize alerts=count(), worst=min(sev), lastFired=max(tostring(e.startDateTime)),"
        " names=make_set(name, 3) by id, type, resourceGroup, subscriptionId"
        f" | order by worst asc, alerts desc | take {MAX_ITEMS + 1}", subscriptions=scope)
    return [_item(r, CRITICAL if r["worst"] <= crit else WARNING,
                  f"{r['alerts']} alert(s), worst Sev{r['worst']}, latest {alert_detail.when(r['lastFired'])}: {', '.join(r['names'])}")
            for r in rows]


def _failed(client, scope, rg_clause, options):
    rows = client.query(
        "Resources | extend state = tostring(properties.provisioningState)"
        f" | where state in~ ('Failed', 'Canceled') and {_noise_clause()}{rg_clause}"
        f" | project id, name, type, kind, resourceGroup, subscriptionId, state | take {MAX_ITEMS + 1}", subscriptions=scope)
    return [_item(r, CRITICAL, f"provisioningState {r['state']}") for r in rows]


def _aks(client, scope, rg_clause, options):
    rows = client.query(
        "Resources | where type =~ 'microsoft.containerservice/managedclusters'"
        f"{rg_clause}"
        " | extend power = tostring(properties.powerState.code), state = tostring(properties.provisioningState)"
        " | mv-expand pool = properties.agentPoolProfiles"
        " | extend atMax = tobool(pool.enableAutoScaling) and toint(pool['count']) >= toint(pool.maxCount)"
        " | summarize poolsAtMax=make_set_if(strcat(tostring(pool.name), ' ', tostring(pool['count']), '/',"
        " tostring(pool.maxCount)), atMax) by id, name, type, resourceGroup, subscriptionId, power, state"
        " | where power != 'Running' or state != 'Succeeded' or array_length(poolsAtMax) > 0"
        f" | take {MAX_ITEMS + 1}", subscriptions=scope)
    items = []
    for r in rows:
        problems = []
        if r["power"] != "Running":
            problems.append(f"power {r['power'] or 'unknown'}")
        if r["state"] != "Succeeded":
            problems.append(f"provisioning {r['state']}")
        if r["poolsAtMax"]:
            problems.append("node pool(s) at max: " + ", ".join(r["poolsAtMax"]))
        status = CRITICAL if r["state"] == "Failed" or r["poolsAtMax"] else WARNING
        items.append(_item(r, status, "; ".join(problems)))
    return items


def _autoscale(client, scope, rg_clause, options):
    rows = client.query(
        "Resources | where type =~ 'microsoft.insights/autoscalesettings' and tobool(properties.enabled)"
        " | extend target = tolower(tostring(properties.targetResourceUri))"
        " | mv-expand p = properties.profiles"
        " | summarize maxCap = max(toint(p.capacity.maximum)) by target, setting=name"
        " | join kind=inner (Resources | project target=tolower(id), id, name, type, kind, resourceGroup,"
        " subscriptionId, cap=toint(sku.capacity)) on target"
        f" | where isnotnull(cap) and maxCap > 0 and cap >= {float(options.get('capacity_near_max_percent', 100 * NEAR_MAX)) / 100} * maxCap{rg_clause}"
        f" | project id, name, type, kind, resourceGroup, subscriptionId, cap, maxCap, setting | take {MAX_ITEMS + 1}",
        subscriptions=scope)
    return [_item(r, CRITICAL if r["cap"] >= r["maxCap"] else WARNING,
                  f"{r['cap']} of max {r['maxCap']} instances (autoscale '{r['setting']}')") for r in rows]


def _private_endpoints(client, scope, rg_clause, options):
    rows = client.query(
        "Resources | where isnotnull(properties.privateEndpointConnections)"
        f"{rg_clause}"
        " | mv-expand c = properties.privateEndpointConnections"
        " | extend state = tostring(c.properties.privateLinkServiceConnectionState.status)"
        " | where state in~ ('Pending', 'Rejected', 'Disconnected')"
        " | summarize states=make_set(state), endpoints=count() by id, name, type, kind, resourceGroup, subscriptionId"
        f" | take {MAX_ITEMS + 1}", subscriptions=scope)
    return [_item(r, CRITICAL if any(s.lower() in ("rejected", "disconnected") for s in r["states"]) else WARNING,
                  f"{r['endpoints']} private endpoint connection(s): {', '.join(r['states'])}") for r in rows]


def _subnets(client, scope, rg_clause, options):
    """Subnets running out of IPs: an AKS scale-out or a new private endpoint fails when one is full."""
    warn, crit = options.get("subnet_warning_percent", 80), options.get("subnet_critical_percent", 90)
    rows = client.query(network._SUBNETS.format(clause=rg_clause), subscriptions=scope)
    items = []
    for row in rows:
        subnet = network._subnet(row, warn, crit, counts_reported=False)
        if subnet["status"] in (CRITICAL, WARNING):
            items.append({"id": row["vnetId"], "name": f"{row['vnet']} / {subnet['name']}", "display_type": "Subnet",
                          "resource_group": row["resourceGroup"], "subscription_id": row["subscriptionId"].lower(),
                          "status": subnet["status"], "percent": subnet["percent"],
                          "detail": f"{subnet['used']} of {subnet['usable']} usable IPs in use ({subnet['percent']}%), "
                                    f"{', '.join(subnet['cidrs'])}"})
    items.sort(key=lambda i: -i["percent"])
    return items[:MAX_ITEMS + 1]


def _changes(client, scope, ids, options):
    """{resource ID: what changed on it lately}, for the resources the cards list as problems only: a change is
    worth seeing next to something that's wrong, not as a list of every deploy."""
    system = "" if options["include_system_changes"] else " | where changedByType != 'System'"
    rows = client.query(
        "resourcechanges"
        " | extend at = todatetime(properties.changeAttributes.timestamp),"
        " id = tostring(properties.targetResourceId), type = tostring(properties.targetResourceType),"
        " changedBy = tostring(properties.changeAttributes.changedBy),"
        " changedByType = tostring(properties.changeAttributes.changedByType),"
        " change = tostring(properties.changeType)"
        f" | where at > ago({int(options['changes_hours'])}h) and {_noise_clause()}{system}"
        f" | where tolower(id) in ({kql_list([i.lower() for i in ids])})"
        " | summarize changes=count(), lastChange=max(at), who=make_set(changedBy, 3), kinds=make_set(change, 3)"
        " by id = tolower(id)", subscriptions=scope)
    return {r["id"]: {"at": str(r["lastChange"]), "text": f"Changed {r['changes']}× ({', '.join(r['kinds'])}) in the last "
                                                          f"{int(options['changes_hours'])} h, latest by "
                                                          f"{', '.join(w for w in r['who'] if w) or 'unknown'}"}
            for r in rows}


TICKS_AT_EPOCH = 621355968000000000  # Service Health keeps its times as .NET ticks (100 ns since year 1)
RESOLVED_HOURS = 24
# Service Health portal pages, by the kind of event (portalUrl on the page adds the tenant).
HEALTH_PAGES = {"ServiceIssue": "serviceIssues", "PlannedMaintenance": "plannedMaintenance",
                "HealthAdvisory": "healthAdvisories", "SecurityAdvisory": "securityAdvisories", "resolved": "history"}
EVENT_LABELS = {"ServiceIssue": "Service issue", "PlannedMaintenance": "Planned maintenance",
                "SecurityAdvisory": "Security advisory", "HealthAdvisory": "Health advisory"}


def _ticks_iso(ticks):
    if not ticks:
        return ""
    return datetime.fromtimestamp((int(ticks) - TICKS_AT_EPOCH) / 1e7, timezone.utc).isoformat(timespec="seconds")


def _where(row):
    impacts = []
    for text in row.get("impacts") or []:
        try:
            impacts += json.loads(text or "[]")
        except ValueError:
            pass
    services = sorted({i.get("ImpactedService") for i in impacts if i.get("ImpactedService")})
    regions = sorted({g.get("ImpactedRegion") for i in impacts for g in i.get("ImpactedRegions") or []
                      if g.get("ImpactedRegion")})
    parts = [", ".join(services), ", ".join(regions[:6]) or "all regions", f"{row['subscriptions']} subscription(s)",
             f"{row['events']} events" if row.get("events", 1) > 1 else ""]
    return "; ".join(p for p in parts if p)


def _event(name, label, status, phase, detail, link):
    """`phase` is what alert rules read: only "active" items raise "A new Service Health event"."""
    return {"id": "", "name": name, "display_type": label, "resource_group": "", "subscription_id": "",
            "status": status, "detail": detail, "phase": phase, "link": link}


def _service_health(client, scope, rg_clause, options):
    """Azure-wide outages from the public status feed, then your subscriptions' Service Health: incidents,
    maintenance, security advisories, incidents resolved in the last day, and one line for the health advisories."""
    rows = client.query(
        "ServiceHealthResources | where type =~ 'microsoft.resourcehealth/events'"
        " | extend status = tostring(properties.Status), eventType = tostring(properties.EventType),"
        " updated = tolong(properties.LastUpdateTime)"
        " | where (status =~ 'Active' and eventType in ('ServiceIssue', 'PlannedMaintenance', 'SecurityAdvisory',"
        " 'HealthAdvisory'))"
        f" or (status =~ 'Resolved' and eventType =~ 'ServiceIssue' and updated > tolong(ago({int(options.get('resolved_hours', RESOLVED_HOURS))}h)))"
        " | extend phase = iff(status =~ 'Active', 'active', 'resolved')"
        " | summarize subscriptions=dcount(subscriptionId), events=dcount(tostring(properties.TrackingId)),"
        " impacts=make_set(tostring(properties.Impact), 20), updated=max(updated)"
        " by title=tostring(properties.Title), eventType, phase", subscriptions=scope)
    incidents, maintenance, resolved, security, advisories = [], [], [], [], []
    for r in rows:
        kind, phase = r["eventType"], r.get("phase") or "active"
        label = EVENT_LABELS.get(kind, kind)
        if phase == "resolved":
            resolved.append((r.get("updated") or 0, _event(
                r["title"], f"{label}, resolved", INFO, "resolved",
                f"Resolved, last update {alert_detail.when(_ticks_iso(r.get('updated')))}; {_where(r)}",
                {"health": HEALTH_PAGES["resolved"]})))
        elif kind == "HealthAdvisory":
            advisories.append(r)
        else:
            item = _event(r["title"], label, WARNING if kind == "ServiceIssue" else INFO,
                          "active" if kind in ("ServiceIssue", "PlannedMaintenance") else "advisory", _where(r),
                          {"health": HEALTH_PAGES.get(kind, "serviceIssues")})
            (incidents if kind == "ServiceIssue" else maintenance if kind == "PlannedMaintenance" else security).append(item)
    items = incidents + maintenance + [i for _, i in sorted(resolved, key=lambda p: p[0], reverse=True)] + security
    if advisories:
        widest = sorted(advisories, key=lambda r: (-r["subscriptions"], r["title"]))
        items.append(_event(f"{len(advisories)} health advisories", "Health advisories", INFO, "advisory",
                            "Retirements and action required; widest: "
                            + " · ".join(r["title"] for r in widest[:3]), {"health": HEALTH_PAGES["HealthAdvisory"]}))
    if not options.get("azure_status_feed", True):
        return items
    feed = status_feed.read()
    outages = [_event(f["title"], "Azure status (public)", WARNING, "active",
                      f"Posted {alert_detail.when(f['published'])}" + (f": {f['text']}" if f["text"] else ""),
                      {"url": f["link"]}) for f in feed["items"]]
    if feed["error"]:
        note = (f"The public Azure status feed wasn't read: {feed['error']} Turn it off with triage.azure_status_feed "
                "in Settings.")
    else:
        posted = f"{len(outages)} Azure-wide outage(s) posted, listed first" if outages else "no Azure-wide outage posted"
        note = f"Public Azure status page: {posted} (read {alert_detail.when(feed['read_at'])})."
    return outages + items, note


def _public_access(client, scope, rg_clause, options):
    """Security's exposure warnings, one item per resource; information, since it's a setting to review, not an
    outage. A read that fails raises, so the card says so instead of looking clear."""
    found, errors = security.exposure_warnings(client, scope, rg_clause, options.get("risky_ports"))
    if errors and not found:
        raise ArmError("; ".join(errors))
    return [build_item({"id": r["id"], "name": r["name"], "type": r["type"], "resourceGroup": r["resource_group"],
                        "subscriptionId": r["subscription_id"]}, INFO, "; ".join(r["findings"]),
                       badge={"tone": WARNING, "label": security.EXPOSED})  # Security's warning colour, outside the problem counts
            for r in found][:MAX_ITEMS + 1]


CARDS = [
    ("alerts", "Fired alerts", "Sev0–2 alerts still fired, grouped by resource", _alerts),
    ("service_health", "Azure service status",
     "Azure-wide outages, incidents and maintenance in your subscriptions, advisories, and what was resolved in the last day",
     _service_health),
    ("failed", "Failed deployments", "Resources whose provisioning failed or was cancelled", _failed),
    ("aks", "AKS clusters", "Stopped, failed, or with node pools at their maximum", _aks),
    ("autoscale", "Autoscale near max", "At or near their autoscale maximum", _autoscale),
    ("private_endpoints", "Private endpoints", "Connections pending, rejected or disconnected", _private_endpoints),
    ("public_access", "Exposed to the internet", "Security's exposure warnings: details in Security → Exposure & configuration",
     _public_access),
    ("subnets", "Subnets running low", "Subnets with most of their usable IPs in use (see the Network tab)", _subnets),
]


# Links under a card, to what the console can't read itself: M365 health needs ServiceHealth.Read.All (#117).
CARD_LINKS = {"service_health": [{"label": "Azure status", "url": status_feed.STATUS_PAGE},
                                 {"label": "Microsoft 365 service health", "url": "https://admin.microsoft.com/#/servicehealth"}]}


def triage(client, subscriptions, options, group_keys=None, cards=None):
    """`cards` limits which card keys run (an alert rule needs only the ones its signals read)."""
    clause = rg_clause(group_keys)
    scope = list(subscriptions) or None

    def run(card):
        key, title, description, query = card
        note = None
        try:
            items = query(client, scope, clause, options)
            error = None
        except ArmError as failure:
            items, error = [], str(failure)
        if isinstance(items, tuple):  # (items, a note on a part that wasn't read, without failing the card)
            items, note = items
        if len(items) > MAX_ITEMS:
            items = items[:MAX_ITEMS]
            note = " ".join(filter(None, [note, f"Only the first {MAX_ITEMS} are listed, worst first: there are more. Narrow the scope to see the rest."]))
        return build_card(key, title, description, items, error, note=note, links=CARD_LINKS.get(key, []))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, [c for c in CARDS if cards is None or c[0] in cards]))
    problems = {i["id"].lower() for c in results for i in c["items"] if i.get("id") and i.get("status") in (CRITICAL, WARNING)}
    changes_error = None
    if problems and cards is None:
        try:
            changed = _changes(client, scope, sorted(problems)[:CHANGE_IDS], options)
        except ArmError as failure:
            changed, changes_error = {}, f"Recent changes couldn't be read: {failure}"
        for card in results:
            for item in card["items"]:
                if item.get("id") and item["id"].lower() in changed:
                    item["changed"] = changed[item["id"].lower()]
    return {"cards": results, "subscriptions": len(subscriptions), "changes_error": changes_error}
