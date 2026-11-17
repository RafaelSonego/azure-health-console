"""Application Insights for one component: requests by role against the same window last week, failing operations,
exceptions, failing dependencies (asked through Investigate's source, so both pages send the same queries),
availability tests and the last hour from metrics. Each a GET on the component's own query or metrics endpoint."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..arm import AccessDenied, ArmError, kql_list
from ..health import METRICS_API_VERSION

API_VERSION = "2018-04-20"
COMPONENT_TYPE = "microsoft.insights/components"
WINDOWS = {"1h": 1, "6h": 6, "24h": 24}
COMPONENT_ID = re.compile(r"/subscriptions/[^/\s]+/resourcegroups/[^/\s]+/providers/microsoft\.insights/components/[^/\s]+", re.I)

# `last` is reserved in this endpoint's KQL, hence `lastSeen`.
QUERIES = {
    "roles": "requests | summarize total=count(), failed=countif(success == false), "
             "p95=round(percentile(duration, 95), 0) by role=cloud_RoleName | top 20 by total",
    "availability": "availabilityResults | summarize runs=count(), failed=countif(success == 0), "
                    "lastSeen=max(timestamp) by test=name, location | top 20 by failed",
}


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(payload):
    tables = payload.get("tables") or []
    if not tables:
        return []
    columns = [c["name"] for c in tables[0].get("columns") or []]
    rows = [dict(zip(columns, row)) for row in tables[0].get("rows") or []]
    for row in rows:
        if isinstance(row.get("codes"), str):
            try:
                row["codes"] = [c for c in json.loads(row["codes"]) if c not in ("", None)]
            except ValueError:
                row["codes"] = []
    return rows


def query(client, component_id, kql, start, end):
    """Rows of one KQL query on a component's own endpoint, over start–end."""
    return _rows(client.get(f"{component_id}/query", API_VERSION,
                            params={"query": kql, "timespan": f"{iso(start)}/{iso(end)}"}))


def why(error):
    if isinstance(error, AccessDenied) or getattr(error, "status", None) == 403:
        return "No access to this component's data: it needs Reader (or Monitoring Reader) on it."
    return str(error)[:300]


def _merge_roles(now, week):
    before = {r["role"]: r for r in week}
    out = []
    for row in now:
        old = before.pop(row["role"], None)
        out.append({**row, "failed_pct": pct(row["failed"], row["total"]),
                    "week": {**old, "failed_pct": pct(old["failed"], old["total"])} if old else None})
    out += [{"role": role, "total": 0, "failed": 0, "p95": None, "failed_pct": None,
             "week": {**old, "failed_pct": pct(old["failed"], old["total"])}} for role, old in before.items()]
    return out


def pct(part, whole):
    """A percentage to one decimal, or None without a whole."""
    return round(100.0 * part / whole, 1) if whole else None


def insights(client, component_id, window="1h"):
    """Every section for `window`, each failing on its own; `queries` lists what was sent, as on the page."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {list(WINDOWS)}")
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=WINDOWS[window])
    from ..investigate.appinsights import AppInsightsSource  # it builds on this module, so imported here
    source = AppInsightsSource(client, component_id)
    jobs = {key: (kql, start, end) for key, kql in QUERIES.items()}
    jobs["roles_week"] = (QUERIES["roles"], start - timedelta(days=7), end - timedelta(days=7))
    shared = ("operations", "exceptions", "dependencies")
    results, errors, sent = {}, [], [{"section": key, "query": kql} for key, kql in QUERIES.items()]

    def run(key):
        if key in shared:
            answer = getattr(source, key)(window)
            return key, answer["rows"], answer["errors"], answer["queries"]
        if key == "now":
            found, error = measure(client, component_id)
            return key, found, [error] if error else [], []
        kql, begin, stop = jobs[key]
        try:
            return key, query(client, component_id, kql, begin, stop), [], []
        except ArmError as error:
            return key, [], [why(error)], []

    with ThreadPoolExecutor(max_workers=3) as pool:
        for key, rows, problems, queries in pool.map(run, [*jobs, *shared, "now"]):
            results[key] = rows
            sent += [{"section": key, "query": q["query"]} for q in queries]
            errors += [e for e in problems if e not in errors]
    return {"component_id": component_id, "window": window, "from": iso(start), "to": iso(end),
            "roles": _merge_roles(results["roles"], results["roles_week"]), "now": results["now"],
            **{key: results[key] for key in (*shared, "availability")}, "queries": sent, "errors": errors}


# ------------------------------------------------------------------ an app's component
SITE_TYPE = "microsoft.web/sites"
NAME_NOISE = re.compile(r"(-|_)?(appi|appins|appinsights|insights|ai|func|fn|app|api|web|wa|as)$")


def component_in(text):
    match = COMPONENT_ID.search(text or "")
    return match.group(0) if match else ""


def _stem(name):
    """A name without separators and its usual suffixes, so `shop-api-func` and `shop-api-appi` meet as `shopapi`."""
    name = name.lower()
    for _ in range(2):
        name = NAME_NOISE.sub("", name)
    return re.sub(r"[-_.]", "", name)


def link(client, app_id, picked=""):
    """The component an app reports to and how it was found: its `hidden-link` tag, your pick, or a related name in
    its resource group (probable); with the group's components to choose from."""
    rows = client.query(
        f"Resources | where tolower(id) in ({kql_list([app_id.lower()])})"
        " | project id, name, type, tags, resourceGroup, subscriptionId"
        " | union (Resources | where type =~ 'microsoft.insights/components'"
        f" | where strcat(tolower(subscriptionId), '/', tolower(resourceGroup)) in ({kql_list([_group_key(app_id)])})"
        " | project id, name, type, tags, resourceGroup, subscriptionId)")
    app = next((r for r in rows if r["id"].lower() == app_id.lower()), None)
    candidates = sorted(({"id": r["id"], "name": r["name"]} for r in rows if r["type"].lower() == COMPONENT_TYPE),
                        key=lambda c: c["name"].lower())
    out = {"how": None, "component_id": "", "component_name": "", "candidates": candidates}
    if picked:
        return {**out, "how": "picked", "component_id": picked, "component_name": picked.rsplit("/", 1)[-1]}
    for key, value in ((app or {}).get("tags") or {}).items():
        if key.lower().startswith("hidden-link"):
            found = component_in(value) or component_in(key)
            if found:
                return {**out, "how": "tag", "component_id": found, "component_name": found.rsplit("/", 1)[-1]}
    stem = _stem(app["name"]) if app else ""
    related = [c for c in candidates if stem and len(stem) >= 4 and _stem(c["name"]) == stem]
    if not related:
        related = [c for c in candidates if stem and len(stem) >= 4 and (_stem(c["name"]) in stem or stem in _stem(c["name"]))]
    if len(related) == 1:
        return {**out, "how": "name", "component_id": related[0]["id"], "component_name": related[0]["name"]}
    return out


def _group_key(resource_id):
    parts = resource_id.split("/")
    return f"{parts[2].lower()}/{parts[4].lower()}"


# ------------------------------------------------------------------ Triage: failure spikes from metrics
MAX_COMPONENTS = 50
METRICS = "requests/count,requests/failed,exceptions/count"
TREND_HOURS = 24
RESULT_CODE = "request/resultCode"


def _buckets(payload):
    """{metric: (last hour, same hour a week ago)}: the series starts a week before the last hour, hourly. `trend`
    holds each metric's last TREND_HOURS hourly counts, oldest first."""
    out = {"trend": {}}
    for metric in payload.get("value") or []:
        series = (metric.get("timeseries") or [{}])[0].get("data") or []
        counts = [point.get("count") or 0 for point in series]
        out[metric["name"]["value"]] = (counts[-1], counts[0]) if counts else (0, 0)
        out["trend"][metric["name"]["value"]] = counts[-TREND_HOURS:]
    return out


def _server_errors(payload):
    """Failed requests whose result code is 5xx, from a metrics answer split by result code."""
    total = 0
    for metric in payload.get("value") or []:
        for series in metric.get("timeseries") or []:
            code = next((str(m.get("value") or "") for m in series.get("metadatavalues") or []
                         if ((m.get("name") or {}).get("value") or "").lower() == RESULT_CODE.lower()), "")
            if code.startswith("5"):
                total += sum(point.get("count") or 0 for point in series.get("data") or [])
    return total


def server_errors(client, rows):
    """{component ID: (5xx in the last hour or None, error or None)}: failed requests split by result code, one
    metrics GET each, eight at a time."""
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    params = {"metricnames": "requests/failed", "aggregation": "count", "interval": "PT1H",
              "timespan": f"{iso(end - timedelta(hours=1))}/{iso(end)}", "$filter": f"{RESULT_CODE} eq '*'", "top": "100"}

    def read(row):
        try:
            payload = client.get(f"{row['id']}/providers/microsoft.insights/metrics", METRICS_API_VERSION, params=params)
            return row["id"], (_server_errors(payload or {}), None)
        except ArmError as error:
            return row["id"], (None, error)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(read, rows))


def assess(counts):
    """(status, detail, causes) when failures or exceptions are up on last week, else None; causes names which
    ("failed", "exceptions")."""
    (total, total_wk), (failed, failed_wk), (exc, exc_wk) = (counts.get(m, (0, 0)) for m in METRICS.split(","))
    rate, rate_wk = pct(failed, total), pct(failed_wk, total_wk)
    failing = failed >= 5 and (rate_wk is None or (rate or 0) > rate_wk + 1)
    throwing = exc >= 10 and exc >= 2 * exc_wk
    if not failing and not throwing:
        return None
    parts = []
    if failing:
        parts.append(f"{failed:,} of {total:,} requests failed ({rate}%), same hour last week {rate_wk if rate_wk is not None else 0}%")
    if throwing:
        parts.append(f"{exc:,} exceptions, last week {exc_wk:,}")
    critical = failing and (rate or 0) >= 20 and failed >= 20
    causes = [name for name, hit in (("failed", failing), ("exceptions", throwing)) if hit]
    return ("critical" if critical else "warning"), "; ".join(parts), causes


def summary(counts):
    """One component's last hour against the same hour last week, its spike verdict and 24 h trend, from last_hour."""
    (total, total_wk), (failed, failed_wk), (exceptions, exceptions_wk) = (counts.get(m, (0, 0)) for m in METRICS.split(","))
    verdict = assess(counts)
    return {"requests": total, "failed": failed, "failed_pct": pct(failed, total), "exceptions": exceptions,
            "week": {"requests": total_wk, "failed": failed_wk, "failed_pct": pct(failed_wk, total_wk),
                     "exceptions": exceptions_wk},
            "status": verdict[0] if verdict else None, "why": verdict[1] if verdict else "",
            "causes": verdict[2] if verdict else [], "trend": (counts.get("trend") or {}).get("requests/failed", [])}


def measure(client, component_id):
    """(summary with its last hour's 5xx, error or ""): last_hour and server_errors for one component."""
    row = {"id": component_id}
    (_, counts, error), = last_hour(client, [row])
    if error:
        return None, why(error)
    return {**summary(counts), "server_errors": server_errors(client, [row])[component_id][0]}, ""


def last_hour(client, rows):
    """[(row, {metric: (last hour, same hour a week ago)} or None, error or None)] for components, one metrics GET
    each, eight at a time."""
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    timespan = f"{iso(end - timedelta(days=7, hours=1))}/{iso(end)}"

    def read(row):
        try:
            payload = client.get(f"{row['id']}/providers/microsoft.insights/metrics", METRICS_API_VERSION,
                                 params={"metricnames": METRICS, "aggregation": "count", "interval": "PT1H",
                                         "timespan": timespan})
            return row, _buckets(payload or {}), None
        except ArmError as error:
            return row, None, error

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(read, rows))


def spikes(client, subscriptions, rg_clause=""):
    """The Triage card: components in scope whose last hour fails or throws more than the same hour last week."""
    rows = client.query("Resources | where type =~ 'microsoft.insights/components'"
                        f" | project id, name, type, kind, resourceGroup, subscriptionId{rg_clause} | order by name asc",
                        subscriptions=list(subscriptions) or None)
    checked = rows[:MAX_COMPONENTS]
    items, unread = [], 0
    for row, counts, error in last_hour(client, checked):
        verdict = assess(counts) if counts is not None else None
        if error:
            unread += 1
        elif verdict:
            items.append({"id": row["id"], "name": row["name"], "display_type": "Application Insights",
                          "resource_group": row.get("resourceGroup") or "",
                          "subscription_id": (row.get("subscriptionId") or "").lower(),
                          "status": verdict[0], "detail": verdict[1]})
    items.sort(key=lambda i: (i["status"] != "critical", i["name"].lower()))
    notes = []
    if len(rows) > MAX_COMPONENTS:
        notes.append(f"Checked {MAX_COMPONENTS} of {len(rows)} components in scope (alphabetically); narrow the scope to check the rest.")
    if unread:
        notes.append(f"{unread} component(s) couldn't be read (no access or no metrics).")
    return {"key": "appinsights", "title": "App Insights: failure spikes",
            "description": "Last hour's failed requests or exceptions up on the same hour last week",
            "items": items, "error": " ".join(notes) or None, "checked": len(checked), "total": len(rows),
            "status": next((s for s in ("critical", "warning") if any(i["status"] == s for i in items)), None)}
