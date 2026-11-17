"""Another user's access, as far as the viewer can read it: their groups (Graph), active roles (Resource Graph, plus
one ARM read per management group) and PIM-eligible roles (one ARM read per subscription). Read-only throughout."""
import re
from concurrent.futures import ThreadPoolExecutor

from . import graph, inventory, pim, scopes
from .arm import AccessDenied, ArmError, Cancelled, kql_list
from .cache import TtlCache

ASSIGNMENTS_API = "2022-04-01"
ASSIGNMENTS_PATH = "/subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments"
ELIGIBLE_PATH = "/subscriptions/{sub}/providers/Microsoft.Authorization/roleEligibilityScheduleInstances"
BUILT_IN_PATH = "/providers/Microsoft.Authorization/roleDefinitions"
BUILT_IN_SECONDS = 24 * 3600  # Resource Graph holds custom roles only; built-in names are one read (~1.6 MB) a day
PARALLEL = 8
MAX_SUBSCRIPTIONS = 100  # eligibility costs one request each; above this the page asks first
PRINCIPAL_BATCH = 400  # principals per Resource Graph query: someone in hundreds of groups stays under its size limit
_ROLE_GUID = re.compile(r"([0-9a-f-]{36})$")

_ACTIVE = (
    "authorizationresources | where type =~ 'microsoft.authorization/roleassignments'"
    " | extend principalId = tolower(tostring(properties.principalId)), scope = tostring(properties.scope),"
    " roleGuid = tolower(extract(@'([0-9a-fA-F-]{{36}})$', 1, tostring(properties.roleDefinitionId)))"
    " | where principalId in ({principals})"
    " | join kind=leftouter (authorizationresources | where type =~ 'microsoft.authorization/roledefinitions'"
    " | summarize role = any(tostring(properties.roleName)) by roleGuid = tolower(name)) on roleGuid"
    " | project principalId, scope, roleGuid, role")
_ROLE_NAMES = ("authorizationresources | where type =~ 'microsoft.authorization/roledefinitions'"
               " | where tolower(name) in ({guids}) | summarize role = any(tostring(properties.roleName)) by roleGuid = tolower(name)")
_ROLE_PERMISSIONS = ("authorizationresources | where type =~ 'microsoft.authorization/roledefinitions'"
                     " | where tolower(name) in ({guids})"
                     " | summarize permissions = any(properties.permissions) by roleGuid = tolower(name)")
_CHAINS = ("resourcecontainers | where type =~ 'microsoft.resources/subscriptions'"
           " | project subscriptionId, parent = tostring(properties.managementGroupAncestorsChain[0].name)")


_built_in = TtlCache("built-in role names", BUILT_IN_SECONDS, access_bound=False)


def _built_in_definitions(client):
    """{role GUID: {"name", "permissions"}} for Azure's built-in roles, the same in every tenant; one read, cached per
    tenant for a day."""
    def load():
        rows = client.get_paged(BUILT_IN_PATH, ASSIGNMENTS_API, params={"$filter": "type eq 'BuiltInRole'"})
        return {(r.get("name") or "").lower(): {"name": (r.get("properties") or {}).get("roleName") or "",
                                                "permissions": (r.get("properties") or {}).get("permissions") or []}
                for r in rows}
    return _built_in.get_or_load(client.identity.tenant_id, load)


def built_in_roles(client):
    """{role GUID: name} for Azure's built-in roles."""
    return {guid: d["name"] for guid, d in _built_in_definitions(client).items()}


def role_permissions(client, guids):
    """{role GUID: its permissions blocks ([{"actions", "notActions"}])}: built-in from the daily list, custom from
    Resource Graph. Roles it can't read are left out."""
    out = {}
    try:
        built = _built_in_definitions(client)
        out.update({g: built[g]["permissions"] for g in guids if g in built})
    except ArmError:
        pass
    custom = sorted({g for g in guids if g} - set(out))
    if custom:
        try:
            out.update({r["roleGuid"]: r.get("permissions") or [] for r in client.query(_ROLE_PERMISSIONS.format(guids=kql_list(custom)))})
        except ArmError:
            pass
    return out


# What a role lets you do on the control plane, tested on stand-in actions: any provider's read, write and delete, and
# writing a role assignment. Data actions (blob contents, secrets) aren't counted.
_ANY_READ, _ANY_WRITE, _ANY_DELETE = "x.example/things/read", "x.example/things/write", "x.example/things/delete"
_ASSIGN = "microsoft.authorization/roleassignments/write"


def _pattern(action):
    return re.compile("^" + re.escape(action.lower()).replace(r"\*", ".*") + "$")


def _allows(permissions, action):
    """Whether any block grants `action` without its notActions taking it back."""
    return any(any(_pattern(a).match(action) for a in block.get("actions") or [])
               and not any(_pattern(n).match(action) for n in block.get("notActions") or []) for block in permissions)


def _some(permissions, test):
    return any(test(a.lower()) for block in permissions for a in block.get("actions") or [])


def capabilities(permissions):
    """{"read", "change": "all"|"some"|"", "manage_access": bool} for one role's permissions."""
    read = "all" if _allows(permissions, _ANY_READ) else "some" if _some(permissions, lambda a: a.endswith("/read")) else ""
    change = ("all" if _allows(permissions, _ANY_WRITE) and _allows(permissions, _ANY_DELETE)
              else "some" if _some(permissions, lambda a: not a.endswith("/read") and not a.startswith("microsoft.authorization/"))
              else "")
    return {"read": read, "change": change, "manage_access": _allows(permissions, _ASSIGN)}


RANK = {"": 0, "some": 1, "all": 2}


def combined(roles, permissions):
    """What a set of roles adds up to: each capability at its widest, with the roles giving it."""
    out = {"read": "", "change": "", "manage_access": False, "by": {"read": [], "change": [], "manage_access": []}}
    for role in roles:
        can = capabilities(permissions.get(role["role_id"]) or [])
        for key in ("read", "change"):
            if RANK[can[key]] > RANK[out[key]]:
                out[key], out["by"][key] = can[key], [role["role"]]
            elif can[key] and can[key] == out[key] and role["role"] not in out["by"][key]:
                out["by"][key].append(role["role"])
        if can["manage_access"]:
            out["manage_access"] = True
            out["by"]["manage_access"] = list(dict.fromkeys(out["by"]["manage_access"] + [role["role"]]))
    out["known"] = all(r["role_id"] in permissions for r in roles)
    return out


def _adds(now, can):
    """The capabilities an eligible role would add to what you hold."""
    added = [key for key in ("read", "change") if RANK[can[key]] > RANK[now[key]]]
    return added + (["manage_access"] if can["manage_access"] and not now["manage_access"] else [])


def role_names(client, guids, known=None):
    """{role GUID: name}: built-in roles from the daily cached list (or `known`), custom ones from Resource Graph,
    which holds only custom roles. Names it can't find are left out, so callers show the GUID."""
    names = dict(known) if known is not None else {}
    if known is None:
        try:
            names.update(built_in_roles(client))
        except ArmError:
            pass
    custom = sorted({g for g in guids if g} - set(names))
    if custom:
        try:
            names.update({r["roleGuid"]: r["role"] for r in client.query(_ROLE_NAMES.format(guids=kql_list(custom)))})
        except ArmError:
            pass
    return names


def _guid(role_definition_id):
    match = _ROLE_GUID.search((role_definition_id or "").lower())
    return match.group(1) if match else ""


def _via(principal_id, user_id, group_names):
    return "" if principal_id == user_id else group_names.get(principal_id, principal_id)


def _active(client, user_id, groups, subscriptions, built_in):
    principals = [user_id] + [g["id"].lower() for g in groups]
    names = {g["id"].lower(): g["name"] for g in groups}
    batches = [principals[i:i + PRINCIPAL_BATCH] for i in range(0, len(principals), PRINCIPAL_BATCH)]
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        pages = list(pool.map(lambda b: client.query(_ACTIVE.format(principals=kql_list(b)), subscriptions=subscriptions), batches))
    rows = [row for page in pages for row in page]
    return [{"scope": r["scope"], "role": r.get("role") or built_in.get(r["roleGuid"]) or r["roleGuid"], "role_id": r["roleGuid"],
             "via": _via(r["principalId"], user_id, names)} for r in rows]


def _management_groups(client, user_id, groups, subscriptions, notes, built_in):
    """Roles held at management groups (or /) reach a subscription by inheritance: one assignedTo read per distinct
    parent management group finds them, rather than one per subscription."""
    try:
        rows = client.query(_CHAINS, subscriptions=subscriptions)
    except ArmError as error:
        notes.append(f"Management-group roles weren't read: {str(error).splitlines()[0]}")
        return []
    picks = {}
    for row in rows:
        picks.setdefault(row.get("parent") or "", row["subscriptionId"].lower())
    names = {g["id"].lower(): g["name"] for g in groups}
    found, refused = {}, 0

    def read(sub):
        try:
            return client.get_paged(ASSIGNMENTS_PATH.format(sub=sub), ASSIGNMENTS_API,
                                    params={"$filter": f"assignedTo('{user_id}')"})
        except AccessDenied:
            return None
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        answers = list(pool.map(read, picks.values()))
    for items in answers:
        if items is None:
            refused += 1
            continue
        for item in items:
            props = item.get("properties") or {}
            scope = props.get("scope") or ""
            if scope.startswith("/subscriptions/"):
                continue  # Resource Graph already has these
            key = (scope.lower(), _guid(props.get("roleDefinitionId")), (props.get("principalId") or "").lower())
            found[key] = {"scope": scope or "/", "role_id": key[1], "via": _via(key[2], user_id, names)}
    if refused:
        notes.append(f"Management-group roles: {refused} of {len(picks)} reads were refused (Reader on a subscription "
                     "under each management group reads them).")
    names = role_names(client, {f["role_id"] for f in found.values()}, known=built_in)
    for item in found.values():
        item["role"] = names.get(item["role_id"]) or item["role_id"]
    return list(found.values())


def _eligible(client, user_id, subscriptions, progress, notes):
    done, rows, refused, failed = [0], {}, [], []

    def one(sub):
        try:
            return sub, client.get_paged(ELIGIBLE_PATH.format(sub=sub), pim.API_VERSION,
                                         params={"$filter": f"assignedTo('{user_id}')"}), None
        except Cancelled:
            raise
        except ArmError as error:
            return sub, [], error

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for sub, items, error in pool.map(one, subscriptions):
            done[0] += 1
            if progress:
                progress(f"Eligible roles: {done[0]} of {len(subscriptions)} subscriptions")
            if isinstance(error, AccessDenied):
                refused.append(sub)
            elif error:
                failed.append(sub)
            for item in items:
                row = pim._row(item)
                rows[(row["scope"], row["role_id"], row["via"])] = row  # management-group ones repeat per subscription
    if refused:
        notes.append(f"Eligible roles weren't readable in {len(refused)} subscription(s): Reader on the subscription "
                     "reads them.")
    if failed:
        notes.append(f"Eligible roles: Azure didn't answer for {len(failed)} subscription(s); Check again to retry.")
    return [{"scope": r["scope_id"], "role": r["role"], "role_id": r["role_id"], "via": r["via"], "end": r["end"]}
            for r in rows.values()]


def check(client, token_provider, user, subscriptions, progress=None, publish=None):
    """`user` is {id, name, upn}. Publishes groups and active roles first, then adds the eligible ones."""
    user_id = user["id"].lower()
    notes = ["Groups and roles from a PIM group the user is only eligible to join aren't shown: Azure lists those "
             "only to the user themselves."]
    if progress:
        progress("Groups")
    groups = graph.user_groups(token_provider, user_id)
    sub_names = {s["id"]: s["name"] for s in inventory.list_subscriptions(client)}
    if progress:
        progress("Active roles")
    try:
        built_in = built_in_roles(client)
    except ArmError as error:
        built_in = {}
        notes.append(f"Built-in role names couldn't be read, so some roles show as IDs: {str(error).splitlines()[0]}")
    active = (_active(client, user_id, groups, subscriptions, built_in)
              + _management_groups(client, user_id, groups, subscriptions, notes, built_in))

    def result(eligible, done):
        rows = {"active": active, "eligible": eligible}
        for kind in rows.values():
            for row in kind:
                row["scope_label"] = scopes.label(row["scope"], sub_names)
                row["scope_type"] = scopes.kind(row["scope"])
            kind.sort(key=lambda r: (r["scope"].count("/"), r["scope_label"].lower(), r["role"].lower()))
        granting = {}
        for row in active + eligible:
            if row["via"]:
                granting[row["via"]] = granting.get(row["via"], 0) + 1
        return {"user": user, "subscriptions": len(subscriptions), "done": done, "notes": list(notes),
                "groups": [{**g, "roles": granting.get(g["name"], 0)} for g in groups], **rows}

    if publish:
        publish(result([], False))
    eligible = _eligible(client, user_id, subscriptions, progress, notes)
    return result(eligible, True)


def standing(client, token_provider):
    """A loader for `pim.roles`: every role you hold now, standing or activated, direct or through a group, in every
    subscription you can read and at management groups, so PIM doesn't list what you already have as "eligible"."""
    def load():
        me = (client.identity.object_id or "").lower()
        with ThreadPoolExecutor(max_workers=2) as pool:
            groups_f = pool.submit(graph.my_groups, token_provider) if token_provider is not None else None
            built_f = pool.submit(built_in_roles, client)
        groups = groups_f.result() if groups_f else []
        try:
            built_in = built_f.result()
        except ArmError:
            built_in = {}  # the roles still list, named by their IDs
        with ThreadPoolExecutor(max_workers=2) as pool:
            active_f = pool.submit(_active, client, me, groups, None, built_in)
            groups_mg = pool.submit(_management_groups, client, me, groups, None, [], built_in)
        rows = active_f.result() + groups_mg.result()
        return [{"scope": r["scope"], "role_id": r["role_id"], "role": r.get("role") or r["role_id"], "via": r["via"]}
                for r in rows]
    return load


def my_access(client, token_provider, target_id):
    """Your roles that apply to `target_id` (a resource, resource group or subscription): standing ones, direct or
    through a group, and PIM activations with their time left, from one `assignedTo` read at that scope; plus the
    PIM-eligible roles that would cover it. Read-only."""
    me = (client.identity.object_id or "").lower()
    target = target_id.rstrip("/")
    found = pim.roles(client, standing=standing(client, token_provider))
    held = {(h["role_id"], h["scope"].lower().rstrip("/")): h for h in found["held"]}
    try:
        items = client.get_paged(f"{target}/providers/Microsoft.Authorization/roleAssignments", ASSIGNMENTS_API,
                                 params={"$filter": f"assignedTo('{me}')"})
    except AccessDenied as error:
        return {"roles": [], "eligible": [], "error": str(error).splitlines()[0], "required": error.role}
    except ArmError as error:
        return {"roles": [], "eligible": [], "error": str(error).splitlines()[0]}
    groups = {}
    if token_provider is not None:
        try:
            groups = {g["id"].lower(): g["name"] for g in graph.my_groups(token_provider)}
        except ArmError:
            pass
    rows = []
    for item in items:
        props = item.get("properties") or {}
        scope = (props.get("scope") or "/").lower().rstrip("/") or "/"
        if not pim.covers(scope, target):
            continue  # a role on something below this one doesn't apply to all of it
        role_id = _guid(props.get("roleDefinitionId"))
        principal = (props.get("principalId") or "").lower()
        active = held.get((role_id, scope))
        rows.append({"role_id": role_id, "scope": scope, "from": scopes.relation(scope, target),
                     "via": "" if principal == me else groups.get(principal, "a group you're in"),
                     "pim_until": active["end"] if active else None})
    names = role_names(client, {r["role_id"] for r in rows})
    unique = {}
    for row in rows:
        row["role"] = names.get(row["role_id"]) or row["role_id"]
        unique.setdefault((row["role_id"], row["scope"], row["via"]), row)
    ordered = sorted(unique.values(), key=lambda r: (r["pim_until"] is None, r["scope"].count("/") * -1, r["role"].lower()))
    hints = pim.hints(found, target)
    eligible = hints["hints"][:5]
    permissions = role_permissions(client, {r["role_id"] for r in ordered} | {h["role_id"] for h in eligible})
    can = combined(ordered, permissions)
    for hint in eligible:
        hint["adds"] = _adds(can, capabilities(permissions.get(hint["role_id"]) or [])) if hint["role_id"] in permissions else []
    return {"roles": ordered, "can": can, "eligible": eligible, "more_eligible": hints["more"],
            "activate_url": hints["activate_url"], "pim_error": found.get("error")}
