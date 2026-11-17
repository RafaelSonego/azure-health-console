"""The pickers: tenants, subscriptions, resource groups and the resources in them."""
import json

from . import exposure, taxonomy
from .arm import kql_list

TENANTS_API_VERSION = "2022-12-01"
SUBSCRIPTIONS_API_VERSION = "2022-12-01"


def list_tenants(client):
    rows = client.get_paged("/tenants", TENANTS_API_VERSION)
    tenants = [{
        "id": row.get("tenantId"),
        "name": row.get("displayName") or row.get("defaultDomain") or row.get("tenantId"),
        "domain": row.get("defaultDomain") or "",
    } for row in rows]
    return sorted(tenants, key=lambda tenant: (tenant["name"] or "").lower())


def list_subscriptions(client):
    rows = client.get_paged("/subscriptions", SUBSCRIPTIONS_API_VERSION)
    subscriptions = [{
        "id": (row.get("subscriptionId") or "").lower(),
        "name": row.get("displayName") or row.get("subscriptionId"),
        "state": row.get("state") or "",
        "tenant_id": row.get("tenantId") or "",
    } for row in rows]
    return sorted(subscriptions, key=lambda sub: (sub["name"] or "").lower())


def list_resource_groups(client, subscription_ids):
    if not subscription_ids:
        return []
    rows = client.query(
        "ResourceContainers"
        " | where type =~ 'microsoft.resources/subscriptions/resourcegroups'"
        " | project id, name, subscriptionId, location, tags",
        subscriptions=subscription_ids,
    )
    groups = [{
        "id": row.get("id"),
        "name": row.get("name"),
        "subscription_id": (row.get("subscriptionId") or "").lower(),
        "location": row.get("location") or "",
        # Azure tag names are case-insensitive, so filters match on the lowercased key.
        "tags": {str(k).lower(): str(v) for k, v in (row.get("tags") or {}).items()},
    } for row in rows]
    return sorted(groups, key=lambda group: (group["name"] or "").lower())


def list_resources(client, groups):
    """`groups` is a list of (subscription_id, resource_group) pairs."""
    if not groups:
        return {"resources": [], "hidden": 0}
    keys = [f"{sub.lower()}/{rg.lower()}" for sub, rg in groups]
    rows = client.query(
        "Resources"
        f" | where strcat(tolower(subscriptionId), '/', tolower(resourceGroup)) in ({kql_list(keys)})"
        " | project id, name, type=tolower(type), kind, location, resourceGroup,"
        f" subscriptionId, provisioningState=tostring(properties.provisioningState), {exposure.COLUMNS}",
        subscriptions=sorted({sub.lower() for sub, _rg in groups}),
    )
    kept = [row for row in rows if not taxonomy.is_noise(row.get("type"))]
    exposure.add_web_restrictions(client, kept, sorted({sub.lower() for sub, _rg in groups}))  # failed: stays "not read"
    resources = sorted((_resource(row) for row in kept), key=lambda r: (r["display_type"].lower(), (r["name"] or "").lower()))
    noise = sorted((_resource(row) for row in rows if taxonomy.is_noise(row.get("type"))),
                   key=lambda r: (r["display_type"].lower(), (r["name"] or "").lower()))
    return {"resources": resources, "hidden": len(noise), "hidden_resources": noise}


def _resource(row):
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "type": row.get("type"),
        "display_type": taxonomy.display_name(row.get("type"), row.get("kind")),
        "location": row.get("location") or "",
        "resource_group": row.get("resourceGroup") or "",
        "subscription_id": (row.get("subscriptionId") or "").lower(),
        "provisioning_state": row.get("provisioningState") or "",
        "exposure": exposure.classify(row),
    }


FIND_MIN = 2
FIND_LIMIT = 500


def find_resources(client, text, subscriptions, groups=None, limit=FIND_LIMIT):
    """Resources in scope whose name or type mentions `text` ("kubernetes" finds AKS clusters): one Resource Graph
    query across the scope, for Browse's filter. {"resources": [...], "truncated": bool}."""
    text = (text or "").strip()
    if len(text) < FIND_MIN or not subscriptions:
        return {"resources": [], "truncated": False}
    needle = json.dumps(text)
    types = taxonomy.types_called(text)
    where = f"name contains {needle} or type contains {needle}" + (f" or type in~ ({kql_list(types)})" if types else "")
    scope = (f" | where strcat(tolower(subscriptionId), '/', tolower(resourceGroup)) in ({kql_list([g.lower() for g in groups])})"
             if groups is not None else "")
    rows = client.query(
        f"Resources{scope} | where ({where}) and type !in~ ({kql_list(taxonomy.NOISE_TYPES)})"  # the limit counts kept ones
        " | project id, name, type=tolower(type), kind, location, resourceGroup,"
        f" subscriptionId, provisioningState=tostring(properties.provisioningState), {exposure.COLUMNS}"
        f" | order by name asc | take {limit + 1}",
        subscriptions=sorted(s.lower() for s in subscriptions),
    )
    kept = [_resource(row) for row in rows[:limit] if not taxonomy.is_noise(row.get("type"))]
    kept.sort(key=lambda r: (r["display_type"].lower(), (r["name"] or "").lower()))
    return {"resources": kept, "truncated": len(rows) > limit}
