"""The questions every monitoring tool is asked. A provider says which it can answer (`capabilities`) and returns
the same shapes whichever tool it is; the page never needs to know which tool answered."""
import re

from ..alert_check import AlertHistory
from ..guarded import ServiceError
from .matching import match

ALERTS, ENTITIES, LOGS, ALERT_HISTORY = "alerts", "entities", "logs", "alert history"
CAPABILITY_LABELS = {ALERTS: "alerts", ENTITIES: "entities", LOGS: "logs", ALERT_HISTORY: "alert history"}
LOG_WINDOWS = (15, 30, 60, 360)  # minutes
LOG_LIMIT = 100
# Questions about one resource besides its logs; a provider lists the ones it answers in `reports`.
REPORTS = {"errors": "Errors", "slow": "Slow transactions"}
REPORT_LIMIT = 50
# A log store a tool keeps apart (New Relic: NRDB data partitions, event types Log and Log_<name>).
PARTITION = re.compile(r"^Log(_[A-Za-z0-9_]+)?$")
SEVERITY_RANK = {"critical": 0, "warning": 1, "ok": 2, "unknown": 3}
# Azure resource names allow letters, digits and - _ . ( ); anything else isn't worth a search.
SEARCHABLE_NAME = re.compile(r"^[A-Za-z0-9._()-]{2,}$")
ENTITY_SEARCH_LIMIT = 30


class ProviderError(ServiceError):
    """A monitoring tool couldn't answer; every provider's own error extends this."""


class Provider(AlertHistory):
    """One connection to one tool. Shapes returned:
    entity: id, name, type, severity (critical|warning|ok|unknown), reporting, url, account, account_id, matches
    issue:  id, title, priority (critical|warning|info), priority_label, condition, policy, entities, activated_at, url, matches
    log line: time, level, entity, message, attributes"""

    kind = ""
    label = ""
    capabilities = ()
    reports = ()

    def __init__(self, monitor):
        self.monitor = monitor

    def describe(self):
        """Which part of the tool this connection reads, from its settings (e.g. region and accounts)."""
        return ""

    def console_url(self):
        """The tool's own web console for this connection, where a query shown on the page can be pasted."""
        return ""

    def alerting(self, resources, refresh=False):
        """{"entities", "issues", "notes", "truncated", "fetched_at"}, matched to `resources`."""
        raise NotImplementedError

    def named(self, name, resources):
        """Entities named like one resource, in any state."""
        return []

    def search_entities(self, text, limit=ENTITY_SEARCH_LIMIT):
        """Entities whose name contains `text` (an app or entity name typed on the page), in any state, unmatched."""
        return []

    # Each question has a build step (`*_queries`: what would be sent, sent nowhere, so the page can show it before Run)
    # and the run itself, which sends exactly those. `window` is {"minutes", "since", "until"} (monitors.window_of).
    def log_partitions(self, account_id):
        """(the account's log stores, a note when they couldn't be listed)."""
        return ["Log"], ""

    def log_queries(self, entities, window, text, partitions=None, match=None):
        """{"queries": [{"account", "nrql", "partitions"}], "notes", "partitions"} for `logs`."""
        raise NotImplementedError

    def logs(self, entities, window, text, partitions=None, match=None):
        """`log_queries` plus {"lines", "truncated"}: recent lines from these entities, newest first, from every log
        partition, or only `partitions` when given. `match` ({"attribute", "value", "accounts"}) also takes lines whose
        attribute equals the value, for logs labelled by name rather than by entity; with {"search", "accounts"}, any
        line mentioning that text in any attribute (no setup needed)."""
        raise NotImplementedError

    def report_queries(self, kind, entities, window, text, match=None):
        """{"queries": [{"account", "nrql"}], "columns"} for `report`."""
        raise NotImplementedError

    def report(self, kind, entities, window, text, match=None):
        """{"columns", "rows", "queries", "truncated", "notes"} for one of `reports` (see REPORTS), over these entities
        or, with {"name", "accounts"} in `match`, anything the tool labels with that name; `text` narrows it."""
        raise NotImplementedError

    def report_ready(self, kind, entities):
        """Why a report can't come from these entities ("" when it can), shown before Run."""
        return ""

    # Is this alert right? (`alert_check`): a tool that can give an alert's rule and its history lists ALERT_HISTORY.
    def alert_rule(self, ref):
        raise ProviderError(f"{self.label or 'This tool'} can't give an alert's rule here yet, so its alerts can't be checked.")

    def alert_series(self, rule, days):
        raise ProviderError(f"{self.label or 'This tool'} can't give an alert's history here yet.")


def whole_name(name, matches):
    """An entity named exactly like a resource is a name match even when the name is short: it's the tool's own label."""
    return [{**m, "confidence": "name"} if m["confidence"] == "possible" and m["name"].lower() == name.lower() else m
            for m in matches]


def match_entity(name, ids, index, by_id):
    return whole_name(name, match([name], ids, index, by_id, id_via="tag"))
