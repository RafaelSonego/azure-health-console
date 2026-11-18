"""Public network access: which resources can be reached from the internet, read from Resource Graph properties.
One KQL fragment and one classifier, shared by Browse (a chip on each row), Triage (a card) and Security
(core/security.py). Three levels: open to all networks, restricted (a firewall or authorized ranges), or a public endpoint
whose firewall rules live in child resources this doesn't read (PostgreSQL, Redis...). Web Apps' access restrictions
live in their config/web row, read by add_web_restrictions before classify; Service Bus, Event Hubs and SQL rules
need one ARM read each, so only Security asks for them (add_firewall_rules)."""
from concurrent.futures import ThreadPoolExecutor

from .arm import MAX_CONNECTIONS, ArmError, kql_list

OPEN, RESTRICTED, UNCHECKED = "open", "restricted", "unchecked"
WEB_TYPE = "microsoft.web/sites"
WEB_BATCH = 500
ANY_ADDRESS = {"any", "*", "0.0.0.0/0", "::/0"}
# Only the restriction fields: the same config row also carries publishing and connection-string fields.
WEB_QUERY = ("appserviceresources | where type =~ 'microsoft.web/sites/config'"
             " | extend site = tolower(substring(id, 0, strlen(id) - strlen('/config/web')))"
             " | where site in ({ids})"
             " | project site, mainDefault=tolower(tostring(properties.IpSecurityRestrictionsDefaultAction)),"
             " main=properties.IpSecurityRestrictions,"
             " scmDefault=tolower(tostring(properties.ScmIpSecurityRestrictionsDefaultAction)),"
             " scmUseMain=tobool(properties.ScmIpSecurityRestrictionsUseMain), scm=properties.ScmIpSecurityRestrictions")
AKS_TYPE = "microsoft.containerservice/managedclusters"
COSMOS_TYPE = "microsoft.documentdb/databaseaccounts"
STORAGE_TYPE = "microsoft.storage/storageaccounts"
DISK_TYPES = {"microsoft.compute/disks", "microsoft.compute/snapshots"}  # their public access is export by SAS
# Types whose firewall is `networkAcls`/`networkRuleSet` on the resource itself: no rule set means every network.
ACL_TYPES = {STORAGE_TYPE, "microsoft.keyvault/vaults", "microsoft.cognitiveservices/accounts",
             "microsoft.containerregistry/registries"}
SEARCH_TYPE = "microsoft.search/searchservices"
RECOVERY_TYPE = "microsoft.recoveryservices/vaults"
# A private link scope sets how Arc agents connect; it isn't an endpoint itself.
NOT_ENDPOINTS = {"microsoft.kubernetesconfiguration/privatelinkscopes", "microsoft.hybridcompute/privatelinkscopes"}
SQL_TYPE, SQL_API = "microsoft.sql/servers", "2021-11-01"
# Types whose firewall is a firewallRules child list, read with the API version beside them (judged like SQL's).
RULE_LIST_APIS = {SQL_TYPE: SQL_API, "microsoft.synapse/workspaces": "2021-06-01"}
APIM_TYPE, KUSTO_TYPE = "microsoft.apimanagement/service", "microsoft.kusto/clusters"
# Public network access on, and no IP firewall in ARM to read: said plainly instead of "not read".
NO_ARM_FIREWALL = {
    "microsoft.datafactory/factories": ("public network access on; Data Factory has no IP firewall",
                                        "Disable public network access if it should only be reached through private endpoints."),
    "microsoft.databricks/workspaces": ("workspace reachable from any network unless its IP access lists (a workspace "
                                        "setting, not in Azure Resource Manager) limit it",
                                        "Check the workspace's IP access lists, or disable public network access."),
}
RULE_SET_APIS = {"microsoft.servicebus/namespaces": "2021-11-01", "microsoft.eventhub/namespaces": "2024-01-01"}
FIREWALL_TYPES = {*RULE_LIST_APIS, *RULE_SET_APIS}
FIREWALL_PARALLEL = MAX_CONNECTIONS  # measured: 228 reads in 3.5 s at 32, 6.5 s at 16
DISK_LIST = ", ".join(f"'{t}'" for t in sorted(DISK_TYPES))

COLUMNS = ("pna=tolower(coalesce(tostring(properties.publicNetworkAccess), tostring(properties.network.publicNetworkAccess))),"
           " acl=tolower(coalesce(tostring(properties.networkAcls.defaultAction), tostring(properties.networkRuleSet.defaultAction))),"
           " privateCluster=tolower(tostring(properties.apiServerAccessProfile.enablePrivateCluster)),"
           " authorizedRanges=array_length(properties.apiServerAccessProfile.authorizedIPRanges),"
           " ipRules=array_length(properties.ipRules), searchIpRules=array_length(properties.networkRuleSet.ipRules),"
           " vnetFilter=tolower(tostring(properties.isVirtualNetworkFilterEnabled)),"
           " anonymousBlob=tolower(tostring(properties.allowBlobPublicAccess)),"
           " diskPolicy=tolower(tostring(properties.networkAccessPolicy)),"
           " apimVnet=tolower(tostring(properties.virtualNetworkType)),"
           " kustoRanges=array_length(properties.allowedIpRangeList)")
COLUMN_NAMES = ", ".join(part.split("=", 1)[0].strip() for part in COLUMNS.split(", ") if "=" in part)


def classify(row):
    """{"level", "what", "action"} for a row carrying COLUMNS (and type), or None when it isn't reachable publicly
    as far as its properties say."""
    kind = (row.get("type") or "").lower()
    pna, acl = row.get("pna") or "", row.get("acl") or ""
    if kind == AKS_TYPE:
        if row.get("privateCluster") == "true":
            return None
        if row.get("authorizedRanges"):
            return _found(RESTRICTED, "API server limited to authorized IP ranges")
        return _found(OPEN, "API server reachable from any network",
                      "Make it a private cluster, or set authorized IP ranges.")
    if pna == "disabled" or kind in NOT_ENDPOINTS:
        return None
    if kind in DISK_TYPES:
        policy = row.get("diskPolicy") or ""
        if policy == "denyall":
            return None
        if policy == "allowprivate":
            return _found(RESTRICTED, "export (SAS) only through a private disk access")
        return _found(UNCHECKED, "can be exported by SAS from any network", "Set its network access to private or deny.")
    if pna == "securedbyperimeter":
        return _found(RESTRICTED, "public access through a network security perimeter")
    blob = "; anonymous blob access allowed" if kind == STORAGE_TYPE and row.get("anonymousBlob") == "true" else ""
    if kind == COSMOS_TYPE and pna == "enabled":
        if row.get("ipRules") or row.get("vnetFilter") == "true":
            return _found(RESTRICTED, "public endpoint limited to IP rules or virtual networks")
        return _found(OPEN, "public endpoint open to all networks", "Add IP rules or virtual networks, or disable public access.")
    if kind == SEARCH_TYPE and pna == "enabled":
        if row.get("searchIpRules"):
            return _found(RESTRICTED, "public endpoint limited to IP rules")
        return _found(OPEN, "public endpoint open to all networks", "Add IP rules, or disable public access.")
    if acl == "deny":
        return _found(RESTRICTED, "public endpoint, firewall allows selected networks only" + blob,
                      "Turn off anonymous blob access." if blob else "")
    if acl == "allow" or (pna == "enabled" and kind in ACL_TYPES):
        return _found(OPEN, "public endpoint open to all networks" + blob,
                      "Disable public network access, or allow selected networks only.")
    if kind == WEB_TYPE and pna == "enabled" and row.get("web"):
        return _web(row["web"])
    if pna == "enabled" and "ruleSet" in row:
        return _rule_set(row["ruleSet"])
    if pna == "enabled" and "sqlRules" in row:
        return _sql(row["sqlRules"])
    if kind == APIM_TYPE and pna == "enabled":
        if row.get("apimVnet") == "internal":
            return _found(RESTRICTED, "gateway reachable only inside its virtual network (internal mode)")
        return _found(OPEN, "API gateway reachable from any network (no internal virtual network)",
                      "Expected for a public API; otherwise use internal mode or put it behind an application gateway.")
    if kind == KUSTO_TYPE and pna == "enabled":
        if row.get("kustoRanges"):
            return _found(RESTRICTED, "public endpoint limited to allowed IP ranges")
        return _found(OPEN, "public endpoint open to all networks", "Add allowed IP ranges, or disable public access.")
    if kind in NO_ARM_FIREWALL and pna == "enabled":
        return _found(UNCHECKED, *NO_ARM_FIREWALL[kind])
    if kind == RECOVERY_TYPE and pna == "enabled":
        return _found(UNCHECKED, "public network access on (backup and site recovery traffic); a vault has no IP firewall",
                      "Disable public network access if backups should only use private endpoints.")
    if pna == "enabled":
        return _found(UNCHECKED, "public network access on; its firewall rules aren't read here",
                      "Check its firewall or access restrictions in the portal.")
    return None


def _found(level, what, action=""):
    return {"level": level, "what": what, "action": action}


def add_web_restrictions(client, rows, subscriptions=None):
    """Puts each public Web App's access restrictions on its row as `web`, in batched appserviceresources reads.
    Returns (the queries sent, the error text when a read fails); rows a failed read missed stay "not read"."""
    sites = {r["id"].lower(): r for r in rows
             if (r.get("type") or "").lower() == WEB_TYPE and (r.get("pna") or "") == "enabled" and r.get("id")}
    ids, sent = sorted(sites), []
    for start in range(0, len(ids), WEB_BATCH):
        sent.append(WEB_QUERY.format(ids=kql_list(ids[start:start + WEB_BATCH])))
        try:
            found = client.query(sent[-1], subscriptions=subscriptions)
        except ArmError as error:
            return sent, str(error)[:300]
        for config in found:
            if config.get("site") in sites:
                sites[config["site"]]["web"] = config
    return sent, None


def _web_open(default, rules):
    """True when a rule set lets every address in: no default Deny, or an Allow rule for any address."""
    allow_any = any(str(r.get("Action") or "").lower() == "allow"
                    and str(r.get("IpAddress") or "").lower() in ANY_ADDRESS for r in rules or [])
    return default != "deny" or allow_any


def _web(config):
    site = _web_open(config.get("mainDefault") or "", config.get("main"))
    scm = site if config.get("scmUseMain") else _web_open(config.get("scmDefault") or "", config.get("scm"))
    if site and scm:
        return _found(OPEN, "site and SCM (Kudu) open to all networks",
                      "Add access restrictions if it isn't meant to be public; limit the SCM site to known addresses either way.")
    if scm:
        return _found(OPEN, "SCM (Kudu) open to all networks; the site has access restrictions",
                      "Restrict the SCM site too, or have it use the main site's rules.")
    if site:
        return _found(OPEN, "site open to all networks; SCM (Kudu) has access restrictions",
                      "Add access restrictions if it isn't meant to be public.")
    return _found(RESTRICTED, "public endpoint, access restrictions on the site and SCM (Kudu)")


def add_firewall_rules(client, rows):
    """Puts each public Service Bus / Event Hubs namespace's network rule set (`ruleSet`) and each public SQL server's
    firewall rules (`sqlRules`) on its row, one ARM read each in parallel. Returns (reads sent, error text or None);
    a row whose read failed stays "not read"."""
    todo = [r for r in rows if r.get("id") and (r.get("pna") or "") == "enabled"
            and (r.get("type") or "").lower() in FIREWALL_TYPES]

    def one(row):
        kind = row["type"].lower()
        try:
            if kind in RULE_LIST_APIS:
                row["sqlRules"] = client.get_paged(f"{row['id']}/firewallRules", RULE_LIST_APIS[kind])
            else:
                found = client.get(f"{row['id']}/networkRuleSets/default", RULE_SET_APIS[kind], tolerate=(404,))
                if found is not None:
                    row["ruleSet"] = found.get("properties") or {}
        except ArmError as error:
            return str(error).splitlines()[0][:300]
        return None

    if not todo:
        return 0, None
    with ThreadPoolExecutor(max_workers=min(FIREWALL_PARALLEL, len(todo))) as pool:
        failed = [e for e in pool.map(one, todo) if e]
    return len(todo), (f"{len(failed)} of {len(todo)} not readable ({failed[0]})" if failed else None)


def _rule_set(props):
    """Service Bus / Event Hubs: rules apply only with default action Deny; Allow keeps them stored but unused."""
    if (props.get("publicNetworkAccess") or "").lower() == "disabled":
        return None
    rules = len(props.get("ipRules") or []) + len(props.get("virtualNetworkRules") or [])
    if (props.get("defaultAction") or "").lower() == "deny" and rules:
        return _found(RESTRICTED, "public endpoint, firewall allows selected networks only")
    stored = f"; {rules} stored rule(s) not applied (default action Allow)" if rules else ""
    return _found(OPEN, "public endpoint open to all networks" + stored,
                  "Allow selected networks only (default action Deny, with IP or virtual network rules), or disable public access.")


def _sql(rules):
    """SQL server firewall: 0.0.0.0–255.255.255.255 is every address; 0.0.0.0–0.0.0.0 is "Allow Azure services"."""
    ranges = [((r.get("properties") or {}).get("startIpAddress"), (r.get("properties") or {}).get("endIpAddress")) for r in rules]
    if ("0.0.0.0", "255.255.255.255") in ranges:
        return _found(OPEN, "firewall allows every address (0.0.0.0–255.255.255.255)",
                      "Remove that rule; allow known addresses only, or use private endpoints.")
    azure = ("0.0.0.0", "0.0.0.0") in ranges
    others = len(ranges) - azure
    what = f"public endpoint, firewall allows {others} address range(s)" if others else "public endpoint, no IP firewall rules"
    if azure:
        return _found(RESTRICTED, what + " and every Azure service (any customer's)",
                      "Turn off \"Allow Azure services\" unless something needs it.")
    return _found(RESTRICTED, what)
