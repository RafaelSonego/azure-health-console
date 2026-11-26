"""What a snapshot is made of. Plain dataclasses, so any front end or report can render them."""
from dataclasses import asdict, dataclass, field, fields

from . import diagram

CRITICAL = "critical"
WARNING = "warning"
HEALTHY = "healthy"
UNKNOWN = "unknown"
INFO = "info"

_RANK = {CRITICAL: 3, WARNING: 2, HEALTHY: 1}


def worst(statuses):
    """The worst *reported* status. INFO never counts; nothing reported is UNKNOWN."""
    ranked = [status for status in statuses if status in _RANK]
    return max(ranked, key=_RANK.get) if ranked else UNKNOWN


def red_or_yellow(statuses, otherwise=None):
    """CRITICAL if any is, else WARNING if any is, else `otherwise`: a card's or section's colour from its items."""
    statuses = set(statuses)
    return CRITICAL if CRITICAL in statuses else WARNING if WARNING in statuses else otherwise


@dataclass
class Signal:
    source: str
    name: str
    status: str
    detail: str = ""
    value: float | None = None
    unit: str = ""
    threshold: str = ""
    baseline: float | None = None
    at: str = ""  # when it happened (a fired alert's latest start), for the page to show as a time
    links: list = field(default_factory=list)  # alert_detail.links: where to look at it
    label: str = ""  # Azure's display name for a metric ("Overall Vault Saturation" for SaturationShoebox)


@dataclass
class Node:
    id: str
    name: str
    type: str
    display_type: str
    hop: int
    direction: str = "outbound"
    kind: str = ""
    location: str = ""
    subscription_id: str = ""
    resource_group: str = ""
    readable: bool = True
    status: str = UNKNOWN
    signals: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    roles: list = field(default_factory=list)  # roles held on whole groups or subscriptions: {role, scope, scope_kind, scope_name}
    properties: dict = field(default_factory=dict, repr=False)

    def to_dict(self):
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "properties"}
        data["signals"] = [asdict(signal) for signal in self.signals]
        return data

    def problems(self):
        return [s for s in self.signals if s.status in (CRITICAL, WARNING)]


@dataclass
class Edge:
    source: str
    target: str
    label: str
    kind: str = ""  # "role": a role assignment (who can reach what); "part": the target is part of the source


@dataclass
class Snapshot:
    root_id: str
    tenant_id: str
    taken_by: str
    taken_at: str
    depth: int
    nodes: list
    edges: list
    warnings: list = field(default_factory=list)
    service_health: list = field(default_factory=list)
    arm_calls: int = 0
    kind: str = "resource"
    title: str = ""
    done: bool = True
    baseline: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)  # signal sources still running ("detectors") while done is False

    @property
    def root(self):
        return next((node for node in self.nodes if node.id == self.root_id), None)

    @property
    def status(self):
        return worst(node.status for node in self.nodes)

    def counts(self):
        totals = {CRITICAL: 0, WARNING: 0, HEALTHY: 0, UNKNOWN: 0}
        for node in self.nodes:
            totals[node.status] = totals.get(node.status, 0) + 1
        return totals

    def to_dict(self):
        nodes, edges = [node.to_dict() for node in self.nodes], [asdict(edge) for edge in self.edges]
        return {
            "root_id": self.root_id,
            "tenant_id": self.tenant_id,
            "taken_by": self.taken_by,
            "taken_at": self.taken_at,
            "depth": self.depth,
            "status": self.status,
            "counts": self.counts(),
            "nodes": nodes,
            "edges": edges,
            "graph": diagram.fold(nodes, edges, self.root_id),
            "warnings": self.warnings,
            "service_health": self.service_health,
            "arm_calls": self.arm_calls,
            "kind": self.kind,
            "title": self.title,
            "done": self.done,
            "pending": list(self.pending),
            "baseline": self.baseline,
        }
