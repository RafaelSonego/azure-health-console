"""From one resource outward: what it references and what references it, up to N hops.

Relations come from resource IDs found inside properties, so this works on
types nobody wrote a rule for; type-specific rules only add edges."""
import json
import logging
from dataclasses import dataclass

from . import ids, taxonomy
from .arm import AccessDenied, ArmError, kql_list
from .model import UNKNOWN, Edge, Node

logger = logging.getLogger(__name__)

FETCH_BATCH = 100
RESOURCE_COLUMNS = ("id, name, type=tolower(type), kind, location, resourceGroup,"
                    " subscriptionId, properties, identity, sku, managedBy")


@dataclass
class DiscoveryOptions:
    auto: bool = False
    depth: int = 2
    max_nodes: int = 60
    inbound: bool = True
    inbound_limit: int = 25
    inbound_scope: str = "subscription"


class _Graph:
    def __init__(self, max_nodes):
        self.nodes = {}
        self.edges = []
        self.warnings = []
        self.max_nodes = max_nodes
        self._full_warned = False

    def add(self, node):
        if node.id in self.nodes:
            return True
        if len(self.nodes) >= self.max_nodes:
            if not self._full_warned:
                self.warnings.append(
                    f"Stopped at {self.max_nodes} resources. Raise max_nodes or "
                    f"lower the depth to see more."
                )
                self._full_warned = True
            return False
        self.nodes[node.id] = node
        return True

    def link(self, source, target, label):
        if source != target and source in self.nodes and target in self.nodes:
            self.edges.append(Edge(source=source, target=target, label=label))


def discover(client, root_id, options, progress=None):
    report = progress or (lambda message: None)
    graph = _Graph(options.max_nodes)
    root = ids.normalise(root_id)

    report("Reading the selected resource")
    rows = _fetch(client, {root})
    if root not in rows:
        graph.add(_unreadable(root, hop=0, direction="root"))
        graph.warnings.append(
            "The selected resource can't be read with this session. It may no longer exist, "
            "or it needs Reader on the resource, its resource group or subscription "
            "(activate it in PIM if you're eligible, then click Refresh access)."
        )
        return graph
    graph.add(_node(rows[root], hop=0, direction="root"))

    if options.inbound and options.depth > 0:
        report("Finding resources that point at it")
        _add_inbound(client, graph, graph.nodes[root], options)

    frontier = [root]
    for hop in range(1, options.depth + 1):
        report(f"Following references, hop {hop} of {options.depth}")
        frontier = _expand(client, graph, frontier, hop)
        if not frontier:
            break

    _dedupe(graph)
    return graph


def discover_group(client, subscription_id, resource_group, options, progress=None):
    """Every resource in one resource group, the links between them, and what they reference
    outside the group (one hop, not expanded further)."""
    report = progress or (lambda message: None)
    graph = _Graph(options.max_nodes)
    report("Listing the resource group's resources")
    rows = client.query(
        f"Resources | where resourceGroup =~ {json.dumps(resource_group)}"
        f" | where tolower(type) !in ({kql_list(taxonomy.NOISE_TYPES)})"
        f" | project {RESOURCE_COLUMNS}", subscriptions=[subscription_id])
    rows.sort(key=lambda r: (taxonomy.display_name(r.get("type"), r.get("kind")), r.get("name") or ""))
    for row in rows:
        graph.add(_node(row, hop=0, direction="group"))
    if not rows:
        graph.warnings.append("No resources in this resource group are readable with this session.")
        return graph
    report("Linking resources")
    members = [node_id for node_id, node in graph.nodes.items() if node.hop == 0]
    _expand(client, graph, members, hop=1, follow_leaves=True)
    _dedupe(graph)
    return graph


def _expand(client, graph, frontier, hop, follow_leaves=False):
    referenced = {}
    for source_id in frontier:
        source = graph.nodes[source_id]
        if not source.readable or (source.hop > 0 and taxonomy.is_leaf(source.type) and not follow_leaves):
            continue
        for candidate in _referenced_ids(source.properties):
            referenced.setdefault(candidate, set()).add(source_id)

    resolved, rows = _resolve(client, set(referenced))
    added = []
    for candidate, sources in sorted(referenced.items()):
        target = resolved.get(candidate)
        if target is None:
            target = ids.top_level(candidate)
            if taxonomy.is_noise(ids.resource_type_of(target)):
                continue
            node = _unreadable(target, hop=hop, direction="outbound")
        elif target in graph.nodes:
            node = graph.nodes[target]
        else:
            row = rows[target]
            if taxonomy.is_noise(row.get("type")):
                continue
            node = _node(row, hop=hop, direction="outbound")
        is_new = node.id not in graph.nodes
        if not graph.add(node):
            continue
        if is_new:
            added.append(node.id)
        for source_id in sources:
            graph.link(source_id, node.id, _label(graph.nodes[source_id], node))

    for source_id in frontier:
        for row, label in _extra_relations(client, graph.nodes[source_id]):
            node = _node(row, hop=hop, direction="outbound")
            is_new = node.id not in graph.nodes
            if graph.add(node):
                if is_new:
                    added.append(node.id)
                graph.link(source_id, node.id, label)
    return added


def _add_inbound(client, graph, root, options):
    """Resources whose properties mention the root, e.g. a private endpoint or an App Gateway."""
    scope = [root.subscription_id] if options.inbound_scope == "subscription" else None
    try:
        rows = client.query(
            f"Resources | where tostring(properties) contains {json.dumps(root.id)}"
            f" | where tolower(type) !in ({kql_list(taxonomy.NOISE_TYPES)})"
            f" | project {RESOURCE_COLUMNS} | take {options.inbound_limit + 1}",
            subscriptions=scope,
        )
    except ArmError as error:
        graph.warnings.append(f"Couldn't search for resources pointing at it: {error}")
        return

    matched = 0
    for row in rows:
        row_id = ids.normalise(row.get("id"))
        if row_id == root.id or matched >= options.inbound_limit or taxonomy.is_noise(row.get("type")):
            continue
        # `contains` is a substring match, so ".../app" also matches ".../app-2".
        refs = {ids.normalise(candidate) for candidate in _referenced_ids(row)}
        if not any(ref == root.id or ref.startswith(root.id + "/") for ref in refs):
            continue
        node = _node(row, hop=1, direction="inbound")
        if graph.add(node):
            matched += 1
            graph.link(node.id, root.id, _label(node, root))
    if len(rows) > options.inbound_limit:
        graph.warnings.append(
            f"More than {options.inbound_limit} resources point at the selected "
            f"one; only the first {options.inbound_limit} are shown."
        )


def _extra_relations(client, node):
    """Links that aren't a resource ID in properties. Add new type rules here."""
    if not node.readable:
        return []
    if node.type == "microsoft.containerservice/managedclusters":
        node_rg = (node.properties.get("properties") or {}).get("nodeResourceGroup")
        if not node_rg:
            return []
        try:
            rows = client.query(
                f"Resources | where resourceGroup =~ {json.dumps(node_rg)}"
                " | where tolower(type) in ('microsoft.compute/virtualmachinescalesets',"
                " 'microsoft.network/loadbalancers')"
                f" | project {RESOURCE_COLUMNS}",
                subscriptions=[node.subscription_id],
            )
        except ArmError:
            return []
        labels = {"microsoft.compute/virtualmachinescalesets": "node pool",
                  "microsoft.network/loadbalancers": "load balancer"}
        return [(row, labels.get(row.get("type"), "uses")) for row in rows]
    return []


# ------------------------------------------------------------------ fetching


def _fetch(client, resource_ids):
    """Full rows (with properties) from Resource Graph, keyed by normalised ID."""
    wanted = sorted({ids.normalise(rid) for rid in resource_ids})
    rows = {}
    for start in range(0, len(wanted), FETCH_BATCH):
        batch = wanted[start:start + FETCH_BATCH]
        try:
            result = client.query(
                f"Resources | where tolower(id) in ({kql_list(batch)})"
                f" | project {RESOURCE_COLUMNS}"
            )
        except AccessDenied:
            continue
        for row in result:
            rows[ids.normalise(row.get("id"))] = row
    return rows


def _resolve(client, candidates):
    """Map each referenced ID to the deepest resource Resource Graph knows, so a
    subnet reference lands on its VNet. None means it couldn't be read at all."""
    pending = {candidate: ids.normalise(candidate) for candidate in candidates}
    resolved, rows = {}, {}
    while pending:
        rows.update(_fetch(client, set(pending.values()) - set(rows)))
        next_pending = {}
        for original, current in pending.items():
            if current in rows:
                resolved[original] = current
                continue
            parent = ids.parent_of(current)
            if parent:
                next_pending[original] = parent
            else:
                resolved[original] = None
        pending = next_pending
    return resolved, rows


def _walk(value, found):
    """IDs hide in values and in dict keys (how user-assigned identities are encoded)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.update(ids.RESOURCE_ID_PATTERN.findall(key))
            _walk(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk(item, found)
    elif isinstance(value, str):
        found.update(ids.RESOURCE_ID_PATTERN.findall(value))


def _referenced_ids(row):
    found = set()
    for key in ("properties", "identity", "managedBy"):
        _walk(row.get(key), found)
    own = ids.normalise(row.get("id"))
    return {ids.normalise(candidate) for candidate in found} - {own}


# --------------------------------------------------------------------- nodes


def _node(row, hop, direction):
    resource_id = ids.normalise(row.get("id"))
    resource_type = (row.get("type") or ids.resource_type_of(resource_id)).lower()
    return Node(
        id=resource_id,
        name=row.get("name") or ids.name_of(resource_id),
        type=resource_type,
        display_type=taxonomy.display_name(resource_type, row.get("kind")),
        hop=hop,
        direction=direction,
        kind=row.get("kind") or "",
        location=row.get("location") or "",
        subscription_id=(row.get("subscriptionId") or ids.subscription_of(resource_id)).lower(),
        resource_group=row.get("resourceGroup") or ids.resource_group_of(resource_id),
        properties=row,
    )


def _unreadable(resource_id, hop, direction):
    resource_type = ids.resource_type_of(resource_id)
    return Node(
        id=resource_id,
        name=ids.name_of(resource_id),
        type=resource_type,
        display_type=taxonomy.display_name(resource_type),
        hop=hop,
        direction=direction,
        subscription_id=ids.subscription_of(resource_id),
        resource_group=ids.resource_group_of(resource_id),
        readable=False,
        status=UNKNOWN,
    )


def _label(source, target):
    source_type, target_type = source.type, target.type
    if source_type == "microsoft.web/sites" and target_type == "microsoft.web/serverfarms":
        return "runs on"
    if source_type == "microsoft.network/privateendpoints":
        return "private link to"
    if source_type == "microsoft.insights/autoscalesettings":
        return "scales"
    if "managedidentity" in target_type:
        return "authenticates as"
    if "keyvault" in target_type:
        return "uses vault"
    if "insights" in target_type or "operationalinsights" in target_type:
        return "sends telemetry to"
    if target_type == "microsoft.network/virtualnetworks":
        return "in network"
    if "microsoft.network/" in target_type:
        return "uses"
    return "references"


def _dedupe(graph):
    seen = set()
    kept = []
    for edge in graph.edges:
        key = (edge.source, edge.target)
        if key in seen:
            continue
        seen.add(key)
        kept.append(edge)
    graph.edges = kept
