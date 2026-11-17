"""What Investigate asks a tracing tool. App Insights answers today; another tool (New Relic) adds a source with the
same questions and shapes, and the page never needs to know which one answered.

Shapes returned:
operation: operation, role, total, failed, codes, last_seen
exception: type, operation, role, total, last_seen, trace_id, instance, message (the latest one's)
dependency: type, target, role, total, failed, failed_pct, codes, p95_ms, last_seen, trace_id (one failed call's)
sample:    time, trace_id, role, instance, code, duration_ms, url, success
span:      time, kind (request|dependency|exception|trace), name, role, instance, success, duration_ms, code, target,
           message, stack, severity, id, parent, source; `trace` adds offset_ms and depth
line:      time, kind (trace|exception), severity, role, instance, operation, trace_id, message"""
import re
from datetime import datetime, timedelta, timezone

from ..guarded import ServiceError

WINDOWS = {"1h": 1, "6h": 6, "24h": 24}
TRACE_SPREAD = timedelta(minutes=30)  # a request's spans are read this far either side of its time
MAX_OPERATIONS, MAX_SAMPLES, MAX_SPANS, MAX_LINES = 25, 20, 500, 100
# W3C trace IDs are 32 hex characters; older App Insights SDKs used "|abc.1." style IDs.
TRACE_ID = re.compile(r"^[A-Za-z0-9|._-]{8,64}$")
SEVERITIES = {0: "Verbose", 1: "Information", 2: "Warning", 3: "Error", 4: "Critical"}


class InvestigateError(ServiceError):
    """A tracing tool couldn't answer; every source's own error extends this."""


def now():
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def window_span(window):
    """(start, end) for the last `window` (one of WINDOWS)."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {list(WINDOWS)}")
    end = now()
    return end - timedelta(hours=WINDOWS[window]), end


def parse_time(text):
    """An ISO time from the page (a sample's), as UTC; ValueError when it isn't one or lies in the future."""
    try:
        moment = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("at must be an ISO time") from None
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    if moment > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("at can't be in the future")
    return moment.astimezone(timezone.utc)


def trace_span(at):
    """Where one request's spans are looked for: around its time, or the last day when only its ID is known."""
    if at is None:
        end = now()
        return end - timedelta(hours=24), end
    return at - TRACE_SPREAD, min(at + TRACE_SPREAD, now() + timedelta(minutes=1))


def check_trace_id(trace_id):
    if not TRACE_ID.match(trace_id or ""):
        raise ValueError("trace must be an operation ID (8 to 64 letters, digits or | . _ -)")
    return trace_id


def tree(spans):
    """Spans in time order, each with its offset from the first and its depth under its parent (0 for a root, or for one
    whose parent wasn't read)."""
    spans = sorted(spans, key=lambda s: s["time"] or "")
    by_id = {s["id"]: s for s in spans if s.get("id")}
    start = _ms(spans[0]["time"]) if spans else 0

    def depth(span, seen=()):
        parent = by_id.get(span.get("parent"))
        if not parent or parent is span or parent["id"] in seen:
            return 0
        return 1 + depth(parent, seen + (span.get("id"),))

    return [{**s, "offset_ms": max(0, _ms(s["time"]) - start), "depth": min(depth(s), 8)} for s in spans]


def _ms(text):
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


class TraceSource:
    """One place requests are traced (an App Insights component). Each question has a build step (`*_queries`: what
    would be sent, sent nowhere) and the read, which sends exactly those; reads return {"rows", "queries", "errors"}."""

    kind = ""
    label = ""

    def operations_queries(self, window):
        raise NotImplementedError

    def operations(self, window):
        """Operations with failed requests in the window, worst first."""
        raise NotImplementedError

    def exceptions_queries(self, window):
        raise NotImplementedError

    def dependencies_queries(self, window):
        raise NotImplementedError

    def dependencies(self, window):
        """Outgoing calls (databases, queues, other APIs) that failed in the window, by type and target, most first."""
        raise NotImplementedError

    def exceptions(self, window):
        """Exceptions in the window by type, operation and service, most first, each with its latest trace."""
        raise NotImplementedError

    def samples_queries(self, operation, role, window, failed=True):
        raise NotImplementedError

    def samples(self, operation, role, window, failed=True):
        """The latest requests of one operation (failed only, by default), newest first."""
        raise NotImplementedError

    def trace_queries(self, trace_id, at=None):
        raise NotImplementedError

    def trace(self, trace_id, at=None):
        """Every span of one request end to end, with its exceptions (and stack) and its log lines."""
        raise NotImplementedError

    def search_queries(self, text, severity, window):
        raise NotImplementedError

    def search(self, text, severity, window):
        """Log lines and exceptions containing `text`, at `severity` or worse, newest first."""
        raise NotImplementedError
