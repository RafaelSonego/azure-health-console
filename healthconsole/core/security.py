"""Security posture across the scope, from Resource Graph alone: exposure (public network access, NSG rules open to
the internet, public IPs), keys and local auth, transport and deletion protection. Settings are checked by property
name, not by resource type, so every type that has the setting is covered. Each finding is a warning or info;
"restricted" marks a public endpoint behind a firewall, listed but not counted as a problem."""
from . import exposure, taxonomy
from .arm import ArmError, kql_list

WARNING, INFO, RESTRICTED = "warning", "info", "restricted"
CATEGORIES = (("exposure", "Exposure"), ("keys", "Keys & local auth"), ("transport", "Transport"),
              ("deletion", "Deletion protection"))
MAX_ROWS = 10000
CHANGE_DAYS = 14  # how long Resource Graph keeps change history
STORAGE, KEY_VAULT, SQL_SERVER = exposure.STORAGE_TYPE, "microsoft.keyvault/vaults", "microsoft.sql/servers"
RISKY_PORTS = {22: "SSH", 3389: "RDP", 1433: "SQL Server", 3306: "MySQL", 5432: "PostgreSQL", 6379: "Redis",
               27017: "MongoDB", 5985: "WinRM", 5986: "WinRM"}
INTERNET = ("*", "internet", "0.0.0.0/0", "any")
CHANGED_FIELDS = ("publicNetworkAccess", "networkAcls", "apiServerAccessProfile", "ipRules", "securityRules",
                  "ipConfigurations", "disableLocalAuth", "allowSharedKeyAccess", "adminUserEnabled",
                  "azureADOnlyAuthentication", "minimumTlsVersion", "minimalTlsVersion", "httpsOnly",
                  "supportsHttpsTrafficOnly", "enableNonSslPort", "enablePurgeProtection", "enableSoftDelete",
                  "allowBlobPublicAccess")

COLUMNS = (exposure.COLUMNS +
           ", localAuth=tolower(tostring(properties.disableLocalAuth)),"
           " sharedKey=tolower(tostring(properties.allowSharedKeyAccess)),"
           " adminUser=tolower(tostring(properties.adminUserEnabled)),"
           " aadOnly=tolower(tostring(properties.administrators.azureADOnlyAuthentication)),"
           " tls=replace_string(replace_string(tolower(coalesce(tostring(properties.minimumTlsVersion),"
           " tostring(properties.minimalTlsVersion))), 'tls', ''), '_', '.'),"
           " httpsOnly=tolower(tostring(properties.httpsOnly)),"
           " httpsTraffic=tolower(tostring(properties.supportsHttpsTrafficOnly)),"
           " nonSsl=tolower(tostring(properties.enableNonSslPort)),"
           " purge=tolower(tostring(properties.enablePurgeProtection)),"
           " softDelete=tolower(tostring(properties.enableSoftDelete))")
COLUMN_NAMES = ", ".join(part.split("=", 1)[0].strip() for part in COLUMNS.split(", ") if "=" in part)
# Only resources with something to say come back; the rest of the scope never leaves Resource Graph.
CANDIDATES = (f"(pna != 'disabled' and (isnotempty(pna) or isnotempty(acl))"
              f" and not(type in~ ({exposure.DISK_LIST}) and diskPolicy == 'denyall'))"
              f" or (type =~ '{exposure.AKS_TYPE}' and privateCluster != 'true')"
              " or localAuth == 'false' or adminUser == 'true'"
              f" or (type =~ '{STORAGE}' and sharedKey != 'false') or (type =~ '{SQL_SERVER}' and aadOnly != 'true')"
              " or tls in ('1.0', '1.1', 'none') or httpsOnly == 'false' or httpsTraffic == 'false' or nonSsl == 'true'"
              f" or (type =~ '{KEY_VAULT}' and (purge != 'true' or softDelete == 'false'))")


def _finding(category, severity, what, action=""):
    return {"category": category, "severity": severity, "what": what, "action": action}


def findings(row):
    """Every finding for one resource row carrying COLUMNS."""
    kind, out = (row.get("type") or "").lower(), []
    level = exposure.classify(row)
    if level:
        severity = {exposure.OPEN: WARNING, exposure.UNCHECKED: INFO}.get(level["level"], RESTRICTED)
        out.append(_finding("exposure", severity, level["what"], level["action"]))
        if severity == RESTRICTED and kind == STORAGE and row.get("anonymousBlob") == "true":
            out.append(_finding("exposure", WARNING, "anonymous blob access allowed (from the networks the firewall lets in)",
                                "Turn off anonymous blob access."))
    if row.get("localAuth") == "false":
        out.append(_finding("keys", INFO, "local (key or SAS) authentication allowed",
                            "Disable local authentication and use Entra ID."))
    if kind == STORAGE and row.get("sharedKey") != "false":
        out.append(_finding("keys", INFO, "shared key access allowed", "Disallow shared key access and use Entra ID."))
    if row.get("adminUser") == "true":
        out.append(_finding("keys", INFO, "admin user enabled", "Disable the admin user; use Entra ID or tokens."))
    if kind == SQL_SERVER and row.get("aadOnly") != "true":
        out.append(_finding("keys", INFO, "SQL logins allowed (not Entra-only)", "Turn on Entra-only authentication."))
    tls = row.get("tls") or ""
    if tls in ("1.0", "1.1", "none"):
        out.append(_finding("transport", INFO, f"minimum TLS {'not set' if tls == 'none' else tls}", "Require TLS 1.2 or later."))
    if row.get("httpsOnly") == "false" or row.get("httpsTraffic") == "false":
        out.append(_finding("transport", INFO, "HTTP allowed (HTTPS only is off)", "Turn on HTTPS only."))
    if row.get("nonSsl") == "true":
        out.append(_finding("transport", INFO, "non-TLS port 6379 open", "Disable the non-SSL port."))
    if kind == KEY_VAULT and row.get("purge") != "true":
        out.append(_finding("deletion", INFO, "purge protection off", "Turn on purge protection (it can't be turned off again)."))
    if kind == KEY_VAULT and row.get("softDelete") == "false":
        out.append(_finding("deletion", INFO, "soft delete off", "Turn on soft delete."))
    return out


def _ports(rule):
    """(label, risky) for a rule's destination ports: "all ports", or the named risky ones first."""
    ranges = [p for p in [rule.get("port"), *(rule.get("ports") or [])] if p]
    if any(p == "*" for p in ranges):
        return "all ports", True
    named = []
    for text in ranges:
        low, _, high = str(text).partition("-")
        try:
            low, high = int(low), int(high or low)
        except ValueError:
            continue
        named += [f"{name} ({port})" for port, name in RISKY_PORTS.items() if low <= port <= high]
    return (", ".join(dict.fromkeys(named)), True) if named else (f"port(s) {', '.join(map(str, ranges))}", False)


def nsg_findings(rows):
    """Findings from NSG inbound Allow rules whose source is the internet (one per rule)."""
    out = []
    for row in rows:
        label, risky = _ports(row)
        out.append(_resource(row, _finding(
            "exposure", WARNING if risky else INFO, f"rule {row.get('rule')}: {label} open to the internet",
            "Limit the source to known addresses, or use Bastion or a VPN for management ports." if risky
            else "Check the internet really needs it.")))
    return out


def ip_findings(rows):
    """Findings for network interfaces with a public IP, on their VM when they have one."""
    out = []
    for row in rows:
        vm = row.get("vm") or ""
        target = {**row, "id": vm or row["nic"], "name": vm.rsplit("/", 1)[-1] if vm else row["nicName"],
                  "type": "microsoft.compute/virtualmachines" if vm else "microsoft.network/networkinterfaces"}
        out.append(_resource(target, _finding("exposure", WARNING, "public IP on its network interface",
                                              "Remove the public IP; reach it through Bastion, a VPN or a load balancer.")))
    return out


EXPOSED = "exposed"  # how an exposure warning is labelled wherever it's listed (Triage, Summary, Security)


def _resource(row, finding):
    kind = (row.get("type") or "").lower()
    return {"id": row["id"], "name": row["name"], "type": kind, "display_type": taxonomy.display_name(kind, row.get("kind")),
            "resource_group": row.get("resourceGroup") or "", "location": row.get("location") or "",
            "subscription_id": (row.get("subscriptionId") or "").lower(), **finding,
            **({"badge": {"tone": WARNING, "label": EXPOSED}} if finding["category"] == "exposure" and finding["severity"] == WARNING else {})}


def queries(clause=""):
    """{name: KQL} for the posture reads; `clause` is arm.rg_clause's resource-group limit."""
    internet = " or ".join(f"src == '{s}'" for s in INTERNET)
    return {
        "settings": (f"Resources | where isnotempty(properties){clause} | extend {COLUMNS} | where {CANDIDATES}"
                     f" | project id, name, type=tolower(type), kind, resourceGroup, subscriptionId, location, {COLUMN_NAMES}"
                     f" | take {MAX_ROWS}"),
        "nsg": ("Resources | where type =~ 'microsoft.network/networksecuritygroups'" + clause +
                " | mv-expand rule = properties.securityRules"
                " | where tolower(tostring(rule.properties.direction)) == 'inbound'"
                " and tolower(tostring(rule.properties.access)) == 'allow'"
                " | extend src = tolower(tostring(rule.properties.sourceAddressPrefix)),"
                " srcs = tolower(tostring(rule.properties.sourceAddressPrefixes))"
                f" | where {internet} or srcs contains '0.0.0.0/0' or srcs contains '\"*\"' or srcs contains 'internet'"
                " | project id, name, type=tolower(type), resourceGroup, subscriptionId, location, rule=tostring(rule.name),"
                " port=tostring(rule.properties.destinationPortRange), ports=rule.properties.destinationPortRanges"
                f" | take {MAX_ROWS}"),
        "public_ips": ("Resources | where type =~ 'microsoft.network/networkinterfaces'" + clause +
                       " | mv-expand ipc = properties.ipConfigurations"
                       " | where isnotempty(tostring(ipc.properties.publicIPAddress.id))"
                       " | project nic=id, nicName=name, resourceGroup, subscriptionId, location,"
                       " vm=tostring(properties.virtualMachine.id) | distinct nic, nicName, resourceGroup, subscriptionId,"
                       f" location, vm | take {MAX_ROWS}"),
        # Joined to the resources with findings on the server: a tenant's 14 days hold hundreds of thousands of creates.
        "changes": ("resourcechanges"
                    " | extend target = tolower(tostring(properties.targetResourceId)),"
                    " at = todatetime(properties.changeAttributes.timestamp), change = tostring(properties.changeType),"
                    " who = tostring(properties.changeAttributes.changedBy), fields = tostring(properties.changes)"
                    f" | where at > ago({CHANGE_DAYS}d)"
                    " | where change == 'Create' or " + " or ".join(f"fields has '{f}'" for f in CHANGED_FIELDS) +
                    f" | join kind=inner (Resources | where isnotempty(properties){clause} | extend {COLUMNS}"
                    f" | where {CANDIDATES} or type =~ 'microsoft.network/networksecuritygroups'"
                    " or (type =~ 'microsoft.network/networkinterfaces' and tostring(properties.ipConfigurations) contains 'publicIPAddress')"
                    " | project target=tolower(id)) on target"
                    " | summarize arg_max(at, who) by target, created = change == 'Create'"),
    }


def _web_restrictions(client, rows, scope, errors):
    """Web Apps' access restrictions onto `rows` before they're judged; ([query entries], error) with the error
    also named in `errors`."""
    sent, error = exposure.add_web_restrictions(client, rows, scope)
    if error:
        errors.append(f"web access restrictions: {error}")
    return [{"label": "web access restrictions", "query": q} for q in sent], error


def posture(client, subscriptions, clause="", history=True):
    """Every finding in scope, warnings first, each with who created the resource or changed one of the checked
    settings in the last CHANGE_DAYS days (unless `history` is off). A read that fails is named in `errors`; the
    others still stand."""
    sent, rows, errors, truncated = queries(clause), [], [], False
    if not history:
        sent.pop("changes")
    scope = list(subscriptions) or None

    def read(name):
        nonlocal truncated
        try:
            found = client.query(sent[name], subscriptions=scope)
        except ArmError as error:
            errors.append(f"{name.replace('_', ' ')}: {str(error)[:300]}")
            return []
        truncated = truncated or (name != "changes" and len(found) >= MAX_ROWS)
        return found

    settings = read("settings")
    web_sent, _ = _web_restrictions(client, settings, scope, errors)
    for row in settings:
        rows += [_resource(row, f) for f in findings(row)]
    rows += nsg_findings(read("nsg")) + ip_findings(read("public_ips"))
    changed = {}
    for change in read("changes") if history else []:
        entry = changed.setdefault(change["target"], {})
        prefix = "created" if change.get("created") else "changed"
        entry[f"{prefix}_at"], entry[f"{prefix}_by"] = str(change.get("at") or ""), change.get("who") or ""
    for row in rows:
        row.update(changed.get(row["id"].lower(), {}))
    order = (WARNING, INFO, RESTRICTED)
    rows.sort(key=lambda r: (order.index(r["severity"]), r["category"], r["display_type"].lower(), r["name"].lower()))
    counts = {key: {s: sum(1 for r in rows if r["category"] == key and r["severity"] == s) for s in order}
              for key, _ in CATEGORIES}
    return {"rows": rows, "counts": counts, "categories": [{"key": k, "label": label} for k, label in CATEGORIES],
            "truncated": truncated, "max_rows": MAX_ROWS, "change_days": CHANGE_DAYS, "errors": errors,
            "queries": [{"label": name.replace("_", " "), "query": q} for name, q in sent.items()] + web_sent}


def exposure_warnings(client, subscriptions, clause=""):
    """(one entry per resource with an exposure warning, its findings joined; errors): Triage's card, the same
    judgement as Security's tab without the change history."""
    data = posture(client, subscriptions, clause, history=False)
    grouped = {}
    for row in data["rows"]:
        if row["category"] == "exposure" and row["severity"] == WARNING:
            entry = grouped.setdefault(row["id"].lower(), {**row, "findings": []})
            entry["findings"].append(row["what"])
    return list(grouped.values()), data["errors"]


def for_resource(client, resource_id):
    """One resource's findings (a Browse Overview block): its settings, and its rules for an NSG or its network
    interfaces' public IPs for a VM. Restricted exposure is included, so the block can say "behind a firewall"."""
    literal = kql_list([resource_id.lower()])
    sent = queries(f" | where tolower(id) in ({literal})")
    scope = [resource_id.split("/")[2]]
    asked = {"settings": sent["settings"]}
    kind = resource_id.lower().split("/providers/", 1)[-1]
    if kind.startswith("microsoft.network/networksecuritygroups/"):
        asked["nsg"] = sent["nsg"]
    if kind.startswith("microsoft.compute/virtualmachines/"):
        asked["public_ips"] = queries()["public_ips"].replace(" | distinct", f" | where tolower(vm) in ({literal}) | distinct")
    found, errors, web_sent = [], [], []
    for name, kql in asked.items():
        try:
            rows = client.query(kql, subscriptions=scope)
        except ArmError as error:
            errors.append(f"{name.replace('_', ' ')}: {str(error)[:300]}")
            continue
        if name == "settings":
            web_sent, _ = _web_restrictions(client, rows, scope, errors)
            found += [_resource(row, f) for row in rows for f in findings(row)]
        else:
            found += (nsg_findings if name == "nsg" else ip_findings)(rows)
    order = (WARNING, INFO, RESTRICTED)
    found.sort(key=lambda r: (order.index(r["severity"]), r["category"]))
    return {"findings": found, "errors": errors, "categories": dict(CATEGORIES),
            "queries": [{"label": name.replace("_", " "), "query": q} for name, q in asked.items()] + web_sent}
