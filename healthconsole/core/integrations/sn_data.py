"""What the ServiceNow tab shows (incidents, changes now or soon, Event Management alerts and events), matched to
Azure resources in scope. Works without a CMDB; a CMDB that doesn't map a matched resource is reported."""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .matching import _cache, cached, hits_resource, inventory, match, merge, name_index  # noqa: F401 (re-exported)
from .servicenow import ServiceNowClient, ServiceNowError, forget_hidden, no_access

CACHE_SECONDS = 60
CLOSED_CACHE_SECONDS = 900  # closed changes don't change: kept longer than what's open
RELATED_PER_CHANGE = 3      # affected CIs or tasks read per change, at most (times change_max_records)
EVENT_WINDOW_HOURS = 24  # Event Management events this far back
# Fields that hold an Azure resource ID on a CI, when the instance's CMDB has one (e.g. from a cloud connector).
CI_ID_FIELDS = ("cmdb_ci.object_id", "cmdb_ci.correlation_id")
CI_VIA = ("configuration item", "affected CI", "change task")  # an Azure ID from any of these is the CMDB mapping it
TASK_CI_FIELDS = ("task", "ci_item", "ci_item.object_id", "ci_item.correlation_id")
CHANGE_TASK_FIELDS = ("change_request", "number", "short_description", "description", "state", "cmdb_ci",
                      *CI_ID_FIELDS)
INCIDENT_FIELDS = ("sys_id", "number", "short_description", "description", "priority", "state", "assignment_group",
                   "assigned_to", "opened_at", "sys_updated_on", "cmdb_ci")
CHANGE_FIELDS = ("sys_id", "number", "short_description", "description", "state", "type", "risk", "assignment_group",
                 "start_date", "end_date", "closed_at", "cmdb_ci", "close_code")
CHANGE_ORDER = {"in progress": 0, "past its window": 1, "scheduled": 2, "not scheduled": 3}  # then closed, newest first
ALERT_FIELDS = ("sys_id", "number", "description", "severity", "state", "node", "resource", "source", "cmdb_ci",
                "initial_remote_time", "last_remote_time")
EVENT_FIELDS = ("sys_id", "description", "severity", "state", "node", "resource", "source", "cmdb_ci", "time_of_event")


def _val(row, field, raw=False):
    cell = row.get(field)
    if isinstance(cell, dict):
        return (cell.get("value") if raw else cell.get("display_value") or cell.get("value")) or ""
    return cell or ""


READ_TABLES = ("incident", "change_request", "task_ci", "change_task", "em_alert", "em_event")


def _sn_time(moment):
    """ServiceNow's stored-time format (UTC), for a query."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _when(row, field):
    """ServiceNow's stored value is UTC ("2026-11-03 14:00:00"); ISO, so the page shows local time."""
    text = _val(row, field, raw=True)
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ""


def cmdb_note(record, matches):
    """When a record names an Azure resource but its CI doesn't map it, say so: the CMDB is out of step."""
    strong = [m for m in matches if m["confidence"] in ("exact", "name") and m.get("display_type") != "Subscription"]
    if not strong or any(m["confidence"] == "exact" and m["via"] in CI_VIA for m in strong):
        return ""
    ci = record["ci"]
    names = ", ".join(m["name"] for m in strong[:3])
    if not ci:
        return f"No configuration item is set, though it names {names}."
    if record["ci_ids"]:
        return f"Its configuration item ({ci}) points to a different Azure resource than {names}."
    return f"Its configuration item ({ci}) has no Azure resource ID, so the CMDB doesn't map {names}."


# ------------------------------------------------------------------ ServiceNow side


def _record(row, client, table, kind, match_fields, index, by_id, title_field="short_description"):
    ci_ids = [v for f in CI_ID_FIELDS if (v := _val(row, f, raw=True)) and v.lower().startswith("/subscriptions/")]
    texts = [_val(row, f) for f in match_fields] + [_val(row, f) for f in ("node", "resource")]
    record = {"kind": kind, "sys_id": _val(row, "sys_id", raw=True), "number": _val(row, "number"),
              "title": _val(row, title_field) or _val(row, "description")[:160],
              "description": _val(row, "description")[:400], "state": _val(row, "state"),
              "group": _val(row, "assignment_group"), "assigned_to": _val(row, "assigned_to"),
              "ci": _val(row, "cmdb_ci"), "ci_ids": ci_ids,
              "ci_url": f"{client.instance}/cmdb_ci.do?sys_id={ci_sys}" if (ci_sys := _val(row, "cmdb_ci", raw=True)) else "",
              "url": f"{client.instance}/{table}.do?sys_id={_val(row, 'sys_id', raw=True)}"}
    record["matches"] = match(texts, ci_ids, index, by_id)
    record["cmdb_note"] = cmdb_note(record, record["matches"])
    return record


def list_url(instance, table, query):
    """The same table and query as a ServiceNow list, opened in the browser as whoever is signed in there."""
    return f"{instance}/{table}_list.do?sysparm_query={quote(query, safe='')}"


def _priority(row):
    raw = _val(row, "priority", raw=True)
    return int(raw) if str(raw).isdigit() else 5


def _fetch_table(client, table, query, fields, limit, optional=False, cap=None, totals=None, key=None, denied=None):
    """(rows, note): the note says why a table was skipped, so the page never shows a silent empty list. An
    `optional` table (one that only adds to another) that isn't ticked is skipped without a note. With `cap`, rows are
    read `limit` a page up to `cap`, and how many matched in all goes into `totals[key]`."""
    if table not in client.tables:
        return [], "" if optional else f"{table} isn't selected in Settings → Integrations → ServiceNow → What to read."
    try:
        if cap:
            rows, totals[key] = client.table_pages(table, query, fields, limit, cap)
            return rows, ""
        return client.table(table, query, fields, limit), ""
    except ServiceNowError as error:
        if error.kind in ("auth", "unreachable"):  # the whole connection, not this table: said once, everywhere
            raise
        if error.kind == "access" and denied is not None:
            denied.add(table)
        hint = (" Untick it under Settings → Integrations → ServiceNow → What to read to stop checking it."
                if error.kind == "missing" else "")
        return [], f"{error}{hint}"


def _related(client, table, parent_field, parent_terms, fields, limit, cap, denied=None):
    """{parent change sys_id: [rows]} of `table` whose parent change matches every one of `parent_terms` (filters on
    the change, dot-walked through `parent_field`), in one paged read; and a note when it couldn't be read."""
    query = "^".join(f"{parent_field}.{term}" for term in parent_terms) + f"^ORDERBY{parent_field}"
    rows, note = _fetch_table(client, table, query, fields, limit, optional=True, cap=cap, totals={}, key=table, denied=denied)
    found = {}
    for row in rows:
        found.setdefault(_val(row, parent_field, raw=True), []).append(row)
    return found, note


def _azure_ids(row, fields):
    return [v for f in fields if (v := _val(row, f, raw=True)) and v.lower().startswith("/subscriptions/")]


def collect(cfg, resources, refresh=False):
    """Everything the ServiceNow tab shows, matched against `resources` (from `inventory`)."""
    client = ServiceNowClient(cfg)
    index, by_id = name_index(resources)
    match_fields = [f for f in cfg["match_fields"] if f]
    change_text = [f for f in cfg["change_match_fields"] if f and f not in match_fields]
    limit = cfg["max_records"]

    fields_extra = tuple(dict.fromkeys(list(CI_ID_FIELDS) + match_fields))
    change_fields = CHANGE_FIELDS + fields_extra + tuple(change_text)
    history = _sn_time(datetime.now(timezone.utc) - timedelta(days=cfg["change_history_days"]))

    related_reads = (("task_ci", "task", TASK_CI_FIELDS),
                     ("change_task", "change_request", CHANGE_TASK_FIELDS + tuple(change_text)))

    def fetch_closed():
        """Changes closed in the span (closed_at: every closed change has one, its planned start may be empty)."""
        totals, denied = {}, set()
        closed_query = f"active=false^closed_at>={history}^ORDERBYDESCclosed_at"
        rows, note = _fetch_table(client, "change_request", closed_query,
                                  change_fields, limit, cap=cfg["change_max_records"], totals=totals, key="closed", denied=denied)
        related = {table: _related(client, table, parent, ["active=false", f"closed_at>={history}"], extra, limit,
                                   cfg["change_max_records"] * RELATED_PER_CHANGE, denied)
                   for table, parent, extra in related_reads}
        return {"rows": rows, "note": note, "total": totals.get("closed"), "related": related, "denied": sorted(denied),
                "query": closed_query}

    def fetch():
        now = datetime.now(timezone.utc)
        out = {"notes": {}, "totals": {}}
        denied = set()
        query = "^".join(q for q in (cfg["incident_filter"], "ORDERBYpriority", "ORDERBYDESCsys_updated_on") if q)
        out["incident"], out["notes"]["incident"] = _fetch_table(
            client, "incident", query, INCIDENT_FIELDS + fields_extra, limit, cap=cfg["incident_max_records"],
            totals=out["totals"], key="incident", denied=denied)
        # Open changes in the span: starting by `ahead` (or undated) and not finished before `history`, newest first
        open_query = (f"active=true^start_date<={_sn_time(now + timedelta(days=cfg['change_ahead_days']))}^ORstart_dateISEMPTY"
                      f"^end_date>={history}^ORend_dateISEMPTY^ORDERBYDESCstart_date")
        out["change_request"], out["notes"]["change_request"] = _fetch_table(
            client, "change_request", open_query, change_fields, limit, cap=cfg["change_max_records"], totals=out["totals"],
            key="change_request", denied=denied)
        closed = cached(key + ("closed",), CLOSED_CACHE_SECONDS, fetch_closed, refresh)
        out["change_closed"], out["notes"]["change_closed"] = closed["rows"], closed["note"]
        out["totals"]["change_closed"] = closed["total"]
        for table, parent, extra in related_reads:  # open changes' now, closed ones' with them (kept 15 minutes)
            now_open, note = _related(client, table, parent, ["active=true"], extra, limit,
                                      cfg["change_max_records"] * RELATED_PER_CHANGE, denied)
            done, closed_note = closed["related"][table]
            out[table] = {**done, **now_open}
            out["notes"][table] = note or closed_note
        alert_query = "state!=Closed^ORDERBYDESCsys_updated_on"
        out["em_alert"], out["notes"]["em_alert"] = _fetch_table(
            client, "em_alert", alert_query, ALERT_FIELDS + CI_ID_FIELDS, limit, denied=denied)
        since = (now - timedelta(hours=EVENT_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        event_query = f"time_of_event>={since}^ORDERBYDESCtime_of_event"
        out["em_event"], out["notes"]["em_event"] = _fetch_table(
            client, "em_event", event_query, EVENT_FIELDS + CI_ID_FIELDS, limit, denied=denied)
        out["queries"] = {"incident": ("incident", query), "change_request": ("change_request", open_query),
                          "change_closed": ("change_request", closed.get("query", "")),
                          "em_alert": ("em_alert", alert_query), "em_event": ("em_event", event_query)}
        out["denied"] = sorted(denied | set(closed["denied"]))
        out["fetched_at"] = now.isoformat()
        return out

    key = ("servicenow", client.host, cfg["auth"], cfg["username"], cfg["incident_filter"], tuple(cfg["tables"]),
           tuple(match_fields), tuple(change_text), cfg["change_history_days"], cfg["change_ahead_days"],
           cfg["change_max_records"], limit)
    caps = {"incident": cfg["incident_max_records"], "change_request": cfg["change_max_records"],
            "change_closed": cfg["change_max_records"]}
    if refresh:
        forget_hidden()
    raw = cached(key, CACHE_SECONDS, fetch, refresh)
    read = [t for t in READ_TABLES if t in client.tables]
    if read and set(raw.get("denied", [])) >= set(read):  # nothing readable: one message for the connection, not each table
        raise ServiceNowError(no_access(client.who, read), "access")
    now = datetime.now(timezone.utc)

    incidents = []
    for row in raw["incident"]:
        record = _record(row, client, "incident", "incident", match_fields, index, by_id)
        record.update(priority=_priority(row), priority_label=_val(row, "priority"),
                      opened_at=_when(row, "opened_at"), updated_at=_when(row, "sys_updated_on"))
        incidents.append(record)

    changes = []
    horizon = now + timedelta(days=cfg["change_ahead_days"])
    affected, tasks_of = raw.get("task_ci", {}), raw.get("change_task", {})
    for row in [*raw["change_request"], *({**r, "_closed": True} for r in raw.get("change_closed", []))]:
        start, end = _when(row, "start_date"), _when(row, "end_date")
        start_at = datetime.fromisoformat(start) if start else None
        end_at = datetime.fromisoformat(end) if end else None
        if row.get("_closed"):
            window = "closed"
        elif start_at and start_at <= now and (not end_at or end_at >= now):
            window = "in progress"
        elif start_at and now < start_at <= horizon:
            window = "scheduled"
        elif start_at and end_at and end_at < now:
            window = "past its window"  # still open after its planned end
        elif not start_at:
            window = "not scheduled"
        else:
            continue  # scheduled further ahead than change_ahead_days
        record = _record(row, client, "change_request", "change", match_fields + change_text, index, by_id)
        sys_id = _val(row, "sys_id", raw=True)
        cis, tasks = affected.get(sys_id, []), tasks_of.get(sys_id, [])
        record["matches"] = merge(
            record["matches"],
            match([_val(c, "ci_item") for c in cis], [i for c in cis for i in _azure_ids(c, TASK_CI_FIELDS[2:])],
                  index, by_id, id_via="affected CI"),
            match([_val(t, f) for t in tasks for f in ("short_description", "description", "cmdb_ci", *change_text)],
                  [i for t in tasks for i in _azure_ids(t, CI_ID_FIELDS)], index, by_id, id_via="change task"))
        record["cmdb_note"] = cmdb_note(record, record["matches"])
        record.update(window=window, start=start, end=end, closed_at=_when(row, "closed_at"), risk=_val(row, "risk"), type=_val(row, "type"),
                      outcome=_val(row, "close_code"), affected_cis=[_val(c, "ci_item") for c in cis][:20],
                      tasks=[{"number": _val(t, "number"), "title": _val(t, "short_description"), "state": _val(t, "state")}
                             for t in tasks][:20])
        changes.append(record)
    live = sorted((c for c in changes if c["window"] != "closed"), key=lambda c: (CHANGE_ORDER[c["window"]], c["start"]))
    changes = live + sorted((c for c in changes if c["window"] == "closed"), key=lambda c: c["closed_at"] or c["start"], reverse=True)

    alerts = [{**_record(row, client, "em_alert", "alert", [], index, by_id, "description"),
               "severity": _val(row, "severity"), "source": _val(row, "source"), "updated_at": _when(row, "last_remote_time")}
              for row in raw["em_alert"]]
    events = [{**_record(row, client, "em_event", "event", [], index, by_id, "description"),
               "severity": _val(row, "severity"), "source": _val(row, "source"), "updated_at": _when(row, "time_of_event")}
              for row in raw["em_event"]]
    return {"instance": client.instance, "fetched_at": raw["fetched_at"], "incidents": incidents, "changes": changes,
            "alerts": alerts, "events": events, "notes": {k: v for k, v in raw["notes"].items() if v},
            "limit": limit, "change_history_days": cfg["change_history_days"], "change_ahead_days": cfg["change_ahead_days"],
            "change_max_records": cfg["change_max_records"],
            "lists": {k: list_url(client.instance, table, q) for k, (table, q) in raw.get("queries", {}).items() if q},
            "totals": raw.get("totals", {}), "who": client.who, "caps": caps,
            # of the rows asked for (all of them, or the first `cap`), how many read rules kept from this account
            "hidden": {k: min(total, caps[k]) - len(raw.get(k, [])) for k, total in raw.get("totals", {}).items()
                       if total and len(raw.get(k, [])) < min(total, caps[k])},
            "truncated": {**{k: len(raw.get(k, [])) >= limit for k in ("em_alert", "em_event")},
                          **{k: (raw.get("totals", {}).get(k) or 0) > caps[k] for k in caps}}}


def for_resource(data, resource_id):
    """The records that match one Azure resource (or, for a resource group, anything in it)."""
    return {key: [r for r in data[key] if hits_resource(r["matches"], resource_id)]
            for key in ("incidents", "changes", "alerts", "events")}
