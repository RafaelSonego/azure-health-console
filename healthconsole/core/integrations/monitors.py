"""Every turned-on monitoring connection, behind one interface (`provider.py`). Alerts from all of them are shown
together, each row naming its connection; logs are read from one connection at a time, on request."""
import re
import time
from datetime import datetime, timezone

from . import failed, monitor_status, unavailable
from .. import ids
from .matching import AZURE_ID, STRONG_NAME, hits_resource
from .nr_data import NewRelicProvider
from .provider import (CAPABILITY_LABELS, ENTITIES, LOG_WINDOWS, LOGS, PARTITION, REPORTS, SEARCHABLE_NAME,
                       SEVERITY_RANK, ProviderError)

PROVIDERS = {"newrelic": NewRelicProvider}
# What tools without a provider yet will answer, so Settings can say so before they connect.
PLANNED = {"datadog": ("Datadog", ("alerts", "entities", "logs")), "grafana": ("Grafana", ("alerts", "logs"))}


def label(kind):
    return PROVIDERS[kind].label if kind in PROVIDERS else PLANNED.get(kind, (kind,))[0]


def capabilities():
    """{kind: {"connected", "capabilities"}} for every tool Settings offers."""
    out = {kind: {"connected": True, "capabilities": [CAPABILITY_LABELS[c] for c in p.capabilities]}
           for kind, p in PROVIDERS.items()}
    out.update({kind: {"connected": False, "capabilities": list(caps)} for kind, (_, caps) in PLANNED.items()})
    return out


def _head(monitor, provider=None):
    return {"name": monitor["name"], "kind": monitor["type"], "label": label(monitor["type"]),
            "description": provider.describe() if provider else "", "console": provider.console_url() if provider else "",
            "capabilities": list(provider.capabilities) if provider else [],
            "reports": [{"key": k, "label": REPORTS[k]} for k in (provider.reports if provider else ())]}


def _unavailable(monitor):
    return unavailable(monitor["name"], monitor_status(monitor)["problems"])


def provider_for(monitor):
    cls = PROVIDERS.get(monitor["type"])
    return cls(monitor) if cls else None


def _sorted(entities):
    return sorted(entities, key=lambda e: (SEVERITY_RANK.get(e["severity"], 3), e["name"].lower()))


MATCH_RANK = {"exact": 0, "name": 1, "possible": 2}
SOURCE_FIELDS = ("id", "type", "type_class", "domain", "severity", "url", "reporting")


def group_entities(entities):
    """Entities with the same name in the same account as one (e.g. a cluster seen by the Azure integration and by
    the Kubernetes one), each kept as a source with its own type, state and link; the worst state stands for it."""
    groups = {}
    for entity in entities:
        groups.setdefault((entity["name"].lower(), entity.get("account_id")), []).append(entity)
    out = []
    for members in groups.values():
        members = _sorted(members)
        best = {}
        for match in (m for e in members for m in e["matches"]):
            key = match["id"].lower()
            if key not in best or MATCH_RANK[match["confidence"]] < MATCH_RANK[best[key]["confidence"]]:
                best[key] = match
        out.append({**members[0], "ids": [e["id"] for e in members], "reporting": any(e["reporting"] for e in members),
                    "type": " · ".join(dict.fromkeys(e["type"] for e in members if e["type"])),
                    "matches": sorted(best.values(), key=lambda m: (MATCH_RANK[m["confidence"]], m["name"])),
                    "sources": [{k: e.get(k) for k in SOURCE_FIELDS} for e in members]})
    return _sorted(out)


def connection(monitor, resources, refresh=False):
    """One connection's alerting entities (grouped) and open issues, matched to `resources`; or why it can't be read."""
    view = _alerting(monitor, resources, refresh)
    return {**view, "entities": group_entities(view["entities"])} if view["available"] else view


def _alerting(monitor, resources, refresh=False):
    blocked = _unavailable(monitor)
    if blocked:
        return {**_head(monitor), **blocked}
    provider = provider_for(monitor)
    head = _head(monitor, provider)
    try:
        data = provider.alerting(resources, refresh)
    except ProviderError as error:
        return {**head, **failed(error)}
    return {**head, "available": True, **data, "entities": _sorted(data["entities"])}


def for_resource(monitor, resource_id, name, resources):
    """What one connection says about one resource: entities that match it (alerting or not) and open issues
    touching it. For a resource group, anything in it."""
    view = _alerting(monitor, resources)
    if not view["available"]:
        return view
    alerting = [e for e in view["entities"] if hits_resource(e["matches"], resource_id)]
    named, notes = [], list(view["notes"])
    if ENTITIES in view["capabilities"]:
        try:
            named = provider_for(monitor).named(name, resources)
        except ProviderError as error:
            notes.append(f"Entities named like {name} couldn't be searched: {error}")
    seen = {e["id"] for e in alerting}
    named = [e for e in named if e["id"] not in seen and hits_resource(e["matches"], resource_id)]
    issues = [i for i in view["issues"] if hits_resource(i["matches"], resource_id)]
    return {**view, "notes": notes, "entities": group_entities(alerting + named), "issues": issues}


def _check_partitions(partitions):
    if not all(PARTITION.match(p) for p in partitions or []):
        raise ValueError("partition must be Log or Log_<name>")
    return list(partitions or [])


def _sources(entities):
    return [{"id": s["id"], "name": e["name"], "account_id": e.get("account_id")} for e in entities for s in e["sources"]]


def log_attribute(monitor, resource_id):
    """(resource type, the log attribute this connection matches its name on, or "")."""
    resource_type = ids.resource_type_of(resource_id)
    return resource_type, (monitor.get("log_attributes") or {}).get(resource_type, "")


def searchable(name):
    """A name distinctive enough to search every log line for (6+ characters with a dash, dot, underscore or digit);
    a short or plain one, like "web", would match half the logs."""
    return bool(name) and bool(SEARCHABLE_NAME.match(name)) and bool(STRONG_NAME.match(name.lower()))


def _name_note(name):
    return "" if searchable(name) else f"'{name}' is too short, common or spaced to search every log line for, so only entity-matched lines were read."


def window_of(minutes, until=None, since=None):
    """{"minutes", "since", "until"}: the last `minutes` (`since` in epoch ms, for Load older to keep), or (Load older)
    that window's `since` up to `until`, the oldest line shown."""
    if minutes not in LOG_WINDOWS:
        raise ValueError(f"minutes must be one of {list(LOG_WINDOWS)}")
    now = int(time.time() * 1000)
    if until is None:
        return {"minutes": minutes, "since": now - minutes * 60_000}
    since, until = int(since), int(until)
    if not 0 < since < until <= now or until - since > minutes * 60_000:
        raise ValueError("since and until must be a window of at most `minutes`, ending before now")
    return {"minutes": minutes, "since": since, "until": until}


def _resolved(monitor, resource_id, name, resources, mentions=True, by_id=True):
    """(view, entities, log match, report match): what one resource is in this connection, before any query.
    entities is None when the connection can't be read (the view says why)."""
    view = for_resource(monitor, resource_id, name, resources)
    if not view["available"]:
        return view, None, None, None
    accounts = monitor.get("account_ids") or []
    log_match = {"accounts": accounts, "search": name if mentions and searchable(name) else "",
                 "resource_ids": [resource_id.lower()] if by_id else []}
    _, attribute = log_attribute(monitor, resource_id)
    if attribute:
        log_match.update(attribute=attribute, value=name)
    report_match = {"accounts": accounts, "name": name if SEARCHABLE_NAME.match(name or "") else ""}
    return view, [e for e in view["entities"] if e["matches"]], log_match, report_match


def _view_head(view):
    return {k: view.get(k, "") for k in ("name", "kind", "label", "description", "console")}


def _blocked(view):
    return {**_view_head(view), **{k: view[k] for k in ("available", "reason", "problems", "fix")}}


def _no_logs(view):
    return {**_view_head(view), "available": False, "reason": f"{view['label']} doesn't provide logs here.", "problems": [],
            "fix": ""}


def _nothing_note(view, name, entities, match):
    if entities or match.get("search") or match.get("attribute") or match.get("resource_ids"):
        return ""
    return (f"No {view['label']} entity matches this resource, and '{name}' is too short, common or spaced to search every log line for, "
            "so there's nothing to read.")


def _id_fields(azure_ids, match):
    """The page's Azure ID line: the IDs it can match on, and whether this read does."""
    return {"azure_ids": list(azure_ids), "by_id": bool((match or {}).get("resource_ids"))}


def plan(monitor, resource_id, name, resources, minutes=30, mentions=True, by_id=True):
    """What the Query tab will read for one resource, before Run: the entities it matched and how, the name search,
    the saved attribute, the log partitions, which reports can answer, and the exact queries (nothing is run)."""
    view, entities, log_match, report_match = _resolved(monitor, resource_id, name, resources, mentions, by_id)
    if entities is None:
        return _blocked(view)
    provider, window = provider_for(monitor), window_of(minutes)
    out = {**_view_head(view), "available": True, "minutes": minutes, "notes": view["notes"],
           "entities": [{"name": e["name"], "type": e["type"], "account": e.get("account", ""), "url": e.get("url", ""),
                         "how": e["matches"][0]["confidence"],
                         "apm": any((x.get("domain") or "").upper() == "APM" for x in e["sources"])} for e in entities],
           "attribute": log_match.get("attribute", ""), "search": name if searchable(name) else "",
           "search_note": "" if searchable(name) else _name_note(name), "mentions": bool(log_match["search"]),
           **_id_fields([resource_id], log_match)}
    if LOGS in view["capabilities"]:
        try:
            out["logs"] = provider.log_queries(_sources(entities), window, "", None, log_match)
        except ProviderError as error:
            return {**_view_head(view), **failed(error)}
        out["logs"]["note"] = _nothing_note(view, name, entities, log_match)
    out["reports"] = []
    for key in view["reports"]:
        why = provider.report_ready(key["key"], entities)
        runnable = not why or bool(report_match["name"])
        note = f"{why} It still looks for apps named '{name}'." if why and runnable else why
        queries = provider.report_queries(key["key"], _sources(entities), window, "", report_match)["queries"] if runnable else []
        out["reports"].append({**key, "runnable": runnable and bool(queries), "note": note, "queries": queries})
    return out


def logs(monitor, resource_id, name, resources, minutes, text="", partitions=None, mentions=True, until=None, since=None,
         by_id=True):
    """Recent log lines for one resource from one connection: lines from the entities that match it, an attribute saved
    for its type, (with `by_id`) lines carrying its Azure ID, and (with `mentions`, when the name is distinctive) any
    line mentioning its name."""
    view, entities, match, _ = _resolved(monitor, resource_id, name, resources, mentions, by_id)
    if entities is None:
        return _blocked(view)
    if LOGS not in view["capabilities"]:
        return _no_logs(view)
    partitions, window = _check_partitions(partitions), window_of(minutes, until, since)
    try:
        data = provider_for(monitor).logs(_sources(entities), window, text.strip()[:200], partitions, match)
    except ProviderError as error:
        return {**_view_head(view), **failed(error)}
    name_note = _name_note(name) if entities and mentions else ""
    return {**_view_head(view), "available": True, "minutes": minutes, "window": window,
            "entities": [e["name"] for e in entities], "attribute": match.get("attribute", ""), "search": match["search"],
            **_id_fields([resource_id], match), "fetched_at": datetime.now(timezone.utc).isoformat(), **data,
            "note": _nothing_note(view, name, entities, match), "notes": data.get("notes", []) + ([name_note] if name_note else [])}


ENTITY_ID = re.compile(r"^[A-Za-z0-9+/=_-]{8,200}$")
MAX_LOG_ENTITIES = 20


def _azure_ids(resource_ids):
    """The Azure resources a picked row matched, checked to be resource IDs (at most MAX_LOG_ENTITIES)."""
    found = list(dict.fromkeys(i.lower() for i in resource_ids or []))
    if len(found) > MAX_LOG_ENTITIES or not all(AZURE_ID.fullmatch(i) for i in found):
        raise ValueError(f"resource must be up to {MAX_LOG_ENTITIES} Azure resource IDs")
    return found


def _entity_match(search, azure_ids, by_id):
    match = {"search": search, "resource_ids": azure_ids if by_id else []}
    return match if match["search"] or match["resource_ids"] else None


def _picked(monitor, entity_ids, account_id, name="", mentions=True):
    """(provider, head, entities, name search): entities picked on the page, checked to be IDs in one of the connection's
    own accounts; or (None, head with why, ...) when the tool can't answer."""
    provider = provider_for(monitor)
    head = _head(monitor, provider)
    if account_id not in [int(a) for a in monitor.get("account_ids") or []]:
        raise ValueError(f"account {account_id} isn't one of {monitor['name']}'s accounts")
    ids = list(dict.fromkeys(entity_ids))
    if not ids or len(ids) > MAX_LOG_ENTITIES or not all(ENTITY_ID.match(i) for i in ids):
        raise ValueError(f"entity must be 1 to {MAX_LOG_ENTITIES} entity IDs")
    entities = [{"id": i, "name": name, "account_id": account_id} for i in ids]
    return provider, head, entities, (name if mentions and searchable(name) else "")


def _no_answer(head, what="logs"):
    return {**head, "available": False, "reason": f"{head['label']} doesn't provide {what} here.", "problems": [], "fix": ""}


def entity_logs(monitor, entity_ids, account_id, minutes, text="", partitions=None, name="", mentions=True, until=None,
                since=None, resource_ids=(), by_id=True):
    """Recent log lines for entities picked on the tool's tab (an alerting entity, an open issue's entities, or one
    found by name), in one of the connection's own accounts; with `name` and `mentions`, also lines mentioning it, and
    with `by_id`, lines carrying the Azure IDs of the resources the row matched."""
    provider, head, entities, search = _picked(monitor, entity_ids, account_id, name, mentions)
    azure_ids = _azure_ids(resource_ids)
    if not provider or LOGS not in provider.capabilities:
        return _no_answer(head)
    partitions, window = _check_partitions(partitions), window_of(minutes, until, since)
    match = _entity_match(search, azure_ids, by_id)
    try:
        data = provider.logs(entities, window, text.strip()[:200], partitions, match)
    except ProviderError as error:
        return {**head, **failed(error)}
    note = _name_note(name) if name and mentions else ""
    return {**head, "available": True, "minutes": minutes, "window": window, "search": search, **_id_fields(azure_ids, match),
            "fetched_at": datetime.now(timezone.utc).isoformat(), **data,
            "entities": sorted({line["entity"] for line in data["lines"] if line["entity"]}),
            "notes": data.get("notes", []) + ([note] if note else [])}


def find_entities(monitor, text):
    """The connection's entities whose name contains `text`, grouped like the Alerts list, for the Query tab."""
    provider = provider_for(monitor)
    text = (text or "").strip()
    if not provider or ENTITIES not in provider.capabilities or not 2 <= len(text) <= 100:
        return []
    try:
        found = group_entities([{**e, "matches": []} for e in provider.search_entities(text)])
    except ProviderError:
        return []
    return [{"name": e["name"], "type": e["type"], "account": e.get("account", ""), "account_id": e.get("account_id"),
             "url": e.get("url", ""), "severity": e["severity"], "ids": e["ids"],
             "apm": any((s.get("domain") or "").upper() == "APM" for s in e["sources"])} for e in found]


def _entity_report_match(account_id, name):
    return {"accounts": [account_id], "name": name if SEARCHABLE_NAME.match(name or "") else ""}


def entity_plan(monitor, entity_ids, account_id, name, apm, minutes=30, mentions=True, resource_ids=(), by_id=True):
    """The Query tab's plan for an entity picked by name: the same shape as `plan`, nothing run."""
    provider, head, entities, search = _picked(monitor, entity_ids, account_id, name, mentions)
    azure_ids = _azure_ids(resource_ids)
    match = _entity_match(search, azure_ids, by_id)
    if not provider:
        return _no_answer(head, "queries")
    window = window_of(minutes)
    out = {**_view_head(head), "available": True, "minutes": minutes, "notes": [], "attribute": "",
           "entities": [{"name": name, "type": "", "account": str(account_id), "url": "", "how": "picked", "apm": apm}],
           "search": name if searchable(name) else "", "search_note": "" if searchable(name) else _name_note(name),
           "mentions": bool(search), **_id_fields(azure_ids, match)}
    try:
        if LOGS in provider.capabilities:
            out["logs"] = provider.log_queries(entities, window, "", None, match)
            out["logs"]["note"] = ""
        out["reports"] = []
        for key in head["reports"]:
            why = "" if apm else "This entity isn't an APM app, and only APM agents report this."
            runnable = not why or bool(_entity_report_match(account_id, name)["name"])
            queries = provider.report_queries(key["key"], entities, window, "", _entity_report_match(account_id, name))["queries"]
            out["reports"].append({**key, "runnable": runnable and bool(queries), "queries": queries,
                                   "note": f"{why} It still looks for apps named '{name}'." if why and runnable else why})
    except ProviderError as error:
        return {**_view_head(head), **failed(error)}
    return out


def entity_report(monitor, entity_ids, account_id, minutes, kind, text="", name=""):
    """Errors or slow transactions for an entity picked by name, or apps labelled with its name."""
    provider, head, entities, _ = _picked(monitor, entity_ids, account_id, name)
    if not provider or kind not in [r["key"] for r in head["reports"]]:
        return {**_view_head(head), "available": False, "reason": f"{head['label']} doesn't answer '{kind}' here.",
                "problems": [], "fix": ""}
    window = window_of(minutes)
    try:
        data = provider.report(kind, entities, window, text.strip()[:200], _entity_report_match(account_id, name))
    except ProviderError as error:
        return {**_view_head(head), **failed(error)}
    return {**_view_head(head), "available": True, "report": kind, "report_label": REPORTS[kind], "minutes": minutes,
            "window": window, "entities": [name] if name else [], "fetched_at": datetime.now(timezone.utc).isoformat(), **data}


def report(monitor, resource_id, name, resources, minutes, kind, text=""):
    """Errors or slow transactions for one resource: from the entities that match it, or labelled with its name."""
    view, entities, _, match = _resolved(monitor, resource_id, name, resources)
    if entities is None:
        return _blocked(view)
    if kind not in [r["key"] for r in view["reports"]]:
        return {**_view_head(view), "available": False, "reason": f"{view['label']} doesn't answer '{kind}' here.",
                "problems": [], "fix": ""}
    window = window_of(minutes)
    try:
        data = provider_for(monitor).report(kind, _sources(entities), window, text.strip()[:200], match)
    except ProviderError as error:
        return {**_view_head(view), **failed(error)}
    return {**_view_head(view), "available": True, "report": kind, "report_label": REPORTS[kind], "minutes": minutes,
            "window": window, "entities": [e["name"] for e in entities], "fetched_at": datetime.now(timezone.utc).isoformat(),
            **data}
