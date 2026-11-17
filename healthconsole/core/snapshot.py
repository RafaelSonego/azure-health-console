"""One call from a resource ID (or a resource group) to a finished, renderable snapshot."""
from datetime import datetime, timezone

from . import baseline, discovery, health
from .model import Snapshot


def build(client, root_id, discovery_options, health_options, profiles, progress=None, publish=None):
    """A snapshot centred on one resource. `publish(snapshot)` receives it as soon as the graph
    is known, and its nodes fill in while health runs; the returned snapshot is complete."""
    calls_before = client.call_count
    graph = discovery.discover(client, root_id, discovery_options, progress)
    nodes = sorted(graph.nodes.values(), key=lambda n: (n.hop, n.direction, n.display_type, n.name))
    root = nodes[0].id if nodes and nodes[0].hop == 0 else root_id.lower()
    return _finish(client, graph, nodes, root, "resource", "", discovery_options.depth, health_options,
                   profiles, progress, publish, calls_before)


def build_group(client, subscription_id, resource_group, discovery_options, health_options, profiles,
                progress=None, publish=None):
    """A snapshot of one whole resource group."""
    calls_before = client.call_count
    graph = discovery.discover_group(client, subscription_id, resource_group, discovery_options, progress)
    nodes = sorted(graph.nodes.values(), key=lambda n: (n.hop, n.display_type, n.name))
    group_id = f"/subscriptions/{subscription_id}/resourcegroups/{resource_group}".lower()
    return _finish(client, graph, nodes, group_id, "group", f"Resource group {resource_group}", 1,
                   health_options, profiles, progress, publish, calls_before)


def _finish(client, graph, nodes, root_id, kind, title, depth, health_options, profiles, progress, publish,
            calls_before):
    identity = client.identity
    snap = Snapshot(
        root_id=root_id, tenant_id=identity.tenant_id, taken_by=identity.name,
        taken_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        depth=depth, nodes=nodes, edges=graph.edges, warnings=list(graph.warnings),
        kind=kind, title=title, done=False,
    )
    if publish:
        publish(snap)

    def quick():
        if publish:
            snap.pending = ["detectors"]
            publish(snap)
    health_warnings, service_health = health.assess(client, nodes, health_options, profiles, progress, on_quick=quick)
    snap.pending = []
    snap.warnings += health_warnings
    snap.service_health = service_health
    if kind == "group" and health_options.compare_last_week:
        snap.baseline = baseline.compare(nodes, health_options.window_minutes)
    snap.arm_calls = client.call_count - calls_before
    snap.done = True
    return snap
