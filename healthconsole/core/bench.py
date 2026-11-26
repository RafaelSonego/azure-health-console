"""`azure-health-console bench`: times what the page does, against the signed-in tenant, read-only.
Each scenario starts with an empty cache, so the numbers are what a first click costs."""
import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import checks, config, inventory, network, search, snapshot, triage
from .arm import ArmError
from .discovery import DiscoveryOptions
from .health import HealthOptions

SCENARIOS = ("triage", "search", "resource", "resource-graph", "group", "aks-fleet", "network", "secrets")
DEFAULT_SCENARIOS = tuple(s for s in SCENARIOS if s != "secrets")  # secrets lists vault metadata: opt in
_PICK_TYPES = ("microsoft.containerservice/managedclusters", "microsoft.web/sites", "microsoft.sql/servers/databases",
               "microsoft.cache/redis", "microsoft.storage/storageaccounts")


class _Recorder(logging.Handler):
    """Collects (path, status, ms) from the `healthconsole.calls` log lines."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.calls = []
        self._lock = threading.Lock()

    def emit(self, record):
        if len(record.args or ()) == 5:
            method, path, _version, status, ms = record.args
            with self._lock:
                self.calls.append((method, path, str(status), float(ms)))

    def take(self):
        with self._lock:
            taken, self.calls = self.calls, []
        return taken


def endpoint(path):
    """A call's path without names or IDs: what kind of call it was, for grouping."""
    lowered = path.lower()
    if "/providers/" not in lowered:
        return re.sub(r"/subscriptions/[^/]+", "/subscriptions/*", lowered)
    tail = lowered.rsplit("/providers/", 1)[1].split("/")
    kept = tail[:2] + [segment for index, segment in enumerate(tail[2:]) if index % 2 == 1]
    return "/".join(kept)


def _pick(client, subscriptions, wanted_id=None):
    if wanted_id:
        rows = client.query(f"Resources | where tolower(id) == {json.dumps(wanted_id.lower())} | project id, name, resourceGroup,"
                            " subscriptionId, type")
        return rows[0] if rows else None
    for resource_type in _PICK_TYPES:
        rows = client.query(f"Resources | where type =~ '{resource_type}' | project id, name, resourceGroup,"
                            " subscriptionId, type | take 1", subscriptions=subscriptions)
        if rows:
            return rows[0]
    return None


def run(client, settings, tenant, scenarios=DEFAULT_SCENARIOS, resource_id=None, say=print):
    """Runs each scenario once and returns {"rows", "endpoints", "slowest", ...} for `to_markdown`."""
    recorder = _Recorder()
    calls_log = logging.getLogger("healthconsole.calls")
    previous_level = calls_log.level
    calls_log.setLevel(logging.INFO)
    calls_log.addHandler(recorder)
    try:
        subs = inventory.list_subscriptions(client)
        try:
            label, scope = config.chosen_subscriptions(settings, tenant, subs)
        except config.ConfigError:
            label, scope = "", []
        if not scope:
            label, scope = "every subscription", [s["id"] for s in subs]
        target = _pick(client, scope, resource_id)
        say(f"Scope: {len(scope)} subscription(s), {label}; "
            f"resource: {target['name'] if target else 'none found'}")
        health_options = HealthOptions(**settings["health"], aks_leftover_hours=settings["aks"]["leftover_hours"])
        graph = settings["graph"]
        net = settings["network"]
        work = {
            "triage": lambda: triage.triage(client, scope, {**settings["health"], **settings["triage"], **net}),
            "search": lambda: search.search(client, (target or {}).get("name", "app")[:4], subscriptions=scope),
            "aks-fleet": lambda: checks.aks_fleet(client, scope),
            "network": lambda: network.overview(client, scope, None, net["subnet_warning_percent"],
                                                net["subnet_critical_percent"]),
            "secrets": lambda: checks.keyvault_scan(client, scope, settings["health"]["expiry_warning_days"],
                                                    critical_days=settings["health"]["expiry_critical_days"]),
        }
        if target:
            profiles_ = {}
            work["resource"] = lambda: snapshot.build(
                client, target["id"], DiscoveryOptions(**{**graph, "depth": 0, "inbound": False}), health_options, profiles_)
            work["resource-graph"] = lambda: snapshot.build(
                client, target["id"], DiscoveryOptions(**graph), health_options, profiles_)
            work["group"] = lambda: snapshot.build_group(
                client, target["subscriptionId"], target["resourceGroup"], DiscoveryOptions(**graph), health_options,
                profiles_)
        recorder.take()
        rows, all_calls = [], []
        for name in scenarios:
            if name not in work:
                rows.append({"scenario": name, "skipped": "no resource to check in this scope"})
                continue
            client._cache.clear()
            say(f"  {name}…")
            started = time.monotonic()
            error = None
            try:
                result = work[name]()
            except ArmError as failure:
                result, error = None, str(failure).splitlines()[0]
            seconds = time.monotonic() - started
            calls = recorder.take()
            all_calls += [(name, *call) for call in calls]
            size = len(getattr(result, "nodes", None) or []) if result is not None else 0
            rows.append({"scenario": name, "seconds": round(seconds, 1), "calls": len(calls),
                         "call_seconds": round(sum(c[3] for c in calls) / 1000, 1),
                         "slowest_ms": int(max((c[3] for c in calls), default=0)),
                         "errors": sum(1 for c in calls if not c[2].startswith("2")),
                         "resources": size, "error": error})
    finally:
        calls_log.removeHandler(recorder)
        calls_log.setLevel(previous_level)

    grouped = defaultdict(lambda: {"calls": 0, "ms": 0.0, "max": 0.0, "failed": 0})
    for _scenario, method, path, status, ms in all_calls:
        entry = grouped[f"{method} {endpoint(path)}"]
        entry["calls"] += 1
        entry["ms"] += ms
        entry["max"] = max(entry["max"], ms)
        entry["failed"] += 0 if status.startswith("2") else 1
    endpoints = sorted(({"endpoint": k, **v} for k, v in grouped.items()), key=lambda e: -e["ms"])
    slowest = sorted(all_calls, key=lambda c: -c[4])[:15]
    return {"rows": rows, "endpoints": endpoints[:20], "slowest": slowest, "target": target,
            "subscriptions": len(scope), "taken_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


def to_markdown(result):
    lines = ["# Azure Health Console benchmark", "",
             f"Taken {result['taken_at']} across {result['subscriptions']} subscription(s); resource "
             f"`{(result['target'] or {}).get('id', 'none')}`. Each scenario starts with an empty cache. "
             "Call time adds up calls that ran in parallel, so it can exceed wall time.", "",
             "| Scenario | Wall time (s) | Azure calls | Call time (s) | Slowest call (ms) | Failed calls | Resources | Note |",
             "|---|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        if r.get("skipped"):
            lines.append(f"| {r['scenario']} | – | – | – | – | – | – | skipped: {r['skipped']} |")
            continue
        lines.append(f"| {r['scenario']} | {r['seconds']} | {r['calls']} | {r['call_seconds']} | {r['slowest_ms']} | "
                     f"{r['errors']} | {r['resources'] or ''} | {r['error'] or ''} |")
    lines += ["", "## Where the time goes (by kind of call)", "",
              "| Call | Count | Total (s) | Average (ms) | Slowest (ms) | Failed |", "|---|---|---|---|---|---|"]
    for e in result["endpoints"]:
        lines.append(f"| `{e['endpoint']}` | {e['calls']} | {e['ms'] / 1000:.1f} | {e['ms'] / e['calls']:.0f} | "
                     f"{e['max']:.0f} | {e['failed']} |")
    lines += ["", "## Slowest single calls", "", "| Scenario | Call | Status | ms |", "|---|---|---|---|"]
    for scenario, method, path, status, ms in result["slowest"]:
        lines.append(f"| {scenario} | `{method} {endpoint(path)}` | {status} | {ms:.0f} |")
    return "\n".join(lines) + "\n"
