"""Application Insights as a trace source: KQL on a component's own query endpoint, one GET per question. A request's
spans can sit in more than one component (API Management logs to its own), so `trace` reads each one it's given."""
import json
from concurrent.futures import ThreadPoolExecutor

from ..arm import ArmError, kql_string  # noqa: F401 (pod and gateway import it from here)
from ..explore import appinsights
from .source import (MAX_LINES, MAX_OPERATIONS, MAX_SAMPLES, MAX_SPANS, SEVERITIES, TraceSource, check_trace_id,
                     trace_span, tree, window_span)

STACK_CHARS = 8000

# Counts are sum(itemCount), not count(): a sampled row stands for itemCount requests.
OPERATIONS = ("requests | summarize total=sum(itemCount), failed=sumif(itemCount, success == false), "
              "codes=make_set_if(resultCode, success == false, 5), last_seen=maxif(timestamp, success == false), "
              "path=take_anyif(name, success == false) "
              f"by operation=operation_Name, role=cloud_RoleName | where failed > 0 | top {MAX_OPERATIONS} by failed")

MAX_CODES, CODE_TOP = 30, 5
# One pass over the failed requests, by code and endpoint; from that, each code's totals (rows with `code_failed`) and its
# busiest endpoints, ranked within the code so a rare code keeps its endpoints too.
CODES = ("let byEndpoint = materialize(requests | where success == false | summarize failed=sum(itemCount), "
         "arg_max(timestamp, operation_Id, name) by code=resultCode, operation=operation_Name, role=cloud_RoleName); "
         "let totals = byEndpoint | summarize code_failed=sum(failed), arg_max(timestamp, operation_Id), "
         f"endpoints=count() by code | top {MAX_CODES} by code_failed; "
         "byEndpoint | join kind=leftsemi totals on code | order by code asc, failed desc "
         f"| extend rank=row_number(1, prev(code) != code) | where rank <= {CODE_TOP} "
         "| project code, operation, role, failed, last_seen=timestamp, path=name "
         "| union (totals | project code, code_failed, last_seen=timestamp, trace_id=operation_Id, endpoints)")
# Why failures without a usable HTTP status failed: responses cut short ("… [not sent in full …]", API Management) and
# code 0 (functions and jobs not called over HTTP), from the exceptions on those requests, most requests first, per code.
CUT_REASONS = ("requests | where success == false and (resultCode has 'not sent in full' or resultCode == '0') "
               "| distinct operation_Id, code=resultCode, itemCount | join kind=inner (exceptions "
               "| project operation_Id, type, problemId, message=coalesce(outerMessage, innermostMessage), timestamp) "
               "on operation_Id | summarize arg_max(timestamp, message) by operation_Id, code, itemCount, type, problemId "
               "| summarize requests=sum(itemCount), arg_max(timestamp, operation_Id, message) "
               "by code, type, problemId | order by code asc, requests desc "
               f"| extend rank=row_number(1, prev(code) != code) | where rank <= {CODE_TOP} "
               "| project code, type, problem=problemId, requests, message, trace_id=operation_Id, last_seen=timestamp")

EXCEPTIONS = ("exceptions | extend message=coalesce(outerMessage, innermostMessage) | summarize total=sum(itemCount), "
              "arg_max(timestamp, operation_Id, cloud_RoleInstance, message) by type, operation=operation_Name, "
              "role=cloud_RoleName | project type, operation, role, total, last_seen=timestamp, trace_id=operation_Id, "
              f"instance=cloud_RoleInstance, message | top {MAX_OPERATIONS} by total")
MESSAGE_CHARS = 300

MAX_DETAIL_ENDPOINTS, DETAIL_TOP = 100, 3
# For the endpoints listed: the backends their failed requests called that failed too (each request counted once per
# backend), how many had no failed backend call, and who called (API Management's subscription). `{picked}` is a datatable.
DETAILS = ("let picked = {picked}; "
           "let failed = materialize(requests | where success == false "
           "| join kind=inner hint.strategy=broadcast picked on $left.operation_Name == $right.operation, "
           "$left.cloud_RoleName == $right.role | where code == '' or resultCode == code "
           "| project operation_Id, operation, role, code, itemCount, caller=tostring(customDimensions['Subscription Name'])); "
           "let calls = materialize(failed | join kind=inner (dependencies | where success == false "
           "| distinct operation_Id, backend=target, backend_code=resultCode) on operation_Id); "
           "let totals = failed | summarize failed=sum(itemCount) by operation, role, code "
           "| join kind=leftouter (calls | distinct operation_Id, operation, role, code, itemCount "
           "| summarize with_backend=sum(itemCount) by operation, role, code) on operation, role, code "
           "| join kind=leftouter (calls | summarize backends=dcount(backend) by operation, role, code) on operation, role, code "
           "| project part='total', operation, role, code, failed, with_backend=coalesce(with_backend, 0), "
           "backends=coalesce(backends, 0); "
           "let backends = calls | summarize requests=sum(itemCount) by operation, role, code, backend, backend_code "
           "| order by operation asc, role asc, code asc, requests desc "
           "| extend rank=row_number(1, prev(operation) != operation or prev(role) != role or prev(code) != code) "
           "| where rank <= {top} | project part='backend', operation, role, code, backend, backend_code, requests; "
           "let callers = failed | where isnotempty(caller) | summarize requests=sum(itemCount) by operation, role, code, caller "
           "| order by operation asc, role asc, code asc, requests desc "
           "| extend rank=row_number(1, prev(operation) != operation or prev(role) != role or prev(code) != code) "
           "| where rank <= {top} | project part='caller', operation, role, code, caller, requests; "
           "union totals, backends, callers")

DEPENDENCIES = ("dependencies | summarize total=sum(itemCount), failed=sumif(itemCount, success == false), "
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

    def codes_queries(self, window):
        start, end = window_span(window)
        return [_sent(self.component_id, CODES, start, end, "by code"),
                _sent(self.component_id, CUT_REASONS, start, end, "why: no usable status")]

    def codes(self, window):
        sent = self.codes_queries(window)
        with ThreadPoolExecutor(max_workers=len(sent)) as pool:  # side by side: the section waits for the slowest only
            (found, error), (reasons, cut_error) = pool.map(
                lambda q: _read(self.client, self.component_id, q, q["start"], q["end"]), sent)
        rows = sorted(({"code": r.get("code"), "failed": r["code_failed"], "last_seen": r.get("last_seen"),
                        "trace_id": r.get("trace_id"), "endpoints": r.get("endpoints")}
                       for r in found if r.get("code_failed") is not None), key=lambda r: -(r["failed"] or 0))
        top = [r for r in found if r.get("code_failed") is None]
        every = sum(r["failed"] or 0 for r in rows)
        by_reason = {}
        for r in reasons:
            r["message"] = (r.get("message") or "")[:MESSAGE_CHARS]
            by_reason.setdefault(str(r.get("code")), []).append(
                {k: r.get(k) for k in ("type", "problem", "requests", "message", "trace_id", "last_seen")})
        by_code = {}
        for r in top:
            by_code.setdefault(str(r.get("code")), []).append(
                {k: r.get(k) for k in ("operation", "role", "failed", "last_seen", "path")})
        for r in rows:
            r["failed_pct"] = round(100.0 * r["failed"] / every, 1) if every else None
            r["top"] = by_code.get(str(r.get("code")), [])
            r["reasons"] = by_reason.get(str(r.get("code")), [])
        return {"rows": rows, "queries": _public(sent), "errors": [e for e in (error, cut_error) if e]}

    def exceptions(self, window):
        out = self._run(self.exceptions_queries(window))
        for row in out["rows"]:
            row["message"] = (row.get("message") or "")[:MESSAGE_CHARS]
        return out

    def endpoint_details_queries(self, endpoints, window):
        """`endpoints`: [{operation, role, code}] as the tables list them (code "" for any failure)."""
        if not endpoints or len(endpoints) > MAX_DETAIL_ENDPOINTS:
            raise ValueError(f"endpoints must be 1 to {MAX_DETAIL_ENDPOINTS}")
        rows = ", ".join(f"{kql_string(str(e.get('operation') or ''))}, {kql_string(str(e.get('role') or ''))}, "
                         f"{kql_string(str(e.get('code') or ''))}" for e in endpoints)
        picked = f"datatable(operation:string, role:string, code:string)[{rows}]"
        return [_sent(self.component_id, DETAILS.replace("{picked}", picked).replace("{top}", str(DETAIL_TOP)),
                      *window_span(window), "backends and callers")]

    def endpoint_details(self, endpoints, window):
        """{"rows": [{operation, role, code, failed, with_backend, backends: [{backend, code, requests}], backend_count,
        callers: [{caller, requests}]}], ...}: one row per endpoint asked about that failed in the window."""
        out = self._run(self.endpoint_details_queries(endpoints, window))
        key = lambda r: (r.get("operation") or "", r.get("role") or "", r.get("code") or "")  # noqa: E731
        merged = {}
        for r in out["rows"]:
            if r.get("part") == "total":
                merged[key(r)] = {"operation": r.get("operation"), "role": r.get("role"), "code": r.get("code") or "",
                                  "failed": r.get("failed"), "with_backend": r.get("with_backend"),
                                  "backend_count": r.get("backends"), "backends": [], "callers": []}
        for r in out["rows"]:
            at = merged.get(key(r))
            if at is None:
                continue
            if r.get("part") == "backend":
                at["backends"].append({"backend": r.get("backend"), "code": r.get("backend_code") or "",
                                       "requests": r.get("requests")})
            elif r.get("part") == "caller":
                at["callers"].append({"caller": r.get("caller"), "requests": r.get("requests")})
        out["rows"] = list(merged.values())
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

    def samples_queries(self, operation, role, window, failed=True, code=None):
        kql = (f"requests | where operation_Name == {kql_string(operation)} and cloud_RoleName == {kql_string(role)}"
               + (" and success == false" if failed else "")
               + (f" and resultCode == {kql_string(code)}" if code else "")
               + " | project timestamp, trace_id=operation_Id, role=cloud_RoleName, instance=cloud_RoleInstance, "
                 "code=resultCode, duration_ms=round(duration, 0), url, success"
               f" | order by timestamp desc | take {MAX_SAMPLES}")
        return [_sent(self.component_id, kql, *window_span(window))]

    def samples(self, operation, role, window, failed=True, code=None):
        return self._run(self.samples_queries(operation, role, window, failed, code))

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
