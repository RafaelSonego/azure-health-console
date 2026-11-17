"""Public network access: which resources can be reached from the internet, read from Resource Graph properties.
One KQL fragment and one classifier, shared by Browse (a chip on each row), Triage (a card) and Security
(core/security.py). Three levels: open to all networks, restricted (a firewall or authorized ranges), or a public endpoint
whose firewall rules live in child resources this doesn't read (SQL, PostgreSQL...). Web Apps' access restrictions
live in their config/web row, read by add_web_restrictions before classify."""
from .arm import ArmError, kql_list

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
DISK_LIST = ", ".join(f"'{t}'" for t in sorted(DISK_TYPES))

COLUMNS = ("pna=tolower(coalesce(tostring(properties.publicNetworkAccess), tostring(properties.network.publicNetworkAccess))),"
           " acl=tolower(coalesce(tostring(properties.networkAcls.defaultAction), tostring(properties.networkRuleSet.defaultAction))),"
           " privateCluster=tolower(tostring(properties.apiServerAccessProfile.enablePrivateCluster)),"
           " authorizedRanges=array_length(properties.apiServerAccessProfile.authorizedIPRanges),"
           " ipRules=array_length(properties.ipRules), vnetFilter=tolower(tostring(properties.isVirtualNetworkFilterEnabled)),"
           " anonymousBlob=tolower(tostring(properties.allowBlobPublicAccess)),"
           " diskPolicy=tolower(tostring(properties.networkAccessPolicy))")
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
    if pna == "disabled":
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
    if acl == "deny":
        return _found(RESTRICTED, "public endpoint, firewall allows selected networks only" + blob,
                      "Turn off anonymous blob access." if blob else "")
    if acl == "allow" or (pna == "enabled" and kind in ACL_TYPES):
        return _found(OPEN, "public endpoint open to all networks" + blob,
                      "Disable public network access, or allow selected networks only.")
    if kind == WEB_TYPE and pna == "enabled" and row.get("web"):
        return _web(row["web"])
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
