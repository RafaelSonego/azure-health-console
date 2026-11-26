"""API Management in front of the apps: where each service logs, so one request can be followed through the gateway.
A logger to Application Insights shares the request's operation ID (the gateway forwards `traceparent`); gateway logs
sent to a workspace don't, so those are read by time and path and marked probable."""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from ..arm import ArmError
from ..explore import aks, appinsights
from .appinsights import kql_string

APIM_TYPE = "microsoft.apimanagement/service"
APIM_API_VERSION = "2022-08-01"
DIAGNOSTICS_API_VERSION = "2021-05-01-preview"
MAX_GATEWAYS = 100  # only bounds a huge tenant: each is two reads, eight at a time
GATEWAY_SPREAD = timedelta(minutes=2)
GATEWAY_ROWS = 50
URL_PATH = re.compile(r"^/[A-Za-z0-9/_.~%:@!$&'()*+,;=-]{0,300}$")
GATEWAY_LOG_CATEGORIES = {"gatewaylogs", "alllogs"}
COLUMNS = ("TimeGenerated", "Method", "Url", "ResponseCode", "BackendResponseCode", "TotalTime", "BackendTime", "ApiId",
           "OperationId", "LastErrorReason", "LastErrorMessage", "CorrelationId")


def _loggers(client, service_id):
    """(App Insights components the service logs to, loggers to App Insights that name no component)."""
    components, unnamed = [], 0
    for logger in client.get_paged(f"{service_id}/loggers", APIM_API_VERSION) or []:
        props = logger.get("properties") or {}
        if (props.get("loggerType") or "").lower() != "applicationinsights":
            continue
        found = appinsights.COMPONENT_ID.search(props.get("resourceId") or "")
        if found:
            components.append(found.group(0))
        else:
            unnamed += 1
    return list(dict.fromkeys(components)), unnamed


def link(client, service_id, picked=""):
    """The component an API Management service logs to, by its App Insights loggers, in the shape of
    appinsights.link: the first logger's, or your pick among them."""
    components, _ = _loggers(client, service_id)
    candidates = [{"id": c, "name": c.rsplit("/", 1)[-1]} for c in components]
    if picked and any(c.lower() == picked.lower() for c in components):
        return {"how": "picked", "component_id": picked, "component_name": picked.rsplit("/", 1)[-1], "candidates": candidates}
    if not components:
        return {"how": None, "component_id": "", "component_name": "", "candidates": [], "gateway": True}
    return {"how": "logger", "component_id": components[0], "component_name": candidates[0]["name"],
            "candidates": candidates if len(candidates) > 1 else []}


def _workspace_logs(client, service_id, categories=GATEWAY_LOG_CATEGORIES):
    """(whether its logs of `categories` go to a Log Analytics workspace, whether into their own table)."""
    payload = client.get(f"{service_id}/providers/microsoft.insights/diagnosticSettings", DIAGNOSTICS_API_VERSION,
                         tolerate=(400, 403, 404)) or {}
    for setting in payload.get("value") or []:
        props = setting.get("properties") or {}
        enabled = {(log.get("category") or log.get("categoryGroup") or "").lower()
                   for log in props.get("logs") or [] if log.get("enabled")}
        if props.get("workspaceId") and enabled & set(categories):
            return True, (props.get("logAnalyticsDestinationType") or "").lower() == "dedicated"
    return False, False


def gateways(client, subscriptions):
    """API Management services in scope and where each logs: App Insights components (followed by operation ID) and a
    workspace (gateway logs around a time)."""
    rows = client.query(f"Resources | where type =~ '{APIM_TYPE}'"
                        " | project id, name, resourceGroup, subscriptionId | order by name asc",
                        subscriptions=list(subscriptions) or None)

    def read(row):
        out = {"id": row["id"], "name": row["name"], "resource_group": row.get("resourceGroup") or "",
               "subscription_id": (row.get("subscriptionId") or "").lower(), "components": [], "unnamed_loggers": 0,
               "workspace": False, "dedicated": False, "error": ""}
        try:
            out["components"], out["unnamed_loggers"] = _loggers(client, row["id"])
            out["workspace"], out["dedicated"] = _workspace_logs(client, row["id"])
        except ArmError as error:
            out["error"] = appinsights.why(error)
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = list(pool.map(read, rows[:MAX_GATEWAYS]))
    notes = [f"Read {MAX_GATEWAYS} of {len(rows)} API Management services in scope (A–Z); narrow the scope for the rest."]\
        if len(rows) > MAX_GATEWAYS else []
    return {"gateways": found, "notes": notes}


def log_queries(service_id, at, path=""):
    """Gateway log rows within GATEWAY_SPREAD of `at` whose URL contains `path`: the resource-specific table first,
    then AzureDiagnostics for services that still log there."""
    if path and not URL_PATH.match(path):
        raise ValueError("path must be a URL path")
    start, end = at - GATEWAY_SPREAD, at + GATEWAY_SPREAD
    when = f"TimeGenerated between (datetime({appinsights.iso(start)}) .. datetime({appinsights.iso(end)}))"
    tail = f" | project {', '.join(COLUMNS)} | order by TimeGenerated desc | take {GATEWAY_ROWS}"
    dedicated = (f"ApiManagementGatewayLogs | where {when}" + (f" and Url contains {kql_string(path)}" if path else "") + tail)
    legacy = (f"AzureDiagnostics | where {when} and ResourceProvider == 'MICROSOFT.APIMANAGEMENT' and Category == 'GatewayLogs'"
              + (f" and url_s contains {kql_string(path)}" if path else "")
              + " | extend Method=method_s, Url=url_s, ResponseCode=toint(responseCode_d),"
                " BackendResponseCode=toint(backendResponseCode_d), TotalTime=DurationMs, BackendTime=backendTime_d,"
                " ApiId=apiId_s, OperationId=operationId_s, LastErrorReason=lastError_reason_s,"
                " LastErrorMessage=lastError_message_s" + tail)
    span = f"{appinsights.iso(start)}/{appinsights.iso(end)}"
    return [{"label": "ApiManagementGatewayLogs", "query": dedicated, "timespan": span},
            {"label": "AzureDiagnostics", "query": legacy, "timespan": span}]


def logs(client, service_id, at, path="", dedicated=None):
    """{"rows", "table", "queries", "errors"}: the first table with rows answers; the service's own table first
    (`dedicated`, from its diagnostic setting), and when it's known to log to AzureDiagnostics that one alone."""
    queries, errors = log_queries(service_id, at, path), []
    if dedicated is False:
        queries = queries[1:]
    for query in queries:
        try:
            rows = aks.resource_logs(client, service_id, query["query"], query["timespan"])
        except ArmError as error:
            errors.append(f"{query['label']}: {str(error)[:200]}")
            continue
        if rows:
            return {"rows": rows, "table": query["label"], "queries": [query], "errors": [], "probable": True}
    return {"rows": [], "table": "", "queries": queries, "errors": errors if len(errors) == len(queries) else [],
            "probable": True}
