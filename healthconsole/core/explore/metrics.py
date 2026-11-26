"""Any metric of any resource over time, optionally split by one of its dimensions."""
from datetime import datetime, timedelta, timezone

from ..health import METRICS_API_VERSION

RANGES = {  # label -> (window, grain)
    "1h": (timedelta(hours=1), "PT1M"),
    "6h": (timedelta(hours=6), "PT5M"),
    "24h": (timedelta(hours=24), "PT15M"),
    "7d": (timedelta(days=7), "PT1H"),
    "30d": (timedelta(days=30), "PT6H"),
    "90d": (timedelta(days=90), "P1D"),  # Azure keeps platform metrics 93 days
}
MAX_SERIES = 8


def definitions(client, resource_id):
    payload = client.get(f"{resource_id}/providers/Microsoft.Insights/metricDefinitions",
                         METRICS_API_VERSION, tolerate=(400, 404, 409)) or {}
    out = []
    for item in payload.get("value", []):
        name = (item.get("name") or {}).get("value")
        if not name:
            continue
        out.append({
            "name": name,
            "label": (item.get("name") or {}).get("localizedValue") or name,
            "unit": item.get("unit") or "",
            "primary": item.get("primaryAggregationType") or "Average",
            "aggregations": item.get("supportedAggregationTypes") or [],
            "dimensions": [d.get("value") for d in item.get("dimensions") or [] if d.get("value")],
        })
    return sorted(out, key=lambda m: m["label"].lower())


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def read(client, resource_id, metric, aggregation, start, end, grain, namespace=None, metric_filter=None, top=None):
    """(payload, [{"label", "points": [[time, value], ...]}]) for one metric: the one read every chart and check uses.
    Azure answers 400 for a metric/aggregation/filter it doesn't offer; the payload is then empty."""
    params = {"metricnames": metric, "aggregation": aggregation, "timespan": f"{_iso(start)}/{_iso(end)}", "interval": grain}
    if namespace:
        params["metricnamespace"] = namespace
    if metric_filter:
        params["$filter"] = metric_filter
    if top:
        params["top"] = str(top)
    payload = client.get(f"{resource_id}/providers/Microsoft.Insights/metrics", METRICS_API_VERSION,
                         params=params, tolerate=(400,)) or {}
    key = aggregation.lower()
    out = []
    for item in payload.get("value", []):
        for ts in item.get("timeseries") or []:
            label = ", ".join(m.get("value") or "(empty)" for m in ts.get("metadatavalues") or []) or metric
            out.append({"label": label, "points": [[p["timeStamp"], p.get(key)] for p in ts.get("data") or []]})
    return payload, out


def series(client, resource_id, metric, aggregation, time_range="6h", split=None):
    window, grain = RANGES.get(time_range, RANGES["6h"])
    end = datetime.now(timezone.utc).replace(microsecond=0)
    # With a split, the API returns the top N series by the chosen aggregation; the rest fold into "Other" by omission.
    payload, out = read(client, resource_id, metric, aggregation, end - window, end, grain,
                        metric_filter=f"{split} eq '*'" if split else None, top=MAX_SERIES if split else None)
    if not payload:
        return {"series": [], "error": "Azure rejected that metric/aggregation/split combination."}
    return {"series": out[:MAX_SERIES], "unit": (payload.get("value") or [{}])[0].get("unit", ""),
            "grain": grain, "truncated": len(out) > MAX_SERIES}
