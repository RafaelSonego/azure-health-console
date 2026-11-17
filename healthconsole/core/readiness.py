"""Pre-peak readiness: "if traffic doubles before the peak ends, what stops us?" for the whole scope.
Resource Graph and quota reads are always run; Kubernetes and Key Vault (one request each) are the deep part."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from . import network, triage
from .cache import TtlCache
from .arm import ArmError, kql_list, rg_clause
from .checks import keyvault_scan, summarise_errors
from .explore import aks, aks_checks
from .explore.scaling import NEAR_MAX, _status
from .model import CRITICAL, HEALTHY, INFO, UNKNOWN, WARNING, red_or_yellow

COMPUTE_API_VERSION = "2024-07-01"
SKUS_API_VERSION = "2021-07-01"
SKU_CACHE_SECONDS = 24 * 3600
MAX_ITEMS = 500
MAX_PARALLEL = 8
LONG_EXPIRED_DAYS = 90
_ORDER = {CRITICAL: 0, WARNING: 1, UNKNOWN: 2, INFO: 3, HEALTHY: 4}
_SEVERITY = {"HIGH": CRITICAL, "MEDIUM": WARNING, "LOW": INFO, "INFO": INFO}
# Plans Azure scales on its own (consumption, elastic premium, Logic Apps Standard) or that can't scale out.
_ELASTIC_TIERS = ("dynamic", "elasticpremium", "flexconsumption", "workflowstandard", "free", "shared")
_sku_cache = TtlCache("VM sizes per region", SKU_CACHE_SECONDS, access_bound=False)
_sku_locks = {}
_sku_locks_guard = threading.Lock()


def _item(row, status, detail, fix="", name=None, display_type=None):
    item = triage._item(row, status, detail)
    if name:
        item["name"] = name
    if display_type:
        item["display_type"] = display_type
    item["fix"] = fix
    return item


def _sorted(items):
    return sorted(items, key=lambda i: (_ORDER.get(i["status"], 5), i["name"].lower()))[:MAX_ITEMS]


# ------------------------------------------------------------------ Resource Graph inputs

def _pools(client, scope, clause):
    """One row per AKS node pool, with the cluster's network settings (what decides IPs per node)."""
    return client.query(
        "Resources | where type =~ 'microsoft.containerservice/managedclusters'"
        f"{clause}"
        " | extend net = properties.networkProfile"
        " | mv-expand pool = properties.agentPoolProfiles"
        " | project id, name, type, kind, resourceGroup, subscriptionId, location,"
        " plugin = tolower(tostring(net.networkPlugin)), pluginMode = tolower(tostring(net.networkPluginMode)),"
        " pool = tostring(pool.name), mode = tostring(pool.mode), vmSize = tostring(pool.vmSize),"
        " autoscale = tobool(pool.enableAutoScaling), nodes = toint(pool['count']), minCount = toint(pool.minCount),"
        " maxCount = toint(pool.maxCount), maxPods = toint(pool.maxPods), priority = tostring(pool.scaleSetPriority),"
        " subnetId = tolower(tostring(pool.vnetSubnetID)), podSubnetId = tolower(tostring(pool.podSubnetID)),"
        " power = tostring(pool.powerState.code)", subscriptions=scope)


def _autoscaled(client, scope, clause):
    """Every autoscale setting and the resource it scales, with that resource's current capacity."""
    return client.query(
        "Resources | where type =~ 'microsoft.insights/autoscalesettings'"
        " | extend target = tolower(tostring(properties.targetResourceUri)), enabled = tobool(properties.enabled)"
        " | mv-expand p = properties.profiles"
        " | summarize maxCap = max(toint(p.capacity.maximum)) by target, setting = name, enabled"
        " | join kind=inner (Resources | project target = tolower(id), id, name, type, kind, resourceGroup,"
        " subscriptionId, location, cap = toint(sku.capacity), vmSize = tostring(sku.name),"
        " priority = tostring(properties.virtualMachineProfile.priority)) on target"
        f"{clause}"
        " | project id, name, type, kind, resourceGroup, subscriptionId, location, cap, maxCap, setting, enabled,"
        " vmSize, priority", subscriptions=scope)


def _plans(client, scope, clause):
    return client.query(
        "Resources | where type =~ 'microsoft.web/serverfarms'"
        f" | where tolower(tostring(sku.tier)) !in ({kql_list(_ELASTIC_TIERS)}){clause}"
        " | where toint(properties.numberOfSites) > 0"
        " | project id, name, type, kind, resourceGroup, subscriptionId, cap = toint(sku.capacity),"
        " sku = tostring(sku.name), sites = toint(properties.numberOfSites)", subscriptions=scope)


# ------------------------------------------------------------------ 1. scaling headroom

def _pool_name(row):
    return f"{row['name']} / {row['pool']}"


def scaling_section(pools, autoscaled, plans):
    items = []
    targets = set()
    for row in autoscaled:
        targets.add(row["id"].lower())
        if row["enabled"] is False:
            items.append(_item(row, WARNING, f"Autoscale '{row['setting']}' is disabled; capacity stays at {row['cap']}.",
                               "Enable the autoscale setting, or scale out by hand before the peak."))
            continue
        status = _status(row["cap"], row["maxCap"])
        fix = f"Raise the maximum of autoscale '{row['setting']}' (and check quota)." if status in (CRITICAL, WARNING) else ""
        items.append(_item(row, status, f"{row['cap']} of max {row['maxCap']} instances (autoscale '{row['setting']}')", fix))
    for row in pools:
        if row["autoscale"]:
            status = _status(row["nodes"], row["maxCount"])
            detail = f"{row['nodes']} of max {row['maxCount']} nodes ({row['vmSize']}, autoscaler {row['minCount']}–{row['maxCount']})"
            fix = "Raise the pool's maxCount (check vCPU quota and subnet IPs below)." if status in (CRITICAL, WARNING) else ""
        else:
            status = WARNING if row["mode"] != "System" else INFO
            detail = (f"{row['nodes']} node{'' if row['nodes'] == 1 else 's'}, fixed ({row['vmSize']}, autoscaling "
                      f"disabled, {row['mode']} pool)")
            fix = "Enable the cluster autoscaler on this pool, or scale it out before the peak." if status == WARNING else ""
        if row.get("power") == "Stopped":
            status, detail = WARNING, detail + "; pool is stopped"
        items.append(_item(row, status, detail, fix, name=_pool_name(row), display_type="AKS node pool"))
    for row in plans:
        if row["id"].lower() not in targets:
            items.append(_item(row, INFO, f"{row['cap']} instance(s) of {row['sku']}, no autoscale setting; "
                                          f"{row['sites']} app(s) on it"))
    return items


# ------------------------------------------------------------------ 2. vCPU quota

def _vm_sizes(client, subscription, location):
    """{vm size (lower): (family, vCPUs)} for one region, from the SKU list; cached for a day."""
    with _sku_locks_guard:
        lock = _sku_locks.setdefault(location, threading.Lock())
    with lock:  # parallel subscriptions in one region share one fetch
        return _sku_cache.get_or_load(location, lambda: _fetch_vm_sizes(client, subscription, location))


def _fetch_vm_sizes(client, subscription, location):
    values = client.get_paged(f"/subscriptions/{subscription}/providers/Microsoft.Compute/skus", SKUS_API_VERSION,
                              params={"$filter": f"location eq '{location}'"})
    sizes = {}
    for sku in values:
        if sku.get("resourceType") != "virtualMachines" or not sku.get("name"):
            continue
        caps = {c.get("name"): c.get("value") for c in sku.get("capabilities") or []}
        try:
            vcpus = int(caps.get("vCPUs") or 0)
        except ValueError:
            vcpus = 0
        sizes[sku["name"].lower()] = (sku.get("family") or "", vcpus)
    return sizes


def _demands(pools, autoscaled):
    """[(subscription, location, vm size, extra instances at max, spot, who)] for whatever can still scale out."""
    out = []
    for row in pools:
        if row["autoscale"] and row["maxCount"]:
            out.append((row["subscriptionId"].lower(), (row["location"] or "").lower(), row["vmSize"],
                        max(row["maxCount"] - (row["nodes"] or 0), 0), row["priority"] == "Spot", _pool_name(row)))
    for row in autoscaled:
        if (row["type"] or "").lower() == "microsoft.compute/virtualmachinescalesets" and row["enabled"] is not False:
            out.append((row["subscriptionId"].lower(), (row["location"] or "").lower(), row["vmSize"],
                        max((row["maxCap"] or 0) - (row["cap"] or 0), 0), row["priority"] == "Spot", row["name"]))
    return out


def _quota_row(sub, location, label, usage, need, who):
    current, limit = int(usage.get("currentValue") or 0), int(usage.get("limit") or 0)
    free = limit - current
    if need > free:
        status = CRITICAL
    elif limit and current + need >= NEAR_MAX * limit:
        status = WARNING
    else:
        status = HEALTHY
    detail = f"{current} of {limit} vCPUs in use, {free} free"
    if need:
        detail += f"; {', '.join(sorted(who)[:4])}{'…' if len(who) > 4 else ''} can add up to {need} at their maximum"
    fix = (f"Request more {label} quota in {location} (Portal → Quotas), or lower the maximums."
           if status != HEALTHY else "")
    return {"id": f"/subscriptions/{sub}", "name": f"{label} · {location}", "display_type": "vCPU quota",
            "resource_group": "", "subscription_id": sub, "status": status, "detail": detail, "fix": fix}


def quota_section(client, pools, autoscaled):
    """Free vCPUs per subscription, region and VM family against what scope could add at its maximums."""
    demands = _demands(pools, autoscaled)
    regions = sorted({(d[0], d[1]) for d in demands if d[1]})
    items, unchecked = [], []

    def one(key):
        sub, location = key
        try:
            usages = client.get_paged(f"/subscriptions/{sub}/providers/Microsoft.Compute/locations/{location}/usages",
                                      COMPUTE_API_VERSION)
            sizes = _vm_sizes(client, sub, location)
        except ArmError as error:
            return [], [f"{sub} in {location}: {str(error).splitlines()[0]} (needs Reader on the subscription)"]
        by_name = {(u.get("name") or {}).get("value", "").lower(): u for u in usages}
        families, total, spot, rows, notes = {}, [0, set()], [0, set()], [], []
        for d_sub, d_loc, size, extra, is_spot, who in demands:
            if (d_sub, d_loc) != key:
                continue
            family, vcpus = sizes.get((size or "").lower(), ("", 0))
            if not vcpus:
                notes.append(f"{who}: VM size {size} isn't in {location}'s SKU list, so its quota wasn't counted")
                continue
            if is_spot:
                spot[0] += extra * vcpus
                spot[1].add(who)
                continue
            entry = families.setdefault(family.lower(), [0, set(), family])
            entry[0] += extra * vcpus
            entry[1].add(who)
            total[0] += extra * vcpus
            total[1].add(who)
        if "cores" in by_name:
            rows.append(_quota_row(sub, location, "Total regional vCPUs", by_name["cores"], total[0], total[1]))
        for key_name, (need, who, family) in sorted(families.items()):
            usage = by_name.get(key_name)
            if usage:
                rows.append(_quota_row(sub, location, (usage.get("name") or {}).get("localizedValue") or family,
                                       usage, need, who))
            else:
                notes.append(f"{', '.join(sorted(who))}: no quota entry for family {family} in {location}")
        if spot[0] and "lowprioritycores" in by_name:
            rows.append(_quota_row(sub, location, "Spot vCPUs", by_name["lowprioritycores"], spot[0], spot[1]))
        return rows, notes

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        for rows, notes in pool.map(one, regions):
            items += rows
            unchecked += notes
    return items, unchecked


# ------------------------------------------------------------------ 3. subnets

def ips_per_node(row):
    """(node-subnet IPs, pod-subnet IPs) one more node takes: Azure CNI without overlay gives every pod a
    node-subnet IP; overlay, kubenet and a separate pod subnet take one node IP."""
    pods = row.get("maxPods") or 30
    if row.get("podSubnetId"):
        return 1, pods
    if row.get("plugin") == "azure" and row.get("pluginMode") != "overlay":
        return pods + 1, 0
    return 1, 0


def subnet_section(client, scope, clause, pools, options):
    warn, crit = options["subnet_warning_percent"], options["subnet_critical_percent"]
    items = triage._subnets(client, scope, clause, options)
    for item in items:
        item["fix"] = "Free IPs or move workloads to a larger subnet before anything has to scale into it."
    needs, notes = {}, []
    for row in pools:
        node_ips, pod_ips = ips_per_node(row)
        extra = max((row["maxCount"] or 0) - (row["nodes"] or 0), 0) if row["autoscale"] else 0
        if not row["subnetId"]:
            notes.append(f"{_pool_name(row)}: network managed by AKS (no subnet of yours to run out)")
            continue
        for subnet_id, per_node in ((row["subnetId"], node_ips), (row["podSubnetId"], pod_ips)):
            if subnet_id and per_node and extra:
                entry = needs.setdefault(subnet_id, [0, [], False])
                entry[0] += extra * per_node
                entry[1].append(f"{_pool_name(row)} (+{extra} node{'' if extra == 1 else 's'} × {per_node} "
                                f"IP{'' if per_node == 1 else 's'})")
                entry[2] = entry[2] or per_node > 1
    if not needs:
        return items, notes
    vnets = sorted({s.split("/subnets/")[0] for s in needs})
    rows = client.query(network._SUBNETS.format(clause=f" | where tolower(id) in ({kql_list(vnets)})"))
    found = {(r.get("subnetId") or "").lower(): r for r in rows}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        usages = {}
        for result in pool.map(lambda v: network._usages(client, v), vnets):
            usages.update(result)
    for subnet_id, (need, who, per_pod) in sorted(needs.items()):
        row = found.get(subnet_id)
        if not row:
            notes.append(f"Subnet {subnet_id.rsplit('/', 1)[-1]} used by {', '.join(who)} isn't readable with this session")
            continue
        subnet = network._subnet(row, warn, crit, usages.get(subnet_id))
        if subnet["used"] is None or not subnet["usable"]:
            notes.append(f"{row['vnet']} / {subnet['name']}: IP usage not reported, so AKS headroom wasn't checked")
            continue
        free = subnet["usable"] - subnet["used"]
        if need > free:
            status = CRITICAL
        elif 100 * (subnet["used"] + need) / subnet["usable"] >= crit:
            status = WARNING
        else:
            status = HEALTHY
        fix = ""
        if status != HEALTHY:
            fix = ("Lower the pools' maxCount, add a pod subnet or move to Azure CNI Overlay" if per_pod else
                   "Lower the pools' maxCount or give the subnet more addresses")
            fix += "; a full subnet stops scale-out and upgrades (surge nodes need IPs too)."
        items.append({"id": row["vnetId"], "name": f"{row['vnet']} / {subnet['name']}", "display_type": "AKS subnet",
                      "resource_group": row["resourceGroup"], "subscription_id": row["subscriptionId"].lower(),
                      "status": status, "fix": fix,
                      "detail": f"{free} of {subnet['usable']} IPs free; at their maximum the pools need {need}: "
                                + ", ".join(who)})
    return items, notes


# ------------------------------------------------------------------ 4. single points of failure

def single_points_section(pools, autoscaled, plans):
    items = []
    scaled = {r["id"].lower() for r in autoscaled if r["enabled"] is not False}
    for row in plans:
        if (row["cap"] or 0) <= 1 and row["id"].lower() not in scaled:
            items.append(_item(row, WARNING, f"1 instance of {row['sku']}, no autoscale; {row['sites']} app(s) on it",
                               "Run at least 2 instances (and turn on zone redundancy where the tier allows)."))
    for row in pools:
        top = row["maxCount"] if row["autoscale"] else row["nodes"]
        if top == 1:
            what = "system pods" if row["mode"] == "System" else "its workloads"
            items.append(_item(row, WARNING, f"At most 1 node ({row['vmSize']}): {what} go down with that node",
                               "Allow at least 2 nodes (3 across zones for a system pool).",
                               name=_pool_name(row), display_type="AKS node pool"))
    return items


# ------------------------------------------------------------------ 4 + 5 from Kubernetes

def _cluster_ids(pools):
    seen = {}
    for row in pools:
        seen.setdefault(row["id"], row)
    return list(seen.values())


def kubernetes_sections(client, pools, kube_settings, progress):
    """(single-point items, HPA items, capacity items, notes) from each cluster's workloads, HPAs, PDBs and nodes."""
    clusters = _cluster_ids(pools)
    done = [0]

    def one(row):
        result = aks.kubernetes(client, row["id"], kube_settings=kube_settings)
        done[0] += 1
        progress(f"Kubernetes: cluster {done[0]} of {len(clusters)}")
        return row, result

    singles, hpas, capacity, notes = [], [], [], []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        outcomes = list(pool.map(one, clusters))
    for row, result in outcomes:
        if not result.get("available"):
            notes.append(f"{row['name']}: Kubernetes not readable ({str(result.get('reason') or '').splitlines()[0][:200]})")
            continue
        for note in result.get("unreadable") or []:
            if note["list"] in ("hpas", "pdbs", "deployments", "statefulsets"):
                notes.append(f"{row['name']}: can't list {note['list']}, so {note['effect']} ({note['reason']})")
        by_workload = {}
        for f in result["findings"]:
            where = f"{row['name']} / {f['namespace']}/{f['workload']}"
            text = f"{f['problem']}: {f['why']}" if f["why"] else f["problem"]
            if f["category"] == "capacity":
                capacity.append(_item(row, _SEVERITY[f["severity"]], text, f["action"], name=row["name"],
                                      display_type="AKS capacity"))
            elif f["category"] == "availability" and f["kind"] == aks_checks.CONFIG:
                singles.append(_item(row, _SEVERITY[f["severity"]], text, f["action"], name=where,
                                     display_type="Kubernetes workload"))
            elif f["category"] in ("hpa", "scaling") and f["workload"]:
                by_workload.setdefault((f["namespace"], f["workload"]), []).append(f)
        for hpa in result.get("hpas") or []:
            found = by_workload.pop((hpa["namespace"], hpa["workload"]), [])
            status = _status(hpa["current"], hpa["max"])
            worst = min([_ORDER[status]] + [_ORDER[_SEVERITY[f["severity"]]] for f in found])
            status = next(s for s, rank in _ORDER.items() if rank == worst)
            detail = f"{hpa['current']} of max {hpa['max']} replicas (min {hpa['min']}); {hpa['targets']}"
            if found:
                detail += "; " + "; ".join(f["problem"] for f in found)
            fix = " ".join(dict.fromkeys(f["action"] for f in found if f["action"])) or (
                "Raise maxReplicas, and make sure the node pool can fit the extra pods." if status != HEALTHY else "")
            hpas.append(_item(row, status, detail, fix, name=f"{row['name']} / {hpa['namespace']}/{hpa['workload']}",
                              display_type="HPA"))
        for (namespace, workload), found in by_workload.items():
            worst = min(found, key=lambda f: aks_checks.SEVERITIES.index(f["severity"]))
            hpas.append(_item(row, _SEVERITY[worst["severity"]], "; ".join(f["problem"] for f in found),
                              worst["action"], name=f"{row['name']} / {namespace}/{workload}",
                              display_type="Kubernetes workload"))
    return singles, hpas, capacity, notes


# ------------------------------------------------------------------ 6. certificates

def certificate_section(client, subscriptions, group_keys, peak_end, warn_days, include_secrets, progress, today=None):
    """Judged against the end of the peak: anything that expires before it is red."""
    today = today or datetime.now(timezone.utc).date()
    to_end = (peak_end - today).days
    scan = keyvault_scan(client, subscriptions, warn_days, group_keys, progress)
    items, counts = [], {"healthy": 0, "no_expiry": 0, "long_expired": 0}
    for row in scan["rows"]:
        if (row["kind"] != "certificate" and not include_secrets) or row.get("enabled") is False:
            continue
        days = row["days_left"]
        if days is None:
            counts["no_expiry"] += 1
            continue
        if days < -LONG_EXPIRED_DAYS:
            counts["long_expired"] += 1
            continue
        if days < 0:
            status, detail = CRITICAL, f"expired {-days} day(s) ago ({row['expires']})"
        elif days <= to_end:
            status, detail = CRITICAL, f"expires {row['expires']}, before the peak ends"
        elif days <= to_end + warn_days:
            status, detail = WARNING, f"expires {row['expires']}, {days - to_end} day(s) after the peak ends"
        else:
            counts["healthy"] += 1
            continue
        items.append({"id": row["vault_id"], "name": f"{row['vault']} / {row['name']}", "display_type": row["kind"].title(),
                      "resource_group": row["resource_group"], "subscription_id": row["subscription_id"],
                      "status": status, "detail": detail + (f"; {row['note']}" if "Key Vault" in row["note"] else ""),
                      "fix": "Renew it and roll it out before the peak (a change freeze may block it later)."})
    notes = [f"Key Vault: {e}" for e in summarise_errors(scan["errors"])]
    summary = (f"{scan['vaults']} vault(s) and App Service certificates read; {counts['healthy']} fine past the peak, "
               f"{counts['no_expiry']} without an expiry date, {counts['long_expired']} expired over "
               f"{LONG_EXPIRED_DAYS} days ago (left out as unused).")
    return items, notes, summary


# ------------------------------------------------------------------ the report

def parse_peak_end(text, today=None):
    today = today or datetime.now(timezone.utc).date()
    try:
        peak_end = date.fromisoformat(str(text or ""))
    except ValueError:
        raise ValueError("Peak ends on must be a date (YYYY-MM-DD).") from None
    if peak_end < today:
        raise ValueError(f"Peak ends on ({peak_end}) is in the past.")
    return peak_end


SECTIONS = [
    ("scaling", "Scaling headroom", "Autoscale settings, AKS node pools and App Service plans: how far each can still scale out."),
    ("quota", "vCPU quota", "Free vCPUs per subscription, region and VM family, against what the pools and scale sets in "
              "scope could add at their maximum. Quota is shared with anything else in the subscription."),
    ("subnets", "Subnet IPs", "Subnets running low, and whether each AKS subnet can hold its pools at their maximum."),
    ("single_points", "Single points of failure", "Single-instance plans and pools, and (deep) one-replica workloads, "
                                                  "HPAs with minReplicas 1 and PDBs that block drains."),
    ("hpas", "HPAs", "Every HorizontalPodAutoscaler: replicas against its maximum, and HPAs that can't scale (deep)."),
    ("certificates", "Certificates", "Certificates that expire before the peak ends, or soon after it (deep)."),
]


def run(client, subscriptions, options, peak_end, group_keys=None, deep=True, include_secrets=False,
        kube_settings=None, progress=None, today=None):
    """The report: sections of items (status, name, detail, fix) and, per section, what couldn't be checked."""
    report = progress or (lambda message: None)
    today = today or datetime.now(timezone.utc).date()
    scope, clause = list(subscriptions) or None, rg_clause(group_keys)
    sections = {key: {"key": key, "title": title, "description": desc, "items": [], "unchecked": [], "error": None,
                      "summary": "", "skipped": False} for key, title, desc in SECTIONS}

    def guarded(key, work):
        try:
            return work()
        except ArmError as error:
            sections[key]["error"] = str(error).splitlines()[0]
            return None

    report("Reading AKS pools, autoscale settings and App Service plans")
    pools = guarded("scaling", lambda: _pools(client, scope, clause)) or []
    autoscaled = guarded("scaling", lambda: _autoscaled(client, scope, clause)) or []
    plans = guarded("scaling", lambda: _plans(client, scope, clause)) or []
    sections["scaling"]["items"] = scaling_section(pools, autoscaled, plans)
    sections["single_points"]["items"] = single_points_section(pools, autoscaled, plans)

    report("Reading vCPU quotas")
    found = guarded("quota", lambda: quota_section(client, pools, autoscaled))
    if found:
        sections["quota"]["items"], sections["quota"]["unchecked"] = found

    report("Reading subnets")
    found = guarded("subnets", lambda: subnet_section(client, scope, clause, pools, options))
    if found:
        sections["subnets"]["items"], sections["subnets"]["unchecked"] = found

    if deep:
        report("Reading Kubernetes")
        singles, hpas, capacity, notes = kubernetes_sections(client, pools, kube_settings, report)
        sections["single_points"]["items"] += singles
        sections["scaling"]["items"] += capacity
        sections["hpas"]["items"], sections["hpas"]["unchecked"] = hpas, notes
        report("Reading Key Vaults")
        found = guarded("certificates", lambda: certificate_section(
            client, subscriptions, group_keys, peak_end, options["expiry_warning_days"], include_secrets, report, today))
        if found:
            sections["certificates"]["items"], sections["certificates"]["unchecked"], sections["certificates"]["summary"] = found
    else:
        for key in ("hpas", "certificates"):
            sections[key]["skipped"] = True

    out = []
    for key, _title, _desc in SECTIONS:
        section = sections[key]
        section["items"] = _sorted(section["items"])
        statuses = {i["status"] for i in section["items"]}
        section["status"] = red_or_yellow(statuses, UNKNOWN if section["error"] or section["unchecked"]
                                          else HEALTHY if section["items"] else None)
        out.append(section)
    counts = {s: sum(1 for sec in out for i in sec["items"] if i["status"] == s)
              for s in (CRITICAL, WARNING, HEALTHY, INFO)}
    return {"peak_end": peak_end.isoformat(), "days_to_peak_end": (peak_end - today).days, "deep": deep,
            "include_secrets": include_secrets, "subscriptions": len(subscriptions), "clusters": len(_cluster_ids(pools)),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "identity": getattr(client.identity, "name", ""), "sections": out, "counts": counts}


_LABELS = {CRITICAL: "critical", WARNING: "warning", HEALTHY: "healthy", INFO: "info", UNKNOWN: "unknown"}


def to_doc(result, sub_names=None, include_healthy=True):
    """The report as a `tables.render` document (Markdown / PDF)."""
    names = sub_names or {}
    c = result["counts"]
    subtitle = (f"Peak ends {result['peak_end']} ({result['days_to_peak_end']} day(s) away). "
                f"{result['subscriptions']} subscription(s), {result['clusters']} AKS cluster(s). "
                f"Taken {result['generated_at']} by {result['identity']}. "
                f"{c[CRITICAL]} critical, {c[WARNING]} warning, {c[HEALTHY]} healthy."
                + ("" if result["deep"] else " Kubernetes and Key Vault checks were not run."))
    sections = []
    for section in result["sections"]:
        rows = [i for i in section["items"] if include_healthy or i["status"] not in (HEALTHY, INFO)]
        note = " ".join(filter(None, [
            section["description"], section["summary"],
            "Not run (deep checks off)." if section["skipped"] else "",
            f"Couldn't read: {section['error']}" if section["error"] else ""]))
        sections.append({
            "heading": section["title"], "note": note, "columns": ["Status", "Name", "Type", "Where", "Detail", "Fix"],
            "widths": [7, 18, 9, 16, 30, 20], "status_column": 0,
            "rows": [[_LABELS.get(i["status"], i["status"]), i["name"], i["display_type"],
                      " / ".join(filter(None, [names.get(i["subscription_id"], i["subscription_id"]), i["resource_group"]])),
                      i["detail"], i.get("fix", "")] for i in rows]})
        if section["unchecked"]:
            sections.append({"heading": f"{section['title']}: not checked", "columns": ["What and why"],
                             "rows": [[n] for n in section["unchecked"]]})
    return {"title": "Pre-peak readiness", "subtitle": subtitle, "sections": sections}
