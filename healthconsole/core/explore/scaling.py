"""Can it still scale? Autoscale settings that target a resource, and AKS node pools,
compared with current capacity. "Already at max" is the peak-traffic question."""
import json

from .. import ids
from ..arm import ArmError, kql_list
from ..model import CRITICAL, HEALTHY, INFO, WARNING

NEAR_MAX = 0.8


def _status(current, maximum):
    if current is None or not maximum:
        return INFO
    if current >= maximum:
        return CRITICAL
    if current >= NEAR_MAX * maximum:
        return WARNING
    return HEALTHY


def autoscale_settings(client, target_ids):
    """{target_id: [setting]} for every enabled autoscale setting aimed at these resources."""
    if not target_ids:
        return {}
    rows = client.query(
        "Resources | where type =~ 'microsoft.insights/autoscalesettings'"
        " | extend target = tolower(tostring(properties.targetResourceUri))"
        f" | where target in ({kql_list([t.lower() for t in target_ids])})"
        " | project target, name, enabled=tobool(properties.enabled), profiles=properties.profiles")
    out = {}
    for row in rows:
        out.setdefault(row["target"], []).append(row)
    return out


def _capacity(row):
    capacity = (row.get("sku") or {}).get("capacity")
    return int(capacity) if isinstance(capacity, (int, float)) else None


def items_for(row, settings):
    """Scaling facts for one resource row (with properties and sku). Works for any type."""
    items = []
    current = _capacity(row)
    for setting in settings:
        profiles = setting.get("profiles") or []
        maxima = [int((p.get("capacity") or {}).get("maximum") or 0) for p in profiles]
        minima = [int((p.get("capacity") or {}).get("minimum") or 0) for p in profiles]
        highest = max(maxima) if maxima else None
        enabled = setting.get("enabled") is not False
        status = _status(current, highest) if enabled else WARNING
        detail = ("Autoscale is disabled." if not enabled else
                  f"{len(profiles)} profile(s); highest maximum across profiles {highest}.")
        items.append({"kind": "autoscale", "name": setting.get("name"), "current": current,
                      "min": min(minima) if minima else None, "max": highest,
                      "status": status, "detail": detail})

    props = row.get("properties") or {}
    for pool in props.get("agentPoolProfiles") or []:
        autoscale = bool(pool.get("enableAutoScaling"))
        count, maximum = pool.get("count"), pool.get("maxCount")
        if autoscale:
            status = _status(count, maximum)
            detail = f"{pool.get('vmSize')}, autoscaler {pool.get('minCount')}–{maximum}"
        else:
            # A system pool may legitimately run fixed-size; user pools can't absorb a peak without it.
            status = WARNING if pool.get("mode") != "System" else INFO
            detail = f"{pool.get('vmSize')}, autoscaling disabled ({pool.get('mode')} pool)"
        if (pool.get("powerState") or {}).get("code") == "Stopped":
            status, detail = WARNING, detail + "; pool is stopped"
        items.append({"kind": "node pool", "name": pool.get("name"), "current": count,
                      "min": pool.get("minCount"), "max": maximum, "status": status, "detail": detail})
    return items


def scaling(client, resource_id):
    """Scaling view for one resource; a web app is judged by its App Service plan."""
    target = resource_id.lower()
    errors = []
    try:
        rows = client.query(f"Resources | where tolower(id) == {json.dumps(target)}"
                            " | project id, type=tolower(type), sku, properties")
    except ArmError as error:
        return {"items": [], "errors": [str(error)]}
    if not rows:
        return {"items": [], "errors": ["Resource not readable."]}
    row = rows[0]
    subject = row
    note = ""
    if row["type"] == "microsoft.web/sites":
        plan_id = ((row.get("properties") or {}).get("serverFarmId") or "").lower()
        plans = client.query(f"Resources | where tolower(id) == {json.dumps(plan_id)}"
                             " | project id, type=tolower(type), sku, properties") if plan_id else []
        if plans:
            subject = plans[0]
            note = f"Capacity comes from its App Service plan, {ids.name_of(plan_id)}."
    try:
        settings = autoscale_settings(client, [subject["id"]]).get(subject["id"].lower(), [])
    except ArmError as error:
        settings, errors = [], [f"autoscale: {error}"]
    items = items_for(subject, settings)
    if not items and _capacity(subject) is not None:
        items.append({"kind": "fixed", "name": ids.name_of(subject["id"]), "current": _capacity(subject),
                      "min": None, "max": None, "status": INFO,
                      "detail": "No autoscale setting: capacity is fixed at the SKU's instance count."})
    return {"items": items, "note": note, "errors": errors}
