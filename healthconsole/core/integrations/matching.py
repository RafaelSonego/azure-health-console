"""Matching records from ServiceNow or a monitoring tool to Azure resources in scope, on the laptop: an Azure ID is
an exact match, a distinctive full name a name match, a short or plain name only a possible one."""
import re

from ..cache import TtlCache
from .. import taxonomy
from ..arm import rg_clause

INVENTORY_CACHE_SECONDS = 300
# Names that are only "possible" matches: too short or plain words, so they'd match unrelated text.
STRONG_NAME = re.compile(r"^(?=.{6,})(?=.*[-_.0-9]).+$")
TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]*[a-z0-9]")
AZURE_ID = re.compile(r"/subscriptions/[0-9a-f-]{36}/resourcegroups/[^\s\"'<>]+", re.I)
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_cache = TtlCache("integrations and the scope's inventory", INVENTORY_CACHE_SECONDS)


def cached(key, seconds, build, refresh=False):
    """What ServiceNow, New Relic and the inventory reuse; keys start with what makes them the viewer's own."""
    return _cache.get_or_load(key, build, refresh, ttl=seconds)


def inventory(client, subscriptions, groups=None, refresh=False):
    """Every resource, resource group and subscription in scope: the names records are matched against."""
    def build():
        rows = client.query(
            f"Resources | project id, name, type, resourceGroup, subscriptionId{rg_clause(groups)}"
            " | union (ResourceContainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups'"
            f" | extend resourceGroup = name | project id, name, type, resourceGroup, subscriptionId{rg_clause(groups)})"
            " | union (ResourceContainers | where type =~ 'microsoft.resources/subscriptions'"
            " | extend resourceGroup = '' | project id, name, type, resourceGroup, subscriptionId)",
            subscriptions=list(subscriptions) or None)
        return [r for r in rows if not taxonomy.is_noise((r.get("type") or "").lower())]
    key = ("inventory", client.identity.object_id, tuple(sorted(subscriptions)), tuple(sorted(groups or [])))
    return cached(key, INVENTORY_CACHE_SECONDS, build, refresh)


def name_index(resources):
    index, by_id = {}, {}
    for r in resources:
        kind = r["type"].lower()
        entry = {"id": r["id"], "name": r["name"], "resource_group": r.get("resourceGroup") or "",
                 "subscription_id": (r.get("subscriptionId") or "").lower(),
                 "display_type": "Resource group" if kind.endswith("/resourcegroups")
                 else "Subscription" if kind == "microsoft.resources/subscriptions" else taxonomy.display_name(kind)}
        index.setdefault(r["name"].lower(), []).append(entry)
        by_id[r["id"].lower()] = entry
        if entry["display_type"] == "Subscription" and entry["subscription_id"]:
            index.setdefault(entry["subscription_id"], []).append(entry)
    return index, by_id


def match(texts, ids, index, by_id, id_via="configuration item"):
    """Azure resources a record refers to, each with how sure: exact (an Azure ID from `ids`, labelled `id_via`,
    or in the text), name (a distinctive full name), or possible (a short or plain name)."""
    found = {}
    for azure_id in list(ids) + [m.group(0) for t in texts for m in AZURE_ID.finditer(t or "")]:
        hit = by_id.get(azure_id.lower().rstrip("/."))
        if hit:
            found[hit["id"].lower()] = {**hit, "confidence": "exact", "via": id_via if azure_id in ids else "text"}
    for text in texts:
        for token in sorted(_words(text)):
            for hit in index.get(token, []):
                key = hit["id"].lower()
                if key not in found:
                    exact = bool(GUID.match(token))  # a subscription's ID
                    found[key] = {**hit, "confidence": "exact" if exact else "name" if STRONG_NAME.match(token) else "possible",
                                  "via": "text" if exact else "name"}
    rank = {"exact": 0, "name": 1, "possible": 2}
    return sorted(found.values(), key=lambda m: (rank[m["confidence"]], m["name"]))


def merge(*lists):
    """Matches from several sources in one list: each resource once, at its strongest (an exact one keeps its `via`)."""
    rank = {"exact": 0, "name": 1, "possible": 2}
    best = {}
    for found in lists:
        for m in found:
            key = m["id"].lower()
            if key not in best or rank[m["confidence"]] < rank[best[key]["confidence"]]:
                best[key] = m
    return sorted(best.values(), key=lambda m: (rank[m["confidence"]], m["name"]))


def _words(text):
    """Every word, plus the parts of a dotted or underscored one, so `myapp.azurewebsites.net` also tries `myapp` and
    `my-rg_prod` (a condition name glued on) tries `my-rg`; the whole word still counts, for names with `_` in them."""
    words = set(TOKEN.findall((text or "").lower()))
    return words | {part for word in words if "." in word or "_" in word
                    for part in re.split(r"[._]", word) if len(part) > 1}


def hits_resource(matches, resource_id):
    """True when a match is the resource or, for a resource group, anything in it."""
    target = resource_id.lower().rstrip("/")
    return any(m["id"].lower() == target or m["id"].lower().startswith(target + "/providers/") for m in matches)
