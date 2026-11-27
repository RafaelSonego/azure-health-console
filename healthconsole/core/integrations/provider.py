"""The questions every monitoring tool is asked. A provider says which it can answer (`capabilities`) and returns
the same shapes whichever tool it is; the page never needs to know which tool answered."""
import re

from ..alert_check import AlertHistory
from ..guarded import ServiceError
from .matching import match

ALERTS, ENTITIES, LOGS, ALERT_HISTORY, OOM_KILLS = "alerts", "entities", "logs", "alert history", "oom kills"
WORKLOADS = "kubernetes workloads"
REQUESTS = "app requests"
WORKLOAD_USAGE, RUNTIME_MEMORY = "workload usage", "runtime memory"
CLUSTER_WEEK = "cluster week"
WARNING_EVENTS = "warning events"
CAPABILITY_LABELS = {ALERTS: "alerts", ENTITIES: "entities", LOGS: "logs", ALERT_HISTORY: "alert history",
                     OOM_KILLS: "Kubernetes OOM kills", WORKLOADS: "Kubernetes workloads by namespace",
                     REQUESTS: "app names, to match a workload to its app",
                     WORKLOAD_USAGE: "a workload's CPU and memory over 30 days, and its pods' memory hour by hour",
                     CLUSTER_WEEK: "a cluster's restarts, OOM kills, memory and HPA time at max over 7 days",
                     RUNTIME_MEMORY: "an app's heap and process memory",
                     WARNING_EVENTS: "Kubernetes warning events"}
LOG_WINDOWS = (15, 30, 60, 360, 1440, 10080)  # minutes, up to 7 days; the tool's own retention still applies
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

    # A Kubernetes cluster's OOM kills, for clusters without Container Insights (OOM_KILLS in `capabilities`).
    def oom_kills(self, cluster_name, hours):
        """{"found", "rows", "total", "by_owner", "queries", "notes"}: `found` False when the tool has no data for this
        cluster; `rows` the newest kills, each KilledAt, Namespace, Pod, Container, Node, OwnerKind, Owner, MemBytes,
        LimitBytes, NodeMemBytes, NodeAllocBytes, Kernel, SystemOom (`aks.oom_rows` turns them into the page's rows);
        `total` every kill in the look-back and `by_owner` all of them counted per [{Namespace, OwnerKind, Owner, Pods,
        Kills}], so a list cut for length never hides how many there were. A read that hit the tool's own limit is in
        `notes`."""
        raise ProviderError(f"{self.label or 'This tool'} can't give a cluster's OOM kills here yet.")

    # A Kubernetes cluster's Warning events (WARNING_EVENTS in `capabilities`): kept longer than the cluster's own hour.
    def warning_events(self, cluster_name, hours):
        """{"found", "rows", "total", "queries", "notes"}: one row per reason and object, shaped like Container
        Insights' (Reason, Namespace, Object, ObjectKind, Count, Last, Message), most first; `total` every row before
        the list was cut for length. A read that hit the tool's own limit is in `notes`."""
        raise ProviderError(f"{self.label or 'This tool'} can't give a cluster's warning events here yet.")

    # A namespace's Deployments, StatefulSets and DaemonSets, when the cluster can't be asked (WORKLOADS in `capabilities`).
    def workload_names(self, cluster_name, namespace):
        """{"found", "workloads": [{kind, name}], "queries"}: what ran in that namespace over the last hour."""
        raise ProviderError(f"{self.label or 'This tool'} can't list a cluster's workloads here yet.")

    # Workload details (REQUESTS): the apps that sent requests, to find the one that is a workload (its runtime memory).
    def app_names(self):
        """{"names", "queries"}: the apps that sent requests over the last day."""
        raise ProviderError(f"{self.label or 'This tool'} can't list apps here yet.")

    # Workload details (WORKLOAD_USAGE), for clusters without Container Insights.
    def workload_usage(self, cluster_name, namespace, kind, name):
        """{"found", "containers": [{container, cpu: {p95, p99, max_7d, max_30d}, memory: {…}}], "queries"}: per
        container, CPU in cores and memory (working set) in bytes over 7 days, and the 30-day max."""
        raise ProviderError(f"{self.label or 'This tool'} can't give a workload's CPU and memory over time here yet.")

    def workload_memory(self, cluster_name, namespace, kind, name):
        """{"found", "pods": {pod: {"points": [[iso, bytes]], "restarts": [[iso, n]], "oom": [iso]}}, "queries"}: each pod's
        working-set memory per hour over 7 days (its containers added up), with its restarts and OOM kills per hour."""
        raise ProviderError(f"{self.label or 'This tool'} can't give a workload's memory hour by hour here yet.")

    # Workloads → Memory & scaling (CLUSTER_WEEK): the week of every workload in a cluster.
    def cluster_week(self, cluster_name):
        """{"found", "workloads": [{namespace, workload, restarts, oom_pods, max_of_limit, avg_of_request}],
        "hpas": [{namespace, hpa, at_max_percent}], "queries"} over the last 7 days."""
        raise ProviderError(f"{self.label or 'This tool'} can't give a cluster's week here yet.")

    # Workload details (RUNTIME_MEMORY): what the app's runtime holds, against what the pod's memory shows.
    def runtime_memory(self, app):
        """{"found", "heap_used", "heap_max", "heap_committed", "non_heap_used", "process", "heap_daily": [[time, MB]],
        "queries"}: the last 15 minutes in MB, and heap used per day over 30 days."""
        raise ProviderError(f"{self.label or 'This tool'} can't give an app's runtime memory here yet.")


def whole_name(name, matches):
    """An entity named exactly like a resource is a name match even when the name is short: it's the tool's own label."""
    return [{**m, "confidence": "name"} if m["confidence"] == "possible" and m["name"].lower() == name.lower() else m
            for m in matches]


def match_entity(name, ids, index, by_id):
    return whole_name(name, match([name], ids, index, by_id, id_via="tag"))
