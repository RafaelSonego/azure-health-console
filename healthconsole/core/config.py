"""User settings: `~/.config/azure-health-console/config.yaml`, or the path in AHC_CONFIG.
Lives outside the repo so subscription and resource group names never get committed."""
import copy
import fnmatch
import os
import re
from pathlib import Path

from . import localfiles, yamlio

DEFAULT_PATH = Path("~/.config/azure-health-console/config.yaml")

LOOK_BACK_HOURS = (1, 6, 24, 72, 168, 336, 720)  # what Workloads' Look back offers (a workspace keeps 30 days by default)

DEFAULTS = {
    "default_tenant": None,
    "tenants": {},
    "graph": {"auto": False, "depth": 2, "max_nodes": 60, "inbound": True,
              "inbound_limit": 25, "inbound_scope": "subscription"},
    "health": {"window_minutes": 15, "compare_last_week": True, "changes_hours": 24, "alert_hours": 24,
               "critical_max_severity": 1, "warning_max_severity": 2, "heuristic_limit": 8,
               "detectors": True, "expiry_warning_days": 30, "expiry_critical_days": 7, "baseline_change_percent": 200},
    "network": {"subnet_warning_percent": 80, "subnet_critical_percent": 90},
    "alerts": {"history_hours": 24},
    "alert_check": {"days": 7, "min_buckets": 24, "always_on_percent": 50, "rare_percent": 5, "change_percent": 50},
    "profiles_dir": "~/.config/azure-health-console/profiles",
    "exports_dir": "exports",
    "cache_dir": "~/.cache/azure-health-console",
    # This machine's log: errors, actions and requests sent (Settings → Logs); never tokens or secret values.
    "logs": {"dir": "~/.local/state/azure-health-console/logs", "keep_days": 30},
    "pdf_font": None,
    # AKS Workloads' rules of thumb (the checks say which threshold they used); Kubernetes' own rules aren't here.
    "aks": {"kubeconfig_paths": [], "fetch_credentials": True, "memory_near_limit_percent": 85, "cpu_near_limit_percent": 80,
            "cpu_above_request_percent": 90, "cpu_idle_percent": 30, "hpa_target_high_percent": 85, "hpa_near_max_percent": 80,
            "hpa_overshoot_percent": 150, "pool_requests_percent": 80, "restarts_warning": 5, "leftover_hours": 24,
            "rollout_hours": 24, "node_image_warning_days": 60, "node_image_critical_days": 180,
            "restarts_look_back_hours": 1, "oom_look_back_hours": 24, "events_look_back_hours": 1,
            "oom_near_limit_percent": 90, "oom_node_full_percent": 95},
    # When an App Insights component's last hour counts as worse than the same hour last week (Triage, Investigate,
    # Path's requests): the same rule everywhere.
    "appinsights": {"failed_min": 5, "failed_rate_points": 1, "exceptions_min": 10, "exceptions_percent": 200,
                    "critical_rate_percent": 20, "critical_failed_min": 20},
    "path": {"drop_percent": 50, "drop_min": 100, "restarts_min": 3, "restarts_critical": 10},
    "security": {"risky_ports": [22, 3389, 1433, 3306, 5432, 6379, 27017, 5985, 5986]},
    "triage": {"scope": "none", "changes_hours": 1, "include_system_changes": False,
               "secret_scan_max_vaults": 200, "auto_refresh_minutes": 0,
               "azure_status_feed": True, "capacity_near_max_percent": 80, "resolved_hours": 24},
    "cost": {"spike_percent": 200},
    "vulnerabilities": {"eol_warning_days": 90},
    "watchlist_file": "~/.config/azure-health-console/watchlist.yaml",
    "integrations": {"servicenow": [], "monitors": []},
    "summary": {"top_items": 3, "readiness_quick": True, "budget_warn_percent": 80, "compare_hours": 24,
                "keep_days": 8},
    "personas": {},
    "persona": "",
    "updates": {"check": True},
    # Access audit: actions and data actions that don't count (config.example.yaml says why each).
    "audit": {"keep_days": 7, "ignore_actions": [
        "Microsoft.Support/*", "*/*download*/action", "Microsoft.Insights/alertRules/*", "Microsoft.Resources/deployments/*",
        "*/operationResults/*", "*/register/action", "Microsoft.ResourceHealth/*/action",
        "Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action",
        "*/readMetadata/action", "Microsoft.KeyVault/vaults/*/read",
        "Microsoft.OperationalInsights/*/read", "Microsoft.OperationalInsights/workspaces/jobs/export/action",
        "Microsoft.Insights/*/read", "Microsoft.Monitor/*/read", "*/logstream/action",
        "Microsoft.ContainerService/managedClusters/*/read",
        "Microsoft.ContainerService/managedClusters/listClusterUserCredential/action"]},
}
# One ServiceNow connection's settings; `servicenow` is a list of these, each with a unique name.
SERVICENOW_DEFAULTS = {"name": "ServiceNow", "enabled": False, "instance": "", "auth": "basic", "username": "",
                       "client_id": "", "tables": ["incident", "change_request", "task_ci", "change_task", "em_alert", "em_event"],
                       "incident_filter": "active=true^state!=6",
                       "match_fields": ["short_description", "description", "cmdb_ci.name"], "max_records": 500,
                       "change_history_days": 30, "change_ahead_days": 14, "change_max_records": 5000,
                       "incident_max_records": 2000,
                       "change_match_fields": ["justification", "implementation_plan", "backout_plan", "test_plan",
                                               "business_service"]}
# The tables a connection can read, with what each is for; task_ci and change_task only add to change requests.
SERVICENOW_TABLES = {"incident": "Incidents", "change_request": "Change requests",
                     "task_ci": "Change requests' affected CIs", "change_task": "Change tasks",
                     "em_alert": "Event Management alerts", "em_event": "Event Management events"}
SERVICENOW_AUTH = ("basic", "oauth_browser", "oauth_client")
MONITOR_TYPES = ("newrelic", "datadog", "grafana")
# Each monitoring tool's settings (secrets live in the OS keychain, never here).
MONITOR_FIELDS = {
    "newrelic": {"region": ("us", "eu"), "account_ids": "ints", "log_attributes": "attribute_map"},
    "datadog": {"site": "text"},
    "grafana": {"url": "text", "auth": ("token", "azure")},
}
# favourite_* and tag_filters are read only until moved into saved scopes (legacy_scope); open_with replaced triage.scope.
LEGACY_TENANT_KEYS = ("favourite_subscriptions", "favourite_resource_groups", "tag_filters")
_TENANT_KEYS = {*LEGACY_TENANT_KEYS, "integrations", "saved_scopes", "alert_rules", "open_with", "workload_links"}
# Workload path: a workload's confirmed front doors (decision 150); each field is checked in _check_workload_links.
WORKLOAD_LINK_KEYS = {"key", "name", "cluster_id", "namespace", "kind", "site_id", "apim", "agw", "app", "component"}
SAVED_SCOPE_KEYS = {"name", "subscriptions", "resource_groups", "tag_filters", "connections"}
# What the page opens with: nothing, every subscription, or a saved scope by name (so no scope may use these two).
OPEN_WITH = ("none", "all")
LEGACY_SCOPE_NAME = "Favourites"
# Alert rules (phase 13): what each signal means, which a scope or the Watchlist can use, and the interval limits.
ALERT_SIGNALS = {"status": "A status gets worse", "new_red": "A new red resource",
                 "service_health": "A new Service Health event",
                 "servicenow": "A new ServiceNow incident on something in scope",
                 "monitors": "A monitoring tool alert opening on something in scope",
                 "last_week": "A metric doubles or halves against last week"}
ALERT_SIGNALS_BY_KIND = {"scope": ("status", "new_red", "service_health", "servicenow", "monitors"),
                         "watchlist": ("status", "last_week")}
ALERT_DEFAULT_SIGNALS = {"scope": ("status", "new_red", "service_health"), "watchlist": ("status",)}
ALERT_RULE_KEYS = {"name", "scope", "watchlist", "signals", "interval_minutes", "notify", "notify_recovered", "enabled",
                   "servicenow_priority"}
ALERT_MINUTES = {"min": 2, "max": 30, "scope_min": 5}
_TRIAGE_SCOPES = {"none", "favourites", "all"}


class ConfigError(ValueError):
    pass


# Log attributes that hold a resource's name, per Azure resource type (e.g. AKS clusters: cluster_name).
RESOURCE_TYPE = re.compile(r"^[a-z0-9.]+(/[a-z0-9.]+)+$")
LOG_ATTRIBUTE = re.compile(r"^[A-Za-z0-9_.:@ -]{1,100}$")

# A connection's name is shown in the Scope bar and keys its keychain item and environment variable.
CONNECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,39}$")


def check_connection_names(items, what):
    """Each name usable and unique within the list, ignoring case, spaces and punctuation, since names differing
    only by those share an environment variable (AHC_MONITOR_NR_PROD_API_KEY for "NR Prod" and "nr-prod")."""
    seen = {}
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Every {what} connection needs a name.")
        if not CONNECTION_NAME.match(name):
            raise ConfigError(f"'{name[:60]}' can't be used as a name: 1 to 40 characters, starting with a letter or digit, "
                              "using letters, digits, spaces and . _ - ( ) only.")
        key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if key in seen:
            same = seen[key] == name
            raise ConfigError(f"Two {what} connections are called '{name}'." if same else
                              f"'{seen[key]}' and '{name}' are too alike: names must differ by more than case, spaces "
                              "or punctuation, since each one's key is stored under its name.")
        seen[key] = name


# Every scalar setting: (section, key, type, help). Drives type validation and the Settings tab.
# type is "int", "bool", "text", or a tuple of allowed values.
SCHEMA = [
    ("triage", "auto_refresh_minutes", (0, 1, 2, 5, 10, 15),
     "Refresh Triage by itself while it's open and visible, marking what's new since the previous refresh. "
     "0 is off; each refresh costs about as much as pressing Refresh."),
    ("triage", "changes_hours", "int", "How far back Triage looks for changes on the resources it lists as problems, in hours."),
    ("triage", "include_system_changes", "bool", "Include changes made by Azure itself (noisy)."),
    ("triage", "azure_status_feed", "bool",
     "Read the public Azure status feed (azure.status.microsoft) for Azure-wide outages on the service status card."),
    ("triage", "capacity_near_max_percent", "int",
     "Triage lists a resource whose autoscale instance count is at this share of its maximum or more (at the maximum is red)."),
    ("triage", "resolved_hours", "int", "Triage keeps showing Service Health issues this many hours after they're resolved."),
    ("triage", "secret_scan_max_vaults", "int",
     "The Secrets tab scans Key Vaults automatically up to this many vaults; above it, a Scan now button."),
    ("graph", "auto", "bool",
     "Draw the dependency graph on every click. Off: a click checks only that resource (fast), "
     "and Show dependencies draws the graph."),
    ("graph", "depth", "int", "How many hops to follow from a selected resource."),
    ("graph", "max_nodes", "int", "Stop drawing a graph at this many resources."),
    ("graph", "inbound", "bool", "Also find resources that point at the selected one (private endpoints, gateways)."),
    ("graph", "inbound_limit", "int", "At most this many inbound resources."),
    ("graph", "inbound_scope", ("subscription", "tenant"),
     "Where to look for inbound references; tenant is slower but finds hub private endpoints."),
    ("health", "window_minutes", "int", "Metric window for \"now\", in minutes."),
    ("health", "compare_last_week", "bool", "Show the same window a week earlier next to each metric."),
    ("health", "changes_hours", "int", "Recent changes shown per resource, in hours."),
    ("health", "alert_hours", "int", "Stateless alerts older than this are ignored, in hours."),
    ("health", "critical_max_severity", "int", "Alerts at this severity or worse count as critical (Sev0 is worst)."),
    ("health", "warning_max_severity", "int", "Alerts at this severity or worse count as warning; milder ones are shown only."),
    ("health", "heuristic_limit", "int", "Metrics picked automatically for types without a profile."),
    ("health", "expiry_warning_days", "int", "Secrets, keys and certificates turn yellow this many days before expiry."),
    ("health", "expiry_critical_days", "int", "...and red this many days before expiry (expired is always red)."),
    ("health", "detectors", "bool", "Run Azure's health/risk detectors in each snapshot (AKS, App Service, Redis)."),
    ("health", "baseline_change_percent", "int",
     "A group's \"moved most against last week\": a metric at this share of last week's value or more (200 = doubled), "
     "or at its inverse or less (halved)."),
    ("cost", "spike_percent", "int", "Cost: a day at this share of the usual day or more (the median of the 7 before) is a spike."),
    ("vulnerabilities", "eol_warning_days", "int", "Security: an App Service runtime turns yellow this many days before its end of support."),
    ("network", "subnet_warning_percent", "int", "A subnet with this share of its usable IPs in use turns yellow."),
    ("network", "subnet_critical_percent", "int", "...and red at this share."),
    ("audit", "keep_days", "int",
     "Access audit: keep its last result per scope on this machine this many days, so it opens at once with its age "
     "shown; Refresh reads it again. 0 keeps nothing. Readable only by you; Clear cached data removes it."),
    ("alerts", "history_hours", "int",
     "How long the Alerts bar keeps the console's own alerts, in hours (1 to 168). Kept in memory, at most 500 per "
     "sign-in: a restart clears them."),
    ("alert_check", "days", "int", "Is this alert right?: how many days of history to read (1 to 30)."),
    ("alert_check", "min_buckets", "int", "...the fewest values needed before any verdict is given."),
    ("alert_check", "always_on_percent", "int",
     "...a rule true in at least this share of the history fires on ordinary values: its threshold is set too close."),
    ("alert_check", "rare_percent", "int", "...a rule true now but in at most this share of the history is unusual."),
    ("alert_check", "change_percent", "int",
     "...now this much higher (or lower) than the same time last week counts as a change, e.g. 50 = 1.5×."),
    ("aks", "fetch_credentials", "bool",
     "Fetch the AKS user credential from Azure when no local kubeconfig context matches."),
    ("aks", "memory_near_limit_percent", "int", "A container using this share of its memory limit or more is near being OOMKilled (red)."),
    ("aks", "cpu_near_limit_percent", "int", "A container using this share of its CPU limit or more is being throttled (red)."),
    ("aks", "cpu_above_request_percent", "int", "A container using more than this share of its CPU request relies on spare node CPU (yellow)."),
    ("aks", "cpu_idle_percent", "int", "A container using less than this share of its CPU request has CPU reserved and unused (a setup note)."),
    ("aks", "hpa_target_high_percent", "int", "An HPA utilization target above this is high: pods are nearly full before it adds more."),
    ("aks", "hpa_near_max_percent", "int", "An HPA at this share of its maxReplicas or more has little room left (at the maximum is red)."),
    ("aks", "hpa_overshoot_percent", "int", "An HPA metric at this share of its target or more means the request is too low (150 = 1.5×)."),
    ("aks", "pool_requests_percent", "int", "A node pool whose pods request this share of its allocatable CPU or memory has little room left."),
    ("aks", "restarts_warning", "int", "Container restarts from this many on are a warning; fewer are only shown."),
    ("aks", "leftover_hours", "int", "A Failed pod older than this many hours is left over (usually already replaced): listed apart on Workloads, "
     "and info rather than a warning on Check and the AKS card."),
    ("aks", "rollout_hours", "int", "A rollout within this many hours shows on its workload as recent."),
    ("aks", "node_image_warning_days", "int", "A node pool's OS image this many days old turns yellow (Nodes and Security)."),
    ("aks", "node_image_critical_days", "int", "...and red at this age."),
    ("aks", "restarts_look_back_hours", LOOK_BACK_HOURS, "Workloads' Look back on *default*: restarts are counted over this many hours."),
    ("aks", "oom_look_back_hours", LOOK_BACK_HOURS, "...OOM kills are listed over this many hours (Container Insights and the monitoring tool)."),
    ("aks", "events_look_back_hours", LOOK_BACK_HOURS, "...warning events are listed over this many hours."),
    ("aks", "oom_near_limit_percent", "int",
     "An OOM kill is probably the container's own limit when memory was at this share of the limit just before (no kernel message)."),
    ("aks", "oom_node_full_percent", "int", "...and probably the node running out when the node was at this share of its memory."),
    ("appinsights", "failed_min", "int", "App Insights: failed requests in the last hour before it can count as failing more (Triage, Investigate, Path)."),
    ("appinsights", "failed_rate_points", "int", "...and its failure rate must be this many points above the same hour last week."),
    ("appinsights", "exceptions_min", "int", "...exceptions in the last hour before it can count as throwing more."),
    ("appinsights", "exceptions_percent", "int", "...and they must be at this share of the same hour last week or more (200 = doubled)."),
    ("appinsights", "critical_rate_percent", "int", "...failing turns red at this failure rate or more..."),
    ("appinsights", "critical_failed_min", "int", "...with at least this many failed requests."),
    ("path", "drop_percent", "int", "Path: a hop's traffic under this share of the same hour last week is a drop..."),
    ("path", "drop_min", "int", "...when that hour last week had at least this many requests."),
    ("path", "restarts_min", "int", "Path: pods restarting this many times in the hour (and twice last week's) are rising..."),
    ("path", "restarts_critical", "int", "...red from this many."),
    ("security", "risky_ports", "ports",
     "Security: ports that are a finding when open to the internet (comma-separated numbers)."),
    ("summary", "top_items", "int", "Summary: how many items each card lists (problems, findings, subscriptions)."),
    ("summary", "readiness_quick", "bool",
     "Summary runs Readiness's quick checks (scaling, quotas, subnets, single instances) when it opens. Kubernetes and "
     "Key Vault checks stay on the Readiness page."),
    ("summary", "budget_warn_percent", "int", "Summary: a budget spent or forecast past this share turns yellow (100% is red)."),
    ("summary", "compare_hours", "int",
     "Summary's What changed compares with the summary from at least this many hours ago (24 = since yesterday)."),
    ("summary", "keep_days", "int", "Summary: how many days of summaries are kept on this machine for What changed."),
    ("updates", "check", "bool",
     "Ask PyPI, once every 6 hours while the page is open, whether a newer version is out, and say so in the account "
     "menu. Nothing about you is sent; off for machines that can't reach PyPI or teams that pin a version."),
    (None, "persona", "text",
     "The persona the console opens with: its home page, which views lead the menu, card order and detail. Empty asks "
     "on first run, then keeps the last one picked in this browser. View as still switches it for the session."),
    (None, "default_tenant", "text", "Tenant ID to open with; empty follows the az CLI default."),
    (None, "watchlist_file", "text", "Where the Watchlist is saved."),
    (None, "profiles_dir", "text", "Folder with your own health profiles."),
    (None, "exports_dir", "text", "Where the export command writes files."),
    (None, "cache_dir", "text", "Where costs are kept between runs (one file per tenant and account, readable only by you)."),
    (None, "pdf_font", "text", "A .ttf font for PDFs with non-Latin names; empty uses built-in fonts."),
    ("logs", "dir", "text", "Where this machine's log is kept: errors, what was done in the console and every request it "
     "sent (Settings → Logs). Readable only by you."),
    ("logs", "keep_days", "int", "Days the log keeps, 1 to 365; older days are deleted."),
]


def _check_types(settings, where):
    for section, key, kind, _help in SCHEMA:
        value = settings[section][key] if section else settings[key]
        label = f"{section}.{key}" if section else key
        if value is None and kind == "text":
            continue
        if kind == "int" and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ConfigError(f"{where}: {label} must be a whole number, 0 or more (got {value!r}).")
        if kind == "bool" and not isinstance(value, bool):
            raise ConfigError(f"{where}: {label} must be true or false (got {value!r}).")
        if kind == "ports" and not (isinstance(value, list) and all(
                isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535 for p in value)):
            raise ConfigError(f"{where}: {label} must be a list of port numbers, 1 to 65535 (got {value!r}).")
        if kind == "text" and not isinstance(value, str):
            raise ConfigError(f"{where}: {label} must be text (got {value!r}).")
        if isinstance(kind, tuple) and (isinstance(value, bool) or value not in kind):
            raise ConfigError(f"{where}: {label} must be one of {list(kind)} (got {value!r}).")
    if not 1 <= settings["logs"]["keep_days"] <= 365:
        raise ConfigError(f"{where}: logs.keep_days must be 1 to 365 (got {settings['logs']['keep_days']}).")
    if not 1 <= settings["alerts"]["history_hours"] <= 168:
        raise ConfigError(f"{where}: alerts.history_hours must be 1 to 168 (got {settings['alerts']['history_hours']}).")
    check = settings["alert_check"]
    if not 1 <= check["days"] <= 30:
        raise ConfigError(f"{where}: alert_check.days must be 1 to 30 (got {check['days']}).")
    for key in ("always_on_percent", "rare_percent"):
        if check[key] > 100:
            raise ConfigError(f"{where}: alert_check.{key} is a share of the history, 0 to 100 (got {check[key]}).")
    if check["rare_percent"] >= check["always_on_percent"]:
        raise ConfigError(f"{where}: alert_check.rare_percent must be below alert_check.always_on_percent.")
    if not 1 <= check["change_percent"] <= 1000 or check["min_buckets"] < 2:
        raise ConfigError(f"{where}: alert_check.change_percent must be 1 to 1000 and alert_check.min_buckets 2 or more.")
    _check_integrations(settings["integrations"], where)
    for tenant, entry in settings["tenants"].items():
        if "integrations" in entry:
            _check_integrations(entry["integrations"], f"{where}: tenants.{tenant}")
    ignore = settings["audit"]["ignore_actions"]
    if not isinstance(ignore, list) or not all(isinstance(a, str) and a.strip() for a in ignore):
        raise ConfigError(f"{where}: audit.ignore_actions must be a list of actions, e.g. Microsoft.Support/*.")
    paths = settings["aks"]["kubeconfig_paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ConfigError(f"{where}: aks.kubeconfig_paths must be a list of paths.")
    for tenant, entry in settings["tenants"].items():
        for key in ("favourite_subscriptions", "favourite_resource_groups"):
            values = entry.get(key) or []
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ConfigError(f"{where}: tenants.{tenant}.{key} must be a list of names or patterns.")
        if not isinstance(entry.get("tag_filters") or {}, dict):
            raise ConfigError(f"{where}: tenants.{tenant}.tag_filters must be a mapping of tag: value.")
        _check_saved_scopes(entry.get("saved_scopes"), f"{where}: tenants.{tenant}.saved_scopes")
        if "open_with" in entry and (not isinstance(entry["open_with"], str) or not entry["open_with"].strip()):
            raise ConfigError(f"{where}: tenants.{tenant}.open_with must be none, all or a saved scope's name.")
        check_alert_rules(entry.get("alert_rules"), f"{where}: tenants.{tenant}.alert_rules")
        check_workload_links(entry.get("workload_links"), f"{where}: tenants.{tenant}.workload_links")


def check_workload_links(links, where="workload_links"):
    """Each link names its workload (an AKS one by cluster, namespace, kind and name, or an App Service by ID) and its
    front doors: API Management APIs (service_id, api), App Gateway routes (gateway_id, listener, pool), the monitoring
    tool's app name and an App Insights component ID, all optional."""
    if links is None:
        return
    if not isinstance(links, list):
        raise ConfigError(f"{where} must be a list.")
    keys = set()
    for index, link in enumerate(links):
        label = f"{where}[{index}]"
        if not isinstance(link, dict) or not isinstance(link.get("key"), str) or not link["key"].strip():
            raise ConfigError(f"{label} needs a key (the workload it belongs to).")
        unknown = set(link) - WORKLOAD_LINK_KEYS
        if unknown:
            raise ConfigError(f"{label} unknown key(s) {sorted(unknown)}; allowed {sorted(WORKLOAD_LINK_KEYS)}.")
        if link["key"].lower() in keys:
            raise ConfigError(f"{where}: two links for {link['key']}.")
        keys.add(link["key"].lower())
        if not (link.get("site_id") or all(isinstance(link.get(k), str) and link[k] for k in ("cluster_id", "namespace", "kind"))):
            raise ConfigError(f"{label} needs site_id, or cluster_id, namespace and kind.")
        for field, required in (("apim", ("service_id", "api")), ("agw", ("gateway_id", "listener", "pool"))):
            items = link.get(field) or []
            if not isinstance(items, list) or not all(isinstance(i, dict) and all(isinstance(i.get(r), str) and i[r] for r in required)
                                                      for i in items):
                raise ConfigError(f"{label}.{field} must be a list, each with {', '.join(required)}.")
        for field in ("name", "app", "component", "site_id", "cluster_id", "namespace", "kind"):
            if field in link and link[field] is not None and not isinstance(link[field], str):
                raise ConfigError(f"{label}.{field} must be text.")


def workload_links(settings, tenant):
    """This tenant's saved workload links (its own entry, never "*")."""
    key = next((k for k in entry_keys(settings, tenant) if settings["tenants"][k].get("workload_links")), None)
    return [dict(link) for link in settings["tenants"][key]["workload_links"]] if key else []


def check_alert_rules(rules, where="alert_rules"):
    """Each rule watches one saved scope (`scope: <name>`) or the Watchlist (`watchlist: true`), never both."""
    if rules is None:
        return
    if not isinstance(rules, list):
        raise ConfigError(f"{where} must be a list of rules.")
    names = set()
    for index, rule in enumerate(rules):
        label = f"{where}[{index}]"
        if not isinstance(rule, dict) or not isinstance(rule.get("name"), str) or not rule["name"].strip():
            raise ConfigError(f"{label} needs a name.")
        label = f"{where} '{rule['name']}'"
        unknown = set(rule) - ALERT_RULE_KEYS
        if unknown:
            raise ConfigError(f"{label}: unknown key(s) {sorted(unknown)}; allowed {sorted(ALERT_RULE_KEYS)}.")
        if rule["name"].strip().lower() in names:
            raise ConfigError(f"{where}: two rules are called '{rule['name']}'.")
        names.add(rule["name"].strip().lower())
        watchlist, scope = rule.get("watchlist", False), rule.get("scope")
        if not isinstance(watchlist, bool):
            raise ConfigError(f"{label}: watchlist must be true or false.")
        if watchlist == bool(isinstance(scope, str) and scope.strip()):
            raise ConfigError(f"{label}: set either scope (a saved scope's name) or watchlist: true, not both.")
        if scope is not None and not isinstance(scope, str):
            raise ConfigError(f"{label}: scope must be a saved scope's name.")
        kind = "watchlist" if watchlist else "scope"
        signals = rule.get("signals")
        allowed = ALERT_SIGNALS_BY_KIND[kind]
        if signals is not None and (not isinstance(signals, list) or not signals
                                    or not all(isinstance(s, str) and s in allowed for s in signals)):
            raise ConfigError(f"{label}: signals must be a non-empty list from {list(allowed)}.")
        minutes = rule.get("interval_minutes", ALERT_MINUTES["scope_min"])
        floor = ALERT_MINUTES["min"] if watchlist else ALERT_MINUTES["scope_min"]
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not floor <= minutes <= ALERT_MINUTES["max"]:
            raise ConfigError(f"{label}: interval_minutes must be {floor} to {ALERT_MINUTES['max']}"
                              + (" (5 for a scope: each check queries the whole scope)." if not watchlist else "."))
        priority = rule.get("servicenow_priority", 2)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5:
            raise ConfigError(f"{label}: servicenow_priority must be 1 to 5 (2 alerts on P1 and P2).")
        for key in ("notify", "notify_recovered", "enabled"):
            if not isinstance(rule.get(key, True), bool):
                raise ConfigError(f"{label}: {key} must be true or false.")


def _check_saved_scopes(scopes, where):
    if scopes is None:
        return
    if not isinstance(scopes, list):
        raise ConfigError(f"{where} must be a list of scopes.")
    names = set()
    for index, scope in enumerate(scopes):
        label = f"{where}[{index}]"
        if not isinstance(scope, dict) or not isinstance(scope.get("name"), str) or not scope["name"].strip():
            raise ConfigError(f"{label} needs a name.")
        unknown = set(scope) - SAVED_SCOPE_KEYS
        if unknown:
            raise ConfigError(f"{label} unknown key(s) {sorted(unknown)}; allowed {sorted(SAVED_SCOPE_KEYS)}.")
        if scope["name"].lower() in names:
            raise ConfigError(f"{where}: two scopes are called '{scope['name']}'.")
        if scope["name"].strip().lower() in OPEN_WITH:
            raise ConfigError(f"{label}: '{scope['name']}' is reserved (Open with uses {' and '.join(OPEN_WITH)}); "
                              "pick another name, such as 'All subscriptions'.")
        names.add(scope["name"].lower())
        for key in ("subscriptions", "resource_groups"):
            values = scope.get(key) or []
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ConfigError(f"{label}.{key} must be a list of names or patterns.")
        for key in ("tag_filters", "connections"):
            values = scope.get(key) or {}
            if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
                raise ConfigError(f"{label}.{key} must be a mapping of text to text.")


def _check_integrations(integrations, where):
    for items, what in ((integrations["servicenow"], "ServiceNow"), (integrations["monitors"], "monitoring")):
        try:
            check_connection_names(items, what)
        except ConfigError as error:
            raise ConfigError(f"{where}: {error}") from None
    names = set()
    for index, sn in enumerate(integrations["servicenow"]):
        label = f"integrations.servicenow[{index}]"
        for key, kind in (("name", str), ("enabled", bool), ("instance", str), ("username", str), ("client_id", str),
                          ("incident_filter", str), ("max_records", int), ("change_history_days", int),
                          ("change_ahead_days", int), ("change_max_records", int), ("incident_max_records", int)):
            if not isinstance(sn.get(key), kind) or (kind is int and isinstance(sn.get(key), bool)):
                raise ConfigError(f"{where}: {label}.{key} has the wrong type (got {sn.get(key)!r}).")
        if not sn["name"].strip():
            raise ConfigError(f"{where}: {label} needs a name.")
        if sn["name"].lower() in names:
            raise ConfigError(f"{where}: two ServiceNow connections are called '{sn['name']}'.")
        names.add(sn["name"].lower())
        if sn["auth"] not in SERVICENOW_AUTH:
            raise ConfigError(f"{where}: {label}.auth must be one of {list(SERVICENOW_AUTH)}.")
        for key in ("tables", "match_fields", "change_match_fields"):
            if not isinstance(sn[key], list) or not all(isinstance(v, str) and v for v in sn[key]):
                raise ConfigError(f"{where}: {label}.{key} must be a list of names.")
        for key in ("change_history_days", "change_ahead_days"):
            if not 0 <= sn[key] <= 365:
                raise ConfigError(f"{where}: {label}.{key} must be between 0 and 365 days.")
        for key in ("change_max_records", "incident_max_records"):
            if not 1 <= sn[key] <= 50000:
                raise ConfigError(f"{where}: {label}.{key} must be between 1 and 50000.")
        if sn["instance"] and not sn["instance"].lower().startswith("https://"):
            raise ConfigError(f"{where}: {label}.instance must start with https://.")
    monitors = integrations["monitors"]
    if not isinstance(monitors, list):
        raise ConfigError(f"{where}: integrations.monitors must be a list of connections.")
    names = set()
    for index, monitor in enumerate(monitors):
        label = f"integrations.monitors[{index}]"
        if not isinstance(monitor, dict) or not str(monitor.get("name") or "").strip():
            raise ConfigError(f"{where}: {label} needs a name.")
        if monitor["name"].lower() in names:
            raise ConfigError(f"{where}: two monitor connections are called '{monitor['name']}'.")
        names.add(monitor["name"].lower())
        kind = monitor.get("type")
        if kind not in MONITOR_TYPES:
            raise ConfigError(f"{where}: {label}.type must be one of {list(MONITOR_TYPES)}.")
        allowed = {"name", "type", "enabled"} | set(MONITOR_FIELDS[kind])
        unknown = set(monitor) - allowed
        if unknown:
            raise ConfigError(f"{where}: {label} unknown key(s) {sorted(unknown)} for {kind}.")
        monitor.setdefault("enabled", False)
        if not isinstance(monitor["enabled"], bool):
            raise ConfigError(f"{where}: {label}.enabled must be true or false.")
        for key, rule in MONITOR_FIELDS[kind].items():
            value = monitor.get(key)
            if value is None:
                continue
            if isinstance(rule, tuple) and value not in rule:
                raise ConfigError(f"{where}: {label}.{key} must be one of {list(rule)}.")
            if rule == "ints" and (not isinstance(value, list) or not all(isinstance(v, int) for v in value)):
                raise ConfigError(f"{where}: {label}.{key} must be a list of numbers.")
            if rule == "text" and not isinstance(value, str):
                raise ConfigError(f"{where}: {label}.{key} must be text.")
            if rule == "attribute_map" and (not isinstance(value, dict) or not all(
                    isinstance(k, str) and RESOURCE_TYPE.match(k) and isinstance(v, str) and LOG_ATTRIBUTE.match(v)
                    for k, v in value.items())):
                raise ConfigError(f"{where}: {label}.{key} must map Azure resource types (e.g. "
                                  "microsoft.containerservice/managedclusters) to log attribute names.")


def path():
    return Path(os.environ.get("AHC_CONFIG") or DEFAULT_PATH).expanduser()


def load(config_path=None):
    file = Path(config_path).expanduser() if config_path else path()
    if not file.is_file():
        return copy.deepcopy(DEFAULTS)
    try:
        content = localfiles.read_trusted(file, "Settings")
    except localfiles.UnsafePath as error:
        raise ConfigError(str(error)) from error
    return from_data(yamlio.load(content) or {}, str(file))


def from_data(data, where="settings"):
    """Defaults with `data` merged on top, validated; raises ConfigError on any mistake."""
    settings = copy.deepcopy(DEFAULTS)
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping of settings.")
    _merge(settings, data, where)
    _check_types(settings, where)
    return settings


def _merge(settings, data, where):
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}; allowed {sorted(DEFAULTS)}.")
    for key, value in data.items():
        if key == "personas":
            from . import personas  # personas imports nothing from here
            wrong = personas.problems(value or {}, f"{where}: personas")
            if wrong:
                raise ConfigError("\n".join(wrong))
            settings["personas"] = dict(value or {})
            continue
        if isinstance(DEFAULTS[key], dict) and key != "tenants":
            unknown = set(value or {}) - set(DEFAULTS[key])
            if unknown:
                raise ConfigError(f"{where}: unknown {key} key(s) {sorted(unknown)}.")
            if key == "integrations":
                _merge_integrations(settings, value or {}, where)
                continue
            settings[key].update(value or {})
        elif key == "tenants":
            if not isinstance(value, dict):
                raise ConfigError(f"{where}: tenants must be a mapping of tenant (ID, domain or \"*\") to settings.")
            for tenant_id, tenant in (value or {}).items():
                unknown = set(tenant or {}) - _TENANT_KEYS
                if unknown:
                    raise ConfigError(f"{where}: tenants.{tenant_id} unknown key(s) {sorted(unknown)}.")
            settings["tenants"] = {str(k).lower(): dict(v or {}) for k, v in (value or {}).items()}
            for tenant_id, entry in settings["tenants"].items():
                if "integrations" in entry:
                    entry["integrations"] = _integrations_from(entry["integrations"], f"{where}: tenants.{tenant_id}")
        else:
            settings[key] = value
    if settings["triage"]["scope"] not in _TRIAGE_SCOPES:
        raise ConfigError(f"{where}: triage.scope must be one of {sorted(_TRIAGE_SCOPES)}.")
    if settings["persona"]:
        from . import personas
        names = [p["name"] for p in personas.merged(settings["personas"])]
        if settings["persona"] not in names:
            raise ConfigError(f"{where}: persona '{settings['persona']}' isn't defined; choose from {names}, or leave it empty.")


def _merge_integrations(settings, value, where):
    settings["integrations"] = _integrations_from(value, where)


def _integrations_from(value, where):
    """Defaults with one integrations block merged on top (the shape checks come later, in _check_integrations)."""
    if not isinstance(value or {}, dict):
        raise ConfigError(f"{where}: integrations must be a mapping.")
    value = value or {}
    unknown = set(value) - set(DEFAULTS["integrations"])
    if unknown:
        raise ConfigError(f"{where}: unknown integrations key(s) {sorted(unknown)}.")
    out = copy.deepcopy(DEFAULTS["integrations"])
    sn = value.get("servicenow") or []
    if isinstance(sn, dict):  # one connection, as saved before there could be several
        sn = [sn] if sn.get("instance") or sn.get("enabled") else []
    if not isinstance(sn, list):
        raise ConfigError(f"{where}: integrations.servicenow must be a list of connections.")
    for index, item in enumerate(sn):
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: integrations.servicenow[{index}] must be a mapping.")
        unknown = set(item) - set(SERVICENOW_DEFAULTS)
        if unknown:
            raise ConfigError(f"{where}: unknown integrations.servicenow[{index}] key(s) {sorted(unknown)}.")
        out["servicenow"].append({**copy.deepcopy(SERVICENOW_DEFAULTS), **copy.deepcopy(item)})
    out["monitors"] = copy.deepcopy(value.get("monitors") or [])
    return out


def entry_keys(settings, tenant, wildcard=False):
    """The `tenants` keys this tenant's settings are under, best first: its ID, domain, display name (then "*")."""
    names = (tenant.get("id"), tenant.get("domain"), tenant.get("name")) + (("*",) if wildcard else ())
    return [n.lower() for n in names if n and n.lower() in settings["tenants"]]


def tenant_integrations(settings, tenant):
    """(key, integrations) for this tenant; never "*", so one tenant's ServiceNow or monitoring tools never show up in
    another. Defaults (all off) when it has none."""
    key = next((k for k in entry_keys(settings, tenant) if "integrations" in settings["tenants"][k]), None)
    return (key, settings["tenants"][key]["integrations"]) if key else (None, copy.deepcopy(DEFAULTS["integrations"]))


def tenant_key(settings, tenant):
    """Where this tenant's settings are saved: its existing entry (not "*"), else its domain, else its ID."""
    keys = entry_keys(settings, tenant)
    return keys[0] if keys else (tenant.get("domain") or tenant.get("id") or "").lower()


def _own_entry(settings, tenant):
    """This tenant's own entry (never "*"), or None."""
    keys = entry_keys(settings, tenant)
    return settings["tenants"][keys[0]] if keys else None


def stored_scopes(settings, tenant):
    """The saved scopes in this tenant's own entry (never "*": they name this tenant's connections)."""
    entry = _own_entry(settings, tenant) or {}
    return [{"name": s["name"], "subscriptions": list(s.get("subscriptions") or []),
             "resource_groups": list(s.get("resource_groups") or []), "tag_filters": dict(s.get("tag_filters") or {}),
             "connections": dict(s.get("connections") or {})} for s in entry.get("saved_scopes") or []]


def legacy_scope(settings, tenant):
    """Favourites, default tag filters and triage.scope from before saved scopes did their job, until this tenant
    has its own open_with (Move writes it): {"scopes": what Move would save, "open_with", "keys": what is there}."""
    own = _own_entry(settings, tenant)
    if own is not None and "open_with" in own:
        return None
    entry = tenant_settings(settings, tenant)
    subs = [str(s) for s in entry.get("favourite_subscriptions") or []]
    groups = [str(g) for g in entry.get("favourite_resource_groups") or []]
    tags = {str(k).lower(): str(v) for k, v in (entry.get("tag_filters") or {}).items()}
    opened = settings["triage"]["scope"]
    keys = [k for k in LEGACY_TENANT_KEYS if entry.get(k)] + (["triage.scope"] if opened != "none" else [])
    if not keys:
        return None
    taken = {s["name"].lower() for s in stored_scopes(settings, tenant)}

    def free(name):
        return next(n for n in (name, f"{name} (moved)", f"{name} (moved 2)") if n.lower() not in taken)

    scopes = []
    if subs or tags:
        scopes.append({"name": free(LEGACY_SCOPE_NAME), "subscriptions": subs or ["*"], "resource_groups": [],
                       "tag_filters": tags, "connections": {}})
    if groups:
        scopes.append({"name": free("Favourite groups"), "subscriptions": subs or ["*"], "resource_groups": groups,
                       "tag_filters": {}, "connections": {}})
    if opened == "favourites":
        open_with = scopes[0]["name"] if subs else "all"  # it fell back to all when no favourite was set
    else:
        open_with = opened if opened in OPEN_WITH else "none"
    return {"scopes": scopes, "open_with": open_with, "keys": keys}


def saved_scopes(settings, tenant):
    """This tenant's saved scopes, then (until moved) the ones its old favourites make, marked `legacy`."""
    legacy = legacy_scope(settings, tenant)
    return stored_scopes(settings, tenant) + [{**s, "legacy": True} for s in (legacy or {}).get("scopes", [])]


def open_with(settings, tenant):
    """What the page opens with: "none", "all" or a saved scope's name (which may no longer exist: the page says so)."""
    own = _own_entry(settings, tenant)
    if own is not None and "open_with" in own:
        return own["open_with"].strip()
    legacy = legacy_scope(settings, tenant)
    return legacy["open_with"] if legacy else "none"


def scope_subscription_ids(scope, subscriptions):
    """The IDs of `subscriptions` a saved scope's names or patterns match."""
    patterns = [str(p).lower() for p in scope.get("subscriptions") or []]
    return [s["id"] for s in subscriptions if patterns and matches(patterns, s["name"], s["id"])]


def find_scope(settings, tenant, name):
    wanted = (name or "").strip().lower()
    return next((s for s in saved_scopes(settings, tenant) if s["name"].lower() == wanted), None)


def chosen_subscriptions(settings, tenant, subscriptions, scope_name=None):
    """(label, subscription IDs) for the CLI: the saved scope named, else what the tenant opens with; ([] when that's
    nothing). Raises ConfigError for a scope name that doesn't exist."""
    name = scope_name or open_with(settings, tenant)
    if name == "all" and not scope_name:
        return "every subscription (Open with)", [s["id"] for s in subscriptions]
    if name == "none" and not scope_name:
        return "nothing (Open with)", []
    scope = find_scope(settings, tenant, name)
    if scope is None:
        raise ConfigError(f"No saved scope is called '{name}'.")
    return f"saved scope {scope['name']}", scope_subscription_ids(scope, subscriptions)


def alert_rules(settings, tenant):
    """This tenant's alert rules with defaults filled in, from its own entry only (they name its saved scopes)."""
    rules = []
    for rule in (_own_entry(settings, tenant) or {}).get("alert_rules") or []:
        kind = "watchlist" if rule.get("watchlist") else "scope"
        rules.append({"name": rule["name"].strip(), "kind": kind,
                      "scope": None if kind == "watchlist" else rule["scope"].strip(),
                      "signals": list(rule.get("signals") or ALERT_DEFAULT_SIGNALS[kind]),
                      "servicenow_priority": rule.get("servicenow_priority", 2),
                      "interval_minutes": rule.get("interval_minutes", ALERT_MINUTES["scope_min"]),
                      "notify": rule.get("notify", True), "notify_recovered": rule.get("notify_recovered", True),
                      "enabled": rule.get("enabled", True)})
    return rules


def legacy_integrations(settings):
    """The old global block, when it holds anything: offered for moving into a tenant, used for nothing else."""
    block = settings["integrations"]
    return block if block["servicenow"] or block["monitors"] else None


def tenant_settings(settings, tenant):
    """The tenants entry for this tenant, matched by ID, then domain, then display name, then "*"."""
    keys = entry_keys(settings, tenant, wildcard=True)
    return settings["tenants"][keys[0]] if keys else {}


def matches(patterns, *values):
    """Case-insensitive glob match of any value against any pattern ("app-*-002")."""
    lowered = [str(v).lower() for v in values if v]
    return any(fnmatch.fnmatchcase(v, p.lower()) for p in patterns for v in lowered)
