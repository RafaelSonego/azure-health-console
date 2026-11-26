"""What each feature needs, as the actions a role must allow (so custom roles count, not only role names), and a check
of the viewer's own access against it: per subscription in scope, or per resource of the type the feature reads.
The same list feeds the Access tab's "What the console needs" and the README's Permissions table. Read-only."""
from concurrent.futures import ThreadPoolExecutor

from . import graph, pim, scopes
from .arm import ArmError, kql_list
from .user_access import _allows, role_permissions

ANY_READ = "x.example/things/read"
USER_CREDENTIAL = "microsoft.containerservice/managedclusters/listclusterusercredential/action"
AKS_READ = "microsoft.containerservice/managedclusters/pods/read"
ACR_PULL = ("microsoft.containerregistry/registries/pull/read", "microsoft.containerregistry/registries/repositories/content/read")
AKS_TYPE, ACR_TYPE = "microsoft.containerservice/managedclusters", "microsoft.containerregistry/registries"

# key, feature, what it needs (shown as is), roles that give it, per ("subscription" or a resource type),
# actions (all needed) / data actions (any one is enough), what the page does without it.
REQUIREMENTS = [
    {"key": "read", "feature": "Triage, Browse, health, metrics, timeline, Readiness, Search, Secrets (names and dates), Network",
     "needs": "Reader on the subscriptions", "roles": ["Reader"], "per": "subscription", "actions": [ANY_READ],
     "without": "Subscriptions you can't read aren't in the scope picker; resources you can't read show as not readable"},
    {"key": "cost", "feature": "Cost", "needs": "Cost Management Reader or Reader on the subscriptions",
     "roles": ["Cost Management Reader", "Reader"], "per": "subscription", "actions": ["microsoft.costmanagement/query/read"],
     "without": "The Cost card names the subscriptions it couldn't read and the role to ask for"},
    {"key": "audit", "feature": "Access audit, Check someone's access", "needs": "Reader on the subscriptions (role assignments and PIM records)",
     "roles": ["Reader"], "per": "subscription", "actions": ["microsoft.authorization/roleassignmentscheduleinstances/read"],
     "without": "Those subscriptions are read resource group by resource group from Resource Graph; groups still refused can't be told from PIM activations"},
    {"key": "aks", "feature": "AKS Workloads, Nodes, pod logs, Running image",
     "needs": "AKS Cluster User Role and a Kubernetes read role (AKS RBAC Reader, or a view binding), plus a network path to the API server",
     "roles": ["Azure Kubernetes Service Cluster User Role", "Azure Kubernetes Service RBAC Reader"], "per": AKS_TYPE,
     "actions": [USER_CREDENTIAL], "data_actions": [AKS_READ],
     "note": "A Kubernetes binding (instead of AKS RBAC Reader) and the network path (VPN for private clusters) can't be seen from Azure: they're "
             "checked when you open Workloads.",
     "without": "Workloads and Nodes name the role, binding or network path that's missing; platform metrics and Container Insights still show"},
    {"key": "deps", "feature": "AKS dependencies of a workload",
     "needs": "Reader on the cluster's subscription (managed identities and their federated credentials, role assignments, Cosmos DB role assignments);"
              " for its configuration, the AKS roles and network path above",
     "roles": ["Reader"], "per": "subscription", "actions": [ANY_READ],
     "note": "The configuration part reads the workload's ConfigMaps (values are scanned for host names, never shown); Secrets are never read.",
     "without": "Without the Kubernetes API the list comes from the workload identity's roles alone, and says so"},
    {"key": "acr", "feature": "Vulnerabilities: find tags (registry images)", "needs": "AcrPull on the registries (or Container Registry Repository Reader)",
     "roles": ["AcrPull", "Container Registry Repository Reader"], "per": ACR_TYPE, "registry_pull": True,
     "without": "The image's digest still shows; the page says what refused"},
    {"key": "graph", "feature": "Access audit (group members), Check someone's access",
     "needs": "Your account can read groups' members and other users in Microsoft Graph (most tenants allow it)",
     "roles": [], "per": "directory",
     "without": "The audit names the groups whose members couldn't be listed; the user search shows Graph's refusal"},
    {"key": "defender", "feature": "Vulnerabilities: findings, Recommendations from Defender", "needs": "Defender for Servers and Defender for "
                                   "Containers turned on (paid plans, a setting, not a role)",
     "roles": [], "per": "setting",
     "without": "Only the platform checks show (AKS versions and node images, runtimes, TLS)"},
    {"key": "insights", "feature": "AKS logs and pod history (Container Insights)", "needs": "Container Insights turned on for the cluster (a setting),"
                                                                                             " and Reader on the cluster or Log Analytics Reader on its"
                                                                                             " workspace",
     "roles": ["Log Analytics Reader"], "per": "setting",
     "without": "Workloads says Container Insights isn't available and shows the other layers"},
]

_TYPES = ("resources | where type in~ ({types}) | project id = tolower(id), type = tolower(type),"
          " insights = isnotempty(properties.addonProfiles.omsagent) and tobool(properties.addonProfiles.omsagent.enabled)")


def _registry_pull(permissions, data):
    """A registry honours pull from `*` (Owner, Contributor) or its own provider's actions (AcrPull), never from
    Reader's `*/read` (measured: Reader is refused); with repository permissions, the content data action."""
    own = [{**b, "actions": [a for a in b.get("actions") or [] if a == "*" or a.lower().startswith("microsoft.containerregistry/")]}
           for b in permissions]
    return _allows(own, ACR_PULL[0]) or _allows(data, ACR_PULL[1])


def _allowed(permissions, req):
    """Whether one role's permissions give `req`: every action, and one of its data actions when it has some."""
    data = [{"actions": b.get("dataActions") or [], "notActions": b.get("notDataActions") or []} for b in permissions]
    if req.get("registry_pull"):
        return _registry_pull(permissions, data)
    return (all(_allows(permissions, a) for a in req.get("actions") or [])
            and (not req.get("data_actions") or any(_allows(data, a) for a in req["data_actions"])))


def _status(have, eligible, total):
    if not total:
        return "info"
    return "healthy" if have == total else "warning" if have + eligible > 0 else "critical"


def check(client, token_provider, subscriptions, found=None):
    """One row per requirement: how many of its subscriptions or resources your roles give it (standing or activated),
    how many more a PIM-eligible role would, and how many neither."""
    if found is None:
        from .user_access import standing  # it builds on pim, as this module does
        found = pim.roles(client, standing=standing(client, token_provider))
    held = found.get("standing") or []
    eligible = [{"scope": r["scope"], "role_id": r["role_id"], "role": r["role"]} for r in found.get("eligible") or []]
    permissions = role_permissions(client, {r["role_id"] for r in held + eligible})
    try:
        resources = client.query(_TYPES.format(types=kql_list([AKS_TYPE, ACR_TYPE])), subscriptions=list(subscriptions) or None)
    except ArmError:
        resources = []
    targets = {"subscription": [f"/subscriptions/{s}" for s in subscriptions],
               AKS_TYPE: [r["id"] for r in resources if r["type"] == AKS_TYPE],
               ACR_TYPE: [r["id"] for r in resources if r["type"] == ACR_TYPE]}

    def gives(roles, req, target):
        return next((r for r in roles if scopes.covers(r["scope"], target) == "yes"
                     and _allowed(permissions.get(r["role_id"]) or [], req)), None)

    rows = []
    for req in REQUIREMENTS:
        row = {k: req.get(k) for k in ("key", "feature", "needs", "roles", "per", "without", "note")}
        if req["per"] in targets:
            items = targets[req["per"]]
            have = [t for t in items if gives(held, req, t)]
            could = {}
            for t in items:
                if t not in have:
                    role = gives(eligible, req, t)
                    if role:
                        could.setdefault(role["role"], []).append(t)
            row.update(total=len(items), have=len(have), eligible=sum(len(v) for v in could.values()),
                       activate=sorted(could), unit="subscriptions" if req["per"] == "subscription" else
                       ("clusters" if req["per"] == AKS_TYPE else "registries"),
                       missing=[t for t in items if t not in have and not any(t in v for v in could.values())][:20])
            row["status"] = _status(row["have"], row["eligible"], row["total"])
        rows.append(row)
    _directory(token_provider, rows)
    _settings(client, subscriptions, resources, rows)
    return {"rows": rows, "pim_error": found.get("error"), "subscriptions": len(subscriptions)}


def _directory(token_provider, rows):
    """Graph: list one of your own groups' members, the read the audit and Check someone's access make."""
    row = next(r for r in rows if r["key"] == "graph")
    try:
        groups = graph.my_groups(token_provider) if token_provider is not None else []
        if groups:
            graph.group_users(token_provider, groups[0]["id"])
        row.update(status="healthy", detail="Microsoft Graph lists group members for you." if groups else
                   "You're in no group, so it couldn't be tried; user search will tell.")
    except ArmError as error:
        row.update(status="critical", detail=f"Microsoft Graph refused: {str(error).splitlines()[0][:160]}")


def _settings(client, subscriptions, resources, rows):
    """Defender plans per subscription and Container Insights per cluster: settings, not roles."""
    from . import vulnerabilities  # imported here: it imports this package's siblings
    defender = next(r for r in rows if r["key"] == "defender")
    try:
        cov = vulnerabilities.coverage(client, subscriptions)
        off = {s for p in cov.get("plans_off") or [] for s in p["subscriptions"]}
        defender.update(total=len(subscriptions), have=len(subscriptions) - len(off), unit="subscriptions",
                        status="healthy" if not off else "warning",
                        detail="; ".join(f"{p['label']} is off in {len(p['subscriptions'])} subscription(s)"
                                         for p in cov.get("plans_off") or []) or "Both scanning plans are on.")
    except ArmError as error:
        defender.update(status="unknown", detail=str(error).splitlines()[0][:160])
    insights = next(r for r in rows if r["key"] == "insights")
    clusters = [r for r in resources if r["type"] == AKS_TYPE]
    on = sum(1 for r in clusters if r.get("insights"))
    insights.update(total=len(clusters), have=on, unit="clusters", status=_status(on, 0, len(clusters)),
                    detail=f"Container Insights is on for {on} of {len(clusters)} cluster(s)." if clusters else "No AKS cluster in scope.")


CUSTOM_ROLE = {
    "Name": "Azure Health Console Reader",
    "IsCustom": True,
    "Description": "Everything Azure Health Console reads: Reader, the AKS user credential, Kubernetes objects other "
                   "than secrets, and registry images for tags. It changes nothing.",
    "Actions": ["*/read", "Microsoft.ContainerService/managedClusters/listClusterUserCredential/action"],
    "NotActions": [],
    "DataActions": ["Microsoft.ContainerService/managedClusters/*/read",
                    "Microsoft.ContainerRegistry/registries/repositories/content/read",
                    "Microsoft.ContainerRegistry/registries/repositories/metadata/read"],
    "NotDataActions": ["Microsoft.ContainerService/managedClusters/secrets/*"],
    "AssignableScopes": ["/subscriptions/00000000-0000-0000-0000-000000000000"],
}
