"""Azure Advisor recommendations and Defender for Cloud findings for one resource and the resources inside it."""
import json

from ..arm import ArmError

_IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}
# Advisor's reference list per category, for a recommendation that brings no link of its own (each page opened first).
ADVISOR_GUIDES = {
    "HighAvailability": "https://learn.microsoft.com/azure/advisor/advisor-reference-reliability-recommendations",
    "Cost": "https://learn.microsoft.com/azure/advisor/advisor-reference-cost-recommendations",
    "Performance": "https://learn.microsoft.com/azure/advisor/advisor-reference-performance-recommendations",
    "OperationalExcellence": "https://learn.microsoft.com/azure/advisor/advisor-reference-operational-excellence-recommendations",
    "Security": "https://learn.microsoft.com/azure/defender-for-cloud/review-security-recommendations",
}
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
                             " problem = coalesce(tostring(properties.shortDescription.problem), tostring(properties.label)),"
                             " solution = coalesce(tostring(properties.shortDescription.solution), tostring(properties.description)),"
                             " link = tostring(properties.learnMoreLink)",
                     target, ("category", "impact", "problem", "solution", "link"))
        for r in rows:
            if r.get("solution") == r.get("problem"):
                r["solution"] = ""  # Advisor often repeats the problem as its solution
            r["guide"] = "" if r.get("link") else ADVISOR_GUIDES.get(r.get("category") or "", "")
        out["advisor"] = sorted(rows, key=lambda r: (r["inside"], _IMPACT_ORDER.get(r.get("impact"), 3), -r["count"],
                                                     r.get("category") or ""))
    except ArmError as error:
        out["errors"].append(f"Advisor: {error}")
    try:
        rows = _rows(client, "securityresources | where type =~ 'microsoft.security/assessments'"
                             " | where tostring(properties.status.code) == 'Unhealthy'"
                             " | extend rid = tolower(tostring(properties.resourceDetails.Id)),"
                             " name = tostring(properties.displayName), severity = tostring(properties.metadata.severity),"
                             " category = tostring(properties.metadata.categories[0]),"
                             " link = tostring(properties.links.azurePortal)",
                     target, ("name", "severity", "category", "link"))
        out["security"] = sorted(rows, key=lambda r: (r["inside"], _IMPACT_ORDER.get(r.get("severity"), 3), -r["count"],
                                                      r.get("name") or ""))
    except ArmError as error:
        out["errors"].append(f"Defender for Cloud: {error}")
    return out
