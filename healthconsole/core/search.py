"""Find resources and resource groups by name across every subscription the session can see:
one Resource Graph query, so it answers in about a second even across hundreds of subscriptions."""
import json

from . import taxonomy
from .arm import kql_list

MIN_LENGTH = 2
LIMIT = 40


def _scope_clause(subscriptions, groups):
    """A KQL boolean for "in the scope in use": its subscriptions, or its resource groups when it's narrowed."""
    if groups is not None:
        return ("strcat(tolower(subscriptionId), '/', tolower(resourceGroup)) in "
                f"({kql_list([g.lower() for g in groups])})")
    if subscriptions:
        return f"tolower(subscriptionId) in ({kql_list([s.lower() for s in subscriptions])})"
    return None


def search(client, text, limit=LIMIT, subscriptions=None, groups=None, only_scope=False):
    """Every subscription the session can see, ranked: exact name, in the scope in use, name starts with the text,
    shortest. Ranked inside the query, before the cut, so a common word ("api") can't push
    an exact match out. `only_scope` keeps just what's in scope."""
    text = (text or "").strip()
    if len(text) < MIN_LENGTH:
        return []
    needle, lowered_needle = json.dumps(text), json.dumps(text.lower())
    in_scope = _scope_clause(subscriptions, groups)
    rows = client.query(
        f"Resources | where name contains {needle}"
        " | project id, name, type=tolower(type), resourceKind=kind, resourceGroup, subscriptionId"
        " | union (ResourceContainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups'"
        f" and name contains {needle}"
        " | project id, name, type=tolower(type), resourceKind='', resourceGroup=name, subscriptionId)"
        f" | extend exact = tolower(name) == {lowered_needle}, prefix = tolower(name) startswith {lowered_needle},"
        f" inScope = {in_scope or 'false'}"
        + (" | where inScope" if only_scope and in_scope else "")
        + f" | order by exact desc, inScope desc, prefix desc | take {limit * 3}")
    results = []
    for row in rows:
        if taxonomy.is_noise(row.get("type")):
            continue
        is_group = row["type"] == "microsoft.resources/subscriptions/resourcegroups"
        name = row.get("name") or ""
        sub = (row.get("subscriptionId") or "").lower()
        scoped = bool(row.get("inScope")) if in_scope else None
        results.append({
            "id": row["id"],
            "name": name,
            "kind": "resource group" if is_group else "resource",
            "display_type": "Resource group" if is_group else taxonomy.display_name(row["type"], row.get("resourceKind")),
            "resource_group": row.get("resourceGroup") or "",
            "subscription_id": sub,
            "in_scope": scoped,
            "_rank": (name.lower() != text.lower(), not scoped, not name.lower().startswith(text.lower()), len(name)),
        })
    results.sort(key=lambda r: r.pop("_rank"))
    return results[:limit]
