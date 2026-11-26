"""Which Azure resources an AKS workload depends on, from Azure and the cluster rather than a monitoring tool: the host
names in its configuration (env and the ConfigMaps it reads, through the Kubernetes API) and what its workload
identity holds roles on (Azure only). Values are scanned here; only host names leave, and Secrets are never read."""
import re
from concurrent.futures import ThreadPoolExecutor

from ..arm import ArmError, kql_list, kql_string
from ..user_access import role_names
from . import aks, kube

# Azure's own DNS suffixes (fixed by Azure, the same for everyone): the resource type a host name belongs to.
AZURE_HOSTS = (
    (r"\.redis\.cache\.windows\.net$", ("microsoft.cache/redis",)),
    (r"\.redisenterprise\.cache\.azure\.net$", ("microsoft.cache/redisenterprise",)),
    (r"\.(documents|mongo\.cosmos|table\.cosmos|gremlin\.cosmos|cassandra\.cosmos)\.azure\.com$",
     ("microsoft.documentdb/databaseaccounts",)),
    (r"\.database\.windows\.net$", ("microsoft.sql/servers",)),
    (r"\.postgres\.database\.azure\.com$", ("microsoft.dbforpostgresql/flexibleservers", "microsoft.dbforpostgresql/servers")),
    (r"\.mysql\.database\.azure\.com$", ("microsoft.dbformysql/flexibleservers", "microsoft.dbformysql/servers")),
    (r"\.(blob|queue|table|file|dfs|web)\.core\.windows\.net$", ("microsoft.storage/storageaccounts",)),
    (r"\.servicebus\.windows\.net$", ("microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces")),
    (r"\.vault\.azure\.net$", ("microsoft.keyvault/vaults",)),
    (r"\.azurecr\.io$", ("microsoft.containerregistry/registries",)),
    (r"\.azurewebsites\.net$", ("microsoft.web/sites",)),
    (r"\.azure-api\.net$", ("microsoft.apimanagement/service",)),
    (r"\.azconfig\.io$", ("microsoft.appconfiguration/configurationstores",)),
    (r"\.search\.windows\.net$", ("microsoft.search/searchservices",)),
    (r"\.(openai|cognitiveservices)\.azure\.com$", ("microsoft.cognitiveservices/accounts",)),
    (r"\.service\.signalr\.net$", ("microsoft.signalrservice/signalr",)),
    (r"\.eventgrid\.azure\.net$", ("microsoft.eventgrid/topics", "microsoft.eventgrid/domains")),
)
_AZURE = [(re.compile(p, re.I), types) for p, types in AZURE_HOSTS]
# Where a host that isn't an Azure name may still be one of its custom domains.
CUSTOM_DOMAIN_TYPES = ("microsoft.apimanagement/service", "microsoft.web/sites", "microsoft.cdn/profiles/customdomains",
                       "microsoft.cdn/profiles/afdendpoints")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_FQDN = re.compile(rf"(?<![a-z0-9.-])((?:{_LABEL}\.)+[a-z]{{2,63}})(?![a-z0-9-])", re.I)
_URL_HOST = re.compile(rf"[a-z][a-z0-9+.-]*://(?:[^@/\s]*@)?((?:{_LABEL}\.)*{_LABEL})", re.I)
_STORAGE_PARTS = re.compile(r"AccountName=([a-z0-9]{3,24});.*?EndpointSuffix=([a-z0-9.]+)", re.I | re.S)
_IN_CLUSTER = re.compile(r"\.svc(\.cluster\.local)?$", re.I)
MAX_CUSTOM_HOSTS = 20
MAX_IDENTITIES = 400
MAX_COSMOS = 30
PARALLEL = 8


def azure_types(host):
    return next((types for pattern, types in _AZURE if pattern.search(host)), ())


def hosts_in(text):
    """The host names in one configuration value: Azure names anywhere, other names only inside a URL."""
    text = str(text or "")
    found = {h.lower() for h in _URL_HOST.findall(text)}
    found |= {h.lower() for h in _FQDN.findall(text) if azure_types(h) or _IN_CLUSTER.search(h)}
    found |= {f"{name.lower()}.blob.{suffix.lower()}" for name, suffix in _STORAGE_PARTS.findall(text)}
    return {h for h in found if "." in h}


def _candidates(host):
    """Resource names a host may stand for: its first label, without `privatelink.`, and (Cosmos regional endpoints,
    `account-canadacentral`) without its last hyphenated part."""
    labels = [label for label in host.split(".") if label != "privatelink"]
    first = labels[0]
    return {first, first.rsplit("-", 1)[0]} if "-" in first else {first}


# ---------------------------------------------------------------------------------------------- configuration

def _spec(k, namespace, pod=None, workload=None):
    """(pod spec, service account, what was read) for a pod, or a Deployment/StatefulSet/DaemonSet's pod template."""
    if pod:
        item = k.get(f"/api/v1/namespaces/{namespace}/pods/{pod}") or {}
        return item.get("spec") or {}, (item.get("spec") or {}).get("serviceAccountName"), f"pod {pod}"
    kind, name = workload
    item = k.get(f"/apis/apps/v1/namespaces/{namespace}/{kind.lower()}s/{name}") or {}
    spec = ((item.get("spec") or {}).get("template") or {}).get("spec") or {}
    return spec, spec.get("serviceAccountName"), f"{kind} {name}"


def _references(spec):
    """(env values [(where, text)], ConfigMap keys wanted {name: keys or None for all}, Secret names)."""
    values, maps, secrets = [], {}, set()

    def want(name, key=None):
        if name:
            keys = maps.setdefault(name, set())
            if keys is not None:
                maps[name] = None if key is None else keys | {key}
    for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for e in c.get("env") or []:
            ref = e.get("valueFrom") or {}
            if "value" in e:
                values.append((f"env {e.get('name')} ({c.get('name')})", e["value"]))
            elif ref.get("configMapKeyRef"):
                want(ref["configMapKeyRef"].get("name"), ref["configMapKeyRef"].get("key"))
            elif ref.get("secretKeyRef"):
                secrets.add(ref["secretKeyRef"].get("name"))
        for source in c.get("envFrom") or []:
            if source.get("configMapRef"):
                want(source["configMapRef"].get("name"))
            if source.get("secretRef"):
                secrets.add(source["secretRef"].get("name"))
    for v in spec.get("volumes") or []:
        if v.get("configMap"):
            want(v["configMap"].get("name"))
        if v.get("secret"):
            secrets.add(v["secret"].get("secretName"))
        for source in (v.get("projected") or {}).get("sources") or []:
            if source.get("configMap"):
                want(source["configMap"].get("name"))
            if source.get("secret"):
                secrets.add(source["secret"].get("name"))
    return values, maps, sorted(s for s in secrets if s)


def configuration(k, namespace, pod=None, workload=None):
    """{"hosts": {host: [where]}, "service_account", "read", "secrets", "config_maps", "notes"}."""
    spec, account, read = _spec(k, namespace, pod, workload)
    if not spec:
        return {"hosts": {}, "service_account": None, "read": read, "secrets": [], "config_maps": [],
                "notes": [f"The Kubernetes API has no {read} in {namespace}."]}
    values, maps, secrets = _references(spec)
    hosts, notes = {}, []
    for where, text in values:
        for h in hosts_in(text):
            hosts.setdefault(h, []).append(where)

    def fetch(name):
        return name, k.get(f"/api/v1/namespaces/{namespace}/configmaps/{name}")
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for name, item in pool.map(fetch, sorted(maps)):
            if item is None:
                notes.append(f"ConfigMap {name} doesn't exist.")
                continue
            for key, text in (item.get("data") or {}).items():
                if maps[name] is None or key in maps[name]:
                    for h in hosts_in(text):
                        hosts.setdefault(h, []).append(f"ConfigMap {name}, key {key}")
    return {"hosts": hosts, "service_account": account or "default", "read": read, "secrets": secrets,
            "config_maps": sorted(maps), "notes": notes}


# ------------------------------------------------------------------------------------------------- identity

def identities(client, cluster, namespace, service_account=None):
    """The user-assigned identities in the cluster's subscription federated to this namespace's service account (or,
    with none known, to any of its service accounts), via the cluster's OIDC issuer. ([{id, name, principal, subject}],
    notes)."""
    issuer = (cluster.get("issuer") or "").rstrip("/")
    if not issuer:
        return [], ["The cluster has no OIDC issuer, so it can't use workload identity."]
    rows = client.query("Resources | where type =~ 'microsoft.managedidentity/userassignedidentities'"
                        f" and subscriptionId == {kql_string(cluster['subscriptionId'])}"
                        " | project id, name, principal = tostring(properties.principalId)",
                        subscriptions=[cluster["subscriptionId"]])
    notes = []
    if len(rows) > MAX_IDENTITIES:
        notes.append(f"Looked at {MAX_IDENTITIES} of the {len(rows)} identities in the cluster's subscription.")
    subject = f"system:serviceaccount:{namespace}:{service_account}" if service_account else f"system:serviceaccount:{namespace}:"

    def credentials(identity):
        try:
            found = client.get(f"{identity['id']}/federatedIdentityCredentials", "2023-01-31", tolerate=(403, 404)) or {}
        except ArmError:
            return identity, []
        return identity, found.get("value") or []
    out = []
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for identity, creds in pool.map(credentials, rows[:MAX_IDENTITIES]):
            for cred in creds:
                p = cred.get("properties") or {}
                same = p.get("subject") == subject if service_account else (p.get("subject") or "").startswith(subject)
                if (p.get("issuer") or "").rstrip("/") == issuer and same:
                    out.append({"id": identity["id"], "name": identity["name"], "principal": identity["principal"],
                                "subject": p["subject"]})
    notes.append(f"Looked for workload identities in the cluster's subscription ({min(len(rows), MAX_IDENTITIES)} "
                 "identities); one in another subscription isn't found.")
    return out, notes


def _role_assignments(client, principals, subscriptions):
    if not principals:
        return []
    rows = client.query("authorizationresources | where type =~ 'microsoft.authorization/roleassignments'"
                        f" | where tostring(properties.principalId) in ({kql_list(principals)})"
                        " | project principal = tostring(properties.principalId), scope = tolower(tostring(properties.scope)),"
                        " role = tolower(tostring(split(properties.roleDefinitionId, '/')[-1]))",
                        subscriptions=subscriptions)
    names = role_names(client, {r["role"] for r in rows})
    return [{**r, "role_name": names.get(r["role"], r["role"])} for r in rows]


def _cosmos_data_roles(client, account_id, principals):
    """Cosmos DB's own (data-plane) role assignments for these principals: how Entra-only accounts grant access."""
    try:
        assigned = (client.get(f"{account_id}/sqlRoleAssignments", "2024-05-15", tolerate=(403, 404)) or {}).get("value") or []
        defs = (client.get(f"{account_id}/sqlRoleDefinitions", "2024-05-15", tolerate=(403, 404)) or {}).get("value") or []
    except ArmError:
        return []
    names = {d["id"].lower(): (d.get("properties") or {}).get("roleName") for d in defs}
    return [names.get((a["properties"].get("roleDefinitionId") or "").lower(), "a data role")
            for a in assigned if (a.get("properties") or {}).get("principalId") in principals]


# ------------------------------------------------------------------------------------------------- together

def _resolve(client, subscriptions, hosts):
    """{host: resource row}: Azure names by type and name, then custom domains of gateways and web apps."""
    by_type = {}
    for h in hosts:
        for t in azure_types(h):
            by_type.setdefault(t, set()).update(_candidates(h))
    out = {}
    if by_type:
        clause = " or ".join(f"(type =~ {kql_string(t)} and name in~ ({kql_list(sorted(n))}))" for t, n in sorted(by_type.items()))
        found = client.query(f"Resources | where {clause} | project id, name, type, resourceGroup, subscriptionId",
                             subscriptions=subscriptions)
        for h in hosts:
            names, types = _candidates(h), azure_types(h)
            hit = next((r for r in found if r["type"].lower() in types and r["name"].lower() in names), None)
            if hit:
                out[h] = hit
    custom = sorted(h for h in hosts if h not in out and not azure_types(h) and not _IN_CLUSTER.search(h))[:MAX_CUSTOM_HOSTS]
    if custom:
        has = " or ".join(f"blob has {kql_string(h)}" for h in custom)
        found = client.query(f"Resources | where type in~ ({kql_list(CUSTOM_DOMAIN_TYPES)}) | extend blob = tolower(tostring(properties))"
                             f" | where {has} | project id, name, type, resourceGroup, subscriptionId, blob",
                             subscriptions=subscriptions)
        for h in custom:
            hit = next((r for r in found if h in r["blob"]), None)
            if hit:
                out[h] = {k: v for k, v in hit.items() if k != "blob"}
    return out


_AKS_ID = re.compile(r"^/subscriptions/([0-9a-f-]{36})/resourcegroups/[-\w.()]{1,90}"
                     r"/providers/microsoft\.containerservice/managedclusters/[-\w]{1,63}$", re.I)


def _cluster(client, cluster_id):
    match = _AKS_ID.match(cluster_id or "")
    if not match:
        raise ValueError("That isn't an AKS cluster's resource ID.")
    rows = client.query("Resources | where type =~ 'microsoft.containerservice/managedclusters'"
                        f" and id =~ {kql_string(cluster_id)} | project id, name, subscriptionId,"
                        " issuer = tostring(properties.oidcIssuerProfile.issuerURL)", subscriptions=[match.group(1)])
    rows = [r for r in rows if str(r.get("id", "")).lower() == cluster_id.lower()]
    if not rows:
        raise ValueError("That AKS cluster isn't in Resource Graph for this account.")
    return rows[0]


def dependencies(client, subscriptions, cluster_id, namespace, pod=None, workload=None, kube_settings=None):
    """What an AKS workload (a pod, or `workload` = (kind, name)) depends on in Azure, by resource, with where each was
    found: its configuration (Kubernetes API, when reachable) and its workload identity's roles (Azure)."""
    for value in (namespace, pod, workload and workload[1]):
        if value is not None and not aks.K8S_NAME.match(value):
            raise ValueError(f"'{value}' isn't a valid Kubernetes name.")
    if workload and workload[0] not in ("Deployment", "StatefulSet", "DaemonSet"):
        raise ValueError("workload must be a Deployment, StatefulSet or DaemonSet.")
    cluster = _cluster(client, cluster_id)
    config, config_reason = None, ""
    k, why = aks._connect(client, cluster_id, kube_settings)
    if k is None:
        config_reason = why
    else:
        try:
            config = configuration(k, namespace, pod, workload)
        except kube.KubeUnavailable as error:
            config_reason = str(error)
    account = (config or {}).get("service_account")
    found_ids, id_notes = identities(client, cluster, namespace, account)
    principals = [i["principal"] for i in found_ids]
    assignments = _role_assignments(client, principals, list(subscriptions) or None)
    hosts = (config or {}).get("hosts") or {}
    resolved = _resolve(client, list(subscriptions) or None, list(hosts))

    rows = {}

    def row(resource):
        return rows.setdefault(resource["id"].lower(), {"id": resource["id"], "name": resource["name"],
                                                        "type": resource["type"].lower(), "hosts": [], "found_in": [],
                                                        "roles": [], "data_roles": []})
    for h, where in hosts.items():
        if h in resolved:
            r = row(resolved[h])
            r["hosts"].append(h)
            r["found_in"] += [w for w in where if w not in r["found_in"]]
    scoped = sorted({a["scope"] for a in assignments})
    at_scope = {r["id"].lower(): r for r in client.query(
        f"Resources | where id in~ ({kql_list(scoped)}) | project id, name, type, resourceGroup, subscriptionId",
        subscriptions=list(subscriptions) or None)} if scoped else {}
    broad = []
    for a in assignments:
        if a["scope"] in at_scope:
            row(at_scope[a["scope"]])["roles"].append(a["role_name"])
        else:
            broad.append({"scope": a["scope"], "role": a["role_name"]})
    if principals:  # Entra-only Cosmos DB accounts grant access in their own role assignments, not Azure RBAC
        for account in client.query("Resources | where type =~ 'microsoft.documentdb/databaseaccounts'"
                                    f" and subscriptionId == '{cluster['subscriptionId']}' and properties.disableLocalAuth == true"
                                    " | project id, name, type, resourceGroup, subscriptionId",
                                    subscriptions=[cluster["subscriptionId"]])[:MAX_COSMOS]:
            if account["id"].lower() not in rows:
                roles = _cosmos_data_roles(client, account["id"], principals)
                if roles:
                    row(account)["data_roles"] = roles
    for r in rows.values():
        if r["type"] == "microsoft.documentdb/databaseaccounts" and principals and not r["data_roles"]:
            r["data_roles"] = _cosmos_data_roles(client, r["id"], principals)
        r["roles"] = sorted(set(r["roles"]))
    in_cluster = [{"host": h, "found_in": w} for h, w in sorted(hosts.items()) if _IN_CLUSTER.search(h)]
    other = [{"host": h, "found_in": w} for h, w in sorted(hosts.items()) if h not in resolved and not _IN_CLUSTER.search(h)]
    order = {"configuration and identity": 0, "configuration": 1, "identity": 2}

    def source(r):
        return ("configuration and identity" if r["found_in"] and (r["roles"] or r["data_roles"])
                else "configuration" if r["found_in"] else "identity")
    resources = sorted(({**r, "source": source(r)} for r in rows.values()), key=lambda r: (order[r["source"]], r["type"], r["name"]))
    return {"cluster": cluster["name"], "namespace": namespace, "read": (config or {}).get("read") or (f"pod {pod}" if pod else ""),
            "config_available": config is not None, "config_reason": config_reason,
            "service_account": account, "identities": found_ids, "resources": resources, "broad_roles": broad,
            "in_cluster": in_cluster, "other_hosts": other, "secrets_not_read": (config or {}).get("secrets", []),
            "config_maps": (config or {}).get("config_maps", []), "notes": (config or {}).get("notes", []) + id_notes}
