"""Application Insights as a trace source: KQL on a component's own query endpoint, one GET per question. A request's
spans can sit in more than one component (API Management logs to its own), so `trace` reads each one it's given."""
import json
from concurrent.futures import ThreadPoolExecutor

from ..arm import ArmError
from ..explore import appinsights
from .source import (MAX_LINES, MAX_OPERATIONS, MAX_SAMPLES, MAX_SPANS, SEVERITIES, TraceSource, check_trace_id,
                     trace_span, tree, window_span)

STACK_CHARS = 8000

OPERATIONS = ("requests | summarize total=count(), failed=countif(success == false), "
              "codes=make_set_if(resultCode, success == false, 5), last_seen=maxif(timestamp, success == false) "
              f"by operation=operation_Name, role=cloud_RoleName | where failed > 0 | top {MAX_OPERATIONS} by failed")

EXCEPTIONS = ("exceptions | extend message=coalesce(outerMessage, innermostMessage) | summarize total=count(), "
              "arg_max(timestamp, operation_Id, cloud_RoleInstance, message) by type, operation=operation_Name, "
              "role=cloud_RoleName | project type, operation, role, total, last_seen=timestamp, trace_id=operation_Id, "
              f"instance=cloud_RoleInstance, message | top {MAX_OPERATIONS} by total")
MESSAGE_CHARS = 300

DEPENDENCIES = ("dependencies | summarize total=count(), failed=countif(success == false), "
                "codes=make_set_if(resultCode, success == false, 5), p95_ms=round(percentile(duration, 95), 0), "
                "last_seen=maxif(timestamp, success == false), trace_ids=make_set_if(operation_Id, success == false, 1) "
                f"by type, target, role=cloud_RoleName | where failed > 0 | top {MAX_OPERATIONS} by failed")

SPAN_TABLES = {
    "request": "requests | where operation_Id == {id} | project timestamp, span_kind='request', name, "
               "role=cloud_RoleName, instance=cloud_RoleInstance, success=tostring(success), duration_ms=duration, "
               "code=resultCode, target=url, message='', stack='', severity=int(null), id, parent=operation_ParentId",
    "dependency": "dependencies | where operation_Id == {id} | project timestamp, span_kind='dependency', name, "
                  "role=cloud_RoleName, instance=cloud_RoleInstance, success=tostring(success), duration_ms=duration, "
                  "code=resultCode, target=strcat(type, ' ', target), message=data, stack='', severity=int(null), id, "
                  "parent=operation_ParentId",
    "exception": "exceptions | where operation_Id == {id} | project timestamp, span_kind='exception', name=type, "
                 "role=cloud_RoleName, instance=cloud_RoleInstance, success='False', duration_ms=real(null), code='', "
                 "target=method, message=coalesce(outerMessage, innermostMessage), "
                 "stack=coalesce(tostring(details[0].rawStack), tostring(details[0].parsedStack)), "
                 "severity=severityLevel, id=itemId, parent=operation_ParentId",
    "trace": "traces | where operation_Id == {id} | project timestamp, span_kind='trace', name='', role=cloud_RoleName, "
             "instance=cloud_RoleInstance, success='', duration_ms=real(null), code='', target='', message, stack='', "
             "severity=severityLevel, id=itemId, parent=operation_ParentId",
}


def kql_string(text):
    """A KQL string literal; JSON string escaping is valid KQL."""
    return json.dumps(str(text))


# This endpoint rejects `time` and `kind` as column names (400 "invalid properties"); rows get them back here.
RENAMED = {"timestamp": "time", "span_kind": "kind"}


def _read(client, component_id, query, start, end):
    """(rows, error or "")."""
    try:
        rows = appinsights.query(client, component_id, query["query"], start, end)
        return [{RENAMED.get(k, k): v for k, v in row.items()} for row in rows], ""
    except ArmError as error:
        return [], f"{component_id.rsplit('/', 1)[-1]}: {appinsights.why(error)}"


def _sent(component_id, kql, start, end, label=""):
    return {"label": label or component_id.rsplit("/", 1)[-1], "component_id": component_id, "query": kql,
            "timespan": f"{appinsights.iso(start)}/{appinsights.iso(end)}", "start": start, "end": end}


def _public(queries):
    return [{k: q[k] for k in ("label", "component_id", "query", "timespan")} for q in queries]


class AppInsightsSource(TraceSource):
    kind = "appinsights"
    label = "Application Insights"

    def __init__(self, client, component_id, also=()):
        """`also`: other components a request's spans may be in (an API Management logger's)."""
        self.client = client
        self.component_id = component_id
        self.also = [c for c in dict.fromkeys(also) if c.lower() != component_id.lower()]

    def _run(self, queries):
        rows, errors = [], []
        for query in queries:
            found, error = _read(self.client, query["component_id"], query, query["start"], query["end"])
            rows += [{**r, "source": query["label"]} for r in found]
            errors += [error] if error else []
        return {"rows": rows, "queries": _public(queries), "errors": errors}

    def operations_queries(self, window):
        return [_sent(self.component_id, OPERATIONS, *window_span(window))]

    def operations(self, window):
        out = self._run(self.operations_queries(window))
        for row in out["rows"]:
            row["failed_pct"] = round(100.0 * row["failed"] / row["total"], 1) if row.get("total") else None
        return out

    def exceptions_queries(self, window):
        return [_sent(self.component_id, EXCEPTIONS, *window_span(window))]

    def exceptions(self, window):
        out = self._run(self.exceptions_queries(window))
        for row in out["rows"]:
            row["message"] = (row.get("message") or "")[:MESSAGE_CHARS]
        return out

    def dependencies_queries(self, window):
        return [_sent(self.component_id, DEPENDENCIES, *window_span(window))]

    def dependencies(self, window):
        out = self._run(self.dependencies_queries(window))
        for row in out["rows"]:
            ids = row.pop("trace_ids", None)
            if isinstance(ids, str):
                try:
                    ids = json.loads(ids)
                except ValueError:
                    ids = []
            row["trace_id"] = (ids or [None])[0]
            row["failed_pct"] = round(100.0 * row["failed"] / row["total"], 1) if row.get("total") else None
        return out

    def samples_queries(self, operation, role, window, failed=True):
        kql = (f"requests | where operation_Name == {kql_string(operation)} and cloud_RoleName == {kql_string(role)}"
               + (" and success == false" if failed else "")
               + " | project timestamp, trace_id=operation_Id, role=cloud_RoleName, instance=cloud_RoleInstance, "
                 "code=resultCode, duration_ms=round(duration, 0), url, success"
               f" | order by timestamp desc | take {MAX_SAMPLES}")
        return [_sent(self.component_id, kql, *window_span(window))]

    def samples(self, operation, role, window, failed=True):
        return self._run(self.samples_queries(operation, role, window, failed))

    def trace_queries(self, trace_id, at=None):
        literal = kql_string(check_trace_id(trace_id))
        kql = ("union isfuzzy=true " + ", ".join(f"({t.format(id=literal)})" for t in SPAN_TABLES.values())
               + f" | order by timestamp asc | take {MAX_SPANS}")
        start, end = trace_span(at)
        return [_sent(c, kql, start, end, label) for c, label in
                [(self.component_id, ""), *((c, f"{c.rsplit('/', 1)[-1]} (gateway)") for c in self.also)]]

    def trace(self, trace_id, at=None):
        queries = self.trace_queries(trace_id, at)
        with ThreadPoolExecutor(max_workers=4) as pool:
            parts = list(pool.map(lambda q: (q, *_read(self.client, q["component_id"], q, q["start"], q["end"])), queries))
        spans, errors = [], []
        for query, rows, error in parts:
            errors += [error] if error else []
            spans += [{**r, "source": query["label"], "gateway": query["component_id"] != self.component_id,
                       "stack": (r.get("stack") or "")[:STACK_CHARS],
                       "severity": SEVERITIES.get(r.get("severity"), r.get("severity"))} for r in rows]
        return {"rows": tree(spans), "truncated": any(len(r) >= MAX_SPANS for _, r, _ in parts),
                "queries": _public(queries), "errors": errors}

    def search_queries(self, text, severity, window):
        where = []
        if text:
            where.append(f"message contains {kql_string(text)}")
        if severity:
            where.append(f"severity >= {int(severity)}")
        common = "role=cloud_RoleName, instance=cloud_RoleInstance, operation=operation_Name, trace_id=operation_Id"
        kql = (f"union isfuzzy=true (traces | project timestamp, span_kind='trace', severity=severityLevel, message, {common}), "
               "(exceptions | project timestamp, span_kind='exception', severity=severityLevel, "
               f"message=strcat(type, ': ', coalesce(outerMessage, innermostMessage)), {common})"
               + (f" | where {' and '.join(where)}" if where else "")
               + f" | order by timestamp desc | take {MAX_LINES}")
        return [_sent(self.component_id, kql, *window_span(window))]

    def search(self, text, severity, window):
        out = self._run(self.search_queries(text, severity, window))
        for row in out["rows"]:
            row["severity"] = SEVERITIES.get(row.get("severity"), row.get("severity"))
        out["truncated"] = len(out["rows"]) >= MAX_LINES
        return out
