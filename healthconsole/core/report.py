"""Snapshot → Markdown (Mermaid graph), PDF (fpdf2, pure Python) or a printable HTML page.
Exports return bytes; the front end decides whether to save or download them."""
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import alert_detail, baseline, diagram
from .model import CRITICAL, HEALTHY, INFO, UNKNOWN, WARNING

TEMPLATES = Path(__file__).resolve().parent.parent / "report_templates"
FORMATS = ("md", "pdf")

STATUS_COLOURS = {
    CRITICAL: ("#fde2e1", "#b42318"),
    WARNING: ("#fff3d6", "#9a6700"),
    HEALTHY: ("#e3f5e8", "#1a7f37"),
    "soon": ("#ffe2cc", "#a33f00"),  # orange: critical but not yet expired (Secrets)
    UNKNOWN: ("#eef0f3", "#57606a"),
    INFO: ("#eef0f3", "#57606a"),
}
STATUS_ICONS = {CRITICAL: "🔴", WARNING: "🟠", HEALTHY: "🟢", UNKNOWN: "⚪", INFO: "⚪"}


def filename(snapshot, fmt):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", title(snapshot))
    stamp = re.sub(r"[^0-9]", "", snapshot.taken_at)[:12]
    return f"{stem}-{stamp}.{fmt}"


def render(snapshot, fmt, font_path=None):
    if fmt == "md":
        return to_markdown(snapshot).encode("utf-8")
    if fmt == "pdf":
        return to_pdf(snapshot, font_path)
    raise ValueError(f"Unknown format '{fmt}'; choose one of {FORMATS}.")


def title(snapshot):
    root = snapshot.root
    return snapshot.title or (root.name if root else snapshot.root_id.rsplit("/", 1)[-1])


def signal_detail(signal):
    """A signal's detail, led by when it happened (a fired alert's time)."""
    return " · ".join(filter(None, [alert_detail.when(signal.at) if signal.at else "", signal.detail]))


def summary(node):
    problems = node.problems()
    if problems:
        return "; ".join(f"{s.name}: {s.detail or s.status}" for s in problems[:3])
    if not node.readable:
        return node.signals[0].detail if node.signals else "Not readable with this session"
    return ""


def _fmt(value):
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _cell(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ")


# ------------------------------------------------------------------ markdown


def _mermaid(snapshot):
    keys = {node.id: f"n{index}" for index, node in enumerate(snapshot.nodes)}
    lines = ["```mermaid", "flowchart LR"]
    for node in snapshot.nodes:
        label = f"{node.name}<br/><small>{node.display_type}</small>".replace('"', "#quot;")
        lines.append(f'    {keys[node.id]}["{label}"]:::{node.status}')
    for edge in snapshot.edges:
        lines.append(f"    {keys[edge.source]} -->|{edge.label}| {keys[edge.target]}")
    for status, (fill, stroke) in STATUS_COLOURS.items():
        lines.append(f"    classDef {status} fill:{fill},stroke:{stroke},color:#1f2328")
    lines.append("```")
    return "\n".join(lines)


def to_markdown(snapshot):
    counts = snapshot.counts()
    out = [
        f"# Health snapshot: {title(snapshot)}",
        "",
        f"- **Overall:** {STATUS_ICONS[snapshot.status]} {snapshot.status}",
        f"- **Resources:** {len(snapshot.nodes)} "
        f"({counts[CRITICAL]} critical, {counts[WARNING]} warning, "
        f"{counts[HEALTHY]} healthy, {counts[UNKNOWN]} unknown)",
        f"- **Taken:** {snapshot.taken_at} by {snapshot.taken_by} (tenant {snapshot.tenant_id})",
        f"- **Selected:** `{snapshot.root_id}`",
        f"- **Depth:** {snapshot.depth} hops",
        "",
    ]
    if snapshot.service_health:
        out += ["## Active Service Health events", ""]
        for event in snapshot.service_health:
            many = f", {len(event['tracking_ids'])} events" if len(event.get("tracking_ids", [])) > 1 else ""
            out.append(f"- **{event['type']}**: {event['title']}{many} "
                       f"({', '.join(event['services'])}; {', '.join(event['regions']) or 'all regions'})")
        out.append("")
    if snapshot.baseline.get("movers"):
        b = snapshot.baseline
        out += [f"## Against last week (same {b['window_minutes']} minutes, a week earlier)", "",
                "| Resource | Metric | Now | Last week | Change |", "|---|---|---|---|---|"]
        out += [f"| {_cell(m['name'])} | {_cell(m.get('label') or m['signal'])} | {_fmt(m['value'])} {m['unit']} | {_fmt(m['baseline'])} | "
                f"{baseline.describe(m)} |" for m in b["movers"]]
        out.append("")
    out += ["## Dependency graph", "", _mermaid(snapshot), "",
            "## Resources", "",
            "| Status | Name | Type | Resource group | Why |",
            "|---|---|---|---|---|"]
    for node in snapshot.nodes:
        out.append(f"| {STATUS_ICONS[node.status]} {node.status} | {_cell(node.name)} | "
                   f"{_cell(node.display_type)} | {_cell(node.resource_group)} | {_cell(summary(node))} |")
    out.append("")

    for node in snapshot.nodes:
        out += [f"### {STATUS_ICONS[node.status]} {node.name}", "",
                f"{node.display_type} · `{node.id}`", ""]
        if node.signals:
            out += ["| Signal | Source | Status | Value | Threshold | Last week | Detail |",
                    "|---|---|---|---|---|---|---|"]
            for s in node.signals:
                value = f"{_fmt(s.value)} {s.unit}".strip() if s.value is not None else "–"
                out.append(f"| {_cell(s.name)} | {s.source} | {s.status} | {_cell(value)} | "
                           f"{_cell(s.threshold)} | {_fmt(s.baseline)} | {_cell(signal_detail(s))} |")
            out.append("")
        if node.changes:
            out += ["**Recent changes:**", ""]
            for change in node.changes[:10]:
                fields = ", ".join(change["fields"])
                out.append(f"- {change['at']}: {change['type']} by {change['by'] or 'unknown'}"
                           + (f" ({fields})" if fields else ""))
            out.append("")
    if snapshot.warnings:
        out += ["## Notes", ""] + [f"- {warning}" for warning in snapshot.warnings] + [""]
    out.append(f"_{snapshot.arm_calls} Azure calls; read-only._")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- html/pdf

BOX_W, BOX_H, GAP_X, GAP_Y, PAD = 190, 46, 70, 14, 10


GROUP_ROWS = 8


def _column(node):
    if node.direction == "inbound":
        return 0
    if node.direction == "root":
        return 1
    return 1 + node.hop


def _group_columns(snapshot):
    """A resource group has no centre: fill columns of GROUP_ROWS in type order, outside references last."""
    members = [n for n in snapshot.nodes if n.hop == 0]
    outside = [n for n in snapshot.nodes if n.hop > 0]
    columns = {}
    for index, node in enumerate(members):
        columns.setdefault(index // GROUP_ROWS, []).append(node)
    base = len(columns)
    for index, node in enumerate(outside):
        columns.setdefault(base + index // GROUP_ROWS, []).append(node)
    return columns


def layout(snapshot):
    """Layered: inbound on the left, the selected resource, then each hop out (a resource group
    fills columns instead). Returns ({node_id: (x, y)}, width, height) in units shared by SVG and PDF."""
    if snapshot.kind == "group":
        columns = _group_columns(snapshot)
    else:
        columns = {}
        for node in snapshot.nodes:
            columns.setdefault(_column(node), []).append(node)
    used = sorted(columns)
    position = {}
    for col_index, col in enumerate(used):
        for row_index, node in enumerate(columns[col]):
            position[node.id] = (PAD + col_index * (BOX_W + GAP_X),
                                 PAD + row_index * (BOX_H + GAP_Y))
    width = PAD * 2 + len(used) * BOX_W + (len(used) - 1) * GAP_X
    height = PAD * 2 + max((len(c) for c in columns.values()), default=0) * (BOX_H + GAP_Y)
    return position, width, height


def to_html(snapshot, printable=False):
    environment = Environment(loader=FileSystemLoader(TEMPLATES),
                              autoescape=select_autoescape(["html"]))
    environment.filters["num"] = _fmt
    return environment.get_template("report.html").render(
        s=snapshot, counts=snapshot.counts(), svg=diagram.render_svg(snapshot.to_dict()), theme=diagram.theme_css(light_only=True),
        summary=summary, colours=STATUS_COLOURS, printable=printable, title=title(snapshot),
    )


def to_pdf(snapshot, font_path=None):
    from . import pdf  # imported here: pdf.py reuses this module's layout and colours

    return pdf.render(snapshot, font_path)
