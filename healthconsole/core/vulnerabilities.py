"""Vulnerabilities in a scope, read-only: Defender for Cloud's findings (one recommendation per vulnerable package, since
Microsoft retired sub-assessments), where they run and whether that's reachable, what Defender isn't scanning, and
platform checks that need no Defender (AKS version support and node images, runtimes past end of life, weak TLS)."""
import json
import re
from datetime import date, datetime, timezone

from . import cards
from .arm import AccessDenied, ArmError, kql_list, rg_clause
from .cache import TtlCache
from .explore.aks_checks import NODE_IMAGE_CRIT_DAYS, NODE_IMAGE_WARN_DAYS, node_image_age
from .model import CRITICAL, HEALTHY, INFO, WARNING

MAX_ROWS = 300
CVE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)
MAX_TERMS, MAX_TERM_LENGTH = 5, 100
CACHE_SECONDS = 24 * 3600
SEVERITIES = ("High", "Medium", "Low")
AKS_VERSIONS_API = "2024-02-01"
STACKS_API = "2023-12-01"
EOL_WARN_DAYS = 90
# Microsoft's assessment keys for "this machine isn't being scanned for vulnerabilities" (the same in every tenant).
NOT_SCANNED_KEYS = ("152f2a79-cfb0-5dcb-9aad-84b53398424f", "ffff0522-1e88-47fc-8382-2a80ba848f5d")
SCANNING_PLANS = {"VirtualMachines": "Defender for Servers (VM vulnerability scanning)",
                  "Containers": "Defender for Containers (image and running-container scanning)"}

# AKS versions per region and Azure's runtime lists: the same for everyone, so Refresh access keeps them.
_reference = TtlCache("AKS versions and runtime stacks", CACHE_SECONDS, access_bound=False)


def forget():
    _reference.clear()


# ------------------------------------------------------------------ Defender findings

def _base(filters=None):
    """One row per open finding (a vulnerable package on a resource), with its parent resource worked out: a finding on
    a running container or a registry image sits below its cluster or registry."""
    filters = filters or {}
    kql = (
        "securityresources | where type =~ 'microsoft.security/assessments'"
        " | where tostring(properties.status.code) =~ 'Unhealthy'"
        " | extend add = properties.additionalData"
        " | where isnotempty(tostring(add.CvesDetails)) or isnotempty(tostring(add.MaxCvssScore))"
        " | extend rid = tostring(properties.resourceDetails.Id) | extend parts = split(rid, '/')"
        " | extend parent = tolower(strcat_array(array_slice(parts, 0, 8), '/')),"
        " rtype = tolower(strcat(tostring(parts[6]), '/', tostring(parts[7]))), resourceGroup = tostring(parts[4])"
        " | extend child = iff(array_length(parts) > 9, tostring(properties.resourceDetails.ResourceName), ''),"
        " childKind = tostring(properties.resourceDetails.ResourceType)"
        " | extend scanners = parse_json(tostring(add.ScannersDetails))"
        " | extend software = coalesce(tostring(add.SoftwareName), tostring(properties.displayName)),"
        " severity = tostring(properties.metadata.severity),"
        " cvss = todouble(add.MaxCvssScore),"
        " fixed = coalesce(tostring(add.FixedVersion), tostring(scanners.mdvm.FixedVersion)),"
        " detected = coalesce(tostring(add.DetectedSoftwareVersions), tostring(scanners.mdvm.DetectedSoftwareVersions))"
        " | extend hasFix = isnotempty(fixed) or tostring(add.CvesDetails) contains 'FixAvailable'"
        " | extend live = childKind =~ 'K8s-container' or rtype in ('microsoft.compute/virtualmachines',"
        " 'microsoft.compute/virtualmachinescalesets', 'microsoft.hybridcompute/machines')")
    severities = [s for s in filters.get("severity") or [] if s in SEVERITIES]
    if severities:
        kql += f" | where severity in~ ({kql_list(severities)})"
    if filters.get("fixable"):
        kql += " | where hasFix"
    if filters.get("cvss_min"):
        kql += f" | where cvss >= {float(filters['cvss_min'])}"
    if filters.get("type"):
        kql += f" | where rtype == {json.dumps(filters['type'].lower())}"
    if filters.get("software"):
        kql += f" | where software == {json.dumps(filters['software'])}"
    if filters.get("running"):
        kql += " | where live"
    for term in search_terms(filters.get("q")):
        kql += f" | where {_term_clause(term)}"
    return kql


def search_terms(text):
    """Up to five words, each at most 100 characters; every one must match."""
    return [t[:MAX_TERM_LENGTH] for t in (text or "").split()][:MAX_TERMS]


def _term_clause(term):
    """A full CVE ID matches exactly (CVE-2026-1234, not CVE-2026-12345); anything else matches as part of the CVEs,
    package, resource, container or image, or a version. KQL's `contains` ignores case."""
    if CVE.match(term):
        quoted = json.dumps(json.dumps(term.upper()))  # the ID with its JSON quotes, as it sits in CvesDetails
        return f"tostring(add.CvesDetails) contains {quoted} or tostring(scanners) contains {quoted}"
    needle = json.dumps(term)
    fields = ("tostring(add.CvesDetails)", "software", "tostring(properties.displayName)", "parent", "child", "fixed",
              "detected")
    return " or ".join(f"{field} contains {needle}" for field in fields)


_COUNTS = ("high=countif(severity =~ 'High'), medium=countif(severity =~ 'Medium'), low=countif(severity =~ 'Low'),"
           " fixable=countif(hasFix), maxCvss=max(cvss)")


def _where(kind):
    return {"K8s-container": "running in AKS", ".containerimage": "image in a registry"}.get(kind, "")


def _exposure(client, ids):
    """{resource id: [labels]} for what can be reached from the internet: a VM's public IP, or public network access
    left on. An AKS cluster's labels say "API server", since a container's own exposure needs the Kubernetes API."""
    if not ids:
        return {}
    found = {i: [] for i in ids}
    rows = client.query(
        "Resources | where tolower(id) in ({ids})"
        " | project id=tolower(id), type=tolower(type), pna=tostring(properties.publicNetworkAccess),"
        " privateCluster=tostring(properties.apiServerAccessProfile.enablePrivateCluster)".format(ids=kql_list(ids)))
    for r in rows:
        if r["type"] == "microsoft.containerservice/managedclusters":
            if r.get("privateCluster", "").lower() != "true":
                found[r["id"]].append("API server public")
        elif (r.get("pna") or "").lower() == "enabled":
            found[r["id"]].append("public network access")
    vms = [i for i in ids if "/microsoft.compute/virtualmachines/" in i]
    if vms:
        for r in client.query(
                "Resources | where type =~ 'microsoft.network/networkinterfaces'"
                " | mv-expand ipc = properties.ipConfigurations"
                " | where isnotempty(tostring(ipc.properties.publicIPAddress.id))"
                " | project vm = tolower(tostring(properties.virtualMachine.id))"
                f" | where vm in ({kql_list(vms)}) | distinct vm"):
            found[r["vm"]].append("public IP")
    return found


def _priority(row):
    """Reachable first, then live (a VM or running container, not just an image), then fixable, then CVSS."""
    return (-bool(row["exposure"]), -bool(row["live"]), -bool(row["fixable"]), -(row["maxCvss"] or 0), -row["high"])


def findings(client, subscriptions, groups=None, filters=None):
    """The scope's findings: a summary, the top resources (by priority) and the top software, and the types present."""
    base = _base(filters) + rg_clause(groups)
    scope = list(subscriptions) or None
    summary = (client.query(f"{base} | summarize findings=count(), resources=dcount(parent), {_COUNTS},"
                            " running=countif(childKind =~ 'K8s-container'), images=countif(childKind =~ '.containerimage')",
                            subscriptions=scope) or [{}])[0]
    types = client.query(f"{_base()}{rg_clause(groups)} | summarize findings=count() by rtype | order by findings desc",
                         subscriptions=scope)
    resources = client.query(
        f"{base} | summarize findings=count(), {_COUNTS}, live=countif(live), running=countif(childKind =~ 'K8s-container'),"
        " images=countif(childKind =~ '.containerimage'), children=dcount(child)"
        " by parent, rtype, subscriptionId, resourceGroup"
        f" | order by maxCvss desc, high desc, findings desc | take {MAX_ROWS + 1}", subscriptions=scope)
    exposure = _exposure(client, [r["parent"] for r in resources[:MAX_ROWS]])
    for r in resources:
        r["name"] = r["parent"].rsplit("/", 1)[-1]
        r["exposure"] = exposure.get(r["parent"], [])
    resources = sorted(resources[:MAX_ROWS], key=_priority)
    software = client.query(
        f"{base} | summarize resources=dcount(parent), findings=count(), {_COUNTS},"
        " fixedIn=make_set_if(fixed, isnotempty(fixed), 3), types=make_set(rtype, 4),"
        " running=countif(childKind =~ 'K8s-container')"
        f" by software | order by maxCvss desc, resources desc | take {MAX_ROWS + 1}", subscriptions=scope)
    cves = client.query(
        f"{base} | extend list = parse_json(tostring(add.CvesDetails)) | mv-expand item = list"
        " | extend cve = toupper(tostring(item.CveId)), cveSeverity = tostring(item.Severity) | where isnotempty(cve)"
        " | summarize resources=dcount(parent), findings=count(), packages=make_set(software, 3), maxCvss=max(cvss),"
        " severity=take_any(cveSeverity), fixable=countif(hasFix), running=countif(childKind =~ 'K8s-container'),"
        " images=dcountif(child, childKind =~ '.containerimage')"
        f" by cve | order by maxCvss desc, resources desc | take {MAX_ROWS + 1}", subscriptions=scope)
    counts = {k: summary.get(k) or 0 for k in ("findings", "resources", "high", "medium", "low", "fixable", "running", "images")}
    return {"summary": {**counts, "maxCvss": summary.get("maxCvss")},
            "types": types, "resources": resources, "software": software[:MAX_ROWS], "cves": cves[:MAX_ROWS],
            "truncated": {"resources": len(resources) >= MAX_ROWS, "software": len(software) > MAX_ROWS,
                          "cves": len(cves) > MAX_ROWS}}


def headline(client, subscriptions, groups=None, top=3):
    """For a summary: the scope's totals and its `top` resources by the same priority as `findings`, in two queries."""
    base = _base() + rg_clause(groups)
    scope = list(subscriptions) or None
    summary = (client.query(f"{base} | summarize findings=count(), resources=dcount(parent), {_COUNTS},"
                            " running=countif(childKind =~ 'K8s-container'), images=countif(childKind =~ '.containerimage')",
                            subscriptions=scope) or [{}])[0]
    resources = client.query(
        f"{base} | summarize findings=count(), {_COUNTS}, live=countif(live), running=countif(childKind =~ 'K8s-container')"
        f" by parent, rtype, subscriptionId, resourceGroup | order by maxCvss desc, high desc, findings desc | take {MAX_ROWS}",
        subscriptions=scope)
    exposure = _exposure(client, [r["parent"] for r in resources])
    for r in resources:
        r["name"] = r["parent"].rsplit("/", 1)[-1]
        r["exposure"] = exposure.get(r["parent"], [])
    counts = {k: summary.get(k) or 0 for k in ("findings", "resources", "high", "medium", "low", "fixable", "running", "images")}
    return {"summary": {**counts, "maxCvss": summary.get("maxCvss")}, "resources": sorted(resources, key=_priority)[:max(top, 0)]}


def _cves(text):
    try:
        items = json.loads(text or "[]")
    except ValueError:
        return []
    return [{"id": c.get("CveId") or "", "severity": c.get("Severity") or "", "fix": c.get("FixStatus") or "",
             "fixed": c.get("FixedVersion") or ""} for c in items if isinstance(c, dict)][:50]


def _versions(text):
    try:
        value = json.loads(text) if text and text.startswith("[") else text
    except ValueError:
        value = text
    return ", ".join(value) if isinstance(value, list) else (value or "")


def for_software(client, subscriptions, software, groups=None, filters=None):
    """The resources one package is vulnerable on, worst first."""
    rows = client.query(
        f"{_base({**(filters or {}), 'software': software})}{rg_clause(groups)}"
        f" | summarize findings=count(), maxCvss=max(cvss), fixed=make_set_if(fixed, isnotempty(fixed), 3),"
        " detected=make_set(detected, 3), children=make_set_if(child, isnotempty(child), 5), live=countif(live)"
        f" by parent, rtype, resourceGroup | order by maxCvss desc | take {MAX_ROWS}", subscriptions=list(subscriptions) or None)
    for r in rows:
        r["name"] = r["parent"].rsplit("/", 1)[-1]
        r["detected"] = sorted({v for d in r["detected"] for v in _versions(d).split(", ") if v})
    return rows


_DIGEST = re.compile(r"(sha256:[0-9a-f]{64})")
# A running container's native ID: ...namespace-<ns>-toplevelworkloadkind-<kind>-pod-<pod>-container-<container>
# (98% of a live sample parsed; Kubernetes names can't contain these marker words).
_K8S = re.compile(r"namespace-(?P<namespace>[a-z0-9-]+?)-toplevelworkloadkind-(?P<kind>[a-z]+)-pod-(?P<pod>[a-z0-9.-]+?)"
                  r"-container-(?P<container>[a-z0-9.-]+?)-?$")


def _image_ref(row):
    """For a registry image: its repository and digest; for a running container: namespace, workload kind, pod
    (as Defender names it) and container. Enough to ask the registry for tags, or the cluster for the running image."""
    kind = row.get("childKind") or ""
    if kind == ".containerimage":
        digest = _DIGEST.search(row.get("rid") or "")
        return {"repository": row.get("child") or "", "digest": digest.group(1) if digest else ""}
    if kind == "K8s-container":
        found = _K8S.search((row.get("native") or "").lower())
        return found.groupdict() if found else {}
    return {}


def counts_for(client, resource_id):
    """One resource's totals (its own findings and those on containers or images inside it), in one query: what
    Overview says before the Vulnerabilities tab lists them."""
    target = resource_id.lower().rstrip("/")
    row = (client.query(f"{_base()} | where parent == {json.dumps(target)} | summarize findings=count(), {_COUNTS},"
                        " running=countif(childKind =~ 'K8s-container'), images=countif(childKind =~ '.containerimage')",
                        subscriptions=[target.split("/")[2]]) or [{}])[0]
    return {**{k: row.get(k) or 0 for k in ("findings", "high", "medium", "low", "fixable", "running", "images")},
            "maxCvss": row.get("maxCvss")}


def _portal_link(link):
    """Defender's own page for a finding: the assessment's portal link, which Azure gives without a scheme."""
    if not link:
        return ""
    return link if link.startswith("https://") else "https://" + link.removeprefix("http://").lstrip("/")


def for_resource(client, resource_id):
    """One resource's findings (and those on containers or images inside it), with their CVEs."""
    target = resource_id.lower().rstrip("/")
    rows = client.query(
        f"{_base()} | where parent == {json.dumps(target)}"
        " | project title=tostring(properties.displayName), software, severity, cvss, fixed, detected, hasFix, child, childKind,"
        " rid, native=tostring(properties.resourceDetails.NativeResourceId), portal=tostring(properties.links.azurePortal),"
        f" cves=tostring(add.CvesDetails) | order by cvss desc | take {MAX_ROWS + 1}")
    out = [{**{k: r.get(k) for k in ("title", "software", "severity", "cvss", "fixed", "hasFix", "child")},
            "where": _where(r.get("childKind")), "detected": _versions(r.get("detected")), "cves": _cves(r.get("cves")),
            "image": _image_ref(r), "defender": _portal_link(r.get("portal"))}
           for r in rows[:MAX_ROWS]]
    return {"findings": out, "truncated": len(rows) > MAX_ROWS, "resource_id": resource_id}


def running_images(kube, refs):
    """{"namespace/container": [{"pod", "image", "image_id"}]} for the containers Defender named, read from the pods in
    their namespaces (one read per namespace). Pods whose name starts with Defender's pod name come first."""
    by_namespace = {}
    for ref in refs:
        if ref.get("namespace") and ref.get("container"):
            by_namespace.setdefault(ref["namespace"], []).append(ref)
    out = {}
    for namespace, wanted in by_namespace.items():
        pods = kube.items(f"/api/v1/namespaces/{namespace}/pods")
        for ref in wanted:
            found = []
            for pod in pods:
                name = (pod.get("metadata") or {}).get("name") or ""
                statuses = {c.get("name"): c for c in ((pod.get("status") or {}).get("containerStatuses") or [])
                            + ((pod.get("status") or {}).get("initContainerStatuses") or [])}
                for container in ((pod.get("spec") or {}).get("containers") or []) + ((pod.get("spec") or {}).get("initContainers") or []):
                    if container.get("name") != ref["container"]:
                        continue
                    found.append({"pod": name, "image": container.get("image") or "",
                                  "image_id": (statuses.get(ref["container"]) or {}).get("imageID") or "",
                                  "match": name.startswith(ref.get("pod") or "\0")})
            found.sort(key=lambda f: (not f["match"], f["pod"]))
            out[f"{namespace}/{ref['container']}"] = found[:10]
    return out


# ------------------------------------------------------------------ what isn't scanned

def coverage(client, subscriptions, groups=None):
    """What Defender isn't scanning in the scope, so "no findings" is never read as "nothing vulnerable"."""
    scope = list(subscriptions) or None
    machines = client.query(
        "securityresources | where type =~ 'microsoft.security/assessments'"
        f" | where name in ({kql_list(NOT_SCANNED_KEYS)}) and tostring(properties.status.code) =~ 'Unhealthy'"
        " | extend rid = tolower(tostring(properties.resourceDetails.Id)) | extend resourceGroup = tostring(split(rid, '/')[4])"
        f"{rg_clause(groups)} | distinct rid, subscriptionId, resourceGroup | take {MAX_ROWS + 1}", subscriptions=scope)
    plans = client.query(
        "securityresources | where type =~ 'microsoft.security/pricings'"
        f" | where name in ({kql_list(SCANNING_PLANS)})"
        " | project subscriptionId, plan=name, tier=tostring(properties.pricingTier)", subscriptions=scope)
    off, on = {}, set()
    for p in plans:
        if (p.get("tier") or "").lower() != "standard":
            off.setdefault(p["plan"], []).append(p["subscriptionId"].lower())
        else:
            on.add(p["plan"])
    # "unknown": no plan settings came back at all (Microsoft.Security not registered, or not readable).
    defender = "unknown" if not plans else "off" if not on else "partial" if off else "on"
    return {"machines": [{"id": m["rid"], "name": m["rid"].rsplit("/", 1)[-1], "resource_group": m["resourceGroup"],
                          "subscription_id": m["subscriptionId"].lower()} for m in machines[:MAX_ROWS]],
            "machines_truncated": len(machines) > MAX_ROWS,
            "defender": defender,
            "plans_off": [{"plan": plan, "label": SCANNING_PLANS[plan], "subscriptions": subs} for plan, subs in off.items()]}


# ------------------------------------------------------------------ platform checks (no Defender needed)

def _item(row, status, detail, display_type):
    return cards.item(row, status, detail, display_type=display_type)


def _semver(text):
    return tuple(int(p) for p in re.findall(r"\d+", text or "")[:3])


def aks_versions(client, subscription_id, location):
    """{minor: {"plans": [...], "patches": [...]}} Azure supports in a region; cached for a day."""
    def load():
        payload = client.get(f"/subscriptions/{subscription_id}/providers/Microsoft.ContainerService/locations/{location}"
                             "/kubernetesVersions", AKS_VERSIONS_API) or {}
        return {v["version"]: {"plans": (v.get("capabilities") or {}).get("supportPlan") or [],
                               "patches": sorted((v.get("patchVersions") or {}).keys(), key=_semver),
                               "preview": bool(v.get("isPreview"))} for v in payload.get("values") or []}
    return _reference.get_or_load(("aks", client.identity.tenant_id, location.lower()), load)


def aks_checks(client, subscriptions, groups=None, today=None, warn_days=NODE_IMAGE_WARN_DAYS, crit_days=NODE_IMAGE_CRIT_DAYS):
    """Two cards: Kubernetes version support (from Azure's own list per region) and node image age."""
    today = today or datetime.now(timezone.utc).date()
    clusters = client.query(
        "Resources | where type =~ 'microsoft.containerservice/managedclusters'"
        f"{rg_clause(groups)}"
        " | project id, name, location, resourceGroup, subscriptionId, version=tostring(properties.currentKubernetesVersion),"
        " plan=tostring(properties.supportPlan), pools=properties.agentPoolProfiles", subscriptions=list(subscriptions) or None)
    versions, errors, version_items, image_items = {}, [], [], []
    for c in clusters:
        location = c["location"].lower()
        if location not in versions:
            try:
                versions[location] = aks_versions(client, c["subscriptionId"], location)
            except ArmError as error:
                versions[location] = None
                errors.append(f"Supported AKS versions in {location}: {str(error).splitlines()[0]}")
        listed = versions[location]
        minor = ".".join(c["version"].split(".")[:2])
        if listed:
            known = {m: v for m, v in listed.items() if not v["preview"]}
            supported = sorted(known, key=_semver)
            info = known.get(minor)
            lts = (c.get("plan") or "").lower() == "akslongtermsupport"
            if info is None and supported and _semver(minor) < _semver(supported[0]):
                version_items.append(_item(c, CRITICAL, f"Kubernetes {c['version']} is out of support: Azure lists "
                                           f"{supported[0]} to {supported[-1]} in {location}.", "AKS cluster"))
            elif info and info["plans"] == ["AKSLongTermSupport"] and not lts:
                version_items.append(_item(c, CRITICAL, f"Kubernetes {minor} is supported only under Long Term Support, which "
                                           "this cluster isn't on.", "AKS cluster"))
            elif info and info["patches"] and _semver(c["version"]) < _semver(info["patches"][-1]):
                version_items.append(_item(c, WARNING, f"Kubernetes {c['version']}: a newer patch, {info['patches'][-1]}, "
                                           "is available (patches carry security fixes).", "AKS cluster"))
        for pool in c.get("pools") or []:
            age = node_image_age(pool.get("nodeImageVersion"), today)
            if age is None or age < warn_days:
                continue
            image_items.append({**_item(c, CRITICAL if age >= crit_days else WARNING,
                                        f"Node pool {pool.get('name')}: node image {pool.get('nodeImageVersion')}, "
                                        f"{age} days old (Azure ships updated images weekly).", "AKS node pool"),
                                "age": age})
    image_items.sort(key=lambda i: -i["age"])
    return [_card("aks_versions", "AKS Kubernetes versions",
                  "Clusters out of support, on a version only Long Term Support covers, or behind on patches",
                  version_items, errors),
            _card("aks_node_images", "AKS node images",
                  f"Node pools whose OS image is over {warn_days} days old (red over {crit_days})",
                  image_items, [])]


def runtime_stacks(client):
    """{runtime string (lower): {"eol", "deprecated"}} from Azure's own Web and Function app stacks; cached for a day."""
    def load():
        stacks = {}
        for path in ("/providers/Microsoft.Web/webAppStacks", "/providers/Microsoft.Web/functionAppStacks"):
            for os_type, settings_key in (("Linux", "linuxRuntimeSettings"), ("Windows", "windowsRuntimeSettings")):
                payload = client.get(path, STACKS_API, params={"stackOsType": os_type}) or {}
                for stack in payload.get("value") or []:
                    for major in (stack.get("properties") or {}).get("majorVersions") or []:
                        for minor in major.get("minorVersions") or []:
                            settings = (minor.get("stackSettings") or {}).get(settings_key) or {}
                            runtime = settings.get("runtimeVersion")
                            if runtime:
                                stacks.setdefault(f"{os_type.lower()}:{runtime.lower()}",
                                                  {"eol": (settings.get("endOfLifeDate") or "")[:10],
                                                   "deprecated": bool(settings.get("isDeprecated"))})
        return stacks
    return _reference.get_or_load(("stacks", client.identity.tenant_id), load)


def judge_runtime(runtime, os_type, stacks, today, eol_days=EOL_WARN_DAYS):
    """(status, detail) for one app's runtime, or None when there's nothing to say (a container, or current)."""
    if not runtime or runtime.split("|")[0].upper() in ("DOCKER", "COMPOSE", "KUBE"):
        return None
    info = stacks.get(f"{os_type}:{runtime.lower()}")
    if info is None:
        return WARNING, f"{runtime} isn't in Azure's current runtime list, so it's likely retired."
    if info["eol"]:
        ends = date.fromisoformat(info["eol"])
        days = (ends - today).days
        if days < 0:
            return CRITICAL, f"{runtime} reached end of life on {info['eol']}: no more security fixes."
        if days <= eol_days:
            return WARNING, f"{runtime} reaches end of life on {info['eol']} ({days} days)."
    if info["deprecated"]:
        return WARNING, f"{runtime} is deprecated in Azure."
    return None


def app_runtime_checks(client, subscriptions, groups=None, today=None, eol_days=EOL_WARN_DAYS):
    """Linux Web and Function apps whose runtime is past or near end of life (Windows apps: on each app's page, since
    their runtime isn't in Resource Graph)."""
    today = today or datetime.now(timezone.utc).date()
    apps = client.query(
        "Resources | where type =~ 'microsoft.web/sites'"
        f"{rg_clause(groups)}"
        " | project id, name, kind, resourceGroup, subscriptionId, fx=tostring(properties.siteConfig.linuxFxVersion)",
        subscriptions=list(subscriptions) or None)
    errors = []
    try:
        stacks = runtime_stacks(client)
    except ArmError as error:
        stacks, errors = None, [f"Azure's runtime list: {str(error).splitlines()[0]}"]
    items, windows = [], 0
    for app in apps:
        if "linux" not in (app.get("kind") or "").lower():
            windows += 1
            continue
        verdict = judge_runtime(app.get("fx"), "linux", stacks or {}, today, eol_days) if stacks is not None else None
        if verdict:
            kind = "Function App" if "functionapp" in (app.get("kind") or "").lower() else "Web App"
            items.append(_item(app, verdict[0], verdict[1], kind))
    card = _card("runtimes", "App Service runtimes", "Linux Web and Function apps on a runtime past or near end of life "
                 "(Azure's own dates)", items, errors)
    if windows:
        card["note"] = f"{windows} Windows app(s) aren't checked here: their runtime isn't in Resource Graph. Open one to check it."
    return card


def tls_checks(client, subscriptions, groups=None):
    """Storage, SQL and Redis accepting TLS below 1.2, and storage or apps allowing plain HTTP."""
    rows = client.query(
        "Resources | where type in~ ('microsoft.storage/storageaccounts', 'microsoft.sql/servers', 'microsoft.cache/redis',"
        " 'microsoft.web/sites')"
        f"{rg_clause(groups)}"
        " | extend tls = coalesce(tostring(properties.minimumTlsVersion), tostring(properties.minimalTlsVersion)),"
        " https = tostring(coalesce(properties.supportsHttpsTrafficOnly, properties.httpsOnly))"
        " | where tls in ('TLS1_0', 'TLS1_1', '1.0', '1.1') or https =~ 'false'"
        f" | project id, name, type=tolower(type), resourceGroup, subscriptionId, tls, https | take {MAX_ROWS}",
        subscriptions=list(subscriptions) or None)
    names = {"microsoft.storage/storageaccounts": "Storage account", "microsoft.sql/servers": "SQL server",
             "microsoft.cache/redis": "Redis cache", "microsoft.web/sites": "App Service"}
    items = []
    for r in rows:
        problems = []
        if r.get("tls"):
            problems.append(f"accepts TLS {r['tls'].replace('TLS', '').replace('_', '.')} (1.2 is the minimum to keep)")
        if (r.get("https") or "").lower() == "false":
            problems.append("allows plain HTTP")
        if problems:
            text = "; ".join(problems)
            items.append(_item(r, WARNING, text[0].upper() + text[1:] + ".", names.get(r["type"], r["type"])))
    return _card("tls", "TLS and HTTPS", "Resources accepting old TLS versions or plain HTTP", items, [])


def _card(key, title, description, items, errors):
    return cards.card(key, title, description, items[:MAX_ROWS], "; ".join(errors))


def platform(client, subscriptions, groups=None, today=None, settings=None):
    """`settings`: the console's, for the ages and days that turn a check yellow or red (aks.*, vulnerabilities.*)."""
    aks, vuln = (settings or {}).get("aks") or {}, (settings or {}).get("vulnerabilities") or {}
    days = {aks_checks: {"warn_days": aks.get("node_image_warning_days", NODE_IMAGE_WARN_DAYS),
                         "crit_days": aks.get("node_image_critical_days", NODE_IMAGE_CRIT_DAYS)},
            app_runtime_checks: {"eol_days": vuln.get("eol_warning_days", EOL_WARN_DAYS)}}
    cards = []
    for check in (aks_checks, app_runtime_checks, tls_checks):
        try:
            found = check(client, subscriptions, groups, today, **days[check]) if check is not tls_checks else check(client, subscriptions, groups)
        except AccessDenied as error:
            found = _card(check.__name__, check.__name__, "", [], [str(error).splitlines()[0]])
        cards += found if isinstance(found, list) else [found]
    return cards


def site_runtime(client, resource_id, today=None, eol_days=EOL_WARN_DAYS):
    """One App Service app's runtime and TLS from its configuration (a GET; Windows apps' runtime lives only here)."""
    today = today or datetime.now(timezone.utc).date()
    config = (client.get(f"{resource_id}/config/web", STACKS_API) or {}).get("properties") or {}
    linux = bool(config.get("linuxFxVersion"))
    runtime = config.get("linuxFxVersion") or config.get("windowsFxVersion") or ""
    if not runtime and config.get("netFrameworkVersion"):
        runtime = config["netFrameworkVersion"]
    stacks = runtime_stacks(client)
    verdict = judge_runtime(runtime, "linux" if linux else "windows", stacks, today, eol_days) if runtime else None
    checks = [{"name": "Runtime", "value": runtime or "not set",
               "status": verdict[0] if verdict else HEALTHY, "detail": verdict[1] if verdict else "Current in Azure's list."}]
    tls = config.get("minTlsVersion") or ""
    checks.append({"name": "Minimum TLS", "value": tls or "not set",
                   "status": WARNING if tls in ("1.0", "1.1") else HEALTHY,
                   "detail": "Accepts TLS below 1.2." if tls in ("1.0", "1.1") else ""})
    ftps = config.get("ftpsState") or ""
    checks.append({"name": "FTP", "value": ftps or "not set", "status": WARNING if ftps == "AllAllowed" else INFO,
                   "detail": "Plain FTP is allowed." if ftps == "AllAllowed" else ""})
    return checks
