"""Access audit: the people who can change things, manage access or read data without going through PIM, in the
scope's subscriptions. Per subscription, one read of Azure's role assignment instances (every assignment at, below
and inherited above it, plain or through PIM) and one of PIM-eligible ones; each role classified by what its
permissions allow, not by its name; groups opened to their users with Microsoft Graph. Read-only throughout."""
import re
from concurrent.futures import ThreadPoolExecutor

from . import graph, inventory, pim, scopes, taxonomy
from .arm import AccessDenied, ArmError, Cancelled
from .user_access import _allows, _pattern, capabilities, role_names, role_permissions

INSTANCES_PATH = "/subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignmentScheduleInstances"
ELIGIBLE_PATH = "/subscriptions/{sub}/providers/Microsoft.Authorization/roleEligibilityScheduleInstances"
PARALLEL = 8
MAX_SUBSCRIPTIONS = 100  # two reads each; above this the page asks first
# Worst first: what each level lets someone do without PIM.
LEVELS = ("manage_access", "change_all", "credentials", "data_write", "data_read", "change_some", "unknown")
LEVEL_LABELS = {"manage_access": "Manages access", "change_all": "Changes everything",
                "credentials": "Gets keys or secrets, or signs in", "data_write": "Changes data", "data_read": "Sees data",
                "change_some": "Changes some things", "unknown": "Role not readable"}
PEOPLE = ("User", "Group")
# Ignored by default: on a cluster with Entra ID it only hands out the kubeconfig; without Entra ID it's admin access.
USER_CREDENTIAL = "microsoft.containerservice/managedclusters/listclusterusercredential/action"
_NO_ENTRA = ("resources | where type =~ 'microsoft.containerservice/managedclusters'"
             " | where isempty(tostring(properties.aadProfile)) | project id = tolower(id), name")
# Every assignment to a person or group that Resource Graph holds: it covers subscriptions where the instances read is
# refused (no Reader on the subscription) and the few assignments the instances API leaves out.
_GRAPH_ASSIGNMENTS = ("authorizationresources | where type =~ 'microsoft.authorization/roleassignments'"
                      " | extend principalType = tostring(properties.principalType) | where principalType in ('User', 'Group')"
                      " | project scope = tostring(properties.scope), principalId = tolower(tostring(properties.principalId)),"
                      " principalType, roleGuid = tolower(extract(@'([0-9a-fA-F-]{36})$', 1, tostring(properties.roleDefinitionId))),"
                      " subscriptionId = tolower(subscriptionId)")
GRAPH_LIMIT = 200000
# Key Vaults still on the access policy model give secrets through their policies, not role assignments.
_VAULT_POLICIES = ("resources | where type =~ 'microsoft.keyvault/vaults'"
                   " | where tostring(properties.enableRbacAuthorization) != 'true'"
                   " | mv-expand p = properties.accessPolicies | where isnotempty(tostring(p.objectId))"
                   " | project id, subscriptionId = tolower(subscriptionId), objectId = tolower(tostring(p.objectId)),"
                   " secrets = p.permissions.secrets, keys = p.permissions.keys, certificates = p.permissions.certificates,"
                   " storage = p.permissions.storage")
_KEY_USE = {"decrypt", "encrypt", "unwrapkey", "wrapkey", "sign", "verify"}
_CONDITIONS = ("authorizationresources | where type =~ 'microsoft.authorization/roleassignments'"
               " | where isnotempty(tostring(properties.condition))"
               " | project scope = tolower(tostring(properties.scope)), principal = tolower(tostring(properties.principalId)),"
               " roleGuid = tolower(extract(@'([0-9a-fA-F-]{36})$', 1, tostring(properties.roleDefinitionId))),"
               " condition = tostring(properties.condition)")
_DATA_WRITE = re.compile(r"(/write|/delete|/\*|^\*)$")
# Handing over access itself: keys, credentials, connection strings, SAS, a machine's sign-in, a secret's value.
_CREDENTIALS = re.compile(r"/(list\w*(keys|credentials?|secrets|connectionstrings|sas)|readonlykeys|get\w*(credentials?|secrets?)"
                          r"|begingetaccess|generate\w*token|\w*loginasadmin)/action$")
_SECRET_VALUES = ("microsoft.keyvault/vaults/secrets/getsecret/action", "microsoft.keyvault/vaults/keys/release/action")
# Azure names many reads as actions (checkNameAvailability, getBgpPeerStatus, backendHealth, exportTemplate): by the
# verb before /action, the same in every tenant. A read that hands over access matches _CREDENTIALS first.
_READ_VERB = re.compile(r"^(check|validate|query|search|export|get|list|fetch|count|preview|read|download|analy[sz]e|test"
                        r"|lookup|evaluate|calculate|estimate|find|retrieve)|(status|health|routes|reports?|details|export"
                        r"|availability|requirements|indicators|usages?|metrics|telemetry|view)$")


def _read_shaped(action):
    """A control action that only reads, though Azure calls it an action."""
    action = action.lower()
    if "/read/" in action:  # a wildcard under a Read segment (EdgeMachines/Read/*)
        return True
    if not action.endswith("/action") or _CREDENTIALS.search(action):
        return False
    parts = action.split("/")
    return len(parts) > 2 and bool(_READ_VERB.search(parts[-2]))


def _strip(permissions, ignore):
    """The permissions without the actions and data actions `ignore` lists (e.g. Microsoft.Support/*)."""
    patterns = [_pattern(p) for p in ignore or []]
    keep = lambda actions: [a for a in actions or [] if not any(p.match(a.lower()) for p in patterns)]  # noqa: E731
    return [{**block, "actions": [a for a in keep(block.get("actions")) if not _read_shaped(a)],
             "dataActions": keep(block.get("dataActions"))} for block in permissions]


def level(permissions, ignore=()):
    """The widest thing a role allows: a key of LEVELS, or "" for reads only (and the ignored actions)."""
    blocks = _strip(permissions, ignore)
    can = capabilities(blocks)
    if can["manage_access"]:
        return "manage_access"
    if can["change"] == "all":
        return "change_all"
    actions = [a.lower() for b in blocks for a in b.get("actions") or []
               if not any(_pattern(n).match(a.lower()) for n in b.get("notActions") or [])]
    if any(_CREDENTIALS.search(a) for a in actions):
        return "credentials"
    data = [a.lower() for b in blocks for a in b.get("dataActions") or []
            if not any(_pattern(n).match(a.lower()) for n in b.get("notDataActions") or [])]
    data_blocks = [{"actions": b.get("dataActions") or [], "notActions": b.get("notDataActions") or []} for b in blocks]
    if any(_allows(data_blocks, value) for value in _SECRET_VALUES) or any(a.endswith("loginasadmin/action") for a in data):
        return "credentials"  # a secret's value, or signing in to a machine as its administrator
    if any(_DATA_WRITE.search(a) for a in data):
        return "data_write"
    if data:
        return "data_read"
    return "change_some" if can["change"] == "some" else ""


def _instance(item):
    props = item.get("properties") or {}
    expanded = props.get("expandedProperties") or {}
    principal = expanded.get("principal") or {}
    scope = (props.get("scope") or "/").rstrip("/") or "/"
    return {"principal_id": (props.get("principalId") or "").lower(),
            "principal_type": props.get("principalType") or principal.get("type") or "",
            "principal_name": principal.get("displayName") or "", "principal_mail": principal.get("email") or "",
            "role_id": (props.get("roleDefinitionId") or "").rsplit("/", 1)[-1].lower(),
            "role": (expanded.get("roleDefinition") or {}).get("displayName") or "",
            "scope": scope, "scope_name": (expanded.get("scope") or {}).get("displayName") or "",
            "assignment_type": props.get("assignmentType") or "", "end": props.get("endDateTime")}


def _refused(error):
    """Without Reader, the PIM lists answer 400 ("use $filter=asTarget()") rather than 403."""
    return isinstance(error, AccessDenied) or (getattr(error, "status", None) == 400 and "permissions" in str(error))


def _read_all(client, subscriptions, progress, notes):
    """Assignment and eligibility instances for every subscription, deduplicated (inherited ones repeat per subscription)."""
    done, assigned, eligible, refused, failed = [0], {}, {}, [], []

    def one(sub):
        out = []
        for path in (INSTANCES_PATH, ELIGIBLE_PATH):
            try:
                out.append(client.get_paged(path.format(sub=sub), pim.API_VERSION))
            except Cancelled:
                raise
            except ArmError as error:
                out.append(error)
        return sub, out

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for sub, (assignments, eligibility) in pool.map(one, subscriptions):
            done[0] += 1
            if progress:
                progress(f"Role assignments: {done[0]} of {len(subscriptions)} subscriptions")
            if isinstance(assignments, ArmError):
                (refused if _refused(assignments) else failed).append(sub)
                continue
            for item in assignments:
                row = _instance(item)
                assigned[_key(row)] = row
            for item in [] if isinstance(eligibility, ArmError) else eligibility:
                row = _instance(item)
                eligible[_key(row)] = row
    if failed:
        notes.append(f"Azure didn't answer for {len(failed)} subscription(s); Check again to retry.")
    return assigned, eligible, refused


def policy_level(secrets, keys, certificates, storage):
    """What an access policy entry allows, as a level: reading a secret's value or a key's release is the access
    itself; any other permission past get and list changes data; using keys sees data; get and list alone are metadata."""
    sets = [{str(p).lower() for p in perms or []} for perms in (secrets, keys, certificates, storage)]
    s, k, c, st = sets
    if s & {"get", "all"} or k & {"release", "all"} or st & {"get", "getsas", "all"}:
        return "credentials"
    if any(perms - {"get", "list"} - _KEY_USE for perms in sets):
        return "data_write"
    return "data_read" if k & _KEY_USE else ""


def _policy_name(row):
    parts = [f"{kind} {', '.join(sorted(str(p).lower() for p in row[kind]))}"
             for kind in ("secrets", "keys", "certificates", "storage") if row.get(kind)]
    return "Key Vault access policy: " + "; ".join(parts)


def _vault_policies(client, token_provider, subscriptions, notes):
    """(rows like assignments for people and groups, orphaned entries): each vault's access policies, the object
    behind each entry looked up in Microsoft Graph (deleted ones are orphaned, apps left out)."""
    try:
        entries = client.query(_VAULT_POLICIES, subscriptions=subscriptions, limit=GRAPH_LIMIT)
    except ArmError as error:
        notes.append(f"Key Vault access policies couldn't be read: {str(error).splitlines()[0]}")
        return [], []
    entries = [e for e in entries if policy_level(e["secrets"], e["keys"], e["certificates"], e["storage"])]

    def look(object_id):
        try:
            return object_id, graph.directory_object(token_provider, object_id), None
        except ArmError as error:
            return object_id, None, error.status
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        found = {oid: (obj, status) for oid, obj, status in pool.map(look, sorted({e["objectId"] for e in entries}))}
    rows, orphaned = [], []
    for e in entries:
        obj, status = found[e["objectId"]]
        row = {"principal_id": e["objectId"], "principal_type": "", "principal_name": "", "principal_mail": "",
               "role_id": "access-policy", "role": _policy_name(e), "scope": e["id"], "scope_name": "",
               "assignment_type": "AccessPolicy", "end": None, "sub": e["subscriptionId"],
               "level": policy_level(e["secrets"], e["keys"], e["certificates"], e["storage"])}
        if status == 404:
            orphaned.append({**row, "principal_type": "deleted"})
        elif obj and obj["type"] in ("user", "group"):
            rows.append({**row, "principal_type": obj["type"].capitalize(), "principal_name": obj["name"],
                         "principal_mail": obj["upn"]})
    vaults = len({e["id"] for e in entries})
    if vaults:
        notes.append(f"{vaults} Key Vault(s) still use access policies: their entries that read or change secrets, keys "
                     "or certificates are listed as \"Key Vault access policy\" (standing: access policies have no PIM).")
    return rows, orphaned


def _key(row):
    return row["scope"].lower().rstrip("/") or "/", row["role_id"], row["principal_id"]


def _from_resource_graph(client, subscriptions, notes):
    try:
        rows = client.query(_GRAPH_ASSIGNMENTS, subscriptions=subscriptions, limit=GRAPH_LIMIT)
    except ArmError as error:
        notes.append(f"Resource Graph's role assignments couldn't be read, so subscriptions you can't read at "
                     f"subscription level are missing: {str(error).splitlines()[0]}")
        return []
    return [{"principal_id": r["principalId"], "principal_type": r["principalType"], "principal_name": "", "principal_mail": "",
             "role_id": r["roleGuid"], "role": "", "scope": (r["scope"] or "/").rstrip("/") or "/", "scope_name": "",
             "assignment_type": "Assigned", "end": None, "sub": r["subscriptionId"]} for r in rows]


def _group_of(scope):
    parts = scope.lower().split("/")
    return "/".join(parts[:5]) if len(parts) >= 5 and parts[3] == "resourcegroups" else ""


def _fill_refused(client, refused, graph_rows, assigned, eligible, notes):
    """Where the subscription-level read is refused, read each resource group that holds assignments (Reader on the
    group is enough there). Returns the resource groups still refused, whose rows can't be told from PIM activations."""
    refused = set(refused)
    groups = sorted({_group_of(r["scope"]) for r in graph_rows if r["sub"] in refused} - {""})

    def one(group):
        out = []
        for path in (INSTANCES_PATH, ELIGIBLE_PATH):
            try:
                out.append(client.get_paged(path.replace("/subscriptions/{sub}", group), pim.API_VERSION))
            except Cancelled:
                raise
            except ArmError as error:
                out.append(error)
        return group, out

    still = set()
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for group, (assignments, eligibility) in pool.map(one, groups):
            if isinstance(assignments, ArmError):
                still.add(group)
                continue
            for item in assignments:
                row = _instance(item)
                assigned.setdefault(_key(row), row)
            for item in [] if isinstance(eligibility, ArmError) else eligibility:
                row = _instance(item)
                eligible.setdefault(_key(row), row)
    if refused:
        notes.append(f"{len(refused)} subscription(s) aren't readable at subscription level, so their assignments come "
                     f"from Resource Graph, read resource group by resource group ({len(groups) - len(still)} of "
                     f"{len(groups)} group(s) readable). Reader on the subscription reads them in one go.")
    return still


def _merge(graph_rows, assigned, refused, still):
    """Resource Graph's assignments the instances didn't have: plain assignments (a PIM activation is always an
    instance), except in resource groups nobody could read, where it can't be told."""
    refused, added = set(refused), 0
    for row in graph_rows:
        key = _key(row)
        if key in assigned:
            continue
        if row["sub"] in refused and (_group_of(row["scope"]) in still or not _group_of(row["scope"])):
            row["pim_unknown"] = True
        assigned[key] = row
        added += 1
    return added


def resource_type(scope):
    """("microsoft.keyvault/vaults", "Key Vault") for a role on a resource (child types joined, as Azure writes them);
    ("", "Subscription") and the like for one on a management group, subscription or resource group."""
    parts = scope.strip("/").split("/")
    lowered = [p.lower() for p in parts]
    if "providers" in lowered and lowered[:1] == ["subscriptions"]:
        rest = parts[lowered.index("providers") + 1:]
        if len(rest) >= 3:
            types = [t.lower() for t in rest[1::2]]
            rtype = "/".join([rest[0].lower()] + types)
            if len(types) > 1 and rtype not in taxonomy.DISPLAY_NAMES:  # "Storage Account › containers"
                return rtype, f"{taxonomy.display_name(f'{rest[0].lower()}/{types[0]}')} › {types[-1]}"
            return rtype, taxonomy.display_name(rtype)
    return "", scopes.kind(scope).capitalize()


def _in_groups(scope, groups):
    """With the scope narrowed to resource groups ("sub/rg"): roles above them (subscription, management group) still
    apply; roles on other resource groups don't."""
    if not groups:
        return True
    scope = scope.lower()
    if not scope.startswith("/subscriptions/") or scope.count("/") < 4:
        return True
    parts = scope.split("/")
    return f"{parts[2]}/{parts[4]}" in {g.lower() for g in groups}


def _members(token_provider, group_ids, notes):
    """{group id: [users]} for each group, nested groups included; a group Graph refuses is noted and left empty."""
    out, refused, deleted = {}, 0, set()

    def one(group_id):
        try:
            return group_id, graph.group_users(token_provider, group_id), None
        except ArmError as error:
            return group_id, None, error.status
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for group_id, users, status in pool.map(one, sorted(group_ids)):
            if status == 404:
                deleted.add(group_id)
            elif users is None:
                refused += 1
            out[group_id] = users or []
    if refused:
        notes.append(f"Microsoft Graph didn't list the members of {refused} group(s), so the people in them are missing.")
    return out, deleted


def _group_names(token_provider, group_ids):
    """Names for groups Resource Graph gave only by ID (Azure's instances carry names, Resource Graph doesn't)."""
    def one(group_id):
        try:
            return group_id, graph.group(token_provider, group_id)["name"]
        except ArmError:
            return group_id, ""
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        return {g: n for g, n in pool.map(one, sorted(group_ids)) if n}


def _people(token_provider, user_ids, notes):
    """{user id: user} for people assigned a role directly (name, sign-in name, guest, enabled)."""
    out, missing, deleted = {}, 0, set()

    def one(user_id):
        try:
            return user_id, graph.user(token_provider, user_id), None
        except ArmError as error:
            return user_id, None, error.status
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for user_id, user, status in pool.map(one, sorted(user_ids)):
            if status == 404:
                deleted.add(user_id)
            elif user is None:
                missing += 1
            else:
                out[user_id] = user
    if missing:
        notes.append(f"{missing} user(s) couldn't be read in Microsoft Graph (deleted, or not visible to you): they show "
                     "by the name Azure keeps on the assignment.")
    return out, deleted


def _clusters_without_entra(client, subscriptions, notes):
    try:
        return client.query(_NO_ENTRA, subscriptions=subscriptions)
    except ArmError as error:
        notes.append(f"Which AKS clusters lack Entra ID couldn't be read: {str(error).splitlines()[0]}")
        return []


def _conditions(client, notes):
    """{(scope, role, principal): condition}: Resource Graph keeps an assignment's condition (management groups too);
    the assignment instances don't. Few assignments have one, so it's one small tenant-wide query."""
    try:
        rows = client.query(_CONDITIONS)
    except ArmError as error:
        notes.append(f"Role assignment conditions couldn't be read: {str(error).splitlines()[0]}")
        return {}
    return {((r["scope"].rstrip("/") or "/"), r["roleGuid"], r["principal"]): r["condition"] for r in rows}


def _upgrade_user_credential(row, permissions, clusters, rank):
    """The AKS user credential counts where it's admin: on clusters without Entra ID under the assignment's scope."""
    if rank.get(row["level"], len(LEVELS)) <= rank["credentials"] or not _allows(permissions, USER_CREDENTIAL):
        return
    covered = [c["name"] for c in clusters if pim.covers(row["scope"].lower(), c["id"])]
    if covered:
        row["level"] = "credentials"
        row["note"] = (f"Its AKS user credential is admin on {len(covered)} cluster(s) without Entra ID: "
                       f"{', '.join(covered[:5])}{'…' if len(covered) > 5 else ''}")


def audit(client, token_provider, subscriptions, groups=None, ignore=(), progress=None):
    """{"people": [...], "summary", "notes"}: one entry per person with what they can do without PIM, worst first."""
    notes = ["Group memberships made through PIM for Groups can't be told apart here (Microsoft Graph needs a "
             "permission the Azure CLI sign-in doesn't have), so a person in a group is listed as standing."]
    sub_names = {s["id"]: s["name"] for s in inventory.list_subscriptions(client)}
    assigned, eligible, refused = _read_all(client, subscriptions, progress, notes)
    if progress:
        progress("Checking against Resource Graph")
    graph_rows = _from_resource_graph(client, subscriptions, notes)
    still = _fill_refused(client, refused, graph_rows, assigned, eligible, notes) if refused else set()
    _merge(graph_rows, assigned, refused, still)
    eligible = list(eligible.values())
    kept = [r for r in assigned.values() if r["principal_type"] in PEOPLE and r["assignment_type"] != "Activated"
            and _in_groups(r["scope"], groups)]
    policy_rows, policy_orphans = _vault_policies(client, token_provider, subscriptions, notes)
    kept += [r for r in policy_rows if _in_groups(r["scope"], groups)]
    names = role_names(client, {r["role_id"] for r in kept if not r["role"]})
    for row in kept:
        row["role"] = row["role"] or names.get(row["role_id"]) or row["role_id"]
    if progress:
        progress("Reading what each role allows")
    permissions = role_permissions(client, {r["role_id"] for r in kept})
    rank = {key: i for i, key in enumerate(LEVELS)}
    ignores_credential = any(_pattern(p).match(USER_CREDENTIAL) for p in ignore or [])
    clusters = (_clusters_without_entra(client, subscriptions, notes) if ignores_credential
                and any(_allows(permissions.get(r["role_id"]) or [], USER_CREDENTIAL) for r in kept) else [])
    for row in kept:
        if row["role_id"] == "access-policy":
            continue  # its level comes from the policy's own permissions
        row["level"] = level(permissions[row["role_id"]], ignore) if row["role_id"] in permissions else "unknown"
        if clusters:
            _upgrade_user_credential(row, permissions.get(row["role_id"]) or [], clusters, rank)
    kept = [r for r in kept if r["level"]]
    conditions = _conditions(client, notes)
    for row in kept:
        row["condition"] = conditions.get((row["scope"].lower(), row["role_id"], row["principal_id"]), "")
    if progress:
        progress("Opening groups to their members")
    group_ids = {r["principal_id"] for r in kept if r["principal_type"] == "Group"}
    eligible_groups = {r["principal_id"] for r in eligible if r["principal_type"] == "Group"
                       and r["role_id"] in {k["role_id"] for k in kept}}
    members, deleted_groups = _members(token_provider, group_ids | eligible_groups, notes)
    direct, deleted_users = _people(token_provider, {r["principal_id"] for r in kept if r["principal_type"] == "User"}, notes)
    group_names = _group_names(token_provider, {r["principal_id"] for r in kept if r["principal_type"] == "Group"
                                                and not r["principal_name"]} - deleted_groups)
    orphaned = [{"principal_id": r["principal_id"], "principal_type": r["principal_type"], "role": r["role"],
                 "level": r["level"], "scope": r["scope"], "scope_label": scopes.label(r["scope"], sub_names),
                 "type_label": resource_type(r["scope"])[1]}
                for r in kept if r["principal_id"] in (deleted_users | deleted_groups)]
    orphaned += [{"principal_id": r["principal_id"], "principal_type": "deleted", "role": r["role"], "level": r["level"],
                  "scope": r["scope"], "scope_label": scopes.label(r["scope"], sub_names), "type_label": "Key Vault"}
                 for r in policy_orphans if _in_groups(r["scope"], groups)]
    kept = [r for r in kept if r["principal_id"] not in (deleted_users | deleted_groups)]

    eligible_by_user = {}
    for row in eligible:
        users = [row["principal_id"]] if row["principal_type"] == "User" else [u["id"] for u in members.get(row["principal_id"], [])]
        for user_id in users:
            eligible_by_user.setdefault(user_id, []).append(row)

    people = {}
    for row in kept:
        if row["principal_type"] == "User":
            fallback = {"id": row["principal_id"], "name": row["principal_name"], "upn": row["principal_mail"],
                        "guest": False, "enabled": True}
            holders = [(direct.get(row["principal_id"], fallback), "")]
        else:
            group = row["principal_name"] or group_names.get(row["principal_id"]) or row["principal_id"]
            holders = [(user, group) for user in members.get(row["principal_id"], [])]
        rtype, type_label = resource_type(row["scope"])
        for user, via in holders:
            person = people.setdefault(user["id"], {**user, "items": []})
            also = any(e["role_id"] == row["role_id"] and pim.covers(e["scope"].lower(), row["scope"].lower())
                       for e in eligible_by_user.get(user["id"], []))
            person["items"].append({
                "role": row["role"] or row["role_id"], "role_id": row["role_id"], "level": row["level"],
                "scope": row["scope"], "scope_label": scopes.label(row["scope"], sub_names), "scope_type": scopes.kind(row["scope"]),
                "resource_type": rtype, "type_label": type_label,
                "via": via, "until": row["end"], "eligible_too": also,
                "condition": row["condition"], "note": row.get("note", ""), "pim_unknown": bool(row.get("pim_unknown"))})
    for person in people.values():
        person["items"].sort(key=lambda i: (rank[i["level"]], i["scope"].count("/"), i["scope_label"].lower()))
        person["level"] = person["items"][0]["level"]
        person["direct"] = sum(1 for i in person["items"] if not i["via"])
        person["eligible_too"] = sum(1 for i in person["items"] if i["eligible_too"])
    ordered = sorted(people.values(), key=lambda p: (rank[p["level"]], -len(p["items"]), (p["name"] or "").lower()))
    summary = {"people": len(ordered), "assignments": len(kept), "subscriptions": len(subscriptions),
               "levels": {key: sum(1 for p in ordered if p["level"] == key) for key in LEVELS},
               "guests": sum(1 for p in ordered if p.get("guest")),
               "eligible_too": sum(1 for p in ordered if p["eligible_too"]),
               "time_limited": sum(1 for p in ordered for i in p["items"] if i["until"]),
               "orphaned": len(orphaned), "refused_subscriptions": len(refused)}
    orphaned.sort(key=lambda o: (rank[o["level"]], o["scope_label"].lower()))
    return {"people": ordered, "orphaned": orphaned, "summary": summary, "notes": notes, "labels": LEVEL_LABELS}
