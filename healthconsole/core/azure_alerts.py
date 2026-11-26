"""Azure Monitor metric alerts as a source for the alert check (`alert_check.AlertHistory`): the rule from the alert
rule resource, its history from Azure Monitor metrics at the rule's own window (Azure answers 7 days of 1-minute values
in one read), so no scaling is needed. Log alert rules run a query in Log Analytics and aren't read here yet."""
import re
from datetime import datetime, timedelta, timezone

from . import alert_check
from .alert_detail import DURATION
from .arm import ArmError, kql_string
from .explore import metrics

RULE_API = "2018-03-01"
OPERATORS = {"greaterthan": ">", "greaterthanorequal": ">=", "lessthan": "<", "lessthanorequal": "<=", "equals": "="}
ADDITIVE = {"total": True, "count": True, "average": False, "maximum": False, "minimum": False}
GRAINS = (1, 5, 15, 30, 60, 360, 720, 1440)  # minutes the metrics API offers as an interval
MAX_POINTS = 10_080  # 7 days of 1-minute values answered in one read (measured 1.4 s)
DIMENSION_NAME = re.compile(r"^[A-Za-z0-9_.\-/ ]{1,128}$")


def _dimension(name):
    """A dimension name as the filter's identifier: refused (not passed through) if it could carry filter syntax."""
    if not DIMENSION_NAME.match(str(name)):
        raise CheckError(f"The alert rule's dimension name {name!r} isn't one this check can filter on.")
    return name


def _literal(value):
    """A dimension value as a quoted filter literal: a quote inside it is doubled, so it can't end the string."""
    return "'" + str(value).replace("'", "''") + "'"


_ALERT = ("AlertsManagementResources | where type =~ 'microsoft.alertsmanagement/alerts' | where id =~ {id}"
          " | extend e = properties.essentials | project rule=tostring(e.alertRule), target=tostring(e.targetResource),"
          " started=tostring(e.startDateTime)")


class CheckError(ArmError):
    """Why this alert can't be checked, in words for the page."""


def _minutes(iso):
    match = DURATION.match(iso or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return days * 1440 + hours * 60 + minutes


def _grain(minutes):
    return {1: "PT1M", 5: "PT5M", 15: "PT15M", 30: "PT30M", 60: "PT1H", 360: "PT6H", 720: "PT12H", 1440: "P1D"}[minutes]


class AzureMetricAlerts(alert_check.AlertHistory):
    label = "Azure Monitor"

    def __init__(self, client):
        self.client = client

    def alert_rule(self, ref):
        """`ref` is a fired alert's ID (Alerts Management) or a metric alert rule's ID."""
        rule_id, target = ref, None
        if "/providers/microsoft.alertsmanagement/alerts/" in ref.lower():
            rows = self.client.query(_ALERT.format(id=kql_string(ref)))
            if not rows:
                raise CheckError("That alert isn't in Azure's alert history any more.")
            rule_id, target = rows[0]["rule"], rows[0]["target"]
        if not rule_id:
            raise CheckError("This alert doesn't name its rule.")
        if "/providers/microsoft.insights/metricalerts/" not in rule_id.lower():
            kind = "/".join(rule_id.split("/providers/")[-1].split("/")[:2])
            raise CheckError(f"Only metric alert rules can be checked so far; this alert comes from a {kind} rule.")
        rule = self.client.get(rule_id, RULE_API)
        props = rule.get("properties") or {}
        criteria = ((props.get("criteria") or {}).get("allOf") or [])
        if not criteria:
            raise CheckError("The alert rule has no metric criterion to check.")
        first = criteria[0]
        operator = OPERATORS.get(str(first.get("operator") or "").lower())
        dynamic = str(first.get("criterionType") or "").lower().startswith("dynamic")
        aggregation = first.get("timeAggregation") or "Average"
        window = _minutes(props.get("windowSize")) or 5
        notes = []
        if len(criteria) > 1:
            notes.append(f"The rule has {len(criteria)} criteria; the check reads the first ({first.get('metricName')}).")
        dimensions = [d for d in first.get("dimensions") or [] if d.get("name")]
        scope = target or (props.get("scopes") or [None])[0]
        if not scope:
            raise CheckError("The alert rule doesn't say which resource it watches.")
        split = ", ".join(f"{d['name']} {d.get('operator', 'Include')} {d.get('values')}" for d in dimensions)
        query = (f"{aggregation} of {first.get('metricName')} ({first.get('metricNamespace') or 'default namespace'}) "
                 f"per {window} min{', ' + split if split else ''}")
        return {"source": self.label, "name": rule.get("name") or rule_id.rsplit("/", 1)[-1], "id": rule_id,
                "query": {"label": "Azure Monitor metric", "text": query},
                "operator": operator, "threshold": None if dynamic else first.get("threshold"),
                "window_minutes": window, "duration_minutes": window, "additive": ADDITIVE.get(aggregation.lower()),
                "function": aggregation.lower(), "unit": "", "links": [{"label": "Alert rule in the portal", "resource": rule_id}],
                "resource": scope, "metric": first.get("metricName"),
                "namespace": first.get("metricNamespace"), "aggregation": aggregation, "dimensions": dimensions,
                "dynamic": dynamic, "notes": notes + (["Dynamic threshold: Azure sets it from history itself."] if dynamic else [])}

    def alert_series(self, rule, days):
        bucket = alert_check.bucket_for(days, rule["window_minutes"], MAX_POINTS, GRAINS)
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        include = [d for d in rule["dimensions"] if str(d.get("operator", "Include")).lower() == "include"]
        notes = []
        if len(include) < len(rule["dimensions"]):
            notes.append("Dimensions the rule excludes aren't filtered out here, so values can be a little higher.")
        metric_filter = " and ".join(
            "(" + " or ".join(f"{_dimension(d['name'])} eq {_literal(v)}" for v in (d.get("values") or ["*"])) + ")"
            for d in include) or None
        payload, series = metrics.read(self.client, rule["resource"], rule["metric"], rule["aggregation"],
                                       end - timedelta(days=days), end, _grain(bucket), namespace=rule["namespace"],
                                       metric_filter=metric_filter)
        if not payload:
            raise CheckError("Azure Monitor refused this metric, aggregation or filter for the resource.")
        if len(series) > 1:  # one value per dimension: the rule fires when any of them crosses, so take the worst
            notes.append(f"{len(series)} dimension values: each bucket takes the one nearest to firing.")
        pick = min if rule["operator"] in alert_check.BELOW else max
        times = [t for t, _ in series[0]["points"]] if series else []
        points = []
        for i, t in enumerate(times):
            values = [s["points"][i][1] for s in series if i < len(s["points"]) and s["points"][i][1] is not None]
            points.append([t, pick(values) if values else None])
        return {"points": points, "bucket_minutes": bucket, "notes": notes,
                "queries": [{"label": "Azure Monitor metrics", "query": (
                    f"GET {rule['resource']}/providers/Microsoft.Insights/metrics?metricnames={rule['metric']}"
                    f"&aggregation={rule['aggregation']}&interval={_grain(bucket)}&timespan=P{days}D"
                    + (f"&metricnamespace={rule['namespace']}" if rule["namespace"] else "")
                    + (f"&$filter={metric_filter}" if metric_filter else ""))}]}
