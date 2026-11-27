"""Health for every node without knowing its type in advance. Layers: generic signals,
then existing metric alert rules, then a curated profile, then a name-based fallback."""
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from . import access, alert_detail
from .arm import AccessDenied, ArmError, kql_list
from .model import CRITICAL, HEALTHY, INFO, UNKNOWN, WARNING, Signal, worst
from .explore import detectors, keyvault, scaling
from .profiles import MetricSpec

logger = logging.getLogger(__name__)

RESOURCE_HEALTH_API_VERSION = "2025-05-01"
METRICS_API_VERSION = "2018-01-01"
MAX_PARALLEL_NODES = 8
METRICS_PER_CALL = 20
METRIC_INTERVAL = "PT5M"
ID_BATCH = 100

HEURISTIC_PATTERN = re.compile(
    r"error|fail|5xx|throttl|unhealthy|deadletter|availab|latency|responsetime"
    r"|percent|evict|timeout|dropped|rejected|exception",
    re.IGNORECASE,
)

_AVAILABILITY = {"available": HEALTHY, "degraded": WARNING,
                 "unavailable": CRITICAL, "unknown": UNKNOWN}
_AGGREGATION_KEYS = {"Average": "average", "Maximum": "maximum", "Minimum": "minimum",
                     "Total": "total", "Count": "count"}


@dataclass
class HealthOptions:
    window_minutes: int = 15
    compare_last_week: bool = True
    changes_hours: int = 24
    alert_hours: int = 24
    critical_max_severity: int = 1
    warning_max_severity: int = 2
    heuristic_limit: int = 8
    detectors: bool = True
    expiry_warning_days: int = 30
    expiry_critical_days: int = 7
    baseline_change_percent: int = 200
    aks_leftover_hours: int = 24


@dataclass
class _Check:
    spec: MetricSpec
    source: str
    rule: str = ""
    severity: int | None = None
    dynamic: bool = False


def assess(client, nodes, options, profiles, progress=None, on_quick=None):
    """Fills each node's signals, alerts, changes and status. Returns (warnings, service_health). Azure's detectors
    (often 10–25 s each) run last: `on_quick()` is called once every other signal is in, so the page can show them."""
    report = progress or (lambda message: None)
    warnings = []
    readable = [node for node in nodes if node.readable]
    node_ids = [node.id for node in readable]

    report("Reading fired alerts, recent changes and alert rules")
    alerts = _batched(client, lambda c, b: _fired_alerts(c, b, options.alert_hours),
                      node_ids, warnings, "fired alerts")
    changes = _batched(client, lambda c, b: _recent_changes(c, b, options.changes_hours),
                       node_ids, warnings, "recent changes")
    rules = _batched(client, _metric_rules, node_ids, warnings, "metric alert rules")
    autoscale = _batched(client, scaling.autoscale_settings, node_ids, warnings, "autoscale settings")
    service_health = _service_health(client, readable, warnings)

    report(f"Checking health and metrics for {len(readable)} resources")

    def run(node):
        try:
            _assess_node(client, node, options, profiles,
                         alerts.get(node.id, []), changes.get(node.id, []),
                         rules.get(node.id, []), autoscale.get(node.id, []), detectors_too=False)
        except Exception as error:  # one bad node mustn't sink the snapshot
            logger.exception("node assessment failed")
            node.signals.append(Signal("console", "assessment", UNKNOWN,
                                       f"Couldn't assess: {str(error)[:200]}"))
            node.status = worst(s.status for s in node.signals)

    done = [0]
    done_lock = threading.Lock()

    def run_and_count(node):
        run(node)
        with done_lock:
            done[0] += 1
            report(f"Checked {done[0]} of {len(readable)} resources")

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_NODES) as pool:
        list(pool.map(run_and_count, readable))
    if options.detectors and readable:
        if on_quick:
            on_quick()
        report(f"Waiting for Azure's detectors on {len(readable)} resource(s)")

        def detect(node):
            found = _layer("detectors", lambda: _detector_signals(client, node))
            if found:
                node.signals = [s for s in node.signals if (s.source, s.name) != ("console", "signals")] + found
                node.status = worst(s.status for s in node.signals)

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_NODES) as pool:
            list(pool.map(detect, readable))

    for node in nodes:
        if not node.readable:
            node.signals = [Signal("access", "readable", UNKNOWN, access.short(*access.describe(node.id)))]
            node.status = UNKNOWN
    return warnings, service_health


def _layer(name, build):
    """One signal source; if it fails, only its own signal says so and the rest stand."""
    try:
        result = build()
    except Exception as error:  # a timeout or a surprise in one source mustn't sink the node
        logger.warning("signal source %s failed: %s", name, error)
        return [Signal(name, name, UNKNOWN, f"Couldn't check {name}: {str(error)[:200]}")]
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


def _assess_node(client, node, options, profiles, alerts, changes, rules, autoscale=(), detectors_too=True):
    profile = profiles.get(node.type)
    signals = _layer("provisioning", lambda: _provisioning(node))
    if profile:
        signals += _layer("state", lambda: _states(node, profile))
    signals += _layer("resource health", lambda: _resource_health(client, node))
    signals += _layer("alerts", lambda: _alert_signals(alerts, options))
    signals += _layer("metrics", lambda: _metric_signals(client, node, options, profile, rules))
    signals += _layer("scaling", lambda: _scaling_signals(node, autoscale))
    if node.type == "microsoft.containerservice/managedclusters":
        signals += _layer("pods", lambda: _aks_signals(client, node, options.aks_leftover_hours))
    if node.type == "microsoft.keyvault/vaults":
        signals += _layer("key vault", lambda: _key_vault_signals(client, node, options))
    if options.detectors and detectors_too:
        signals += _layer("detectors", lambda: _detector_signals(client, node))
    node.signals = [signal for signal in signals if signal]
    if all(signal.status == INFO for signal in node.signals):
        node.signals.append(Signal("console", "signals", INFO,
                                   "Azure reports no health signals for this resource type."))
    node.alerts = alerts
    node.changes = changes
    node.status = worst(signal.status for signal in node.signals)


# ------------------------------------------------------------ generic signals


def _provisioning(node):
    state = str((node.properties.get("properties") or {}).get("provisioningState") or "")
    if not state:
        return None
    lowered = state.lower()
    if lowered == "succeeded":
        status = HEALTHY
    elif lowered in ("failed", "canceled"):
        status = CRITICAL
    else:
        status = WARNING
    return Signal("provisioning", "provisioningState", status, state)


def _lookup(data, path):
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _states(node, profile):
    signals = []
    for rule in profile.states:
        value = _lookup(node.properties, rule.path)
        if value is None:
            continue
        lowered = str(value).lower()
        if lowered in rule.critical:
            status = CRITICAL
        elif lowered in rule.warning:
            status = WARNING
        elif lowered in rule.healthy:
            status = HEALTHY
        else:
            status = INFO
        signals.append(Signal("state", rule.path.split(".")[-1], status, str(value)))
    return signals


def _resource_health(client, node):
    try:
        payload = client.get(
            f"{node.id}/providers/Microsoft.ResourceHealth/availabilityStatuses/current",
            RESOURCE_HEALTH_API_VERSION, tolerate=(400, 404, 422),
        )
    except AccessDenied as error:
        return Signal("resource health", "availability", UNKNOWN, error.short)
    except ArmError as error:
        if error.status == 409:
            return Signal("resource health", "availability", INFO,
                          "Microsoft.ResourceHealth isn't registered in this subscription.")
        return Signal("resource health", "availability", UNKNOWN, str(error)[:200])
    if not payload:
        return Signal("resource health", "availability", INFO, "Not reported for this resource type.")
    properties = payload.get("properties") or {}
    state = properties.get("availabilityState") or "Unknown"
    return Signal("resource health", "availability",
                  _AVAILABILITY.get(state.lower(), UNKNOWN),
                  f"{state}: {properties.get('summary') or ''}".strip(": "))


def _severity_status(severity, options):
    if severity is None:
        return INFO
    if severity <= options.critical_max_severity:
        return CRITICAL
    if severity <= options.warning_max_severity:
        return WARNING
    return INFO


def _alert_signals(alerts, options):
    """One signal per alert name; a stateless rule firing every 30 minutes is one problem, not 38."""
    grouped = {}
    for alert in alerts:
        grouped.setdefault(alert["name"], []).append(alert)
    signals = []
    for name, fired in grouped.items():
        severity = min((a["severity"] for a in fired if a["severity"] is not None), default=None)
        last = max(fired, key=lambda a: a["started"])
        times = f"fired {len(fired)} times" if len(fired) > 1 else ""  # when: `at`, shown as a time on the page
        detail = " · ".join(filter(None, [f"Sev{severity}" if severity is not None else "", times,
                                          alert_detail.condition(last.get("criterion"), last.get("window") or ""), last.get("service")]))
        signals.append(Signal("alert", name, _severity_status(severity, options), detail, at=last["started"],
                              links=alert_detail.links(last)))
    return signals


def _scaling_signals(node, autoscale):
    signals = []
    for item in scaling.items_for(node.properties, autoscale):
        if item["status"] in (CRITICAL, WARNING):
            limit = f" of max {item['max']}" if item["max"] else ""
            signals.append(Signal("scaling", f"{item['kind']} {item['name']}", item["status"],
                                  f"{item['current']}{limit}; {item['detail']}"))
    return signals


def _aks_signals(client, node, leftover_hours=24):
    from .explore import aks  # imported here: explore.aks reads this module's constants

    try:
        phases = aks.pod_phases(client, node.id)
    except AccessDenied as error:
        return [Signal("pods", "pods", UNKNOWN, error.short)]
    except ArmError:
        return []
    totals = {}
    for row in phases:
        totals.setdefault(row["phase"], []).append(row)
    totals["Failed"] = aks.failed_split(client, node.id, phases, leftover_hours)
    where = lambda rows, n: ", ".join(f"{r['namespace']} ({r[n]})" for r in sorted(rows, key=lambda r: -r[n])[:3] if r[n])  # noqa: E731
    signals = []
    for phase in ("Pending", "Failed", "Unknown"):
        rows = totals.get(phase)
        if not rows:
            continue
        count = sum(r["pods"] for r in rows)
        if phase != "Failed":
            signals.append(Signal("pods", f"{phase} pods", WARNING, f"{count} {phase.lower()}: {where(rows, 'pods')}", value=count))
            continue
        new = sum(r["new"] for r in rows)
        signals.append(Signal("pods", "Failed pods", WARNING, f"{count} failed, {new} in the last {leftover_hours} h: "
                              f"{where(rows, 'new')}", value=new) if new else
                       Signal("pods", "Failed pods", INFO, f"{count} failed, none in the last {leftover_hours} h: left over, "
                              f"usually already replaced (AKS → Workloads lists each with its age): {where(rows, 'pods')}",
                              value=count))
    if not any(s.status == WARNING for s in signals) and phases:
        running = sum(r["pods"] for r in phases if r["phase"] == "Running")
        signals.append(Signal("pods", "pods", HEALTHY, f"{running} running, none pending or {'newly ' if signals else ''}failed", value=running))
    return signals


def _key_vault_signals(client, node, options):
    result = keyvault.expiry(client, node.id, options.expiry_warning_days, options.expiry_critical_days,
                             max_pages=keyvault.PAGE_MAX_PAGES)
    found = keyvault.signals(result)
    if result["errors"] and not found:
        return [Signal("key vault", "secrets and keys", UNKNOWN, result["errors"][0].split("\n")[0][:200])]
    return found


def _detector_signals(client, node):
    """The same detectors as the Diagnostics tab (and the same kept answers), so the graph and the tab agree."""
    try:
        result = detectors.run(client, node.id)
    except ArmError:
        return []
    if result.get("error"):
        return [Signal("detector", "detectors", UNKNOWN, result["error"].split("\n")[0][:200])]
    signals = []
    for item in result.get("results", []):
        if item["status"] in (CRITICAL, WARNING):
            text = next((i["text"] for i in item["insights"] if i["status"] == item["status"]), item["title"])
            where = f"Azure 'Diagnose and solve problems' > {item['category'] or 'detectors'}, last 24 h"
            rated = " Azure rates it critical." if item["status"] == CRITICAL else ""
            # Detectors look back 24 h and mix in best practices, so they never colour a resource red alone.
            signals.append(Signal("detector", item["title"], WARNING, f"{text[:200]} ({where}).{rated}"))
    return signals


# ------------------------------------------------------------ batch queries


def _batched(client, query, node_ids, warnings, label):
    merged = {}
    for start in range(0, len(node_ids), ID_BATCH):
        try:
            for key, items in query(client, node_ids[start:start + ID_BATCH]).items():
                merged.setdefault(key, []).extend(items)
        except ArmError as error:
            warnings.append(f"Couldn't read {label}: {str(error)[:200]}")
            break
    return merged


def _parse_severity(value):
    match = re.search(r"\d", str(value or ""))
    return int(match.group()) if match else None


def _fired_alerts(client, node_ids, hours):
    """Stateful alerts count while still fired; stateless ones never resolve, so only recent ones count."""
    rows = client.query(
        "AlertsManagementResources"
        " | where type =~ 'microsoft.alertsmanagement/alerts'"
        " | extend e = properties.essentials"
        " | extend target = tolower(tostring(e.targetResource))"
        f" | where target in ({kql_list(node_ids)})"
        " | where tostring(e.monitorCondition) == 'Fired' and tostring(e.alertState) != 'Closed'"
        f" | where tobool(e.isStatefulAlert) or todatetime(e.startDateTime) > ago({int(hours)}h)"
        " | project target, name, severity=tostring(e.severity), state=tostring(e.alertState),"
        " started=tostring(e.startDateTime), service=tostring(e.monitorService),"
        + alert_detail.FIELDS +
        " | order by started desc"
    )
    alerts = {}
    for row in rows:
        alerts.setdefault(row["target"], []).append({
            "name": row.get("name") or "",
            "severity": _parse_severity(row.get("severity")),
            "state": row.get("state") or "",
            "started": row.get("started") or "",
            "service": row.get("service") or "",
            **{k: row.get(k) or "" for k in ("alert_id", "rule", "signal", "description", "criterion", "window")},
        })
    return alerts


def _recent_changes(client, node_ids, hours):
    rows = client.query(
        "resourcechanges"
        " | extend target = tolower(tostring(properties.targetResourceId)),"
        " at = todatetime(properties.changeAttributes.timestamp)"
        f" | where target in ({kql_list(node_ids)}) and at > ago({int(hours)}h)"
        " | project target, at, changeType=tostring(properties.changeType),"
        " changedBy=tostring(properties.changeAttributes.changedBy),"
        " clientType=tostring(properties.changeAttributes.clientType),"
        " fields=bag_keys(properties.changes)"
        " | order by at desc"
    )
    changes = {}
    for row in rows:
        changes.setdefault(row["target"], []).append({
            "at": row.get("at") or "",
            "type": row.get("changeType") or "",
            "by": row.get("changedBy") or "",
            "client": row.get("clientType") or "",
            "fields": (row.get("fields") or [])[:8],
        })
    return changes


def _metric_rules(client, node_ids):
    """Enabled metric alert rules scoped to these resources: someone already
    decided which metrics matter and where the line is."""
    rows = client.query(
        "Resources"
        " | where type =~ 'microsoft.insights/metricalerts' and properties.enabled == true"
        " | mv-expand scope = properties.scopes"
        " | extend scope = tolower(tostring(scope))"
        f" | where scope in ({kql_list(node_ids)})"
        " | mv-expand c = properties.criteria.allOf"
        " | project scope, rule=name, severity=toint(properties.severity),"
        " metric=tostring(c.metricName), namespace=tostring(c.metricNamespace),"
        " operator=tostring(c.operator), threshold=todouble(c.threshold),"
        " aggregation=tostring(c.timeAggregation), criterion=tostring(c.criterionType),"
        " dimensions=array_length(c.dimensions)"
    )
    rules = {}
    for row in rows:
        rules.setdefault(row["scope"], []).append(row)
    return rules


def _normalise_region(name):
    return re.sub(r"\s", "", (name or "")).lower()


def _service_health(client, nodes, warnings):
    subscriptions = sorted({node.subscription_id for node in nodes if node.subscription_id})
    regions = {_normalise_region(node.location) for node in nodes}
    if not subscriptions:
        return []
    try:
        rows = client.query(
            "ServiceHealthResources"
            " | where type =~ 'microsoft.resourcehealth/events'"
            " | where tostring(properties.Status) =~ 'Active'"
            " | where tostring(properties.EventType) in ('ServiceIssue', 'PlannedMaintenance')"
            " | project trackingId=tostring(properties.TrackingId), title=tostring(properties.Title),"
            " eventType=tostring(properties.EventType), impact=properties.Impact,"
            " lastUpdate=tostring(properties.LastUpdateTime)",
            subscriptions=subscriptions,
        )
    except ArmError as error:
        warnings.append(f"Couldn't read Service Health: {str(error)[:200]}")
        return []

    events = {}
    for row in rows:
        services, impacted = set(), set()
        for impact in row.get("impact") or []:
            services.add(impact.get("ImpactedService") or "")
            for region in impact.get("ImpactedRegions") or []:
                impacted.add(region.get("ImpactedRegion") or "")
        relevant = {r for r in impacted if _normalise_region(r) in regions or r.lower() == "global"}
        if impacted and not relevant:
            continue
        # Azure often raises several events (tracking IDs) with one title; show them as one.
        event = events.setdefault((row.get("eventType") or "", row.get("title") or ""), {
            "tracking_ids": [], "title": row.get("title") or "", "type": row.get("eventType") or "",
            "services": [], "regions": [], "last_update": ""})
        if row.get("trackingId") and row["trackingId"] not in event["tracking_ids"]:
            event["tracking_ids"].append(row["trackingId"])
        event["services"] = sorted(set(event["services"]) | {s for s in services if s})
        event["regions"] = sorted(set(event["regions"]) | relevant)
        event["last_update"] = max(event["last_update"], row.get("lastUpdate") or "")
    return sorted(events.values(), key=lambda event: event["last_update"], reverse=True)


# ------------------------------------------------------------------- metrics


def _metric_signals(client, node, options, profile, rules):
    checks, signals = [], []
    platform_namespace = node.type
    for rule in rules:
        namespace = (rule.get("namespace") or "").lower()
        if namespace and namespace != platform_namespace:
            signals.append(Signal("alert rule", rule.get("metric") or "", INFO,
                                  f"Rule '{rule.get('rule')}' uses custom namespace {namespace}; not checked."))
            continue
        dynamic = rule.get("criterion") == "DynamicThresholdCriterion"
        checks.append(_Check(
            spec=MetricSpec(name=rule.get("metric") or "",
                            aggregation=rule.get("aggregation") or "Average",
                            operator=rule.get("operator") or "GreaterThan",
                            crit=None if dynamic else rule.get("threshold")),
            source="alert rule", rule=rule.get("rule") or "",
            severity=rule.get("severity"), dynamic=dynamic,
        ))
    if profile:
        # A team's own rule on a metric outranks the generic profile threshold for it.
        ruled = {check.spec.name.lower() for check in checks}
        checks.extend(_Check(spec=spec, source="profile") for spec in profile.metrics
                      if spec.name.lower() not in ruled)

    try:
        definitions = _metric_definitions(client, node.id)
    except AccessDenied as error:
        return signals + [Signal("metrics", "metrics", UNKNOWN, error.short)]
    if not definitions:
        return signals

    if not checks:
        checks = _heuristic_checks(definitions, options.heuristic_limit)

    available = []
    for check in checks:
        definition = definitions.get(check.spec.name.lower())
        if not definition:
            signals.append(Signal(check.source, check.spec.name, INFO,
                                  "Metric isn't available on this resource."))
            continue
        check.spec = replace(check.spec, name=definition["name"])
        available.append(check)
    if not available:
        return signals

    now = datetime.now(timezone.utc).replace(microsecond=0)
    window = timedelta(minutes=options.window_minutes)
    current = _metric_values(client, node.id, available, now - window, now)
    baseline = {}
    if options.compare_last_week:
        week = timedelta(days=7)
        baseline = _metric_values(client, node.id, available, now - week - window, now - week)

    for check in available:
        key = (check.spec.name, check.spec.aggregation)
        definition = definitions[check.spec.name.lower()]
        signal = _evaluate(check, current.get(key), baseline.get(key), definition["unit"], options)
        signal.label = definition.get("label") or ""
        signals.append(signal)
    return signals


def _metric_definitions(client, resource_id):
    try:
        payload = client.get(f"{resource_id}/providers/Microsoft.Insights/metricDefinitions",
                             METRICS_API_VERSION, tolerate=(400, 404, 409))
    except AccessDenied:
        raise
    except ArmError:
        return {}
    definitions = {}
    for item in (payload or {}).get("value", []):
        name = (item.get("name") or {}).get("value")
        if name:
            definitions[name.lower()] = {
                "name": name,
                "unit": item.get("unit") or "",
                "primary": item.get("primaryAggregationType") or "Average",
                "label": (item.get("name") or {}).get("localizedValue") or "",
            }
    return definitions


def _heuristic_checks(definitions, limit):
    picked = [d for d in definitions.values()
              if HEURISTIC_PATTERN.search(d["name"]) and "," not in d["name"]]
    return [_Check(spec=MetricSpec(name=d["name"], aggregation=d["primary"]), source="heuristic")
            for d in picked[:limit]]


def _metric_values(client, resource_id, checks, start, end):
    """{(name, aggregation): value} over the window, one call per 20 metrics."""
    names = sorted({c.spec.name for c in checks if "," not in c.spec.name})
    aggregations = sorted({c.spec.aggregation for c in checks})
    values = {}
    for index in range(0, len(names), METRICS_PER_CALL):
        chunk = names[index:index + METRICS_PER_CALL]
        try:
            payload = client.get(
                f"{resource_id}/providers/Microsoft.Insights/metrics", METRICS_API_VERSION,
                params={
                    "metricnames": ",".join(chunk),
                    "timespan": f"{start.isoformat().replace('+00:00', 'Z')}/"
                                f"{end.isoformat().replace('+00:00', 'Z')}",
                    "interval": METRIC_INTERVAL,
                    "aggregation": ",".join(aggregations),
                },
                tolerate=(400, 404),
            )
        except ArmError:
            continue
        for metric in (payload or {}).get("value", []):
            name = (metric.get("name") or {}).get("value")
            points = []
            for series in metric.get("timeseries") or []:
                points.extend(series.get("data") or [])
            for aggregation in aggregations:
                values[(name, aggregation)] = _combine(points, aggregation)
    return values


def _combine(points, aggregation):
    key = _AGGREGATION_KEYS[aggregation]
    numbers = [point[key] for point in points if point.get(key) is not None]
    if not numbers:
        return None
    if aggregation == "Average":
        return sum(numbers) / len(numbers)
    if aggregation == "Maximum":
        return max(numbers)
    if aggregation == "Minimum":
        return min(numbers)
    return sum(numbers)


def _breached(value, operator, threshold):
    if threshold is None:
        return False
    return {
        "GreaterThan": value > threshold,
        "GreaterThanOrEqual": value >= threshold,
        "LessThan": value < threshold,
        "LessThanOrEqual": value <= threshold,
    }.get(operator, False)


_OPERATOR_SYMBOLS = {"GreaterThan": ">", "GreaterThanOrEqual": "≥",
                     "LessThan": "<", "LessThanOrEqual": "≤"}


def _evaluate(check, value, baseline, unit, options):
    spec = check.spec
    symbol = _OPERATOR_SYMBOLS.get(spec.operator, spec.operator)
    label = f"{spec.name} ({spec.aggregation})"
    signal = Signal(check.source, label, INFO, value=value, unit=unit, baseline=baseline)

    if check.source == "alert rule":
        signal.detail = f"Rule '{check.rule}', Sev{check.severity}"
        if check.dynamic:
            signal.threshold = "dynamic"
        elif spec.crit is not None:
            signal.threshold = f"{symbol} {spec.crit:g}"
    elif spec.warn is not None or spec.crit is not None:
        parts = [f"warn {symbol} {spec.warn:g}" if spec.warn is not None else "",
                 f"crit {symbol} {spec.crit:g}" if spec.crit is not None else ""]
        signal.threshold = ", ".join(p for p in parts if p)

    if value is None:
        signal.detail = (signal.detail + "; " if signal.detail else "") + "no data in window"
        return signal

    if check.source == "alert rule" and not check.dynamic and spec.crit is not None:
        signal.status = (_severity_status(check.severity, options)
                         if _breached(value, spec.operator, spec.crit) else HEALTHY)
    elif check.source == "profile" and (spec.warn is not None or spec.crit is not None):
        if _breached(value, spec.operator, spec.crit):
            signal.status = CRITICAL
        elif _breached(value, spec.operator, spec.warn):
            signal.status = WARNING
        else:
            signal.status = HEALTHY

    if baseline is not None and baseline > 0 and value >= options.baseline_change_percent / 100 * baseline:
        note = f"{value / baseline:.1f}x last week"
        signal.detail = f"{signal.detail}; {note}" if signal.detail else note
    return signal
