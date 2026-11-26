"""Workload path: one workload and the hops in front of it (App Gateway, API Management), each read from wherever its
data is, last hour against the same hour last week, with a verdict on where it breaks.

Nothing in the cluster or in the logs ties a gateway's route to a workload for an ordinary viewer (decision 150), so the
front doors are suggested from data (`candidates`) and confirmed once by the viewer; the link is kept per tenant
(`tenants.<t>.workload_links`) and `read` then reads each hop from it."""
import ipaddress
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..arm import ArmError
from ..investigate import gateway
from ..investigate.appinsights import kql_string
from . import aks, appinsights

AGW_TYPE = "microsoft.network/applicationgateways"
AGW_LOG_CATEGORIES = {"applicationgatewayaccesslog", "alllogs"}
CLUSTER_ID = re.compile(r"^/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\.containerservice"
                        r"/managedclusters/[^/]+$", re.I)
SITE_ID = re.compile(r"^/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\.web/sites/[^/]+$", re.I)
K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
KINDS = ("Deployment", "StatefulSet", "DaemonSet")
APIS_PER_SERVICE = 5000
TOP_CANDIDATES = 15
# Words that name nothing in any naming scheme (protocols, plumbing). A team's own words (an environment, a company
# prefix, a region code) are found in the data instead: see common_words.
STOP_WORDS = {"api", "apis", "app", "apps", "svc", "service", "services", "http", "https", "www", "com", "net", "org",
              "default", "web", "backend", "azure"}
COMMON_SHARE, COMMON_MIN = 0.5, 4  # a word in at least half of 4+ names is the convention, not the workload
HOPS = ("agw", "apim", "requests", "pods")  # front to back
HOP_LABELS = {"agw": "App Gateway", "apim": "API Management", "requests": "Requests", "pods": "Pods"}


def tokens(text):
    """The words in a name, URL or path that could name a workload (lowercase, 3+ characters, not STOP_WORDS)."""
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in STOP_WORDS and not w.isdigit() and not re.fullmatch(r"v\d+", w)}


def common_words(texts):
    """Words in at least half of `texts` (when there are COMMON_MIN or more): what every name carries (an environment,
    a company prefix, a region code) can't tell one workload from another, whatever the naming scheme."""
    texts = [t for t in texts if t]
    if len(texts) < COMMON_MIN:
        return set()
    counts = Counter(w for t in texts for w in tokens(t))
    return {w for w, n in counts.items() if n >= COMMON_SHARE * len(texts)}


def workload_key(cluster_id, namespace, kind, name):
    return f"{cluster_id.lower()}/namespaces/{namespace}/{kind.lower()}s/{name}"


def check_workload(cluster_id, namespace, kind, name):
    if not CLUSTER_ID.match(cluster_id or ""):
        raise ValueError("cluster must be an AKS cluster ID")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    if not K8S_NAME.match(namespace or "") or not K8S_NAME.match(name or ""):
        raise ValueError("namespace and workload must be Kubernetes names")


# ------------------------------------------------------------------ front doors, suggested

def _api_rows(client, service, cache):
    """A service's current API revisions: {service_id, service, api, name, path, backend}; kept in `cache` an hour."""
    def load():
        rows = []
        for api in client.get_paged(f"{service['id']}/apis", gateway.APIM_API_VERSION) or []:
            props = api.get("properties") or {}
            if props.get("isCurrent") is False:
                continue
            rows.append({"service_id": service["id"], "service": service["name"], "api": api.get("name") or "",
                         "name": props.get("displayName") or api.get("name") or "", "path": props.get("path") or "",
                         "backend": props.get("serviceUrl") or ""})
            if len(rows) >= APIS_PER_SERVICE:
                break
        return rows
    return cache.get_or_load(service["id"].lower(), load) if cache else load()


def _backend_path(url):
    match = re.match(r"^[a-z]+://[^/]+(/[^?#]*)?", (url or "").lower())
    return (match.group(1) or "").rstrip("/") if match else ""


def apim_candidates(client, subscriptions, name, request_paths=(), cache=None):
    """(API Management APIs that could front `name`, best first, each with `score` and `why`; notes)."""
    services = client.query(f"Resources | where type =~ '{gateway.APIM_TYPE}' | project id, name | order by name asc",
                            subscriptions=list(subscriptions) or None)
    wanted, paths = tokens(name), [p.lower().rstrip("/") for p in request_paths if p and p != "/"]
    notes, out = [], []

    def read(service):
        try:
            return _api_rows(client, service, cache), None
        except ArmError as error:
            return [], f"{service['name']}: {appinsights.why(error)}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        read_all = list(pool.map(read, services))
    noise = common_words([" ".join((r["api"], r["name"], r["path"])) for rows, _ in read_all for r in rows])
    wanted = wanted - noise
    if True:
        for rows, error in read_all:
            if error:
                notes.append(f"APIs not read from {error}")
            for row in rows:
                why, score = [], 0
                shared = wanted & (tokens(row["api"]) | tokens(row["name"]) | tokens(row["path"]) | tokens(row["backend"]))
                if shared:
                    score += 2 * len(shared)
                    why.append(f"shares {', '.join(sorted(shared))} with the workload's name")
                backend_path = _backend_path(row["backend"])
                served = [p for p in paths if len(backend_path) > 1 and p.startswith(backend_path)]
                if served:
                    score += 3
                    why.append(f"its backend path {backend_path} is one the workload serves")
                if score:
                    out.append({**row, "score": score, "why": why})
    out.sort(key=lambda r: (-r["score"], r["service"], r["name"]))
    return out[:TOP_CANDIDATES], notes


def _cluster_network(client, cluster_id):
    """(its internal load balancers' frontend IPs, its node subnets' prefixes) from Resource Graph."""
    rows = client.query("Resources | where id =~ " + kql_string(cluster_id)
                        + " | project node=tostring(properties.nodeResourceGroup), pools=properties.agentPoolProfiles")
    if not rows:
        return set(), []
    subnets = {(p.get("vnetSubnetID") or "").lower() for p in rows[0].get("pools") or [] if p.get("vnetSubnetID")}
    lbs = client.query("Resources | where type =~ 'microsoft.network/loadbalancers' and resourceGroup =~ "
                       + kql_string(rows[0].get("node") or "-")
                       + " | mv-expand fe=properties.frontendIPConfigurations"
                       " | project ip=tostring(fe.properties.privateIPAddress)")
    prefixes = []
    if subnets:
        found = client.query("Resources | where type =~ 'microsoft.network/virtualnetworks'"
                             " | mv-expand s=properties.subnets | where tolower(tostring(s.id)) in~ ("
                             + ", ".join(kql_string(s) for s in sorted(subnets)) + ")"
                             " | project prefix=tostring(s.properties.addressPrefix), prefixes=s.properties.addressPrefixes")
        for row in found:
            prefixes += [p for p in [row.get("prefix"), *(row.get("prefixes") or [])] if p]
    return {r["ip"] for r in lbs if r.get("ip")}, [ipaddress.ip_network(p, strict=False) for p in dict.fromkeys(prefixes)]


def _routes(gw):
    """[(listener name, its host names, backend pool name, paths)] of one App Gateway's routing rules."""
    props = gw.get("props") or {}
    by_id = lambda items: {(i.get("id") or "").lower(): i for i in items or []}  # noqa: E731
    listeners, pools, maps = by_id(props.get("httpListeners")), by_id(props.get("backendAddressPools")), by_id(props.get("urlPathMaps"))
    out = []
    for rule in props.get("requestRoutingRules") or []:
        rp = rule.get("properties") or {}
        listener = listeners.get(((rp.get("httpListener") or {}).get("id") or "").lower(), {})
        lp = listener.get("properties") or {}
        hosts = [h for h in [lp.get("hostName"), *(lp.get("hostNames") or [])] if h]
        pool = pools.get(((rp.get("backendAddressPool") or {}).get("id") or "").lower())
        if pool:
            out.append((listener.get("name") or "", hosts, pool, []))
        path_map = maps.get(((rp.get("urlPathMap") or {}).get("id") or "").lower())
        for path_rule in ((path_map or {}).get("properties") or {}).get("pathRules") or []:
            pr = path_rule.get("properties") or {}
            target = pools.get(((pr.get("backendAddressPool") or {}).get("id") or "").lower())
            if target:
                out.append((listener.get("name") or "", hosts, target, pr.get("paths") or []))
    return out


def agw_candidates(client, subscriptions, name, cluster_id="", site_hosts=()):
    """(App Gateway routes that could send to the workload: a backend pool holding the cluster's load balancer or an
    address in its subnets, or the App Service's host names; best first, with `how` and `why`; notes)."""
    gateways = client.query(f"Resources | where type =~ '{AGW_TYPE}' | project id, name, props=properties | order by name asc",
                            subscriptions=list(subscriptions) or None)
    lb_ips, prefixes = _cluster_network(client, cluster_id) if cluster_id else (set(), [])
    hosts = {h.lower() for h in site_hosts}
    routes = [(gw, route) for gw in gateways for route in _routes(gw)]
    noise = common_words([" ".join([*hosts_, *paths_, pool_.get("name") or ""]) for _, (_, hosts_, pool_, paths_) in routes])
    wanted, out = tokens(name) - noise, []
    for gw, (listener, listener_hosts, pool, paths) in routes:
        if True:
            how = ""
            for address in (pool.get("properties") or {}).get("backendAddresses") or []:
                ip, fqdn = address.get("ipAddress") or "", (address.get("fqdn") or "").lower()
                if ip and ip in lb_ips:
                    how = "load balancer"
                elif ip and not how and any(_in(ip, net) for net in prefixes):
                    how = "subnet"
                elif fqdn and fqdn in hosts:
                    how = "host name"
                if how == "load balancer":
                    break
            if not how:
                continue
            shared = wanted & (tokens(" ".join(listener_hosts)) | tokens(" ".join(paths)) | tokens(pool.get("name")))
            why = {"load balancer": "its backend pool is the cluster's internal load balancer",
                   "subnet": "its backend pool is an address in the cluster's subnet (cluster known, not the workload)",
                   "host name": "its backend pool is the app's host name"}[how]
            out.append({"gateway_id": gw["id"], "gateway": gw["name"], "listener": listener, "hosts": listener_hosts,
                        "pool": pool.get("name") or "", "paths": paths, "how": how,
                        "score": {"host name": 6, "load balancer": 4, "subnet": 1}[how] + 2 * len(shared),
                        "why": [why] + ([f"shares {', '.join(sorted(shared))} with the workload's name"] if shared else [])})
    out.sort(key=lambda r: (-r["score"], r["gateway"], r["listener"], r["pool"]))
    unique = list({(r["gateway_id"].lower(), r["listener"], r["pool"]): r for r in out}.values())
    notes = [] if out or not cluster_id or lb_ips or prefixes else [
        "The cluster's load balancer and subnets couldn't be read (its node resource group may be outside your access)."]
    return unique[:TOP_CANDIDATES], notes


def _in(ip, net):
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def app_candidates(names, name):
    """Monitoring-tool app names that could be the workload's: same name first, then shared words."""
    wanted, out = tokens(name) - common_words(names), []
    for app in names:
        exact = app.lower() == name.lower()
        shared = wanted & tokens(app)
        if exact or shared:
            out.append({"app": app, "score": 10 if exact else 2 * len(shared),
                        "why": ["same name as the workload"] if exact else [f"shares {', '.join(sorted(shared))}"]})
    out.sort(key=lambda r: (-r["score"], r["app"]))
    return out[:TOP_CANDIDATES]


def component_candidates(client, subscriptions, name):
    """App Insights components named like the workload (when its requests are in App Insights, not the tool)."""
    rows = client.query(f"Resources | where type =~ '{appinsights.COMPONENT_TYPE}' | project id, name | order by name asc",
                        subscriptions=list(subscriptions) or None)
    wanted, out = tokens(name) - common_words([r["name"] for r in rows]), []
    for row in rows:
        shared = wanted & tokens(row["name"])
        if shared:
            out.append({"component": row["id"], "name": row["name"], "score": 2 * len(shared),
                        "why": [f"shares {', '.join(sorted(shared))} with the workload's name"]})
    out.sort(key=lambda r: (-r["score"], r["name"]))
    return out[:TOP_CANDIDATES]


# ------------------------------------------------------------------ hops, read

def _windows(now=None):
    """The KQL time filter for the last hour and the same hour a week ago, and the query timespan covering both."""
    now = now or datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(days=7, hours=1)
    return (f"(TimeGenerated > datetime({appinsights.iso(now - timedelta(hours=1))})"
            f" or TimeGenerated between (datetime({appinsights.iso(start)}) .. datetime({appinsights.iso(now - timedelta(days=7))})))",
            f"iff(TimeGenerated > datetime({appinsights.iso(now - timedelta(hours=1))}), 'now', 'week')",
            f"{appinsights.iso(start)}/{appinsights.iso(now)}", now)


def _hop(hop, source, queries, now=None, week=None, minutes=(), details=None, reason=""):
    return {"hop": hop, "label": HOP_LABELS[hop], "source": source, "available": not reason, "reason": reason,
            "now": now or {}, "week": week or {}, "minutes": list(minutes), "details": details or {}, "queries": queries}


def _periods(rows):
    by = {r.get("period"): r for r in rows}
    clean = lambda r: {k: v for k, v in (r or {}).items() if k != "period"}  # noqa: E731
    return clean(by.get("now")), clean(by.get("week"))


def _run(client, resource_id, queries):
    """Each (label, kql, timespan) against one resource's logs, in parallel: [rows or ArmError]."""
    def one(q):
        try:
            return aks.resource_logs(client, resource_id, q[1], q[2])
        except ArmError as error:
            return error
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(one, queries))


def apim_hop(client, service_id, api):
    """One API's gateway logs: requests, the gateway's own 5xx (no backend answer) against the backend's 5xx, p95
    backend time, by period; errors per minute; the last hour's top error reasons."""
    try:
        in_workspace, dedicated = gateway._workspace_logs(client, service_id)
    except ArmError as error:
        return _hop("apim", "Log Analytics", [], reason=f"Its diagnostic settings couldn't be read: {appinsights.why(error)}")
    if not in_workspace:
        return _hop("apim", "Log Analytics", [], reason="This API Management service doesn't send gateway logs to a workspace.")
    when, period, span, now = _windows()
    if dedicated:
        base = f"ApiManagementGatewayLogs | where {when} and ApiId =~ {kql_string(api)}"
        cols = "code=toint(ResponseCode), backend=toint(BackendResponseCode), btime=todouble(BackendTime), reason=LastErrorReason"
        table = "ApiManagementGatewayLogs"
    else:
        base = (f"AzureDiagnostics | where {when} and ResourceProvider == 'MICROSOFT.APIMANAGEMENT' and Category == 'GatewayLogs'"
                f" and apiId_s =~ {kql_string(api)}")
        cols = "code=toint(responseCode_d), backend=toint(backendResponseCode_d), btime=backendTime_d, reason=lastError_reason_s"
        table = "AzureDiagnostics"
    queries = [
        ("By period", f"{base} | extend {cols}, period={period}"
                      " | summarize total=count(), errors=countif(code >= 500), backend_errors=countif(backend >= 500),"
                      " gateway_errors=countif(code >= 500 and (isnull(backend) or backend == 0)), p95_ms=percentile(btime, 95) by period", span),
        ("Per minute, last hour", f"{base} | extend {cols}, period={period} | where period == 'now'"
                                  " | summarize total=count(), errors=countif(code >= 500) by bin(TimeGenerated, 1m) | order by TimeGenerated asc", span),
        ("Top error reasons, last hour", f"{base} | extend {cols}, period={period} | where period == 'now' and isnotempty(reason)"
                                         " | summarize count=count() by reason | top 5 by ['count']", span)]
    results = _run(client, service_id, queries)
    sent = [{"label": f"{table}: {label}", "query": kql, "timespan": ts} for label, kql, ts in queries]
    if isinstance(results[0], ArmError):
        return _hop("apim", "Log Analytics", sent, reason=f"Gateway logs couldn't be read: {appinsights.why(results[0])}")
    now_row, week_row = _periods(results[0])
    minutes = [] if isinstance(results[1], ArmError) else [[r["TimeGenerated"], r["total"], r["errors"]] for r in results[1]]
    reasons = [] if isinstance(results[2], ArmError) else [[r["reason"], r["count"]] for r in results[2]]
    return _hop("apim", "Log Analytics", sent, now_row, week_row, minutes, {"reasons": reasons, "api": api})


def agw_hop(client, gateway_id, listener, pool):
    """One App Gateway route's access logs: requests, 5xx, 502 (backend unreachable) and 504 (backend timed out), p95
    time taken, by period; errors per minute; the last hour's top backend statuses."""
    try:
        in_workspace, dedicated = gateway._workspace_logs(client, gateway_id, AGW_LOG_CATEGORIES)
    except ArmError as error:
        return _hop("agw", "Log Analytics", [], reason=f"Its diagnostic settings couldn't be read: {appinsights.why(error)}")
    if not in_workspace:
        return _hop("agw", "Log Analytics", [], reason="This App Gateway doesn't send access logs to a workspace.")
    when, period, span, _ = _windows()
    if dedicated:
        base = (f"AGWAccessLogs | where {when} and ListenerName == {kql_string(listener)}"
                f" and BackendPoolName == {kql_string(pool)}")
        cols = "code=toint(HttpStatus), taken_ms=todouble(TimeTaken) * 1000, server=tostring(ServerStatus)"
        table = "AGWAccessLogs"
    else:
        base = (f"AzureDiagnostics | where {when} and Category == 'ApplicationGatewayAccessLog'"
                f" and listenerName_s == {kql_string(listener)} and backendPoolName_s == {kql_string(pool)}")
        cols = "code=toint(httpStatus_d), taken_ms=timeTaken_d * 1000, server=serverStatus_s"
        table = "AzureDiagnostics"
    queries = [
        ("By period", f"{base} | extend {cols}, period={period}"
                      " | summarize total=count(), errors=countif(code >= 500), unreachable=countif(code == 502),"
                      " timed_out=countif(code == 504), p95_ms=percentile(taken_ms, 95) by period", span),
        ("Per minute, last hour", f"{base} | extend {cols}, period={period} | where period == 'now'"
                                  " | summarize total=count(), errors=countif(code >= 500) by bin(TimeGenerated, 1m) | order by TimeGenerated asc", span),
        ("Backend statuses, last hour", f"{base} | extend {cols}, period={period} | where period == 'now' and code >= 500"
                                        " | summarize count=count() by server | top 5 by ['count']", span)]
    results = _run(client, gateway_id, queries)
    sent = [{"label": f"{table}: {label}", "query": kql, "timespan": ts} for label, kql, ts in queries]
    if isinstance(results[0], ArmError):
        return _hop("agw", "Log Analytics", sent, reason=f"Access logs couldn't be read: {appinsights.why(results[0])}")
    now_row, week_row = _periods(results[0])
    minutes = [] if isinstance(results[1], ArmError) else [[r["TimeGenerated"], r["total"], r["errors"]] for r in results[1]]
    statuses = [] if isinstance(results[2], ArmError) else [[r["server"] or "no backend answer", r["count"]] for r in results[2]]
    return _hop("agw", "Log Analytics", sent, now_row, week_row, minutes, {"backend_statuses": statuses,
                                                                           "listener": listener, "pool": pool})


def _owner_regex(kind, name):
    """KubePodInventory's ControllerName for the workload's pods: a Deployment's ReplicaSets add a hash."""
    escaped = re.escape(name)
    return f"^{escaped}-[a-z0-9]{{5,10}}$" if kind == "Deployment" else f"^{escaped}$"


def pods_hop_ci(client, cluster_id, namespace, kind, name):
    """The workload's pods from Container Insights: restarts and OOM kills by period, pods not ready now, restarts per
    5 minutes over the last hour, the last hour's warning events by reason. (hop, "") or (None, why not)."""
    when, period, span, _ = _windows()
    owner = kql_string(_owner_regex(kind, name))
    base = (f"KubePodInventory | where {when} and Namespace == {kql_string(namespace)} and ControllerName matches regex {owner}"
            " and isnotempty(ContainerName)")
    queries = [
        ("By period", f"{base} | extend period={period}"
                      " | summarize r=max(ContainerRestartCount) - min(ContainerRestartCount),"
                      " oom=max(toint(tostring(ContainerLastStatus) has 'OOMKilled')) by period, Name, ContainerName"
                      " | summarize restarts=sum(r), oom_containers=sum(oom), pods=dcount(Name) by period", span),
        ("Not ready now", f"{base} | where TimeGenerated > ago(15m) | summarize arg_max(TimeGenerated, PodStatus, ContainerStatus) by Name"
                          " | summarize pods=count(), not_ready=countif(PodStatus != 'Running' or ContainerStatus !in ('running', 'Running'))", span),
        ("Restarts per 5 minutes, last hour", f"{base} | extend period={period} | where period == 'now'"
                                              " | summarize r=max(ContainerRestartCount) - min(ContainerRestartCount)"
                                              " by bin(TimeGenerated, 5m), Name, ContainerName"
                                              " | summarize restarts=sum(r) by TimeGenerated | order by TimeGenerated asc", span),
        ("Warning events, last hour", f"KubeEvents | where TimeGenerated > ago(1h) and Namespace == {kql_string(namespace)}"
                                      f" and KubeEventType == 'Warning' and Name startswith {kql_string(name)}"
                                      " | summarize count=count() by Reason | top 5 by ['count']", span)]
    results = _run(client, cluster_id, queries)
    sent = [{"label": f"Container Insights: {label}", "query": kql, "timespan": ts} for label, kql, ts in queries]
    if isinstance(results[0], ArmError):
        return None, f"Container Insights couldn't be read: {appinsights.why(results[0])}", sent
    now_row, week_row = _periods(results[0])
    if not now_row and not week_row:
        return None, "Container Insights has no rows for this workload in the last hour or the same hour last week.", sent
    ready = (results[1] or [{}])[0] if not isinstance(results[1], ArmError) else {}
    minutes = [] if isinstance(results[2], ArmError) else [[r["TimeGenerated"], None, r["restarts"]] for r in results[2]]
    events = [] if isinstance(results[3], ArmError) else [[r["Reason"], r["count"]] for r in results[3]]
    now_row = {**now_row, "not_ready": ready.get("not_ready") or 0, "errors": now_row.get("restarts") or 0}
    week_row = {**week_row, "errors": week_row.get("restarts") or 0}
    return _hop("pods", "Container Insights", sent, now_row, week_row, minutes, {"events": events}), "", sent


def requests_hop_ai(client, component_id):
    """An App Insights component's requests: the last hour and the same hour last week (hourly metrics), failed
    requests per minute over the last hour."""
    (_, counts, error), = appinsights.last_hour(client, [{"id": component_id}])
    if error:
        return _hop("requests", "App Insights", [], reason=f"Its metrics couldn't be read: {appinsights.why(error)}")
    (total, total_wk), (failed, failed_wk), _ = (counts.get(m, (0, 0)) for m in appinsights.METRICS.split(","))
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    params = {"metricnames": "requests/count,requests/failed", "aggregation": "count", "interval": "PT1M",
              "timespan": f"{appinsights.iso(end - timedelta(hours=1))}/{appinsights.iso(end)}"}
    minutes = []
    try:
        payload = client.get(f"{component_id}/providers/microsoft.insights/metrics", appinsights.METRICS_API_VERSION, params=params)
        series = {m["name"]["value"]: (m.get("timeseries") or [{}])[0].get("data") or [] for m in payload.get("value") or []}
        for point, failed_point in zip(series.get("requests/count", []), series.get("requests/failed", [])):
            minutes.append([point.get("timeStamp"), point.get("count") or 0, failed_point.get("count") or 0])
    except ArmError:
        pass
    sent = [{"label": "Azure Monitor metrics", "query": "requests/count, requests/failed (hourly, then per minute)", "timespan": ""}]
    return _hop("requests", "App Insights", sent, {"total": total, "errors": failed}, {"total": total_wk, "errors": failed_wk},
                minutes, {"component": component_id.rsplit("/", 1)[-1]})


def _from_tool(hop, label, call):
    """A hop read through the monitoring tool's provider: its answer, or why not."""
    try:
        found = call()
    except Exception as error:  # a provider's own error, or the tool unreachable: said on the page, never fatal
        return _hop(hop, label, [], reason=str(error).splitlines()[0][:300])
    sent = [{"label": f"{label}, account {q['account']}", "query": q["nrql"], "timespan": ""} for q in found["queries"]]
    if not found["found"]:
        return _hop(hop, label, sent, reason=f"{label} has no data for it in the last hour or the same hour last week.")
    return _hop(hop, label, sent, found["now"], found["week"], found["minutes"])


def read(client, link, source=None, can=(), rules=None, request_rules=None):
    """Every hop of a saved link, read in parallel; `source` is the monitoring tool's provider (or None) and `can`
    its capabilities. {"hops", "verdict"}: one hop per App Gateway route and per API, then requests, then pods."""
    tasks = [lambda r=r: agw_hop(client, r["gateway_id"], r["listener"], r["pool"]) for r in link.get("agw") or []]
    tasks += [lambda a=a: apim_hop(client, a["service_id"], a["api"]) for a in link.get("apim") or []]
    app, component, label = link.get("app") or "", link.get("component") or "", getattr(source, "label", "")

    def requests():
        if app and source is not None and "app requests" in can:
            return _from_tool("requests", label, lambda: source.requests(app))
        if component:
            return requests_hop_ai(client, component)
        why = ("Pick the workload's app in the monitoring tool, or its App Insights component, to read its requests."
               if (source is not None and "app requests" in can) else
               "No App Insights component is linked and the monitoring tool in the Scope bar can't give requests.")
        return _hop("requests", label or "App Insights", [], reason=why)
    tasks.append(requests)

    if link.get("cluster_id"):
        def pods():
            hop, why, sent = pods_hop_ci(client, link["cluster_id"], link["namespace"], link["kind"], link["name"])
            if hop:
                return hop
            if source is not None and "workload pods" in can:
                cluster = link["cluster_id"].rstrip("/").split("/")[-1]
                tool = _from_tool("pods", label, lambda: source.workload_pods(cluster, link["namespace"], link["kind"], link["name"]))
                if tool["available"]:
                    return tool
                why = f"{why} {tool['reason']}"
            return _hop("pods", "Container Insights", sent, reason=why)
        tasks.append(pods)

    with ThreadPoolExecutor(max_workers=6) as pool:
        hops = list(pool.map(lambda task: task(), tasks))
    hops.sort(key=lambda h: HOPS.index(h["hop"]))
    return {"hops": hops, "verdict": verdict(hops, rules, request_rules),
            "rules": {**PATH_RULES, **(rules or {}), **{f"requests_{k}": v for k, v in {**appinsights.RULES, **(request_rules or {})}.items()}}}


# ------------------------------------------------------------------ verdict

def rate(row):
    total = row.get("total")
    return round(100.0 * (row.get("errors") or 0) / total, 1) if total else None


PATH_RULES = {"drop_percent": 50, "drop_min": 100, "restarts_min": 3, "restarts_critical": 10}  # settings' path.*


def assess(hop, rules=None, request_rules=None):
    """(status, why) for one hop: rising when its errors (pods: restarts) are up on the same hour last week. Requests
    follow App Insights' failure rule (`request_rules`), so a hop and the spike card agree."""
    r = {**PATH_RULES, **(rules or {})}
    if not hop["available"] or not hop["now"]:
        return None, ""
    now, week = hop["now"], hop["week"] or {}
    errors, before = now.get("errors") or 0, week.get("errors") or 0
    if hop["hop"] == "pods":
        if errors >= r["restarts_min"] and errors > 2 * before:
            return ("critical" if errors >= r["restarts_critical"] else "warning",
                    f"{errors} restarts in the last hour (same hour last week {before})")
        if now.get("not_ready"):
            return "warning", f"{now['not_ready']} pod(s) not ready now"
        return "healthy", ""
    now_rate, week_rate = rate(now), rate(week)
    if appinsights.failing_more(errors, now_rate, week_rate, request_rules):
        status = "critical" if appinsights.failing_critical(errors, now_rate, request_rules) else "warning"
        return status, (f"{errors:,} of {now.get('total', 0):,} failed ({now_rate}%), same hour last week "
                        f"{week_rate if week_rate is not None else 0}%")
    total, total_week = now.get("total") or 0, week.get("total") or 0
    if total_week >= r["drop_min"] and total < r["drop_percent"] / 100 * total_week:
        return "warning", f"traffic down {round(100 - 100 * total / total_week)}% ({total:,} requests, same hour last week {total_week:,})"
    return "healthy", ""


def verdict(hops, rules=None, request_rules=None):
    """Each hop's status, and one sentence naming the hop furthest back whose errors rose, with what the others saw."""
    for hop in hops:
        hop["status"], hop["why"] = assess(hop, rules, request_rules)
    rising = [h for h in sorted(hops, key=lambda h: HOPS.index(h["hop"]), reverse=True) if h["status"] in ("critical", "warning")]
    unread = [h["label"] for h in hops if not h["available"]]
    tail = f" Not read: {', '.join(unread)}." if unread else ""
    if not rising:
        return "No hop shows more errors or much less traffic than the same hour last week." + tail
    first, others = rising[0], rising[1:]
    fine = [h["label"] for h in hops if h["status"] == "healthy"]
    parts = [f"Breaks at {first['label']}: {first['why']}."]
    if others:
        parts.append("Also up: " + "; ".join(f"{h['label']} ({h['why']})" for h in others) + ".")
    if fine:
        parts.append(f"Fine: {', '.join(fine)}.")
    return " ".join(parts) + tail
