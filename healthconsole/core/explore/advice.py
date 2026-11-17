"""Azure Advisor recommendations and Defender for Cloud findings for one resource and the resources inside it."""
import json

from ..arm import ArmError

_IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}
EXAMPLES = 5


def _rows(client, kql, target, keys):
    """Rows on the resource itself, then one row per problem for what's inside it, with a count and a few examples:
    an API Management service can carry the same finding on hundreds of APIs (checked live 2026-11-09)."""
    exact, below = json.dumps(target), json.dumps(target + "/")
    rows = client.query(
        f"{kql} | where rid == {exact} or rid startswith {below}"
        f" | extend own = rid == {exact}, part = substring(rid, {len(target) + 1})"
        f" | summarize n = count(), parts = make_set(part, {EXAMPLES}) by own, {', '.join(keys)}")
    out = []
    for r in rows:
        own = bool(r.pop("own"))
        n, parts = r.pop("n"), [p for p in r.pop("parts") or [] if p]
        out.append({**r, "count": 1 if own else n, "on": "" if own else parts, "inside": not own})
    return out


def recommendations(client, resource_id):
    target = resource_id.lower().rstrip("/")
    out = {"advisor": [], "security": [], "errors": []}
    try:
        rows = _rows(client, "AdvisorResources | where type =~ 'microsoft.advisor/recommendations'"
                             " | extend rid = tolower(tostring(properties.resourceMetadata.resourceId)),"
                             " category = tostring(properties.category), impact = tostring(properties.impact),"
                             " problem = tostring(properties.shortDescription.problem),"
                             " solution = tostring(properties.shortDescription.solution)",
                     target, ("category", "impact", "problem", "solution"))
        out["advisor"] = sorted(rows, key=lambda r: (r["inside"], _IMPACT_ORDER.get(r.get("impact"), 3), -r["count"],
                                                     r.get("category") or ""))
    except ArmError as error:
        out["errors"].append(f"Advisor: {error}")
    try:
        rows = _rows(client, "securityresources | where type =~ 'microsoft.security/assessments'"
                             " | where tostring(properties.status.code) == 'Unhealthy'"
                             " | extend rid = tolower(tostring(properties.resourceDetails.Id)),"
                             " name = tostring(properties.displayName), severity = tostring(properties.metadata.severity),"
                             " category = tostring(properties.metadata.categories[0])",
                     target, ("name", "severity", "category"))
        out["security"] = sorted(rows, key=lambda r: (r["inside"], _IMPACT_ORDER.get(r.get("severity"), 3), -r["count"],
                                                      r.get("name") or ""))
    except ArmError as error:
        out["errors"].append(f"Defender for Cloud: {error}")
    return out
