"""How the console names an Azure scope (tenant root, management group, subscription, resource group, resource), in
one place: the Access tab, Check someone's access and Your access here all describe scopes with these."""

MANAGEMENT_GROUP = "/providers/microsoft.management/managementgroups/"


def normal(scope):
    """Lower case, no trailing slash; the tenant root stays "/"."""
    return (scope or "/").lower().rstrip("/") or "/"


def kind(scope):
    """"management group", "subscription", "resource group", "resource" or "scope"."""
    scope = normal(scope)
    if MANAGEMENT_GROUP in scope:
        return "management group"
    if scope == "/":
        return "tenant root"
    return {2: "subscription", 4: "resource group"}.get(scope.count("/"), "resource" if scope.count("/") > 4 else "scope")


def label(scope, sub_names=None):
    """"sub-a / my-rg / my-app" instead of an ID; management groups and the tenant root by name."""
    scope = normal(scope)
    parts = scope.strip("/").split("/")
    if MANAGEMENT_GROUP in scope:
        return f"Management group {parts[-1]}"
    if scope == "/":
        return "Tenant root (/)"
    names = sub_names or {}
    out = [names.get(parts[1], parts[1])] if len(parts) > 1 else []
    if len(parts) > 3:
        out.append(parts[3])
    if len(parts) > 4:
        out.append(parts[-1])
    return " / ".join(out)


def relation(scope, target):
    """Where a role at `scope` comes from, seen from `target`: here, the resource group, the subscription, a
    management group or the tenant root."""
    scope, target = normal(scope), normal(target)
    if scope == target:
        return "here"
    if scope == "/":
        return "tenant root (/)"
    if MANAGEMENT_GROUP in scope:
        return f"management group {scope.rsplit('/', 1)[-1]}"
    return {"subscription": "the subscription", "resource group": "the resource group"}.get(kind(scope), "a parent resource")


def covers(scope, target):
    """"yes" when a role at `scope` applies to `target` (the same or a parent), "maybe" for a management group (its
    children can't be checked from here), "" otherwise."""
    scope, target = normal(scope), normal(target)
    if scope == "/" or target == scope or target.startswith(scope + "/"):
        return "yes"
    return "maybe" if MANAGEMENT_GROUP in scope else ""
