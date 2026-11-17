"""Deeper sweeps that need one request per resource, so they run on demand with progress:
secret, key and certificate expiry across many vaults, and the AKS fleet."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .arm import ArmError
from .explore import aks, detectors, keyvault
from .model import CRITICAL, HEALTHY, INFO, UNKNOWN, WARNING
from .triage import rg_clause

MAX_PARALLEL = 16
_ORDER = {CRITICAL: 0, WARNING: 1, UNKNOWN: 2, HEALTHY: 3, INFO: 4}


def _resources(client, resource_type, subscriptions, group_keys):
    return client.query(
        f"Resources | where type =~ '{resource_type}'{rg_clause(group_keys)}"
        " | project id, name, resourceGroup, subscriptionId, properties | order by name asc",
        subscriptions=list(subscriptions) or None)


def _run(items, work, progress, label):
    total, done, results = len(items), [0], []

    def one(item):
        result = work(item)
        done[0] += 1
        progress(f"{label} {done[0]} of {total}")
        return result

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        results = list(pool.map(one, items))
    return results


def count_vaults(client, subscriptions, group_keys=None):
    rows = client.query(
        f"Resources | where type =~ 'microsoft.keyvault/vaults'{rg_clause(group_keys)} | summarize n=count()",
        subscriptions=list(subscriptions) or None)
    return int(rows[0]["n"]) if rows else 0


def summarise_errors(errors):
    """Many vaults usually fail for the same reason; one line per reason, with a count."""
    reasons = {}
    for entry in errors:
        name, _, reason = entry.partition(": ")
        reasons.setdefault(reason or entry, []).append(name)
    return [f"{len(names)} vault(s): {reason} ({', '.join(names[:3])}{'…' if len(names) > 3 else ''})"
            for reason, names in sorted(reasons.items(), key=lambda kv: -len(kv[1]))]


def _app_service_certificates(client, subscriptions, group_keys, warn_days, critical_days):
    """App Service certificates carry their expiry in Resource Graph, so one query covers them all."""
    rows = client.query(
        f"Resources | where type =~ 'microsoft.web/certificates'{rg_clause(group_keys)}"
        " | project id, name, resourceGroup, subscriptionId, expires = tostring(properties.expirationDate),"
        " keyVault = tostring(properties.keyVaultId)", subscriptions=list(subscriptions) or None)
    now = datetime.now(timezone.utc)
    found = []
    for row in rows:
        exp = None
        try:
            exp = int(datetime.fromisoformat(row["expires"].replace("Z", "+00:00")).timestamp()) if row["expires"] else None
        except ValueError:
            pass
        entry = {"name": row["name"], "properties": {"attributes": {"enabled": True, "exp": exp}}}
        item = keyvault._item(entry, warn_days, now, critical_days)
        if row["keyVault"]:
            item["note"] += f" (from Key Vault {row['keyVault'].rsplit('/', 1)[-1]})"
        found.append({"source": "App Service", "vault": "App Service certificate", "vault_id": row["id"],
                      "resource_group": row["resourceGroup"], "subscription_id": row["subscriptionId"].lower(),
                      "kind": "certificate", **item})
    return found


def urgency(row):
    """Red first (most recently expired, then what expires soonest), then yellow, soonest first."""
    days = row["days_left"]
    if row["status"] == CRITICAL:
        return (0, -days) if days is not None and days < 0 else (1, days if days is not None else 0)
    return (2 + _ORDER.get(row["status"], 5), days if days is not None else 10**6)


def keyvault_scan(client, subscriptions, warn_days, group_keys=None, progress=None, critical_days=7):
    """Every secret, key and certificate expiry in scope: Key Vaults (one listing each) plus App Service
    certificates (one Resource Graph query)."""
    report = progress or (lambda message: None)
    report("Listing Key Vaults")
    vaults = _resources(client, "microsoft.keyvault/vaults", subscriptions, group_keys)

    def scan(vault):
        result = keyvault.expiry(client, vault["id"], warn_days, critical_days)
        rows = [{"source": "Key Vault", "vault": vault["name"], "vault_id": vault["id"],
                 "resource_group": vault["resourceGroup"], "subscription_id": vault["subscriptionId"].lower(),
                 "kind": kind.rstrip("s"), **item} for kind in keyvault.KINDS for item in result.get(kind) or []]
        error = result["errors"][0].split("\n")[0] if result["errors"] else None
        return rows, (vault["name"], error) if error else None

    outcomes = _run(vaults, scan, report, "Vault")
    rows = [row for found, _ in outcomes for row in found]
    errors = [f"{name}: {error}" for _, failed in outcomes if failed for name, error in [failed]]
    try:
        rows += _app_service_certificates(client, subscriptions, group_keys, warn_days, critical_days)
    except ArmError as error:
        errors.append(f"App Service certificates: {str(error).splitlines()[0]}")
    rows.sort(key=lambda r: (_ORDER.get(r["status"], 5), r["days_left"] if r["days_left"] is not None else 10**6))
    counts = {status: sum(1 for r in rows if r["status"] == status) for status in (CRITICAL, WARNING, HEALTHY, INFO)}
    counts["expired"] = sum(1 for r in rows if r["status"] == CRITICAL and r["days_left"] < 0)
    return {"vaults": len(vaults), "rows": rows, "counts": counts, "errors": errors, "warn_days": warn_days,
            "critical_days": critical_days}


def aks_fleet(client, subscriptions, group_keys=None, with_detectors=False, progress=None, only_id=None):
    """Every cluster: power, provisioning, version, pools at max, pods by phase, optionally detectors. `only_id`
    checks just that cluster (a cluster's own page)."""
    report = progress or (lambda message: None)
    report("Listing AKS clusters")
    clusters = _resources(client, "microsoft.containerservice/managedclusters", subscriptions, group_keys)
    if only_id:
        clusters = [c for c in clusters if c["id"].lower() == only_id.lower()]

    def check(cluster):
        props = cluster.get("properties") or {}
        row = {"id": cluster["id"], "name": cluster["name"], "resource_group": cluster["resourceGroup"],
               "version": props.get("currentKubernetesVersion"),
               "power": (props.get("powerState") or {}).get("code"),
               "provisioning": props.get("provisioningState"), "problems": []}
        if row["power"] != "Running":
            row["problems"].append((WARNING, f"power state {row['power']}"))
        if row["provisioning"] != "Succeeded":
            row["problems"].append((CRITICAL if row["provisioning"] == "Failed" else WARNING,
                                    f"provisioning {row['provisioning']}"))
        for pool in props.get("agentPoolProfiles") or []:
            if pool.get("enableAutoScaling") and pool.get("maxCount") and (pool.get("count") or 0) >= pool["maxCount"]:
                row["problems"].append((CRITICAL, f"pool {pool.get('name')} at max ({pool['maxCount']} nodes)"))
        try:
            for phase in aks.pod_phases(client, cluster["id"]):
                if phase["phase"] in aks.PROBLEM_PHASES:
                    row["problems"].append((WARNING, f"{phase['pods']} {phase['phase'].lower()} in {phase['namespace']}"))
        except ArmError as error:
            row["problems"].append((UNKNOWN, f"pods: {str(error).splitlines()[0]}"))
        if with_detectors:
            result = detectors.run(client, cluster["id"], words=("health", "risk"))
            for item in result.get("results", []):
                if item["status"] in (CRITICAL, WARNING):
                    row["problems"].append((WARNING, f"detector: {item['title']}"))
        statuses = [status for status, _ in row["problems"]]
        row["status"] = next((s for s in (CRITICAL, WARNING, UNKNOWN) if s in statuses), HEALTHY)
        row["problems"] = [text for _, text in sorted(row["problems"], key=lambda p: _ORDER[p[0]])]
        return row

    rows = _run(clusters, check, report, "Cluster")
    rows.sort(key=lambda r: (_ORDER.get(r["status"], 5), r["name"]))
    counts = {status: sum(1 for r in rows if r["status"] == status) for status in (CRITICAL, WARNING, UNKNOWN, HEALTHY)}
    return {"clusters": len(rows), "rows": rows, "counts": counts}
