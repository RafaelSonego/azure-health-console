"""PIM eligibility hints: which roles this session could activate that would cover a resource it can't read.
Two reads of the caller's own eligibilities and activation requests; the console never activates anything."""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import scopes
from .arm import ArmError
from .cache import TtlCache

API_VERSION = "2020-10-01"
ELIGIBLE_PATH = "/providers/Microsoft.Authorization/roleEligibilityScheduleInstances"
# Not roleAssignmentScheduleInstances: it lists every standing assignment too and took ~60 s on a large tenant.
REQUESTS_PATH = "/providers/Microsoft.Authorization/roleAssignmentScheduleRequests"
_ACTIVATE = ("SelfActivate", "SelfExtend", "SelfRenew")
ACTIVATE_URL = "https://portal.azure.com/#view/Microsoft_Azure_PIMCommon/ActivationMenuBlade/~/azurerbac"
CACHE_SECONDS = 300
MAX_HINTS = 6
MAX_LIST = 300
_cache = TtlCache("pim roles", CACHE_SECONDS)


def _row(item):
    props = item.get("properties") or {}
    expanded = props.get("expandedProperties") or {}
    scope = (props.get("scope") or "").rstrip("/") or "/"
    principal = expanded.get("principal") or {}
    return {"scope": scope.lower(), "scope_id": scope, "scope_name": (expanded.get("scope") or {}).get("displayName") or scope,
            "scope_type": ((expanded.get("scope") or {}).get("type") or "").lower(),
            "role": (expanded.get("roleDefinition") or {}).get("displayName") or "",
            "role_id": (props.get("roleDefinitionId") or "").rsplit("/", 1)[-1].lower(),
            "member_type": props.get("memberType") or "", "end": props.get("endDateTime"),
            "via": (principal.get("displayName") or "") if principal.get("type") == "Group" else ""}


def _duration(text):
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", text or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g or 0) for g in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes)


def active_activations(requests, now=None):
    """[{scope, role_id, end}] for self-activations still running: provisioned, not expired, not deactivated since."""
    now = now or datetime.now(timezone.utc)
    latest = {}
    for item in requests:
        props = item.get("properties") or {}
        info = props.get("scheduleInfo") or {}
        key = ((props.get("scope") or "").lower().rstrip("/"), (props.get("roleDefinitionId") or "").rsplit("/", 1)[-1].lower())
        try:
            start = datetime.fromisoformat(str(info.get("startDateTime") or props.get("createdOn")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if key not in latest or start > latest[key][0]:
            latest[key] = (start, props.get("requestType"), props.get("status"), (info.get("expiration") or {}))
    out = []
    for (scope, role_id), (start, kind, status, expiration) in latest.items():
        if kind not in _ACTIVATE or status not in ("Provisioned", "Granted"):
            continue
        end = expiration.get("endDateTime")
        end_at = (datetime.fromisoformat(end.replace("Z", "+00:00")) if end else
                  start + (_duration(expiration.get("duration")) or timedelta(0)))
        if end_at > now:
            out.append({"scope": scope, "role_id": role_id, "end": end_at.isoformat()})
    return out


def roles(client, refresh=False, standing=None):
    """{"eligible": [...], "held": [active activations], "standing": [...], "error"}: cached per identity.
    `standing()` reads the roles held now ([{scope, role_id, via}]), so eligibility doesn't offer them again."""
    key = getattr(client.identity, "object_id", "") + "|" + getattr(client.identity, "tenant_id", "")
    cached = None if refresh else _cache.get(key)
    if cached is not None:
        return cached
    params = {"$filter": "asTarget()"}
    with ThreadPoolExecutor(max_workers=3) as pool:  # three independent reads: ~9 s one after another
        eligible_f = pool.submit(client.get_paged, ELIGIBLE_PATH, API_VERSION, params=params)
        held_f = pool.submit(client.get_paged, REQUESTS_PATH, API_VERSION, params=params)
        standing_f = pool.submit(standing) if standing is not None else None
    eligible, held, error, status = [], [], None, None
    try:
        eligible = [_row(i) for i in eligible_f.result()]
    except ArmError as failed:
        error, status = str(failed).splitlines()[0], failed.status  # 400: no PIM licence; the roles you hold still list
    if error is None:
        try:
            held = active_activations(held_f.result())
        except ArmError:
            held = []
    owned, owned_error = [], None
    if standing_f is not None:
        try:
            owned = [{**r, "scope": scopes.normal(r["scope"])} for r in standing_f.result()]
        except ArmError as unread:
            owned_error = str(unread).splitlines()[0]
    found = {"eligible": eligible, "held": held, "standing": owned, "standing_error": owned_error, "error": error}
    return found if error and status != 400 else _cache.put(key, found)  # a failed read isn't kept; a missing licence is


def forget():
    """After Refresh access: an activation should show at once, not after the cache expires."""
    _cache.clear()


def covers(scope, target):
    """A role at `scope` applies to `target`: see `scopes.covers` (management groups are "maybe")."""
    return scopes.covers(scope, target)


def holds(found, role_id, scope):
    """The role you already hold (standing, or activated) that covers `scope` for `role_id`, if any."""
    return next((s for s in found.get("standing") or [] if s["role_id"] == role_id and covers(s["scope"], scope) == "yes"),
                None)


def _rank(row, required):
    role = row["role"].lower()
    named = bool(role) and role in (required or "").lower()
    reads = "reader" in role or "monitoring" in role
    return (not named, not reads, row["coverage"] != "yes", -len(row["scope"]), role)


def hints(found, target, required=""):
    """Eligible roles, not already held, that cover `target`; the role a 403 named first, then readers."""
    out = []
    for row in found["eligible"]:
        coverage = covers(row["scope"], target)
        if not coverage or holds(found, row["role_id"], target) or any(
                h["role_id"] == row["role_id"] and covers(h["scope"], target) == "yes" for h in found["held"]):
            continue
        out.append({**row, "coverage": coverage})
    unique = {(r["scope"], r["role_id"]): r for r in out}
    ranked = sorted(unique.values(), key=lambda r: _rank(r, required))
    return {"hints": ranked[:MAX_HINTS], "more": max(len(ranked) - MAX_HINTS, 0), "activate_url": ACTIVATE_URL,
            "error": found.get("error")}


def in_scope(scope, subscriptions, groups=None):
    """What a role at `scope` covers of the scope in use: the subscriptions, and resource group IDs when the
    scope is narrowed to some. Empty when it covers none of it."""
    subs = {s.lower() for s in subscriptions or []}
    if scope == "/":
        return "everything"
    if "/providers/microsoft.management/managementgroups/" in scope:
        return "may cover (management group)"
    parts = scope.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "subscriptions" or parts[1] not in subs:
        return ""
    if len(parts) == 2:
        if groups is None:
            return "everything in it"
        inside = [g for g in groups if g.lower().startswith(f"/subscriptions/{parts[1]}/")]
        return f"{len(inside)} resource group(s)" if inside else ""
    rg = "/".join(parts[:4])
    if groups is not None and not any(g.lower() == f"/{rg}" for g in groups):
        return ""
    return "1 resource group" if len(parts) == 4 else "1 resource"


def active(found):
    """The activations running now, named from the eligibilities they came from."""
    out = []
    for h in found["held"]:
        source = next((r for r in found["eligible"] if r["role_id"] == h["role_id"] and covers(r["scope"], h["scope"])), None)
        out.append({"role": source["role"] if source else h["role_id"], "scope_id": h["scope"],
                    "scope_name": h["scope"].rstrip("/").rsplit("/", 1)[-1], "scope_type": scopes.kind(h["scope"]),
                    "via": source["via"] if source else "", "until": h["end"]})
    return sorted(out, key=lambda a: a["until"])


def current(found, text="", subscriptions=None, groups=None, scope_only=False):
    """The roles you hold now (standing, or a PIM activation with its end), filtered like `listing`."""
    words, rows = text.lower().split(), []
    for row in found.get("standing") or []:
        role, via = row.get("role") or row["role_id"], row.get("via") or ""
        scope_name = row["scope"].rstrip("/").rsplit("/", 1)[-1] or "/"
        if not all(w in f"{role} {scope_name} {row['scope']} {via}".lower() for w in words):
            continue
        cover = in_scope(row["scope"], subscriptions, groups) if subscriptions else ""
        if scope_only and subscriptions and not cover:
            continue
        running = next((h for h in found["held"] if h["role_id"] == row["role_id"] and h["scope"] == row["scope"]), None)
        rows.append({"role": role, "role_id": row["role_id"], "scope": row["scope"], "scope_name": scope_name,
                     "scope_type": scopes.kind(row["scope"]), "via": via, "covers": cover,
                     "until": running["end"] if running else None})
    unique = {(r["scope"], r["role_id"], r["via"]): r for r in rows}
    return sorted(unique.values(), key=lambda r: (r["until"] is None, r["scope_type"] != "subscription",
                                                  r["scope_name"].lower(), r["role"].lower()))


def listing(found, text="", subscriptions=None, groups=None, scope_only=False):
    """Eligible roles, filtered by role, scope or group name and, with `scope_only`, to those covering the scope
    in use; ones activated now (there or below) marked active, ones a role you hold already covers marked held."""
    words = text.lower().split()
    rows = []
    for row in found["eligible"]:
        haystack = f"{row['role']} {row['scope_name']} {row['scope']} {row['via']}".lower()
        if not all(w in haystack for w in words):
            continue
        cover = in_scope(row["scope"], subscriptions, groups) if subscriptions else ""
        if scope_only and subscriptions and not cover:
            continue
        running = next((h for h in found["held"] if h["role_id"] == row["role_id"] and covers(row["scope"], h["scope"])), None)
        owned = None if running else holds(found, row["role_id"], row["scope"])
        rows.append({**row, "covers": cover,
                     "active": running and {"until": running["end"], "child": running["scope"] != row["scope"]},
                     "held": owned and {"via": owned.get("via") or "", "scope": owned["scope"], "kind": scopes.kind(owned["scope"]),
                                        "same": owned["scope"] == row["scope"]}})
    rows.sort(key=lambda r: (not r["active"], bool(r["held"]), "reader" not in r["role"].lower(),
                             r["scope_type"] != "subscription", r["scope_name"].lower(), r["role"].lower()))
    now = current(found, text, subscriptions, groups, scope_only)
    return {"rows": rows[:MAX_LIST], "total": len(found["eligible"]), "matched": len(rows),
            "current": now[:MAX_LIST], "current_matched": len(now), "current_total": len(found.get("standing") or []),
            "held": sum(1 for r in rows if r["held"]), "active": active(found), "activate_url": ACTIVATE_URL,
            "error": found.get("error"), "standing_error": found.get("standing_error")}
