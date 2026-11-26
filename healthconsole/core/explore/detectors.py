"""Azure's own "Diagnose and solve problems" detectors, where a resource type offers them
(App Service, AKS, Redis, Logic Apps at the time of writing). Read-only GETs."""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .. import ids
from ..arm import AccessDenied, ArmError, ArmTimeout
from ..cache import TtlCache
from ..model import CRITICAL, HEALTHY, INFO, UNKNOWN, WARNING, red_or_yellow

logger = logging.getLogger(__name__)

MAX_DETECTORS = 40
MAX_TABLE_ROWS = 60
DETAIL_TYPES = {7, 26}       # insights: one finding per Message/Title, with detail sections
TABLE_TYPES = {1, 2, 8}      # data tables and time series
MARKDOWN_TYPES = {9}         # free-standing markdown blocks
LIST_TIMEOUT_SECONDS = 10
NO_DETECTORS_TTL_SECONDS = 3600
UNAVAILABLE_TTL_SECONDS = 900
NOT_OFFERED = (400, 404, 405)  # no detectors route for the type
# Resource type -> why its detectors couldn't be listed (None: not offered); kept per entry for its own lifetime.
_without_detectors = TtlCache("types without detectors", NO_DETECTORS_TTL_SECONDS)
_NOT_OFFERED = "not offered"
MAX_PARALLEL = 8
INSIGHT_TYPES = {7, 26}  # rendering types that carry Status/Message rows
_STATUS_IDS = {0: CRITICAL, 1: WARNING, 2: INFO, 3: HEALTHY}
_STATUS_NAMES = {"critical": CRITICAL, "warning": WARNING, "info": INFO, "success": HEALTHY}

# Chosen by category, never by detector name, so it holds for every provider.
_HEALTH_WORDS = ("availability", "performance", "health", "risk")
_SKIP_WORDS = ("demo", "sample", "workflow", "analysis", "uncategorized", "zworkflow")


def _wanted(category, words=_HEALTH_WORDS):
    lowered = (category or "").lower()
    if any(word in lowered for word in _SKIP_WORDS):
        return False
    return any(word in lowered for word in words) or not lowered


def _not_offered(error):
    """No route, or a route that never works: Storage, Event Hubs, Service Bus and Container Registry answer 503 after
    ~41 s on every API version (checked 2026-11-18), so a timeout or 5xx counts as none, not as "try later"."""
    return isinstance(error, ArmTimeout) or error.status in NOT_OFFERED or (error.status or 0) >= 500


def _why(error):
    if error.status == 401:
        return "it refused the request (401) for this resource type, although your sign-in works elsewhere"
    text = str(error).split(": ", 1)[-1].strip()
    return f"it answered {error.status}" + (f": {text[:200]}" if text else "")


def available(client, resource_id):
    """(detectors, api version, access error or None, why they couldn't be listed or None). Checked live across 123
    types (2026-11-09): App Service, AKS, Redis, Logic Apps and Batch list them."""
    version = client.api_version(resource_id)
    if not version:
        return [], None, None, None
    resource_type = ids.resource_type_of(resource_id)
    known = _without_detectors.get(resource_type)
    if known is not None:
        return [], version, None, None if known == _NOT_OFFERED else known
    why = None
    try:
        payload = client.get(f"{resource_id}/detectors", version, timeout=LIST_TIMEOUT_SECONDS)
    except AccessDenied as error:
        return [], version, str(error), None
    except ArmError as error:
        payload, why = None, None if _not_offered(error) else _why(error)
    if not (payload or {}).get("value"):
        # Remembered, since some providers (Storage) take ~40 s to fail; a 401 or throttling is retried sooner.
        _without_detectors.put(resource_type, why or _NOT_OFFERED,
                               NO_DETECTORS_TTL_SECONDS if why is None else UNAVAILABLE_TTL_SECONDS)
        return [], version, None, why
    listed = [{
        "name": item.get("name"),
        "title": ((item.get("properties") or {}).get("metadata") or {}).get("name") or item.get("name"),
        "category": ((item.get("properties") or {}).get("metadata") or {}).get("category") or "",
    } for item in payload.get("value", [])]
    return listed, version, None, None


def _rows(table):
    tab = table.get("table") or {}
    columns = [c.get("columnName") for c in tab.get("columns") or []]
    return columns, tab.get("rows") or []


def _insights(dataset):
    """Findings with the detail sections the portal shows under "View details". Azure's text is
    Markdown with some HTML (tables); the page renders it safely, never as raw HTML."""
    found = {}
    for table in dataset or []:
        rendering = (table.get("renderingProperties") or {}).get("type")
        if rendering not in DETAIL_TYPES:
            continue
        columns, rows = _rows(table)
        for row in rows:
            record = dict(zip(columns, row))
            text = str(record.get("Message") or record.get("Title") or "")
            if not text:
                continue
            insight = found.setdefault(text, {
                "status": _STATUS_NAMES.get(str(record.get("Status", "")).lower(), INFO),
                "text": text[:300], "details": []})
            if record.get("Data.Name") and record.get("Data.Value"):
                insight["details"].append({"name": str(record["Data.Name"]), "value": str(record["Data.Value"])})
            for key in ("Description", "Solution"):
                if record.get(key):
                    insight["details"].append({"name": key, "value": str(record[key])})
    return list(found.values())


def _extras(dataset):
    """Tables, time series and markdown blocks that sit beside the findings."""
    tables, notes = [], []
    for table in dataset or []:
        props = table.get("renderingProperties") or {}
        rendering = props.get("type")
        if rendering in TABLE_TYPES:
            columns, rows = _rows(table)
            if rows:
                tables.append({"title": props.get("title") or "Data", "columns": columns,
                               "rows": [[None if v is None else str(v) for v in r] for r in rows[:MAX_TABLE_ROWS]],
                               "total": len(rows)})
        elif rendering in MARKDOWN_TYPES:
            _, rows = _rows(table)
            text = "\n\n".join(str(r[0]) for r in rows if r and r[0])
            if text:
                notes.append({"title": props.get("title") or "", "value": text})
    return tables, notes


def plan(client, resource_id, only_health=True, words=_HEALTH_WORDS):
    """The detectors `run` would run, without running them (one list read): the page runs each one itself and shows it
    as it answers, since one detector can take 25 s."""
    listed, version, denied, unavailable = available(client, resource_id)
    if denied:
        return {"supported": True, "detectors": [], "error": denied}
    if not listed:
        return {"supported": False, "detectors": [], **({"unavailable": unavailable} if unavailable else {})}
    chosen = [d for d in listed if _wanted(d["category"], words)] if only_health else listed
    return {"supported": True, "detectors": chosen[:MAX_DETECTORS], "total": len(listed),
            "skipped": max(0, len(chosen) - MAX_DETECTORS)}


def run_one(client, resource_id, name, hours=24):
    """One detector's result, by the name `plan` listed."""
    listed, version, denied, _ = available(client, resource_id)
    detector = next((d for d in listed if d["name"] == name), None)
    if denied or detector is None:
        return {"name": name, "title": name, "category": "", "status": UNKNOWN, "insights": [],
                "error": denied or "Azure doesn't list this detector for the resource."}
    return _run_detector(client, resource_id, version, detector, _window(hours))


def _window(hours):
    end = datetime.now(timezone.utc).replace(microsecond=0)
    return {"startTime": (end - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
            "endTime": end.isoformat().replace("+00:00", "Z")}


def run(client, resource_id, hours=24, only_health=True, words=_HEALTH_WORDS):
    planned = plan(client, resource_id, only_health, words)
    if not planned["detectors"]:
        return {**{k: v for k, v in planned.items() if k != "detectors"}, "results": []}
    version = client.api_version(resource_id)
    params = _window(hours)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        results = list(pool.map(lambda d: _run_detector(client, resource_id, version, d, params), planned["detectors"]))
    order = {CRITICAL: 0, WARNING: 1, INFO: 2, HEALTHY: 3, UNKNOWN: 4}
    results.sort(key=lambda r: (order.get(r["status"], 5), r["category"], r["title"] or ""))
    return {"supported": True, "results": results, "total": planned["total"], "ran": len(results),
            "skipped": planned["skipped"]}


def _run_detector(client, resource_id, version, detector, params):
    """One detector, read and judged."""
    try:
        payload = client.get(f"{resource_id}/detectors/{detector['name']}", version,
                             params=params, tolerate=(400, 404, 500, 503)) or {}
    except ArmError as error:
        return {**detector, "status": UNKNOWN, "insights": [], "error": str(error)[:160]}
    props = payload.get("properties") or {}
    status_id = (props.get("status") or {}).get("statusId")
    insights = _insights(props.get("dataset"))
    tables, notes = _extras(props.get("dataset"))
    status = _STATUS_IDS.get(status_id)
    if status is None:
        # statusId 4 ("none") means the detector only lists sub-results; judge by its insights.
        ranked = [i["status"] for i in insights]
        status = red_or_yellow(ranked, HEALTHY if HEALTHY in ranked else INFO)
    return {**detector, "status": status, "insights": insights, "tables": tables, "notes": notes,
            "description": ((props.get("metadata") or {}).get("description") or "")[:300]}
