"""VNet address space and subnet IP usage: allocated, free, sizes that still fit, how full each subnet is.
Two Resource Graph queries cover every VNet in scope; opening one adds Azure's own `usages` list."""
import ipaddress

from .arm import ArmError, kql_string, rg_clause
from .model import CRITICAL, HEALTHY, INFO, UNKNOWN, WARNING, red_or_yellow

NETWORK_API_VERSION = "2024-05-01"
# Azure reserves 5 addresses per IPv4 subnet; the smallest IPv4 subnet is /29, IPv6 subnets are /64.
AZURE_RESERVED_V4 = 5
MIN_SUBNET_PREFIX_V4 = 29
IPV6_SUBNET_PREFIX = 64
_ORDER = {CRITICAL: 0, WARNING: 1, HEALTHY: 2, UNKNOWN: 3, INFO: 4}  # INFO: a VNet without subnets, nothing to act on

_VNETS = ("Resources | where type =~ 'microsoft.network/virtualnetworks'{clause}"
          " | project id, name, resourceGroup, subscriptionId, location,"
          " spaces = properties.addressSpace.addressPrefixes, subnetCount = array_length(properties.subnets)")
_SUBNETS = ("Resources | where type =~ 'microsoft.network/virtualnetworks'{clause}"
            " | mv-expand s = properties.subnets | where isnotempty(s.name)"
            " | extend p = s.properties"
            " | project vnetId = id, vnet = name, resourceGroup, subscriptionId, subnet = tostring(s.name),"
            " subnetId = tostring(s.id), prefix = tostring(p.addressPrefix), prefixes = p.addressPrefixes,"
            " used = array_length(p.ipConfigurations), links = array_length(p.serviceAssociationLinks),"
            " nsg = tostring(p.networkSecurityGroup.id), routeTable = tostring(p.routeTable.id),"
            " delegations = p.delegations")


def usable(net):
    return max(net.num_addresses - AZURE_RESERVED_V4, 0) if net.version == 4 else net.num_addresses


def _short(resource_id):
    return resource_id.rstrip("/").rsplit("/", 1)[-1] if resource_id else ""


def _networks(values):
    found = []
    for value in values or []:
        try:
            found.append(ipaddress.ip_network(str(value), strict=False))
        except ValueError:
            continue
    return found


def free_blocks(space, used):
    """The unallocated part of `space` as the fewest CIDR blocks."""
    free = [space]
    for block in used:
        remaining = []
        for candidate in free:
            if block.version != candidate.version:
                remaining.append(candidate)
            elif block.subnet_of(candidate):
                remaining.extend(candidate.address_exclude(block))
            elif not candidate.subnet_of(block):
                remaining.append(candidate)
        free = remaining
    return sorted(ipaddress.collapse_addresses(free))


def subnet_status(used, usable_ips, delegated, warn_pct, crit_pct):
    """(status, percent) for one subnet's IP usage; delegated subnets don't report it."""
    if used is None:
        return (INFO if delegated else UNKNOWN), None
    if not usable_ips:
        return INFO, None
    percent = round(100 * used / usable_ips, 1)
    if percent >= crit_pct:
        return CRITICAL, percent
    if percent >= warn_pct:
        return WARNING, percent
    return HEALTHY, percent


def _subnet(row, warn_pct, crit_pct, used_override=None, counts_reported=True):
    prefixes = _networks(row.get("prefixes") or ([row["prefix"]] if row.get("prefix") else []))
    usable_ips = sum(usable(n) for n in prefixes)
    delegations = ", ".join((d.get("properties") or {}).get("serviceName") or d.get("serviceName") or ""
                            for d in row.get("delegations") or [] if isinstance(d, dict))
    used = used_override if used_override is not None else row.get("used")
    if used is None and counts_reported and not row.get("links") and not delegations:
        used = 0  # Resource Graph omits an empty ipConfigurations list
    status, percent = subnet_status(used, usable_ips, bool(delegations or row.get("links")), warn_pct, crit_pct)
    note = ""
    if status == INFO and (delegations or row.get("links")):
        note = "delegated to a service, which doesn't report per-IP usage here"
    return {"name": row["subnet"], "id": row.get("subnetId") or "", "cidrs": [str(n) for n in prefixes],
            "networks": prefixes, "total": sum(n.num_addresses for n in prefixes), "usable": usable_ips,
            "used": used, "percent": percent, "status": status, "note": note,
            "nsg": _short(row.get("nsg")), "route_table": _short(row.get("routeTable")), "delegations": delegations}


def subnet_rows(client, subscriptions, group_keys=None):
    return client.query(_SUBNETS.format(clause=rg_clause(group_keys)), subscriptions=list(subscriptions) or None)


def overview(client, subscriptions, group_keys, warn_pct, crit_pct):
    """Every VNet in scope: address space allocated, and its fullest subnets."""
    clause = rg_clause(group_keys)
    vnets = client.query(_VNETS.format(clause=clause), subscriptions=list(subscriptions) or None)
    rows = subnet_rows(client, subscriptions, group_keys)
    reported = any(row.get("used") is not None for row in rows)
    by_vnet = {}
    for row in rows:
        by_vnet.setdefault(row["vnetId"].lower(), []).append(
            _subnet(row, warn_pct, crit_pct, counts_reported=reported))
    out = []
    for vnet in vnets:
        spaces = _networks(vnet.get("spaces"))
        subnets = by_vnet.get(vnet["id"].lower(), [])
        total = sum(n.num_addresses for n in spaces)
        allocated = sum(n.num_addresses for s in subnets for n in s["networks"]
                        if any(n.version == sp.version and n.subnet_of(sp) for sp in spaces))
        ranked = sorted((s for s in subnets if s["percent"] is not None), key=lambda s: -s["percent"])
        statuses = [s["status"] for s in subnets]
        status = red_or_yellow(statuses, HEALTHY if subnets else INFO)
        out.append({
            "id": vnet["id"], "name": vnet["name"], "resource_group": vnet["resourceGroup"],
            "subscription_id": vnet["subscriptionId"].lower(), "location": vnet.get("location"),
            "spaces": [str(n) for n in spaces], "subnets": len(subnets), "status": status,
            "allocated_percent": round(100 * allocated / total, 1) if total else None,
            "fullest": [{"name": s["name"], "percent": s["percent"], "used": s["used"], "usable": s["usable"],
                         "status": s["status"]} for s in ranked[:3]],
            "problem_subnets": sum(1 for s in statuses if s in (CRITICAL, WARNING)),
        })
    out.sort(key=lambda v: (_ORDER.get(v["status"], 5), -(v["allocated_percent"] or 0), v["name"]))
    return {"vnets": out, "warn_percent": warn_pct, "critical_percent": crit_pct, "usage_reported": reported}


def _usages(client, vnet_id):
    """Azure's per-subnet usage count, keyed by subnet ID; empty when it can't be read."""
    try:
        values = client.get_paged(f"{vnet_id}/usages", NETWORK_API_VERSION)
    except ArmError:
        return {}
    return {str(v.get("id") or "").lower(): int(v.get("currentValue") or 0) for v in values if v.get("id")}


def available_sizes(spaces):
    """For each prefix length that still fits, the first free CIDR and how many fit."""
    rows = []
    for space in spaces:
        free = space["free_networks"]
        if not free:
            continue
        cidr = ipaddress.ip_network(space["cidr"])
        if cidr.version == 6:
            lengths = [IPV6_SUBNET_PREFIX] if min(f.prefixlen for f in free) <= IPV6_SUBNET_PREFIX else []
        else:
            lengths = range(min(f.prefixlen for f in free), MIN_SUBNET_PREFIX_V4 + 1)
        for length in lengths:
            fitting = [f for f in free if f.prefixlen <= length]
            if not fitting:
                continue
            first = next(fitting[0].subnets(new_prefix=length))
            rows.append({"address_space": space["cidr"], "prefix": f"/{length}", "next_cidr": str(first),
                         "count": sum(2 ** (length - f.prefixlen) for f in fitting), "usable_ips": usable(first)})
    return rows


def vnet_detail(client, vnet_id, warn_pct, crit_pct):
    """One VNet: address spaces, every subnet with its IP usage, free ranges and the sizes that still fit."""
    vnet_id = vnet_id.rstrip("/")
    clause = f" | where tolower(id) == {kql_string(vnet_id.lower())}"
    vnets = client.query(_VNETS.format(clause=clause))
    if not vnets:
        raise ArmError("That VNet wasn't found, or your session can't read it.", 404)
    vnet = vnets[0]
    usages = _usages(client, vnet_id)
    subnets = [_subnet(row, warn_pct, crit_pct, usages.get((row.get("subnetId") or "").lower()))
               for row in client.query(_SUBNETS.format(clause=clause))]
    subnets.sort(key=lambda s: s["name"].lower())

    spaces, outside = [], []
    space_networks = _networks(vnet.get("spaces"))
    for space in space_networks:
        inside = [n for s in subnets for n in s["networks"] if n.version == space.version and n.subnet_of(space)]
        free = free_blocks(space, inside)
        allocated = sum(n.num_addresses for n in inside)
        spaces.append({"cidr": str(space), "total": space.num_addresses, "allocated": allocated,
                       "free": sum(f.num_addresses for f in free), "percent": round(100 * allocated / space.num_addresses, 1),
                       "subnets": sorted({s["name"] for s in subnets for n in s["networks"]
                                          if n.version == space.version and n.subnet_of(space)}),
                       "blocks": [{"cidr": str(n), "subnet": s["name"]} for s in subnets for n in s["networks"]
                                  if n.version == space.version and n.subnet_of(space)],
                       "free_networks": free, "free_ranges": [str(f) for f in free]})
    for s in subnets:
        for n in s["networks"]:
            if not any(n.version == sp.version and n.subnet_of(sp) for sp in space_networks):
                outside.append(f"{s['name']} {n}")
    sizes = available_sizes(spaces)
    for space in spaces:
        space.pop("free_networks")
    for s in subnets:
        s.pop("networks")
    return {"id": vnet["id"], "name": vnet["name"], "resource_group": vnet["resourceGroup"],
            "subscription_id": vnet["subscriptionId"].lower(), "location": vnet.get("location"),
            "spaces": spaces, "subnets": subnets, "outside": outside, "sizes": sizes,
            "usage_source": "Azure's subnet usage list" if usages else "Resource Graph (IP configurations)",
            "warn_percent": warn_pct, "critical_percent": crit_pct}
