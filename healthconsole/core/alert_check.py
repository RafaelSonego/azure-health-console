"""Is this alert right? For any alert whose source can give its rule and the history of what the rule watches (Azure
Monitor metric alerts, a monitoring tool's conditions), the same arithmetic in every case: how much of the last days the
rule was true, the value now against last week and against the usual, and the threshold against the usual. Every
limit is a setting (`alert_check.*`) and every verdict carries its numbers and the rule that produced it, so nothing
is decided out of sight (README → "Is this alert right?")."""
import statistics
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

OPERATORS = {">": lambda v, t: v > t, ">=": lambda v, t: v >= t, "<": lambda v, t: v < t, "<=": lambda v, t: v <= t,
             "=": lambda v, t: v == t, "!=": lambda v, t: v != t}
ABOVE = (">", ">=")
BELOW = ("<", "<=")
WEEK = timedelta(days=7)


class AlertHistory(ABC):
    """What a source must answer for the check (Azure Monitor and each monitoring tool implement it; the core never
    knows which one it's talking to). A rule is a dict:
      source, name, query {"label", "text"}, operator (one of OPERATORS), threshold (number or None for a dynamic one),
      window_minutes (the period one value covers), duration_minutes (how long it must hold), additive (True for counts
      and sums, False for averages, maxima, minima; None when unknown), per_bucket (True when the source divides the
      value by the bucket's length, e.g. NRQL's rate() over a level), function, unit, links ([{"label", "url"} or
      {"label", "resource": an Azure ID}], the rule in its own tool), notes.
    A series is {"points": [[iso time, value or None], ...], "bucket_minutes", "queries": [...], "notes": [...]},
    oldest first, one point per bucket."""

    @abstractmethod
    def alert_rule(self, ref):
        """The rule behind one alert (`ref` is the source's own reference), or raise with why it can't be read."""

    @abstractmethod
    def alert_series(self, rule, days):
        """What the rule watches over the last `days`: in the rule's own window when the source's reads allow it,
        else the smallest bucket they do (`bucket_for`, `spans`)."""


def _parse(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))


def scale_for(rule, bucket_minutes):
    """(factor, explanation): a count or sum read in a bucket bigger than the rule's own window is divided down to that
    window; an average, maximum or minimum is compared as it is (a bigger bucket can smooth a short spike)."""
    window = rule.get("window_minutes") or bucket_minutes
    if not bucket_minutes or bucket_minutes <= window:
        return 1.0, f"read in {bucket_minutes:g}-minute buckets, the rule's own window"
    if rule.get("per_bucket"):
        return bucket_minutes / window, (f"read in {bucket_minutes:g}-minute buckets and multiplied by "
                                         f"{bucket_minutes / window:g} to match the rule's {window:g}-minute window (a "
                                         f"{rule.get('function') or 'rate'} is divided by the bucket's length)")
    if rule.get("additive"):
        return window / bucket_minutes, (f"read in {bucket_minutes}-minute buckets and divided by {bucket_minutes / window:g} "
                                         f"to match the rule's {window:g}-minute window (a {rule.get('function') or 'count'} adds up)")
    return 1.0, (f"read in {bucket_minutes}-minute buckets and compared as they are (a {rule.get('function') or 'level'} doesn't "
                 f"add up; a spike shorter than a bucket can be smoothed out)")


def assess(rule, series, settings):
    """The numbers and the verdicts. `settings` is the config's `alert_check` section."""
    bucket = series.get("bucket_minutes") or 0
    factor, how = scale_for(rule, bucket)
    raw = [(_parse(t), v) for t, v in series.get("points") or []]
    points = [(t, v * factor) for t, v in raw if v is not None]
    out = {"rule": rule, "bucket_minutes": bucket, "scaled": how, "points": [[t.isoformat(), v] for t, v in points],
           "queries": series.get("queries") or [], "notes": list(rule.get("notes") or []) + list(series.get("notes") or []),
           "numbers": {}, "verdicts": []}
    minimum = int(settings["min_buckets"])
    if len(points) < minimum:
        out["verdicts"].append(_verdict("unknown", f"Not enough history: {len(points)} value(s), {minimum} needed.",
                                        f"needs at least {minimum} values", "alert_check.min_buckets"))
        return out
    complete = points[:-1] if len(points) > minimum else points  # the latest bucket is usually still filling in
    now_at, now = complete[-1]
    by_time = {t: v for t, v in complete}
    since = now_at - timedelta(days=int(settings["days"]))  # sources read a day more, so last week has a value
    values = [v for t, v in complete if t > since]
    week_ago = by_time.get(now_at - WEEK)
    if week_ago is None and bucket:  # an empty bucket then: the nearest value within one bucket of it
        near = [(abs((t - (now_at - WEEK)).total_seconds()), v) for t, v in complete
                if abs((t - (now_at - WEEK)).total_seconds()) <= bucket * 60]
        week_ago = min(near, key=lambda pair: pair[0])[1] if near else None
    usual = statistics.median(values)
    threshold, op = rule.get("threshold"), rule.get("operator")
    numbers = {"values": len(values), "now": now, "now_at": now_at.isoformat(), "usual": usual,
               "week_ago": week_ago, "now_vs_week": _ratio(now, week_ago), "now_vs_usual": _ratio(now, usual),
               "min": min(values), "max": max(values)}
    out["numbers"] = numbers
    if threshold is not None and op in OPERATORS:
        hits = sum(1 for v in values if OPERATORS[op](v, threshold))
        share = hits / len(values) * 100
        numbers.update(threshold=threshold, operator=op, breaching=hits, breaching_percent=round(share, 2 if share < 1 else 1),
                       threshold_vs_usual=_ratio(threshold, usual), true_now=OPERATORS[op](now, threshold))
        always, rare = float(settings["always_on_percent"]), float(settings["rare_percent"])
        side = "below" if op in ABOVE else "above" if op in BELOW else "at"
        if share >= always:
            out["verdicts"].append(_verdict(
                "warning", f"Not new: the rule was true in {hits} of {len(values)} buckets ({share:.0f}%). Either its "
                f"threshold is {side} the values this resource normally has, or this has been the case for most of "
                f"the period and nobody has fixed it.",
                f"true in at least {always:g}% of the buckets", "alert_check.always_on_percent"))
        elif share <= rare and numbers["true_now"]:
            out["verdicts"].append(_verdict(
                "critical", f"Unusual: the rule is true now and was true in only {hits} of {len(values)} buckets ({_share(share)}).",
                f"true now, and in at most {rare:g}% of the buckets", "alert_check.rare_percent"))
        elif share <= rare:
            out["verdicts"].append(_verdict(
                "ok", f"Rare and not true now: true in {hits} of {len(values)} buckets ({_share(share)}), not in the latest "
                f"one, so it has cleared (an alert can stay open until its source resolves it).",
                f"not true now, and in at most {rare:g}% of the buckets", "alert_check.rare_percent"))
        else:
            out["verdicts"].append(_verdict(
                "info", f"The rule was true in {hits} of {len(values)} buckets ({_share(share)}): neither always on nor rare.",
                f"between {rare:g}% and {always:g}%", "alert_check.rare_percent, alert_check.always_on_percent"))
    else:
        out["verdicts"].append(_verdict("info", "The rule has no fixed threshold (the source sets it itself), so only "
                                        "the values are compared.", "no fixed threshold", ""))
    change = 1 + float(settings["change_percent"]) / 100
    ratio = numbers["now_vs_week"]
    if week_ago == 0:  # nothing last week: the same if nothing now either, else new
        ratio = numbers["now_vs_week"] = 1.0 if now == 0 else None
        if now != 0:
            out["verdicts"].append(_verdict("warning", f"New since last week: {now:,.2f} now, 0 at the same time last week.",
                                            "0 last week, more than 0 now", ""))
    if ratio is not None and (ratio >= change or ratio <= 1 / change):
        word = "higher" if ratio >= change else "lower"
        out["verdicts"].append(_verdict(
            "warning" if word == "higher" else "info",
            f"Now {ratio:.2f}× the same time last week: {word} than last week.",
            f"now ÷ last week at least {change:g}× (or at most {1 / change:.2f}×)", "alert_check.change_percent"))
    elif ratio is not None:
        out["verdicts"].append(_verdict("ok", f"Now {ratio:.2f}× the same time last week: about the same as last week.",
                                        f"within {1 / change:.2f}× to {change:g}×", "alert_check.change_percent"))
    return out


def _share(percent):
    return f"{percent:.0f}%" if percent >= 1 or percent == 0 else f"{percent:.2f}%"


def _ratio(a, b):
    return round(a / b, 2) if a is not None and b not in (None, 0) else None


def _verdict(status, text, rule, setting):
    return {"status": status, "text": text, "rule": rule, "setting": setting}


def bucket_for(days, window_minutes, max_buckets, offered=None, reads=1):
    """The smallest bucket that is at least the rule's window and fits `days` in `reads` reads of `max_buckets` (or,
    with `offered`, the smallest of the source's own sizes that does). Without `offered`, the window itself is a size."""
    need = max(window_minutes or 1, days * 1440 / (max_buckets * reads))
    sizes = offered or sorted({*(1, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440), *((window_minutes,) if window_minutes else ())})
    return next((s for s in sizes if s >= need), sizes[-1])


def spans(days, bucket_minutes, max_buckets, now):
    """[(since, until), ...] covering the last `days` up to `now`, oldest first, each at most `max_buckets` buckets
    and starting on a bucket boundary, so reads made in parallel tile without gaps or overlaps."""
    step = timedelta(minutes=bucket_minutes)
    start = now - timedelta(days=days)
    start -= timedelta(seconds=start.timestamp() % step.total_seconds())
    out = []
    while start < now:
        until = min(start + step * max_buckets, now)
        out.append((start, until))
        start = until
    return out


def check(source, ref, settings):
    """The whole check for one alert from any source: its rule, its history (a day more than `alert_check.days`, so
    the oldest value still has one a week before it), and the verdicts."""
    rule = source.alert_rule(ref)
    series = source.alert_series(rule, int(settings["days"]) + 1)
    return {**assess(rule, series, settings), "source": rule.get("source") or getattr(source, "label", ""),
            "days": int(settings["days"])}
