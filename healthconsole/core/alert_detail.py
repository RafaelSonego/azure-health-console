"""What a fired Azure Monitor alert says about itself, in words, and where to look at it: shared by a resource's
signals and its timeline. Read from the alert's own Resource Graph row (`properties.context`), so it costs no call."""
import re

# KQL projected next to an alert's essentials (`e`): its ID, rule, and the first criterion that fired.
FIELDS = (" alert_id=id, rule=tostring(e.alertRule), signal=tostring(e.signalType), description=tostring(e.description),"
          " criterion=properties.context.context.condition.allOf[0],"
          " window=tostring(properties.context.context.condition.windowSize)")
OPERATORS = {"greaterthan": ">", "greaterthanorequal": "≥", "lessthan": "<", "lessthanorequal": "≤", "equals": "=",
             "notequals": "≠", "greaterorlessthan": "outside"}
DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$", re.I)
PORTAL = re.compile(r"^https://portal\.azure\.com[/#]", re.I)


def duration(text):
    """"PT5M" → "5 min", "PT1H" → "1 h", "P1D" → "1 day"; anything else as given."""
    match = DURATION.match(text or "")
    if not match or not any(match.groups()):
        return text or ""
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return " ".join(part for part in (f"{days} day{'s' if days > 1 else ''}" if days else "", f"{hours} h" if hours else "",
                                      f"{minutes} min" if minutes else "") if part)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value) if value not in (None, "") else ""
    return f"{number:,.0f}" if number == int(number) else f"{number:,.2f}".rstrip("0").rstrip(".")


def condition(criterion, window=""):
    """"Average CapacityUnits was 92.3, alert when > 80 (over 5 min)"; "" when the alert doesn't say."""
    if not isinstance(criterion, dict):
        return ""
    if criterion.get("searchQuery") is not None:
        what = f"{criterion.get('timeAggregation') or 'Count'} of {criterion.get('metricMeasureColumn') or 'query results'}"
    else:
        what = " ".join(filter(None, [criterion.get("timeAggregation"), criterion.get("metricName")]))
    if not what:
        return ""
    operator = OPERATORS.get(str(criterion.get("operator") or "").lower(), criterion.get("operator") or "")
    value, threshold = _number(criterion.get("metricValue")), _number(criterion.get("threshold"))
    text = f"{what} was {value}" if value else what
    if operator and threshold:
        text += f", alert when {operator} {threshold}"
    return text + (f" (over {duration(window)})" if window else "")


def links(row):
    """[{"label", "resource"|"alert"|"url"}]: the rule and the alert (the page makes portal links for this tenant), and
    for a log alert its query results in the portal."""
    out = []
    if row.get("alert_id"):
        out.append({"label": "Alert", "alert": row["alert_id"]})
    if (row.get("rule") or "").lower().startswith("/subscriptions/"):
        out.append({"label": "Alert rule", "resource": row["rule"]})
    criterion = row.get("criterion") if isinstance(row.get("criterion"), dict) else {}
    results = criterion.get("linkToFilteredSearchResultsUI") or criterion.get("linkToSearchResultsUI") or ""
    if PORTAL.match(results):
        out.append({"label": "Query results", "url": results})
    return out


def when(iso):
    """"2026-11-11 12:02 UTC" from an Azure time."""
    return f"{iso[:10]} {iso[11:16]} UTC" if iso and len(iso) >= 16 else iso or ""
