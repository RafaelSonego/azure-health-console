"""Type-specific facts worth seeing in an incident. Generic rules run for every type;
a handler per type adds what only that type has. Unknown types still get the generic part."""
import contextvars
import json
from datetime import datetime, timezone

from .. import ids
from ..arm import AccessDenied, ArmError
from ..model import CRITICAL, HEALTHY, WARNING
from . import keyvault

CERT_WARN_DAYS = 30
CERT_CRIT_DAYS = 7


def fact(label, value, status=None):
    return {"label": label, "value": "–" if value in (None, "") else value, "status": status}


_errors = contextvars.ContextVar("details_errors", default=None)


def _get(client, path, version, **kwargs):
    """Missing child data shouldn't sink the tab; a 403 is reported with the role it needs."""
    try:
        return client.get(path, version, tolerate=(400, 404, 409), **kwargs) or {}
    except AccessDenied as error:
        errors = _errors.get()
        if errors is not None and str(error) not in errors:
            errors.append(str(error))
        return {}
    except ArmError:
        return {}


def _row(client, resource_id):
    rows = client.query(f"Resources | where tolower(id) == {json.dumps(resource_id.lower())}"
                        " | project id, name, type=tolower(type), kind, location, sku, zones,"
                        " properties, identity, tags")
    return rows[0] if rows else None


def _days_left(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - datetime.now(timezone.utc)).days


def _expiry_fact(label, value):
    days = _days_left(value)
    if days is None:
        return fact(label, value)
    status = CRITICAL if days < CERT_CRIT_DAYS else WARNING if days < CERT_WARN_DAYS else HEALTHY
    return fact(label, f"{str(value)[:10]} ({days} days)", status)


# ------------------------------------------------------------------ generic


def _generic(row):
    props = row.get("properties") or {}
    sku = row.get("sku") or {}
    facts = []
    if sku:
        facts.append(fact("SKU", " / ".join(str(sku[k]) for k in ("name", "tier", "capacity") if sku.get(k) is not None)))
    if row.get("kind"):
        facts.append(fact("Kind", row["kind"]))
    facts.append(fact("Location", row.get("location")))
    if row.get("zones"):
        facts.append(fact("Zones", ", ".join(row["zones"])))
    for key, label in (("zoneRedundant", "Zone redundant"), ("publicNetworkAccess", "Public network access"),
                       ("minimumTlsVersion", "Minimum TLS"), ("minTlsVersion", "Minimum TLS")):
        if key in props:
            facts.append(fact(label, props[key]))
    sections = [{"title": "Overview", "facts": facts}]

    connections = []
    for connection in props.get("privateEndpointConnections") or []:
        cprops = connection.get("properties") or {}
        state = (cprops.get("privateLinkServiceConnectionState") or {}).get("status")
        endpoint = (cprops.get("privateEndpoint") or {}).get("id")
        status = HEALTHY if state == "Approved" else WARNING if state == "Pending" else CRITICAL
        connections.append(fact(ids.name_of(endpoint) or connection.get("name"), state, status))
    if connections:
        sections.append({"title": "Private endpoint connections", "facts": connections})
    return sections


# ------------------------------------------------------------- per type


def _sites(client, row):
    props = row.get("properties") or {}
    state = props.get("state")
    facts = [
        fact("State", state, HEALTHY if state == "Running" else WARNING),
        fact("Availability", props.get("availabilityState"),
             HEALTHY if props.get("availabilityState") == "Normal" else WARNING),
        fact("Usage state", props.get("usageState"),
             CRITICAL if props.get("usageState") == "Exceeded" else None),
        fact("Default host", props.get("defaultHostName")),
        fact("HTTPS only", props.get("httpsOnly")),
    ]
    config = (_get(client, f"{row['id']}/config/web", client.api_version(row["id"])).get("properties") or {})
    if config:
        always_on = config.get("alwaysOn")
        health_path = config.get("healthCheckPath")
        facts += [
            fact("Runtime", config.get("linuxFxVersion") or config.get("windowsFxVersion")
                 or config.get("netFrameworkVersion")),
            fact("Always On", always_on, WARNING if always_on is False and "functionapp" not in (row.get("kind") or "") else None),
            fact("Health check path", health_path or "not set", None if health_path else WARNING),
            fact("Pre-warmed instances", config.get("preWarmedInstanceCount")),
            fact("Minimum elastic instances", config.get("minimumElasticInstanceCount")),
        ]
    sections = [{"title": "App", "facts": facts}]

    version = client.api_version(row["id"])
    slots = _get(client, f"{row['id']}/slots", version).get("value") or []
    if slots:
        sections.append({"title": "Deployment slots", "facts": [
            fact(s.get("name", "").split("/")[-1], (s.get("properties") or {}).get("state")) for s in slots]})
    if "functionapp" in (row.get("kind") or ""):
        functions = _get(client, f"{row['id']}/functions", version).get("value") or []
        disabled = [f for f in functions if (f.get("properties") or {}).get("isDisabled")]
        sections.append({"title": f"Functions ({len(functions)})", "facts": [
            fact(f.get("name", "").split("/")[-1],
                 "disabled" if (f.get("properties") or {}).get("isDisabled") else "enabled",
                 WARNING if f in disabled else None) for f in functions[:40]]})
    return sections


def _plan(client, row):
    props = row.get("properties") or {}
    return [{"title": "App Service plan", "facts": [
        fact("Status", props.get("status"), HEALTHY if props.get("status") == "Ready" else WARNING),
        fact("Apps on the plan", props.get("numberOfSites")),
        fact("Instances", (row.get("sku") or {}).get("capacity")),
        fact("Maximum instances", props.get("maximumNumberOfWorkers")),
        fact("Elastic scale", props.get("elasticScaleEnabled")),
    ]}]


def _sql_db(client, row):
    props = row.get("properties") or {}
    version = client.api_version(row["id"])
    status = props.get("status")
    facts = [
        fact("Status", status, HEALTHY if status == "Online" else WARNING),
        fact("Service objective", props.get("currentServiceObjectiveName")),
        fact("Elastic pool", ids.name_of(props.get("elasticPoolId")) if props.get("elasticPoolId") else "none"),
        fact("Read scale", props.get("readScale")),
        fact("Max size", f"{(props.get('maxSizeBytes') or 0) / 2**30:.0f} GiB" if props.get("maxSizeBytes") else None),
    ]
    for usage in _get(client, f"{row['id']}/usages", version).get("value") or []:
        uprops = usage.get("properties") or usage
        current, limit = uprops.get("currentValue"), uprops.get("limit")
        if current is not None and limit:
            ratio = current / limit
            facts.append(fact(uprops.get("displayName") or usage.get("name"),
                              f"{ratio:.0%} of limit",
                              CRITICAL if ratio > 0.95 else WARNING if ratio > 0.85 else HEALTHY))
    tuning = _get(client, f"{row['id']}/automaticTuning/current", version).get("properties") or {}
    if tuning:
        facts.append(fact("Automatic tuning", tuning.get("actualState")))
    sections = [{"title": "Database", "facts": facts}]
    links = _get(client, f"{row['id']}/replicationLinks", version).get("value") or []
    if links:
        sections.append({"title": "Geo-replication", "facts": [
            fact(f"{(link.get('properties') or {}).get('role')} → {(link.get('properties') or {}).get('partnerServer')}",
                 (link.get("properties") or {}).get("replicationState"),
                 HEALTHY if (link.get("properties") or {}).get("replicationState") in ("CATCH_UP", "SEEDING", None) else WARNING)
            for link in links]})
    return sections


def _redis(client, row):
    props = row.get("properties") or {}
    version = client.api_version(row["id"])
    facts = [
        fact("Version", props.get("redisVersion")),
        fact("Shards", props.get("shardCount") or 1),
        fact("Replicas per primary", props.get("replicasPerPrimary") or props.get("replicasPerMaster")),
        fact("Non-TLS port", props.get("enableNonSslPort")),
        fact("Host", props.get("hostName")),
    ]
    sections = [{"title": "Cache", "facts": facts}]
    schedule = (_get(client, f"{row['id']}/patchSchedules/default", version).get("properties") or {})
    entries = schedule.get("scheduleEntries") or []
    sections.append({"title": "Patch schedule", "facts": [
        fact(e.get("dayOfWeek"), f"{e.get('startHourUtc')}:00 UTC for {e.get('maintenanceWindow', 'PT5H')}")
        for e in entries] or [fact("Schedule", "none set: Azure patches at any time", WARNING)]})
    linked = _get(client, f"{row['id']}/linkedServers", version).get("value") or []
    if linked:
        sections.append({"title": "Geo-replication", "facts": [
            fact(ids.name_of((link.get("properties") or {}).get("linkedRedisCacheId")),
                 (link.get("properties") or {}).get("serverRole")) for link in linked]})
    return sections


def _cosmos(client, row):
    props = row.get("properties") or {}
    facts = [
        fact("Consistency", (props.get("consistencyPolicy") or {}).get("defaultConsistencyLevel")),
        fact("Automatic failover", props.get("enableAutomaticFailover")),
        fact("Multi-region writes", props.get("enableMultipleWriteLocations")),
        fact("Backup", (props.get("backupPolicy") or {}).get("type")),
    ]
    regions = [fact(f"{loc.get('locationName')} (priority {loc.get('failoverPriority')})",
                    "zone redundant" if loc.get("isZoneRedundant") else "single zone")
               for loc in sorted(props.get("locations") or [], key=lambda loc: loc.get("failoverPriority", 0))]
    return [{"title": "Account", "facts": facts}, {"title": "Regions", "facts": regions}]


def _storage(client, row):
    props = row.get("properties") or {}
    primary, secondary = props.get("statusOfPrimary"), props.get("statusOfSecondary")
    return [{"title": "Storage", "facts": [
        fact("Primary", primary, HEALTHY if primary == "available" else CRITICAL),
        fact("Secondary", secondary, None if secondary in (None, "available") else WARNING),
        fact("Replication", (row.get("sku") or {}).get("name")),
        fact("Last geo-failover", props.get("lastGeoFailoverTime")),
        fact("Hierarchical namespace", props.get("isHnsEnabled")),
    ]}]


def _messaging(client, row):
    version = client.api_version(row["id"])
    sections = []
    if row["type"] == "microsoft.servicebus/namespaces":
        queues = _get(client, f"{row['id']}/queues", version).get("value") or []
        if queues:
            sections.append({"title": f"Queues ({len(queues)})", "table": {
                "columns": ["Queue", "Status", "Active", "Dead-lettered", "Scheduled"],
                "rows": [[q.get("name"), (q.get("properties") or {}).get("status"),
                          ((q.get("properties") or {}).get("countDetails") or {}).get("activeMessageCount"),
                          ((q.get("properties") or {}).get("countDetails") or {}).get("deadLetterMessageCount"),
                          ((q.get("properties") or {}).get("countDetails") or {}).get("scheduledMessageCount")]
                         for q in sorted(queues, key=_dead_letters)]}})
        topics = _get(client, f"{row['id']}/topics", version).get("value") or []
        if topics:
            sections.append({"title": f"Topics ({len(topics)})", "table": {
                "columns": ["Topic", "Status", "Subscriptions", "Size (MB)"],
                "rows": [[t.get("name"), (t.get("properties") or {}).get("status"),
                          (t.get("properties") or {}).get("subscriptionCount"),
                          round(((t.get("properties") or {}).get("sizeInBytes") or 0) / 2**20, 1)] for t in topics]}})
    else:
        hubs = _get(client, f"{row['id']}/eventhubs", version).get("value") or []
        if hubs:
            sections.append({"title": f"Event hubs ({len(hubs)})", "table": {
                "columns": ["Event hub", "Status", "Partitions", "Retention (days)"],
                "rows": [[h.get("name"), (h.get("properties") or {}).get("status"),
                          (h.get("properties") or {}).get("partitionCount"),
                          (h.get("properties") or {}).get("messageRetentionInDays")] for h in hubs]}})
    return sections


def _key_vault(client, row, warn_days=CERT_WARN_DAYS, critical_days=7):
    props = row.get("properties") or {}
    sections = [{"title": "Vault", "facts": [
        fact("Soft delete", props.get("enableSoftDelete")),
        fact("Purge protection", props.get("enablePurgeProtection")),
        fact("RBAC authorization", props.get("enableRbacAuthorization")),
    ]}]
    result = keyvault.expiry(client, row["id"], warn_days, critical_days, max_pages=keyvault.PAGE_MAX_PAGES)
    for kind in keyvault.KINDS:
        items = result.get(kind) or []
        sections.append({"title": f"{kind.capitalize()} ({len(items)}): expiry, names only",
                         "note": f"Green: fine. Yellow: expires within {warn_days} days. "
                                 f"Red: expired or within {critical_days} days. "
                                 "Grey: no expiry date, or disabled. Values are never read.",
                         "facts": [
            fact(i["name"], f"{i['note']}" + (f" ({i['expires']})" if i["expires"] else ""), i["status"])
            for i in items] or [fact(f"No {kind}", "this vault has none, or none are visible to you")]})
    errors = _errors.get()
    if errors is not None:
        errors.extend(e for e in result["errors"] if e not in errors)
    return sections


def _vm(client, row):
    view = _get(client, f"{row['id']}/instanceView", client.api_version(row["id"]))
    facts = []
    for status in view.get("statuses") or []:
        code = status.get("code") or ""
        if code.startswith(("PowerState/", "ProvisioningState/")):
            facts.append(fact(code.split("/")[0], status.get("displayStatus"),
                              HEALTHY if code in ("PowerState/running", "ProvisioningState/succeeded") else WARNING))
    agent = view.get("vmAgent") or {}
    for status in agent.get("statuses") or []:
        facts.append(fact("VM agent", status.get("displayStatus"),
                          HEALTHY if status.get("displayStatus") == "Ready" else WARNING))
    return [{"title": "Virtual machine", "facts": facts}] if facts else []


def _flexible_server(client, row):
    props = row.get("properties") or {}
    ha = props.get("highAvailability") or {}
    storage = props.get("storage") or {}
    state = props.get("state")
    return [{"title": "Server", "facts": [
        fact("State", state, HEALTHY if state == "Ready" else WARNING),
        fact("Version", props.get("version")),
        fact("High availability", f"{ha.get('mode')} ({ha.get('state')})" if ha.get("mode") else "disabled"),
        fact("Replication role", props.get("replicationRole")),
        fact("Storage", f"{storage.get('storageSizeGB')} GB, autogrow {storage.get('autoGrow')}"),
    ]}]


def _app_gateway(client, row):
    props = row.get("properties") or {}
    autoscale = props.get("autoscaleConfiguration") or {}
    waf = props.get("webApplicationFirewallConfiguration") or {}
    return [{"title": "Application Gateway", "facts": [
        fact("Operational state", props.get("operationalState"),
             HEALTHY if props.get("operationalState") == "Running" else WARNING),
        fact("Autoscale", f"{autoscale.get('minCapacity')}–{autoscale.get('maxCapacity')}" if autoscale else "fixed"),
        fact("Backend pools", len(props.get("backendAddressPools") or [])),
        fact("WAF", f"{waf.get('firewallMode')}" if waf else ("policy" if props.get("firewallPolicy") else "off")),
        fact("Backend health", "see the UnhealthyHostCount metric, split by BackendSettingsPool"),
    ]}]


def _aks(client, row):
    props = row.get("properties") or {}
    return [{"title": "Cluster", "facts": [
        fact("Kubernetes version", props.get("currentKubernetesVersion")),
        fact("Power state", (props.get("powerState") or {}).get("code"),
             HEALTHY if (props.get("powerState") or {}).get("code") == "Running" else WARNING),
        fact("Tier", (row.get("sku") or {}).get("tier")),
        fact("Private cluster", (props.get("apiServerAccessProfile") or {}).get("enablePrivateCluster")),
        fact("Network plugin", (props.get("networkProfile") or {}).get("networkPlugin")),
        fact("Auto-upgrade", (props.get("autoUpgradeProfile") or {}).get("upgradeChannel")),
        fact("Container Insights", (props.get("addonProfiles") or {}).get("omsagent", {}).get("enabled")
             or (props.get("azureMonitorProfile") or {}).get("containerInsights", {}).get("enabled")),
    ]}]


def _certificate(client, row):
    props = row.get("properties") or {}
    return [{"title": "Certificate", "facts": [
        _expiry_fact("Expires", props.get("expirationDate")),
        fact("Subject", props.get("subjectName")),
        fact("Issuer", props.get("issuer")),
    ]}]


HANDLERS = {
    "microsoft.web/sites": _sites,
    "microsoft.web/serverfarms": _plan,
    "microsoft.web/certificates": _certificate,
    "microsoft.sql/servers/databases": _sql_db,
    "microsoft.cache/redis": _redis,
    "microsoft.documentdb/databaseaccounts": _cosmos,
    "microsoft.storage/storageaccounts": _storage,
    "microsoft.servicebus/namespaces": _messaging,
    "microsoft.eventhub/namespaces": _messaging,
    "microsoft.keyvault/vaults": _key_vault,
    "microsoft.compute/virtualmachines": _vm,
    "microsoft.dbforpostgresql/flexibleservers": _flexible_server,
    "microsoft.dbformysql/flexibleservers": _flexible_server,
    "microsoft.network/applicationgateways": _app_gateway,
    "microsoft.containerservice/managedclusters": _aks,
}


def _dead_letters(queue):
    """Sort key: the most dead-lettered queue first."""
    return -(((queue.get("properties") or {}).get("countDetails") or {}).get("deadLetterMessageCount") or 0)


def details(client, resource_id, expiry_warning_days=CERT_WARN_DAYS, expiry_critical_days=7):
    try:
        row = _row(client, resource_id)
    except ArmError as error:
        return {"sections": [], "errors": [str(error)]}
    if not row:
        return {"sections": [], "errors": ["Resource not readable with this session."]}
    sections = _generic(row)
    handler = HANDLERS.get(row["type"])
    errors = []
    token = _errors.set(errors)
    try:
        if row["type"] == "microsoft.keyvault/vaults":
            sections += _key_vault(client, row, expiry_warning_days, expiry_critical_days)
        elif handler:
            sections += handler(client, row)
    except ArmError as error:
        errors.append(str(error))
    finally:
        _errors.reset(token)
    return {"sections": sections, "errors": errors}
