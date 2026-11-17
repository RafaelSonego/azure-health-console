"""New Relic as a monitoring provider: alerting entities and open issues (NerdGraph), entities by name, and logs
(NRQL on Log, by the entities that match a resource). Queries only; the client's guard refuses anything else."""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .. import alert_check

from .matching import AZURE_ID, cached, match, name_index
from .newrelic import NewRelicClient, NewRelicError
from .provider import (ALERT_HISTORY, ALERTS, ENTITIES, ENTITY_SEARCH_LIMIT, LOG_LIMIT, LOGS, PARTITION, REPORT_LIMIT,
                       SEARCHABLE_NAME, Provider, ProviderError, match_entity)

CACHE_SECONDS = 60
MAX_ENTITIES = 1000
MAX_ISSUES = 500
ISSUE_DAYS = 7
PARTITION_CACHE_SECONDS = 3600
SEVERITY = {"CRITICAL": "critical", "WARNING": "warning", "NOT_ALERTING": "ok"}
PRIORITY = {"CRITICAL": "critical", "HIGH": "warning", "MEDIUM": "warning", "LOW": "info"}
# Shown as the line's level when present; nothing is filtered on them, since log setups name it differently.
LEVEL_ATTRIBUTES = ("level", "log.level", "severity", "loglevel", "status")
RANK = {"exact": 0, "name": 1, "possible": 2}
# Azure diagnostic logs sent by New Relic's Azure log forwarder carry the resource's ID here, often in upper case.
AZURE_ID_ATTRIBUTE = "resourceId"
# Is this alert right?: NRQL's TIMESERIES returns at most 366 buckets, so the days are split over up to MAX_READS reads.
MAX_BUCKETS = 366
MAX_READS = 8
PARALLEL_READS = 4
OPERATORS = {"ABOVE": ">", "ABOVE_OR_EQUALS": ">=", "BELOW": "<", "BELOW_OR_EQUALS": "<=", "EQUALS": "=", "NOT_EQUALS": "!="}
ADDITIVE = ("count", "sum")
_FUNCTION = re.compile(r"\bSELECT\s+([A-Za-z]+)\s*\(\s*(?:([A-Za-z]+)\s*\()?", re.I)
_TIME_KEYS = {"beginTimeSeconds", "endTimeSeconds", "facet"}


def _first(value):
    return (value[0] if value else "") if isinstance(value, list) else (value or "")


def _iso_ms(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def _pretty(entity_type):
    return (entity_type or "").replace("_ENTITY", "").replace("_", " ").title()


def nrql_string(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _by_account(entities, accounts):
    """{account: its entities}, with every configured account present (an empty list) for the name-based parts."""
    out = {}
    for e in entities:
        if e.get("account_id") and e["id"]:
            out.setdefault(int(e["account_id"]), []).append(e)
    for account_id in accounts or []:
        out.setdefault(int(account_id), [])
    return dict(sorted(out.items()))


def _window(window):
    """NRQL for a window: "SINCE 30 minutes ago" to read, or absolute epoch ms with `until` (Load older pages it)."""
    if window.get("until"):
        return f"SINCE {int(window['since'])} UNTIL {int(window['until'])}"
    return f"SINCE {int(window['minutes'])} minutes ago"


class NewRelicProvider(Provider):
    kind = "newrelic"
    label = "New Relic"
    capabilities = (ALERTS, ENTITIES, LOGS, ALERT_HISTORY)
    reports = ("errors", "slow")

    def __init__(self, monitor, client=None):
        super().__init__(monitor)
        self.client = client or NewRelicClient(monitor)
        self.accounts = [int(a) for a in monitor.get("account_ids") or []]

    def describe(self):
        accounts = ", ".join(str(a) for a in self.accounts)
        return f"{(self.monitor.get('region') or 'us').upper()} · account{'s' if len(self.accounts) > 1 else ''} {accounts}"

    def console_url(self):
        return "https://one.eu.newrelic.com" if self.monitor.get("region") == "eu" else "https://one.newrelic.com"

    def _key(self, *extra):
        return ("newrelic", self.monitor.get("tenant"), self.monitor["name"], self.monitor.get("region"),
                tuple(self.accounts)) + extra

    def _entity(self, raw, index, by_id):
        tags = {t.get("key"): t.get("values") or [] for t in raw.get("tags") or [] if t.get("key")}
        ids = [v for values in tags.values() for v in values if isinstance(v, str) and AZURE_ID.fullmatch(v.strip())]
        account = raw.get("account") or {}
        return {"id": raw.get("guid") or "", "name": raw.get("name") or "",
                "type": raw.get("type") or _pretty(raw.get("entityType")),  # specific (e.g. KUBERNETESCLUSTER), not the class
                "type_class": _pretty(raw.get("entityType")), "domain": raw.get("domain") or "",
                "severity": SEVERITY.get(raw.get("alertSeverity"), "unknown"),
                "reporting": raw.get("reporting") is not False, "url": raw.get("permalink") or "",
                "account": account.get("name") or str(account.get("id") or ""), "account_id": account.get("id"),
                "matches": match_entity(raw.get("name") or "", ids, index, by_id)}

    def _issue(self, raw, by_guid, index, by_id):
        guids = raw.get("entityGuids") or []
        names = [n for n in raw.get("entityNames") or [] if n]
        found = {m["id"].lower(): m for g in guids for m in (by_guid.get(g) or {}).get("matches", [])}
        for m in match(names, [], index, by_id):
            found.setdefault(m["id"].lower(), m)
        # A query condition's issue has no entity ("Metric query"): its condition and title name the resource.
        for m in match([_first(raw.get("conditionName")), _first(raw.get("title"))], [], index, by_id):
            if m["confidence"] != "possible":
                found.setdefault(m["id"].lower(), m)
        first = next((by_guid[g] for g in guids if g in by_guid), None)
        return {"id": raw.get("issueId") or "", "title": _first(raw.get("title")),
                "priority": PRIORITY.get(raw.get("priority"), "info"), "priority_label": (raw.get("priority") or "").title(),
                "condition": _first(raw.get("conditionName")), "policy": _first(raw.get("policyName")),
                "entities": names, "entity_ids": [g for g in guids if g], "account_id": raw.get("_account"),
                "activated_at": _iso_ms(raw.get("activatedAt") or raw.get("createdAt")),
                # New Relic's own link for an issue: the condition's query charted around when it fired
                "url": _first(raw.get("deepLinkUrl")) or (first["url"] if first else ""),
                "matches": sorted(found.values(), key=lambda m: (RANK[m["confidence"]], m["name"]))}

    def alerting(self, resources, refresh=False):
        index, by_id = name_index(resources)

        def fetch():
            notes = []
            entities, truncated = self.client.entities("alertSeverity IN ('CRITICAL', 'WARNING')", MAX_ENTITIES)
            issues = []
            for account_id in self.accounts:
                try:
                    found, more = self.client.open_issues(account_id, ISSUE_DAYS, MAX_ISSUES)
                except NewRelicError as error:
                    if error.kind in ("auth", "network"):
                        raise
                    notes.append(f"Open issues in account {account_id} couldn't be read: {error}")
                    continue
                issues += [{**issue, "_account": account_id} for issue in found]
                truncated = truncated or more
            return {"entities": entities, "issues": issues, "notes": notes, "truncated": truncated,
                    "fetched_at": datetime.now(timezone.utc).isoformat()}

        raw = cached(self._key(), CACHE_SECONDS, fetch, refresh)
        entities = [self._entity(e, index, by_id) for e in raw["entities"]]
        by_guid = {e["id"]: e for e in entities}
        return {"fetched_at": raw["fetched_at"], "entities": entities, "notes": raw["notes"], "truncated": raw["truncated"],
                "issues": [self._issue(i, by_guid, index, by_id) for i in raw["issues"]]}

    def named(self, name, resources):
        if not SEARCHABLE_NAME.match(name):
            return []
        index, by_id = name_index(resources)
        raw = cached(self._key("name", name.lower()), CACHE_SECONDS,
                     lambda: self.client.entities(f"name LIKE {nrql_string(name)}", 50)[0])
        return [self._entity(e, index, by_id) for e in raw]

    def search_entities(self, text, limit=ENTITY_SEARCH_LIMIT):
        """Entities in this connection's accounts whose name contains `text` (NerdGraph's LIKE is a substring match)."""
        raw = cached(self._key("search", text.lower()), CACHE_SECONDS,
                     lambda: self.client.entities(f"name LIKE {nrql_string(text)}", limit)[0])
        found = [self._entity(e, {}, {}) for e in raw]
        return [e for e in found if not self.accounts or e["account_id"] in self.accounts]

    def log_partitions(self, account_id):
        """(partitions, note): the account's log event types, Log first then Log_<name> (NRDB data partitions),
        cached for an hour. If they can't be listed, just Log, and the note says so."""
        def fetch():
            rows = self.client.nrql(account_id, "SHOW EVENT TYPES SINCE 1 week ago")
            # One row holding a list ({"eventTypes": [...]}); a row per type is accepted too.
            values = [v for row in rows for value in row.values() for v in (value if isinstance(value, list) else [value])]
            names = {v for v in values if isinstance(v, str) and PARTITION.match(v)}
            return sorted(names, key=lambda n: (n != "Log", n.lower()))
        try:
            found = cached(self._key("partitions", account_id), PARTITION_CACHE_SECONDS, fetch)
        except NewRelicError as error:
            return ["Log"], f"Log partitions in account {account_id} couldn't be listed, so only Log was read: {error}"
        if not found:
            return ["Log"], f"No log event types were found in account {account_id} in the last week, so only Log was read."
        return found, ""

    def log_queries(self, entities, window, text, partitions=None, match=None):
        """{"queries", "notes", "partitions"}: what `logs` sends, per account, built without sending it."""
        queries, notes, available = [], [], set()
        for account_id, group in _by_account(entities, (match or {}).get("accounts")).items():
            found, note = self.log_partitions(account_id)
            notes += [note] if note else []
            available.update(found)
            chosen = [p for p in found if not partitions or p in partitions]
            if not chosen:
                notes.append(f"Account {account_id} has no {', '.join(partitions)} partition.")
                continue
            ors = [f"entity.guid IN ({', '.join(nrql_string(e['id']) for e in group)})"] if group else []
            if match and match.get("attribute"):
                ors.append(f"`{match['attribute']}` = {nrql_string(match['value'])}")
            if match and match.get("resource_ids"):
                ors.append(f"lower(`{AZURE_ID_ATTRIBUTE}`) IN ({', '.join(nrql_string(i.lower()) for i in match['resource_ids'])})")
            if match and match.get("search"):  # any line mentioning the name, in any attribute
                ors.append(f"allColumnSearch({nrql_string(match['search'])}, insensitive: true)")
            if not ors:
                continue
            where = f"({' OR '.join(ors)})" if len(ors) > 1 else ors[0]
            if text:
                where += f" AND message LIKE {nrql_string('%' + text + '%')}"
            queries.append({"account": account_id, "partitions": chosen,
                            "nrql": f"SELECT * FROM {', '.join(chosen)} WHERE {where} {_window(window)} LIMIT {LOG_LIMIT}"})
        return {"queries": queries, "notes": notes, "partitions": sorted(available, key=lambda n: (n != "Log", n.lower()))}

    def logs(self, entities, window, text, partitions=None, match=None):
        plan = self.log_queries(entities, window, text, partitions, match)
        names = {e["id"]: e["name"] for e in entities}
        azure_ids = {i.lower() for i in (match or {}).get("resource_ids") or []}
        lines, truncated = [], False
        for query in plan["queries"]:
            rows = self.client.nrql(query["account"], query["nrql"])
            truncated = truncated or len(rows) >= LOG_LIMIT
            for row in rows:
                level = next((str(row[k]) for k in LEVEL_ATTRIBUTES if row.get(k) not in (None, "")), "")
                attributes = {k: v for k, v in row.items() if k not in ("message", "timestamp") and v not in (None, "")}
                attributed = row.get("entity.guid") in names or str(row.get(AZURE_ID_ATTRIBUTE) or "").lower() in azure_ids or (
                    match and match.get("attribute") and row.get(match["attribute"]) == match.get("value"))
                lines.append({"time": _iso_ms(row.get("timestamp")), "ms": row.get("timestamp"), "level": level,
                              "message": str(row.get("message") or ""),
                              "entity": row.get("entity.name") or names.get(row.get("entity.guid")) or "",
                              "via": "entity" if attributed else "mention", "attributes": attributes})
        lines.sort(key=lambda line: line["time"], reverse=True)
        return {**plan, "lines": lines[:LOG_LIMIT], "truncated": truncated}

    # APM events: an agent labels them with entityGuid and appName. FACET rows come back sorted by the first value.
    REPORT_NRQL = {
        "errors": ("SELECT count(*) AS 'errors', latest(error.message) AS 'last_message', latest(timestamp) AS 'last_seen'"
                   " FROM TransactionError", "error.message", ("appName", "transactionName", "error.class")),
        "slow": ("SELECT percentile(duration, 95) AS 'p95_s', average(duration) AS 'avg_s', max(duration) AS 'max_s',"
                 " count(*) AS 'calls' FROM Transaction", "name", ("appName", "name")),
    }

    def report_ready(self, kind, entities):
        if any((s.get("domain") or "").upper() == "APM" for e in entities for s in e.get("sources") or [e]):
            return ""
        return "None of the matched entities is an APM app, and only APM agents report this."

    def report_queries(self, kind, entities, window, text, match=None):
        """{"queries", "columns"}: what `report` sends, per account, built without sending it."""
        select, text_field, facets = self.REPORT_NRQL[kind]
        columns = list(facets) + [a.split("'")[1] for a in select.split(" AS ")[1:]]
        queries = []
        for account_id, group in _by_account(entities, (match or {}).get("accounts")).items():
            ors = [f"entityGuid IN ({', '.join(nrql_string(e['id']) for e in group)})"] if group else []
            if match and match.get("name"):
                ors.append(f"lower(appName) = {nrql_string(match['name'].lower())}")
            if not ors:
                continue
            where = f"({' OR '.join(ors)})" if len(ors) > 1 else ors[0]
            if text:
                where += f" AND {text_field} LIKE {nrql_string('%' + text + '%')}"
            queries.append({"account": account_id, "nrql": f"{select} WHERE {where} FACET {', '.join(facets)} "
                                                           f"{_window(window)} LIMIT {REPORT_LIMIT}"})
        return {"queries": queries, "columns": columns, "facets": facets}

    def report(self, kind, entities, window, text, match=None):
        plan = self.report_queries(kind, entities, window, text, match)
        rows, truncated = [], False
        for query in plan["queries"]:
            found = self.client.nrql(query["account"], query["nrql"])
            truncated = truncated or len(found) >= REPORT_LIMIT
            rows += [_facet_row(r, plan["facets"], plan["columns"], query["account"]) for r in found]
        return {"columns": plan["columns"] + ["account"], "rows": rows, "queries": plan["queries"], "truncated": truncated,
                "notes": []}

    # ---- Is this alert right? (`alert_check.AlertHistory`)

    def alert_rule(self, ref):
        """`ref` is one open issue's {"account_id", "condition", "policy", "priority"}: its NRQL condition, found by
        name in that account (issues name their condition; New Relic's issue API gives no condition ID)."""
        account, name = ref.get("account_id"), (ref.get("condition") or "").strip()
        if not account or not name:
            raise ProviderError("This issue doesn't name its condition and account, so its rule can't be read.")
        found = [c for c in self.client.nrql_conditions(account, name) if (c.get("name") or "").lower() == name.lower()]
        if not found:
            raise ProviderError(f"No NRQL condition named '{name}' in account {account}: it may be another kind of "
                                "condition (APM, infrastructure), which this check can't read yet.")
        enabled = [c for c in found if c.get("enabled") is not False] or found
        if len(enabled) > 1:
            raise ProviderError(f"{len(enabled)} NRQL conditions are named '{name}' in account {account} (IDs "
                                f"{', '.join(str(c.get('id')) for c in enabled)}), so the one that fired can't be told apart.")
        condition = enabled[0]
        terms = condition.get("terms") or []
        wanted = (ref.get("priority") or "").upper()
        term = next((t for t in terms if (t.get("priority") or "").upper() == wanted), terms[0] if terms else {})
        query = (condition.get("nrql") or {}).get("query") or ""
        found_function = _FUNCTION.search(query)
        function, inner = ((found_function.group(1) or "").lower(), (found_function.group(2) or "").lower()) if found_function else ("", "")
        if function == "filter" and inner:  # filter(max(x), WHERE …) is a max of fewer events
            function, inner = inner, ""
        per_bucket = function == "rate" and bool(inner) and inner not in ADDITIVE
        window = ((condition.get("signal") or {}).get("aggregationWindow") or 60) / 60
        baseline = (condition.get("type") or "").upper() != "STATIC"
        notes = [] if len(terms) <= 1 else [f"The condition has {len(terms)} thresholds; the check uses the "
                                            f"{(term.get('priority') or 'first').lower()} one."]
        if term.get("thresholdOccurrences") == "AT_LEAST_ONCE":
            notes.append("The condition fires when the value crosses at least once in its duration, so even one bucket "
                         "over the threshold can fire it.")
        return {"source": f"{self.label} ({self.monitor['name']})", "name": condition.get("name") or name,
                "id": str(condition.get("id") or ""), "account_id": int(account),
                "query": {"label": "NRQL condition", "text": query},
                "operator": OPERATORS.get((term.get("operator") or "").upper()),
                "threshold": None if baseline else term.get("threshold"),
                "window_minutes": window, "duration_minutes": (term.get("thresholdDuration") or 0) / 60,
                "additive": function in ADDITIVE if function else None, "per_bucket": per_bucket,
                "function": f"rate of {inner}" if function == "rate" and inner else function, "unit": "",
                "links": [link for link in (
                    {"label": "Condition in New Relic", "url": f"{self.console_url()}/redirect/entity/{condition['entityGuid']}"}
                    if condition.get("entityGuid") else None,
                    {"label": "Runbook", "url": condition["runbookUrl"]} if condition.get("runbookUrl") else None) if link],
                "dynamic": baseline,
                "notes": notes + (["Anomaly condition: New Relic sets its threshold from history itself."] if baseline else [])}

    def alert_series(self, rule, days):
        bucket = alert_check.bucket_for(days, rule["window_minutes"], MAX_BUCKETS, reads=MAX_READS)
        size = f"{bucket:g} minutes" if float(bucket).is_integer() else f"{round(bucket * 60)} seconds"
        query = rule["query"]["text"].strip()
        nrqls = [f"{query} SINCE {int(a.timestamp() * 1000)} UNTIL {int(b.timestamp() * 1000)} TIMESERIES {size} LIMIT MAX"
                 for a, b in alert_check.spans(days, bucket, MAX_BUCKETS, datetime.now(timezone.utc))]

        def read(nrql):
            try:
                return self.client.nrql(rule["account_id"], nrql)
            except NewRelicError as error:
                raise NewRelicError(f"{error} The NRQL sent: {nrql}", error.kind) from error
        with ThreadPoolExecutor(max_workers=PARALLEL_READS) as pool:
            rows = [row for part in pool.map(read, nrqls) for row in part]
        pick = min if rule["operator"] in alert_check.BELOW else max
        buckets, facets = {}, set()
        for row in rows:
            begin = row.get("beginTimeSeconds")
            value = _value(row)
            if begin is None:
                continue
            if row.get("facet") is not None:
                facets.add(str(row.get("facet")))
            if value is not None:
                buckets[begin] = pick(buckets[begin], value) if buckets.get(begin) is not None else value
            else:
                buckets.setdefault(begin, None)
        points = [[datetime.fromtimestamp(t, timezone.utc).isoformat(), v] for t, v in sorted(buckets.items())]
        notes = [f"{len(facets)} facets: each bucket takes the one nearest to firing."] if len(facets) > 1 else []
        return {"points": points, "bucket_minutes": bucket, "notes": notes,
                "queries": [{"label": f"NRQL, account {rule['account_id']}" + (f", read {i + 1} of {len(nrqls)}" if len(nrqls) > 1 else ""),
                             "query": nrql} for i, nrql in enumerate(nrqls)]}


def _value(row):
    """The one value in a TIMESERIES row (named after its function, e.g. "count" or "average.duration"; a percentile
    is a map of percentile to value)."""
    for key, value in row.items():
        if key in _TIME_KEYS or isinstance(value, str):
            continue
        if isinstance(value, dict):
            value = next((v for v in value.values() if isinstance(v, (int, float))), None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _number(value):
    """An NRQL value: plain, or nested like percentile's {"95": x}; times as ISO."""
    while isinstance(value, dict) and value:
        value = next(iter(value.values()))
    return value


def _facet_row(row, facets, columns, account_id):
    keys = row.get("facet")
    keys = keys if isinstance(keys, list) else [keys]
    out = {f: (keys[i] if i < len(keys) else row.get(f)) for i, f in enumerate(facets)}
    for column in columns[len(facets):]:
        value = _number(row.get(column))
        out[column] = _iso_ms(value) if column == "last_seen" and isinstance(value, (int, float)) else value
    return {**out, "account": account_id}
