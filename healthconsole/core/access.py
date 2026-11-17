"""Turns a 403 into something a person can act on: what was being read, the role that
grants it, and Azure's own message. Matched on the request path, so it covers any type."""
import re

PIM_HINT = "If you're eligible for it through PIM, activate it, then click Refresh access."

# (path pattern, what was being read, role that grants it); first match wins.
_RULES = [
    (r"/providers/microsoft\.costmanagement/(query|forecast)$", "costs (Cost Management)",
     "Cost Management Reader or Reader on the subscription or resource group"),
    (r"/providers/microsoft\.consumption/budgets$", "budgets",
     "Cost Management Reader or Reader on the subscription or resource group"),
    (r"/providers/microsoft\.keyvault/vaults/[^/]+/(secrets|keys)$",
     "Key Vault secret and key metadata (names and expiry dates, never values)",
     "Reader on the vault, its resource group or subscription"),
    (r"/listclusterusercredential$", "the AKS cluster user credential",
     "Azure Kubernetes Service Cluster User Role on the cluster"),
    (r"/providers/microsoft\.insights/logs$", "Container Insights / log data",
     "Reader on the resource (resource-context log access), or Log Analytics Reader on its workspace"),
    (r"/providers/microsoft\.insights/(metrics|metricdefinitions)$", "metrics",
     "Monitoring Reader or Reader on the resource"),
    (r"/providers/microsoft\.resourcehealth/", "Resource Health",
     "Reader on the resource, its resource group or subscription"),
    (r"/providers/microsoft\.insights/eventtypes/management/values$", "the Activity Log",
     "Reader or Monitoring Reader on the subscription"),
    (r"/detectors(/[^/]+)?$", "Azure diagnostics (detectors)",
     "Reader on the resource"),
    (r"/providers/microsoft\.resourcegraph/resources$", "Resource Graph",
     "Reader on the subscriptions you picked"),
    (r"^/subscriptions$|^/tenants$", "the subscription list",
     "any role on at least one subscription in this tenant"),
    (r"/providers/[^/]+$", "the resource provider's metadata",
     "Reader on the subscription"),
]
_DEFAULT = ("this resource's configuration", "Reader on the resource, its resource group or subscription")

KUBERNETES_ROLE = ("Azure Kubernetes Service RBAC Reader (clusters using Azure RBAC), or a Kubernetes "
                   "RoleBinding/ClusterRoleBinding to the 'view' role (clusters using Kubernetes RBAC)")


def describe(path):
    lowered = (path or "").split("?")[0].rstrip("/").lower()
    for pattern, what, role in _RULES:
        if re.search(pattern, lowered):
            return what, role
    return _DEFAULT


def message(what, role, azure_message=""):
    lines = [f"No access to {what}.", f"Required: {role}.", PIM_HINT]
    if azure_message:
        lines.append(f"Azure said: {azure_message}")
    return "\n".join(lines)


def short(what, role):
    """One line, for a signal or a table cell."""
    return f"No access to {what}: needs {role}."
